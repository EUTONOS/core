"""Offline Hermes endpoint regressions (slice 3B.12): temp device folder, temp Hermes home with a minimal
state.db, and a fake listener that runs the plugin's own claim/inject code. No live app, plugin or account is used."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load("crosstalk_hermes_core", KIT / "crosstalk_p0_core.py")
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli = load("crosstalk_hermes_cli", KIT / "crosstalk_p0_cli.py")
    installer = load("crosstalk_hermes_install", KIT / "crosstalk_p0_install.py")
plugin = load("crosstalk_hermes_plugin", KIT / "hermes-plugin" / "crosstalk" / "__init__.py")

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
H1 = "20261013_230248_6a7bf5"
H2 = "20261013_230300_0b1c2d"
H_ARCHIVED = "20261012_100000_aaaaaa"
H_HIDDEN = "20261012_110000_bbbbbb"
H_OLD = "20250101_000000_cccccc"


class FakeCtx:
    """What the plugin sees from the Hermes host."""

    def __init__(self, returns=True):
        self.returns = returns
        self.calls = []
        self._manager = SimpleNamespace(has_tui_message_injector=True)

    def inject_message(self, text, role, session_key=None):
        self.calls.append((text, role, session_key))
        return self.returns


class FakeListener(threading.Thread):
    """A stand-in for the plugin loop: heartbeat plus claim/inject through the plugin's own _handle."""

    def __init__(self, ctx, mode="normal", beat=True):
        super().__init__(daemon=True)
        self.ctx, self.mode, self.beat, self.stop_flag = ctx, mode, beat, threading.Event()

    def run(self):
        base = plugin._base()
        while not self.stop_flag.is_set():
            if self.beat:
                plugin._write_json(base / "heartbeat.json", {"pid": os.getpid(), "utc": plugin._now(),
                                                              "hosts_chats": True, "plugin_version": "test"})
            for path in sorted((base / "inbox").glob("*.json")):
                if self.mode == "claim-only":
                    os.replace(path, base / "work" / (path.stem + ".claimed.json"))
                elif self.mode in ("normal", "full"):
                    plugin._handle(self.ctx, path)
            if self.mode == "full":
                plugin._waiting_pass(self.ctx, base)
            time.sleep(0.02)


class HermesCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-hermes-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device = self.root / "device"
        self.hermes = self.root / "hermes"
        (self.hermes / "plugins" / "crosstalk").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_DEVICE_DIR": str(self.device),
                                          "CROSSTALK_P0_HERMES_HOME": str(self.hermes),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_CODEX_WAKE": "off"})
        env.start()
        self.addCleanup(env.stop)
        home = mock.patch.object(plugin, "_home", return_value=self.hermes)
        home.start()
        self.addCleanup(home.stop)
        config = mock.patch.object(plugin, "_config", return_value={"device_dir": str(self.device)})
        config.start()
        self.addCleanup(config.stop)
        for name, value in (("HERMES_RESULT_WAIT_S", 0.5), ("HERMES_RESULT_POLL_S", 0.02)):
            patcher = mock.patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for sub in ("inbox", "work", "done", "cancelled"):
            (self.hermes / "crosstalk" / sub).mkdir(parents=True)
        self.make_state_db()
        core.controls_update(messaging="on")

    # ------------------------------------------------------------ fixtures
    def make_state_db(self):
        db = sqlite3.connect(str(self.hermes / "state.db"))
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, archived INTEGER,"
                   " hidden INTEGER, last_activity_at REAL, started_at REAL NOT NULL, profile_name TEXT)")
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                   "role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL, message_uid TEXT)")
        rows = [(H1, "desktop", "Planning chat", 0, 0, 200.0, 100.0, "default"),
                (H2, "tui", None, 0, 0, 300.0, 150.0, None),
                (H_ARCHIVED, "desktop", "Old archived", 1, 0, 90.0, 50.0, None),
                (H_HIDDEN, "desktop", "Hidden helper", 0, 1, 90.0, 50.0, None),
                (H_OLD, "desktop", "Ancient", 0, 0, 1.0, 1.0, None)]
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", rows)
        db.commit()
        db.close()

    def add_message(self, session, role, content):
        db = sqlite3.connect(str(self.hermes / "state.db"))
        db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                   (session, role, content, time.time()))
        db.commit()
        db.close()

    def listener(self, **options):
        ctx = FakeCtx(options.pop("returns", True))
        thread = FakeListener(ctx, **options)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(thread.stop_flag.set)
        deadline = time.monotonic() + 3
        while options.get("beat", True) and not (self.hermes / "crosstalk" / "heartbeat.json").is_file() \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        return ctx, thread

    def letter(self, letter_id):
        return core.letters_load()[letter_id]

    def states(self, letter_id):
        return [row["state"] for row in self.letter(letter_id)["history"]]

    def inbox_files(self):
        return sorted((self.hermes / "crosstalk" / "inbox").glob("*"))

    def send_to_hermes(self, to="hermes:" + H1, text="Hello Hermes"):
        return core.send_letter("claude", CLAUDE, to, text)


class DiscoveryTests(HermesCase):
    def test_list_registers_recent_visible_chats_only(self):
        rows = {row["key"]: row for row in core.list_directory()}
        self.assertEqual(sorted(rows), sorted(["hermes:" + H1, "hermes:" + H2, "hermes:" + H_OLD]))
        self.assertEqual(rows["hermes:" + H1]["title"], "Planning chat")
        entry = core.directory_load()["entries"]["hermes:" + H1]
        self.assertEqual((entry["app"], entry["id"], entry["hermes_source"], entry["profile"]),
                         ("hermes", H1, "desktop", "default"))
        self.assertNotIn("hermes:" + H_ARCHIVED, rows)
        self.assertNotIn("hermes:" + H_HIDDEN, rows)

    def test_creator_supplied_title_survives_hermes_auto_title(self):
        core.directory_register("hermes", H1, {"title": "G Hermes", "title_source": core.CREATOR_TITLE_SOURCE})
        # state.db says Planning chat (Hermes's own title); discovery and registration must not replace ours
        core.hermes_discover()
        core.register_hermes(H1)
        entry = core.directory_load()["entries"]["hermes:" + H1]
        self.assertEqual((entry["title"], entry["title_source"]), ("G Hermes", core.CREATOR_TITLE_SOURCE))

    def test_discovery_is_bounded(self):
        db = sqlite3.connect(str(self.hermes / "state.db"))
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                       [("20260101_%06d_%06x" % (i, i), "desktop", "t", 0, 0, 1000.0 + i, 1.0, None) for i in range(80)])
        db.commit()
        db.close()
        self.assertEqual(core.hermes_discover()["seen"], core.HERMES_DISCOVER_LIMIT)

    def test_delivery_hint_follows_listener_heartbeat(self):
        hint = lambda: {r["key"]: r for r in core.list_directory()}["hermes:" + H1]["delivery_hint"]
        self.assertEqual(hint(), "blocked:not-running")  # no heartbeat
        _, thread = self.listener()
        self.assertEqual(hint(), "sendable")
        self.assertFalse(core.hermes_listener_alive(now=time.time() + 600))
        thread.stop_flag.set()
        thread.join(2)
        beat = self.hermes / "crosstalk" / "heartbeat.json"
        beat.write_text(json.dumps({"pid": os.getpid(), "utc": "2020-01-01T00:00:00+00:00", "hosts_chats": True}))
        self.assertFalse(core.hermes_listener_alive())
        self.assertEqual(hint(), "blocked:not-running")
        beat.write_text("{not json")
        self.assertFalse(core.hermes_listener_alive())

    def test_heartbeat_from_a_non_hosting_process_does_not_count(self):
        (self.hermes / "crosstalk" / "heartbeat.json").write_text(
            json.dumps({"pid": os.getpid(), "utc": plugin._now(), "hosts_chats": False}))
        self.assertFalse(core.hermes_listener_alive())

    def test_resolve_accepts_key_or_bare_id_and_discovers_by_id(self):
        self.assertEqual(core.resolve_recipient("hermes:" + H1, "claude")["id"], H1)
        self.assertEqual(core.resolve_recipient(H2, "codex")["key"], "hermes:" + H2)
        with self.assertRaises(core.ProbeError) as raised:
            core.resolve_recipient("20261013_999999_ffffff", "claude")
        self.assertEqual(raised.exception.reason, "recipient-not-in-directory")
        with self.assertRaises(core.ProbeError) as raised:
            core.resolve_recipient("Planning chat", "claude")
        self.assertEqual(raised.exception.reason, "to-must-be-exact-id")

    def test_archived_chat_is_not_resolved(self):
        with self.assertRaises(core.ProbeError):
            core.resolve_recipient("hermes:" + H_ARCHIVED, "claude")

    def test_unreadable_state_db_is_unknown_not_absence(self):
        core.list_directory()
        (self.hermes / "state.db").write_bytes(b"this is not a database" * 100)
        result = core.hermes_discover()
        self.assertFalse(result["read"])
        self.assertIn("hermes:" + H1, core.directory_load()["entries"])
        self.assertIn("hermes:" + H1, {row["key"] for row in core.list_directory()})
        (self.hermes / "state.db").unlink()
        self.assertEqual(core.hermes_discover()["note"], "state-db-not-found")

    def test_discovery_stays_off_without_the_plugin(self):
        (self.hermes / "plugins" / "crosstalk").rmdir()
        for sub in ("inbox", "work", "done", "cancelled"):
            (self.hermes / "crosstalk" / sub).rmdir()
        (self.hermes / "crosstalk").rmdir()
        self.assertEqual(core.hermes_discover()["note"], "hermes-plugin-not-installed")
        self.assertEqual(core.directory_load()["entries"], {})

    def test_state_db_is_opened_read_only(self):
        connection = core.hermes_db_connect()
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("DELETE FROM sessions")
        connection.close()


