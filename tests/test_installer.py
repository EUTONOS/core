"""The installer: record and state IO, the setup checkpoint, the machine lock, install, uninstall, update, rollback.

Every test uses a temporary EUTONOS home, temporary Claude, Codex and Hermes folders and fake OS ports. Nothing here
touches the real machine.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import tomllib
import unittest
import unittest.mock

from eutonos import install_record as ir
from eutonos import setup_checkpoint as cp
from eutonos import tokenslang as ts


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.h = self.base / "eutonos-home"
        self.layout = ir.Layout(self.h)


class RuntimePipTrustTests(Base):
    def ports(self, version="26.2.2"):
        from eutonos.installer_ports import Ports
        ports = Ports("windows")
        ports.commands = []
        def run(argv, **kwargs):
            ports.commands.append(argv)
            return (0, version, "") if "-c" in argv else (0, "", "")
        ports.run = run
        return ports

    def test_verified_pip_is_installed_and_read_back_before_product_wheel(self):
        ports = self.ports()
        ports.build_runtime(self.base / "runtime", self.base / "eutonos.whl")
        self.assertEqual(len(ports.commands), 4)
        self.assertTrue(str(ports.commands[1][-1]).endswith("pip-26.2.2-py3-none-any.whl"))
        self.assertIn("--no-index", ports.commands[1])
        self.assertEqual(ports.commands[-1][-1], str(self.base / "eutonos.whl"))

    def test_corrupt_pip_refuses_before_creating_runtime(self):
        ports = self.ports()
        with unittest.mock.patch.object(Path, "read_bytes", return_value=b"corrupt"):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                ports.build_runtime(self.base / "runtime", self.base / "eutonos.whl")
        self.assertEqual(ports.commands, [])

    def test_failed_pip_readback_prevents_product_install(self):
        ports = self.ports("25.2")
        with self.assertRaisesRegex(RuntimeError, "version readback"):
            ports.build_runtime(self.base / "runtime", self.base / "eutonos.whl")
        self.assertEqual(len(ports.commands), 3)


class RecordTests(Base):
    def test_state_round_trip_and_default(self):
        state = ir.read_state(self.layout)
        self.assertEqual(state["profile"], ir.STATE_PROFILE)
        self.assertIsNone(state["active"])
        state["channel"] = "beta"
        ir.write_state(self.layout, state)
        self.assertEqual(ir.read_state(self.layout)["channel"], "beta")

    def test_damaged_state_is_an_error_never_replaced(self):
        self.layout.state.parent.mkdir(parents=True)
        self.layout.state.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ts.ReadError):
            ir.read_state(self.layout)
        self.assertEqual(self.layout.state.read_text(encoding="utf-8"), "{not json")

    def test_record_hash_is_noted_in_the_state(self):
        state = ir.read_state(self.layout)
        record = ir.new_record(gen_id="0123456789abcdef", previous_gen=None, os_name="windows", version="1.0.0",
                               wheel={"file": "a.whl", "sha256": "0" * 64, "artifact_id": "eutonos-1.0.0-000000000000"},
                               kits=[], consent=None)
        digest = ir.write_record(self.layout, state, record)
        self.assertEqual(state["generations"]["0123456789abcdef"], digest)
        self.assertTrue(ir.record_intact(self.layout, state, "0123456789abcdef"))
        record["state"] = "active"
        ir.write_record(self.layout, state, record)
        self.assertTrue(ir.record_intact(self.layout, state, "0123456789abcdef"))
        self.layout.record("0123456789abcdef").write_text("{}", encoding="utf-8")
        self.assertFalse(ir.record_intact(self.layout, state, "0123456789abcdef"))

    def test_effects_merge_by_id_and_are_checked(self):
        record = ir.new_record(gen_id="g" * 16, previous_gen=None, os_name="macos", version="1", wheel={}, kits=[],
                               consent=None)
        effect = {name: None for name in ir.EFFECT_FIELDS}
        effect.update(effect_id="a", kind="file-create", target="x", component="core")
        ir.merge_effects(record, [effect])
        ir.merge_effects(record, [dict(effect, after_sha256="1" * 64), dict(effect, effect_id="b")])
        self.assertEqual([item["effect_id"] for item in record["effects"]], ["a", "b"])
        self.assertEqual(record["effects"][0]["after_sha256"], "1" * 64)
        with self.assertRaises(ts.ReadError):
            ir.merge_effects(record, [dict(effect, kind="bogus")])

    def test_journal_keeps_lines_and_drops_a_torn_last_one(self):
        ir.journal_append(self.layout, "t1", {"op": "begin"})
        ir.journal_append(self.layout, "t1", {"op": "effect", "id": "x"})
        with self.layout.journal("t1").open("ab") as stream:
            stream.write(b'{"op": "tor')
        self.assertEqual([item["op"] for item in ir.journal_read(self.layout, "t1")], ["begin", "effect"])
        self.assertEqual(ir.journal_read(self.layout, "missing"), [])

    def test_receipts_name_the_phase_by_position(self):
        path = ir.write_receipt(self.layout, "g1", "surfaces", {"ok": True})
        self.assertEqual(path.name, "03-surfaces.json")
        ir.write_receipt(self.layout, "g1", "runtime", {})
        self.assertEqual(ir.done_phases(self.layout, "g1"), ["runtime", "surfaces"])

    def test_artifact_id(self):
        self.assertEqual(ir.artifact_id("1.2.3", "ab" * 32), "eutonos-1.2.3-abababababab")


class CheckpointTests(Base):
    def steps(self):
        return [{"id": "runtime", "kind": "mechanical", "runner": "agent", "state": "done", "evidence": ["r1"]},
                {"id": "restart-apps", "kind": "person", "runner": "owner"},
                {"id": "orchestration", "kind": "agent", "runner": "agent"}]

    def test_shape(self):
        value = cp.start("install", self.steps(), checklist_revision="r1", gen_id="g1", home=self.h)
        self.assertEqual(cp.read(self.h), value)
        self.assertEqual(value["profile"], "eutonos.setup-checkpoint.v1")
        self.assertEqual(set(value), {"profile", "checklist", "checklist_revision", "gen_id", "root", "updated_at", "steps"})
        self.assertIsNone(value["root"])
        for step in value["steps"]:
            self.assertEqual(set(step), {"id", "kind", "runner", "state", "evidence", "reason", "next_action"})
        self.assertEqual(cp.next_open_step(value)["id"], "restart-apps")
        self.assertEqual(cp.summary(value)["done"], 1)

    def test_rules(self):
        cp.start("install", self.steps(), checklist_revision="r1", home=self.h)
        with self.assertRaises(cp.CheckpointError):
            cp.record_step("restart-apps", "done", home=self.h)  # a person step needs evidence
        with self.assertRaises(cp.CheckpointError):
            cp.record_step("orchestration", "skipped", home=self.h)  # a skip needs a reason
        with self.assertRaises(cp.CheckpointError):
            cp.record_step("nope", "done", ["x"], home=self.h)
        with self.assertRaises(cp.CheckpointError):
            cp.start("bogus", [], checklist_revision="r", home=self.h)
        with self.assertRaises(cp.CheckpointError):
            cp.start("install", self.steps() + self.steps()[:1], checklist_revision="r", home=self.h)
        cp.record_step("restart-apps", "done", ["readback: apps restarted"], home=self.h)
        cp.record_step("orchestration", "skipped", reason="no Hermes", home=self.h)
        value = cp.read(self.h)
        self.assertEqual([step["state"] for step in value["steps"]], ["done", "done", "skipped"])
        self.assertIsNone(cp.next_open_step(value))

    def test_blocked_step_keeps_its_next_action(self):
        cp.start("uninstall", self.steps(), checklist_revision="r1", home=self.h)
        cp.record_step("orchestration", "blocked", reason="chat still writing", next_action="stop the chat", home=self.h)
        step = cp.read(self.h)["steps"][2]
        self.assertEqual((step["state"], step["next_action"]), ("blocked", "stop the chat"))

    def test_damaged_checkpoint_is_an_error(self):
        cp.checkpoint_path(self.h).parent.mkdir(parents=True)
        cp.checkpoint_path(self.h).write_text("[]", encoding="utf-8")
        with self.assertRaises(cp.CheckpointError):
            cp.read(self.h)


class LockTests(Base):
    def test_a_second_operation_is_refused_while_the_lock_is_held(self):
        held, release, outcome = threading.Event(), threading.Event(), {}

        def holder():
            with cp.machine_lock(self.h, wait=0):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(held.wait(10))
            with self.assertRaises(cp.LifecycleBusy):
                with cp.machine_lock(self.h, wait=0):
                    outcome["entered"] = True
            self.assertNotIn("entered", outcome)
        finally:
            release.set()
            thread.join(10)
        with cp.machine_lock(self.h, wait=0):  # free again
            pass

    def test_the_holder_can_write_its_checkpoint(self):
        with cp.machine_lock(self.h, wait=0):
            cp.start("install", [{"id": "a"}], checklist_revision="r", home=self.h)
        self.assertEqual(cp.read(self.h)["steps"][0]["id"], "a")


# ------------------------------------------------------------------ install and uninstall
import sys
import types
import zipfile

from eutonos import installer
from eutonos import completion_policy as cpol
from eutonos import crosstalk_instructions as ci
from eutonos import decisions_instructions as dpol
from eutonos import doctor
from eutonos import component_hermes
from eutonos import installer_effects as fx
from eutonos import memory_instructions as mi
from eutonos.installer_ports import FakePorts

CONSENT = {"text_id": "eutonos.consent.v1", "text_sha256": "0" * 64, "surface": "test",
           "accepted_at": "2026-10-22T00:00:00Z", "choices": {}}
TEMPLATE = ("const RUNTIME = '__CROSSTALK_P0_RUNTIME__';\nconst CLI = '__CROSSTALK_P0_CLI__';\n"
            "const HELPER = '__CROSSTALK_P0_HELPER__';\n")
LEGACY_TOML = (
    'model = "x"\n\n'
    "# >>> crosstalk-p0 throwaway probe (remove with uninstall.ps1) >>>\n"
    "[mcp_servers.crosstalk_p0]\n"
    'command = "C:/old/python.exe"\n'
    'args = ["-I", "-B", "C:/old/codex_tool_server.py"]\n'
    'default_tools_approval_mode = "approve"\n'
    "# <<< crosstalk-p0 <<<\n")


def build_wheel(folder, version="1.2.3", body="one", scripts=("eutonos-crosstalk", "eutonos-install", "eutonos-crosstalk-mcp"),
                gui=("crosstalk-helper", "desktop-companion"), plugin_version=None) -> Path:
    """A tiny wheel with the shapes the installer reads: package files, one kit, METADATA and entry points."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"eutonos-{version}-py3-none-any.whl"
    kit = "eutonos/kits/crosstalk/"
    entry = "[console_scripts]\n" + "".join(f"{name} = eutonos.payload:main\n" for name in scripts)
    entry += "[gui_scripts]\n" + "".join(f"{name} = eutonos.payload:main\n" for name in gui)
    files = {
        "eutonos/__init__.py": "",
        "eutonos/payload.py": f"VALUE = {body!r}\n",
        kit + "crosstalk_p0_cli.py": f"print({body!r})\n",
        kit + "QUICKSTART.md": "# quickstart\n",
        kit + "claude-plugin/.claude-plugin/marketplace.json": json.dumps({"name": "crosstalk-p0-local"}),
        kit + "claude-plugin/plugin/.claude-plugin/plugin.json": json.dumps(
            {"name": "crosstalk-p0", **({"version": plugin_version} if plugin_version else {})}),
        kit + "claude-plugin/plugin-src/register.mjs.in": TEMPLATE,
        f"eutonos-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: eutonos\nVersion: {version}\n",
        f"eutonos-{version}.dist-info/entry_points.txt": entry,
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def snapshot(folder) -> dict:
    folder = Path(folder)
    return {item.relative_to(folder).as_posix(): item.read_bytes() for item in sorted(folder.rglob("*")) if item.is_file()}


class LifeBase(Base):
    OS = "macos"

    def setUp(self):
        super().setUp()
        self.user = self.base / "user"
        for name in (".claude", ".codex"):
            (self.user / name).mkdir(parents=True)
        self.startup = self.base / "startup"
        self.ports = FakePorts(self.OS, startup=self.startup)
        self.wheel = build_wheel(self.base / "dl")

    def kw(self, **extra):
        values = dict(home=self.h, user_home=self.user, os_name=self.OS, ports=self.ports, consent=CONSENT,
                      prereq=lambda: {"outcome": "ready", "missing": []}, components=[])
        values.update(extra)
        return values

    def install(self, wheel=None, **extra):
        return installer.install(wheel or self.wheel, **self.kw(**extra))

    def uninstall(self, **extra):
        values = self.kw(**extra)
        for name in ("consent", "prereq"):
            values.pop(name)
        return installer.uninstall(**values)

    def seed_user_files(self):
        (self.user / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"}, indent=4) + "\n", encoding="utf-8")
        (self.user / ".claude" / "CLAUDE.md").write_text("# my rules\n", encoding="utf-8")
        (self.user / ".codex" / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        (self.user / ".codex" / "AGENTS.md").write_text("mine\n", encoding="utf-8")
        (self.user / ".zprofile").write_text("export A=1\n", encoding="utf-8")


def fake_component(test, name="fakecomp", *, fail=None, licence=None, requires=None, default="on", status=None):
    """A component manifest whose entry points live in a throwaway module (contract c)."""
    calls = []

    def target(ctx):
        return Path(ctx["user_home"]) / f"{name}.marker"

    def plan(ctx):
        return [fx.effect(f"{name}.marker", name, "file-create", target(ctx), after="x")]

    def apply(ctx, effects):
        if fail == "interrupt":
            fail_once = calls.count("apply") == 0
            calls.append("apply")
            if fail_once:
                raise KeyboardInterrupt()
        calls.append("apply")
        path = target(ctx)
        path.write_bytes(b"marker\n")
        built = fx.effect(f"{name}.marker", name, "file-create", path, after=fx.sha256_bytes(b"marker\n"),
                          undo={"op": "marker-delete"})
        if fail == "error":
            exc = RuntimeError("component failed")
            exc.effects = [built]
            raise exc
        return [built]

    def verify(ctx):
        return {"ok": True}

    def remove(ctx, effects):
        removed, kept = [], []
        for item in effects:
            path = Path(item["target"])
            if path.is_file() and fx.sha256_file(path) == item["after_sha256"]:
                path.unlink()
                removed.append(item["effect_id"])
            elif not path.exists():
                removed.append(item["effect_id"])
            else:
                kept.append({"effect_id": item["effect_id"], "reason": "changed by you, kept"})
        return {"removed": removed, "kept": kept}

    module = types.ModuleType("fake_" + name)
    module.plan, module.apply, module.verify, module.remove = plan, apply, verify, remove
    module.check = (lambda ctx: licence) if licence else None
    if status is not None:
        module.status = lambda ctx: status
    sys.modules[module.__name__] = module
    test.addCleanup(sys.modules.pop, module.__name__, None)
    manifest = {"profile": "eutonos.component.v1", "id": name, "name": name, "version": "0.1", "kind": "bundled",
                "requires": requires or {}, "default": default, "permissions": [], "consent_lines": [],
                "entry": {key: f"{module.__name__}:{key}" for key in ("plan", "apply", "verify", "remove")}}
    if licence:
        manifest["licence"] = {"required": True, "check": f"{module.__name__}:check"}
    return manifest, calls


class InstallTests(LifeBase):
    def test_install_then_uninstall_leaves_user_files_byte_identical(self):
        self.seed_user_files()
        before = snapshot(self.user)
        result = self.install()
        self.assertEqual(result["outcome"], "installed", result)
        during = snapshot(self.user)
        self.assertNotEqual(during, before)
        self.assertIn(str(self.h / "bin"), during[".zprofile"].decode("utf-8"))
        self.assertIn(b"eutonos:crosstalk", during[".codex/AGENTS.md"])
        self.assertIn(b"mcp_servers.crosstalk_p0", during[".codex/config.toml"])
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(snapshot(self.user), before)
        self.assertEqual(gone["kept"], [])
        state = ir.read_state(self.layout)
        self.assertIsNone(state["active"])
        self.assertEqual(ir.read_record(self.layout, result["gen_id"])["state"], "retired")
        self.assertFalse((self.h / "runtimes" / result["artifact_id"]).exists())
        self.assertTrue(self.layout.record(result["gen_id"]).is_file())  # the record stays
        self.assertTrue(any(self.layout.backups(result["gen_id"]).iterdir()))  # backups stay

    def test_a_fresh_machine_gets_files_and_uninstall_removes_them(self):
        result = self.install()
        self.assertEqual(result["outcome"], "installed", result)
        self.assertTrue((self.user / ".claude" / "CLAUDE.md").is_file())
        self.assertTrue((self.h / "settings" / "claude-launch.json").is_file())
        self.assertTrue((self.h / "marketplace" / "crosstalk" / "QUICKSTART.md").is_file())
        hook = (self.h / "marketplace" / "crosstalk" / "plugin" / "hooks" / "register.mjs").read_text(encoding="utf-8")
        self.assertIn("eutonos-crosstalk", hook)
        self.assertNotIn("__CROSSTALK_P0", hook)
        self.assertTrue((self.h / "bin" / "eutonos-crosstalk").is_file())
        self.uninstall()
        self.assertEqual(snapshot(self.user), {})
        self.assertFalse((self.h / "bin").exists())
        self.assertFalse((self.h / "marketplace" / "crosstalk").exists())

    def test_state_record_receipts_and_checkpoint_shape(self):
        result = self.install()
        state = ir.read_state(self.layout)
        self.assertEqual(state["active"], result["gen_id"])
        self.assertIsNone(state["pending"])
        record = ir.read_record(self.layout, result["gen_id"])
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["wheel"]["artifact_id"], f"eutonos-1.2.3-{record['wheel']['sha256'][:12]}")
        self.assertEqual([item["component"] for item in record["kits"]], ["crosstalk"])
        self.assertEqual(record["consent"]["text_id"], "eutonos.consent.v1")
        for effect in record["effects"]:
            ir.check_effect(effect)
        ids = {effect["effect_id"] for effect in record["effects"]}
        self.assertTrue({"surface.bin", "surface.marketplace", "env-path.bin"} <= ids)
        self.assertTrue(any(item.startswith("runtime.") for item in ids))
        self.assertEqual(ir.done_phases(self.layout, result["gen_id"]), list(ir.PHASES))
        self.assertTrue(ir.journal_read(self.layout, state["generations"] and next(iter(
            p.stem for p in (self.h / "install" / "journal").glob("*.jsonl")))))
        checkpoint = cp.read(self.h)
        self.assertEqual(checkpoint["profile"], cp.PROFILE)
        self.assertEqual(checkpoint["checklist"], "install")
        self.assertEqual(checkpoint["checklist_revision"], installer.CHECKLIST_REVISION)
        by_id = {step["id"]: step for step in checkpoint["steps"]}
        self.assertEqual(by_id["runtime"]["state"], "done")
        self.assertEqual(by_id["consent"]["kind"], "person")
        self.assertEqual(by_id["consent"]["state"], "done")
        self.assertEqual(by_id["owner-profile"]["state"], "skipped")
        self.assertEqual(by_id["restart-apps"]["state"], "pending")
        self.assertEqual(by_id["mac-grants"]["state"], "pending")
        self.assertIn("restart-apps", result["open_steps"])

    def test_a_second_install_is_refused(self):
        self.install()
        again = self.install()
        self.assertEqual((again["outcome"], again["reason"]), ("refused", "already-installed"))

    def test_consent_and_prereq_come_first(self):
        refused = installer.install(self.wheel, **self.kw(consent=None))
        self.assertEqual(refused["outcome"], "consent-refused")
        self.assertFalse(self.layout.state.exists())
        self.assertFalse((self.h / "runtimes").exists())
        missing = installer.install(self.wheel, **self.kw(prereq=lambda: {"outcome": "missing", "missing": ["git"]}))
        self.assertEqual(missing["outcome"], "prereq-missing")
        self.assertFalse(self.layout.state.exists())

    def test_the_lock_refuses_a_second_operation(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with cp.machine_lock(self.h, wait=0):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(held.wait(10))
            self.assertEqual(self.install()["outcome"], "busy")
            self.assertEqual(self.uninstall()["outcome"], "busy")
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(self.install()["outcome"], "installed")

    def test_dry_run_gives_a_component_plan_a_real_kit_folder_and_removes_it_after(self):
        seen = []

        def plan(ctx):
            kit = Path(ctx["kit_path"])
            seen.append((kit, (kit / "crosstalk_p0_cli.py").is_file()))
            return [fx.effect("kitcomp.marker", "kitcomp", "file-create", Path(ctx["user_home"]) / "kitcomp.marker", after="x")]

        module = types.ModuleType("fake_kitcomp")
        module.plan = plan
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        manifest = {"profile": "eutonos.component.v1", "id": "kitcomp", "name": "kitcomp", "version": "0.1",
                    "kind": "bundled", "requires": {}, "default": "on", "permissions": [], "consent_lines": [],
                    "kit": "kits/crosstalk", "entry": {"plan": "fake_kitcomp:plan"}}
        result = self.install(dry_run=True, components=[manifest])
        self.assertEqual(result["outcome"], "dry-run", result)
        self.assertEqual([found for _kit, found in seen], [True])
        self.assertFalse(seen[0][0].exists())  # the scratch copy is gone
        self.assertFalse(self.h.exists())

    def test_dry_run_reports_only_the_selected_components_like_a_real_install(self):
        def plan(ctx):
            return [fx.effect("one.marker", "one", "file-create", Path(ctx["user_home"]) / "one.marker", after="x")]

        module = types.ModuleType("fake_dry_components")
        module.plan = plan
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        manifests = [{"profile": "eutonos.component.v1", "id": name, "name": name, "version": "0.1", "kind": "bundled",
                      "requires": {}, "default": "on", "permissions": [], "consent_lines": [],
                      "entry": {"plan": "fake_dry_components:plan"}} for name in ("one", "two")]

        def rows(**extra):
            plan = self.install(dry_run=True, components=manifests, **extra)
            self.assertEqual(plan["outcome"], "dry-run", plan)
            return [row["action"] for row in plan["writes"] if row["action"].startswith("component ")]

        self.assertEqual(rows(), ["component one: file-create", "component two: file-create"])
        self.assertEqual(rows(component_choices={"two": "off"}),
                         ["component one: file-create", "component two installed and left switched off"])
        self.assertEqual(rows(component_choices={"one": "off", "two": "off"}),
                         ["component one installed and left switched off", "component two installed and left switched off"])
        self.assertFalse(self.h.exists())

    def test_dry_run_writes_nothing(self):
        self.seed_user_files()
        before = snapshot(self.base)
        plan = self.install(dry_run=True)
        self.assertEqual(plan["outcome"], "dry-run")
        self.assertTrue(plan["changes_nothing"])
        phases = {row["phase"] for row in plan["writes"]}
        self.assertTrue({"runtime", "kits", "surfaces", "effects", "plugin", "activate"} <= phases)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse(self.h.exists())
        self.assertEqual(self.ports.calls, [])
        self.install()
        installed = snapshot(self.base)
        preview = self.uninstall(dry_run=True)
        self.assertEqual(preview["outcome"], "dry-run")
        self.assertTrue(preview["would_reverse"])
        self.assertEqual(snapshot(self.base), installed)

    def test_the_owner_profile_is_copied_and_removed(self):
        profile = self.base / "owner.json"
        profile.write_text(json.dumps({"profile": "eutonos.owner-profile.v1", "name": "t"}), encoding="utf-8")
        self.install(owner_profile=profile)
        self.assertEqual((self.h / "profiles" / "owner.json").read_bytes(), profile.read_bytes())
        self.uninstall()
        self.assertFalse((self.h / "profiles" / "owner.json").exists())
        bad = self.base / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.install(owner_profile=bad, home=self.base / "other-home")


class ComponentTests(LifeBase):
    def test_components_are_resolved_applied_and_reversed(self):
        good, _ = fake_component(self, "fakecomp")
        wrong_os, _ = fake_component(self, "windowsonly", requires={"os": ["windows"]})
        held, _ = fake_component(self, "heldcomp", default="held")
        off, _ = fake_component(self, "offcomp", default="off")
        licence, _ = fake_component(self, "paid", licence={"state": "missing", "detail": "no key"})
        needs, _ = fake_component(self, "needy", requires={"components": ["windowsonly"]})
        result = self.install(components=[good, wrong_os, held, off, licence, needs])
        self.assertEqual(result["outcome"], "installed", result)
        states = {item["id"]: item["state"] for item in result["components"]}
        self.assertEqual(states, {"fakecomp": "installed", "windowsonly": "skipped", "heldcomp": "held",
                                  "offcomp": "installed", "paid": "unlicensed", "needy": "skipped"})
        enabled = {item["id"]: item["enabled"] for item in result["components"]}
        self.assertEqual(enabled, {"fakecomp": True, "windowsonly": False, "heldcomp": False, "offcomp": False,
                                   "paid": False, "needy": False})
        self.assertTrue((self.user / "fakecomp.marker").is_file())
        self.assertFalse((self.user / "offcomp.marker").exists())  # installed, but switched off: nothing applied
        self.assertFalse((self.user / "paid.marker").exists())
        effect = next(item for item in ir.read_record(self.layout, result["gen_id"])["effects"]
                      if item["effect_id"] == "fakecomp.marker")
        self.assertEqual(effect["by"], "component")
        gone = self.uninstall(components=[good])
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertFalse((self.user / "fakecomp.marker").exists())

    def test_owner_profile_can_turn_a_component_off(self):
        good, _ = fake_component(self, "fakecomp")
        profile = self.base / "owner.json"
        profile.write_text(json.dumps({"profile": "eutonos.owner-profile.v1", "components": {"fakecomp": "off"}}),
                           encoding="utf-8")
        result = self.install(components=[good], owner_profile=profile)
        self.assertEqual(result["components"][0]["state"], "installed")
        self.assertFalse(result["components"][0]["enabled"])
        self.assertFalse((self.user / "fakecomp.marker").exists())

    def test_first_install_abort_reverses_its_own_effects(self):
        self.seed_user_files()
        before = snapshot(self.user)
        bad, _ = fake_component(self, "fakecomp", fail="error")
        result = self.install(components=[bad])
        self.assertEqual(result["outcome"], "rolled-back", result)
        self.assertEqual(result["phase"], "effects")
        self.assertEqual(snapshot(self.user), before)
        state = ir.read_state(self.layout)
        self.assertIsNone(state["active"])
        self.assertIsNone(state["pending"])
        gen = next(iter(state["generations"]))
        self.assertEqual(ir.read_record(self.layout, gen)["state"], "rolled-back")
        self.assertEqual(list((self.h / "runtimes").iterdir()), [])
        self.assertEqual(list((self.h / "kits").iterdir()), [])
        self.assertFalse((self.h / "bin").exists())
        again = self.install(components=[])  # a clean retry works
        self.assertEqual(again["outcome"], "installed", again)

    def test_an_interrupted_run_is_resumed_at_the_next_phase(self):
        interrupted, calls = fake_component(self, "fakecomp", fail="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            self.install(components=[interrupted])
        info = installer.status(home=self.h)
        self.assertEqual(info["active"], None)
        recovery = info["pending"]
        self.assertEqual(recovery["completed_phases"], ["runtime", "kits", "surfaces"])
        self.assertEqual(recovery["next_phase"], "effects")
        self.assertTrue(any(item["effect_id"].startswith("runtime.") for item in recovery["targets"]))
        self.assertEqual(self.install(components=[interrupted])["reason"], "pending")
        result = installer.resume(home=self.h, user_home=self.user, os_name=self.OS, ports=self.ports,
                                  components=[interrupted])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(result["recovery"]["next_phase"], "effects")
        state = ir.read_state(self.layout)
        self.assertEqual(state["active"], result["gen_id"])
        self.assertIsNone(state["pending"])
        self.assertTrue((self.user / "fakecomp.marker").is_file())
        self.uninstall(components=[interrupted])
        self.assertEqual(snapshot(self.user), {})

    def test_effects_written_before_a_crash_are_recovered_from_the_journal(self):
        interrupted, _ = fake_component(self, "fakecomp", fail="interrupt")
        with self.assertRaises(KeyboardInterrupt):
            self.install(components=[interrupted])
        state = ir.read_state(self.layout)
        gen = state["pending"]["gen_id"]
        record = ir.read_record(self.layout, gen)
        txn = state["pending"]["txn_id"]
        ghost = fx.effect("ghost", "core", "file-create", self.h / "ghost.txt", after="x")
        ir.journal_append(self.layout, txn, {"event": "effect", "effect": ghost})
        run = installer._restore_run(self.h, user_home=self.user, os_name=self.OS, ports=self.ports)
        self.assertIn("ghost", {item["effect_id"] for item in run.record["effects"]})
        self.assertNotIn("ghost", {item["effect_id"] for item in record["effects"]})


class AdoptTests(LifeBase):
    OS = "windows"

    def setUp(self):
        super().setUp()
        (self.user / ".codex" / "config.toml").write_text(LEGACY_TOML, encoding="utf-8")
        self.ports.shortcut_write(self.startup / fx.HELPER_SHORTCUT, "C:/old/python.exe", "helper.py", "C:/old", "legacy")
        self.run_value = '"C:/Py/pythonw.exe" -I -B -m eutonos.desktop_companion_runtime --tray'
        self.ports.runkeys[fx.COMPANION_RUN_VALUE] = self.run_value
        (self.h / "runtime").mkdir(parents=True)
        (self.h / "runtime" / "keep.txt").write_text("legacy runtime\n", encoding="utf-8")
        (self.h / "crosstalk-helper-src").mkdir()
        (self.h / "crosstalk-helper-src" / "keep.txt").write_text("legacy helper\n", encoding="utf-8")
        self.ports.path_value = "C:/Windows;" + str(self.h / "bin")

    def test_adopt_repoints_legacy_items_and_uninstall_restores_them(self):
        legacy_before = snapshot(self.h)
        shortcut_before = (self.startup / fx.HELPER_SHORTCUT).read_bytes()
        config_before = (self.user / ".codex" / "config.toml").read_bytes()
        result = self.install(adopt=True)
        self.assertEqual(result["outcome"], "installed", result)
        state = ir.read_state(self.layout)
        self.assertTrue(state["legacy"]["migrated"])
        self.assertTrue(state["legacy"]["inspected"]["codex_block"])
        for name in ("runtime", "crosstalk-helper-src"):  # never touched
            self.assertEqual({k: v for k, v in snapshot(self.h / name).items()},
                             {k[len(name) + 1:]: v for k, v in legacy_before.items() if k.startswith(name + "/")})
        config = (self.user / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("throwaway probe", config)
        self.assertIn("eutonos-crosstalk-mcp", config)
        self.assertIn("eutonos-crosstalk-helper", json.loads((self.startup / fx.HELPER_SHORTCUT).read_text())["target"])
        self.assertIn("eutonos-desktop-companion", self.ports.runkeys[fx.COMPANION_RUN_VALUE])
        self.assertTrue(self.ports.runkeys[fx.COMPANION_RUN_VALUE].endswith("--tray"))
        record = ir.read_record(self.layout, result["gen_id"])
        migrated = {item["effect_id"]: item for item in record["effects"] if item["effect_id"].startswith("migrate.")}
        self.assertEqual(set(migrated), {"migrate.codex-block", "migrate.startup-shortcut", "migrate.run-key"})
        self.assertTrue(all(item["adopted"] and item["repointed"] for item in migrated.values()))
        self.assertTrue(next(item for item in record["effects"] if item["effect_id"] == "env-path.bin")["adopted"])
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual((self.user / ".codex" / "config.toml").read_bytes(), config_before)
        self.assertEqual((self.startup / fx.HELPER_SHORTCUT).read_bytes(), shortcut_before)
        self.assertEqual(self.ports.runkeys[fx.COMPANION_RUN_VALUE], self.run_value)
        self.assertIn(str(self.h / "bin"), self.ports.path_value)  # adopted: it was there before
        kept = {item["effect_id"] for item in gone["kept"]}
        self.assertIn("env-path.bin", kept)

    def test_adopt_after_uninstall_repoints_the_restored_legacy_items(self):
        self.install(adopt=True)
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertFalse(ir.read_state(self.layout)["legacy"]["migrated"])
        self.assertIn("throwaway probe", (self.user / ".codex" / "config.toml").read_text(encoding="utf-8"))
        again = self.install(adopt=True)
        self.assertEqual(again["outcome"], "installed", again)
        config = (self.user / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("throwaway probe", config)
        self.assertIn("eutonos-crosstalk-mcp", tomllib.loads(config)["mcp_servers"]["crosstalk_p0"]["command"])
        self.assertIn("eutonos-crosstalk-helper", json.loads((self.startup / fx.HELPER_SHORTCUT).read_text())["target"])
        self.assertIn("eutonos-desktop-companion", self.ports.runkeys[fx.COMPANION_RUN_VALUE])

    def test_without_adopt_the_legacy_items_are_left_alone(self):
        result = self.install()
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.ports.runkeys[fx.COMPANION_RUN_VALUE], self.run_value)
        self.assertEqual(self.ports.shortcuts[str(self.startup / fx.HELPER_SHORTCUT)]["target"], "C:/old/python.exe")


class MarketplaceRepointTests(LifeBase):
    """The Claude marketplace is already registered at an earlier hand install; adopt repoints it, uninstall puts it back."""
    OS = "windows"
    OLD = "C:/old/helper-src/claude-plugin"

    def setUp(self):
        super().setUp()
        self.registry = self.user / ".claude" / "plugins" / "known_marketplaces.json"
        self.registry.parent.mkdir(parents=True)
        self.market = {fx.MARKETPLACE_NAME: {"source": {"source": "directory", "path": self.OLD}}}
        self.plugins = {fx.PLUGIN_ID}
        self.fail_on = []  # command prefixes that fail once each (the first time they run)
        self.ports.answers = self.claude
        self.write()

    def sources(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        return {name: row["source"]["path"] for name, row in data.items()}

    def write(self):
        self.registry.write_text(json.dumps(self.market), encoding="utf-8")

    def claude(self, argv):
        if argv[1:2] != ["plugin"]:
            return 0, "", ""
        command = " ".join(argv[2:])
        for prefix in list(self.fail_on):
            if command.startswith(prefix):
                self.fail_on.remove(prefix)
                return 1, "", f"fake failure: {command}"
        if argv[2:4] == ["marketplace", "remove"]:
            self.market.pop(argv[4], None)
            self.plugins.discard(fx.PLUGIN_ID)
        elif argv[2:4] == ["marketplace", "add"]:
            self.market[fx.MARKETPLACE_NAME] = {"source": {"source": "directory", "path": argv[4]}}
        elif argv[2] == "install":
            if fx.MARKETPLACE_NAME not in self.market:
                return 1, "", "marketplace not found"
            self.plugins.add(argv[3])
        elif argv[2] == "uninstall":
            self.plugins.discard(argv[3])
        self.write()
        return 0, "", ""

    def claude_calls(self):
        return [" ".join(call[2:]) for call in self.ports.calls if call[1:2] == ["plugin"]]

    def ours(self):
        return str(self.layout.marketplace / "crosstalk")

    def plugin_receipt(self, gen_id):
        return json.loads((self.layout.receipts(gen_id) / "05-plugin.json").read_text(encoding="utf-8"))

    def app_commands(self, gen_id):
        return [item["effect_id"] for item in ir.read_record(self.layout, gen_id)["effects"] if item["kind"] == "app-command"]

    def test_adopt_repoints_the_marketplace_and_records_a_migrate_effect(self):
        result = self.install(adopt=True)
        self.assertEqual(result["outcome"], "installed", result)
        self.assertTrue(fx._same_entry(self.sources()[fx.MARKETPLACE_NAME], self.ours()))
        self.assertIn(fx.PLUGIN_ID, self.plugins)
        self.assertEqual(self.claude_calls(), [f"marketplace remove {fx.MARKETPLACE_NAME}", f"marketplace add {self.ours()}",
                                               f"install {fx.PLUGIN_ID}"])
        self.assertEqual(self.app_commands(result["gen_id"]), ["migrate.marketplace", "plugin.marketplace", "plugin.install"])
        record = ir.read_record(self.layout, result["gen_id"])
        migrate = next(item for item in record["effects"] if item["effect_id"] == "migrate.marketplace")
        self.assertTrue(migrate["adopted"] and migrate["repointed"])
        self.assertEqual(migrate["component"], "crosstalk")
        self.assertEqual(migrate["undo"]["op"], "app-command")
        self.assertEqual(migrate["undo"]["argvs"], [["/fake/claude", "plugin", "marketplace", "add", self.OLD],
                                                    ["/fake/claude", "plugin", "install", fx.PLUGIN_ID]])
        self.assertEqual(self.plugin_receipt(result["gen_id"])["plugin"], "registered")

    def test_uninstall_restores_the_old_source_and_reinstalls_the_plugin_in_order(self):
        self.install(adopt=True)
        self.ports.calls.clear()
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(self.claude_calls(), [f"uninstall {fx.PLUGIN_ID}", f"marketplace remove {fx.MARKETPLACE_NAME}",
                                               f"marketplace add {self.OLD}", f"install {fx.PLUGIN_ID}"])
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        self.assertIn(fx.PLUGIN_ID, self.plugins)
        self.assertIn("migrate.marketplace", gone["removed"])

    def test_a_failed_add_after_the_remove_puts_the_old_marketplace_and_plugin_back(self):
        self.fail_on = ["marketplace add " + self.ours()]
        result = self.install(adopt=True)
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        self.assertIn(fx.PLUGIN_ID, self.plugins)
        receipt = self.plugin_receipt(result["gen_id"])
        self.assertEqual(receipt["plugin"], "skipped")
        self.assertIn("put back", receipt["reason"])
        self.assertEqual(self.app_commands(result["gen_id"]), [])
        self.assertEqual(self.claude_calls()[-2:], [f"marketplace add {self.OLD}", f"install {fx.PLUGIN_ID}"])

    def test_a_failed_install_after_the_add_undoes_ours_and_restores_the_old_one(self):
        self.fail_on = ["install " + fx.PLUGIN_ID]  # fails once: the new install; the restore's install then succeeds
        result = self.install(adopt=True)
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        self.assertIn(fx.PLUGIN_ID, self.plugins)
        self.assertEqual(self.plugin_receipt(result["gen_id"])["plugin"], "skipped")
        self.assertEqual(self.app_commands(result["gen_id"]), [])
        self.assertEqual(self.claude_calls()[-3:], [f"marketplace remove {fx.MARKETPLACE_NAME}", f"marketplace add {self.OLD}",
                                                    f"install {fx.PLUGIN_ID}"])
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})

    def test_a_failed_restore_stays_recorded_so_uninstall_retries_it(self):
        self.fail_on = ["marketplace add " + self.ours()]
        original = self.ports.answers

        def broken_restore(argv):
            if " ".join(argv[2:]) == f"marketplace add {self.OLD}" and self.fail_on == []:
                self.fail_on.append("never")
                return 1, "", "fake failure: restore"
            return original(argv)

        self.ports.answers = broken_restore
        result = self.install(adopt=True)
        self.assertEqual(result["outcome"], "installed", result)
        self.assertIn("did not finish", self.plugin_receipt(result["gen_id"])["reason"])
        self.assertEqual(self.app_commands(result["gen_id"]), ["migrate.marketplace"])
        self.ports.answers = original
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        self.assertIn(fx.PLUGIN_ID, self.plugins)

    def test_a_failed_remove_changes_nothing(self):
        self.fail_on = ["marketplace remove"]
        result = self.install(adopt=True)
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.claude_calls(), [f"marketplace remove {fx.MARKETPLACE_NAME}"])
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        self.assertEqual(self.app_commands(result["gen_id"]), [])
        receipt = self.plugin_receipt(result["gen_id"])
        self.assertEqual(receipt["plugin"], "skipped")
        self.assertIn("nothing was changed", receipt["reason"])

    def test_without_adopt_the_registration_is_left_alone(self):
        result = self.install()
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        self.assertEqual(self.plugin_receipt(result["gen_id"])["plugin"], "skipped")

    def test_the_dry_run_plan_lists_the_repoint_only_in_adopt_mode(self):
        plan = self.install(dry_run=True, adopt=True)
        rows = [row for row in plan["writes"] if row.get("item") == "migrate.marketplace"]
        self.assertEqual(len(rows), 1, plan["writes"])
        self.assertEqual(rows[0]["target"], self.OLD)
        self.assertEqual(self.claude_calls(), [])
        plain = self.install(dry_run=True)
        self.assertEqual([row for row in plain["writes"] if row.get("item") == "migrate.marketplace"], [])

    def test_adopt_uninstall_adopt_repoints_again(self):
        self.install(adopt=True)
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.sources(), {fx.MARKETPLACE_NAME: self.OLD})
        again = self.install(adopt=True)
        self.assertEqual(again["outcome"], "installed", again)
        self.assertTrue(fx._same_entry(self.sources()[fx.MARKETPLACE_NAME], self.ours()))
        self.assertIn("migrate.marketplace", self.app_commands(again["gen_id"]))


class MissingClaudeCommandTests(LifeBase):
    """A Claude folder without the claude command: the plugin cannot be registered, and the person is told."""
    OS = "windows"

    def pending(self):
        from eutonos import pending_actions
        return [item for item in pending_actions.list_actions(self.h) if item["source"] == installer.PLUGIN_SOURCE]

    def test_a_missing_claude_command_leaves_one_pending_step_and_a_later_install_clears_it(self):
        self.ports.has.pop("claude")
        result = self.install()
        self.assertEqual(result["outcome"], "installed", result)
        receipt = json.loads((self.layout.receipts(result["gen_id"]) / "05-plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["plugin"], receipt["reason"]), ("skipped", "claude was not found on PATH"))
        found = self.pending()
        self.assertEqual(len(found), 1, found)
        self.assertIn("claude command was not found", found[0]["action"])
        self.assertIn(fx.PLUGIN_ID, found[0]["action"])
        self.assertIn("marketplace add", found[0]["action"])
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(len(self.pending()), 1)  # the step stays until a run that finds the command
        self.ports.has["claude"] = "/fake/claude"
        self.assertEqual(self.install()["outcome"], "installed")
        self.assertEqual(self.pending(), [])

    def test_a_present_claude_command_leaves_no_pending_step(self):
        self.install()
        self.assertEqual(self.pending(), [])


class ProjectGuardTests(LifeBase):
    def test_uninstall_refuses_while_a_project_is_registered(self):
        self.install()
        (self.h / "projects.json").write_text(json.dumps(
            {"profile": "eutonos.projects.v1", "projects": [{"root": "C:/work/app"}]}), encoding="utf-8")
        before = snapshot(self.base)
        refused = self.uninstall()
        self.assertEqual((refused["outcome"], refused["reason"]), ("refused", "projects-registered"))
        self.assertIn("remove-project", refused["next_action"])
        self.assertIn("--keep-projects", refused["next_action"])
        self.assertEqual(snapshot(self.base), before)
        self.assertIsNotNone(ir.read_state(self.layout)["active"])
        kept = self.uninstall(keep_projects=True)
        self.assertEqual(kept["outcome"], "uninstalled", kept)
        self.assertEqual(kept["kept_projects"], ["C:/work/app"])
        self.assertEqual(ir.read_record(self.layout, ir.read_state(self.layout)["generations"] and kept["gen_id"])["kept_projects"],
                         ["C:/work/app"])
        self.assertTrue((self.h / "projects.json").is_file())  # never deleted

    def test_a_damaged_projects_file_counts_as_registered(self):
        self.install()
        (self.h / "projects.json").write_text("{nope", encoding="utf-8")
        self.assertEqual(self.uninstall()["reason"], "projects-registered")

    def test_changed_by_you_is_kept_and_reported(self):
        self.install()
        agents = self.user / ".codex" / "AGENTS.md"
        text = agents.read_text(encoding="utf-8").replace("Completion contract", "Completion contract (edited)")
        agents.write_text(text, encoding="utf-8")
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled")
        self.assertIn("edited", agents.read_text(encoding="utf-8"))
        self.assertTrue(any("instructions.completion" in item["effect_id"] for item in gone["kept"]))
        self.assertTrue(any(item["id"] == "restart-apps" for item in gone["person_reversals"]))
        checkpoint = cp.read(self.h)
        self.assertEqual(checkpoint["checklist"], "uninstall")
        self.assertIn("undo-restart-apps", {step["id"] for step in checkpoint["steps"]})


class WindowsPathTests(LifeBase):
    OS = "windows"

    def test_the_user_path_gains_the_bin_folder_and_loses_it_again(self):
        before = self.ports.path_value
        self.install()
        self.assertEqual(self.ports.path_value, before + ";" + str(self.h / "bin"))
        self.assertTrue((self.h / "bin" / "eutonos-crosstalk.exe").is_file())
        self.uninstall()
        self.assertEqual(self.ports.path_value, before)
        self.assertFalse((self.h / "bin").exists())

    def _round_trip(self, original, kind):
        """Install over this user PATH and uninstall it again; returns what the machine held in between."""
        self.ports.path_value, self.ports.path_kind = original, kind
        self.assertEqual(self.install()["outcome"], "installed")
        between = (self.ports.path_value, self.ports.path_kind)
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        return between

    def test_a_path_that_did_not_exist_is_deleted_again_not_left_empty(self):
        between = self._round_trip(None, None)
        self.assertEqual(between, (str(self.h / "bin"), 2))  # a new value is the type Windows itself uses
        self.assertIsNone(self.ports.path_value)
        self.assertIsNone(self.ports.user_path_state())

    def test_empty_entries_and_a_trailing_separator_come_back_exactly(self):
        for original in (r"C:\A;;C:\B", r"C:\A;;C:\B;", ";C:\\A", "", r"C:\A;C:\B;;"):
            with self.subTest(original=original):
                bin_dir = str(self.h / "bin")
                between, _ = self._round_trip(original, 2)
                self.assertIn(bin_dir, between.split(";"))
                self.assertTrue(between.startswith(original), between)  # nothing of the original was collapsed
                self.assertEqual(self.ports.path_value, original)
                self.assertEqual(self.ports.path_kind, 2)

    def test_a_plain_string_path_stays_a_plain_string(self):
        for kind in (1, 2):
            with self.subTest(kind=kind):
                between = self._round_trip(r"C:\Windows;%SystemRoot%", kind)
                self.assertEqual(between[1], kind)
                self.assertEqual((self.ports.path_value, self.ports.path_kind), (r"C:\Windows;%SystemRoot%", kind))

    def test_an_entry_the_person_added_after_the_install_survives_the_removal(self):
        self.ports.path_value, self.ports.path_kind = None, None  # EUTONOS creates the value...
        self.install()
        self.ports.path_value += r";C:\Later"  # ...and the person adds to it
        self.uninstall()
        self.assertEqual((self.ports.path_value, self.ports.path_kind), (r"C:\Later", 2))
        self.ports.path_value, self.ports.path_kind = r"C:\A;", 1
        self.install()
        self.ports.path_value += r";C:\Later"
        self.uninstall()
        self.assertEqual((self.ports.path_value, self.ports.path_kind), (r"C:\A;C:\Later", 1))

    def test_a_record_from_before_these_fields_still_removes_only_its_entry(self):
        from eutonos import installer_effects
        self.ports.path_value, self.ports.path_kind = r"C:\A;;C:\B;C:\bin;", 1
        remover = installer_effects.Remover(self.layout, self.ports, self.user, "windows")
        state, _ = remover.one({"undo": {"op": "path-remove", "entry": r"C:\bin"}})
        self.assertEqual((state, self.ports.path_value, self.ports.path_kind), ("removed", r"C:\A;;C:\B;", 1))
        self.assertEqual(remover.one({"undo": {"op": "path-remove", "entry": r"C:\bin"}})[0], "gone")

    def test_the_codex_block_names_the_stable_launcher(self):
        self.install()
        config = (self.user / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("eutonos-crosstalk-mcp.exe", config)
        self.assertIn(str(self.h / "bin").replace("\\", "\\\\"), config)
        self.assertNotIn("python", config.lower())


class ContextTests(LifeBase):
    def test_a_component_receives_the_contract_c_context(self):
        seen = {}
        manifest, _ = fake_component(self, "fakecomp")
        original = sys.modules["fake_fakecomp"].apply

        def spy(ctx, effects):
            seen.update(ctx)
            return original(ctx, effects)

        sys.modules["fake_fakecomp"].apply = spy
        self.install(components=[manifest])
        for key in ("home", "os", "gen_id", "runtime_python", "kit_path", "projects", "record", "dry_run", "now"):
            self.assertIn(key, seen)
        self.assertEqual(seen["home"], self.h)
        self.assertFalse(seen["dry_run"])
        self.assertEqual(seen["os"], "macos")
        self.assertTrue(str(seen["runtime_python"]).startswith(str(self.h / "runtimes")))
        self.assertEqual(seen["record"]["state"], "prepared")

    def test_ctx_carries_seams_and_component_choices(self):
        seen = {}
        manifest, _ = fake_component(self, "fakecomp")
        original = sys.modules["fake_fakecomp"].apply

        def spy(ctx, effects):
            seen.update(ctx)
            return original(ctx, effects)

        sys.modules["fake_fakecomp"].apply = spy
        self.ports.seam_map["claude_exe"] = "fake-claude"
        self.install(components=[manifest], component_choices={"fakecomp": "on"})
        self.assertEqual(seen["seams"], {"claude_exe": "fake-claude"})
        self.assertEqual(seen["component_choices"], {"fakecomp": "on"})

    def test_a_component_status_can_hold_or_skip_it(self):
        held, _ = fake_component(self, "heldbystatus", status={"state": "held", "reason": "gate closed"})
        skipped, _ = fake_component(self, "skippedbystatus", status={"state": "skipped", "reason": "no app"})
        fine, _ = fake_component(self, "finebystatus", status={"state": "installed", "reason": ""})
        result = self.install(components=[held, skipped, fine])
        rows = {item["id"]: item for item in result["components"]}
        self.assertEqual({key: value["state"] for key, value in rows.items()},
                         {"heldbystatus": "held", "skippedbystatus": "skipped", "finebystatus": "installed"})
        self.assertEqual(rows["heldbystatus"]["reason"], "gate closed")
        self.assertFalse((self.user / "heldbystatus.marker").exists())
        self.assertTrue((self.user / "finebystatus.marker").is_file())

    def test_a_broken_status_skips_the_component_without_failing_the_install(self):
        manifest, _ = fake_component(self, "brokenstatus")

        def boom(ctx):
            raise RuntimeError("no")

        sys.modules["fake_brokenstatus"].status = boom
        result = self.install(components=[manifest])
        self.assertEqual(result["outcome"], "installed", result)
        self.assertEqual(result["components"][0]["state"], "skipped")
        self.assertIn("status failed", result["components"][0]["reason"])


class MarketplaceSourceTests(unittest.TestCase):
    def test_a_registered_source_may_be_claudes_object_or_a_plain_path_or_unknown(self):
        self.assertEqual(installer._source_path({"source": {"source": "directory", "path": "/m/crosstalk"}}), "/m/crosstalk")
        self.assertEqual(installer._source_path({"source": "/m/crosstalk"}), "/m/crosstalk")
        for odd in (None, {}, {"source": None}, {"source": 3}, "text", {"source": {"url": "x"}}):
            self.assertEqual(installer._source_path(odd), "", odd)


class StagingHomeTests(Base):
    def test_a_staged_home_never_defaults_to_the_real_user_home(self):
        self.assertEqual(installer.default_user_home(self.h), self.h / "staging-user")
        self.assertIsNone(installer.default_user_home(Path.home() / ".eutonos"))

    def test_install_without_user_home_stays_inside_the_staged_home(self):
        ports = FakePorts("macos", startup=self.base / "startup")
        wheel = build_wheel(self.base / "dl")
        result = installer.install(wheel, home=self.h, os_name="macos", ports=ports, consent=CONSENT,
                                   prereq=lambda: {"outcome": "ready", "missing": []}, components=[])
        self.assertEqual(result["outcome"], "installed", result)
        record = ir.read_record(self.layout, result["gen_id"])
        for item in record["effects"]:
            if item["effect_id"].startswith(("runtime.", "kit.")) or not Path(item["target"]).is_absolute():
                continue  # private runtime effects and command-style targets (plugin registration) are not paths
            target = Path(item["target"]).resolve()
            self.assertTrue(str(target).startswith(str(self.h.resolve())), item["target"])


class ComponentUndoTests(LifeBase):
    def test_a_registered_undo_handler_reverses_an_effect_whose_component_is_gone(self):
        calls = []
        support = types.ModuleType("eutonos.component_support")

        def remove_effects(ctx, effects):
            calls.append((ctx["home"], [item["effect_id"] for item in effects]))
            return {"removed": [item["effect_id"] for item in effects], "kept": []}

        support.UNDO_HANDLERS = {"fake-undo": object()}
        support.remove_effects = remove_effects
        with unittest.mock.patch.dict(sys.modules, {"eutonos.component_support": support}),                 unittest.mock.patch("eutonos.component_support", support, create=True):
            remover = fx.Remover(self.layout, self.ports, self.user, "macos")
            item = fx.effect("odd", "gone", "state", self.user / "x", undo={"op": "fake-undo"})
            self.assertEqual(remover.one(item), ("removed", None))
            other = fx.effect("odd2", "gone", "state", self.user / "y", undo={"op": "nobody-knows"})
            self.assertEqual(remover.one(other)[0], "kept")
        self.assertEqual(calls, [(self.h, ["odd"])])


# ------------------------------------------------------------------ update and rollback
from eutonos import release_source as rs


def make_release(folder, name, *, version, body, channels=("dev",), min_compatible=None, scripts=None, sha=None,
                 existing=None):
    """A release folder: the wheel and its ``eutonos-release.json``. Returns the manifest. `existing` reuses a wheel."""
    where = Path(folder) / name
    extra = {"scripts": scripts} if scripts else {}
    if existing:
        where.mkdir(parents=True, exist_ok=True)
        wheel = where / Path(existing).name
        wheel.write_bytes(Path(existing).read_bytes())
    else:
        wheel = build_wheel(where, version=version, body=body, **extra)
    digest = ir.sha256_file(wheel)
    manifest = {"profile": "eutonos.release.v1", "version": version, "artifact_id": ir.artifact_id(version, digest),
                "wheel": wheel.name, "sha256": sha or digest, "channels": list(channels), "min_compatible": min_compatible}
    (where / "eutonos-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def settled(folder) -> dict:
    """Every file under `folder` except lock files, for before/after comparisons."""
    return {name: data for name, data in snapshot(folder).items() if "lock" not in name}


class UpdateBase(LifeBase):
    def setUp(self):
        super().setUp()
        self.releases = self.base / "releases"
        self.doctor_calls = []

    def doctor(self, argv):
        self.doctor_calls.append(argv)
        return 0

    def update(self, **extra):
        values = self.kw(handover=False, source=self.releases, doctor=self.doctor, allow_unsigned=True)  # unsigned folder
        for name in ("consent", "prereq"):
            values.pop(name)
        values.update(extra)
        return installer.update(**values)

    def rollback(self, **extra):
        values = self.kw()
        for name in ("consent", "prereq"):
            values.pop(name)
        values.update(extra)
        return installer.rollback(**values)

    def first(self, **extra):
        result = self.install(**extra)
        self.assertEqual(result["outcome"], "installed", result)
        return result

    def launcher(self, name="eutonos-crosstalk"):
        return (self.h / "bin" / name).read_text(encoding="utf-8")


class ReleaseSourceTests(Base):
    def manifest(self, **change):
        value = {"profile": "eutonos.release.v1", "version": "1.0.0", "artifact_id": "eutonos-1.0.0-abcdef012345",
                 "wheel": "eutonos-1.0.0-py3-none-any.whl", "sha256": "a" * 64, "channels": ["stable"],
                 "min_compatible": None}
        value.update(change)
        return value

    def test_the_manifest_is_validated(self):
        self.assertEqual(rs.validate_manifest(self.manifest())["channels"], ["stable"])
        for bad in (self.manifest(profile="x"), self.manifest(sha256="zz"), self.manifest(channels=[]),
                    self.manifest(channels=["nightly"]), self.manifest(wheel="../evil.whl"),
                    self.manifest(wheel="a/b.whl"), self.manifest(artifact_id="nope"), self.manifest(version="")):
            with self.assertRaises(ts.ReadError):
                rs.validate_manifest(bad)

    def test_newest_picks_the_highest_version_naming_the_channel(self):
        releases = [{"id": name, "manifest": rs.validate_manifest(self.manifest(version=version, channels=channels)),
                     "published": published}
                    for name, version, channels, published in
                    (("a", "1.2.0", ["stable"], "2026-01-01"), ("b", "1.10.0", ["stable", "beta"], "2026-02-01"),
                     ("c", "2.0.0", ["dev"], "2026-03-01"), ("d", "1.10.0", ["stable"], "2026-04-01"))]
        self.assertEqual(rs.newest(releases, "stable")["id"], "d")  # same version: the later publication
        self.assertEqual(rs.newest(releases, "dev")["id"], "c")
        self.assertEqual(rs.newest(releases, "beta")["id"], "b")
        with self.assertRaises(ts.ReadError):
            rs.newest(releases, "nightly")

    def test_a_folder_source_skips_damaged_releases(self):
        make_release(self.base, "good", version="1.0.0", body="a")
        (self.base / "broken").mkdir()
        (self.base / "broken" / "eutonos-release.json").write_text("{nope", encoding="utf-8")
        (self.base / "empty").mkdir()
        found = rs.FolderSource(self.base, allow_unsigned=True).releases()
        self.assertEqual([item["id"] for item in found], ["good"])
        target = rs.FolderSource(self.base, allow_unsigned=True).fetch_wheel(found[0], self.base / "out")
        self.assertEqual(target.read_bytes(), (self.base / "good" / found[0]["manifest"]["wheel"]).read_bytes())


class UpdateTests(UpdateBase):
    def test_update_from_a_local_release_folder(self):
        old = self.first()
        manifest = make_release(self.releases, "r2", version="1.3.0", body="two", min_compatible="1.0.0")
        result = self.update()
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual((result["version"], result["artifact_id"]), ("1.3.0", manifest["artifact_id"]))
        state = ir.read_state(self.layout)
        self.assertEqual(state["active"], result["gen_id"])
        new = ir.read_record(self.layout, result["gen_id"])
        self.assertEqual((new["state"], new["previous_gen"], new["min_compatible"]), ("active", old["gen_id"], "1.0.0"))
        self.assertEqual(ir.read_record(self.layout, old["gen_id"])["state"], "retired")
        self.assertTrue((self.h / "runtimes" / old["artifact_id"]).is_dir())  # two generations are kept
        self.assertTrue((self.h / "runtimes" / manifest["artifact_id"]).is_dir())
        self.assertIn(manifest["artifact_id"], self.launcher())
        self.assertEqual(ir.done_phases(self.layout, result["gen_id"]), list(ir.PHASES))
        self.assertEqual(self.doctor_calls, [["--home", str(self.h), "--user-home", str(self.user)]])
        self.assertEqual(result["doctor"]["ran"], True)
        checkpoint = cp.read(self.h)
        steps = {step["id"]: step for step in checkpoint["steps"]}
        self.assertEqual((steps["prereq"]["state"], steps["consent"]["state"]), ("skipped", "skipped"))
        self.assertEqual(ir.record_intact(self.layout, state, result["gen_id"]), True)

    def test_update_keeps_the_first_installs_backups_and_uninstall_restores_the_user_files(self):
        self.seed_user_files()
        before = snapshot(self.user)
        old = self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        result = self.update()
        self.assertEqual(result["outcome"], "updated", result)
        new = ir.read_record(self.layout, result["gen_id"])
        blocks = {item["effect_id"]: item for item in new["effects"] if item["effect_id"].startswith("instructions.")}
        first_blocks = {item["effect_id"]: item for item in ir.read_record(self.layout, old["gen_id"])["effects"]
                        if item["effect_id"].startswith("instructions.")}
        self.assertEqual(set(blocks), set(first_blocks))
        for name, item in blocks.items():
            self.assertEqual(item["backup"], first_blocks[name]["backup"])
            self.assertEqual(item["before_sha256"], first_blocks[name]["before_sha256"])
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(snapshot(self.user), before)
        self.assertEqual(gone["kept"], [])
        self.assertEqual(sorted(item.name for item in (self.h / "runtimes").iterdir()), [])  # both runtimes removed

    def test_up_to_date_and_no_release(self):
        old = self.first()
        self.assertEqual(self.update()["outcome"], "no-release")
        make_release(self.releases, "same", version="1.2.3", body="one", existing=self.wheel)
        same = self.update()
        self.assertEqual(same["outcome"], "up-to-date", same)
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])
        make_release(self.releases, "elsewhere", version="9.0.0", body="x", channels=("stable",))
        self.assertEqual(self.update(channel="beta")["outcome"], "no-release")  # no release names beta

    def test_a_sha256_mismatch_is_refused_before_any_write(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two", sha="0" * 64)
        home_before, user_before = settled(self.h), snapshot(self.user)
        result = self.update()
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "sha256-mismatch"), result)
        self.assertEqual(settled(self.h), home_before)
        self.assertEqual(snapshot(self.user), user_before)

    def test_a_manifest_that_names_another_artifact_is_refused(self):
        self.first()
        manifest = make_release(self.releases, "r2", version="1.3.0", body="two")
        manifest["artifact_id"] = "eutonos-1.3.0-000000000000"
        (self.releases / "r2" / "eutonos-release.json").write_text(json.dumps(manifest), encoding="utf-8")
        before = settled(self.h)
        result = self.update()
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "artifact-mismatch"), result)
        self.assertEqual(settled(self.h), before)

    def test_dry_run_reports_the_release_and_changes_nothing(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        before = settled(self.h)
        result = self.update(dry_run=True)
        self.assertEqual((result["outcome"], result["would_install"]["version"]), ("dry-run", "1.3.0"))
        self.assertEqual(settled(self.h), before)

    def test_update_needs_an_install_and_refuses_while_pending(self):
        make_release(self.releases, "r2", version="1.3.0", body="two")
        self.assertEqual(self.update()["reason"], "not-installed")
        self.first()
        state = ir.read_state(self.layout)
        state["pending"] = {"txn_id": "t", "gen_id": "g" * 16, "kind": "update", "phase": "effects", "params": {}}
        ir.write_state(self.layout, state)
        self.assertEqual(self.update()["reason"], "pending")

    def test_a_user_changed_launcher_is_kept_and_listed(self):
        self.first()
        mine = self.h / "bin" / "eutonos-crosstalk"
        mine.write_bytes(b"my own launcher\n")
        make_release(self.releases, "r2", version="1.3.0", body="two")
        result = self.update()
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(mine.read_bytes(), b"my own launcher\n")
        self.assertIn(result["artifact_id"], self.launcher("eutonos-install"))  # the other launchers moved on
        rows = [item for item in result["three_way"] if item["block"] == "bin"]
        self.assertEqual([item["target"] for item in rows], [str(mine)])
        self.assertEqual(rows[0]["current_sha256"], fx.sha256_bytes(b"my own launcher\n"))
        self.assertNotEqual(rows[0]["old_shipped_sha256"], rows[0]["new_shipped_sha256"])
        self.assertIn("three-way", result["open_steps"])
        self.assertIn(rows[0], ir.read_record(self.layout, result["gen_id"])["three_way"])
        gone = self.uninstall()
        self.assertEqual(mine.read_bytes(), b"my own launcher\n")  # uninstall keeps it as well
        self.assertTrue(any("changed by you" in item["reason"] for item in gone["kept"]), gone)

    def test_a_customized_instruction_block_is_kept_and_listed(self):
        self.first()
        agents = self.user / ".codex" / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        marker = "## Completion contract"
        self.assertIn(marker, text)
        agents.write_text(text.replace(marker, marker + "\n\nMY EXTRA RULE", 1), encoding="utf-8")
        edited = agents.read_bytes()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        result = self.update()
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(agents.read_bytes(), edited)
        rows = [item for item in result["three_way"] if item["target"] == str(agents)]
        self.assertEqual(len(rows), 1, result["three_way"])
        self.assertEqual(rows[0]["current_sha256"], fx.sha256_bytes(edited))
        self.assertTrue(rows[0]["old_shipped_sha256"] and rows[0]["new_shipped_sha256"])

    def test_a_launcher_the_new_release_drops_is_removed_while_unchanged(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two", scripts=("eutonos-crosstalk", "eutonos-crosstalk-mcp"))
        result = self.update()
        self.assertEqual(result["outcome"], "updated", result)
        self.assertFalse((self.h / "bin" / "eutonos-install").exists())
        self.assertTrue((self.h / "bin" / "eutonos-crosstalk").is_file())
        receipt = json.loads((self.layout.receipts(result["gen_id"]) / "03-surfaces.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["stale_removed"], ["eutonos-install"])

    def test_a_dropped_launcher_you_changed_is_kept(self):
        self.first()
        mine = self.h / "bin" / "eutonos-install"
        mine.write_bytes(b"edited\n")
        make_release(self.releases, "r2", version="1.3.0", body="two", scripts=("eutonos-crosstalk", "eutonos-crosstalk-mcp"))
        self.assertEqual(self.update()["outcome"], "updated")
        self.assertEqual(mine.read_bytes(), b"edited\n")

    def test_a_failed_update_leaves_the_earlier_generation_active(self):
        manifest, _ = fake_component(self, "fakecomp")
        old = self.first(components=[manifest])
        good = sys.modules["fake_fakecomp"].apply

        def broken(ctx, effects):
            raise RuntimeError("component failed on update")

        sys.modules["fake_fakecomp"].apply = broken
        make_release(self.releases, "r2", version="1.3.0", body="two")
        launcher_before = self.launcher()
        result = self.update(components=[manifest])
        self.assertEqual(result["outcome"], "rolled-back", result)
        self.assertEqual(result["phase"], "effects")
        state = ir.read_state(self.layout)
        self.assertEqual((state["active"], state["pending"]), (old["gen_id"], None))
        self.assertEqual(ir.read_record(self.layout, result["gen_id"])["state"], "rolled-back")
        self.assertEqual(ir.read_record(self.layout, old["gen_id"])["state"], "active")
        self.assertEqual(self.launcher(), launcher_before)  # H/bin points at the earlier runtime again
        new_artifact = ir.artifact_id("1.3.0", ir.sha256_file(self.releases / "r2" / "eutonos-1.3.0-py3-none-any.whl"))
        self.assertFalse((self.h / "runtimes" / new_artifact).exists())
        self.assertTrue((self.h / "runtimes" / old["artifact_id"]).is_dir())
        self.assertEqual(ir.done_phases(self.layout, result["gen_id"]).count("activate"), 0)
        self.assertTrue((self.layout.receipts(result["gen_id"]) / "95-abort.json").is_file())
        sys.modules["fake_fakecomp"].apply = good  # the cause is fixed: the next update goes through
        self.assertEqual(self.update(components=[manifest])["outcome"], "updated")

    def test_channels(self):
        self.first()
        make_release(self.releases, "dev2", version="1.3.0", body="dev", channels=("dev",))
        make_release(self.releases, "stable1", version="1.1.0", body="stable", channels=("stable",))
        refused = self.update(channel="stable")  # moving to stable means an older version: that is allowed on a switch
        self.assertEqual(refused["outcome"], "updated", refused)
        self.assertEqual(ir.read_state(self.layout)["channel"], "stable")
        self.assertEqual(refused["version"], "1.1.0")
        make_release(self.releases, "stable0", version="1.0.0", body="old", channels=("stable",))
        self.assertEqual(self.update()["outcome"], "up-to-date")  # stable's newest is already installed
        make_release(self.releases, "stable2", version="1.0.5", body="older", channels=("stable",))
        self.assertEqual(self.update()["outcome"], "up-to-date")
        make_release(self.releases, "stable3", version="1.2.0", body="newer", channels=("stable",))
        self.assertEqual(self.update()["outcome"], "updated")

    def test_an_older_release_without_a_channel_switch_is_refused(self):
        self.first()
        make_release(self.releases, "old", version="1.0.0", body="old", channels=("dev",))
        result = self.update()
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "older-than-installed"), result)

    def test_a_busy_machine_is_refused(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        held, release, answer = threading.Event(), threading.Event(), {}

        def holder():
            with cp.machine_lock(self.h, wait=0):
                held.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(held.wait(10))
            answer["result"] = self.update()
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(answer["result"]["outcome"], "busy")

    def test_an_unreachable_source_changes_nothing(self):
        self.first()
        before = settled(self.h)
        def offline(url, headers):
            raise OSError("no network")

        source = rs.GitHubSource("Owner/Repo", opener=offline)
        result = self.update(source=source)
        self.assertEqual((result["outcome"], result["reason"]), ("unavailable", "unavailable"), result)
        self.assertEqual(settled(self.h), before)

    def test_doctor_is_a_lazy_import(self):
        original = installer.importlib.import_module

        def missing(name, package=None):
            raise ImportError("no doctor", name="eutonos.doctor")

        installer.importlib.import_module = missing
        try:
            self.assertEqual(installer.run_doctor(), {"ran": False, "reason": "doctor not built yet"})
        finally:
            installer.importlib.import_module = original
        self.assertEqual(installer.run_doctor(lambda argv: print("all good") or 0)["output"], "all good\n")

    def test_doctor_is_pointed_at_this_installs_home_and_user_home(self):
        seen = []
        installer.run_doctor(lambda argv: seen.append(list(argv)) or 0, home=self.h, user_home=self.user)
        self.assertEqual(seen, [["--home", str(self.h), "--user-home", str(self.user)]])
        installer.run_doctor(lambda argv: seen.append(list(argv)) or 0)
        self.assertEqual(seen[-1], [])


class HandoverTests(UpdateBase):
    def child(self, **extra):
        """A stand-in for the new runtime's process: the same library, called with the token it was given."""
        calls = []

        def run_child(python, gen_id, home, token):
            calls.append((python, gen_id, token))
            values = self.kw(doctor=lambda argv: 0, **extra)
            for name in ("consent", "prereq"):
                values.pop(name)
            result = installer.continue_handover(gen_id=gen_id, token=token, **values)
            return 0, json.dumps(result), ""

        self.ports.handover_call = run_child
        return calls

    def test_the_new_runtime_finishes_the_update(self):
        old = self.first()
        manifest = make_release(self.releases, "r2", version="1.3.0", body="two")
        calls = self.child()
        result = self.update(handover=True)
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(len(calls), 1)
        python, gen_id, token = calls[0]
        self.assertEqual(gen_id, result["gen_id"])
        self.assertTrue(token)
        self.assertEqual(Path(python), self.ports.runtime_python(self.h / "runtimes" / manifest["artifact_id"]))
        self.assertEqual(self.ports.handovers[0][2], str(self.h))
        self.assertEqual(result["doctor"]["ran"], True)  # the new runtime ran doctor
        self.assertEqual(self.doctor_calls, [])  # not this process
        state = ir.read_state(self.layout)
        self.assertEqual((state["active"], state["pending"]), (result["gen_id"], None))
        self.assertEqual(ir.read_record(self.layout, old["gen_id"])["state"], "retired")
        self.assertEqual(ir.done_phases(self.layout, result["gen_id"]), list(ir.PHASES))
        self.assertEqual(cp.read(self.h)["checklist"], "install")

    def test_only_the_runtime_and_kits_phases_run_before_the_handover(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        seen = {}

        def look(python, gen_id, home, token):
            seen["done"] = ir.done_phases(self.layout, gen_id)
            seen["pending"] = ir.read_state(self.layout)["pending"]
            return 1, "", "stop here"

        self.ports.handover_call = look
        self.update(handover=True)
        self.assertEqual(seen["done"], ["runtime", "kits"])
        self.assertEqual(seen["pending"]["kind"], "update")

    def test_a_new_runtime_that_does_not_finish_rolls_the_update_back(self):
        old = self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        launcher_before = self.launcher()
        self.ports.handover_call = lambda python, gen_id, home, token: (3, "", "the new runtime crashed")
        result = self.update(handover=True)
        self.assertEqual((result["outcome"], result["phase"]), ("rolled-back", "handover"), result)
        self.assertIn("crashed", result["error"])
        state = ir.read_state(self.layout)
        self.assertEqual((state["active"], state["pending"]), (old["gen_id"], None))
        self.assertEqual(self.launcher(), launcher_before)

    def test_a_failure_inside_the_new_runtime_is_reported_after_it_rolled_back(self):
        manifest, _ = fake_component(self, "fakecomp")
        old = self.first(components=[manifest])

        def broken(ctx, effects):
            raise RuntimeError("boom in the new runtime")

        sys.modules["fake_fakecomp"].apply = broken
        make_release(self.releases, "r2", version="1.3.0", body="two")
        self.child(components=[manifest])
        result = self.update(handover=True, components=[manifest])
        self.assertEqual((result["outcome"], result["phase"]), ("rolled-back", "effects"), result)
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])

    def test_a_wrong_token_is_only_an_ordinary_resume(self):
        manifest, _ = fake_component(self, "fakecomp", fail="interrupt")
        old = self.first_with_interrupt(manifest)
        make_release(self.releases, "r2", version="1.3.0", body="two")
        with self.assertRaises(KeyboardInterrupt):
            self.update(components=[manifest])
        pending = ir.read_state(self.layout)["pending"]
        self.assertEqual(pending["kind"], "update")
        result = installer.continue_handover(gen_id=pending["gen_id"], token="not-the-token",
                                             **{k: v for k, v in self.kw(components=[manifest]).items()
                                                if k not in ("consent", "prereq")})
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(ir.read_state(self.layout)["active"], pending["gen_id"])
        self.assertNotEqual(pending["gen_id"], old["gen_id"])

    def first_with_interrupt(self, manifest):
        """Install once with the component working (the interrupt is armed per call count)."""
        module = sys.modules["fake_fakecomp"]
        armed = module.apply
        def working(ctx, effects):
            path = Path(ctx["user_home"]) / "fakecomp.marker"
            path.write_bytes(b"marker\n")
            return [fx.effect("fakecomp.marker", "fakecomp", "file-create", path, after=fx.sha256_bytes(b"marker\n"),
                              undo={"op": "marker-delete"})]

        module.apply = working
        result = self.first(components=[manifest])
        module.apply = armed
        return result

    def test_the_real_handover_command_carries_the_token_and_the_home(self):
        from eutonos.installer_ports import Ports
        seen = {}

        class Spy(Ports):
            def run(self, argv, **kw):
                seen["argv"], seen["env"] = [str(item) for item in argv], kw.get("env") or {}
                return 0, "", ""

        Spy("macos").handover(Path("/h/runtimes/x/bin/python"), "abc", Path("/h"), "tok")
        self.assertEqual(seen["argv"][1:], ["-I", "-B", "-m", "eutonos.installer", "continue", "--gen-id", "abc", "--token", "tok"])
        self.assertEqual(seen["env"]["EUTONOS_HOME"], str(Path("/h")))


class RollbackTests(UpdateBase):
    def two_generations(self):
        old = self.first()
        manifest = make_release(self.releases, "r2", version="1.3.0", body="two")
        new = self.update()
        self.assertEqual(new["outcome"], "updated", new)
        return old, new, manifest

    def test_rollback_reactivates_the_previous_generation(self):
        self.seed_user_files()
        before = snapshot(self.user)
        old, new, manifest = self.two_generations()
        self.assertIn(manifest["artifact_id"], self.launcher())
        result = self.rollback()
        self.assertEqual(result["outcome"], "rolled-back-to", result)
        self.assertEqual((result["gen_id"], result["from"], result["version"]), (old["gen_id"], new["gen_id"], "1.2.3"))
        state = ir.read_state(self.layout)
        self.assertEqual((state["active"], state["pending"]), (old["gen_id"], None))
        self.assertEqual(ir.read_record(self.layout, old["gen_id"])["state"], "active")
        newer = ir.read_record(self.layout, new["gen_id"])
        self.assertEqual(newer["state"], "rolled-back")
        self.assertEqual(newer["rollback"]["to"], old["gen_id"])
        self.assertIn(old["artifact_id"], self.launcher())
        self.assertNotIn(manifest["artifact_id"], self.launcher())
        self.assertTrue((self.layout.receipts(new["gen_id"]) / "91-rollback.json").is_file())
        self.assertTrue(ir.record_intact(self.layout, state, old["gen_id"]))
        self.assertTrue(ir.record_intact(self.layout, state, new["gen_id"]))
        # a rolled-back machine still uninstalls cleanly to the user's own bytes
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(snapshot(self.user), before)

    def test_each_generation_keeps_its_own_marketplace_and_the_registered_folder_follows_the_active_one(self):
        registered = self.h / "marketplace" / "crosstalk" / "plugin" / ".claude-plugin" / "plugin.json"

        def plugin(folder):
            return json.loads((folder / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]

        old = self.first(wheel=build_wheel(self.base / "w1", version="1.2.3", plugin_version="1.2.3"))
        make_release(self.releases, "r2", version="1.3.0", body="two",
                     existing=build_wheel(self.base / "w2", version="1.3.0", plugin_version="1.3.0"))
        new = self.update()
        self.assertEqual(new["outcome"], "updated", new)
        own_old, own_new = (self.layout.generation_marketplace(item["gen_id"]) for item in (old, new))
        self.assertEqual((plugin(own_old), plugin(own_new)), ("1.2.3", "1.3.0"))  # each generation keeps its own copy
        self.assertEqual(json.loads(registered.read_text(encoding="utf-8"))["version"], "1.3.0")
        self.assertEqual(snapshot(self.h / "marketplace" / "crosstalk"), snapshot(own_new))
        result = self.rollback()
        self.assertEqual(result["outcome"], "rolled-back-to", result)
        self.assertEqual(json.loads(registered.read_text(encoding="utf-8"))["version"], "1.2.3")  # a matching plugin
        self.assertEqual(snapshot(self.h / "marketplace" / "crosstalk"), snapshot(own_old))
        self.assertTrue(own_new.is_dir())  # the rolled-back generation keeps its copy until it is pruned

    def test_a_file_you_changed_in_the_registered_marketplace_survives_an_update(self):
        self.first(wheel=build_wheel(self.base / "w1", version="1.2.3", plugin_version="1.2.3"))
        mine = self.h / "marketplace" / "crosstalk" / "QUICKSTART.md"
        mine.write_text("# mine\n", encoding="utf-8")
        make_release(self.releases, "r2", version="1.3.0", body="two",
                     existing=build_wheel(self.base / "w2", version="1.3.0", plugin_version="1.3.0"))
        new = self.update()
        self.assertEqual(new["outcome"], "updated", new)
        self.assertEqual(mine.read_text(encoding="utf-8"), "# mine\n")
        self.assertIn("marketplace", [row["block"] for row in new["three_way"]])

    def test_pruning_and_uninstall_remove_the_generation_marketplace_copies(self):
        first = self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        second = self.update()
        make_release(self.releases, "r3", version="1.4.0", body="three")
        third = self.update()
        self.assertEqual(third["outcome"], "updated", third)
        self.assertFalse(self.layout.generation_marketplace(first["gen_id"]).exists())  # pruned: only two are kept
        self.assertTrue(self.layout.generation_marketplace(second["gen_id"]).is_dir())
        self.assertTrue(self.layout.generation_marketplace(third["gen_id"]).is_dir())
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertFalse(self.layout.generation_marketplace(second["gen_id"]).exists())
        self.assertFalse(self.layout.generation_marketplace(third["gen_id"]).exists())

    def test_effects_only_the_newer_generation_made_are_reversed(self):
        manifest, _ = fake_component(self, "fakecomp")
        profile = self.base / "owner.json"
        profile.write_text(json.dumps({"profile": "eutonos.owner-profile.v1", "components": {"fakecomp": "off"}}),
                           encoding="utf-8")
        old = self.first(components=[manifest], owner_profile=profile)
        marker = self.user / "fakecomp.marker"
        self.assertFalse(marker.exists())
        (self.h / "profiles" / "owner.json").write_text(
            json.dumps({"profile": "eutonos.owner-profile.v1", "components": {"fakecomp": "on"}}), encoding="utf-8")
        make_release(self.releases, "r2", version="1.3.0", body="two")
        new = self.update(components=[manifest])
        self.assertEqual(new["outcome"], "updated", new)
        self.assertTrue(marker.is_file())
        result = self.rollback(components=[manifest])
        self.assertEqual(result["outcome"], "rolled-back-to", result)
        self.assertIn("fakecomp.marker", result["removed"])
        self.assertFalse(marker.exists())
        self.assertEqual(ir.read_state(self.layout)["active"], old["gen_id"])

    def test_blocks_the_newer_generation_refreshed_are_listed_not_undone(self):
        old, new, _ = self.two_generations()
        state = ir.read_state(self.layout)
        newer = ir.read_record(self.layout, new["gen_id"])
        for item in newer["effects"]:
            if item["effect_id"] == "instructions.crosstalk.codex-agents":
                item["shipped_sha256"] = "f" * 64
                item["after_sha256"] = "e" * 64
        ir.write_record(self.layout, state, newer)
        ir.write_state(self.layout, state)
        before = (self.user / ".codex" / "AGENTS.md").read_bytes()
        result = self.rollback()
        self.assertEqual([item["effect_id"] for item in result["kept_newer"]], ["instructions.crosstalk.codex-agents"])
        self.assertEqual((self.user / ".codex" / "AGENTS.md").read_bytes(), before)
        self.assertIn("three-way", [step["id"] for step in cp.read(self.h)["steps"]])

    def test_rollback_refuses_below_min_compatible(self):
        old = self.first()
        make_release(self.releases, "r2", version="2.0.0", body="two", min_compatible="2.0.0")
        # An update refuses a release whose floor is above the installed version (see UpdateCompatibilityTests), so this
        # drives the rollback check on its own: it still protects a record whose floor was raised after the update.
        with unittest.mock.patch.object(installer, "compatibility", return_value=None):
            new = self.update()
        self.assertEqual(new["outcome"], "updated", new)
        before = settled(self.h)
        result = self.rollback()
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "below-min-compatible"), result)
        self.assertEqual((result["previous"], result["min_compatible"]), ("1.2.3", "2.0.0"))
        self.assertEqual(settled(self.h), before)
        self.assertEqual(ir.read_state(self.layout)["active"], new["gen_id"])
        self.assertNotEqual(old["gen_id"], new["gen_id"])

    def test_rollback_at_or_above_the_floor_is_allowed(self):
        self.first()
        make_release(self.releases, "r2", version="2.0.0", body="two", min_compatible="1.2.3")
        self.assertEqual(self.update()["outcome"], "updated")
        self.assertEqual(self.rollback()["outcome"], "rolled-back-to")

    def test_rollback_needs_a_previous_generation(self):
        self.assertEqual(self.rollback()["reason"], "not-installed")
        self.first()
        result = self.rollback()
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "no-previous-generation"), result)

    def test_rollback_refuses_a_damaged_previous_runtime(self):
        old, new, _ = self.two_generations()
        site = self.ports.site_packages(self.h / "runtimes" / old["artifact_id"])
        (site / "eutonos" / "payload.py").write_text("VALUE = 'tampered'\n", encoding="utf-8")
        before = settled(self.h)
        result = self.rollback()
        self.assertEqual((result["outcome"], result["reason"]), ("refused", "previous-generation-damaged"), result)
        self.assertEqual(settled(self.h), before)

    def test_rollback_dry_run_changes_nothing(self):
        self.two_generations()
        before = settled(self.h)
        result = self.rollback(dry_run=True)
        self.assertEqual((result["outcome"], result["version"]), ("dry-run", "1.2.3"))
        self.assertEqual(settled(self.h), before)

    def test_an_update_after_a_rollback_prunes_the_abandoned_generation(self):
        old, new, manifest = self.two_generations()
        self.assertEqual(self.rollback()["outcome"], "rolled-back-to")
        make_release(self.releases, "r3", version="1.4.0", body="three")
        third = self.update()
        self.assertEqual(third["outcome"], "updated", third)
        runtimes = sorted(item.name for item in (self.h / "runtimes").iterdir())
        self.assertIn(old["artifact_id"], runtimes)
        self.assertIn(third["artifact_id"], runtimes)
        self.assertNotIn(manifest["artifact_id"], runtimes)  # the rolled-back generation's runtime is gone
        self.assertEqual(ir.read_record(self.layout, third["gen_id"])["previous_gen"], old["gen_id"])

    def test_a_third_update_prunes_the_oldest_generation(self):
        old, new, _ = self.two_generations()
        make_release(self.releases, "r3", version="1.4.0", body="three")
        third = self.update()
        self.assertEqual(third["outcome"], "updated", third)
        runtimes = {item.name for item in (self.h / "runtimes").iterdir()}
        self.assertNotIn(old["artifact_id"], runtimes)
        self.assertIn(new["artifact_id"], runtimes)
        self.assertEqual(self.rollback()["outcome"], "rolled-back-to")  # back to the second generation


