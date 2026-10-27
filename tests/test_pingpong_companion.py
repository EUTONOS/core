"""Pure-logic tests for the ping-pong overlay companion: window selection,
placement math, animation stepping, session-state -> visibility mapping, and
the Crosstalk halt/resume result mapping. No Tk window and no real
win32/UI-Automation call is exercised here (those need an actual desktop and
are covered by --sample, run manually against the real machine)."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "pingpong_companion.py"
SPEC = importlib.util.spec_from_file_location("pingpong_companion", SCRIPT)
companion = importlib.util.module_from_spec(SPEC)
# Dataclasses under `from __future__ import annotations` resolve their string
# annotations via sys.modules[cls.__module__]; register before exec_module so
# that lookup does not crash for a module that was never `import`-ed normally.
sys.modules[SPEC.name] = companion
SPEC.loader.exec_module(companion)


def make_window(hwnd=1, pid=100, title="Claude", class_name="Chrome_WidgetWin_1",
                exe_path=r"C:\Program Files\WindowsApps\Claude_2.9.0_x64__pzs8sxrjxfjjc\app\claude.exe",
                visible=True, tool_window=False, owned=False, rect=(0, 0, 800, 600)):
    return companion.WindowInfo(hwnd=hwnd, pid=pid, title=title, class_name=class_name,
                                exe_path=exe_path, visible=visible, tool_window=tool_window,
                                owned=owned, rect=rect)


class WindowPredicateTest(unittest.TestCase):
    def test_claude_desktop_matches_the_msix_package(self):
        self.assertTrue(companion.is_claude_desktop_path(
            r"C:\Program Files\WindowsApps\Claude_2.9939.2.0_x64__pzs8sxrjxfjjc\app\claude.exe"))
        self.assertTrue(companion.is_claude_desktop_path(
            r"C:\Users\me\AppData\Local\AnthropicClaude\app\claude.exe"))

    def test_claude_desktop_excludes_the_cli(self):
        self.assertFalse(companion.is_claude_desktop_path(
            r"C:\Program Files\Python314\Scripts\claude.exe"))
        self.assertFalse(companion.is_claude_desktop_path(""))
        self.assertFalse(companion.is_claude_desktop_path(
            r"C:\Program Files\WindowsApps\Claude_2.9.0_x64\app\other.exe"))

    def test_codex_matches_the_chatgpt_desktop_shell(self):
        self.assertTrue(companion.is_codex_app_path(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.924.1866.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"))

    def test_codex_excludes_unrelated_processes(self):
        self.assertFalse(companion.is_codex_app_path(
            r"C:\Users\me\AppData\Local\OpenAI\Codex\runtimes\cua_node\node.exe"))
        self.assertFalse(companion.is_codex_app_path(""))


class SelectWindowTest(unittest.TestCase):
    def test_picks_the_matching_window(self):
        windows = [
            make_window(hwnd=1, title="", exe_path=r"C:\x\claude.exe"),  # no title: helper window
            make_window(hwnd=2, title="Claude", visible=False),          # hidden background window
            make_window(hwnd=3, title="Claude", tool_window=True),       # tool window: not a main window
            make_window(hwnd=4, title="Claude", owned=True),             # an owned popup
            make_window(hwnd=5, title="ChatGPT",
                        exe_path=r"C:\Program Files\WindowsApps\OpenAI.Codex_1_x64\app\ChatGPT.exe"),
            make_window(hwnd=6, title="Claude"),                          # the real one
        ]
        found = companion.select_window(windows, companion.is_claude_desktop_path)
        self.assertEqual(found.hwnd, 6)

    def test_no_match_returns_none(self):
        windows = [make_window(exe_path=r"C:\x\notepad.exe")]
        self.assertIsNone(companion.select_window(windows, companion.is_claude_desktop_path))

    def test_multiple_windows_of_the_same_app_pick_the_largest(self):
        small = make_window(hwnd=10, rect=(0, 0, 100, 100))
        large = make_window(hwnd=11, rect=(0, 0, 1200, 900))
        found = companion.select_window([small, large], companion.is_claude_desktop_path)
        self.assertEqual(found.hwnd, 11)

    def test_reflects_the_real_hidden_full_screen_helper_window_case(self):
        # Captured on the owner's machine: Claude Desktop keeps a hidden,
        # full-monitor-sized "Claude" window alongside its real, smaller,
        # visible one. Visibility must win over raw area.
        hidden_huge = make_window(hwnd=100, visible=False, rect=(0, 0, 3072, 1728))
        visible_real = make_window(hwnd=101, visible=True, rect=(26, 512, 1478, 1564))
        found = companion.select_window([hidden_huge, visible_real], companion.is_claude_desktop_path)
        self.assertEqual(found.hwnd, 101)


class LabelDockPositionTest(unittest.TestCase):
    """Geometry captured from a live, read-only UI Automation probe against
    the owner's running Claude Desktop ("Model: Opus 5.5", a Button) and
    Codex ("GPT-6 Astra High", also a bare Button) windows."""

    def setUp(self):
        self.work = (0, 0, 3840, 2160)

    def test_docks_left_of_the_label_with_a_gap(self):
        label_rect = (1576, 1893, 1653, 1918)  # Claude's "Model: Opus 5.5" button
        size = (60, 22)
        x, y = companion.compute_label_dock_position(label_rect, size, self.work, dpi_scale=1.0)
        self.assertEqual(x, label_rect[0] - companion.LABEL_GAP_PX - size[0])
        # Vertically centered on the label.
        label_mid = (label_rect[1] + label_rect[3]) / 2
        self.assertAlmostEqual(y + size[1] / 2, label_mid, delta=1)

    def test_codex_label_docks_left_of_the_bare_model_name_button(self):
        label_rect = (3078, 1884, 3251, 1919)  # Codex's "GPT-6 Astra High" button
        size = (60, 22)
        x, y = companion.compute_label_dock_position(label_rect, size, self.work, dpi_scale=1.0)
        self.assertLess(x + size[0], label_rect[0])

    def test_dpi_scale_widens_the_gap(self):
        label_rect = (1000, 500, 1100, 530)
        size = (60, 22)
        x1, _ = companion.compute_label_dock_position(label_rect, size, self.work, dpi_scale=1.0)
        x2, _ = companion.compute_label_dock_position(label_rect, size, self.work, dpi_scale=2.0)
        gap1 = label_rect[0] - (x1 + size[0])
        gap2 = label_rect[0] - (x2 + size[0])
        self.assertAlmostEqual(gap2, gap1 * 2, delta=0.5)

    def test_narrow_window_clamps_to_the_work_area_instead_of_going_off_screen(self):
        # A label hugging the left edge of a narrow/moved window: there is no
        # room to its left at all.
        label_rect = (5, 900, 90, 930)
        size = (200, 22)
        work = (0, 0, 1920, 1080)
        x, y = companion.compute_label_dock_position(label_rect, size, work, dpi_scale=1.0)
        self.assertGreaterEqual(x, work[0])
        self.assertLessEqual(x + size[0], work[2])
        self.assertGreaterEqual(y, work[1])
        self.assertLessEqual(y + size[1], work[3])

    def test_clamps_vertically_to_the_work_area(self):
        label_rect = (500, 3, 600, 20)  # label flush with the very top of its monitor
        size = (60, 40)
        x, y = companion.compute_label_dock_position(label_rect, size, self.work, dpi_scale=1.0)
        self.assertGreaterEqual(y, self.work[1])


class LabelOffsetTest(unittest.TestCase):
    """The label is kept as an offset from the host's bottom-right corner so
    the widget follows a drag every frame between probe passes."""

    def test_offset_uses_the_corner_sampled_with_the_label(self):
        # Probe line taken mid-drag: window corner then was (1846, 1817), but
        # the window has since moved 200px right and 50px down.
        label = {"left": 1560, "top": 1790, "right": 1640, "bottom": 1812,
                 "wright": 1846, "wbottom": 1817}
        moved = (565, 628, 2046, 1867)
        offset = companion.label_offset(label, moved)
        self.assertEqual(offset, (-286, -27, -206, -5))
        self.assertEqual(companion.apply_label_offset(offset, moved),
                         (1760, 1840, 1840, 1862))

    def test_line_without_corner_falls_back_to_current_rect(self):
        label = {"left": 100, "top": 90, "right": 150, "bottom": 110}
        host = (0, 0, 200, 120)
        offset = companion.label_offset(label, host)
        self.assertEqual(companion.apply_label_offset(offset, host), (100, 90, 150, 110))


def _fake_user32(prev: dict, topmost=()):
    """user32 stand-in: ``prev`` maps hwnd -> the window right above it."""
    fake = mock.Mock()
    fake.GetWindow.side_effect = lambda hwnd, _cmd: prev.get(hwnd)
    fake.GetWindowLongPtrW.side_effect = lambda hwnd, _idx: 0x8 if hwnd in topmost else 0
    return fake


class StackInsertAfterTest(unittest.TestCase):
    """Z-order: host < drawing < click pad, never above anything else."""

    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("Win32 z-order helper")

    def test_goes_just_below_the_window_covering_the_host(self):
        with mock.patch.object(companion, "USER32", _fake_user32({22: 33})):
            self.assertEqual(companion.stack_insert_after(22, (11, 12)), 33)

    def test_skips_the_widgets_own_windows(self):
        with mock.patch.object(companion, "USER32", _fake_user32({22: 11, 11: 12, 12: 33})):
            self.assertEqual(companion.stack_insert_after(22, (11, 12)), 33)

    def test_frontmost_host_uses_top_of_normal_band_not_topmost(self):
        with mock.patch.object(companion, "USER32", _fake_user32({22: 44}, topmost={44})):
            self.assertIs(companion.stack_insert_after(22), companion.HWND_TOP)
        with mock.patch.object(companion, "USER32", _fake_user32({})):
            self.assertIs(companion.stack_insert_after(22), companion.HWND_TOP)

    def test_is_stacked_needs_drawing_then_pad_right_above_host(self):
        with mock.patch.object(companion, "USER32", _fake_user32({22: 11, 11: 12})):
            self.assertTrue(companion.is_stacked(22, 11, 12))
        with mock.patch.object(companion, "USER32", _fake_user32({22: 12, 12: 11})):
            self.assertFalse(companion.is_stacked(22, 11, 12))


class OverlayPlaceTest(unittest.TestCase):
    """``Overlay._place`` runs on every tick and win event; exercise it with a
    fake user32 so a missing Win32 constant fails here, not in pythonw."""

    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("Win32 placement")

    def _run(self, prev):
        fake = _fake_user32(prev)
        overlay = companion.Overlay.__new__(companion.Overlay)
        overlay.frame = 11
        overlay.pad_frame = 12
        overlay.size = (42, 22)
        overlay.dpi_scale = 1.25
        overlay._last_pos = None
        overlay.label_offset = (-322, -45, -245, -20)
        with mock.patch.object(companion, "USER32", fake), \
                mock.patch.object(companion, "monitor_work_rect", return_value=(0, 0, 3840, 2100)):
            overlay._place(22, (365, 578, 1846, 1817))
        return fake, overlay

    def test_places_left_of_label_with_pad_on_top(self):
        fake, overlay = self._run({22: 33})
        pad_call, frame_call = [c.args for c in fake.SetWindowPos.call_args_list]
        self.assertEqual(pad_call[:2], (12, 33))
        self.assertEqual(frame_call[:2], (11, 12))
        self.assertEqual(overlay._last_pos, (frame_call[2], frame_call[3]))
        self.assertEqual(frame_call[2] + 42 + round(companion.LABEL_GAP_PX * 1.25), 1524)

    def test_already_stacked_moves_without_touching_z_order(self):
        fake, _overlay = self._run({22: 11, 11: 12})
        for call in fake.SetWindowPos.call_args_list:
            self.assertTrue(call.args[6] & companion.SWP_NOZORDER)


class RoundedRectPointsTest(unittest.TestCase):
    def test_stays_inside_the_box_and_clamps_the_radius(self):
        pts = companion.rounded_rect_points(3, 0, 31, 18, 50)
        xs, ys = pts[0::2], pts[1::2]
        self.assertEqual((min(xs), max(xs), min(ys), max(ys)), (3, 31, 0, 18))
        self.assertEqual(pts[0], 3 + 9)  # radius clamped to half the height


class FallbackPositionTest(unittest.TestCase):
    """No label found: land inside the host window's own composer area."""

    def setUp(self):
        self.work = (0, 0, 1920, 1080)

    def test_lands_inside_the_host_window_bottom_right(self):
        host = (300, 400, 1800, 1000)
        size = (60, 22)
        x, y = companion.compute_fallback_position(host, size, self.work)
        left, top, right, bottom = host
        self.assertGreaterEqual(x, left)
        self.assertLessEqual(x + size[0], right)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(y + size[1], bottom)
        # Inside the composer, not straddling the border like the old scheme.
        self.assertLess(x + size[0], right)
        self.assertLess(y + size[1], bottom)

    def test_clamps_to_a_host_window_smaller_than_the_usual_inset(self):
        host = (700, 700, 820, 760)  # much smaller than FALLBACK_INSET_X/Y
        size = (60, 22)
        x, y = companion.compute_fallback_position(host, size, self.work)
        left, top, right, bottom = host
        self.assertGreaterEqual(x, left)
        self.assertLessEqual(x + size[0], right)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(y + size[1], bottom)

    def test_clamps_to_the_monitor_work_area(self):
        host = (-50, -50, 300, 300)  # window partly off the top-left of its monitor
        size = (60, 22)
        x, y = companion.compute_fallback_position(host, size, self.work)
        self.assertGreaterEqual(x, self.work[0])
        self.assertGreaterEqual(y, self.work[1])


