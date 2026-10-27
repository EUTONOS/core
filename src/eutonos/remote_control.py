"""EUTONOS Remote Control launcher: ``python -I -m eutonos.remote_control`` (IPC5 lane B, permissions entry 1).

It runs ``claude remote-control --permission-mode auto --name "EUTONOS remote"`` so new chats can be started from
claude.ai/code or the phone app. The Startup shortcut (Windows) or launchd item (macOS) that the ``remote-control``
component writes starts it at login with no window; ``--visible`` keeps the console for the one first-run y/n
prompt that the owner answers by hand.

``find_claude`` looks, in order, for ``claude`` on PATH, ``~/.local/bin/claude``, and the newest ``claude`` that
Claude Desktop carries. The Desktop copy is what a machine has when only the Desktop app is installed. Every lookup
and the process start are injectable, so tests touch nothing real.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import home as eutonos_home

SERVER_NAME = "EUTONOS remote"
PERMISSION_MODE = "auto"


def _version_key(path: Path):
    folder = path.parent.parent.name if path.name.lower() == "claude.exe" else path.parent.name
    return [int(piece) if piece.isdigit() else 0 for piece in re.split(r"[.\-]", folder)], str(path)


def desktop_bundled_claude(user_home=None, local_app_data=None) -> str | None:
    """The newest ``claude`` carried by Claude Desktop, or None. Windows: ``Claude-Data/<package>/claude-code/<version>/
    <hash>/claude.exe`` under ``%LOCALAPPDATA%``; macOS: ``Application Support/Claude/claude-code/<version>/claude``.
    Parity copy of ``desktop_bundled_claude`` in the Crosstalk kit (the kit cannot import this package)."""
    home = Path(user_home) if user_home else Path.home()
    local = Path(local_app_data) if local_app_data else Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    found = []
    for base, pattern in ((local / "Claude-Data", "*/claude-code/*/*/claude.exe"),
                          (home / "Library" / "Application Support" / "Claude", "claude-code/*/claude")):
        try:
            found += [path for path in base.glob(pattern) if path.is_file()]
        except OSError:
            continue
    return str(max(found, key=_version_key)) if found else None


def find_claude(*, which=shutil.which, user_home=None, local_app_data=None) -> str | None:
    found = which("claude")
    if found:
        return found
    home = Path(user_home) if user_home else Path.home()
    for name in ("claude.exe", "claude"):
        candidate = home / ".local" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return desktop_bundled_claude(home, local_app_data)


def command(exe: str, *, name: str = SERVER_NAME, settings=None) -> list[str]:
    """The launch argv. ``settings`` (the per-launch ``H/settings/claude-launch.json``) is passed with ``--settings``
    when the file exists, so chats started remotely get the same record-keeping and letter rules as ``create-claude``."""
    argv = [exe, "remote-control", "--permission-mode", PERMISSION_MODE, "--name", name]
    if settings and Path(settings).is_file():
        argv += ["--settings", str(settings)]
    return argv


def working_folder(home=None, user_home=None) -> str:
    """The first added project that still exists (``H/projects.json``), else the user's home."""
    base = Path(home) if home else eutonos_home.home()
    try:
        projects = json.loads((base / "projects.json").read_text(encoding="utf-8")).get("projects") or []
    except (OSError, ValueError, AttributeError):
        projects = []
    for item in projects:
        root = item.get("root") if isinstance(item, dict) else None
        if isinstance(root, str) and Path(root).is_dir():
            return root
    return str(Path(user_home) if user_home else Path.home())


def main(argv=None, *, find=None, run=None, home=None, user_home=None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m eutonos.remote_control", description=__doc__)
    parser.add_argument("--visible", action="store_true", help="keep the console (for the first-run y/n prompt)")
    parser.add_argument("--print", dest="show", action="store_true", help="print the command and exit")
    parser.add_argument("--name", default=SERVER_NAME)
    options = parser.parse_args(argv)
    exe = (find or find_claude)()
    if not exe:
        print("EUTONOS Remote Control: the claude command was not found (not on PATH, in ~/.local/bin or in Claude "
              "Desktop's folder).", file=sys.stderr)
        return 2
    base = Path(home) if home else eutonos_home.home()
    argv_out = command(exe, name=options.name, settings=base / "settings" / "claude-launch.json")
    if options.show:
        print(json.dumps(argv_out))
        return 0
    flags = 0 if options.visible else getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = (run or subprocess.run)(argv_out, cwd=working_folder(base, user_home), creationflags=flags)
    return int(getattr(done, "returncode", 0) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