class UpdateCliTests(UpdateBase):
    def run_main(self, *argv):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = installer.main(["--home", str(self.h), *argv])
        return code, buffer.getvalue()

    def test_the_commands_exist(self):
        parser = installer._parser()
        for argv in (["update"], ["update", "--source", "x", "--channel", "beta", "--dry-run"], ["rollback"],
                     ["rollback", "--dry-run"], ["continue", "--gen-id", "g", "--token", "t"]):
            self.assertEqual(parser.parse_args(argv).command, argv[0])

    def test_update_dry_run_from_the_command_line(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        code, out = self.run_main("update", "--source", str(self.releases), "--dry-run", "--allow-unsigned")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["would_install"]["version"], "1.3.0")

    def test_a_refusal_exits_1(self):
        self.first()
        code, out = self.run_main("rollback")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "no-previous-generation")


class InstructionBlockTests(LifeBase):
    """The three managed instruction blocks per target file: adding, wrapping an exact unmarked copy, leaving a
    similar copy alone, and a lossless per-block uninstall (temp homes only)."""

    def first(self, **extra):
        result = self.install(**extra)
        self.assertEqual(result["outcome"], "installed", result)
        return result

    def claude(self):
        return self.user / ".claude" / "CLAUDE.md"

    def codex(self):
        return self.user / ".codex" / "AGENTS.md"

    def blocks(self, raw: bytes) -> list:
        return [name for name in ("completion", "crosstalk", "decisions", "memory") if b"<!-- eutonos:%s:start" % name.encode() in raw]

    def effect(self, result, block, label):
        record = ir.read_record(self.layout, result["gen_id"])
        return next(item for item in record["effects"] if item["effect_id"] == f"instructions.{block}.{label}")

    def test_decisions_block_installs_to_both_global_files_and_uninstall_removes_it(self):
        self.seed_user_files()
        before = snapshot(self.user)
        result = self.first()
        for path in (self.claude(), self.codex()):
            raw = path.read_bytes()
            self.assertEqual(self.blocks(raw), ["completion", "crosstalk", "decisions", "memory"])
            self.assertIn(b"## Decisions", raw)
            self.assertIn(b"**Decided:**", raw)
            self.assertEqual(raw.count(dpol.TEXT.encode()), 1)
        self.assertEqual(self.effect(result, "decisions", "codex-agents")["undo"]["mode"], "added")
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        self.assertEqual(snapshot(self.user), before)

    def test_files_that_did_not_exist_are_created_and_removed_again(self):
        self.assertFalse(self.claude().exists())
        result = self.first()
        self.assertEqual(self.blocks(self.claude().read_bytes()), ["completion", "crosstalk", "decisions", "memory"])
        self.assertTrue(self.effect(result, "completion", "claude-claude")["undo"]["created"])
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertFalse(self.claude().exists())

    def test_an_exact_unmarked_copy_is_wrapped_in_place_and_uninstall_leaves_the_text(self):
        text = ("# my rules\n\nEarlier paragraph.\n\n## Completion contract\n\n" + cpol.CONTRACT + "   \n\n"
                "## Decisions\r\n\r\n" + cpol.DECISIONS + "\r\n\n## Mine\nLater paragraph.\n")
        original = text.encode("utf-8")
        self.claude().write_bytes(original)
        result = self.first()
        raw = self.claude().read_bytes()
        self.assertEqual(raw.count(cpol.CONTRACT.encode()), 1, "no second copy")
        self.assertEqual(raw.count(dpol.TEXT.encode()), 1, "no second copy")
        self.assertIn(b"# my rules\n\nEarlier paragraph.\n\n<!-- eutonos:completion:start", raw)  # nothing moved
        self.assertIn(b"## Mine\nLater paragraph.\n", raw)
        for block in ("completion", "decisions"):
            effect = self.effect(result, block, "claude-claude")
            self.assertEqual((effect["adoption"], effect["undo"]["mode"]), ("wrap", "wrap"))
            self.assertEqual(effect["undo"]["op"], "block-remove")
        # The person edits elsewhere; the wrapped text is then no longer what a plain restore would find.
        self.claude().write_bytes(raw.replace(b"Later paragraph.", b"Later paragraph, edited."))
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        left = self.claude().read_bytes()
        self.assertNotIn(b"eutonos:", left)
        self.assertEqual(left, original.replace(b"Later paragraph.", b"Later paragraph, edited."))

    def doctor_states(self):
        report = doctor.report(home=self.h, user_home=self.user, root=self.base)
        return {item["effect_id"]: item["state"] for item in report["declared"]["effects"]
                if item["effect_id"].startswith("instructions.")}

    def test_doctor_reads_every_block_of_a_file_not_only_the_last_one_written(self):
        self.seed_user_files()
        self.first()
        every = {f"instructions.{block}.{label}": "ok" for block in ("completion", "crosstalk", "decisions", "memory")
                 for label in ("codex-agents", "claude-claude")}
        self.assertEqual(self.doctor_states(), every)
        # Another block of the same file is untouched by an edit inside one of them, and by text outside them.
        raw = self.claude().read_bytes()
        self.claude().write_bytes(raw.replace(b"**Decided:**", b"**Decided, my way:**", 1) + b"\nmy later note\n")
        states = self.doctor_states()
        self.assertEqual(states["instructions.decisions.claude-claude"], "changed by you")
        self.assertEqual({key: value for key, value in states.items() if key != "instructions.decisions.claude-claude"},
                         {key: "ok" for key in every if key != "instructions.decisions.claude-claude"})
        # A block taken out of one file reads missing there, and only there.
        codex = self.codex().read_bytes()
        start = codex.index(b"<!-- eutonos:crosstalk:start")
        end = codex.index(b"<!-- eutonos:crosstalk:end -->") + len(b"<!-- eutonos:crosstalk:end -->")
        self.codex().write_bytes(codex[:start] + codex[end:])
        states = self.doctor_states()
        self.assertEqual(states["instructions.crosstalk.codex-agents"], "missing")
        self.assertEqual(states["instructions.crosstalk.claude-claude"], "ok")
        self.assertEqual(states["instructions.completion.codex-agents"], "ok")

    def test_doctor_reads_a_wrapped_copy_and_a_crlf_file_as_ok(self):
        text = ("# my rules\n\n## Completion contract\n\n" + cpol.CONTRACT + "   \n\n## Mine\nLater paragraph.\n")
        self.claude().write_bytes(text.encode("utf-8"))
        self.first()
        self.assertEqual(set(self.doctor_states().values()), {"ok"})
        for path in (self.claude(), self.codex()):
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))  # checked out as CRLF
        self.assertEqual(set(self.doctor_states().values()), {"ok"})

    def test_a_similar_unmarked_copy_writes_nothing_and_is_a_pending_three_way_choice(self):
        mine = "# my rules\n\n## Decisions\n\nAsk me before every change.\n"
        self.claude().write_text(mine, encoding="utf-8")
        result = self.first()
        raw = self.claude().read_bytes()
        self.assertNotIn(b"eutonos:decisions", raw)
        self.assertEqual(raw.count(b"## Decisions"), 1)
        self.assertIn(b"Ask me before every change.", raw)
        self.assertEqual(self.blocks(raw), ["completion", "crosstalk", "memory"])  # the other blocks still went in
        self.assertEqual(self.blocks(self.codex().read_bytes()), ["completion", "crosstalk", "decisions", "memory"])
        rows = [item for item in ir.read_record(self.layout, result["gen_id"])["three_way"] if item["block"] == "decisions"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["target"], str(self.claude()))
        self.assertEqual(rows[0]["reason"], "similar-unmarked-copy")
        self.assertEqual(rows[0]["new_shipped_sha256"], fx.sha256_bytes(dpol.block()))
        self.assertTrue(rows[0]["current_sha256"])
        self.assertIn("three-way", [step["id"] for step in cp.read(self.h)["steps"]])
        report = doctor.report(home=self.h, user_home=self.user, root=self.base)
        self.assertEqual([item["reason"] for item in report["three_way"] if item["block"] == "decisions"], ["similar-unmarked-copy"])
        self.assertIn("Three-way review pending", doctor._short_text(report))
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(self.claude().read_text(encoding="utf-8"), mine)

    def test_a_replaced_older_marked_crosstalk_block_is_restored_byte_identical(self):
        older = ci.block("C:/old/checkout")  # an earlier hand install: another path, same markers
        original = b"# first\n\nMy paragraph before.\n\n" + older + b"\n\nMy paragraph after.\n"
        self.claude().write_bytes(original)
        result = self.first()
        raw = self.claude().read_bytes()
        self.assertNotIn(b"C:/old/checkout", raw)
        self.assertEqual(self.effect(result, "crosstalk", "claude-claude")["undo"]["mode"], "replaced")
        # An unrelated edit elsewhere after install, at the top and the bottom.
        edited = b"added at the top\n" + raw + b"added at the bottom\n"
        self.claude().write_bytes(edited)
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        self.assertEqual(self.claude().read_bytes(), b"added at the top\n" + original + b"added at the bottom\n")

    def test_an_added_block_comes_out_with_its_separator_after_edits_elsewhere(self):
        self.claude().write_bytes(b"# mine\nline\n")
        self.first()
        raw = self.claude().read_bytes()
        self.claude().write_bytes(raw + b"typed after install\n")
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        # The crosstalk block went in at the top, completion and decisions at the end: all three come out cleanly.
        self.assertEqual(self.claude().read_bytes(), b"# mine\nline\ntyped after install\n")

    def test_a_block_edited_inside_is_kept_and_reported(self):
        self.seed_user_files()
        self.first()
        raw = self.claude().read_bytes()
        edited = raw.replace(b"Report in plain English", b"Report in rhyme")
        self.assertNotEqual(raw, edited)
        self.claude().write_bytes(edited)
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        kept = [item for item in gone["kept"] if "instructions.decisions.claude-claude" in item["effect_id"]]
        self.assertEqual(len(kept), 1, gone["kept"])
        self.assertIn("changed by you, kept", kept[0]["reason"])
        left = self.claude().read_bytes()
        self.assertIn(b"Report in rhyme", left)
        self.assertEqual(self.blocks(left), ["decisions"])  # the intact ones were removed

    def test_an_undo_recorded_in_the_old_shape_still_works(self):
        self.seed_user_files()
        before = snapshot(self.user)
        self.first()
        state = ir.read_state(self.layout)
        record = ir.read_record(self.layout, state["active"])
        for item in record["effects"]:
            if item["effect_id"].startswith("instructions."):
                item["undo"] = {"op": "block-remove", "module": item["undo"]["module"], "created": item["undo"]["created"]}
        ir.write_record(self.layout, state, record)
        ir.write_state(self.layout, state)
        self.claude().write_bytes(self.claude().read_bytes() + b"typed after install\n")
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        left = self.claude().read_bytes()
        self.assertEqual(self.blocks(left), [])
        self.assertTrue(left.startswith(b"# my rules"), left)
        self.assertTrue(left.endswith(b"typed after install\n"))
        self.assertEqual(self.codex().read_bytes(), before[".codex/AGENTS.md"])

    def test_crlf_files_stay_crlf(self):
        original = b"# mine\r\n\r\n## Completion contract\r\n\r\n" + cpol.CONTRACT.encode() + b"\r\n\r\n## Tail\r\ntail\r\n"
        self.claude().write_bytes(original)
        self.codex().write_bytes(b"codex line\r\n")
        self.first()
        for path in (self.claude(), self.codex()):
            raw = path.read_bytes()
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), path)
            self.assertEqual(self.blocks(raw), ["completion", "crosstalk", "decisions", "memory"])
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.claude().read_bytes(), original)
        self.assertEqual(self.codex().read_bytes(), b"codex line\r\n")

    def test_the_dry_run_names_wrapped_and_pending_blocks(self):
        self.claude().write_text("## Decisions\n\nMy own rule.\n\n## Completion contract\n\n" + cpol.CONTRACT + "\n",
                                 encoding="utf-8")
        plan = self.install(dry_run=True)
        actions = [row["action"] for row in plan["writes"] if row["phase"] == "effects"]
        self.assertTrue(any("put markers around your existing copy of the completion instruction block" in a for a in actions), actions)
        self.assertTrue(any("decisions instruction block: your similar copy stays active" in a for a in actions), actions)


