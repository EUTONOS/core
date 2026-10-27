"""Pure-logic tests for the focus-invariance probe (AWR-X stress item S-13):
snapshot composition and the compare/diff logic. No real win32 call is
exercised here -- the ctypes-backed readers are mocked, matching the
convention in tests/test_pingpong_companion.py."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "focus_probe.py"
SPEC = importlib.util.spec_from_file_location("focus_probe", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class TakeSnapshotTest(unittest.TestCase):
    def test_reports_unsupported_on_non_windows(self):
        with mock.patch.object(probe, "WINDOWS", False), \
             mock.patch.object(probe, "_read_foreground_window_info",
                                side_effect=AssertionError("must not be called")), \
             mock.patch.object(probe, "_read_clipboard_sequence",
                                side_effect=AssertionError("must not be called")):
            snapshot = probe.take_snapshot()
        self.assertEqual(snapshot, {"supported": False, "timestamp": snapshot["timestamp"]})
        self.assertFalse(snapshot["supported"])
        self.assertIn("timestamp", snapshot)

    def test_composes_a_full_snapshot_on_windows(self):
        foreground = {"hwnd": 123, "title": "Claude", "pid": 456, "exe_name": "claude.exe"}
        with mock.patch.object(probe, "WINDOWS", True), \
             mock.patch.object(probe, "_read_foreground_window_info", return_value=foreground), \
             mock.patch.object(probe, "_read_clipboard_sequence", return_value=7):
            snapshot = probe.take_snapshot()
        self.assertTrue(snapshot["supported"])
        self.assertEqual(snapshot["foreground"], foreground)
        self.assertEqual(snapshot["clipboard_sequence"], 7)
        self.assertIn("timestamp", snapshot)

    def test_timestamps_differ_across_calls_but_do_not_affect_equality_checks(self):
        with mock.patch.object(probe, "WINDOWS", False):
            first = probe.take_snapshot()
            second = probe.take_snapshot()
        # Not asserting they differ (clock resolution is not guaranteed here);
        # just that both are well-formed and comparable via compare_snapshots.
        self.assertEqual(probe.compare_snapshots(first, second), {"changed": False, "fields": []})


class CompareSnapshotsTest(unittest.TestCase):
    def _snapshot(self, hwnd=1, title="Claude", pid=100, exe_name="claude.exe", seq=5, ts="t1"):
        return {"supported": True, "timestamp": ts,
                "foreground": {"hwnd": hwnd, "title": title, "pid": pid, "exe_name": exe_name},
                "clipboard_sequence": seq}

    def test_identical_snapshots_report_unchanged(self):
        a = self._snapshot()
        b = self._snapshot()
        self.assertEqual(probe.compare_snapshots(a, b), {"changed": False, "fields": []})

    def test_timestamp_alone_never_counts_as_a_change(self):
        a = self._snapshot(ts="2026-09-26T00:00:00+00:00")
        b = self._snapshot(ts="2026-09-26T00:00:05+00:00")
        self.assertEqual(probe.compare_snapshots(a, b), {"changed": False, "fields": []})

    def test_foreground_window_change_is_reported(self):
        a = self._snapshot(hwnd=1, title="Claude")
        b = self._snapshot(hwnd=2, title="Codex")
        result = probe.compare_snapshots(a, b)
        self.assertTrue(result["changed"])
        self.assertIn("foreground.hwnd", result["fields"])
        self.assertIn("foreground.title", result["fields"])
        self.assertNotIn("foreground.pid", result["fields"])

    def test_clipboard_sequence_change_is_reported(self):
        a = self._snapshot(seq=5)
        b = self._snapshot(seq=6)
        result = probe.compare_snapshots(a, b)
        self.assertEqual(result, {"changed": True, "fields": ["clipboard_sequence"]})

    def test_supported_flag_flip_is_reported(self):
        a = {"supported": False, "timestamp": "t1"}
        b = self._snapshot()
        result = probe.compare_snapshots(a, b)
        self.assertTrue(result["changed"])
        self.assertIn("supported", result["fields"])

    def test_both_unsupported_is_unchanged(self):
        a = {"supported": False, "timestamp": "t1"}
        b = {"supported": False, "timestamp": "t2"}
        self.assertEqual(probe.compare_snapshots(a, b), {"changed": False, "fields": []})

    def test_fields_are_sorted_and_deterministic(self):
        a = self._snapshot(hwnd=1, title="A", pid=1, exe_name="a.exe", seq=1)
        b = self._snapshot(hwnd=2, title="B", pid=2, exe_name="b.exe", seq=2)
        result = probe.compare_snapshots(a, b)
        self.assertEqual(result["fields"], sorted(result["fields"]))
        self.assertEqual(
            set(result["fields"]),
            {"clipboard_sequence", "foreground.exe_name", "foreground.hwnd",
             "foreground.pid", "foreground.title"})


class CliExitCodeTest(unittest.TestCase):
    """compare must exit 0 for unchanged, 1 for changed, per the tool's contract."""

    def _write(self, tmpdir, name, data):
        path = Path(tmpdir) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_exit_code_zero_when_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._write(tmp, "a.json", {"supported": False, "timestamp": "t1"})
            b = self._write(tmp, "b.json", {"supported": False, "timestamp": "t2"})
            args = probe.argparse.Namespace(a=a, b=b)
            self.assertEqual(probe.cmd_compare(args), 0)

    def test_exit_code_one_when_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self._write(tmp, "a.json", {"supported": True, "timestamp": "t1",
                                            "foreground": {"hwnd": 1, "title": "Claude",
                                                           "pid": 1, "exe_name": "claude.exe"},
                                            "clipboard_sequence": 1})
            b = self._write(tmp, "b.json", {"supported": True, "timestamp": "t2",
                                            "foreground": {"hwnd": 1, "title": "Claude",
                                                           "pid": 1, "exe_name": "claude.exe"},
                                            "clipboard_sequence": 2})
            args = probe.argparse.Namespace(a=a, b=b)
            self.assertEqual(probe.cmd_compare(args), 1)

    def test_snapshot_writes_to_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "snap.json")
            args = probe.argparse.Namespace(out=out)
            with mock.patch.object(probe, "WINDOWS", False):
                self.assertEqual(probe.cmd_snapshot(args), 0)
            data = json.loads(Path(out).read_text(encoding="utf-8"))
            self.assertFalse(data["supported"])


if __name__ == "__main__":
    unittest.main()
