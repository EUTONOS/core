"""Offline tests: crosstalk create-hermes (a Hermes Desktop chat created through the running backend's /api/ws).

The WebSocket and the HTTP token page are fakes, the Hermes home is a temp folder and the core's subprocess is
stubbed; no real Hermes, no model, no real token."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load("crosstalk_create_hermes_core", KIT / "crosstalk_p0_core.py")
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli = load("crosstalk_create_hermes_cli", KIT / "crosstalk_p0_cli.py")
conn = load("crosstalk_create_hermes_conn", KIT / "crosstalk_hermes_connector.py")

TOKEN = "SECRET-token_Zx9-never-print-me"
STORED = "20261014_101500_a1b2c3"
RUNTIME_SID = "deadbeef"


class Clock:
    """Advances on every read so a wait loop ends without real sleeping."""

    def __init__(self, step=0.5):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class FakeWs:
    """Stands in for a websockets sync connection. `behaviour` shapes the gateway."""

    def __init__(self, behaviour=None):
        self.b = behaviour or {}
        self.sent: list[dict] = []
        self.queue: list[str] = []
        self.closed = False
        if not self.b.get("no_ready"):
            self.queue.append(json.dumps({"jsonrpc": "2.0", "method": "event",
                                          "params": {"type": "gateway.ready", "payload": {}}}))

    def send(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        method, rid = frame.get("method"), frame.get("id")
        if method == "session.create":
            if self.b.get("create_error"):
                self.queue.append(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": 4002, "message": "bad " + TOKEN}}))
                return
            self.queue.append(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "session_id": RUNTIME_SID, "stored_session_id": self.b.get("stored", STORED),
                "info": {"model": "fake/model", "provider": "fakeprov"}}}))
        elif method == "prompt.submit":
            if self.b.get("submit_error"):
                self.queue.append(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": 4090, "message": "busy"}}))
                return
            self.queue.append(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}}))
            finish = self.b.get("finish", "complete")
            if finish == "complete":
                self.queue.append(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                    "type": "message.complete", "session_id": RUNTIME_SID, "payload": {"text": "BODY", "status": "complete"}}}))
            elif finish == "error":
                self.queue.append(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                    "type": "error", "session_id": RUNTIME_SID, "payload": {"message": "boom " + TOKEN}}}))

    def recv(self, timeout=None):
        if self.queue:
            return self.queue.pop(0)
        raise TimeoutError

    def close(self):
        self.closed = True


def page(token=TOKEN):
    return f'<script>window.__HERMES_SESSION_TOKEN__={json.dumps(token)};window.__HERMES_AUTH_REQUIRED__=false;</script>'


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-create-hermes-conn-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        # Safety: no test may ever reach a real Hermes backend (a live one may be running on this machine).
        def no_network(*args, **kwargs):
            raise AssertionError("a test tried to use the real network or websocket")
        for target, name in ((conn, "http_get_root"), (conn, "ws_connect")):
            patcher = mock.patch.object(target, name, no_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_ledger()
        self.urls: list[str] = []

    def write_ledger(self, rows=None):
        rows = rows if rows is not None else [
            {"purpose": "cli", "port": None, "host": "", "registered_at": 9},
            {"purpose": "serve", "port": 45111, "host": "127.0.0.1", "registered_at": 1, "hermes_home": str(self.home)},
            {"purpose": "serve", "port": 45123, "host": "127.0.0.1", "registered_at": 2, "hermes_home": str(self.home).upper()},
            {"purpose": "serve", "port": 45999, "host": "127.0.0.1", "registered_at": 3, "hermes_home": "C:\\other\\home"},
        ]
        (self.home / "spawn-ledger.json").write_text(json.dumps(rows), encoding="utf-8")

    def request(self, **extra):
        value = {"hermes_home": str(self.home), "prompt": "say hi", "wait_s": 5}
        value.update(extra)
        return value

    def run_create(self, request=None, ws=None, http_get=None):
        ws = ws or FakeWs()
        def connect(url):
            self.urls.append(url)
            return ws
        value = conn.create_chat(request or self.request(), http_get=http_get or (lambda port: page()),
                                 connect=connect, clock=Clock())
        return value, ws

    def test_ledger_ports_are_serve_only_newest_first_and_home_filtered(self):
        self.assertEqual(conn.ledger_ports(str(self.home)), [45123, 45111])
        self.assertEqual(conn.ledger_ports(str(self.home / "missing")), [])

    def test_success_no_model_sends_no_model_field(self):
        value, ws = self.run_create()
        create = next(f for f in ws.sent if f["method"] == "session.create")["params"]
        self.assertEqual(create["source"], "desktop")
        for field in ("model", "provider", "reasoning_effort"):
            self.assertNotIn(field, create)
        submit = next(f for f in ws.sent if f["method"] == "prompt.submit")["params"]
        self.assertEqual(submit, {"session_id": RUNTIME_SID, "text": "say hi"})
        self.assertEqual(value["stored_session_id"], STORED)
        self.assertEqual(value["turn"], "completed")
        self.assertTrue(ws.closed)
        self.assertIn("/api/ws?token=", self.urls[0])
        self.assertIn("127.0.0.1:45123", self.urls[0])

    def test_model_provider_effort_title_and_idempotency_key_are_sent(self):
        value, ws = self.run_create(self.request(model="gpt-x", provider="openrouter", effort="xhigh",
                                                 title="  My   chat ", idempotency_key="abc"))
        create = next(f for f in ws.sent if f["method"] == "session.create")["params"]
        self.assertEqual((create["model"], create["provider"], create["reasoning_effort"]), ("gpt-x", "openrouter", "xhigh"))
        self.assertEqual(create["title"], "My chat")
        self.assertEqual(create["idempotency_key"], "crosstalk-abc")
        self.assertEqual(value["requested"], {"model": "gpt-x", "provider": "openrouter", "effort": "xhigh"})

    def test_creator_title_is_pre_registered_before_the_first_prompt(self):
        order = []
        real = conn.subprocess.run
        def fake_run(argv, **kwargs):
            order.append(("register", [a for a in argv if a.startswith("--")], argv[-1]))
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        ws = FakeWs()
        original = ws.send
        def spy(text):
            order.append((json.loads(text).get("method"),))
            original(text)
        ws.send = spy
        with mock.patch.object(conn.subprocess, "run", fake_run):
            self.run_create(self.request(title="Creator", pre_register_argv=["py", "cli", "hermes-pre-register", "--title", "Creator"]), ws=ws)
        names = [item[0] for item in order]
        self.assertLess(names.index("register"), names.index("prompt.submit"))
        self.assertGreater(names.index("register"), names.index("session.create"))
        self.assertEqual(order[names.index("register")][2], STORED)

    def test_pre_register_failure_never_blocks_the_create(self):
        def boom(*a, **k):
            raise OSError("nope")
        with mock.patch.object(conn.subprocess, "run", boom):
            value, _ = self.run_create(self.request(pre_register_argv=["x"]))
        self.assertEqual(value["turn"], "completed")

    def test_effort_is_validated_before_any_connection(self):
        calls = []
        with self.assertRaises(conn.ConnectorError) as raised:
            conn.create_chat(self.request(effort="turbo"), http_get=lambda p: calls.append(p), connect=calls.append)
        self.assertEqual(raised.exception.reason, "invalid-effort")
        self.assertEqual(calls, [])
        for effort in conn.EFFORTS:
            self.assertEqual(conn.validate_effort(effort), effort)
        self.assertEqual(tuple(conn.EFFORTS), tuple(core.HERMES_EFFORTS))

    def test_hermes_not_running_when_no_ledger_or_nothing_listens(self):
        (self.home / "spawn-ledger.json").unlink()
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create()
        self.assertEqual(raised.exception.reason, "hermes-not-running")
        self.write_ledger()
        def refused(port):
            raise ConnectionRefusedError()
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create(http_get=refused)
        self.assertEqual(raised.exception.reason, "hermes-not-running")

    def test_gated_backend_without_token_is_reported(self):
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create(http_get=lambda port: "<html>no token here</html>")
        self.assertEqual(raised.exception.reason, "hermes-token-unavailable")

    def test_token_never_leaves_in_errors(self):
        def connect(url):
            raise OSError("connect to " + url + " failed")
        with self.assertRaises(conn.ConnectorError) as raised:
            conn.create_chat(self.request(), http_get=lambda p: page(), connect=connect, clock=Clock())
        text = raised.exception.reason + raised.exception.detail
        self.assertNotIn(TOKEN, text)
        self.assertIn("[token]", text)
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create(ws=FakeWs({"create_error": True}))
        self.assertNotIn(TOKEN, raised.exception.detail)
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create(ws=FakeWs({"finish": "error"}))
        self.assertEqual(raised.exception.reason, "create-turn-error")
        self.assertEqual(raised.exception.session_id, STORED)
        self.assertNotIn(TOKEN, raised.exception.detail)

    def test_token_never_in_success_output_line(self):
        out = io.StringIO()
        request = json.dumps(self.request())
        with mock.patch.object(conn, "http_get_root", lambda port: page()), \
             mock.patch.object(conn, "ws_connect", lambda url: FakeWs()), \
             mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(request.encode()))), \
             contextlib.redirect_stdout(out):
            code = conn.main()
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["stored_session_id"], STORED)

    def test_prompt_submit_failure_names_the_chat(self):
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create(ws=FakeWs({"submit_error": True}))
        self.assertEqual(raised.exception.reason, "create-prompt-submit-failed")
        self.assertEqual(raised.exception.session_id, STORED)

    def test_bad_stored_id_is_refused(self):
        with self.assertRaises(conn.ConnectorError) as raised:
            self.run_create(ws=FakeWs({"stored": "not-an-id"}))
        self.assertEqual(raised.exception.reason, "create-session-id-missing")

    def test_turn_still_running_at_the_bounded_wait_is_not_a_failure(self):
        value, ws = self.run_create(ws=FakeWs({"finish": "never"}), request=self.request(wait_s=2))
        self.assertEqual(value["turn"], "running")
        self.assertEqual(value["stored_session_id"], STORED)
        self.assertTrue(ws.closed)

    def test_no_gateway_ready_fails(self):
        with mock.patch.object(conn, "READY_TIMEOUT_S", 1.0):
            with self.assertRaises(conn.ConnectorError) as raised:
                self.run_create(ws=FakeWs({"no_ready": True}))
        self.assertEqual(raised.exception.reason, "hermes-gateway-not-ready")


class CoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-create-hermes-core-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hermes = self.root / "hermes"
        (self.hermes / "plugins" / "crosstalk").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.hermes),
                                           "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                           "CROSSTALK_P0_HERMES_PYTHON": sys.executable})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)

    def good(self, **extra):
        value = {"tag": conn.TAG, "ok": True, "stored_session_id": STORED, "turn": "completed", "turn_status": "complete",
                 "requested": {}, "reported": {"model": "fake/model", "provider": "fakeprov"}}
        value.update(extra)
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(value) + "\n", stderr="")

    def run_core(self, completed, **kwargs):
        with mock.patch.object(core.subprocess, "run", return_value=completed) as run:
            kwargs.setdefault("prompt", "do X")
            try:
                return core.create_hermes(**kwargs), run
            except core.ProbeError as error:
                error.run = run
                raise

    def test_registers_and_journals_and_sends_prompt_only_on_stdin(self):
        value, run = self.run_core(self.good(), title="Hermes worker", model="gpt-x", provider="openrouter", effort="high")
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:3], ["-I", "-B"])
        self.assertTrue(argv[3].endswith("crosstalk_hermes_connector.py"))
        self.assertEqual(len(argv), 4)  # nothing but the script: no prompt, no secret on a command line
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual((sent["prompt"], sent["model"], sent["provider"], sent["effort"]), ("do X", "gpt-x", "openrouter", "high"))
        self.assertEqual(sent["hermes_home"], str(self.hermes))
        self.assertTrue(value["ok"])
        self.assertEqual(value["key"], "hermes:" + STORED)
        self.assertEqual(value["requested"], {"model": "gpt-x", "provider": "openrouter", "effort": "high"})
        entry = core.directory_load()["entries"]["hermes:" + STORED]
        self.assertEqual((entry["app"], entry["title"], entry["source"]), ("hermes", "Hermes worker", "create-hermes"))
        rows = [row for row in core.journal_rows() if row.get("entry_key") == "hermes:" + STORED]
        self.assertEqual(rows[-1]["caller"]["source"], "create-hermes")

    def test_no_model_sends_no_model(self):
        _, run = self.run_core(self.good())
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertIsNone(sent["model"])
        self.assertIsNone(sent["provider"])
        self.assertIsNone(sent["effort"])

    def test_refusals_never_reach_the_connector(self):
        cases = [({"effort": "turbo"}, "invalid-effort"), ({"provider": "openrouter"}, "provider-needs-model"),
                 ({"model": "bad model!"}, "invalid-model"), ({"prompt": "  "}, "prompt-invalid"),
                 ({"wait_s": 1}, "invalid-wait")]
        for kwargs, reason in cases:
            with mock.patch.object(core.subprocess, "run") as run:
                with self.assertRaises(core.ProbeError) as raised:
                    core.create_hermes(**dict({"prompt": "x"}, **kwargs))
                self.assertEqual(raised.exception.reason, reason, kwargs)
                run.assert_not_called()

    def test_every_hermes_effort_is_accepted(self):
        for effort in core.HERMES_EFFORTS:
            self.run_core(self.good(), effort=effort)

    def test_halt_refuses(self):
        core.controls_update(halt=True)
        with mock.patch.object(core.subprocess, "run") as run:
            with self.assertRaises(core.ProbeError):
                core.create_hermes("x")
            run.assert_not_called()

    def test_connector_failure_after_create_names_the_chat_and_is_not_registered(self):
        failed = subprocess.CompletedProcess([], 1, stdout=json.dumps({
            "ok": False, "reason": "create-turn-error", "detail": "boom", "stored_session_id": STORED}) + "\n", stderr="")
        with self.assertRaises(core.ProbeError) as raised:
            self.run_core(failed)
        self.assertEqual(raised.exception.reason, "create-turn-error")
        self.assertEqual(raised.exception.stored_session_id, STORED)
        self.assertIn("do not create a second", raised.exception.detail)
        self.assertNotIn("hermes:" + STORED, core.directory_load()["entries"])

    def test_creator_title_entry_exists_for_a_first_turn_letter_and_failure_removes_it(self):
        core.register_hermes_pending(STORED, "  Creator   title ")
        entry = core.directory_load()["entries"]["hermes:" + STORED]
        self.assertEqual((entry["title"], entry["source"]), ("Creator title", core.PENDING_CREATE_SOURCE))
        # Hermes' auto-title (a later register/discover) must not replace it.
        self.assertTrue(core._creator_title_kept(entry))
        core.register_hermes(STORED, "tool-call")
        self.assertEqual(core.directory_load()["entries"]["hermes:" + STORED]["title"], "Creator title")
        core.directory_register("hermes", STORED, {"source": core.PENDING_CREATE_SOURCE})
        failed = subprocess.CompletedProcess([], 1, stdout=json.dumps({
            "ok": False, "reason": "create-turn-error", "detail": "boom", "stored_session_id": STORED}) + "\n", stderr="")
        with self.assertRaises(core.ProbeError):
            self.run_core(failed, title="Creator title")
        self.assertNotIn("hermes:" + STORED, core.directory_load()["entries"])

    def test_request_carries_pre_register_argv_only_with_a_title(self):
        _, run = self.run_core(self.good(), title="T")
        argv = json.loads(run.call_args.kwargs["input"])["pre_register_argv"]
        self.assertEqual(argv[-1], "--title=T")
        self.assertIn("hermes-pre-register", argv)
        _, run = self.run_core(self.good())
        self.assertNotIn("pre_register_argv", json.loads(run.call_args.kwargs["input"]))

    def test_clip_reason_keeps_readable_text_and_marks_cuts(self):
        self.assertEqual(core.clip_reason("a" * 300), "a" * 300)
        cut = core.clip_reason("b" * 5000)
        self.assertEqual(len(cut), core.REASON_MAX)
        self.assertTrue(cut.endswith("…"))
        self.assertEqual(core.clip_reason(None), "")

    def test_not_running_failure_passes_through(self):
        failed = subprocess.CompletedProcess([], 1, stdout=json.dumps({
            "ok": False, "reason": "hermes-not-running", "detail": "no live backend"}) + "\n", stderr="")
        with self.assertRaises(core.ProbeError) as raised:
            self.run_core(failed)
        self.assertEqual(raised.exception.reason, "hermes-not-running")
        self.assertFalse(getattr(raised.exception, "stored_session_id", None))

    def test_garbage_output_is_uncertain_not_retried(self):
        with self.assertRaises(core.ProbeError) as raised:
            self.run_core(subprocess.CompletedProcess([], 0, stdout="oops", stderr=""))
        self.assertEqual(raised.exception.reason, "create-hermes-output-not-json")
        self.assertIn("check Hermes Desktop", raised.exception.detail)
        self.assertEqual(raised.exception.run.call_count, 1)

    def test_real_connector_subprocess_reports_hermes_not_running(self):
        # The real script, run isolated with this python; no ledger exists, so it fails before any websocket.
        with self.assertRaises(core.ProbeError) as raised:
            core.create_hermes("do X")
        self.assertEqual(raised.exception.reason, "hermes-not-running")

    def test_cli_json_shape_success_and_failure(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("do X", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(core.subprocess, "run", return_value=self.good()), contextlib.redirect_stdout(out):
            code = cli.main(["create-hermes", "--prompt-file", str(prompt), "--title", "T", "--effort", "low"])
        value = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        for key in ("tag", "ok", "session_id", "key", "requested", "first_turn", "turn_status", "state_db", "reported"):
            self.assertIn(key, value)
        self.assertEqual((value["ok"], value["key"], value["first_turn"]), (True, "hermes:" + STORED, "completed"))
        self.assertEqual(value["requested"], {"model": None, "provider": None, "effort": "low"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["create-hermes", "--prompt", "x"])  # real connector, no Hermes: clear failure
        value = json.loads(out.getvalue())
        self.assertEqual((code, value["ok"], value["reason"]), (1, False, "hermes-not-running"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["create-hermes", "--prompt", "x", "--effort", "turbo"])  # argparse refuses before connecting
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["create-hermes"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