class DeliveryTests(HermesCase):
    def test_accepted_then_arrived_by_readback(self):
        ctx, _ = self.listener(returns=True)
        core.list_directory()
        # The host shows the injected text as a user turn a moment after it accepts it.
        original = core.hermes_arrivals
        seen = []

        def arrivals(session, marker, timeout=2.0):
            if not seen:
                self.add_message(session, "assistant", "I mention ref " + marker + " but I am not a user row")
                self.add_message(session, "user", ctx.calls[0][0])
            seen.append(marker)
            return original(session, marker, timeout)

        with mock.patch.object(core, "hermes_arrivals", side_effect=arrivals):
            result = self.send_to_hermes()
        self.assertEqual(result["state"], "arrived")
        self.assertEqual(result["arrivals"], 1)
        self.assertTrue(result["sent"])
        self.assertEqual(self.states(result["letter_id"]), ["journaled", "sending", "accepted", "arrived"])
        self.assertEqual(self.letter(result["letter_id"])["route"], "hermes-inject")
        self.assertEqual(len(ctx.calls), 1)
        text, role, key = ctx.calls[0]
        self.assertEqual((role, key), ("user", H1))
        self.assertTrue(text.startswith("Crosstalk message from Claude ("))
        self.assertTrue(text.endswith("\nref " + result["letter_id"]))

    def test_busy_chat_is_held_then_promoted_by_readback(self):
        ctx, _ = self.listener(returns=True)
        result = self.send_to_hermes()
        self.assertEqual(result["state"], "held")
        self.assertTrue(result["transport_accepted"])
        self.assertEqual(result["reason"], "native-input-not-observed")
        self.assertEqual(core.reconcile(resend=False)["promoted"], [])
        self.add_message(H1, "user", ctx.calls[0][0])
        report = core.reconcile(resend=False)
        self.assertEqual([p["letter_id"] for p in report["promoted"]], [result["letter_id"]])
        self.assertEqual(self.letter(result["letter_id"])["state"], "arrived")
        self.assertEqual(len(ctx.calls), 1)  # never injected twice

    def test_returned_false_is_refused_chat_not_open(self):
        ctx, _ = self.listener(returns=False)
        result = self.send_to_hermes()
        self.assertEqual((result["state"], result["sent"], result["reason"]), ("refused", False, "hermes-chat-not-open"))
        self.assertIn("opens that Hermes chat", result["guidance"])
        self.assertTrue(result["report_to_owner"])
        self.assertEqual(self.states(result["letter_id"]), ["journaled", "sending", "refused"])
        self.assertEqual(core.reconcile()["resent"], [])  # terminal: never retried

    def test_no_listener_refuses_without_writing_an_inbox_file(self):
        result = self.send_to_hermes()
        self.assertEqual((result["state"], result["sent"], result["reason"]), ("refused", False, "hermes-not-running"))
        self.assertEqual(self.inbox_files(), [])

    def test_listener_that_stops_claiming_is_cancelled_and_refused(self):
        self.listener(mode="ignore")
        result = self.send_to_hermes()
        self.assertEqual((result["state"], result["sent"], result["reason"]), ("refused", False, "hermes-not-running"))
        self.assertEqual(self.inbox_files(), [])
        self.assertEqual([p.name for p in (self.hermes / "crosstalk" / "cancelled").glob("*.json")],
                         [result["letter_id"] + ".json"])

    def test_claimed_without_result_is_uncertain_and_never_resent(self):
        self.listener(mode="claim-only")
        result = self.send_to_hermes()
        self.assertEqual(result["state"], "uncertain")
        self.assertIsNone(result["sent"])
        self.assertEqual(result["reason"], "hermes-claimed-no-result")
        with mock.patch.object(core, "deliver_hermes") as leg:
            first = core.reconcile()
            second = core.reconcile()
        leg.assert_not_called()
        self.assertEqual(first["resent"] + second["resent"], [])
        self.assertEqual(self.letter(result["letter_id"])["state"], "uncertain")
        self.assertEqual(self.inbox_files(), [])
        # Only native readback promotes it.
        self.add_message(H1, "user", self.letter(result["letter_id"])["message"])
        core.reconcile()
        self.assertEqual(self.letter(result["letter_id"])["state"], "arrived")

    def test_inject_exception_is_uncertain(self):
        class Boom(FakeCtx):
            def inject_message(self, *args, **kwargs):
                raise RuntimeError("host exploded")

        thread = FakeListener(Boom())
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(thread.stop_flag.set)
        deadline = time.monotonic() + 3  # wait for the heartbeat, as listener() does: a slow start is not a refusal
        while not (self.hermes / "crosstalk" / "heartbeat.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        result = self.send_to_hermes()
        self.assertEqual(result["state"], "uncertain")
        self.assertIn("hermes-inject-outcome-unverified", result["reason"])

    def test_text_is_carried_byte_exact(self):
        ctx, _ = self.listener(returns=True)
        text = "line one\nline \u00e9\u4e2d\u6587 \"quoted\" 'single' $HOME %PATH% `tick`"
        result = self.send_to_hermes(text=text)
        self.assertEqual(result["state"], "held")
        self.assertIn("\n" + text + "\nref ", ctx.calls[0][0])

    def test_only_hermes_chats_are_hermes_recipients_and_hermes_to_hermes_is_refused(self):
        core.list_directory()
        result = core.send_letter("hermes", H2, "hermes:" + H1, "hi")
        self.assertEqual((result["state"], result["reason"], result["sent"]), ("refused", "same-app-recipient", False))


class ControlsTests(HermesCase):
    def test_core_controls_refuse_before_any_inbox_file(self):
        self.listener()
        core.list_directory()
        for change, reason in (({"halt": True}, "halted"), ({"halt": False, "messaging": "off"}, "messaging-off")):
            core.controls_update(**change)
            result = self.send_to_hermes()
            self.assertEqual((result["state"], result["reason"], result["sent"]), ("refused", reason, False))
        core.controls_update(messaging="on", deny_add="hermes:" + H1)
        result = self.send_to_hermes()
        self.assertEqual((result["state"], result["reason"]), ("refused", "denied"))
        self.assertEqual(self.inbox_files(), [])

    def test_listener_rechecks_controls_before_injecting(self):
        ctx, _ = self.listener(returns=True)
        # The core's own checks pass, then the owner turns messaging off before the plugin injects.
        with mock.patch.object(core, "controls_check", return_value=None):
            core.controls_update(messaging="off")
            result = self.send_to_hermes()
        self.assertEqual((result["state"], result["reason"], result["sent"]), ("refused", "messaging-off", False))
        self.assertEqual(ctx.calls, [])

    def test_listener_control_rules_mirror_the_core(self):
        path = self.device / "controls.json"
        key = "hermes:" + H1
        cases = [({"messaging": "on"}, None), ({"messaging": "on", "halt": True}, "halted"),
                 ({"messaging": "off"}, "messaging-off"), ({}, "messaging-off"),
                 ({"messaging": "on", "deny": [key]}, "denied"), ({"messaging": "on", "deny": ["x"]}, None),
                 ({"messaging": "on", "halt": "yes"}, None)]
        for controls, expected in cases:
            path.write_text(json.dumps(controls))
            self.assertEqual(plugin.controls_refusal(key, str(path)), expected, controls)
            self.assertEqual(core.controls_check(key), expected, controls)
        path.unlink()
        self.assertEqual(plugin.controls_refusal(key, str(path)), "messaging-off")
        self.assertEqual(plugin.controls_refusal(key, None), "messaging-off")
        path.write_text("{not json")
        self.assertEqual(plugin.controls_refusal(key, str(path)), "messaging-off")

    def test_handle_writes_a_refusal_result_and_does_not_inject(self):
        core.controls_update(halt=True)
        ctx = FakeCtx()
        inbox = self.hermes / "crosstalk" / "inbox" / "CTP0-abcdefabcdef.json"
        inbox.write_text(json.dumps({"letter_id": "CTP0-abcdefabcdef", "session_key": H1,
                                     "text": core.frame_message("CTP0-abcdefabcdef", "claude", "Chat", "x")}))
        plugin._handle(ctx, inbox)
        result = json.loads((self.hermes / "crosstalk" / "done" / "CTP0-abcdefabcdef.result.json").read_text())
        self.assertEqual((result["returned"], result["refused"]), (False, "halted"))
        self.assertEqual(ctx.calls, [])
        self.assertFalse(inbox.exists())

    def test_handle_rejects_a_malformed_session_key(self):
        ctx = FakeCtx()
        inbox = self.hermes / "crosstalk" / "inbox" / "CTP0-000000000000.json"
        inbox.write_text(json.dumps({"session_key": "../../etc", "text": "x"}))
        plugin._handle(ctx, inbox)
        result = json.loads((self.hermes / "crosstalk" / "done" / "CTP0-000000000000.result.json").read_text())
        self.assertIsNone(result["returned"])
        self.assertIn("error", result)
        self.assertEqual(ctx.calls, [])


class FakeServer:
    """The slice of tui_gateway.server the plugin uses to load a closed chat."""

    def __init__(self, reply=None, raises=None):
        self.reply, self.raises, self.calls, self.transports = reply, raises, [], []
        self._detached_ws_transport = object()
        self._methods = {"session.resume": self.resume}
        self._sessions = {"ab": {"agent": object()}}

    def resume(self, rid, params):
        self.calls.append((rid, params))
        self.transports.append(self.current)
        if self.raises:
            raise self.raises
        return self.reply


class ResumeTests(HermesCase):
    """A letter to a chat that is not live in the backend: load it detached, then inject (docs/Crosstalk README)."""

    def setUp(self):
        super().setUp()
        self.bound = []
        transport = SimpleNamespace(
            bind_transport=lambda sink: (self.bound.append(sink), sink)[1],
            reset_transport=lambda token: self.bound.append("reset"))
        self.server = FakeServer(reply={"jsonrpc": "2.0", "id": "x", "result": {"session_id": "ab", "resumed": H1}})
        self.server.current = None
        modules = mock.patch.dict("sys.modules", {"tui_gateway": SimpleNamespace(), "tui_gateway.server": self.server,
                                                 "tui_gateway.transport": transport})
        modules.start()
        self.addCleanup(modules.stop)

    def run_handle(self, ctx, letter="CTP0-abcdefabcdef", key=H1):
        inbox = self.hermes / "crosstalk" / "inbox" / (letter + ".json")
        inbox.write_text(json.dumps({"letter_id": letter, "session_key": key,
                                     "text": core.frame_message(letter, "claude", "Chat", "x")}))
        plugin._handle(ctx, inbox)
        return json.loads((self.hermes / "crosstalk" / "done" / (letter + ".result.json")).read_text())

    def test_closed_chat_is_resumed_detached_and_then_injected(self):
        ctx = FakeCtx()
        answers = iter([False, True])
        ctx.inject_message = lambda text, role, session_key=None: (ctx.calls.append(session_key), next(answers))[1]
        result = self.run_handle(ctx)
        self.assertEqual((result["returned"], result["resumed"]), (True, True))
        self.assertNotIn("refused", result)
        self.assertEqual(ctx.calls, [H1, H1])
        rid, params = self.server.calls[0]
        self.assertEqual(params, {"session_id": H1, "source": "desktop", "eager_build": True, "omit_messages": True})
        self.assertEqual(self.bound, [self.server._detached_ws_transport, "reset"])

    def test_open_chat_is_not_resumed(self):
        result = self.run_handle(FakeCtx(returns=True))
        self.assertEqual(result["returned"], True)
        self.assertNotIn("resumed", result)
        self.assertEqual(self.server.calls, [])

    def test_resume_error_is_a_proven_refusal(self):
        self.server.reply = {"jsonrpc": "2.0", "id": "x", "error": {"code": 4007, "message": "session not found"}}
        ctx = FakeCtx(returns=False)
        result = self.run_handle(ctx)
        self.assertEqual((result["returned"], result["refused"]), (False, "hermes-resume-failed:code=4007"))
        self.assertEqual(len(ctx.calls), 1)  # never injected after a failed load

    def test_resume_exception_is_a_proven_refusal(self):
        self.server.raises = RuntimeError("boom")
        ctx = FakeCtx(returns=False)
        result = self.run_handle(ctx)
        self.assertEqual(result["refused"], "hermes-resume-failed:RuntimeError")
        self.assertEqual(len(ctx.calls), 1)

    def test_resume_that_followed_a_tip_is_refused(self):
        self.server.reply = {"jsonrpc": "2.0", "id": "x", "result": {"session_id": "ab", "resumed": H2}}
        result = self.run_handle(FakeCtx(returns=False))
        self.assertEqual(result["refused"], "hermes-resume-id-mismatch")

    def test_resume_without_an_agent_is_a_proven_refusal(self):
        self.server._sessions["ab"] = {"agent": None}
        ctx = FakeCtx(returns=False)
        result = self.run_handle(ctx)
        self.assertEqual((result["returned"], result["refused"]), (False, "hermes-resume-no-agent"))
        self.assertEqual(len(ctx.calls), 1)  # an inject now would be accepted, then refused by the turn gate

    def test_resume_waits_for_an_agent_still_building(self):
        session = {"agent": None}
        session["agent_ready"] = SimpleNamespace(wait=lambda timeout: session.update(agent=object()))
        self.server._sessions["ab"] = session
        ctx = FakeCtx()
        answers = iter([False, True])
        ctx.inject_message = lambda text, role, session_key=None: next(answers)
        result = self.run_handle(ctx)
        self.assertEqual((result["returned"], result["resumed"]), (True, True))

    def test_second_inject_false_stays_not_open(self):
        ctx = FakeCtx(returns=False)
        result = self.run_handle(ctx)
        self.assertEqual((result["returned"], result["resumed"]), (False, True))
        self.assertNotIn("refused", result)
        self.assertEqual(len(ctx.calls), 2)

    def test_second_inject_exception_is_uncertain(self):
        ctx = FakeCtx()
        answers = iter([False, RuntimeError("late")])

        def inject(text, role, session_key=None):
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value
        ctx.inject_message = inject
        result = self.run_handle(ctx)
        self.assertIsNone(result["returned"])
        self.assertIn("RuntimeError", result["error"])

    def test_controls_changed_during_resume_block_the_second_inject(self):
        ctx = FakeCtx(returns=False)
        original = self.server.resume

        def resume(rid, params):
            core.controls_update(halt=True)
            return original(rid, params)
        self.server._methods["session.resume"] = resume
        result = self.run_handle(ctx)
        self.assertEqual(result["refused"], "halted")
        self.assertEqual(len(ctx.calls), 1)

    def test_no_gateway_module_keeps_the_chat_not_open(self):
        with mock.patch.dict("sys.modules", {"tui_gateway.server": None}):
            result = self.run_handle(FakeCtx(returns=False))
        self.assertEqual(result["returned"], False)
        self.assertNotIn("refused", result)
        self.assertEqual(result["resume_note"], "no-gateway-module")

    def test_end_to_end_closed_chat_letter_is_accepted(self):
        ctx = FakeCtx()
        answers = iter([False, True])
        ctx.inject_message = lambda text, role, session_key=None: next(answers)
        self.listener_with(ctx)
        core.list_directory()
        result = self.send_to_hermes()
        self.assertIn(result["state"], ("accepted", "held", "arrived"))  # no state.db row to read back here
        self.assertIn("accepted", self.states(result["letter_id"]))

    def listener_with(self, ctx):
        thread = FakeListener(ctx)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(thread.stop_flag.set)
        deadline = time.monotonic() + 3
        while not (self.hermes / "crosstalk" / "heartbeat.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.01)


class BusyChatTests(HermesCase):
    """A letter to a chat whose turn is running is parked, never injected, and injected once when it is idle."""

    LETTER = "CTP0-aaaaaaaaaaaa"

    def setUp(self):
        super().setUp()
        self.session = {"session_key": H1, "running": True, "agent": object()}
        self.server = SimpleNamespace(_sessions={"ui1": self.session}, _sessions_lock=threading.Lock())
        modules = mock.patch.dict("sys.modules", {"tui_gateway": SimpleNamespace(), "tui_gateway.server": self.server})
        modules.start()
        self.addCleanup(modules.stop)
        self.base = self.hermes / "crosstalk"

    def drop(self, letter=None, key=H1):
        letter = letter or self.LETTER
        path = self.base / "inbox" / (letter + ".json")
        path.write_text(json.dumps({"letter_id": letter, "session_key": key,
                                    "text": core.frame_message(letter, "claude", "Chat", "x")}))
        return path

    def result(self, letter=None):
        return json.loads((self.base / "done" / ((letter or self.LETTER) + ".result.json")).read_text())

    def test_busy_chat_is_parked_with_a_deferred_result_and_no_inject(self):
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop())
        self.assertEqual(ctx.calls, [])
        self.assertTrue((self.base / "waiting" / (self.LETTER + ".json")).is_file())
        self.assertEqual(self.inbox_files(), [])
        result = self.result()
        self.assertEqual((result["deferred"], result["returned"]), ("session-busy", None))
        self.assertNotIn("refused", result)

    def test_parked_letter_is_injected_once_when_idle_and_not_before(self):
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop())
        self.assertEqual(plugin._waiting_pass(ctx, self.base), 1)
        self.assertEqual(ctx.calls, [])
        self.session["running"] = False
        self.assertEqual(plugin._waiting_pass(ctx, self.base), 0)
        self.assertEqual(plugin._waiting_pass(ctx, self.base), 0)
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(ctx.calls[0][2], H1)
        self.assertEqual(self.result()["returned"], True)
        self.assertTrue((self.base / "done" / (self.LETTER + ".json")).is_file())

    def test_parked_letters_go_in_send_order_not_letter_id_order(self):
        # Live gauntlet 2026-10-21: letter 3 (CTP0-6123...) went in before letter 2 (CTP0-f26f...) on a name sort.
        ctx = FakeCtx()
        older, newer = "CTP0-ffffffffffff", "CTP0-111111111111"
        for letter, stamp in ((older, time.time() - 20), (newer, time.time() - 10)):
            path = self.drop(letter)
            os.utime(path, (stamp, stamp))
            plugin._handle(ctx, path)
        self.session["running"] = False
        plugin._waiting_pass(ctx, self.base)
        self.assertEqual([next(i for i in (older, newer) if i in call[0]) for call in ctx.calls], [older, newer])

    # ---- mid-turn delivery through the transform_tool_result hook (plugin 0.3.0)
    def park(self, *letters):
        ctx = FakeCtx()
        for n, letter in enumerate(letters):
            path = self.drop(letter)
            stamp = time.time() - 100 + n
            os.utime(path, (stamp, stamp))
            plugin._handle(ctx, path)
        return ctx

    def hook(self, session=H1, result="tool output"):
        return plugin._transform_tool_result(tool_name="terminal", result=result, session_id=session)

    def test_hook_delivers_a_parked_letter_once_and_the_waiting_pass_does_not_repeat_it(self):
        ctx = self.park(self.LETTER)
        out = self.hook()
        self.assertTrue(out.startswith("tool output"))
        self.assertIn(plugin.MIDTURN_HEAD, out)
        self.assertIn("Crosstalk message from ", out)
        self.assertIn("\nref " + self.LETTER, out)
        self.assertFalse((self.base / "waiting" / (self.LETTER + ".json")).exists())
        self.assertTrue((self.base / "done" / (self.LETTER + ".json")).is_file())
        self.assertEqual(self.result()["returned"], True)
        self.assertIsNone(self.hook())  # nothing left: a second tool call gets nothing
        self.session["running"] = False
        plugin._waiting_pass(ctx, self.base)
        self.assertEqual(ctx.calls, [])

    def test_hook_on_another_session_leaves_the_letter_parked(self):
        self.park(self.LETTER)
        self.assertIsNone(self.hook(session=H2))
        self.assertTrue((self.base / "waiting" / (self.LETTER + ".json")).is_file())

    def test_hook_failure_leaves_the_letter_parked(self):
        self.park(self.LETTER)
        self.assertIsNone(self.hook(result={"not": "a string"}))
        with mock.patch.object(plugin, "_write_json", side_effect=OSError("disk")):
            self.assertIsNone(self.hook())
        self.assertTrue((self.base / "waiting" / (self.LETTER + ".json")).is_file())
        self.assertFalse(list((self.base / "work").glob("*.hook*.json")))

    def test_hook_refuses_nothing_when_controls_halt_and_leaves_it_for_the_waiting_pass(self):
        self.park(self.LETTER)
        core.controls_update(halt=True)
        self.assertIsNone(self.hook())
        self.assertTrue((self.base / "waiting" / (self.LETTER + ".json")).is_file())

    def test_hook_delivers_several_letters_in_send_order(self):
        older, newer = "CTP0-ffffffffffff", "CTP0-111111111111"
        self.park(older, newer)
        out = self.hook()
        self.assertLess(out.index("ref " + older), out.index("ref " + newer))
        self.assertEqual(list((self.base / "waiting").glob("*.json")), [])

    def test_a_letter_delivered_in_a_tool_result_is_counted_as_one_arrival(self):
        self.park(self.LETTER)
        out = self.hook()
        self.add_message(H1, "tool", out)  # Hermes stores the transformed result as a tool row
        self.add_message(H1, "tool", "plain output mentioning ref " + self.LETTER)  # not a letter block
        self.assertEqual(core.hermes_arrivals(H1, self.LETTER), (1, "state-db-read"))

    def test_registers_the_hook_on_the_host(self):
        ctx = FakeCtx()
        ctx.register_hook = mock.Mock()
        ctx.register_tool = mock.Mock()
        with mock.patch.object(plugin.threading, "Thread"):
            plugin.register(ctx)
        ctx.register_hook.assert_called_once_with("transform_tool_result", plugin._transform_tool_result)

    def test_a_queued_prompt_counts_as_busy_so_letters_are_serialized(self):
        ctx = FakeCtx()
        self.session["running"] = False
        self.session["queued_prompt"] = {"text": "earlier letter"}
        plugin._handle(ctx, self.drop())
        self.assertEqual(ctx.calls, [])
        self.session["queued_prompt"] = None
        plugin._waiting_pass(ctx, self.base)
        self.assertEqual(len(ctx.calls), 1)

    def test_idle_chat_is_injected_at_once_and_other_chats_do_not_block(self):
        ctx = FakeCtx()
        self.session["running"] = False
        self.server._sessions["ui2"] = {"session_key": H2, "running": True}
        plugin._handle(ctx, self.drop())
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(self.result()["returned"], True)

    def test_controls_are_rechecked_before_the_deferred_inject(self):
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop())
        self.session["running"] = False
        core.controls_update(halt=True)
        plugin._waiting_pass(ctx, self.base)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(self.result()["refused"], "halted")

    def test_halt_while_still_busy_cancels_the_parked_letter(self):
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop())
        core.controls_update(halt=True)
        plugin._waiting_pass(ctx, self.base)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(self.result()["refused"], "halted")
        self.assertFalse((self.base / "waiting" / (self.LETTER + ".json")).exists())

    def test_a_letter_waiting_too_long_is_cancelled_not_injected(self):
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop())
        with mock.patch.object(plugin, "WAIT_MAX_S", -1.0):
            plugin._waiting_pass(ctx, self.base)
        self.assertEqual(ctx.calls, [])
        self.assertEqual(self.result()["refused"], "hermes-busy-timeout")

    def test_end_to_end_busy_chat_is_held_then_arrives_once(self):
        ctx = FakeCtx()
        thread = FakeListener(ctx, mode="full")
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(thread.stop_flag.set)
        deadline = time.monotonic() + 3
        while not (self.base / "heartbeat.json").is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        core.list_directory()
        result = self.send_to_hermes()
        self.assertEqual(result["state"], "held")
        self.assertTrue(result["transport_accepted"])
        self.assertIn("QUEUED, NOT LOST", result["guidance"])
        self.assertEqual(ctx.calls, [])
        self.session["running"] = False
        deadline = time.monotonic() + 3
        while not ctx.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        self.assertEqual(len(ctx.calls), 1)  # exactly one inject
        self.add_message(H1, "user", ctx.calls[0][0])
        report = core.reconcile(resend=False)
        self.assertEqual([p["letter_id"] for p in report["promoted"]], [result["letter_id"]])
        self.assertEqual(len(ctx.calls), 1)


