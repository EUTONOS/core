"""Crosstalk messaging, RAM and linked Claude accounts: the read/set API and ``eutonos-install setting``.

Everything runs in temporary homes, a temporary device folder (``CROSSTALK_P0_DEVICE_DIR`` from the component test
case) and a temporary project folder. Nothing here touches the real ``~/.eutonos`` or the real home.
"""
import codecs
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import feature_settings as fs
from eutonos import installer
from eutonos import settings
from eutonos import setup_checkpoint as sc

import test_components as tc

ACCOUNT = "3f2a9c1e-5b7d-4e8a-9c0b-1a2b3c4d5e6f"
OTHER = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


class KitCase(tc.ComponentCase):
    """A materialized Crosstalk kit and a temporary device folder."""

    def setUp(self):
        super().setUp()
        self.device = self.eh / "crosstalk-probe"
        self.kw = dict(home=self.eh, kit_path=self.kit)

    def core(self):
        return fs._core(fs._kit_ctx(self.eh, self.kit))

    def controls(self):
        return json.loads((self.device / "controls.json").read_text(encoding="utf-8"))

    def config_path(self):
        return self.device / "helper" / "config.json"

    def write_config(self, data: bytes):
        self.config_path().parent.mkdir(parents=True, exist_ok=True)
        self.config_path().write_bytes(data)

    def see(self, account, email):
        core = self.core()
        with fs.cs.home_env(fs._kit_ctx(self.eh, self.kit)):
            self.assertTrue(core.remember_desktop_account({"CLAUDE_CODE_ACCOUNT_UUID": account,
                                                           "CLAUDE_CODE_USER_EMAIL": email}))


class MessagingTests(KitCase):
    def test_a_fresh_device_has_messaging_off(self):
        found = fs.crosstalk_messaging_status(**self.kw)
        self.assertEqual((found["outcome"], found["enabled"], found["halt"]), ("ok", False, False))
        self.assertFalse((self.device / "controls.json").exists())  # reading writes nothing

    def test_on_and_off_report_what_changed_and_the_same_again_changes_nothing(self):
        on = fs.set_crosstalk_messaging(True, **self.kw)
        self.assertEqual((on["outcome"], on["before"], on["after"]), ("enabled", "off", "on"))
        self.assertEqual(self.controls()["messaging"], "on")
        stamp = (self.device / "controls.json").read_bytes()
        again = fs.set_crosstalk_messaging(True, **self.kw)
        self.assertEqual(again["outcome"], "unchanged")
        self.assertEqual((self.device / "controls.json").read_bytes(), stamp)
        off = fs.set_crosstalk_messaging(False, **self.kw)
        self.assertEqual((off["outcome"], self.controls()["messaging"]), ("disabled", "off"))
        self.assertEqual(fs.set_crosstalk_messaging(False, **self.kw)["outcome"], "unchanged")

    def test_the_owners_stop_is_never_touched_and_is_reported(self):
        core = self.core()
        with fs.cs.home_env(fs._kit_ctx(self.eh, self.kit)):
            core.controls_update(halt=True, deny_add="claude:x")
        for wanted in (True, False):
            with self.subTest(enable=wanted):
                answer = fs.set_crosstalk_messaging(wanted, **self.kw)
                self.assertTrue(answer["halt"])
                self.assertTrue(any("halted" in note for note in answer["notes"]))
                self.assertIs(self.controls()["halt"], True)
                self.assertEqual(self.controls()["deny"], ["claude:x"])
        self.assertTrue(fs.crosstalk_messaging_status(**self.kw)["halt"])

    def test_turning_it_on_while_the_part_is_off_says_so(self):
        from eutonos import component_state
        component_state.set_enabled(self.eh, "crosstalk", False)
        answer = fs.set_crosstalk_messaging(True, **self.kw)
        self.assertEqual(answer["outcome"], "enabled")
        self.assertTrue(any("switched off" in note for note in answer["notes"]))

    def test_without_an_installation_or_a_kit_it_refuses_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as empty:
            found = fs.set_crosstalk_messaging(True, home=Path(empty).resolve())
            self.assertEqual((found["outcome"], found["reason"]), ("refused", "not-installed"))
        found = fs.set_crosstalk_messaging(True, home=self.eh, kit_path=self.eh / "no-kit")
        self.assertEqual((found["outcome"], found["reason"]), ("refused", "kit-missing"))
        self.assertFalse((self.device / "controls.json").exists())

    def test_a_busy_machine_lock_is_reported(self):
        with mock.patch.object(sc, "machine_lock", side_effect=sc.LifecycleBusy("busy")):
            self.assertEqual(fs.set_crosstalk_messaging(True, **self.kw)["outcome"], "busy")
            self.assertEqual(fs.set_linked_accounts(["a@example.com"], **self.kw)["outcome"], "busy")
        self.assertFalse((self.device / "controls.json").exists())

    def test_a_kit_error_is_a_failure_not_a_crash(self):
        with mock.patch.object(self.core(), "controls_update", side_effect=OSError("disk full")):
            found = fs.set_crosstalk_messaging(True, **self.kw)
        self.assertEqual(found["outcome"], "failed")
        self.assertIn("disk full", found["error"])


