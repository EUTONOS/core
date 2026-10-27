import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from eutonos import companion_history as history


class TrackingSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = self.root / ".eutonos-local"
        self.local.mkdir()

    def legacy(self, enabled=True, pad=0):
        rows = {f"r{i}": {"id": f"r{i}", "pad": "x" * 1000} for i in range(pad)}
        (self.local / history.STATE_NAME).write_text(json.dumps(
            {"profile": history.PROFILE, "tracking": {"enabled": enabled, "windows": []}, "rows": rows}), encoding="utf-8")

    def test_default_enabled_without_state(self):
        self.assertTrue(history.tracking_enabled_bounded(self.root))

    def test_large_history_does_not_hide_switch_after_migration(self):
        self.legacy(True, pad=600)
        self.assertGreater((self.local / history.STATE_NAME).stat().st_size, 256 * 1024)
        self.assertIsNone(history.tracking_enabled_bounded(self.root))  # legacy + oversized: unknown
        history.sync(self.root, [])  # writer path migrates
        self.assertTrue((self.local / history.TRACKING_NAME).exists())
        self.assertIs(history.tracking_enabled_bounded(self.root), True)

    def test_migration_keeps_off_state(self):
        self.legacy(False, pad=600)
        history.sync(self.root, [])
        self.assertIs(history.tracking_enabled_bounded(self.root), False)
        self.assertFalse(history.tracking(self.root)["enabled"])

    def test_small_legacy_history_still_readable_without_migration(self):
        self.legacy(False)
        self.assertIs(history.tracking_enabled_bounded(self.root), False)

    def test_set_tracking_updates_small_file_with_large_history(self):
        self.legacy(True, pad=600)
        history.set_tracking(self.root, False)
        self.assertIs(history.tracking_enabled_bounded(self.root), False)
        self.assertEqual(len(history.tracking(self.root)["windows"]), 1)
        history.set_tracking(self.root, True)
        self.assertIs(history.tracking_enabled_bounded(self.root), True)
        self.assertIsNotNone(history.tracking(self.root)["windows"][0]["ended_at"])

    def test_small_file_is_authoritative_for_sync_windows(self):
        self.legacy(True)
        history.set_tracking(self.root, False)
        self.assertEqual(history.sync(self.root, [{"id": "a", "start": "2000-01-01T00:00:00Z"}])["accepted"], 0)

    def test_contention_and_malformed_are_unknown(self):
        history.set_tracking(self.root, True)
        with mock.patch("eutonos.companion_history.local_lock", side_effect=TimeoutError):
            self.assertIsNone(history.tracking_enabled_bounded(self.root))
        (self.local / history.TRACKING_NAME).write_text("{bad", encoding="utf-8")
        self.assertIsNone(history.tracking_enabled_bounded(self.root))


if __name__ == "__main__":
    unittest.main()
