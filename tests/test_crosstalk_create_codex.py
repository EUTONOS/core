"""Offline tests: crosstalk create-codex (a Codex chat created through a fresh app-server).

The app server is a fake and the rollout is a fixture file; no real codex process and no real app data."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_create_codex_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_create_codex_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

THREAD = "01a13613-74fe-7da0-b4f8-c6882554b559"
TURN = "01a13613-766e-7f32-b1d3-2d12b57580e8"
ACCOUNT = {"app": "codex", "id": "acct-1", "type": "chatgpt"}


class FakeServer:
    """Scripted stand-in for core.AppServer. `script` controls what the fake does."""

    instances: list = []

    def __init__(self, exe, script=None, kill_on_close=False):
        self.exe = exe
        self.script = script if script is not None else {}
        self.requests: list = []
        self.lines: queue.Queue = queue.Queue()
        self.closed = False
        self.client_name = None
        self.job_ok = True
        self._next = 0
        self.replies = []
        FakeServer.instances.append(self)

    def start(self, experimental, timeout, client_name="crosstalk_p0_probe"):
        self.client_name = client_name

    def request(self, method, params, timeout):
        if method == "model/list":
            if "models" not in self.script:
                raise core.ProbeError("app-server-error", "unknown method")  # not recorded: older tests
            self.requests.append((method, params))
            if self.script["models"] == "error":
                raise core.ProbeError("app-server-error", "boom")
            return {"data": [{"id": m, "model": m} for m in self.script["models"]], "nextCursor": None}
        self.requests.append((method, params))
        if method == "project/list":
            if self.script.get("project_list_error"):
                raise core.ProbeError("app-server-error", "unknown method")
            pages = self.script.get("project_pages", [{"data": []}])
            cursor = (params or {}).get("cursor")
            return pages[int(cursor) if cursor else 0]
        if method == "thread/start":
            if self.script.get("project_id_rejected") and "projectId" in params:
                raise core.ProbeError("app-server-error", "unknown field projectId")
            return self.script.get("thread_start", {})
        if method == "thread/metadata/update":
            return {}
        if method == "turn/start":
            if self.script.get("turn_start_error"):
                raise core.ProbeError("app-server-error", "boom")
            for message in self.script.get("after_turn", []):
                self.lines.put(message)
            return {"turn": {"id": TURN, "status": "inProgress"}}
        if method == "thread/name/set":
            if self.script.get("name_error"):
                raise core.ProbeError("app-server-error", "unknown method")
            return {}
        raise AssertionError("unexpected request " + method)

    def close(self):
        self.closed = True

    def send_request(self, method, params):
        self._next += 1
        if method == "turn/interrupt":
            self.requests.append((method, params))
            self.lines.put(turn_done("interrupted"))
            return self._next
        try:
            result = self.request(method, params, 1)
            self.lines.put({"id": self._next, "result": result})
        except core.ProbeError as error:
            self.lines.put({"id": self._next, "error": {"message": error.detail}})
        return self._next

    def reply(self, request_id, result=None, error=None):
        self.replies.append((request_id, result, error))

    def methods(self):
        return [method for method, _ in self.requests]


def item_done(text, phase="final_answer"):
    return {"method": "item/completed", "params": {"threadId": THREAD, "turnId": TURN,
                                                   "item": {"type": "agentMessage", "id": "m", "text": text,
                                                            "phase": phase}}}


def turn_done(status="completed", error=None):
    turn = {"id": TURN, "status": status, "error": error}
    return {"method": "turn/completed", "params": {"threadId": THREAD, "turn": turn}}


BS = chr(92)


class CreateCodexProjectTests(unittest.TestCase):
    def test_path_key_normalizes(self):
        key = core._project_path_key
        self.assertEqual(key(BS * 2 + "?" + BS + "C:" + BS + "Users" + BS + "X" + BS), "c:" + BS + "users" + BS + "x")
        self.assertEqual(key("c:/users/x"), "c:" + BS + "users" + BS + "x")
        self.assertEqual(key(BS * 2 + "?" + BS + "UNC" + BS + "srv" + BS + "sh"), BS * 2 + "srv" + BS + "sh")
        self.assertIsNone(key(""))

    def lookup(self, pages, folder, **script):
        server = FakeServer("codex", dict(project_pages=pages, **script))
        return core._codex_project_for_folder(server, folder), server

    def test_match_on_later_page_with_prefixed_root(self):
        pages = [{"data": [{"id": "p1", "roots": [{"path": "C:" + BS + "other"}]}], "nextCursor": "1"},
                 {"data": [{"id": "p2", "roots": [{"path": BS * 2 + "?" + BS + "C:" + BS + "Apps" + BS + "Toy" + BS}]}]}]
        (found, state), server = self.lookup(pages, "c:/apps/toy")
        self.assertEqual((found, state), ("p2", "assigned"))
        self.assertEqual(server.methods(), ["project/list", "project/list"])

    def test_no_match_and_unsupported(self):
        self.assertEqual(self.lookup([{"data": []}], "C:" + BS + "x")[0], (None, "none-for-folder"))
        self.assertEqual(self.lookup([], "C:" + BS + "x", project_list_error=True)[0], (None, "unsupported"))


class CreateCodexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-create-codex-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.rollout = self.root / "rollout.jsonl"
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device")})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)
        FakeServer.instances = []
        self.workers = []
        for patcher in (mock.patch.object(core, "codex_exe", return_value="codex"),
                        mock.patch.object(core, "codex_account", return_value=dict(ACCOUNT)),
                        mock.patch.object(core, "CODEX_ROLLOUT_WAIT_S", 0.2),
                        mock.patch.object(core, "_spawn_detached", side_effect=self.spawn)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def spawn(self, argv):
        core.codex_create_worker(argv[-1])
        return os.getpid()

    @contextlib.contextmanager
    def async_creator(self, script):
        workers = []
        def spawn(argv):
            worker = threading.Thread(target=core.codex_create_worker, args=(argv[-1],), daemon=True)
            workers.append(worker)
            worker.start()
            return os.getpid()
        with mock.patch.object(core, "_spawn_detached", side_effect=spawn), \
                mock.patch.object(core, "AppServer", side_effect=lambda exe, **kwargs: FakeServer(exe, script, **kwargs)):
            try:
                yield workers
            finally:
                for server in FakeServer.instances:
                    if not server.closed:
                        server.lines.put(turn_done("interrupted"))
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), "test worker did not stop")

    def write_rollout(self, model="gpt-6-luna", effort="xhigh", finished=True, thread=THREAD):
        rows = [{"type": "session_meta", "payload": {"id": thread, "source": "vscode", "originator": "crosstalk_create_codex",
                                                     "cli_version": "0.162.0-test", "cwd": str(self.folder)}},
                {"type": "turn_context", "payload": {"turn_id": TURN, "model": model, "effort": effort}}]
        if finished:
            rows.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN}})
        self.rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def script(self, **overrides):
        value = {"thread_start": {"thread": {"id": THREAD, "source": "vscode", "path": str(self.rollout)},
                                  "model": "gpt-6-luna", "reasoningEffort": "xhigh"},
                 "after_turn": [item_done("working", "commentary"), item_done("READY"), turn_done()]}
        value.update(overrides)
        return value

    def run_create(self, script, model="gpt-6-luna", effort="xhigh", title="Worker one", prompt="say READY"):
        with mock.patch.object(core, "AppServer", side_effect=lambda exe, **kwargs: FakeServer(exe, script, **kwargs)):
            return core.create_codex(str(self.folder), model, effort, prompt, title=title)

    def test_success_registers_with_exactly_one_thread_start(self):
        self.write_rollout()
        value = self.run_create(self.script())
        server = FakeServer.instances[0]
        self.assertEqual(len(FakeServer.instances), 1)
        self.assertEqual(server.methods(), ["project/list", "thread/start", "thread/name/set", "turn/start"])
        self.assertEqual(server.client_name, "crosstalk_create_codex")
        self.assertTrue(server.closed)
        start = server.requests[1][1]
        self.assertEqual(start, {"cwd": str(self.folder), "model": "gpt-6-luna", "allowProviderModelFallback": False,
                                 "ephemeral": False, "config": {"model_reasoning_effort": "xhigh"}})
        self.assertEqual(server.requests[3][1]["input"], [{"type": "text", "text": "say READY"}])
        self.assertEqual(server.requests[2][1], {"threadId": THREAD, "name": "Worker one"})
        self.assertTrue(value["ok"])
        self.assertEqual(value["thread_id"], THREAD)
        self.assertEqual(value["key"], "codex:" + THREAD)
        self.assertEqual(value["requested"], {"model": "gpt-6-luna", "effort": "xhigh"})
        self.assertTrue(value["title_set"])
        self.assertEqual(value["account_id"], "acct-1")
        verified = value["verified"]
        self.assertEqual((verified["model"], verified["effort"], verified["source"]), ("gpt-6-luna", "xhigh", "vscode"))
        self.assertEqual(verified["rollout"], str(self.rollout))
        self.assertTrue(verified["first_turn_persisted"])
        self.assertEqual(verified["first_reply"], "READY")
        self.assertEqual(verified["reply_phase"], "final_answer")
        entry = core.directory_load()["entries"][value["key"]]
        self.assertEqual(entry["project_folder"], str(self.folder))
        self.assertEqual(entry["project_folder_source"], "create-codex")
        self.assertEqual(entry["title"], "Worker one")
        self.assertEqual(entry["source"], "create-codex")
        self.assertEqual(entry["native"], {"model": "gpt-6-luna",
                                           "effort": "xhigh", "source": "vscode"})
        self.assertEqual([row["key"] for row in core.list_directory(None)], [value["key"]])

    def test_unknown_model_is_refused_before_thread_start(self):
        self.write_rollout()
        with self.assertRaises(core.ProbeError) as caught:
            self.run_create(self.script(models=["gpt-6-luna", "gpt-6-sol"]), model="gpt-nonexistent-9")
        self.assertEqual(caught.exception.reason, "unknown-model")
        self.assertIn("gpt-6-luna", caught.exception.detail)
        self.assertNotIn("thread/start", FakeServer.instances[0].methods())
        self.assertEqual(core.directory_load()["entries"], {})

    def test_known_model_proceeds_and_unreadable_list_does_not_block(self):
        self.write_rollout()
        value = self.run_create(self.script(models=["gpt-6-luna"]))
        self.assertTrue(value["ok"])
        self.assertEqual(FakeServer.instances[0].methods()[:2], ["model/list", "project/list"])
        FakeServer.instances = []
        value = self.run_create(self.script(models="error"))
        self.assertTrue(value["ok"])
        self.assertEqual(value["model_check_note"], "model-list-app-server-error")

    def test_first_turn_error_after_thread_start_reports_the_error_text(self):
        self.write_rollout()
        error = {"method": "error", "params": {"threadId": THREAD, "turnId": TURN, "willRetry": False,
                                                "error": {"message": "HTTP 400 invalid model gpt-x"}}}
        with self.assertRaises(core.ProbeError) as caught:
            self.run_create(self.script(after_turn=[error]))
        self.assertEqual(caught.exception.reason, "create-turn-failed-uncertain")
        self.assertIn("HTTP 400 invalid model", caught.exception.detail)
        self.assertEqual(caught.exception.thread_id, THREAD)

    def test_no_title_skips_naming_and_says_so(self):
        self.write_rollout()
        value = self.run_create(self.script(), title=None)
        self.assertEqual(FakeServer.instances[0].methods(), ["project/list", "thread/start", "turn/start"])
        self.assertFalse(value["title_set"])

    def test_unsupported_naming_still_succeeds_with_title_set_false(self):
        self.write_rollout()
        value = self.run_create(self.script(name_error=True))
        self.assertTrue(value["ok"])
        self.assertFalse(value["title_set"])
        self.assertEqual(value["title_note"], "app-server-error")

    def test_returned_model_or_effort_mismatch_refuses_with_thread_id_and_no_turn(self):
        for returned in ({"model": "gpt-6-sol", "reasoningEffort": "xhigh"},
                         {"model": "gpt-6-luna", "reasoningEffort": "medium"}):
            FakeServer.instances = []
            script = self.script(thread_start={"thread": {"id": THREAD, "path": str(self.rollout)}, **returned})
            with self.assertRaises(core.ProbeError) as caught:
                self.run_create(script)
            self.assertEqual(caught.exception.reason, "create-model-effort-refused")
            self.assertEqual(caught.exception.thread_id, THREAD)
            self.assertIn(THREAD, caught.exception.detail)
            self.assertEqual(FakeServer.instances[0].methods(), ["project/list", "thread/start"])
            self.assertTrue(FakeServer.instances[0].closed)
            self.assertEqual(core.directory_load()["entries"], {})

    def test_running_assignment_returns_before_completion_and_outlives_old_deadline(self):
        self.write_rollout(finished=False)
        with self.async_creator(self.script(after_turn=[])) as workers, mock.patch.object(core, "CREATE_TIMEOUT_S", 0):
            value = core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "work", title="Worker one")
            self.assertEqual(value["assignment_state"], "running")
            self.assertFalse(value["verified"]["first_turn_completed"])
            server = FakeServer.instances[0]
            self.assertFalse(server.closed)
            self.assertEqual(core.directory_load()["entries"][value["key"]]["title"], "Worker one")
            self.write_rollout()
            server.lines.put(turn_done())
            workers[0].join(3)
            self.assertFalse(workers[0].is_alive())
            self.assertEqual(core.create_codex_status(value["operation_id"])["assignment_state"], "completed")

    def test_startup_timeout_and_delayed_rollout_leave_accepted_worker_alive_without_replay(self):
        with self.async_creator(self.script(after_turn=[])) as workers, \
                mock.patch.object(core, "CREATE_CODEX_STARTUP_S", 0.1), \
                mock.patch.object(core, "CODEX_HEADLESS_CONTROLS_S", 0.01):
            with self.assertRaises(core.ProbeError) as caught:
                core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "work", title="Early title")
            self.assertEqual(caught.exception.reason, "create-startup-unconfirmed")
            self.assertEqual(caught.exception.thread_id, THREAD)
            operation = caught.exception.operation_id
            server = FakeServer.instances[0]
            self.assertFalse(server.closed)
            status = core.create_codex_status(operation)
            self.assertEqual(status["assignment_state"], "accepted-unverified")
            self.assertNotIn("request", status)
            self.assertEqual(core.directory_load()["entries"]["codex:" + THREAD]["title"], "Early title")
            self.assertEqual(core.codex_create_worker(operation)["reason"], "create-operation-already-claimed")
            self.write_rollout(finished=False)
            deadline = time.monotonic() + 3
            while not core.create_codex_status(operation).get("ok") and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(core.create_codex_status(operation)["assignment_state"], "running")
            self.assertFalse(server.closed)
            self.assertEqual(server.methods().count("thread/start"), 1)
            self.assertEqual(server.methods().count("turn/start"), 1)

    def test_control_changes_interrupt_running_assignment_and_decline_server_requests(self):
        for change, reason in (({"halt": True}, "halted"), ({"messaging": "off"}, "messaging-off"),
                               ({"deny_add": "codex:" + THREAD}, "denied")):
            with self.subTest(reason=reason):
                core.controls_update(messaging="on", halt=False, deny_remove="codex:" + THREAD)
                self.write_rollout(finished=False)
                script = self.script(after_turn=[{"method": "item/commandExecution/requestApproval", "id": 99}])
                with self.async_creator(script) as workers, mock.patch.object(core, "CODEX_HEADLESS_CONTROLS_S", 0.01):
                    value = core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "work")
                    core.controls_update(**change)
                    workers[0].join(3)
                    self.assertFalse(workers[0].is_alive())
                    server = FakeServer.instances[-1]
                    self.assertIn("turn/interrupt", server.methods())
                    self.assertEqual(server.replies, [(99, {"decision": "decline"}, None)])
                    status = core.create_codex_status(value["operation_id"])
                    self.assertEqual((status["assignment_state"], status["interrupted_by"]), ("interrupted", reason))
                    self.assertFalse(status["owner_interrupted"])

    def test_dead_worker_status_is_unknown_without_resending(self):
        self.write_rollout()
        value = self.run_create(self.script())
        core._create_codex_update(value["operation_id"], assignment_state="running")
        with mock.patch.object(core, "pid_alive", return_value=False):
            status = core.create_codex_status(value["operation_id"])
        self.assertEqual(status["assignment_state"], "unknown")
        self.assertFalse(status["ok"])
        self.assertEqual(FakeServer.instances[0].methods().count("thread/start"), 1)

    def test_lost_turn_start_acknowledgement_keeps_same_turn_alive(self):
        self.write_rollout(finished=False)
        original_call = core._headless_call
        def lose_ack(server, method, params, timeout, backlog, tally):
            if method == "turn/start":
                server.request(method, params, timeout)
                raise TimeoutError("lost response")
            return original_call(server, method, params, timeout, backlog, tally)
        with self.async_creator(self.script(after_turn=[])) as workers, \
                mock.patch.object(core, "_headless_call", side_effect=lose_ack):
            value = core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "work")
            self.assertEqual((value["assignment_state"], value["turn_id"]), ("running", TURN))
            self.assertTrue(value["turn_start_uncertain"])
            server = FakeServer.instances[0]
            self.assertFalse(server.closed)
            self.write_rollout()
            server.lines.put(turn_done())
            workers[0].join(3)
            self.assertEqual(core.create_codex_status(value["operation_id"])["assignment_state"], "completed")
            self.assertEqual(server.methods().count("turn/start"), 1)

    def test_stale_worker_heartbeat_and_public_status_are_read_only(self):
        self.write_rollout()
        value = self.run_create(self.script())
        operation = value["operation_id"]
        core._create_codex_update(operation, assignment_state="running", heartbeat_ns=1)
        before = core._create_codex_path(operation).read_bytes()
        status = core.create_codex_status(operation)
        self.assertEqual((status["assignment_state"], status["worker_observation"]), ("unknown", "stale"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["create-codex-status", operation])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["recorded_assignment_state"], "running")
        self.assertEqual(core._create_codex_path(operation).read_bytes(), before)

    def test_control_recheck_and_directory_failure_prevent_assignment_start(self):
        self.write_rollout()
        with mock.patch.object(core, "directory_register", side_effect=OSError("storage")), self.assertRaises(core.ProbeError):
            self.run_create(self.script())
        self.assertNotIn("turn/start", FakeServer.instances[-1].methods())
        with mock.patch.object(core, "controls_check", side_effect=lambda key: "denied" if key else None), \
                self.assertRaises(core.ProbeError) as caught:
            self.run_create(self.script())
        self.assertEqual(caught.exception.reason, "controls-denied")
        self.assertNotIn("turn/start", FakeServer.instances[-1].methods())

    def test_failed_turn_and_turn_start_error_carry_thread_id(self):
        self.write_rollout()
        cases = (("create-turn-not-completed", self.script(after_turn=[turn_done("failed", {"message": "nope"})])),
                 ("create-turn-start-refused", self.script(turn_start_error=True)))
        for reason, script in cases:
            FakeServer.instances = []
            with self.assertRaises(core.ProbeError) as caught:
                self.run_create(script)
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(caught.exception.thread_id, THREAD)
            self.assertEqual(FakeServer.instances[0].methods().count("thread/start"), 1)

    def test_server_exit_after_thread_start_carries_thread_id(self):
        self.write_rollout()
        with self.assertRaises(core.ProbeError) as caught:
            self.run_create(self.script(after_turn=[None]))
        self.assertEqual(caught.exception.reason, "create-server-exited")
        self.assertEqual(caught.exception.thread_id, THREAD)

    def test_rollout_that_disagrees_or_is_unfinished_refuses_with_thread_id(self):
        for reason, build in (("create-rollout-model-effort-mismatch", lambda: self.write_rollout(model="other")),
                              ("create-rollout-model-effort-mismatch", lambda: self.write_rollout(effort="low")),
                              ("create-rollout-thread-mismatch", lambda: self.write_rollout(thread="x"))):
            build()
            with self.assertRaises(core.ProbeError) as caught:
                self.run_create(self.script())
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(caught.exception.thread_id, THREAD)
            self.assertIn("codex:" + THREAD, core.directory_load()["entries"])

    def test_missing_rollout_refuses_with_thread_id(self):
        with self.assertRaises(core.ProbeError) as caught:
            self.run_create(self.script())
        self.assertEqual(caught.exception.reason, "create-rollout-incomplete")
        self.assertEqual(caught.exception.thread_id, THREAD)

    def test_thread_start_uncertain_is_not_retried(self):
        class Slow(FakeServer):
            def request(self, method, params, timeout):
                self.requests.append((method, params))
                raise TimeoutError(method)

        with mock.patch.object(core, "AppServer", side_effect=lambda exe, **kwargs: Slow(exe)), \
                self.assertRaises(core.ProbeError) as caught:
            core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "hi")
        self.assertEqual(caught.exception.reason, "create-thread-start-uncertain")
        self.assertEqual(len(FakeServer.instances), 1)
        self.assertEqual(FakeServer.instances[0].methods(), ["model/list", "project/list", "thread/start"])
        self.assertTrue(FakeServer.instances[0].closed)

    def test_controls_refuse_before_any_app_server_call(self):
        for change, reason in (({"halt": True}, "controls-halted"),
                               ({"halt": False, "messaging": "off"}, "controls-messaging-off")):
            core.controls_update(**change)
            with mock.patch.object(core, "AppServer") as app_server, self.assertRaises(core.ProbeError) as caught:
                core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "hi")
            self.assertEqual(caught.exception.reason, reason)
            app_server.assert_not_called()

    def test_invalid_arguments_refuse_before_any_app_server_call(self):
        folder = str(self.folder)
        cases = ((folder, "gpt-6-luna", "turbo", "hi", "invalid-effort"),
                 (str(self.root / "missing"), "gpt-6-luna", "high", "hi", "folder-must-be-existing-absolute-dir"),
                 ("relative", "gpt-6-luna", "high", "hi", "folder-must-be-existing-absolute-dir"),
                 (folder, "bad model;", "high", "hi", "invalid-model"),
                 (folder, "gpt-6-luna", "high", "  ", "prompt-invalid"),
                 (folder, "gpt-6-luna", "high", "a\x00b", "prompt-invalid"))
        for args in cases:
            with mock.patch.object(core, "AppServer") as app_server, self.assertRaises(core.ProbeError) as caught:
                core.create_codex(*args[:4])
            self.assertEqual(caught.exception.reason, args[4])
            app_server.assert_not_called()

    def test_missing_codex_cli_refuses(self):
        with mock.patch.object(core, "codex_exe", return_value=None), mock.patch.object(core, "AppServer") as app_server, \
                self.assertRaises(core.ProbeError) as caught:
            core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "hi")
        self.assertEqual(caught.exception.reason, "codex-cli-not-found")
        app_server.assert_not_called()

    def test_default_client_name_is_unchanged_for_other_callers(self):
        sent = []
        with mock.patch.object(core.AppServer, "__init__", lambda self, exe: None):
            server = core.AppServer("codex")
            server.request = lambda method, params, timeout: sent.append((method, params))
            server.send = lambda message: sent.append(("send", message))
            server.start(False, 1)
            server.start(True, 1, "crosstalk_create_codex")
        self.assertEqual(sent[0][1]["clientInfo"]["name"], "crosstalk_p0_probe")
        self.assertEqual(sent[2][1]["clientInfo"]["name"], "crosstalk_create_codex")

    def test_cli_prints_one_json_object_and_names_the_thread_on_failure(self):
        buffer = io.StringIO()
        with mock.patch.object(core, "create_codex", return_value={"ok": True}) as create, \
                contextlib.redirect_stdout(buffer):
            code = cli.main(["create-codex", "--folder", str(self.folder), "--model", "m", "--effort", "low",
                             "--title", "T", "--prompt", "hi"])
        self.assertEqual(code, 0)
        self.assertEqual(create.call_args.args, (str(self.folder), "m", "low", "hi"))
        self.assertEqual(create.call_args.kwargs, {"title": "T"})
        self.assertTrue(json.loads(buffer.getvalue())["ok"])
        failure = core.ProbeError("create-turn-timeout", f"thread_id={THREAD} x")
        failure.thread_id = THREAD
        buffer = io.StringIO()
        with mock.patch.object(core, "create_codex", side_effect=failure), contextlib.redirect_stdout(buffer):
            code = cli.main(["create-codex", "--folder", str(self.folder), "--model", "m", "--effort", "low",
                             "--prompt", "hi"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())["thread_id"], THREAD)

    def test_config_overrides_reach_thread_start_and_effort_stays_owned_by_effort(self):
        self.write_rollout()
        with mock.patch.object(core, "AppServer", side_effect=lambda exe, **kwargs: FakeServer(exe, self.script(), **kwargs)):
            value = core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "say READY", title="Worker one",
                                      config={"memories.use_memories": False, "memories.generate_memories": False})
        start = FakeServer.instances[0].requests[1][1]
        self.assertEqual(start["config"], {"memories.use_memories": False, "memories.generate_memories": False,
                                           "model_reasoning_effort": "xhigh"})
        self.assertEqual(value["requested"]["config"], {"memories.use_memories": False,
                                                        "memories.generate_memories": False})
        for bad in ({"model_reasoning_effort": "low"}, {"Bad Key": 1}, {"k": [1]}, {"k": ["x"] * 9}, {"k": "x" * 201}):
            with self.assertRaises(core.ProbeError) as caught:
                core.create_codex(str(self.folder), "gpt-6-luna", "xhigh", "hi", config=bad)
            self.assertEqual(caught.exception.reason, "config-invalid")

    def test_cli_parses_repeated_config_values(self):
        buffer = io.StringIO()
        with mock.patch.object(core, "create_codex", return_value={"ok": True}) as create,                 contextlib.redirect_stdout(buffer):
            code = cli.main(["create-codex", "--folder", str(self.folder), "--model", "m", "--effort", "low",
                             "--prompt", "hi", "--config", "memories.use_memories=false", "--config", "a.n=3",
                             "--config", "a.s=text", "--config", 'a.l=["C:/x"]'])
        self.assertEqual(code, 0)
        self.assertEqual(create.call_args.kwargs["config"],
                         {"memories.use_memories": False, "a.n": 3, "a.s": "text", "a.l": ["C:/x"]})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["create-codex", "--folder", str(self.folder), "--model", "m", "--effort",
                                       "low", "--prompt", "hi", "--config", "novalue"]), 2)

    def test_cli_requires_exactly_one_prompt_source(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["create-codex", "--folder", str(self.folder), "--model", "m", "--effort", "low"])
        self.assertEqual(code, 2)


class CreateCodexAssignTests(CreateCodexTests):
    def project_script(self, **extra):
        pages = [{"data": [{"id": "proj-1", "roots": [{"path": BS * 2 + "?" + BS + str(self.folder).upper()}]}]}]
        return self.script(project_pages=pages, **extra)

    def starts(self):
        return [params for method, params in FakeServer.instances[-1].requests if method == "thread/start"]

    def test_matching_project_is_assigned(self):
        self.write_rollout()
        value = self.run_create(self.project_script())
        self.assertEqual(self.starts()[0].get("projectId"), "proj-1")
        self.assertEqual(value["project_id"], "proj-1")
        self.assertEqual(value["project_assignment"], "assigned")
        self.assertEqual(value["desktop_integration"], "unverified")

    def test_no_project_for_folder_creates_thread_without_project(self):
        self.write_rollout()
        value = self.run_create(self.script())
        self.assertNotIn("projectId", self.starts()[0])
        self.assertEqual(value["project_assignment"], "none-for-folder")
        self.assertIsNone(value["project_id"])

    def test_rejected_project_id_retries_once_without_duplicate_thread(self):
        self.write_rollout()
        value = self.run_create(self.project_script(project_id_rejected=True))
        self.assertEqual(len(FakeServer.instances), 1)
        self.assertEqual([("projectId" in p) for p in self.starts()], [True, False])  # rejected, then retried bare
        self.assertEqual(sum(1 for m in FakeServer.instances[0].methods() if m == "thread/name/set"), 1)
        self.assertEqual(value["project_assignment"], "unsupported")
        self.assertEqual(FakeServer.instances[0].methods().count("turn/start"), 1)

    def test_assign_project_cli_core(self):
        server = FakeServer("codex", self.project_script())
        with mock.patch.object(core, "AppServer", side_effect=lambda exe, **kw: server):
            result = core.codex_assign_project(THREAD, str(self.folder))
        self.assertTrue(result["ok"])
        self.assertIn(("thread/metadata/update", {"threadId": THREAD, "projectId": "proj-1"}), server.requests)
        server = FakeServer("codex", self.script())
        with mock.patch.object(core, "AppServer", side_effect=lambda exe, **kw: server):
            result = core.codex_assign_project(THREAD, str(self.folder))
        self.assertFalse(result["ok"])
        self.assertNotIn("thread/metadata/update", server.methods())


if __name__ == "__main__":
    unittest.main()
