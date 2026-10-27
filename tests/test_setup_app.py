"""The Windows Setup app: the consent-file route, the real-install mark, the user variable, the wizard helpers,
the bootstrap, the build script's Python checks and a compile of the Inno script (skipped without ISCC)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_offers  # noqa: E402
import build_setup  # noqa: E402
import build_wheel  # noqa: E402
import test_installer as it  # noqa: E402
import test_offers as offers_fixture  # noqa: E402
from eutonos import consent  # noqa: E402
from eutonos import dashboard_cli  # noqa: E402
from eutonos import home as eutonos_home  # noqa: E402
from eutonos import install_record as ir  # noqa: E402
from eutonos import installer  # noqa: E402
from eutonos import installer_effects as fx  # noqa: E402
from eutonos import setup_wizard as wiz  # noqa: E402
from eutonos.installer_ports import FakePorts  # noqa: E402

BOOTSTRAP = ROOT / "installer" / "windows" / "bootstrap.py"
ISS = ROOT / "installer" / "windows" / "EUTONOS.iss"
READY = {"ok": True, "name": "git", "guidance": ""}


def parse_ini(path) -> dict:
    values = {}
    for line in Path(path).read_text(encoding="ascii").splitlines()[1:]:
        key, _, value = line.partition("=")
        values[key] = value
    return values


class SetupBase(it.UpdateBase):
    """A Windows install into a home that is not ``~/.eutonos``, with the person's home faked so nothing real is read."""
    OS = "windows"

    def setUp(self):
        super().setUp()
        self.lad = self.base / "localappdata"
        patches = [mock.patch.object(Path, "home", classmethod(lambda cls, _user=self.user: _user)),
                   mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.lad), "APPDATA": str(self.base / "appdata"),
                                                "USERPROFILE": str(self.user), "HOME": str(self.user)})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def prepared(self, *, choices=None, adopt=False, user_home=None, home=None):
        home = home or self.h
        return wiz.prepare_consent(home, choices=choices or {}, for_this_user=True, adopt=adopt,
                                   python_dir=Path(home) / "python", installed_apps=True, user_home=user_home,
                                   os_name="windows", manifests=[])

    def consent_file(self, template, **changes):
        self._files = getattr(self, "_files", 0) + 1
        path = self.base / f"consent{self._files}.json"
        value = wiz.accept_template(template) if changes.pop("accepted", True) else dict(template)
        value.update(changes)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def real_install(self, **extra):
        return self.install(user_home=None, for_this_user=True, **extra)


