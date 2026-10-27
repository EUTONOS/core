"""Offline tests: an unknown account identity blocks Crosstalk delivery (slice 3B.3).

Claude recipients are checked against the current Claude account, Codex recipients against the current Codex
account. A known mismatch is final (blocked:other-account); an unknown identity on either side is not
(blocked:unknown-identity): nothing is sent, the letter waits and is rechecked. Fixture directories and mocks
only; no real claude or codex call and no real app data."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_identity_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT_A = "99999999-2222-4333-8444-555555555555"
ACCOUNT_B = "88888888-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"
CODEX_A = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CODEX_B = "dddddddd-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-identity-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "main.log"
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
                                          "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.log),
                                          "CROSSTALK_P0_CODEX_WAKE": "off", "CROSSTALK_P0_FOCUS": "off"})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)

    def sessions_folder(self, account):
        folder = self.root / "AppData" / "claude-code-sessions" / account / ORG
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def desktop_is(self, account):
        """Desktop's main.log names this account as the active one (None: the log says nothing)."""
        if account is None:
            self.log.write_text("", encoding="utf-8")
        else:
            self.log.write_text(f"2026-10-13 [info] Loaded 5 persisted sessions from {self.sessions_folder(account)}\n",
                                encoding="utf-8")

    def rows(self, letter_id):
        return [row for row in core.journal_rows() if row.get("letter_id") == letter_id]


