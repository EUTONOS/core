"""Public CLI and native retry recovery in disposable roots/hosts only."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from tests import test_board_import as fixture
from tests import test_board_import_interrupted as interrupted
from eutonos import effects, host_cli, managed_write, tokenslang as ts, write_retry
from eutonos.effects import LocalRoot, decode, digest
from eutonos.host import connect


class ImportRecoveryUsabilityTests(unittest.TestCase):
    # Composition only: no imported base TestCase or inherited test collection.
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
    interrupt = interrupted.InterruptedImportTests.interrupt
    journal = interrupted.InterruptedImportTests.journal
    snapshot = interrupted.InterruptedImportTests.snapshot
    no_replan = interrupted.InterruptedImportTests.no_replan

    def cli(self, command, row, *, request=None, paths=()):
        """Exercise the public parser/dispatch, including connected authority."""
        coordinator, _ = connect(self.root, self.host, "writer")
        state = coordinator.inspect(row["attempt"])
        argv = [command, "--root", str(self.root), "--host", str(self.host),
                "--actor", "writer", "--attempt", row["attempt"],
                "--generation", str(state["generation"]), "--effect-id", row["id"]]
        if request is not None:
            path = Path(self.temp.name) / "cli-request.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            argv.extend(["--request", str(path)])
        for path in paths:
            argv.extend(["--path", path])
        output = io.BytesIO()
        with mock.patch.object(sys, "stdout", io.TextIOWrapper(output, encoding="utf-8")) as stream:
            code = host_cli.main(argv)
            stream.flush()
            body = output.getvalue()
            stream.detach()
        return code, json.loads(body)

    def binding(self, attempt):
        db = write_retry._connect(self.host, readonly=True)
        try:
            return tuple(db.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?",
                                    (attempt,)).fetchone())
        finally:
            db.close()

    def assert_images(self, row, field):
        for change in ts.loads(row["plan"])["changes"]:
            self.assertEqual(LocalRoot(self.root).read(change["path"]), decode(change[field]), change["path"])

    def active_not_applied(self, batch=None):
        batch = self.batch() if batch is None else batch
        result, coordinator, _, _, row = self.interrupt(batch)
        with self.no_replan():
            code, aborted = self.cli("rollback", row)
            self.assertEqual((code, aborted["outcome"]), (0, "abort-awaiting-reconcile"))
            code, reconciled = self.cli("reconcile", row)
            self.assertEqual((code, reconciled["outcome"]), (0, "not-applied"))
        current = self.journal(coordinator, row["attempt"])
        self.assertEqual(current, {**row, "state": "not-applied"})
        state = coordinator.inspect(row["attempt"])
        self.assertEqual(state["state"], "active")
        self.assertTrue(state["reservations"])
        self.assertEqual(state["unfinished_effects"], [])
        self.assert_images(current, "backup")
        return batch, result, coordinator, current

    def recover_clean(self, coordinator, row):
        before_binding = self.binding(row["attempt"])
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Cleanup wrote targets")):
            result = managed_write.recover(self.root, self.host, "writer", row["attempt"],
                {"completed": "Verified disposable recovery", "next": "Retry", "pending": None})
        self.assertEqual((result["outcome"], result["complete"]), ("retry", False))
        state = coordinator.inspect(row["attempt"])
        self.assertEqual(state["state"], "paused")
        self.assertEqual(state["reservations"], [])
        self.assertEqual(state["unfinished_effects"], [])
        self.assertEqual(self.journal(coordinator, row["attempt"]), row)
        self.assertEqual(self.binding(row["attempt"]), before_binding)
        self.assert_images(row, "backup")

    def test_public_original_abort_reconcile_recover_then_same_batch_completes(self):
        batch, result, coordinator, row = self.active_not_applied()
        self.recover_clean(coordinator, row)
        completed = self.submit(batch)
        self.assertEqual(completed["request_id"], result["request_id"])
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "completed"})
        self.assert_images(row, "after")
        with coordinator._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects WHERE attempt=?", (row["attempt"],)).fetchone()[0], 1)

    def test_public_inverse_abort_reconcile_recover_preserves_completed_parent(self):
        original = self.submit(self.batch())
        coordinator, _ = connect(self.root, self.host, "writer")
        parent = self.journal(coordinator, original["request_id"])
        parent_binding = self.binding(original["request_id"])
        inverse = self.batch(self.undo_request(original), "public-inverse-recovery")
        batch, result, coordinator, row = self.active_not_applied(inverse)
        self.recover_clean(coordinator, row)
        completed = self.submit(batch)
        self.assertEqual(completed["request_id"], result["request_id"])
        self.assert_images(row, "after")
        self.assertEqual(self.journal(coordinator, parent["attempt"]), parent)
        self.assertEqual(self.binding(parent["attempt"]), parent_binding)
        self.assertEqual(self.journal(coordinator, row["attempt"]), {**row, "state": "completed"})

    def test_changed_tracked_markdown_same_id_refuses_then_fresh_operation_completes(self):
        batch, result, coordinator, row = self.active_not_applied()
        self.recover_clean(coordinator, row)
        self.put("README.md", "# Unrelated writer changed tracked Markdown\n")
        binding = self.binding(row["attempt"])
        snapshot = self.snapshot(coordinator, row)
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Changed census retry wrote")):
            refused = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(refused["outcome"], "error", refused)
        self.assertEqual(refused["request_id"], result["request_id"])
        self.assertIn("fresh operation_id", refused["message"])
        self.assertIn("verified cleanup", refused["message"])
        self.assertEqual(self.snapshot(coordinator, row), snapshot)
        self.assertEqual(self.binding(row["attempt"]), binding)
        fresh = deepcopy(batch)
        fresh["operation_id"] = "fresh-after-changed-census"
        completed = self.submit(fresh)
        self.assertNotEqual(completed["request_id"], result["request_id"])
        self.assertEqual(self.journal(coordinator, row["attempt"]), row)
        self.assertEqual(self.binding(row["attempt"]), binding)
        self.assertEqual((self.root / "README.md").read_text(), "# Unrelated writer changed tracked Markdown\n")
        new = self.journal(coordinator, completed["request_id"])
        self.assertEqual(new["state"], "completed")
        self.assert_images(new, "after")
        owner = ts.loads(LocalRoot(self.root).read("docs/campaigns/imported.md").split(b"```tokenslang-board\n")[1].split(b"\n```")[0])
        self.assertEqual([item["id"] for item in owner["items"]], [item["id"] for item in self.source_items])
        self.assertEqual(len({item["id"] for item in owner["items"]}), len(self.source_items))

    def assert_active_drift_refused(self, *, released):
        _, _, coordinator, row = self.active_not_applied()
        changes = ts.loads(row["plan"])["changes"]
        path = "docs/campaigns/imported.md"
        selected = [path] if released == "one" else [change["path"] for change in changes] if released == "all" else []
        if selected:
            expected = {name: digest(LocalRoot(self.root).read(name)) for name in selected}
            code, result = self.cli("release", row, request=expected)
            self.assertEqual(code, 0, result)
            self.assertEqual(result["outcome"], "released")
            # Genuine public pause/resume after baseline release, never DB edits.
            self.assertEqual(self.cli("pause", row)[0], 0)
            self.assertEqual(self.cli("resume", row)[0], 0)
            remaining = coordinator.inspect(row["attempt"])["reservations"]
            self.assertEqual(len(remaining), len(changes) - len(selected))
        foreign = b"Foreign writer bytes after reconciliation\n"
        (self.root / path).write_bytes(foreign)
        before = self.snapshot(coordinator, row)
        binding = self.binding(row["attempt"])
        with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Drift recovery wrote")):
            with self.assertRaises(ts.ReadError) as refused:
                managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.assertEqual(refused.exception.code, "stale")
        self.assertIn("bytes changed before cleanup", refused.exception.message)
        self.assertEqual(self.snapshot(coordinator, row), before)
        self.assertEqual(self.binding(row["attempt"]), binding)
        self.assertEqual((self.root / path).read_bytes(), foreign)

    def test_active_not_applied_drift_preserves_all_owned_reservations(self):
        self.assert_active_drift_refused(released="none")

    def test_resumed_not_applied_checks_unreserved_path_with_fewer_reservations(self):
        self.assert_active_drift_refused(released="one")

    def test_resumed_not_applied_checks_all_images_with_zero_reservations(self):
        self.assert_active_drift_refused(released="all")

    def test_normal_closeout_race_preserves_foreign_bytes_and_every_reservation(self):
        _, _, coordinator, row = self.active_not_applied()
        path = ts.loads(row["plan"])["changes"][-1]["path"]
        foreign = b"Concurrent edit after recovery preflight\n"
        before_state = self.snapshot(coordinator, row)[0]
        original_closeout = effects.Coordinator.closeout
        calls = []
        def raced(owner, authority, attempt, generation, expected, checkpoint):
            calls.append(attempt)
            self.assert_images(row, "backup")
            (self.root / path).write_bytes(foreign)
            return original_closeout(owner, authority, attempt, generation, expected, checkpoint)
        with self.no_replan(), mock.patch.object(effects.Coordinator, "closeout", raced), \
                mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Race cleanup wrote")):
            with self.assertRaises(ts.ReadError) as refused:
                managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.assertEqual(refused.exception.code, "stale")
        self.assertIn("Release source changed", refused.exception.message)
        self.assertEqual(calls, [row["attempt"]])
        self.assertEqual(self.snapshot(coordinator, row)[0], before_state)
        self.assertEqual((self.root / path).read_bytes(), foreign)

    def test_partial_and_abort_incomplete_advice_names_actual_public_abort_route(self):
        batch = self.batch()
        _, coordinator, _, _, row = self.interrupt(batch)
        for abort_incomplete in (False, True):
            if abort_incomplete:
                with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=OSError("Abort interrupted")):
                    code, result = self.cli("rollback", row)
                self.assertEqual(code, 2, result)
                self.assertEqual(self.journal(coordinator, row["attempt"])["state"], "rolling-back")
            before = self.snapshot(coordinator, row)
            with self.no_replan(), mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Partial recovery wrote")):
                with self.assertRaises(ts.ReadError) as refused:
                    managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
            self.assertEqual(refused.exception.code, "conflict")
            self.assertIn("Use host rollback", refused.exception.message)
            self.assertIn("then retry managed recovery", refused.exception.message)
            self.assertEqual(self.snapshot(coordinator, row), before)
            self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])
        # Follow the advice through the real parser/dispatch and native retry.
        with self.no_replan():
            self.assertEqual(self.cli("rollback", row)[0], 0)
            code, result = self.cli("reconcile", row)
        self.assertEqual((code, result["outcome"]), (0, "not-applied"))
        row = self.journal(coordinator, row["attempt"])
        self.recover_clean(coordinator, row)
        self.submit(batch)
        self.assert_images(row, "after")

    def ordinary_rollback(self, *, preserve_foreign):
        local = LocalRoot(self.root)
        effect = effects.plan(local, {"README.md": b"Ordinary change\n", "docs/admission.md": b"Other change\n"})
        real = LocalRoot.replace
        calls = []
        def interrupted_write(owner, path, expected, body):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("Ordinary write interrupted")
            return real(owner, path, expected, body)
        with mock.patch.object(LocalRoot, "replace", interrupted_write):
            with self.assertRaises(OSError):
                managed_write.apply(self.root, self.host, "writer", "ordinary-usability", "ordinary-effect",
                                    effect, digest(ts.encoded(effect)), [c["path"] for c in effect["changes"]], {})
        coordinator, _ = connect(self.root, self.host, "writer")
        row = self.journal(coordinator, "ordinary-usability")
        if preserve_foreign:
            (self.root / calls[0]).write_bytes(b"Preserved ordinary foreign edit\n")
        code, result = self.cli("rollback", row)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["outcome"], "rolled-back-with-preserved-edits" if preserve_foreign else "rolled-back")
        return coordinator, self.journal(coordinator, row["attempt"]), calls[0]

    def test_active_ordinary_rolled_back_preserved_edit_is_not_auto_released(self):
        coordinator, row, foreign_path = self.ordinary_rollback(preserve_foreign=True)
        before = self.snapshot(coordinator, row)
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Ordinary recovery wrote")):
            with self.assertRaises(ts.ReadError) as refused:
                managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.assertEqual(refused.exception.code, "conflict")
        self.assertNotIn("host rollback", refused.exception.message)
        self.assertEqual(self.snapshot(coordinator, row), before)
        self.assertTrue(coordinator.inspect(row["attempt"])["reservations"])
        self.assertEqual((self.root / foreign_path).read_bytes(), b"Preserved ordinary foreign edit\n")

    def test_clean_paused_ordinary_rolled_back_keeps_no_write_retry_compatibility(self):
        coordinator, row, _ = self.ordinary_rollback(preserve_foreign=False)
        expected = {c["path"]: c["before"] for c in ts.loads(row["plan"])["changes"]}
        self.assertEqual(self.cli("release", row, request=expected)[0], 0)
        self.assertEqual(self.cli("pause", row)[0], 0)
        before = self.snapshot(coordinator, row)
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Paused recovery wrote")):
            result = managed_write.recover(self.root, self.host, "writer", row["attempt"], {})
        self.assertEqual((result["outcome"], result["complete"]), ("retry", False))
        self.assertEqual(self.snapshot(coordinator, row), before)
        self.assertEqual(coordinator.inspect(row["attempt"])["reservations"], [])


if __name__ == "__main__":
    unittest.main()