class ConsentFileTests(SetupBase):
    def install_with(self, path, **extra):
        return self.install(consent=None, consent_file=path, for_this_user=True, user_home=None, **extra)

    def test_an_accepted_file_installs_and_the_record_keeps_exactly_what_was_shown(self):
        template = self.prepared()
        result = self.install_with(self.consent_file(template))
        self.assertEqual(result["outcome"], "installed", result)
        block = ir.read_record(self.layout, result["gen_id"])["consent"]
        self.assertEqual((block["surface"], block["text"]), ("setup-wizard", template["text"]))
        self.assertEqual(block["text_sha256"], consent.text_sha256(template["text"]))
        self.assertEqual(block["base_sha256"], consent.text_sha256(consent.render(None, None, os_name="windows")))
        self.assertRegex(block["accepted_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("What this setup will do:", block["text"])
        self.assertIn(str(self.h), block["text"])

    def test_the_text_and_the_choices_are_checked_against_this_computers_own_rendering(self):
        good = self.prepared()
        shown = good["text"]
        elsewhere = self.prepared(home=self.base / "other-home")
        adopting = self.prepared(adopt=True)
        cases = {
            "declined": self.consent_file(good, accepted=False),
            "wrong-surface": self.consent_file(good, surface="terminal"),
            "hash-mismatch": self.consent_file(good, text=shown + "extra\n"),
            "bad-time": self.consent_file(good, accepted_at="yesterday"),
            "text-mismatch": self.consent_file(elsewhere),
            "unknown-choice": self.consent_file(good, choices={"not-a-permission": "on"}),
        }
        for reason, path in cases.items():
            result = self.install_with(path)
            self.assertEqual((result["outcome"], result["reason"]), ("consent-refused", reason), result)
        forged = dict(adopting, text=shown, text_sha256=consent.text_sha256(shown))
        forged["text"] = shown.replace("What this setup will do:", "What this setup will do:\n- Nothing at all.")
        forged["text_sha256"] = consent.text_sha256(forged["text"])
        self.assertEqual(self.install_with(self.consent_file(forged))["reason"], "text-mismatch")
        self.assertEqual(self.install_with(self.base / "missing.json")["reason"], "unreadable")
        (self.base / "bad.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(self.install_with(self.base / "bad.json")["reason"], "unreadable")
        self.assertFalse((self.h / "runtimes").exists())  # every refusal came before anything was written
        self.assertEqual(it.snapshot(self.user), {})

    def test_adopt_changes_the_text_so_a_file_shown_without_it_is_refused_with_it(self):
        plain = self.prepared()
        result = self.install_with(self.consent_file(plain), adopt=True)
        self.assertEqual((result["outcome"], result["reason"]), ("consent-refused", "text-mismatch"), result)
        again = self.install_with(self.consent_file(self.prepared(adopt=True)), adopt=True)
        self.assertEqual(again["outcome"], "installed", again)

    def test_the_parts_chosen_in_the_wizard_must_be_the_parts_installed(self):
        comp, _ = it.fake_component(self, "fakecomp")
        shown = wiz.prepare_consent(self.h, choices={"fakecomp": "off"}, for_this_user=True, adopt=False,
                                    python_dir=self.h / "python", installed_apps=True, os_name="windows", manifests=[comp])
        path = self.consent_file(shown)
        refused = self.install_with(path, components=[comp], component_choices={"fakecomp": "on"})
        self.assertEqual(refused["reason"], "text-mismatch", refused)
        accepted = self.install_with(path, components=[comp], component_choices={"fakecomp": "off"})
        self.assertEqual(accepted["outcome"], "installed", accepted)
        self.assertFalse((self.user / "fakecomp.marker").exists())

    def test_the_only_i_use_this_login_note_is_part_of_the_accepted_text_and_a_changed_note_is_refused(self):
        comp, _ = it.fake_component(self, "fakecomp")

        def shown(**extra):
            return wiz.prepare_consent(self.h, for_this_user=True, adopt=False, python_dir=self.h / "python",
                                       installed_apps=True, os_name="windows", manifests=[comp], **extra)
        solo = shown(notes=[wiz.SOLO_NOTE])
        self.assertIn(wiz.SOLO_NOTE, solo["text"])
        self.assertNotIn("only you use this Windows login", shown()["text"])
        # the text carries the note but the recorded extras do not: this computer renders other text, so it is refused
        forged = dict(solo, setup=dict(solo["setup"], notes=[]))
        refused = self.install_with(self.consent_file(forged), components=[comp])
        self.assertEqual((refused["outcome"], refused["reason"]), ("consent-refused", "text-mismatch"), refused)
        self.assertFalse((self.h / "runtimes").exists())
        accepted = self.install_with(self.consent_file(solo), components=[comp])  # the engine reads the note from the file
        self.assertEqual(accepted["outcome"], "installed", accepted)
        self.assertIn(wiz.SOLO_NOTE, ir.read_record(self.layout, accepted["gen_id"])["consent"]["text"])

    def test_the_typed_console_route_is_unchanged(self):
        result = self.install(user_home=self.user)  # consent=CONSENT, the dictionary the console route produces
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(ir.read_record(self.layout, result["gen_id"])["consent"]["surface"], "test")


class RealInstallMarkTests(SetupBase):
    def test_a_marked_install_reaches_the_real_home_and_update_and_uninstall_follow_the_mark(self):
        comp, _ = it.fake_component(self, "fakecomp")
        first = self.real_install(components=[comp])
        self.assertEqual(first["outcome"], "installed", first)
        self.assertTrue((self.user / "fakecomp.marker").is_file())
        self.assertFalse((self.h / "staging-user").exists())
        self.assertTrue(eutonos_home.for_this_user(self.h))
        self.assertTrue(ir.read_record(self.layout, first["gen_id"])["for_this_user"])
        it.make_release(self.releases, "r2", version="1.3.0", body="two")
        updated = self.update(user_home=None, components=[comp])
        self.assertEqual(updated["outcome"], "updated", updated)
        self.assertTrue(ir.read_record(self.layout, updated["gen_id"])["for_this_user"])
        self.assertTrue((self.user / "fakecomp.marker").is_file())
        self.assertFalse((self.h / "staging-user").exists())
        gone = self.uninstall(user_home=None, components=[comp])
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertFalse((self.user / "fakecomp.marker").exists())
        self.assertTrue(eutonos_home.for_this_user(self.h))  # the mark stays with the home, for the next install

    def test_rollback_follows_the_mark(self):
        comp, _ = it.fake_component(self, "fakecomp")
        self.real_install(components=[comp])
        it.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual(self.update(user_home=None, components=[comp])["outcome"], "updated")
        back = self.rollback(user_home=None, components=[comp])
        self.assertEqual(back["outcome"], "rolled-back-to", back)
        self.assertTrue((self.user / "fakecomp.marker").is_file())
        self.assertFalse((self.h / "staging-user").exists())

    def test_resume_follows_the_mark(self):
        interrupted, _ = it.fake_component(self, "fakecomp", fail="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            self.real_install(components=[interrupted])
        self.assertTrue(eutonos_home.for_this_user(self.h))
        result = installer.resume(home=self.h, user_home=None, os_name=self.OS, ports=self.ports, components=[interrupted])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertTrue((self.user / "fakecomp.marker").is_file())
        self.assertFalse((self.h / "staging-user").exists())

    def test_the_mark_is_found_without_being_told_by_every_home_lookup(self):
        self.assertEqual(installer.default_user_home(self.h), self.h / "staging-user")
        self.real_install(components=[])
        self.assertIsNone(installer.default_user_home(self.h))
        from eutonos import component_support as cs
        self.assertEqual(cs.user_home({"home": str(self.h)}), self.user)
        run = installer.Run(self.h, kind="uninstall", ports=self.ports, os_name=self.OS)
        self.assertTrue(run.for_this_user)
        self.assertEqual(run.user_home, self.user)

    def test_an_unflagged_home_that_is_not_the_default_stays_a_staging_home(self):
        comp, _ = it.fake_component(self, "fakecomp")
        (self.h / "staging-user").mkdir(parents=True)  # the fake component does not make its own folder
        result = self.install(user_home=None, components=[comp])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertTrue((self.h / "staging-user" / "fakecomp.marker").is_file())
        self.assertEqual(it.snapshot(self.user), {})
        self.assertFalse(eutonos_home.for_this_user(self.h))
        self.assertFalse(ir.read_record(self.layout, result["gen_id"])["for_this_user"])
        self.assertEqual(self.ports.user_env, {})
        gone = self.uninstall(user_home=None, components=[comp])
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertFalse((self.h / "staging-user" / "fakecomp.marker").exists())
        self.assertEqual(it.snapshot(self.user), {})

    def test_an_unreadable_state_keeps_the_staging_rule(self):
        self.h.joinpath("install").mkdir(parents=True)
        self.h.joinpath("install", "state.json").write_text("{nope", encoding="utf-8")
        self.assertFalse(eutonos_home.for_this_user(self.h))
        self.assertEqual(installer.default_user_home(self.h), self.h / "staging-user")

    def test_the_mark_is_refused_off_windows_for_a_staging_folder(self):
        ports = FakePorts("macos")
        result = installer.install(self.wheel, home=self.h, user_home=None, os_name="macos", ports=ports, consent=it.CONSENT,
                                   prereq=lambda: {"outcome": "ready", "missing": []}, components=[], for_this_user=True)
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "for-this-user-windows-only"))
        self.assertFalse(self.h.exists())


class EnvVarTests(SetupBase):
    def test_a_real_install_of_another_folder_sets_eutonos_home_and_uninstall_removes_it(self):
        result = self.real_install(components=[])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.ports.user_env, {"EUTONOS_HOME": str(self.h)})
        effect = next(item for item in ir.read_record(self.layout, result["gen_id"])["effects"]
                      if item["effect_id"] == "env-var.eutonos-home")
        self.assertEqual((effect["kind"], effect["permission_entry"], effect["undo"]["op"]),
                         ("env-var", "user-env-home", "env-var-remove"))
        gone = self.uninstall(user_home=None, components=[])
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(self.ports.user_env, {})
        self.assertIn("env-var.eutonos-home", gone["removed"])

    def test_the_default_home_needs_no_variable(self):
        default = self.user / ".eutonos"
        result = self.install(home=default, user_home=None, for_this_user=True, components=[])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.ports.user_env, {})

    def test_a_staging_install_never_sets_it(self):
        self.install(user_home=self.user, components=[])
        self.assertEqual(self.ports.user_env, {})

    def test_a_value_the_person_already_set_is_left_alone_and_noted(self):
        self.ports.user_env["EUTONOS_HOME"] = "D:/mine"
        result = self.real_install(components=[])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.ports.user_env["EUTONOS_HOME"], "D:/mine")
        ids = {item["effect_id"] for item in ir.read_record(self.layout, result["gen_id"])["effects"]}
        self.assertNotIn("env-var.eutonos-home", ids)
        self.uninstall(user_home=None, components=[])
        self.assertEqual(self.ports.user_env["EUTONOS_HOME"], "D:/mine")

    def test_a_value_the_person_changed_later_is_kept_on_uninstall(self):
        self.real_install(components=[])
        self.ports.user_env["EUTONOS_HOME"] = "D:/changed"
        gone = self.uninstall(user_home=None, components=[])
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(self.ports.user_env["EUTONOS_HOME"], "D:/changed")
        self.assertIn("env-var.eutonos-home", {item["effect_id"] for item in gone["kept"]})

    def test_the_dry_run_plans_the_variable_and_writes_nothing(self):
        plan = self.install(user_home=None, for_this_user=True, components=[], dry_run=True)
        self.assertEqual(plan["outcome"], "dry-run", plan)
        self.assertIn("EUTONOS_HOME", json.dumps(plan))
        self.assertEqual(self.ports.user_env, {})
        self.assertFalse(self.h.exists())

    def test_the_new_permissions_are_declared_and_off_in_the_typed_route(self):
        from eutonos import permissions
        entries = {item["id"]: item for item in permissions.entries(permissions.load())}
        for name in ("user-env-home", "installed-apps-entry"):
            self.assertEqual((entries[name]["default"], entries[name]["writer"], entries[name]["required"]),
                             ("off", "installer", False), name)
        core = json.loads((ROOT / "src" / "eutonos" / "components" / "core.json").read_text(encoding="utf-8"))
        self.assertTrue({"user-env-home", "installed-apps-entry"} <= set(core["permissions"]))
        self.assertIn("[user-env-home]", self.prepared()["text"] + "[user-env-home]")  # shown in every consent text


class ProgressTests(SetupBase):
    def test_each_phase_reports_once_in_order_with_text(self):
        seen = []
        with mock.patch.object(installer, "PROGRESS", lambda phase, number, total: seen.append((phase, number, total))):
            result = self.install(user_home=self.user, components=[])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(seen, [(phase, index + 1, len(ir.PHASES)) for index, phase in enumerate(ir.PHASES)])
        self.assertTrue(set(ir.PHASES) <= set(installer.PHASE_TEXT))

    def test_the_command_line_has_the_wizards_flags_and_refuses_a_bad_component(self):
        args = installer._parser().parse_args(["--home", "H", "install", "--wheel", "w", "--for-this-user", "--consent-file", "f",
                                                "--component", "a=on", "--component", "b=off", "--progress", "--adopt"])
        self.assertEqual((args.for_this_user, args.consent_file, args.component, args.progress, args.adopt),
                         (True, "f", ["a=on", "b=off"], True, True))
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(installer.main(["--home", str(self.h), "install", "--wheel", "w", "--component", "x=maybe"]), 2)
        self.assertFalse(self.h.exists())


