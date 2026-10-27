"""Offline, deterministic policy and accounting tests. No API calls or credentials."""
import unittest
from decimal import Decimal

from eutonos.prompt_cache_policy import (
    CacheCapability, InputRates, InputUsage, forecast_reuse,
    input_cost_usd, preview_explicit_request, read_input_usage,
)


class PromptCachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.rates = InputRates("1", "0.1")
        self.ready = CacheCapability(True, True, True)
        self.usage = {
            "input_tokens": 12000,
            "input_tokens_details": {
                "cached_tokens": 4000,
                "cache_write_tokens": 7000,
                "cache_write_tokens_details": {
                    "ttl_30m_tokens": 3000,
                    "ttl_12h_tokens": 4000,
                },
            },
        }

    def test_complete_api_usage_separates_cache_reads_writes_and_ordinary(self):
        observed = read_input_usage(self.usage)
        self.assertEqual(
            (observed.input_tokens, observed.cached_tokens,
             observed.ttl_30m_tokens, observed.ttl_12h_tokens,
             observed.ordinary_tokens),
            (12000, 4000, 3000, 4000, 1000),
        )

    def test_input_cost_uses_full_write_multipliers_not_surcharges(self):
        observed = read_input_usage(self.usage)
        # 1000 ordinary + 4000 * .1 read + 3000 * 1.25 write
        # + 4000 * 2.5 write, at $1 per million.
        self.assertEqual(input_cost_usd(observed, self.rates), Decimal("0.01515"))

    def test_missing_accounting_is_unknown_not_free(self):
        cases = [
            {},
            {"input_tokens": 12000},
            {"input_tokens": 12000, "input_tokens_details": {"cached_tokens": 10}},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_input_usage(value)

    def test_broken_write_breakdown_is_rejected(self):
        import copy
        bad = copy.deepcopy(self.usage)
        bad["input_tokens_details"]["cache_write_tokens"] = 6999
        with self.assertRaisesRegex(ValueError, "disagree"):
            read_input_usage(bad)
        bad = copy.deepcopy(self.usage)
        del bad["input_tokens_details"]["cache_write_tokens_details"]["ttl_12h_tokens"]
        with self.assertRaises(ValueError):
            read_input_usage(bad)

    def test_negative_bool_and_overlap_counters_fail_closed(self):
        with self.assertRaises(ValueError):
            InputUsage(100, 50, 50, 1)
        for value in (-2, True, 3.14):
            bad = dict(self.usage)
            bad["input_tokens"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_input_usage(bad)

    def test_rates_refuse_nan_and_negative(self):
        for rate in ("NaN", "Infinity", "-0.01", True):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                InputRates(rate, "0.1")

    def test_unsupported_api_model_and_owner_flag_prohibit_policy(self):
        for capability in (
            CacheCapability(),
            CacheCapability(True, False, True),
            CacheCapability(False, True, True),
            CacheCapability(True, True, False),
        ):
            with self.subTest(capability=capability):
                f = forecast_reuse(
                    200000, 5, 10, self.rates, capability, horizon_hours=8,
                )
                self.assertEqual(f.policy, "unsupported")
                self.assertEqual(set(f.candidates_usd), {"uncached"})
                with self.assertRaises(ValueError):
                    preview_explicit_request(
                        "stable", "working", "question", stable_ttl="12h",
                        working_ttl="30m", capability=capability,
                    )

    def test_uncacheable_prefix_is_not_promoted(self):
        f = forecast_reuse(1023, 3, 10, self.rates, self.ready, horizon_hours=8)
        self.assertEqual(f.policy, "uncached")

    def test_single_call_benefit_does_not_pay_for_a_write(self):
        f = forecast_reuse(200000, 1, 1, self.rates, self.ready, horizon_hours=8)
        self.assertEqual(f.policy, "uncached")

    def test_frequent_single_window_prefers_30m(self):
        f = forecast_reuse(200000, 1, 5, self.rates, self.ready, horizon_hours=4)
        self.assertEqual(f.policy, "30m")
        self.assertLess(f.candidates_usd["30m"], f.candidates_usd["12h"])

    def test_three_idle_windows_inside_12h_can_prefer_long_cache(self):
        f = forecast_reuse(200000, 3, 1, self.rates, self.ready, horizon_hours=8)
        self.assertEqual(f.policy, "12h")
        self.assertLess(f.candidates_usd["12h"], f.candidates_usd["30m"])

    def test_two_infrequent_requests_should_avoid_long_write(self):
        f = forecast_reuse(200000, 2, 1, self.rates, self.ready, horizon_hours=8)
        self.assertEqual(f.policy, "uncached")
        self.assertGreater(f.candidates_usd["12h"], f.candidates_usd["uncached"])

    def test_later_than_12h_or_unknown_horizon_excludes_long_ttl(self):
        for horizon in (None, 12.01, 24):
            f = forecast_reuse(
                200000, 3, 10, self.rates, self.ready, horizon_hours=horizon,
            )
            with self.subTest(horizon=horizon):
                self.assertNotIn("12h", f.candidates_usd)
                self.assertEqual(f.policy, "30m")

    def test_cached_read_pricing_is_provider_supplied(self):
        cheap = forecast_reuse(
            200000, 3, 2, InputRates("1", "0.05"), self.ready, horizon_hours=8,
        )
        other = forecast_reuse(
            200000, 3, 2, InputRates("1", "0.1"), self.ready, horizon_hours=8,
        )
        self.assertLess(cheap.candidates_usd["12h"], other.candidates_usd["12h"])

    def test_upgrading_existing_30m_to_12h_is_new_full_write(self):
        short = input_cost_usd(InputUsage(200000, 0, 200000, 0), self.rates)
        upgraded = input_cost_usd(InputUsage(200000, 0, 0, 200000), self.rates)
        self.assertEqual(short, Decimal("0.25"))
        self.assertEqual(upgraded, Decimal("0.5"))
        self.assertEqual(short + upgraded, Decimal("0.75"))

    def test_preview_mixed_lifetimes_uses_explicit_content_breakpoints(self):
        payload = preview_explicit_request(
            "stable knowledge", "working session", "latest question",
            stable_ttl="12h", working_ttl="30m", capability=self.ready,
        )
        self.assertEqual(payload["prompt_cache_options"]["mode"], "explicit")
        content = [message["content"][0] for message in payload["input"]]
        self.assertEqual(content[0]["prompt_cache_breakpoint"]["ttl"], "12h")
        self.assertEqual(content[1]["prompt_cache_breakpoint"]["ttl"], "30m")
        self.assertNotIn("prompt_cache_breakpoint", content[2])
        self.assertEqual(content[2]["text"], "latest question")

    def test_preview_refuses_unsupported_marker_order_or_invalid_ttl(self):
        for a, b in (("30m", "12h"), (None, "30m"), ("24h", None)):
            with self.subTest(first=a, second=b), self.assertRaises(ValueError):
                preview_explicit_request(
                    "stable", "working", "question",
                    stable_ttl=a, working_ttl=b, capability=self.ready,
                )

    def test_preview_does_not_require_secret_or_network(self):
        payload = preview_explicit_request(
            "stable", "dynamic", "task",
            stable_ttl="12h", working_ttl=None, capability=self.ready,
        )
        self.assertNotIn("model", payload)
        self.assertNotIn("api_key", str(payload).lower())
        self.assertEqual(len(payload["input"]), 3)

    def test_bad_forecast_dimensions_are_rejected(self):
        for kwargs in (
            {"prefix_tokens": True, "windows": 1, "calls_per_window": 1},
            {"prefix_tokens": 10000, "windows": 0, "calls_per_window": 1},
            {"prefix_tokens": 10000, "windows": 1, "calls_per_window": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                forecast_reuse(**kwargs, rates=self.rates,
                               capability=self.ready, horizon_hours=8)


if __name__ == "__main__":
    unittest.main()