class AnimateStepTest(unittest.TestCase):
    def test_moves_monotonically_before_bouncing(self):
        state = companion.AnimationState(ball_x=companion.BALL_MIN_X, direction=1, t=0.0)
        positions = []
        for _ in range(5):
            state = companion.animate_step(state, 0.05)
            positions.append(state.ball_x)
        for earlier, later in zip(positions, positions[1:]):
            self.assertLessEqual(earlier, later)

    def test_bounces_at_the_right_paddle_and_stays_in_bounds(self):
        state = companion.AnimationState(ball_x=companion.BALL_MIN_X, direction=1, t=0.0)
        for _ in range(200):
            state = companion.animate_step(state, 0.02)
            self.assertGreaterEqual(state.ball_x, companion.BALL_MIN_X)
            self.assertLessEqual(state.ball_x, companion.BALL_MAX_X)
        self.assertIn(state.direction, (1, -1))

    def test_large_dt_reflects_symmetrically_off_the_bound(self):
        state = companion.AnimationState(ball_x=companion.BALL_MIN_X, direction=1, t=0.0)
        # One big jump that overshoots the right paddle by 10px.
        dt = (companion.BALL_MAX_X - companion.BALL_MIN_X + 10) / companion.BALL_SPEED
        state = companion.animate_step(state, dt)
        self.assertEqual(state.direction, -1)
        self.assertAlmostEqual(state.ball_x, companion.BALL_MAX_X - 10, places=6)

    def test_time_accumulates(self):
        state = companion.AnimationState(ball_x=companion.BALL_MIN_X, direction=1, t=1.0)
        state = companion.animate_step(state, 0.25)
        self.assertAlmostEqual(state.t, 1.25, places=6)

    def test_negative_dt_is_treated_as_zero(self):
        state = companion.AnimationState(ball_x=20.0, direction=1, t=0.0)
        next_state = companion.animate_step(state, -1.0)
        self.assertEqual(next_state.ball_x, 20.0)
        self.assertEqual(next_state.t, 0.0)