class WizardHelperTests(SetupBase):
    def inspect(self, **extra):
        values = dict(ports=self.ports, os_name="windows", user_home=self.user, manifests=None, probe=lambda name: True)
        values.update(extra)
        with mock.patch.object(wiz.prereq, "prereq_check", lambda **kw: {"requirements": [READY]}):
            return wiz.inspect(self.h, **values)

    def parts(self, found):
        return {row["id"]: row for row in found["parts"]}

    def inspect_without_git(self, manifests=None):
        gone = {"ok": False, "name": "git", "guidance": "Install Git for Windows"}
        with mock.patch.object(wiz.prereq, "prereq_check", lambda **kw: {"requirements": [dict(gone)]}):
            return wiz.inspect(self.h, ports=self.ports, os_name="windows", user_home=self.user, manifests=manifests,
                               probe=lambda name: True)

    def test_without_git_core_still_installs_and_says_what_it_loses(self):
        found = self.inspect_without_git()
        self.assertFalse(found["git_ok"])
        core = self.parts(found)["core"]
        self.assertTrue(core["available"])
        self.assertIn("project memory", core["note"])
        self.assertEqual(found["git_unavailable"], ["core"])
        self.assertIn("project memory", found["git_note"])
        for name, row in self.parts(found).items():  # every part that does not use Git is untouched
            if name != "core" and row.get("in_build", True):
                self.assertTrue(row["available"], name)

    def test_a_part_that_declares_git_is_unavailable_without_it_and_available_with_it(self):
        manifests = installer.load_components()
        needy = [dict(item, requires=dict(item["requires"], tools=["git"])) if item["id"] == "companion" else item
                 for item in manifests]
        found = self.inspect_without_git(needy)
        self.assertFalse(self.parts(found)["companion"]["available"])
        self.assertIn("Git", self.parts(found)["companion"]["note"])
        self.assertIn("not available", self.parts(found)["companion"]["label"])
        self.assertIn("EUTONOS companion", found["git_note"])
        self.assertTrue(self.parts(found)["crosstalk"]["available"])
        with mock.patch.object(wiz.prereq, "prereq_check", lambda **kw: {"requirements": [dict(READY)]}):
            with_git = wiz.inspect(self.h, ports=self.ports, os_name="windows", user_home=self.user, manifests=needy,
                                   probe=lambda name: True)
        self.assertTrue(self.parts(with_git)["companion"]["available"])
        self.assertEqual((with_git["git_note"], with_git["git_unavailable"]), ("", []))

    def test_the_ini_carries_the_git_note_and_the_setup_script_no_longer_refuses_without_git(self):
        found = self.inspect_without_git()
        path = self.base / "without-git.ini"
        wiz.write_ini(path, found)
        values = parse_ini(path)
        self.assertEqual(values["git_ok"], "0")
        self.assertIn("project memory", values["git_note"])
        self.assertEqual(values["git_unavailable"], "core")
        script = ISS.read_text(encoding="utf-8")
        self.assertNotIn("EUTONOS needs Git for Windows", script)
        self.assertIn("git_note", script)

    def test_without_a_launcher_remote_control_starts_on_and_core_is_first_and_required(self):
        found = self.inspect()
        self.assertEqual(found["parts"][0]["id"], "core")
        self.assertEqual(found["parts"][0]["state"], "required")
        self.assertEqual(self.parts(found)["remote-control"]["default"], "on")
        self.assertEqual(found["remote_control_launchers"], [])
        self.assertTrue(found["git_ok"])
        self.assertEqual({"core", "crosstalk", "hermes", "remote-control", "companion", "simon"}, set(self.parts(found)))
        self.assertEqual(self.parts(found)["simon"]["default"], "off")  # listed, unticked: the owner's tick is theirs
        self.assertNotIn("role-hooks", self.parts(found))  # held
        self.assertNotIn("atlas-dummy", self.parts(found))  # tests only

    def test_a_remote_control_launcher_the_person_already_has_turns_the_part_off_and_says_why_and_is_not_touched(self):
        lnk = self.startup / "Claude Remote Control.lnk"
        self.ports.shortcut_write(lnk, "wscript.exe", '"C:/Users/x/claude-remote-control/start-hidden.vbs"', "C:/x", "mine")
        before_bytes, before_map = lnk.read_bytes(), json.loads(json.dumps(self.ports.shortcuts))
        found = self.inspect()
        row = self.parts(found)["remote-control"]
        self.assertEqual(row["default"], "off")
        self.assertIn("you already have one at login", row["note"])
        self.assertIn("Claude Remote Control.lnk", row["note"])
        self.assertEqual([item["name"] for item in found["remote_control_launchers"]], ["Claude Remote Control.lnk"])
        self.assertFalse(found["adopt"])  # a personal launcher is not an earlier EUTONOS install
        self.assertEqual(lnk.read_bytes(), before_bytes)
        self.assertEqual(self.ports.shortcuts, before_map)
        self.assertEqual(sorted(item.name for item in self.startup.iterdir()), ["Claude Remote Control.lnk"])
        self.assertEqual(self.parts(found)["crosstalk"]["default"], "on")  # nothing else changes

    def test_a_launcher_is_found_by_its_target_too_and_the_components_own_shortcut_is_not_one(self):
        self.ports.shortcut_write(self.startup / "Start Claude.lnk", "wscript.exe", "C:/x/RemoteControl-start.vbs", "C:/x", "")
        self.ports.shortcut_write(self.startup / fx.REMOTE_CONTROL_LNK, "C:/eutonos/remote.exe", "", "C:/e", "")
        self.ports.shortcut_write(self.startup / "Other.lnk", "notepad.exe", "", "C:/e", "")
        names = [item["name"] for item in fx.find_remote_control_launchers(self.ports, "windows")]
        self.assertEqual(names, ["Start Claude.lnk"])
        self.assertEqual(fx.find_remote_control_launchers(self.ports, "macos"), [])

    def test_an_app_that_is_not_here_makes_its_part_unavailable_with_a_reason(self):
        found = self.inspect(probe=lambda name: name != "hermes")
        rows = self.parts(found)
        self.assertFalse(rows["hermes"]["available"])
        self.assertIn("hermes", rows["hermes"]["note"])
        self.assertTrue(rows["crosstalk"]["available"])

    def test_an_earlier_hand_install_is_reported_for_adopt(self):
        self.ports.shortcut_write(self.startup / fx.HELPER_SHORTCUT, "C:/old/python.exe", "helper.py", "C:/old", "legacy")
        found = self.inspect()
        self.assertTrue(found["adopt"])
        self.assertEqual(found["adopt_found"], ["startup_shortcut"])
        self.assertFalse(self.inspect(user_home=self.base / "nobody")["adopt"] and False)

    def test_the_ini_lines_are_plain_ascii_key_value_pairs_the_wizard_can_read(self):
        self.ports.shortcut_write(self.startup / "Fern\u00e9 Remote Control.lnk", "wscript.exe", "x.vbs", "C:/x", "")
        path = self.base / "inspect.ini"
        wiz.write_ini(path, self.inspect())
        values = parse_ini(path)
        self.assertEqual(Path(path).read_text(encoding="ascii").splitlines()[0], "[setup]")
        parts = values["parts"].split(",")
        self.assertEqual(parts[0], "core")
        for name in parts:
            for suffix in ("name", "label", "default", "available", "note"):
                self.assertIn(f"{name}_{suffix}", values)
        self.assertEqual((values["rc_launcher"], values["remote-control_default"]), ("1", "off"))
        self.assertIn("already have one at login", values["remote-control_note"])
        self.assertTrue(values["rc_launcher_name"].endswith(".lnk"))
        self.assertEqual((values["git_ok"], values["pending"], values["existing_install"]), ("1", "0", "0"))
        self.assertEqual(values["solo_note"], wiz.SOLO_NOTE)
        self.assertEqual(values["shared_login_note"], wiz.fs.SHARED_LOGIN_NOTE)
        self.assertEqual(values["core_label"], "Core (always installed and on)" if values["core_name"] == "Core"
                         else values["core_name"] + " (always installed and on)")
        self.assertEqual(values["crosstalk_label"], values["crosstalk_name"] + " (installed; tick to switch on now)")

    def test_the_consent_text_lists_the_change_list_and_the_template_is_not_yet_accepted(self):
        template = self.prepared(choices={"crosstalk": "off"}, adopt=True)
        text = template["text"]
        self.assertIn(f"Install EUTONOS into {self.h}", text)
        self.assertIn("Installed apps", text)
        self.assertIn("earlier hand install", text)
        self.assertIn("EUTONOS_HOME", text)
        self.assertTrue(text.rstrip().endswith("Tick I agree to continue, or go back to change your choices."))
        self.assertFalse(template["accepted"])
        self.assertEqual(template["surface"], consent.SETUP_SURFACE)
        self.assertEqual(template["text_sha256"], consent.text_sha256(text))
        self.assertTrue(all(line.startswith("- ") for line in template["lines"]))

    def test_accepting_stamps_the_time_and_changes_nothing_else(self):
        template = self.prepared()
        done = wiz.accept_template(template)
        self.assertTrue(done["accepted"])
        self.assertRegex(done["accepted_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual({k: v for k, v in done.items() if k not in ("accepted", "accepted_at")},
                         {k: v for k, v in template.items() if k != "accepted"})
        self.assertFalse(template["accepted"])  # the template itself is untouched

    def test_the_parts_line_says_switched_on_or_off_and_that_every_part_is_installed(self):
        comp, _ = it.fake_component(self, "fakecomp")
        lines = {}
        for choice in ("on", "off"):
            shown = wiz.prepare_consent(self.h, choices={"fakecomp": choice}, for_this_user=True, adopt=False,
                                        python_dir=self.h / "python", installed_apps=True, os_name="windows", manifests=[comp])
            (lines[choice],) = [line for line in shown["lines"] if line.startswith("- Parts:")]
            self.assertIn(lines[choice], shown["text"])
        self.assertIn("fakecomp (switched on)", lines["on"])
        self.assertIn("fakecomp (switched off)", lines["off"])
        for line in lines.values():
            self.assertIn("Every part this computer can run is installed.", line)
            self.assertIn("adds nothing outside the EUTONOS folder until you switch it on later", line)
            self.assertNotIn("(on)", line)
            self.assertNotIn("(off)", line)

    def test_the_parts_captions_say_installed_and_tick_to_switch_on_and_unavailable_parts_say_skipped(self):
        found = self.inspect(probe=lambda name: name != "hermes")
        rows = self.parts(found)
        self.assertEqual(rows["core"]["label"], rows["core"]["name"] + " (always installed and on)")
        self.assertEqual(rows["crosstalk"]["label"], rows["crosstalk"]["name"] + " (installed; tick to switch on now)")
        self.assertEqual(rows["hermes"]["label"], rows["hermes"]["name"] + " (not available on this PC, so skipped)")

    def test_a_part_whose_bundle_is_not_prepared_in_this_build_says_so_instead_of_installed(self):
        with mock.patch.dict(wiz.PREPARED_CHECKS, {"simon": lambda: "no wheels"}):
            rows = self.parts(self.inspect())
        self.assertEqual(rows["simon"]["label"], rows["simon"]["name"] + " (not in this build)")
        self.assertFalse(rows["simon"]["available"])
        self.assertEqual(rows["crosstalk"]["label"], rows["crosstalk"]["name"] + " (installed; tick to switch on now)")
        with mock.patch.dict(wiz.PREPARED_CHECKS, {"simon": lambda: None}):
            rows = self.parts(self.inspect())
        self.assertEqual(rows["simon"]["label"], rows["simon"]["name"] + " (installed; tick to switch on now)")

    def test_the_command_line_writes_the_files_the_wizard_reads(self):
        out = self.base / "out"
        out.mkdir()
        argv = ["--user-home", str(self.user)]
        with mock.patch("sys.stdout", new_callable=io.StringIO) as shown:
            self.assertEqual(wiz.main(argv + ["consent-prepare", "--home", str(self.h), "--component", "crosstalk=on",
                                              "--python-dir", str(self.h / "python"), "--installed-apps",
                                              "--note", "A plain note.", "--text-out", str(out / "t.txt"),
                                              "--lines-out", str(out / "l.txt"), "--template-out", str(out / "t.json")]), 0)
            self.assertEqual(wiz.main(argv + ["consent-accept", "--template", str(out / "t.json"),
                                              "--out", str(out / "a.json")]), 0)
        self.assertEqual(json.loads(shown.getvalue().splitlines()[0])["outcome"], "ok")
        raw = (out / "t.txt").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))  # UTF-8 with a mark, so the wizard's memo reads it right
        self.assertIn(b"\r\n", raw)
        self.assertIn("A plain note.", (out / "l.txt").read_text(encoding="utf-8-sig"))
        accepted = json.loads((out / "a.json").read_text(encoding="utf-8"))
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["text"].replace("\r\n", "\n"), (out / "t.txt").read_text(encoding="utf-8-sig").replace("\r\n", "\n"))

    def test_a_file_the_command_line_made_is_accepted_by_the_engine(self):
        out = self.base / "out2"
        out.mkdir()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            wiz.main(["--user-home", str(self.user), "consent-prepare", "--home", str(self.h), "--text-out", str(out / "t.txt"),
                      "--template-out", str(out / "t.json")])
            wiz.main(["consent-accept", "--template", str(out / "t.json"), "--out", str(out / "a.json")])
        block = consent.from_file(out / "a.json", None, self.h, os_name=wiz.iports.default_os_name(),
                                  setup=installer.setup_base(
                                      installer.Run(self.h, kind="install", ports=self.ports, user_home=self.user,
                                                    os_name=wiz.iports.default_os_name(), manifests=None, for_this_user=True),
                                      {}, False), user_home=self.user)
        self.assertEqual(block["surface"], "setup-wizard")


