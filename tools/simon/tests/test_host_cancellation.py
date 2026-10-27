from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_helper
from tests.runtime_support import RuntimeGuardedTestCase, redirect_runtime

import helperctl
import simon_common


class HostCancellationFixture(RuntimeGuardedTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.runtime_dir = Path(self.temp_dir.name) / "runtime"
        self.runtime_dir.mkdir(parents=True)
        self.path_values = redirect_runtime(self, self.runtime_dir)

    def make_app(self) -> agent_helper.AgentHelperApp:
        app = object.__new__(agent_helper.AgentHelperApp)
        app.shutdown_started = False
        app.external_command_lock = threading.Lock()
        app.active_external_job = None
        app.external_command_generation = 0
        app.last_external_command_id = ""
        app.bridge_instance_id = "test-instance"
        app.last_bridge_heartbeat = -999.0
        app.helper_temporarily_hidden = False
        app.debug_window_visible = False
        app._mark_activity = mock.Mock()
        app._log = mock.Mock()
        app._hide_helper_for_target_view = mock.Mock()
        app._bring_helper_to_front = mock.Mock()
        app._request_shutdown = mock.Mock()
        return app

    def bridge_history(self) -> list[dict[str, object]]:
        path = self.path_values["HELPER_BRIDGE_HISTORY_FILE"]
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class RouterCancellationTests(HostCancellationFixture):
    def test_cancellable_sleep_raises_when_token_is_cancelled(self) -> None:
        token = helperctl.CommandCancellationToken()
        token.cancel("unit test stop")

        started = time.monotonic()
        with helperctl.command_cancellation(token):
            with self.assertRaises(helperctl.CommandCancelled):
                helperctl.cancellable_sleep(5.0)

        self.assertLess(time.monotonic() - started, 0.1)

    def test_dispatch_returns_uncertain_when_cancelled_before_handler(self) -> None:
        token = helperctl.CommandCancellationToken()
        token.cancel("cancel before dispatch")

        with helperctl.command_cancellation(token):
            result = helperctl.dispatch("helper.commands", {})

        self.assertEqual(result["status"], "UNCERTAIN")
        self.assertEqual(result["data"]["failure_type"], "HELPER_COMMAND_CANCELLED")
        self.assertEqual(result["data"]["cancel_reason"], "cancel before dispatch")
        self.assertEqual(result["next_allowed_actions"], ["helper.bridge_status"])


class HostWorkerCancellationTests(HostCancellationFixture):
    def slow_dispatch(self, entered: threading.Event) -> mock.Mock:
        def run(command: str, _args: dict[str, object]) -> dict[str, object]:
            entered.set()
            while True:
                helperctl.cancellable_sleep(0.02)

        return mock.Mock(side_effect=run)

    def test_worker_start_returns_while_command_is_in_flight_and_heartbeat_stays_executing(self) -> None:
        app = self.make_app()
        entered = threading.Event()
        request = {
            "request_id": "REQ-UNIT-1",
            "command": "status.current",
            "args": {},
            "created_epoch": time.time(),
            "timeout_seconds": 5.0,
        }

        with mock.patch.object(helperctl, "dispatch", self.slow_dispatch(entered)):
            started = time.monotonic()
            job = app._start_external_command(request)

            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(entered.wait(1.0))
            self.assertIs(app.active_external_job, job)
            self.assertTrue(job.thread and job.thread.is_alive())

            app._write_bridge_heartbeat()
            status = simon_common.read_json(self.path_values["HELPER_BRIDGE_STATUS_FILE"])
            self.assertEqual(status["state"], "executing")
            self.assertEqual(status["data"]["request_id"], "REQ-UNIT-1")
            self.assertTrue(status["data"]["worker_alive"])

            app._cancel_active_external_command("unit stop", "unit_test")
            job.thread.join(1.0)
            app._poll_active_external_job()

        result = simon_common.read_json(self.path_values["HELPER_RESULT_FILE"])
        self.assertEqual(result["request_id"], "REQ-UNIT-1")
        self.assertEqual(result["result"]["status"], "UNCERTAIN")
        self.assertEqual(result["result"]["data"]["failure_type"], "HELPER_COMMAND_CANCELLED")
        self.assertEqual(result["result"]["data"]["cancel_reason"], "unit stop")
        self.assertEqual(result["result"]["data"]["cancel_event"], "unit_test")
        self.assertTrue(any(entry["event"] == "helper_late_result_ignored" for entry in self.bridge_history()))

    def test_watchdog_timeout_writes_uncertain_result_and_requests_shutdown(self) -> None:
        app = self.make_app()
        entered = threading.Event()
        request = {
            "request_id": "REQ-UNIT-TIMEOUT",
            "command": "status.current",
            "args": {},
            "created_epoch": time.time() - 1.0,
            "timeout_seconds": 0.1,
        }

        with mock.patch.object(helperctl, "dispatch", self.slow_dispatch(entered)):
            job = app._start_external_command(request)
            self.assertTrue(entered.wait(1.0))
            app._poll_active_external_job()
            job.thread.join(1.0)
            app._poll_active_external_job()

        result = simon_common.read_json(self.path_values["HELPER_RESULT_FILE"])
        self.assertEqual(result["request_id"], "REQ-UNIT-TIMEOUT")
        self.assertEqual(result["result"]["status"], "UNCERTAIN")
        self.assertEqual(result["result"]["data"]["failure_type"], "HELPER_COMMAND_TIMEOUT")
        self.assertTrue(job.token.is_cancelled())
        app._request_shutdown.assert_called_once()
        self.assertTrue(any(entry["event"] == "helper_timed_out_request" for entry in self.bridge_history()))

    def test_result_from_non_current_generation_cannot_overwrite_active_request(self) -> None:
        app = self.make_app()
        old_job = agent_helper.ExternalCommandJob(
            request={},
            request_id="REQ-OLD",
            command="status.current",
            args={},
            created_epoch=time.time(),
            started_epoch=time.time(),
            timeout_seconds=5.0,
            generation=1,
            token=helperctl.CommandCancellationToken(),
            result=helperctl.command_result("SUCCESS", "status.current", "old"),
            completed_epoch=time.time(),
        )
        new_job = agent_helper.ExternalCommandJob(
            request={},
            request_id="REQ-NEW",
            command="helper.commands",
            args={},
            created_epoch=time.time(),
            started_epoch=time.time(),
            timeout_seconds=5.0,
            generation=2,
            token=helperctl.CommandCancellationToken(),
        )
        app.external_command_generation = 2
        app.active_external_job = new_job

        app._complete_external_job(old_job)

        self.assertFalse(self.path_values["HELPER_RESULT_FILE"].exists())
        self.assertTrue(any(entry["event"] == "helper_late_result_ignored" for entry in self.bridge_history()))


if __name__ == "__main__":
    unittest.main()
