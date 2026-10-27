"""Offline tests: crosstalk create-claude, Desktop-account detection and held-letter release.

Everything uses fixture directories and mocks; no real claude call and no real app data."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_create_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_create_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

CLAUDE = "11111111-2222-4333-8444-555555555555"
CODEX = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT_OLD = "99999999-2222-4333-8444-555555555555"
ACCOUNT_NEW = "88888888-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-create-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.log = self.root / "main.log"
        self.env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                               "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                               "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
                                               "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.log)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.set_json_account(ACCOUNT_NEW)
        core.controls_update(messaging="on", halt=False)

    def set_json_account(self, account_id):
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account_id}}))

    def sessions_folder(self, account=ACCOUNT_NEW):
        folder = self.root / "AppData" / "claude-code-sessions" / account / ORG
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def set_desktop_log(self, *lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def loaded(self, account=ACCOUNT_NEW, count=5):
        return f"2026-10-09 [info] Loaded {count} persisted sessions from {self.sessions_folder(account)}"

    def write_transcript(self, session_id, text):
        folder = self.root / "claude" / "projects" / "slug"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session_id}.jsonl").write_text(text, encoding="utf-8")


class CreateClaudeTests(Base):
    def run_create(self, result_for=lambda sid: {"session_id": sid, "is_error": False,
                                                 "modelUsage": {"claude-sonnet-5-5": {}}},
                   transcript="claude-sonnet-5-5", show=True, prompt='say "hi" & echo $HOME'):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1:3] == ["auth", "status"]:
                return mock.Mock(stdout='{"loggedIn": true}', returncode=0)
            sid = argv[argv.index("--session-id") + 1]
            if transcript is not None:
                self.write_transcript(sid, transcript)
            return mock.Mock(stdout=json.dumps(result_for(sid)), returncode=0)

        with mock.patch.object(core, "claude_exe", return_value="claude"), \
                mock.patch.object(core.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(core.subprocess, "Popen") as popen:
            value = core.create_claude(str(self.folder), "claude-sonnet-5-5", "high", "auto", prompt, title="T", show=show)
        return value, calls, popen

    def test_success_builds_list_argv_without_shell_and_registers(self):
        value, calls, popen = self.run_create()
        argv, kwargs = calls[-1]
        self.assertIsInstance(argv, list)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["cwd"], str(self.folder))
        self.assertEqual(kwargs["input"], 'say "hi" & echo $HOME')
        self.assertNotIn('say "hi" & echo $HOME', argv)
        for flag, expected in (("--model", "claude-sonnet-5-5"), ("--effort", "high"), ("--permission-mode", "auto"),
                               ("--output-format", "json")):
            self.assertEqual(argv[argv.index(flag) + 1], expected)
        self.assertTrue(value["ok"])
        self.assertFalse(value["effort_verified"])
        self.assertTrue(value["verified"]["model_in_transcript"])
        self.assertTrue(value["verified"]["first_turn_persisted"])
        self.assertEqual(value["key"], "claude:" + value["session_id"])
        entry = core.directory_load()["entries"][value["key"]]
        self.assertEqual(entry["account"]["id"], ACCOUNT_NEW)
        self.assertEqual(entry["project_folder"], str(self.folder))
        self.assertEqual(entry["title"], "T")
        self.assertEqual(popen.call_args[0][0][-3:], ["--desktop", "--resume", value["session_id"]])
        self.assertTrue(value["show"]["launched"])
        self.assertNotIn("stdout", popen.call_args[1])

    def test_title_is_passed_as_name_reported_and_survives_a_later_hook_registration(self):
        value, calls, _ = self.run_create(show=False)
        argv = calls[-1][0]
        self.assertEqual(argv[argv.index("--name") + 1], "T")
        self.assertTrue(value["title_set"])
        self.assertIn("title_note", value)
        with mock.patch.object(core, "claude_registration", return_value={
                "live": True, "status": "idle", "entrypoint": "x", "name": "eutonos-03"}):
            entry = core.register_claude({"sessionId": value["session_id"], "cwd": str(self.folder)}, "session.start")
        self.assertEqual(entry["title"], "T")  # the native name never overwrites the creator's title

    def test_no_show_does_not_launch(self):
        value, _, popen = self.run_create(show=False)
        self.assertFalse(value["show"]["launched"])
        popen.assert_not_called()

    def assert_fails(self, reason, **kwargs):
        with self.assertRaises(core.ProbeError) as caught:
            self.run_create(**kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(core.directory_load()["entries"], {})

    def test_session_id_mismatch_fails(self):
        self.assert_fails("create-session-id-mismatch", result_for=lambda sid: {"session_id": "other", "is_error": False})

    def test_is_error_fails(self):
        self.assert_fails("create-is-error", result_for=lambda sid: {"session_id": sid, "is_error": True})

    def test_missing_transcript_fails(self):
        self.assert_fails("create-transcript-missing", transcript=None)

    def test_model_absent_from_transcript_fails(self):
        self.assert_fails("create-model-not-in-transcript", transcript="nothing relevant")

    def test_controls_refuse_before_any_process(self):
        for change, reason in (({"halt": True}, "controls-halted"),
                               ({"halt": False, "messaging": "off"}, "controls-messaging-off")):
            core.controls_update(**change)
            with mock.patch.object(core.subprocess, "run") as run, self.assertRaises(core.ProbeError) as caught:
                core.create_claude(str(self.folder), "m", "high", "auto", "hi")
            self.assertEqual(caught.exception.reason, reason)
            run.assert_not_called()

    def test_haiku_models_are_refused_before_any_process(self):
        for model in ("claude-haiku-4-5", "claude-3-5-haiku-20241022", "Claude-Haiku-5"):
            with mock.patch.object(core.subprocess, "run") as run, self.assertRaises(core.ProbeError) as caught:
                core.create_claude(str(self.folder), model, "high", "auto", "hi")
            self.assertEqual(caught.exception.reason, "model-haiku-refused")
            run.assert_not_called()
        self.assertEqual(core.directory_load()["entries"], {})

    def test_haiku_5_5_and_later_pass_the_auto_mode_check(self):
        for model in ("claude-haiku-5-5", "claude-haiku-6", "haiku", "claude-sonnet-5-5", "opus"):
            self.assertFalse(core._auto_mode_refused(model), model)
        for model in ("claude-haiku-4-5", "claude-3-5-haiku-20241022", "claude-haiku-5"):
            self.assertTrue(core._auto_mode_refused(model), model)
        with mock.patch.object(core.subprocess, "run", side_effect=RuntimeError("reached the process")), \
                self.assertRaises(RuntimeError):
            core.create_claude(str(self.folder), "claude-haiku-5-5", "high", "auto", "hi")

    def test_not_logged_in_and_missing_cli(self):
        with mock.patch.object(core, "claude_exe", return_value=None), self.assertRaises(core.ProbeError) as caught:
            core.create_claude(str(self.folder), "m", "high", "auto", "hi")
        self.assertEqual(caught.exception.reason, "claude-cli-not-found")
        with mock.patch.object(core, "claude_exe", return_value="claude"), \
                mock.patch.object(core.subprocess, "run", return_value=mock.Mock(stdout='{"loggedIn": false}')), \
                self.assertRaises(core.ProbeError) as caught:
            core.create_claude(str(self.folder), "m", "high", "auto", "hi")
        self.assertEqual(caught.exception.reason, "claude-not-logged-in")

    def test_cli_command_prints_one_json_object(self):
        buffer = io.StringIO()
        with mock.patch.object(core, "create_claude", return_value={"ok": True}) as create, \
                contextlib.redirect_stdout(buffer):
            code = cli.main(["create-claude", "--folder", str(self.folder), "--model", "m", "--effort", "low",
                             "--prompt", "hi", "--no-show"])
        self.assertEqual(code, 0)
        self.assertFalse(create.call_args.kwargs["show"])
        self.assertTrue(json.loads(buffer.getvalue())["ok"])


class DesktopAccountTests(Base):
    def test_log_parse_last_nonempty_load_wins(self):
        self.set_desktop_log(self.loaded(ACCOUNT_OLD), self.loaded(ORG, count=0), self.loaded(ACCOUNT_NEW))
        account = core.current_account()
        self.assertEqual(account["id"], ACCOUNT_NEW)
        self.assertEqual(account["source"], "claude-desktop main.log")

    def test_logged_out_then_fallback_to_claude_json(self):
        self.set_desktop_log(self.loaded(ACCOUNT_OLD), "[LocalSessionManager] Account logged out")
        self.set_json_account(ACCOUNT_NEW)
        account = core.current_account()
        self.assertEqual(account["id"], ACCOUNT_NEW)
        self.assertTrue(account["source"].startswith("fallback"))

    def test_missing_log_falls_back(self):
        account = core.current_account()
        self.assertEqual(account["id"], ACCOUNT_NEW)
        self.assertTrue(account["source"].startswith("fallback"))


class HeldLetterTests(Base):
    def setUp(self):
        super().setUp()
        core.directory_register("claude", CLAUDE, {"account": {"id": ACCOUNT_OLD}})
        core.directory_register("codex", CODEX, {})
        self.set_json_account(ACCOUNT_OLD)  # the CLI-login file must NOT decide
        self.set_desktop_log(self.loaded(ACCOUNT_NEW))
        patcher = mock.patch.object(core, "claude_registration", return_value={"live": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self):
        return core.send_letter("codex", CODEX, "claude:" + CLAUDE, "Hello")

    def add_record(self):
        (self.sessions_folder() / "local_abc.json").write_text(json.dumps({"cliSessionId": CLAUDE}))

    def test_mismatch_without_record_is_undeliverable_with_reason(self):
        with mock.patch.object(core, "deliver_claude") as leg:
            result = self.send()
        leg.assert_not_called()
        self.assertEqual(result["state"], "blocked:other-account")
        self.assertFalse(result["sent"])
        self.assertEqual(result["registered_account"], ACCOUNT_OLD)
        self.assertEqual(result["current_account"], ACCOUNT_NEW)
        for text in ("UNDELIVERABLE, NOT SENT", "will not be sent later", "Reason:", ACCOUNT_OLD, ACCOUNT_NEW,
                     "tell the owner"):
            self.assertIn(text, result["guidance"])
        self.assertTrue(result["report_to_owner"])
        self.assertEqual(core.directory_load()["entries"]["claude:" + CLAUDE]["account"]["id"], ACCOUNT_OLD)

    def test_binding_mismatch_but_record_under_active_account_delivers_and_refreshes(self):
        self.add_record()
        with mock.patch.object(core, "deliver_claude", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(1, "read")):
            result = self.send()
        self.assertEqual(leg.call_count, 1)
        self.assertTrue(result["sent"])
        self.assertEqual(core.directory_load()["entries"]["claude:" + CLAUDE]["account"]["id"], ACCOUNT_NEW)
        events = [row for row in core.journal_rows() if row.get("event") == "binding-refresh"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_account"], ACCOUNT_OLD)
        self.assertEqual(events[0]["to_account"], ACCOUNT_NEW)

    def test_undeliverable_letter_is_never_sent_later_even_when_reachable(self):
        with mock.patch.object(core, "deliver_claude", return_value=("accepted", {})) as leg, \
                mock.patch.object(core, "arrivals", return_value=(0, "read")):
            letter_id = self.send()["letter_id"]
            self.add_record()  # recipient becomes reachable afterwards
            core.reconcile(resend=False)
            core.reconcile(resend=True, grace_s=0)
            self.assertIsNone(core.attempt_delivery(letter_id).get("route"))
            leg.assert_not_called()
        self.assertEqual(core.letters_load()[letter_id]["state"], "blocked:other-account")

    def test_claude_readback_rechecks_once_after_a_short_delay(self):
        self.add_record()
        with mock.patch.object(core, "deliver_claude", return_value=("accepted", {})) as leg,                 mock.patch.object(core, "arrivals", side_effect=[(0, "complete-scan"), (1, "complete-scan")]) as read,                 mock.patch.object(core.time, "sleep") as pause:
            result = self.send()
        self.assertEqual(leg.call_count, 1)
        self.assertEqual(read.call_count, 2)
        pause.assert_called_once_with(core.CLAUDE_READBACK_RECHECK_S)
        self.assertEqual(result["state"], "arrived")

    def test_claude_readback_still_unseen_stays_held_without_resend(self):
        self.add_record()
        with mock.patch.object(core, "deliver_claude", return_value=("accepted", {})) as leg,                 mock.patch.object(core, "arrivals", return_value=(0, "complete-scan")) as read,                 mock.patch.object(core.time, "sleep"):
            result = self.send()
        self.assertEqual(leg.call_count, 1)
        self.assertEqual(read.call_count, 1 + len(core.held_reread_delays("claude")))  # first read plus the re-reads
        self.assertEqual(result["state"], "held")


if __name__ == "__main__":
    unittest.main()