def mini_wheel(folder: Path, *, installed_text="from the wheel") -> Path:
    path = folder / "eutonos-9.9.9-py3-none-any.whl"
    stubs = {"eutonos/__init__.py": "",
             "eutonos/setup_wizard.py": f"import sys\ndef main(argv):\n    print('wizard', {installed_text!r}, *argv)\n    return 0\n",
             "eutonos/installer.py": f"import sys\ndef main(argv):\n    print('engine', {installed_text!r}, *argv)\n    return 3\n"}
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in stubs.items():
            archive.writestr(name, text)
    return path


class OpenDashboardTests(unittest.TestCase):
    """The finish page's "Open dashboard" button: what its helper starts, and when. Every effect is a recording
    fake, so nothing here starts a real dashboard, a process or a browser."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.h = Path(self._tmp.name) / "home"
        (self.h / "bin").mkdir(parents=True)
        self.launcher = self.h / "bin" / "eutonos-dashboard.exe"
        self.launcher.write_bytes(b"x")
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self.log = []
        self.up = False
        self.url = "http://127.0.0.1:8765"

    def registry(self, *roots):
        (self.h / "projects.json").write_text(json.dumps(
            {"profile": "eutonos.projects.v1", "projects": [{"root": str(root)} for root in roots]}), encoding="utf-8")

    def spawn(self, argv):
        self.log.append(("spawn", argv))
        self.up = True  # the dashboard answers once it was started

    def run_it(self, **extra):
        values = dict(os_name="windows", listening=lambda: self.up, spawn=self.spawn,
                      browser=lambda url: self.log.append(("browser", url)), sleep=lambda seconds: None)
        values.update(extra)
        return wiz.open_dashboard(self.h, **values)

    def test_a_stopped_dashboard_is_started_on_the_registered_projects_that_exist_then_opened(self):
        self.registry(self.project, self.h / "gone")
        self.assertEqual(self.run_it(), {"outcome": "opened", "url": self.url, "started": True})
        self.assertEqual(self.log, [("spawn", [str(self.launcher), "serve", "--port", "8765", "--root", str(self.project)]),
                                    ("browser", self.url)])

    def test_a_new_install_has_no_project_so_the_dashboard_is_started_on_the_home_itself(self):
        self.run_it()
        self.assertEqual(self.log[0][1][-2:], ["--root", str(self.h)])
        for damaged in ("{not json", json.dumps({"profile": "something.else", "projects": []})):
            (self.h / "projects.json").write_text(damaged, encoding="utf-8")
            self.assertEqual(wiz.dashboard_roots(self.h), [str(self.h)])

    def test_reading_the_registry_leaves_the_environment_as_it_was(self):
        self.registry(self.project)
        with mock.patch.dict(os.environ, {"EUTONOS_HOME": r"X:\kept"}):
            self.assertEqual(wiz.dashboard_roots(self.h), [str(self.project)])
            self.assertEqual(os.environ["EUTONOS_HOME"], r"X:\kept")
        with mock.patch.dict(os.environ):
            os.environ.pop("EUTONOS_HOME", None)
            wiz.dashboard_roots(self.h)
            self.assertNotIn("EUTONOS_HOME", os.environ)

    def test_a_dashboard_that_is_already_running_is_only_opened(self):
        self.up = True
        self.launcher.unlink()  # not needed, so not looked for
        self.assertEqual(self.run_it(), {"outcome": "opened", "url": self.url, "started": False})
        self.assertEqual(self.log, [("browser", self.url)])

    def test_a_missing_launcher_starts_and_opens_nothing_and_says_which_file(self):
        self.launcher.unlink()
        result = self.run_it()
        self.assertEqual(result["outcome"], "error")
        self.assertIn("launcher is missing", result["error"])
        self.assertIn("eutonos-dashboard.exe", result["error"])
        self.assertEqual(self.log, [])

    def test_a_dashboard_that_never_answers_gives_up_after_the_wait_without_opening_the_browser(self):
        ticks = iter(range(0, 1000, 10))
        slept = []
        result = self.run_it(spawn=lambda argv: self.log.append(("spawn", argv)), wait=30,
                             clock=lambda: next(ticks), sleep=slept.append)
        self.assertEqual(result["outcome"], "error")
        self.assertIn("did not answer within 30 seconds", result["error"])
        self.assertEqual(slept, [wiz.DASHBOARD_PROBE_SECONDS] * 2)
        self.assertEqual([entry[0] for entry in self.log], ["spawn"])

    def test_a_launcher_that_cannot_start_is_an_error_and_the_browser_stays_closed(self):
        def broken(argv):
            raise OSError("access denied")
        result = self.run_it(spawn=broken)
        self.assertEqual(result["outcome"], "error")
        self.assertIn("access denied", result["error"])
        self.assertEqual(self.log, [])

    def test_the_helper_uses_the_dashboards_own_default_port(self):
        self.assertEqual(dashboard_cli.parser().parse_args([]).port, wiz.DASHBOARD_PORT)

    def test_the_dashboard_is_started_hidden_and_on_its_own_with_no_window_and_no_input(self):
        with mock.patch.object(wiz.subprocess, "Popen") as popen:
            wiz._spawn(["x.exe", "serve"])
        (argv,), kwargs = popen.call_args
        self.assertEqual(argv, ["x.exe", "serve"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[stream], wiz.subprocess.DEVNULL)
        hidden = getattr(wiz.subprocess, "CREATE_NO_WINDOW", 0)
        self.assertEqual(kwargs["creationflags"] & hidden, hidden)

    def test_the_command_line_reports_the_outcome_and_a_failure_is_also_said_in_a_message(self):
        opened = {"outcome": "opened", "url": self.url, "started": False}
        failed = {"outcome": "error", "error": "the dashboard did not answer within 60 seconds"}
        for result, code in ((opened, 0), (failed, 1)):
            with mock.patch.object(wiz, "open_dashboard", return_value=result) as called, \
                    mock.patch.object(wiz, "_tell") as told, mock.patch("sys.stdout", new_callable=io.StringIO) as shown:
                self.assertEqual(wiz.main(["open-dashboard", "--home", str(self.h)]), code)
            called.assert_called_once_with(str(self.h))
            self.assertEqual(json.loads(shown.getvalue()), result)
            if code:
                self.assertIn(result["error"], told.call_args.args[0])
            else:
                told.assert_not_called()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.wheel = mini_wheel(self.base)

    def run_bootstrap(self, *argv):
        env = {name: value for name, value in os.environ.items() if not name.startswith("PYTHON")}
        env["TEMP"] = env["TMP"] = str(self.base)
        return subprocess.run([sys.executable, "-I", "-B", str(BOOTSTRAP), *map(str, argv)], capture_output=True, text=True,
                              env=env, cwd=str(self.base), timeout=120)

    def leftovers(self):
        return sorted(item.name for item in self.base.iterdir() if item.name.startswith(("eutonos-setup-", "eutonos-uninstall-")))

    def test_it_runs_the_wizard_helpers_and_the_engine_from_the_wheel_and_passes_the_exit_code(self):
        wizard = self.run_bootstrap(self.wheel, "wizard", "inspect", "--home", "H")
        self.assertEqual((wizard.returncode, wizard.stdout.strip()), (0, "wizard from the wheel inspect --home H"), wizard.stderr)
        engine = self.run_bootstrap(self.wheel, "engine", "--home", "H", "install")
        self.assertEqual((engine.returncode, engine.stdout.strip()), (3, "engine from the wheel --home H install"), engine.stderr)
        self.assertEqual(self.leftovers(), [])  # each run removed the folder it unpacked into

    def test_a_missing_or_broken_wheel_and_a_bad_command_are_refused_plainly(self):
        missing = self.run_bootstrap(self.base / "no.whl", "wizard", "x")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("missing or damaged", missing.stderr)
        (self.base / "bad.whl").write_text("not a zip", encoding="utf-8")
        self.assertEqual(self.run_bootstrap(self.base / "bad.whl", "wizard", "x").returncode, 2)
        self.assertEqual(self.run_bootstrap(self.wheel, "other").returncode, 2)
        self.assertEqual(self.run_bootstrap().returncode, 2)
        self.assertEqual(self.leftovers(), [])

    def test_a_wheel_with_a_path_that_escapes_is_refused(self):
        evil = self.base / "evil.whl"
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("../escape.txt", "x")
        done = self.run_bootstrap(evil, "wizard", "x")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((self.base.parent / "escape.txt").exists())
        self.assertEqual(self.leftovers(), [])  # a refused wheel leaves no folder behind

    def test_uninstall_prefers_the_engine_installed_in_the_home_and_falls_back_to_the_wheel(self):
        home = self.base / "home"
        gen, artifact = "gen-1", "eutonos-1.0.0-abc"
        (home / "install" / "generations" / gen).mkdir(parents=True)
        (home / "install" / "state.json").write_text(json.dumps({"active": gen}), encoding="utf-8")
        (home / "install" / "generations" / gen / "record.json").write_text(
            json.dumps({"wheel": {"artifact_id": artifact}}), encoding="utf-8")
        none = self.run_bootstrap("--installed-engine", home, self.wheel, "engine", "--home", home, "uninstall")
        self.assertIn("from the wheel", none.stdout)  # no installed engine yet
        package = home / "runtimes" / artifact / "Lib" / "site-packages" / "eutonos"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "installer.py").write_text("def main(argv):\n    print('installed engine', *argv)\n    return 0\n", encoding="utf-8")
        used = self.run_bootstrap("--installed-engine", home, self.wheel, "engine", "--home", home, "uninstall", "--keep-projects")
        self.assertEqual((used.returncode, used.stdout.strip()), (0, f"installed engine --home {home} uninstall --keep-projects"), used.stderr)
        other = self.run_bootstrap("--installed-engine", home, self.wheel, "engine", "--home", home, "install")
        self.assertIn("from the wheel", other.stdout)  # only uninstall uses the installed copy
        pending = "pending-gen"
        (home / "install" / "generations" / pending).mkdir()
        (home / "install" / "generations" / pending / "record.json").write_text(
            json.dumps({"wheel": {"artifact_id": "newer"}}), encoding="utf-8")
        (home / "install" / "state.json").write_text(json.dumps({"active": gen, "pending": {"gen_id": pending}}), encoding="utf-8")
        newer = home / "runtimes" / "newer" / "Lib" / "site-packages" / "eutonos"
        newer.mkdir(parents=True)
        (newer / "__init__.py").write_text("", encoding="utf-8")
        (newer / "installer.py").write_text("def main(argv):\n    print('pending engine')\n    return 0\n", encoding="utf-8")
        used = self.run_bootstrap("--installed-engine", home, self.wheel, "engine", "--home", home, "uninstall")
        self.assertEqual((used.returncode, used.stdout.strip()), (0, "pending engine"), used.stderr)
        self.assertEqual(self.leftovers(), [])  # neither the wheel's folder nor the installed-engine copy stays
        self.assertTrue(package.is_dir())  # and only the folders the run made were removed


def fake_python(folder: Path, *, name="Python314") -> Path:
    """A tree shaped like a python.org install, with files to keep and files to leave out."""
    root = Path(folder) / name
    keep = ["python.exe", "pythonw.exe", "python3.dll", "python314.dll", "vcruntime140.dll", "LICENSE.txt", "DLLs/_tkinter.pyd",
            "Lib/os.py", "Lib/ensurepip/__init__.py", "Lib/ensurepip/_bundled/pip.whl", "Lib/venv/__init__.py",
            "Lib/tkinter/__init__.py", "Lib/json/decoder.py", "tcl/tcl8.6/init.tcl"]
    drop = ["Lib/test/test_x.py", "Lib/unittest/test/test_y.py", "Lib/json/tests/test_z.py", "Lib/idlelib/idle.py",
            "Lib/turtledemo/demo.py", "Lib/site-packages/pip/__init__.py", "Lib/__pycache__/os.cpython-314.pyc",
            "Lib/json/__pycache__/decoder.cpython-314.pyc", "Lib/tkinter/idle_test/t.py", "Lib/stray.pyc",
            "Doc/python.chm", "include/Python.h", "libs/python314.lib", "Scripts/pip.exe", "Tools/x.py", "NEWS.txt"]
    for relative in keep + drop:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    return root


GOOD = {"version": [3, 14, 0], "bits": 64, "tkinter": True, "prefix": "C:/P", "base_prefix": "C:/P"}


class BuildSetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.py = fake_python(self.base)

    def refused(self, *, probe=None, folder=None, **expect):
        with self.assertRaises(build_setup.BuildError) as caught:
            build_setup.check_python(folder or self.py, lambda exe: dict(GOOD, **(probe or {})))
        return str(caught.exception)

    def test_a_python_org_install_of_3_12_or_newer_with_tkinter_is_accepted(self):
        found = build_setup.check_python(self.py, lambda exe: dict(GOOD))
        self.assertEqual(found["version"], [3, 14, 0])
        self.assertEqual(build_setup.check_python(self.py, lambda exe: dict(GOOD, version=[3, 12, 0]))["bits"], 64)

    def test_each_wrong_python_is_refused_with_its_reason(self):
        self.assertIn("too old", self.refused(probe={"version": [3, 11, 9]}))
        self.assertIn("32-bit", self.refused(probe={"bits": 32}))
        self.assertIn("tkinter", self.refused(probe={"tkinter": False}))
        self.assertIn("virtual environment", self.refused(probe={"prefix": "C:/venv", "base_prefix": "C:/P"}))
        (self.py / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        self.assertIn("virtual environment", self.refused())
        (self.py / "pyvenv.cfg").unlink()
        (self.py / "python314._pth").write_text("python314.zip\n", encoding="utf-8")
        self.assertIn("embeddable", self.refused())
        (self.py / "python314._pth").unlink()
        store = fake_python(self.base / "WindowsApps", name="PythonSoftwareFoundation.Python.3.14")
        self.assertIn("Store", self.refused(folder=store))
        self.assertIn("no python.exe", self.refused(folder=self.base / "nothing"))
        (self.py / "tcl" / "tcl8.6" / "init.tcl").unlink()
        shutil.rmtree(self.py / "tcl")
        self.assertIn("not a python.org layout", self.refused())

    def test_a_python_without_venv_or_ensurepip_is_refused(self):
        shutil.rmtree(self.py / "Lib" / "ensurepip")
        self.assertIn("ensurepip", self.refused())

    def test_staging_leaves_out_tests_docs_site_packages_and_bytecode_and_keeps_the_rest(self):
        target = self.base / "stage" / "python"
        counts = build_setup.stage_python(self.py, target)
        have = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
        self.assertEqual(counts["files"], len(have))
        for kept in ("python.exe", "pythonw.exe", "python314.dll", "vcruntime140.dll", "LICENSE.txt", "DLLs/_tkinter.pyd",
                     "Lib/os.py", "Lib/ensurepip/__init__.py", "Lib/ensurepip/_bundled/pip.whl", "Lib/venv/__init__.py",
                     "Lib/tkinter/__init__.py", "Lib/json/decoder.py", "tcl/tcl8.6/init.tcl"):
            self.assertIn(kept, have)
        for dropped in ("Lib/test/test_x.py", "Lib/unittest/test/test_y.py", "Lib/json/tests/test_z.py", "Lib/idlelib/idle.py",
                        "Lib/turtledemo/demo.py", "Lib/site-packages/pip/__init__.py", "Lib/__pycache__/os.cpython-314.pyc",
                        "Lib/json/__pycache__/decoder.cpython-314.pyc", "Lib/tkinter/idle_test/t.py", "Lib/stray.pyc",
                        "Doc/python.chm", "include/Python.h", "libs/python314.lib", "Scripts/pip.exe", "Tools/x.py", "NEWS.txt"):
            self.assertNotIn(dropped, have)
        self.assertEqual(list((target / "Lib" / "site-packages").iterdir()), [])  # an empty folder is kept
        for folder in ("Doc", "include", "libs", "Scripts", "Tools"):
            self.assertFalse((target / folder).exists(), folder)
        self.assertFalse((target / "Lib" / "test").exists())
        self.assertTrue(self.py.joinpath("Lib", "test", "test_x.py").is_file())  # the source is not touched

    def test_staging_replaces_an_earlier_stage_and_never_downloads(self):
        target = self.base / "stage"
        (target).mkdir()
        (target / "old.txt").write_text("old", encoding="utf-8")
        build_setup.stage_python(self.py, target)
        self.assertFalse((target / "old.txt").exists())
        body = (ROOT / "tools" / "build_setup.py").read_text(encoding="utf-8").split('"""', 2)[2]
        for word in ("urllib", "urlopen", "requests", "pip install", "curl"):
            self.assertNotIn(word, body)

    def test_the_command_line_refuses_a_bad_python_before_building_anything(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = build_setup.main(["--python", str(self.base / "nothing"), "--no-compile", "--root", str(self.base)])
        self.assertEqual(code, 1)
        self.assertIn("no python.exe", err.getvalue())
        self.assertFalse((self.base / "build").exists())

    def test_the_staging_folder_can_live_outside_the_repository_and_the_repository_gets_no_build_folder(self):
        root = self.base / "repo"
        (root / "installer" / "windows").mkdir(parents=True)
        (root / "installer" / "windows" / "bootstrap.py").write_text("print('boot')\n", encoding="utf-8")
        work = self.base / "work"

        def fake_wheel(repo, output):
            wheel = Path(output) / "eutonos-9.9.9-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            return wheel

        with mock.patch.object(build_wheel, "build", fake_wheel):
            result = build_setup.build(root, python=self.py, probe=lambda exe: dict(GOOD), compile_setup=False, work=work,
                                       offers_check=lambda checked: None)  # this fixture has no catalogue documents
        stage = (work / "stage").resolve()
        self.assertEqual(Path(result["stage"]), stage)
        for kept in ("bootstrap.py", "wheel/eutonos-9.9.9-py3-none-any.whl", "python/python.exe"):
            self.assertTrue((stage / kept).is_file(), kept)
        self.assertFalse((root / "build").exists())

    def offers_repo(self, *, stale):
        """A fixture repository whose catalogue documents and offers file agree, or (stale) no longer agree."""
        root = self.base / "repo"
        (root / "installer" / "windows").mkdir(parents=True)
        (root / "installer" / "windows" / "bootstrap.py").write_text("print('boot')\n", encoding="utf-8")
        offers_fixture.make_root(root, current_feature=[offers_fixture.item("f-one", "feature", "completed")])
        build_offers.write(root)
        if stale:
            offers_fixture.write_owner(root, offers_fixture.OWNERS[("current", "feature")], [
                offers_fixture.item("f-one", "feature", "completed"), offers_fixture.item("f-added", "feature", "completed")])
        return root

    def test_a_stale_offers_file_stops_the_build_before_anything_is_staged_and_names_the_fix(self):
        root, work, built = self.offers_repo(stale=True), self.base / "work", mock.Mock()
        with mock.patch.object(build_wheel, "build", built):
            with self.assertRaises(build_setup.BuildError) as caught:
                build_setup.build(root, python=self.py, probe=lambda exe: dict(GOOD), compile_setup=False, work=work)
        self.assertIn("python tools/build_offers.py", str(caught.exception))
        self.assertIn("f-added", str(caught.exception))
        built.assert_not_called()
        self.assertFalse((work / "stage").exists())

    def test_the_command_line_refuses_a_stale_offers_file_with_the_same_words(self):
        root = self.offers_repo(stale=True)
        with mock.patch.object(build_setup, "_probe", lambda exe: dict(GOOD)), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = build_setup.main(["--python", str(self.py), "--no-compile", "--root", str(root), "--work", str(self.base / "work")])
        self.assertEqual(code, 1)
        self.assertIn("python tools/build_offers.py", err.getvalue())
        self.assertFalse((self.base / "work" / "stage").exists())

    def test_a_current_offers_file_lets_the_build_go_on(self):
        root, work = self.offers_repo(stale=False), self.base / "work"

        def fake_wheel(repo, output):
            wheel = Path(output) / "eutonos-9.9.9-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            return wheel

        with mock.patch.object(build_wheel, "build", fake_wheel):
            result = build_setup.build(root, python=self.py, probe=lambda exe: dict(GOOD), compile_setup=False, work=work)
        self.assertTrue((Path(result["stage"]) / "wheel" / "eutonos-9.9.9-py3-none-any.whl").is_file())

    def test_a_bad_python_is_still_refused_first(self):
        root = self.offers_repo(stale=True)
        with self.assertRaisesRegex(build_setup.BuildError, "no python.exe"):
            build_setup.build(root, python=self.base / "nothing", compile_setup=False, work=self.base / "work")

    def test_the_compiler_is_found_from_an_explicit_path_only_when_it_exists(self):
        fake = self.base / "ISCC.exe"
        self.assertIsNone(build_setup.find_iscc(str(fake)))
        fake.write_bytes(b"x")
        self.assertEqual(build_setup.find_iscc(str(fake)), fake)

    @unittest.skipUnless(sys.platform == "win32" and Path(build_setup.DEFAULT_PYTHON, "python.exe").is_file(),
                         "needs the python.org Python at the default location")
    def test_the_real_python_passes_the_checks_when_present(self):
        found = build_setup.check_python(build_setup.DEFAULT_PYTHON)
        self.assertGreaterEqual(tuple(found["version"]), build_setup.MIN_PYTHON)


@unittest.skipUnless(build_setup.find_iscc(), "Inno Setup (ISCC) is not installed")
class CompileTests(unittest.TestCase):
    """The script must compile; its wizard pages can only be checked by running Setup (the owner's trial)."""

    def test_the_inno_script_compiles_against_a_small_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stage = tmp / "stage"
            python = fake_python(tmp, name="py")
            build_setup.stage_python(python, stage / "python")
            (stage / "wheel").mkdir()
            wheel = it.build_wheel(stage / "wheel", version="9.9.9")
            shutil.copy2(BOOTSTRAP, stage / "bootstrap.py")
            out = tmp / "out"
            done = subprocess.run([str(build_setup.find_iscc()), "/DAppVersion=9.9.9", f"/DStageDir={stage}", f"/DOutputDir={out}",
                                   "/DOutputBaseFilename=EUTONOS-Setup-test", str(ISS)], capture_output=True, text=True, timeout=600)
            self.assertEqual(done.returncode, 0, done.stdout[-1500:] + done.stderr[-500:])
            setup = out / "EUTONOS-Setup-test.exe"
            self.assertTrue(setup.is_file())
            self.assertGreater(setup.stat().st_size, 1_000_000)
            self.assertTrue(wheel.is_file())

    def test_the_same_stage_gives_the_same_setup_whatever_time_its_files_were_written(self):
        """Compiled twice, with every staged file's modification time changed in between; the result is never run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stage = tmp / "stage"
            python = fake_python(tmp, name="py")
            build_setup.stage_python(python, stage / "python")
            (stage / "wheel").mkdir()
            it.build_wheel(stage / "wheel", version="9.9.9")
            shutil.copy2(BOOTSTRAP, stage / "bootstrap.py")
            digests = []
            for number, stamp in enumerate((1_000_000_000, 1_700_000_000)):
                for path in stage.rglob("*"):
                    if path.is_file():
                        os.utime(path, (stamp, stamp))
                out = tmp / f"out{number}"
                done = subprocess.run([str(build_setup.find_iscc()), "/DAppVersion=9.9.9", f"/DStageDir={stage}",
                                       f"/DOutputDir={out}", "/DOutputBaseFilename=EUTONOS-Setup-test", str(ISS)],
                                      capture_output=True, text=True, timeout=600)
                self.assertEqual(done.returncode, 0, done.stdout[-1500:] + done.stderr[-500:])
                digests.append(build_setup.sha256_file(out / "EUTONOS-Setup-test.exe"))
            self.assertEqual(digests[0], digests[1])


class ScriptContentTests(unittest.TestCase):
    """The Inno script's promises that can be read off the text."""

    def setUp(self):
        self.text = ISS.read_text(encoding="utf-8")

    def test_every_part_setup_offers_has_a_description_on_the_parts_page(self):
        body = self.text.split("function PartDescription", 1)[1].split("\nend;", 1)[0]
        described = set(re.findall(r"Id = '([^']+)'", body))
        offered = {row["id"] for row in installer.setup_parts(installer.load_components())}
        self.assertEqual(described, offered)
        for part in described:  # none is blank
            self.assertRegex(body, r"Id = '" + re.escape(part) + r"' then\s+Result := '[^']+'")

    def test_it_installs_per_user_with_the_recommended_folder_and_no_silent_route(self):
        self.assertIn("PrivilegesRequired=lowest", self.text)
        self.assertIn(r"DefaultDirName={%USERPROFILE}\.eutonos", self.text)
        self.assertIn("WizardSilent", self.text)
        self.assertIn("Recommended location", self.text)
        self.assertIn("I agree", self.text)

    def test_the_engine_is_run_with_the_wizards_flags_and_without_pythonpath(self):
        for flag in ("--for-this-user", "--consent-file", "--progress", "--component", "--adopt", "--installed-engine",
                     "--keep-projects", "-I -B"):
            self.assertIn(flag, self.text)
        self.assertNotIn("PYTHONPATH", self.text)

    def test_uninstall_stops_without_deleting_when_the_engine_fails(self):
        body = self.text.split("procedure CurUninstallStepChanged", 1)[1]
        self.assertIn("usUninstall", body)
        self.assertGreater(body.count("Abort;"), 1)
        self.assertIn("nothing was deleted", body)
        self.assertNotIn("DelTree", self.text)
        self.assertNotIn("DeleteFile(Home", self.text)

    def test_the_script_never_writes_the_registry_the_startup_folder_or_the_users_files(self):
        for word in ("RegWriteStringValue", "RegDeleteKeyIncludingSubkeys", "[Registry]", "[Icons]", "{userstartup}", "{commonstartup}",
                     ".claude", ".codex"):
            self.assertNotIn(word, self.text, word)

    def test_every_installed_file_stores_no_time_stamp_so_a_build_is_reproducible(self):
        sources = [line for line in self.text.splitlines() if line.startswith("Source:")]
        self.assertEqual(len(sources), 3)
        for line in sources:
            self.assertIn("notimestamp", line)

    def test_the_finish_page_button_runs_the_helper_from_the_installed_files_on_a_click_only(self):
        body = self.text.split("procedure OpenDashboardClick", 1)[1].split("function GoodHome", 1)[0]
        self.assertIn("wizard open-dashboard --home", body)
        self.assertIn(r"ChosenHome + '\python\python.exe'", body)  # the installed copy; the temporary one is deleted at exit
        self.assertNotIn("TempPython", body)
        self.assertIn("ewNoWait", body)
        self.assertIn("DashButton.OnClick := @OpenDashboardClick;", self.text)
        self.assertIn("DashButton.Visible := (CurPageID = wpFinished) and (not Failed) and (not RepairMode);", self.text)
        self.assertEqual(self.text.count("open-dashboard"), 1)  # started nowhere else
        run_section = self.text.split("[Run]", 1)[1].split("[Code]", 1)[0]
        self.assertNotIn("dashboard", run_section.lower())  # never started by Setup on its own

    def test_the_wizard_says_what_eutonos_is_and_names_the_remote_control_note_source(self):
        for phrase in ("shared memory", "Crosstalk", "task boards", "Claude, Codex and Hermes"):
            self.assertIn(phrase, self.text)

    def test_choosing_a_different_home_resets_the_explicit_repair_choice(self):
        body = self.text.split("function NextButtonClick", 1)[1].split("procedure CurPageChanged", 1)[0]
        self.assertLess(body.index("RepairMode := False;"), body.index("Inspect(ChosenHome)"))
        self.assertIn("RepairMode := MsgBox", body)

    def test_the_parts_page_says_every_part_is_installed_and_the_tick_switches_it_on(self):
        self.assertIn("'Choose what to switch on'", self.text)
        self.assertNotIn("Choose what to install", self.text)
        self.assertIn("Setup installs every part this PC can run. Tick the ones to switch on now", self.text)
        self.assertIn("IniGet(Id + '_label', IniGet(Id + '_name', Id))", self.text)  # the helper words each caption
        self.assertNotIn("Tick the extra parts you want", self.text)

    def test_only_i_use_this_login_is_unticked_by_default_and_its_words_come_from_the_inspection_file(self):
        self.assertIn("SoloBox.Caption := 'Only I use this Windows login';", self.text)
        self.assertIn("SoloBox.Checked := False;", self.text)
        self.assertNotIn("SoloBox.Checked := True", self.text)
        self.assertIn("IniGet('shared_login_note', '')", self.text)  # the shared-login note sits beside the box
        self.assertIn("IniGet('solo_note', '')", self.text)
        self.assertNotIn(wiz.SOLO_NOTE[:40], self.text)  # one copy of the wording, in setup_wizard
        self.assertNotIn(wiz.fs.SHARED_LOGIN_NOTE[:40], self.text)

    def test_the_confirm_logins_page_is_after_the_install_and_only_for_a_good_ticked_install(self):
        self.assertIn("LinkedPage := CreateCustomPage(wpInstalling,", self.text)
        self.assertIn("Result := RepairMode or Failed or (not SoloBox.Checked);", self.text)
        self.assertEqual(self.text.count("suggest-accounts"), 1)  # started from one place
        self.assertEqual(self.text.count("apply-settings"), 2)  # the logins (ApplyLinked) and the update choice (ApplyUpdateMode)
        body = self.text.split("procedure ApplyLinked", 1)[1].split("function UpdateModeWord", 1)[0]
        self.assertIn("RunWizardTool('apply-settings", body)
        self.assertNotIn("Failed := True", body)  # a settings problem never fails the install
        self.assertIn("LinkedMemo.Text", body)
        self.assertIn("ApplyLinked;", self.text.split("function NextButtonClick", 1)[1].split("procedure CurPageChanged", 1)[0])

    def test_notes_go_to_the_consent_text_only_the_engines_install_command_has_no_note_option(self):
        shared = self.text.split("function SharedArgs", 1)[1].split("{ Extra plain lines", 1)[0]
        self.assertNotIn("--note", shared)
        prepare = self.text.split("function PrepareConsent", 1)[1].split("procedure AgreeClick", 1)[0]
        self.assertIn("SharedArgs + NoteArgs", prepare)
        install = [line for line in self.text.splitlines() if "--consent-file" in line]
        self.assertEqual(len(install), 1)
        self.assertIn("SharedArgs + ' --progress'", install[0])
        self.assertNotIn("NoteArgs", install[0])

    def test_the_codex_support_file_repair_behaviour_is_kept(self):
        skip = self.text.split("function ShouldSkipPage", 1)[1].split("function InstallWorked", 1)[0]
        self.assertIn("RepairMode and ((PageID = PartsPage.ID) or (PageID = SoloPage.ID) or (PageID = ConsentPage.ID))", skip)
        prepare = self.text.split("function PrepareToInstall", 1)[1].split("procedure CurStepChanged", 1)[0]
        self.assertIn("RepairMode and not RunWizardTool('repair-support", prepare)
        step = self.text.split("procedure CurStepChanged", 1)[1].split("{ Uninstall:", 1)[0]
        self.assertLess(step.index("if RepairMode then\n    Exit;"), step.index("wizard consent-accept"))  # no consent replay
        self.assertIn("function RecoveryCommands", self.text)
        for verb in ("status", "resume", "uninstall --keep-projects"):
            self.assertIn(f"Prefix + '{verb}'", self.text)
        self.assertIn("function FindWheel", self.text)
        self.assertIn("eutonos-{#AppVersion}-py3-none-any.whl", self.text)  # the exact-version wheel is tried first
        self.assertIn("--installed-engine", self.text)

    def test_the_script_keeps_its_windows_line_endings(self):
        raw = ISS.read_bytes()
        self.assertGreater(raw.count(b"\n"), 700)
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))

    def test_every_file_it_needs_is_registered_for_the_release(self):
        release = json.loads((ROOT / "src" / "eutonos" / "release.json").read_text(encoding="utf-8"))
        paths = {path for module in release["modules"] for path in module["paths"]}
        for name in ("installer/windows/EUTONOS.iss", "installer/windows/bootstrap.py", "installer/windows/README.md",
                     "tools/build_setup.py", "src/eutonos/setup_wizard.py"):
            self.assertIn(name, paths)
            self.assertTrue((ROOT / name).is_file(), name)

    def test_the_install_page_has_the_setup_exe_section(self):
        page = (ROOT / "src" / "eutonos" / "checklists" / "install.md").read_text(encoding="utf-8")
        self.assertIn("## Install with Setup.exe (Windows)", page)
        for phrase in ("SmartScreen", "More info", "Run anyway", "I agree", "already have one at login", "EUTONOS_HOME"):
            self.assertIn(phrase, page)


