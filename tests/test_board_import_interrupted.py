"""Frozen native import abort/recovery in disposable Git roots and hosts."""
from copy import deepcopy
from contextlib import ExitStack
import base64
from dataclasses import replace
import unittest
from unittest import mock

from tests import test_board_import as fixture
from eutonos import board_import, board_census, effects, managed_write, tokenslang as ts, write_retry
from eutonos.effects import LocalRoot, decode, digest
from eutonos.host import connect
from eutonos.source import GitView


class InterruptedImportTests(unittest.TestCase):
    setUp = fixture.BoardImportTests.setUp
    git = fixture.BoardImportTests.git
    commit = fixture.BoardImportTests.commit
    put = fixture.BoardImportTests.put
    write_board = fixture.BoardImportTests.write_board
    item = fixture.BoardImportTests.item
    request = fixture.BoardImportTests.request
    batch = fixture.BoardImportTests.batch
    submit = fixture.BoardImportTests.submit
    undo_request = fixture.BoardImportTests.undo_request

    def interrupt(self, batch, writes=1):
        real = LocalRoot.replace
        calls = []
        def interrupted(local, path, expected, body):
            calls.append(path)
            if len(calls) > writes:
                raise OSError("Disposable typed import interrupted")
            return real(local, path, expected, body)
        with mock.patch.object(LocalRoot, "replace", interrupted):
            result = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(result["outcome"], "error", result)
        coordinator, authority = connect(self.root, self.host, "writer")
        state = coordinator.inspect(result["request_id"])
        self.assertEqual(state["state"], "active")
        row = self.journal(coordinator, result["request_id"])
        self.assertEqual(row["state"], "prepared")
        return result, coordinator, authority, state["generation"], row

    def journal(self, coordinator, attempt):
        with coordinator._db() as db:
            return dict(db.execute("SELECT * FROM effects WHERE attempt=?", (attempt,)).fetchone())

    def snapshot(self, coordinator, row):
        value = ts.loads(row["plan"])
        with coordinator._db() as db:
            state = {table: [tuple(r) for r in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                     for table in ("attempts", "effects", "reservations")}
        return state, {c["path"]: LocalRoot(self.root).read(c["path"]) for c in value["changes"]}

    def no_replan(self):
        stack = ExitStack()
        for owner, name in ((GitView, "read"), (board_census, "freeze"), (board_census, "validate")):
            stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError("Abort/recovery replanned source")))
        return stack

    def abort(self, coordinator, authority, generation, row):
        with self.no_replan():
            result = coordinator.rollback(authority, row["attempt"], generation, row["id"])
        self.assertEqual(result["outcome"], "abort-awaiting-reconcile")
        current = self.journal(coordinator, row["attempt"])
        self.assertEqual(current, {**row, "state": "rolling-back"})
        self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])
        for c in ts.loads(row["plan"])["changes"]:
            self.assertEqual(LocalRoot(self.root).read(c["path"]), decode(c["backup"]))
        return current

    def recover_baseline(self, coordinator, row):
        with self.no_replan():
            recovered = managed_write.recover(self.root, self.host, "writer", row["attempt"],
                {"completed": "Disposable abort verified", "next": "Exact retry", "pending": None})
        self.assertEqual(recovered["outcome"], "retry")
        self.assertFalse(recovered["complete"])
        state = coordinator.inspect(row["attempt"])
        self.assertEqual(state["state"], "paused")
        self.assertEqual(state["reservations"], [])
        self.assertEqual(state["unfinished_effects"], [])
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "not-applied"})

    def assert_denied_unchanged(self, coordinator, authority, generation, row, code):
        before = self.snapshot(coordinator, row)
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Rejected abort wrote")):
            with self.assertRaises(ts.ReadError) as caught:
                coordinator.rollback(authority, row["attempt"], generation, row["id"])
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.snapshot(coordinator, row), before)

    def test_partial_abort_retains_fence_then_recover_and_exact_retry_completes(self):
        batch = self.batch()
        result, coordinator, authority, generation, row = self.interrupt(batch)
        self.abort(coordinator, authority, generation, row)
        again = self.abort(coordinator, authority, generation, {**row, "state": "rolling-back"})
        # Re-open the actual host/coordinator, equivalent to losing in-process state.
        coordinator, _ = connect(self.root, self.host, "writer")
        self.recover_baseline(coordinator, again)
        complete = self.submit(batch)
        self.assertEqual(complete["request_id"], result["request_id"])
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "completed"})
        for c in ts.loads(row["plan"])["changes"]:
            self.assertEqual(LocalRoot(self.root).read(c["path"]), decode(c["after"]))

    def test_interrupted_inverse_abort_and_exact_inverse_retry(self):
        original = self.submit(self.batch())
        inverse = self.batch(self.undo_request(original), "interrupted-inverse-abort")
        result, coordinator, authority, generation, row = self.interrupt(inverse)
        self.abort(coordinator, authority, generation, row)
        self.recover_baseline(coordinator, {**row, "state": "rolling-back"})
        completed = self.submit(inverse)
        self.assertEqual(completed["request_id"], result["request_id"])
        for c in ts.loads(row["plan"])["changes"]:
            self.assertEqual(LocalRoot(self.root).read(c["path"]), decode(c["after"]))
        generation = coordinator.resume(authority, row["attempt"], coordinator.inspect(row["attempt"])["generation"])
        coordinator.reserve(authority, row["attempt"], generation, [c["path"] for c in ts.loads(row["plan"])["changes"]])
        self.assert_denied_unchanged(coordinator, authority, generation, self.journal(coordinator, row["attempt"]), "denied")

    def test_unprefixed_inverse_abort_reopen_recover_and_exact_retry(self):
        original = self.submit(self.batch())
        request = self.undo_request(original)
        request["request_id"] = request["request_id"].removeprefix("write-batch-")
        inverse = self.batch(request, "unprefixed-interrupted-inverse-abort")
        result, coordinator, authority, generation, row = self.interrupt(inverse)
        frozen = ts.loads(row["plan"])
        self.assertEqual(frozen["board_import"]["original_batch"]["operations"][0]["request"]["request_id"],
                         request["request_id"])
        self.assertFalse(request["request_id"].startswith("write-batch-"))
        aborted = self.abort(coordinator, authority, generation, row)
        coordinator, authority = connect(self.root, self.host, "writer")
        self.recover_baseline(coordinator, aborted)
        completed = self.submit(inverse)
        self.assertEqual(completed["request_id"], result["request_id"])
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "completed"})
        for c in frozen["changes"]:
            self.assertEqual(LocalRoot(self.root).read(c["path"]), decode(c["after"]))
        generation = coordinator.resume(authority, row["attempt"], coordinator.inspect(row["attempt"])["generation"])
        coordinator.reserve(authority, row["attempt"], generation, [c["path"] for c in frozen["changes"]])
        self.assert_denied_unchanged(coordinator, authority, generation,
                                     self.journal(coordinator, row["attempt"]), "denied")

    def test_divergence_anywhere_refuses_before_journal_or_target_mutation(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch())
        for c in ts.loads(row["plan"])["changes"]:
            original = LocalRoot(self.root).read(c["path"])
            (self.root / c["path"]).write_bytes((original or b"") + b"Concurrent bytes\n")
            self.assert_denied_unchanged(coordinator, authority, generation, row, "stale")
            if original is None:
                (self.root / c["path"]).unlink()
            else:
                (self.root / c["path"]).write_bytes(original)

    def test_second_phase_preflight_refuses_new_divergence_without_target_restore(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch())
        real = coordinator._import_abort_preflight
        calls = []
        changed = ts.loads(row["plan"])["changes"][-1]["path"]
        def raced(current_authority, value):
            calls.append(True)
            if len(calls) == 2:
                (self.root / changed).write_bytes(b"Concurrent second-phase edit\n")
            real(current_authority, value)
        before = self.snapshot(coordinator, row)[1]
        with mock.patch.object(coordinator, "_import_abort_preflight", side_effect=raced), \
                mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Second phase wrote")):
            with self.assertRaises(ts.ReadError) as caught:
                coordinator.rollback(authority, row["attempt"], generation, row["id"])
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "rolling-back"})
        for path, body in before.items():
            self.assertEqual(LocalRoot(self.root).read(path), b"Concurrent second-phase edit\n" if path == changed else body)
        self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])

    def test_mid_abort_fault_is_resumable_with_all_reservations(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch(), writes=3)
        real = LocalRoot.replace
        calls = []
        held = coordinator.inspect(row["attempt"])["reservations"]
        def fault(local, path, expected, body):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("Disposable abort fault")
            return real(local, path, expected, body)
        with mock.patch.object(LocalRoot, "replace", fault):
            with self.assertRaises(OSError):
                coordinator.rollback(authority, row["attempt"], generation, row["id"])
        self.assertEqual(len(calls), 2)
        coordinator, authority = connect(self.root, self.host, "writer")
        self.assertEqual(coordinator.inspect(row["attempt"])["reservations"], held)
        rolling = self.journal(coordinator, row["attempt"])
        self.assertEqual(rolling, {**row, "state": "rolling-back"})
        with self.assertRaises(ts.ReadError):
            managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.abort(coordinator, authority, generation, rolling)
        self.recover_baseline(coordinator, rolling)

    def test_mid_abort_cas_failure_preserves_diverged_bytes_and_fence(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch(), writes=3)
        real = LocalRoot.replace
        calls = []
        def raced(local, path, expected, body):
            calls.append(path)
            if len(calls) == 2:
                (self.root / path).write_bytes(b"Unmanaged writer won CAS\n")
            return real(local, path, expected, body)
        with mock.patch.object(LocalRoot, "replace", raced):
            with self.assertRaises(ts.ReadError):
                coordinator.rollback(authority, row["attempt"], generation, row["id"])
        self.assertEqual(LocalRoot(self.root).read(calls[-1]), b"Unmanaged writer won CAS\n")
        rolling = self.journal(coordinator, row["attempt"])
        self.assertEqual(rolling, {**row, "state": "rolling-back"})
        self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])
        self.assert_denied_unchanged(coordinator, authority, generation, rolling, "stale")

    def test_wrong_actor_root_generation_binding_and_journal_fingerprint_refuse(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch())
        self.assert_denied_unchanged(coordinator, replace(authority, actor="other"), generation, row, "conflict")
        wrong_root = self.root.parent / "unrelated-root"
        wrong_root.mkdir()
        self.assert_denied_unchanged(coordinator, replace(authority, root=LocalRoot(wrong_root)), generation, row, "conflict")
        self.assert_denied_unchanged(coordinator, authority, generation + 1, row, "conflict")
        queue = write_retry._connect(self.host)
        try:
            original = tuple(queue.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?",
                                          (row["attempt"],)).fetchone())
            for key, value in (("root", str(wrong_root)), ("actor", "other"), ("payload_sha256", "0" * 64)):
                with queue:
                    queue.execute("UPDATE batch_bindings SET " + key + "=? WHERE id=?", (value, row["attempt"]))
                with self.subTest(binding=key):
                    self.assert_denied_unchanged(coordinator, authority, generation, row, "conflict")
                with queue:
                    queue.execute("UPDATE batch_bindings SET root=?,actor=?,payload_sha256=? WHERE id=?",
                                  (*original, row["attempt"]))
        finally:
            queue.close()
        with coordinator._db() as db:
            db.execute("UPDATE effects SET fingerprint=? WHERE id=?", ("0" * 64, row["id"]))
        self.assert_denied_unchanged(coordinator, authority, generation, self.journal(coordinator, row["attempt"]), "conflict")

    def test_inverse_parent_fingerprint_and_images_cannot_be_forged(self):
        original = self.submit(self.batch())
        inverse = self.batch(self.undo_request(original), "inverse-validation")
        _, coordinator, authority, generation, row = self.interrupt(inverse)
        value = ts.loads(row["plan"])
        parent_id = value["board_import"]["original_batch"]["operations"][0]["request"]["effect_id"]
        with coordinator._db() as db:
            parent = dict(db.execute("SELECT * FROM effects WHERE id=?", (parent_id,)).fetchone())
            db.execute("UPDATE effects SET fingerprint=? WHERE id=?", ("0" * 64, parent_id))
        self.assert_denied_unchanged(coordinator, authority, generation, row, "conflict")
        with coordinator._db() as db:
            db.execute("UPDATE effects SET fingerprint=? WHERE id=?", (parent["fingerprint"], parent_id))
            parent_state = dict(db.execute("SELECT * FROM attempts WHERE id=?", (parent["attempt"],)).fetchone())
        for key, wrong in (("actor", "other"), ("root", "different-physical-root")):
            with coordinator._db() as db:
                db.execute("UPDATE attempts SET " + key + "=? WHERE id=?", (wrong, parent["attempt"]))
            with self.subTest(parent=key):
                self.assert_denied_unchanged(coordinator, authority, generation, row, "wrong-scope")
            with coordinator._db() as db:
                db.execute("UPDATE attempts SET " + key + "=? WHERE id=?", (parent_state[key], parent["attempt"]))
        forged = deepcopy(value)
        forged["changes"][0]["after"] = forged["changes"][0]["backup"]
        with coordinator._db() as db:
            db.execute("UPDATE effects SET plan=?,fingerprint=? WHERE id=?",
                       (ts.encoded(forged).decode(), digest(ts.encoded(forged)), row["id"]))
        self.assert_denied_unchanged(coordinator, authority, generation, self.journal(coordinator, row["attempt"]), "conflict")

    def test_hash_consistent_unrelated_scope_cannot_be_aborted(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch())
        value = ts.loads(row["plan"])
        change = deepcopy(value["changes"][0])
        change["path"] = "docs/unrelated-private.md"
        value["changes"].append(change)
        value["observed"][change["path"]] = change["before"]
        with coordinator._db() as db:
            db.execute("UPDATE effects SET plan=?,fingerprint=? WHERE id=?",
                       (ts.encoded(value).decode(), digest(ts.encoded(value)), row["id"]))
        forged = self.journal(coordinator, row["attempt"])
        self.assert_denied_unchanged(coordinator, authority, generation, forged, "denied")
        self.assertFalse((self.root / change["path"]).exists())

    def test_hash_consistent_same_owner_backup_must_match_reserved_baseline(self):
        _, coordinator, authority, generation, row = self.interrupt(self.batch())
        value = ts.loads(row["plan"])
        change = value["changes"][0]
        backup = b"Corrupted frozen before-image inside legitimate scope\n"
        change["backup"] = base64.b64encode(backup).decode("ascii")
        change["before"] = digest(backup)
        value["observed"][change["path"]] = change["before"]
        with coordinator._db() as db:
            db.execute("UPDATE effects SET plan=?,fingerprint=? WHERE id=?",
                       (ts.encoded(value).decode(), digest(ts.encoded(value)), row["id"]))
        forged = self.journal(coordinator, row["attempt"])
        before = self.snapshot(coordinator, forged)
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Corrupted backup wrote")):
            with self.assertRaises(ts.ReadError) as caught:
                coordinator.rollback(authority, row["attempt"], generation, row["id"])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("independently reserved baseline", caught.exception.message)
        self.assertEqual(self.snapshot(coordinator, forged), before)
