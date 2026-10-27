import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import companion_capture as capture


class CompanionCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = {"thread_id": "thread-a", "turn_id": "turn-1", "stage": "completed",
                     "disposition": "captured", "coverage": "summary-only",
                     "completion_basis": "assessment",
                     "summary": "Primary supplied summary; no transcript export.",
                     "ram": {"path": "RAM/CCP-MD/s/1-a--m.json", "revision": 2},
                     "assertion": {"native_proof": "verified-readback"}}

    def ram_readback(self):
        identity = {"work_id": "CCP-MD", "session_id": "support", "issuer_id": "codex-luna",
                    "memory_id": "pre-final-findings"}
        request = {"profile": "eutonos.tokenslang.ram-read.v1", "operation": "get", "identity": identity,
                   "revision": 13, "max_bytes": 32768}
        return request, {"profile": "eutonos.tokenslang.ram-read.v1", "outcome": "ok", "complete": True,
                          "items": [{"identity": identity, "path": "RAM/CCP-MD/support/10-luna.json",
                                     "record_revision": 13, "historical": False,
                                     "experience": {"revision": 13}}]}

    def test_record_is_idempotent_and_status_lists_completion(self):
        first = capture.write_receipt(self.temp.name, self.base)
        second = capture.write_receipt(self.temp.name, self.base)
        self.assertEqual(first["outcome"], "recorded")
        self.assertEqual(second["outcome"], "idempotent")
        self.assertEqual(capture.status(self.temp.name)["counts_completed"], [first["receipt"]["receipt_id"]])

    def test_pre_final_cannot_count_completed_and_is_pending(self):
        value = {**self.base, "stage": "pre-final", "counts_completed": True}
        with self.assertRaisesRegex(ValueError, "cannot count"):
            capture.make_receipt(value)
        value["counts_completed"] = False
        result = capture.write_receipt(self.temp.name, value)
        self.assertIn(result["receipt"]["receipt_id"], capture.status(self.temp.name)["pending"])

    def test_draft_requests_cannot_change_capture_coverage(self):
        first = capture.write_receipt(self.temp.name, self.base)
        directory = capture.root_dir(self.temp.name)
        draft = {**self.base, "profile": capture.PROFILE, "stage": "pre-final",
                 "counts_completed": True}
        for name in ("current-prefinal-receipt-request.json", "capture-request.json"):
            (directory / name).write_text(json.dumps(draft), encoding="utf-8")
        state = capture.status(self.temp.name)
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["counts_completed"], [first["receipt"]["receipt_id"]])
        self.assertEqual(len(state["receipts"]), 1)

    def test_gap_is_visible_and_native_proof_must_be_explicit(self):
        value = {**self.base, "disposition": "gap", "assertion": {"native_proof": "not-asserted"}}
        result = capture.write_receipt(self.temp.name, value)
        self.assertIn(result["receipt"]["receipt_id"], capture.status(self.temp.name)["pending"])
        with self.assertRaisesRegex(ValueError, "Native proof"):
            capture.make_receipt({**self.base, "assertion": {}})

    def test_gap_cannot_count_completed_and_captured_requires_ram(self):
        gap = capture.make_receipt({**self.base, "disposition": "gap"})
        self.assertFalse(gap["counts_completed"])
        with self.assertRaisesRegex(ValueError, "requires a RAM"):
            capture.make_receipt({**self.base, "ram": None})

    def test_ram_path_and_assertion_are_bounded(self):
        for path in ("C:/secret.json", "/secret.json", "RAM/../secret.json"):
            with self.assertRaisesRegex(ValueError, "safe relative"):
                capture.make_receipt({**self.base, "ram": {"path": path, "revision": 1}})
        with self.assertRaisesRegex(ValueError, "Native proof"):
            capture.make_receipt({**self.base, "assertion": {"native_proof": "not-asserted", "secret": "x"}})

    def test_completed_receipt_closes_prior_pre_final_pending(self):
        pre = capture.write_receipt(self.temp.name, {**self.base, "stage": "pre-final"})
        done = capture.write_receipt(self.temp.name, self.base)
        state = capture.status(self.temp.name)
        self.assertNotIn(pre["receipt"]["receipt_id"], state["pending"])
        self.assertIn(done["receipt"]["receipt_id"], state["counts_completed"])
        self.assertEqual(len(state["receipts"]), 2)

    def test_completed_receipt_can_supersede_pre_final_same_turn(self):
        pre = capture.write_receipt(self.temp.name, {**self.base, "stage": "pre-final"})
        done = capture.write_receipt(self.temp.name, {**self.base, "stage": "completed", "disposition": "no-op", "ram": None},
                                     pre["receipt"]["receipt_id"])
        state = capture.status(self.temp.name)
        self.assertNotIn(pre["receipt"]["receipt_id"], state["pending"])
        self.assertIn(done["receipt"]["receipt_id"], state["counts_completed"])

    def test_conflict_requires_supersedes(self):
        first = capture.write_receipt(self.temp.name, self.base)
        changed = {**self.base, "summary": "Changed supplied summary."}
        with self.assertRaisesRegex(ValueError, "supersedes"):
            capture.write_receipt(self.temp.name, changed)
        result = capture.write_receipt(self.temp.name, changed, first["receipt"]["receipt_id"])
        self.assertEqual(result["outcome"], "recorded")
        self.assertEqual(result["receipt"]["supersedes"], first["receipt"]["receipt_id"])
        third = capture.write_receipt(self.temp.name, {**changed, "summary": "Third correction."}, result["receipt"]["receipt_id"])
        self.assertEqual(third["outcome"], "recorded")

    def test_superseded_gap_is_not_pending(self):
        gap = capture.write_receipt(self.temp.name, {**self.base, "stage": "pre-final", "disposition": "gap", "ram": None,
                                                       "assertion": {"native_proof": "not-asserted"}})
        done = capture.write_receipt(self.temp.name, {**self.base, "stage": "pre-final", "disposition": "no-op", "ram": None,
                                                       "assertion": {"native_proof": "not-asserted"}}, gap["receipt"]["receipt_id"])
        self.assertNotIn(gap["receipt"]["receipt_id"], capture.status(self.temp.name)["pending"])
        self.assertIn(done["receipt"]["receipt_id"], capture.status(self.temp.name)["pending"])

    def test_withdrawn_identity_is_not_completed_or_pending(self):
        first = capture.write_receipt(self.temp.name, self.base)
        correction = {**self.base, "disposition": "withdrawn", "ram": None,
                      "summary": "Wrong self-reported identity; replaced by observed identity.",
                      "assertion": {"native_proof": "not-asserted"}}
        capture.write_receipt(self.temp.name, correction, first["receipt"]["receipt_id"])
        state = capture.status(self.temp.name)
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["counts_completed"], [])

    def test_receipt_is_summary_only(self):
        result = capture.write_receipt(self.temp.name, self.base)
        body = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("summary-only", body)
        self.assertNotIn("response_item", body)
        self.assertNotIn("tool_arguments", body)

    def test_completed_request_selects_latest_final_and_exact_current_ram(self):
        log = Path(self.temp.name) / "native.jsonl"
        events = [{"type": "session_meta", "payload": {"id": "thread-a"}}]
        for turn in ("turn-1", "turn-2"):
            events.extend([{"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                           {"type": "response_item", "payload": {"role": "assistant", "phase": "final_answer",
                                                                  "content": "PRIVATE"}},
                           {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}])
        events.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "active"}})
        log.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
        request, readback = self.ram_readback()
        assessment = {"disposition": "no-op", "coverage": "summary-only", "completion_basis": "assessment",
                      "summary": "The latest completed exchange is already represented by RAM revision 13."}
        value = capture.completed_request(log, assessment, request, readback)
        self.assertEqual(value["turn_id"], "turn-2")
        self.assertEqual(value["ram"], {"path": "RAM/CCP-MD/support/10-luna.json", "revision": 13})
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_completed_request_rejects_wrong_owner_historical_or_stale_readback(self):
        request, readback = self.ram_readback()
        with self.assertRaisesRegex(ValueError, "expected owner"):
            capture.verified_ram_reference(readback, {**request, "identity": {**request["identity"], "issuer_id": "other"}})
        readback["items"][0]["historical"] = True
        with self.assertRaisesRegex(ValueError, "exact latest"):
            capture.verified_ram_reference(readback, request)
        readback["items"][0]["historical"] = False
        readback["items"][0]["experience"]["revision"] = 12
        with self.assertRaisesRegex(ValueError, "exact latest"):
            capture.verified_ram_reference(readback, request)

    def test_native_ram_readback_fetches_exact_request_read_only(self):
        request, expected = self.ram_readback()
        with mock.patch.object(capture.ram, "Store") as store:
            store.return_value.read.return_value = expected
            observed = capture.native_ram_readback(self.temp.name, request)
        self.assertIs(observed, expected)
        store.return_value.read.assert_called_once_with(request)

    def test_native_ram_readback_rejects_non_get_request(self):
        request, _ = self.ram_readback()
        request["operation"] = "browse"
        with self.assertRaisesRegex(ValueError, "exact native get"):
            capture.native_ram_readback(self.temp.name, request)

    def test_receipt_timestamp_is_generated_and_exactly_read_back(self):
        with self.assertRaisesRegex(ValueError, "generated"):
            capture.make_receipt({**self.base, "recorded_at": "PLACEHOLDER"})
        observed = "2026-09-13T01:23:45.678901+00:00"
        result = capture.write_and_read_receipt(self.temp.name, self.base, recorded_at=observed)
        self.assertEqual(result["receipt"]["recorded_at"], observed)
        self.assertEqual(result["readback"], result["receipt"])
        self.assertTrue(result["counted_completed"])
        self.assertTrue(result["counted_verified"])

    def test_atomic_receipt_failure_leaves_no_partial_and_retry_succeeds(self):
        with mock.patch.object(capture.os, "link", side_effect=OSError("simulated atomic link failure")):
            with self.assertRaisesRegex(OSError, "atomic link"):
                capture.write_and_read_receipt(self.temp.name, self.base)
        directory = capture.root_dir(self.temp.name)
        self.assertEqual(list(directory.glob("capture-*.json")), [])
        self.assertEqual(list(directory.glob(".capture-write-*.tmp")), [])
        retry = capture.write_and_read_receipt(self.temp.name, self.base)
        self.assertEqual(retry["outcome"], "recorded")
        self.assertEqual(capture.write_and_read_receipt(self.temp.name, self.base)["outcome"], "idempotent")


if __name__ == "__main__":
    unittest.main()
