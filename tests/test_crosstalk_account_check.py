"""Offline tests: crosstalk account_check (Desktop active account vs Claude CLI login) and its surfaces.

Fakes for both sources (a fixture ~/.claude.json and a fixture Desktop main.log); no real app data."""
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
spec = importlib.util.spec_from_file_location("crosstalk_account_check_core", KIT / "crosstalk_p0_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
with mock.patch.dict("sys.modules", {"crosstalk_p0_core": core}):
    cli_spec = importlib.util.spec_from_file_location("crosstalk_account_check_cli", KIT / "crosstalk_p0_cli.py")
    cli = importlib.util.module_from_spec(cli_spec)
    cli_spec.loader.exec_module(cli)

ACCOUNT_A = "99999999-2222-4333-8444-555555555555"
ACCOUNT_B = "88888888-2222-4333-8444-555555555555"
ORG = "77777777-2222-4333-8444-555555555555"


class AccountCheckBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="crosstalk-account-check-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.log = self.root / "main.log"
        env = mock.patch.dict(os.environ, {"CROSSTALK_P0_HERMES_HOME": str(self.root / "no-hermes"), "CROSSTALK_P0_DEVICE_DIR": str(self.root / "device"),
                                           "CROSSTALK_P0_CLAUDE_HOME": str(self.root / "claude"),
                                           "CROSSTALK_P0_CLAUDE_JSON": str(self.root / "claude.json"),
                                           "CROSSTALK_P0_CLAUDE_DESKTOP_LOG": str(self.log),
                                           "CROSSTALK_P0_FOCUS": "off", "CROSSTALK_P0_CODEX_WAKE": "off"})
        env.start()
        self.addCleanup(env.stop)
        core.controls_update(messaging="on", halt=False)

    def set_cli(self, account_id):
        if account_id is None:
            return  # no ~/.claude.json at all
        (self.root / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account_id}}))

    def set_desktop(self, account_id):
        if account_id is None:
            return  # no Desktop log at all
        folder = self.root / "AppData" / "claude-code-sessions" / account_id / ORG
        folder.mkdir(parents=True, exist_ok=True)
        self.log.write_text(f"2026-10-13 [info] Loaded 2 persisted sessions from {folder}\n", encoding="utf-8")


class CodexCacheTests(AccountCheckBase):
    def test_the_cached_codex_account_is_returned_as_a_copy(self):
        core._ACCOUNT_CACHE.update(at=core.time.monotonic(), value={"app": "codex", "id": "cached"})
        self.addCleanup(core._ACCOUNT_CACHE.update, at=0.0, value=None)
        first = core.codex_account()
        first["id"] = "mutated"
        self.assertEqual(core.codex_account(), {"app": "codex", "id": "cached"})


