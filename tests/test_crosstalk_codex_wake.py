"""Offline tests: the last-resort wake of an ASLEEP Codex chat (open codex://threads/<id> once).

Fixture rollouts and mocks only: no real link is opened, no process is started, no codex call is made."""
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
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_wake_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_wake_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
LETTER = "CTP0-0123456789ab"
ORIGINAL_SPAWN, ORIGINAL_OPEN = core.spawn_codex_wake, core.open_codex_link  # setUp replaces both on core


def event(kind: str, **extra) -> dict:
    return {"timestamp": "2026-10-12T04:20:00.000Z", "type": "event_msg",
            "payload": dict({"type": kind, "turn_id": "turn-1"}, **extra)}


STARTED, COMPLETE, ABORTED = event("task_started"), event("task_complete"), event("turn_aborted", reason="interrupted")


class CodexWakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-wake-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_CODEX_SESSIONS_DIR": str(self.root / "sessions"),
                                          "CROSSTALK_P0_FOCUS": "off"})  # no overlay, no window handling in tests
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CROSSTALK_P0_CODEX_WAKE", None)
        core.controls_update(messaging="on", halt=False)
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}})
        codex_now = mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT})
        codex_now.start()  # the identity gate compares the recipient's recorded Codex account with this one
        self.addCleanup(codex_now.stop)
        self.links, self.sleeps, self.spawned = [], [], []
        self.arrive_on_link = False
        # Safety net: nothing in this file can open a real link or start a real process by accident.
        for patcher in (
                mock.patch.object(core, "open_codex_link", side_effect=self.links.append),
                mock.patch.object(core, "spawn_codex_wake", side_effect=self.spawn),
                mock.patch.object(core.subprocess, "Popen", side_effect=AssertionError("process started")),
                mock.patch.object(core, "arrivals", side_effect=self.arrivals),
                mock.patch.object(core, "deliver_codex", side_effect=AssertionError("letter resent"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def spawn(self, letter_id):
        self.spawned.append(letter_id)
        return 4242

    def arrivals(self, letter, timeout=None):
        return (1, "thread-read") if self.arrive_on_link and self.links else (0, "thread-read")

    def letter(self, letter_id=LETTER, state="held", app="codex", thread=None):
        native_id = thread or (CODEX if app == "codex" else CLAUDE)
        core.journal_append(core._letter_row(letter_id, state, to={"app": app, "id": native_id}, sent=True,
                                            created_ns=time.time_ns(),
                                            message=core.frame_message(letter_id, "claude", "sender", "Hello")))
        return letter_id

    def rollout(self, thread=CODEX, rows=(), day="12", filler=0, name_time="2026-10-12T04-17-00"):
        path = self.root / "sessions" / "2026" / "10" / day / f"rollout-{name_time}-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(row) for row in rows]
        if filler:  # one huge ordinary row after the markers pushes them out of the first tail span
            lines.append(json.dumps({"type": "response_item", "payload": {"text": "x" * filler}}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def wake(self, letter_id=LETTER, **kwargs):
        kwargs.setdefault("opener", self.links.append)
        return core.codex_wake(letter_id, delay_s=60.0, sleep=self.sleeps.append, **kwargs)

    def journal_claims(self):
        return [row for row in core.journal_rows() if row.get("codex_link_ns")]

    def state(self, letter_id=LETTER):
        return core.letters_load()[letter_id]["state"]

    # ------------------------------------------------------------ the link
    def test_idle_chat_opens_the_exact_link_once_and_the_letter_arrives(self):
        self.letter()
        self.rollout(rows=[STARTED, COMPLETE])
        self.arrive_on_link = True
        report = self.wake()
        self.assertEqual(self.links, [f"codex://threads/{CODEX}"])
        self.assertEqual(report["wake"], "link-opened")
        self.assertEqual(self.sleeps[0], 60.0)
        self.assertEqual(self.state(), "arrived")
        self.assertEqual(core.letters_load()[LETTER]["codex_link_result"], "opened")
        self.assertEqual(len(self.journal_claims()), 1)
        self.assertEqual(self.journal_claims()[0]["state"], "held")

    def test_chat_whose_last_turn_was_interrupted_is_idle(self):
        self.letter()
        self.rollout(rows=[STARTED, ABORTED])
        self.wake()
        self.assertEqual(self.links, [f"codex://threads/{CODEX}"])

    def test_second_waiter_for_the_same_letter_opens_no_second_link(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        first = self.wake()
        second = self.wake()
        self.assertEqual(first["wake"], "link-opened")
        self.assertEqual(second["wake"], "skipped:link-already-claimed")
        self.assertEqual(len(self.links), 1)
        self.assertEqual(len(self.journal_claims()), 1)
        self.assertEqual(self.state(), "held")

    def test_failed_open_is_recorded_and_never_retried(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        attempts = []

        def broken(url):
            attempts.append(url)
            raise OSError("no handler")

        first = self.wake(opener=broken)
        second = self.wake(opener=broken)
        self.assertEqual(first["wake"], "skipped:open-failed:OSError")
        self.assertEqual(second["wake"], "skipped:link-already-claimed")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(core.letters_load()[LETTER]["codex_link_result"], "open-failed:OSError")
        self.assertEqual(self.state(), "held")

    def test_two_letters_to_one_chat_within_the_cooldown_share_one_link(self):
        first, second = self.letter("CTP0-000000000001"), self.letter("CTP0-000000000002")
        self.rollout(rows=[COMPLETE])
        self.wake(first)
        report = self.wake(second)
        self.assertEqual(len(self.links), 1)
        self.assertEqual(report["wake"], "skipped:link-cooldown")
        self.assertEqual(core.letters_load()[second]["codex_wake"], "skipped:link-cooldown")
        self.assertGreater(len(self.sleeps), 2)  # the second letter still watches for its arrival
        # Once the cooldown is over a letter that is still waiting gets its own link.
        with mock.patch.object(core, "CODEX_LINK_COOLDOWN_S", 0.0):
            self.assertEqual(self.wake(second)["wake"], "link-opened")
        self.assertEqual(len(self.links), 2)

    def test_the_cooldown_is_per_chat(self):
        core.directory_register("codex", OTHER, {"account": {"id": CODEX_ACCOUNT}})
        a, b = self.letter("CTP0-000000000001"), self.letter("CTP0-000000000002", thread=OTHER)
        self.rollout(CODEX, [COMPLETE])
        self.rollout(OTHER, [COMPLETE])
        self.wake(a)
        self.wake(b)
        self.assertEqual(self.links, [f"codex://threads/{CODEX}", f"codex://threads/{OTHER}"])

    # --------------------------------------------------------- no link
    def test_chat_with_a_turn_in_progress_gets_no_link(self):
        for index, rows in enumerate(([STARTED], [COMPLETE, STARTED], [STARTED, COMPLETE, STARTED])):
            with self.subTest(rows=index):
                letter = self.letter(f"CTP0-{index:012x}")
                self.rollout(rows=rows)
                report = self.wake(letter)
                self.assertEqual(report["wake"], "skipped:in-progress")
                self.assertEqual(self.state(letter), "held")
                self.assertEqual(core.letters_load()[letter]["reason"], "codex-wake-skipped:in-progress")
                self.assertEqual(core.letters_load()[letter]["codex_wake"], "skipped:in-progress")
        self.assertEqual(self.links, [])
        self.assertEqual(self.journal_claims(), [])

    def test_unknown_activity_gets_no_link(self):
        self.letter()
        cases = {"rollout-not-found": lambda: None,
                 "no-turn-marker": lambda: self.rollout(rows=[{"type": "session_meta"}, event("token_count")]),
                 "rollout-tail-partial": lambda: self.rollout(rows=[COMPLETE]).write_text(
                     json.dumps(COMPLETE) + '\n{"type":"event_msg","payl', encoding="utf-8")}
        for note, make in cases.items():
            with self.subTest(note=note):
                make()
                report = self.wake()
                self.assertEqual(report["wake"], "skipped:unknown")
                self.assertEqual(report["detail"], note)
        with self.subTest(note="rollout-unreadable"), \
                mock.patch.object(core, "codex_rollout_path", return_value=self.root / "gone.jsonl"):
            self.assertEqual(self.wake()["detail"], "rollout-unreadable")
        self.assertEqual(self.links, [])
        self.assertEqual(self.state(), "held")

    def test_compressed_only_rollout_is_unknown(self):
        self.letter()
        path = self.rollout(rows=[COMPLETE])
        path.rename(path.with_name(path.name + ".zst"))
        self.assertEqual(self.wake()["wake"], "skipped:unknown")
        self.assertEqual(self.links, [])

    def test_readback_unavailable_gets_no_link(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        with mock.patch.object(core, "arrivals", return_value=(None, "codex-cli-unavailable")):
            report = self.wake()
        self.assertEqual(report["wake"], "skipped:readback-unavailable")
        self.assertEqual(self.links, [])

    def test_letter_that_arrived_before_the_delay_ended_gets_no_link(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        with mock.patch.object(core, "arrivals", return_value=(1, "thread-read")), \
                mock.patch.object(core, "codex_thread_activity", side_effect=AssertionError("looked at the chat")):
            report = self.wake()
        self.assertEqual(report["wake"], "done:letter-arrived")
        self.assertEqual(self.state(), "arrived")
        self.assertEqual(self.links, [])
        self.assertEqual(self.sleeps, [60.0])

    def test_halt_deny_and_messaging_off_get_no_link(self):
        self.rollout(rows=[COMPLETE])
        for index, (control, reason) in enumerate((({"halt": True}, "halted"),
                                                   ({"messaging": "off"}, "messaging-off"),
                                                   ({"deny_add": "codex:" + CODEX}, "denied"))):
            with self.subTest(reason=reason):
                core.controls_update(messaging="on", halt=False, deny_remove="codex:" + CODEX)
                core.controls_update(**control)
                letter = self.letter(f"CTP0-{index:012x}")
                self.assertEqual(self.wake(letter)["wake"], "skipped:" + reason)
                self.assertEqual(core.letters_load()[letter]["codex_wake"], "skipped:" + reason)
                self.assertEqual(self.state(letter), "held")
        self.assertEqual(self.links, [])
        self.assertEqual(self.journal_claims(), [])

    def test_halt_just_before_the_link_opens_gets_no_link_and_spends_the_claim(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        with mock.patch.object(core, "controls_check", side_effect=[None, "halted"]):
            report = self.wake()
        self.assertEqual(report["wake"], "skipped:halted")
        self.assertEqual(self.links, [])
        self.assertEqual(core.letters_load()[LETTER]["codex_link_result"], "refused:halted")
        self.assertEqual(self.wake()["wake"], "skipped:link-already-claimed")
        self.assertEqual(self.links, [])

    def test_the_account_is_rechecked_fresh_at_the_open_boundary(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT}) as read:
            self.wake()
        self.assertEqual(self.links, [f"codex://threads/{CODEX}"])
        self.assertEqual(read.call_args.kwargs.get("use_cache"), False)  # never the cached identity

    def test_a_different_unknown_or_unreadable_account_opens_no_link_and_never_resends(self):
        self.rollout(rows=[COMPLETE])
        cases = {"mismatch": {"app": "codex", "id": OTHER},
                 "unknown": {"app": "codex", "id": None, "error": "codex-cli-unavailable"}}
        for index, (state, account) in enumerate(cases.items()):
            with self.subTest(state=state), mock.patch.object(core, "codex_account", return_value=account):
                letter = self.letter(f"CTP0-{index:012x}")
                report = self.wake(letter)
                self.assertEqual(report["wake"], "skipped:account-" + state)
                self.assertEqual(core.letters_load()[letter]["codex_link_result"], "refused:account-" + state)
                self.assertEqual(self.state(letter), "held")
        with mock.patch.object(core, "codex_account", side_effect=OSError("boom")):
            letter = self.letter("CTP0-0000000000ff")
            self.assertEqual(self.wake(letter)["wake"], "skipped:account-unknown")
        self.assertEqual(self.links, [])

    def test_a_chat_with_no_recorded_account_gets_no_link(self):
        core.directory_register("codex", OTHER, {})
        letter = self.letter(thread=OTHER)
        self.rollout(OTHER, [COMPLETE])
        self.assertEqual(self.wake(letter)["wake"], "skipped:account-unknown")
        self.assertEqual(self.links, [])

    def test_the_link_open_goes_through_the_focus_gate_and_the_arrival_confirms_it(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        self.arrive_on_link = True
        self.wake()
        rows = [json.loads(line) for line in (core.device_dir() / "focus" / "log.jsonl").read_text().splitlines()]
        self.assertEqual([(r["kind"], r["target"], r["launched"], r["confirmed"]) for r in rows],
                         [("codex-link", f"codex:{CODEX}", True, True)])

    def test_unsupported_platform_records_and_opens_nothing(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        with mock.patch.object(core, "default_link_opener", return_value=None):
            report = core.codex_wake(LETTER, delay_s=0.0, sleep=self.sleeps.append)
        self.assertEqual(report["wake"], "skipped:unsupported-platform")
        self.assertEqual(core.letters_load()[LETTER]["codex_wake"], "skipped:unsupported-platform")
        self.assertEqual([r["reason"] for r in core.journal_rows() if r.get("codex_wake")],
                         ["codex-link-unsupported-platform"])
        self.assertEqual(self.journal_claims(), [])
        self.assertEqual(self.links, [])

    def test_default_opener_hands_the_exact_url_to_windows(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        with mock.patch.object(core.os, "startfile", create=True) as shell:
            core.codex_wake(LETTER, delay_s=0.0, sleep=self.sleeps.append)  # routed to core.open_codex_link (recorder)
            ORIGINAL_OPEN(f"codex://threads/{CODEX}")
        self.assertEqual(self.links, [f"codex://threads/{CODEX}"])
        shell.assert_called_once_with(f"codex://threads/{CODEX}")
        self.assertIs(core.default_link_opener(), core.open_codex_link)

    def test_claude_recipient_gets_no_waiter_and_no_link(self):
        letter = self.letter("CTP0-00000000c1a0", app="claude")
        self.rollout(rows=[COMPLETE])
        self.assertFalse(core.start_codex_wake(letter))
        self.assertEqual(self.spawned, [])
        report = self.wake(letter)
        self.assertEqual(report["wake"], "skipped:not-a-codex-letter")
        self.assertEqual((self.links, self.sleeps), ([], []))

    def test_wake_never_touches_a_letter_that_is_not_pending(self):
        self.letter(state="arrived")
        self.rollout(rows=[COMPLETE])
        self.assertEqual(self.wake()["wake"], "done:letter-arrived")
        self.assertEqual(self.links, [])
        self.assertFalse(core._note_letter(LETTER, reason="late-note"))
        self.assertEqual(self.state(), "arrived")
        self.assertIsNone(core.letters_load()[LETTER].get("reason"))

    # -------------------------------------------------------- the spawner
    def deliver(self, count=0, text="Hello"):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {"queue_id": "native-q"})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(count, "thread-read")):
            result = core.send_letter("claude", CLAUDE, "codex:" + CODEX, text)
        self.assertEqual(leg.call_count, 1)
        return result

    def test_held_codex_letter_starts_exactly_one_waiter(self):
        result = self.deliver()
        self.assertEqual(result["state"], "held")
        self.assertEqual(self.spawned, [result["letter_id"]])
        letter = core.letters_load()[result["letter_id"]]
        self.assertEqual((letter["codex_wake_pid"], letter["state"]), (4242, "held"))
        self.assertFalse(core.start_codex_wake(result["letter_id"]))  # a letter never gets a second waiter
        self.assertEqual(len(self.spawned), 1)

    def test_held_letter_to_a_busy_chat_says_it_runs_at_turn_end(self):
        # Live 2026-10-19 (CTP0-30df0a09fcd5): the recipient was mid-turn, so the queue held the letter until that
        # turn ended, but the sender was told to "open or resume the recipient chat".
        self.rollout(rows=[COMPLETE, STARTED])
        busy = self.deliver()
        self.assertEqual((busy["state"], busy["recipient_activity"]), ("held", "in-progress"))
        self.assertTrue(busy["guidance"].startswith(core.CODEX_BUSY_GUIDANCE))
        self.assertIn(core.OWNER_REPORT, busy["guidance"])
        self.rollout(rows=[STARTED, COMPLETE])
        idle = self.deliver()
        self.assertEqual(idle["recipient_activity"], "idle")
        self.assertTrue(idle["guidance"].startswith(core.GUIDANCE["held"]))
        with mock.patch.object(core, "codex_thread_activity", side_effect=OSError("unreadable")):
            self.assertEqual(self.deliver()["recipient_activity"], "unknown")
        self.assertEqual(self.deliver(count=1).get("recipient_activity"), None)  # arrived: no extra field

    def test_arrived_letter_and_disabled_wake_start_no_waiter(self):
        self.assertEqual(self.deliver(count=1)["state"], "arrived")
        with mock.patch.dict(os.environ, {"CROSSTALK_P0_CODEX_WAKE": "off"}):
            self.assertEqual(self.deliver()["state"], "held")
        self.assertEqual(self.spawned, [])

    def test_spawn_failure_changes_neither_state_nor_result(self):
        baseline = self.deliver()
        failures = (mock.patch.object(core, "spawn_codex_wake", side_effect=OSError("no process")),
                    mock.patch.object(core, "_note_letter", side_effect=OSError("journal busy")))
        for failure in failures:
            with self.subTest(failure=failure.attribute), failure:
                result = self.deliver()
            self.assertEqual({k: v for k, v in result.items() if k != "letter_id"},
                             {k: v for k, v in baseline.items() if k != "letter_id"})
            self.assertEqual(self.state(result["letter_id"]), "held")
            self.assertIsNone(core.letters_load()[result["letter_id"]].get("codex_link_ns"))

    def test_real_spawner_failure_is_swallowed(self):
        letter = self.letter()
        with mock.patch.object(core, "spawn_codex_wake", ORIGINAL_SPAWN), \
                mock.patch.object(core.subprocess, "Popen", side_effect=FileNotFoundError("python")):
            self.assertFalse(core.start_codex_wake(letter))
        self.assertEqual(self.state(letter), "held")
        self.assertIsNone(core.letters_load()[letter].get("codex_wake_spawned_ns"))

    def test_detached_child_uses_a_windowless_interpreter(self):
        if os.name != "nt":
            self.assertEqual(core.windowless_python(), sys.executable)
            return
        with mock.patch.object(core.sys, "executable", str(Path(__file__).resolve())):
            self.assertEqual(core.windowless_python(), str(Path(__file__).resolve()))  # no sibling pythonw: unchanged
        self.assertTrue(core.windowless_python().lower().endswith(("pythonw.exe", "python.exe")))

    def test_spawner_starts_the_cli_detached_with_the_same_interpreter(self):
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return mock.Mock(pid=777)

        with mock.patch.object(core.subprocess, "Popen", side_effect=popen):
            pid = ORIGINAL_SPAWN(LETTER)
        argv, kwargs = calls[0]
        self.assertEqual(pid, 777)
        self.assertEqual(argv, [core.windowless_python(), "-I", "-B", str(KIT / "crosstalk_p0_cli.py"), "codex-wake", LETTER])
        self.assertEqual((kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]), (subprocess.DEVNULL,) * 3)
        if os.name == "nt":
            for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
                self.assertTrue(kwargs["creationflags"] & getattr(subprocess, name), name)
        else:
            self.assertTrue(kwargs["start_new_session"])

    @unittest.skipUnless(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), "Windows job breakaway")
    def test_spawner_leaves_the_host_job_or_starts_inside_it_when_refused(self):
        calls = []

        def popen(argv, **kwargs):
            calls.append(kwargs["creationflags"])
            if len(calls) == 1:
                raise PermissionError("job forbids breakaway")
            return mock.Mock(pid=778)

        with mock.patch.object(core.subprocess, "Popen", side_effect=popen):
            self.assertEqual(ORIGINAL_SPAWN(LETTER), 778)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(calls[1] & subprocess.DETACHED_PROCESS)

    def test_waiter_reads_back_with_a_longer_limit(self):
        self.letter()
        self.rollout(rows=[STARTED])
        timeouts = []
        with mock.patch.object(core, "arrivals", side_effect=lambda letter, timeout=None: (timeouts.append(timeout),
                                                                                          (0, "thread-read"))[1]):
            self.wake()
        self.assertEqual(timeouts[0], core.CODEX_WAKE_READBACK_TIMEOUT_S)

    # ------------------------------------------------------------ rollouts
    def test_activity_follows_the_last_turn_marker(self):
        mention = {"type": "response_item", "payload": {"type": "message", "text": '"task_started" is just words'}}
        cases = (([STARTED], "in-progress"), ([STARTED, COMPLETE], "idle"), ([STARTED, ABORTED], "idle"),
                 ([COMPLETE, STARTED], "in-progress"), ([COMPLETE, mention], "idle"), ([STARTED, mention], "in-progress"),
                 ([mention], "unknown"))
        for rows, expected in cases:
            with self.subTest(rows=[r["payload"].get("type") for r in rows], expected=expected):
                self.rollout(rows=rows)
                self.assertEqual(core.codex_thread_activity(CODEX)[0], expected)

    def test_activity_reads_further_back_when_the_first_tail_has_no_marker(self):
        self.rollout(rows=[STARTED, COMPLETE], filler=300 * 1024)
        self.assertEqual(core.codex_thread_activity(CODEX), ("idle", "rollout-tail"))
        self.rollout(rows=[COMPLETE, STARTED], filler=300 * 1024)
        self.assertEqual(core.codex_thread_activity(CODEX), ("in-progress", "rollout-tail"))

    def test_rollout_is_found_in_any_day_folder_and_the_newest_file_wins(self):
        older = self.rollout(rows=[STARTED], day="01", name_time="2026-10-01T00-00-00")
        newer = self.rollout(rows=[STARTED, COMPLETE], day="12")
        os.utime(older, (1_000_000, 1_000_000))
        self.assertEqual(core.codex_rollout_path(CODEX), newer)
        self.assertEqual(core.codex_thread_activity(CODEX)[0], "idle")
        self.assertIsNone(core.codex_rollout_path("not-a-uuid"))
        self.assertIsNone(core.codex_rollout_path(OTHER))

    def test_sessions_dir_follows_codex_home_unless_overridden(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "ch")}):
            self.assertEqual(core.codex_sessions_dir(), self.root / "sessions")
            os.environ.pop("CROSSTALK_P0_CODEX_SESSIONS_DIR")
            self.assertEqual(core.codex_sessions_dir(), self.root / "ch" / "sessions")

    # ------------------------------------------------------- the wake budget (rework slice 5)
    def test_over_budget_the_link_is_not_opened_and_nothing_is_claimed_so_it_can_be_retried(self):
        self.letter()
        self.rollout(rows=[COMPLETE])
        core.controls_update(wake_budget={"per_chat": 0})
        report = self.wake()
        self.assertEqual(report["wake"], "skipped:wake-budget")
        self.assertEqual(self.links, [])
        self.assertEqual(self.state(), "held")  # waits in the Codex queue: not lost, never resent
        self.assertEqual(core.letters_load()[LETTER]["codex_wake"], "skipped:wake-budget")
        self.assertEqual(self.journal_claims(), [])  # no link claim was burned
        self.assertEqual(core.codex_budget_retry_candidates(), [])  # still no room
        core.controls_update(wake_budget={"per_chat": 1})
        self.assertEqual(core.codex_budget_retry_candidates(), [LETTER])
        self.assertTrue(core.codex_budget_retry(LETTER))
        self.assertEqual(self.spawned, [LETTER])
        self.assertEqual(core.letters_load()[LETTER]["codex_wake"], "retry-spawned")
        self.assertEqual(core.codex_budget_retry_candidates(), [])  # one waiter at a time
        self.assertEqual(self.wake()["wake"], "link-opened")  # the retried waiter now opens it
        self.assertEqual(len(self.links), 1)

    def test_a_link_open_is_counted_and_a_cooldown_skip_gives_its_slot_back(self):
        first, second = self.letter("CTP0-000000000001"), self.letter("CTP0-000000000002")
        self.rollout(rows=[COMPLETE])
        self.wake(first)
        self.assertEqual(core.wake_budget_status()["used_total"], 1)
        with mock.patch.object(core, "WAKE_BATCH_S", 0.0):  # not batched: the cooldown alone refuses the second
            report = self.wake(second)
        self.assertEqual(report["wake"], "skipped:link-cooldown")
        self.assertEqual(core.wake_budget_status()["used_total"], 1)

    def test_a_retry_never_starts_while_halted_or_for_a_letter_that_is_not_held_back(self):
        self.letter()
        self.assertFalse(core.codex_budget_retry(LETTER))  # not held back by the budget
        core.journal_append(core._letter_row(LETTER, "held", codex_wake="skipped:wake-budget"))
        core.controls_update(halt=True)
        self.assertFalse(core.codex_budget_retry(LETTER))
        self.assertEqual(self.spawned, [])

    # ------------------------------------------- a vanished or skipped waiter is re-spawned (2026-10-20 live defect)
    def stranded(self, pid=4242, age_s=400.0, **fields):
        spawned = time.time_ns() - int(age_s * 1e9)
        core.journal_append(core._letter_row(LETTER, "held", codex_wake_spawned_ns=spawned, codex_wake_pid=pid, **fields))
        return spawned

    def test_a_dead_waiter_with_no_outcome_is_respawned_once_per_tick_and_journaled(self):
        self.letter()
        self.stranded()
        with mock.patch.object(core, "pid_alive", return_value=False):
            self.assertEqual(core.codex_wake_stranded_candidates(), [LETTER])
            self.assertTrue(core.codex_wake_respawn(LETTER))
            self.assertEqual(self.spawned, [LETTER])
            row = core.letters_load()[LETTER]
            self.assertEqual((row["codex_wake"], row["codex_wake_respawns"]), ("respawned", 1))
            self.assertEqual(core.codex_wake_stranded_candidates(), [])  # the new waiter is fresh: one at a time
            self.assertEqual(self.state(), "held")  # never resent, never re-queued

    def test_a_live_recent_waiter_is_left_alone_but_an_ancient_one_is_not(self):
        self.letter()
        self.stranded(age_s=100.0)
        with mock.patch.object(core, "pid_alive", return_value=True):
            self.assertEqual(core.codex_wake_stranded_candidates(), [])
        core.journal_append(core._letter_row(LETTER, "held", codex_wake_spawned_ns=time.time_ns() - int(1e9 * 3600)))
        with mock.patch.object(core, "pid_alive", return_value=True):  # pid reuse: far past delay + watch
            self.assertEqual(core.codex_wake_stranded_candidates(), [LETTER])

    def test_respawns_are_bounded_and_blocked_by_controls_claims_and_headless(self):
        self.letter()
        self.stranded(codex_wake_respawns=core.CODEX_WAKE_MAX_RESPAWNS)
        with mock.patch.object(core, "pid_alive", return_value=False):
            self.assertEqual(core.codex_wake_stranded_candidates(), [])
            core.journal_append(core._letter_row(LETTER, "held", codex_wake_respawns=0))
            core.controls_update(halt=True)
            self.assertFalse(core.codex_wake_respawn(LETTER))
            core.controls_update(halt=False)
            core.journal_append(core._letter_row(LETTER, "held", codex_link_ns=time.time_ns()))
            self.assertEqual(core.codex_wake_stranded_candidates(), [])  # the link was already claimed
        self.assertEqual(self.spawned, [])

    def test_a_turn_in_progress_skip_is_retried_later_not_given_up(self):
        self.letter()
        self.stranded()
        self.rollout(rows=[STARTED])
        self.assertEqual(self.wake()["wake"], "skipped:in-progress")
        with mock.patch.object(core, "pid_alive", return_value=False):
            self.assertEqual(core.codex_wake_stranded_candidates(), [])  # too soon
            later = time.time_ns() + int((core.CODEX_WAKE_SKIP_RETRY_S + 5) * 1e9)
            self.assertEqual(core.codex_wake_stranded_candidates(later), [LETTER])
        self.assertEqual(self.links, [])

    def test_a_waiter_that_dies_by_exception_or_silent_path_still_journals_an_outcome(self):
        self.letter()
        with mock.patch.object(core, "_codex_wake_body", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.wake()
        row = core.letters_load()[LETTER]
        self.assertEqual((row["codex_wake"], row["codex_wake_detail"]), ("skipped:waiter-exit", "RuntimeError"))
        self.assertIn("skipped:waiter-exit", core.CODEX_WAKE_RETRYABLE_SKIPS)

    def test_helper_tick_respawns_through_core(self):
        helper_spec = importlib.util.spec_from_file_location("crosstalk_wake_helper", KIT / "crosstalk_helper.py")
        with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
            helper = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper)
        self.letter()
        self.stranded()
        fake = mock.Mock(clock=time.time)
        with mock.patch.object(core, "pid_alive", return_value=False):
            report = helper.Helper.retry_stranded_codex_wakes(fake)
        self.assertEqual(report, {"respawned": [LETTER]})

    # ----------------------------------------------------------------- CLI
    def test_cli_codex_wake_prints_one_json_line(self):
        self.letter()
        output = io.StringIO()
        with mock.patch.object(core, "arrivals", return_value=(1, "thread-read")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["codex-wake", LETTER, "--delay", "0"]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual((value["wake"], value["state"], value["letter_id"]), ("done:letter-arrived", "arrived", LETTER))
        self.assertEqual(output.getvalue().count("\n"), 1)
        self.assertEqual(cli.build_parser().parse_args(["codex-wake", LETTER]).delay, core.CODEX_WAKE_DELAY_S)


if __name__ == "__main__":
    unittest.main()
