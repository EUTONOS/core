"""The install consent screen: its text, its plan and the typed "I agree". Offline; temp folders only."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eutonos import consent, permissions


class Console(io.StringIO):
    def __init__(self, text="", tty=True):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class ConsentTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        base = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.root, self.user = base / "project", base / "user"
        self.root.mkdir()
        self.user.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.user), "USERPROFILE": str(self.user), "CODEX_HOME": str(self.user / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.user / ".claude"), "EUTONOS_HOME": str(base / "eh")}))

    def ask(self, *lines, stdin_tty=True, stdout_tty=True):
        stdout = Console(tty=stdout_tty)
        receipt = consent.accept(self.root, self.user, stdin=Console("".join(line + "\n" for line in lines), stdin_tty),
                                 stdout=stdout, now=datetime.datetime(2026, 10, 22, 12, 0, tzinfo=datetime.timezone.utc))
        return receipt, stdout.getvalue()

    def test_text_lists_every_shipped_entry_and_the_frozen_sentences(self):
        text = consent.render(self.root, self.user)
        data = permissions.load()
        for entry in permissions.entries(data):
            with self.subTest(entry=entry["id"]):
                self.assertIn(entry["id"], text)
        for entry in permissions.entries(data):
            if entry["writer"] in ("installer", "launch-flag") and entry["default"] == "on":
                self.assertIn(consent.WHAT[entry["id"]], text)
                self.assertIn(entry["why"], text)
            if entry["writer"] in ("guided", "admin-only") and entry["default"] != "held":
                self.assertIn(data["guided_steps"][entry["id"]], text)
        for sentence in ("Let AI agents work together on this computer?", "Messaging stays off until you agree here.",
                         "commit and push their own record-keeping changes", "Recommended, not required",
                         "We never change a value you set.", "Type I agree to continue, or press Enter to cancel."):
            self.assertIn(sentence, text)

    def test_every_installer_entry_has_a_one_line_what(self):
        for entry in permissions.entries():
            if entry["writer"] in ("installer", "launch-flag") and entry["default"] == "on":
                self.assertIn(entry["id"], consent.WHAT)
                self.assertNotIn("\n", consent.WHAT[entry["id"]])

    def test_text_hash_follows_the_text_and_tuning_shows_current_values(self):
        first = consent.render(self.root, self.user)
        self.assertEqual(consent.text_sha256(first), consent.text_sha256(consent.render(self.root, self.user)))
        (self.user / ".claude").mkdir()
        (self.user / ".claude" / "settings.json").write_text('{"autoCompactWindow": 123}')
        second = consent.render(self.root, self.user)
        self.assertIn("recommended 289000, yours 123", second)
        self.assertNotEqual(consent.text_sha256(first), consent.text_sha256(second))

    def test_accept_takes_only_a_typed_phrase_on_an_interactive_console(self):
        receipt, shown = self.ask("", "I agree")
        self.assertEqual(set(receipt), {"text_id", "text_sha256", "surface", "accepted_at", "choices"})
        self.assertEqual((receipt["text_id"], receipt["surface"], receipt["accepted_at"]),
                         ("eutonos.consent.v1", "terminal", "2026-10-22T12:00:00Z"))
        self.assertEqual(receipt["text_sha256"], consent.text_sha256(consent.render(self.root, self.user)))
        self.assertEqual(receipt["choices"]["send-message"], "on")
        self.assertIn("Let AI agents work together", shown)
        for answer in ("", "i agree", "yes", "I agree.", "y"):
            with self.subTest(answer=answer), self.assertRaises(consent.ConsentRefused) as caught:
                self.ask("", answer)
            self.assertEqual(caught.exception.reason, "declined")

    def test_non_interactive_input_refuses_and_writes_nothing(self):
        for kwargs in ({"stdin_tty": False}, {"stdout_tty": False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(consent.ConsentRefused) as caught:
                self.ask("", "I agree", **kwargs)
            self.assertEqual(caught.exception.reason, "non-interactive")
        with patch("sys.stdin", io.StringIO("I agree\n")), patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(consent.main(["accept", "--root", str(self.root)]), 2)
        self.assertIn("non-interactive", out.getvalue())
        self.assertFalse((self.user / ".claude").exists())
        self.assertFalse((self.root / ".claude").exists())

    def test_there_is_no_skip_flag(self):
        for flag in ("--yes", "--agree", "--force", "-y", "--accept"):
            with self.subTest(flag=flag), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                consent.main(["accept", flag])

    def test_optional_items_can_be_turned_off_and_required_ones_cannot(self):
        receipt, _ = self.ask("codex-record-rule, send-message", "I agree")
        self.assertEqual((receipt["choices"]["codex-record-rule"], receipt["choices"]["send-message"]), ("off", "off"))
        self.assertEqual(receipt["choices"]["set-remote-control"], "on")
        for name in ("set-remote-control", "nonsense"):
            with self.subTest(name=name), self.assertRaises(consent.ConsentRefused) as caught:
                self.ask(name, "I agree")
            self.assertEqual(caught.exception.reason, "unknown-choice")
        plan = permissions.plan(self.root, self.user, choices=receipt["choices"])
        self.assertEqual(set(plan["off"]), {"codex-record-rule", "send-message"})

    def test_plan_reports_the_hash_and_the_additions_without_writing(self):
        before = sorted(path for path in self.root.rglob("*") if ".git" not in path.parts)
        result = consent.plan(self.root, self.user)
        self.assertEqual(result["text_id"], "eutonos.consent.v1")
        self.assertEqual(result["text_sha256"], consent.text_sha256(consent.render(self.root, self.user)))
        self.assertTrue(result["permissions"]["files"])
        self.assertEqual(sorted(path for path in self.root.rglob("*") if ".git" not in path.parts), before)
        with patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(consent.main(["plan", "--root", str(self.root)]), 0)
        self.assertEqual(json.loads(out.getvalue())["text_id"], "eutonos.consent.v1")


if __name__ == "__main__":
    unittest.main()
