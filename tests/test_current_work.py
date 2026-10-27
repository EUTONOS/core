"""Source-backed shared Current Work and disposable-cache behavior."""
import json
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import current_work
from eutonos import repository_state
from eutonos.document_views import boards_view


class CurrentWorkTests(unittest.TestCase):
    def test_orientation_uses_real_native_projection_and_rechecks_source_on_resume(self):
        from eutonos import orientation, tokenslang as ts
        catalog = {"profile": ts.CATALOG, "repository_id": "fixture.repo", "scope_id": "fixture.local",
                   "namespace": "fixture.native", "owners": {"current_task": {
                       "scope_id": "fixture.local", "namespace": "fixture.native", "resource_id": "work"}},
                   "bindings": [{"resource_id": "work", "path": "docs/work.md", "kind": "docs",
                                 "label": "Work", "visibility": "public", "state": "active"}],
                   "excluded_roots": [], "source_paths": []}
        (self.root / "eutonos.read.json").write_bytes(ts.encoded(catalog))
        self.commit(self.root)
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                  if p.is_file() and ".git" not in p.parts}
        result = orientation.orient(self.root)
        self.assertEqual(result["outcome"], "orientation", result)
        self.assertEqual(result["selected_work_ids"], ["C"])
        self.assertEqual(result["selected_work"][0]["status"], "active")
        self.assertEqual(before, {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                                 if p.is_file() and ".git" not in p.parts})
        changed = deepcopy(self.base_items)
        changed[0]["next_action"] = "Changed native next action"
        (self.root / "docs/work.md").write_text("# Work\n\n```tokenslang-board\n" + json.dumps({
            "profile": "eutonos.tokenslang.board.v1", "items": changed}) + "\n```\n", encoding="utf-8")
        resumed = orientation.orient(self.root, {"mode": "resume", "receipt": result["receipt"]})
        self.assertEqual(resumed["selected_work"][0]["next_action"], "Changed native next action", resumed)
        self.assertIn("work:C", resumed["delta"]["changed_pins"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-current-work-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.home = Path(self.temp.name) / "device"
        patch = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.git(self.root, "init", "-q", "-b", "main")
        self.base_items = [self.item("C", "campaign", "active"),
                           self.item("S1", "stage", "planned", "C"),
                           self.item("S2", "stage", "planned", "C")]
        self.write(self.root, self.base_items)
        self.commit(self.root)
        self.base = self.git(self.root, "rev-parse", "HEAD")
        self.git(self.root, "update-ref", "refs/remotes/origin/main", self.base)

    @staticmethod
    def item(identity, kind, status, parent=None):
        item = {"id": identity, "type": kind, "title": identity + " title",
                "summary": identity + " source summary", "status": status,
                "next_action": "Inspect the exact native source"}
        if parent:
            item["parent_id"] = parent
        return item

    def test_compact_work_keeps_bounded_check_restart_details(self):
        checks = [{"id": f"proof-{n}", "title": f"Proof {n}", "required": True,
                   "state": "partial", "evidence": [], "related_work_ids": [],
                   "remaining_action": f"Finish proof {n}"} for n in range(10)]
        item = self.item("S1", "stage", "active", "C")
        item["extensions"] = {"completion_checks": checks}
        board = boards_view({"status": "available", "items": [item]})
        brief = current_work.compact({"status": "available", "view": "current",
                                      "boards": board})["current"][0]
        self.assertEqual(brief["completion_check_summary"]["unmet_required"], 10)
        self.assertEqual(brief["completion_checks"][0]["remaining_action"], "Finish proof 0")
        self.assertEqual(len(brief["completion_checks"]), 8)
        self.assertEqual(brief["completion_checks_omitted"], 2)

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

    def write(self, root, items):
        (root / "docs").mkdir(exist_ok=True)
        (root / "eutonos.read.json").write_text(json.dumps({"repository_id": "fixture.repo",
            "scope_id": "fixture.local", "namespace": "fixture.native"}), encoding="utf-8")
        (root / "eutonos.boards.json").write_text(json.dumps({"profile": "eutonos.tokenslang.boards.v1",
            "owners": ["docs/work.md"]}), encoding="utf-8")
        (root / "eutonos.work.json").write_text(json.dumps({"profile": "eutonos.work-order.v1",
            "current": ["C"], "next": []}), encoding="utf-8")
        (root / "docs/work.md").write_text("# Work\n\n```tokenslang-board\n" + json.dumps({
            "profile": "eutonos.tokenslang.board.v1", "items": items}) + "\n```\n", encoding="utf-8")

    def branch(self, name, changed):
        path = Path(self.temp.name) / name
        self.git(self.root, "worktree", "add", "-qb", name, str(path), self.base)
        items = [dict(item) for item in self.base_items]
        for item in items:
            if item["id"] in changed:
                item["status"] = changed[item["id"]]
        self.write(path, items)
        self.commit(path)
        return path

    def board(self, result):
        return {item["id"]: item for item in result["boards"]["items"]}

    def test_independent_stage_updates_compose_with_full_fields_and_order(self):
        self.branch("first", {"S1": "completed"})
        self.branch("second", {"S2": "completed"})
        result = current_work.project(self.root, refresh=True)
        items = self.board(result)
        self.assertEqual((items["S1"]["status"], items["S2"]["status"]),
                         ("completed", "completed"))
        self.assertEqual(items["C"]["child_counts"],
                         {"completed": 2, "total": 2, "conflicted": 0})
        self.assertEqual(items["S1"]["summary"], "S1 source summary")
        self.assertEqual(items["S1"]["next_action"], "Inspect the exact native source")
        self.assertEqual(items["S1"]["order"], 1)
        self.assertTrue(items["S1"]["shared_read_only"])
        self.assertIn("C", result["boards"]["work_order"]["current"])
        self.assertEqual(self.board(current_work.project(self.root, view="main"))["S1"]["status"], "planned")
        first = next(row for row in result["sources"] if row["ref"] == "refs/heads/first")
        self.assertEqual(self.board(current_work.project(self.root, view="source", source=first["id"]))["S1"]["status"],
                         "completed")
        self.assertEqual(self.board(current_work.project(self.root, view="checkout"))["S1"]["status"], "planned")

    def test_stage_rows_and_together_groups_project_with_current_row_and_admission(self):
        saved = {"profile": "eutonos.work-order.v1", "next": [],
                 "current": [{"together": ["S1", "C"]}, "S2"]}
        (self.root / "eutonos.work.json").write_text(json.dumps(saved), encoding="utf-8")
        self.commit(self.root)
        self.git(self.root, "update-ref", "refs/remotes/origin/main", self.git(self.root, "rev-parse", "HEAD"))
        result = current_work.project(self.root, refresh=True)
        order = result["boards"]["work_order"]
        self.assertEqual(order["rows"]["current"], [{"together": ["S1", "C"]}, "S2"])
        self.assertEqual(order["current"], ["C"])
        compact = current_work.compact(result)
        row = compact["current_row"]
        self.assertTrue(row["together"])
        self.assertEqual([(i["id"], i.get("campaign")) for i in row["items"]], [("S1", "C title"), ("C", None)])
        self.assertIn("no further per-stage approval", row["admission"])
        # An old string-only order has the same compact row shape with one plain item.
        (self.root / "eutonos.work.json").write_text(json.dumps({**saved, "current": ["C"]}), encoding="utf-8")
        self.commit(self.root)
        self.git(self.root, "update-ref", "refs/remotes/origin/main", self.git(self.root, "rev-parse", "HEAD"))
        plain = current_work.compact(current_work.project(self.root, refresh=True))["current_row"]
        self.assertEqual((plain["together"], [i["id"] for i in plain["items"]]), (False, ["C"]))

    def test_malformed_order_rows_fall_back_to_the_appended_order(self):
        (self.root / "eutonos.work.json").write_text(json.dumps({"profile": "eutonos.work-order.v1", "next": [],
            "current": [{"together": ["S1"]}]}), encoding="utf-8")
        self.commit(self.root)
        self.git(self.root, "update-ref", "refs/remotes/origin/main", self.git(self.root, "rev-parse", "HEAD"))
        order = current_work.project(self.root, refresh=True)["boards"]["work_order"]
        self.assertEqual(order["current"], ["C"])

    def test_divergent_same_stage_is_visible_as_conflict(self):
        self.branch("first", {"S1": "completed"})
        self.branch("second", {"S1": "blocked"})
        result = current_work.project(self.root, refresh=True)
        item = self.board(result)["S1"]
        self.assertEqual(item["status"], "conflict")
        self.assertEqual(item["candidate_statuses"], ["blocked", "completed"])
        self.assertEqual(len(item["conflict_candidates"]), 2)
        self.assertEqual(self.board(result)["C"]["child_counts"]["conflicted"], 1)
        self.assertIn("S1", {row["id"] for row in result["boards"]["conflicts"]})
        self.assertIn("S1", {row["id"] for row in current_work.compact(result)["conflicts"]})

    def test_shared_cache_invalidation_and_selected_checkout_rebinding(self):
        branch = self.branch("first", {"S1": "completed"})
        first = current_work.sources(self.root, refresh=True)
        cached = current_work.sources(branch)
        self.assertEqual(cached["freshness"]["kind"], "cached")
        self.assertEqual(next(row for row in cached["sources"] if row["is_checkout"])["path"], str(branch))
        self.assertEqual(next(row for row in first["sources"] if row["is_checkout"])["path"], str(self.root))
        self.assertTrue(current_work.invalidate(self.root))
        refreshed = current_work.sources(branch)
        self.assertEqual(refreshed["freshness"]["kind"], "live")

    def test_board_only_read_preserves_source_shape_without_knowledge_scan(self):
        with mock.patch("eutonos.current_work_knowledge.project") as knowledge:
            fast = current_work.project(self.root, refresh=True, include_knowledge=False)
            knowledge.assert_not_called()
        self.assertEqual(fast["boards"]["status"], "available")
        self.assertEqual(fast["view"], "current")
        self.assertTrue(fast["sources"])
        self.assertIn("storage", fast)
        self.assertNotIn("ram", fast)
        self.assertNotIn("documents", fast)
        with mock.patch("eutonos.current_work_knowledge.project", return_value={"status": "available", "ram": {"status": "available"}}) as knowledge:
            full = current_work.project(self.root)
            knowledge.assert_called_once()
        self.assertEqual(full["ram"]["status"], "available")

    def test_old_worktree_source_pin_refuses_changed_head(self):
        branch = self.branch("first", {"S1": "completed"})
        descriptor = next(row for row in current_work.sources(self.root, refresh=True)["sources"]
                          if row.get("path") == str(branch))
        self.write(branch, [*self.base_items, self.item("LATER", "slice", "active", "C")])
        self.commit(branch)
        with repository_state.SourceReader(self.root) as reader:
            with self.assertRaisesRegex(ValueError, "changed|stale"):
                reader.read(descriptor, "docs/work.md")
            with self.assertRaisesRegex(ValueError, "changed|stale"):
                reader.list(descriptor, "docs")
        with self.assertRaisesRegex(ValueError, "changed|stale"):
            repository_state.source_snapshot(self.root, descriptor)
        with mock.patch.object(current_work, "CACHE_SECONDS", 60):
            stale = current_work.project(self.root, view="source", source=descriptor["id"],
                                         include_knowledge=False)
        self.assertEqual(stale["boards"]["status"], "unavailable")
        self.assertEqual(stale["status"], "partial")
        self.assertTrue(any("HEAD changed" in line for line in stale["diagnostics"]))
        with self.assertRaisesRegex(ValueError, "Source ID is unavailable"):
            current_work.project(self.root, view="source", source=descriptor["id"],
                                 refresh=True, include_knowledge=False)

    def test_corrupt_cache_recovers_and_scan_failure_retains_stale_last_good(self):
        current_work.sources(self.root, refresh=True)
        cache = current_work._paths(self.root)[0]
        cache.write_bytes(b"{corrupt")
        recovered = current_work.sources(self.root)
        self.assertEqual(recovered["freshness"]["kind"], "live")
        with mock.patch("eutonos.current_work.repository_state.project", side_effect=ValueError("probe failed")):
            stale = current_work.project(self.root, refresh=True)
        self.assertEqual(stale["freshness"]["kind"], "stale")
        self.assertEqual(stale["boards"]["items"][0]["id"], "C")
        self.assertTrue(any("probe failed" in line for line in stale["diagnostics"]))

    def test_repeated_partial_scans_preserve_available_last_good(self):
        current_work.sources(self.root, refresh=True)
        cache = current_work._paths(self.root)[0]
        good_hash = current_work._load(cache)["sha256"]
        partial = deepcopy(repository_state.project(self.root, refresh=True))
        partial["status"] = "partial"
        partial["diagnostics"] = ["bounded source unavailable"]
        with mock.patch("eutonos.current_work.repository_state.project", return_value=partial):
            first = current_work.sources(self.root, refresh=True)
            second = current_work.sources(self.root, refresh=True)
        self.assertEqual(first["freshness"]["kind"], "stale")
        self.assertEqual(second["freshness"]["kind"], "stale")
        self.assertEqual(current_work._load(cache)["sha256"], good_hash)


if __name__ == "__main__":
    unittest.main()