class LinkedAccountsTests(KitCase):
    def test_status_before_anything_is_off_with_the_shared_login_note(self):
        found = fs.linked_accounts_status(**self.kw)
        self.assertEqual((found["outcome"], found["enabled"], found["accounts"], found["suggested"]), ("ok", False, [], []))
        self.assertTrue(any("other people" in note for note in found["notes"]))

    def test_suggest_lists_the_emails_crosstalk_has_seen_and_writes_nothing(self):
        self.see(ACCOUNT, "me@example.com")
        self.see(OTHER, "work@example.org")
        found = fs.suggest_linked_accounts(**self.kw)
        self.assertEqual(found["suggested_emails"], ["me@example.com", "work@example.org"])
        self.assertEqual(found["accounts"], [])
        self.assertFalse(self.config_path().exists())
        status = fs.linked_accounts_status(**self.kw)
        self.assertEqual({item["email"] for item in status["suggested"]}, {"me@example.com", "work@example.org"})
        self.assertFalse(self.config_path().exists())

    def test_set_writes_the_list_without_a_bom_and_keeps_the_other_keys(self):
        self.write_config(json.dumps({"poll_s": 7, "nested": {"a": [1, 2]}}).encode("utf-8"))
        self.see(ACCOUNT, "me@example.com")
        answer = fs.set_linked_accounts(["me@example.com", OTHER.upper()], **self.kw)
        self.assertEqual((answer["outcome"], answer["enabled"], answer["before"]), ("enabled", True, []))
        raw = self.config_path().read_bytes()
        self.assertFalse(raw.startswith(codecs.BOM_UTF8))
        self.assertEqual(json.loads(raw.decode("utf-8")),
                         {"poll_s": 7, "nested": {"a": [1, 2]}, "switch_history": ["me@example.com", OTHER.upper()]})
        self.assertEqual(self.core()._read_json(self.config_path(), {})["switch_history"][0], "me@example.com")
        status = fs.linked_accounts_status(**self.kw)
        self.assertEqual((status["enabled"], status["suggested"], status["unmatched"]), (True, [], []))

    def test_the_same_list_again_changes_nothing_and_a_duplicate_is_dropped(self):
        fs.set_linked_accounts(["me@example.com"], **self.kw)
        before = self.config_path().read_bytes()
        again = fs.set_linked_accounts(["me@example.com", " ME@example.com "], **self.kw)
        self.assertEqual(again["outcome"], "unchanged")
        self.assertEqual(self.config_path().read_bytes(), before)

    def test_an_empty_list_turns_it_off_and_keeps_the_other_keys(self):
        self.write_config(b'{"poll_s": 7}')
        fs.set_linked_accounts(["me@example.com"], **self.kw)
        off = fs.set_linked_accounts([], **self.kw)
        self.assertEqual((off["outcome"], off["enabled"], off["accounts"], off["before"]),
                         ("disabled", False, [], ["me@example.com"]))
        self.assertEqual(json.loads(self.config_path().read_text(encoding="utf-8")),
                         {"poll_s": 7, "switch_history": []})
        self.assertEqual(fs.set_linked_accounts([], **self.kw)["outcome"], "unchanged")
        self.assertFalse(fs.linked_accounts_status(**self.kw)["enabled"])

    def test_turning_it_off_when_it_was_never_set_writes_nothing(self):
        self.assertEqual(fs.set_linked_accounts([], **self.kw)["outcome"], "unchanged")
        self.assertFalse(self.config_path().exists())

    def test_a_bad_entry_is_refused_and_the_file_is_left_alone(self):
        self.write_config(b'{"switch_history": ["keep@example.com"]}')
        before = self.config_path().read_bytes()
        for bad in (["not an email"], [""], [3], "me@example.com", None,
                    [f"user{n}@example.com" for n in range(17)]):
            with self.subTest(bad=bad):
                found = fs.set_linked_accounts(bad, **self.kw)
                self.assertEqual((found["outcome"], found["reason"]), ("refused", "invalid"))
                self.assertEqual(self.config_path().read_bytes(), before)

    def test_an_unreadable_config_is_never_replaced(self):
        for raw in (b"{broken", b"[1, 2]", b'"text"'):
            with self.subTest(raw=raw):
                self.write_config(raw)
                found = fs.set_linked_accounts(["me@example.com"], **self.kw)
                self.assertEqual((found["outcome"], found["reason"]), ("refused", "config-unreadable"))
                self.assertEqual(self.config_path().read_bytes(), raw)
                self.assertEqual(fs.linked_accounts_status(**self.kw)["outcome"], "refused")

    def test_a_config_with_a_bom_is_read_and_rewritten_without_it(self):
        self.write_config(codecs.BOM_UTF8 + b'{"poll_s": 3, "switch_history": ["me@example.com"]}')
        found = fs.set_linked_accounts(["me@example.com"], **self.kw)
        self.assertEqual(found["outcome"], "enabled")  # the helper ignored the BOM file, so this is a real change
        raw = self.config_path().read_bytes()
        self.assertFalse(raw.startswith(codecs.BOM_UTF8))
        self.assertEqual(json.loads(raw.decode("utf-8")), {"poll_s": 3, "switch_history": ["me@example.com"]})

    def test_an_email_never_seen_is_listed_as_unmatched_but_an_account_id_is_not(self):
        fs.set_linked_accounts(["new@example.com", ACCOUNT], **self.kw)
        self.assertEqual(fs.linked_accounts_status(**self.kw)["unmatched"], ["new@example.com"])


class RamTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="eutonos-feature-ram-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "project"
        self.root.mkdir()

    def test_status_is_the_projects_existing_ram_section(self):
        found = fs.ram_status(root=self.root)
        saved = settings.status(self.root)["ram"]
        self.assertEqual((found["outcome"], found["scope"], found["capture"], found["retrieval"], found["enabled"]),
                         ("ok", "project", saved["capture"], saved["retrieval"], True))
        self.assertFalse((self.root / ".eutonos-local" / "settings.json").exists())  # reading writes nothing

    def test_off_and_on_set_both_switches_through_settings_and_keep_cadence_and_detail(self):
        before = settings.status(self.root)["ram"]
        off = fs.set_ram(root=self.root, capture=False, retrieval=False)
        self.assertEqual((off["outcome"], off["enabled"]), ("disabled", False))
        self.assertEqual(off["changed"], {"capture": [True, False], "retrieval": ["on-demand", "off"]})
        saved = settings.status(self.root)["ram"]
        self.assertEqual((saved["capture"], saved["retrieval"], saved["cadence"], saved["detail"]),
                         (False, "off", before["cadence"], before["detail"]))
        self.assertEqual(fs.set_ram(root=self.root, capture=False, retrieval=False)["outcome"], "unchanged")
        on = fs.set_ram(root=self.root, capture=True, retrieval=True)
        self.assertEqual((on["outcome"], on["enabled"], on["retrieval"]), ("enabled", True, "on-demand"))

    def test_one_switch_alone_and_a_mixed_change(self):
        only = fs.set_ram(root=self.root, capture=False)
        self.assertEqual((only["outcome"], only["capture"], only["retrieval"]), ("disabled", False, "on-demand"))
        self.assertTrue(any("closeout" in note for note in only["notes"]))
        mixed = fs.set_ram(root=self.root, capture=True, retrieval=False)
        self.assertEqual(mixed["outcome"], "changed")
        self.assertEqual((settings.status(self.root)["ram"]["capture"], settings.status(self.root)["ram"]["retrieval"]),
                         (True, "off"))

    def test_a_bad_value_is_refused(self):
        for kwargs in ({"retrieval": "sometimes"}, {"capture": "yes"}):
            with self.subTest(kwargs=kwargs):
                found = fs.set_ram(root=self.root, **kwargs)
                self.assertEqual((found["outcome"], found["reason"]), ("refused", "invalid"))

    def test_a_settings_change_in_between_is_retried_once(self):
        real = settings.update
        calls = []

        def stale_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise settings.StaleSettings({"revision": "x"})
            return real(*args, **kwargs)

        with mock.patch.object(settings, "update", stale_once):
            found = fs.set_ram(root=self.root, capture=False)
        self.assertEqual((found["outcome"], len(calls)), ("disabled", 2))
        with mock.patch.object(settings, "update", side_effect=settings.StaleSettings({"revision": "x"})):
            self.assertEqual(fs.set_ram(root=self.root, capture=True)["outcome"], "failed")

    def test_an_unreadable_settings_file_is_refused_not_overwritten(self):
        target = self.root / ".eutonos-local" / "settings.json"
        target.parent.mkdir()
        target.write_text("{broken", encoding="utf-8")
        found = fs.set_ram(root=self.root, capture=False)
        self.assertEqual((found["outcome"], found["reason"]), ("refused", "settings-unavailable"))
        self.assertEqual(target.read_text(encoding="utf-8"), "{broken")


