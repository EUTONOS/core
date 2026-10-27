"""Durable native import proof in disposable Git roots and temporary hosts."""
from copy import deepcopy
import base64
import json
import time
import unittest
from unittest import mock

from tests import retention_support as retention
from tests import test_board_import as fixture
from eutonos import board_import, boards, effects, write_batch, write_retry, tokenslang as ts
from eutonos.effects import LocalRoot, decode, digest
from eutonos.host import connect
from eutonos.source import GitView


class DurableImportTests(unittest.TestCase):
    # Reuse setup/helpers, without inheriting or recollecting its unrelated tests.
    setUp = fixture.BoardImportTests.setUp
    git = fixture.BoardImportTests.git
    commit = fixture.BoardImportTests.commit
    put = fixture.BoardImportTests.put
    write_board = fixture.BoardImportTests.write_board
    item = fixture.BoardImportTests.item
    request = fixture.BoardImportTests.request
    batch = fixture.BoardImportTests.batch
    submit = fixture.BoardImportTests.submit

    def journal(self, result):
        coordinator, _ = connect(self.root, self.host, "writer")
        with coordinator._db() as db:
            return dict(db.execute("SELECT * FROM effects WHERE attempt=?", (result["request_id"],)).fetchone())

    def undo_request(self, result):
        row = self.journal(result)
        return {"profile": board_import.PROFILE, "mode": "undo", "host": str(self.host), "actor": "writer",
                "request_id": result["request_id"], "effect_id": row["id"], "fingerprint": row["fingerprint"]}

    def rewrite_journal(self, result, effect):
        """Synthetic trusted-store corruption, never a successful admission route."""
        coordinator, _ = connect(self.root, self.host, "writer")
        row = self.journal(result)
        fingerprint = digest(ts.encoded(effect))
        with coordinator._db() as db:
            db.execute("UPDATE effects SET plan=?,fingerprint=? WHERE id=?",
                       (ts.encoded(effect).decode(), fingerprint, row["id"]))
        undo = self.undo_request(result)
        return undo

    def test_completions_past_retention_prune_receipt_then_inverse_is_durable(self):
        batch = self.batch(); result = self.submit(batch)
        journal = self.journal(result); effect = ts.loads(journal["plan"])
        self.assertEqual(effect["board_import"]["original_batch"], write_batch.normalize(batch))
        self.assertEqual(write_retry.TERMINAL_RETENTION, 500)
        with retention.patched_retention():
            retention.complete_past_limit(self, self.host, self.root, "Ordinary retention fixture")
        db = write_retry._connect(self.host, readonly=True)
        try:
            self.assertIsNone(db.execute("SELECT id FROM requests WHERE id=?", (result["request_id"],)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM requests WHERE root=? AND state='complete'", (str(self.root),)).fetchone()[0], retention.PATCHED_RETENTION)
            self.assertEqual(tuple(db.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?",
                (result["request_id"],)).fetchone()), (str(self.root), "writer", digest(ts.encoded(write_batch.normalize(batch)))))
        finally: db.close()
        self.assertEqual(self.journal(result)["state"], "completed")
        for change in effect["changes"]:
            self.assertEqual(LocalRoot(self.root).read(change["path"]), decode(change["after"]))
        # Synthetic legacy representation of the genuinely pruned original:
        # absence cannot be reconstructed from tags or imported rows.
        legacy = deepcopy(effect)
        legacy["board_import"]["profile"] = board_import.EFFECT_V1
        legacy["board_import"].pop("original_batch")
        undo = self.rewrite_journal(result, legacy)
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "legacy-pruned-undo"))
        undo = self.rewrite_journal(result, effect)
        inverse = self.batch(undo, "durable-inverse")
        self.submit(inverse)
        for change in effect["changes"]:
            self.assertEqual(LocalRoot(self.root).read(change["path"]), decode(change["backup"]))
        self.assertEqual(self.journal(result)["plan"], journal["plan"])
        with mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Inverse retry wrote files")), \
                mock.patch.object(GitView, "read", side_effect=AssertionError("Inverse retry reread source")):
            self.submit(inverse)
        changed = deepcopy(inverse); changed["operations"][0]["request"]["fingerprint"] = "0" * 64
        with self.assertRaises(ts.ReadError): self.submit(changed)

    def test_multi_owner_retains_original_physical_normalized_payload(self):
        first = self.request(items=self.source_items[:2])
        second = self.request(owner="docs/campaigns/second.md", items=self.source_items[2:])
        batch = self.batch(first); batch["operations"].append({"kind": "import", "request": second})
        result = self.submit(batch)
        metadata = ts.loads(self.journal(result)["plan"])["board_import"]
        self.assertEqual(metadata["original_batch"], write_batch.normalize(batch))
        self.assertEqual(first["expected_config_sha256"], second["expected_config_sha256"])
        self.assertEqual(first["expected_registry_sha256"], second["expected_registry_sha256"])
        self.submit(self.batch(self.undo_request(result), "multi-inverse"))

    def test_pure_metadata_validation_is_bounded_and_refuses_malformed_intent(self):
        metadata = write_batch.plan(self.root, self.batch())["effect"]["board_import"]
        with mock.patch.object(GitView, "read", side_effect=AssertionError("Pure validator read source")):
            self.assertEqual(board_import.validate_metadata(metadata), metadata["original_batch"])
            for key, value in [("profile", "unknown"), ("profile", []), ("operation_id", "other"), ("request_sha256", "0" * 64),
                               ("modes", ["undo"]), ("original_batch", None)]:
                altered = deepcopy(metadata); altered[key] = value
                with self.subTest(key=key), self.assertRaises(ts.ReadError): board_import.validate_metadata(altered)
            for mutate in [lambda b: b["operations"][0].update(kind="update"),
                           lambda b: b["operations"][0]["request"].update(profile="unknown"),
                           lambda b: b["operations"][0]["request"].update(owner_path="../outside.md"),
                           lambda b: b["operations"][0]["request"].update(document=None)]:
                altered = deepcopy(metadata); mutate(altered["original_batch"])
                altered["request_sha256"] = digest(ts.encoded(altered["original_batch"]))
                with self.assertRaises(ts.ReadError): board_import.validate_metadata(altered)
            oversized = deepcopy(metadata); oversized["original_batch"]["requested_by"] = {"kind": "agent", "value": "x" * (1024 * 1024)}
            with self.assertRaises(ts.ReadError): board_import.validate_metadata(oversized)
            too_many = deepcopy(metadata); too_many["original_batch"]["operations"] *= 17
            too_many["request_sha256"] = digest(ts.encoded(too_many["original_batch"]))
            with self.assertRaises(ts.ReadError): board_import.validate_metadata(too_many)

    def test_binding_actor_root_digest_and_missing_store_fail_closed(self):
        result = self.submit(self.batch()); undo = self.undo_request(result)
        db = write_retry._connect(self.host)
        try:
            original = tuple(db.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?", (result["request_id"],)).fetchone())
            for key, value in [("root", str(self.root.parent)), ("actor", "other"), ("payload_sha256", "0" * 64)]:
                with db: db.execute("UPDATE batch_bindings SET " + key + "=? WHERE id=?", (value, result["request_id"]))
                with self.subTest(key=key), self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "binding-inverse"))
                with db: db.execute("UPDATE batch_bindings SET root=?,actor=?,payload_sha256=? WHERE id=?", (*original, result["request_id"]))
            with db: db.execute("DELETE FROM batch_bindings WHERE id=?", (result["request_id"],))
            with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "missing-binding-inverse"))
        finally: db.close()
        normalized = write_batch.normalize(self.batch())
        self.assertEqual(write_retry._request_id(self.root, "writer", "batch", {"batch": normalized}),
                         write_retry._request_id(self.root, "other", "batch", {"batch": normalized}))

    def test_retained_payload_receipt_and_effect_scope_disagreements_refused(self):
        result = self.submit(self.batch()); original = ts.loads(self.journal(result)["plan"])
        altered = deepcopy(original)
        altered["board_import"]["original_batch"]["operations"][0]["request"]["document"]["role"] = "changed-role"
        altered["board_import"]["request_sha256"] = digest(ts.encoded(altered["board_import"]["original_batch"]))
        undo = self.rewrite_journal(result, altered)
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "payload-inverse"))
        for mutate in [lambda e: e.update(root_identity=[0, 0]),
                       lambda e: e["board_census"].update(root_identity=[0, 0]),
                       lambda e: e["changes"][0].update(path="docs/unrelated.md")]:
            altered = deepcopy(original); mutate(altered)
            undo = self.rewrite_journal(result, altered)
            with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "corrupt-inverse"))
        self.rewrite_journal(result, original)

    def test_surviving_failed_receipt_requires_normal_recovery_before_inverse(self):
        batch = self.batch()
        with mock.patch.object(effects.Coordinator, "closeout", side_effect=OSError("Fixture interrupted before native completion")):
            result = write_retry.submit_batch(self.host, self.root, "writer", batch)
        self.assertEqual(result["outcome"], "error")
        undo = self.undo_request(result)
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "failed-receipt-inverse"))
        completed = self.submit(batch)
        self.submit(self.batch(self.undo_request(completed), "recovered-original-inverse"))

    def test_inverse_closeout_interruption_recovers_without_replaying_source(self):
        result = self.submit(self.batch()); inverse = self.batch(self.undo_request(result), "interrupted-inverse")
        original = ts.loads(self.journal(result)["plan"])
        with mock.patch.object(effects.Coordinator, "closeout", side_effect=OSError("Fixture inverse closeout interruption")):
            interrupted = write_retry.submit_batch(self.host, self.root, "writer", inverse)
        self.assertEqual(interrupted["outcome"], "error")
        for change in original["changes"]:
            self.assertEqual(LocalRoot(self.root).read(change["path"]), decode(change["backup"]))
        with mock.patch.object(GitView, "read", side_effect=AssertionError("Inverse recovery reread source")), \
                mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Inverse recovery wrote again")):
            completed = self.submit(inverse)
        self.assertEqual(completed["outcome"], "complete")
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(self.undo_request(completed), "undo-of-undo"))

    def test_typed_apply_requires_native_binding_and_retained_request_consistency(self):
        batch = self.batch(); prepared = write_batch.plan(self.root, batch)
        request_id = write_retry._request_id(self.root, "writer", "batch", {"batch": write_batch.normalize(batch)})
        with self.assertRaises(ts.ReadError): write_batch.apply_prepared(self.root, self.host, "writer", prepared, request_id)
        self.assertFalse((self.root / "docs/campaigns/imported.md").exists())
        write_retry.enqueue(self.host, self.root, "writer", "batch", {"batch": write_batch.normalize(batch)})
        altered = deepcopy(prepared); altered["request"]["operations"][0]["request"]["document"]["role"] = "other"
        with self.assertRaises(ts.ReadError): write_batch.apply_prepared(self.root, self.host, "writer", altered, request_id)
        self.assertFalse((self.root / "docs/campaigns/imported.md").exists())

    def test_valid_binding_altered_output_refused_by_typed_and_generic_routes(self):
        from eutonos import host, managed_write
        batch = self.batch(); prepared = write_batch.plan(self.root, batch)
        queued = write_retry.enqueue(self.host, self.root, "writer", "batch", {"batch": write_batch.normalize(batch)})
        altered = deepcopy(prepared)
        change = next(c for c in altered["effect"]["changes"] if c["path"] == "docs/campaigns/imported.md")
        body = decode(change["after"]); board, matches = boards._parse(body, change["path"])
        board["items"][0]["title"] = "Injected title outside requested source derivation"
        change["after"] = base64.b64encode(boards._replace(body, matches[0], board)).decode()
        altered["fingerprint"] = digest(ts.encoded(altered["effect"]))
        effects.validate_plan(LocalRoot(self.root), altered["effect"])
        board_import.native_binding(LocalRoot(self.root), self.host, "writer", queued["request_id"], write_batch.normalize(batch))
        before = {p: LocalRoot(self.root).read(p) for p in altered["paths"]}
        with self.assertRaises(ts.ReadError) as rejected:
            write_batch.apply_prepared(self.root, self.host, "writer", altered, queued["request_id"])
        self.assertIn("source-derived", rejected.exception.message)
        coordinator, authority = connect(self.root, self.host, "writer")
        with coordinator._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE id=?", (queued["request_id"],)).fetchone()[0], 0)
        with self.assertRaises(ts.ReadError):
            managed_write.apply(self.root, self.host, "writer", queued["request_id"], "generic-effect",
                altered["effect"], altered["fingerprint"], altered["paths"],
                {"completed": "Fixture", "next": "None", "pending": None})
        generation = coordinator.start(authority, queued["request_id"])
        coordinator.reserve(authority, queued["request_id"], generation, altered["paths"])
        with self.assertRaises(ts.ReadError):
            host.selected_apply(coordinator, authority, queued["request_id"], generation, "generic-effect",
                                altered["effect"], altered["fingerprint"])
        with coordinator._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM effects WHERE attempt=?", (queued["request_id"],)).fetchone()[0], 0)
        for path, raw in before.items(): self.assertEqual(LocalRoot(self.root).read(path), raw)
        coordinator.closeout(authority, queued["request_id"], generation,
                             {p: digest(raw) for p, raw in before.items()},
                             {"completed": "Rejected before writes", "next": "No action", "pending": None})

    def test_direct_completed_typed_apply_recovers_frozen_without_source_or_writes(self):
        batch = self.batch(); prepared = write_batch.plan(self.root, batch)
        result = self.submit(batch)
        with mock.patch.object(GitView, "read", side_effect=AssertionError("Completed apply replanned source")), \
                mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Completed apply rewrote files")):
            repeated = write_batch.apply_prepared(self.root, self.host, "writer", prepared, result["request_id"])
        self.assertTrue(repeated["complete"])
        self.assertEqual(repeated["writes"], 0)

    def test_legacy_managed_route_remains_readable_with_surviving_receipt(self):
        real_plan = write_batch.plan
        def legacy_plan(*args, **kwargs):
            planned = real_plan(*args, **kwargs)
            planned["effect"]["board_import"]["profile"] = board_import.EFFECT_V1
            planned["effect"]["board_import"].pop("original_batch")
            planned["fingerprint"] = digest(ts.encoded(planned["effect"]))
            return planned
        with mock.patch.object(write_batch, "plan", side_effect=legacy_plan): result = self.submit(self.batch())
        self.assertEqual(ts.loads(self.journal(result)["plan"])["board_import"]["profile"], board_import.EFFECT_V1)
        self.submit(self.batch(self.undo_request(result), "legacy-inverse"))

    def test_ordinary_native_batch_unchanged_and_stale_inverse_refused(self):
        ordinary = {"profile": write_batch.PROFILE, "operation_id": "ordinary-fixture", "operations": [{"kind": "update", "request": {
            "owner_path": "docs/WORK_ITEMS.md", "expected_sha256": digest(LocalRoot(self.root).read("docs/WORK_ITEMS.md")),
            "item_id": "LIVE", "changes": {"summary": "Ordinary native operation"}}}]}
        result = self.submit(ordinary)
        self.assertNotIn("board_import", ts.loads(self.journal(result)["plan"]))
        result = self.submit(self.batch()); undo = self.undo_request(result)
        with (self.root / "docs/campaigns/imported.md").open("ab") as stream: stream.write(b"Changed afterimage\n")
        with self.assertRaises(ts.ReadError): write_batch.plan(self.root, self.batch(undo, "stale-inverse"))


if __name__ == "__main__":
    unittest.main()
