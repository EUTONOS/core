"""Prerequisite check for installing EUTONOS: detect, report, guide; never install.

Python 3.12+ is required. Git is required by prereq_check but optional for an install
(install_prereq_check): only the parts that declare it are limited without it. Node,
Claude, Codex and Hermes are only reported. Keep this module stdlib-only.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

MIN_PYTHON = (3, 12)
OPTIONAL_TOOLS = ("node", "claude", "codex", "hermes")
GUIDANCE = {
    "python": {
        "windows": "Install Python 3.12 or newer from https://www.python.org/downloads/ or run: winget install Python.Python.3.12",
        "macos": "Install Python 3.12 or newer from https://www.python.org/downloads/ or run: brew install python@3.12",
        "linux": "Install Python 3.12 or newer with your package manager, or from https://www.python.org/downloads/",
    },
    "git": {
        "windows": "Install git from https://git-scm.com/download/win or run: winget install Git.Git",
        "macos": "Install git with: xcode-select --install (or brew install git)",
        "linux": "Install git with your package manager, for example: sudo apt install git",
    },
}
OPTIONAL_GUIDANCE = {
    "node": "Optional. Install Node.js from https://nodejs.org/ if you want the tools that need it.",
    "claude": "Optional. Install Claude Code from https://claude.com/claude-code to use EUTONOS with Claude.",
    "codex": "Optional. Install Codex from https://developers.openai.com/codex to use EUTONOS with Codex.",
    "hermes": "Optional. Hermes is only needed to message Hermes chats through Crosstalk.",
}


def _os_key(platform):
    return "windows" if platform.startswith("win") else "macos" if platform == "darwin" else "linux"


def _probe(path, run):
    try:
        done = run([path, "--version"], capture_output=True, text=True, timeout=10,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout or done.stderr or ""
    lines = text.strip().splitlines() if isinstance(text, str) else []
    return lines[0].strip()[:120] if lines else None


def prereq_check(*, which=None, version_info=None, platform=None, run=None, probe_optional=True):
    """Report what is present. Reads PATH and runs `--version` only; changes nothing."""
    which = shutil.which if which is None else which
    run = subprocess.run if run is None else run
    info = tuple(sys.version_info[:3]) if version_info is None else tuple(version_info)
    key = _os_key(sys.platform if platform is None else platform)
    rows = []
    python_ok = info[:2] >= MIN_PYTHON
    rows.append({"name": "python", "required": True, "ok": python_ok,
                 "version": ".".join(str(part) for part in info), "path": sys.executable,
                 "guidance": None if python_ok else GUIDANCE["python"][key]})
    git_path = which("git")
    rows.append({"name": "git", "required": True, "ok": bool(git_path),
                 "version": _probe(git_path, run) if git_path and probe_optional else None, "path": git_path,
                 "guidance": None if git_path else GUIDANCE["git"][key]})
    for name in OPTIONAL_TOOLS:
        path = which(name)
        rows.append({"name": name, "required": False, "ok": bool(path),
                     "version": _probe(path, run) if path and probe_optional else None, "path": path,
                     "guidance": None if path else OPTIONAL_GUIDANCE[name]})
    missing = [row["name"] for row in rows if row["required"] and not row["ok"]]
    return {"profile": "eutonos.prereq.v1", "outcome": "missing" if missing else "ready",
            "missing": missing, "installs": "none", "requirements": rows,
            "guidance": [row["guidance"] for row in rows if row["guidance"] and row["required"]]}


def prereq_lines(report):
    lines = []
    for row in report["requirements"]:
        state = "ok" if row["ok"] else "MISSING" if row["required"] else "not found"
        detail = row["version"] or (row["path"] if row["ok"] else "")
        lines.append("%s %s%s%s" % ("[required]" if row["required"] else "[optional]", row["name"] + ":", " " + state,
                                    " (" + detail + ")" if detail else ""))
    if report["missing"]:
        lines.append("EUTONOS needs: " + ", ".join(report["missing"]) + ". Nothing was installed. What to do:")
        lines.extend("  - " + line for line in report["guidance"])
    else:
        lines.append("All required tools are present. Nothing was installed.")
    return lines


# What the installed package uses (the bootstrap copy above keeps git required). ISU stage 6: Git is needed only by the
# parts whose manifest says ``requires.tools: ["git"]``, so a missing Git limits those parts and never refuses the install.

def install_prereq_check(**kwargs):
    """`prereq_check` with Git relaxed: only Python is required to install. The git row stays in the report (with its
    guidance) so Setup, the doctor and the installer can say which parts are unavailable without it."""
    report = prereq_check(**kwargs)
    for row in report["requirements"]:
        if row["name"] == "git":
            row["required"] = False
            row["needed_by_parts"] = True
    missing = [row["name"] for row in report["requirements"] if row["required"] and not row["ok"]]
    report.update(outcome="missing" if missing else "ready", missing=missing,
                  guidance=[row["guidance"] for row in report["requirements"] if row["guidance"] and row["required"]])
    return report


def git_row(report):
    return next((row for row in report["requirements"] if row["name"] == "git"), None)


def unavailable_without_git(report, parts):
    """The `parts` (``[{id, name}]``) that cannot work here because Git was not found; ``[]`` when Git is present."""
    row = git_row(report)
    return [] if row is None or row["ok"] else list(parts)


def without_git_line(parts):
    """One plain sentence naming what is unavailable without Git (empty when `parts` is empty). The core part is never
    skipped, so it is described as losing its project memory features, not as unavailable."""
    core = any(item["id"] == "core" for item in parts)
    others = ", ".join(item["name"] for item in parts if item["id"] != "core")
    if not core and not others:
        return ""
    lost = []
    if core:
        lost.append("the project memory features (boards, notes and delivery) are unavailable")
    if others:
        lost.append("these parts are skipped: " + others)
    return ("Git was not found. Until you install it, " + " and ".join(lost) + ". "
            "Everything else installs and works without it.")


def install_prereq_lines(report, parts=()):
    lines = prereq_lines(report)
    git = git_row(report)
    if git is not None and not git["ok"]:
        lines = [line for line in lines if not line.startswith("[optional] git:")]
        lines.append("[needed by some parts] git: not found")
        note = without_git_line(unavailable_without_git(report, parts))
        lines.append(note or "Git was not found. Parts that use Git are unavailable until you install it.")
        lines.append("  - " + (git["guidance"] or ""))
    return lines


def parts_needing_git():
    """``[{id, name}]`` for the installed package's parts that declare ``requires.tools: ["git"]`` ([] if unreadable)."""
    try:
        from . import component_support
        from .installer import load_components
        return component_support.parts_needing(load_components(), "git")
    except Exception:  # the list is a courtesy; the check itself never depends on it
        return []


def main(argv=None):
    report = install_prereq_check()
    parts = unavailable_without_git(report, parts_needing_git())
    print("\n".join(install_prereq_lines(report, parts)))
    return 2 if report["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
