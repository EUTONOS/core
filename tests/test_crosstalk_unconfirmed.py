"""Offline tests for slice 3B.15: Codex readback from the rollout file (never thread/read of a huge thread), the
re-read before a letter is reported held, and the one-time notice to the sender of a letter that never arrives.
Fixture files and mocks only; no real app, account, rollout or transport is used."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_unconfirmed_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    helper_spec = importlib.util.spec_from_file_location("crosstalk_unconfirmed_helper", KIT / "crosstalk_helper.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SENDER = "dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee"  # a registered Codex chat that sent the stuck letter
LETTER = "CTP0-0123456789ab"
OTHER = "CTP0-ba9876543210"
MINUTE_NS = 60 * 10**9
HOUR_NS = 3600 * 10**9


def user_rows(letter_id: str, sender: str = "Claude (sender)") -> list[dict]:
    """The two row kinds Codex writes for one user input (shapes verified on real rollouts, 2026-10-20)."""
    text = f"Crosstalk message from {sender}\nHello\nref {letter_id}"
    return [{"timestamp": "t", "type": "response_item", "payload": {
                "type": "message", "role": "user", "id": "m1", "content": [{"type": "input_text", "text": text}]}},
            {"timestamp": "t", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": "x", "item": {
                    "type": "UserMessage", "id": "item-1", "content": [{"type": "text", "text": text}]}}}]


def assistant_row(letter_id: str) -> dict:
    return {"timestamp": "t", "type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": f"Crosstalk message from Claude (x)\nHello\nref {letter_id}"}]}}


def write(path: Path, rows) -> None:
    with path.open("ab") as stream:
        for row in rows:
            stream.write((row if isinstance(row, bytes) else json.dumps(row).encode()) + b"\n")


class RolloutReadbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-rollout-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "rollout-x.jsonl"

    def scan(self, offset=None, timeout=5.0):
        return core.codex_rollout_arrivals(self.path, LETTER, offset, timeout)

    def test_a_genuine_user_input_counts_once_across_its_row_kinds(self):
        write(self.path, [{"type": "session_meta", "payload": {}}] + user_rows(LETTER))
        self.assertEqual(self.scan(), (1, "rollout-tail-scan"))

    def test_quoted_assistant_tool_and_developer_text_never_counts(self):
        tool = {"type": "response_item", "payload": {"type": "function_call_output", "output": f"ref {LETTER}"}}
        developer = {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": f"Crosstalk message from A (b)\nx\nref {LETTER}"}]}}
        mention = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": f"I saw {LETTER} in the log"}]}}
        write(self.path, [assistant_row(LETTER), tool, developer, mention, b"not json " + LETTER.encode()])
        self.assertEqual(self.scan()[0], 0)

    def test_the_offset_scans_forward_from_send_time_only(self):
        write(self.path, user_rows(LETTER))  # an older input with the same id is before the send: not counted
        offset = self.path.stat().st_size
        write(self.path, [assistant_row("CTP0-000000000000")])
        self.assertEqual(self.scan(offset), (0, "rollout-scan"))
        write(self.path, user_rows(LETTER))
        self.assertEqual(self.scan(offset), (1, "rollout-scan"))

    def test_a_missing_or_stale_offset_falls_back_to_the_tail(self):
        write(self.path, user_rows(LETTER))
        self.assertEqual(self.scan(self.path.stat().st_size + 10)[0], 1)  # offset past the end: the file was replaced

    def test_a_huge_row_is_skipped_not_loaded_and_the_scan_is_bounded(self):
        write(self.path, user_rows(LETTER))
        write(self.path, [b'{"type":"response_item","payload":{"type":"message","role":"user","content":['
                          b'{"type":"input_image","image_url":"' + b"A" * (3 * 1024 * 1024) + b'"}]}}'])
        write(self.path, [b'{"pad":"' + LETTER.encode() + b'ref ' + b"B" * (400 * 1024) + b'"}'])  # an oversized row naming it
        with mock.patch.object(core, "ROLLOUT_SCAN_CHUNK", 1024 * 1024):
            self.assertEqual(self.scan()[0], 1)
            with mock.patch.object(core, "ROLLOUT_TAIL_SCAN_BYTES", 1024 * 1024):
                self.assertEqual(self.scan()[0], 0)  # the tail scan never reaches the old input
            with mock.patch.object(core, "ROLLOUT_SCAN_MAX_BYTES", 1024 * 1024):
                self.assertEqual(self.scan(0), (None, "rollout-scan-capped"))
            self.assertEqual(self.scan(0, timeout=0.0), (None, "rollout-readback-timeout"))

    def test_codex_arrivals_reads_the_rollout_and_never_starts_the_app_server(self):
        write(self.path, user_rows(LETTER))
        with mock.patch.object(core, "codex_rollout_path", return_value=self.path), \
                mock.patch.object(core, "AppServer", side_effect=AssertionError("thread/read must not run")):
            self.assertEqual(core.codex_arrivals(CODEX, LETTER)[0], 1)
            self.assertEqual(core.codex_arrivals(CODEX, OTHER)[0], 0)


class BaseCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-unconfirmed-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"),
                                           "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                           "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                           "CROSSTALK_P0_CODEX_WAKE": "off", "CROSSTALK_P0_FOCUS": "off"})
        env.start()
        self.addCleanup(env.stop)
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_ACCOUNT}, "title": "Builder"})
        core.directory_register("codex", SENDER, {"account": {"id": CODEX_ACCOUNT}})
        account = mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": CODEX_ACCOUNT})
        account.start()
        self.addCleanup(account.stop)
        core.controls_update(messaging="on")

    def letter(self, letter_id=LETTER, state="held", age_ns=10 * MINUTE_NS, sender=("claude", CLAUDE), **extra):
        now = time.time_ns()
        core.journal_append(core._letter_row(
            letter_id, "journaled", to={"app": "codex", "id": CODEX}, created_ns=now - age_ns,
            message=core.frame_message(letter_id, sender[0], "sender", "Hello"),
            **{"from": {"app": sender[0], "id": sender[1], "turn_id": None}}))
        core.journal_append(core._letter_row(letter_id, "sending", sending_ns=now - age_ns, route="codex-queue"))
        core.journal_append(core._letter_row(letter_id, state, sent=True, **extra))
        return letter_id

    def notices(self):
        return [row for row in core.letters_load().values() if row.get("notice_for")]


class SendPathTests(BaseCase):
    def send(self):
        return core.send_letter("claude", CLAUDE, "codex:" + CODEX, "Hello")

    def test_the_rollout_size_is_recorded_at_send_time_as_the_offset(self):
        rollout = self.root / "rollout.jsonl"
        write(rollout, [{"type": "session_meta"}])
        size = rollout.stat().st_size
        with mock.patch.object(core, "codex_rollout_path", return_value=rollout), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", return_value=(1, "rollout-scan")):
            result = self.send()
        self.assertEqual(core.letters_load()[result["letter_id"]]["recipient_offset"], size)

    def test_an_unknown_rollout_records_no_offset_and_readback_uses_the_tail(self):
        with mock.patch.object(core, "codex_rollout_path", return_value=None), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", return_value=(1, "x")):
            result = self.send()
        self.assertNotIn("recipient_offset", core.letters_load()[result["letter_id"]])

    def test_a_letter_that_lands_a_moment_later_is_reported_arrived_not_held(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", side_effect=[(0, "n"), (0, "n"), (1, "n")]) as read, \
                mock.patch.object(core.time, "sleep") as pause:
            result = self.send()
        self.assertEqual(result["state"], "arrived")
        self.assertEqual(read.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    def test_hermes_is_re_read_too(self):
        # The plugin's inject returns before Hermes writes the user row (live 2026-10-20).
        self.assertEqual(len(core.held_reread_delays("hermes")), 3)

    def test_still_unseen_stays_held_with_plain_text_and_one_held_row(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(0, "n")) as read, \
                mock.patch.object(core.time, "sleep"):
            result = self.send()
        self.assertEqual((result["state"], leg.call_count), ("held", 1))
        self.assertEqual(read.call_count, 1 + len(core.held_reread_delays("codex")))
        self.assertIn("QUEUED, NOT YET CONFIRMED", result["guidance"])
        self.assertIn("Do NOT resend", result["guidance"])
        states = [row["state"] for row in core.letters_load()[result["letter_id"]]["history"]]
        self.assertEqual(states.count("held"), 1)

    def test_an_unavailable_readback_is_not_re_read(self):
        with mock.patch.object(core, "deliver_codex", return_value=("accepted", {})), \
                mock.patch.object(core, "arrivals", return_value=(None, "rollout-readback-timeout")) as read, \
                mock.patch.object(core.time, "sleep") as pause:
            self.assertEqual(self.send()["state"], "held")
        self.assertEqual((read.call_count, pause.call_count), (1, 0))


class NudgeTests(BaseCase):
    def run_pass(self, **kwargs):
        with mock.patch.object(core, "arrivals", return_value=kwargs.pop("readback", (0, "n"))), \
                mock.patch.object(core, "attempt_delivery", side_effect=lambda lid: {"state": "accepted", "letter_id": lid}) as send:
            report = core.nudge_unconfirmed(**kwargs)
        return report, send

    def test_one_notice_to_the_sender_only_never_a_resend_and_never_twice(self):
        letter_id = self.letter(sender=("codex", SENDER))
        report, send = self.run_pass()
        self.assertEqual([row["letter_id"] for row in report["nudged"]], [letter_id])
        notice = self.notices()[0]
        self.assertEqual(send.call_args.args[0], notice["letter_id"])  # delivery was asked for the notice only
        self.assertEqual(notice["to"], {"app": "codex", "id": SENDER})  # the sender, never the recipient
        self.assertEqual(notice["from"]["app"], "crosstalk")
        self.assertTrue(notice["message"].startswith("Crosstalk message from Crosstalk (delivery notice)\n"))
        self.assertTrue(notice["message"].endswith("\nref " + notice["letter_id"]))
        for needed in (letter_id, "Builder", CODEX, "held", "not confirmed yet", "Do not resend"):
            self.assertIn(needed, notice["message"])
        self.assertEqual(core.pre_send_guard(notice["letter_id"], "codex-queue"), None)
        self.assertEqual(core.letters_load()[letter_id]["state"], "held")  # untouched
        again, send = self.run_pass()  # exactly one nudge per letter
        send.assert_not_called()
        self.assertEqual(again["nudged"], [])
        self.assertEqual(len(self.notices()), 1)

    def test_a_young_letter_a_settled_letter_and_a_late_arrival_get_no_notice(self):
        self.letter("CTP0-00000000young", age_ns=MINUTE_NS)
        self.letter("CTP0-0000000arrived", state="arrived")
        self.letter("CTP0-000000000found", state="held")
        report, send = self.run_pass(readback=(1, "n"))  # the fresh readback now finds them
        send.assert_not_called()
        self.assertEqual(report["nudged"], [])
        self.assertEqual(core.letters_load()["CTP0-000000000found"]["state"], "arrived")

    def test_never_a_notice_about_a_notice(self):
        self.letter()
        self.run_pass()
        notice = self.notices()[0]
        core.journal_append(core._letter_row(notice["letter_id"], "held", sent=True))
        core.journal_append(core._letter_row(notice["letter_id"], "held", sending_ns=time.time_ns() - HOUR_NS))
        _report, send = self.run_pass(now_ns=time.time_ns() + 2 * HOUR_NS)
        send.assert_not_called()
        self.assertEqual(len(self.notices()), 1)

    def test_halt_messaging_off_and_deny_stop_the_notice_and_it_is_sent_later(self):
        self.letter()
        core.controls_update(halt=True)
        report, send = self.run_pass()
        send.assert_not_called()
        self.assertEqual(report["blocked"], "halted")
        core.controls_update(halt=False, messaging="off")
        self.assertEqual(self.run_pass()[0]["blocked"], "messaging-off")
        core.controls_update(messaging="on", deny_add=f"claude:{CLAUDE}")
        report, send = self.run_pass()
        send.assert_not_called()
        self.assertEqual(report["skipped"][0]["why"], "denied")
        self.assertEqual(self.notices(), [])  # nothing was claimed: it goes out once the control is lifted
        core.controls_update(deny_remove=f"claude:{CLAUDE}")
        self.assertEqual(len(self.run_pass()[0]["nudged"]), 1)

    def test_an_asleep_sender_over_the_wake_budget_is_skipped_and_not_marked(self):
        self.letter()
        core.controls_update(wake_budget={"per_chat": 0})
        report, send = self.run_pass()
        send.assert_not_called()
        self.assertEqual(report["skipped"][0]["why"], "wake-budget")
        self.assertNotIn("nudged_ns", core.letters_load()[LETTER])
        core.controls_update(wake_budget={"per_chat": 6})
        self.assertEqual(len(self.run_pass()[0]["nudged"]), 1)

    def test_at_most_one_notice_per_pass(self):
        self.letter("CTP0-0000000000a1")
        self.letter("CTP0-0000000000a2")
        report, send = self.run_pass()
        self.assertEqual((len(report["nudged"]), send.call_count), (1, 1))
        self.assertEqual(len(self.run_pass()[0]["nudged"]), 1)
        self.assertEqual(len(self.run_pass()[0]["nudged"]), 0)

    def test_after_24_hours_the_letter_is_retired_with_one_final_notice_and_never_resent(self):
        letter_id = self.letter(age_ns=25 * HOUR_NS, nudged_ns=1)  # the 5 minute nudge already went out
        report, send = self.run_pass()
        self.assertEqual(report["retired"], [letter_id])
        letter = core.letters_load()[letter_id]
        self.assertEqual(letter["state"], "stale-unconfirmed")
        self.assertTrue(letter["reason"].startswith("auto-retired-older-than-24h:was-held"))
        self.assertEqual([row["letter_id"] for row in report["final"]], [letter_id])
        notice = self.notices()[0]
        self.assertIn("retired", notice["message"])
        self.assertIn("not resent", notice["message"])
        self.assertEqual(notice["to"], {"app": "claude", "id": CLAUDE})
        self.assertEqual(send.call_args.args[0], notice["letter_id"])
        _again, send = self.run_pass()
        send.assert_not_called()
        self.assertEqual(len(self.notices()), 1)

    def test_an_owner_retired_letter_gets_no_final_notice(self):
        letter_id = self.letter(age_ns=25 * HOUR_NS)
        core.mark_stale(24)
        self.assertEqual(core.letters_load()[letter_id]["state"], "stale-unconfirmed")
        _report, send = self.run_pass()
        send.assert_not_called()

    def test_a_sender_app_that_refuses_the_notice_falls_back_to_a_helper_notice(self):
        letter_id = self.letter()
        with mock.patch.object(core, "arrivals", return_value=(0, "n")), \
                mock.patch.object(core, "attempt_delivery", return_value={"state": "blocked:other-account"}):
            core.nudge_unconfirmed()
        shown = core.helper_notices_load()["notices"][core.NOTICE_FALLBACK_PREFIX + letter_id]
        self.assertIn(letter_id, shown["message"])
        core.journal_append(core._letter_row(letter_id, "arrived", sent=True))
        core.nudge_unconfirmed()  # the notice ends with the letter
        self.assertNotIn(core.NOTICE_FALLBACK_PREFIX + letter_id, core.helper_notices_load()["notices"])

    def test_a_real_notice_goes_through_the_normal_send_path_and_its_guard(self):
        letter_id = self.letter(sender=("codex", SENDER))
        with mock.patch.object(core, "arrivals", return_value=(0, "n")), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {})) as leg:
            core.nudge_unconfirmed()
        self.assertEqual(leg.call_count, 1)
        self.assertEqual(leg.call_args.args[0], SENDER)  # the native leg ran once, to the sender, with the notice
        self.assertTrue(leg.call_args.args[1].startswith("Crosstalk message from Crosstalk (delivery notice)"))
        notice = self.notices()[0]
        self.assertEqual(notice["state"], "held")
        self.assertEqual(notice["notice_for"], letter_id)
        self.assertEqual(core.letters_load()[letter_id]["state"], "held")
        core.controls_update(halt=True)
        self.assertEqual(core.pre_send_guard(notice["letter_id"], "codex-queue"), "halted")


class HelperTickTests(BaseCase):
    def test_the_helper_runs_the_pass_at_most_once_a_minute(self):
        now = [1_000_000.0]
        runner = helper.Helper(clock=lambda: now[0])
        with mock.patch.object(core, "nudge_unconfirmed", return_value={"nudged": []}) as run:
            runner.nudge_letters()
            runner.nudge_letters()
            now[0] += helper.NUDGE_EVERY_S + 1
            runner.nudge_letters()
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
