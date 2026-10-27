"""Offline installer upgrade regressions; never changes native app configuration."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


KIT = Path(__file__).resolve().parents[1] / "tools" / "crosstalk"
spec = importlib.util.spec_from_file_location("crosstalk_install_test", KIT / "crosstalk_p0_install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CrosstalkInstallTests(unittest.TestCase):
    def test_uninstall_command_defaults_to_preserving_other_changes(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is required for uninstall command selection")
        source = (KIT / "uninstall.ps1").read_text(encoding="utf-8")
        # Execute the shipped argument-selection branch only: no runtime, app CLI or config access.
        selection = source[source.index("$restoreArgs ="):source.index("$result = Invoke-Helper -Arguments $restoreArgs")]
        self.assertIn("[switch]$RestoreOriginalConfiguration", source)
        for restore, keep, expected in ((False, False, ["restore", "--keep-other-changes"]),
                                        (False, True, ["restore", "--keep-other-changes"]),
                                        (True, False, ["restore"])):
            command = "$RestoreOriginalConfiguration=$" + str(restore).lower() + ";$KeepOtherChanges=$" + str(keep).lower() + ";"
            command += selection + ";ConvertTo-Json -Compress -InputObject @($restoreArgs)"
            result = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), expected)

    def test_surgical_restore_preserves_current_config_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            config.write_text('model="owner-current"\n')
            original = config.read_bytes()
            directory = root / "install"
            installer.save_manifest(directory, {"state": "installed", "files": [
                {"id": "codex-config", "path": str(config), "existed": True,
                 "sha256": "old-hash", "backup": "should-never-be-read"}]})
            with patch.object(installer.core, "claude_home", return_value=root), patch.object(installer.core, "journal_path", return_value=root / "journal.jsonl"):
                result = installer.restore(directory, keep_other_changes=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["files"][0]["action"], "kept")
            self.assertEqual(config.read_bytes(), original)

    def test_upgrade_preserves_permissions_limits_and_unrelated_config(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            text = 'model = "chosen"\n' + installer.build_block("approve", "old-python", "old-server", 77, 321, "\r\n")
            text += '\n[mcp_servers.other]\ncommand = "other"\n'
            config.write_bytes(text.encode())
            before = installer._parse_toml(text)
            result = installer.toml_upgrade(config, "new-python", "new-server")
            after = installer._parse_toml(config.read_text())
            self.assertEqual(result["mode"], "approve")
            expected = before["mcp_servers"]["crosstalk_p0"]
            expected.update(command="new-python", args=["-I", "-B", "new-server"])
            self.assertEqual(after, before)
            self.assertIn('\r\n', config.read_bytes().decode())

    def test_explicit_permission_change_refuses_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            original = installer.build_block("approve", "old-python", "old-server", 20, 120, "\n").encode()
            config.write_bytes(original)
            with self.assertRaises(installer.InstallError) as raised:
                installer.toml_upgrade(config, "new-python", "new-server", "prompt")
            self.assertEqual(raised.exception.reason, "upgrade-permission-change-refused")
            self.assertEqual(config.read_bytes(), original)

    def test_snapshot_keeps_original_backups_and_current_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, settings = root / "config.toml", root / "settings.json"
            config.write_text('model="current"')
            settings.write_text(json.dumps({"permissions": {"allow": ["owner-rule"]}}))
            state = root / "state"
            original = {"state": "installed", "files": [{"backup": "original-path"}]}
            installer.save_manifest(state, original)
            with patch.object(installer.core, "claude_home", return_value=root / "claude"):
                snapshot = Path(installer.upgrade_snapshot(config, settings, state)["snapshot"])
            self.assertEqual(installer.load_manifest(state), original)
            self.assertEqual((snapshot / "settings.json").read_bytes(), settings.read_bytes())

    def test_upgrade_rejects_changed_permissions_before_cache_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            (snapshot / "settings.json").write_text('{"permissions":{"allow":["owner-rule"]}}')
            settings = root / "settings.json"
            settings.write_text('{"permissions":{"allow":["broader-rule"]}}')
            with self.assertRaises(installer.InstallError) as raised:
                installer.verify_upgrade(settings, snapshot)
            self.assertEqual(raised.exception.reason, "upgrade-settings-changed")

    def test_rebind_preserves_disabled_plugin_permissions_and_other_marketplace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plugins = root / "plugins"
            plugins.mkdir()
            settings = root / "settings.json"
            old_path = str(root / "old-kit")
            config = {"permissions": {"allow": ["owner-rule"]},
                      "enabledPlugins": {installer.PLUGIN_ID: False, "other": True},
                      "extraKnownMarketplaces": {installer.MARKETPLACE: {"source": {"source": "directory", "path": old_path}},
                                                 "other": {"source": {"source": "github", "repo": "owner/other"}}}}
            settings.write_text(json.dumps(config))
            registry = {installer.MARKETPLACE: {"source": {"source": "directory", "path": old_path}, "installLocation": old_path},
                        "other": {"source": {"source": "github", "repo": "owner/other"}}}
            (plugins / "known_marketplaces.json").write_text(json.dumps(registry))
            (plugins / "installed_plugins.json").write_text(json.dumps({"plugins": {installer.PLUGIN_ID: [{"scope": "user", "installPath": "existing-cache"}]}}))
            with patch.object(installer.core, "claude_home", return_value=root):
                installer.rebind_marketplace(settings)
            after = json.loads(settings.read_text())
            self.assertEqual(after["permissions"], config["permissions"])
            self.assertEqual(after["enabledPlugins"], config["enabledPlugins"])
            self.assertEqual(after["extraKnownMarketplaces"]["other"], config["extraKnownMarketplaces"]["other"])
            self.assertEqual(after["extraKnownMarketplaces"][installer.MARKETPLACE]["source"]["path"], str(KIT / "claude-plugin"))
            self.assertEqual(json.loads((plugins / "known_marketplaces.json").read_text())["other"], registry["other"])

    def test_render_plugin_writes_the_hook_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            here = Path(folder)
            template = here / "claude-plugin" / "plugin-src" / "register.mjs.in"
            template.parent.mkdir(parents=True)
            template.write_text("const a = '__CROSSTALK_P0_RUNTIME__'\nconst b = '__CROSSTALK_P0_CLI__'\n"
                                "const c = '__CROSSTALK_P0_HELPER__'\n", encoding="utf-8")
            hooks = here / "claude-plugin" / "plugin" / "hooks"
            target = hooks / "register.mjs"
            hooks.mkdir(parents=True)
            target.write_text("previous good hook\n", encoding="utf-8")
            with patch.object(installer, "HERE", here), \
                    patch.object(installer.core, "helper_dir", return_value=here / "helper"):
                # A failed swap leaves the previous hook untouched and no temporary file behind.
                with patch.object(installer.os, "replace", side_effect=OSError("denied")):
                    with self.assertRaises(OSError):
                        installer.render_plugin("C:\\py'thon.exe", "cli.py")
                self.assertEqual(target.read_text(encoding="utf-8"), "previous good hook\n")
                self.assertEqual([p.name for p in hooks.iterdir()], ["register.mjs"])
                report = installer.render_plugin("C:\\py'thon.exe", "cli.py")
            self.assertTrue(report["ok"])
            text = target.read_bytes().decode("utf-8")
            self.assertIn("const a = 'C:\\\\py\\'thon.exe'\n", text)
            self.assertNotIn("__CROSSTALK_P0_", text)
            self.assertNotIn("\r", text)
            self.assertEqual([p.name for p in hooks.iterdir()], ["register.mjs"])

    def test_a_kit_renders_the_launcher_hook_the_eutonos_installer_renders(self):
        """Parity (IPC5 F6): the kit's own render and the installer's marketplace give the same hook bytes."""
        from eutonos import install_record as ir
        from eutonos import installer_effects as fx
        from eutonos.installer_ports import FakePorts
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "eh"
            ports = FakePorts("windows" if os.name == "nt" else "macos")
            with patch.dict(os.environ, {"EUTONOS_HOME": str(home), "CROSSTALK_P0_DEVICE_DIR": ""}),                     patch.object(installer.core, "kit_mode", return_value=True):
                os.environ.pop("CROSSTALK_P0_DEVICE_DIR")
                report = installer.render_plugin()
                rendered = Path(report["rendered"])
                shipped = fx.render_marketplace(ir.Layout(home), ports, KIT, apply=False)
            self.assertEqual(rendered, home / "marketplace" / "crosstalk" / "plugin" / "hooks" / "register.mjs")
            self.assertEqual(fx.sha256_bytes(rendered.read_bytes()), shipped["plugin/hooks/register.mjs"])
            self.assertEqual(report["cli"], "-")
            self.assertEqual(Path(report["runtime"]).parent, home / "bin")
            text = rendered.read_bytes().decode("utf-8")
            self.assertIn("const CLI = '-'", text)
            self.assertNotIn("\r", text)

    def test_the_hook_runs_the_launcher_without_interpreter_flags_when_cli_is_dash(self):
        template = (KIT / "claude-plugin" / "plugin-src" / "register.mjs.in").read_text(encoding="utf-8")
        self.assertIn("CLI === '-' ? [RUNTIME, sub] : [RUNTIME, '-I', '-B', CLI, sub]", template)

    def test_a_kit_never_writes_the_codex_block_itself(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(installer.core, "kit_mode", return_value=True):
            config = Path(folder) / "config.toml"
            with self.assertRaises(installer.InstallError) as raised:
                installer.toml_add(config, "approve", "py", "server.py")
            self.assertEqual(raised.exception.reason, "codex-block-is-permission-entry-10")
            with self.assertRaises(installer.InstallError):
                installer.toml_upgrade(config, "py", "server.py")
            self.assertFalse(config.exists())

    def test_permission_entries_are_reported_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            kit = Path(folder) / "kit"
            entries = installer.permission_entries(kit)
            self.assertEqual([item["id"] for item in entries], [
                "set-remote-control", "send-message", "install-script", "uninstall-script", "kit-folder-edit"])
            for item in entries:  # every entry says app, exact rule, feature, why, required/optional and default
                self.assertEqual({"id", "app", "rule", "feature", "why", "required", "default"}, set(item))
                self.assertNotIn("{", item["rule"])  # the kit folder is filled in
            wanted = [item["rule"] for item in entries]
            self.assertEqual(wanted[:2], ["mcp__ccd_session_mgmt__set_remote_control", "mcp__ccd_session_mgmt__send_message"])
            self.assertIn(f"{kit}\\install.ps1", wanted[2])
            self.assertTrue(wanted[4].startswith("Edit(//") and wanted[4].endswith("/kit/**)"))
            self.assertEqual([item["required"] for item in entries], [True, False, False, False, False])
            missing_file = installer.permission_rules_status(settings, kit)
            self.assertFalse(missing_file["readable"])
            self.assertEqual({item["present"] for item in missing_file["entries"]}, {None})  # unknown, never present
            settings.write_text(json.dumps({"permissions": {"allow": [wanted[0], wanted[3], "Read"]}}), encoding="utf-8")
            before = settings.read_bytes()
            partial = installer.permission_rules_status(settings, kit)
            self.assertEqual([item["present"] for item in partial["entries"]], [True, False, False, True, False])
            self.assertEqual(partial["missing"], [])  # nothing required is missing
            self.assertEqual(partial["missing_optional"], [wanted[1], wanted[2], wanted[4]])
            self.assertEqual(partial["would_add"], [])  # only default-"allow" entries are added; the one that is, is there
            settings.write_text(json.dumps({"permissions": {"allow": wanted[1:]}}), encoding="utf-8")
            self.assertEqual(installer.permission_rules_status(settings, kit)["missing"], [wanted[0]])
            settings.write_bytes(before)
            installer.permission_rules_status(settings, kit)
            self.assertEqual(settings.read_bytes(), before)  # nothing was written

    def settings_and_state(self, folder, allow=("Read",)):
        settings, state = Path(folder) / "settings.json", Path(folder) / "state"
        text = json.dumps({"model": "chosen", "permissions": {"allow": list(allow), "deny": ["Bash(rm *)"]}, "z": 1}, indent=4)
        settings.write_text(text + "\n", encoding="utf-8", newline="")
        return settings, state

    def run_main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = installer.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_install_only_shows_the_plan_unless_asked_to_apply_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            settings, state = self.settings_and_state(folder)
            before = settings.read_bytes()
            for command in ("permissions", "permission-rules"):
                code, value = self.run_main(["--claude-settings", str(settings), "--state-dir", str(state), command])
                self.assertEqual((code, value.get("applied", False)), (0, False))
                self.assertEqual(len(value["would_add"]), 1)  # send-message is off: permissions.json owns it, project-local
            self.assertEqual(settings.read_bytes(), before)  # not written, not even backed up
            self.assertFalse(state.exists())

    def test_apply_permissions_backs_up_adds_only_missing_entries_and_records_them(self):
        with tempfile.TemporaryDirectory() as folder:
            send = "mcp__ccd_session_mgmt__send_message"
            settings, state = self.settings_and_state(folder, allow=("Read", send))  # the owner already has one
            before = settings.read_bytes()
            result = installer.permissions_apply(settings, state, Path(folder) / "kit")
            self.assertEqual(result["added"], ["mcp__ccd_session_mgmt__set_remote_control"])
            self.assertEqual(Path(result["backup"]).read_bytes(), before)  # backed up first, byte for byte
            written = settings.read_text(encoding="utf-8")
            after = json.loads(written)
            self.assertEqual(after["permissions"]["allow"], ["Read", send, "mcp__ccd_session_mgmt__set_remote_control"])
            self.assertEqual((after["model"], after["permissions"]["deny"], after["z"]), ("chosen", ["Bash(rm *)"], 1))
            self.assertEqual(list(after), ["model", "permissions", "z"])  # key order kept
            self.assertIn('\n    "model"', written)  # indent kept
            self.assertTrue(written.endswith("\n"))
            record = json.loads((state / installer.PERMISSIONS_RECORD).read_text())
            self.assertEqual([row["rule"] for row in record["added"]], ["mcp__ccd_session_mgmt__set_remote_control"])
            again = installer.permissions_apply(settings, state, Path(folder) / "kit")  # idempotent: nothing to add
            self.assertEqual((again["added"], again["backup"]), ([], None))
            self.assertEqual(len(json.loads((state / installer.PERMISSIONS_RECORD).read_text())["added"]), 1)

    def test_the_cli_writes_settings_only_with_the_explicit_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            settings, state = self.settings_and_state(folder)
            argv = ["--claude-settings", str(settings), "--state-dir", str(state), "permissions"]
            code, value = self.run_main(argv + ["--apply-permissions"])
            self.assertEqual(code, 0)
            self.assertTrue(value["applied"])
            self.assertEqual(len(value["added"]), 1)
            self.assertIn("mcp__ccd_session_mgmt__set_remote_control", json.loads(settings.read_text())["permissions"]["allow"])

    def test_apply_refuses_unreadable_or_odd_settings_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            settings = Path(folder) / "settings.json"
            with self.assertRaises(installer.InstallError):
                installer.permissions_apply(settings, state)  # missing
            settings.write_text("{not json", encoding="utf-8")
            with self.assertRaises(installer.InstallError):
                installer.permissions_apply(settings, state)
            settings.write_text(json.dumps({"permissions": {"allow": "oops"}}), encoding="utf-8")
            with self.assertRaises(installer.InstallError) as caught:
                installer.permissions_apply(settings, state)
            self.assertEqual(caught.exception.reason, "settings-allow-not-a-list")
            self.assertFalse(state.exists())

    def test_uninstall_removes_only_the_rules_install_recorded_adding(self):
        with tempfile.TemporaryDirectory() as folder:
            send = "mcp__ccd_session_mgmt__send_message"
            remote = "mcp__ccd_session_mgmt__set_remote_control"
            settings, state = self.settings_and_state(folder, allow=("Read", send))
            installer.permissions_apply(settings, state, Path(folder) / "kit")  # adds only `remote`
            data = json.loads(settings.read_text())
            data["permissions"]["allow"].append("Bash(git status)")  # the owner adds another later
            settings.write_text(json.dumps(data, indent=4) + "\n")
            result = installer.permissions_remove(settings, state)
            self.assertEqual((result["recorded"], result["removed"]), ([remote], [remote]))
            self.assertEqual(json.loads(settings.read_text())["permissions"]["allow"], ["Read", send, "Bash(git status)"])
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertFalse((state / installer.PERMISSIONS_RECORD).exists())

    def test_uninstall_with_nothing_recorded_touches_nothing_and_makes_no_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            settings, state = self.settings_and_state(
                folder, allow=("mcp__ccd_session_mgmt__set_remote_control", "mcp__ccd_session_mgmt__send_message"))
            before = settings.read_bytes()
            result = installer.permissions_remove(settings, state)  # the owner's own rules: never recorded
            self.assertEqual((result["recorded"], result["removed"], result["backup"]), ([], [], None))
            self.assertEqual(settings.read_bytes(), before)
            self.assertFalse(state.exists())

    def test_uninstall_keeps_the_record_when_settings_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as folder:
            settings, state = self.settings_and_state(folder)
            installer.permissions_apply(settings, state, Path(folder) / "kit")
            settings.write_text("{broken", encoding="utf-8")
            result = installer.permissions_remove(settings, state)
            self.assertEqual(result["removed"], [])
            self.assertTrue((state / installer.PERMISSIONS_RECORD).exists())

    def test_startup_shortcut_is_created_refreshed_and_removed_in_temp_dirs(self):
        with tempfile.TemporaryDirectory() as folder:
            startup = Path(folder) / "Startup"
            script_a = Path(folder) / "src-a" / "crosstalk_helper.py"
            script_b = Path(folder) / "src-b" / "crosstalk_helper.py"
            for script in (script_a, script_b):
                script.parent.mkdir()
                script.write_text("")

            def create(plan):  # stands in for the WScript.Shell step: writes a marker file
                Path(plan["shortcut"]).write_text(plan["arguments"])

            neighbour = startup / "Claude chat sync.lnk"
            startup.mkdir()
            neighbour.write_text("keep")
            first = installer.helper_startup_install(str(startup), pythonw="py.exe", create=create, script=str(script_a))
            self.assertEqual(first["action"], "created")
            self.assertIn(str(script_a), (startup / "Crosstalk helper.lnk").read_text())
            second = installer.helper_startup_install(str(startup), pythonw="py.exe", create=create, script=str(script_b))
            self.assertEqual(second["action"], "refreshed")  # same file, now pointing at the path it was given
            self.assertIn(str(script_b), (startup / "Crosstalk helper.lnk").read_text())
            self.assertNotIn(str(script_a), (startup / "Crosstalk helper.lnk").read_text())
            self.assertEqual(second["working_directory"], str(script_b.parent))
            self.assertEqual(sorted(path.name for path in startup.iterdir()), ["Claude chat sync.lnk", "Crosstalk helper.lnk"])
            removed = installer.helper_startup_remove(str(startup))
            self.assertTrue(removed["removed"])
            self.assertEqual([path.name for path in startup.iterdir()], ["Claude chat sync.lnk"])
            self.assertEqual(neighbour.read_text(), "keep")
            self.assertFalse(installer.helper_startup_remove(str(startup))["removed"])  # nothing left: no error

    def test_a_missing_helper_script_is_refused_before_any_shortcut_is_made(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(installer.InstallError) as caught:
                installer.helper_startup_install(str(Path(folder) / "Startup"), pythonw="py.exe",
                                                 create=lambda plan: self.fail("created"), script=str(Path(folder) / "no.py"))
            self.assertEqual(caught.exception.reason, "helper-script-missing")

    def test_the_shortcut_step_runs_powershell_wscript_and_never_starts_the_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "crosstalk_helper.py"
            script.write_text("")
            plan = installer.helper_startup_plan(str(Path(folder) / "Startup"), pythonw="py.exe", script=str(script))
            done = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(installer.subprocess, "run", return_value=done) as run, \
                    patch.object(installer.subprocess, "Popen", side_effect=AssertionError("started something")):
                installer._create_shortcut_with_powershell(plan)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "powershell.exe")
            self.assertIn("WScript.Shell", argv[-1])
            self.assertEqual(run.call_args.kwargs["env"]["CT_LNK"], plan["shortcut"])
            failed = subprocess.CompletedProcess([], 1, "", "boom")
            with patch.object(installer.subprocess, "run", return_value=failed), self.assertRaises(installer.InstallError):
                installer._create_shortcut_with_powershell(plan)

    def test_the_scripts_wire_the_shortcut_and_permission_steps(self):
        install = (KIT / "install.ps1").read_text(encoding="utf-8")
        uninstall = (KIT / "uninstall.ps1").read_text(encoding="utf-8")
        for needle in ("helper-startup-install", "-HelperPath", "-ApplyPermissions", "--apply-permissions"):
            self.assertIn(needle, install)
        for needle in ("helper-startup-remove", "permissions-remove", "relay-folder-remove"):
            self.assertIn(needle, uninstall)

    def test_uninstall_removes_only_the_relay_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            device = Path(folder)
            (device / "relay" / "jobs").mkdir(parents=True)
            (device / "helper").mkdir()
            (device / "helper" / "state.json").write_text("{}")
            with patch.object(installer.core, "device_dir", return_value=device):
                first = installer.relay_folder_remove()
                second = installer.relay_folder_remove()
            self.assertEqual((first["existed"], first["removed"]), (True, True))
            self.assertEqual((second["existed"], second["removed"]), (False, True))
            self.assertTrue((device / "helper" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
