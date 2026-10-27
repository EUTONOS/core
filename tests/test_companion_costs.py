"""Costs require actual observed pairings, complete counters and matched quality."""
import unittest
from copy import deepcopy
from eutonos.companion_results import observed_cost, comparisons


class CostTests(unittest.TestCase):
    def setUp(self):
        self.row = {"experiment_id": "off", "state": "completed", "models": ["gpt-6-astra"], "max_input": 40000,
                    "usage": {"input_tokens": 100000, "cached_input_tokens": 90000, "output_tokens": 1000, "reasoning_output_tokens": 200}, "end": "2026-09-12T19:00:00Z"}
        self.base = {"id": "off", "mode": "off", "primary_model": "gpt-6-astra", "comparison_key": ["task", "source", "conditions"],
                     "accounting_basis": "separate-worker-logs-no-children", "assessment": {"score": 95, "limitations": ["One sample"]}}
        self.on = {**deepcopy(self.base), "id": "on", "mode": "on", "baseline_id": "off"}
        self.rows = [self.row, {**self.row, "experiment_id": "on"}, {**self.row, "experiment_id": "on", "models": ["gpt-6-luna"]}]

    def test_only_observed_model_estimate(self):
        self.assertAlmostEqual(observed_cost(self.row), .24)
        for patch in ({"models": []}, {"models": ["gpt-6-astra", "gpt-6-luna"]}, {"max_input": 272001}, {"state": "running"}, {"usage": {}}):
            self.assertIsNone(observed_cost({**self.row, **patch}))

    def test_negative_savings_is_retained(self):
        value = comparisons([self.base, self.on], self.rows)[0]
        self.assertLess(value["savings_usd"], 0)
        self.assertEqual(value["notes"], ["One sample"])

    def test_unmatched_or_worse_quality_withholds_savings(self):
        for patch in ({"comparison_key": None}, {"assessment": {"score": 94}}, {"accounting_basis": "unknown"}):
            self.assertIsNone(comparisons([self.base, {**self.on, **patch}], self.rows)[0]["savings_usd"])

    def test_reused_baseline_cannot_inflate_total(self):
        values = comparisons([self.base, self.on, {**self.on, "id": "another"}], self.rows)
        self.assertTrue(all(v["savings_usd"] is None for v in values))

if __name__ == "__main__":
    unittest.main()