class LetterFramingTests(HermesCase):
    """Finding 2 (review 2026-10-14): the plugin must never inject text that is not a framed Crosstalk letter."""

    LETTER = "CTP0-0123456789ab"

    def drop(self, text, letter_id=LETTER, session=H1):
        inbox = self.hermes / "crosstalk" / "inbox" / (self.LETTER + ".json")
        message = {"session_key": session, "text": text}
        if letter_id is not None:
            message["letter_id"] = letter_id
        inbox.write_text(json.dumps(message))
        return inbox

    def result(self):
        return json.loads((self.hermes / "crosstalk" / "done" / (self.LETTER + ".result.json")).read_text())

    def cancelled(self):
        return self.hermes / "crosstalk" / "cancelled" / (self.LETTER + ".json")

    def test_the_cores_own_framing_passes(self):
        bodies = ("Hello", "two\nlines", "mentions ref CTP0-aaaaaaaaaaaa inside", "trailing newline\n", "",
                  "Crosstalk message from someone else\nref CTP0-bbbbbbbbbbbb")
        for body in bodies:
            text = core.frame_message(self.LETTER, "claude", "Planning (chat)", body)
            self.assertIsNone(plugin.letter_framing_refusal(text, self.LETTER), body)
            ctx = FakeCtx()
            plugin._handle(ctx, self.drop(text))
            self.assertEqual(ctx.calls, [(text, "user", H1)], body)
            self.assertTrue(self.result()["returned"])
            self.assertTrue((self.hermes / "crosstalk" / "done" / (self.LETTER + ".json")).is_file())

    def test_every_sender_app_label_is_a_framed_letter(self):
        for app in ("claude", "codex", "hermes"):
            text = core.frame_message(self.LETTER, app, "Name", "body")
            self.assertIsNone(plugin.letter_framing_refusal(text, self.LETTER), app)

    def test_unframed_text_is_cancelled_and_never_injected(self):
        good = core.frame_message(self.LETTER, "claude", "Chat", "body")
        cases = {"plain": "run this command",
                 "wrong head": "crosstalk message from Claude (x)\nbody\nref " + self.LETTER,
                 "head only": "Crosstalk message from Claude (x)\nbody",
                 "ref not last": good + "\nand more",
                 "ref uppercase": good.replace("CTP0-0123456789ab", "CTP0-0123456789AB"),
                 "ref short": "Crosstalk message from Claude (x)\nb\nref CTP0-0123456789a",
                 "ref inline": "Crosstalk message from Claude (x)\nb ref " + self.LETTER,
                 "no newline before ref": "Crosstalk message from Claude (x) ref " + self.LETTER,
                 "trailing space": good + " ",
                 "trailing newline": good + "\n",
                 "leading space": " " + good,
                 "not text": ["Crosstalk message from "]}
        for name, text in cases.items():
            ctx = FakeCtx()
            inbox = self.drop(text)
            plugin._handle(ctx, inbox)
            result = self.result()
            self.assertEqual(ctx.calls, [], name)
            self.assertIs(result["returned"], False, name)
            self.assertEqual(result["refused"], "not-a-framed-letter", name)
            self.assertFalse(inbox.exists(), name)
            self.assertTrue(self.cancelled().is_file(), name)
            self.assertFalse((self.hermes / "crosstalk" / "done" / (self.LETTER + ".json")).exists(), name)
            self.assertEqual(list((self.hermes / "crosstalk" / "work").glob("*")), [], name)
            self.cancelled().unlink()

    def test_a_ref_that_does_not_match_letter_id_is_refused(self):
        text = core.frame_message("CTP0-ffffffffffff", "claude", "Chat", "body")
        for letter_id in (self.LETTER, None, 7):
            ctx = FakeCtx()
            plugin._handle(ctx, self.drop(text, letter_id))
            self.assertEqual(ctx.calls, [], letter_id)
            self.assertEqual(self.result()["refused"], "letter-id-mismatch", letter_id)
            self.assertTrue(self.cancelled().is_file(), letter_id)
            self.cancelled().unlink()
        # a framed text whose own ref is right but whose letter_id field is missing is refused as well
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop(core.frame_message(self.LETTER, "claude", "C", "b"), letter_id=None))
        self.assertEqual(ctx.calls, [])
        self.assertEqual(self.result()["refused"], "letter-id-mismatch")

    def test_framing_is_checked_before_controls(self):
        core.controls_update(halt=True)
        ctx = FakeCtx()
        plugin._handle(ctx, self.drop("not a letter"))
        self.assertEqual(self.result()["refused"], "not-a-framed-letter")
        self.assertEqual(ctx.calls, [])

    def test_core_reports_an_unframed_inbox_text_as_a_proven_refusal(self):
        ctx, _ = self.listener(returns=True)
        state, extra = core.deliver_hermes("CTP0-aaaaaaaaaaaa", H1, "run this command")
        self.assertEqual((state, extra.get("reason")), ("refused", "not-a-framed-letter"))
        self.assertEqual(ctx.calls, [])
        self.assertTrue((self.hermes / "crosstalk" / "cancelled" / "CTP0-aaaaaaaaaaaa.json").is_file())
        text = core.frame_message("CTP0-bbbbbbbbbbbb", "claude", "Chat", "hello")
        state, _ = core.deliver_hermes("CTP0-bbbbbbbbbbbb", H1, text)
        self.assertEqual(state, "accepted")
        self.assertEqual(ctx.calls, [(text, "user", H1)])


