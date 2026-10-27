"""Switching installed parts on and off: the state file, ``eutonos-install component``, and what each part does.

Setup installs every available part; its boxes only choose which start switched on. These tests use a temporary home,
temporary user folders and fake ports and fake Claude commands. Nothing here touches the real machine.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eutonos import component_crosstalk, component_state, doctor, installer
from eutonos import install_record as ir

import test_components as tc
import test_installer as ti


# ------------------------------------------------------------------------------------- the state file
class StateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "eutonos-home"

    def test_a_missing_file_and_a_missing_key_mean_the_install_time_choice(self):
        self.assertEqual(component_state.read(self.home), {})
        self.assertTrue(component_state.component_enabled(self.home, "crosstalk"))
        self.assertFalse(component_state.component_enabled(self.home, "crosstalk", default=False))
        component_state.set_enabled(self.home, "hermes", False)
        self.assertTrue(component_state.component_enabled(self.home, "crosstalk"))
        self.assertFalse(component_state.component_enabled(self.home, "crosstalk", default=False))
        self.assertFalse(component_state.component_enabled(self.home, "hermes"))

    def test_entries_round_trip_and_keep_the_others(self):
        component_state.write_all(self.home, {"a": {"enabled": True}, "b": {"enabled": False, "detail": {"x": 1}}})
        self.assertEqual(component_state.read(self.home)["b"]["detail"], {"x": 1})
        self.assertEqual(component_state.read(self.home)["a"]["source"], "install")
        component_state.set_enabled(self.home, "a", False, detail={"plugin": "disabled"})
        found = component_state.read(self.home)
        self.assertEqual((found["a"]["enabled"], found["a"]["source"], found["a"]["detail"]),
                         (False, "toggle", {"plugin": "disabled"}))
        self.assertEqual((found["b"]["enabled"], found["b"]["source"]), (False, "install"))
        document = json.loads(component_state.path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(document["profile"], component_state.PROFILE)
        self.assertEqual(list(component_state.path(self.home).parent.glob("*.tmp")), [])

    def test_an_unreadable_file_fails_visibly_and_is_never_read_as_everything_on(self):
        target = component_state.path(self.home)
        target.parent.mkdir(parents=True)
        for text in ("{not json", "[]", json.dumps({"profile": "other", "components": {}}),
                     json.dumps({"profile": component_state.PROFILE, "components": []}),
                     json.dumps({"profile": component_state.PROFILE, "components": {"a": {"enabled": "yes"}}}),
                     json.dumps({"profile": component_state.PROFILE, "components": {"a": 3}})):
            with self.subTest(text=text):
                target.write_text(text, encoding="utf-8")
                with self.assertRaises(component_state.ComponentStateError):
                    component_state.read(self.home)
                with self.assertRaises(component_state.ComponentStateError):
                    component_state.component_enabled(self.home, "a")
                with self.assertRaises(component_state.ComponentStateError):
                    component_state.set_enabled(self.home, "a", True)
                self.assertEqual(target.read_text(encoding="utf-8"), text)  # a refused write leaves it as it was

    def test_remove_file_reports_whether_there_was_one(self):
        self.assertFalse(component_state.remove_file(self.home))
        component_state.set_enabled(self.home, "a", True)
        self.assertTrue(component_state.remove_file(self.home))
        self.assertFalse(component_state.path(self.home).exists())


# -------------------------------------------------------------------------- parts with fake components
class ToggleBase(ti.UpdateBase):
    """An installed home with fake parts. ``fakecomp`` and ``second`` are independent; ``needy`` requires ``fakecomp``."""

    def parts(self, *names, **kinds):
        manifests = []
        for name in names:
            manifest, _ = ti.fake_component(self, name, **kinds.get(name, {}))
            manifests.append(manifest)
        return manifests

    def toggle(self, component_id, enable, **extra):
        values = dict(home=self.h, user_home=self.user, os_name=self.OS, ports=self.ports)
        values.update(extra)
        return installer.set_component(component_id, enable, **values)

    def marker(self, name):
        return self.user / f"{name}.marker"

    def saved(self):
        return component_state.read(self.h)

    def record(self):
        return ir.read_record(self.layout, ir.read_state(self.layout)["active"])

    def effect_ids(self):
        return {item["effect_id"] for item in self.record()["effects"]}


class ToggleTests(ToggleBase):
    def test_disable_removes_what_the_part_applied_and_enable_puts_it_back(self):
        manifests = self.parts("fakecomp", "second")
        self.first(components=manifests)
        self.assertTrue(self.marker("fakecomp").is_file())
        self.assertIn("fakecomp.marker", self.effect_ids())
        off = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual(off["outcome"], "disabled", off)
        self.assertEqual(off["removed"], ["fakecomp.marker"])
        self.assertFalse(self.marker("fakecomp").exists())
        self.assertTrue(self.marker("second").is_file())  # the other part is untouched
        self.assertNotIn("fakecomp.marker", self.effect_ids())
        entry = self.saved()["fakecomp"]
        self.assertEqual((entry["enabled"], entry["source"]), (False, "toggle"))
        row = next(item for item in self.record()["components"] if item["id"] == "fakecomp")
        self.assertEqual((row["state"], row["enabled"]), ("installed", False))
        on = self.toggle("fakecomp", True, components=manifests)
        self.assertEqual(on["outcome"], "enabled", on)
        self.assertEqual(on["applied"], ["fakecomp.marker"])
        self.assertTrue(self.marker("fakecomp").is_file())
        self.assertIn("fakecomp.marker", self.effect_ids())
        self.assertTrue(self.saved()["fakecomp"]["enabled"])
        found = next(item for item in self.record()["effects"] if item["effect_id"] == "fakecomp.marker")
        self.assertEqual(found["by"], "component")

    def test_asking_for_the_state_a_part_already_has_changes_nothing(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        before = (self.record(), self.saved(), ti.snapshot(self.user))
        again = self.toggle("fakecomp", True, components=manifests)
        self.assertEqual((again["outcome"], again["enabled"]), ("unchanged", True))
        self.toggle("fakecomp", False, components=manifests)
        after_off = (self.record(), self.saved(), ti.snapshot(self.user))
        twice = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual((twice["outcome"], twice["enabled"]), ("unchanged", False))
        self.assertEqual((self.record(), self.saved(), ti.snapshot(self.user)), after_off)
        self.toggle("fakecomp", True, components=manifests)
        self.assertEqual(ti.snapshot(self.user), before[2])

    def test_refusals(self):
        manifests = self.parts("fakecomp", "needy", "heldcomp", "windowsonly",
                               needy={"requires": {"components": ["fakecomp"]}}, heldcomp={"default": "held"},
                               windowsonly={"requires": {"os": ["windows"]}})
        core, _ = ti.fake_component(self, "corepart")
        core["kind"] = "core"
        manifests.append(core)
        self.assertEqual(self.toggle("fakecomp", False, components=manifests)["reason"], "not-installed")  # nothing yet
        self.first(components=manifests)
        for name, enable, reason in (("nosuchpart", False, "unknown-component"), ("corepart", False, "core-always-on"),
                                     ("heldcomp", True, "held"), ("windowsonly", True, "not-installed")):
            with self.subTest(name=name):
                answer = self.toggle(name, enable, components=manifests)
                self.assertEqual((answer["outcome"], answer["reason"]), ("refused", reason), answer)
        needed = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual((needed["outcome"], needed["reason"], needed["needed_by"]), ("refused", "needed-by", ["needy"]))
        self.assertTrue(self.marker("fakecomp").is_file())
        self.assertEqual(self.toggle("needy", False, components=manifests)["outcome"], "disabled")
        self.assertEqual(self.toggle("fakecomp", False, components=manifests)["outcome"], "disabled")
        lacking = self.toggle("needy", True, components=manifests)
        self.assertEqual((lacking["reason"], lacking["needs"]), ("needs-disabled-part", ["fakecomp"]))
        self.assertFalse(self.marker("needy").exists())

    def test_an_unfinished_install_refuses_a_switch(self):
        interrupted, _ = ti.fake_component(self, "fakecomp", fail="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            self.install(components=[interrupted])
        answer = self.toggle("fakecomp", False, components=[interrupted])
        self.assertEqual((answer["outcome"], answer["reason"]), ("refused", "pending"))

    def test_an_unreadable_state_file_refuses_and_says_so(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        component_state.path(self.h).write_text("{broken", encoding="utf-8")
        answer = self.toggle("fakecomp", False, components=manifests)
        self.assertEqual((answer["outcome"], answer["reason"]), ("refused", "state-unreadable"))
        self.assertTrue(self.marker("fakecomp").is_file())
        status = installer.component_status(home=self.h, components=manifests)
        self.assertEqual((status["outcome"], status["reason"]), ("refused", "state-unreadable"))

    def test_status_lists_every_part_with_its_state(self):
        manifests = self.parts("fakecomp", "second", "heldcomp", "windowsonly", heldcomp={"default": "held"},
                               windowsonly={"requires": {"os": ["windows"]}}, second={"default": "off"})
        self.first(components=manifests)
        self.toggle("fakecomp", False, components=manifests)
        found = installer.component_status(home=self.h, components=manifests)
        by_id = {item["id"]: item for item in found["components"]}
        self.assertEqual({name: (item["state"], item["enabled"], item["toggleable"]) for name, item in by_id.items()},
                         {"fakecomp": ("installed", False, True), "second": ("installed", False, True),
                          "heldcomp": ("held", False, False), "windowsonly": ("skipped", False, False)})
        text = installer.format_components(found)
        self.assertRegex(text, r"fakecomp\s+off")
        self.assertRegex(text, r"heldcomp\s+held")

    def test_status_before_install_says_not_installed(self):
        self.assertEqual(installer.component_status(home=self.h, components=[])["outcome"], "not-installed")

    def test_a_part_unticked_at_install_is_installed_but_off_and_can_be_enabled(self):
        manifests = self.parts("fakecomp", "second")
        profile = self.base / "owner.json"
        profile.write_text(json.dumps({"profile": "eutonos.owner-profile.v1", "components": {"second": "off"}}),
                           encoding="utf-8")
        self.first(components=manifests, owner_profile=profile)
        self.assertEqual({name: item["enabled"] for name, item in self.saved().items()},
                         {"fakecomp": True, "second": False})
        self.assertEqual(self.saved()["second"]["source"], "install")
        self.assertFalse(self.marker("second").exists())
        self.assertEqual(self.toggle("second", True, components=manifests)["outcome"], "enabled")
        self.assertTrue(self.marker("second").is_file())

    def test_uninstall_restores_the_users_files_whatever_mix_of_parts_is_on(self):
        manifests = self.parts("fakecomp", "second", "third")
        for switched in ((), ("fakecomp",), ("fakecomp", "third"), ("fakecomp", "second", "third")):
            with self.subTest(off=switched):
                self.seed_user_files()
                before = ti.snapshot(self.user)
                self.first(components=manifests)
                for name in switched:
                    self.assertEqual(self.toggle(name, False, components=manifests)["outcome"], "disabled")
                gone = self.uninstall(components=manifests)
                self.assertEqual(gone["outcome"], "uninstalled", gone)
                self.assertEqual(ti.snapshot(self.user), before)
                self.assertFalse(component_state.path(self.h).exists())
                for item in list(self.user.iterdir()):  # next round starts from nothing
                    if item.is_file():
                        item.unlink()
                for name in (".claude", ".codex"):
                    for item in (self.user / name).glob("*"):
                        item.unlink()

    def test_uninstall_after_a_part_was_switched_on_again_removes_it_too(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        self.toggle("fakecomp", False, components=manifests)
        self.toggle("fakecomp", True, components=manifests)
        self.assertEqual(self.uninstall(components=manifests)["outcome"], "uninstalled")
        self.assertFalse(self.marker("fakecomp").exists())

    def test_the_change_is_journalled(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        self.toggle("fakecomp", False, components=manifests)
        events = [json.loads(line) for path in (self.h / "install").rglob("*.jsonl") for line in
                  path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "component-disable" and item.get("component") == "fakecomp"
                            for item in events), events[-5:])
        self.assertTrue(any(item.get("event") == "component" and item.get("enabled") is False for item in events))


class BlockTests(ToggleBase):
    """Instruction blocks follow their part: Crosstalk's block, and the Hermes SOUL.md memory block."""

    def blocks(self):
        return {name: (self.user / name).read_text(encoding="utf-8") if (self.user / name).is_file() else ""
                for name in (".claude/CLAUDE.md", ".codex/AGENTS.md")}

    def test_the_crosstalk_block_follows_the_part_and_the_core_blocks_stay(self):
        manifests = self.parts("crosstalk")
        self.seed_user_files()
        before = ti.snapshot(self.user)
        self.first(components=manifests)
        self.assertTrue(all("eutonos:crosstalk" in text for text in self.blocks().values()))
        self.assertEqual(self.toggle("crosstalk", False, components=manifests)["outcome"], "disabled")
        for text in self.blocks().values():
            self.assertNotIn("eutonos:crosstalk", text)
            self.assertIn("eutonos:memory", text)  # core blocks are always on
            self.assertIn("eutonos:decisions", text)
        self.assertFalse([item for item in self.effect_ids() if item.startswith("instructions.crosstalk.")])
        self.assertEqual(self.toggle("crosstalk", True, components=manifests)["outcome"], "enabled")
        self.assertTrue(all("eutonos:crosstalk" in text for text in self.blocks().values()))
        self.assertTrue([item for item in self.effect_ids() if item.startswith("instructions.crosstalk.")])
        self.toggle("crosstalk", False, components=manifests)
        self.assertEqual(self.uninstall(components=manifests)["outcome"], "uninstalled")
        self.assertEqual(ti.snapshot(self.user), before)

    def test_a_part_installed_but_off_gets_no_block_at_install(self):
        manifests = self.parts("crosstalk", crosstalk={"default": "off"})
        self.first(components=manifests)
        for text in self.blocks().values():
            self.assertNotIn("eutonos:crosstalk", text)
            self.assertIn("eutonos:memory", text)
        self.toggle("crosstalk", True, components=manifests)
        self.assertTrue(all("eutonos:crosstalk" in text for text in self.blocks().values()))

    def test_the_hermes_memory_block_follows_hermes(self):
        manifests = self.parts("hermes")
        hermes = self.base / "hermes"
        hermes.mkdir()
        (hermes / "SOUL.md").write_text("my soul\n", encoding="utf-8")
        with mock.patch.object(installer, "hermes_soul_path", lambda run, rows: hermes / "SOUL.md"):
            self.first(components=manifests)
            self.assertIn("eutonos:memory", (hermes / "SOUL.md").read_text(encoding="utf-8"))
            self.assertEqual(self.toggle("hermes", False, components=manifests)["outcome"], "disabled")
            self.assertEqual((hermes / "SOUL.md").read_text(encoding="utf-8"), "my soul\n")
            self.assertEqual(self.toggle("hermes", True, components=manifests)["outcome"], "enabled")
            self.assertIn("eutonos:memory", (hermes / "SOUL.md").read_text(encoding="utf-8"))
            self.toggle("hermes", False, components=manifests)
            self.uninstall(components=manifests)
        self.assertEqual((hermes / "SOUL.md").read_text(encoding="utf-8"), "my soul\n")


