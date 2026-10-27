import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from eutonos import boards, ram, tokenslang as ts, work_precheck as precheck
from eutonos.effects import Coordinator, FixtureAuthority, LocalRoot, MARKER, MARKER_BYTES


class WorkPrecheckTests(unittest.TestCase):
    ITEM = "slice-testcheck"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-precheck-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repo"
        self.root.mkdir()
        (self.root / "docs").mkdir()
        (self.root / "RAM").mkdir()
        # Mirror the real repository's .gitignore so a claim file written under
        # the gitignored .eutonos-local/ scratch directory is never itself
        # counted as an uncommitted live edit.
        (self.root / ".gitignore").write_text(".eutonos-local/\n")
        self.owner = "docs/WORK_ITEMS.md"
        self.write_board([self.item(self.ITEM)])
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": [self.owner]}))
        self.ram_cfg = {"profile": ram.CONFIG, "repository_id": "test.repo", "scope_id": "test.scope",
                        "namespace": "test.ns", "store_root": "RAM", "current_task": self.owner,
                        "work_owners": {self.ITEM: self.owner}}
        (self.root / "eutonos.ram.json").write_text(json.dumps(self.ram_cfg))
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        self.git("init", "-q", "-b", "main")
        self.pin = self.commit()
        self.local = LocalRoot(self.root)
        self.store = ram.Store(self.local)
        self.coordinator = Coordinator.initialize(self.root.parent / "owner.sqlite")
        self.auth = None
        self.started = False

    # ------------------------------------------------------------- fixtures

    def item(self, identity, status="active", **extra):
        return {"id": identity, "type": "slice", "status": status, "title": identity + " title",
                "summary": "Useful scope.", **extra}

    def write_board(self, items):
        (self.root / self.owner).write_text(
            "# Work\n\nKeep prose.\n\n```tokenslang-board\n" + json.dumps({"profile": boards.BOARD, "items": items}) + "\n```\n")

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.run(["git", "-c", "safe.directory=" + str(self.root), "-C", str(self.root), *args],
                              env=env, check=True, capture_output=True)

    def commit(self, message="baseline"):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], env=env, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-C", str(self.root), "commit", "-qm", message], env=env, check=True, capture_output=True)
        return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                              env=env, check=True, capture_output=True).stdout.decode().strip()

    def admit(self, value):
        paths = frozenset(value["effect"]["observed"])
        self.auth = FixtureAuthority(self.local, "recorder", paths | (self.auth.paths if self.auth else frozenset()), time.time() + 600)
        if not self.started:
            self.coordinator.start(self.auth, "ram-attempt")
            self.started = True
        self.coordinator.reserve(self.auth, "ram-attempt", 1, [c["path"] for c in value["effect"]["changes"]])

    def add_ram_record(self, *, summary, recorded_at, occurred_at=None, memory_id="observation1"):
        identity = {"work_id": self.ITEM, "session_id": "session1", "issuer_id": "fixture", "memory_id": memory_id}
        request = {"profile": ram.WRITE, "operation_id": "capture-" + memory_id, "operation": "capture",
                   "identity": identity, "expected_revision": None, "at": recorded_at, "reason": "Test capture",
                   "experience": {"kind": "summary", "summary": summary, "recorded_at": recorded_at,
                                  "occurred_at": occurred_at, "unfinished": False,
                                  "actors": [{"id": "worker-one", "attribution": "supplied"}],
                                  "work_path": self.owner, "source_commit": self.pin,
                                  "sources": [{"path": self.owner, "evidence": "inspected"}],
                                  "children": [], "skill": None}}
        value = self.store.plan(request, "recorder")
        self.admit(value)
        result = ram.apply(self.coordinator, self.auth, "ram-attempt", 1, value, value["fingerprint"])
        self.assertIn(result["outcome"], {"completed", "unchanged"}, result)
        # Commit the new RAM record so it isn't itself picked up as an
        # uncommitted live edit by the very code this fixture is testing.
        self.commit("record " + memory_id)
        return result

    def set_extensions(self, extensions=None, next_action=None, status="active"):
        item = self.item(self.ITEM, status=status)
        if extensions is not None:
            item["extensions"] = extensions
        if next_action is not None:
            item["next_action"] = next_action
        self.write_board([item])
        self.commit("update board item")

    # ------------------------------------------------------------------ ID

    def test_item_id_validation_rejects_unsafe_ids(self):
        for bad in ("../etc/passwd", "a/b", "", "a" * 90, "-leading-dash", "with space"):
            with self.assertRaises(ts.ReadError):
                precheck.validate_item_id(bad)
        precheck.validate_item_id(self.ITEM)  # does not raise

    def test_cli_rejects_unsafe_item_id(self):
        run = precheck.main(["../nope", "--root", str(self.root)])
        self.assertEqual(run, 2)

    # ------------------------------------------------------------- verdicts

    def test_clear_when_nothing_found(self):
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "clear")
        self.assertEqual(result["item"]["status"], "active")
        self.assertEqual(result["ram"]["records"], [])
        self.assertEqual(result["claims"], {"active": [], "expired": []})

    def test_clear_reason_does_not_claim_absence_of_all_records(self):
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "clear")
        self.assertTrue(any("since the last checkpoint" in r for r in result["reasons"]))

    def test_partial_work_from_recorded_progress(self):
        self.set_extensions({"progress": {"percent": 40, "basis": "Reader is complete; rest remains."}})
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "partial-work")
        self.assertTrue(any("40%" in reason for reason in result["reasons"]))

    def test_selected_native_progress_is_consistent_in_shared_solo_and_fallback(self):
        from eutonos import current_work
        self.set_extensions({'progress': {'percent': 50, 'basis': 'Half implemented.'}})
        (self.root / 'eutonos.read.json').write_text(json.dumps({'profile':ts.CATALOG,
            'repository_id':'test.repo', 'scope_id':'test.scope', 'namespace':'test.ns'}))
        self.commit('native fixture identity')
        observed = current_work.project(self.root, refresh=True, include_knowledge=False)
        source = next(row for row in observed['boards']['items'] if row['id'] == self.ITEM)
        for shared, fallback in ((True, False), (False, False), (False, True)):
            with self.subTest(shared=shared, fallback=fallback):
                view = {**observed, 'shared_work': {'enabled': shared, 'coverage': 'complete'},
                        'status': 'unavailable' if fallback else 'available'}
                with mock.patch('eutonos.current_work.project', return_value=view):
                    result = precheck.precheck(self.root, self.ITEM)
                self.assertEqual(result['verdict'], 'partial-work', result)
                self.assertEqual(result['item']['progress']['percent'], 50)
                self.assertTrue(any('50%' in reason for reason in result['reasons']))
                if not fallback:
                    self.assertEqual(result['item']['source_revision'], source['source_revision'])
                    self.assertEqual(result['item']['extensions'], source['extensions'])

    def test_selected_native_checkpoint_filters_shared_and_solo_history(self):
        from eutonos import current_work
        self.set_extensions({'checkpoint': {'what': 'Checked', 'why': 'Restart', 'result': 'Recorded',
                                            'checks': ['fixture'], 'date': '2026-10-01'}})
        (self.root / 'eutonos.read.json').write_text(json.dumps({'profile':ts.CATALOG,
            'repository_id':'test.repo', 'scope_id':'test.scope', 'namespace':'test.ns'}))
        self.commit('native fixture identity')
        observed = current_work.project(self.root, refresh=True, include_knowledge=False)
        old_ram = {'available': True, 'records': [{'recorded_at': '2026-09-30T12:00:00Z'}]}
        old_commits = {'available': True, 'matching_message': [{'sha': 'old', 'date': '2026-09-30'}], 'matching_paths': []}
        live = {'available': True, 'worktrees': [], 'relevant': []}
        for shared, fallback in ((True, False), (False, False), (False, True)):
            with self.subTest(shared=shared, fallback=fallback):
                view = {**observed, 'shared_work': {'enabled': shared, 'coverage': 'complete'},
                        'status': 'unavailable' if fallback else 'available'}
                with mock.patch('eutonos.current_work.project', return_value=view), \
                     mock.patch.object(precheck, '_ram_records', return_value=old_ram), \
                     mock.patch.object(precheck, '_commits', return_value=old_commits), \
                     mock.patch.object(precheck, '_worktrees', return_value=live):
                    result = precheck.precheck(self.root, self.ITEM)
                self.assertEqual(result['verdict'], 'clear', result)
                self.assertEqual(result['item']['checkpoint']['date'], '2026-10-01')

    def test_partial_work_from_ram_record_since_checkpoint(self):
        self.set_extensions({"checkpoint": {"what": "Did X", "why": "Needed", "result": "Landed",
                                            "checks": ["tests"], "date": "2020-01-01"}})
        self.add_ram_record(summary="Implemented most of the reader.", recorded_at="2026-09-20T10:00:00+00:00")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "partial-work", result)
        self.assertEqual(len(result["ram"]["records"]), 1)
        self.assertTrue(any("RAM record" in reason for reason in result["reasons"]))

    def test_no_partial_work_when_ram_record_predates_checkpoint(self):
        self.set_extensions({"checkpoint": {"what": "Did X", "why": "Needed", "result": "Landed",
                                            "checks": ["tests"], "date": "2027-01-01"}})
        self.add_ram_record(summary="Old note from before the checkpoint.", recorded_at="2020-01-01T10:00:00+00:00")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "clear", result)

    def test_partial_work_from_matching_commit(self):
        (self.root / "docs" / "NOTE.md").write_text("Progress note for " + self.ITEM + "\n")
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Work on " + self.ITEM)
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "partial-work", result)
        self.assertEqual(len(result["commits"]["matching_message"]), 1)
        self.assertFalse(result["commits"]["matching_message"][0]["records_only"])

    def test_item_id_inside_a_longer_word_does_not_match(self):
        (self.root / "docs" / "NOTE.md").write_text("Unrelated\n")
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Tests x" + self.ITEM + "y and " + self.ITEM + "-extra")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["commits"]["matching_message"], [], result)

    def test_records_only_commit_is_not_a_reason_but_is_listed(self):
        # A commit that only touches board/RAM record paths (as check_docs's
        # generated views or RAM/eutonos.*.json do) is bookkeeping, not
        # evidence of real work -- it must not drive the verdict, but must
        # still show up in the commits list, labelled records_only.
        (self.root / "eutonos.boards.json").write_text(
            json.dumps({"profile": boards.CONFIG, "owners": [self.owner], "note": "bump"}))
        self.git("add", "--all")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Record " + self.ITEM + " slice")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "clear", result)
        self.assertEqual(len(result["commits"]["matching_message"]), 1)
        self.assertTrue(result["commits"]["matching_message"][0]["records_only"])
        text = precheck.format_text(result)
        self.assertIn("(records only)", text)

    def test_partial_work_from_uncommitted_edit_in_matched_path(self):
        # The only worktree's branch is "main", which does not contain the item
        # id, so this dirty file counts toward the verdict only because --paths
        # covers it.
        (self.root / "docs" / "SCRATCH.md").write_text("draft\n")
        result = precheck.precheck(self.root, self.ITEM, paths=["docs"])
        self.assertEqual(result["verdict"], "partial-work", result)
        worktree = result["live_edits"]["worktrees"][0]
        self.assertGreaterEqual(worktree["changed_count"], 1)
        self.assertEqual(len(result["live_edits"]["relevant"]), 1)
        self.assertEqual(result["live_edits"]["other_dirty_count"], 0)

    def test_dirty_quoted_path_and_rename_are_matched_by_precheck(self):
        original = self.root / 'docs' / 'Task Name.md'
        original.write_text('baseline\n')
        self.commit('add spaced filename')
        original.write_text('modified\n')
        live = precheck._worktrees(self.root, self.ITEM, ['docs/'])
        self.assertTrue(live['available'])
        self.assertEqual(len(live['relevant']), 1)
        self.assertEqual(live['relevant'][0]['changed_count'], 1)
        self.commit('save spaced filename')
        self.git('mv', 'docs/Task Name.md', 'docs/Renamed Task.md')
        renamed = precheck._worktrees(self.root, self.ITEM, ['docs/Task Name.md'])
        self.assertEqual(len(renamed['relevant']), 1)
        self.assertEqual(renamed['worktrees'][0]['paths_changed_count'], 1)
        self.assertEqual(renamed['worktrees'][0]['changed_count'], 1)

    def test_unrelated_uncommitted_edit_is_not_a_reason(self):
        # Same dirty file, but no --paths and a branch that doesn't mention the
        # item: it must not push the verdict to partial-work, and it must not
        # be listed as an individual reason -- only as an "other worktrees"
        # count-only summary.
        (self.root / "docs" / "SCRATCH.md").write_text("draft\n")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "clear", result)
        self.assertEqual(result["live_edits"]["relevant"], [])
        self.assertEqual(result["live_edits"]["other_dirty_count"], 1)
        text = precheck.format_text(result)
        self.assertIn("Other worktrees: 1 dirty, not linked to this item.", text)
        self.assertNotIn("linked to this item.\n  -", text)  # no per-worktree reason line

    def test_uncommitted_edit_counts_when_branch_matches_item(self):
        # A second worktree on a branch that contains the item id: its dirty
        # file must count toward the verdict even with no --paths given.
        worktree_path = Path(self.temp.name) / "wt-item"
        self.git("worktree", "add", "-b", "work-on-" + self.ITEM, str(worktree_path), "main")
        (worktree_path / "NOTES.md").write_text("draft\n")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "partial-work", result)
        self.assertEqual(len(result["live_edits"]["relevant"]), 1)
        self.assertEqual(result["live_edits"]["relevant"][0]["branch"], "work-on-" + self.ITEM)
        self.assertEqual(result["live_edits"]["other_dirty_count"], 0)
        self.assertTrue(any("linked to this item" in reason for reason in result["reasons"]))

    def test_likely_duplicate_when_item_completed(self):
        self.set_extensions(status="completed")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["verdict"], "likely-duplicate", result)
        self.assertTrue(any("completed" in reason for reason in result["reasons"]))

    def test_likely_duplicate_when_claim_by_other_chat(self):
        precheck.write_claim(self.root, self.ITEM, "codex-main", harness="codex")
        result = precheck.precheck(self.root, self.ITEM, chat="claude-main")
        self.assertEqual(result["verdict"], "likely-duplicate", result)
        self.assertTrue(any("codex-main" in reason for reason in result["reasons"]))

    def test_no_duplicate_flag_for_own_claim(self):
        precheck.write_claim(self.root, self.ITEM, "claude-main", harness="claude")
        result = precheck.precheck(self.root, self.ITEM, chat="claude-main")
        self.assertEqual(result["verdict"], "clear", result)

    def test_missing_item_still_returns_a_report(self):
        result = precheck.precheck(self.root, "slice-doesnotexist")
        self.assertIsNone(result["item"])
        self.assertEqual(result["verdict"], "clear")

    # -------------------------------------------------------------- claims

    def test_claim_write_refresh_release(self):
        first = precheck.write_claim(self.root, self.ITEM, "claude-main", harness="claude", ttl_minutes=60)
        self.assertEqual(first["chat"], "claude-main")
        second = precheck.write_claim(self.root, self.ITEM, "claude-main", harness="claude", ttl_minutes=5)
        self.assertNotEqual(first["expires"], second["expires"])
        released = precheck.release_claim(self.root, self.ITEM, "claude-main")
        self.assertEqual(released["outcome"], "released")
        self.assertIsNone(precheck.read_claim(self.root, self.ITEM))

    def test_release_by_non_owner_is_refused(self):
        precheck.write_claim(self.root, self.ITEM, "claude-main")
        result = precheck.release_claim(self.root, self.ITEM, "codex-main")
        self.assertEqual(result["outcome"], "not-owner")
        self.assertIsNotNone(precheck.read_claim(self.root, self.ITEM))

    def test_release_when_absent(self):
        result = precheck.release_claim(self.root, self.ITEM, "claude-main")
        self.assertEqual(result["outcome"], "absent")

    def test_expired_claim_is_reported_not_deleted(self):
        path = precheck._claim_path(self.root, self.ITEM)
        path.parent.mkdir(parents=True, exist_ok=True)
        expired = {"item": self.ITEM, "chat": "claude-main", "harness": "claude",
                   "t": "2020-01-01T00:00:00+00:00", "expires": "2020-01-01T01:00:00+00:00"}
        path.write_text(json.dumps(expired), newline="\n")
        result = precheck.precheck(self.root, self.ITEM)
        self.assertEqual(result["claims"], {"active": [], "expired": [expired]})
        self.assertTrue(path.exists())  # never auto-deleted by a reader

    def test_claim_write_validates_inputs(self):
        with self.assertRaises(ts.ReadError):
            precheck.write_claim(self.root, self.ITEM, "")
        with self.assertRaises(ts.ReadError):
            precheck.write_claim(self.root, self.ITEM, "chat", harness="gemini")
        with self.assertRaises(ts.ReadError):
            precheck.write_claim(self.root, self.ITEM, "chat", ttl_minutes=0)

    # ---------------------------------------------------------- CLI/bounds

    def test_cli_claim_and_release_round_trip(self):
        out = precheck.main(["--claim", "--chat", "claude-main", "--harness", "claude", self.ITEM, "--root", str(self.root)])
        self.assertEqual(out, 0)
        self.assertIsNotNone(precheck.read_claim(self.root, self.ITEM))
        out = precheck.main(["--release", "--chat", "claude-main", self.ITEM, "--root", str(self.root)])
        self.assertEqual(out, 0)
        self.assertIsNone(precheck.read_claim(self.root, self.ITEM))

    def test_cli_text_output(self):
        import sys
        run = subprocess.run([sys.executable, "-m", "eutonos.work_precheck", self.ITEM, "--root", str(self.root), "--text"],
                             capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(b"Verdict:", run.stdout)
        self.assertIn(self.ITEM.encode(), run.stdout)

    def test_bounded_when_git_executable_is_missing(self):
        with mock.patch("eutonos.work_precheck.subprocess.run", side_effect=FileNotFoundError("no git")):
            result = precheck.precheck(self.root, self.ITEM)
        self.assertFalse(result["commits"]["available"])
        self.assertFalse(result["live_edits"]["available"])
        # RAM/board reads are unaffected by a missing git executable; no exception raised.
        self.assertEqual(result["verdict"], "unknown", result)

    def test_bounded_when_git_times_out(self):
        with mock.patch("eutonos.work_precheck.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)):
            result = precheck.precheck(self.root, self.ITEM)
        self.assertFalse(result["commits"]["available"])
        self.assertFalse(result["live_edits"]["available"])
        self.assertEqual(result["verdict"], "unknown", result)

    def test_bounded_when_root_is_not_a_git_repository(self):
        bare = Path(self.temp.name) / "not-a-repo"
        bare.mkdir()
        result = precheck.precheck(bare, "slice-anything")
        self.assertFalse(result["commits"]["available"])
        self.assertFalse(result["live_edits"]["available"])
        self.assertIsNone(result["item"])
        self.assertFalse(result["ram"]["available"])
        self.assertEqual(result["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
