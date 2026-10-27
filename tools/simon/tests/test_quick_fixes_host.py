"""Deterministic host-side coverage for the 2026-10-01 quick fixes (B1 final stop status, B2 capture tally).

No live desktop: Tk, timers, process exit, window lookup and screen capture are all mocked.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import agent_helper
from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime

import helperctl
import simon_common


class QuickFixHostFixture(RuntimeGuardedTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runtime_dir = Path(self.temp_dir.name) / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.path_values = redirect_runtime(self, self.runtime_dir)
        log_patcher = mock.patch.object(agent_helper, "HELPER_LOG_FILE", self.runtime_dir / "agent_helper_log.txt")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)
        # The real force-exit timer would call os._exit(0) two seconds later; never start it in tests.
        timer_patcher = mock.patch.object(agent_helper.threading, "Timer")
        self.timer_cls = timer_patcher.start()
        self.addCleanup(timer_patcher.stop)

    def make_app(self) -> agent_helper.AgentHelperApp:
        app = object.__new__(agent_helper.AgentHelperApp)
        app.root = mock.Mock()
        app.shutdown_started = False
        app.shutdown_reason = ""
        app.shutdown_event = ""
        app.shutdown_requested_at = ""
        app.final_status_written = False
        app.last_capture_route_thread = None
        app.external_command_lock = threading.Lock()
        app.active_external_job = None
        app.external_command_generation = 0
        app.last_external_command_id = ""
        app.bridge_instance_id = "test-instance"
        app.last_bridge_heartbeat = -999.0
        app.helper_temporarily_hidden = False
        app.debug_window_visible = False
        app.overlay_window = None
        app.overlay_canvas = None
        app.overlay_text_id = None
        app.escape_poll_after_id = None
        app.external_poll_after_id = None
        app.hide_debug_after_id = None
        app.idle_poll_after_id = None
        app.frozen = False
        app.freeze_count = 0
        app.run_result = ""
        app._mark_activity = mock.Mock()
        app._log = mock.Mock()
        app._set_state = mock.Mock()
        app._hide_helper_for_target_view = mock.Mock()
        app._bring_helper_to_front = mock.Mock()
        return app

    def bridge_status(self) -> dict[str, Any]:
        status = simon_common.read_json(self.path_values["HELPER_BRIDGE_STATUS_FILE"])
        self.assertIsInstance(status, dict)
        return status

    def bridge_history(self) -> list[dict[str, Any]]:
        path = self.path_values["HELPER_BRIDGE_HISTORY_FILE"]
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def run_tk_shutdown_callback(self, app: agent_helper.AgentHelperApp) -> None:
        app.root.after.assert_called()
        delay_ms, callback = app.root.after.call_args.args[:2]
        self.assertIsInstance(delay_ms, int)
        callback()

    def force_exit_callback(self) -> Any:
        self.timer_cls.assert_called_once()
        delay_seconds, callback = self.timer_cls.call_args.args[:2]
        self.assertEqual(delay_seconds, 2.0)
        return callback

    def assert_terminal_stopped(self, event: str, *, forced: bool = False) -> dict[str, Any]:
        status = self.bridge_status()
        self.assertEqual(status["state"], "stopped")
        data = status["data"]
        self.assertEqual(data["stop_event"], event)
        self.assertEqual(data["event"], event)
        self.assertTrue(str(data["stop_reason"]).strip())
        self.assertEqual(data["note"], data["stop_reason"])
        self.assertTrue(str(data["stopped_at"]).strip())
        self.assertGreater(float(data["stopped_epoch"]), 0.0)
        self.assertIs(data["forced_exit"], forced)
        self.assertEqual(data["helper_pid"], os.getpid())
        self.assertEqual(data["instance_id"], "test-instance")
        stopped_events = [entry for entry in self.bridge_history() if entry["event"] == "helper_stopped"]
        self.assertEqual(len(stopped_events), 1)
        return data


class FinalStoppedStatusTests(QuickFixHostFixture):
    def record_states(self) -> tuple[mock._patch, list[str]]:
        states: list[str] = []
        real_write = helperctl.write_bridge_status

        def recording_write(state: str, data: dict[str, Any] | None = None) -> Any:
            states.append(state)
            return real_write(state, data)

        return mock.patch.object(helperctl, "write_bridge_status", side_effect=recording_write), states

    def test_helper_exit_goes_stopping_then_stopped_without_idle_or_premature_stopped(self) -> None:
        app = self.make_app()
        job = agent_helper.ExternalCommandJob(
            request={},
            request_id="REQ-EXIT",
            command="helper.exit",
            args={},
            created_epoch=time.time(),
            started_epoch=time.time(),
            timeout_seconds=5.0,
            generation=1,
            token=helperctl.CommandCancellationToken(),
            result=helperctl.command_result("SUCCESS", "helper.exit", "exit", data={"helper_should_exit": True}),
            completed_epoch=time.time(),
        )
        app.external_command_generation = 1
        app.active_external_job = job
        patcher, states = self.record_states()
        with patcher:
            app._complete_external_job(job)
            self.assertEqual(self.bridge_status()["state"], "stopping")
            self.assertEqual(self.bridge_status()["data"]["event"], "helper_exit")
            self.run_tk_shutdown_callback(app)

        self.assertEqual(states, ["stopping", "stopped"])
        data = self.assert_terminal_stopped("helper_exit")
        self.assertEqual(data["stop_reason"], "helper.exit requested.")
        self.assertTrue(str(data["stopping_at"]).strip())
        app.root.quit.assert_called_once()
        app.root.destroy.assert_called_once()
        events = [entry["event"] for entry in self.bridge_history()]
        self.assertIn("helper_exit_requested", events)
        self.assertLess(events.index("helper_exit_requested"), events.index("helper_stopped"))
        result = simon_common.read_json(self.path_values["HELPER_RESULT_FILE"])
        self.assertEqual(result["request_id"], "REQ-EXIT")
        self.assertEqual(result["result"]["status"], "SUCCESS")

    def test_non_exit_completion_still_reports_idle(self) -> None:
        app = self.make_app()
        job = agent_helper.ExternalCommandJob(
            request={},
            request_id="REQ-OK",
            command="helper.commands",
            args={},
            created_epoch=time.time(),
            started_epoch=time.time(),
            timeout_seconds=5.0,
            generation=1,
            token=helperctl.CommandCancellationToken(),
            result=helperctl.command_result("SUCCESS", "helper.commands", "ok"),
            completed_epoch=time.time(),
        )
        app.external_command_generation = 1
        app.active_external_job = job

        app._complete_external_job(job)

        self.assertEqual(self.bridge_status()["state"], "idle")
        self.assertFalse(app.shutdown_started)
        app.root.after.assert_not_called()

    def test_escape_emergency_stop_ends_in_stopped(self) -> None:
        app = self.make_app()
        patcher, states = self.record_states()
        with patcher:
            app._emergency_stop("Escape key pressed.")
            self.assertEqual(self.bridge_status()["state"], "stopping")
            self.run_tk_shutdown_callback(app)

        self.assertEqual(states, ["stopping", "stopped"])
        data = self.assert_terminal_stopped("emergency_stop")
        self.assertEqual(data["stop_reason"], "Escape key pressed.")
        self.assertIn("helper_emergency_stopped", [entry["event"] for entry in self.bridge_history()])

    def test_idle_auto_exit_ends_in_stopped(self) -> None:
        app = self.make_app()
        app.last_agent_activity_at = time.monotonic() - 120.0
        app.last_local_activity_at = time.monotonic() - 120.0
        with mock.patch.object(agent_helper, "AI_IDLE_EXIT_SECONDS", 30.0):
            app._poll_idle_auto_exit()
            self.run_tk_shutdown_callback(app)

        data = self.assert_terminal_stopped("idle_auto_exit")
        self.assertIn("auto-exiting", data["stop_reason"])

    def test_watchdog_timeout_shutdown_ends_in_stopped(self) -> None:
        app = self.make_app()
        job = agent_helper.ExternalCommandJob(
            request={},
            request_id="REQ-TIMEOUT",
            command="status.current",
            args={},
            created_epoch=time.time() - 10.0,
            started_epoch=time.time() - 10.0,
            timeout_seconds=0.1,
            generation=1,
            token=helperctl.CommandCancellationToken(),
        )
        app.external_command_generation = 1
        app.active_external_job = job
        patcher, states = self.record_states()
        with patcher:
            app._timeout_external_job(job)
            self.run_tk_shutdown_callback(app)

        self.assertEqual(states, ["cancelling", "stopping", "stopped"])
        data = self.assert_terminal_stopped("external_command_timeout")
        self.assertIn("timeout", data["stop_reason"])
        self.assertTrue(job.token.is_cancelled())

    def test_window_close_ends_in_stopped(self) -> None:
        app = self.make_app()
        app._request_shutdown("Debug window close requested.", "window_close")
        self.assertEqual(app.root.after.call_args.args[0], 0)
        self.run_tk_shutdown_callback(app)

        data = self.assert_terminal_stopped("window_close")
        self.assertEqual(data["stop_reason"], "Debug window close requested.")

    def test_force_exit_fallback_writes_forced_stopped_when_tk_callback_never_runs(self) -> None:
        app = self.make_app()
        app._request_shutdown("helper.exit requested.", "helper_exit", delay_ms=50)
        force_exit = self.force_exit_callback()
        with mock.patch.object(agent_helper.os, "_exit") as exit_mock, mock.patch.object(agent_helper, "release_instance_lock") as release_mock:
            force_exit()

        exit_mock.assert_called_once_with(0)
        release_mock.assert_called_once()
        self.assert_terminal_stopped("helper_exit", forced=True)

    def test_force_exit_after_clean_shutdown_does_not_overwrite_terminal_status(self) -> None:
        app = self.make_app()
        app._request_shutdown("Escape key pressed.", "emergency_stop", delay_ms=50)
        self.run_tk_shutdown_callback(app)
        first = self.assert_terminal_stopped("emergency_stop")
        force_exit = self.force_exit_callback()
        with mock.patch.object(agent_helper.os, "_exit"), mock.patch.object(agent_helper, "release_instance_lock"):
            force_exit()

        second = self.assert_terminal_stopped("emergency_stop", forced=False)
        self.assertEqual(first["stopped_at"], second["stopped_at"])

    def test_failed_status_writes_never_block_shutdown_and_force_exit_retries(self) -> None:
        app = self.make_app()
        with mock.patch.object(helperctl, "write_bridge_status", side_effect=OSError("locked by sync client")):
            app._request_shutdown("helper.exit requested.", "helper_exit", delay_ms=50)
            app.root.after.assert_called_once()
            self.timer_cls.return_value.start.assert_called_once()
            self.run_tk_shutdown_callback(app)

        app.root.quit.assert_called_once()
        app.root.destroy.assert_called_once()
        self.assertFalse(app.final_status_written)
        self.assertFalse(self.path_values["HELPER_BRIDGE_STATUS_FILE"].exists())
        force_exit = self.force_exit_callback()
        with mock.patch.object(agent_helper.os, "_exit") as exit_mock, mock.patch.object(agent_helper, "release_instance_lock"):
            force_exit()

        exit_mock.assert_called_once_with(0)
        self.assert_terminal_stopped("helper_exit", forced=True)

    def test_log_failure_during_shutdown_still_destroys_root(self) -> None:
        app = self.make_app()
        app._request_shutdown("Escape key pressed.", "emergency_stop", delay_ms=50)
        with mock.patch.object(agent_helper, "append_log", side_effect=OSError("log locked")):
            self.run_tk_shutdown_callback(app)

        app.root.destroy.assert_called_once()
        self.assert_terminal_stopped("emergency_stop")

    def test_final_status_write_gives_up_quickly_when_lock_is_held(self) -> None:
        app = self.make_app()
        self.assertTrue(agent_helper.FINAL_STATUS_LOCK.acquire(timeout=1.0))
        try:
            started = time.monotonic()
            written = app._write_final_stopped_status(fallback_reason="unit", fallback_event="unit")
            elapsed = time.monotonic() - started
        finally:
            agent_helper.FINAL_STATUS_LOCK.release()

        self.assertFalse(written)
        self.assertLess(elapsed, agent_helper.FINAL_STATUS_LOCK_TIMEOUT_SECONDS + 0.5)
        self.assertFalse(self.path_values["HELPER_BRIDGE_STATUS_FILE"].exists())

    def test_mainloop_fallback_reason_is_used_when_no_shutdown_was_recorded(self) -> None:
        app = self.make_app()
        self.assertTrue(
            app._write_final_stopped_status(
                fallback_reason="Tk mainloop returned without a recorded shutdown.",
                fallback_event="mainloop_returned",
            )
        )
        data = self.assert_terminal_stopped("mainloop_returned")
        self.assertIsNone(data["stopping_at"])


class FakeImage:
    width = 400
    height = 300

    def save(self, path: Any) -> None:
        Path(path).write_bytes(b"fake-png")

    def copy(self) -> "FakeImage":
        return self


class CaptureRouteTallyTests(QuickFixHostFixture):
    def setUp(self) -> None:
        super().setUp()
        self.window = agent_helper.WindowInfo(hwnd=4242, title="Simon Target", left=10, top=20, right=410, bottom=320, minimized=False)
        patches = [
            mock.patch.object(agent_helper, "FOCUS_SETTLE_SECONDS", 0.0),
            mock.patch.object(agent_helper, "find_window_by_title", return_value=self.window),
            mock.patch.object(helperctl, "find_best_window_for_args", return_value=self.window),
            mock.patch.object(helperctl, "get_foreground_window_snapshot", return_value={"hwnd": 4242}),
            mock.patch.object(helperctl, "capture_image_transform", return_value={"verified": False}),
            mock.patch.object(helperctl, "capture_fallback_for_window", return_value=(None, {"method": "capture_failed", "attempts": []})),
            mock.patch.object(helperctl, "render_planner_grid", side_effect=lambda image, _c, _r: (image, {})),
            mock.patch.object(helperctl, "build_active_context_packet_for_capture", return_value={}),
            mock.patch.object(agent_helper, "bring_window_to_front", return_value=True),
            mock.patch.object(helperctl, "bring_window_to_front", return_value=True),
            mock.patch.object(agent_helper, "get_foreground_window_snapshot", return_value={"hwnd": 4242}),
            mock.patch.object(helperctl, "monitor_for_window", return_value=None),
            mock.patch.object(helperctl, "monitor_payload", return_value=None),
            mock.patch.object(helperctl, "window_payload", return_value={"hwnd": 4242}),
            mock.patch.object(helperctl, "build_active_context_packet", return_value={}),
            mock.patch.object(helperctl, "build_action_menu", return_value=[]),
            mock.patch.object(helperctl, "build_planner_coaching", return_value={}),
            mock.patch.object(helperctl, "load_planner_working_memory", return_value={}),
            mock.patch.object(helperctl, "save_candidate_overlay", return_value=None),
            mock.patch.object(
                helperctl,
                "create_planner_request",
                return_value={"planner_request_path": str(self.runtime_dir / "planner_requests" / "x.json")},
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def capture(self, app: agent_helper.AgentHelperApp, **args: Any) -> dict[str, Any]:
        payload = {"target": "target_window", "title": "Simon Target", "request_id": "REQ-CAP"}
        payload.update(args)
        result = app._external_capture_grid(payload, ui_prepared=True)
        if app.last_capture_route_thread is not None:
            app.last_capture_route_thread.join(2.0)
            self.assertFalse(app.last_capture_route_thread.is_alive())
        return result

    def tally(self) -> dict[str, Any]:
        tally = simon_common.read_json(self.path_values["HELPER_CAPTURE_ROUTE_TALLY_FILE"])
        self.assertIsInstance(tally, dict)
        return tally

    def route_history(self) -> list[dict[str, Any]]:
        path = self.path_values["HELPER_CAPTURE_ROUTE_HISTORY_FILE"]
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_window_capture_success_records_route_actually_used(self) -> None:
        app = self.make_app()
        meta = {
            "method": "window_pillow_bbox",
            "attempts": [
                {"method": "window_pillow_all_screens", "status": "FAILED", "error": "x"},
                {"method": "window_pillow_bbox", "status": "SUCCESS"},
            ],
        }
        with mock.patch.object(helperctl, "capture_window_image_with_details", return_value=(FakeImage(), meta)):
            result = self.capture(app)

        self.assertEqual(result["status"], "NEEDS_PLANNER")
        self.assertEqual(result["data"]["capture_method"], "window_pillow_bbox")
        self.assertEqual(result["data"]["capture_route"]["target"], "target_window")
        self.assertEqual(len(result["data"]["capture_route"]["attempts"]), 2)
        tally = self.tally()
        self.assertEqual(tally["schema"], "capture-route-tally.v1")
        self.assertEqual(tally["totals"]["captures"], 1)
        self.assertEqual(tally["totals"]["success"], 1)
        self.assertEqual(tally["by_method"]["window_pillow_bbox"]["success"], 1)
        self.assertEqual(tally["by_target"]["target_window"]["last_method"], "window_pillow_bbox")
        history = self.route_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "SUCCESS")
        self.assertEqual(len(history[0]["attempts"]), 2)

    def test_desktop_capture_success_records_desktop_route(self) -> None:
        app = self.make_app()
        meta = {"method": "desktop_pyautogui", "attempts": [{"method": "desktop_pyautogui", "status": "SUCCESS"}]}
        with mock.patch.object(helperctl, "capture_desktop_image_with_details", return_value=(FakeImage(), meta)), mock.patch.object(
            helperctl, "get_virtual_screen_origin", return_value=(0, 0)
        ):
            result = self.capture(app, target="desktop")

        self.assertEqual(result["status"], "NEEDS_PLANNER")
        tally = self.tally()
        self.assertEqual(tally["by_method"]["desktop_pyautogui"]["success"], 1)
        self.assertEqual(tally["by_target"]["desktop"]["count"], 1)

    def test_capture_returning_no_image_records_capture_failed(self) -> None:
        app = self.make_app()
        meta = {
            "method": "capture_failed",
            "attempts": [{"method": "window_gdi_region", "status": "FAILED", "error": "gdi failed"}],
            "error": "gdi failed: access denied",
        }
        with mock.patch.object(helperctl, "capture_window_image_with_details", return_value=(None, meta)):
            result = self.capture(app)

        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertIn("gdi failed: access denied", result["message"])
        tally = self.tally()
        self.assertEqual(tally["totals"]["uncertain"], 1)
        self.assertEqual(tally["by_method"]["capture_failed"]["uncertain"], 1)
        history = self.route_history()
        self.assertEqual(history[0]["status"], "UNCERTAIN")
        self.assertEqual(history[0]["attempts"][0]["method"], "window_gdi_region")

    def test_capture_exception_records_capture_failed_with_error_attempt(self) -> None:
        app = self.make_app()
        with mock.patch.object(helperctl, "capture_window_image_with_details", side_effect=RuntimeError("PrintWindow exploded")):
            result = self.capture(app)

        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertIn("PrintWindow exploded", result["message"])
        history = self.route_history()
        self.assertEqual(history[0]["method"], "capture_failed")
        self.assertEqual(history[0]["attempts"][-1]["method"], "capture_exception")
        self.assertIn("PrintWindow exploded", history[0]["attempts"][-1]["error"])

    def test_window_not_found_is_not_a_capture_route_outcome(self) -> None:
        app = self.make_app()
        with mock.patch.object(helperctl, "find_best_window_for_args", return_value=None), mock.patch.object(
            helperctl, "capture_window_image_with_details"
        ) as capture_mock:
            result = self.capture(app)

        self.assertEqual(result["status"], "UNCERTAIN")
        capture_mock.assert_not_called()
        self.assertIsNone(app.last_capture_route_thread)
        self.assertFalse(self.path_values["HELPER_CAPTURE_ROUTE_TALLY_FILE"].exists())

    def test_tally_failure_never_fails_the_capture(self) -> None:
        app = self.make_app()
        meta = {"method": "window_pillow_all_screens", "attempts": []}
        with mock.patch.object(helperctl, "capture_window_image_with_details", return_value=(FakeImage(), meta)), mock.patch.object(
            helperctl, "record_capture_route", side_effect=RuntimeError("tally exploded")
        ) as record_mock:
            result = self.capture(app)

        self.assertEqual(result["status"], "NEEDS_PLANNER")
        record_mock.assert_called_once()
        self.assertIn("tally exploded", (self.runtime_dir / "agent_helper_log.txt").read_text(encoding="utf-8"))

    def test_slow_tally_never_slows_the_capture(self) -> None:
        app = self.make_app()
        meta = {"method": "window_pillow_all_screens", "attempts": []}
        release = threading.Event()
        recorded: list[tuple[Any, ...]] = []

        def slow_record(method: str, target: str, status: str, started: float, attempts: Any = None) -> None:
            release.wait(5.0)
            recorded.append((method, target, status, round((time.monotonic() - started) * 1000.0, 1)))

        with mock.patch.object(helperctl, "capture_window_image_with_details", return_value=(FakeImage(), meta)), mock.patch.object(
            helperctl, "record_capture_route", side_effect=slow_record
        ):
            started = time.monotonic()
            result = app._external_capture_grid(
                {"target": "target_window", "title": "Simon Target", "request_id": "REQ-SLOW"},
                ui_prepared=True,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result["status"], "NEEDS_PLANNER")
            self.assertLess(elapsed, 1.0)
            self.assertTrue(app.last_capture_route_thread.is_alive())
            release.set()
            app.last_capture_route_thread.join(2.0)

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][:3], ("window_pillow_all_screens", "target_window", "SUCCESS"))

    def test_capture_grid_through_worker_path_records_tally(self) -> None:
        app = self.make_app()
        meta = {"method": "window_pillow_all_screens", "attempts": [{"method": "window_pillow_all_screens", "status": "SUCCESS"}]}
        job = agent_helper.ExternalCommandJob(
            request={},
            request_id="REQ-WORKER",
            command="recovery.capture_grid",
            args={"target": "target_window", "title": "Simon Target", "request_id": "REQ-WORKER"},
            created_epoch=time.time(),
            started_epoch=time.time(),
            timeout_seconds=5.0,
            generation=1,
            token=helperctl.CommandCancellationToken(),
        )
        with mock.patch.object(helperctl, "capture_window_image_with_details", return_value=(FakeImage(), meta)):
            with helperctl.command_cancellation(job.token):
                result, preserve_foreground = app._execute_external_command_in_worker(job)
            app.last_capture_route_thread.join(2.0)

        self.assertEqual(result["status"], "NEEDS_PLANNER")
        self.assertFalse(preserve_foreground)
        self.assertEqual(self.tally()["totals"]["success"], 1)


if __name__ == "__main__":
    unittest.main()
