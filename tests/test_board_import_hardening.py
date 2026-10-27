"""Import rollback and typed-request boundaries in disposable native fixtures."""
from copy import deepcopy
import unittest
from unittest import mock

from tests import test_board_import as fixture
from eutonos import board_import, effects, write_batch, write_retry, tokenslang as ts
from eutonos.effects import LocalRoot, decode, digest
from eutonos.host import connect


class ImportHardeningTests(unittest.TestCase):
    # Reuse only fixture setup/helpers, without collecting its existing tests.
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

    def journal(self, coordinator, attempt):
        with coordinator._db() as db:
            return dict(db.execute("SELECT * FROM effects WHERE attempt=?", (attempt,)).fetchone())

    def store_snapshot(self, coordinator):
        with coordinator._db() as db:
            return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                    for table in ("attempts", "effects", "reservations")}

    def bytes_snapshot(self, paths):
        return {path: LocalRoot(self.root).read(path) for path in paths}

    def assert_rollback_refused(self, coordinator, authority, attempt, generation, row,
                                code="denied", message="typed undo"):
        effect = ts.loads(row["plan"])
        before = self.bytes_snapshot(change["path"] for change in effect["changes"])
        state = self.store_snapshot(coordinator)
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Rollback wrote import bytes")):
            with self.assertRaises(ts.ReadError) as refused:
                coordinator.rollback(authority, attempt, generation, row["id"])
        self.assertEqual(refused.exception.code, code)
        self.assertIn(message, refused.exception.message)
        self.assertEqual(self.bytes_snapshot(before), before)
        self.assertEqual(self.store_snapshot(coordinator), state)

    def test_completed_import_generic_rollback_refuses_and_typed_inverse_survives(self):
        result = self.submit(self.batch())
        coordinator, authority = connect(self.root, self.host, "writer")
        attempt = result["request_id"]
        row = self.journal(coordinator, attempt)
        self.assertEqual(row["state"], "completed")
        effect = ts.loads(row["plan"])
        paths = [change["path"] for change in effect["changes"]]
        state = coordinator.inspect(attempt)
        self.assertEqual(state["state"], "paused")
        generation = coordinator.resume(authority, attempt, state["generation"])
        coordinator.reserve(authority, attempt, generation, paths)
        self.assertEqual(len(coordinator.inspect(attempt)["reservations"]), len(paths))
        for change in effect["changes"]:
            self.assertEqual(LocalRoot(self.root).read(change["path"]), decode(change["after"]))
        self.assert_rollback_refused(coordinator, authority, attempt, generation, row)
        # A diverged shared after-image must not permit a partial generic inverse.
        path = "docs/REGISTRY.json"
        original = LocalRoot(self.root).read(path)
        (self.root / path).write_bytes(original + b" \n")
        self.assert_rollback_refused(coordinator, authority, attempt, generation, row)
        with self.assertRaises(ts.ReadError):
            write_batch.plan(self.root, self.batch(self.undo_request(result), "diverged-inverse"))
        (self.root / path).write_bytes(original)
        coordinator.closeout(authority, attempt, generation,
                             {path: digest(LocalRoot(self.root).read(path)) for path in paths},
                             {"completed": "Generic inverse refused", "next": "Typed undo", "pending": None})
        self.submit(self.batch(self.undo_request(result), "hardening-typed-inverse"))
        for change in effect["changes"]:
            self.assertEqual(LocalRoot(self.root).read(change["path"]), decode(change["backup"]))
        self.assertEqual(self.journal(coordinator, attempt), row)

    def test_interrupted_import_divergence_refuses_in_prepared_and_rolling_back_states(self):
        replace = LocalRoot.replace
        calls = []
        def interrupted(local, path, expected, body):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("Fixture interrupted during typed import")
            return replace(local, path, expected, body)
        with mock.patch.object(LocalRoot, "replace", interrupted):
            result = write_retry.submit_batch(self.host, self.root, "writer", self.batch())
        self.assertEqual(result["outcome"], "error", result)
        coordinator, authority = connect(self.root, self.host, "writer")
        attempt = result["request_id"]
        state = coordinator.inspect(attempt)
        generation = (coordinator.resume(authority, attempt, state["generation"])
                      if state["state"] == "paused" else state["generation"])
        row = self.journal(coordinator, attempt)
        self.assertEqual(row["state"], "prepared")
        self.assertTrue(coordinator.inspect(attempt)["reservations"])
        divergent_path = ts.loads(row["plan"])["changes"][0]["path"]
        original = LocalRoot(self.root).read(divergent_path)
        (self.root / divergent_path).write_bytes((original or b"") + b"Concurrent edit\n")
        self.assert_rollback_refused(coordinator, authority, attempt, generation, row,
                                     "stale", "every path")
        if original is None:
            (self.root / divergent_path).unlink()
        else:
            (self.root / divergent_path).write_bytes(original)
        inspection = coordinator.reconcile(authority, attempt, generation, row["id"])
        self.assertEqual(inspection["outcome"], "partial")
        self.assertEqual(inspection["writes"], 0)
        self.assertEqual(self.journal(coordinator, attempt), row)
        # Failed native submissions have no completed result receipt. Use the
        # actual attempt identity and journal, never the completed-result helper.
        undo = {"profile": board_import.PROFILE, "mode": "undo", "host": str(self.host), "actor": "writer",
                "request_id": result["request_id"], "effect_id": row["id"], "fingerprint": row["fingerprint"]}
        with self.assertRaises(ts.ReadError) as refused:
            write_batch.plan(self.root, self.batch(undo, "partial-inverse"))
        self.assertEqual(refused.exception.code, "conflict")
        self.assertEqual(refused.exception.message, "Undo requires one verified completed import journal effect.")
        # Model a legacy rollback interruption using the genuine typed journal.
        with coordinator._db() as db:
            db.execute("UPDATE effects SET state='rolling-back' WHERE id=?", (row["id"],))
        row = self.journal(coordinator, attempt)
        (self.root / divergent_path).write_bytes((original or b"") + b"Concurrent edit\n")
        self.assert_rollback_refused(coordinator, authority, attempt, generation, row,
                                     "stale", "every path")
        inspection = coordinator.reconcile(authority, attempt, generation, row["id"])
        self.assertEqual(inspection["outcome"], "rollback-incomplete")
        self.assertEqual(inspection["writes"], 0)
        self.assertEqual(self.journal(coordinator, attempt), row)
        self.assertTrue(coordinator.inspect(attempt)["reservations"])

    def assert_apply_refused_without_effects(self, prepared, request_id, code="denied"):
        coordinator, _ = connect(self.root, self.host, "writer")
        before = self.bytes_snapshot(prepared["paths"])
        state = self.store_snapshot(coordinator)
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Inconsistent import wrote bytes")):
            with self.assertRaises(ts.ReadError) as refused:
                write_batch.apply_prepared(self.root, self.host, "writer", prepared, request_id)
        self.assertEqual(refused.exception.code, code)
        self.assertEqual(self.bytes_snapshot(before), before)
        self.assertEqual(self.store_snapshot(coordinator), state)

    def test_valid_native_import_binding_cannot_apply_with_removed_tags(self):
        batch = self.batch()
        prepared = write_batch.plan(self.root, batch)
        queued = write_retry.enqueue(self.host, self.root, "writer", "batch", {"batch": write_batch.normalize(batch)})
        for remove_census in (False, True):
            altered = deepcopy(prepared)
            altered["effect"].pop("board_import")
            if remove_census:
                altered["effect"].pop("board_census")
            altered["fingerprint"] = digest(ts.encoded(altered["effect"]))
            effects.validate_plan(LocalRoot(self.root), altered["effect"])
            board_import.native_binding(LocalRoot(self.root), self.host, "writer", queued["request_id"], prepared["request"])
            with self.subTest(remove_census=remove_census):
                self.assert_apply_refused_without_effects(altered, queued["request_id"])
            board_import.native_binding(LocalRoot(self.root), self.host, "writer", queued["request_id"], prepared["request"])
        # The rejected calls did not consume or damage the native original intent.
        self.assertEqual(write_retry.run_once(self.host, self.root)["outcome"], "complete")

    def test_ordinary_request_cannot_apply_with_import_tag(self):
        tagged = write_batch.plan(self.root, self.batch())["effect"]["board_import"]
        ordinary = {"profile": write_batch.PROFILE, "operation_id": "ordinary-hardening",
                    "operations": [{"kind": "work", "request": {"operation": "refresh"}}]}
        prepared = write_batch.plan(self.root, ordinary)
        queued = write_retry.enqueue(self.host, self.root, "writer", "batch", {"batch": write_batch.normalize(ordinary)})
        prepared["effect"]["board_import"] = tagged
        prepared["fingerprint"] = digest(ts.encoded(prepared["effect"]))
        self.assert_apply_refused_without_effects(prepared, queued["request_id"])

    def test_mixed_and_unknown_request_shapes_refuse_before_attempt_creation(self):
        prepared = write_batch.plan(self.root, self.batch())
        queued = write_retry.enqueue(self.host, self.root, "writer", "batch", {"batch": prepared["request"]})
        for tagged in (False, True):
            altered = deepcopy(prepared)
            if not tagged:
                altered["effect"].pop("board_import")
                altered["effect"].pop("board_census")
            altered["request"]["operations"].append({"kind": "work", "request": {"operation": "refresh"}})
            altered["fingerprint"] = digest(ts.encoded(altered["effect"]))
            with self.subTest(tagged=tagged):
                self.assert_apply_refused_without_effects(altered, queued["request_id"])
        for malformed in ("unknown-kind", "missing-request"):
            altered = deepcopy(prepared)
            if malformed == "unknown-kind":
                altered["request"]["operations"][0]["kind"] = "unknown"
            else:
                altered["request"]["operations"][0].pop("request")
            with self.subTest(malformed=malformed):
                self.assert_apply_refused_without_effects(altered, queued["request_id"], "invalid")
