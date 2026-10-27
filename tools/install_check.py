"""EUTONOS install check (IPC5 lane A): prove the install, uninstall and reinstall on temporary folders only.

It builds the wheel from a `git archive HEAD` subset in a throwaway git repository, installs it into a temporary
EUTONOS home with a temporary user home (seeded Claude, Codex and shell files) and fake OS and app ports, verifies the
result, uninstalls, checks every user file is byte-identical to the seed and no carrier is left, then installs and
uninstalls again to prove the reinstall is idempotent. On Windows it also installs over a scheduled task that is already
there under the companion's name: one EUTONOS registered earlier is replaced and put back on uninstall; one that is not
EUTONOS's is left alone by both (the fake task seam answers ``owned`` and refuses a foreign task as the real runtime
does). Nothing here reaches the real home, registry, Startup folder,
scheduled tasks, PATH or the Claude, Codex and Hermes apps: every OS and app call is a recording fake.

    python tools/install_check.py [--root CHECKOUT] [--real-venv] [--keep]

`--real-venv` builds the runtime as a real venv (a real `venv` and `pip` process, no index); everything else stays fake.
Exit 0 when every step passed, 1 otherwise. The report is JSON on stdout.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types

ROOT = Path(__file__).resolve().parents[1]
SUBSET = ("pyproject.toml", "src", "tools/crosstalk", "tools/simon", "tools/build_wheel.py", "docs/Crosstalk/QUICKSTART.md", "docs/Simon")
SEED = {
    ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"}, indent=4) + "\n",
    ".claude/CLAUDE.md": "# my rules\n",
    ".codex/config.toml": 'model = "x"\n',
    ".codex/AGENTS.md": "mine\n",
    ".zprofile": "export A=1\n",
}
ENV_KEYS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "EUTONOS_HOME",
            "CROSSTALK_P0_DEVICE_DIR", "CROSSTALK_P0_HERMES_HOME", "CROSSTALK_P0_CLAUDE_HOME", "PYTHONPATH")
MARKERS = ("crosstalk", "eutonos")  # words that must not remain in a user file after uninstall


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(folder) -> dict:
    """``{relative path: bytes}`` for every file under `folder`."""
    folder = Path(folder)
    return {item.relative_to(folder).as_posix(): item.read_bytes() for item in sorted(folder.rglob("*")) if item.is_file()}


class Machine:
    """Recording fakes for everything outside the temp folders: the user PATH, Startup shortcuts, the scheduled task,
    the claude, hermes and launchctl commands, the licence and the gate (the shapes the components read as seams)."""

    def __init__(self, base: Path, user: Path, hermes: Path):
        self.base, self.user, self.hermes = base, user, hermes
        self.calls: list = []
        self.env = r"C:\Windows;C:\Tools"
        self.shortcuts: dict = {}
        self.task = None
        self.task_description = None  # what the task of that name says about itself; None while there is no task
        self.task_triggers = "logon"  # the part of the definition the person may edit (triggers, conditions, settings)

    def seed_task(self, task, description=None) -> None:
        """Put a scheduled task of the companion's name on this machine as it is before EUTONOS is installed:
        ``task`` is ``(command, arguments)`` and ``description`` is what that task says about itself (EUTONOS's own
        task carries ``desktop_companion_runtime.TASK_DESCRIPTION``). ``None`` clears it."""
        self.task, self.task_description = task, description if task is not None else None

    def task_xml(self):
        """The task's exported definition, as the real ``export_task`` gives it: command, arguments and the rest of the
        definition. None while there is no task."""
        if self.task is None:
            return None
        from xml.sax.saxutils import escape
        return ("<?xml version='1.0' encoding='UTF-16'?><Task xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task'>"
                f"<RegistrationInfo><Date>{len(self.calls)}</Date><URI>EUTONOS Desktop Companion</URI></RegistrationInfo>"
                f"<Triggers>{escape(self.task_triggers)}</Triggers><Actions><Exec><Command>{escape(self.task[0])}</Command>"
                f"<Arguments>{escape(self.task[1])}</Arguments></Exec></Actions></Task>")

    def task_owned(self) -> bool:
        """Whether the task of the companion's name is EUTONOS's: the test the real ``_task_owned`` applies (its
        description, and an ensure action of the companion runtime), answered from what this machine holds, which is
        what ``register`` wrote or what ``seed_task`` put there."""
        from eutonos import desktop_companion_runtime as runtime
        if self.task is None:
            return False
        arguments = self.task[1]
        return (self.task_description == runtime.TASK_DESCRIPTION and "eutonos.desktop_companion_runtime" in arguments
                and "--ensure-current" in arguments)

    def claude_run(self, argv):
        """The Claude app's registry files, written the way `claude plugin ...` would write them."""
        self.calls.append(("claude", list(argv)))
        args = list(argv[1:])
        registry = self.user / ".claude" / "plugins" / "known_marketplaces.json"
        settings = self.user / ".claude" / "settings.json"
        known = json.loads(registry.read_text(encoding="utf-8")) if registry.is_file() else {}
        document = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
        if args[:3] == ["plugin", "marketplace", "add"]:
            # Claude names a marketplace by the name in its own manifest, not by the folder it was added from.
            manifest = Path(str(args[3])) / ".claude-plugin" / "marketplace.json"
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name") if manifest.is_file() else None
            known[name or os.path.basename(str(args[3]))] = {"source": args[3]}
        elif args[:3] == ["plugin", "marketplace", "remove"]:
            known.pop(args[3], None)
        elif args[:2] == ["plugin", "install"]:
            document.setdefault("enabledPlugins", {})[args[2]] = True
        elif args[:2] == ["plugin", "disable"]:  # the plugin stays installed, its enabledPlugins flag turns false
            if args[2] not in document.get("enabledPlugins", {}):
                return 1, "plugin not installed"
            document["enabledPlugins"][args[2]] = False
        elif args[:2] == ["plugin", "enable"]:
            if args[2] not in document.get("enabledPlugins", {}):
                return 1, "plugin not installed"
            document["enabledPlugins"][args[2]] = True
        elif args[:2] == ["plugin", "uninstall"]:
            document.get("enabledPlugins", {}).pop(args[2], None)
            if not document.get("enabledPlugins", True):
                document.pop("enabledPlugins")
        if known:
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(json.dumps(known, indent=2) + "\n", encoding="utf-8", newline="\n")
        elif registry.exists():
            registry.unlink()
        if settings.exists() or document:
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps(document, indent=4) + "\n", encoding="utf-8", newline="\n")
        return 0, "ok"

    def hermes_runner(self, command, **_kwargs):
        self.calls.append(("hermes", [Path(command[0]).name] + list(command[1:])))
        config = self.hermes / "config.yaml"
        raw = config.read_text(encoding="utf-8") if config.exists() else ""
        try:
            value = json.loads(raw)
        except ValueError:
            value = {"original_text": raw}
        entry = value.setdefault("plugins", {}).setdefault("entries", {}).setdefault("crosstalk", {})
        if command[1:3] in (["plugins", "enable"], ["plugins", "disable"]):
            entry["enabled"] = command[2] == "enable"
        elif command[1:3] == ["config", "set"]:
            entry["allow_gateway_injection"] = command[-1] == "true"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stderr="")

    def launchctl(self, action, plist):
        self.calls.append(("launchctl", action, Path(plist).name))
        return 0, ""

    def seams(self) -> dict:
        def create(spec):
            self.calls.append(("shortcut-create", spec["path"]))
            Path(spec["path"]).parent.mkdir(parents=True, exist_ok=True)
            self.shortcuts[spec["path"]] = {"target": spec["target"], "arguments": spec["arguments"], "cwd": spec["cwd"]}
            Path(spec["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(spec["path"]).write_bytes(json.dumps(self.shortcuts[spec["path"]]).encode("utf-8"))

        def delete(path):
            self.calls.append(("shortcut-delete", path))
            self.shortcuts.pop(path, None)
            Path(path).unlink(missing_ok=True)

        def shortcut_restore(path, raw, spec):
            Path(path).write_bytes(raw)
            self.shortcuts[path] = dict(spec)

        def set_env(value):
            self.calls.append(("env-set", value))
            self.env = value

        def register(argv):
            from eutonos import desktop_companion_runtime as runtime
            self.calls.append(("task-register", list(argv)))
            if self.task is not None and not self.task_owned():  # as the real register_task does
                raise OSError("Companion task name belongs to another task")
            self.task = (argv[0], subprocess.list2cmdline(argv[1:]))
            self.task_description = runtime.TASK_DESCRIPTION
            self.task_triggers = "logon"  # a new registration is a fresh definition

        def drop():
            self.calls.append(("task-delete",))
            if self.task is not None and not self.task_owned():  # as the real delete_task does
                raise OSError("Companion task name belongs to another task")
            gone = self.task is not None
            self.seed_task(None)
            return gone

        return {"env_path": {"get": lambda: self.env, "set": set_env},
                "shortcut": {"read": lambda path: dict(self.shortcuts[path]) if path in self.shortcuts else None,
                             "create": create, "delete": delete, "restore": shortcut_restore},
                "task": {"query": lambda: self.task, "register": register, "delete": drop, "owned": self.task_owned,
                         "export": self.task_xml},
                "claude_exe": "claude-fake", "claude_run": self.claude_run,
                "hermes_home": str(self.hermes), "hermes_runner": self.hermes_runner,
                "launchctl": self.launchctl, "startup_folder": str(self.base / "Startup"),
                "licence_state": None, "gate_open": False}

    def state(self) -> str:
        return json.dumps([self.env, self.shortcuts, self.task, self.task_description], sort_keys=True, default=str)


class Report:
    def __init__(self):
        self.steps: list[dict] = []

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(item["ok"] for item in self.steps)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_subset(root: Path, repo: Path) -> None:
    """A clean committed copy of the tracked files the wheel needs: `git archive HEAD` of SUBSET into a new repo."""
    done = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", "HEAD", *SUBSET], capture_output=True)
    if done.returncode:
        raise RuntimeError("git archive failed: " + done.stderr.decode("utf-8", "replace")[-300:])
    repo.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        archive.extractall(repo, filter="data")
    git = ["git", "-C", str(repo), "-c", "user.name=install-check", "-c", "user.email=check@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "install check"]):
        done = subprocess.run(git + argv, capture_output=True)
        if done.returncode:
            raise RuntimeError("git " + argv[0] + " failed: " + done.stderr.decode("utf-8", "replace")[-300:])


@contextlib.contextmanager
def isolated_environment(base: Path, user: Path):
    """Point every variable the code reads for a home or app folder at the temporary tree; restore on exit."""
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ("PYTHONPATH", "CROSSTALK_P0_DEVICE_DIR", "CROSSTALK_P0_HERMES_HOME", "CROSSTALK_P0_CLAUDE_HOME"):
            os.environ.pop(key, None)
        os.environ.update(HOME=str(user), USERPROFILE=str(user), APPDATA=str(base / "appdata"),
                          LOCALAPPDATA=str(base / "localappdata"), CODEX_HOME=str(user / ".codex"),
                          CLAUDE_CONFIG_DIR=str(user / ".claude"), EUTONOS_HOME=str(base / "eutonos-home"))
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_ports(os_name: str, startup: Path, seams: dict, real_venv: bool):
    from eutonos import installer_ports as iports
    if not real_venv:
        ports = iports.FakePorts(os_name, startup=startup)
    else:
        class VenvPorts(iports.FakePorts):
            """Fake everywhere except the runtime: a real `venv` and `pip install --no-index` of the wheel."""

            def run(self, argv, **kwargs):
                if str(argv[0]) == sys.executable or Path(str(argv[0])).name.lower().startswith("python"):
                    return iports.Ports.run(self, argv, **kwargs)
                return super().run(argv, **kwargs)

            def build_runtime(self, runtime, wheel):
                iports.Ports.build_runtime(self, runtime, wheel)

        ports = VenvPorts(os_name, startup=startup)
    ports.seam_map = seams
    return ports


def effect_signature(record: dict) -> list:
    return sorted((item["effect_id"], item["kind"], item["target"], item.get("after_sha256"))
                  for item in record["effects"])


def leftovers(base: Path, user: Path, home: Path, seeded: dict) -> list[str]:
    """Carriers still present after an uninstall: launchers, the marketplace, a word of ours in a user file, or a
    file that was not in the seed."""
    found = []
    bin_dir = home / "bin"
    if bin_dir.is_dir() and any(bin_dir.iterdir()):
        found.append("launchers left in " + str(bin_dir))
    if (home / "marketplace" / "crosstalk").exists():
        found.append("marketplace folder left")
    for name, data in snapshot(user).items():
        text = data.decode("utf-8", "replace").lower()
        if name not in seeded:
            found.append("new user file left: " + name)
        elif any(word in text for word in MARKERS):
            found.append("a carrier word is left in " + name)
    return found


def run(root=ROOT, *, real_venv: bool = False, keep: bool = False, os_name: str | None = None, work=None) -> dict:
    """Run the whole check; returns ``{outcome, steps, work}``. Never raises for a failed check."""
    report = Report()
    root = Path(root).resolve()
    work = Path(work) if work else Path(tempfile.mkdtemp(prefix="eutonos-install-check-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    try:
        _run(report, root, work, real_venv, os_name)
    except Exception as exc:  # a crash is a failed check with its reason, not a traceback
        report.step("check completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return {"outcome": "passed" if report.passed else "failed", "steps": report.steps,
            "work": str(work) if keep else None}


def _run(report: Report, root: Path, work: Path, real_venv: bool, os_name):
    user = work / "user"
    home = work / "eutonos-home"
    hermes = work / "hermes"
    for relative, text in SEED.items():
        target = user / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
    (hermes / "bin").mkdir(parents=True)
    (hermes / "bin" / "hermes.cmd").write_text("@echo off\n", encoding="utf-8", newline="\n")
    (hermes / "config.yaml").write_text("model: x\n", encoding="utf-8", newline="\n")
    seeded = snapshot(user)
    seeded_hermes = snapshot(hermes)
    with isolated_environment(work, user):
        from eutonos import consent, install_record as ir, installer, installer_effects as fx, permissions
        repo = work / "repo"
        archive_subset(root, repo)
        builder = load_module("install_check_build_wheel", repo / "tools" / "build_wheel.py")
        wheel = builder.build(repo, work / "dist")
        report.step("wheel built from a clean git archive", wheel.is_file(), wheel.name)
        os_name = os_name or ("windows" if sys.platform == "win32" else "macos")
        machine = Machine(work, user, hermes)
        ports = make_ports(os_name, work / "startup", machine.seams(), real_venv)
        if hasattr(ports, "has"):
            ports.has["hermes"] = str(hermes / "bin" / "hermes.cmd")
        before_machine = machine.state()
        data = permissions.load()
        text = consent.render(None, user, os_name=os_name, data=data)
        accepted = {"text_id": consent.TEXT_ID, "text_sha256": consent.text_sha256(text), "surface": "install-check",
                    "accepted_at": "2026-10-22T00:00:00Z",
                    "choices": {entry["id"]: "on" for entry in permissions.entries(data)
                                if entry["default"] == "on" and entry["writer"] in ("installer", "launch-flag")}}
        ready = lambda: {"outcome": "ready", "missing": []}  # noqa: E731 - the check does not probe the real machine
        common = dict(home=home, user_home=user, os_name=os_name, ports=ports)

        planned = installer.install(wheel, consent=accepted, prereq=ready, dry_run=True, **common)
        report.step("dry run plans and changes nothing", planned.get("outcome") == "dry-run"
                    and snapshot(user) == seeded and not home.exists(), str(planned.get("outcome")))

        first = installer.install(wheel, consent=accepted, prereq=ready, **common)
        report.step("install", first.get("outcome") == "installed", json.dumps(first, default=str)[:300])
        if os_name == "windows":
            hermes_row = next((row for row in first.get("components", []) if row["id"] == "hermes"), {})
            report.step("Hermes is selected, installed and its CLI changed the temporary config",
                        hermes_row.get("state") == "installed" and hermes_row.get("enabled") is True
                        and any(call[0] == "hermes" for call in machine.calls)
                        and (hermes / "config.yaml").read_bytes() != seeded_hermes["config.yaml"],
                        json.dumps(hermes_row, default=str))
        if first.get("outcome") != "installed":
            return
        state = installer.status(home=home)
        record = ir.read_record(ir.Layout(home), state["active"])
        report.step("record is intact and active", bool(state["active_record"]["intact"]) and record["state"] == "active",
                    state["active"])
        _verify(report, home, user, os_name, ports, record, wheel, installer, fx)
        installed_tree = snapshot(user)
        installed_effects = effect_signature(record)

        again = installer.install(wheel, consent=accepted, prereq=ready, **common)
        report.step("a second install is refused and changes nothing",
                    again.get("outcome") == "refused" and again.get("reason") == "already-installed"
                    and snapshot(user) == installed_tree, str(again.get("reason")))

        gone = installer.uninstall(**common)
        report.step("uninstall", gone.get("outcome") == "uninstalled", json.dumps(gone, default=str)[:300])
        _after_uninstall(report, "uninstall", home, user, seeded, machine, before_machine, ports, hermes, seeded_hermes)

        second = installer.install(wheel, consent=accepted, prereq=ready, **common)
        report.step("reinstall", second.get("outcome") == "installed", json.dumps(second, default=str)[:300])
        if second.get("outcome") == "installed":
            state = installer.status(home=home)
            record = ir.read_record(ir.Layout(home), state["active"])
            report.step("reinstall is idempotent: same Effects and the same user files",
                        effect_signature(record) == installed_effects and snapshot(user) == installed_tree,
                        "effects differ" if effect_signature(record) != installed_effects
                        else ("user files differ" if snapshot(user) != installed_tree else ""))
            _toggle_cycle(report, machine, lambda: installer.install(wheel, consent=accepted, prereq=ready, **common),
                          home, user, os_name, seeded, before_machine, ports, hermes, seeded_hermes, common,
                          installer, ir, installed_effects, installed_tree)
            _patch_cycle(report, repo, wheel, work, home, user, common, installer, fx, ir)
            gone = installer.uninstall(**common)
            report.step("second uninstall", gone.get("outcome") == "uninstalled", json.dumps(gone, default=str)[:300])
            _after_uninstall(report, "second uninstall", home, user, seeded, machine, before_machine, ports, hermes,
                             seeded_hermes)
            if os_name == "windows":  # the companion's scheduled task exists only there
                _task_scenarios(report, machine, lambda: installer.install(wheel, consent=accepted, prereq=ready, **common),
                                lambda: installer.uninstall(**common), home, user, seeded, installer, ir)


# The scheduled task of the companion's name as someone else may already have it. EUTONOS replaces a task of that name
# only when it is its own: an ensure action of the companion runtime that also carries EUTONOS's description.
OLDER_COMPANION_TASK = (r"C:\old\pythonw.exe", "-I -m eutonos.desktop_companion_runtime --ensure-current")
FOREIGN_TASKS = (
    ("a task that runs something else", (r"C:\Tools\backup.exe", "--nightly"), "My nightly backup"),
    ("a task that runs our command under its own description", OLDER_COMPANION_TASK, "My own recovery task"),
)


def _companion_effects(home, installer, ir) -> list:
    state = installer.status(home=home)
    record = ir.read_record(ir.Layout(home), state["active"])
    return [item for item in record["effects"] if item["kind"] == "scheduled-task"]


def _task_scenarios(report, machine, install, uninstall, home, user, seeded, installer, ir):
    """A scheduled task of the companion's name that is already there, installed over and uninstalled. A task that
    EUTONOS registered is replaced and put back on uninstall; one that is not EUTONOS's (another command, or our command
    under another description) is left alone by both. The machine's task seam answers ``owned`` and refuses to register
    over or delete a task that is not EUTONOS's, as the real runtime does, so a step that touches one fails the install."""
    from eutonos import desktop_companion_runtime as runtime
    for label, task, description in FOREIGN_TASKS:
        machine.seed_task(task, description)
        seeded_machine, calls = machine.state(), len(machine.calls)
        done = install()
        touched = [call for call in machine.calls[calls:] if call[0] in ("task-register", "task-delete")]
        installed = done.get("outcome") == "installed"
        effects = _companion_effects(home, installer, ir) if installed else []
        report.step(f"companion task, {label}: install leaves it alone",
                    installed and (machine.task, machine.task_description) == (task, description) and not touched
                    and not effects,
                    json.dumps({"outcome": done.get("outcome"), "touched": touched, "effects": len(effects)}, default=str)[:300])
        if installed:
            gone = uninstall()
            report.step(f"companion task, {label}: uninstall leaves it as it was",
                        gone.get("outcome") == "uninstalled" and machine.state() == seeded_machine
                        and snapshot(user) == seeded and not leftovers(home.parent, user, home, seeded),
                        json.dumps(gone, default=str)[:300])
        machine.seed_task(None)
        if not installed:
            return
    machine.seed_task(OLDER_COMPANION_TASK, runtime.TASK_DESCRIPTION)
    seeded_machine = machine.state()
    done = install()
    installed = done.get("outcome") == "installed"
    effects = _companion_effects(home, installer, ir) if installed else []
    report.step("companion task, one EUTONOS registered earlier: install replaces it and keeps it for uninstall",
                installed and machine.task is not None and machine.task != OLDER_COMPANION_TASK and machine.task_owned()
                and len(effects) == 1 and (effects[0].get("undo") or {}).get("op") == "task-restore",
                json.dumps({"outcome": done.get("outcome"), "task": machine.task, "effects": effects}, default=str)[:300])
    if installed:
        gone = uninstall()
        report.step("companion task, one EUTONOS registered earlier: uninstall puts it back",
                    gone.get("outcome") == "uninstalled" and machine.state() == seeded_machine
                    and snapshot(user) == seeded and not leftovers(home.parent, user, home, seeded),
                    json.dumps({"outcome": gone.get("outcome"), "task": machine.task}, default=str)[:300])
    machine.seed_task(None)


def _stable(signature: list) -> list:
    """The Effect signature without the after-hash of Effects that share a file with another Effect: that hash is of
    the whole file at the moment the Effect was applied, so it depends on the order the Effects were applied in (a part
    switched back on is applied after the others). The final file is compared separately, byte for byte."""
    shared = {}
    for _, _, target, _ in signature:
        shared[target] = shared.get(target, 0) + 1
    return [(name, kind, target, after if shared[target] == 1 else None) for name, kind, target, after in signature]


def _difference(now: list, before: list) -> str:
    """Which Effects differ between two signatures, as short text."""
    added = [item[0] for item in now if item not in before]
    gone = [item[0] for item in before if item not in now]
    return "effects differ: now only " + ", ".join(added) + "; before only " + ", ".join(gone)


def _components(home, installer) -> dict:
    """``{id: row}`` of the parts as ``component status`` reports them."""
    found = installer.component_status(home=home)
    return {item["id"]: item for item in found.get("components", [])}


def _toggle_cycle(report, machine, reinstall, home, user, os_name, seeded, before_machine, ports, hermes, seeded_hermes,
                  common, installer, ir, installed_effects, installed_tree):
    """Every part switched off, back on, then a mix left off while uninstalling: the parts really leave and return,
    the install record always matches what is present, and uninstall still gives back every user file byte for byte."""
    layout = ir.Layout(home)
    parts = [name for name, item in _components(home, installer).items() if item["toggleable"] and item["enabled"]]
    report.step("component status lists the switchable parts, all on", bool(parts), ", ".join(parts))
    if not parts:
        return

    def record():
        return ir.read_record(layout, installer.status(home=home)["active"])

    def flip(names, enable):
        # The generation records components in dependency order. Disable dependents
        # first; admitting Hermes now makes its Crosstalk dependency real here.
        order = {row["id"]: index for index, row in enumerate(record()["components"])}
        names = sorted(names, key=lambda name: order[name], reverse=not enable)
        verb = "enable" if enable else "disable"
        answers = [installer.set_component(name, enable, **common) for name in names]
        bad = [f"{name}: {answer.get('outcome')} {answer.get('reason') or ''}".strip() for name, answer in zip(names, answers)
               if answer.get("outcome") != ("enabled" if enable else "disabled")]
        again = [installer.set_component(name, enable, **common).get("outcome") for name in names]
        report.step(f"component {verb} on {', '.join(names)}", not bad, "; ".join(bad))
        report.step(f"component {verb} a second time changes nothing", set(again) == {"unchanged"}, ", ".join(again))

    flip(parts, False)
    state = _components(home, installer)
    report.step("every switched-off part reads as off, installed and recorded",
                all(state[name]["enabled"] is False and state[name]["state"] == "installed" for name in parts)
                and all(row.get("enabled") is False for row in record()["components"] if row["id"] in parts),
                json.dumps({name: state[name]["enabled"] for name in parts}))
    off_files = " ".join(text.decode("utf-8", "replace").lower() for name, text in snapshot(user).items()
                         if name in seeded and name != ".claude/settings.json")
    leftover = [word for word in ("crosstalk_p0", "eutonos:crosstalk") if word in off_files]
    report.step("switched off: no Crosstalk block or Codex entry, Startup shortcut or task is left",
                ("crosstalk" not in parts or not leftover) and not machine.shortcuts
                and (machine.task is None or "companion" not in parts),
                json.dumps({"shortcuts": sorted(machine.shortcuts), "task": machine.task}, default=str)[:200])
    plugins = json.loads((user / ".claude" / "settings.json").read_text(encoding="utf-8")).get("enabledPlugins", {})
    report.step("switched off: the Claude plugin stays installed but disabled",
                "crosstalk" not in parts or list(plugins.values()) == [False], json.dumps(plugins))
    report.step("switched off: the Hermes folder is as seeded", "hermes" not in parts or snapshot(hermes) == seeded_hermes, "")
    flip(parts, True)
    report.step("switched back on: the same Effects and the same user files as the first install",
                _stable(effect_signature(record())) == _stable(installed_effects) and snapshot(user) == installed_tree,
                _difference(_stable(effect_signature(record())), _stable(installed_effects))
                if _stable(effect_signature(record())) != _stable(installed_effects)
                else ("user files differ" if snapshot(user) != installed_tree else ""))
    report.step("switched back on: the machine is as the first install left it", True if os_name != "windows" else
                bool(machine.shortcuts) and (machine.task is not None or "companion" not in parts), "")
    mixed = [name for name in parts if name in ("crosstalk", "hermes")] or parts[:1]
    flip(mixed, False)
    gone = installer.uninstall(**common)
    report.step("uninstall with some parts switched off", gone.get("outcome") == "uninstalled",
                json.dumps(gone, default=str)[:300])
    _after_uninstall(report, "uninstall after switching parts", home, user, seeded, machine, before_machine, ports, hermes,
                     seeded_hermes)
    report.step("uninstall removes the component state file", not (home / "settings" / "components.json").exists(), "")
    back = reinstall()
    report.step("reinstall after the toggles", back.get("outcome") == "installed", json.dumps(back, default=str)[:300])
    if back.get("outcome") == "installed":
        report.step("reinstall after the toggles: the same Effects and the same user files",
                    effect_signature(record()) == installed_effects and snapshot(user) == installed_tree, "")


def _commit(repo: Path, note: str) -> str:
    """Append a line to the checkout's package and commit it (a local patch's fix); returns the new commit."""
    with open(repo / "src" / "eutonos" / "__init__.py", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"# {note}\n")
    git = ["git", "-C", str(repo), "-c", "user.name=install-check", "-c", "user.email=check@example.invalid",
           "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    for argv in (["add", "-A"], ["commit", "-q", "-m", note]):
        done = subprocess.run(git + argv, capture_output=True)
        if done.returncode:
            raise RuntimeError("git " + argv[0] + " failed: " + done.stderr.decode("utf-8", "replace")[-300:])
    return subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _patch_cycle(report, repo, wheel, work, home, user, common, installer, fx, ir):
    """Patch, rollback, patch, official update, final rollback and uninstall restore the seed."""
    stub = lambda argv: 0  # noqa: E731 - doctor is exercised by its own tests
    kept = snapshot(user)
    fix = _commit(repo, "local patch: the fix")
    planned = installer.patch(checkout=repo, reason="install check fix", dry_run=True, handover=False, doctor=stub, **common)
    report.step("patch dry run builds the wheel and changes nothing",
                planned.get("outcome") == "dry-run" and snapshot(user) == kept
                and "eutonos/__init__.py" in (planned.get("local_patch") or {}).get("changed_files", []),
                json.dumps(planned, default=str)[:300])
    patched = installer.patch(checkout=repo, reason="install check fix", handover=False, doctor=stub, **common)
    meta = patched.get("local_patch") or {}
    report.step("patch installs a new generation marked local",
                patched.get("outcome") == "patched" and meta.get("source_commit") == fix and meta.get("dirty") is False
                and installer.status(home=home).get("local_patch_line") == patched.get("status_line"),
                json.dumps(patched, default=str)[:300])
    if patched.get("outcome") != "patched":
        return
    back = installer.rollback(official=True, **common)
    report.step("rollback --official returns to the official generation",
                back.get("outcome") == "rolled-back-to" and (back.get("left_local_patch") or {}).get("source_commit") == fix
                and "local_patch_line" not in installer.status(home=home), json.dumps(back, default=str)[:300])
    again = installer.patch(checkout=repo, reason="install check fix, again", handover=False, doctor=stub, **common)
    report.step("patch again", again.get("outcome") == "patched", json.dumps(again, default=str)[:300])
    later = _commit(repo, "a later commit")
    info = fx.inspect_wheel(wheel)
    release = work / "official" / "r1"
    release.mkdir(parents=True)
    (release / wheel.name).write_bytes(wheel.read_bytes())
    (release / "eutonos-release.json").write_text(json.dumps({
        "profile": "eutonos.release.v1", "version": info["version"], "artifact_id": info["artifact_id"],
        "wheel": wheel.name, "sha256": info["sha256"], "channels": [installer.status(home=home)["channel"]],
        "min_compatible": None, "source_commit": later}), encoding="utf-8")
    newer = installer.update(source=release.parent, handover=False, doctor=stub, allow_unsigned=True, **common)
    verdict = (newer.get("replaced_local_patch") or {}).get("fix")
    report.step("an official update replaces the patch and proves the fix is included",
                newer.get("outcome") == "updated" and verdict == "included"
                and "local_patch_line" not in installer.status(home=home), json.dumps(newer, default=str)[:300])
    final_back = installer.rollback(**common)
    report.step("final rollback keeps refreshed effect ownership for uninstall",
                final_back.get("outcome") == "rolled-back-to", json.dumps(final_back, default=str)[:300])


def _verify(report, home, user, os_name, ports, record, wheel, installer, fx):
    layout_bin = home / "bin"
    launchers = sorted(item.name for item in layout_bin.iterdir()) if layout_bin.is_dir() else []
    wanted = ports.launcher_name("eutonos-crosstalk")
    report.step("stable launchers are in H/bin", wanted in launchers, ", ".join(launchers[:6]))
    hook = home / "marketplace" / "crosstalk" / "plugin" / "hooks" / "register.mjs"
    text = hook.read_text(encoding="utf-8") if hook.is_file() else ""
    report.step("the Claude hook runs the launcher (carriers point only into H)",
                bool(text) and "const CLI = '-'" in text and json.dumps(str(layout_bin))[1:-1] in text,
                str(hook))
    run_ = installer.Run(home, kind="verify", ports=ports, user_home=user, os_name=os_name)
    run_.record, run_.info = record, fx.inspect_wheel(wheel)
    problems = []
    for row in record.get("components", []):
        if row.get("state") != "installed":
            continue
        if row.get("enabled") is False:
            continue  # installed but switched off: its outside-the-home parts are deliberately absent
        manifest = run_.component(row["id"])
        function = installer._entry_function(manifest, "verify", None) if manifest else None
        if function is None:
            continue
        outcome = function(run_.component_ctx(manifest))
        if isinstance(outcome, dict) and (outcome.get("state") not in (None, "ok") or outcome.get("ok") is False):
            problems.append(f"{row['id']}: {outcome.get('state') or outcome.get('ok')}")
    report.step("every installed component verifies", not problems, "; ".join(problems))


def _after_uninstall(report, label, home, user, seeded, machine, before_machine, ports, hermes, seeded_hermes):
    now = snapshot(user)
    changed = sorted(name for name in seeded if now.get(name) != seeded[name])
    report.step(f"{label}: user files are byte-identical to the seed", not changed, ", ".join(changed))
    now_hermes = snapshot(hermes)
    changed = sorted((set(now_hermes) ^ set(seeded_hermes))
                     | {name for name in seeded_hermes if now_hermes.get(name) != seeded_hermes[name]})
    report.step(f"{label}: the Hermes folder is byte-identical to the seed", not changed, ", ".join(changed))
    found = leftovers(work_of(home), user, home, seeded)
    report.step(f"{label}: no carriers are left", not found, "; ".join(found))
    private = [str(path) for folder in (home / "runtimes", home / "kits") if folder.exists() for path in folder.iterdir()]
    report.step(f"{label}: no retained runtime or kit is left", not private, "; ".join(private))
    report.step(f"{label}: no PATH, shortcut, Run key or task is left", machine.state() == before_machine
                and not ports.runkeys and not ports.shortcuts, "")
    report.step(f"{label}: data stays (hosts, backups and the install log are never deleted)",
                (home / "install").is_dir(), str(home / "install"))


def work_of(home: Path) -> Path:
    return home.parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout whose HEAD is built (default: this one)")
    parser.add_argument("--real-venv", action="store_true", help="build the runtime as a real venv (no index)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary tree and print its path")
    args = parser.parse_args(argv)
    source = (args.root.resolve() / "src")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    result = run(args.root, real_venv=args.real_venv, keep=args.keep)
    print(json.dumps(result, indent=2))
    return 0 if result["outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
