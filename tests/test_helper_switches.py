"""The Crosstalk and Claude helper switches (``feature_settings.HELPER_SWITCHES``), the Crosstalk part's stay-on rule,
Turn everything off, the chat copy guard, and the controls the Crosstalk page writes (stop, deny list, wake budget).

Everything runs in temporary homes and device folders, and a temporary Startup folder; nothing here reads or writes the
real ``~/.eutonos``, the real Startup folder or the real Crosstalk controls. The platform and the part switch are seams.
"""
import json
import unittest
from unittest import mock

from eutonos import component_state
from eutonos import feature_settings as fs
from eutonos import installer
from eutonos import pending_actions

import test_feature_settings as tf

CHAT = "local_0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_CHAT = "local_3f2a9c1e-5b7d-4e8a-9c0b-1a2b3c4d5e6f"
CLAUDE_KEY = "claude:0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
BOOLS = [item["key"] for item in fs.HELPER_SWITCHES if item["kind"] == "bool"]


class SwitchCase(tf.KitCase):
    run_cli = tf.CliTests.run_cli

    def setUp(self):
        super().setUp()
        self.startup = self.base / "startup"
        self.startup.mkdir()
        for patcher in (mock.patch.object(fs, "_is_windows", return_value=True),
                        mock.patch.object(fs, "_startup_folder", return_value=self.startup)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self):
        return json.loads(self.config_path().read_text(encoding="utf-8")) if self.config_path().is_file() else {}

    def row(self, key):
        return next(item for item in fs.helper_switches_status(**self.kw)["switches"] if item["key"] == key)

    def steps(self):
        return pending_actions.list_actions(state="pending")

    def everything_off(self):
        with mock.patch.object(fs, "_set_part", return_value={"outcome": "disabled"}):
            return fs.turn_everything_off(**self.kw)