class MemoryBlockTests(LifeBase):
    """The fourth managed block, ``eutonos:memory``: each file gets its own rendered text, it rides the same lossless
    route as the other three, and Hermes' ``SOUL.md`` gets it only when the hermes component is installed and the
    file already exists (temp homes only; Hermes is a fake folder with a fake ``bin/hermes.cmd``)."""

    FILES = (("claude", "claude-claude"), ("codex", "codex-agents"))
    NEWER = "{{harness}} text, second edition. Read {{file}}; tools: {{crosstalk_tools}}; home: {{eutonos}}."
    SOUL = b"# Soul\r\n\r\nMy persona.\r\n\r\n<!-- EUTONOS:BEGIN -->\r\nhand written block\r\n<!-- EUTONOS:END -->\r\n"

    def setUp(self):
        super().setUp()
        self.hermes = self.base / "hermes-home"

    def claude(self):
        return self.user / ".claude" / "CLAUDE.md"

    def codex(self):
        return self.user / ".codex" / "AGENTS.md"

    def soul(self):
        return self.hermes / "SOUL.md"

    def path_of(self, harness):
        return {"claude": self.claude(), "codex": self.codex(), "hermes": self.soul()}[harness]

    def seed(self):
        """The usual user files, the two instruction files as LF bytes so no check depends on the platform's newline."""
        self.seed_user_files()
        self.claude().write_bytes(b"# my rules\n")
        self.codex().write_bytes(b"mine\n")

    def first(self, **extra):
        result = self.install(**extra)
        self.assertEqual(result["outcome"], "installed", result)
        return result

    def blocks(self, raw: bytes) -> list:
        return [name for name in ("completion", "crosstalk", "decisions", "memory") if b"<!-- eutonos:%s:start" % name.encode() in raw]

    def effects(self, result) -> dict:
        record = ir.read_record(self.layout, result["gen_id"])
        return {item["effect_id"]: item for item in record["effects"] if item["effect_id"].startswith("instructions.memory.")}

    def effect(self, result, label):
        return self.effects(result)[f"instructions.memory.{label}"]

    def update_kwargs(self, **extra):
        values = self.kw(handover=False, source=self.base / "releases", doctor=lambda argv: 0, allow_unsigned=True)
        for name in ("consent", "prereq"):
            values.pop(name)
        values.update(extra)
        return values

    def hermes_installed(self, soul=SOUL, *, cli=True):
        """A fake Hermes home the hermes component finds (its ``bin/hermes.cmd``) and, when `soul` is not None, its
        SOUL.md. Returns the component manifest to pass to install and uninstall."""
        (self.hermes / "bin").mkdir(parents=True)
        if cli:
            (self.hermes / "bin" / "hermes.cmd").write_bytes(b"@echo off\r\n")
        if soul is not None:
            self.soul().write_bytes(soul)
        self.ports.seam_map["hermes_home"] = str(self.hermes)
        manifest, _calls = fake_component(self, "hermes")
        sys.modules["fake_hermes"].status = component_hermes.status  # the real status: found only with the CLI
        return manifest

    # ------------------------------------------------------------------ the two global files
    def test_install_adds_each_files_own_block_and_keeps_every_user_byte(self):
        self.seed()
        seeded = {"claude": b"# my rules\n", "codex": b"mine\n"}
        result = self.first()
        self.assertNotEqual(mi.block("claude", self.h), mi.block("codex", self.h))
        for harness, label in self.FILES:
            raw = self.path_of(harness).read_bytes()
            own = mi.block(harness, self.h)
            self.assertEqual(raw.count(own), 1, harness)
            self.assertEqual(raw.count(b"<!-- eutonos:memory:start"), 1, harness)
            self.assertIn(seeded[harness], raw, harness)
            self.assertEqual(self.blocks(raw), ["completion", "crosstalk", "decisions", "memory"])
            item = self.effect(result, label)
            self.assertEqual((item["undo"]["op"], item["undo"]["module"], item["undo"]["mode"]),
                             ("block-remove", "memory", "added"))
            self.assertEqual((item["kind"], item["selector"]), ("file-block", "eutonos:memory"))
            self.assertEqual(item["shipped_sha256"], fx.sha256_bytes(own), "each row ships its own file's bytes")
            self.assertEqual(item["after_sha256"], fx.sha256_file(self.path_of(harness)))
        self.assertEqual(sorted(self.effects(result)), ["instructions.memory.claude-claude", "instructions.memory.codex-agents"])
        self.assertFalse(self.soul().exists())
        self.assertEqual(sorted(item["effect_id"] for item in ir.read_record(self.layout, result["gen_id"])["effects"]
                                if item["effect_id"].startswith("instructions.") and item["effect_id"].endswith("codex-agents")),
                         ["instructions.completion.codex-agents", "instructions.crosstalk.codex-agents",
                          "instructions.decisions.codex-agents", "instructions.memory.codex-agents"])

    def test_the_block_names_the_install_it_came_from_and_carries_no_owner_text(self):
        self.first()
        text = self.claude().read_text(encoding="utf-8")
        start = text.index("<!-- eutonos:memory:start")
        block = text[start:text.index("<!-- eutonos:memory:end -->") + len("<!-- eutonos:memory:end -->")]
        self.assertIn(mi.TITLE, block)
        self.assertNotIn("{{", block)
        self.assertLessEqual(len(block.encode("utf-8")), mi.SIZE_CAP)

    def test_planning_again_after_an_install_changes_nothing(self):
        self.seed()
        result = self.first()
        written = snapshot(self.user)
        effects, three_way, notes = fx.instruction_effects(
            self.layout, self.user, "gen-second", previous={}, codex_home=self.user / ".codex",
            claude_home=self.user / ".claude")
        self.assertEqual((effects, three_way, notes), ([], [], []))
        self.assertEqual(snapshot(self.user), written)
        # And a second install of the same machine is refused rather than run again.
        again = self.install()
        self.assertNotEqual(again["outcome"], "installed", again)
        self.assertEqual(snapshot(self.user), written)
        self.assertEqual(sorted(self.effects(result)), ["instructions.memory.claude-claude", "instructions.memory.codex-agents"])

    def test_uninstall_removes_the_block_and_restores_the_files_byte_for_byte(self):
        self.seed()
        before = snapshot(self.user)
        self.first()
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        self.assertEqual(snapshot(self.user), before)

    def test_text_typed_after_the_install_survives_the_uninstall(self):
        self.seed()
        self.first()
        self.claude().write_bytes(b"typed first\n" + self.claude().read_bytes() + b"typed last\n")
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(self.claude().read_bytes(), b"typed first\n# my rules\ntyped last\n")

    def test_a_memory_block_edited_inside_is_kept_and_reported(self):
        self.seed()
        self.first()
        raw = self.claude().read_bytes()
        marker = raw.index(b"<!-- eutonos:memory:start")
        edited = raw[:marker] + raw[marker:].replace(mi.TITLE.encode(), b"## Mine, edited", 1)
        self.assertNotEqual(raw, edited)
        self.claude().write_bytes(edited)
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        kept = [item for item in gone["kept"] if "instructions.memory.claude-claude" in item["effect_id"]]
        self.assertEqual(len(kept), 1, gone["kept"])
        self.assertIn("changed by you, kept", kept[0]["reason"])
        left = self.claude().read_bytes()
        self.assertIn(b"## Mine, edited", left)
        self.assertEqual(self.blocks(left), ["memory"])  # the intact blocks were removed
        self.assertEqual(self.blocks(self.codex().read_bytes()), [])  # the other file's block came out cleanly

    def test_files_that_did_not_exist_are_created_with_the_block_and_removed_again(self):
        self.assertFalse(self.claude().exists() or self.codex().exists())
        self.first()
        for harness, _label in self.FILES:
            self.assertIn(mi.block(harness, self.h), self.path_of(harness).read_bytes())
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertFalse(self.claude().exists() or self.codex().exists())

    def test_crlf_and_a_byte_order_mark_are_kept(self):
        original = b"\xef\xbb\xbf# mine\r\n\r\nline\r\n"
        self.claude().write_bytes(original)
        self.codex().write_bytes(b"codex line\r\n")
        self.first()
        for path in (self.claude(), self.codex()):
            raw = path.read_bytes()
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), path)
            self.assertEqual(self.blocks(raw), ["completion", "crosstalk", "decisions", "memory"])
        self.assertTrue(self.claude().read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.claude().read_bytes(), original)
        self.assertEqual(self.codex().read_bytes(), b"codex line\r\n")

    def test_an_older_marked_memory_block_is_refreshed_in_place_and_restored_on_uninstall(self):
        older = cpol.make_block(mi.MARK, cpol.block_body("An older sentence.", mi.TITLE))
        original = b"# first\n\nMy paragraph before.\n\n" + older + b"\n\nMy paragraph after.\n"
        self.claude().write_bytes(original)
        result = self.first()
        raw = self.claude().read_bytes()
        self.assertNotIn(b"An older sentence.", raw)
        self.assertIn(b"My paragraph before.\n\n<!-- eutonos:memory:start", raw)  # replaced where it stood
        self.assertEqual(self.effect(result, "claude-claude")["undo"]["mode"], "replaced")
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.claude().read_bytes(), original)

    def test_an_exact_unmarked_copy_is_wrapped_in_place(self):
        text = mi.render("claude", self.h)
        original = ("# mine\n\n" + mi.TITLE + "\n\n" + text + "\n\n## Tail\ntail\n").encode("utf-8")
        self.claude().write_bytes(original)
        result = self.first()
        raw = self.claude().read_bytes()
        self.assertEqual(raw.count(text.encode()), 1, "no second copy")
        item = self.effect(result, "claude-claude")
        self.assertEqual((item["adoption"], item["undo"]["mode"]), ("wrap", "wrap"))
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.claude().read_bytes(), original)

    def test_a_heading_collision_with_different_text_is_a_pending_choice_and_writes_nothing(self):
        mine = "# my rules\n\n" + mi.TITLE + "\n\nMy own notes about EUTONOS.\n"
        self.claude().write_text(mine, encoding="utf-8")
        result = self.first()
        raw = self.claude().read_bytes()
        self.assertNotIn(b"eutonos:memory", raw)
        self.assertIn(b"My own notes about EUTONOS.", raw)
        self.assertEqual(self.blocks(raw), ["completion", "crosstalk"] + ["decisions"])  # the others still went in
        self.assertEqual(self.blocks(self.codex().read_bytes()), ["completion", "crosstalk", "decisions", "memory"])
        rows = [item for item in ir.read_record(self.layout, result["gen_id"])["three_way"] if item["block"] == "memory"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["target"], rows[0]["reason"]), (str(self.claude()), "similar-unmarked-copy"))
        self.assertEqual(rows[0]["new_shipped_sha256"], fx.sha256_bytes(mi.block("claude", self.h)))
        self.assertNotIn("instructions.memory.claude-claude", self.effects(result))
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(self.claude().read_text(encoding="utf-8"), mine)

    def test_adopt_records_a_block_that_is_already_there_without_rewriting_it(self):
        mine = b"# mine\n\n" + mi.block("claude", self.h) + b"\n"
        self.claude().write_bytes(mine)
        result = self.first(adopt=True)
        item = self.effect(result, "claude-claude")
        self.assertTrue(item["adopted"])
        self.assertEqual(item["shipped_sha256"], fx.sha256_bytes(mi.block("claude", self.h)))
        self.assertEqual(self.claude().read_bytes().count(mi.block("claude", self.h)), 1)

    # ------------------------------------------------------------------ update
    def test_an_update_with_the_same_text_changes_no_user_file_and_keeps_the_effects(self):
        self.seed()
        before = snapshot(self.user)
        first = self.first()
        written = snapshot(self.user)
        make_release(self.base / "releases", "r2", version="1.3.0", body="two")
        updated = installer.update(**self.update_kwargs())
        self.assertEqual(updated["outcome"], "updated", updated)
        self.assertEqual(snapshot(self.user), written)
        self.assertEqual(sorted(self.effects(updated)), sorted(self.effects(first)))
        for label in ("claude-claude", "codex-agents"):
            self.assertEqual(self.effect(updated, label)["backup"], self.effect(first, label)["backup"])
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")
        self.assertEqual(snapshot(self.user), before)

    def test_an_update_with_new_text_replaces_the_block_in_place_and_uninstall_still_restores_the_files(self):
        self.seed()
        before = snapshot(self.user)
        first = self.first()
        old = {harness: mi.block(harness, self.h) for harness, _label in self.FILES}
        make_release(self.base / "releases", "r2", version="1.3.0", body="two")
        with unittest.mock.patch.object(mi, "template", return_value=self.NEWER):
            new = {harness: mi.block(harness, self.h) for harness, _label in self.FILES}
            updated = installer.update(**self.update_kwargs())
        self.assertEqual(updated["outcome"], "updated", updated)
        for harness, label in self.FILES:
            raw = self.path_of(harness).read_bytes()
            self.assertNotEqual(old[harness], new[harness])
            self.assertNotIn(old[harness], raw)
            self.assertEqual(raw.count(new[harness]), 1, harness)
            self.assertEqual(raw.count(b"<!-- eutonos:memory:start"), 1, harness)
            self.assertIn(before[f".{'claude' if harness == 'claude' else 'codex'}/{self.path_of(harness).name}"], raw)
            earlier, later = self.effect(first, label), self.effect(updated, label)
            for key in ("before_sha256", "backup", "undo", "kind", "target"):
                self.assertEqual(later[key], earlier[key], key)  # the first install's way back is kept
            self.assertNotEqual(later["shipped_sha256"], earlier["shipped_sha256"])
            self.assertEqual(later["shipped_sha256"], fx.sha256_bytes(new[harness]))
            self.assertEqual(later["after_sha256"], fx.sha256_file(self.path_of(harness)))
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        self.assertEqual(snapshot(self.user), before)

    # ------------------------------------------------------------------ the registry
    def test_every_block_is_registered_for_uninstall_and_the_memory_block_is_one_of_them(self):
        names = [name for name, _module, _marker in fx.BLOCKS]
        self.assertEqual(names, ["completion", "crosstalk", "decisions", "memory", "simon"])
        self.assertEqual(fx.OPT_IN_BLOCKS, {"simon"})  # written only while the Simon part is on
        self.assertEqual(sorted(fx.MODULES), sorted(names))
        for name, module, marker in fx.BLOCKS:
            self.assertIs(fx.MODULES[name], module)
            self.assertTrue(marker.startswith("eutonos:"), marker)
            for attribute in ("PATTERN", "START", "END"):
                self.assertTrue(hasattr(module, attribute), (name, attribute))
            self.assertIn(marker.encode(), module.START)

    def test_an_undo_recorded_without_a_shape_still_removes_the_memory_block(self):
        self.seed()
        before = snapshot(self.user)
        self.first()
        state = ir.read_state(self.layout)
        record = ir.read_record(self.layout, state["active"])
        for item in record["effects"]:
            if item["effect_id"].startswith("instructions."):
                item["undo"] = {"op": "block-remove", "module": item["undo"]["module"], "created": item["undo"]["created"]}
        ir.write_record(self.layout, state, record)
        ir.write_state(self.layout, state)
        self.claude().write_bytes(self.claude().read_bytes() + b"typed after install\n")
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        left = self.claude().read_bytes()
        self.assertNotIn(b"eutonos:memory", left)
        self.assertEqual(self.blocks(left), [])
        self.assertTrue(left.startswith(b"# my rules"), left)
        self.assertTrue(left.endswith(b"typed after install\n"))
        self.assertEqual(self.codex().read_bytes(), before[".codex/AGENTS.md"])

    # ------------------------------------------------------------------ Hermes
    def test_hermes_installed_with_a_soul_file_gets_the_block_appended_and_loses_it_on_uninstall(self):
        self.seed()
        hermes = self.hermes_installed()
        before = snapshot(self.user)
        result = self.first(components=[hermes])
        raw = self.soul().read_bytes()
        own = mi.block("hermes", self.h)
        self.assertEqual(raw.count(own.replace(b"\n", b"\r\n")), 1, "the block follows the file's CRLF")
        self.assertEqual(self.blocks(raw), ["memory"], "only the memory block goes into SOUL.md")
        self.assertIn(self.SOUL, raw)
        self.assertIn(b"<!-- EUTONOS:BEGIN -->\r\nhand written block\r\n<!-- EUTONOS:END -->", raw)
        self.assertTrue(raw.startswith(self.SOUL), "appended after the persona, nothing moved")
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "CRLF kept")
        self.assertEqual(sorted(self.effects(result)), ["instructions.memory.claude-claude", "instructions.memory.codex-agents",
                                                        "instructions.memory.hermes-soul"])
        item = self.effect(result, "hermes-soul")
        self.assertEqual((item["target"], item["undo"]["module"], item["undo"]["mode"]),
                         (str(self.soul()), "memory", "added"))
        self.assertEqual(item["shipped_sha256"], fx.sha256_bytes(own))
        self.assertNotEqual(item["shipped_sha256"], self.effect(result, "claude-claude")["shipped_sha256"])
        self.assertNotIn("instructions.completion.hermes-soul", [e["effect_id"] for e in ir.read_record(
            self.layout, result["gen_id"])["effects"]])
        self.assertEqual(result["notes"], [])
        gone = self.uninstall(components=[hermes])
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertEqual(gone["kept"], [])
        self.assertEqual(self.soul().read_bytes(), self.SOUL)
        self.assertEqual(snapshot(self.user), before)

    def test_hermes_installed_without_a_soul_file_gets_no_file_and_a_note(self):
        hermes = self.hermes_installed(soul=None)
        result = self.first(components=[hermes])
        self.assertFalse(self.soul().exists(), "SOUL.md is never created")
        self.assertNotIn("instructions.memory.hermes-soul", self.effects(result))
        self.assertEqual(len(result["notes"]), 1, result["notes"])
        self.assertIn("SOUL.md", result["notes"][0])
        self.assertIn("never creates", result["notes"][0])
        self.assertEqual(self.blocks(self.claude().read_bytes()), ["completion", "crosstalk", "decisions", "memory"])
        self.assertEqual(self.uninstall(components=[hermes])["outcome"], "uninstalled")
        self.assertFalse(self.soul().exists())

    def test_a_soul_file_that_appears_later_gets_the_block_on_the_next_update(self):
        hermes = self.hermes_installed(soul=None)
        self.first(components=[hermes])
        self.soul().write_bytes(self.SOUL)
        make_release(self.base / "releases", "r2", version="1.3.0", body="two")
        updated = installer.update(**self.update_kwargs(components=[hermes]))
        self.assertEqual(updated["outcome"], "updated", updated)
        self.assertEqual(self.soul().read_bytes(),
                         self.SOUL + b"\r\n\r\n" + mi.block("hermes", self.h).replace(b"\n", b"\r\n") + b"\r\n")
        self.assertIn("instructions.memory.hermes-soul", self.effects(updated))
        self.assertEqual(self.uninstall(components=[hermes])["outcome"], "uninstalled")
        self.assertEqual(self.soul().read_bytes(), self.SOUL)

    def test_hermes_that_is_not_installed_gets_nothing(self):
        # No seam and no Hermes folder (a Mac): the component is skipped and no SOUL.md is looked for.
        result = self.first(components=[fake_component(self, "hermes")[0]])
        self.assertNotIn("instructions.memory.hermes-soul", self.effects(result))
        self.assertEqual(result["notes"], [])
        self.assertEqual(self.uninstall(components=[fake_component(self, "hermes")[0]])["outcome"], "uninstalled")

    def test_hermes_without_its_cli_is_not_installed_so_an_existing_soul_file_is_left_alone(self):
        hermes = self.hermes_installed(cli=False)
        result = self.first(components=[hermes])
        self.assertEqual(self.soul().read_bytes(), self.SOUL)
        self.assertNotIn("instructions.memory.hermes-soul", self.effects(result))
        self.assertEqual(result["notes"], [])

    def test_a_person_who_turned_hermes_off_keeps_their_soul_file_untouched(self):
        hermes = self.hermes_installed()
        profile = self.base / "owner.json"
        profile.write_text(json.dumps({"profile": "eutonos.owner-profile.v1", "components": {"hermes": "off"}}),
                           encoding="utf-8")
        result = self.first(components=[hermes], owner_profile=profile)
        self.assertEqual(self.soul().read_bytes(), self.SOUL)
        self.assertNotIn("instructions.memory.hermes-soul", self.effects(result))

    def test_a_soul_file_near_the_size_cap_gets_a_note(self):
        near = b"# Soul\n" + b"x" * (mi.SOUL_NOTE_CHARS + 100) + b"\n"
        hermes = self.hermes_installed(soul=near)
        result = self.first(components=[hermes])
        self.assertEqual(len(result["notes"]), 1, result["notes"])
        self.assertIn(str(mi.SOUL_CAP_CHARS), result["notes"][0])
        self.assertIn(mi.block("hermes", self.h), self.soul().read_bytes())  # the note warns; it does not refuse

    def test_a_customized_memory_block_in_soul_is_listed_for_review_and_left_alone(self):
        custom = self.SOUL + b"\r\n" + mi.block("hermes", self.h).replace(mi.TITLE.encode(), b"## Mine") + b"\r\n"
        hermes = self.hermes_installed(soul=custom)
        result = self.first(components=[hermes])
        self.assertEqual(self.soul().read_bytes(), custom)
        rows = [item for item in ir.read_record(self.layout, result["gen_id"])["three_way"]
                if item["block"] == "memory" and item["target"] == str(self.soul())]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["new_shipped_sha256"], fx.sha256_bytes(mi.block("hermes", self.h)))
        self.assertNotIn("instructions.memory.hermes-soul", self.effects(result))

    # ------------------------------------------------------------------ dry run and doctor
    def test_the_dry_run_lists_the_memory_block_for_each_file_and_writes_nothing(self):
        self.seed()
        hermes = self.hermes_installed()
        before = (snapshot(self.user), snapshot(self.hermes))
        plan = self.install(dry_run=True, components=[hermes])
        rows = [row for row in plan["writes"] if row["phase"] == "effects" and "memory instruction block" in row["action"]]
        self.assertEqual(sorted(row["target"] for row in rows), sorted(str(p) for p in (self.claude(), self.codex(), self.soul())))
        self.assertTrue(all(row["action"].startswith("add the") for row in rows), rows)
        self.assertEqual((snapshot(self.user), snapshot(self.hermes)), before)
        self.assertFalse(self.h.exists() and any(self.h.glob("install/records/*")))

    def test_the_dry_run_says_what_it_will_do_about_a_missing_soul_file(self):
        hermes = self.hermes_installed(soul=None)
        plan = self.install(dry_run=True, components=[hermes])
        notes = [row for row in plan["writes"] if row["phase"] == "effects" and "SOUL.md" in row["action"]]
        self.assertEqual(len(notes), 1, notes)
        self.assertFalse(self.soul().exists())

    def test_doctor_reads_the_memory_effects_after_an_install(self):
        hermes = self.hermes_installed()
        self.first(components=[hermes])
        report = doctor.report(home=self.h, user_home=self.user, root=self.base)
        states = {item["effect_id"]: item for item in report["declared"]["effects"]
                  if item["effect_id"].startswith("instructions.memory.")}
        self.assertEqual(sorted(states), ["instructions.memory.claude-claude", "instructions.memory.codex-agents",
                                          "instructions.memory.hermes-soul"])
        # Doctor reads a block Effect by its own markers, so every block in a file reads "ok" on a fresh install,
        # not only the one written last (each Effect recorded the whole file as it stood right after its block).
        self.assertEqual({key: item["state"] for key, item in states.items()},
                         {key: "ok" for key in states}, states)
        self.assertEqual(states["instructions.memory.hermes-soul"]["target"], str(self.soul()))
        self.assertEqual(report["three_way"], [])
        # Text typed outside a block is the person's own and changes nothing; an edit inside the block shows up.
        self.soul().write_bytes(self.soul().read_bytes() + b"typed later\r\n")
        again = doctor.report(home=self.h, user_home=self.user, root=self.base)
        later = {item["effect_id"]: item["state"] for item in again["declared"]["effects"]}
        self.assertEqual(later["instructions.memory.hermes-soul"], "ok")
        self.soul().write_bytes(self.soul().read_bytes().replace(mi.TITLE.encode(), b"## My own heading"))
        edited = doctor.report(home=self.h, user_home=self.user, root=self.base)
        last = {item["effect_id"]: item["state"] for item in edited["declared"]["effects"]}
        self.assertEqual(last["instructions.memory.hermes-soul"], "changed by you")
        self.assertEqual(last["instructions.memory.claude-claude"], "ok")


