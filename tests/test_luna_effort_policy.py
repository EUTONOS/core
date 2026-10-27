"""Owner effort policy for Luna selections, separate from native capability evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog


class LunaEffortPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home),
                                                              "EUTONOS_HOME": str(self.home / "eutonos-home")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def cache(self, *models):
        rows = [{"slug": model, "visibility": "list",
                 "supported_reasoning_levels": [{"effort": effort} for effort in efforts]}
                for model, efforts in models]
        (self.home / "models_cache.json").write_text(
            json.dumps({"models": rows}), encoding="utf-8", newline="\n")

    def test_native_capabilities_stay_true_while_luna_picker_is_policy_filtered(self):
        self.cache(("gpt-6-luna", ("low", "medium", "high", "xhigh", "max")),
                   ("gpt-6-sol", ("low", "medium", "high", "xhigh")))
        catalog = model_catalog.describe_catalog()["models"]

        self.assertEqual(catalog["gpt-6-luna"]["supported_efforts"],
                         ("low", "medium", "high", "xhigh", "max"))
        self.assertEqual(catalog["gpt-6-luna"]["efforts"], ("xhigh", "max"))
        self.assertEqual(catalog["luna"]["supported_efforts"],
                         catalog["gpt-6-luna"]["supported_efforts"])
        self.assertEqual(catalog["luna"]["efforts"], ("xhigh", "max"))
        self.assertEqual(catalog["gpt-6-sol"]["efforts"],
                         catalog["gpt-6-sol"]["supported_efforts"])

    def test_omitted_luna_effort_defaults_to_xhigh_without_rewriting_exact_pin(self):
        self.cache(("gpt-6-luna", ("low", "high", "xhigh", "max")),
                   ("gpt-7-luna", ("xhigh", "max")))

        family = model_catalog.resolve_choice("luna", "codex")
        pin = model_catalog.resolve_choice("gpt-6-luna", "codex")

        self.assertEqual((family["resolved"], family["selection_kind"], family["default_effort"]),
                         ("gpt-7-luna", "family", "xhigh"))
        self.assertEqual((pin["resolved"], pin["selection_kind"], pin["default_effort"]),
                         ("gpt-6-luna", "pin", "xhigh"))
        self.assertEqual(family["allowed_efforts"], ("xhigh", "max"))

    def test_omitted_luna_effort_uses_max_only_when_xhigh_is_not_supported(self):
        self.cache(("gpt-6-luna", ("low", "medium", "max")),)

        result = model_catalog.resolve_choice("luna", "codex")

        self.assertEqual(result["default_effort"], "max")
        self.assertEqual(result["allowed_efforts"], ("max",))
        self.assertEqual(result["supported_efforts"], ("low", "medium", "max"))

    def test_explicit_lower_luna_effort_is_refused_without_bumping(self):
        self.cache(("gpt-6-luna", ("low", "medium", "high", "xhigh", "max")),)

        with self.assertRaisesRegex(ValueError, "below the owner policy minimum"):
            model_catalog.resolve_choice("gpt-6-luna", "codex", effort="high")
        explicit = model_catalog.resolve_choice("gpt-6-luna", "codex", effort="xhigh")
        self.assertEqual(explicit["resolved"], "gpt-6-luna")
        self.assertNotIn("default_effort", explicit)

    def test_omitted_luna_effort_refuses_when_native_catalog_has_no_allowed_effort(self):
        self.cache(("gpt-6-luna", ("low", "medium", "high")),)

        catalog = model_catalog.describe_catalog()["models"]
        self.assertEqual(catalog["gpt-6-luna"]["supported_efforts"], ("low", "medium", "high"))
        self.assertEqual(catalog["gpt-6-luna"]["efforts"], ())
        with self.assertRaisesRegex(ValueError, "no natively supported effort at or above XHigh"):
            model_catalog.resolve_choice("luna", "codex")

    def test_non_luna_effort_choices_and_result_shape_are_unchanged(self):
        self.cache(("gpt-6-sol", ("low", "medium", "high")),)

        result = model_catalog.resolve_choice("gpt-6-sol", "codex", effort="high")

        self.assertEqual(result["supported_efforts"], ("low", "medium", "high"))
        self.assertEqual(set(result), {"requested", "resolved", "selection_kind", "family",
                                      "supported_efforts", "basis"})

    def test_future_native_higher_effort_respects_minimum_without_a_second_catalog(self):
        self.cache(("gpt-7-luna", ("high", "ultra")),)
        result = model_catalog.resolve_choice("luna", "codex")
        self.assertEqual(result["default_effort"], "ultra")
        self.assertEqual(result["allowed_efforts"], ("ultra",))
        self.assertEqual(model_catalog.resolve_choice("luna", "codex", effort="ultra")["resolved"],
                         "gpt-7-luna")


if __name__ == "__main__":
    unittest.main()