class StatusAndRoundTrips(SwitchCase):
    def test_every_declared_switch_is_listed_with_its_default_and_reading_writes_nothing(self):
        found = fs.helper_switches_status(**self.kw)
        self.assertEqual(found["outcome"], "ok")
        self.assertEqual([item["key"] for item in found["switches"]], [item["key"] for item in fs.HELPER_SWITCHES])
        defaults = {item["key"]: item["value"] for item in found["switches"]}
        self.assertIs(defaults["chat_copy"], False)  # off until the owner chooses it
        for key in BOOLS:
            if key != "chat_copy":
                self.assertIs(defaults[key], True, key)
        self.assertEqual((defaults["link_repair_offer"], defaults["focus_notice_seconds"], defaults["rename_min_idle_s"]),
                         ("ask", 3, 15))
        self.assertFalse(self.config_path().exists())
        for item in found["switches"]:
            self.assertTrue(item["label"] and item["help"] and item["page"] in ("crosstalk", "claude"), item["key"])

    def test_each_boolean_switch_round_trips_with_explicit_values_and_keeps_other_keys(self):
        self.write_config(json.dumps({"keep": {"a": 1}, "switch_history": ["me@example.com"]}).encode("utf-8"))
        for key in BOOLS:
            with self.subTest(key=key):
                before = self.row(key)["value"]
                flipped = fs.set_helper_switch(key, not before, **self.kw)
                self.assertEqual(flipped["outcome"], "enabled" if not before else "disabled", flipped)
                self.assertIs(self.config()[key], not before)
                self.assertIs(self.row(key)["value"], not before)
                back = fs.set_helper_switch(key, before, **self.kw)
                self.assertEqual(back["outcome"], "enabled" if before else "disabled")
                self.assertIs(self.config()[key], before)  # an explicit value, even when it is the default
                self.assertEqual(fs.set_helper_switch(key, before, **self.kw)["outcome"], "unchanged")
        self.assertEqual(self.config()["keep"], {"a": 1})
        self.assertEqual(self.config()["switch_history"], ["me@example.com"])
        self.assertFalse(self.config_path().read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_choice_number_and_list_switches_round_trip_and_bad_values_change_nothing(self):
        self.assertEqual(fs.set_helper_switch("link_repair_offer", "auto", **self.kw)["outcome"], "changed")
        self.assertEqual(self.config()["link_repair_offer"], "auto")
        self.assertEqual(fs.set_helper_switch("focus_notice_seconds", 0, **self.kw)["outcome"], "changed")
        self.assertEqual(self.config()["focus_notice_seconds"], 0)
        self.assertEqual(fs.set_helper_switch("rename_min_idle_s", 30.0, **self.kw)["value"], 30)
        listed = fs.set_helper_switch("always_remote", [CHAT.upper(), OTHER_CHAT, CHAT], **self.kw)
        self.assertEqual((listed["outcome"], listed["value"]), ("changed", [CHAT, OTHER_CHAT]))  # lower case, no repeats
        self.assertEqual(self.config()["always_remote"], [CHAT, OTHER_CHAT])
        self.assertEqual(fs.set_helper_switch("always_remote", [], **self.kw)["outcome"], "changed")
        self.assertEqual(self.config()["always_remote"], [])
        stamp = self.config_path().read_bytes()
        for key, bad in (("link_repair_offer", "bogus"), ("focus_notice_seconds", 31), ("focus_notice_seconds", -1),
                         ("focus_notice_seconds", True), ("rename_min_idle_s", 1), ("rename_min_idle_s", float("nan")),
                         ("always_remote", ["not a chat"]), ("always_remote", "local_x"),
                         ("letter_wake", "yes"), ("letter_wake", 1)):
            with self.subTest(key=key, value=bad):
                found = fs.set_helper_switch(key, bad, **self.kw)
                self.assertEqual((found["outcome"], found["reason"]), ("refused", "invalid"))
        self.assertEqual(self.config_path().read_bytes(), stamp)

    def test_the_same_choice_or_number_again_writes_nothing_and_the_default_on_a_fresh_file_is_not_stored(self):
        self.assertEqual(fs.set_helper_switch("link_repair_offer", "ask", **self.kw)["outcome"], "unchanged")
        self.assertEqual(fs.set_helper_switch("focus_notice_seconds", 3, **self.kw)["outcome"], "unchanged")
        self.assertFalse(self.config_path().exists())

    def test_unknown_and_managed_elsewhere_switches_are_refused(self):
        self.assertEqual(fs.set_helper_switch("nope", True, **self.kw)["reason"], "unknown-switch")
        self.assertEqual(fs.set_helper_switch("switch_history", ["a@example.com"], **self.kw)["reason"], "managed-elsewhere")
        self.assertFalse(self.config_path().exists())

    def test_without_a_kit_it_refuses_and_writes_nothing(self):
        found = fs.set_helper_switch("letter_wake", False, home=self.eh, kit_path=self.eh / "no-kit")
        self.assertEqual((found["outcome"], found["reason"]), ("refused", "kit-missing"))
        self.assertFalse(self.config_path().exists())
        self.assertEqual(fs.helper_switches_status(home=self.eh, kit_path=self.eh / "no-kit")["outcome"], "refused")

    def test_the_cli_reads_and_sets_a_switch(self):
        code, text = self.run_cli("setting", "set", "crosstalk-switch", "letter_wake", "off")
        self.assertEqual((code, json.loads(text)["outcome"]), (0, "disabled"))
        self.assertIs(self.config()["letter_wake"], False)
        code, text = self.run_cli("setting", "set", "crosstalk-switch", "always_remote", CHAT, OTHER_CHAT)
        self.assertEqual((code, json.loads(text)["value"]), (0, [CHAT, OTHER_CHAT]))
        code, text = self.run_cli("setting", "status")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"crosstalk-switches\s+\d+ of \d+ on")


