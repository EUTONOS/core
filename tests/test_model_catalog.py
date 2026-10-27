"""Model family resolution from bounded native capability metadata."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog, settings


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home), "EUTONOS_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def cache(self, *models):
        rows = []
        for model, efforts, extras in models:
            rows.append({"slug": model, "visibility": "list",
                         "supported_reasoning_levels": [{"effort": effort} for effort in efforts],
                         **extras})
        (self.home / "models_cache.json").write_text(json.dumps({"identity": {"secret": "ignored"},
                                                                  "models": rows}), encoding="utf-8")

    def claude_report(self, **overrides):
        path = self.home / "model-watch" / "claude-models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"profile": "eutonos.claude-model-report.v1", "reported_on": "2026-09-29",
                 "source": "Claude Code native listing", "models": [
                     {"id": "claude-fable-5-1", "supported_efforts": ["low", "medium", "high"]}]}
        value.update(overrides)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_latest_stable_stays_within_family_and_preview_remains_pin(self):
        self.cache(("gpt-6-sol", ("low", "high"), {}),
                   ("gpt-6.1-sol", ("low", "medium", "ultra"), {}),
                   ("gpt-6.2-sol-preview", ("low",), {}),
                   ("gpt-7-luna", ("high",), {}),
                   ("gpt-9-sol", ("high",), {"visibility": "hide"}),
                   ("gpt-8-sol", ("high",), {"picker_enabled": False}),
                   ("gpt-reserve", ("high",), {}))
        catalog = model_catalog.describe_catalog()["models"]
        self.assertEqual(catalog["sol"]["resolved"], "gpt-6.1-sol")
        self.assertEqual(catalog["luna"]["resolved"], "gpt-7-luna")
        self.assertIn("gpt-6.2-sol-preview", catalog)
        self.assertNotIn("gpt-9-sol", catalog)
        self.assertNotIn("gpt-8-sol", catalog)
        self.assertNotIn("gpt-reserve", catalog)
        self.assertNotIn("secret", repr(catalog))

    def test_numeric_version_order_and_exact_pin(self):
        self.cache(("gpt-6.9-sol", ("high",), {}), ("gpt-6.10-sol", ("medium",), {}))
        self.assertEqual(model_catalog.resolve_choice("sol", "codex")["resolved"], "gpt-6.10-sol")
        pin = model_catalog.resolve_choice("gpt-6.9-sol", "codex", effort="high")
        self.assertEqual((pin["resolved"], pin["selection_kind"]), ("gpt-6.9-sol", "pin"))
        with self.assertRaisesRegex(ValueError, "Unsupported effort"):
            model_catalog.resolve_choice("sol", "codex", effort="ultra")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            model_catalog.resolve_choice("gpt-6-sol", "codex")

    def test_native_new_model_is_visible_and_old_pin_is_preserved_on_read(self):
        self.cache(("gpt-7-astra", ("low", "high"), {}))
        self.assertEqual(model_catalog.resolve_choice("astra", "codex")["resolved"], "gpt-7-astra")
        self.assertTrue(model_catalog.is_exact_model_id("gpt-6.1-sol"))
        self.assertFalse(model_catalog.is_exact_model_id("sol"))
        self.assertFalse(model_catalog.is_exact_model_id("gpt-" + "6" * 130))
        stored = {"worker": {"options": [{"harness": "codex", "model": "gpt-6-sol", "effort": "high"}],
                             "preamble": None, "autonomy": "ask"}}
        self.assertEqual(settings.validate_roles(stored, allow_unavailable=True)["worker"]["options"][0]["model"],
                         "gpt-6-sol")
        with self.assertRaisesRegex(ValueError, "Unknown codex model"):
            settings.validate_roles(stored)

    def test_corrupt_and_oversized_cache_use_dated_fallback(self):
        path = self.home / "models_cache.json"
        path.write_text("{invalid", encoding="utf-8")
        self.assertIn("dated-fallback", model_catalog.resolve_choice("sol", "codex")["basis"])
        path.write_bytes(b"x" * (model_catalog.MAX_CACHE_BYTES + 1))
        self.assertIn("dated-fallback", model_catalog.resolve_choice("luna", "codex")["basis"])

    def test_stored_effort_survives_capability_removal_but_new_dispatch_refuses(self):
        self.cache(("gpt-6-sol", ("low",), {}))
        option = {"harness": "codex", "model": "gpt-6-sol", "effort": "high"}
        self.assertEqual(settings._validate_role_option(option, allow_unavailable=True), option)
        routing = {"enabled": True, "routing": "fixed", "model": "gpt-6-sol",
                   "effort": "high", "allowed_models": ["gpt-6-sol"],
                   "routing_profile": "optimus", "max_concurrent_companions": 1}
        settings.validate_routing(routing, allow_unavailable=True)
        with self.assertRaisesRegex(ValueError, "Unsupported effort"):
            settings._validate_role_option(option)
        with self.assertRaisesRegex(ValueError, "Invalid fixed"):
            settings.validate_routing(routing)
        with self.assertRaisesRegex(ValueError, "Unsupported effort"):
            model_catalog.resolve_choice("gpt-6-sol", "codex", effort="high")

    def test_missing_home_uses_dated_fallback(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}), mock.patch.object(
                model_catalog.Path, "home", side_effect=RuntimeError("home unavailable")):
            result = model_catalog.resolve_choice("sol", "codex")
        self.assertIn("dated-fallback", result["basis"])

    def test_new_defaults_are_families(self):
        self.assertEqual(settings.DEFAULT_ROLES["worker-hard"]["options"][0]["model"], "sol")
        self.assertEqual(settings.DEFAULT_ROLES["director"]["options"][0]["model"], "opus")

    def test_unknown_codex_family_is_exact_pin_without_alias(self):
        self.cache(("gpt-7-fable", ("medium", "high"), {}))
        catalog = model_catalog.describe_catalog()["models"]
        self.assertIn("gpt-7-fable", catalog)
        self.assertNotIn("fable", catalog)
        pin = model_catalog.resolve_choice("gpt-7-fable", "codex", effort="high")
        self.assertEqual((pin["selection_kind"], pin["family"], pin["resolved"]),
                         ("pin", None, "gpt-7-fable"))

    def test_reported_unknown_claude_family_is_exact_pin_without_alias(self):
        self.cache(("gpt-6-sol", ("high",), {}))
        self.assertTrue(model_catalog.is_exact_model_id("claude-fable-5-1"))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            model_catalog.resolve_choice("claude-fable-5-1", "claude", root=self.home)
        self.claude_report()
        catalog = model_catalog.describe_catalog(self.home)["models"]
        self.assertNotIn("fable", catalog)
        self.assertEqual(catalog["claude-fable-5-1"]["family"], None)
        pin = model_catalog.resolve_choice("claude-fable-5-1", "claude", root=self.home, effort="high")
        self.assertEqual((pin["resolved"], pin["selection_kind"]), ("claude-fable-5-1", "pin"))
        self.assertIn("claude-local-report-2026-09-29", pin["basis"])
        with self.assertRaisesRegex(ValueError, "Unsupported effort"):
            model_catalog.resolve_choice("claude-fable-5-1", "claude", root=self.home, effort="xhigh")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            model_catalog.resolve_choice("fable", "claude", root=self.home)

    def test_report_without_efforts_labels_dated_assumption_and_invalid_report_fails_closed(self):
        path = self.claude_report(models=[{"id": "claude-fable-5-1"}])
        pin = model_catalog.resolve_choice("claude-fable-5-1", "claude", root=self.home)
        self.assertIn("dated-effort-assumption-2026-10-14", pin["basis"])
        path.write_bytes(b"x" * (model_catalog.MAX_REPORT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            model_catalog.resolve_choice("claude-fable-5-1", "claude", root=self.home)


class ClaudeFiveFiveTests(unittest.TestCase):
    setUp = ModelCatalogTests.setUp

    def test_new_claude_models_are_pins_and_haiku_is_not_a_worker(self):
        ids = {row["model"] if isinstance(row, dict) and "model" in row else row for row in model_catalog._CLAUDE} \
            if not isinstance(model_catalog._CLAUDE, dict) else set(model_catalog._CLAUDE)
        text = json.dumps(sorted(map(str, ids)))
        for model in ("claude-sonnet-5-5", "claude-haiku-5-5", "claude-sonnet-5", "claude-opus-5-5"):
            self.assertIn(model, text)
        self.assertFalse(settings.worker_model_disabled_reason("claude", "claude-haiku-5-5"))
        self.assertTrue(settings.worker_model_disabled_reason("claude", "claude-haiku-4-5-20251001"))
        self.assertFalse(settings.worker_model_disabled_reason("claude", "claude-sonnet-5-5"))

    SUPPORTED = ("claude-haiku-5-5", "claude-haiku-5-5-20271001", "claude-haiku-5-6", "claude-haiku-6",
                 "claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5", "claude-fable-1", "claude-fable-5-5",
                 "sonnet", "opus", "fable", "haiku", "Haiku", "Claude-Sonnet-5-5", "claude-opus-5-5[1m]")
    UNSUPPORTED = ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-haiku-4", "claude-haiku-3-5",
                   "claude-haiku-5", "claude-haiku-5-20271001", "claude-3-haiku-20240307",
                   "claude-3-5-sonnet-20241022", "claude-3-opus-20240229", "claude-sonnet-3-7",
                   "claude-mystery-9", "claude-haiku", "claude-haiku-20251001", "", None, 5)

    def test_auto_mode_supported_follows_the_owner_rule(self):
        for model in self.SUPPORTED:
            self.assertTrue(model_catalog.auto_mode_supported(model), model)
        for model in self.UNSUPPORTED:
            self.assertFalse(model_catalog.auto_mode_supported(model), model)
        # Every Claude model the catalog carries is judged consistently with the rule.
        self.assertTrue(all(model_catalog.auto_mode_supported(m) for m in model_catalog._CLAUDE
                            if "haiku-4" not in m))
        self.assertFalse(model_catalog.auto_mode_supported("claude-haiku-4-5-20251001"))

    def test_bare_haiku_resolves_to_the_latest_haiku_like_the_other_families(self):
        latest = max((m for m in model_catalog._CLAUDE if m.startswith("claude-haiku-")),
                     key=model_catalog._version)
        self.assertEqual(latest, "claude-haiku-5-5")
        self.assertTrue(model_catalog.auto_mode_supported(latest))
        self.assertTrue(model_catalog.auto_mode_supported("haiku"))
        self.assertFalse(model_catalog.auto_mode_refused("haiku"))

    def test_auto_mode_refused_only_names_claude_models_that_cannot_run_it(self):
        for model in ("claude-haiku-4-5-20251001", "claude-3-opus-20240229", "claude-haiku-5"):
            self.assertTrue(model_catalog.auto_mode_refused(model), model)
        for model in ("claude-haiku-5-5", "haiku", "Haiku", "claude-sonnet-5-5", "gpt-6-luna", "default", "", None):
            self.assertFalse(model_catalog.auto_mode_refused(model), model)

    def test_the_crosstalk_kit_parity_copy_matches_the_helper(self):
        import importlib.util
        kit = Path(__file__).resolve().parents[1] / "tools" / "crosstalk" / "crosstalk_p0_core.py"
        spec = importlib.util.spec_from_file_location("crosstalk_core_for_auto_mode_parity", kit)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        extra = ("claude-opus-4-1", "claude-sonnet-4-5", "default", "gpt-6-luna", "m",
                 "claude-haiku-5-5[1m]", " Claude-Haiku-5-5 ")
        for model in self.SUPPORTED + self.UNSUPPORTED + extra:
            with self.subTest(model=model):
                self.assertEqual(core._auto_mode_supported(model), model_catalog.auto_mode_supported(model))
                self.assertEqual(core._auto_mode_refused(model), model_catalog.auto_mode_refused(model))

    def test_write_claude_report_round_trips_and_rejects_bad_ids(self):
        payload = model_catalog.write_claude_report(["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
                                                    reported_on="2026-10-14")
        self.assertEqual([m["id"] for m in payload["models"]], ["claude-sonnet-5-5", "claude-opus-5-5"])
        read = model_catalog.read_claude_capabilities()
        self.assertEqual(read["status"], "ok")
        self.assertEqual(set(read["models"]), {"claude-sonnet-5-5", "claude-opus-5-5"})
        for bad in ("sonnet", "gpt-5.5", "claude-x; rm"):
            with self.assertRaises(ValueError):
                model_catalog.write_claude_report([bad])
        with self.assertRaises(ValueError):
            model_catalog.write_claude_report([])


if __name__ == "__main__":
    unittest.main()