class OwnHooksTests(ToggleBase):
    """A part may decide for itself how it is switched off and on (``disable`` and ``enable`` beside ``plan``)."""

    def hooked(self, name="hooked"):
        manifest, _ = ti.fake_component(self, name)
        module = __import__("sys").modules[f"fake_{name}"]
        calls = []

        def disable(ctx, effects):
            calls.append(("disable", [item["effect_id"] for item in effects], dict(ctx.get("toggle_detail") or {})))
            return {"removed": [], "kept": [], "detail": {"how": "my-own-way"}, "notes": ["kept the marker on purpose"]}

        def enable(ctx):
            calls.append(("enable", dict(ctx.get("toggle_detail") or {})))
            return {"detail": {}}

        module.disable, module.enable = disable, enable
        return manifest, calls

    def test_its_own_hooks_are_used_and_its_detail_is_saved_for_the_way_back(self):
        manifest, calls = self.hooked()
        self.first(components=[manifest])
        off = self.toggle("hooked", False, components=[manifest])
        self.assertEqual(off["outcome"], "disabled", off)
        self.assertEqual(off["how"], {"how": "my-own-way"})
        self.assertIn("kept the marker on purpose", off["notes"])
        self.assertEqual(self.saved()["hooked"]["detail"], {"how": "my-own-way"})
        self.assertTrue(self.marker("hooked").is_file())  # its hook decided to leave it
        self.assertIn("hooked.marker", self.effect_ids())
        self.toggle("hooked", True, components=[manifest])
        self.assertEqual(calls[0][0], "disable")
        self.assertEqual(calls[1], ("enable", {"how": "my-own-way"}))
        self.assertEqual(self.saved()["hooked"]["detail"], {})

    def test_an_error_in_the_part_is_reported_and_leaves_the_state_as_it_was(self):
        manifest, _ = self.hooked()
        module = __import__("sys").modules["fake_hooked"]

        def broken(ctx, effects):
            raise RuntimeError("it broke")

        module.disable = broken
        self.first(components=[manifest])
        answer = self.toggle("hooked", False, components=[manifest])
        self.assertEqual(answer["outcome"], "failed", answer)
        self.assertIn("it broke", answer["error"])
        self.assertTrue(self.saved()["hooked"]["enabled"])