class NotWindows(SwitchCase):
    def test_a_windows_only_switch_is_listed_disabled_never_hidden_and_cannot_be_set(self):
        with mock.patch.object(fs, "_is_windows", return_value=False):
            found = fs.helper_switches_status(**self.kw)
            self.assertFalse(found["windows"])
            rows = {item["key"]: item for item in found["switches"]}
            self.assertEqual(set(rows), {item["key"] for item in fs.HELPER_SWITCHES})
            for item in fs.HELPER_SWITCHES:
                self.assertEqual(rows[item["key"]]["available"], item["platform"] == "any", item["key"])
            self.assertTrue(rows["link_repair_offer"]["available"])
            for key in ("letter_wake", "relink", "rename_titles", "chat_copy", "focus_notice_seconds"):
                answer = fs.set_helper_switch(key, False if key != "focus_notice_seconds" else 5, **self.kw)
                self.assertEqual((answer["outcome"], answer["reason"]), ("refused", "not-available"), key)
            self.assertFalse(self.config_path().exists())
            self.assertEqual(fs.set_helper_switch("link_repair_offer", "off", **self.kw)["outcome"], "changed")
            self.assertEqual(fs.crosstalk_part_blockers(**self.kw), [])  # nothing that acts here counts as on


class PartOffAndEverythingOff(SwitchCase):
    def setUp(self):
        super().setUp()
        real = fs._kit_ctx  # the installer's guard asks for the active kit; this test home has the kit but no install record
        patcher = mock.patch.object(fs, "_kit_ctx", lambda base, kit_path=None: real(base, kit_path or self.kit))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_switching_the_part_off_is_never_refused_because_a_switch_is_on(self):
        fs.set_crosstalk_messaging(True, **self.kw)
        self.assertTrue(fs.crosstalk_part_blockers(**self.kw))  # switches are on, and that does not matter
        with mock.patch.object(installer, "Run", side_effect=RuntimeError("reached the install checks")):
            with self.assertRaises(RuntimeError):
                installer.set_component("crosstalk", False, home=self.eh)   # no refusal came first

    def test_the_switches_are_kept_untouched_while_the_part_is_off(self):
        fs.set_helper_switch("relink_alert", True, **self.kw)
        before = self.config()
        component_state.set_enabled(self.eh, "crosstalk", False)
        self.assertEqual(self.config(), before)
        self.assertEqual(fs.helper_switches_status(**self.kw)["switches"][0]["key"], fs.HELPER_SWITCHES[0]["key"])

    def test_one_helper_switch_alone_is_enough(self):
        self.everything_off()
        self.assertEqual(fs.crosstalk_part_blockers(**self.kw), [])
        fs.set_helper_switch("relink_alert", True, **self.kw)
        self.assertEqual([item["key"] for item in fs.crosstalk_part_blockers(**self.kw)], ["relink_alert"])

    def test_turn_everything_off_switches_every_switch_off_then_the_part_and_leaves_the_rest(self):
        core = self.core()
        with fs.cs.home_env(fs._kit_ctx(self.eh, self.kit)):
            core.controls_update(halt=True, deny_add="claude:x", wake_budget={"per_chat": 2})
        fs.set_crosstalk_messaging(True, **self.kw)
        fs.set_helper_switch("link_repair_offer", "auto", **self.kw)
        fs.set_linked_accounts(["me@example.com", "you@example.com"], **self.kw)
        with mock.patch.object(fs, "_set_part", return_value={"outcome": "disabled", "notes": []}) as part:
            answer = fs.turn_everything_off(**self.kw)
            part.assert_called_once_with(self.eh, False)
        self.assertEqual((answer["outcome"], answer["part"]), ("disabled", "disabled"))
        self.assertIn("Crosstalk messaging (letters)", answer["turned_off"])
        self.assertEqual(self.controls()["messaging"], "off")
        for key in BOOLS:
            self.assertIs(self.config()[key], False, key)
        self.assertEqual(self.config()["link_repair_offer"], "auto")
        self.assertEqual(self.config()["switch_history"], ["me@example.com", "you@example.com"])
        self.assertTrue(self.controls()["halt"])  # the owner's stop is never cleared or set by this
        self.assertEqual(self.controls()["deny"], ["claude:x"])
        self.assertEqual(self.controls()["wake_budget"]["per_chat"], 2)
        self.assertEqual(fs.crosstalk_part_blockers(**self.kw), [])

    def test_turn_everything_off_says_so_when_the_part_itself_will_not_switch_off(self):
        refused = {"outcome": "refused", "reason": "needed-by", "detail": "remote-control needs crosstalk.",
                   "next_action": "Switch remote-control off first."}
        with mock.patch.object(fs, "_set_part", return_value=refused):
            answer = fs.turn_everything_off(**self.kw)
        self.assertEqual((answer["outcome"], answer["reason"]), ("refused", "needed-by"))
        self.assertIn("Every switch is now off", answer["detail"])
        self.assertEqual(fs.crosstalk_part_blockers(**self.kw), [])

    def test_turn_everything_off_with_no_kit_refuses_and_writes_nothing(self):
        answer = fs.turn_everything_off(home=self.eh, kit_path=self.eh / "no-kit")
        self.assertEqual((answer["outcome"], answer["reason"]), ("refused", "kit-missing"))
        self.assertFalse(self.config_path().exists())

    def test_the_cli_turns_everything_off(self):
        with mock.patch.object(fs, "_set_part", return_value={"outcome": "disabled"}):
            code, text = self.run_cli("setting", "set", "crosstalk-everything", "off")
        self.assertEqual((code, json.loads(text)["outcome"]), (0, "disabled"))
        self.assertEqual(fs.crosstalk_part_blockers(**self.kw), [])


