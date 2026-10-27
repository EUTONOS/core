"""Lifetime metrics stay bounded to recorded rows and qualified comparisons."""
import unittest

from datetime import datetime, timezone
from eutonos.companion_metrics import project, window
from eutonos.companion_results import CREDIT_PRICING, PRICING, standard_credit_estimate


def row(identity, *, role="primary", model="gpt-6-astra", effort="medium", usage=None, state="completed", experiment="pair"):
    return {"id": identity, "role": role, "models": [model], "efforts": [effort] if effort else [], "state": state, "experiment_id": experiment,
            "start": "2026-09-12T10:00:00Z", "end": "2026-09-12T10:01:00Z", "max_input": 100,
            "usage": usage if usage is not None else {"input_tokens": 100, "cached_input_tokens": 80,
                                                        "output_tokens": 10, "reasoning_output_tokens": 4}}


class LifetimeMetricsTests(unittest.TestCase):
    def metrics(self, runs, comparisons=()):
        experiments = [{"id": "pair", "primary_model": "gpt-6-astra", "support_model": "gpt-6-luna"}]
        return project(runs, experiments, list(comparisons), PRICING, {"enabled": True, "windows": []}, 2, 1)

    def test_orchestration_counts_do_not_invent_token_attribution(self):
        observed = row("observed")
        observed["tool_counts"] = {"collaboration.spawn_agent": 2, "send_message": 3,
                                  "collaboration.wait_agent": 4, "functions.exec": 20,
                                  "application.spawn_agent": 5}
        unknown = row("unknown")
        active = row("active", state="running")
        active["tool_counts"] = {"spawn_agent": 9}
        result = self.metrics([observed, unknown, active])["orchestration"]
        self.assertEqual(result["calls"]["launches"], 2)
        self.assertEqual(result["calls"]["instructions"], 3)
        self.assertEqual(result["observed_call_count"], 9)
        self.assertEqual(result["unknown_turn_count"], 1)
        self.assertIsNone(result["attributed_tokens"])

    def test_unknown_usage_is_not_zero_and_subsets_are_not_added(self):
        result = self.metrics([row("a"), row("b", usage={})])
        self.assertIsNone(result["tokens"]["input_tokens"])
        self.assertEqual(result["tokens"]["measured_run_count"], 1)
        self.assertEqual(result["tokens"]["unknown_run_count"], 1)
        self.assertEqual(result["coverage"]["duplicate_turns_excluded"], 1)
        self.assertEqual(result["models"][0]["tokens"]["reasoning_output_tokens"], None)
        self.assertEqual(result["tokens"]["observed_input_tokens"], 100)
        self.assertAlmostEqual(result["observed"]["observed_standard_api_estimate_usd"], .00078)
        self.assertAlmostEqual(result["credits"]["standard_rate_credit_estimate"], .0195)
        self.assertEqual((result["credits"]["priced_run_count"], result["credits"]["unpriced_run_count"]), (1, 1))
        self.assertEqual(result["credit_pricing"]["checked_at"], "2026-09-13")
        self.assertIn("not actual deduction", result["credit_pricing"]["basis"])

    def test_helper_frequency_model_pair_and_date_trend(self):
        result = self.metrics([row("primary"), row("helper", role="support", model="gpt-6-luna")])
        self.assertEqual(result["helper"]["share_of_recorded_completed_agent_turns"], .5)
        self.assertEqual(result["helper"]["workgroups"]["recorded_workgroup_helper_incidence"], 1)
        self.assertEqual(result["pairs"][0]["primary_model"], "gpt-6-astra")
        self.assertEqual(result["pairs"][0]["helper_model"], "gpt-6-luna")
        self.assertEqual(result["trends"][0]["date"], "2026-09-12")
        self.assertIn("credits", result["models"][0])
        self.assertIn("credits", result["pairs"][0])
        self.assertEqual({item["effort"] for item in result["model_roles"]}, {"medium"})
        self.assertEqual(result["efforts"][0]["effort"], "medium")
        reviewed = self.metrics([row("review", role="reviewer")])
        self.assertEqual(reviewed["model_roles"][0]["role"], "reviewer")

    def test_counterfactual_is_labeled_and_not_observed_savings(self):
        result = self.metrics([row("luna", model="gpt-6-luna")])
        self.assertEqual(result["counterfactual"]["model"], "gpt-6-astra")
        self.assertIn("hypothetical", result["counterfactual"]["assumption"])
        self.assertIsNone(result["observed"]["matched_savings_usd"])

    def test_invalid_subsets_and_long_peak_are_excluded_from_price_scenarios(self):
        invalid = row("invalid", usage={"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 4, "reasoning_output_tokens": 5})
        long = row("long"); long["max_input"] = 272001
        result = self.metrics([invalid, long])
        self.assertEqual(result["tokens"]["measured_run_count"], 1)
        self.assertEqual(result["tokens"]["unknown_run_count"], 1)
        self.assertIsNone(result["observed"]["observed_standard_api_estimate_usd"])
        self.assertEqual(result["counterfactual"]["repriced_run_count"], 0)
        self.assertEqual(result["counterfactual"]["excluded_run_count"], 2)

    def test_only_qualified_matched_savings_is_totaled(self):
        result = self.metrics([row("a")], [{"savings_usd": .2}, {"savings_usd": None}])
        self.assertEqual(result["observed"]["matched_savings_sample_count"], 1)
        self.assertEqual(result["observed"]["matched_savings_usd"], .2)

    def test_exact_benchmark_stage_ids_supply_pair_without_label_guessing(self):
        primary = row("primary", experiment="unregistered")
        helper = row("helper", role="support", model="gpt-6-luna", experiment="unregistered")
        solo = row("solo", model="gpt-6-sol", experiment="unregistered")
        unknown = row("unknown", experiment="unregistered")
        suites = [{"results": [
            {"primary_model": "gpt-6-astra", "support_model": "gpt-6-luna", "answer": {"run_ids": ["primary", "helper"]}, "closeout": {"run_ids": []}},
            {"primary_model": "gpt-6-sol", "support_model": None, "answer": {"run_ids": ["solo"]}, "closeout": {"run_ids": []}},
        ]}]
        result = project([primary, helper, solo, unknown], [], [], PRICING, {"enabled": True, "windows": []}, benchmark_suites=suites)
        pairs = {(item["primary_model"], item["helper_model"]): item["run_count"] for item in result["pairs"]}
        self.assertEqual(pairs[("gpt-6-astra", "gpt-6-luna")], 2)
        self.assertEqual(pairs[("gpt-6-sol", None)], 1)
        self.assertEqual(pairs[(None, None)], 1)

    def test_conflicting_benchmark_pair_reference_is_withheld(self):
        primary = row("primary", experiment="unregistered")
        suites = [{"results": [
            {"primary_model": "gpt-6-astra", "support_model": None, "answer": {"run_ids": ["primary"]}, "closeout": {"run_ids": []}},
            {"primary_model": "gpt-6-sol", "support_model": None, "answer": {"run_ids": ["primary"]}, "closeout": {"run_ids": []}},
        ]}]
        result = project([primary], [], [], PRICING, {"enabled": True, "windows": []}, benchmark_suites=suites)
        self.assertEqual(result["pairs"][0]["primary_model"], None)
        self.assertTrue(result["diagnostics"])

    def test_relationship_counts_observed_turns_and_deduplicates_followups_by_workgroup(self):
        runs = [row("p1"), row("p2"),
                row("h1", role="support", model="gpt-6-luna"),
                row("h2", role="support", model="gpt-6-luna")]
        relationship = self.metrics(runs)["relationships"]
        self.assertEqual(relationship, [{
            "primary_model": "gpt-6-astra", "helper_model": "gpt-6-luna",
            "completed_primary_turn_count": 2, "completed_helper_turn_count": 2,
            "recorded_assisted_workgroup_count": 1,
            "workgroup_basis": "Distinct explicit benchmark result membership when available; otherwise experiment IDs. Only groups with observed completed helper turns are counted; this is not user-request frequency."}])

    def test_relationship_excludes_configured_only_primary_turns_sharing_an_assisted_pair(self):
        assisted_primary = row("assisted-primary", experiment="assisted")
        assisted_helper = row("assisted-helper", role="support", model="gpt-6-luna", experiment="assisted")
        configured_only_primary = row("configured-only-primary", experiment="configured-only")
        experiments = [
            {"id": "assisted", "primary_model": "gpt-6-astra", "support_model": "gpt-6-luna"},
            {"id": "configured-only", "primary_model": "gpt-6-astra", "support_model": "gpt-6-luna"},
        ]
        relationship = project([assisted_primary, assisted_helper, configured_only_primary], experiments, [], PRICING,
                               {"enabled": True, "windows": []})["relationships"][0]
        self.assertEqual((relationship["completed_primary_turn_count"], relationship["completed_helper_turn_count"],
                          relationship["recorded_assisted_workgroup_count"]), (1, 1, 1))

    def test_relationship_excludes_configured_only_unknown_and_conflicting_associations(self):
        configured_only = self.metrics([row("primary")])
        self.assertEqual(configured_only["relationships"], [])
        unknown = row("unknown-helper", role="support", model="gpt-6-luna", experiment="unregistered")
        self.assertEqual(project([unknown], [], [], PRICING, {"enabled": True, "windows": []})["relationships"], [])
        primary = row("primary")
        helper = row("helper", role="support", model="gpt-6-luna")
        suites = [{"results": [
            {"primary_model": "gpt-6-astra", "support_model": "gpt-6-luna", "answer": {"run_ids": ["primary", "helper"]}},
            {"primary_model": "gpt-6-sol", "support_model": "gpt-6-luna", "answer": {"run_ids": ["primary", "helper"]}},
        ]}]
        conflicting = project([primary, helper], [{"id": "pair", "primary_model": "gpt-6-astra", "support_model": "gpt-6-luna"}], [], PRICING,
                              {"enabled": True, "windows": []}, benchmark_suites=suites)
        self.assertEqual(conflicting["relationships"], [])
        self.assertTrue(conflicting["diagnostics"])

    def test_benchmark_result_groups_and_ranges_do_not_merge_shared_experiment(self):
        p1, h1 = row("p1", experiment="shared"), row("h1", role="support", model="gpt-6-luna", experiment="shared")
        p2, h2 = row("p2", experiment="shared"), row("h2", role="support", model="gpt-6-luna", experiment="shared")
        p1["end"] = h1["end"] = "2026-09-12T09:55:00Z"
        p2["end"] = h2["end"] = "2026-09-12T08:55:00Z"
        suites = [{"results": [
            {"primary_model": "gpt-6-astra", "support_model": "gpt-6-luna", "answer": {"run_ids": ["p1", "h1"]}},
            {"primary_model": "gpt-6-astra", "support_model": "gpt-6-luna", "answer": {"run_ids": ["p2", "h2"]}},
        ]}]
        base = {"runs": [p1, h1, p2, h2], "experiments": [], "comparisons": [], "pricing": PRICING,
                "tracking": {"enabled": True, "windows": []}, "benchmark_suites": suites}
        all_time = window(base, 0)
        self.assertEqual(all_time["metrics"]["relationships"][0]["recorded_assisted_workgroup_count"], 2)
        ranged = window(base, 0, amount=10, unit="minutes", now=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
        relationship = ranged["metrics"]["relationships"][0]
        self.assertEqual((relationship["completed_primary_turn_count"], relationship["completed_helper_turn_count"], relationship["recorded_assisted_workgroup_count"]), (1, 1, 1))

    def test_credit_estimate_does_not_double_count_cached_or_reasoning_subsets(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 80,
                 "output_tokens": 10, "reasoning_output_tokens": 4}
        self.assertAlmostEqual(standard_credit_estimate(row("astra", usage=usage)), .0195)
        invalid = row("invalid", usage={**usage, "reasoning_output_tokens": 11})
        self.assertIsNone(standard_credit_estimate(invalid))
        self.assertEqual(CREDIT_PRICING["unit"], "credits per million tokens")

    def test_relative_window_includes_whole_completed_turns_by_end(self):
        inside = row("inside"); inside.update(start="2026-09-12T09:40:00Z", end="2026-09-12T09:55:00Z")
        lower = row("lower"); lower.update(end="2026-09-12T09:50:00Z")
        upper = row("upper"); upper.update(end="2026-09-12T10:00:00Z")
        unknown = row("unknown"); unknown["end"] = None
        incomplete = row("active", state="active")
        all_rows = [inside, lower, upper, unknown, incomplete]
        base = self.metrics(all_rows)
        response = window({"runs": all_rows, "experiments": [], "comparisons": [],
                           "pricing": PRICING, "tracking": {"enabled": True, "windows": []},
                           "benchmark_suites": [], "lifetime_metrics": base}, 2,
                          amount="10", unit="minutes",
                          now=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(response["project"], 2)
        self.assertEqual(response["range"]["included_turn_count"], 2)
        self.assertEqual(response["range"]["unknown_end_count"], 1)
        self.assertEqual(response["range"]["incomplete_excluded_count"], 1)
        self.assertEqual(response["range"]["out_of_range_count"], 1)
        self.assertEqual(response["metrics"]["tokens"]["input_tokens"], 200)
        self.assertEqual(response["range"]["basis"], "Completed turns ending in range; whole-turn usage")
        self.assertEqual(response["timeline"], response["metrics"]["timeline"])
        self.assertEqual(response["timeline"]["turns"][0]["model"], "gpt-6-astra")

    def test_custom_window_requires_ordered_timezone_aware_pair(self):
        base = {"runs": [], "experiments": [], "comparisons": [], "pricing": PRICING,
                "tracking": {"enabled": True, "windows": []}, "benchmark_suites": [],
                "lifetime_metrics": self.metrics([])}
        response = window(base, 0, from_value="2026-09-12T09:00:00-01:00",
                          to_value="2026-09-12T12:00:00+01:00")
        self.assertEqual((response["range"]["from"], response["range"]["to"]),
                         ("2026-09-12T10:00:00Z", "2026-09-12T11:00:00Z"))
        for kwargs in ({"amount": "0", "unit": "minutes"},
                       {"amount": "nan", "unit": "minutes"},
                       {"from_value": "2026-09-12T10:00:00", "to_value": "2026-09-12T11:00:00Z"},
                       {"from_value": "2026-09-12T11:00:00Z", "to_value": "2026-09-12T10:00:00Z"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                window(base, 0, **kwargs)

    def test_range_excludes_malformed_comparison_end_and_all_time_counts_unknown_end(self):
        unknown = row("unknown"); unknown["end"] = "not-a-time"
        base_metrics = self.metrics([unknown])
        base = {"runs": [unknown], "experiments": [],
                "comparisons": [{"end": "not-a-time", "savings_usd": 2}], "pricing": PRICING,
                "benchmark_suites": [], "lifetime_metrics": base_metrics}
        all_time = window(base, 0)
        self.assertEqual((all_time["range"]["included_turn_count"], all_time["range"]["unknown_end_count"]), (1, 1))
        ranged = window(base, 0, amount=1, unit="hours",
                        now=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(ranged["metrics"]["observed"]["matched_savings_sample_count"], 0)
        self.assertEqual(ranged["metrics"]["coverage"]["tracking"], base_metrics["coverage"]["tracking"])


if __name__ == "__main__":
    unittest.main()