# -------------------------------------------------------------------------- update and rollback
class UpdateRollbackTests(ToggleBase):
    def test_an_update_keeps_what_the_person_switched(self):
        manifests = self.parts("fakecomp", "second")
        profile = self.base / "owner.json"
        profile.write_text(json.dumps({"profile": "eutonos.owner-profile.v1", "components": {"second": "off"}}),
                           encoding="utf-8")
        self.first(components=manifests, owner_profile=profile)
        self.toggle("fakecomp", False, components=manifests)
        self.toggle("second", True, components=manifests)
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        done = self.update(components=manifests)
        self.assertEqual(done["outcome"], "updated", done)
        self.assertFalse(self.marker("fakecomp").exists())
        self.assertTrue(self.marker("second").is_file())
        self.assertEqual({name: item["enabled"] for name, item in self.saved().items()},
                         {"fakecomp": False, "second": True})
        self.assertEqual({row["id"]: row["enabled"] for row in self.record()["components"] if row["id"] in self.saved()},
                         {"fakecomp": False, "second": True})
        self.assertEqual(self.uninstall(components=manifests)["outcome"], "uninstalled")

    def test_rollback_keeps_the_state_the_person_left(self):
        manifests = self.parts("fakecomp", "second")
        self.seed_user_files()
        before = ti.snapshot(self.user)
        self.first(components=manifests)
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual(self.update(components=manifests)["outcome"], "updated")
        self.toggle("fakecomp", False, components=manifests)
        back = self.rollback(components=manifests)
        self.assertEqual(back["outcome"], "rolled-back-to", back)
        self.assertEqual(back["components"]["disabled"], ["fakecomp"])
        self.assertFalse(self.marker("fakecomp").exists())
        self.assertTrue(self.marker("second").is_file())
        self.assertFalse(self.saved()["fakecomp"]["enabled"])
        status = installer.component_status(home=self.h, components=manifests)
        self.assertEqual({item["id"]: item["enabled"] for item in status["components"]},
                         {"fakecomp": False, "second": True})
        self.assertEqual(self.uninstall(components=manifests)["outcome"], "uninstalled")
        self.assertEqual(ti.snapshot(self.user), before)

    def test_rollback_leaves_the_same_steps_for_the_next_agent_that_a_switch_does(self):
        from eutonos import pending_actions
        manifests = self.parts("fakecomp", "second")
        self.first(components=manifests)
        ti.make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual(self.update(components=manifests)["outcome"], "updated")
        self.toggle("fakecomp", False, components=manifests)
        self.assertEqual(pending_actions.list_actions(self.h), [])  # this part needs no restart on a plain switch
        with mock.patch.dict(installer.COMPONENT_RESTARTS, {"fakecomp": ("claude", "codex")}):
            back = self.rollback(components=manifests)
            self.assertEqual(back["outcome"], "rolled-back-to", back)
            steps = pending_actions.list_actions(self.h)
            self.assertEqual(sorted((item["source"], item["harness"]) for item in steps),
                             [("component:fakecomp", "claude"), ("component:fakecomp", "codex")])
            self.assertTrue(all("switched off" in item["action"] for item in steps))
            self.assertEqual(sorted(back["components"]["pending"]), sorted(item["id"] for item in steps))
            self.assertNotIn("component:second", {item["source"] for item in steps})  # only parts it reconciled


