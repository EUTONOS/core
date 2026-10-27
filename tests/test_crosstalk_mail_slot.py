"""Offline tests: a letter to a Claude chat that is not running waits in its mail slot (no relay chat).

Fixture directories and mocks only; no real claude call, no real app data. The helper's wake and the plugin's
relink are tested in test_crosstalk_helper."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_mail_slot_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

CLAUDE = "11111111-2222-4333-8444-555555555555"
OTHER = "22222222-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
LOCAL = "local_33333333-2222-4333-8444-555555555555"
ACCOUNT = "88888888-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"


class MailSlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-mail-slot-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        log = self.root / "main.log"
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"),
                                          "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                          "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                          "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
                                          "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(log),
                                          "CROSSTALK_P0_FOCUS": "off", "CROSSTALK_P0_CODEX_WAKE": "off"})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT}}))
        self.sessions = self.root / "AppData" / "claude-code-sessions" / ACCOUNT / ORG
        self.sessions.mkdir(parents=True)
        self.record = self.sessions / f"{LOCAL}.json"
        self.record.write_text(json.dumps({"cliSessionId": CLAUDE}))
        log.write_text(f"[info] Loaded 3 persisted sessions from {self.sessions}\n", encoding="utf-8")
        core.controls_update(messaging="on", halt=False)
        core.directory_register("claude", CLAUDE, {"account": {"id": ACCOUNT}})
        core.directory_register("codex", CODEX, {})
        registration = mock.patch.object(core, "claude_registration", return_value={
            "live": False, "entrypoint": "claude-desktop", "name": None, "status": None, "cwd": None})  # asleep
        registration.start()
        self.addCleanup(registration.stop)
        leg = mock.patch.object(core, "deliver_claude", side_effect=AssertionError("headless leg used"))
        leg.start()
        self.addCleanup(leg.stop)

    def send(self):
        return core.send_letter("codex", CODEX, "claude:" + CLAUDE, "Hello")

    def test_an_asleep_chat_blocks_the_letter_and_the_result_says_who_wakes_it(self):
        with mock.patch.object(core, "helper_alive", return_value=True):
            result = self.send()
        self.assertEqual(result["state"], "blocked:not-running")
        self.assertFalse(result["sent"])
        self.assertTrue(result["helper_running"])
        self.assertIn("will wake it", result["guidance"])
        self.assertIn("mail slot", result["guidance"])
        self.assertFalse((core.device_dir() / "relay").exists())  # nothing is written for a relay any more
        with mock.patch.object(core, "helper_alive", return_value=False):
            result = self.send()
        self.assertFalse(result["helper_running"])
        self.assertIn("NOT running", result["guidance"])

    def test_an_archived_or_routine_chat_cannot_be_woken(self):
        for extra in ({"isArchived": True}, {"scheduledTaskId": "ct-test-2"}):
            self.record.write_text(json.dumps(dict({"cliSessionId": CLAUDE}, **extra)))
            result = self.send()
            self.assertEqual(result["state"], "blocked:not-running")
            self.assertFalse(result["wakeable"])
            self.assertIn("archived or a routine run", result["guidance"])

    def test_the_mail_slot_delivers_a_blocked_letter_when_the_chat_opens(self):
        result = self.send()
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "complete-scan")):
            pulled = core.mail_slot_pull(CLAUDE)
        self.assertEqual([row["letter_id"] for row in pulled["letters"]], [result["letter_id"]])
        self.assertTrue(pulled["letters"][0]["text"].endswith("\nref " + result["letter_id"]))
        letter = core.letters_load()[result["letter_id"]]
        self.assertEqual((letter["state"], letter["route"], letter["pulled_session"]), ("sending", "mail-slot", CLAUDE))
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "complete-scan")):
            self.assertEqual(core.mail_slot_pull(CLAUDE)["letters"], [])  # handed out once
            self.assertEqual(core.mail_slot_pull(OTHER)["letters"], [])  # never to another chat
        with mock.patch.object(core, "arrivals", return_value=(1, "read")):
            core.reconcile(resend=False)
        self.assertEqual(core.letters_load()[result["letter_id"]]["state"], "arrived")

    def test_the_mail_slot_never_hands_out_a_letter_when_readback_is_unavailable(self):
        letter_id = self.send()["letter_id"]
        for note in ("transcript-not-found", "transcript-unreadable", "transcript-readback-timeout",
                     "transcript-scan-capped"):
            with self.subTest(note=note), mock.patch.object(core, "claude_arrivals", return_value=(None, note)):
                pulled = core.mail_slot_pull(CLAUDE)
            self.assertEqual(pulled["letters"], [])
            self.assertEqual(pulled["skipped"], [{"letter_id": letter_id, "why": "readback-unavailable"}])
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "complete-scan")):
            self.assertEqual([row["letter_id"] for row in core.mail_slot_pull(CLAUDE)["letters"]], [letter_id])

    def test_a_letter_already_in_the_transcript_is_marked_arrived_not_shown_again(self):
        letter_id = self.send()["letter_id"]
        with mock.patch.object(core, "claude_arrivals", return_value=(1, "complete-scan")):
            pulled = core.mail_slot_pull(CLAUDE)
        self.assertEqual(pulled["letters"], [])
        self.assertEqual(pulled["skipped"][0]["why"], "already-in-transcript")
        self.assertEqual(core.letters_load()[letter_id]["state"], "arrived")

    def test_deny_and_halt_withhold_the_mail_slot(self):
        letter_id = self.send()["letter_id"]
        core.controls_update(deny_add="claude:" + CLAUDE)
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "complete-scan")):
            pulled = core.mail_slot_pull(CLAUDE)
        self.assertEqual(pulled["letters"], [])
        self.assertEqual(pulled["skipped"], [{"letter_id": letter_id, "why": "denied"}])
        self.assertEqual(core.letters_load()[letter_id]["state"], "blocked:not-running")
        core.controls_update(deny_remove="claude:" + CLAUDE, halt=True)
        with mock.patch.object(core, "claude_arrivals", return_value=(0, "complete-scan")):
            self.assertEqual(core.mail_slot_pull(CLAUDE)["letters"], [])
        self.assertEqual(core.letters_load()[letter_id]["state"], "blocked:not-running")

    def test_same_app_letters_stay_refused(self):
        core.directory_register("claude", OTHER, {"account": {"id": ACCOUNT}})
        result = core.send_letter("claude", OTHER, "claude:" + CLAUDE, "Hi")
        self.assertEqual(result["state"], "refused")
        self.assertEqual(result["reason"], "same-app-recipient")


if __name__ == "__main__":
    unittest.main()