class LoginPromptTests(AccountCheckBase):
    def setUp(self):
        super().setUp()
        exe = self.root / "claude.exe"
        exe.write_text("")
        patcher = mock.patch.dict(os.environ, {"CROSSTALK_P0_CLAUDE_EXE": str(exe)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.exe = str(exe)
        self.asked, self.launched = [], []

    def prompt(self, answer):
        return core.account_login_prompt(ask=lambda title, text: self.asked.append(text) or answer,
                                          launch=self.launched.append)

    def test_remember_desktop_account_keeps_only_a_valid_id_and_email(self):
        self.assertFalse(core.remember_desktop_account({}))
        self.assertFalse(core.remember_desktop_account({"CLAUDE_CODE_ACCOUNT_UUID": "nope",
                                                         "CLAUDE_CODE_USER_EMAIL": "a@example.com"}))
        self.assertFalse(core.remember_desktop_account({"CLAUDE_CODE_ACCOUNT_UUID": ACCOUNT_A,
                                                         "CLAUDE_CODE_USER_EMAIL": "a b@example.com"}))
        env = {"CLAUDE_CODE_ACCOUNT_UUID": ACCOUNT_A.upper(), "CLAUDE_CODE_USER_EMAIL": "a@example.com",
               "CLAUDE_CODE_MESSAGING_TOKEN": "secret-value"}
        self.assertTrue(core.remember_desktop_account(env))
        self.assertFalse(core.remember_desktop_account(env))  # unchanged: no write
        self.assertEqual(core.desktop_account_emails(), {ACCOUNT_A: "a@example.com"})
        self.assertNotIn("secret-value", core.accounts_path().read_text(encoding="utf-8"))

    def test_mismatch_asks_and_yes_opens_the_login_prefilled_with_the_desktop_email(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_B)
        core.remember_desktop_account({"CLAUDE_CODE_ACCOUNT_UUID": ACCOUNT_A, "CLAUDE_CODE_USER_EMAIL": "a@example.com"})
        result = self.prompt(True)
        self.assertEqual((result["answer"], result["email_prefilled"]), ("yes", True))
        self.assertEqual(self.launched, [[self.exe, "auth", "login", "--email", "a@example.com"]])
        self.assertIn("Crosstalk uses the Claude CLI to wake and create Claude chats", self.asked[0])
        self.assertIn("a@example.com", self.asked[0])

    def test_no_opens_nothing_and_an_unknown_email_is_not_prefilled(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_B)
        self.assertEqual(self.prompt(False)["answer"], "no")
        self.assertEqual(self.launched, [])
        self.prompt(True)
        self.assertEqual(self.launched, [[self.exe, "auth", "login"]])

    def test_match_or_unknown_asks_nothing(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_A)
        self.assertEqual(self.prompt(True), {"ok": True, "asked": False, "state": "match"})
        self.set_cli("not-a-uuid")
        self.assertEqual(self.prompt(True)["state"], "unknown")
        self.assertEqual((self.asked, self.launched), ([], []))

    def test_the_login_runs_without_desktop_session_settings(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "x", "CLAUDECODE": "1", "ANTHROPIC_BASE_URL": "y"}):
            env = core.standalone_cli_env()
        self.assertFalse({"CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "ANTHROPIC_BASE_URL"} & set(env))


class AccountCheckTests(AccountCheckBase):
    def test_match(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_A)
        check = core.account_check()
        self.assertEqual(check["state"], "match")
        self.assertEqual((check["desktop"], check["cli"]), (ACCOUNT_A, ACCOUNT_A))
        self.assertNotIn("command", check)

    def test_mismatch_names_both_ids_and_the_command(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_B)
        check = core.account_check()
        self.assertEqual(check["state"], "mismatch")
        self.assertEqual((check["desktop"], check["cli"]), (ACCOUNT_A, ACCOUNT_B))
        self.assertEqual(check["command"], "claude auth login")
        for expected in (ACCOUNT_A, ACCOUNT_B, "claude auth login"):
            self.assertIn(expected, check["message"])

    def test_unknown_when_either_or_both_sides_unreadable(self):
        for desktop, cli, missing in ((None, ACCOUNT_A, "Desktop"), (ACCOUNT_A, None, "CLI"),
                                      (None, None, "Desktop and CLI")):
            with self.subTest(desktop=desktop, cli=cli):
                for name in ("main.log", "claude.json"):
                    (self.root / name).unlink(missing_ok=True)
                self.set_desktop(desktop)
                self.set_cli(cli)
                check = core.account_check()
                self.assertEqual(check["state"], "unknown")
                self.assertEqual((check["desktop"], check["cli"]), (desktop, cli))
                self.assertNotIn("command", check)
                self.assertIn(f"the {missing} account ID", check["message"])

    def test_malformed_cli_id_is_unknown(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli("not-a-uuid")
        check = core.account_check()
        self.assertEqual((check["state"], check["cli"]), ("unknown", None))

    def test_check_reads_only_and_prints_only_ids(self):
        self.set_desktop(ACCOUNT_A)
        (self.root / "claude.json").write_text(json.dumps({
            "oauthAccount": {"accountUuid": ACCOUNT_B, "emailAddress": "someone@example.com"},
            "token": "sk-secret"}))
        before = (self.root / "claude.json").read_bytes()
        check = core.account_check()
        self.assertEqual((self.root / "claude.json").read_bytes(), before)
        text = json.dumps(check)
        self.assertNotIn("someone@example.com", text)
        self.assertNotIn("sk-secret", text)


class AccountCheckSurfaceTests(AccountCheckBase):
    def run_create(self):
        def fake_run(argv, **kwargs):
            if argv[1:3] == ["auth", "status"]:
                return mock.Mock(stdout='{"loggedIn": true}', returncode=0)
            sid = argv[argv.index("--session-id") + 1]
            folder = self.root / "claude" / "projects" / "slug"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{sid}.jsonl").write_text("claude-sonnet-5-5", encoding="utf-8")
            return mock.Mock(stdout=json.dumps({"session_id": sid, "is_error": False,
                                                "modelUsage": {"claude-sonnet-5-5": {}}}), returncode=0)

        with mock.patch.object(core, "claude_exe", return_value="claude"), \
                mock.patch.object(core.subprocess, "run", side_effect=fake_run) as run, \
                mock.patch.object(core.subprocess, "Popen"):
            try:
                value = core.create_claude(str(self.folder), "claude-sonnet-5-5", "high", "auto", "hi", show=False)
            except core.ProbeError as error:
                return error, run
        return value, run

    def test_create_claude_refuses_on_mismatch_before_any_process(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_B)
        error, run = self.run_create()
        self.assertIsInstance(error, core.ProbeError)
        self.assertEqual(error.reason, "account-mismatch")
        for expected in (ACCOUNT_A, ACCOUNT_B, "claude auth login"):
            self.assertIn(expected, error.detail)
        run.assert_not_called()
        self.assertEqual(core.directory_load()["entries"], {})

    def test_create_claude_proceeds_on_match_and_unknown_and_reports_the_check(self):
        for desktop, state in ((ACCOUNT_B, "match"), (None, "unknown")):
            with self.subTest(state=state):
                (self.root / "main.log").unlink(missing_ok=True)
                self.set_desktop(desktop)
                self.set_cli(ACCOUNT_B)
                value, _ = self.run_create()
                self.assertIsInstance(value, dict)
                self.assertTrue(value["ok"])
                self.assertEqual(value["account_check"]["state"], state)

    def test_status_command_includes_account_check(self):
        self.set_desktop(ACCOUNT_A)
        self.set_cli(ACCOUNT_B)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.cmd_status(mock.Mock())
        value = json.loads(buffer.getvalue())
        self.assertEqual(value["account_check"]["state"], "mismatch")
        self.assertEqual(value["account_check"]["cli"], ACCOUNT_B)


if __name__ == "__main__":
    unittest.main()
