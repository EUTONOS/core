"""Offline tests for the one pre-send guard (Crosstalk rework slice 1). Fixture directories and mocks only."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_guard_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HERMES = "20261019_000000_abcdef"
ACCOUNT = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee"
LETTER = "CTP0-0000000000a1"


def framed(letter_id=LETTER, text="Hello"):
    return core.frame_message(letter_id, "claude", "sender", text)


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-guard-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {
            "CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
            "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"), "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "c.json"),
            "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.root / "main.log"),
            "CROSSTALK_P0_CODEX_WAKE": "off", "CROSSTALK_P0_FOCUS": "off"})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)

    def journal(self, to, *, letter_id=LETTER, message=None, sender=None, extra_rows=()):
        core.journal_append(core._letter_row(
            letter_id, "journaled", to=to, message=framed(letter_id) if message is None else message,
            created_ns=time.time_ns(), text_chars=5, **{"from": sender or {"app": "claude", "id": CLAUDE}}))
        for state, row in extra_rows:
            core.journal_append(core._letter_row(letter_id, state, **row))
        return letter_id

    def codex_letter(self, **kw):
        core.directory_register("codex", CODEX, {"account": {"id": ACCOUNT}})
        return self.journal({"app": "codex", "id": CODEX}, **kw)


class GuardRefusalTests(Base):
    def test_a_clean_letter_passes(self):
        letter = self.codex_letter()
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}):
            self.assertIsNone(core.pre_send_guard(letter, "codex-queue"))

    def test_halt_messaging_off_and_deny_refuse_with_the_existing_reasons(self):
        letter = self.codex_letter()
        for change, reason in (({"halt": True}, "halted"), ({"halt": False, "messaging": "off"}, "messaging-off"),
                               ({"messaging": "on", "deny_add": "codex:" + CODEX}, "denied")):
            core.controls_update(**change)
            self.assertEqual(core.pre_send_guard(letter, "codex-queue"), reason)

    def test_sender_is_not_the_target(self):
        letter = self.codex_letter(sender={"app": "codex", "id": CODEX})
        self.assertEqual(core.pre_send_guard(letter, "codex-queue"), "sender-is-recipient")

    def test_payload_cap(self):
        long = self.codex_letter(message=framed(text="x" * (core.MAX_TEXT_CHARS + core.MESSAGE_FRAME_OVERHEAD)))
        self.assertEqual(core.pre_send_guard(long, "codex-queue"), "text-too-long")

    def test_letter_framing(self):
        messages = ("no header\nbody\nref " + LETTER,
                    framed().replace("ref " + LETTER, "ref CTP0-other"),
                    "Crosstalk message from Claude (x) ref " + LETTER)
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                letter = self.codex_letter(letter_id=f"CTP0-00000000b{index:03d}", message=message)
                self.assertEqual(core.pre_send_guard(letter, "codex-queue"), "letter-framing-invalid")

    def test_one_attempt_refuses_a_second_but_not_its_own_row(self):
        letter = self.codex_letter(extra_rows=[("sending", {"attempt_id": "aaaa"})])
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}):
            self.assertIsNone(core.pre_send_guard(letter, "codex-queue", attempt_id="aaaa"))
            self.assertEqual(core.pre_send_guard(letter, "codex-queue", attempt_id="bbbb"), "already-attempted")
            self.assertEqual(core.pre_send_guard(letter, "codex-queue"), "already-attempted")
            self.assertEqual(core.pre_send_guard(letter, "desktop-relay"), "already-attempted")
            # the exception is the mail slot only, and only with proven zero arrivals
            self.assertEqual(core.pre_send_guard(letter, "mail-slot"), "already-attempted")
            self.assertEqual(core.pre_send_guard(letter, "codex-queue", zero_arrivals_proven=True),
                             "already-attempted")

    def test_a_concurrent_readback_row_is_not_a_second_attempt(self):
        # 2026-10-20 CTP0-826f792fe8b0: check_arrival re-recorded "sending" (no attempt_id) while the send ran.
        letter = self.codex_letter(extra_rows=[
            ("sending", {"attempt_id": "aaaa", "route": "codex-queue"}),
            ("sending", {"arrivals": 0, "readback_note": "rollout-scan", "readback_ns": 5, "sending_ns": 4})])
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}):
            self.assertIsNone(core.pre_send_guard(letter, "codex-queue", attempt_id="aaaa"))
            # still one attempt: another attempt id, or a caller with none, is refused
            self.assertEqual(core.pre_send_guard(letter, "codex-queue", attempt_id="bbbb"), "already-attempted")
            self.assertEqual(core.pre_send_guard(letter, "codex-queue"), "already-attempted")

    def test_a_legacy_attempt_row_without_attempt_id_still_refuses(self):
        letter = self.codex_letter(extra_rows=[("sending", {"route": "codex-queue"}),
                                               ("sending", {"readback_note": "x", "readback_ns": 9})])
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}):
            self.assertEqual(core.pre_send_guard(letter, "codex-queue", attempt_id="aaaa"), "already-attempted")

    def test_check_arrival_and_note_carry_the_current_attempt_id(self):
        letter = self.codex_letter(extra_rows=[("sending", {"attempt_id": "aaaa", "route": "codex-queue"})])
        with mock.patch.object(core, "arrivals", return_value=(0, "rollout-scan")):
            core.check_arrival(letter)
        row = core.letters_load()[letter]["history"][-1]
        self.assertEqual((row["state"], row.get("attempt_id")), ("sending", "aaaa"))
        core.journal_append(core._letter_row(letter, "held", sent=True, attempt_id="aaaa"))
        self.assertTrue(core._note_letter(letter, codex_wake="x"))
        self.assertEqual(core.letters_load()[letter]["history"][-1].get("attempt_id"), "aaaa")

    def test_mail_slot_exception_with_zero_arrivals_proven(self):
        letter = self.codex_letter(extra_rows=[("uncertain", {"attempt_id": "aaaa", "reason": "x"})])
        self.assertIsNone(core.pre_send_guard(letter, "mail-slot", zero_arrivals_proven=True))

    def test_codex_account_is_read_fresh_every_time(self):
        letter = self.codex_letter()
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}) as read:
            core.pre_send_guard(letter, "codex-queue")
        read.assert_called_once()
        self.assertIs(read.call_args.kwargs.get("use_cache"), False)
        for current, reason in (({"app": "codex", "id": OTHER}, "account-recheck:other-account"),
                                ({"app": "codex", "id": None}, "account-recheck:unknown-identity")):
            with mock.patch.object(core, "codex_account", return_value=current):
                self.assertEqual(core.pre_send_guard(letter, "codex-queue"), reason)

    def test_a_warm_cache_is_not_trusted(self):
        letter = self.codex_letter()
        core._ACCOUNT_CACHE.update(at=time.monotonic(), value={"app": "codex", "id": ACCOUNT})
        self.addCleanup(core._ACCOUNT_CACHE.update, at=0.0, value=None)
        with mock.patch.object(core.subprocess, "Popen", side_effect=OSError("no codex")), \
                mock.patch.object(core.subprocess, "run", side_effect=OSError("no codex")):
            result = core.pre_send_guard(letter, "codex-queue")
        self.assertEqual(result, "account-recheck:unknown-identity")

    def test_claude_account_recheck_uses_block_reason(self):
        letter = self.journal({"app": "claude", "id": CLAUDE}, sender={"app": "codex", "id": CODEX})
        for blocked, expected in (("blocked:other-account", "account-recheck:other-account"),
                                  ("blocked:unknown-identity", "account-recheck:unknown-identity"),
                                  ("blocked:not-running", None), (None, None)):
            with mock.patch.object(core, "block_reason", return_value=blocked):
                self.assertEqual(core.pre_send_guard(letter, "claude-session-send"), expected)
        with mock.patch.object(core, "block_reason", return_value="blocked:other-account"):
            self.assertIsNone(core.pre_send_guard(letter, "mail-slot", zero_arrivals_proven=True))


class RouteWiringTests(Base):
    """Each route calls the guard immediately before its native effect, after the `sending` row exists."""

    def run_route(self, to, deliver):
        order = []
        real = core.pre_send_guard

        def guard(*args, **kwargs):
            order.append("guard")
            return real(*args, **kwargs)

        def effect(*args, **kwargs):
            order.append("effect")
            return "accepted", {}
        sender = ({"app": "codex", "id": CODEX} if to["app"] == "claude" else {"app": "claude", "id": CLAUDE})
        letter = self.journal(to, sender=sender)
        core.directory_register(to["app"], to["id"], {"account": {"id": ACCOUNT}})
        with mock.patch.object(core, "pre_send_guard", guard), mock.patch.object(core, deliver, effect), \
                mock.patch.object(core, "block_reason", return_value=None), \
                mock.patch.object(core, "arrivals", return_value=(1, "read")), \
                mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}), \
                mock.patch.object(core, "start_codex_wake"):
            core.attempt_delivery(letter)
        return order, letter

    def test_claude_codex_and_hermes_call_the_guard_right_before_the_effect(self):
        for to, deliver in (({"app": "claude", "id": CLAUDE}, "deliver_claude"),
                            ({"app": "codex", "id": CODEX}, "deliver_codex"),
                            ({"app": "hermes", "id": HERMES}, "deliver_hermes")):
            with self.subTest(route=deliver):
                order, letter = self.run_route(to, deliver)
                self.assertEqual(order, ["guard", "effect"])
                self.assertEqual(core.letters_load()[letter]["state"], "arrived")
                core.journal_path().unlink()

    def test_a_guard_refusal_stops_the_effect_and_is_journaled_refused(self):
        letter = self.codex_letter()
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": ACCOUNT}), \
                mock.patch.object(core, "pre_send_guard", return_value="halted"), \
                mock.patch.object(core, "deliver_codex") as effect:
            result = core.attempt_delivery(letter)
        effect.assert_not_called()
        self.assertEqual((result["state"], result["reason"], result["sent"]), ("refused", "halted", False))
        self.assertEqual(core.letters_load()[letter]["state"], "refused")

    def test_an_account_change_after_the_claim_is_caught_by_the_fresh_read(self):
        letter = self.codex_letter()
        reads = [{"app": "codex", "id": ACCOUNT}, {"app": "codex", "id": OTHER}]
        with mock.patch.object(core, "codex_account", side_effect=lambda **k: reads.pop(0)), \
                mock.patch.object(core, "deliver_codex") as effect:
            result = core.attempt_delivery(letter)
        effect.assert_not_called()
        self.assertEqual((result["state"], result["reason"]), ("refused", "account-recheck:other-account"))

    def test_the_mail_slot_calls_the_guard_and_applies_its_refusal(self):
        letter = self.journal({"app": "claude", "id": CLAUDE}, sender={"app": "codex", "id": CODEX},
                              extra_rows=[("blocked:not-running", {"reason": "not-running"})])
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "read")):
            with mock.patch.object(core, "pre_send_guard", return_value="halted") as guard:
                refused = core.mail_slot_pull(CLAUDE)
            guard.assert_called_once_with(letter, "mail-slot", zero_arrivals_proven=True)
            self.assertEqual(refused["letters"], [])
            self.assertEqual(refused["skipped"], [{"letter_id": letter, "why": "halted"}])
            handed = core.mail_slot_pull(CLAUDE)
        self.assertEqual([item["letter_id"] for item in handed["letters"]], [letter])

    def test_the_mail_slot_hands_out_an_uncertain_letter_again_only_after_zero_arrivals(self):
        letter = self.journal({"app": "claude", "id": CLAUDE}, sender={"app": "codex", "id": CODEX},
                              extra_rows=[("sending", {"attempt_id": "aaaa", "route": "claude-session-send"}),
                                          ("uncertain", {"attempt_id": "aaaa", "reason": "sender-timeout"})])
        with mock.patch.object(core, "claude_arrivals", return_value=(None, "unreadable")):
            self.assertEqual(core.mail_slot_pull(CLAUDE)["letters"], [])
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "read")):
            self.assertEqual([item["letter_id"] for item in core.mail_slot_pull(CLAUDE)["letters"]], [letter])


if __name__ == "__main__":
    unittest.main()
