"""Review repair: contention is quiet; missing retention is explicitly unknown."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog as catalog, model_watch as watch


class ModelWatchReviewRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "user"),
                                               "CODEX_HOME": str(self.root / "codex")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = self.root / "codex/models_cache.json"
        self.cache.parent.mkdir()
        self.codex("gpt-6-sol")

    def codex(self, *models):
        self.cache.write_text(json.dumps({"models": [{"slug": model, "visibility": "list",
            "supported_reasoning_levels": [{"effort": "high"}]} for model in models]}), encoding="utf-8")

    def test_lazy_acquisition_timeout_defers_without_any_unlocked_work(self):
        with mock.patch.object(watch, "local_lock", side_effect=TimeoutError("contended")), \
                mock.patch.object(watch, "_read_state") as prior, \
                mock.patch.object(watch, "_observations") as observations, \
                mock.patch.object(watch, "_write_state") as write:
            result = watch.scan_if_due(now=0)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "scan-in-progress")
        self.assertNotIn("error", result)
        self.assertIsNone(result["pending_count"])
        self.assertFalse(result["scanned"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["written"])
        prior.assert_not_called()
        observations.assert_not_called()
        write.assert_not_called()

    def test_forced_scan_still_refuses_a_busy_registry(self):
        with mock.patch.object(watch, "local_lock", side_effect=TimeoutError("contended")), \
                mock.patch.object(watch, "_observations") as observations:
            with self.assertRaises(watch.ModelWatchError):
                watch.scan(now=0)
        observations.assert_not_called()

    def test_timeout_after_acquiring_lock_is_attention_not_deferral(self):
        @contextmanager
        def acquired(*args, **kwargs):
            yield

        with mock.patch.object(watch, "local_lock", acquired), \
                mock.patch.object(watch, "_observations", side_effect=TimeoutError("source read failed")), \
                mock.patch.object(watch, "_write_state") as write:
            result = watch.scan_if_due(now=0)
        self.assertEqual(result["status"], "attention-required")
        self.assertIn("error", result)
        self.assertFalse(result["scanned"])
        write.assert_not_called()

    def test_home_timeout_before_lock_attempt_is_attention_not_contention(self):
        with mock.patch.object(watch, "state_path", side_effect=TimeoutError("home inaccessible")), \
                mock.patch.object(watch, "local_lock") as lock:
            result = watch.scan_if_due(now=0)
        self.assertEqual(result["status"], "attention-required")
        self.assertIn("error", result)
        lock.assert_not_called()

    def test_real_lock_contention_then_retry_commits_change_once(self):
        watch.scan(now=0)
        self.codex("gpt-6-sol", "gpt-7-sol")
        path = watch.state_path()
        before = path.read_bytes()
        with watch.local_lock(path.parent, timeout=2):
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(watch.scan_if_due, now=watch.SCAN_INTERVAL).result(timeout=2)
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(watch.pending()["count"], 0)
        retry = watch.scan_if_due(now=watch.SCAN_INTERVAL)
        self.assertEqual(retry["status"], "ok")
        self.assertTrue(retry["changed"])
        notices = watch.pending()["items"]
        self.assertGreater(len(notices), 0)
        self.assertEqual(watch.scan_if_due(now=watch.SCAN_INTERVAL)["status"], "not-due")
        self.assertEqual(watch.pending()["items"], notices)

    def test_missing_snapshot_is_unknown_and_never_fabricates_or_writes(self):
        path = watch.state_path()
        with mock.patch.object(watch, "_fresh_state") as fresh, \
                mock.patch.object(watch, "_observations") as observations, \
                mock.patch.object(watch, "_write_state") as write, \
                mock.patch.object(catalog, "shared_root") as shared_root:
            snapshot = watch.catalog_snapshot(self.root)
        self.assertEqual(snapshot["sources"], {})
        self.assertIn("unknown", snapshot["error"])
        self.assertFalse(path.exists())
        fresh.assert_not_called()
        observations.assert_not_called()
        write.assert_not_called()
        shared_root.assert_not_called()

    def test_removed_snapshot_reports_unknown_without_reinitializing(self):
        watch.scan(now=0)
        path = watch.state_path()
        self.assertTrue(watch.catalog_snapshot()["sources"]["codex"]["initialized"])
        path.unlink()
        snapshot = watch.catalog_snapshot()
        self.assertEqual(snapshot["sources"], {})
        self.assertIn("error", snapshot)
        self.assertFalse(path.exists())

    def test_held_lock_reports_hint_after_sixty_seconds_and_clears_on_scan(self):
        with mock.patch.object(watch, "local_lock", side_effect=TimeoutError("contended")):
            with mock.patch.object(watch.time, "time", return_value=1000):
                first = watch.scan_if_due(now=0)
            with mock.patch.object(watch.time, "time", return_value=1061):
                later = watch.scan_if_due(now=0)
                pickup = watch.pickup_hint()
        self.assertEqual(first["status"], "deferred")
        self.assertNotIn("hint", first)
        self.assertEqual(later["status"], "deferred")
        self.assertIn("60 seconds", later["hint"])
        self.assertIn("holder unknown", pickup["hint"])
        self.assertNotIn("error", later)
        self.assertEqual(watch.scan_if_due(now=0)["status"], "ok")
        self.assertNotIn("hint", watch.pickup_hint())


if __name__ == "__main__":
    unittest.main()
