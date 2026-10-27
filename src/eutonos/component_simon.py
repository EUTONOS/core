"""The Simon component: Simon (the Windows computer-use helper) installed from the ``simon`` kit.

Off by default; the person ticks it in Setup. When it is on:

* ``H/simon/env``: Simon's own Python environment, built from the runtime's interpreter with only the wheels the kit
  bundles (``wheels/``, listed with their sha256 in ``wheels.lock.json``): ``pip install --no-index --no-deps``, so
  nothing is downloaded. Its identity is the marker ``H/simon/env/simon-env.json``. A kit with no locked wheels, or
  with a locked wheel missing, makes the part ``skipped`` with a visible reason; nothing is installed.
* The ``simon-planner`` skill (``SKILL.md`` and the Quickstart beside it) for each app that is present:
  ``~/.claude/skills/simon-planner/``, ``~/.codex/skills/simon-planner/`` and, when Hermes is found,
  ``<Hermes home>/skills/eutonos/simon-planner/``. A file of that name that EUTONOS did not write is the person's own
  and is left alone.
* When Codex is present, one Codex rule (``~/.codex/rules/eutonos-simon.rules``, permissions entry
  ``simon-codex-rule``) that lets Codex run only the installed ``eutonos-simon`` command outside its sandbox, because
  Simon needs the real desktop. A rules file of that name that EUTONOS did not write is left alone.

The wheels are built for one CPython (the lock's ``python`` tag, ``cp314-win_amd64``: the Python Setup carries and
builds the EUTONOS runtime from). A runtime on another Python makes the part ``skipped`` with that reason.

The ``eutonos-simon`` launcher, the instruction block (``simon_instructions``) and the end-turn reminder line
(``role_hooks``) follow the part's on/off state. Switching it off removes the skills (they live outside the EUTONOS
home) and keeps the environment and Simon's runtime data inside the home; uninstall removes the environment. Simon's
development-only Claude ``SessionEnd`` hook is not installed (docs/Simon/INSTALL.md says why).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig

from . import component_support as cs
from . import component_hermes
from . import kits as kits_module
from . import simon_launch

ID = "simon"
SKILL = "simon-planner"
LOCK = "wheels.lock.json"
LOCK_PROFILE = "eutonos.kit-wheels.v1"
ENV_PROFILE = "eutonos.simon-env.v1"
HERMES_CATEGORY = "eutonos"
HERMES_CATEGORY_TEXT = "EUTONOS skills: tools that ship with the EUTONOS installation.\n"
CODEX_RULES = "eutonos-simon.rules"
CODEX_RULE_ENTRY = "simon-codex-rule"  # permissions.json declares it; this part writes it (gated on the part being on)
CODEX_RULE_PATTERN = ["eutonos-simon"]
CODEX_RULES_TEXT = (
    "# Added by EUTONOS (permissions entry simon-codex-rule) while its Simon part is on. Allows only the installed\n"
    "# eutonos-simon command, which needs the real Windows desktop; every other command keeps Codex's normal sandbox\n"
    "# and approval rules. Switching the Simon part off or uninstalling EUTONOS removes this file.\n"
    "prefix_rule(\n"
    "    pattern = [\"eutonos-simon\"],\n"
    "    decision = \"allow\",\n"
    "    justification = \"EUTONOS Simon: desktop control the Codex sandbox cannot see.\",\n"
    "    match = [[\"eutonos-simon\", \"helper.bridge_status\"], [\"eutonos-simon\", \"helper.exit\", \"--via-app\"]],\n"
    "    not_match = [[\"python\", \"helperctl.py\"], [\"eutonos-crosstalk\", \"halt\"]],\n"
    ")\n")
TAG = re.compile(r"cp(\d)(\d+)-(\w+)")


# ----------------------------------------------------------------------------- kit
def kit_folder(ctx: dict) -> Path | None:
    """The Simon kit: the context's ``kit_path`` when it is a Simon kit, else the active one in the home."""
    given = ctx.get("kit_path")
    if given:
        record = kits_module._read_json(Path(given) / kits_module.KIT_FILE)
        if isinstance(record, dict) and record.get("component") == ID:
            return Path(given)
    return kits_module.active_kit(cs.ctx_home(ctx), ID)


