"""Public picker/dispatch projection of retained harness capability evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog as catalog
from eutonos import model_watch, settings


class WatchCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root), "EUTONOS_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def codex(self, *models):
        (self.root / "models_cache.json").write_text(json.dumps({"identity": "PRIVATE",
            "models": [{"slug": model, "visibility": "list", "prompt": "SECRET",
                        "supported_reasoning_levels": [{"effort": e} for e in efforts]}
                       for model, efforts in models]}), encoding="utf-8")

    def claude(self, models):
        path = catalog.watch_home() / "claude-models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"profile": "eutonos.claude-model-report.v1", "reported_on": "2026-09-30",
                                   "source": "Native listing", "private": "SECRET", "models": models}),
                        encoding="utf-8")
        return path

    def snapshot(self, harness="codex", *, status="ok", available=True, missing_since=None):
        model = "gpt-6-sol" if harness == "codex" else "claude-sonnet-5"
        return {"sources": {harness: {"status": status, "basis": f"{harness}-native-evidence",
            "initialized": True, "last_scan_at": 200, "last_good_at": 100,
            "private": "SECRET", "models": {model: {"supported_efforts": ["medium", "high"],
                "family": "sol" if harness == "codex" else "sonnet", "available": available,
                "effort_basis": "native-supported-efforts", "last_seen": 100,
                "missing_since": missing_since, "private": "SECRET"}}}}, "pending_count": 0}

    def describe_with(self, snapshot):
        with mock.patch.object(catalog, "_watch_snapshot", return_value=snapshot):
            return catalog.describe_catalog(self.root)

    def test_canonical_report_needs_no_git(self):
        self.claude([{"id": "claude-orbit-6-1", "supported_efforts": ["medium"]}])
        with mock.patch.object(catalog, "shared_root", side_effect=AssertionError("Git not needed")):
            source = catalog.read_claude_capabilities(self.root)
        self.assertEqual(set(source["models"]), {"claude-orbit-6-1"})
        self.assertEqual(catalog.watch_home(), self.root / "model-watch")

    def test_absent_canonical_is_unknown_and_corrupt_canonical_is_unavailable(self):
        self.assertEqual(catalog.read_claude_capabilities(self.root)["status"], "unknown")
        path = self.claude([])
        path.write_text("{broken", encoding="utf-8")
        source = catalog.read_claude_capabilities(self.root)
        self.assertEqual((source["status"], source["models"]), ("unavailable", {}))

    def test_native_addition_visible_before_next_watch_scan(self):
        self.codex(("gpt-6-sol", ["high"]), ("gpt-6.2-sol", ["medium", "ultra"]))
        state = self.snapshot()
        projected = self.describe_with(state)
        self.assertEqual(projected["models"]["sol"]["resolved"], "gpt-6.2-sol")
        self.assertEqual(projected["models"]["gpt-6.2-sol"]["freshness"], "current")
        self.assertEqual(projected["models"]["gpt-6-sol"]["last_seen"], 100)

    def test_partial_source_keeps_valid_individual_evidence_explicit(self):
        self.codex(("gpt-6-sol", ["high"]))
        path = self.root / "models_cache.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["models"].append({"slug": "gpt-7-sol", "visibility": "list",
                                  "supported_reasoning_levels": "malformed"})
        path.write_text(json.dumps(payload), encoding="utf-8")
        projected = self.describe_with(self.snapshot())
        self.assertEqual(projected["sources"]["codex"]["status"], "unavailable")
        self.assertEqual(projected["models"]["gpt-6-sol"]["freshness"], "current")
        self.assertEqual(projected["models"]["gpt-6-sol"]["last_seen"], 100)
        self.assertNotIn("gpt-7-sol", projected["models"])

    def test_pending_absence_retains_pin_and_alias_until_confirmed(self):
        self.codex()
        state = self.snapshot(missing_since=200)
        state["pending_count"] = 1
        projected = self.describe_with(state)
        self.assertTrue(projected["models"]["gpt-6-sol"]["available"])
        self.assertEqual(projected["models"]["gpt-6-sol"]["missing_since"], 200)
        self.assertEqual(projected["models"]["sol"]["resolved"], "gpt-6-sol")
        self.assertEqual(projected["pending_count"], 1)

    def test_retired_pin_keeps_efforts_and_saved_identity_but_new_dispatch_refuses(self):
        self.codex()
        state = self.snapshot(available=False, missing_since=200)
        with mock.patch.object(catalog, "_watch_snapshot", return_value=state):
            projected = catalog.describe_catalog(self.root)
            pin = projected["models"]["gpt-6-sol"]
            self.assertFalse(pin["available"])
            self.assertEqual(pin["supported_efforts"], ("medium", "high"))
            self.assertNotIn("sol", projected["models"])
            option = {"harness": "codex", "model": "gpt-6-sol", "effort": "high"}
            self.assertEqual(settings._validate_role_option(option, allow_unavailable=True, root=self.root), option)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                catalog.resolve_choice("gpt-6-sol", "codex", root=self.root)

    def test_native_reappearance_restores_confirmed_retirement_immediately(self):
        self.codex(("gpt-6-sol", ["low"]))
        projected = self.describe_with(self.snapshot(available=False, missing_since=200))
        pin = projected["models"]["gpt-6-sol"]
        self.assertTrue(pin["available"])
        self.assertIsNone(pin["missing_since"])
        self.assertEqual(pin["supported_efforts"], ("low",))

    def test_unknown_report_retains_last_good_models_with_explicit_staleness(self):
        self.codex()
        state = self.snapshot("claude", status="unknown")
        projected = self.describe_with(state)
        pin = projected["models"]["claude-sonnet-5"]
        self.assertTrue(pin["available"])
        self.assertEqual((pin["freshness"], pin["last_good_at"]), ("stale", 100))
        self.assertEqual(projected["sources"]["claude"]["status"], "unknown")

    def test_confirmed_claude_removal_overrides_dated_fallback(self):
        self.claude([])
        state = self.snapshot("claude", available=False, missing_since=200)
        models = state["sources"]["claude"]["models"]
        models["claude-sonnet-5-5"] = dict(models["claude-sonnet-5"])  # the newer Sonnet is retired too
        projected = self.describe_with(state)
        self.assertFalse(projected["models"]["claude-sonnet-5"]["available"])
        self.assertFalse(projected["models"]["claude-sonnet-5-5"]["available"])
        self.assertNotIn("sonnet", projected["models"])

    def test_preview_exact_and_unknown_family_never_replace_stable_alias(self):
        self.codex(("gpt-6-sol", ["high"]), ("gpt-9-sol-preview", ["low"]), ("gpt-10-fable", ["medium"]))
        projected = self.describe_with({})["models"]
        self.assertEqual(projected["sol"]["resolved"], "gpt-6-sol")
        self.assertTrue(projected["gpt-9-sol-preview"]["available"])
        self.assertEqual(projected["gpt-10-fable"]["selection_kind"], "pin")
        self.assertNotIn("fable", projected)

    def test_private_fields_do_not_enter_catalog_and_reader_does_not_write(self):
        self.codex(("gpt-6.2-sol", ["high"]))
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"], "private": "PRIVATE"}])
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with mock.patch.object(model_watch, "scan", side_effect=AssertionError("reader scanned")):
            projected = catalog.describe_catalog(self.root)
        self.assertNotIn("SECRET", repr(projected))
        self.assertNotIn("PRIVATE", repr(projected))
        self.assertNotIn("SECRET", repr(self.describe_with(self.snapshot())))
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_corrupt_native_cache_preserves_last_good_efforts_without_static_replacement(self):
        (self.root / "models_cache.json").write_text("{broken", encoding="utf-8")
        projected = self.describe_with(self.snapshot(status="unavailable"))
        self.assertEqual(projected["models"]["sol"]["resolved"], "gpt-6-sol")
        self.assertEqual(projected["models"]["gpt-6-sol"]["supported_efforts"], ("medium", "high"))
        self.assertEqual(projected["models"]["gpt-6-sol"]["freshness"], "stale")
        self.assertNotIn("gpt-6.1-sol", projected["models"])

    def test_deeply_nested_input_is_unknown_not_an_unbounded_failure(self):
        (self.root / "models_cache.json").write_bytes(b"[" * 5000 + b"]" * 5000)
        report = self.claude([])
        report.write_bytes(b"[" * 5000 + b"]" * 5000)
        self.assertEqual(catalog.read_codex_capabilities()["status"], "unavailable")
        self.assertEqual(catalog.read_claude_capabilities()["status"], "unavailable")

    def test_two_spaced_healthy_absences_retire_pin_through_real_snapshot(self):
        self.codex(("gpt-6-sol", ["medium", "high"]))
        model_watch.scan(self.root, now=100)
        self.codex(("gpt-6.2-sol", ["high"]))
        model_watch.scan(self.root, now=200)
        first = catalog.describe_catalog(self.root)["models"]
        self.assertTrue(first["gpt-6-sol"]["available"])
        self.assertEqual(first["gpt-6-sol"]["missing_since"], 200)
        model_watch.scan(self.root, now=200 + model_watch.ABSENCE_INTERVAL - 1)
        self.assertTrue(catalog.describe_catalog(self.root)["models"]["gpt-6-sol"]["available"])
        model_watch.scan(self.root, now=200 + model_watch.ABSENCE_INTERVAL)
        retired = catalog.describe_catalog(self.root)["models"]["gpt-6-sol"]
        self.assertFalse(retired["available"])
        self.assertEqual(retired["supported_efforts"], ("medium", "high"))
        with self.assertRaisesRegex(ValueError, "unavailable"):
            catalog.resolve_choice("gpt-6-sol", "codex", root=self.root)

    def test_failures_between_absences_preserve_real_last_good_snapshot(self):
        self.codex(("gpt-6-sol", ["high"]))
        model_watch.scan(self.root, now=100)
        self.codex()
        model_watch.scan(self.root, now=200)
        (self.root / "models_cache.json").write_text("{broken", encoding="utf-8")
        model_watch.scan(self.root, now=200 + model_watch.ABSENCE_INTERVAL)
        projected = catalog.describe_catalog(self.root)
        self.assertTrue(projected["models"]["gpt-6-sol"]["available"])
        self.assertEqual(projected["models"]["gpt-6-sol"]["freshness"], "stale")
        self.assertEqual(projected["sources"]["codex"]["last_good_at"], 200)

    def test_authoritative_empty_claude_report_retires_static_pins_after_confirmation(self):
        self.claude([])
        model_watch.scan(self.root, now=100)
        first = catalog.describe_catalog(self.root)["models"]
        self.assertTrue(first["claude-sonnet-5"]["available"])
        self.assertIsNotNone(first["claude-sonnet-5"]["missing_since"])
        model_watch.scan(self.root, now=100 + model_watch.ABSENCE_INTERVAL)
        retired = catalog.describe_catalog(self.root)["models"]
        self.assertFalse(retired["claude-sonnet-5"]["available"])
        self.assertFalse(retired["claude-opus-5-5"]["available"])
        self.assertNotIn("sonnet", retired)
        self.assertNotIn("opus", retired)

    def test_catalog_order_is_independent_of_native_and_retained_mapping_order(self):
        models = (("gpt-7-fable", ["high"]), ("gpt-6-luna", ["high"]),
                  ("gpt-6-sol", ["high"]), ("gpt-6.1-sol", ["high"]),
                  ("gpt-7-orbit", ["medium"]))
        self.codex(*models)
        self.claude([{"id": "claude-orbit-6-1", "supported_efforts": ["high"]},
                     {"id": "claude-fable-5-1", "supported_efforts": ["medium"]}])
        state = self.snapshot()
        before = list(self.describe_with(state)["models"])
        self.codex(*reversed(models))
        state["sources"]["codex"]["models"] = dict(reversed(list(state["sources"]["codex"]["models"].items())))
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["medium"]},
                     {"id": "claude-orbit-6-1", "supported_efforts": ["high"]}])
        after = list(self.describe_with(state)["models"])
        self.assertEqual(before, after)
        self.assertLess(after.index("gpt-6.1-sol"), after.index("gpt-6-sol"))
        self.assertLess(after.index("gpt-6-luna"), after.index("gpt-7-fable"))
        self.assertLess(after.index("gpt-7-fable"), after.index("gpt-7-orbit"))
        self.assertLess(after.index("claude-sonnet-5"), after.index("claude-fable-5-1"))

    def test_default_settings_revision_survives_fresh_import_and_sorted_retained_storage(self):
        self.codex(("gpt-6-luna", ["high"]), ("gpt-6-sol", ["medium", "high"]),
                   ("gpt-6.1-sol", ["ultra"]), ("gpt-7-fable", ["medium"]))
        self.claude([{"id": model, "supported_efforts": list(efforts)}
                     for model, efforts in catalog._CLAUDE.items()])
        project = self.root / "project"
        project.mkdir()
        program = ("import json,sys;sys.path.insert(0,sys.argv[1]);from eutonos import settings;"
                   "value=settings.status(sys.argv[2]);"
                   "print(json.dumps({'models':value['models'],'imported':list(settings.MODELS),"
                   "'allowed':value['routing']['allowed_models'],'revision':value['revision']}))")

        def fresh_import():
            process = subprocess.run([sys.executable, "-c", program,
                                      str(Path(__file__).resolve().parents[1] / "src"), str(project)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)

        native = fresh_import()
        model_watch.scan(self.root, now=100)
        (self.root / "models_cache.json").write_text("{broken", encoding="utf-8")
        retained = fresh_import()
        self.assertEqual(native, retained)
        self.assertEqual(retained["models"], retained["imported"])

    def test_claude_source_identity_is_not_public_provenance(self):
        report = self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}])
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["source"] = "PRIVATE account identity"
        report.write_text(json.dumps(payload), encoding="utf-8")
        native = catalog.read_claude_capabilities()
        self.assertNotIn("PRIVATE", repr(native))
        self.assertEqual(native["basis"], "claude-local-report-2026-09-30")
        state = self.snapshot("claude", status="unknown")
        state["sources"]["claude"]["basis"] = "claude-local-report-2026-09-29:PRIVATE account identity"
        report.unlink()
        projected = self.describe_with(state)
        self.assertNotIn("PRIVATE", repr(projected))
        self.assertEqual(projected["models"]["claude-sonnet-5"]["basis"], "claude-local-report-2026-09-29")