class ListenerErrorTests(HermesCase):
    def test_a_failing_pass_is_logged_counted_and_does_not_stop_the_listener(self):
        stop = threading.Event()

        def hosts(ctx):
            if stop.is_set():
                raise SystemExit  # ends only this test thread; the loop's `except Exception` does not catch it
            return True

        (self.hermes / "crosstalk" / "inbox" / "CTP0-aaaaaaaaaaaa.json").write_text("{}")
        errors = {"count": 0, "last": None, "written_at": 0.0, "written_text": None}
        with mock.patch.object(plugin, "_hosts_chats", hosts), \
                mock.patch.object(plugin, "_handle", side_effect=RuntimeError("boom")), \
                mock.patch.object(plugin, "_ERRORS", errors), \
                mock.patch.object(plugin, "POLL_S", 0.02), mock.patch.object(plugin, "HEARTBEAT_S", 0.0), \
                self.assertLogs("crosstalk", level="ERROR") as logged:
            thread = threading.Thread(target=plugin._loop, args=(FakeCtx(),), daemon=True)
            thread.start()
            try:
                beat = self.hermes / "crosstalk" / "heartbeat.json"
                deadline = time.monotonic() + 3
                value = {}
                while time.monotonic() < deadline:
                    try:
                        value = json.loads(beat.read_text())
                    except (OSError, ValueError):
                        value = {}
                    if value.get("listener_errors", 0) >= 2:
                        break
                    time.sleep(0.02)
                self.assertTrue(thread.is_alive())
            finally:
                stop.set()
                thread.join(2)
        self.assertGreaterEqual(value.get("listener_errors", 0), 2, value)
        self.assertEqual(value["last_error"], "RuntimeError: boom")
        self.assertTrue(any("boom" in line for line in logged.output))
        report = json.loads((self.hermes / "crosstalk" / "listener-error.json").read_text())
        self.assertEqual(report["last_error"], "RuntimeError: boom")
        self.assertGreaterEqual(report["count"], 1)