def wheel_lock(kit: Path) -> dict:
    """``{file: sha256}`` from the kit's lock; raises ``ComponentError`` when it is missing or malformed."""
    try:
        lock = json.loads((kit / LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise cs.ComponentError(f"the Simon kit's {LOCK} cannot be read ({type(exc).__name__})") from exc
    wheels = lock.get("wheels") if isinstance(lock, dict) else None
    if not isinstance(lock, dict) or lock.get("profile") != LOCK_PROFILE or not isinstance(wheels, dict):
        raise cs.ComponentError(f"the Simon kit's {LOCK} is not a {LOCK_PROFILE} lock")
    return dict(wheels)


def lock_python(kit: Path) -> str | None:
    """The CPython tag the kit's wheels were locked for (``cp314-win_amd64``), or None when the lock names none."""
    try:
        value = json.loads((kit / LOCK).read_text(encoding="utf-8")).get("python")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and TAG.fullmatch(value) else None


def runtime_tag(ctx: dict) -> str:
    """``cpXY-<platform>`` of the interpreter Simon's environment is built from: the runtime's ``pyvenv.cfg``
    version when it can be read, else this process (the installer runs inside the runtime). Seam ``python_tag``."""
    seam = cs.seam(ctx, "python_tag")
    if seam is not None:
        return seam(ctx)
    version = sys.version_info[:2]
    python = ctx.get("runtime_python")
    if python:
        try:
            for line in (Path(python).parent.parent / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                found = re.match(r"\s*(\d+)\.(\d+)", value) if key.strip() in ("version", "version_info") else None
                if found:
                    version = (int(found.group(1)), int(found.group(2)))
                    break
        except OSError:
            pass
    return f"cp{version[0]}{version[1]}-" + sysconfig.get_platform().replace("-", "_").replace(".", "_")


NOT_IN_BUILD = "the Simon kit is not part of this EUTONOS build"


def _kit_problem(kit: Path) -> tuple[dict, str | None]:
    """``(locked wheels, problem)`` for a kit folder: its lock reads, names wheels, and every wheel is present."""
    try:
        lock = wheel_lock(kit)
    except cs.ComponentError as exc:
        return {}, str(exc)
    if not lock:
        return {}, ("this EUTONOS build does not bundle Simon's Python packages (Pillow, pyautogui) yet, so "
                    "Simon cannot be set up here (in a EUTONOS source checkout, docs/Simon/INSTALL.md explains how to bundle them)")
    missing = sorted(name for name in lock if not (kit / "wheels" / name).is_file())
    if missing:
        return lock, "the Simon kit is missing bundled wheel(s): " + ", ".join(missing)
    return lock, None


def bundled_problem() -> str | None:
    """Why the kit bundled in this build cannot be prepared (no kit, a bad lock, missing wheels), else None. Read only,
    before anything is installed (Setup's parts page); the Python-tag check needs the runtime and stays in ``readiness``."""
    kit = kits_module.source_dir(ID)
    if not kit.is_dir():
        return NOT_IN_BUILD
    return _kit_problem(kit)[1]


def readiness(ctx: dict) -> tuple[Path | None, dict, str | None]:
    """``(kit, locked wheels, problem)``; ``problem`` None means the environment can be built offline."""
    kit = kit_folder(ctx)
    if kit is None:
        return None, {}, NOT_IN_BUILD
    lock, problem = _kit_problem(kit)
    if problem:
        return kit, lock, problem
    wanted, have = lock_python(kit), runtime_tag(ctx)
    if wanted and wanted != have:
        return kit, lock, (f"Simon's bundled Python packages are built for {wanted}, but this EUTONOS runs on {have}; "
                           "install EUTONOS with Setup (it carries the matching Python)")
    return kit, lock, None


def status(ctx: dict) -> dict:
    _kit, _lock, problem = readiness(ctx)
    return {"state": "skipped", "reason": problem} if problem else {"state": "installed", "reason": ""}


# ---------------------------------------------------------------------- environment
def _env_identity(ctx: dict, lock: dict) -> dict:
    return {"profile": ENV_PROFILE, "python": str(ctx.get("runtime_python") or sys.executable),
            "wheels": dict(sorted(lock.items()))}


def _real_build(ctx: dict, folder: Path, kit: Path, lock: dict) -> None:
    """Build ``folder`` as a fresh venv from the runtime interpreter and install the kit's wheels offline."""
    python = str(ctx.get("runtime_python") or sys.executable)
    done = subprocess.run([python, "-I", "-m", "venv", str(folder)], capture_output=True, text=True,
                          timeout=300, creationflags=cs.HIDDEN)
    if done.returncode != 0:
        raise OSError("python -m venv failed: " + (done.stderr or done.stdout or "")[-200:])
    child = simon_launch.env_python(folder.parent.parent)
    wheels = [str(kit / "wheels" / name) for name in sorted(lock)]
    done = subprocess.run([str(child), "-I", "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir",
                           "--disable-pip-version-check", "--no-warn-script-location", *wheels],
                          capture_output=True, text=True, timeout=600, creationflags=cs.HIDDEN)
    if done.returncode != 0:
        raise OSError("pip install of the bundled Simon wheels failed: " + (done.stderr or done.stdout or "")[-200:])


class EnvStep(cs.FileStep):
    """``H/simon/env``, identified by its marker file. EUTONOS owns the folder outright: a different marker (another
    runtime or other wheels) rebuilds it; the marker is written last, so a half-built folder reads as absent."""

    def __init__(self, ctx: dict, kit: Path, lock: dict):
        self.folder = simon_launch.env_dir(cs.ctx_home(ctx))
        self.kit, self.lock = kit, lock
        super().__init__(ID, "environment", self.folder / simon_launch.ENV_MARKER,
                         cs.canonical(_env_identity(ctx, lock)), replace=True)

    def write(self, ctx, data) -> None:
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        (cs.seam(ctx, "simon_env") or _real_build)(ctx, self.folder, self.kit, self.lock)
        cs.permissions._write_atomic(self.target, data)

    def undo(self, ctx, before) -> dict:
        return {"op": "simon-env-delete", "folder": str(self.folder)}

    def backup(self, ctx, before):
        return None  # a rebuilt environment is never restored; the wheels in the kit are its source


@cs.undo_handler("simon-env-delete")
def _undo_env(ctx: dict, effect: dict) -> str | None:
    folder = Path(effect["undo"]["folder"])
    current = cs.permissions._read(folder / simon_launch.ENV_MARKER)
    if current is None and not folder.exists():
        return "already gone"
    if current is not None and cs.sha256(current) != effect.get("after_sha256"):
        return "the Simon environment was rebuilt by something else; kept"
    shutil.rmtree(folder)
    return None


# --------------------------------------------------------------------------- skills
class SkillFile(cs.FileStep):
    """A skill file EUTONOS wrote. An update refreshes it while it still holds the bytes EUTONOS recorded; a file
    of that name EUTONOS did not write is the person's own and is left alone."""

    def may_replace(self, ctx, current) -> bool:
        return any(item.get("effect_id") == self.effect_id for item in cs.recorded(ctx, ID))


def skill_folders(ctx: dict) -> list[tuple[str, Path]]:
    """``(app, skill folder)`` for each app that is present on this machine."""
    home = cs.user_home(ctx)
    found = []
    if (home / ".claude").is_dir():
        found.append(("claude", home / ".claude" / "skills" / SKILL))
    if (home / ".codex").is_dir():
        found.append(("codex", home / ".codex" / "skills" / SKILL))
    if component_hermes.found(ctx):
        found.append(("hermes", component_hermes.hermes_home(ctx) / "skills" / HERMES_CATEGORY / SKILL))
    return found


def codex_rules_file(ctx: dict) -> Path | None:
    """``~/.codex/rules/eutonos-simon.rules`` when Codex is present, else None."""
    codex = cs.user_home(ctx) / ".codex"
    return codex / "rules" / CODEX_RULES if codex.is_dir() else None


def _steps(ctx: dict) -> list:
    kit, lock, problem = readiness(ctx)
    if problem:
        return []
    steps = [EnvStep(ctx, kit, lock)]
    skill = (kit / ".claude" / "skills" / SKILL / "SKILL.md").read_bytes()
    quickstart = (kit / "QUICKSTART.md").read_bytes()
    for app, folder in skill_folders(ctx):
        steps.append(SkillFile(ID, f"{app}-skill", folder / "SKILL.md", skill))
        steps.append(SkillFile(ID, f"{app}-quickstart", folder / "QUICKSTART.md", quickstart))
        if app == "hermes":
            steps.append(SkillFile(ID, "hermes-category", folder.parent / "DESCRIPTION.md", HERMES_CATEGORY_TEXT))
    rules = codex_rules_file(ctx)
    if rules is not None:
        steps.append(SkillFile(ID, "codex-rule", rules, CODEX_RULES_TEXT, entry=CODEX_RULE_ENTRY))
    return steps


# ---------------------------------------------------------------------- contract (c)
def plan(ctx: dict) -> list[dict]:
    return cs.plan_steps(ctx, _steps(ctx))


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    """Build Simon's environment and put the skill beside each app. Idempotent; nothing is written when the kit's
    wheels are not bundled (``status`` has already said why)."""
    return cs.apply_steps(ctx, _steps(ctx), effects)


def verify(ctx: dict) -> dict:
    _kit, _lock, problem = readiness(ctx)
    if problem:
        return cs.verify_result(ID, [{"name": "simon kit", "ok": False, "detail": problem}], state="held")
    return cs.verify_result(ID, cs.step_checks(ctx, ID, _steps(ctx)))


def remove(ctx: dict, effects: list) -> dict:
    return cs.remove_effects(ctx, effects)