class TurningOneOn(SwitchCase):
    def test_a_boolean_switch_turned_on_while_the_part_is_off_switches_the_part_on_first_and_says_when_it_starts(self):
        component_state.set_enabled(self.eh, "crosstalk", False)

        def part_on(base, enable):
            component_state.set_enabled(base, "crosstalk", enable)
            return {"outcome": "enabled", "notes": []}

        with mock.patch.object(fs, "_set_part", side_effect=part_on) as part:
            answer = fs.set_helper_switch("letter_wake", True, **self.kw)
            part.assert_called_once_with(self.eh, True)
        self.assertEqual(answer["outcome"], "enabled")
        joined = " ".join(answer["notes"])
        self.assertIn("switched on first", joined)
        self.assertIn("next Windows login", joined)
        self.assertIn("does not start the helper now", joined)
        self.assertTrue(component_state.component_enabled(self.eh, "crosstalk"))

    def test_messaging_turned_on_does_the_same(self):
        component_state.set_enabled(self.eh, "crosstalk", False)
        with mock.patch.object(fs, "_set_part", return_value={"outcome": "enabled"}) as part:
            answer = fs.set_crosstalk_messaging(True, **self.kw)
            part.assert_called_once_with(self.eh, True)
        self.assertTrue(any("next Windows login" in note for note in answer["notes"]))

    def test_turning_off_or_setting_a_choice_or_number_leaves_the_part_alone(self):
        component_state.set_enabled(self.eh, "crosstalk", False)
        with mock.patch.object(fs, "_set_part") as part:
            fs.set_helper_switch("letter_wake", False, **self.kw)
            fs.set_helper_switch("link_repair_offer", "off", **self.kw)
            fs.set_helper_switch("focus_notice_seconds", 1, **self.kw)
            part.assert_not_called()

    def test_a_part_that_cannot_be_switched_on_is_said_plainly_and_the_switch_is_still_set(self):
        component_state.set_enabled(self.eh, "crosstalk", False)
        with mock.patch.object(fs, "_set_part", return_value={"outcome": "refused", "reason": "not-installed",
                                                              "detail": "crosstalk is not installed here"}):
            answer = fs.set_helper_switch("relink", True, **self.kw)
        self.assertEqual(answer["outcome"], "enabled")
        self.assertIs(self.config()["relink"], True)
        self.assertTrue(any("could not be switched on" in note and "component enable crosstalk" in note
                            for note in answer["notes"]))

    def test_a_part_that_is_on_is_not_touched(self):
        with mock.patch.object(fs, "_set_part") as part:
            fs.set_helper_switch("letter_wake", True, **self.kw)
            part.assert_not_called()