# --------------------------------------------------------------------------------- the command line
class CliTests(ToggleBase):
    def run_cli(self, *argv, patched=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(["--home", str(self.h), *argv])
        return code, out.getvalue()

    def test_status_prints_a_short_table_or_json(self):
        manifests = self.parts("fakecomp", "second", second={"default": "off"})
        self.first(components=manifests)
        with mock.patch.object(installer, "load_components", return_value=manifests):
            code, text = self.run_cli("component", "status")
            self.assertEqual(code, 0)
            self.assertRegex(text, r"fakecomp\s+on")
            self.assertRegex(text, r"second\s+off")
            code, text = self.run_cli("component", "status", "--json")
        found = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual({item["id"]: item["enabled"] for item in found["components"]},
                         {"fakecomp": True, "second": False})

    def test_enable_and_disable_go_through_set_component_and_report_refusals(self):
        calls = []

        def fake(component_id, enable, **kwargs):
            calls.append((component_id, enable, str(kwargs["home"])))
            return {"outcome": "refused", "reason": "held"} if component_id == "held" else \
                {"outcome": "enabled" if enable else "disabled", "component": component_id}

        with mock.patch.object(installer, "set_component", fake):
            code, text = self.run_cli("component", "disable", "crosstalk")
            self.assertEqual((code, json.loads(text)["outcome"]), (0, "disabled"))
            code, _text = self.run_cli("component", "enable", "crosstalk")
            self.assertEqual(code, 0)
            code, text = self.run_cli("component", "enable", "held")
            self.assertEqual((code, json.loads(text)["reason"]), (1, "held"))
            code, text = self.run_cli("component", "enable")
            self.assertEqual(code, 2)
        self.assertEqual(calls, [("crosstalk", False, str(self.h)), ("crosstalk", True, str(self.h)),
                                 ("held", True, str(self.h))])

    def test_the_real_command_switches_a_part_end_to_end(self):
        manifests = self.parts("fakecomp")
        self.first(components=manifests)
        original = installer.set_component

        def with_fakes(component_id, enable, **kwargs):
            return original(component_id, enable, user_home=self.user, os_name=self.OS, ports=self.ports,
                            components=manifests, **{k: v for k, v in kwargs.items() if k == "home"})

        with mock.patch.object(installer, "set_component", with_fakes):
            code, text = self.run_cli("component", "disable", "fakecomp")
            self.assertEqual((code, json.loads(text)["outcome"]), (0, "disabled"))
            self.assertFalse(self.marker("fakecomp").exists())
            code, text = self.run_cli("component", "enable", "fakecomp")
            self.assertEqual((code, json.loads(text)["outcome"]), (0, "enabled"))
            self.assertTrue(self.marker("fakecomp").is_file())


class DoctorTests(unittest.TestCase):
    def test_an_installed_part_that_is_off_reads_as_skipped_not_missing(self):
        record = {"gen_id": "g1", "components": [{"id": "crosstalk", "state": "installed", "enabled": False,
                                                  "reason": "turned off"},
                                                 {"id": "core", "state": "installed", "enabled": True}]}
        with tempfile.TemporaryDirectory() as tmp:
            report = doctor._components_report(Path(doctor.__file__).parent, record, Path(tmp))
        crosstalk = next(item for item in report if item["id"] == "crosstalk")
        self.assertEqual(crosstalk["state"], "skipped")
        self.assertFalse(crosstalk["selected"])
        self.assertIn("turned off", crosstalk["reason"])


# -------------------------------------------------------------------- the real Crosstalk part
class PluginMachine(tc.Machine):
    """The test machine plus Claude's ``plugin disable`` and ``plugin enable`` (a key in ``enabledPlugins``)."""

    def __init__(self, base, user):
        super().__init__(base, user)
        self.disable_fails = False

    def claude_run(self, argv):
        args = argv[1:]
        if args[:2] in (["plugin", "disable"], ["plugin", "enable"]):
            self.calls.append(("claude", list(argv)))
            if self.disable_fails:
                return 1, "unknown command"
            settings = self.user / ".claude" / "settings.json"
            document = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
            document.setdefault("enabledPlugins", {})[args[2]] = args[1] == "enable"
            tc.write_json(settings, document)
            return 0, "ok"
        return super().claude_run(argv)


class CrosstalkToggleTests(tc.ComponentCase):
    def setUp(self):
        super().setUp()
        self.machine = PluginMachine(self.base, self.user)

    def permission_keys(self):
        settings = self.user / ".claude" / "settings.json"
        document = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
        return document.get("permissions", {}).get("allow", [])

    def plugins(self):
        settings = self.user / ".claude" / "settings.json"
        return json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins", {}) if settings.is_file() else {}

    def disabled(self, effects, **extra):
        return component_crosstalk.disable(dict(self.ctx(), **extra), effects)

    def test_disable_switches_the_plugin_off_but_keeps_it_installed(self):
        self.seed_user_files()
        effects = component_crosstalk.apply(self.ctx())
        self.register_plugin()
        self.assertEqual(self.plugins(), {tc.PLUGIN_ID: True})
        entries = self.permission_keys()
        self.assertTrue(len(entries) > 1)
        answer = self.disabled(effects)
        self.assertEqual(answer["detail"], {"plugin": "disabled"})
        self.assertEqual(self.plugins(), {tc.PLUGIN_ID: False})
        self.assertIn(["plugin", "disable", tc.PLUGIN_ID, "--scope", "user"],
                      [call[1][1:] for call in self.machine.calls if call[0] == "claude"])
        self.assertEqual(self.machine.shortcuts, {})  # the Startup shortcut is gone
        self.assertEqual(self.permission_keys(), ["Bash(ls)"])  # only the person's own entry is left
        self.assertTrue((self.eh / "marketplace" / "crosstalk" / "plugin" / "hooks" / "register.mjs").is_file())
        self.assertTrue(component_crosstalk.plugin_present(self.ctx()))
        self.assertIsNone(component_crosstalk.plugin_enabled(self.ctx()))
        self.assertTrue(answer["removed"])
        self.assertFalse([item for item in answer["removed"] if "claude-plugin" in item or "claude-marketplace" in item])

    def test_enable_switches_it_on_and_apply_puts_everything_back(self):
        self.seed_user_files()
        effects = component_crosstalk.apply(self.ctx())
        self.register_plugin()
        installed = self.user_files()
        answer = self.disabled(effects)
        component_crosstalk.enable(self.ctx())
        self.assertEqual(self.plugins(), {tc.PLUGIN_ID: True})
        self.assertEqual(self.machine.shortcuts, {})
        again = component_crosstalk.apply(self.ctx())
        self.assertTrue(answer["removed"])
        self.assertEqual(set(answer["removed"]), {item["effect_id"] for item in again})  # exactly what was undone
        self.assertEqual(self.user_files(), installed)
        self.assertTrue(self.machine.shortcuts)

    def test_uninstall_still_removes_a_plugin_that_was_left_disabled(self):
        """An older record's ``claude-command`` Effect (the installer records its own) is still undone."""
        self.seed_user_files()
        before = self.user_files()
        effects = component_crosstalk.apply(self.ctx())
        self.register_plugin()
        answer = self.disabled(effects)
        left = [item for item in effects if item["effect_id"] not in set(answer["removed"])]
        left.append({"effect_id": "old.plugin", "kind": "app-command", "target": "app:claude",
                     "undo": {"op": "claude-command", "probe": "plugin",
                              "argv": ["plugin", "uninstall", tc.PLUGIN_ID, "--scope", "user"]}})
        left.append({"effect_id": "old.marketplace", "kind": "app-command", "target": "app:claude",
                     "undo": {"op": "claude-command", "probe": "marketplace",
                              "argv": ["plugin", "marketplace", "remove", component_crosstalk.MARKETPLACE, "--scope", "user"]}})
        result = component_crosstalk.remove(self.ctx(), left)
        self.assertEqual(result["kept"], [])
        self.assertEqual(self.plugins(), {})
        self.assertEqual(self.user_files(), before)

    def test_without_the_disable_command_messaging_is_turned_off_and_back_on(self):
        self.seed_user_files()
        effects = component_crosstalk.apply(self.ctx())
        self.register_plugin()
        component_crosstalk._messaging(self.ctx(), "on")
        self.machine.disable_fails = True
        answer = self.disabled(effects)
        self.assertEqual(answer["detail"], {"plugin": "messaging-off", "messaging_before": "on"})
        self.assertTrue(answer["notes"])
        self.assertEqual(self.plugins(), {tc.PLUGIN_ID: True})  # the plugin could not be switched off
        self.assertEqual(component_crosstalk._messaging(self.ctx()), "off")
        component_crosstalk.enable(self.ctx_with(answer["detail"]))
        self.assertEqual(component_crosstalk._messaging(self.ctx()), "on")

    def ctx_with(self, detail):
        return dict(self.ctx(), toggle_detail=detail)

    def test_messaging_that_was_already_off_stays_off_when_switched_on_again(self):
        effects = component_crosstalk.apply(self.ctx())
        self.register_plugin()
        component_crosstalk._messaging(self.ctx(), "off")
        self.machine.disable_fails = True
        answer = self.disabled(effects)
        self.assertEqual(answer["detail"]["messaging_before"], "off")
        component_crosstalk.enable(self.ctx_with(answer["detail"]))
        self.assertEqual(component_crosstalk._messaging(self.ctx()), "off")

    def test_disabling_twice_keeps_the_first_note(self):
        effects = component_crosstalk.apply(self.ctx())
        self.register_plugin()
        first = self.disabled(effects)
        second = self.disabled([], toggle_detail=first["detail"])
        self.assertEqual(second["detail"], first["detail"])
        third = self.disabled([])
        self.assertEqual(third["detail"], {"plugin": "already-disabled"})

    def test_a_part_that_never_registered_the_plugin_has_nothing_to_switch(self):
        with mock.patch("eutonos.remote_control.find_claude", lambda **_kwargs: None):
            effects = component_crosstalk.apply(dict(self.ctx(), seams=dict(self.ctx()["seams"], claude_exe=lambda: None)))
            answer = component_crosstalk.disable(dict(self.ctx(), seams=dict(self.ctx()["seams"], claude_exe=lambda: None)),
                                                 effects)
        self.assertEqual(answer["detail"], {"plugin": "not-registered"})
        self.assertEqual([call for call in self.machine.calls if call[0] == "claude"], [])


if __name__ == "__main__":
    unittest.main()