class CliTests(KitCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(fs, "_kit_ctx", lambda base, kit_path=None: {
                "home": str(base), "kit_path": str(self.kit)}):
            code = installer.main(["--home", str(self.eh), *argv])
        return code, out.getvalue()

    def test_status_text_and_json(self):
        root = self.base / "ramproject"
        root.mkdir()
        code, text = self.run_cli("setting", "status", "--root", str(root))
        self.assertEqual(code, 0)
        self.assertRegex(text, r"crosstalk-messaging\s+off")
        self.assertRegex(text, r"ram\s+on\s+capture on, retrieval on-demand")
        self.assertRegex(text, r"linked-accounts\s+off\s+no accounts linked")
        self.assertRegex(text, r"update-mode\s+auto\s+asks EUTONOS/releases")
        code, text = self.run_cli("setting", "status", "--json", "--root", str(root))
        found = json.loads(text)
        self.assertEqual(sorted(found["settings"]), ["crosstalk-messaging", "crosstalk-switches", "linked-accounts", "ram", "update-mode"])
        self.assertEqual(found["settings"]["update-mode"]["mode"], "auto")  # new installs ask before updating, never install alone

    def test_set_goes_through_the_api_and_the_exit_code_follows_the_outcome(self):
        code, text = self.run_cli("setting", "set", "crosstalk-messaging", "on")
        self.assertEqual((code, json.loads(text)["outcome"]), (0, "enabled"))
        self.assertEqual(self.run_cli("setting", "set", "crosstalk-messaging", "on")[0], 0)
        code, text = self.run_cli("setting", "set", "linked-accounts", "me@example.com", "work@example.org")
        self.assertEqual((code, json.loads(text)["accounts"]), (0, ["me@example.com", "work@example.org"]))
        code, text = self.run_cli("setting", "set", "linked-accounts", "off")
        self.assertEqual((code, json.loads(text)["outcome"]), (0, "disabled"))
        self.assertEqual(self.controls()["messaging"], "on")
        root = self.base / "ramproject"
        root.mkdir()
        code, text = self.run_cli("setting", "set", "ram", "off", "--root", str(root))
        self.assertEqual((code, json.loads(text)["outcome"]), (0, "disabled"))
        for argv in (("setting", "set", "nonsense", "on"), ("setting", "set", "ram", "maybe"),
                     ("setting", "set", "linked-accounts", "bad entry")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(*argv)[0], 1)
        self.assertEqual(self.run_cli("setting", "set")[0], 2)

    def pending(self):
        from eutonos import pending_actions
        return [item for item in pending_actions.list_actions(self.eh) if item["source"].startswith("setting:")]

    def test_messaging_on_with_the_part_off_leaves_a_step_and_a_later_call_supersedes_it(self):
        from eutonos import component_state
        component_state.set_enabled(self.eh, "crosstalk", False)
        code, text = self.run_cli("setting", "set", "crosstalk-messaging", "on")
        answer = json.loads(text)
        self.assertEqual((code, answer["outcome"]), (0, "enabled"))
        steps = self.pending()
        self.assertEqual([(item["source"], item["harness"]) for item in steps], [("setting:crosstalk-messaging", "any")])
        self.assertIn("component enable crosstalk", steps[0]["action"])
        self.assertEqual(answer["pending"], [steps[0]["id"]])
        self.run_cli("setting", "set", "crosstalk-messaging", "on")  # the same state again: still one step, not two
        self.assertEqual(len(self.pending()), 1)
        component_state.set_enabled(self.eh, "crosstalk", True)  # the person did what it asked
        self.run_cli("setting", "set", "crosstalk-messaging", "on")
        self.assertEqual(self.pending(), [])

    def test_messaging_on_while_the_owner_has_halted_leaves_a_step_and_off_clears_it(self):
        core = self.core()
        with fs.cs.home_env(fs._kit_ctx(self.eh, self.kit)):
            core.controls_update(halt=True)
        self.run_cli("setting", "set", "crosstalk-messaging", "on")
        self.assertTrue(any("halted" in item["action"] for item in self.pending()))
        self.run_cli("setting", "set", "crosstalk-messaging", "off")
        self.assertEqual(self.pending(), [])

    def test_a_failed_change_is_left_for_a_retry_and_a_refusal_changes_nothing(self):
        failed = {"outcome": "failed", "setting": "linked-accounts", "error": "OSError: disk full"}
        with mock.patch.object(fs, "set_setting", return_value=dict(failed)):
            code, text = self.run_cli("setting", "set", "linked-accounts", "me@example.com")
        self.assertEqual(json.loads(text)["pending"], [self.pending()[0]["id"]])
        self.assertIn("disk full", self.pending()[0]["action"])
        with mock.patch.object(fs, "set_setting", return_value={"outcome": "refused", "setting": "linked-accounts"}):
            self.run_cli("setting", "set", "linked-accounts", "me@example.com")
        self.assertEqual(len(self.pending()), 1)  # the refusal touched nothing, so the earlier step stays

    def test_a_setting_that_leaves_nothing_undone_leaves_no_step(self):
        self.run_cli("setting", "set", "crosstalk-messaging", "on")
        self.run_cli("setting", "set", "linked-accounts", "me@example.com")
        self.assertEqual(self.pending(), [])

    def test_suggest_prints_and_writes_nothing(self):
        self.see(ACCOUNT, "me@example.com")
        code, text = self.run_cli("setting", "suggest")
        self.assertEqual((code, json.loads(text)["suggested_emails"]), (0, ["me@example.com"]))
        self.assertFalse(self.config_path().exists())


if __name__ == "__main__":
    unittest.main()