if __name__ == "__main__":
    unittest.main()


class ReviewLifecycleTests(UpdateBase):
    def test_failed_uninstall_keeps_retry_state_and_never_replays_completed_app_undo(self):
        manifest, _ = fake_component(self)
        self.first(components=[manifest])
        module = sys.modules["fake_fakecomp"]
        original = module.remove
        def fail(ctx, effects):
            raise OSError("temporary removal failure")
        module.remove = fail
        gone = self.uninstall(components=[manifest])
        self.assertEqual(gone["outcome"], "failed", gone)
        state = ir.read_state(self.layout)
        self.assertTrue(state["active"] and state["uninstall"]["effects"])
        self.assertEqual(self.update()["reason"], "uninstall-incomplete")
        self.assertEqual(self.rollback()["reason"], "uninstall-incomplete")
        self.assertEqual(installer.resume(home=self.h)["reason"], "uninstall-incomplete")
        remaining = {item["effect_id"] for item in state["uninstall"]["effects"]}
        self.assertFalse(set(gone["removed"]) & remaining)
        module.remove = original
        self.assertEqual(self.uninstall(components=[manifest])["outcome"], "uninstalled")
        self.assertIsNone(ir.read_state(self.layout)["active"])

    def test_active_and_interrupted_update_assets_are_both_removed(self):
        old = self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        original = installer.PHASE_FUNCTIONS["surfaces"]
        def interrupt(run):
            original(run)
            raise KeyboardInterrupt("stop after new surfaces")
        with unittest.mock.patch.dict(installer.PHASE_FUNCTIONS, {"surfaces": interrupt}):
            with self.assertRaises(KeyboardInterrupt):
                self.update()
        state = ir.read_state(self.layout)
        self.assertEqual(state["active"], old["gen_id"])
        self.assertTrue(state["pending"])
        gone = self.uninstall()
        self.assertEqual(gone["outcome"], "uninstalled", gone)
        self.assertFalse(any(self.layout.runtimes.iterdir()))
        self.assertFalse(any(self.layout.kits.iterdir()))
        self.assertFalse(list(self.layout.bin.glob("*")))

    def test_refreshed_component_ownership_survives_rollback_for_final_uninstall(self):
        from eutonos import component_support as cs
        manifest, _ = fake_component(self)
        module = sys.modules["fake_fakecomp"]
        def steps(ctx):
            return [cs.FileStep("fakecomp", "refresh", self.user / "refresh.txt", str(ctx["runtime_python"]), replace=True)]
        module.plan = lambda ctx: cs.plan_steps(ctx, steps(ctx))
        module.apply = lambda ctx, effects: cs.apply_steps(ctx, steps(ctx), effects)
        module.remove = cs.remove_effects
        self.first(components=[manifest])
        make_release(self.releases, "r2", version="1.3.0", body="two")
        self.update(components=[manifest])
        back = self.rollback(components=[manifest])
        self.assertTrue(back["kept_newer"])
        self.assertEqual(self.uninstall(components=[manifest])["outcome"], "uninstalled")
        self.assertFalse((self.user / "refresh.txt").exists())
        self.assertFalse(any(self.layout.runtimes.iterdir()))
        self.assertFalse(any(self.layout.kits.iterdir()))

    def test_resume_interrupted_update_hands_over_after_runtime_and_kits(self):
        self.first()
        make_release(self.releases, "r2", version="1.3.0", body="two")
        def interrupt(run):
            raise KeyboardInterrupt("runtime not yet complete")
        with unittest.mock.patch.dict(installer.PHASE_FUNCTIONS, {"runtime": interrupt}):
            with self.assertRaises(KeyboardInterrupt):
                self.update(handover=True)
        seen = []
        def child(python, gen_id, home, token):
            self.assertNotIn("recovery_contract", ir.read_record(self.layout, gen_id))
            seen.append(ir.done_phases(self.layout, gen_id))
            value = installer.continue_handover(gen_id=gen_id, token=token, home=home, user_home=self.user,
                                               os_name=self.OS, ports=self.ports, components=[], doctor=lambda argv: 0)
            return 0, json.dumps(value), ""
        self.ports.handover_call = child
        result = installer.resume(home=self.h, user_home=self.user, os_name=self.OS, ports=self.ports, components=[])
        self.assertEqual(result["outcome"], "updated", result)
        self.assertEqual(seen, [["runtime", "kits"]])
        self.assertEqual(ir.read_record(self.layout, result["gen_id"])["recovery_contract"], 1)

    def test_failed_first_install_cleanup_stays_pending_for_explicit_uninstall(self):
        manifest, _ = fake_component(self)
        module = sys.modules["fake_fakecomp"]
        original_apply, original_remove = module.apply, module.remove
        def apply(ctx, effects):
            written = original_apply(ctx, effects)
            ctx["effect_sink"](written[0])
            raise RuntimeError("later step failed")
        module.apply = apply
        module.remove = lambda ctx, effects: (_ for _ in ()).throw(OSError("locked"))
        result = self.install(components=[manifest])
        self.assertEqual(result["outcome"], "failed", result)
        self.assertEqual(ir.read_state(self.layout)["pending"]["recovery_action"], "uninstall")
        self.assertEqual(installer.resume(home=self.h)["reason"], "uninstall-incomplete")
        module.remove = original_remove
        self.assertEqual(self.uninstall(components=[manifest])["outcome"], "uninstalled")

    def test_uninstall_person_steps_are_inverse_guidance(self):
        steps = {item["id"]: item["what"] for item in installer._person_reversals()}
        self.assertIn("from Auto", steps["desktop-folder-auto"])
        self.assertIn("remove trust", steps["codex-trust"])
        self.assertIn("back off", steps["desktop-rc-default"])


