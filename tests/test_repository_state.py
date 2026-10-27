"""Real Git fixtures for the bounded cross-ref native-state projection."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from eutonos import repository_state


class RepositoryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-repo-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.git(self.root, "init", "-q", "-b", "main")
        self.items = [self.item("AWR", "campaign", "active"),
                      self.item("HIST", "campaign", "completed", imported=True),
                      self.item("FOO", "slice", "planned")]
        self.write_board(self.root, self.items)
        self.commit(self.root)
        self.base = self.git(self.root, "rev-parse", "HEAD")

    @staticmethod
    def item(item_id, kind, status, imported=False):
        row = {"id": item_id, "type": kind, "title": item_id + " title",
               "summary": "Fixture work", "status": status}
        if imported:
            row["extensions"] = {"source_import": {"disposition": "historical"}}
        return row

    def git(self, root, *args):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
                   GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
        return subprocess.check_output(
            ["git", "-c", "safe.directory=" + str(root), "-c", "core.autocrlf=false",
             "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-C", str(root), *args], env=env, stderr=subprocess.PIPE).decode().strip()

    def commit(self, root):
        self.git(root, "add", "--all")
        self.git(root, "commit", "-qm", "fixture")
        return self.git(root, "rev-parse", "HEAD")

    def write_board(self, root, items, identity="fixture.repo"):
        (root / "docs").mkdir(exist_ok=True)
        (root / "eutonos.read.json").write_text(json.dumps({"repository_id": identity,
            "scope_id": "fixture.local", "namespace": "fixture.native"}), encoding="utf-8")
        (root / "eutonos.boards.json").write_text(json.dumps({
            "profile": "eutonos.tokenslang.boards.v1", "owners": ["docs/work.md"]}), encoding="utf-8")
        (root / "docs/work.md").write_text("# Work\n\n```tokenslang-board\n" + json.dumps({
            "profile": "eutonos.tokenslang.board.v1", "items": items}) + "\n```\n", encoding="utf-8")

    def scenario(self):
        main_items = [self.item("AWR", "campaign", "completed"),
                      self.item("HIST", "campaign", "completed", imported=True),
                      self.item("FOO", "slice", "active")]
        self.write_board(self.root, main_items)
        main = self.commit(self.root)
        self.git(self.root, "update-ref", "refs/remotes/origin/main", main)
        topic = Path(self.temp.name) / "topic"
        self.git(self.root, "worktree", "add", "-qb", "topic", str(topic), self.base)
        topic_items = [self.item("AWR", "campaign", "active"),
                       self.item("HIST", "campaign", "completed", imported=True),
                       self.item("FOO", "slice", "completed"),
                       self.item("TASK", "slice", "completed")]
        self.write_board(topic, topic_items)
        (topic / "new-source.py").write_text("value = 1\n", encoding="utf-8")
        tip = self.commit(topic)
        self.git(self.root, "update-ref", "refs/heads/same-topic", tip)
        return topic, main, tip

    def test_main_beats_stale_worktree_and_branch_only_completion_remains_visible(self):
        topic, main, tip = self.scenario()
        before = (self.root / "docs/work.md").read_bytes()
        result = repository_state.project(self.root)
        self.assertEqual(result["main"]["commit"], main)
        self.assertEqual(result["main"]["ref"], "refs/remotes/origin/main")
        self.assertEqual(next(i for i in result["main"]["items"] if i["id"] == "AWR")["status"], "completed")
        self.assertEqual(next(i for i in result["latest"]["items"] if i["id"] == "AWR")["status"], "completed")
        task = next(i for i in result["latest"]["completed_unintegrated"] if i["id"] == "TASK")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["provenance"][0]["commit"], tip)
        self.assertIn("HIST", {i["id"] for i in result["latest"]["completed_unintegrated"]})
        self.assertEqual([i["id"] for i in result["latest"]["items"]].count("TASK"), 1)
        self.assertIn("FOO", {i["id"] for i in result["latest"]["conflicts"]})
        self.assertIn("new-source.py", {r["path"] for r in result["latest"]["unclassified_changes"]})
        self.assertEqual((self.root / "docs/work.md").read_bytes(), before)
        brief = repository_state.compact(result)
        self.assertEqual(next(i for i in brief["main"]["campaigns"] if i["id"] == "AWR")["status"], "completed")
        self.assertIn("TASK", {i["id"] for i in brief["latest"]["completed_unintegrated"]})

    def test_dirty_owner_has_worktree_provenance_and_exact_hash(self):
        topic, _, _ = self.scenario()
        items = [self.item("AWR", "campaign", "active"),
                 self.item("HIST", "campaign", "completed", imported=True),
                 self.item("FOO", "slice", "completed"),
                 self.item("TASK", "slice", "completed"),
                 self.item("DIRTY", "slice", "active")]
        self.write_board(topic, items)
        result = repository_state.project(self.root)
        dirty = next(i for i in result["latest"]["items"] if i["id"] == "DIRTY")
        self.assertEqual(dirty["provenance"][0]["source_kind"], "working-tree")
        self.assertEqual(dirty["owner_sha256"], hashlib.sha256(
            (topic / "docs/work.md").read_bytes()).hexdigest())

    def test_cached_age_requires_refresh_for_new_main_commit(self):
        first = repository_state.project(self.root)
        self.assertEqual(first["observation_freshness"]["kind"], "live")
        items = [self.item("AWR", "campaign", "completed"), *self.items[1:]]
        self.write_board(self.root, items)
        self.commit(self.root)
        cached = repository_state.project(self.root)
        self.assertEqual(cached["observation_freshness"]["kind"], "cached")
        self.assertEqual(cached["main"]["commit"], first["main"]["commit"])
        fresh = repository_state.project(self.root, refresh=True)
        self.assertNotEqual(fresh["main"]["commit"], first["main"]["commit"])
        self.assertEqual(next(i for i in fresh["main"]["items"] if i["id"] == "AWR")["status"], "completed")

    def test_crlf_only_dirty_owner_does_not_invent_native_change(self):
        committed = repository_state.project(self.root)
        path = self.root / "docs/work.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        result = repository_state.project(self.root, refresh=True)
        self.assertEqual(result["latest"]["conflicts"], [])
        self.assertEqual([(i["id"], i["record_sha256"]) for i in result["latest"]["items"]],
                         [(i["id"], i["record_sha256"]) for i in committed["latest"]["items"]])

    def test_older_active_campaign_checkpoint_is_historical_not_current_conflict(self):
        older = self.item("AWR", "campaign", "active")
        older["extensions"] = {"progress": {"percent": 38, "updated_on": "2026-09-23"}}
        self.write_board(self.root, [older, *self.items[1:]])
        self.commit(self.root)
        ancestor = self.git(self.root, "rev-parse", "HEAD")
        completed = self.item("AWR", "campaign", "completed")
        completed["extensions"] = {"progress": {"percent": 100, "updated_on": "2026-10-03"},
                                   "work_history": [{"status": "completed", "result": "accepted"}]}
        self.write_board(self.root, [completed, *self.items[1:]])
        self.commit(self.root)
        topic = Path(self.temp.name) / "stale"
        self.git(self.root, "worktree", "add", "-qb", "stale", str(topic), ancestor)
        changed = self.item("AWR", "campaign", "active")
        changed["extensions"] = {"progress": {"percent": 50, "updated_on": "2026-09-24"}}
        self.write_board(topic, [changed, *self.items[1:]])
        self.commit(topic)
        result = repository_state.project(self.root)
        self.assertEqual(next(i for i in result["latest"]["items"] if i["id"] == "AWR")["status"], "completed")
        self.assertNotIn("AWR", {i["id"] for i in result["latest"]["conflicts"]})
        self.assertIn("AWR", {i["id"] for i in result["latest"]["historical_candidates"]})

    def test_older_progress_with_changed_campaign_scope_stays_conflicted(self):
        older = self.item("AWR", "campaign", "active")
        older["extensions"] = {"progress": {"percent": 38, "updated_on": "2026-09-23"}}
        self.write_board(self.root, [older, *self.items[1:]])
        self.commit(self.root)
        ancestor = self.git(self.root, "rev-parse", "HEAD")
        completed = self.item("AWR", "campaign", "completed")
        completed["extensions"] = {"progress": {"percent": 100, "updated_on": "2026-10-03"},
                                   "work_history": [{"status": "completed"}]}
        self.write_board(self.root, [completed, *self.items[1:]])
        self.commit(self.root)
        topic = Path(self.temp.name) / "changed-scope"
        self.git(self.root, "worktree", "add", "-qb", "changed-scope", str(topic), ancestor)
        changed = self.item("AWR", "campaign", "active")
        changed["summary"] = "Newly expanded campaign scope"
        changed["extensions"] = {"progress": {"percent": 50, "updated_on": "2026-09-24"}}
        self.write_board(topic, [changed, *self.items[1:]])
        self.commit(topic)
        result = repository_state.project(self.root)
        self.assertIn("AWR", {i["id"] for i in result["latest"]["conflicts"]})
        self.assertNotIn("AWR", {i["id"] for i in result["latest"]["historical_candidates"]})

    def test_imported_completed_record_preserves_source_integration_boundary(self):
        source = Path(self.temp.name) / "source"
        self.git(self.root, "worktree", "add", "-qb", "source", str(source), self.base)
        (source / "source.py").write_text("source = True\n", encoding="utf-8")
        source_commit = self.commit(source)
        imported = self.item("HIST", "campaign", "completed", imported=True)
        imported["extensions"]["source_import"].update(
            source_id="HIST", source={"commit": source_commit, "path": "docs/work.md"})
        self.write_board(self.root, [self.items[0], imported, self.items[2]])
        self.commit(self.root)
        result = repository_state.project(self.root)
        item = next(i for i in result["main"]["items"] if i["id"] == "HIST")
        self.assertTrue(item["record_on_main"])
        self.assertEqual(item["source_integration"], "retained-unintegrated")
        self.assertIn("HIST", {i["id"] for i in result["latest"]["completed_unintegrated"]})

    def test_foreign_identity_and_ref_cap_are_explicitly_partial(self):
        self.scenario()
        foreign = Path(self.temp.name) / "foreign"
        self.git(self.root, "worktree", "add", "-qb", "foreign", str(foreign), self.base)
        self.write_board(foreign, self.items + [self.item("ALIEN", "slice", "completed")],
                         identity="another.repo")
        self.commit(foreign)
        capped = repository_state.project(self.root, max_refs=2)
        self.assertEqual(capped["status"], "partial")
        self.assertTrue(capped["coverage"]["refs_truncated"])
        result = repository_state.project(self.root)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("ALIEN", {i["id"] for i in result["latest"]["items"]})
        self.assertTrue(any("Foreign" in d for d in result["diagnostics"]))

    def test_missing_main_native_config_is_partial_unknown(self):
        (self.root / "eutonos.boards.json").unlink()
        self.commit(self.root)
        result = repository_state.project(self.root)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["main"]["status"], "partial")
        self.assertEqual(result["latest"]["items"], [])
        self.assertIn("Main native owners unavailable", result["diagnostics"][0])


if __name__ == "__main__":
    unittest.main()