class ChatCopyGuard(SwitchCase):
    def personal_shortcut(self, text="C:\\Python\\pythonw.exe C:\\Users\\me\\claude-chat-sync\\chat_sync.py --watch",
                          encoding="ascii"):
        path = self.startup / "Claude chat sync.lnk"
        path.write_bytes(b"LNK\x00" + text.encode(encoding))
        return path

    def test_it_is_refused_with_a_pending_step_while_the_personal_copier_is_in_the_startup_folder(self):
        self.personal_shortcut()
        answer = fs.set_helper_switch("chat_copy", True, **self.kw)
        self.assertEqual((answer["outcome"], answer["reason"]), ("refused", "legacy-copier"))
        self.assertIn("Claude chat sync.lnk", answer["detail"])
        self.assertNotIn("chat_copy", self.config())  # nothing written
        steps = self.steps()
        self.assertEqual([(item["source"], item["harness"]) for item in steps], [(fs.CHAT_COPY_SOURCE, "any")])
        self.assertEqual(answer["pending"], [steps[0]["id"]])
        fs.set_helper_switch("chat_copy", True, **self.kw)  # asking again leaves one step, not two
        self.assertEqual(len(self.steps()), 1)

    def test_it_also_finds_the_shortcut_when_its_arguments_are_stored_as_utf16(self):
        self.personal_shortcut(encoding="utf-16-le")
        self.assertEqual(fs.legacy_chat_copier(), ["Claude chat sync.lnk"])
        self.assertEqual(fs.set_helper_switch("chat_copy", True, **self.kw)["reason"], "legacy-copier")

    def test_a_shortcut_for_something_else_does_not_block_it(self):
        (self.startup / "Other.lnk").write_bytes(b"LNK\x00C:\\apps\\other.exe --watch")
        (self.startup / "notes.txt").write_text("chat_sync.py", encoding="utf-8")  # not a launcher
        self.assertEqual(fs.legacy_chat_copier(), [])
        self.assertEqual(fs.set_helper_switch("chat_copy", True, **self.kw)["outcome"], "enabled")

    def test_once_the_shortcut_is_gone_it_turns_on_clears_the_step_and_says_it_needs_two_accounts(self):
        shortcut = self.personal_shortcut()
        fs.set_helper_switch("chat_copy", True, **self.kw)
        self.assertEqual(len(self.steps()), 1)
        shortcut.unlink()
        answer = fs.set_helper_switch("chat_copy", True, **self.kw)
        self.assertEqual(answer["outcome"], "enabled")
        self.assertIs(self.config()["chat_copy"], True)
        self.assertEqual(self.steps(), [])
        self.assertTrue(any("two linked accounts" in note for note in answer["notes"]))

    def test_turning_it_off_is_always_allowed_and_the_other_two_copy_switches_are_not_guarded(self):
        self.personal_shortcut()
        self.assertEqual(fs.set_helper_switch("chat_copy", False, **self.kw)["outcome"], "disabled")
        self.assertEqual(fs.set_helper_switch("chat_copy_deletions", False, **self.kw)["outcome"], "disabled")
        self.assertEqual(fs.set_helper_switch("chat_copy_snapshots", False, **self.kw)["outcome"], "disabled")

    def test_the_status_reports_the_personal_copier_so_the_page_can_say_so(self):
        self.assertEqual(fs.helper_switches_status(**self.kw)["legacy_copier"], {"found": False, "files": []})
        self.personal_shortcut()
        self.assertEqual(fs.helper_switches_status(**self.kw)["legacy_copier"],
                         {"found": True, "files": ["Claude chat sync.lnk"]})

    def test_a_missing_startup_folder_is_not_a_copier(self):
        self.assertEqual(fs.legacy_chat_copier(self.base / "no-such-folder"), [])


