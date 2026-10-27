"""Deterministic coverage for the router quick fixes A1-A7 (helperctl.py).

Nothing here touches the live desktop: window enumeration, cursor reads,
SendInput, SetCursorPos and active-context probes are all replaced by fakes.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime

import helperctl
import simon_common


FAKE_CONTEXT: dict[str, Any] = {
    "active_window": {"title": "Fake App", "hwnd": 501, "process_name": "fake.exe"},
    "dialog_open": False,
    "current_dialog": None,
}


def make_window(hwnd: int, title: str, left: int = 0, top: int = 0, right: int = 800, bottom: int = 600) -> helperctl.WindowInfo:
    return helperctl.WindowInfo(hwnd, title, left, top, right, bottom, False)


def fake_list_windows_factory(windows: list[helperctl.WindowInfo], calls: list[Any] | None = None):
    def fake_list_windows(partial_title: str | None = None) -> list[helperctl.WindowInfo]:
        if calls is not None:
            calls.append(partial_title)
        if partial_title:
            return [window for window in windows if partial_title.lower() in window.title.lower()]
        return list(windows)

    return fake_list_windows


def simple_window_payload(window: helperctl.WindowInfo | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {"hwnd": window.hwnd, "title": window.title, "process_name": "app.exe", "class_name": "AppWindow"}


def parse_cli_payload(argv: list[str]) -> dict[str, Any]:
    namespace = helperctl.build_cli_parser().parse_args(argv)
    extra = json.loads(namespace.json) if getattr(namespace, "json", "") else {}
    return helperctl.build_cli_payload(namespace, extra)


class RouterRuntimeFixture(RuntimeGuardedTestCase):
    """Redirects every runtime path helperctl writes to a temporary directory."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runtime_dir = Path(self.temp_dir.name) / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.path_values = redirect_runtime(self, self.runtime_dir)
        context_patcher = mock.patch.object(helperctl, "build_active_context_packet", return_value=dict(FAKE_CONTEXT))
        context_patcher.start()
        self.addCleanup(context_patcher.stop)

    @property
    def memory_file(self) -> Path:
        return self.path_values["PLANNER_MEMORY_FILE"]

    def seed_memory(self, memory: dict[str, Any]) -> str:
        text = json.dumps(memory, indent=2)
        self.memory_file.write_text(text, encoding="utf-8")
        return text

    def read_memory(self) -> dict[str, Any]:
        return json.loads(self.memory_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- A1 + A5 CLI


class CliDefaultInjectionTests(RouterRuntimeFixture):
    def test_unpassed_flags_are_not_forwarded(self) -> None:
        self.assertEqual(parse_cli_payload(["observe.windows"]), {})

    def test_explicit_flags_are_forwarded_and_json_keys_override(self) -> None:
        payload = parse_cli_payload(
            ["verify.window_state", "--title", "From CLI", "--columns", "5", "--json", '{"title": "From JSON", "title_contains": "Note"}']
        )
        self.assertEqual(payload, {"title": "From JSON", "columns": 5, "title_contains": "Note"})

    def test_main_in_process_dispatch_receives_only_explicit_flags(self) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        def fake_dispatch(command: str, payload: dict[str, Any]) -> dict[str, Any]:
            seen.append((command, dict(payload)))
            return helperctl.command_result("SUCCESS", command, "ok")

        with mock.patch.object(helperctl, "dispatch", side_effect=fake_dispatch), redirect_stdout(io.StringIO()):
            exit_code = helperctl.main(["planner.fast_observe", "--rows", "4", "--pretty"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(seen, [("planner.fast_observe", {"rows": 4})])

    def test_main_bridge_path_keeps_timeout_and_strips_control_flags(self) -> None:
        seen: list[tuple[str, dict[str, Any], float]] = []

        def fake_send(command: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
            seen.append((command, dict(payload), timeout))
            return helperctl.command_result("SUCCESS", command, "ok")

        with mock.patch.object(helperctl, "send_to_helper_app", side_effect=fake_send), mock.patch.object(
            helperctl, "dispatch", side_effect=AssertionError("must not dispatch in-process")
        ), redirect_stdout(io.StringIO()):
            helperctl.main(["status.current", "--via-app", "--timeout", "5", "--title", "Notepad", "--pretty"])
            helperctl.main(["status.current", "--via-app"])

        self.assertEqual(seen[0], ("status.current", {"title": "Notepad", "timeout": 5.0}, 5.0))
        self.assertEqual(seen[1], ("status.current", {}, helperctl.DEFAULT_CLI_BRIDGE_TIMEOUT_SECONDS))

    def test_observe_windows_does_not_filter_by_simon_target(self) -> None:
        windows = [make_window(1, "Simon Target"), make_window(2, "Untitled - Notepad"), make_window(3, "Inbox - Mail")]
        calls: list[Any] = []
        with mock.patch.object(helperctl, "list_windows", side_effect=fake_list_windows_factory(windows, calls)), mock.patch.object(
            helperctl, "window_payload", side_effect=simple_window_payload
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = helperctl.main(["observe.windows"])

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [None])
        self.assertEqual([window["hwnd"] for window in result["data"]["windows"]], [1, 2, 3])

    def test_verify_window_state_matches_with_only_title_contains(self) -> None:
        windows = [make_window(1, "Simon Target"), make_window(2, "Untitled - Notepad")]
        payload = parse_cli_payload(["verify.window_state", "--json", '{"title_contains": "Notepad"}'])
        with mock.patch.object(helperctl, "list_windows", side_effect=fake_list_windows_factory(windows)), mock.patch.object(
            helperctl, "window_payload", side_effect=simple_window_payload
        ), mock.patch.object(helperctl, "get_foreground_window_snapshot", return_value={"hwnd": 2}):
            result = helperctl.cmd_verify_window_state(payload)

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"]["matched_window"]["hwnd"], 2)
        self.assertEqual(result["data"]["expected_exact_title"], "")
        self.assertEqual(result["data"]["expected_title_contains"], "Notepad")

    def test_verify_window_state_without_identity_still_checks_simon_target(self) -> None:
        windows = [make_window(2, "Untitled - Notepad"), make_window(1, "Simon Target")]
        with mock.patch.object(helperctl, "list_windows", side_effect=fake_list_windows_factory(windows)), mock.patch.object(
            helperctl, "window_payload", side_effect=simple_window_payload
        ), mock.patch.object(helperctl, "get_foreground_window_snapshot", return_value={"hwnd": 2}):
            result = helperctl.cmd_verify_window_state(parse_cli_payload(["verify.window_state"]))

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual([item["hwnd"] for item in result["data"]["matched_windows"]], [1])

    def test_planner_fast_observe_defaults_target_to_desktop(self) -> None:
        captured: list[dict[str, Any]] = []

        def fake_capture_grid(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(dict(payload))
            return helperctl.command_result("NEEDS_PLANNER", "recovery.capture_grid", "captured")

        with mock.patch.object(helperctl, "cmd_recovery_capture_grid", side_effect=fake_capture_grid):
            helperctl.cmd_planner_fast_observe(parse_cli_payload(["planner.fast_observe"]))
            helperctl.cmd_planner_fast_observe(parse_cli_payload(["planner.fast_observe", "--target", "target_window"]))

        self.assertEqual(captured[0]["target"], "desktop")
        self.assertEqual((captured[0]["columns"], captured[0]["rows"]), (12, 8))
        self.assertNotIn("title", captured[0])
        self.assertEqual(captured[1]["target"], "target_window")

    def test_click_point_keeps_its_own_window_coordinate_default(self) -> None:
        window = make_window(77, "Simon Target", left=100, top=200, right=500, bottom=600)
        click = mock.Mock(return_value={"click_verified": True, "injection_succeeded": True})
        click_guardrail = mock.Mock(return_value=None)
        with mock.patch.multiple(
            helperctl,
            nudge_cursor_from_failsafe_corner=mock.Mock(return_value={}),
            resolve_working_window=mock.Mock(return_value=window),
            evaluate_vm_boundary_guardrail=mock.Mock(return_value=None),
            cmd_focus_window=mock.Mock(return_value=helperctl.command_result("SUCCESS", "focus.window", "ok")),
            evaluate_click_guardrails=click_guardrail,
            get_foreground_window_snapshot=mock.Mock(return_value={"hwnd": 77}),
            perform_screen_click=click,
            cancellable_sleep=mock.Mock(),
            window_payload=mock.Mock(side_effect=simple_window_payload),
        ):
            window_result = helperctl.cmd_input_click_point(parse_cli_payload(["input.click_point", "--x", "10", "--y", "20"]))
            screen_result = helperctl.cmd_input_click_point(
                parse_cli_payload(["input.click_point", "--x", "10", "--y", "20", "--coordinate-mode", "screen"])
            )

        self.assertEqual(window_result["status"], "SUCCESS")
        self.assertEqual(screen_result["status"], "SUCCESS")
        self.assertEqual(click.call_args_list[0].args, (110, 220))
        self.assertEqual(click.call_args_list[1].args, (10, 20))
        self.assertEqual(click_guardrail.call_args_list[0].args[1]["target"], "target_window")


class CompactOutputTests(RuntimeGuardedTestCase):
    def test_emit_defaults_to_single_line_json(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = helperctl.emit(helperctl.command_result("SUCCESS", "helper.commands", "ok", data={"a": [1, 2]}))

        text = output.getvalue().strip()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn(": ", text)
        self.assertEqual(json.loads(text)["data"], {"a": [1, 2]})

    def test_emit_pretty_is_indented(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            helperctl.emit(helperctl.command_result("SUCCESS", "helper.commands", "ok", data={"a": 1}), pretty=True)

        lines = output.getvalue().strip().splitlines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[1].startswith('  "'))

    def test_main_pretty_flag_controls_output_and_is_not_forwarded(self) -> None:
        payloads: list[dict[str, Any]] = []

        def fake_dispatch(command: str, payload: dict[str, Any]) -> dict[str, Any]:
            payloads.append(dict(payload))
            return helperctl.command_result("SUCCESS", command, "ok", data={"value": 1})

        compact = io.StringIO()
        pretty = io.StringIO()
        with mock.patch.object(helperctl, "dispatch", side_effect=fake_dispatch):
            with redirect_stdout(compact):
                helperctl.main(["helper.commands"])
            with redirect_stdout(pretty):
                helperctl.main(["helper.commands", "--pretty"])

        self.assertEqual(len(compact.getvalue().strip().splitlines()), 1)
        self.assertGreater(len(pretty.getvalue().strip().splitlines()), 1)
        self.assertEqual(payloads, [{}, {}])


# ---------------------------------------------------------------- A2 click


class FakeCursor:
    def __init__(self, after_move: tuple[int, int] | None, after_correction: tuple[int, int] | None = None) -> None:
        self.position: tuple[int, int] | None = (0, 0)
        self.after_move = after_move
        self.after_correction = after_correction

    def read(self) -> dict[str, Any]:
        if self.position is None:
            return {"available": False}
        return {"available": True, "x": self.position[0], "y": self.position[1]}


class ScreenClickTests(RuntimeGuardedTestCase):
    def run_click(
        self,
        cursor: FakeCursor,
        *,
        move_result: dict[str, Any] | None = None,
        down_result: dict[str, Any] | None = None,
        set_cursor_ok: bool = True,
        set_cursor_error: int = 0,
        on_down: Any = None,
        before_press: Any = None,
    ) -> tuple[dict[str, Any], list[tuple[int, bool]], mock.Mock, mock.Mock]:
        sent: list[tuple[int, bool]] = []

        def fake_send_input(flags: int, dx: int = 0, dy: int = 0, *, check_cancel: bool = True) -> dict[str, Any]:
            if check_cancel:
                helperctl.check_command_cancelled()
            sent.append((flags, check_cancel))
            if flags & helperctl.MOUSEEVENTF_MOVE:
                cursor.position = cursor.after_move
                return dict(move_result or {"count": 1, "last_error": 0})
            if flags == helperctl.MOUSEEVENTF_LEFTDOWN:
                if on_down is not None:
                    on_down()
                return dict(down_result or {"count": 1, "last_error": 0})
            return {"count": 1, "last_error": 0}

        last_error = {"value": 0}

        def fake_set_cursor_pos(_x: int, _y: int) -> bool:
            last_error["value"] = 0 if set_cursor_ok else set_cursor_error
            if set_cursor_ok and cursor.after_correction is not None:
                cursor.position = cursor.after_correction
            return set_cursor_ok

        set_cursor = mock.Mock(side_effect=fake_set_cursor_pos)
        legacy_mouse_event = mock.Mock()
        fake_user32 = SimpleNamespace(SetCursorPos=set_cursor, mouse_event=legacy_mouse_event)
        fake_ctypes = SimpleNamespace(
            WinDLL=mock.Mock(return_value=fake_user32),
            set_last_error=lambda value: last_error.update(value=value),
            get_last_error=lambda: last_error["value"],
        )
        blind_pyautogui = mock.Mock()
        blind_pyautogui.click.side_effect = AssertionError("perform_screen_click must never fall back to pyautogui.click")
        with mock.patch.multiple(
            helperctl,
            ctypes=fake_ctypes,
            pyautogui=blind_pyautogui,
            read_cursor_position=mock.Mock(side_effect=cursor.read),
            normalized_virtual_mouse_coordinate=mock.Mock(
                return_value={"x": 1234, "y": 4321, "virtual_bounds": {"left": -3840, "top": 0, "width": 16800, "height": 2160}}
            ),
            send_input_mouse_event=fake_send_input,
            cancellable_sleep=mock.Mock(),
        ):
            result = helperctl.perform_screen_click(-1500, 700, before_press=before_press)
        fake_ctypes.WinDLL.assert_called_once_with("user32", use_last_error=True)
        self.assertEqual(set_cursor.argtypes, (helperctl.wintypes.INT, helperctl.wintypes.INT))
        self.assertIs(set_cursor.restype, helperctl.wintypes.BOOL)
        return result, sent, set_cursor, legacy_mouse_event

    def presses(self, sent: list[tuple[int, bool]]) -> list[int]:
        return [flags for flags, _ in sent if flags in {helperctl.MOUSEEVENTF_LEFTDOWN, helperctl.MOUSEEVENTF_LEFTUP}]

    def test_window_change_after_cursor_move_prevents_the_press(self) -> None:
        result, sent, _, _ = self.run_click(FakeCursor(after_move=(-1500, 700)), before_press=lambda: 'Window moved')
        self.assertEqual(self.presses(sent), [])
        self.assertEqual(result['press_guard_error'], 'Window moved')
        self.assertEqual(result['click_count'], 0)

    def test_one_pixel_offset_is_accepted_with_exactly_one_click(self) -> None:
        result, sent, set_cursor, legacy_mouse_event = self.run_click(FakeCursor(after_move=(-1499, 700)))

        self.assertEqual(self.presses(sent), [helperctl.MOUSEEVENTF_LEFTDOWN, helperctl.MOUSEEVENTF_LEFTUP])
        set_cursor.assert_not_called()
        legacy_mouse_event.assert_not_called()
        self.assertTrue(result["click_verified"])
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(result["click_method"], "sendinput")
        self.assertEqual(result["move_method"], "sendinput_absolute")
        self.assertFalse(result["move_fallback_used"])
        self.assertEqual(result["requested_point"], {"x": -1500, "y": 700})
        self.assertEqual(result["verified_point"], {"x": -1499, "y": 700})
        self.assertEqual(result["cursor_offset_px"], 1)
        self.assertEqual(result["cursor_tolerance_px"], helperctl.CLICK_CURSOR_TOLERANCE_PX)
        self.assertEqual(helperctl.CLICK_CURSOR_TOLERANCE_PX, 2)

    def test_large_offset_is_corrected_then_clicked_once(self) -> None:
        result, sent, set_cursor, legacy_mouse_event = self.run_click(FakeCursor(after_move=(-1460, 670), after_correction=(-1500, 700)))

        set_cursor.assert_called_once_with(-1500, 700)
        self.assertEqual(self.presses(sent), [helperctl.MOUSEEVENTF_LEFTDOWN, helperctl.MOUSEEVENTF_LEFTUP])
        legacy_mouse_event.assert_not_called()
        self.assertTrue(result["click_verified"])
        self.assertEqual(result["click_count"], 1)
        self.assertEqual(result["move_method"], "SetCursorPos_correction")
        self.assertTrue(result["move_fallback_used"])
        self.assertEqual(result["fallback_result"]["cursor_offset_before_correction_px"], 40)
        self.assertEqual(result["verified_point"], {"x": -1500, "y": 700})

    def test_unverifiable_cursor_makes_zero_clicks_and_reports_cursor_unverified(self) -> None:
        result, sent, set_cursor, legacy_mouse_event = self.run_click(FakeCursor(after_move=(-1400, 650), after_correction=(-1400, 650)))

        set_cursor.assert_called_once()
        self.assertEqual(self.presses(sent), [])
        legacy_mouse_event.assert_not_called()
        self.assertFalse(result["click_verified"])
        self.assertEqual(result["click_count"], 0)
        self.assertIsNone(result["verified_point"])
        self.assertEqual(result["click_skipped_reason"], "cursor_unverified")
        with mock.patch.object(helperctl, "current_process_elevation", return_value={"available": True, "is_elevated": False}):
            recovery = helperctl.input_permission_recovery_payload(result, "input.click_point")
        self.assertEqual(recovery["failure_type"], "INPUT_CURSOR_UNVERIFIED")
        self.assertEqual(recovery["recommended_recovery_flow"], ["helper.exit", "helper.start_elevated", "planner.fast_observe"])

    def test_failed_cursor_correction_preserves_last_error_and_makes_zero_clicks(self) -> None:
        result, sent, set_cursor, legacy_mouse_event = self.run_click(
            FakeCursor(after_move=(-1400, 650)),
            set_cursor_ok=False,
            set_cursor_error=5,
        )

        set_cursor.assert_called_once_with(-1500, 700)
        self.assertFalse(result["fallback_result"]["set_cursor_succeeded"])
        self.assertEqual(result["fallback_result"]["set_cursor_last_error"], 5)
        self.assertFalse(result["cursor_verified"])
        self.assertEqual(result["click_count"], 0)
        self.assertEqual(self.presses(sent), [])
        legacy_mouse_event.assert_not_called()

    def test_unreadable_cursor_makes_zero_clicks(self) -> None:
        cursor = FakeCursor(after_move=None, after_correction=None)
        result, sent, _set_cursor, legacy_mouse_event = self.run_click(cursor)

        self.assertEqual(self.presses(sent), [])
        legacy_mouse_event.assert_not_called()
        self.assertEqual(result["click_count"], 0)
        self.assertFalse(result["cursor_read_available"])
        with mock.patch.object(helperctl, "current_process_elevation", return_value={"available": True, "is_elevated": True}):
            recovery = helperctl.input_permission_recovery_payload(result, "input.click_point")
        self.assertEqual(recovery["failure_type"], "INPUT_CURSOR_UNVERIFIED")

    def test_sendinput_access_denied_gets_no_blind_fallback_click(self) -> None:
        result, sent, _set_cursor, legacy_mouse_event = self.run_click(
            FakeCursor(after_move=(0, 0), after_correction=(-1500, 700)),
            move_result={"count": 0, "last_error": 5},
            down_result={"count": 0, "last_error": 5},
        )

        self.assertEqual(self.presses(sent), [helperctl.MOUSEEVENTF_LEFTDOWN])
        legacy_mouse_event.assert_not_called()
        self.assertEqual(result["click_count"], 0)
        self.assertFalse(result["click_verified"])
        with mock.patch.object(helperctl, "current_process_elevation", return_value={"available": True, "is_elevated": False}):
            recovery = helperctl.input_permission_recovery_payload(result, "input.click_point")
        self.assertEqual(recovery["failure_type"], "INPUT_PERMISSION_DENIED")

    def test_cancel_after_press_still_releases_button_once(self) -> None:
        token = helperctl.CommandCancellationToken()
        with helperctl.command_cancellation(token):
            result, sent, _set_cursor, legacy_mouse_event = self.run_click(
                FakeCursor(after_move=(-1500, 700)),
                on_down=lambda: token.cancel("unit test stop"),
            )

        self.assertEqual(self.presses(sent), [helperctl.MOUSEEVENTF_LEFTDOWN, helperctl.MOUSEEVENTF_LEFTUP])
        self.assertEqual(sent[-1], (helperctl.MOUSEEVENTF_LEFTUP, False))
        legacy_mouse_event.assert_not_called()
        self.assertEqual(result["click_count"], 1)

    def test_cancel_before_press_makes_no_click(self) -> None:
        token = helperctl.CommandCancellationToken()
        token.cancel("unit test stop")
        with helperctl.command_cancellation(token):
            with self.assertRaises(helperctl.CommandCancelled):
                self.run_click(FakeCursor(after_move=(-1500, 700)))


# ---------------------------------------------------------------- A3 helper filter


class HelperWindowFilterTests(RuntimeGuardedTestCase):
    def setUp(self) -> None:
        self.helper = make_window(10, "Simon Start Search Helper")
        self.other = make_window(20, "Windows Search", left=100, top=100, right=900, bottom=700)
        self.payloads = {
            10: {"hwnd": 10, "title": self.helper.title, "process_name": "python.exe", "class_name": "TkTopLevel"},
            20: {"hwnd": 20, "title": self.other.title, "process_name": "SearchHost.exe", "class_name": "Windows.UI.Core.CoreWindow"},
        }

    def test_find_likely_search_window_skips_helper_windows(self) -> None:
        with mock.patch.multiple(
            helperctl,
            list_windows=mock.Mock(return_value=[self.helper, self.other]),
            window_payload=mock.Mock(side_effect=lambda window: self.payloads[window.hwnd]),
            get_class_name=mock.Mock(side_effect=lambda hwnd: self.payloads[hwnd]["class_name"]),
            get_process_name_for_hwnd=mock.Mock(side_effect=lambda hwnd: self.payloads[hwnd]["process_name"]),
        ):
            window = helperctl.find_likely_search_window()

        self.assertIsNotNone(window)
        self.assertEqual(window.hwnd, 20)

    @unittest.skipIf(helperctl.Image is None or helperctl.ImageDraw is None, "Pillow is required for the synthetic canvas")
    def test_synthetic_visible_windows_excludes_helper_windows(self) -> None:
        with mock.patch.multiple(
            helperctl,
            list_windows=mock.Mock(return_value=[self.helper, self.other]),
            window_payload=mock.Mock(side_effect=lambda window: self.payloads[window.hwnd]),
            capture_window_printwindow=mock.Mock(return_value=None),
        ):
            image, meta = helperctl.capture_synthetic_visible_windows()

        self.assertIsNotNone(image)
        self.assertEqual(meta["method"], "synthetic_visible_windows")
        self.assertEqual([item["window"]["hwnd"] for item in meta["synthetic_windows"]], [20])


# ---------------------------------------------------------------- A4 sequence


class ActionSequenceForwardingTests(RuntimeGuardedTestCase):
    def run_sequence(self, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        # These tests isolate forwarding; effect behavior is exercised below.
        args = {**args, "actions": [{**action, "expected_change": {"type": "file_exists", "path": "fixture"}}
                                    for action in args["actions"]]}
        calls: dict[str, list[dict[str, Any]]] = {}

        def recorder(name: str, command: str):
            def fake(payload: dict[str, Any]) -> dict[str, Any]:
                calls.setdefault(name, []).append(dict(payload))
                return helperctl.command_result(
                    "SUCCESS",
                    command,
                    "ok",
                    data={"foreground_before": {"hwnd": 1}, "foreground_after": {"hwnd": 2, "title": "After"}},
                )

            return fake

        with mock.patch.multiple(
            helperctl,
            cmd_input_click_point=mock.Mock(side_effect=recorder("click", "input.click_point")),
            cmd_input_type_text=mock.Mock(side_effect=recorder("type_text", "input.type_text")),
            cmd_input_paste_text=mock.Mock(side_effect=recorder("paste_text", "input.paste_text")),
            cmd_input_hotkey=mock.Mock(side_effect=recorder("hotkey", "input.hotkey")),
            cmd_observe_screenshot=mock.Mock(side_effect=recorder("screenshot", "observe.screenshot")),
            get_foreground_window_snapshot=mock.Mock(return_value={"hwnd": 2}),
            capture_effect_snapshot=mock.Mock(return_value={"exists": True}),
            wait_for_expected_change=mock.Mock(return_value={"matched": True, "reason": "forwarding fixture"}),
        ):
            result = helperctl.cmd_input_action_sequence(args)
        return result, calls

    def test_step_level_hwnd_and_allow_path_text_reach_commands(self) -> None:
        result, calls = self.run_sequence(
            {
                "actions": [
                    {"action": "click", "target": "target_window", "hwnd": 4242, "x": 5, "y": 6, "expected_result": "Save dialog opens"},
                    {"action": "type_text", "target": "current_dialog", "dialog_hwnd": 777, "title_contains": "Save", "text": "C:\\tmp\\a.txt", "allow_path_text": True},
                    {"action": "paste_text", "hwnd": 4242, "exact_title": "Untitled - Notepad", "text": "hello"},
                ]
            }
        )

        self.assertEqual(result["status"], "SUCCESS")
        click = calls["click"][0]
        self.assertEqual(click["hwnd"], 4242)
        self.assertEqual(click["target"], "target_window")
        self.assertEqual(click["title"], helperctl.TARGET_TITLE)
        self.assertEqual(click["coordinate_mode"], "screen")
        self.assertEqual(click["expected_result"], "Save dialog opens")
        self.assertNotIn("allow_path_text", click)
        self.assertNotIn("allow_vm_control", click)
        typed = calls["type_text"][0]
        self.assertTrue(typed["allow_path_text"])
        self.assertEqual(typed["dialog_hwnd"], 777)
        self.assertEqual(typed["title_contains"], "Save")
        self.assertNotIn("title", typed)
        pasted = calls["paste_text"][0]
        self.assertEqual(pasted["hwnd"], 4242)
        self.assertEqual(pasted["exact_title"], "Untitled - Notepad")
        self.assertEqual(pasted["target"], "desktop")
        self.assertNotIn("allow_path_text", pasted)
        step = result["data"]["results"][0]
        self.assertEqual(step["foreground_before"], {"hwnd": 1})
        self.assertEqual(step["foreground_after"], {"hwnd": 2, "title": "After"})

    def test_override_flags_are_never_defaulted_on_and_desktop_steps_get_no_target_title(self) -> None:
        _, calls = self.run_sequence({"actions": [{"action": "hotkey", "keys": "ctrl+s"}]})
        self.assertEqual(calls["hotkey"][0], {"target": "desktop", "keys": "ctrl+s"})

        _, calls = self.run_sequence(
            {
                "allow_vm_control": True,
                "title": "Sequence Window",
                "actions": [
                    {"action": "hotkey", "keys": "ctrl+s"},
                    {"action": "hotkey", "keys": "alt+f4", "allow_vm_control": False, "title_contains": "Step"},
                ],
            }
        )
        first, second = calls["hotkey"]
        self.assertTrue(first["allow_vm_control"])
        self.assertEqual(first["title"], "Sequence Window")
        self.assertFalse(second["allow_vm_control"])
        self.assertEqual(second["title_contains"], "Step")
        self.assertNotIn("title", second)
        self.assertNotIn("allow_path_text", first)


# ---------------------------------------------------------------- A6 planner request


class SequenceEffectTests(RouterRuntimeFixture):
    def setUp(self):
        super().setUp()
        self.foreground = mock.patch.object(helperctl, "get_foreground_window_snapshot", return_value={"hwnd": 1})
        self.foreground.start()
        self.addCleanup(self.foreground.stop)

    def test_unverified_input_stops_and_compact_verdict_matches_full(self):
        delivered = helperctl.command_result("SUCCESS", "input.hotkey", "accepted")
        with mock.patch.object(helperctl, "cmd_input_hotkey", return_value=delivered) as send:
            result = helperctl.cmd_input_action_sequence({"actions": [{"action": "hotkey", "keys": "alt+f4"}, {"action": "hotkey", "keys": "enter"}]})
        send.assert_called_once()
        self.assertEqual(result["status"], "NEEDS_PLANNER")
        self.assertEqual(result["outcome"], "needs_decision")
        self.assertEqual(result["data"]["attempted_count"], 1)
        self.assertEqual(result["data"]["requested_count"], 2)
        row = result["data"]["results"][0]
        self.assertEqual(row["result"]["status"], "SUCCESS")
        self.assertEqual(row["verification"]["verdict"], "unverified")
        minimal = helperctl.result_view(result)
        self.assertEqual(minimal["data"]["results"][0]["verification"], row["verification"])
        self.assertFalse(helperctl.action_result_succeeded("input.action_sequence", result))
        self.assertLess(len(json.dumps(minimal).encode()), 3000)

    def test_malformed_later_declaration_rejects_before_first_input(self):
        for effect in ({"type": "unknown"}, {"type": "window_close", "hwnd": "1"}):
            with self.subTest(effect=effect), mock.patch.object(helperctl, "cmd_input_hotkey") as send, mock.patch.object(helperctl, "ensure_window_foreground") as focus:
                result = helperctl.cmd_input_action_sequence({"focus_guard": True, "remembered_window": {"hwnd": 1}, "actions": [{"action": "hotkey", "keys": "a"}, {"action": "hotkey", "keys": "b", "expected_change": effect}]})
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["data"]["attempted_count"], 0)
                send.assert_not_called()
                focus.assert_not_called()

    def test_unsupported_sequence_preview_cannot_emit_input(self):
        for args in ({"preview": True, "actions": [{"action":"hotkey","keys":"a"}]}, {"actions": [{"action":"hotkey","keys":"a","preview":True}]}):
            with self.subTest(args=args), mock.patch.object(helperctl, "cmd_input_hotkey") as send:
                result = helperctl.cmd_input_action_sequence(args)
                self.assertEqual(result["status"], "ERROR")
                send.assert_not_called()

    def test_unchanged_alt_f4_is_uncertain_and_no_second_step_runs(self):
        effect = {"type": "window_close", "hwnd": 1, "max_wait_ms": 0}
        with mock.patch.object(helperctl, "capture_effect_snapshot", return_value={"alive": True}), mock.patch.object(helperctl, "cmd_input_hotkey", return_value=helperctl.command_result("SUCCESS", "input.hotkey", "accepted")) as send:
            result = helperctl.cmd_input_action_sequence({"actions": [{"action": "hotkey", "keys": "alt+f4", "expected_change": effect}, {"action": "hotkey", "keys": "enter"}]})
        send.assert_called_once()
        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertEqual(result["data"]["verification"]["verdict"], "unmet")
        self.assertEqual(result["data"]["stopping_index"], 0)

    def test_verified_steps_continue_and_snapshot_is_after_focus_guard(self):
        events=[]
        effect = {"type": "file_exists", "path": "fixture", "max_wait_ms": 0}
        with mock.patch.object(helperctl, "ensure_window_foreground", side_effect=lambda *a: events.append("focus") or {"status":"SUCCESS"}), mock.patch.object(helperctl, "capture_effect_snapshot", side_effect=lambda *a: events.append("snapshot") or {"exists":True}), mock.patch.object(helperctl, "cmd_input_hotkey", side_effect=lambda *a: events.append("input") or helperctl.command_result("SUCCESS", "input.hotkey", "accepted")) as send:
            result = helperctl.cmd_input_action_sequence({"focus_guard": True, "remembered_window": {"hwnd": 1}, "actions": [{"action": "hotkey", "keys": "a", "expected_change": effect}, {"action": "hotkey", "keys": "b", "expected_change": effect}]})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(events[:3], ["focus", "snapshot", "input"])
        self.assertEqual(result["data"]["attempted_count"], 2)
        self.assertTrue(helperctl.action_result_succeeded("input.action_sequence", result))

    def test_needs_planner_and_cancelled_steps_cannot_authorize_later_input(self):
        with mock.patch.object(helperctl, "cmd_input_hotkey", return_value=helperctl.command_result("NEEDS_PLANNER", "input.hotkey", "inspect")) as send:
            result = helperctl.cmd_input_action_sequence({"actions": [{"action":"hotkey","keys":"a"},{"action":"hotkey","keys":"b"}]})
        send.assert_called_once()
        self.assertEqual(result["status"], "NEEDS_PLANNER")
        token = helperctl.CommandCancellationToken()
        def cancel(args):
            token.cancel("fixture")
            return helperctl.command_result("SUCCESS", "input.hotkey", "accepted")
        with helperctl.command_cancellation(token), mock.patch.object(helperctl, "capture_effect_snapshot", return_value={"exists":True}), mock.patch.object(helperctl, "cmd_input_hotkey", side_effect=cancel) as send:
            cancelled = helperctl.cmd_input_action_sequence({"actions": [{"action":"hotkey","keys":"a","expected_change":{"type":"file_exists","path":"fixture"}},{"action":"hotkey","keys":"b"}]})
        send.assert_called_once()
        self.assertEqual(cancelled["status"], "UNCERTAIN")
        self.assertEqual(cancelled["data"]["failure_type"], "HELPER_COMMAND_CANCELLED")
        self.assertEqual(cancelled["data"]["attempted_count"], 1)
        self.assertEqual(cancelled["data"]["requested_count"], 2)
        self.assertEqual(cancelled["data"]["results"][0]["result"]["status"], "SUCCESS")
        self.assertTrue(cancelled["data"]["delivery_uncertain"])

    def test_wait_and_screenshot_complete_without_input_effect_and_keep_prior_failure(self):
        self.seed_memory({"last_failed_action": {"command": "input.hotkey", "status": "UNCERTAIN"}})
        with mock.patch.object(helperctl, "cmd_observe_screenshot", return_value=helperctl.command_result("SUCCESS", "observe.screenshot", "captured")):
            result = helperctl.cmd_input_action_sequence({"actions": [{"action":"wait","seconds":0},{"action":"screenshot","request_id":"FIXTURE"}]})
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["data"]["attempted_count"], 2)
        helperctl.update_planner_working_memory("input.action_sequence", {}, result)
        self.assertIn("last_failed_action", self.read_memory())


class PlannerRequestDefaultsTests(RouterRuntimeFixture):
    def test_failure_type_defaults_to_none_and_candidate_commands_are_allowed(self) -> None:
        request = helperctl.create_planner_request({"request_id": "REQ-A6-1", "active_context": dict(FAKE_CONTEXT)}, {}, failed_command="planner.fast_observe")

        self.assertEqual(request["failure_type"], "NONE")
        for command in (
            "input.click_candidate",
            "input.invoke_candidate",
            "input.set_candidate_value",
            "input.expand_candidate",
            "input.select_candidate",
            "planner.step",
        ):
            self.assertIn(command, request["allowed_recovery_commands"])
        self.assertEqual(request["planner_recovery_hint"], helperctl.GENERIC_PLANNER_RECOVERY_HINT)
        self.assertNotIn("save dialogs", request["planner_recovery_hint"].lower())
        self.assertTrue(Path(request["planner_request_path"]).exists())

    def test_explicit_failure_type_is_kept(self) -> None:
        request = helperctl.create_planner_request({"request_id": "REQ-A6-2"}, {"failure_type": "WRONG_STATE"}, failed_command="verify.window_state")
        self.assertEqual(request["failure_type"], "WRONG_STATE")

    def test_save_dialog_hint_only_when_save_or_open_dialog_is_active(self) -> None:
        save_context = {"dialog_open": True, "current_dialog": {"title": "Save As", "hwnd": 9}, "active_window": {"title": "Save As"}}
        request = helperctl.create_planner_request({"request_id": "REQ-A6-3", "active_context": save_context}, {}, failed_command="planner.step")
        self.assertEqual(request["planner_recovery_hint"], helperctl.SAVE_DIALOG_PLANNER_RECOVERY_HINT)

        closed_context = {"dialog_open": False, "active_window": {"title": "Save the date - Mail"}}
        request = helperctl.create_planner_request({"request_id": "REQ-A6-4", "active_context": closed_context}, {}, failed_command="planner.step")
        self.assertEqual(request["planner_recovery_hint"], helperctl.GENERIC_PLANNER_RECOVERY_HINT)


# ---------------------------------------------------------------- A7 working memory


class WorkingMemoryTests(RouterRuntimeFixture):
    def test_read_only_commands_do_not_rewrite_memory(self) -> None:
        original = self.seed_memory(
            {
                "last_action": {"command": "input.click_point", "status": "SUCCESS"},
                "last_observation_request_id": "REQ-KEEP",
                "expected_next_state": "Dialog opens",
            }
        )
        for command in ("helper.bridge_status", "observe.windows", "status.current", "verify.window_state"):
            memory = helperctl.update_planner_working_memory(
                command,
                {"request_id": "REQ-ARG", "expected_result": "something else"},
                helperctl.command_result("UNCERTAIN", command, "probe", data={"request_id": "REQ-ARG"}),
            )
            self.assertEqual(memory["last_observation_request_id"], "REQ-KEEP")

        self.assertEqual(self.memory_file.read_text(encoding="utf-8"), original)

    def test_read_only_command_records_only_a_new_observation(self) -> None:
        self.seed_memory({"last_action": {"command": "input.click_point"}, "last_observation_request_id": "REQ-OLD"})
        helperctl.update_planner_working_memory(
            "verify.window_state",
            {},
            helperctl.command_result("NEEDS_PLANNER", "verify.window_state", "not open"),
            new_observation_ids=["REQ-VERIFY-CAPTURE"],
        )
        memory = self.read_memory()
        self.assertEqual(memory["last_observation_request_id"], "REQ-VERIFY-CAPTURE")
        self.assertEqual(memory["last_action"], {"command": "input.click_point"})
        self.assertNotIn("last_failed_action", memory)

    def test_observation_id_comes_from_new_planner_request_not_args(self) -> None:
        self.seed_memory({"last_observation_request_id": "REQ-PREVIOUS"})

        def stale_echo(args: dict[str, Any]) -> dict[str, Any]:
            # Mirrors a STALE_SCREENSHOT result that echoes the old request.
            return helperctl.command_result(
                "UNCERTAIN",
                "recovery.guided_grid_click",
                "stale",
                data={"request_id": args["request_id"], "planner_request": {"request_id": args["request_id"]}},
            )

        def fresh_follow_up(args: dict[str, Any]) -> dict[str, Any]:
            helperctl.write_planner_request({"request_id": "REQ-FOLLOW-UP", "created_at": helperctl.now_iso()})
            return helperctl.command_result(
                "SUCCESS",
                "recovery.guided_grid_click",
                "clicked",
                data={"request_id": args["request_id"], "next_observation": {"request_id": "REQ-FOLLOW-UP"}},
            )

        with mock.patch.multiple(helperctl, write_command_report=mock.Mock(return_value="r"), write_command_failure_bundle=mock.Mock(return_value="b")):
            with mock.patch.object(helperctl, "cmd_recovery_guided_grid_click", side_effect=stale_echo):
                helperctl.dispatch("recovery.guided_grid_click", {"request_id": "REQ-ARG-OLD"})
            self.assertEqual(self.read_memory()["last_observation_request_id"], "REQ-PREVIOUS")

            with mock.patch.object(helperctl, "cmd_recovery_guided_grid_click", side_effect=fresh_follow_up):
                helperctl.dispatch("recovery.guided_grid_click", {"request_id": "REQ-ARG-OLD"})
            self.assertEqual(self.read_memory()["last_observation_request_id"], "REQ-FOLLOW-UP")

    def test_capture_grid_through_dispatch_records_its_new_request(self) -> None:
        def fake_capture(args: dict[str, Any]) -> dict[str, Any]:
            request = helperctl.create_planner_request({"request_id": "REQ-GRID-NEW", "active_context": dict(FAKE_CONTEXT)}, args, failed_command="unknown")
            return helperctl.command_result("NEEDS_PLANNER", "recovery.capture_grid", "grid", data={"request_id": "REQ-GRID-NEW", "planner_request": request})

        with mock.patch.object(helperctl, "cmd_recovery_capture_grid", side_effect=fake_capture):
            result = helperctl.dispatch("recovery.capture_grid", {"target": "desktop"})

        memory = self.read_memory()
        self.assertEqual(memory["last_observation_request_id"], "REQ-GRID-NEW")
        self.assertEqual(result["data"]["working_memory"]["last_observation_request_id"], "REQ-GRID-NEW")
        self.assertNotIn("last_failed_action", memory)

    def test_later_action_success_clears_last_failed_action(self) -> None:
        self.seed_memory({})
        helperctl.update_planner_working_memory("input.click_point", {}, helperctl.command_result("UNCERTAIN", "input.click_point", "unverified"))
        self.assertEqual(self.read_memory()["last_failed_action"]["command"], "input.click_point")

        helperctl.update_planner_working_memory("observe.screenshot", {}, helperctl.command_result("SUCCESS", "observe.screenshot", "ok"))
        self.assertIn("last_failed_action", self.read_memory())
        helperctl.update_planner_working_memory("planner.fast_observe", {}, helperctl.command_result("NEEDS_PLANNER", "planner.fast_observe", "look"))
        self.assertEqual(self.read_memory()["last_failed_action"]["command"], "input.click_point")

        helperctl.update_planner_working_memory("input.click_point", {}, helperctl.command_result("SUCCESS", "input.click_point", "clicked"))
        memory = self.read_memory()
        self.assertNotIn("last_failed_action", memory)
        self.assertEqual(memory["last_successful_action"]["command"], "input.click_point")

    def test_only_verified_planner_effect_clears_last_failed_action(self) -> None:
        self.seed_memory({"last_failed_action": {"command": "input.hotkey", "status": "FAILED"}})
        helperctl.update_planner_working_memory(
            "planner.step",
            {},
            helperctl.command_result(
                "NEEDS_PLANNER",
                "planner.step",
                "stepped",
                data={"action_result": {"status": "SUCCESS"}, "next_observation": {"request_id": "REQ-X"}},
            ),
        )
        memory = self.read_memory()
        self.assertIn("last_failed_action", memory)
        verified = helperctl.command_result("NEEDS_PLANNER", "planner.step", "verified", data={
            "action_result": {"status": "SUCCESS"}, "verification": {"verdict": "verified", "type": "window_close"}})
        helperctl.update_planner_working_memory("planner.step", {}, verified)
        memory = self.read_memory()
        self.assertNotIn("last_failed_action", memory)
        self.assertEqual(memory["last_successful_action"]["command"], "planner.step")


if __name__ == "__main__":
    unittest.main()