class SessionStateTest(unittest.TestCase):
    def test_hidden_while_inactive_and_not_stopped(self):
        state = companion.SessionState()
        self.assertFalse(state.apply({"active": False, "stopped": False}))

    def test_visible_while_active(self):
        state = companion.SessionState()
        self.assertTrue(state.apply({"active": True, "stopped": False}))

    def test_stays_visible_while_stopped_even_if_no_longer_active(self):
        state = companion.SessionState()
        state.apply({"active": True, "stopped": False})
        self.assertTrue(state.apply({"active": False, "stopped": True}))

    def test_hides_again_once_a_stopped_session_fully_ends(self):
        state = companion.SessionState()
        state.apply({"active": True, "stopped": False})
        state.apply({"active": False, "stopped": True})
        self.assertFalse(state.apply({"active": False, "stopped": False}))

    def test_hide_until_next_session_hides_for_the_rest_of_this_session(self):
        state = companion.SessionState()
        state.apply({"active": True, "stopped": False})
        state.hide_until_next_session()
        self.assertFalse(state.apply({"active": True, "stopped": False}))
        self.assertFalse(state.apply({"active": True, "stopped": True}))

    def test_hide_until_next_session_clears_automatically_on_a_fresh_session(self):
        state = companion.SessionState()
        state.apply({"active": True, "stopped": False})
        state.hide_until_next_session()
        self.assertFalse(state.apply({"active": False, "stopped": False}))  # session ends
        self.assertTrue(state.apply({"active": True, "stopped": False}))    # a new one starts

    def test_missing_fields_default_to_hidden_not_a_crash(self):
        state = companion.SessionState()
        self.assertFalse(state.apply({}))