class ClaudeRecipientTests(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(core, "claude_registration", return_value={"live": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, account):
        core.directory_register("codex", CODEX, {})
        core.directory_register("claude", CLAUDE, {"account": {"id": account}} if account else {})

    def send(self):
        return core.send_letter("codex", CODEX, "claude:" + CLAUDE, "Hello")

    def delivered(self):
        return mock.patch.object(core, "deliver_claude", return_value=("accepted", {})), \
            mock.patch.object(core, "arrivals", return_value=(1, "read"))

    def assert_unknown_blocked(self, result, leg):
        leg.assert_not_called()
        self.assertEqual(result["state"], "blocked:unknown-identity")
        self.assertFalse(result["sent"])
        self.assertTrue(result["report_to_owner"])
        self.assertNotIn(result["state"], core.TERMINAL_STATES)
        for text in ("NOT SENT", "Claude Desktop", "signs in", "will not be resent blindly", "Do NOT resend"):
            self.assertIn(text, result["guidance"])

    def test_match_delivers(self):
        self.register(ACCOUNT_A)
        self.desktop_is(ACCOUNT_A)
        leg, read = self.delivered()
        with leg as send, read:
            result = self.send()
        self.assertEqual(send.call_count, 1)
        self.assertTrue(result["sent"])

    def test_recorded_account_unknown_blocks(self):
        self.register(None)
        self.desktop_is(ACCOUNT_A)
        with mock.patch.object(core, "deliver_claude") as leg:
            result = self.send()
        self.assert_unknown_blocked(result, leg)
        self.assertIsNone(result["registered_account"])
        self.assertEqual(result["current_account"], ACCOUNT_A)
        self.assertIn("unknown", result["guidance"])

    def test_current_account_unknown_blocks(self):
        self.register(ACCOUNT_A)
        self.desktop_is(None)  # no Desktop answer and no ~/.claude.json
        with mock.patch.object(core, "deliver_claude") as leg:
            result = self.send()
        self.assert_unknown_blocked(result, leg)
        self.assertEqual(result["registered_account"], ACCOUNT_A)
        self.assertIsNone(result["current_account"])

    def test_known_mismatch_stays_final(self):
        self.register(ACCOUNT_A)
        self.desktop_is(ACCOUNT_B)
        with mock.patch.object(core, "deliver_claude") as leg:
            result = self.send()
        leg.assert_not_called()
        self.assertEqual(result["state"], "blocked:other-account")
        self.assertIn(result["state"], core.TERMINAL_STATES)

    def test_refresh_path_still_works_for_a_chat_desktop_holds(self):
        # The recorded account is unknown, but Desktop lists this chat under the active account: reachable, and
        # the binding is refreshed so the account is recorded from now on.
        self.register(None)
        self.desktop_is(ACCOUNT_B)
        (self.sessions_folder(ACCOUNT_B) / "local_abc.json").write_text(json.dumps({"cliSessionId": CLAUDE}))
        leg, read = self.delivered()
        with leg as send, read:
            result = self.send()
        self.assertEqual(send.call_count, 1)
        self.assertTrue(result["sent"])
        self.assertEqual(core.directory_load()["entries"]["claude:" + CLAUDE]["account"]["id"], ACCOUNT_B)

    def test_unknown_letter_is_not_terminal_and_reconcile_never_sends_while_unknown(self):
        self.register(None)
        self.desktop_is(ACCOUNT_A)
        with mock.patch.object(core, "deliver_claude") as leg:
            letter_id = self.send()["letter_id"]
            for _ in range(2):
                report = core.reconcile(resend=True, grace_s=0)
            leg.assert_not_called()
        self.assertEqual(core.letters_load()[letter_id]["state"], "blocked:unknown-identity")
        self.assertEqual(report["resent"], [])
        self.assertEqual([r["state"] for r in self.rows(letter_id)].count("blocked:unknown-identity"), 1)

    def test_unknown_letter_is_sent_once_after_identity_becomes_known(self):
        self.register(None)
        self.desktop_is(ACCOUNT_A)
        with mock.patch.object(core, "deliver_claude") as leg:
            letter_id = self.send()["letter_id"]
        leg.assert_not_called()
        core.directory_register("claude", CLAUDE, {"account": {"id": ACCOUNT_A}})  # re-registration records it
        leg, read = self.delivered()
        with leg as send, read:
            core.reconcile(resend=True, grace_s=0)
            core.reconcile(resend=True, grace_s=0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(core.letters_load()[letter_id]["state"], "arrived")

    def test_mail_slot_hands_an_unknown_identity_letter_to_the_recipient_chat_itself(self):
        self.register(None)
        self.desktop_is(ACCOUNT_A)
        with mock.patch.object(core, "deliver_claude") as leg:
            letter_id = self.send()["letter_id"]
        leg.assert_not_called()
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "complete-scan")):
            pulled = core.mail_slot_pull(CLAUDE)
        self.assertEqual([item["letter_id"] for item in pulled["letters"]], [letter_id])
        self.assertEqual(core.letters_load()[letter_id]["state"], "sending")
        self.assertEqual(core.letters_load()[letter_id]["route"], "mail-slot")

    def test_a_corrupt_desktop_record_is_the_recoverable_unknown_identity_not_the_final_block(self):
        self.register(ACCOUNT_B)
        self.desktop_is(ACCOUNT_A)
        folder = self.sessions_folder(ACCOUNT_A)
        # Every record readable and none is this chat: a known mismatch, final.
        (folder / "local_44444444-2222-4333-8444-555555555555.json").write_text(json.dumps({"cliSessionId": "x"}))
        with mock.patch.object(core, "deliver_claude") as leg:
            final = self.send()
        leg.assert_not_called()
        self.assertEqual(final["state"], "blocked:other-account")
        # One record is corrupt (it may be this chat's): Desktop cannot vouch either way, so not final.
        (folder / "local_66666666-2222-4333-8444-555555555555.json").write_text("{not json")
        with mock.patch.object(core, "deliver_claude") as leg:
            result = self.send()
        leg.assert_not_called()
        self.assertEqual(result["state"], "blocked:unknown-identity")
        self.assertFalse(result["sent"])
        self.assertNotIn(result["state"], core.TERMINAL_STATES)
        self.assertEqual(core.desktop_lookup(core.current_account(), CLAUDE)[0], "unreadable")

    def test_unreadable_account_never_erases_a_recorded_one_on_reregistration(self):
        self.register(ACCOUNT_A)
        core.directory_register("claude", CLAUDE, {"account": {"app": "claude", "id": None}})
        self.assertEqual(core.directory_load()["entries"]["claude:" + CLAUDE]["account"]["id"], ACCOUNT_A)

    def test_list_hint_shows_the_block(self):
        self.register(None)
        self.desktop_is(ACCOUNT_A)
        with mock.patch.object(core, "codex_account", return_value={"app": "codex", "id": None}):
            hints = {row["key"]: row["delivery_hint"] for row in core.list_directory()}
        self.assertEqual(hints["claude:" + CLAUDE], "blocked:unknown-identity")


class CodexRecipientTests(Base):
    def register(self, account):
        core.directory_register("claude", CLAUDE, {})
        core.directory_register("codex", CODEX, {"account": account} if account is not None else {})

    def send(self):
        return core.send_letter("claude", CLAUDE, "codex:" + CODEX, "Hello")

    def current(self, **fields):
        return mock.patch.object(core, "codex_account", return_value=dict({"app": "codex", "id": None}, **fields))

    def blocked_with(self, recorded, current, state):
        self.register(recorded)
        with self.current(**current), mock.patch.object(core, "deliver_codex") as leg:
            result = self.send()
        leg.assert_not_called()
        self.assertEqual(result["state"], state)
        self.assertFalse(result["sent"])
        return result

    def test_match_delivers(self):
        self.register({"id": CODEX_A})
        with self.current(id=CODEX_A), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {"queue_id": "q"})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "thread-read")):
            result = self.send()
        self.assertEqual(leg.call_count, 1)
        self.assertTrue(result["sent"])

    def test_known_mismatch_is_final(self):
        result = self.blocked_with({"id": CODEX_A}, {"id": CODEX_B}, "blocked:other-account")
        self.assertIn(result["state"], core.TERMINAL_STATES)
        self.assertEqual(result["registered_account"], CODEX_A)
        self.assertEqual(result["current_account"], CODEX_B)
        self.assertIn("Codex", result["guidance"])
        self.assertIn("will not be sent later", result["guidance"])

    def test_recorded_account_unknown_blocks_without_being_final(self):
        result = self.blocked_with(None, {"id": CODEX_A}, "blocked:unknown-identity")
        self.assertNotIn(result["state"], core.TERMINAL_STATES)
        self.assertIn("Do NOT resend", result["guidance"])

    def test_recorded_id_missing_blocks_even_with_an_empty_account_record(self):
        self.blocked_with({"app": "codex", "id": None}, {"id": CODEX_A}, "blocked:unknown-identity")

    def test_current_account_unknown_blocks(self):
        self.blocked_with({"id": CODEX_A}, {"error": "codex-cli-unavailable"}, "blocked:unknown-identity")

    def test_current_account_read_failure_blocks(self):
        self.register({"id": CODEX_A})
        with mock.patch.object(core, "codex_account", side_effect=OSError("boom")), \
                mock.patch.object(core, "deliver_codex") as leg:
            result = self.send()
        leg.assert_not_called()
        self.assertEqual(result["state"], "blocked:unknown-identity")

    def test_email_hash_is_compared_when_an_id_is_missing_on_one_side(self):
        self.register({"id": None, "email_sha256_12": "abc123abc123"})
        with self.current(id=CODEX_A, email_sha256_12="abc123abc123"), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {"queue_id": "q"})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "thread-read")):
            self.send()
        self.assertEqual(leg.call_count, 1)
        self.blocked_with({"id": None, "email_sha256_12": "abc123abc123"},
                          {"id": CODEX_A, "email_sha256_12": "fff000fff000"}, "blocked:other-account")

    def test_unknown_letter_is_not_terminal_and_reconcile_never_sends_while_unknown(self):
        self.register(None)
        with self.current(id=CODEX_A), mock.patch.object(core, "deliver_codex") as leg:
            letter_id = self.send()["letter_id"]
            for _ in range(2):
                report = core.reconcile(resend=True, grace_s=0)
            leg.assert_not_called()
        self.assertEqual(core.letters_load()[letter_id]["state"], "blocked:unknown-identity")
        self.assertEqual(report["resent"], [])
        self.assertEqual([r["state"] for r in self.rows(letter_id)].count("blocked:unknown-identity"), 1)

    def test_unknown_letter_is_sent_once_after_the_account_is_recorded(self):
        self.register(None)
        with self.current(id=CODEX_A), mock.patch.object(core, "deliver_codex") as leg:
            letter_id = self.send()["letter_id"]
        leg.assert_not_called()
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_A}})  # the chat calls status again
        with self.current(id=CODEX_A), \
                mock.patch.object(core, "deliver_codex", return_value=("accepted", {"queue_id": "q"})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "thread-read")):
            core.reconcile(resend=True, grace_s=0)
            core.reconcile(resend=True, grace_s=0)
        self.assertEqual(leg.call_count, 1)
        self.assertEqual(core.letters_load()[letter_id]["state"], "arrived")

    def test_mismatch_letter_is_never_sent_later(self):
        letter_id = self.blocked_with({"id": CODEX_A}, {"id": CODEX_B}, "blocked:other-account")["letter_id"]
        core.directory_register("codex", CODEX, {"account": {"id": CODEX_B}})
        with self.current(id=CODEX_B), mock.patch.object(core, "deliver_codex") as leg:
            core.reconcile(resend=True, grace_s=0)
            leg.assert_not_called()
        self.assertEqual(core.letters_load()[letter_id]["state"], "blocked:other-account")

    def test_consent_and_halt_still_refuse_before_the_identity_gate(self):
        self.register({"id": CODEX_A})
        core.controls_update(halt=True)
        with self.current(id=CODEX_B), mock.patch.object(core, "deliver_codex") as leg:
            result = self.send()
        leg.assert_not_called()
        self.assertEqual(result["state"], "refused")


if __name__ == "__main__":
    unittest.main()
