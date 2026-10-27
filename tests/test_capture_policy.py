import tempfile
from pathlib import Path
import unittest

from eutonos import capture_policy


class CapturePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-policy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = (self.root, "agent", "work", "session")

    def test_policy_matrix(self):
        for cadence, work_size, event, eligible in (
                ("every-turn", "quick", "turn", True),
                ("work-sized", "quick", "turn", False),
                ("work-sized", "task", "turn", True),
                ("work-sized", "campaign", "turn", False),
                ("work-sized", "campaign", "checkpoint", True),
                ("checkpoint", "task", "turn", False),
                ("checkpoint", "quick", "checkpoint", True),
                ("checkpoint", "campaign", "milestone", True),
                ("milestone", "task", "checkpoint", False),
                ("milestone", "quick", "milestone", True)):
            with self.subTest(cadence=cadence, work_size=work_size, event=event):
                self.assertEqual(capture_policy.evaluate(True, cadence, work_size, event)["eligible"], eligible)
        self.assertFalse(capture_policy.evaluate(False, "every-turn")["eligible"])

    def test_pending_is_ordered_idempotent_and_consumed_replay_stays_consumed(self):
        first = {"profile": "delta", "value": "first"}
        second = {"profile": "delta", "value": "second"}
        saved = capture_policy.defer(*self.scope, first)
        self.assertEqual(saved["outcome"], "deferred")
        self.assertEqual(capture_policy.defer(*self.scope, first)["outcome"], "already-pending")
        capture_policy.defer(*self.scope, second)
        frozen = capture_policy.snapshot(*self.scope)
        self.assertEqual([item["content"]["value"] for item in frozen], ["first", "second"])
        cleared = capture_policy.clear(*self.scope, frozen[:1], "memory-one")
        self.assertEqual((cleared["removed"], cleared["remaining"]), (1, 1))
        self.assertEqual(capture_policy.defer(*self.scope, first)["outcome"], "already-consumed")
        self.assertEqual([item["content"]["value"] for item in capture_policy.snapshot(*self.scope)], ["second"])

    def test_clear_only_removes_frozen_entries(self):
        capture_policy.defer(*self.scope, {"value": "old"})
        frozen = capture_policy.snapshot(*self.scope)
        capture_policy.defer(*self.scope, {"value": "concurrent"})
        result = capture_policy.clear(*self.scope, frozen, "memory-one")
        self.assertEqual((result["removed"], result["remaining"]), (1, 1))
        self.assertEqual(capture_policy.snapshot(*self.scope)[0]["content"]["value"], "concurrent")


if __name__ == "__main__":
    unittest.main()