class BuildSampleTest(unittest.TestCase):
    def test_returns_null_windows_when_nothing_matches(self):
        with mock.patch.object(companion, "enum_windows", return_value=[]), \
             mock.patch.object(companion, "call_pingpong", return_value=None):
            result = companion.build_sample(Path("."))
        self.assertEqual(result["windows"], {"claude": None, "codex": None})
        self.assertIsNone(result["pingpong"])

    def test_reports_a_found_window(self):
        window = make_window(hwnd=42)
        with mock.patch.object(companion, "enum_windows", return_value=[window]), \
             mock.patch.object(companion, "call_pingpong",
                                        return_value={"active": True, "stopped": False,
                                                      "waiting": 1, "awaiting_reply": 0,
                                                      "untrusted": True}):
            result = companion.build_sample(Path("."))
        self.assertEqual(result["windows"]["claude"]["hwnd"], 42)
        self.assertIsNone(result["windows"]["codex"])
        self.assertTrue(result["pingpong"]["active"])


class CrosstalkGuardTest(unittest.TestCase):
    """Until ct.pingpong exists (or if the import fails), every call must
    degrade to None rather than raising. call_halt/call_resume run Crosstalk's
    CLI through ``ct.owner_control`` (see CrosstalkControlTest)."""

    def test_missing_crosstalk_module_is_safe(self):
        with mock.patch.object(companion, "ct", None):
            self.assertIsNone(companion.call_pingpong(Path(".")))

    def test_missing_attribute_is_safe(self):
        class Empty:
            pass

        with mock.patch.object(companion, "ct", Empty()):
            self.assertIsNone(companion.call_pingpong(Path(".")))

    def test_raising_function_is_swallowed(self):
        class Boom:
            def pingpong(self, _root):
                raise RuntimeError("nope")

        with mock.patch.object(companion, "ct", Boom()):
            self.assertIsNone(companion.call_pingpong(Path(".")))


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["python"], returncode=returncode, stdout=stdout, stderr=stderr)


