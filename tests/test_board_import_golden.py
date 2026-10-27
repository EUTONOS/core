"""Frozen v2 intent/binding compatibility; no import admission or inverse replay.

The JSON vector was authored with stdlib JSON/SHA256, independently of the
functions under test. Never regenerate its expectations when those functions
change: an intentional format change needs an explicit old-v2 compatibility
decision. Synthetic source hashes establish no real source or authorization.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from eutonos import board_import, tokenslang as ts, write_batch, write_retry
from eutonos.effects import LocalRoot
from eutonos.source import GitView


class BoardImportGoldenTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / "fixtures" / "board_import_v2_golden.json"
        self.golden = json.loads(path.read_text(encoding="utf-8"))

    def test_frozen_v2_normalization_encoding_and_metadata_remain_readable(self):
        golden = self.golden
        original = golden["normalized_batch"]
        # Compare against frozen values, never normalize to construct expected.
        self.assertEqual(write_batch.normalize(golden["raw_batch"]), original,
                         "Generated operation identity or normalized v2 payload changed")
        self.assertEqual(write_batch.normalize(original), original,
                         "Stored v2 normalized intent is no longer a fixed point")
        encoded = golden["canonical_batch_utf8"].encode("utf-8")
        self.assertEqual(json.loads(encoded), original)
        self.assertEqual(ts.encoded(original), encoded,
                         "Canonical retained v2 encoding changed")
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), golden["batch_sha256"])
        self.assertEqual(golden["journal_metadata"]["request_sha256"], golden["batch_sha256"])
        with mock.patch.object(GitView, "read", side_effect=AssertionError("Metadata validation read Git")), \
                mock.patch.object(LocalRoot, "read", side_effect=AssertionError("Metadata validation read root")):
            self.assertEqual(board_import.validate_metadata(golden["journal_metadata"]), original,
                             "Old EFFECT_V2 retained journal metadata is no longer accepted")

    def test_frozen_request_identity_uses_root_string_and_operation_id(self):
        golden = self.golden
        binding = golden["binding"]
        basis = golden["request_id_basis_utf8"].encode("utf-8")
        self.assertEqual(json.loads(basis), {"root": binding["root"],
                                          "operation_id": golden["normalized_batch"]["operation_id"]})
        self.assertEqual("write-batch-" + hashlib.sha256(basis).hexdigest()[:24], binding["id"])
        for actor in (binding["actor"], "different-actor"):
            self.assertEqual(write_retry._request_id(binding["root"], actor, "batch",
                                                     {"batch": golden["normalized_batch"]}), binding["id"],
                             "Old native batch identity basis changed; actor is bound separately")

    def test_frozen_binding_through_actual_read_only_native_lookup(self):
        golden = self.golden
        binding = golden["binding"]
        with tempfile.TemporaryDirectory() as temporary:
            host = Path(temporary)
            database = host / write_retry.DATABASE
            # Fixture construction only. Do not enqueue, enroll, or apply: this
            # historical row exercises validation, not successful admission.
            db = sqlite3.connect(database)
            try:
                db.execute("CREATE TABLE batch_bindings (id TEXT PRIMARY KEY, root TEXT NOT NULL, "
                           "actor TEXT NOT NULL, payload_sha256 TEXT NOT NULL, created_at REAL NOT NULL)")
                db.execute("INSERT INTO batch_bindings VALUES (?,?,?,?,?)",
                           tuple(binding[key] for key in ("id", "root", "actor", "payload_sha256", "created_at")))
                db.commit()
            finally:
                db.close()
            before = database.read_bytes()
            # native_binding only consumes local.root. A plain fixed string
            # preserves exact old identity on Windows and POSIX; LocalRoot's
            # physical-path normalization is deliberately outside this vector.
            local = SimpleNamespace(root=binding["root"])
            with mock.patch.object(GitView, "read", side_effect=AssertionError("Binding lookup read source")), \
                    mock.patch.object(LocalRoot, "replace", side_effect=AssertionError("Binding lookup wrote root")):
                original = board_import.validate_metadata(golden["journal_metadata"])
                board_import.native_binding(local, host, binding["actor"], binding["id"], original)
                with self.assertRaises(ts.ReadError):
                    board_import.native_binding(local, host, "different-actor", binding["id"], original)
                changed = deepcopy(original)
                changed["requested_by"]["value"] = "Changed retained payload"
                with self.assertRaises(ts.ReadError):
                    board_import.native_binding(local, host, binding["actor"], binding["id"], changed)
                with self.assertRaises(ts.ReadError):
                    board_import.native_binding(SimpleNamespace(root=binding["root"] + "/other"), host,
                                                binding["actor"], binding["id"], original)
            self.assertEqual(database.read_bytes(), before, "Read-only binding lookup changed its fixture store")


if __name__ == "__main__":
    unittest.main()