class DurableRecoveryTests(UpdateBase):
    def test_record_ahead_of_state_after_abort_remains_removable(self):
        manifest, _ = fake_component(self)
        module = sys.modules["fake_fakecomp"]
        apply, remove = module.apply, module.remove
        def fail_apply(ctx, effects):
            written = apply(ctx, effects)
            ctx["effect_sink"](written[0])
            raise OSError("later step failed")
        module.apply = fail_apply
        module.remove = lambda ctx, effects: (_ for _ in ()).throw(OSError("locked"))
        save = ir.write_state
        def fail_recovery_state(layout, state):
            if (state.get("pending") or {}).get("recovery_action") == "uninstall":
                raise OSError("state write stopped after record write")
            return save(layout, state)
        with unittest.mock.patch.object(ir, "write_state", fail_recovery_state):
            with self.assertRaises(OSError):
                self.install(components=[manifest])
        state = ir.read_state(self.layout)
        self.assertFalse(ir.record_intact(self.layout, state, state["pending"]["gen_id"]))
        self.assertEqual(installer.resume(home=self.h, ports=self.ports)["reason"], "uninstall-incomplete")
        module.remove = remove
        self.assertEqual(self.uninstall(components=[manifest])["outcome"], "uninstalled")

    def test_receipt_write_failure_does_not_lose_owned_effects(self):
        with unittest.mock.patch.object(ir, "write_receipt", side_effect=OSError("receipt disk error")):
            result = self.install()
        self.assertEqual(result["outcome"], "rolled-back", result)
        self.assertIsNone(ir.read_state(self.layout)["pending"])
        self.assertFalse(any(self.layout.runtimes.iterdir()))

    def test_process_style_stop_after_runtime_build_has_write_ahead_ownership(self):
        build = self.ports.build_runtime
        def stop(runtime, wheel):
            build(runtime, wheel)
            raise KeyboardInterrupt("no post-build sink")
        self.ports.build_runtime = stop
        with self.assertRaises(KeyboardInterrupt):
            self.install()
        pending = ir.read_state(self.layout)["pending"]
        record = ir.read_record(self.layout, pending["gen_id"])
        self.assertTrue(any(item["effect_id"].startswith("runtime.") for item in record["effects"]))
        self.assertEqual(self.uninstall()["outcome"], "uninstalled")

    def test_partial_runtime_is_retained_as_incomplete(self):
        def partial(runtime, wheel):
            runtime.mkdir(parents=True)
            (runtime / "partial.txt").write_bytes(b"not a complete runtime")
            raise OSError("copy failed")
        self.ports.build_runtime = partial
        result = self.install()
        self.assertEqual(result["outcome"], "failed", result)
        self.assertTrue(result["failed"])
        self.assertEqual(self.uninstall()["outcome"], "failed")
        self.assertTrue(ir.read_state(self.layout)["uninstall"])

    def test_rollback_refuses_a_pre_recovery_engine(self):
        old = self.first()
        state = ir.read_state(self.layout)
        record = ir.read_record(self.layout, old["gen_id"])
        record.pop("recovery_contract")
        ir.write_record(self.layout, state, record)
        ir.write_state(self.layout, state)
        make_release(self.releases, "r2", version="1.3.0", body="two")
        new = self.update()
        self.assertEqual(self.rollback()["reason"], "previous-engine-lacks-recovery")
        self.assertEqual(ir.read_state(self.layout)["active"], new["gen_id"])