class ConsolePythonTest(unittest.TestCase):
    def test_prefers_the_console_interpreter_over_pythonw(self):
        with mock.patch.object(companion.sys, "executable",
                                r"C:\eutonos\runtime\Scripts\pythonw.exe"):
            self.assertEqual(companion._console_python().name, "python.exe")

    def test_leaves_the_console_interpreter_alone(self):
        with mock.patch.object(companion.sys, "executable",
                                r"C:\eutonos\runtime\Scripts\python.exe"):
            self.assertEqual(companion._console_python().name, "python.exe")


class CrosstalkControlTest(unittest.TestCase):
    """J11 continues to apply through the fresh-process route: call_halt/call_resume
    must never raise, never collapse "it failed" into the shape of "it worked", and
    never touch the live Crosstalk device folder (a temp one is used) or run a real
    CLI (``ct.run_cli`` is replaced)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = Path(self.tmp.name)

    def write_controls(self, **value):
        (self.device / "controls.json").write_text(json.dumps(value), encoding="utf-8")

    def cli_that(self, **after):
        calls = []

        def run(argv, timeout_seconds=None):
            calls.append(list(argv))
            self.write_controls(**after)
            return 0, "{}"
        return run, calls

    def test_halt_runs_the_crosstalk_cli_and_reports_the_controls_read_back(self):
        run, calls = self.cli_that(messaging="on", halt=True, deny=[])
        with mock.patch.object(companion.ct, "run_cli", run):
            result = companion.call_halt(Path("."))
        self.assertEqual(result, {"ok": True, "result": {"messaging": "on", "halt": True, "deny": []}})
        argv = calls[0]
        self.assertEqual(argv[1:3], ["-I", "-B"])
        self.assertEqual(Path(argv[3]).name, "crosstalk_p0_cli.py")
        self.assertEqual(argv[4], "halt")

    # Stop never depends on the kit CLI: a halt whose CLI is missing, cannot start, times out, exits
    # nonzero or leaves the control unchanged falls back to writing the halt directly
    # (crosstalk_status.degraded_halt, covered in test_components). Resume has no such fallback and
    # keeps reporting each of those outcomes as a failure.
    def halted_on_disk(self):
        return json.loads((self.device / "controls.json").read_text(encoding="utf-8"))["halt"]

    def test_halt_that_did_not_change_the_controls_falls_back_to_a_direct_halt(self):
        self.write_controls(messaging="on", halt=False)
        with mock.patch.object(companion.ct, "run_cli", return_value=(0, "{}")):
            result = companion.call_halt(Path("."))
        self.assertEqual(result, {"ok": True, "result": {"messaging": "on", "halt": True, "deny": []}})
        self.assertIs(self.halted_on_disk(), True)

    def test_halt_nonzero_exit_falls_back_to_a_direct_halt(self):
        self.write_controls(messaging="on", halt=False)
        with mock.patch.object(companion.ct, "run_cli", return_value=(1, "")):
            result = companion.call_halt(Path("."))
        self.assertTrue(result["ok"])
        self.assertIs(self.halted_on_disk(), True)

    def test_halt_timeout_falls_back_to_a_direct_halt(self):
        with mock.patch.object(companion.ct, "run_cli",
                                side_effect=subprocess.TimeoutExpired(cmd="python", timeout=20)):
            result = companion.call_halt(Path("."))
        self.assertEqual(result, {"ok": True, "result": {"messaging": "off", "halt": True, "deny": []}})
        self.assertIs(self.halted_on_disk(), True)

    def test_halt_launch_failure_falls_back_to_a_direct_halt(self):
        with mock.patch.object(companion.ct, "run_cli", side_effect=OSError("no such file")):
            result = companion.call_halt(Path("."))
        self.assertTrue(result["ok"])
        self.assertIs(self.halted_on_disk(), True)

    def test_missing_crosstalk_cli_falls_back_to_a_direct_halt(self):
        with mock.patch.object(companion.ct, "cli_path", return_value=None):
            result = companion.call_halt(Path("."))
        self.assertTrue(result["ok"])
        self.assertIs(self.halted_on_disk(), True)

    def test_halt_that_cannot_be_written_is_a_failure(self):
        with mock.patch.object(companion.ct, "cli_path", return_value=None),                 mock.patch.object(companion.ct, "_controls_lock", side_effect=TimeoutError("busy")):
            result = companion.call_halt(Path("."))
        self.assertEqual(result, {"ok": False, "exception": "controls-lock-busy"})

    def test_resume_launch_failure_is_unavailable(self):
        with mock.patch.object(companion.ct, "run_cli", side_effect=OSError("no such file")):
            result = companion.call_resume(Path("."))
        self.assertFalse(result["ok"])
        self.assertTrue(result["unavailable"])

    def test_missing_crosstalk_cli_is_unavailable_for_resume(self):
        with mock.patch.object(companion.ct, "cli_path", return_value=None):
            result = companion.call_resume(Path("."))
        self.assertFalse(result["ok"])
        self.assertTrue(result["unavailable"])

    def test_missing_crosstalk_module_is_unavailable_for_both_actions(self):
        with mock.patch.object(companion, "ct", None):
            self.assertTrue(companion.call_halt(Path("."))["unavailable"])
            self.assertTrue(companion.call_resume(Path("."))["unavailable"])

    def test_resume_success_reports_ok(self):
        self.write_controls(messaging="on", halt=True)
        run, calls = self.cli_that(messaging="on", halt=False, deny=[])
        with mock.patch.object(companion.ct, "run_cli", run):
            result = companion.call_resume(Path("."))
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][4], "resume")

    def test_resume_that_left_it_halted_is_a_failure(self):
        self.write_controls(messaging="on", halt=True)
        with mock.patch.object(companion.ct, "run_cli", return_value=(0, "{}")):
            result = companion.call_resume(Path("."))
        self.assertEqual(result, {"ok": False, "exception": "crosstalk-control-not-applied"})

    def test_resume_timeout_is_reported(self):
        with mock.patch.object(companion.ct, "run_cli",
                                side_effect=subprocess.TimeoutExpired(cmd="python", timeout=20)):
            result = companion.call_resume(Path("."))
        self.assertEqual(result, {"ok": False, "timeout": True})


class SummarizeHaltResultTest(unittest.TestCase):
    """Pure mapping from a call_halt() outcome to what the overlay shows."""

    def test_success_has_no_message(self):
        summary = companion.summarize_halt_result({"ok": True, "result": {"halted": True}})
        self.assertEqual(summary, {"ok": True, "message": ""})

    def test_none_is_a_failure(self):
        summary = companion.summarize_halt_result(None)
        self.assertFalse(summary["ok"])
        self.assertIn("no response", summary["message"])

    def test_timeout_is_a_failure(self):
        summary = companion.summarize_halt_result({"ok": False, "timeout": True})
        self.assertFalse(summary["ok"])
        self.assertIn("timed out", summary["message"])

    def test_unavailable_is_a_failure(self):
        summary = companion.summarize_halt_result({"ok": False, "unavailable": True})
        self.assertFalse(summary["ok"])
        self.assertIn("unavailable", summary["message"])

    def test_exception_message_names_the_exception_type(self):
        summary = companion.summarize_halt_result(
            {"ok": False, "exception": "RuntimeError", "message": "disk is full"})
        self.assertFalse(summary["ok"])
        self.assertIn("RuntimeError", summary["message"])

    def test_failure_with_no_detail_still_produces_a_message(self):
        summary = companion.summarize_halt_result({"ok": False})
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["message"])


class SummarizeResumeResultTest(unittest.TestCase):
    def test_success_has_no_message(self):
        summary = companion.summarize_resume_result({"ok": True, "result": {"stop": False}})
        self.assertEqual(summary, {"ok": True, "message": ""})

    def test_none_is_a_failure(self):
        summary = companion.summarize_resume_result(None)
        self.assertFalse(summary["ok"])
        self.assertIn("no response", summary["message"])

    def test_timeout_is_a_failure(self):
        summary = companion.summarize_resume_result({"ok": False, "timeout": True})
        self.assertFalse(summary["ok"])
        self.assertIn("timed out", summary["message"])

    def test_not_applied_is_a_failure_with_a_message(self):
        summary = companion.summarize_resume_result({"ok": False, "exception": "crosstalk-control-not-applied"})
        self.assertFalse(summary["ok"])
        self.assertIn("crosstalk-control-not-applied", summary["message"])


class _FakeOverlay:
    """Just enough of ``Overlay`` for ``_sync_ui_state``/``show_halt_result``/
    ``clear_failed`` to run unbound against it -- no Tk widget involved (see
    the module docstring above)."""

    def __init__(self, snapshot=None, failed_ui=False):
        self.app = mock.Mock(snapshot=snapshot or {})
        self.stopped_ui = False
        self.failed_ui = failed_ui
        self._last_failure_message = ""
        self._failure_hide_job = None
        self.render_calls = []

    def _render_pong(self):
        self.render_calls.append("pong")

    def _render_stop_button(self):
        self.render_calls.append("stop")

    def _cancel_failure_hide(self):
        pass

    def _show_failure_tooltip(self, _text):
        pass

    def clear_failed(self):
        companion.Overlay.clear_failed(self)


class InstallSignatureTest(unittest.TestCase):
    """``install_signature()`` is what ``App`` compares at startup against a
    later poll to notice the installed package changed under a long-lived
    widget (see the module docstring's "stale copy" note, and
    ``App.restart_self``)."""

    def test_combines_version_and_the_status_modules_own_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "crosstalk_status.py"
            module_path.write_text("# fake status module\n")
            fake_mb = mock.Mock(__file__=str(module_path))
            with mock.patch.object(companion, "eutonos_build_version", return_value="1.2.3"), \
                    mock.patch.object(companion, "ct", fake_mb):
                version, mtime = companion.install_signature()
            self.assertEqual(version, "1.2.3")
            self.assertAlmostEqual(mtime, module_path.stat().st_mtime, delta=0.001)

    def test_no_status_module_reports_none_mtime_not_a_crash(self):
        with mock.patch.object(companion, "eutonos_build_version", return_value="1.2.3"), \
                mock.patch.object(companion, "ct", None):
            self.assertEqual(companion.install_signature(), ("1.2.3", None))

    def test_a_missing_file_on_disk_is_safe(self):
        fake_mb = mock.Mock(__file__=r"C:\does\not\exist\crosstalk_status.py")
        with mock.patch.object(companion, "eutonos_build_version", return_value="1.2.3"), \
                mock.patch.object(companion, "ct", fake_mb):
            self.assertEqual(companion.install_signature(), ("1.2.3", None))

    def test_a_changed_mtime_changes_the_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "crosstalk_status.py"
            module_path.write_text("# v1\n")
            fake_mb = mock.Mock(__file__=str(module_path))
            with mock.patch.object(companion, "eutonos_build_version", return_value="1.2.3"), \
                    mock.patch.object(companion, "ct", fake_mb):
                before = companion.install_signature()
            newer = module_path.stat().st_mtime + 5
            import os as _os
            _os.utime(module_path, (newer, newer))
            with mock.patch.object(companion, "eutonos_build_version", return_value="1.2.3"), \
                    mock.patch.object(companion, "ct", fake_mb):
                after = companion.install_signature()
            self.assertNotEqual(before, after)


class RelaunchProcessTest(unittest.TestCase):
    """``relaunch_process()`` is the detached-respawn half of
    ``App.restart_self`` -- exercised here without ever actually spawning a
    process."""

    def test_spawns_a_detached_process_with_the_same_interpreter_and_argv(self):
        with mock.patch.object(companion.sys, "argv", ["pingpong_companion.py", "--root", "C:/repo"]), \
                mock.patch.object(companion.subprocess, "Popen") as popen:
            self.assertTrue(companion.relaunch_process())
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [companion.sys.executable, "pingpong_companion.py", "--root", "C:/repo"])
        self.assertTrue(kwargs["creationflags"] & companion.subprocess.DETACHED_PROCESS)
        self.assertTrue(kwargs["creationflags"] & companion.subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_a_launch_failure_returns_false_without_raising(self):
        with mock.patch.object(companion.subprocess, "Popen", side_effect=OSError("no such file")):
            self.assertFalse(companion.relaunch_process())


class _FakeLabelProbe:
    """Stand-in for ``LabelProbe`` that lets a test control exactly when a
    "fresh" probe line lands, the same way the real one's ``generation()``
    counts lines regardless of whether their content changed."""

    def __init__(self):
        self._value = None
        self._generation = 0

    def push(self, value):
        self._value = value
        self._generation += 1

    def read(self, _key):
        return self._value

    def generation(self, _key):
        return self._generation


def _make_resize_overlay(hwnd=42):
    overlay = companion.Overlay.__new__(companion.Overlay)
    overlay.app = mock.Mock(label_probe=_FakeLabelProbe())
    overlay.key = "codex"
    overlay.window = make_window(hwnd=hwnd)
    overlay.label_offset = (-100, -20, -40, -5)
    overlay.last_label_time = float("-inf")
    overlay._last_rect_size = None
    overlay._resize_pending = False
    overlay._resize_settled = False
    overlay._resize_last_change = 0.0
    overlay._resize_probe_baseline = 0
    overlay._suppress_until_label_found = False
    return overlay


class OverlayResizeHandlingTest(unittest.TestCase):
    """``Overlay._handle_resize`` (owner report, 2026-09-27): Codex's composer
    is responsive, so a resize -- unlike a plain move -- must hide the widget
    and wait for a probe answer that postdates the resize settling, instead
    of showing at a stale offset."""

    def test_a_pure_move_never_blocks_or_touches_the_probe(self):
        overlay = _make_resize_overlay()
        self.assertFalse(overlay._handle_resize((100, 100, 900, 700), 0.0))
        self.assertFalse(overlay._handle_resize((150, 140, 950, 740), 0.05))  # same size, moved
        self.assertEqual(overlay.label_offset, (-100, -20, -40, -5))
        self.assertFalse(overlay._resize_pending)

    def test_a_size_change_hides_at_once_and_clears_the_stale_offset(self):
        overlay = _make_resize_overlay()
        overlay._handle_resize((100, 100, 900, 700), 0.0)
        blocked = overlay._handle_resize((100, 100, 950, 700), 0.01)  # width changed
        self.assertTrue(blocked)
        self.assertTrue(overlay._resize_pending)
        self.assertIsNone(overlay.label_offset)

    def test_stays_blocked_until_the_size_has_been_stable_long_enough(self):
        overlay = _make_resize_overlay()
        overlay._handle_resize((100, 100, 900, 700), 0.0)
        overlay._handle_resize((100, 100, 950, 700), 1.0)
        still_settling = 1.0 + companion.RESIZE_SETTLE_SECONDS / 2
        self.assertTrue(overlay._handle_resize((100, 100, 950, 700), still_settling))
        self.assertTrue(overlay._resize_pending)

    def test_ignores_a_probe_line_cached_before_the_resize_settled(self):
        overlay = _make_resize_overlay()
        overlay._handle_resize((100, 100, 900, 700), 0.0)
        overlay._handle_resize((100, 100, 950, 700), 1.0)
        overlay.app.label_probe.push({"found": True, "hwnd": overlay.window.hwnd,
                                       "left": 10, "top": 10, "right": 20, "bottom": 20,
                                       "wright": 900, "wbottom": 700})  # a stale, mid-resize line
        settle_time = 1.0 + companion.RESIZE_SETTLE_SECONDS + 0.01
        self.assertTrue(overlay._handle_resize((100, 100, 950, 700), settle_time))  # settles, captures baseline
        self.assertIsNone(overlay.label_offset)
        self.assertTrue(overlay._handle_resize((100, 100, 950, 700), settle_time + 0.01))  # still no fresh line
        self.assertIsNone(overlay.label_offset)

    def test_shows_only_once_a_fresh_probe_line_lands_after_settling(self):
        overlay = _make_resize_overlay()
        overlay._handle_resize((100, 100, 900, 700), 0.0)
        overlay._handle_resize((100, 100, 950, 700), 1.0)
        settle_time = 1.0 + companion.RESIZE_SETTLE_SECONDS + 0.01
        overlay._handle_resize((100, 100, 950, 700), settle_time)
        overlay.app.label_probe.push({"found": True, "hwnd": overlay.window.hwnd,
                                       "left": 500, "top": 600, "right": 560, "bottom": 620,
                                       "wright": 950, "wbottom": 700})  # a fresh, post-settle line
        blocked = overlay._handle_resize((100, 100, 950, 700), settle_time + 0.02)
        self.assertFalse(blocked)
        self.assertIsNotNone(overlay.label_offset)
        self.assertFalse(overlay._resize_pending)

    def test_a_fresh_miss_after_resize_suppresses_the_fallback_until_found(self):
        overlay = _make_resize_overlay()
        overlay._handle_resize((100, 100, 900, 700), 0.0)
        overlay._handle_resize((100, 100, 950, 700), 1.0)
        settle_time = 1.0 + companion.RESIZE_SETTLE_SECONDS + 0.01
        overlay._handle_resize((100, 100, 950, 700), settle_time)
        overlay.app.label_probe.push({"found": False})  # narrow window, side panel open: no label at all
        blocked = overlay._handle_resize((100, 100, 950, 700), settle_time + 0.01)
        self.assertTrue(blocked)
        self.assertTrue(overlay._suppress_until_label_found)
        self.assertIsNone(overlay.label_offset)
        self.assertFalse(overlay._resize_pending)  # resolved -- just suppressed, not "still resizing"
        # Stays hidden on later ticks even though the size hasn't changed again.
        self.assertTrue(overlay._handle_resize((100, 100, 950, 700), settle_time + 0.5))
        # Only a later found=True (no new resize needed) clears it.
        overlay.app.label_probe.push({"found": True, "hwnd": overlay.window.hwnd,
                                       "left": 500, "top": 600, "right": 560, "bottom": 620,
                                       "wright": 950, "wbottom": 700})
        blocked = overlay._handle_resize((100, 100, 950, 700), settle_time + 0.6)
        self.assertFalse(blocked)
        self.assertFalse(overlay._suppress_until_label_found)
        self.assertIsNotNone(overlay.label_offset)


class SyncUiStateFailureRingTest(unittest.TestCase):
    """C5/P2: a STOP-only ``pingpong()`` snapshot does not prove `halt()`
    finished (it can write STOP and then fail HALTED or a cancel marker --
    a halt can fail part-way), so it must not clear a failure ring on
    its own. Only a later successful ``call_halt`` (``show_halt_result`` ->
    ``clear_failed``) may do that."""

    def test_failed_halt_then_stop_only_snapshot_keeps_the_ring(self):
        overlay = _FakeOverlay(snapshot={"stopped": True}, failed_ui=True)

        companion.Overlay._sync_ui_state(overlay)

        self.assertTrue(overlay.failed_ui)

    def test_successful_halt_clears_the_ring(self):
        overlay = _FakeOverlay(snapshot={"stopped": True}, failed_ui=True)

        companion.Overlay.show_halt_result(overlay, {"ok": True})

        self.assertFalse(overlay.failed_ui)


if __name__ == "__main__":
    unittest.main()