class SenderFramingTests(HermesCase):
    def test_hermes_to_claude_uses_the_hermes_title(self):
        core.directory_register("claude", CLAUDE, {})
        core.register_hermes(H1)
        with mock.patch.object(core, "block_reason", return_value=None), \
                mock.patch.object(core, "refresh_binding_locked"), \
                mock.patch.object(core, "deliver_claude", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "complete-scan")):
            result = core.send_letter("hermes", H1, "claude:" + CLAUDE, "From Hermes")
        self.assertEqual(result["state"], "arrived")
        message = leg.call_args[0][2]
        self.assertTrue(message.startswith("Crosstalk message from Hermes (Planning chat)\nFrom Hermes\nref CTP0-"))

    def test_hermes_to_codex_and_untitled_chat_falls_back_to_its_key(self):
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        core.register_hermes(H2)
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT}), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "thread-read")):
            result = core.send_letter("hermes", H2, "codex:" + CODEX, "Hi Codex")
        self.assertEqual(result["state"], "arrived")
        self.assertTrue(leg.call_args[0][1].startswith("Crosstalk message from Hermes (hermes:" + H2 + ")\n"))

    def test_register_hermes_rejects_bad_ids(self):
        for bad in ("", "abc", CLAUDE, "20261013_230248_6A7BF5", None):
            with self.assertRaises(core.ProbeError):
                core.register_hermes(bad)


class CliTests(HermesCase):
    def run_cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, json.loads(buffer.getvalue())

    def test_status_registers_the_caller_with_its_title(self):
        code, value = self.run_cli("hermes-status", "--session", H1)
        self.assertEqual(code, 0)
        self.assertTrue(value["ok"])
        self.assertEqual(value["registered"]["key"], "hermes:" + H1)
        self.assertEqual(value["registered"]["title"], "Planning chat")
        self.assertFalse(value["hermes"]["listener_alive"])
        self.assertIn("controls", value)
        self.assertIn("reconcile_readback", value)

    def test_status_with_a_bad_session_id_fails(self):
        code, value = self.run_cli("hermes-status", "--session", "nope")
        self.assertEqual((code, value["ok"], value["reason"]), (1, False, "invalid-session-id"))

    def test_list_marks_self(self):
        code, value = self.run_cli("hermes-list", "--session", H1)
        rows = {row["key"]: row for row in value["entries"]}
        self.assertTrue(rows["hermes:" + H1]["self"])
        self.assertFalse(rows["hermes:" + H2].get("self", False))

    def test_send_goes_through_a_utf8_file_to_a_claude_chat(self):
        core.directory_register("claude", CLAUDE, {})
        text_file = self.root / "letter.txt"
        text_file.write_text("Gr\u00fc\u00dfe \u4e2d\u6587\nsecond line", encoding="utf-8")
        with mock.patch.object(core, "block_reason", return_value=None), \
                mock.patch.object(core, "refresh_binding_locked"), \
                mock.patch.object(core, "deliver_claude", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "complete-scan")):
            code, value = self.run_cli("hermes-send", "--session", H1, "--to", "claude:" + CLAUDE,
                                       "--text-file", str(text_file))
        self.assertEqual((code, value["state"]), (0, "arrived"))
        self.assertIn("Gr\u00fc\u00dfe \u4e2d\u6587\nsecond line", leg.call_args[0][2])
        self.assertIn("(Planning chat)", leg.call_args[0][2])

    def test_send_to_another_hermes_chat_is_refused(self):
        text_file = self.root / "letter.txt"
        text_file.write_text("hi", encoding="utf-8")
        code, value = self.run_cli("hermes-send", "--session", H1, "--to", "hermes:" + H2, "--text-file", str(text_file))
        self.assertEqual((value["state"], value["reason"], value["sent"]), ("refused", "same-app-recipient", False))