if __name__ == "__main__":
    unittest.main()


class SupportRepairTests(SetupBase):
    def test_explicit_repair_preflight_copies_nothing_and_leaves_install_state(self):
        self.first()
        before = (self.h / "install" / "state.json").read_bytes()
        user_before = it.snapshot(self.user)
        source = fake_python(self.base / "bundled")
        boot = self.base / "bootstrap.py"
        boot.write_bytes(b"bundled bootstrap")
        extra = self.h / "setup" / "my-notes.txt"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"my notes")
        result = wiz.repair_support(self.h, source, boot, self.wheel)
        self.assertEqual((result["outcome"], result["engine_replayed"]), ("repair-ready", False))
        self.assertFalse((self.h / "python").exists())  # only Inno owns the copy and uninstall log
        self.assertFalse((self.h / "setup" / "bootstrap.py").exists())
        self.assertEqual(extra.read_bytes(), b"my notes")
        self.assertEqual((self.h / "install" / "state.json").read_bytes(), before)
        self.assertEqual(it.snapshot(self.user), user_before)

    def test_repair_refuses_a_fresh_home(self):
        source = fake_python(self.base / "bundled")
        boot = self.base / "bootstrap.py"
        boot.write_bytes(b"bootstrap")
        with self.assertRaises(ValueError):
            wiz.repair_support(self.h, source, boot, self.wheel)
        self.assertFalse((self.h / "python").exists())


    def test_busy_repair_returns_json_error(self):
        from eutonos import setup_checkpoint as sc
        output = io.StringIO()
        with mock.patch.object(wiz, "repair_support", side_effect=sc.LifecycleBusy("another operation is running")), mock.patch("sys.stdout", output):
            code = wiz.main(["repair-support", "--home", str(self.h), "--python-dir", "fake", "--bootstrap", "fake", "--wheel", "fake"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "error")
