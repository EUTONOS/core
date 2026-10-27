"""``eutonos-simon``: run Simon (the Windows computer-use helper) from the EUTONOS home.

Simon ships as the ``simon`` kit (``H/kits/simon-<hash>``, an immutable copy of ``tools/simon``) and runs in its own
Python environment ``H/simon/env``, which the Simon part builds from the wheels bundled in the kit (Pillow and
pyautogui; nothing is downloaded). Runtime data goes to ``H/simon/runtime``, never into the kit.

``eutonos-simon <command> [args]`` runs ``helperctl.py <command> [args]`` with that environment and returns its exit
code; the command list is ``eutonos-simon helper.commands --detail full``. ``eutonos-simon --where`` prints a read-only
JSON diagnosis (kit, Quickstart, environment, runtime folder, whether the part is on). The launcher refuses visibly
(exit 2, one sentence on stderr) when it is not on Windows, the Simon part is not installed or is switched off, there is
no verified Simon kit, or Simon's environment has not been built.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from . import component_state
from . import home as eutonos_home
from . import kits

COMPONENT = "simon"
ENV_MARKER = "simon-env.json"
RUNTIME_ENV = "SIMON_RUNTIME_DIR"


def env_dir(home: Path) -> Path:
    return Path(home) / "simon" / "env"


def env_python(home: Path) -> Path:
    folder = env_dir(home)
    return folder / "Scripts" / "python.exe" if os.name == "nt" else folder / "bin" / "python"


def runtime_dir(home: Path) -> Path:
    return Path(home) / "simon" / "runtime"


def diagnose(home=None, *, platform: str | None = None) -> dict:
    """What the launcher would run, and the first reason it would refuse (``problem`` None means ready). Read-only."""
    base = Path(home) if home else eutonos_home.home()
    platform = platform or sys.platform
    kit = kits.active_kit(base, COMPONENT)
    python = env_python(base)
    try:
        enabled = component_state.component_enabled(base, COMPONENT, default=False)
    except component_state.ComponentStateError as exc:
        enabled, state_problem = False, f"the parts file cannot be read ({exc})"
    else:
        state_problem = None
    problem = None
    if platform != "win32":
        problem = "Simon runs on Windows only."
    elif state_problem:
        problem = state_problem
    elif not enabled:
        problem = ("the Simon part of EUTONOS is not switched on. Tick Simon in EUTONOS Setup, or run "
                   "`eutonos-install component enable simon` when it is installed but off.")
    elif kit is None:
        problem = f"there is no verified Simon kit under {base / 'kits'}. Run an EUTONOS update or repair."
    elif not (kit / "helperctl.py").is_file():
        problem = f"the Simon kit at {kit} has no helperctl.py."
    elif not python.is_file() or not (env_dir(base) / ENV_MARKER).is_file():
        problem = (f"Simon's Python environment at {env_dir(base)} has not been built. Switch the Simon part off and "
                   "on again (`eutonos-install component disable simon`, then `enable simon`), or run an update.")
    return {"profile": "eutonos.simon-where.v1", "home": str(base), "enabled": enabled,
            "kit": None if kit is None else str(kit),
            "quickstart": None if kit is None else str(kit / "QUICKSTART.md"),
            "skill": None if kit is None else str(kit / ".claude" / "skills" / "simon-planner" / "SKILL.md"),
            "python": str(python), "runtime": str(runtime_dir(base)), "ready": problem is None, "problem": problem}


def command(info: dict, argv: list[str]) -> list[str]:
    """The child command: Simon's own interpreter without the user site or bytecode files, then helperctl.py."""
    return [info["python"], "-s", "-B", str(Path(info["kit"]) / "helperctl.py"), *argv]


def child_env(info: dict, environ=None) -> dict:
    env = dict(os.environ if environ is None else environ)
    env[RUNTIME_ENV] = info["runtime"]
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # the kit is immutable once copied; helperctl's own children inherit this
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(name, None)
    return env


def main(argv=None, *, home=None, run=None, platform: str | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    info = diagnose(home, platform=platform)
    if args[:1] == ["--where"]:
        print(json.dumps(info, indent=2))
        return 0 if info["ready"] else 2
    if info["problem"]:
        print("eutonos-simon: " + info["problem"], file=sys.stderr)
        return 2
    Path(info["runtime"]).mkdir(parents=True, exist_ok=True)
    done = (run or subprocess.run)(command(info, args), env=child_env(info))
    return int(getattr(done, "returncode", 1) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
