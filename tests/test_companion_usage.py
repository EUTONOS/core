"""Pilot metadata reader: counter boundaries, privacy and source changes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("companion_usage", Path(__file__).resolve().parents[1] / "tools/companion_usage.py")
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)


class CompanionUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "session.jsonl"
        self.append("session_meta", {"id": "thread-a"})
        self.append("turn_context", {"model": "gpt-6-luna"})
        self.counter(100, 80, 20, 5)
        self.before, _ = usage.collect(self.log, "support", "prepare")

    def append(self, kind, payload):
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": kind, "payload": payload}) + "\n")

    def counter(self, *values):
        self.append("event_msg", {"type": "token_count", "info": {"total_token_usage": dict(zip(usage.FIELDS, values))}})

    def report(self):
        return usage.collect(self.log, "support", "prepare", self.before)[1]

    def test_increments_deduplicate_and_preserve_subsets(self):
        self.counter(120, 90, 26, 7)
        self.counter(120, 90, 26, 7)
        self.counter(150, 100, 32, 9)
        r = self.report()
        self.assertEqual(r["provider_usage"], dict(zip(usage.FIELDS, (50, 20, 12, 4))))
        self.assertEqual(r["duplicate_snapshots"], 1)
        self.assertEqual(r["child_inclusion"], "unknown")
        self.assertIsNone(r["actual_credits"])
        self.assertEqual(r["observation_id"], self.report()["observation_id"])

    def test_reset_does_not_produce_negative_or_complete_total(self):
        self.counter(10, 5, 2, 1)
        self.counter(20, 10, 4, 2)
        r = self.report()
        self.assertEqual(r["counter_gaps"], 1)
        self.assertTrue(all(v is None for v in r["provider_usage"].values()))

    def test_missing_field_stays_unknown(self):
        self.counter(120, None, 26, 7)
        self.assertIsNone(self.report()["provider_usage"]["cached_input_tokens"])

    def test_counter_correction_cannot_create_impossible_delta(self):
        self.counter(110, 100, 26, 7)
        r = self.report()
        self.assertIsNone(r["provider_usage"]["input_tokens"])
        self.assertEqual(r["counter_gaps"], 1)

    def test_source_rewrite_and_truncation_rejected(self):
        self.log.write_bytes(self.log.read_bytes().replace(b"thread-a", b"thread-b"))
        with self.assertRaisesRegex(ValueError, "prefix changed"):
            self.report()
        self.log.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.report()

    def test_partial_append_is_retried_and_text_never_exported(self):
        self.append("response_item", {"type": "function_call", "arguments": "SECRET-ARGUMENT"})
        with self.log.open("ab") as f:
            f.write(b'{"type":"response_item","payload":{"text":"SECRET-TEXT"}')
        state, r = usage.collect(self.log, "support", "prepare", self.before)
        self.assertGreater(r["pending_line_bytes"], 0)
        self.assertEqual(r["tool_calls"], 1)
        self.assertNotIn("SECRET", json.dumps([state, r]))
        self.assertNotIn(str(self.log), json.dumps(r))
        with self.log.open("ab") as f:
            f.write(b'}\n')
        self.counter(120, 90, 26, 7)
        _, second = usage.collect(self.log, "support", "prepare", state)
        self.assertEqual(second["provider_usage"]["input_tokens"], 20)

    def test_malformed_record_reports_gap(self):
        with self.log.open("ab") as f:
            f.write(b'not-json\n')
        self.counter(120, 90, 26, 7)
        self.assertIsNone(self.report()["provider_usage"]["input_tokens"])

    def test_model_switch_is_not_priced_as_one_model(self):
        self.counter(120, 90, 26, 7)
        self.append("turn_context", {"model": "gpt-6-astra"})
        self.counter(130, 95, 28, 8)
        batch = usage.measurement_batch(self.report(), "fixture.scope", "CCP-MD", "abc", "test", "b1")
        self.assertIsNone(batch["events"][0]["provider_usage"]["model"])
        self.assertEqual(batch["visibility"], "private")
        from eutonos import measurement
        measurement.validate(batch)

    def test_wrong_role_and_log_rejected(self):
        with self.assertRaisesRegex(ValueError, "role mismatch"):
            usage.collect(self.log, "primary", "prepare", self.before)
        with self.assertRaisesRegex(ValueError, "another log"):
            usage.collect(self.log.with_name("other.jsonl"), "support", "prepare", self.before)

    def test_cli_no_clobber_and_candidate_not_published(self):
        output = Path(self.temp.name) / "before.json"
        argv = ["start", "--log", str(self.log), "--role", "support", "--output", str(output)]
        self.assertEqual(usage.main(argv), 0)
        saved = output.read_bytes()
        self.assertEqual(usage.main(argv), 2)
        self.assertEqual(output.read_bytes(), saved)

    def test_no_updates_not_inferred_free_work(self):
        r = self.report()
        self.assertEqual(r["provider_usage"]["input_tokens"], 0)
        self.assertEqual(r["coverage"], "partial")
        self.assertIn("unreported", r["limitation"])


if __name__ == "__main__":
    unittest.main()