class Controls(SwitchCase):
    def test_the_status_reports_stop_messaging_deny_budget_and_the_idle_state(self):
        found = fs.crosstalk_controls_status(**self.kw)
        self.assertEqual((found["outcome"], found["halt"], found["messaging"], found["deny"]), ("ok", False, False, []))
        self.assertEqual(set(found["wake_budget"]), {"per_chat", "total", "window_s"})
        self.assertIn("per_chat", found["wake_budget_limits"])
        self.assertEqual(found["recent_letters"], [])
        self.assertFalse(found["idle"])  # the Claude switches default on
        self.everything_off()
        idle = fs.crosstalk_controls_status(**self.kw)
        self.assertEqual((idle["idle"], idle["summary"]), (True, "Idle: everything is switched off"))
        fs.set_crosstalk_messaging(True, **self.kw)
        self.assertEqual(fs.crosstalk_controls_status(**self.kw)["summary"], "On")

    def test_the_deny_list_blocks_and_allows_one_chat_and_refuses_a_bad_key(self):
        self.assertEqual(fs.set_deny("block", CLAUDE_KEY, **self.kw)["outcome"], "enabled")
        self.assertEqual(self.controls()["deny"], [CLAUDE_KEY])
        self.assertEqual(fs.set_deny("block", CLAUDE_KEY, **self.kw)["outcome"], "unchanged")
        self.assertEqual(fs.set_deny("allow", CLAUDE_KEY, **self.kw)["outcome"], "disabled")
        self.assertEqual(self.controls()["deny"], [])
        for action, key in (("block", "nonsense"), ("block", ""), ("ban", CLAUDE_KEY), ("block", 4)):
            self.assertEqual(fs.set_deny(action, key, **self.kw)["outcome"], "refused", (action, key))
        self.assertEqual(self.controls()["deny"], [])

    def test_the_wake_budget_changes_the_fields_sent_and_refuses_out_of_range_values(self):
        before = fs.crosstalk_controls_status(**self.kw)["wake_budget"]
        answer = fs.set_wake_budget({"per_chat": 3}, **self.kw)
        self.assertEqual((answer["outcome"], answer["wake_budget"]["per_chat"]), ("changed", 3))
        self.assertEqual(answer["wake_budget"]["total"], before["total"])
        self.assertEqual(fs.set_wake_budget({"per_chat": 3}, **self.kw)["outcome"], "unchanged")
        for values in ({}, {"bogus": 1}, {"per_chat": 1001}, {"window_s": 10}):
            self.assertEqual(fs.set_wake_budget(values, **self.kw)["outcome"], "refused", values)
        self.assertEqual(self.controls()["wake_budget"]["per_chat"], 3)

    def test_the_stop_goes_through_the_crosstalk_command_and_only_takes_true_or_false(self):
        with mock.patch("eutonos.crosstalk_status.owner_control", return_value={"ok": True}) as control:
            self.assertEqual(fs.set_halt(True)["outcome"], "enabled")
            control.assert_called_with("halt")
            self.assertEqual(fs.set_halt(False)["outcome"], "disabled")
            control.assert_called_with("resume")
            self.assertEqual(fs.set_halt("yes")["outcome"], "refused")
            self.assertEqual(control.call_count, 2)
        with mock.patch("eutonos.crosstalk_status.owner_control", return_value={"ok": False, "message": "no"}):
            failed = fs.set_halt(True)
        self.assertEqual((failed["outcome"], failed["error"]), ("failed", "no"))


class DashboardRead(SwitchCase):
    def test_the_dashboard_read_carries_the_switch_and_controls_blocks_and_works_with_no_project(self):
        from eutonos import dashboard_features
        found = dashboard_features.read()
        self.assertEqual(found["settings"]["ram"]["outcome"], "no-project")
        self.assertIn(found["settings"][fs.HELPER]["outcome"], ("ok", "refused", "failed"))
        self.assertIn("crosstalk-controls", found["settings"])
        with mock.patch.object(fs, "_kit_ctx", side_effect=RuntimeError("boom")):
            hidden = dashboard_features.read()
        self.assertEqual(hidden["settings"][fs.HELPER]["outcome"], "failed")  # one failing block hides nothing else
        self.assertEqual(hidden["pending"]["outcome"], "ok")

    def test_the_dashboard_action_for_a_switch_calls_the_owner_and_everything_off_has_no_body(self):
        from eutonos import dashboard_features
        with mock.patch.object(fs, "set_helper_switch", return_value={"outcome": "enabled"}) as owner:
            dashboard_features.act("helper-switch", {"key": "relink", "value": True})
            owner.assert_called_once_with("relink", True)
        with self.assertRaises(ValueError):
            dashboard_features.act("everything-off", {"now": True})
        with self.assertRaises(ValueError):
            dashboard_features.act("helper-switch", {"key": "relink", "value": None})


if __name__ == "__main__":
    unittest.main()