class RollbackCleanupTests(UpdateBase):
    def test_failed_new_public_reversal_stays_owned_and_removable(self):
        self.first()
        manifest, _ = fake_component(self)
        make_release(self.releases, "r2", version="1.3.0", body="two")
        self.update(components=[manifest])
        module = sys.modules["fake_fakecomp"]
        remove = module.remove
        module.remove = lambda ctx, effects: (_ for _ in ()).throw(OSError("locked newer effect"))
        result = self.rollback(components=[manifest])
        self.assertEqual(result["outcome"], "failed", result)
        self.assertTrue(ir.read_state(self.layout)["uninstall"])
        self.assertEqual(self.update()["reason"], "uninstall-incomplete")
        module.remove = remove
        self.assertEqual(self.uninstall(components=[manifest])["outcome"], "uninstalled")
        self.assertFalse((self.user / "fakecomp.marker").exists())

    def test_write_ahead_withdrawal_removes_stale_record_effect_before_retry(self):
        self.first()
        state = ir.read_state(self.layout)
        record = ir.read_record(self.layout, state["active"])
        item = record["effects"][0]
        installer._recover_effects(record, [{"event": "effect-withdrawn", "effect_ids": [item["effect_id"]]}])
        self.assertNotIn(item["effect_id"], {row["effect_id"] for row in record["effects"]})


