"""Portable, durable native watch; deterministic clocks and no native launches."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import model_catalog as catalog, model_watch as watch


class ModelWatchListenerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.user = self.root / "user"
        self.cache = self.root / "models_cache.json"
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.user), "CODEX_HOME": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.codex("gpt-6-sol")

    def codex(self, *models, effort="high"):
        self.cache.write_text(json.dumps({"models": [{"slug": model, "visibility": "list",
            "supported_reasoning_levels": [{"effort": effort}]} for model in models]}), encoding="utf-8")

    def claude(self, *models):
        report = catalog.watch_home() / "claude-models.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"profile": "eutonos.claude-model-report.v1", "source": "Native listing",
            "reported_on": "2026-09-30", "models": [{"id": model, "supported_efforts": ["high"]}
                                                     for model in models]}), encoding="utf-8")

    def model(self, model="gpt-6-sol", harness="codex"):
        return watch.catalog_snapshot()["sources"][harness]["models"][model]

    def test_portable_scan_and_read_need_no_repository_or_git(self):
        with mock.patch.object(catalog, "shared_root", side_effect=AssertionError("Git must not be needed")):
            result = watch.scan(now=0)
            self.assertEqual(Path(result["path"]), self.user / "model-watch/baseline.json")
            self.assertEqual(watch.pending()["count"], 0)
            self.assertTrue(watch.catalog_snapshot()["sources"]["codex"]["initialized"])
            self.assertEqual(watch.pickup_hint()["pending_count"], 0)

    def test_user_state_is_shared_across_projects_and_isolated_between_users(self):
        watch.scan(now=0)
        self.assertEqual(watch.state_path(self.root / "project-one"), watch.state_path(self.root / "project-two"))
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "other-user")}):
            self.assertFalse(watch.baseline()["found"])
            self.assertIsNone(watch.pickup_hint())

    def test_due_timer_is_persisted_and_frequent_calls_do_not_read_sources(self):
        watch.scan_if_due(now=10)
        with mock.patch.object(watch, "_observations", side_effect=AssertionError("not due")):
            result = watch.scan_if_due(now=10 + watch.SCAN_INTERVAL - 1)
        self.assertEqual(result["status"], "not-due")
        self.assertFalse(result["scanned"])
        self.assertEqual(result["next_due_at"], 10 + watch.SCAN_INTERVAL)
        self.assertTrue(watch.scan_if_due(now=10 + watch.SCAN_INTERVAL)["scanned"])

    def test_per_source_due_checks_only_due_reader(self):
        watch.scan(now=0)
        path = watch.state_path()
        state = watch.baseline()["state"]
        state["sources"]["claude"]["last_scan_at"] = watch.SCAN_INTERVAL
        watch._write_state(path, state)
        with mock.patch.object(catalog, "read_claude_capabilities", side_effect=AssertionError("Claude not due")):
            result = watch.scan_if_due(now=watch.SCAN_INTERVAL)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(result["sources"]), {"codex"})

    def test_two_spaced_healthy_absences_retire_but_force_reads_cannot_accelerate(self):
        watch.scan(now=0)
        self.codex()
        first = watch.scan(now=10)
        self.assertFalse(first["changed"])
        self.assertEqual(self.model()["missing_since"], 10)
        for at in (10, 11, 100, 10 + watch.ABSENCE_INTERVAL - 1):
            self.assertFalse(watch.scan(now=at)["changed"])
            self.assertTrue(self.model()["available"])
        confirmed = watch.scan(now=10 + watch.ABSENCE_INTERVAL)
        self.assertEqual(confirmed["sources"]["codex"]["removed"], ["gpt-6-sol"])
        self.assertFalse(self.model()["available"])
        self.assertFalse(watch.scan(now=10 + 2 * watch.ABSENCE_INTERVAL)["changed"])

    def test_bad_source_does_not_confirm_absence_or_change_last_good(self):
        watch.scan(now=0)
        self.codex()
        watch.scan(now=10)
        before = watch.catalog_snapshot()["sources"]["codex"]
        self.cache.write_text("{bad", encoding="utf-8")
        watch.scan(now=10 + watch.ABSENCE_INTERVAL)
        after = watch.catalog_snapshot()["sources"]["codex"]
        self.assertEqual(after["models"], before["models"])
        self.assertEqual(after["last_good_at"], 10)
        self.assertEqual(after["last_scan_at"], 10 + watch.ABSENCE_INTERVAL)
        self.assertEqual(after["status"], "unavailable")
        self.assertEqual(watch.pending()["count"], 1)
        self.assertEqual(watch.pending()["items"][0]["kind"], "source_failure")

    def test_reappearance_resets_candidates_and_recovers_confirmed_pin(self):
        watch.scan(now=0)
        self.codex()
        watch.scan(now=10)
        self.codex("gpt-6-sol")
        watch.scan(now=20)
        self.assertIsNone(self.model()["missing_since"])
        self.codex()
        watch.scan(now=30)
        self.assertTrue(self.model()["available"])
        watch.scan(now=30 + watch.ABSENCE_INTERVAL)
        self.codex("gpt-6-sol")
        recovery = watch.scan(now=40 + watch.ABSENCE_INTERVAL)
        self.assertEqual(recovery["sources"]["codex"]["recovered"], ["gpt-6-sol"])
        self.assertTrue(self.model()["available"])
        self.assertIsNone(self.model()["missing_since"])

    def test_notices_survive_unchanged_scans_reads_and_explicit_ack(self):
        watch.scan(now=0)
        self.codex("gpt-6-sol", "gpt-7-sol")
        watch.scan(now=10)
        notices = watch.pending()["items"]
        self.assertGreater(len(notices), 0)
        self.assertFalse(watch.scan(now=20)["changed"])
        self.assertEqual(watch.pending()["items"], notices)
        watch.diff(now=20)
        watch.pickup_hint()
        watch.catalog_snapshot()
        self.assertEqual(watch.pending()["items"], notices)
        selected = notices[0]["id"]
        self.assertEqual(watch.ack(ids=[selected])["acknowledged"], [selected])
        self.assertEqual(watch.ack(ids=[selected])["acknowledged"], [])
        self.assertEqual(watch.pending()["count"], len(notices) - 1)
        with self.assertRaises(watch.ModelWatchError):
            watch.ack()

    def test_coalesced_effort_notice_rejects_stale_ack_identity(self):
        watch.scan(now=0)
        self.codex("gpt-6-sol", effort="medium")
        watch.scan(now=10)
        first = next(e for e in watch.pending()["items"] if e["kind"] == "effort")
        self.codex("gpt-6-sol", effort="xhigh")
        watch.scan(now=20)
        second = next(e for e in watch.pending()["items"] if e["kind"] == "effort")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["before"]["supported_efforts"], ["high"])
        self.assertEqual(second["after"]["supported_efforts"], ["xhigh"])
        self.assertEqual(second["first_seen"], 10)
        self.assertEqual(watch.ack(ids=[first["id"]])["acknowledged"], [])
        self.assertIn(second["id"], [item["id"] for item in watch.pending()["items"]])

    def test_failed_atomic_write_preserves_baseline_and_notice_is_retryable(self):
        watch.scan(now=0)
        path = watch.state_path()
        before = path.read_bytes()
        self.codex("gpt-6-sol", "gpt-7-sol")
        with mock.patch.object(watch.os, "replace", side_effect=OSError("replacement failed")):
            with self.assertRaises(watch.ModelWatchError):
                watch.scan(now=10)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(watch.pending()["count"], 0)
        self.assertTrue(watch.scan(now=10)["changed"])
        self.assertGreater(watch.pending()["count"], 0)
        self.assertEqual(list(path.parent.glob(".baseline-*.tmp")), [])

    def test_pending_capacity_failure_preserves_old_state(self):
        watch.scan(now=0)
        before = watch.state_path().read_bytes()
        self.codex("gpt-6-sol", "gpt-7-sol")
        with mock.patch.object(watch, "MAX_PENDING", 1):
            with self.assertRaises(watch.ModelWatchError):
                watch.scan(now=10)
        self.assertEqual(watch.state_path().read_bytes(), before)
        self.assertTrue(watch.scan(now=10)["changed"])

    def test_static_claude_defaults_need_two_healthy_omissions(self):
        watch.scan(now=0)
        self.assertFalse(watch.catalog_snapshot()["sources"]["claude"]["initialized"])
        self.claude("claude-fable-7")
        self.assertFalse(watch.scan(now=10)["changed"])
        pin = self.model("claude-sonnet-5", "claude")
        self.assertTrue(pin["available"])
        self.assertEqual(pin["missing_since"], 10)
        self.assertIsNone(pin["last_seen"])
        watch.scan(now=10 + watch.ABSENCE_INTERVAL)
        self.assertFalse(self.model("claude-sonnet-5", "claude")["available"])
        self.assertTrue(self.model("claude-fable-7", "claude")["available"])

    def test_never_raising_reads_expose_corruption_without_replacing_it(self):
        path = watch.state_path()
        path.parent.mkdir(parents=True)
        for raw in (b"{bad", b"[" * 30000, b"x" * (watch.MAX_STATE_BYTES + 1)):
            with self.subTest(size=len(raw)):
                path.write_bytes(raw)
                self.assertEqual(watch.pickup_hint()["status"], "attention-required")
                self.assertIn("error", watch.catalog_snapshot())
                self.assertEqual(watch.pending()["status"], "attention-required")
                self.assertEqual(watch.scan_if_due(now=0)["status"], "attention-required")
                self.assertEqual(path.read_bytes(), raw)

    def test_busy_lock_and_home_failure_are_safe_for_lazy_trigger_and_pickup(self):
        with mock.patch.object(watch, "local_lock", side_effect=TimeoutError("busy")):
            self.assertEqual(watch.scan_if_due(now=0)["status"], "deferred")
        with mock.patch.object(catalog, "watch_home", side_effect=OSError("C:/PRIVATE-ACCOUNT/credential inaccessible")):
            self.assertEqual(watch.pickup_hint()["status"], "attention-required")
            self.assertIn("error", watch.catalog_snapshot())
            self.assertNotIn("PRIVATE-ACCOUNT", watch.pickup_hint()["error"])

    def test_source_failures_and_recovery_are_durable_and_deduplicated(self):
        watch.scan(now=0)
        self.assertEqual(watch.pending()["count"], 0)  # First missing Claude report is quiet.
        self.cache.write_text("{bad", encoding="utf-8")
        failed = watch.scan(now=10)
        self.assertTrue(failed["changed"])
        notices = watch.pending()["items"]
        self.assertEqual([item["kind"] for item in notices], ["source_failure"])
        self.assertFalse(watch.scan(now=20)["changed"])
        self.assertEqual(watch.pending()["items"], notices)
        watch.ack(ids=[notices[0]["id"]])
        self.assertFalse(watch.scan(now=30)["changed"])
        self.assertEqual(watch.pending()["count"], 0)
        self.codex("gpt-6-sol")
        self.assertTrue(watch.scan(now=40)["changed"])
        self.assertEqual([item["kind"] for item in watch.pending()["items"]], ["source_recovery"])
        self.assertFalse(watch.scan(now=50)["changed"])

    def test_concurrent_due_scans_only_one_accepts_release(self):
        watch.scan(now=0)
        self.codex("gpt-6-sol", "gpt-7-sol")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: watch.scan_if_due(now=watch.SCAN_INTERVAL), range(2)))
        self.assertEqual(sum(r["changed"] for r in results), 1)
        self.assertGreater(watch.pending()["count"], 0)


if __name__ == "__main__":
    unittest.main()
