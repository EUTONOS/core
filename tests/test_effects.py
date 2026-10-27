"""EHF2W prerequisites exercised only in disposable local fixture directories."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import time
import unittest
from unittest.mock import patch

from eutonos import ReadError
from eutonos.effects import Coordinator, FixtureAuthority, LocalRoot, MARKER, MARKER_BYTES, digest, plan


class EffectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eutonos-effects-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "fixture café"
        self.root.mkdir()
        (self.root / MARKER).write_bytes(MARKER_BYTES)
        (self.root / "a.md").write_bytes(b"original a\r\n")
        (self.root / "b.md").write_bytes(b"original b\n")
        self.local = LocalRoot(self.root)
        self.auth = FixtureAuthority(self.local, "test-actor", frozenset({"a.md", "b.md", "new.md"}), time.time() + 300)
        self.dbpath = self.home / "coordinator.sqlite"
        self.coordinator = Coordinator.initialize(self.dbpath)
        self.generation = self.coordinator.start(self.auth, "attempt-a")

    def reject(self, code, function, *args):
        with self.assertRaises(ReadError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def reserve(self, *names):
        self.coordinator.reserve(self.auth, "attempt-a", self.generation, names)

    def apply(self, value, effect_id="effect-a"):
        return self.coordinator.apply(self.auth, "attempt-a", self.generation, effect_id, value)

    def bytes(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}

    def test_disjoint_progress_overlap_and_sibling_checkpoints(self):
        self.reserve("a.md")
        gen = self.coordinator.start(self.auth, "attempt-b")
        self.coordinator.reserve(self.auth, "attempt-b", gen, ["b.md"])
        self.reject("conflict", self.coordinator.reserve, self.auth, "attempt-b", gen, ["a.md"])
        self.coordinator.checkpoint(self.auth, "attempt-a", 1, {"next": "a"})
        self.coordinator.checkpoint(self.auth, "attempt-b", gen, {"next": "b"})
        self.apply(plan(self.local, {"a.md": b"a changed"}))
        self.coordinator.apply(self.auth, "attempt-b", gen, "effect-b", plan(self.local, {"b.md": b"b changed"}))
        self.assertEqual(json.loads(self.coordinator.inspect("attempt-a")["checkpoint"]), {"next": "a"})
        self.assertEqual(json.loads(self.coordinator.inspect("attempt-b")["checkpoint"]), {"next": "b"})

    def test_paused_generation_loses_execution_but_retains_protection(self):
        self.reserve("a.md")
        value = plan(self.local, {"a.md": b"changed"})
        paused = self.coordinator.pause(self.auth, "attempt-a", 1)
        self.reject("conflict", self.apply, value)
        gen = self.coordinator.start(self.auth, "attempt-b")
        self.reject("conflict", self.coordinator.reserve, self.auth, "attempt-b", gen, ["a.md"])
        self.generation = self.coordinator.resume(self.auth, "attempt-a", paused)
        self.assertEqual(self.apply(value)["outcome"], "completed")

    def test_stale_plan_changes_neither_targets_nor_coordinator(self):
        self.reserve("a.md")
        value = plan(self.local, {"a.md": b"new"}, inputs=["b.md"])
        (self.root / "b.md").write_bytes(b"later constraint")
        before, db = self.bytes(), self.dbpath.read_bytes()
        self.reject("stale", self.apply, value)
        self.assertEqual(before, self.bytes())
        self.assertEqual(db, self.dbpath.read_bytes())

    def test_retry_identity_and_native_source_changes_are_preserved(self):
        self.reserve("a.md")
        value = plan(self.local, {"a.md": b"new"})
        self.apply(value)
        (self.root / "a.md").write_bytes(b"later owner edit")
        self.assertEqual(self.apply(value)["outcome"], "already-completed")
        self.assertEqual((self.root / "a.md").read_bytes(), b"later owner edit")
        self.reject("conflict", self.apply, plan(self.local, {"a.md": b"different"}))
        self.reject("conflict", self.coordinator.release, self.auth, "attempt-a", 1,
                    {"a.md": digest(b"later owner edit")})

    def test_unknown_before_write_requires_inspection_then_exact_retry(self):
        self.reserve("a.md")
        value = plan(self.local, {"a.md": b"new"})
        with patch.object(LocalRoot, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.apply(value)
        self.reject("unknown", self.apply, value)
        self.assertEqual(self.coordinator.reconcile(self.auth, "attempt-a", 1, "effect-a")["outcome"], "not-applied")
        altered = plan(self.local, {"a.md": b"changed retry"})
        self.reject("conflict", self.apply, altered)
        self.assertEqual(self.apply(value)["outcome"], "completed")

    def test_process_death_after_first_write_reconciles_and_rolls_back(self):
        self.reserve("a.md", "b.md")
        # The child exits without unwinding SQLite or Python contexts.
        source = '''
import os,sys,time
from eutonos.effects import *
r=LocalRoot(sys.argv[1]); c=Coordinator(sys.argv[2])
a=FixtureAuthority(r,"test-actor",frozenset({"a.md","b.md"}),time.time()+60)
original=LocalRoot.replace
def die(self,*args):
    original(self,*args)
    os._exit(73)
LocalRoot.replace=die
c.apply(a,"attempt-a",1,"crash",plan(r,{"a.md":b"new a","b.md":b"new b"}))
'''
        child = subprocess.run([sys.executable, "-B", "-c", source, str(self.root), str(self.dbpath)], capture_output=True)
        self.assertEqual(child.returncode, 73, child.stderr)
        reopened = Coordinator(self.dbpath)
        result = reopened.reconcile(self.auth, "attempt-a", 1, "crash")
        self.assertEqual(result["outcome"], "partial")
        self.reject("unknown", reopened.apply, self.auth, "attempt-a", 1, "another", plan(self.local, {"b.md": b"other"}))
        self.assertEqual(reopened.rollback(self.auth, "attempt-a", 1, "crash")["outcome"], "rolled-back")
        self.assertEqual((self.root / "a.md").read_bytes(), b"original a\r\n")
        self.assertEqual((self.root / "b.md").read_bytes(), b"original b\n")

    def test_rollback_preserves_later_edits_and_restores_only_attributed_bytes(self):
        self.reserve("a.md", "b.md", "new.md")
        self.apply(plan(self.local, {"a.md": b"new a", "b.md": b"new b", "new.md": b"added"}))
        (self.root / "b.md").write_bytes(b"later b")
        result = self.coordinator.rollback(self.auth, "attempt-a", 1, "effect-a")
        self.assertEqual(result["preserved"], ["b.md"])
        self.assertEqual((self.root / "a.md").read_bytes(), b"original a\r\n")
        self.assertEqual((self.root / "b.md").read_bytes(), b"later b")
        self.assertFalse((self.root / "new.md").exists())
        self.coordinator.release(self.auth, "attempt-a", 1, {"a.md": digest(b"original a\r\n"), "new.md": None})
        self.assertEqual([r["path"] for r in self.coordinator.inspect("attempt-a")["reservations"]], ["b.md"])

    def test_absent_corrupt_or_duplicate_store_is_not_empty_authority(self):
        self.reject("unavailable", Coordinator, self.home / "missing.db")
        self.assertFalse((self.home / "missing.db").exists())
        bad = self.home / "bad.db"
        bad.write_bytes(b"invalid sqlite")
        self.reject("unavailable", Coordinator, bad)
        with self.assertRaises(FileExistsError):
            Coordinator.initialize(self.dbpath)

    def test_readonly_coordinator_reports_cause_without_changing_state(self):
        connect = sqlite3.connect
        before = self.dbpath.read_bytes()
        with patch("eutonos.effects.sqlite3.connect", side_effect=lambda *a, **kw:
                   connect(self.dbpath.as_uri() + "?mode=ro", uri=True)):
            with self.assertRaises(ReadError) as caught:
                self.coordinator.start(self.auth, "readonly-attempt")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("SQLITE_READONLY, code 8", str(caught.exception))
        self.assertIn("authorized host-write context", str(caught.exception))
        self.assertEqual(caught.exception.__cause__.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        self.assertEqual(self.dbpath.read_bytes(), before)
        self.reject("missing", self.coordinator.inspect, "readonly-attempt")

    def test_coordinator_error_does_not_expose_raw_sql_or_values(self):
        error = sqlite3.OperationalError("private SQL and values")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        error.sqlite_errorname = "SQLITE_BUSY"
        with patch("eutonos.effects.sqlite3.connect", side_effect=error):
            with self.assertRaises(ReadError) as caught:
                Coordinator(self.dbpath)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("SQLITE_BUSY, code 5", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("write access", str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)

    def test_expired_scope_and_unmarked_consumer_refuse_effects(self):
        expired = FixtureAuthority(self.local, "test-actor", self.auth.paths, time.time()-1)
        self.reject("denied", self.coordinator.reserve, expired, "attempt-a", 1, ["a.md"])
        self.reject("denied", self.coordinator.reserve, self.auth, "attempt-a", 1, ["outside.md"])
        (self.root / MARKER).unlink()
        self.reject("denied", self.coordinator.reserve, self.auth, "attempt-a", 1, ["a.md"])

    def test_unsafe_nested_case_and_hardlink_targets_are_refused(self):
        for name in ("../a.md", "C:/a.md", "a.md/child", "A.md"):
            with self.assertRaises(ReadError):
                self.local.read(name)
        nested = self.root / "nested"
        nested.mkdir(); (nested / ".git").mkdir()
        self.reject("denied", self.local.read, "nested/file.md")
        os.link(self.root / "a.md", self.root / "linked.md")
        self.reject("unsupported", self.local.read, "linked.md")

    def test_actual_link_or_windows_junction_never_reaches_outside(self):
        outside = self.home / "outside"
        outside.mkdir(); (outside / "secret.md").write_bytes(b"protected")
        link = self.root / "linked"
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            self.reject("unsafe-path", self.local.read, "linked/secret.md")
            self.reject("unsafe-path", LocalRoot, link)
        finally:
            if os.name == "nt":
                os.rmdir(link)  # Remove only the verified fixture junction itself.
            else:
                link.unlink()
        self.assertEqual((outside / "secret.md").read_bytes(), b"protected")

    def test_two_checkouts_have_separate_local_scopes(self):
        self.reserve("a.md")
        other = self.home / "checkout-two"
        other.mkdir(); (other / MARKER).write_bytes(MARKER_BYTES)
        admission = FixtureAuthority(LocalRoot(other), "test-actor", frozenset({"a.md"}), time.time()+60)
        gen = self.coordinator.start(admission, "other-checkout")
        self.coordinator.reserve(admission, "other-checkout", gen, ["a.md"])
        self.coordinator.apply(admission, "other-checkout", gen, "other-effect", plan(admission.root, {"a.md": b"other"}))
        self.assertEqual((self.root / "a.md").read_bytes(), b"original a\r\n")

    def test_simultaneous_connections_cannot_both_reserve_one_path(self):
        self.coordinator.start(self.auth, "attempt-b")
        barrier = Barrier(2)
        def contend(attempt):
            barrier.wait(timeout=5)
            try:
                Coordinator(self.dbpath).reserve(self.auth, attempt, 1, ["a.md"])
                return "reserved"
            except ReadError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(contend, ["attempt-a", "attempt-b"]))
        self.assertEqual(sorted(outcomes), ["conflict", "reserved"])

    def test_lost_receipt_is_reconciled_without_repeating_target_effect(self):
        self.reserve("a.md")
        value = plan(self.local, {"a.md": b"new"})
        original = LocalRoot.replace
        def lost(root, *args):
            original(root, *args)
            raise OSError("receipt lost")
        with patch.object(LocalRoot, "replace", lost):
            with self.assertRaises(OSError):
                self.apply(value)
        self.assertEqual(self.coordinator.reconcile(self.auth, "attempt-a", 1, "effect-a")["outcome"], "completed")
        self.assertEqual(self.apply(value)["writes"], 0)

    def test_interrupted_rollback_retains_its_direction_and_can_continue(self):
        self.reserve("a.md", "b.md")
        self.apply(plan(self.local, {"a.md": b"new a", "b.md": b"new b"}))
        original = LocalRoot.replace
        def lost(root, *args):
            original(root, *args)
            raise OSError("rollback interrupted")
        with patch.object(LocalRoot, "replace", lost):
            with self.assertRaises(OSError):
                self.coordinator.rollback(self.auth, "attempt-a", 1, "effect-a")
        self.assertEqual(self.coordinator.reconcile(self.auth, "attempt-a", 1, "effect-a")["outcome"], "rollback-incomplete")
        self.assertEqual(self.coordinator.rollback(self.auth, "attempt-a", 1, "effect-a")["outcome"], "rolled-back")
        self.assertEqual((self.root / "a.md").read_bytes(), b"original a\r\n")


if __name__ == "__main__":
    unittest.main()