class RunningRuntimeRemovalTests(UpdateBase):
    def test_a_runtime_whose_name_only_starts_like_the_target_is_not_the_running_one(self):
        root = self.base / "runtimes"
        target, sibling = root / "abc-1", root / "abc-10"
        sibling.mkdir(parents=True)
        (sibling / "inner").mkdir()
        target.mkdir()
        for running, inside in ((sibling, False), (target, True), (target / "Scripts", True), (root / "abc", False)):
            self.assertIs(fx._is_inside(running, target), inside, running)
        self.assertFalse(fx._is_inside(self.base / "other", target))
        item = {"undo": {"path": str(target)}, "after_sha256": "x"}
        remover = fx.Remover(self.layout, self.ports, self.user, "windows")
        running = "this is the running runtime; delete the folder after this process exits"
        with unittest.mock.patch.object(sys, "prefix", str(sibling)):
            self.assertNotEqual(remover._dir_delete(item)[1], running)
        with unittest.mock.patch.object(sys, "prefix", str(target)):
            self.assertEqual(remover._dir_delete(item), ("kept", running))

    def test_uninstall_from_the_running_runtime_directs_external_cleanup(self):
        installed = self.first()
        runtime = self.layout.runtimes / installed["artifact_id"]
        with unittest.mock.patch.object(sys, "prefix", str(runtime)):
            result = self.uninstall()
        self.assertEqual(result["outcome"], "failed", result)
        self.assertIn("bundled Python/bootstrap", result["next_action"])
        self.assertIn("Do not retry through the same", result["next_action"])
        self.assertTrue(ir.read_state(self.layout)["uninstall"])
