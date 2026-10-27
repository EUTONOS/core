import unittest
import tempfile
from pathlib import Path

from eutonos.workflow import PROFILE
from eutonos.workflow_diagnostics import diagnose, read_events, record


class WorkflowDiagnosticsTests(unittest.TestCase):
    def test_repeated_unchanged_denial_names_phase_and_narrow_action(self):
        result = {"profile": PROFILE, "outcome": "error", "command": "closeout",
                  "operation": {"attempt": "same"},
                  "result": {"outcome": "error", "code": "denied"}}
        diagnostic = diagnose({"results": [result, result]})
        self.assertEqual(diagnostic["last_failed_phase"], "ram-capture")
        self.assertEqual(diagnostic["repeated_identical"], 2)
        self.assertTrue(diagnostic["unchanged_input"])
        self.assertIn("project host identity", diagnostic["next_supported_action"])
        self.assertIn("no write grant or renewal is required", diagnostic["next_supported_action"])

    def test_different_selector_is_not_reported_as_repeated(self):
        first = {"profile": PROFILE, "outcome": "error", "command": "closeout",
                 "operation": {"attempt": "one"}, "result": {"outcome": "error", "code": "stale"}}
        second = {**first, "operation": {"attempt": "two"}}
        diagnostic = diagnose([first, second])
        self.assertEqual(diagnostic["repeated_identical"], 1)
        self.assertFalse(diagnostic["unchanged_input"])

    def test_non_workflow_input_is_refused(self):
        with self.assertRaisesRegex(ValueError, "only EUTONOS workflow"):
            diagnose([{"outcome": "error", "code": "stale"}])

    def test_record_retains_only_outcome_metadata_and_default_history(self):
        with tempfile.TemporaryDirectory(prefix="workflow-diagnostics-") as root:
            result = {"profile": PROFILE, "outcome": "error", "command": "closeout",
                      "values": {"request": "private request text"},
                      "operation": {"attempt": "same"}, "result": {"outcome": "error", "code": "denied"}}
            first = record(root, result, "2026-09-13T01:00:00+00:00")
            second = record(root, result, "2026-09-13T01:00:01+00:00")
            body = Path(first["path"]).read_text(encoding="utf-8")
            self.assertNotIn("private request text", body)
            events = read_events(root)
            self.assertEqual(len(events), 2)
            self.assertEqual(diagnose(events)["repeated_identical"], 2)

    def test_long_supplied_history_uses_configurable_window_without_rejection(self):
        result = {"profile": PROFILE, "outcome": "error", "command": "closeout",
                  "operation": {"attempt": "same"}, "result": {"outcome": "error", "code": "denied"}}
        diagnostic = diagnose([result] * 70)
        self.assertEqual(diagnostic["observed_results"], 64)
        self.assertEqual(diagnostic["coverage"], {"available_results": 70, "window_limit": 64, "windowed": True})
        expanded = diagnose([result] * 70, limit=70)
        self.assertEqual(expanded["repeated_identical"], 70)


if __name__ == "__main__":
    unittest.main()
