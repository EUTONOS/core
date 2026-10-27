"""Setup's post-install settings: ``wizard suggest-accounts`` and ``wizard apply-settings`` (the "Only I use this
Windows login" step). Everything runs in temporary homes and a temporary device folder; nothing real is read or written."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import feature_settings as fs
from eutonos import pending_actions
from eutonos import setup_wizard as wiz

import test_feature_settings as tfs


class ParseTests(unittest.TestCase):
    def test_commas_semicolons_spaces_and_lines_all_separate_emails(self):
        self.assertEqual(wiz.parse_accounts("a@example.com, b@example.com;c@example.com\r\n d@example.com\n\n"),
                         ["a@example.com", "b@example.com", "c@example.com", "d@example.com"])
        self.assertEqual(wiz.parse_accounts(""), [])
        self.assertEqual(wiz.parse_accounts(None), [])

    def test_the_consent_note_and_the_shared_login_note_reach_the_wizard_through_the_inspection_file(self):
        found = {"home_default": True, "existing_install": False, "pending": False, "git_ok": True, "adopt": False,
                 "remote_control_launchers": [], "git_guidance": "", "git_note": "", "git_unavailable": [],
                 "adopt_found": [], "parts": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "inspect.ini"
            wiz.write_ini(path, found)
            lines = dict(line.partition("=")[::2] for line in path.read_text(encoding="ascii").splitlines()[1:])
        self.assertEqual(lines["solo_note"], wiz.SOLO_NOTE)
        self.assertEqual(lines["shared_login_note"], fs.SHARED_LOGIN_NOTE)
        self.assertNotIn('"', wiz.SOLO_NOTE)  # it travels on a command line inside double quotes


class SuggestTests(tfs.KitCase):
    def test_the_suggestion_lists_the_seen_emails_and_writes_nothing(self):
        self.see(tfs.ACCOUNT, "me@example.com")
        self.see(tfs.OTHER, "work@example.org")
        found = wiz.suggest_accounts(self.eh, kit_path=self.kit)
        self.assertEqual((found["outcome"], found["emails"]), ("ok", ["me@example.com", "work@example.org"]))
        self.assertFalse(self.config_path().exists())

    def test_nothing_seen_is_an_empty_list_and_no_kit_is_unavailable_not_a_crash(self):
        self.assertEqual(wiz.suggest_accounts(self.eh, kit_path=self.kit), {"outcome": "ok", "emails": [], "detail": ""})
        gone = wiz.suggest_accounts(self.eh, kit_path=self.eh / "no-kit")
        self.assertEqual((gone["outcome"], gone["emails"]), ("unavailable", []))
        self.assertFalse(self.config_path().exists())

    def test_the_command_line_writes_one_email_per_line_for_the_wizard(self):
        out = self.base / "suggested.txt"
        with mock.patch.object(wiz, "suggest_accounts", return_value={"outcome": "ok", "emails": ["a@example.com", "b@example.com"],
                                                                      "detail": ""}) as called, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(wiz.main(["suggest-accounts", "--home", str(self.eh), "--out", str(out)]), 0)
        called.assert_called_once_with(str(self.eh))
        self.assertEqual(out.read_text(encoding="ascii"), "a@example.com\nb@example.com\n")
        with mock.patch.object(wiz, "suggest_accounts", return_value={"outcome": "unavailable", "emails": [], "detail": "x"}), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(wiz.main(["suggest-accounts", "--home", str(self.eh), "--out", str(out)]), 0)
        self.assertEqual(out.read_text(encoding="ascii"), "")


class ApplyTests(tfs.KitCase):
    def apply(self, wanted):
        return wiz.apply_settings(self.eh, wanted, kit_path=self.kit)

    def pending(self):
        return pending_actions.list_actions(self.eh)

    def test_confirmed_emails_are_saved_through_the_s3_function_and_reported(self):
        result = self.apply({"linked_accounts": ["me@example.com", "work@example.org"]})
        self.assertEqual((result["outcome"], result["attention"]), ("ok", []))
        (one,) = result["settings"]
        self.assertEqual((one["setting"], one["outcome"], one["pending_id"]), ("linked-accounts", "enabled", None))
        self.assertEqual(json.loads(self.config_path().read_text(encoding="utf-8"))["switch_history"],
                         ["me@example.com", "work@example.org"])
        self.assertIn("me@example.com", result["lines"][0])
        self.assertEqual(self.pending(), [])
        again = self.apply({"linked_accounts": ["me@example.com", "work@example.org"]})
        self.assertEqual(again["settings"][0]["outcome"], "unchanged")
        self.assertIn("already set", again["lines"][0])

    def test_when_the_box_was_not_ticked_nothing_is_written(self):
        for wanted in ({}, None, {"something_else": 1}):
            with self.subTest(wanted=wanted):
                result = self.apply(wanted)
                self.assertEqual((result["outcome"], result["settings"], result["lines"]), ("ok", [], []))
        self.assertFalse(self.config_path().exists())
        self.assertFalse((self.device / "controls.json").exists())
        self.assertFalse(pending_actions.path(self.eh).exists())

    def test_an_empty_list_saves_nothing_and_never_wipes_what_was_already_there(self):
        self.write_config(b'{"switch_history": ["keep@example.com"]}')
        before = self.config_path().read_bytes()
        result = self.apply({"linked_accounts": ["  ", ""]})
        self.assertEqual((result["outcome"], result["settings"][0]["outcome"]), ("ok", "skipped"))
        self.assertIn("nothing was saved", result["lines"][0])
        self.assertEqual(self.config_path().read_bytes(), before)
        self.assertEqual(self.pending(), [])

    def test_a_failure_does_not_fail_setup_it_is_shown_and_a_pending_step_is_left(self):
        for wanted, reason in ((["not an email"], "invalid"), (["me@example.com"], "kit-missing")):
            with self.subTest(reason=reason):
                kit = self.kit if reason == "invalid" else self.eh / "no-kit"
                result = wiz.apply_settings(self.eh, {"linked_accounts": wanted}, kit_path=kit)
                (one,) = result["settings"]
                self.assertEqual((result["outcome"], one["outcome"], one["result"]["reason"]), ("attention", "refused", reason))
                self.assertEqual(result["attention"], ["linked-accounts"])
                self.assertRegex(one["pending_id"], r"^pa-[0-9a-f]{8}$")
                self.assertIn("not saved", one["line"])
                self.assertIn("eutonos-install pending list", one["line"])
        steps = self.pending()
        self.assertEqual({item["source"] for item in steps}, {"setup:linked-accounts"})
        self.assertEqual(len(steps), 2)
        self.assertTrue(all("eutonos-install setting set linked-accounts" in item["action"] for item in steps))
        self.assertFalse(self.config_path().exists())

    def test_the_same_failure_twice_leaves_one_step(self):
        for _ in range(2):
            self.apply({"linked_accounts": ["not an email"]})
        self.assertEqual(len(self.pending()), 1)

    def test_a_pending_store_that_cannot_be_written_only_adds_a_note(self):
        with mock.patch.object(pending_actions, "add", side_effect=OSError("disk full")):
            result = self.apply({"linked_accounts": ["not an email"]})
        (one,) = result["settings"]
        self.assertEqual((result["outcome"], one["pending_id"]), ("attention", None))
        self.assertIn("could not be saved (disk full)", one["line"])

    def test_a_home_with_no_install_is_refused_and_leaves_no_files_behind(self):
        empty = self.base / "empty-home"
        empty.mkdir()
        result = wiz.apply_settings(empty, {"linked_accounts": ["me@example.com"]})
        (one,) = result["settings"]
        self.assertEqual((one["outcome"], one["result"]["reason"], one["pending_id"]), ("refused", "not-installed", None))
        self.assertFalse((empty / "settings").exists())  # no pending-actions file either

    def test_a_list_of_the_wrong_shape_is_refused_not_a_crash(self):
        for bad in ("me@example.com", [1, 2], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.apply({"linked_accounts": bad})["settings"][0]["result"]["reason"], "invalid")

    def test_messaging_and_ram_go_through_their_own_s3_functions_when_asked(self):
        ram = {"outcome": "disabled", "setting": "ram"}
        with mock.patch.object(wiz.fs, "set_ram", return_value=ram) as set_ram:
            result = self.apply({"crosstalk_messaging": True, "ram": {"root": str(self.root), "capture": False}})
        self.assertEqual([item["setting"] for item in result["settings"]], ["crosstalk-messaging", "ram"])
        self.assertEqual((result["outcome"], self.controls()["messaging"]), ("ok", "on"))
        set_ram.assert_called_once_with(root=str(self.root), capture=False, retrieval=None)

    def test_a_ram_failure_is_shown_with_a_pending_step_like_any_other(self):
        failed = {"outcome": "refused", "reason": "settings-unavailable", "detail": "could not be saved"}
        with mock.patch.object(wiz.fs, "set_ram", return_value=failed):
            result = self.apply({"ram": {"root": str(self.root), "capture": True}})
        self.assertEqual((result["outcome"], result["attention"]), ("attention", ["ram"]))
        self.assertEqual([item["source"] for item in self.pending()], ["setup:ram"])

    def test_the_command_line_always_exits_zero_prints_the_outcomes_and_writes_the_report_for_the_finish_page(self):
        typed, report = self.base / "typed.txt", self.base / "report.txt"
        typed.write_text("me@example.com\r\nwork@example.org\r\n", encoding="utf-8")
        attention = {"outcome": "attention", "attention": ["linked-accounts"], "lines": ["Linked accounts: not saved (x)."],
                     "settings": [{"setting": "linked-accounts", "outcome": "refused", "pending_id": "pa-12345678",
                                   "result": {}, "line": "x"}]}
        with mock.patch.object(wiz, "apply_settings", return_value=attention) as called, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as shown:
            code = wiz.main(["apply-settings", "--home", str(self.eh), "--linked-accounts-file", str(typed),
                             "--result-out", str(report)])
        self.assertEqual(code, 0)  # a setting that could not be saved never fails Setup
        called.assert_called_once_with(str(self.eh), {"linked_accounts": ["me@example.com", "work@example.org"]})
        self.assertEqual(json.loads(shown.getvalue())["outcome"], "attention")
        raw = report.read_bytes()
        self.assertEqual(raw, b"Linked accounts: not saved (x).\r\n")

    def test_the_command_line_reads_a_json_file_and_refuses_one_that_is_not_an_object(self):
        spec = self.base / "wanted.json"
        spec.write_text(json.dumps({"crosstalk_messaging": False}), encoding="utf-8")
        with mock.patch.object(wiz, "apply_settings", return_value={"outcome": "ok", "attention": [], "lines": [],
                                                                    "settings": []}) as called, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(wiz.main(["apply-settings", "--home", str(self.eh), "--json", str(spec)]), 0)
        called.assert_called_once_with(str(self.eh), {"crosstalk_messaging": False})
        spec.write_text("[1]", encoding="utf-8")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as shown:
            self.assertEqual(wiz.main(["apply-settings", "--home", str(self.eh), "--json", str(spec)]), 1)
        self.assertEqual(json.loads(shown.getvalue())["outcome"], "error")

    def test_with_no_flags_the_command_line_writes_nothing(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as shown:
            self.assertEqual(wiz.main(["apply-settings", "--home", str(self.eh)]), 0)
        self.assertEqual(json.loads(shown.getvalue())["settings"], [])
        self.assertFalse(self.config_path().exists())

    def test_the_update_choice_is_saved_through_update_check_and_reported(self):
        from eutonos import update_check
        result = self.apply({"update_mode": "notify"})
        self.assertEqual((result["outcome"], result["attention"]), ("ok", []))
        (one,) = result["settings"]
        self.assertEqual((one["setting"], one["outcome"], one["pending_id"]), ("update-mode", "changed", None))
        self.assertEqual(update_check.mode(self.eh), "notify")
        self.assertIn("Updates: saved (notify).", result["lines"][0])
        again = self.apply({"update_mode": "notify"})
        self.assertIn("already set", again["lines"][0])
        self.assertEqual(self.apply({"update_mode": "auto"})["settings"][0]["outcome"], "changed")

    def test_a_bad_update_choice_is_reported_not_raised_and_leaves_a_step(self):
        from eutonos import update_check
        for bad in ("sometimes", 7):
            with self.subTest(bad=bad):
                result = self.apply({"update_mode": bad})
                self.assertEqual((result["outcome"], result["attention"]), ("attention", ["update-mode"]))
                self.assertIn("Updates: not saved", result["lines"][0])
        self.assertEqual(update_check.mode(self.eh), "auto")

    def test_the_command_line_takes_an_update_mode_and_only_the_three_words(self):
        with mock.patch.object(wiz, "apply_settings", return_value={"outcome": "ok", "attention": [], "lines": [],
                                                                    "settings": []}) as called, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(wiz.main(["apply-settings", "--home", str(self.eh), "--update-mode", "manual"]), 0)
        called.assert_called_once_with(str(self.eh), {"update_mode": "manual"})
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            wiz.main(["apply-settings", "--home", str(self.eh), "--update-mode", "often"])

    def test_the_wizard_page_offers_the_three_choices_with_auto_the_default(self):
        text = (Path(__file__).resolve().parents[1] / "installer" / "windows" / "EUTONOS.iss").read_text(encoding="utf-8")
        self.assertIn("Manual (nothing runs on its own)", text)
        self.assertIn("Check and notify", text)
        self.assertIn("Check, download, then ask before installing", text)
        self.assertIn("UpdateBox.ItemIndex := 2;", text)  # the third choice, auto, is selected first
        self.assertIn("--update-mode ' + UpdateModeWord", text)
        self.assertRegex(text, r"PageID = UpdatePage\.ID then\s+Result := RepairMode;")  # skipped in repair mode
        self.assertIn("ConsentPage := CreateCustomPage(UpdatePage.ID,", text)  # between the login page and the consent page


if __name__ == "__main__":
    unittest.main()