class PluginTests(HermesCase):
    CONFIG = {"python": "C:/rt/python.exe", "cli": "C:/kit/crosstalk_p0_cli.py", "device_dir": ""}

    def setUp(self):
        super().setUp()
        config = dict(self.CONFIG, device_dir=str(self.device))
        patcher = mock.patch.object(plugin, "_config", return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, tool, params, **kwargs):
        captured = {}

        def fake_run(argv, **options):
            captured["argv"], captured["options"] = list(argv), options
            if "--text-file" in argv:
                captured["text"] = Path(argv[argv.index("--text-file") + 1]).read_bytes().decode("utf-8")
                captured["text_path"] = argv[argv.index("--text-file") + 1]
            return SimpleNamespace(returncode=0, stdout='noise\n{"ok": true, "state": "arrived"}\n', stderr="")

        handler = plugin._handler(tool)
        with mock.patch.object(plugin.subprocess, "run", side_effect=fake_run):
            out = handler(params, **kwargs)
        return out, captured

    def test_register_declares_four_tools_and_a_single_listener(self):
        tools = []
        ctx = SimpleNamespace(register_tool=lambda **kw: tools.append(kw), _manager=SimpleNamespace(has_tui_message_injector=False))
        with mock.patch.object(plugin.threading, "Thread") as thread:
            plugin.register(ctx)
        self.assertEqual([t["name"] for t in tools], ["crosstalk_status", "crosstalk_list", "crosstalk_send", "crosstalk_create"])
        self.assertEqual({t["toolset"] for t in tools}, {"crosstalk"})
        send = [t for t in tools if t["name"] == "crosstalk_send"][0]
        self.assertEqual(send["schema"]["parameters"]["required"], ["to", "text"])
        self.assertIn("never titles", send["schema"]["description"])
        self.assertIn("exact sender key", send["schema"]["description"])
        self.assertIn("end your turn", send["schema"]["description"])
        self.assertIn("full letter text word for word", send["schema"]["description"])
        thread.assert_called_once()

    def test_send_builds_the_cli_argv_with_a_temp_text_file(self):
        out, seen = self.call("crosstalk_send", {"to": "claude:" + CLAUDE, "text": "caf\u00e9 \u4e2d\u6587 \"q\""},
                              session_id=H1, task_id="t1")
        result = json.loads(out)
        self.assertEqual((result["ok"], result["state"]), (True, "arrived"))
        self.assertEqual(result["letter_text"], "café 中文 \"q\"")  # shown to the owner in the chat
        self.assertIn("word for word", result["show_owner"])
        argv = seen["argv"]
        self.assertEqual(argv[:5], ["C:/rt/python.exe", "-I", "-B", "C:/kit/crosstalk_p0_cli.py", "hermes-send"])
        self.assertEqual(argv[5:9], ["--session", H1, "--to", "claude:" + CLAUDE])
        self.assertEqual(argv[9], "--text-file")
        self.assertEqual(seen["text"], "caf\u00e9 \u4e2d\u6587 \"q\"")
        self.assertNotIn("caf", " ".join(argv[:9]))  # the text never rides on the command line
        self.assertFalse(os.path.exists(seen["text_path"]))
        self.assertEqual(seen["options"]["timeout"], 90)
        self.assertEqual(seen["options"]["env"]["CROSSTALK_P0_DEVICE_DIR"], str(self.device))
        self.assertEqual(seen["options"]["env"]["CROSSTALK_P0_HERMES_HOME"], str(self.hermes))
        self.assertEqual(seen["options"]["creationflags"], plugin.NO_WINDOW)

    def test_status_and_list_argv(self):
        _, seen = self.call("crosstalk_status", {}, session_id=H1)
        self.assertEqual(seen["argv"][4:], ["hermes-status", "--session", H1])
        _, seen = self.call("crosstalk_list", {}, session_id=H1)
        self.assertEqual(seen["argv"][4:], ["hermes-list", "--session", H1])
        out, _ = self.call("crosstalk_status", {}, session_id=H1)
        self.assertNotIn("letter_text", json.loads(out))

    def test_missing_or_bad_session_id_never_runs_the_cli(self):
        with mock.patch.object(plugin.subprocess, "run") as run:
            for kwargs in ({}, {"session_id": None}, {"session_id": "../x"}):
                out = plugin._handler("crosstalk_status")({}, **kwargs)
                self.assertEqual(json.loads(out)["reason"], "no-hermes-session-id")
            out = plugin._handler("crosstalk_send")({"to": 5, "text": "x"}, session_id=H1)
            self.assertEqual(json.loads(out)["reason"], "invalid-arguments")
        run.assert_not_called()

    def test_unconfigured_plugin_reports_it(self):
        with mock.patch.object(plugin, "_config", return_value={}):
            out = plugin._handler("crosstalk_list")({}, session_id=H1)
        self.assertEqual(json.loads(out)["reason"], "plugin-not-configured")

    def test_cli_timeout_is_uncertain_never_refused(self):
        with mock.patch.object(plugin.subprocess, "run", side_effect=plugin.subprocess.TimeoutExpired("x", 90)):
            out = plugin._handler("crosstalk_send")({"to": "claude:" + CLAUDE, "text": "x"}, session_id=H1)
        value = json.loads(out)
        self.assertEqual((value["state"], value["sent"]), ("uncertain", None))
        self.assertIn("Do NOT resend", value["guidance"])


