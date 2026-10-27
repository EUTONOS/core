import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import companion_control as companion


class CompanionControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def path(self):
        return Path(self.temp.name) / ".eutonos-local" / "companion.json"

    def test_default_is_off_adaptive_and_primary_is_unchanged(self):
        result = companion.control(self.temp.name)
        self.assertFalse(result["enabled"])
        # Since the routing-profiles feature (settings.py's read_companion, commit
        # 912551c), an unconfigured companion.json defaults its routing mode to
        # "profile" (follow the configured routing_profile) rather than "adaptive";
        # "adaptive" is now reserved for a routing mode explicitly chosen via
        # control(..., "on") with no fixed model. Both modes fix no model and are
        # otherwise interchangeable in validate_routing.
        self.assertEqual(result["routing"], "profile")
        self.assertIsNone(result["model"])
        self.assertEqual(result["allowed_models"], list(companion.MODELS))
        self.assertIn("primary", result["primary_model"])
        self.assertIn("irrelevant", result["context_policy"])

    def test_on_without_model_selects_adaptive(self):
        result = companion.control(self.temp.name, "on")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["routing"], "adaptive")
        self.assertIsNone(result["model"])
        self.assertIsNone(result["effort"])

    def test_all_explicit_helper_models_are_fixed_selectable(self):
        for model in companion.MODELS:
            effort = "xhigh" if model == "luna" or model.endswith("-luna") else "high"
            result = companion.control(self.temp.name, "on", model, effort)
            self.assertTrue(result["enabled"])
            self.assertEqual(result["routing"], "fixed")
            self.assertEqual(result["model"], model)
            self.assertEqual(result["effort"], effort)

    def test_off_preserves_selected_helper_settings(self):
        companion.control(self.temp.name, "on", "gpt-6-sol", "medium")
        result = companion.control(self.temp.name, "off")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["routing"], "fixed")
        self.assertEqual(result["model"], "gpt-6-sol")
        self.assertEqual(result["effort"], "medium")
        self.assertEqual(result["allowed_models"], list(companion.MODELS))

    def test_invalid_states_are_rejected(self):
        invalid = [
            {"profile": companion.PROFILE, "enabled": True, "routing": "adaptive", "model": None,
             "effort": None, "allowed_models": []},
            {"profile": companion.PROFILE, "enabled": True, "routing": "fixed", "model": "gpt-6-unknown",
             "effort": None, "allowed_models": list(companion.MODELS)},
            {"profile": companion.PROFILE, "enabled": True, "routing": "adaptive", "model": None,
             "effort": None, "allowed_models": None},
            {"profile": companion.PROFILE, "enabled": True, "routing": "adaptive", "model": None,
             "effort": None, "allowed_models": [["gpt-6-luna"]]},
            {"profile": companion.PROFILE, "enabled": True, "routing": ["adaptive"], "model": None,
             "effort": None, "allowed_models": list(companion.MODELS)},
            {"profile": companion.PROFILE, "enabled": True, "routing": "adaptive", "model": None,
             "effort": "extreme", "allowed_models": list(companion.MODELS)},
        ]
        for state in invalid:
            with self.subTest(state=state):
                path = self.path()
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Invalid companion control"):
                    companion.control(self.temp.name)

    def test_non_object_top_level_states_are_rejected(self):
        path = self.path()
        path.parent.mkdir()
        for state in ([], None):
            with self.subTest(state=state):
                path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Invalid companion control"):
                    companion.control(self.temp.name)

    def test_adaptive_global_effort_is_valid_and_preserved(self):
        result = companion.control(self.temp.name, "on", effort="high")
        self.assertEqual((result["routing"], result["model"], result["effort"]),
                         ("adaptive", None, "high"))
        self.assertEqual(companion.control(self.temp.name)["effort"], "high")

    def test_model_effort_compatibility_is_validated(self):
        with self.assertRaisesRegex(ValueError, "Unsupported effort for helper model"):
            companion.control(self.temp.name, "on", "gpt-6-luna", "ultra")

    def test_narrow_allowlist_is_preserved_and_blocks_other_fixed_model(self):
        # The v3 companion-trial-control schema (settings.validate_routing, commit
        # 912551c) added "routing_profile" and "max_concurrent_companions" as
        # required fields; a hand-written v3 state must include them or
        # read_companion rejects the whole file as invalid.
        state = {"profile": companion.PROFILE, "enabled": False, "routing": "adaptive", "model": None,
                 "effort": None, "allowed_models": ["gpt-6-luna", "gpt-6-astra"],
                 "routing_profile": "optimus", "max_concurrent_companions": 1}
        path = self.path()
        path.parent.mkdir()
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(companion.control(self.temp.name, "on")["allowed_models"], state["allowed_models"])
        self.assertEqual(companion.control(self.temp.name, "off")["allowed_models"], state["allowed_models"])
        with self.assertRaisesRegex(ValueError, "outside the configured allowed models"):
            companion.control(self.temp.name, "on", "gpt-6-sol", "medium")

    def test_upgrade_requires_real_approval_and_timeout_falls_back(self):
        decide = companion.routing_decision
        # gpt-6-sol (index 2) outranks gpt-6-luna (index 1), so this pair
        # exercises the same "needs approval" path the old luna/terra pair did.
        self.assertEqual(decide("gpt-6-luna", "gpt-6-sol")["outcome"], "approval-required")
        self.assertEqual(decide("gpt-6-luna", "gpt-6-sol", waited_seconds=59)["wait_seconds_remaining"], 1)
        for args in ({"waited_seconds": 60}, {"approval": "denied"}):
            result = decide("gpt-6-luna", "gpt-6-sol", **args)
            self.assertEqual(result["outcome"], "continue-primary")
            self.assertEqual(result["model"], "gpt-6-luna")
        self.assertEqual(decide("gpt-6-luna", "gpt-6-sol", approval="approved")["model"], "gpt-6-sol")
        self.assertEqual(decide("gpt-6-luna", "gpt-6-sol", user_selected=True)["basis"], "explicit-user-selection")
        self.assertEqual(decide("gpt-6-sol", "gpt-6-luna")["outcome"], "selected")
        self.assertEqual(decide("gpt-6-astra", "gpt-6-astra")["outcome"], "selected")

    def test_routing_rejects_unknown_models_and_invalid_wait(self):
        for seconds in (-1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                companion.routing_decision("gpt-6-astra", "gpt-6-luna", waited_seconds=seconds)
        with self.assertRaises(ValueError):
            companion.routing_decision("unknown", "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
