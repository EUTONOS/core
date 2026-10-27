"""Offline tests: an asleep Codex chat gets its letter by a resumed headless turn (Crosstalk rework slice 3).

The app-server is a fake JSON-RPC peer, the rollout is a fixture file, and no real codex process, link or app data
is touched."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_headless_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    helper_spec = importlib.util.spec_from_file_location("crosstalk_headless_helper", KIT / "crosstalk_helper.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
LETTER = "CTP0-0000000000a1"
LETTER2 = "CTP0-0000000000a2"
TURN = "turn-headless-1"


def framed(letter_id=LETTER, text="Hello asleep chat"):
    return core.frame_message(letter_id, "claude", "sender", text)


class FakeServer:
    """Scripted stand-in for core.AppServer's JSON-RPC peer. `script` keys: resume_error (message text),
    after_turn (messages pushed after turn/start answers), on_interrupt (messages pushed after turn/interrupt)."""

    instances: list = []
    events: list = []

    def __init__(self, exe, kill_on_close=False, script=None):
        self.script = script if script is not None else {}
        self.kill_on_close = kill_on_close
        self.job_ok = self.script.get("job_ok", True)
        self.lines: queue.Queue = queue.Queue()
        self.requests: list = []
        self.replies: list = []
        self.closed = False
        self._next = 0
        FakeServer.instances.append(self)

    def start(self, experimental, timeout, client_name="crosstalk_p0_probe"):
        self.client_name = client_name

    def send_request(self, method, params):
        self._next += 1
        request_id = self._next
        self.requests.append((method, params))
        FakeServer.events.append(method)
        if method == "thread/resume":
            if self.script.get("resume_error"):
                self.lines.put({"id": request_id, "error": {"code": -32600, "message": self.script["resume_error"]}})
            else:
                self.lines.put({"id": request_id, "result": {"thread": {"id": CODEX}}})
        elif method == "turn/start":
            if self.script.get("turn_error"):
                self.lines.put({"id": request_id, "error": {"code": -32602, "message": self.script["turn_error"]}})
            else:
                self.lines.put({"id": request_id, "result": {"turn": {"id": TURN, "status": "inProgress"}}})
                for message in self.script.get("after_turn", []):
                    self.lines.put(message)
        elif method == "turn/interrupt":
            self.lines.put({"id": request_id, "result": {}})
            for message in self.script.get("on_interrupt", []):
                self.lines.put(message)
        return request_id

    def reply(self, request_id, result=None, error=None):
        self.replies.append((request_id, result, error))

    def close(self):
        self.closed = True

    def methods(self):
        return [method for method, _ in self.requests]


def completed(status="completed"):
    return {"method": "turn/completed", "params": {"threadId": CODEX, "turn": {"id": TURN, "status": status}}}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-headless-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        env = mock.patch.dict(os.environ, {
            "CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
            "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"), "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "c.json"),
            "CROSSTALK_P0_CODEX_SESSIONS_DIR": str(self.root / "sessions"),
            "CROSSTALK_P0_CODEX_WAKE": "off", "CROSSTALK_P0_FOCUS": "off"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CROSSTALK_P0_CODEX_HEADLESS", None)
        core.controls_update(messaging="on", halt=False)
        core.directory_register("codex", CODEX, {"account": {"id": ACCOUNT}})
        FakeServer.instances, FakeServer.events = [], []
        self.script: dict = {}
        self.spawned: list = []
        self.threads: list = []
        self.queue_calls: list = []
        for patcher in (mock.patch.object(core, "codex_exe", return_value="codex"),
                        mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}),
                        mock.patch.object(core, "AppServer", side_effect=lambda exe, kill_on_close=False: FakeServer(
                            exe, kill_on_close, self.script)),
                        mock.patch.object(core, "spawn_codex_headless", side_effect=self.spawn),
                        mock.patch.object(core, "arrivals", return_value=(0, "thread-read")),
                        mock.patch.object(core, "CODEX_HEADLESS_POLL_S", 0.01),
                        mock.patch.object(core, "CODEX_HEADLESS_CONTROLS_S", 0.02),
                        mock.patch.object(core, "CODEX_HEADLESS_START_S", 5.0),
                        mock.patch.object(core.subprocess, "run", side_effect=self.fake_queue)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.join_threads)

    # ---- fakes
    def spawn(self, letter_id, cleanup=False):
        self.spawned.append((letter_id, cleanup))
        target = core.codex_headless_cleanup if cleanup else core.codex_headless_wake
        thread = threading.Thread(target=lambda: self.results.setdefault(letter_id, target(letter_id)), daemon=True)
        self.threads.append(thread)
        thread.start()
        return 4242

    results: dict = {}

    def join_threads(self):
        for thread in self.threads:
            thread.join(timeout=10)

    def fake_queue(self, argv, **kwargs):
        FakeServer.events.append("queue")
        self.queue_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"Queued message {'d' * 8}-bbbb-4ccc-8ddd-eeeeeeeeeeee for thread {CODEX}\n",
                                           stderr="")

    # ---- fixtures
    def write_rollout(self, *rows):
        folder = self.root / "sessions" / "2026" / "10" / "19"
        folder.mkdir(parents=True, exist_ok=True)
        context = {"type": "turn_context", "payload": {
            "turn_id": "t0", "model": "gpt-6-luna", "effort": "xhigh", "cwd": str(self.folder),
            "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write", "writable_roots": [],
                                                                "network_access": False}}}
        lines = [json.dumps(context)] + [json.dumps(row) for row in rows]
        (folder / f"rollout-2026-10-19T00-00-00-{CODEX}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def letter(self, letter_id=LETTER, thread=CODEX, **extra):
        message = framed(letter_id)
        core.journal_append(core._letter_row(letter_id, "journaled", to={"app": "codex", "id": thread}, message=message,
                                             created_ns=time.time_ns(), text_chars=5,
                                             **{"from": {"app": "claude", "id": CLAUDE}}))
        core.journal_append(core._letter_row(letter_id, "sending", attempt_id="aaaa", route="codex-queue",
                                             sending_ns=time.time_ns(), **extra))
        return message

    def notes(self, letter_id=LETTER):
        return core.letters_load()[letter_id]

    def claim(self, thread=CODEX):
        return core._claims_load().get(thread) or {}


class ResumeFirstTests(Base):
    def test_resume_success_uses_the_chats_own_settings_and_the_letter_as_the_turn_input(self):
        self.write_rollout()
        message = self.letter()
        self.script["after_turn"] = [completed()]
        state, extra = core.deliver_codex(CODEX, message)
        self.join_threads()
        self.assertEqual(state, "accepted")
        self.assertEqual((extra["codex_headless"], extra["codex_turn_id"]), (True, TURN))
        self.assertEqual(self.queue_calls, [])  # the letter went by one door only
        server = FakeServer.instances[0]
        self.assertTrue(server.kill_on_close)
        self.assertEqual(server.methods(), ["thread/resume", "turn/start"])
        resume, turn = server.requests[0][1], server.requests[1][1]
        self.assertEqual((resume["threadId"], resume["model"], resume["cwd"], resume["approvalPolicy"], resume["sandbox"]),
                         (CODEX, "gpt-6-luna", str(self.folder), "on-request", "workspace-write"))
        self.assertEqual(turn["input"], [{"type": "text", "text": message}])
        self.assertEqual((turn["model"], turn["effort"], turn["cwd"], turn["approvalPolicy"]),
                         ("gpt-6-luna", "xhigh", str(self.folder), "on-request"))
        self.assertEqual(turn["sandboxPolicy"], {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False})
        self.assertTrue(server.closed)
        self.assertTrue(self.claim()["released"])
        self.assertEqual(self.notes()["codex_headless_end"], "completed")

    def test_settings_the_chat_does_not_give_fall_back_to_queue_not_cli_defaults(self):
        message = self.letter()  # no rollout and a bare resume response
        state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual(state, "accepted")
        self.assertTrue(extra["codex_headless_fallback"].startswith("settings-unavailable:"))
        self.assertEqual(FakeServer.instances[0].methods(), ["thread/resume"])  # no turn/start
        self.assertEqual(len(self.queue_calls), 1)

    def test_an_app_held_chat_is_refused_before_any_effect_and_goes_by_queue(self):
        self.write_rollout()
        message = self.letter()
        self.script["resume_error"] = f"thread {CODEX} already has an active writer"
        state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual((state, extra), ("accepted", extra))
        self.assertEqual(extra["codex_headless_fallback"], "app-held")
        self.assertIn("queue_id", extra)
        self.assertEqual(FakeServer.events, ["thread/resume", "queue"])  # nothing sent into the chat before the queue
        self.assertTrue(FakeServer.instances[0].closed)
        self.assertTrue(self.claim()["released"])

    def test_headless_off_goes_straight_to_queue(self):
        message = self.letter()
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_CODEX_HEADLESS": "off"}):
            state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual(state, "accepted")
        self.assertNotIn("codex_headless_fallback", extra)
        self.assertEqual((FakeServer.instances, self.spawned), ([], []))

    def test_a_headless_letter_never_gets_the_app_link_wake(self):
        self.write_rollout()
        message = self.letter()
        self.script["after_turn"] = [completed()]
        core.deliver_codex(CODEX, message)
        self.join_threads()
        core.journal_append(core._letter_row(LETTER, "held", sent=True, codex_headless=True))
        with mock.patch.object(core, "spawn_codex_wake") as wake:
            self.assertFalse(core.start_codex_wake(LETTER))
        wake.assert_not_called()
        self.assertEqual(core.codex_wake(LETTER, delay_s=0, sleep=lambda s: None, opener=lambda url: self.fail("link"))["wake"],
                         "skipped:headless-turn")


class GuardTests(Base):
    def test_every_server_request_is_declined_never_approved(self):
        self.write_rollout()
        message = self.letter()
        self.script["after_turn"] = [
            {"id": 91, "method": "item/commandExecution/requestApproval", "params": {"threadId": CODEX}},
            {"id": 92, "method": "item/tool/requestUserInput", "params": {"threadId": CODEX}},
            {"id": 93, "method": "mcpServer/elicitation/request", "params": {"threadId": CODEX}},
            {"id": 94, "method": "item/fileChange/requestApproval", "params": {"threadId": CODEX}},
            {"id": 95, "method": "item/permissions/requestApproval", "params": {"threadId": CODEX}},
            completed()]
        core.deliver_codex(CODEX, message)
        self.join_threads()
        replies = {request_id: (result, error) for request_id, result, error in FakeServer.instances[0].replies}
        self.assertEqual(replies[91], ({"decision": "decline"}, None))
        self.assertEqual(replies[94], ({"decision": "decline"}, None))
        self.assertEqual(replies[93], ({"action": "decline"}, None))
        self.assertEqual(replies[95], ({"permissions": {}}, None))  # its reply type has no decision; an empty grant grants nothing
        self.assertIsNotNone(replies[92][1])
        for result, _error in replies.values():
            self.assertNotIn("accept", json.dumps(result).lower())
        self.assertEqual(self.notes()["codex_headless_declined"], 5)

    def test_the_turn_deadline_sends_interrupt_and_journals_it(self):
        self.write_rollout()
        message = self.letter()
        self.script["on_interrupt"] = [completed("interrupted")]
        with mock.patch.object(core, "CODEX_HEADLESS_DEADLINE_S", 0.0):
            core.deliver_codex(CODEX, message)
            self.join_threads()
        interrupt = [params for method, params in FakeServer.instances[0].requests if method == "turn/interrupt"]
        self.assertEqual(interrupt, [{"threadId": CODEX, "turnId": TURN}])
        letter = self.notes()
        self.assertEqual((letter["codex_headless_interrupt"], letter["codex_headless_end"]), ("deadline", "interrupted"))
        self.assertNotIn("codex_headless_owner_interrupted", letter)  # our own interrupt is not the owner's

    def test_halt_in_the_middle_of_a_turn_interrupts_it(self):
        self.write_rollout()
        message = self.letter()
        self.script["on_interrupt"] = [completed("interrupted")]  # the turn never ends by itself
        core.deliver_codex(CODEX, message)
        core.controls_update(halt=True)
        self.join_threads()
        self.assertIn("turn/interrupt", FakeServer.instances[0].methods())
        self.assertEqual(self.notes()["codex_headless_interrupt"], "halted")
        self.assertTrue(self.claim()["released"])

    def test_messaging_off_and_deny_also_interrupt(self):
        for change, reason in (({"messaging": "off"}, "messaging-off"), ({"messaging": "on", "deny_add": "codex:" + CODEX}, "denied")):
            with self.subTest(reason=reason):
                core.controls_update(messaging="on", halt=False, deny_remove="codex:" + CODEX)
                letter_id = "CTP0-00000000%04d" % (10 if reason == "denied" else 11)
                self.write_rollout()
                message = self.letter(letter_id)
                self.script = {"on_interrupt": [completed("interrupted")]}
                core.deliver_codex(CODEX, message)
                core.controls_update(**change)
                self.join_threads()
                self.assertEqual(self.notes(letter_id)["codex_headless_interrupt"], reason)
                core.controls_update(messaging="on", deny_remove="codex:" + CODEX)

    def test_a_second_letter_waits_for_the_first_waiter_and_never_queues_over_it(self):
        self.write_rollout()
        self.letter(LETTER)
        core._claims_mutate(lambda claims: claims.update({CODEX: {"letter_id": LETTER, "phase": "turn-started",
                                                                  "started_ns": time.time_ns(), "beat_ns": time.time_ns()}}))
        message2 = self.letter(LETTER2)
        with mock.patch.object(core, "CODEX_CLAIM_WAIT_S", 0.2):
            state, extra = core.deliver_codex(CODEX, message2)
        self.assertEqual((state, extra["reason"]), ("refused", "codex-wake-busy"))
        self.assertEqual((self.queue_calls, self.spawned, FakeServer.instances), ([], [], []))  # no effect, no queue

    def test_a_second_letter_goes_once_the_first_waiter_is_done(self):
        self.write_rollout()
        self.letter(LETTER)
        core._claims_mutate(lambda claims: claims.update({CODEX: {"letter_id": LETTER, "phase": "turn-started",
                                                                  "started_ns": time.time_ns(), "beat_ns": time.time_ns()}}))
        message2 = self.letter(LETTER2)
        self.script["after_turn"] = [completed()]
        timer = threading.Timer(0.15, lambda: core._claims_mutate(lambda claims: claims[CODEX].update(released=True)))
        timer.start()
        self.addCleanup(timer.cancel)
        state, extra = core.deliver_codex(CODEX, message2)
        self.join_threads()
        self.assertEqual((state, extra["codex_headless"]), ("accepted", True))
        self.assertEqual(self.queue_calls, [])

    def test_a_dead_waiters_claim_does_not_block_the_next_letter(self):
        self.write_rollout()
        self.letter(LETTER)
        old = time.time_ns() - int(120 * 1e9)
        core._claims_mutate(lambda claims: claims.update({CODEX: {"letter_id": LETTER, "phase": "turn-started",
                                                                  "started_ns": old, "beat_ns": old}}))
        message2 = self.letter(LETTER2)
        self.script["after_turn"] = [completed()]
        state, extra = core.deliver_codex(CODEX, message2)
        self.join_threads()
        self.assertEqual((state, extra["codex_headless"]), ("accepted", True))

    def test_pre_send_guard_runs_right_before_turn_start(self):
        self.write_rollout()
        message = self.letter()
        calls = []

        def guard(letter_id, route, **kwargs):
            calls.append(list(FakeServer.events))
            return "halted"
        with mock.patch.object(core, "pre_send_guard", side_effect=guard):
            state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual((state, extra["reason"]), ("refused", "halted"))
        self.assertEqual(calls, [["thread/resume"]])  # after the resume, before any turn
        self.assertNotIn("turn/start", FakeServer.events)

    def test_a_server_that_cannot_be_put_in_a_job_object_is_not_used(self):
        self.write_rollout()
        message = self.letter()
        self.script["job_ok"] = False
        state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual(extra["codex_headless_fallback"], "job-object-unavailable")
        self.assertEqual(FakeServer.events, ["queue"])


class OwnerInterruptTests(Base):
    def test_a_turn_the_owner_stopped_is_marked_and_never_resumed(self):
        self.write_rollout()
        message = self.letter()
        self.script["after_turn"] = [completed("interrupted")]  # nobody here interrupted it
        core.deliver_codex(CODEX, message)
        self.join_threads()
        self.assertTrue(self.notes()["codex_headless_owner_interrupted"])
        FakeServer.instances.clear()
        self.assertEqual(core.codex_headless_try(CODEX, message), (None, "owner-interrupted"))
        self.assertEqual((FakeServer.instances, self.spawned[1:]), ([], []))

    def test_a_turn_aborted_after_the_attempt_in_the_rollout_blocks_a_headless_wake(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 60))
        self.write_rollout({"timestamp": stamp, "type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}})
        message = self.letter()
        state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual(extra["codex_headless_fallback"], "owner-interrupted")
        self.assertEqual((FakeServer.instances, self.spawned), ([], []))

    def test_cleanup_does_not_touch_a_chat_whose_turn_the_owner_stopped(self):
        self.write_rollout()
        self.letter()
        core.journal_append(core._letter_row(LETTER, "accepted", codex_headless=True, codex_headless_owner_interrupted=True))
        old = time.time_ns() - int(120 * 1e9)
        core._claims_mutate(lambda claims: claims.update({CODEX: {"letter_id": LETTER, "phase": "turn-started", "turn_id": TURN,
                                                                  "started_ns": old, "beat_ns": old}}))
        result = core.codex_headless_cleanup(LETTER)
        self.assertEqual(result["cleanup"], "owner-interrupted")
        self.assertEqual(FakeServer.instances, [])


class CrashCleanupTests(Base):
    def stale_claim(self, phase="turn-started"):
        old = time.time_ns() - int(120 * 1e9)
        core._claims_mutate(lambda claims: claims.update({CODEX: {"letter_id": LETTER, "phase": phase, "turn_id": TURN,
                                                                  "started_ns": old, "beat_ns": old}}))

    def test_a_turn_with_no_end_marker_counts_as_ended_when_a_fresh_resume_succeeds(self):
        self.write_rollout({"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN}})  # never ended
        self.letter()
        self.stale_claim()
        self.assertEqual(core.codex_claims_needing_cleanup(), [LETTER])
        result = core.codex_headless_cleanup(LETTER)
        self.assertEqual(result["cleanup"], "no-end-marker-resume-ok")
        self.assertEqual(FakeServer.instances[0].methods(), ["thread/resume"])  # never turn/start: no resend
        self.assertTrue(FakeServer.instances[0].closed)
        self.assertTrue(self.claim()["released"])
        self.assertEqual(self.notes()["codex_headless_end"], "no-end-marker-resume-ok")
        self.assertEqual(core.codex_claims_needing_cleanup(), [])

    def test_cleanup_that_cannot_resume_retries_then_lets_go(self):
        self.letter()
        self.stale_claim()
        self.script["resume_error"] = "boom"
        for _ in range(core.CODEX_CLEANUP_MAX_TRIES):
            self.assertFalse(core.codex_headless_cleanup(LETTER)["ok"])
        self.assertTrue(self.claim()["released"])
        self.assertEqual(self.notes()["codex_headless_end"], "cleanup-gave-up")
        self.assertNotIn("turn/start", FakeServer.events)

    def test_the_helper_restarts_a_missing_waiter_for_bookkeeping_only(self):
        self.letter()
        self.stale_claim()
        runner = helper.Helper(clock=time.time)
        runner.log = mock.Mock()
        report = runner.restart_codex_cleanup()
        self.join_threads()
        self.assertEqual(report["cleanup"], [LETTER])
        self.assertEqual(self.spawned, [(LETTER, True)])
        self.assertNotIn("turn/start", FakeServer.events)
        self.assertEqual(self.queue_calls, [])
        core.controls_update(halt=True)
        self.stale_claim()
        self.assertEqual(runner.restart_codex_cleanup(), {"cleanup": []})  # halt stops it like every wake

    def test_a_claim_that_never_got_to_send_is_just_released(self):
        self.letter()
        self.stale_claim("starting")
        self.assertEqual(core.codex_claims_needing_cleanup(), [])
        self.assertTrue(self.claim()["released"])


class WakeBudgetTests(Base):
    """A headless resume is a wake: counted when decided (rework thought experiment 6). Over budget nothing is resumed
    and the letter goes by `codex queue`; an app-held chat woke nothing and gives its slot back."""

    def used(self):
        return core.wake_budget_status()["used_total"]

    def test_a_headless_resume_is_counted_and_a_second_letter_within_a_minute_shares_it(self):
        self.write_rollout()
        self.script["after_turn"] = [completed()]
        state, extra = core.deliver_codex(CODEX, self.letter(LETTER))
        self.join_threads()
        self.assertEqual((state, extra["codex_headless"]), ("accepted", True))
        self.assertEqual(self.used(), 1)
        self.script["after_turn"] = [completed()]
        core.deliver_codex(CODEX, self.letter(LETTER2))
        self.join_threads()
        self.assertEqual(len(FakeServer.instances), 2)  # both resumed headlessly...
        self.assertEqual(self.used(), 1)  # ...and the budget saw one wake

    def test_over_budget_nothing_is_resumed_and_the_letter_waits_in_the_codex_queue(self):
        self.write_rollout()
        core.controls_update(wake_budget={"per_chat": 0})
        message = self.letter()
        state, extra = core.deliver_codex(CODEX, message)
        self.assertEqual(state, "accepted")  # by the queue: held for the chat, not lost and not sent twice
        self.assertEqual(extra["codex_headless_fallback"], "wake-budget-chat")
        self.assertEqual((FakeServer.instances, self.spawned), ([], []))  # no server, no waiter, no resume
        self.assertEqual(len(self.queue_calls), 1)
        self.assertTrue(self.claim()["released"])  # the chat's claim is free again
        self.assertEqual(self.used(), 0)
        self.assertIn("wake-budget", core.helper_notices_load()["notices"])

    def test_the_total_budget_also_refuses_the_resume(self):
        self.write_rollout()
        core.controls_update(wake_budget={"total": 0})
        state, extra = core.deliver_codex(CODEX, self.letter())
        self.assertEqual(extra["codex_headless_fallback"], "wake-budget-total")
        self.assertEqual(FakeServer.instances, [])

    def test_an_app_held_chat_woke_nothing_so_its_slot_is_given_back(self):
        self.write_rollout()
        core.controls_update(wake_budget={"per_chat": 1})
        self.script["resume_error"] = f"thread {CODEX} already has an active writer"
        state, extra = core.deliver_codex(CODEX, self.letter(LETTER))
        self.assertEqual(extra["codex_headless_fallback"], "app-held")
        self.assertEqual(self.used(), 0)
        self.script = {"after_turn": [completed()]}
        state, extra = core.deliver_codex(CODEX, self.letter(LETTER2))
        self.join_threads()
        self.assertEqual(extra["codex_headless"], True)  # the budget was still whole
        self.assertEqual(self.used(), 1)

    def test_a_dead_end_resume_that_did_wake_the_chat_stays_counted(self):
        self.write_rollout()
        self.script["resume_error"] = "thread cannot be resumed"
        state, extra = core.deliver_codex(CODEX, self.letter())
        self.assertTrue(extra["codex_headless_fallback"].startswith("resume-failed"))
        self.assertEqual(self.used(), 1)  # a failing retry loop must still end at the budget

if __name__ == "__main__":
    unittest.main()
