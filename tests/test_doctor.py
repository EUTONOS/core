"""Doctor reporting stays read-only except for its explicit baseline option."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import completion_policy as cpol, doctor, permissions


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-doctor-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root, self.user, self.home, self.local = (base / name for name in ("project", "user", "eutonos-home", "local"))
        for path in (self.root, self.user, self.home, self.local):
            path.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.inventory = self.root / "docs" / "campaigns" / "ipc" / "IPC1_INVENTORY.jsonl"
        self.inventory.parent.mkdir(parents=True)
        self.inventory.write_text("\n".join(json.dumps(row) for row in (
            {"id": "P1", "class": "product"}, {"id": "U1", "class": "unknown"},
            {"id": "X1", "class": "personal"})) + "\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local), "EUTONOS_HOME": str(self.home),
                                                "HOME": str(self.user), "USERPROFILE": str(self.user),
                                                "CODEX_HOME": str(self.user / ".codex"),
                                                "CLAUDE_CONFIG_DIR": str(self.user / ".claude")})
        self.env.start()
        self.addCleanup(self.env.stop)
        from eutonos import doctor_outside, installer_ports
        self.fake_ports = installer_ports.FakePorts("windows")  # never read the real registry
        ports_patch = mock.patch.object(doctor_outside, "default_ports", return_value=self.fake_ports)
        ports_patch.start()
        self.addCleanup(ports_patch.stop)
        self.guards = mock.patch.multiple(doctor,
            _read_run_items=mock.DEFAULT,
            _git_readback=mock.DEFAULT,
            _account_readback=mock.DEFAULT)
        self.mocks = self.guards.start()
        self.addCleanup(self.guards.stop)
        doctor._read_run_items.return_value = ([], None)
        doctor._git_readback.return_value = {"state": "ok", "values": {"safe.directory": [], "core.autocrlf": []}}
        doctor._account_readback.return_value = {"state": "unknown", "reason": "fixture"}

    def result(self):
        return doctor.report(home=self.home, user_home=self.user, root=self.root)

    @staticmethod
    def fingerprints(*roots):
        result = {}
        for root in roots:
            if root.exists():
                for path in root.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def test_declared_items_report_each_state_and_keep_manual_ownership_clear(self):
        plan = {"profile": permissions.PLAN_PROFILE, "files": [{
            "file": "fixture", "additions": [{"entry": "set-remote-control"}],
            "satisfied": ["crosstalk-reply"],
            "kept": [{"entry": "send-message", "reason": "preserved manual value"}],
            "blocked": [{"entry": "codex-sandbox", "status": "unreadable", "reason": "fixture read failed"}]}],
            "held": [], "elsewhere": [], "off": [], "guided": [], "notes": []}
        with (mock.patch.object(permissions, "plan", return_value=plan),
              mock.patch.object(permissions, "desktop_session_readback", return_value={"state": "auto"}),
              mock.patch.object(permissions, "codex_trust_readback", return_value={"state": "trusted"})):
            items = {item["id"]: item for item in self.result()["declared"]["permissions"]["items"]}
        self.assertEqual(items["set-remote-control"]["state"], "missing")
        self.assertEqual(items["crosstalk-reply"]["state"], "ok")
        self.assertEqual(items["send-message"]["state"], "changed by you")
        self.assertEqual(items["codex-sandbox"]["state"], "unknown")
        self.assertEqual(items["codex-hook-trust"]["state"], "held")
        self.assertEqual(items["desktop-rc-default"]["state"], "skipped")
        self.assertEqual(items["crosstalk-reply"]["ownership"], "not-recorded")

    def test_effect_readbacks_distinguish_ok_missing_changed_and_unknown(self):
        same, changed = self.home / "same.json", self.home / "changed.json"
        same.write_text("same", encoding="utf-8")
        changed.write_text("before", encoding="utf-8")
        effects = [{"effect_id": "ok", "target": str(same), "after_sha256": hashlib.sha256(b"same").hexdigest()},
                   {"effect_id": "missing", "target": str(self.home / "absent"), "after_sha256": "0" * 64},
                   {"effect_id": "changed", "target": str(changed), "after_sha256": "0" * 64},
                   {"effect_id": "app", "target": "app:claude", "after_sha256": "0" * 64}]
        result = {item["effect_id"]: item["state"] for item in doctor._effect_report(effects)}
        self.assertEqual(result, {"ok": "ok", "missing": "missing", "changed": "changed by you", "app": "unknown"})

    def test_each_managed_block_is_read_by_its_own_markers_not_by_the_whole_file(self):
        # The installer writes the blocks one after another into one file and records each Effect with the whole file
        # as it stood right after that block, so only the last one would match the finished file by whole-file hash.
        sha = lambda raw: hashlib.sha256(raw).hexdigest()  # noqa: E731
        shipped = {name: cpol.make_block(cpol.markers(name), cpol.block_body("Rule " + name + ".", "## " + name.title()))
                   for name in ("alpha", "beta", "gamma")}
        target, text, effects = self.user / "CLAUDE.md", b"# mine\n\n", []
        for name, block in shipped.items():
            text += block + b"\n"
            effects.append({"effect_id": "instructions." + name, "kind": "file-block", "target": str(target),
                            "selector": "eutonos:" + name, "after_sha256": sha(text), "shipped_sha256": sha(block)})

        def states(raw):
            target.write_bytes(raw)
            return {item["effect_id"][len("instructions."):]: item["state"] for item in doctor._effect_report(effects)}

        everything = {"alpha": "ok", "beta": "ok", "gamma": "ok"}
        self.assertEqual(states(text), everything)
        self.assertEqual(states(text + b"typed later\n"), everything)  # text outside the blocks is not the block's
        self.assertEqual(states(b"# new top line\n" + text), everything)
        self.assertEqual(states(text.replace(b"\n", b"\r\n")), everything)  # a checkout may convert line endings
        self.assertEqual(states(text.replace(b"Rule beta.", b"Rule beta, my way.")),
                         {"alpha": "ok", "beta": "changed by you", "gamma": "ok"})
        self.assertEqual(states(text.replace(shipped["gamma"], b"")), {"alpha": "ok", "beta": "ok", "gamma": "missing"})
        self.assertEqual(states(text + shipped["alpha"]),
                         {"alpha": "changed by you", "beta": "ok", "gamma": "ok"})  # a second copy is not "the" block
        # A block whose body hash still matches but is another text than the record says EUTONOS shipped.
        other = cpol.make_block(cpol.markers("beta"), cpol.block_body("Rule beta, rewritten.", "## Beta"))
        self.assertEqual(states(text.replace(shipped["beta"], other)),
                         {"alpha": "ok", "beta": "changed by you", "gamma": "ok"})
        # An exact copy of the person's own text that EUTONOS wrapped in markers stays theirs and reads ok.
        wrapped = cpol.make_block(cpol.markers("beta"), b"## Beta\n\nRule beta.   \n")
        effects[1]["adoption"] = "wrap"
        self.assertEqual(states(text.replace(shipped["beta"], wrapped)), everything)
        # An unreadable file or a missing one keeps its own state; a block Effect that names no block is unknown.
        target.unlink()
        self.assertEqual({item["state"] for item in doctor._effect_report(effects)}, {"missing"})
        target.write_bytes(text)
        nameless = [dict(effects[0], selector=None)]
        self.assertEqual(doctor._effect_report(nameless)[0]["state"], "unknown")

    def test_undeclared_items_are_reported_without_adopting_them(self):
        claude = self.user / ".claude" / "settings.json"
        claude.parent.mkdir()
        claude.write_text(json.dumps({"permissions": {"allow": ["mcp__ccd_unlisted__call"]}}), encoding="utf-8")
        codex = self.user / ".codex" / "config.toml"
        codex.parent.mkdir()
        codex.write_text("[mcp_servers.crosstalk_p0]\ncommand = 'old-copy'\n", encoding="utf-8")
        (self.home / "hosts" / "unfinished").mkdir(parents=True)
        (self.home / "runtime").mkdir()
        device = {"profile": "eutonos.device-repositories.v1", "cache_generation": 0,
                  "shared": [{"locators": [str(self.root)]}], "personal": None}
        (self.home / "device-repositories.json").write_text(json.dumps(device), encoding="utf-8")
        self.home.joinpath("projects.json").write_text(json.dumps({"profile": "eutonos.projects.v1", "projects": []}), encoding="utf-8")
        doctor._read_run_items.return_value = ([{"source": "Startup", "name": "EUTONOS.lnk"}], None)
        items = self.result()["undeclared"]
        kinds = {item["kind"] for item in items}
        self.assertTrue({"undeclared-allow-rule", "undeclared-codex-mcp", "admission-less-host",
                         "legacy-runtime", "repository-registry-mismatch", "startup-or-run-item"} <= kinds)
        self.assertTrue(all(item["state"] == "unknown" for item in items))

    def test_every_shipped_claude_allow_rule_of_the_crosstalk_entries_is_known(self):
        rules = doctor._known_allow_rules()
        self.assertIn("SendMessage", rules)  # native-send-message
        self.assertIn("mcp__ccd_session_mgmt__send_message", rules)
        self.assertIn("mcp__ccd_session_mgmt__set_remote_control", rules)
        self.assertIn("mcp__crosstalk-p0__send", rules)
        declared = {entry["id"] for entry in doctor.permissions.entries(doctor.permissions.load())
                    if entry.get("app") == "claude" and entry.get("key") == "/permissions/allow"}
        self.assertLessEqual(doctor.KNOWN_ALLOW_ENTRIES, declared)

    def test_the_installed_native_send_message_rule_is_not_reported_undeclared(self):
        local = self.root / ".claude" / "settings.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"permissions": {"allow": ["SendMessage", "mcp__ccd_session_mgmt__send_message"]}}),
                         encoding="utf-8")
        kinds = lambda: [item for item in doctor._undeclared_report(self.home, self.user, self.root, [])
                         if item["kind"] == "undeclared-allow-rule"]
        self.assertEqual(kinds(), [])

    def test_a_codex_block_the_install_recorded_is_not_reported_undeclared(self):
        codex = self.user / ".codex" / "config.toml"
        codex.parent.mkdir()
        codex.write_text("[mcp_servers.crosstalk_p0]\ncommand = 'x'\n", encoding="utf-8")
        recorded = [{"effect_id": "codex-mcp-block.abc", "permission_entry": "codex-mcp-block", "target": str(codex)}]
        kinds = lambda effects: {item["kind"] for item in doctor._undeclared_report(self.home, self.user, self.root, effects)}
        self.assertIn("undeclared-codex-mcp", kinds([]))
        self.assertNotIn("undeclared-codex-mcp", kinds(recorded))

    def test_malformed_record_and_checkpoint_are_unknown_not_fatal(self):
        install = self.home / "install"
        record_dir = install / "generations" / "0123456789abcdef"
        record_dir.mkdir(parents=True)
        malformed = b"{"
        (record_dir / "record.json").write_bytes(malformed)
        (install / "state.json").write_text(json.dumps({"profile": "eutonos.install.state.v1",
                                                         "active": "0123456789abcdef",
                                                         "generations": {"0123456789abcdef": hashlib.sha256(malformed).hexdigest()}}),
                                                encoding="utf-8")
        (install / "checkpoint.json").write_text("not json", encoding="utf-8")
        result = self.result()
        self.assertEqual(result["active_record"]["state"], "unknown")
        self.assertEqual(result["checkpoint"]["state"], "unknown")

    def test_checkpoint_marks_changed_done_evidence_stale_and_reports_next_action(self):
        install = self.home / "install"
        install.mkdir()
        checkpoint = {"profile": "eutonos.setup-checkpoint.v1", "checklist": "install", "checklist_revision": "1",
                      "gen_id": None, "root": str(self.root), "updated_at": "2026-10-22T00:00:00Z",
                      "steps": [{"id": "trust", "kind": "person", "runner": "owner", "state": "done",
                                 "evidence": ["permission:codex-trust=ok"], "reason": None, "next_action": None},
                                {"id": "finish", "kind": "agent", "runner": "agent", "state": "pending",
                                 "evidence": [], "reason": None, "next_action": "Finish setup"}]}
        (install / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        result = self.result()["checkpoint"]
        self.assertEqual(result["steps"][0]["state"], "stale")
        self.assertEqual(result["next_action"], "Finish setup")

    def test_checkpoint_names_its_shipped_checklist_page_and_whether_the_revision_matches(self):
        install = self.home / "install"
        install.mkdir()

        def write(checklist, revision):
            checkpoint = {"profile": "eutonos.setup-checkpoint.v1", "checklist": checklist, "checklist_revision": revision,
                          "gen_id": None, "root": None, "updated_at": "2026-10-22T00:00:00Z",
                          "steps": [{"id": "finish", "kind": "agent", "runner": "agent", "state": "pending",
                                     "evidence": [], "reason": None, "next_action": "Finish setup"}]}
            (install / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
            return self.result()

        old = write("install", "0-old")["checkpoint"]["page"]
        self.assertTrue(Path(old["path"]).is_file())
        self.assertEqual(Path(old["path"]).name, "install.md")
        self.assertEqual(Path(old["path"]).parent.name, "checklists")
        self.assertEqual((old["recorded_revision"], old["matches"]), ("0-old", False))
        current = write("install", old["revision"])
        self.assertTrue(current["checkpoint"]["page"]["matches"])
        text = doctor._short_text(current)
        self.assertIn("Checklist page:", text)
        self.assertNotIn("the checkpoint recorded", text)
        self.assertIn("the checkpoint recorded 0-old", doctor._short_text(write("install", "0-old")))
        self.assertIsNone(write("something-else", "1")["checkpoint"]["page"])

    def test_readiness_keeps_held_and_skipped_out_of_failure_list(self):
        result = self.result()
        self.assertIn("codex-hook-trust", [item["id"] for item in result["readiness"]["held"]])
        self.assertIn("desktop-rc-default", [item["id"] for item in result["readiness"]["skipped"]])
        self.assertNotIn("codex-hook-trust", [item["id"] for item in result["readiness"]["blockers"]])

    def test_a_pc_without_git_is_reported_as_limited_and_is_not_a_blocker(self):
        with mock.patch.object(doctor, "_git_present", return_value=False):
            result = self.result()
        found = result["readbacks"]["git_parts"]
        self.assertEqual((found["state"], found["git"]), ("limited", "not found"))
        self.assertEqual([item["id"] for item in found["parts"]], ["core"])
        self.assertEqual([item["id"] for item in result["readiness"]["limited"]], ["git"])
        self.assertIn("project memory", result["readiness"]["limited"][0]["reason"])
        self.assertNotIn("git", [item["id"] for item in result["readiness"]["blockers"]])
        with mock.patch.object(doctor, "_git_present", return_value=True):
            self.assertEqual(self.result()["readiness"]["limited"], [])

    def test_report_writes_nothing_and_baseline_requires_the_flag(self):
        before = self.fingerprints(self.root, self.user, self.home, self.local)
        result = self.result()
        baseline = self.home / "install" / "doctor-baseline.json"
        self.assertEqual(result["baseline"]["state"], "not-saved")
        self.assertFalse(baseline.exists())
        self.assertEqual(before, self.fingerprints(self.root, self.user, self.home, self.local))
        output = io.StringIO()
        with redirect_stdout(output):
            code = doctor.main(["--json", "--root", str(self.root), "--home", str(self.home),
                                "--user-home", str(self.user), "--save-baseline"])
        self.assertEqual(code, 0)
        saved = json.loads(output.getvalue())["baseline"]
        self.assertEqual(saved["state"], "saved")
        value = json.loads(baseline.read_text(encoding="utf-8"))
        self.assertEqual(value["counts"], {"adopted": 1, "kept": 1, "personal": 1})


if __name__ == "__main__":
    unittest.main()


class RegistryEffectTests(unittest.TestCase):
    def test_real_environment_effect_shapes_have_read_only_readback(self):
        from eutonos import install_record as ir, installer_effects as fx
        from eutonos.installer_ports import FakePorts
        with tempfile.TemporaryDirectory() as folder:
            layout = ir.Layout(Path(folder) / "H")
            ports = FakePorts("windows")
            effect, _ = fx.path_effect(layout, ports, Path(folder) / "user", "g", apply=True)
            env, _ = fx.env_home_effect(layout, ports, "g", apply=True)
            writes = list(ports.calls)
            rows = doctor._effect_report([effect, env], ports=ports)
            self.assertEqual([row["state"] for row in rows], ["ok", "ok"])
            self.assertEqual(ports.calls, writes)


class OutsideReadbackTests(unittest.TestCase):
    """ISU stage 2: every declared outside effect that has no file to hash gets a doctor readback, through fake ports."""

    def setUp(self):
        from eutonos import install_record as ir, installer_ports
        self.tmp = tempfile.TemporaryDirectory(prefix="eutonos-outside-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home, self.user = self.base / "H", self.base / "user"
        self.home.mkdir()
        self.user.mkdir()
        self.layout = ir.Layout(self.home)
        self.ports = installer_ports.FakePorts("windows")

    def read(self, record=None, effects=()):
        from eutonos import doctor_outside
        return doctor_outside.readbacks(self.home, self.user, record, list(effects), self.ports)

    def test_user_path_is_read_back_from_the_ports(self):
        self.assertEqual(self.read()["user-path"]["state"], "missing")
        self.ports.path_value = "C:\\Windows;" + str(self.layout.bin)
        self.assertEqual(self.read()["user-path"]["state"], "ok")
        self.assertEqual(self.ports.calls, [])  # read only

    def test_user_path_on_macos_reads_the_profile_line(self):
        from eutonos import installer_effects as fx, installer_ports
        self.ports = installer_ports.FakePorts("macos")
        self.assertEqual(self.read()["user-path"]["state"], "missing")
        (self.user / ".zprofile").write_text(f'export PATH="{self.layout.bin}:$PATH" {fx.PATH_MARK}\n', encoding="utf-8")
        self.assertEqual(self.read()["user-path"]["state"], "ok")

    def test_eutonos_home_variable_states(self):
        self.assertEqual(self.read()["user-env-home"]["state"], "missing")  # a folder that is not the default
        self.ports.user_env["EUTONOS_HOME"] = str(self.home)
        self.assertEqual(self.read()["user-env-home"]["state"], "ok")
        self.ports.user_env["EUTONOS_HOME"] = str(self.base / "other")
        self.assertEqual(self.read()["user-env-home"]["state"], "changed by you")
        self.ports.user_env.clear()
        self.home = self.user / ".eutonos"
        self.assertEqual(self.read()["user-env-home"]["state"], "ok")  # the default folder needs no variable

    def test_installed_apps_entry_is_read_back(self):
        self.assertEqual(self.read()["installed-apps-entry"]["state"], "missing")
        self.ports.uninstall_entry = {"DisplayName": "EUTONOS", "DisplayVersion": "1.2.3", "UninstallString": "x.exe"}
        row = self.read()["installed-apps-entry"]
        self.assertEqual(row["state"], "ok")
        self.assertIn("1.2.3", row["reason"])
        self.ports.uninstall_entry["UninstallString"] = None
        self.assertEqual(self.read()["installed-apps-entry"]["state"], "changed by you")

    def test_runtime_pip_is_read_from_the_runtime_folder(self):
        from eutonos import permissions
        pin = permissions.entry_by_id(permissions.load(), "runtime-pip")["value"]
        record = {"wheel": {"artifact_id": "art1"}}
        self.assertEqual(self.read()["runtime-pip"]["state"], "missing")  # no record
        site = self.ports.site_packages(self.layout.runtimes / "art1")
        site.mkdir(parents=True)
        self.assertEqual(self.read(record)["runtime-pip"]["state"], "missing")
        (site / "pip-0.0.1.dist-info").mkdir()
        self.assertEqual(self.read(record)["runtime-pip"]["state"], "changed by you")
        (site / f"pip-{pin['version']}.dist-info").mkdir()
        self.assertEqual(self.read(record)["runtime-pip"]["state"], "ok")

    def test_hermes_gateway_compares_the_config_the_install_wrote(self):
        from eutonos import component_support as cs
        self.assertEqual(self.read()["hermes-gateway"]["state"], "skipped")  # Hermes was not there
        folder, config = self.base / "hermes" / "plugins" / "crosstalk", self.base / "hermes" / "config.yaml"
        folder.mkdir(parents=True)
        config.write_text("plugins: [crosstalk]\n", encoding="utf-8")
        effect = {"component": "hermes", "target": str(folder),
                  "undo": {"folder": str(folder), "config": str(config), "config_after": cs.sha256(config.read_bytes())}}
        self.assertEqual(self.read(effects=[effect])["hermes-gateway"]["state"], "ok")
        config.write_text("plugins: []\n", encoding="utf-8")
        self.assertEqual(self.read(effects=[effect])["hermes-gateway"]["state"], "changed by you")
        folder.rmdir()
        self.assertEqual(self.read(effects=[effect])["hermes-gateway"]["state"], "missing")

    def test_the_companion_appearance_value_is_absent_valid_or_foreign(self):
        self.assertEqual(self.read()["companion-appearance"]["state"], "ok")
        self.ports.appearance = "dark"
        self.assertIn("dark", self.read()["companion-appearance"]["reason"])
        self.ports.appearance = "purple"
        self.assertEqual(self.read()["companion-appearance"]["state"], "changed by you")

    def test_no_ports_reports_unknown_not_a_guess(self):
        from eutonos import doctor_outside
        rows = doctor_outside.readbacks(self.home, self.user, None, [], None)
        self.assertEqual({row["state"] for row in rows.values()}, {"unknown"})
        self.assertEqual(set(rows), set(doctor_outside.ENTRIES))

    def test_the_report_uses_them_so_required_entries_are_no_longer_unknown(self):
        with mock.patch.object(doctor, "_git_readback", return_value={"state": "ok", "values": {}}), \
                mock.patch.object(doctor, "_account_readback", return_value={"state": "unknown"}), \
                mock.patch.object(doctor, "_read_run_items", return_value=([], None)):
            result = doctor.report(home=self.home, user_home=self.user, root=self.base, ports=self.ports)
        items = {item["id"]: item for item in result["declared"]["permissions"]["items"]}
        self.assertEqual(items["user-path"]["state"], "missing")  # read, and named (it was "unknown")
        self.assertEqual(items["runtime-pip"]["state"], "missing")
        self.assertEqual(items["installed-apps-entry"]["state"], "skipped")  # off in the typed route ...
        self.assertEqual(items["installed-apps-entry"]["observed"]["state"], "missing")  # ... but still read back
        self.assertIn("companion-appearance", result["readbacks"]["outside"])

    def test_a_startup_shortcut_effect_is_compared_by_its_own_bytes(self):
        lnk = self.base / "EUTONOS.lnk"
        lnk.write_bytes(b"lnk-bytes")
        raw = hashlib.sha256(b"lnk-bytes").hexdigest()
        effect = {"effect_id": "s", "component": "crosstalk", "kind": "shortcut", "target": str(lnk),
                  "after_sha256": "canonical-spec-hash", "undo": {"op": "shortcut-delete", "raw_after": raw}}
        self.assertEqual(doctor._effect_report([effect])[0]["state"], "ok")
        lnk.write_bytes(b"edited")
        self.assertEqual(doctor._effect_report([effect])[0]["state"], "changed by you")

    def test_an_app_effect_is_read_through_its_parts_verify(self):
        effect = {"effect_id": "t", "component": "companion", "kind": "scheduled-task", "target": "app:schtasks"}
        self.assertEqual(doctor._effect_report([effect])[0]["state"], "unknown")
        self.assertEqual(doctor._effect_report([effect], components=[{"id": "companion", "state": "ok"}])[0]["state"], "ok")
        row = doctor._effect_report([effect], components=[{"id": "companion", "state": "missing"}])[0]
        self.assertEqual(row["state"], "unknown")
        self.assertIn("missing", row["reason"])

    def test_the_report_lists_each_declared_outside_effect_with_a_readback(self):
        with mock.patch.object(doctor, "_git_readback", return_value={"state": "ok", "values": {}}), \
                mock.patch.object(doctor, "_account_readback", return_value={"state": "unknown"}), \
                mock.patch.object(doctor, "_read_run_items", return_value=([], None)):
            result = doctor.report(home=self.home, user_home=self.user, root=self.base, ports=self.ports)
        rows = {row["id"]: row for row in result["declared"]["outside_effects"]}
        self.assertEqual(set(rows), {item["id"] for item in permissions.load()["outside_effects"]})
        self.assertEqual(rows["companion-appearance"]["state"], "ok")  # not set, which is fine
        for name in ("crosstalk-helper-startup", "companion-task", "simon-skills", "instruction-blocks"):
            self.assertIn(rows[name]["state"], ("missing", "skipped"), name)  # nothing installed here

    def test_a_declared_effect_reads_the_recorded_effects_it_names(self):
        from eutonos import doctor_outside
        items = [{"id": "x", "component": "crosstalk", "kind": "shortcut", "os": ["windows"], "where": "w", "why": "y",
                  "effects": ["crosstalk.helper-shortcut."], "readback": "effect", "removal": "r"}]
        run = lambda rows, parts=({"id": "crosstalk", "selected": True},), os_name="windows": doctor_outside.declared_report(
            items, os_name=os_name, components=list(parts), effect_rows=rows, outside={})[0]
        self.assertEqual(run([{"effect_id": "crosstalk.helper-shortcut.ab12cd34", "state": "ok"}])["state"], "ok")
        changed = run([{"effect_id": "crosstalk.helper-shortcut.ab12cd34", "state": "changed by you", "reason": "edited"}])
        self.assertEqual((changed["state"], changed["reason"]), ("changed by you", "edited"))
        self.assertEqual(run([])["state"], "missing")
        self.assertEqual(run([], os_name="macos")["state"], "skipped")
        self.assertEqual(run([], parts=({"id": "crosstalk", "selected": False},))["state"], "skipped")
