"""Complete census and first-apply guard proofs in disposable Git fixtures only."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from eutonos import board_census as census, boards, tokenslang as ts
from eutonos.effects import (Coordinator, FixtureAuthority, LocalRoot, MARKER,
                             MARKER_BYTES, digest, plan, validate_plan)


class BoardCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="board-census-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "fixture"
        self.root.mkdir()
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["BOARD.md"]}), encoding="utf-8")
        (self.root / "BOARD.md").write_text("# prose before registration\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("add", "--all")
        self.git("commit", "-qm", "baseline")
        self.local = LocalRoot(self.root)

    def git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_LAZY_FETCH="1")
        return subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "-c", "core.hooksPath=" + os.devnull, "-C", str(self.root), *args],
                              env=env, capture_output=True, check=True).stdout

    def board(self, identity):
        item = {"id": identity, "type": "quick", "title": identity, "summary": "fixture", "status": "planned"}
        return "```tokenslang-board\n" + json.dumps({"profile": boards.BOARD, "items": [item]}) + "\n```\n"

    def reject(self, call, *args, code=None):
        with self.assertRaises(ts.ReadError) as raised:
            call(*args)
        if code is not None:
            self.assertEqual(raised.exception.code, code)

    def host(self, paths=("BOARD.md",)):
        auth = FixtureAuthority(self.local, "fixture-actor", frozenset(paths), time.time() + 300)
        db = self.home / "coordinator.sqlite"
        coordinator = Coordinator.initialize(db)
        generation = coordinator.start(auth, "attempt")
        coordinator.reserve(auth, "attempt", generation, paths)
        return auth, coordinator, generation, db

    def test_complete_scope_above64_includes_unconfigured_done_and_absent(self):
        (self.root / "done").mkdir()
        (self.root / "done" / "history.md").write_text(self.board("OLD"), encoding="utf-8")
        (self.root / "UNCONFIGURED.md").write_text(self.board("U"), encoding="utf-8")
        for index in range(70):
            (self.root / f"prose{index}.md").write_text("OLD is a prose mention only\n", encoding="utf-8")
        self.git("add", "--all")
        guard = census.freeze(self.local, destinations=("new.md",))
        self.assertGreater(len(guard["hashes"]), 64)
        self.assertEqual(set(guard["by_id"]), {"OLD", "U"})
        self.assertEqual(guard["by_id"]["OLD"]["path"], "done/history.md")
        self.assertIsNone(guard["hashes"]["new.md"])
        value = plan(self.local, {"BOARD.md": self.board("NEW").encode()}, board_census=guard)
        self.assertEqual(len(value["observed"]), 1)
        validate_plan(self.local, value)
        census.validate(self.local, json.loads(json.dumps(guard)))

    def test_fence_free_file_becoming_native_is_stale(self):
        (self.root / "unconfigured.md").write_text("# empty\n", encoding="utf-8")
        self.git("add", "--all")
        guard = census.freeze(self.local)
        (self.root / "unconfigured.md").write_text(self.board("COLLISION"), encoding="utf-8")
        self.reject(census.validate, self.local, guard, code="stale")

    def test_absent_destination_becomes_occupied(self):
        guard = census.freeze(self.local, destinations=("new.md",))
        (self.root / "new.md").write_text(self.board("N"), encoding="utf-8")
        self.reject(census.validate, self.local, guard, code="stale")

    def test_tracked_add_remove_and_head_changes_are_stale(self):
        for change in ("add", "remove", "head"):
            with self.subTest(change=change):
                guard = census.freeze(self.local)
                if change == "add":
                    (self.root / "additional.md").write_text("# new\n", encoding="utf-8")
                    self.git("add", "additional.md")
                elif change == "remove":
                    self.git("rm", "--cached", "BOARD.md")
                else:
                    self.git("commit", "--allow-empty", "-qm", "new HEAD")
                self.reject(census.validate, self.local, guard, code="stale")
                self.git("reset", "--mixed", "HEAD")  # disposable test index only

    def test_changed_config_and_root_identity_rejected(self):
        guard = census.freeze(self.local)
        wrong = copy.deepcopy(guard)
        wrong["root_identity"][1] += 1
        self.reject(census.validate, self.local, wrong, code="conflict")
        (self.root / "eutonos.boards.json").write_text(json.dumps({"profile": boards.CONFIG, "owners": ["BOARD.md", "new.md"]}), encoding="utf-8")
        self.reject(census.validate, self.local, guard)

    def test_root_replacement_is_not_same_checkout(self):
        guard = census.freeze(self.local)
        moved = self.home / "old"
        self.root.rename(moved)
        self.root.mkdir()
        for name in (MARKER, "eutonos.boards.json", "BOARD.md"):
            (self.root / name).write_bytes((moved / name).read_bytes())
        self.git("init", "-q", "-b", "main")
        self.git("add", "--all")
        self.git("commit", "-qm", "replacement")
        self.reject(census.validate, self.local, guard)

    def test_malformed_multiple_and_duplicate_fences_refuse_complete_proof(self):
        for body in ("```tokenslang-board\n{", self.board("X") + self.board("Y"), self.board("X") + "```tokenslang-board\n"):
            with self.subTest(body=body):
                (self.root / "BOARD.md").write_text(body, encoding="utf-8")
                self.reject(census.freeze, self.local)
        (self.root / "BOARD.md").write_text(self.board("X"), encoding="utf-8")
        (self.root / "other.md").write_text(self.board("X"), encoding="utf-8")
        self.git("add", "other.md")
        self.reject(census.freeze, self.local, code="ambiguous")

    def test_budgets_refuse_incomplete_coverage(self):
        with mock.patch.object(census, "MAX_TRACKED", 2):
            self.reject(census.freeze, self.local, code="overflow")
        (self.root / "BOARD.md").write_bytes(b"x" * 129)
        with mock.patch.object(census, "MAX_FILE_BYTES", 128):
            self.reject(census.freeze, self.local, code="overflow")
        with mock.patch.object(census, "MAX_TOTAL_BYTES", 32):
            self.reject(census.freeze, self.local, code="overflow")

    def test_nested_repository_hardlink_and_excluded_path_refused(self):
        (self.root / "nested").mkdir()
        (self.root / "nested" / ".git").mkdir()
        (self.root / "nested" / "board.md").write_text("# nested\n", encoding="utf-8")
        self.git("add", "nested/board.md")
        self.reject(census.freeze, self.local, code="denied")
        self.git("rm", "--cached", "nested/board.md")
        os.link(self.root / "BOARD.md", self.root / "linked.md")
        self.git("add", "linked.md")
        self.reject(census.freeze, self.local, code="unsupported")
        (self.root / "linked.md").unlink()
        self.git("rm", "--cached", "linked.md")
        self.reject(census.freeze, LocalRoot(self.root, excluded=("BOARD.md",)), code="denied")

    def test_reparse_or_symlink_candidate_refused(self):
        ordinary = LocalRoot._ordinary

        def reparse(path):
            if path == self.root / "BOARD.md":
                raise ts.ReadError("unsafe-path", "fixture reparse")
            return ordinary(path)

        with mock.patch.object(LocalRoot, "_ordinary", side_effect=reparse):
            self.reject(census.freeze, self.local, code="unsafe-path")

    def test_first_validation_refuses_before_journal_and_targets(self):
        auth, coordinator, generation, db = self.host()
        value = plan(self.local, {"BOARD.md": self.board("X").encode()}, board_census=census.freeze(self.local))
        (self.root / "eutonos.boards.json").write_bytes(b"{}")
        before, database = (self.root / "BOARD.md").read_bytes(), db.read_bytes()
        self.reject(coordinator.apply, auth, "attempt", generation, "import", value)
        self.assertEqual((self.root / "BOARD.md").read_bytes(), before)
        self.assertEqual(db.read_bytes(), database)

    def test_second_validation_precedes_first_write_and_keeps_unknown_journal(self):
        auth, coordinator, generation, _ = self.host()
        value = plan(self.local, {"BOARD.md": self.board("X").encode()}, board_census=census.freeze(self.local))
        original, calls = census.validate, []

        def changed_before_second(local, guard):
            calls.append(1)
            if len(calls) == 2:
                (self.root / "new.md").write_text("# new tracked\n", encoding="utf-8")
                self.git("add", "new.md")
            original(local, guard)

        before = (self.root / "BOARD.md").read_bytes()
        with mock.patch.object(census, "validate", side_effect=changed_before_second):
            self.reject(coordinator.apply, auth, "attempt", generation, "import", value, code="stale")
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.root / "BOARD.md").read_bytes(), before)
        self.reject(coordinator.apply, auth, "attempt", generation, "import", value, code="unknown")
        self.assertEqual(coordinator.reconcile(auth, "attempt", generation, "import")["outcome"], "not-applied")

    def test_completed_replay_bypasses_census_and_changed_guard_conflicts(self):
        auth, coordinator, generation, _ = self.host()
        value = plan(self.local, {"BOARD.md": self.board("X").encode()}, board_census=census.freeze(self.local))
        self.assertEqual(coordinator.apply(auth, "attempt", generation, "import", value)["outcome"], "completed")
        with mock.patch.object(census, "validate", side_effect=AssertionError("completed effects must not recensus")):
            self.assertEqual(coordinator.apply(auth, "attempt", generation, "import", value)["outcome"], "already-completed")
        changed = copy.deepcopy(value)
        changed["board_census"]["head"] = "0" * 40
        self.reject(coordinator.apply, auth, "attempt", generation, "import", changed, code="conflict")

    def test_fixture_marker_and_normal_budgets_unchanged(self):
        self.assertEqual(set(plan(self.local, {})), {"profile", "root_identity", "observed", "changes"})
        self.reject(plan, self.local, {f"new{index}.md": b"x" for index in range(17)}, code="overflow")
        self.reject(plan, self.local, {}, [f"new{index}.md" for index in range(65)], code="overflow")
        guard = census.freeze(self.local)
        (self.root / MARKER).unlink()
        self.reject(census.validate, self.local, guard, code="denied")

    def test_typed_import_metadata_is_bounded_paired_and_fingerprinted(self):
        guard = census.freeze(self.local)
        metadata = {"profile": "eutonos.tokenslang.board-import-effect.v1", "operation_id": "synthetic-import",
                    "request_sha256": "a" * 64, "modes": ["append"]}
        value = plan(self.local, {"BOARD.md": self.board("X").encode()}, board_census=guard, board_import=metadata)
        validate_plan(self.local, value)
        for changes in ({"operation_id": ""}, {"operation_id": "x" * 1025}, {"request_sha256": "z" * 64},
                        {"modes": []}, {"modes": ["execute"]}, {"modes": ["append"] * 17},
                        {"extra": "callback"}, {"profile": "other"}):
            with self.subTest(changes=changes):
                wrong = copy.deepcopy(value)
                wrong["board_import"].update(changes)
                self.reject(validate_plan, self.local, wrong)
        wrong = copy.deepcopy(value)
        del wrong["board_census"]
        self.reject(validate_plan, self.local, wrong, code="invalid")
        # Census-only plans are explicit guard fixtures; metadata alone grants
        # no import authority. The typed batch planner must emit both fields.
        validate_plan(self.local, plan(self.local, {}, board_census=guard))
        auth, coordinator, generation, _ = self.host()
        before = (self.root / "BOARD.md").read_bytes()
        self.reject(coordinator.apply, auth, "attempt", generation, "import", value, code="denied")
        changed = copy.deepcopy(value)
        changed["board_import"]["request_sha256"] = "b" * 64
        self.assertNotEqual(digest(ts.encoded(value)), digest(ts.encoded(changed)))
        self.reject(coordinator.apply, auth, "attempt", generation, "import", changed, code="denied")
        self.assertEqual((self.root / "BOARD.md").read_bytes(), before)
        with coordinator._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects WHERE attempt='attempt'").fetchone()[0], 0)
        # A genuine census-only guard fixture may apply. Its completed logical
        # effect ID cannot subsequently be repurposed with import-looking tags.
        ordinary = copy.deepcopy(value)
        del ordinary["board_import"]
        self.assertEqual(coordinator.apply(auth, "attempt", generation, "import", ordinary)["outcome"], "completed")
        self.reject(coordinator.apply, auth, "attempt", generation, "import", changed, code="conflict")

    def test_mixed_journal_stays_fenced_without_recensus(self):
        (self.root / "b.md").write_text("# b\n", encoding="utf-8")
        self.git("add", "b.md")
        auth, coordinator, generation, _ = self.host(("BOARD.md", "b.md"))
        value = plan(self.local, {"BOARD.md": b"# imported\n", "b.md": b"# second\n"}, board_census=census.freeze(self.local))
        original = LocalRoot.replace

        def partial(local, *args):
            original(local, *args)
            raise OSError("fixture interruption after first write")

        with mock.patch.object(LocalRoot, "replace", side_effect=partial, autospec=True):
            with self.assertRaises(OSError):
                coordinator.apply(auth, "attempt", generation, "import", value)
        self.assertEqual(coordinator.reconcile(auth, "attempt", generation, "import")["outcome"], "partial")
        with mock.patch.object(census, "validate", side_effect=AssertionError("mixed journal must stay fenced")):
            self.reject(coordinator.apply, auth, "attempt", generation, "import", value, code="unknown")

    def test_tracked_symlink_mode_refuses_without_following_payload(self):
        blob = self.git("rev-parse", "HEAD:BOARD.md").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000," + blob + ",link.md")
        self.reject(census.freeze, self.local, code="unsupported")


if __name__ == "__main__":
    unittest.main()
