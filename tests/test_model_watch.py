"""Failure-preserving, bounded native watch and dispatch-parser agreement."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from eutonos import model_catalog, model_watch


class ModelWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "models_cache.json"
        self.report = self.root / "user/model-watch/claude-models.json"
        patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root), "EUTONOS_HOME": str(self.root / "user")})
        patch.start()
        self.addCleanup(patch.stop)
        self.codex(("gpt-6-sol", ["low", "high"]))

    def codex(self, *models):
        self.cache.write_text(json.dumps({"account": "PRIVATE", "instructions": "SECRET",
            "models": [{"slug": model, "visibility": "list", "prompt": "SECRET",
                        "supported_reasoning_levels": [{"effort": e} for e in efforts]}
                       for model, efforts in models]}), encoding="utf-8")

    def claude(self, models, **extras):
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps({"profile": "eutonos.claude-model-report.v1",
            "reported_on": "2026-09-29", "source": "Native listing", "models": models,
            **extras}), encoding="utf-8")

    def test_baseline_release_unknown_pin_and_unchanged_scan_dedupe(self):
        first = model_watch.scan(self.root)
        self.assertTrue(first["initialized"])
        self.assertFalse(first["changed"])
        self.assertFalse(model_watch.scan(self.root)["changed"])
        self.codex(("gpt-6-sol", ["low", "high"]), ("gpt-6.1-sol", ["medium", "ultra"]),
                   ("gpt-7-fable", ["high"]), ("gpt-8-sol-preview", ["low"]))
        change = model_watch.scan(self.root)
        self.assertTrue(change["changed"])
        self.assertEqual(change["new_families"], [{"source": "codex", "id": "gpt-7-fable"}])
        self.assertEqual(change["sources"]["codex"]["family_target_changes"], [
            {"family": "sol", "before": "gpt-6-sol", "after": "gpt-6.1-sol"}])
        self.assertFalse(model_watch.scan(self.root)["changed"])
        self.assertFalse(model_watch.diff(self.root)["changed"])
        state = model_watch.baseline(self.root)["state"]
        self.assertNotIn("PRIVATE", repr(state))
        self.assertNotIn("SECRET", repr(state))
        self.assertNotIn("fable", model_catalog.describe_catalog(self.root)["models"])
        self.assertEqual(model_catalog.resolve_choice("gpt-7-fable", "codex")["selection_kind"], "pin")

    def test_failure_unknown_and_recovery_preserve_each_good_baseline(self):
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}])
        model_watch.scan(self.root)
        saved = model_watch.baseline(self.root)["state"]
        self.cache.write_text("{bad", encoding="utf-8")
        self.report.unlink()
        result = model_watch.scan(self.root)
        self.assertTrue(result["changed"])
        self.assertEqual(result["sources"]["codex"]["status"], "unavailable")
        self.assertEqual(result["sources"]["claude"]["status"], "unknown")
        current = model_watch.baseline(self.root)["state"]
        for harness in ("codex", "claude"):
            self.assertEqual(current["sources"][harness]["models"], saved["sources"][harness]["models"])
            self.assertEqual(current["sources"][harness]["last_good_at"], saved["sources"][harness]["last_good_at"])
        self.codex(("gpt-6-sol", ["low", "high"]))
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}],
                    reported_on="2026-09-30", source="Native listing refreshed")
        self.assertTrue(model_watch.scan(self.root)["changed"])
        self.cache.write_text(json.dumps({"models": []}), encoding="utf-8")
        at = time.time()
        candidate = model_watch.scan(self.root, now=at)
        self.assertEqual(candidate["sources"]["codex"]["removed"], [])
        removal = model_watch.scan(self.root, now=at + model_watch.ABSENCE_INTERVAL)
        self.assertEqual(removal["sources"]["codex"]["removed"], ["gpt-6-sol"])
        self.assertTrue(removal["changed"])

    def test_one_failed_source_does_not_suppress_other_source_change(self):
        self.claude([{"id": "claude-sonnet-5", "supported_efforts": ["high"]}])
        model_watch.scan(self.root)
        self.cache.unlink()
        self.claude([{"id": "claude-sonnet-5", "supported_efforts": ["medium", "high"]},
                     {"id": "claude-fable-5-1"}])
        result = model_watch.scan(self.root)
        self.assertTrue(result["changed"])
        self.assertEqual(result["sources"]["codex"]["removed"], [])
        self.assertEqual(result["sources"]["claude"]["effort_changes"][0]["id"], "claude-sonnet-5")
        self.assertIn("dated-effort-assumption", result["sources"]["claude"]["added"][0]["effort_basis"])

    def test_report_caps_and_validation_match_dispatch(self):
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}])
        model_watch.scan(self.root)
        for value in (b"x" * (model_catalog.MAX_REPORT_BYTES + 1),
                      json.dumps({"profile": "wrong", "models": []}).encode(),
                      json.dumps({"profile": "eutonos.claude-model-report.v1", "models": [
                          {"id": f"claude-fable-5-{i}", "supported_efforts": ["high"]}
                          for i in range(65)]}).encode()):
            self.report.write_bytes(value)
            with self.subTest(value=len(value)):
                result = model_watch.scan(self.root)
                self.assertEqual(result["sources"]["claude"]["removed"], [])
                self.assertEqual(result["sources"]["claude"]["status"], "unavailable")
                self.assertTrue(model_watch.catalog_snapshot()["sources"]["claude"]["models"]["claude-fable-5-1"]["available"])
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["not-an-effort"]}])
        self.assertFalse(model_watch.scan(self.root)["changed"])
        self.assertEqual(model_catalog.read_claude_capabilities(self.root)["invalid_items"], 1)

    def test_worktree_reads_canonical_shared_report_and_state(self):
        child = self.root / "worktree"
        child.mkdir()
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}])
        with mock.patch.object(model_catalog, "_shared_root", return_value=self.root):
            result = model_watch.scan(child)
            self.assertEqual(Path(result["path"]), self.root / "user/model-watch/baseline.json")
            self.assertEqual(model_catalog.resolve_choice("claude-fable-5-1", "claude", root=child)["resolved"], "claude-fable-5-1")

    def test_diff_is_read_only_and_concurrent_scan_reports_release_once(self):
        model_watch.scan(self.root)
        before = model_watch.state_path(self.root).read_bytes()
        self.codex(("gpt-6.1-sol", ["high"]))
        self.assertTrue(model_watch.diff(self.root)["changed"])
        self.assertEqual(model_watch.state_path(self.root).read_bytes(), before)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: model_watch.scan(self.root), range(2)))
        self.assertEqual(sum(result["changed"] for result in results), 1)
        self.assertFalse(model_watch.diff(self.root)["changed"])

    def test_corrupt_oversize_state_and_busy_lock_fail_visibly_without_replacement(self):
        model_watch.scan(self.root)
        path = model_watch.state_path(self.root)
        for data in (b"{invalid", b"x" * (model_watch.MAX_STATE_BYTES + 1)):
            path.write_bytes(data)
            with self.assertRaises(model_watch.ModelWatchError):
                model_watch.scan(self.root)
            self.assertEqual(path.read_bytes(), data)
        path.unlink()
        with mock.patch.object(model_watch, "local_lock", side_effect=TimeoutError):
            with self.assertRaisesRegex(model_watch.ModelWatchError, "busy"):
                model_watch.scan(self.root)
        self.assertFalse(path.exists())

    def test_partial_malformed_source_does_not_look_like_removal(self):
        model_watch.scan(self.root)
        self.cache.write_text(json.dumps({"models": [{"slug": "gpt-6-sol", "visibility": "list",
                                                      "supported_reasoning_levels": "bad"}]}), encoding="utf-8")
        result = model_watch.scan(self.root)
        self.assertTrue(result["changed"])
        self.assertEqual(result["sources"]["codex"]["removed"], [])
        self.assertEqual(result["sources"]["codex"]["status"], "unavailable")

    def test_initial_missing_source_first_healthy_is_quiet_then_release_alerts(self):
        first = model_watch.scan(self.root)
        self.assertFalse(first["sources"]["claude"]["baseline_initialized"])
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}])
        healthy = model_watch.scan(self.root)
        self.assertFalse(healthy["changed"])
        self.assertTrue(healthy["sources"]["claude"]["baseline_established"])
        self.assertEqual(healthy["new_families"], [])
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]},
                     {"id": "claude-orbit-5-1", "supported_efforts": ["medium"]}])
        release = model_watch.scan(self.root)
        self.assertTrue(release["changed"])
        self.assertEqual(release["new_families"], [{"source": "claude", "id": "claude-orbit-5-1"}])
        self.assertFalse(model_watch.scan(self.root)["changed"])

    def test_valid_empty_source_baseline_then_first_model_alerts(self):
        self.claude([])
        initial = model_watch.scan(self.root)
        self.assertTrue(initial["sources"]["claude"]["baseline_initialized"])
        self.assertFalse(initial["changed"])
        self.report.unlink()
        self.assertTrue(model_watch.scan(self.root)["changed"])
        self.claude([{"id": "claude-fable-5-1", "supported_efforts": ["high"]}])
        release = model_watch.scan(self.root)
        self.assertTrue(release["changed"])
        self.assertEqual(release["sources"]["claude"]["added"][0]["id"], "claude-fable-5-1")

    def test_item_caps_and_oversized_native_cache_preserve_baseline(self):
        model_watch.scan(self.root)
        for payload in (b"x" * (model_catalog.MAX_CACHE_BYTES + 1),
                        json.dumps({"models": [{}] * 257}).encode()):
            self.cache.write_bytes(payload)
            result = model_watch.scan(self.root)
            self.assertEqual(result["sources"]["codex"]["removed"], [])
            self.assertEqual(result["sources"]["codex"]["status"], "unavailable")
            self.assertEqual(result["sources"]["codex"]["model_count"], 1)

    def test_cli_baseline_scan_diff_and_json_errors(self):
        for command in ("baseline", "scan", "diff"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = model_watch.main([command, "--root", str(self.root)])
            self.assertEqual(code, 0)
            self.assertIsInstance(json.loads(output.getvalue()), dict)
        model_watch.state_path(self.root).write_text("bad", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = model_watch.main(["scan", "--root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "attention-required")


if __name__ == "__main__":
    unittest.main()