class InstallerTests(HermesCase):
    def setUp(self):
        super().setUp()
        self.target_home = self.root / "hermes-install"
        (self.target_home / "bin").mkdir(parents=True)
        (self.target_home / "bin" / "hermes.cmd").write_text("@echo off\n")
        (self.target_home / "config.yaml").write_text("model: x\n")
        self.python = self.root / "python.exe"
        self.python.write_text("")
        self.calls = []

    def runner(self, command, **options):
        self.calls.append((command, options))
        return SimpleNamespace(returncode=0, stderr="")

    def tree(self, folder):
        return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*"))

    def test_plan_touches_nothing_and_names_the_exact_steps(self):
        before = self.tree(self.target_home)
        plan = installer.hermes_plan(self.target_home, str(self.python))
        self.assertEqual(self.tree(self.target_home), before)
        self.assertTrue(plan["hermes_cli_exists"] and plan["python_exists"])
        self.assertFalse(plan["installed"])
        commands = [step["command"] for step in plan["install_steps"] if "command" in step]
        cli_path = str(self.target_home / "bin" / "hermes.cmd")
        self.assertEqual(commands, [[cli_path, "plugins", "enable", "crosstalk"],
                                    [cli_path, "config", "set",
                                     "plugins.entries.crosstalk.allow_gateway_injection", "true"]])
        self.assertEqual(plan["remove_steps"][0]["command"], [cli_path, "plugins", "disable", "crosstalk"])

    def test_a_kit_install_points_hermes_at_the_stable_launcher(self):
        home = self.root / "eh"
        launcher = home / "bin" / ("eutonos-crosstalk.exe" if os.name == "nt" else "eutonos-crosstalk")
        launcher.parent.mkdir(parents=True)
        launcher.write_text("")
        with mock.patch.object(installer.core, "kit_mode", return_value=True),                 mock.patch.object(installer.core, "eutonos_home", return_value=home):
            report = installer.hermes_install(self.target_home, None, runner=self.runner)
        self.assertTrue(report["ok"], report)
        config = json.loads((self.target_home / "plugins" / "crosstalk" / "crosstalk.json").read_text())
        self.assertEqual((config["python"], config["cli"]), (str(launcher), "-"))
        argv = plugin.build_argv(config, "crosstalk_status", "S", {})
        self.assertEqual(argv, [str(launcher), "hermes-status", "--session", "S"])

    def test_install_backs_up_copies_writes_config_and_enables(self):
        report = installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertTrue(report["ok"], report)
        backup = self.target_home / "config.yaml.bak-crosstalk"
        self.assertEqual(backup.read_text(), "model: x\n")
        plugin_dir = self.target_home / "plugins" / "crosstalk"
        for name in ("__init__.py", "plugin.yaml", "crosstalk.json"):
            self.assertTrue((plugin_dir / name).is_file(), name)
        config = json.loads((plugin_dir / "crosstalk.json").read_text())
        self.assertEqual(config["python"], str(self.python))
        self.assertEqual(Path(config["cli"]), KIT / "crosstalk_p0_cli.py")
        self.assertEqual(config["device_dir"], str(self.device))
        cli_path = str(self.target_home / "bin" / "hermes.cmd")
        self.assertEqual([c for c, _ in self.calls],
                         [[cli_path, "plugins", "enable", "crosstalk"],
                          [cli_path, "config", "set", "plugins.entries.crosstalk.allow_gateway_injection", "true"]])
        self.assertEqual(self.calls[0][1]["env"]["HERMES_HOME"], str(self.target_home))
        # The backup is written once: a second install keeps the first copy.
        (self.target_home / "config.yaml").write_text("model: changed\n")
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertEqual(backup.read_text(), "model: x\n")

    def test_install_stops_at_a_failed_step(self):
        def failing(command, **options):
            self.calls.append(command)
            return SimpleNamespace(returncode=3 if command[-1] == "crosstalk" else 0, stderr="boom")

        report = installer.hermes_install(self.target_home, str(self.python), runner=failing)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "enable-plugin-failed")
        self.assertEqual(len(self.calls), 1)

    def test_install_refuses_when_prerequisites_are_missing(self):
        (self.target_home / "bin" / "hermes.cmd").unlink()
        with self.assertRaises(installer.InstallError) as raised:
            installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertEqual(raised.exception.reason, "hermes-cli-missing")
        (self.target_home / "bin" / "hermes.cmd").write_text("")
        with self.assertRaises(installer.InstallError) as raised:
            installer.hermes_install(self.target_home, str(self.root / "missing.exe"), runner=self.runner)
        self.assertEqual(raised.exception.reason, "runtime-python-missing")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.target_home / "plugins").exists())

    def test_remove_disables_then_deletes_the_plugin_and_leaves_state(self):
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        (self.target_home / "crosstalk").mkdir()
        (self.target_home / "crosstalk" / "heartbeat.json").write_text("{}")
        self.calls.clear()
        report = installer.hermes_remove(self.target_home, runner=self.runner)
        self.assertTrue(report["ok"], report)
        cli_path = str(self.target_home / "bin" / "hermes.cmd")
        self.assertEqual([c for c, _ in self.calls],
                         [[cli_path, "plugins", "disable", "crosstalk"],
                          [cli_path, "config", "set", "plugins.entries.crosstalk.allow_gateway_injection", "false"]])
        self.assertEqual([s["step"] for s in report["steps"]],
                         ["backup-config", "disable-plugin", "reset-injection", "delete-plugin"])
        self.assertFalse((self.target_home / "plugins" / "crosstalk").exists())
        self.assertTrue((self.target_home / "crosstalk" / "heartbeat.json").is_file())
        self.assertTrue((self.target_home / "config.yaml.bak-crosstalk").is_file())

    # -------------------------------------------------- identity (finding 4, review 2026-10-14)
    def plugin_dir(self):
        return self.target_home / "plugins" / "crosstalk"

    def make_foreign(self, **files):
        folder = self.plugin_dir()
        folder.mkdir(parents=True)
        for name, text in (files or {"__init__.py": "# somebody else's plugin\n"}).items():
            (folder / name).write_text(text, encoding="utf-8")
        return folder

    def test_identity_accepts_a_folder_produced_by_the_current_source(self):
        source = installer.hermes_plugin_source()
        self.assertEqual(installer.hermes_plugin_identity(source)["state"], "ours")
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertEqual(installer.hermes_plugin_identity(self.plugin_dir()), {"state": "ours"})

    def test_identity_states(self):
        folder = self.plugin_dir()
        self.assertEqual(installer.hermes_plugin_identity(folder), {"state": "absent"})
        folder.mkdir(parents=True)
        self.assertEqual(installer.hermes_plugin_identity(folder), {"state": "empty"})
        source = installer.hermes_plugin_source()
        manifest = (source / "plugin.yaml").read_text(encoding="utf-8")
        code = (source / "__init__.py").read_text(encoding="utf-8")
        cases = {"other code": (manifest, "print('hi')\n"),
                 "other name": (manifest.replace("name: crosstalk", "name: other", 1), code),
                 "no manifest marker": (manifest.replace("EUTONOS Crosstalk", "Some Crosstalk"), code),
                 "no code marker": (manifest, code.replace("EUTONOS Crosstalk plugin for Hermes", "x")),
                 "no tag": (manifest, code.replace("crosstalk.hermes.plugin.v1", "x"))}
        for name, (plugin_yaml, init) in cases.items():
            (folder / "plugin.yaml").write_text(plugin_yaml, encoding="utf-8")
            (folder / "__init__.py").write_text(init, encoding="utf-8")
            self.assertEqual(installer.hermes_plugin_identity(folder)["state"], "foreign", name)
        (folder / "plugin.yaml").write_text(manifest, encoding="utf-8")
        (folder / "__init__.py").write_text(code, encoding="utf-8")
        self.assertEqual(installer.hermes_plugin_identity(folder)["state"], "ours")
        (folder / "__init__.py").unlink()
        self.assertEqual(installer.hermes_plugin_identity(folder)["state"], "foreign")
        shutil_target = self.root / "plain-file"
        shutil_target.write_text("x")
        self.assertEqual(installer.hermes_plugin_identity(shutil_target)["state"], "foreign")

    def test_identity_never_follows_a_link(self):
        # A real copy of the plugin: ours. The same folder seen as a link/junction: foreign, never followed.
        real = self.root / "elsewhere"
        shutil.copytree(installer.hermes_plugin_source(), real)
        self.assertEqual(installer.hermes_plugin_identity(real)["state"], "ours")
        with mock.patch.object(installer, "_is_link", return_value=True):
            self.assertEqual(installer.hermes_plugin_identity(real),
                             {"state": "foreign", "why": "not-a-real-directory"})

    def test_install_refuses_a_foreign_folder_and_changes_nothing(self):
        folder = self.make_foreign()
        before = self.tree(self.target_home)
        with self.assertRaises(installer.InstallError) as raised:
            installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertEqual(raised.exception.reason, "hermes-plugin-folder-not-ours")
        self.assertEqual(self.tree(self.target_home), before)
        self.assertEqual((folder / "__init__.py").read_text(), "# somebody else's plugin\n")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.target_home / "config.yaml.bak-crosstalk").exists())

    def test_force_replaces_a_foreign_folder_and_keeps_it_aside(self):
        self.make_foreign()
        report = installer.hermes_install(self.target_home, str(self.python), runner=self.runner, force=True)
        self.assertTrue(report["ok"], report)
        copy = [s for s in report["steps"] if s["step"] == "copy-plugin"][0]
        self.assertEqual(copy["previous"], "foreign")
        kept = Path(copy["previous_kept_at"])
        self.assertEqual((kept / "__init__.py").read_text(), "# somebody else's plugin\n")
        self.assertEqual(installer.hermes_plugin_identity(self.plugin_dir()), {"state": "ours"})

    def test_reinstall_replaces_our_own_folder_without_stale_files(self):
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        (self.plugin_dir() / "stale.py").write_text("old")
        (self.plugin_dir() / "crosstalk.json").write_text("{}")
        report = installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertTrue(report["ok"], report)
        copy = [s for s in report["steps"] if s["step"] == "copy-plugin"][0]
        self.assertEqual((copy["replaced"], copy["previous"]), (True, "ours"))
        self.assertNotIn("previous_kept_at", copy)
        self.assertFalse((self.plugin_dir() / "stale.py").exists())
        self.assertEqual(json.loads((self.plugin_dir() / "crosstalk.json").read_text())["python"], str(self.python))
        # no temporary sibling is left behind
        self.assertEqual(sorted(p.name for p in (self.target_home / "plugins").iterdir()), ["crosstalk"])

    def test_install_over_an_empty_folder_and_a_failed_swap_restores_the_old_folder(self):
        self.plugin_dir().mkdir(parents=True)
        self.assertTrue(installer.hermes_install(self.target_home, str(self.python), runner=self.runner)["ok"])
        marker = self.plugin_dir() / "__init__.py"
        before = marker.read_text(encoding="utf-8")
        real_replace = installer.os.replace

        def flaky(src, dst):
            if Path(dst) == self.plugin_dir() and Path(src).name.startswith(".crosstalk.new-"):
                raise OSError("denied")
            return real_replace(src, dst)

        with mock.patch.object(installer.os, "replace", side_effect=flaky):
            with self.assertRaises(OSError):
                installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertEqual(marker.read_text(encoding="utf-8"), before)
        self.assertEqual(installer.hermes_plugin_identity(self.plugin_dir()), {"state": "ours"})
        self.assertEqual(sorted(p.name for p in (self.target_home / "plugins").iterdir()), ["crosstalk"])

    def test_remove_refuses_a_foreign_folder_and_runs_nothing(self):
        folder = self.make_foreign()
        report = installer.hermes_remove(self.target_home, runner=self.runner)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "hermes-plugin-folder-not-ours")
        self.assertEqual(self.calls, [])
        self.assertEqual((folder / "__init__.py").read_text(), "# somebody else's plugin\n")
        self.assertFalse((self.target_home / "config.yaml.bak-crosstalk").exists())

    def test_remove_resets_the_injection_key_and_keeps_the_plugin_when_the_reset_fails(self):
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)

        def runner(command, **options):
            return SimpleNamespace(returncode=1 if command[-1] == "false" else 0, stderr="no")

        report = installer.hermes_remove(self.target_home, runner=runner)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "reset-injection-failed")
        self.assertTrue((self.plugin_dir() / "__init__.py").is_file())

    def test_remove_without_a_hermes_cli_says_the_key_is_still_set(self):
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        (self.target_home / "bin" / "hermes.cmd").unlink()
        report = installer.hermes_remove(self.target_home, runner=self.runner)
        self.assertTrue(report["ok"])
        reset = [s for s in report["steps"] if s["step"] == "reset-injection"][0]
        self.assertEqual(reset["still_set"], "plugins.entries.crosstalk.allow_gateway_injection")
        self.assertFalse(self.plugin_dir().exists())

    def test_remove_with_nothing_installed_still_resets_the_key(self):
        report = installer.hermes_remove(self.target_home, runner=self.runner)
        self.assertTrue(report["ok"], report)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse([s for s in report["steps"] if s["step"] == "delete-plugin"][0]["existed"])

    def test_install_report_carries_no_file_contents(self):
        (self.target_home / "config.yaml").write_text("api_key: sk-SECRET-VALUE\n")
        report = installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertNotIn("SECRET", json.dumps(installer.hermes_remove(self.target_home, runner=self.runner)))

    def test_plan_reports_the_target_identity_and_the_reset_command(self):
        self.make_foreign()
        plan = installer.hermes_plan(self.target_home, str(self.python))
        self.assertEqual(plan["target_identity"]["state"], "foreign")
        cli_path = str(self.target_home / "bin" / "hermes.cmd")
        self.assertEqual(plan["remove_steps"][1]["command"],
                         [cli_path, "config", "set", "plugins.entries.crosstalk.allow_gateway_injection", "false"])

    def test_remove_keeps_the_plugin_when_disable_fails(self):
        installer.hermes_install(self.target_home, str(self.python), runner=self.runner)
        report = installer.hermes_remove(self.target_home,
                                         runner=lambda c, **o: SimpleNamespace(returncode=1, stderr="no"))
        self.assertFalse(report["ok"])
        self.assertTrue((self.target_home / "plugins" / "crosstalk" / "__init__.py").is_file())

    def test_install_command_line_plan(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = installer.main(["--hermes-home", str(self.target_home), "hermes-plan"])
        self.assertEqual(code, 0)
        value = json.loads(buffer.getvalue())
        self.assertTrue(value["ok"])
        self.assertEqual(value["hermes_home"], str(self.target_home))
        self.assertFalse((self.target_home / "plugins").exists())


ROUTE = "agent:main:local:dm:desktop-7"


class GatewayRunner:
    """The slice of the one-gateway runner the plugin reads: its session store (id -> routing entry)."""

    def __init__(self, entries=None):
        self.session_store = SimpleNamespace(
            lookup_by_session_id=lambda sid: (entries or {}).get(sid))


class GatewayCtx(FakeCtx):
    """Hermes after the 2026-10-26 update: no TUI sidecar, the gateway process hosts Desktop chats."""

    def __init__(self, returns=True, entries=None, tui=False):
        super().__init__(returns)
        runner = GatewayRunner(entries if entries is not None else {H1: SimpleNamespace(session_key=ROUTE)})
        self._manager = SimpleNamespace(has_tui_message_injector=tui, has_gateway_message_injector=True,
                                        _gateway_message_injector=(runner, lambda **kw: True))


class GatewayHostTests(HermesCase):
    """Heartbeat and delivery when the host is the gateway runtime (hosts_chats used to need a TUI injector)."""

    def test_host_kind_per_manager(self):
        none = SimpleNamespace(_manager=SimpleNamespace(has_tui_message_injector=False,
                                                        has_gateway_message_injector=False))
        self.assertIsNone(plugin._host_kind(none))
        self.assertFalse(plugin._hosts_chats(none))
        self.assertEqual(plugin._host_kind(FakeCtx()), "tui")
        self.assertEqual(plugin._host_kind(GatewayCtx()), "gateway")
        self.assertTrue(plugin._hosts_chats(GatewayCtx()))
        self.assertEqual(plugin._host_kind(GatewayCtx(tui=True)), "tui")  # an old TUI host keeps its old behavior
        self.assertIsNone(plugin._host_kind(SimpleNamespace()))  # no manager at all
        callable_flags = SimpleNamespace(_manager=SimpleNamespace(has_tui_message_injector=lambda: False,
                                                                  has_gateway_message_injector=lambda: True))
        self.assertEqual(plugin._host_kind(callable_flags), "gateway")
        broken = SimpleNamespace(_manager=SimpleNamespace(has_tui_message_injector=lambda: 1 / 0,
                                                          has_gateway_message_injector=False))
        self.assertIsNone(plugin._host_kind(broken))

    def test_loop_writes_a_gateway_heartbeat(self):
        stop = threading.Event()

        def gateway_flag():
            if stop.is_set():
                raise SystemExit  # ends only this test thread
            return True

        ctx = SimpleNamespace(_manager=SimpleNamespace(has_tui_message_injector=False,
                                                       has_gateway_message_injector=gateway_flag))
        with mock.patch.object(plugin, "POLL_S", 0.02), mock.patch.object(plugin, "HEARTBEAT_S", 0.0):
            thread = threading.Thread(target=plugin._loop, args=(ctx,), daemon=True)
            thread.start()
            try:
                beat = self.hermes / "crosstalk" / "heartbeat.json"
                deadline = time.monotonic() + 3
                value = {}
                while time.monotonic() < deadline and not value:
                    try:
                        value = json.loads(beat.read_text())
                    except (OSError, ValueError):
                        value = {}
                    time.sleep(0.02)
            finally:
                stop.set()
                thread.join(3)
        self.assertEqual((value.get("hosts_chats"), value.get("host"), value.get("pid")), (True, "gateway", os.getpid()))
        self.assertEqual(value.get("plugin_version"), plugin.PLUGIN_VERSION)
        self.assertTrue(core.hermes_listener_alive())

    def test_no_host_no_heartbeat(self):
        stop = threading.Event()

        def flag():
            if stop.is_set():
                raise SystemExit
            return False

        ctx = SimpleNamespace(_manager=SimpleNamespace(has_tui_message_injector=flag,
                                                       has_gateway_message_injector=False))
        with mock.patch.object(plugin, "POLL_S", 0.02), mock.patch.object(plugin, "HEARTBEAT_S", 0.0):
            thread = threading.Thread(target=plugin._loop, args=(ctx,), daemon=True)
            thread.start()
            time.sleep(0.3)
            stop.set()
            thread.join(3)
        self.assertFalse((self.hermes / "crosstalk" / "heartbeat.json").exists())

    def test_inject_key_maps_the_session_id_to_the_gateway_route(self):
        self.assertEqual(plugin._inject_key(GatewayCtx(), H1), ROUTE)
        self.assertEqual(plugin._inject_key(FakeCtx(), H1), H1)  # TUI hosts key sessions by the stored id
        self.assertEqual(plugin._inject_key(GatewayCtx(tui=True), H1), H1)
        # Unknown id: the gateway would accept the bare id and drop it at dispatch, so no key at all.
        self.assertIsNone(plugin._inject_key(GatewayCtx(entries={}), H1))
        broken = GatewayCtx()
        broken._manager._gateway_message_injector = None
        self.assertIsNone(plugin._inject_key(broken, H1))

    def deliver(self, ctx, key=H1, letter="CTP0-abcdefabcdef"):
        inbox = self.hermes / "crosstalk" / "inbox" / (letter + ".json")
        inbox.write_text(json.dumps({"letter_id": letter, "session_key": key,
                                     "text": core.frame_message(letter, "claude", "Chat", "hello")}))
        plugin._handle(ctx, inbox)
        return json.loads((self.hermes / "crosstalk" / "done" / (letter + ".result.json")).read_text())

    def test_a_letter_is_injected_under_the_gateway_route(self):
        ctx = GatewayCtx()
        result = self.deliver(ctx)
        self.assertIs(result["returned"], True)
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(ctx.calls[0][1:], ("user", ROUTE))

    def test_a_closed_chat_is_refused_without_a_tui_resume(self):
        ctx = GatewayCtx(returns=False)
        with mock.patch.object(plugin, "resume_closed_chat", side_effect=AssertionError("no tui resume")):
            result = self.deliver(ctx)
        self.assertIs(result["returned"], False)
        self.assertIsNone(result["resumed"])
        self.assertEqual(result["resume_note"], "gateway-host-no-resume")
        self.assertEqual(len(ctx.calls), 1)

    def test_a_chat_the_gateway_has_no_route_for_is_refused_with_nothing_injected(self):
        ctx = GatewayCtx(entries={})
        with mock.patch.object(plugin, "resume_closed_chat", side_effect=AssertionError("no tui resume")):
            result = self.deliver(ctx)
        self.assertIs(result["returned"], False)
        self.assertEqual(result["resume_note"], "gateway-host-no-resume")
        self.assertEqual(ctx.calls, [])


if __name__ == "__main__":
    unittest.main()
