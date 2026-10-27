"""The ``remote-control`` component (IPC5 lane B, contract c): start new chats from claude.ai/code or the phone.

It adds a login item that runs ``python -I -m eutonos.remote_control`` with the installed runtime (see
``eutonos.remote_control``): a Startup-folder shortcut on Windows, a launchd agent on macOS. The launcher finds
``claude`` (PATH, ``~/.local/bin``, the copy Claude Desktop carries) and runs it with ``--permission-mode auto``
(permissions entry 1) and ``--settings H/settings/claude-launch.json`` when that file exists.

The launch settings file holds entries 7 and 8 (``autoMode.allow`` only). Whichever component runs first writes
them through ``permissions.apply`` (it is idempotent), so a machine with only Remote Control still has the file,
as an Effect, and a machine that installed Crosstalk first records the Effect under Crosstalk.

The owner answers Remote Control's first-run "Enable Remote Control? (y/n)" by hand, once (it is a person step):
run the launcher with ``--visible``. Seams: ``shortcut``, ``startup_folder``, ``launchctl``
(``(action, plist_path) -> (exit, output)``, action ``bootstrap`` or ``bootout``).
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib

from . import component_support as cs

ID = "remote-control"
LNK = "EUTONOS Remote Control.lnk"
LABEL = "com.eutonos.remote-control"
LAUNCH_ENTRIES = {"letter-rule", "record-keeping-rule"}
FIRST_RUN = ("The first run asks \"Enable Remote Control? (y/n)\"; answer it once by running "
             "`python -I -m eutonos.remote_control --visible` with the EUTONOS runtime.")


def launch_argv(ctx: dict) -> list[str]:
    python = cs.runtime_pythonw(ctx) if cs.os_name(ctx) == "windows" else str(ctx.get("runtime_python") or "")
    return [python, "-I", "-m", "eutonos.remote_control"]


def plist_path(ctx: dict) -> Path:
    return cs.user_home(ctx) / "Library" / "LaunchAgents" / (LABEL + ".plist")


def plist_bytes(ctx: dict) -> bytes:
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": launch_argv(ctx), "RunAtLoad": True,
                           "KeepAlive": False, "WorkingDirectory": str(cs.ctx_home(ctx)),
                           "EnvironmentVariables": {"EUTONOS_HOME": str(cs.ctx_home(ctx))}})


def _launchctl(ctx: dict, action: str, plist: str) -> tuple[int, str]:
    function = cs.seam(ctx, "launchctl")
    if function:
        return function(action, plist)
    uid = os.getuid() if hasattr(os, "getuid") else 501
    verb = ["bootstrap", f"gui/{uid}", plist] if action == "bootstrap" else ["bootout", f"gui/{uid}", plist]
    return cs.run_app(["launchctl"] + verb)


class LaunchdStep(cs.FileStep):
    """The launch agent's plist; written, then loaded for this login (a failed load is a recorded, plain failure)."""

    def __init__(self, ctx: dict):
        super().__init__(ID, "launchd-agent", plist_path(ctx), plist_bytes(ctx), kind="launchd")

    def want(self, ctx):
        return plist_bytes(ctx)

    def write(self, ctx, data) -> None:
        super().write(ctx, data)
        code, output = _launchctl(ctx, "bootstrap", str(self.target))
        if code != 0 and "already" not in output.lower():
            raise OSError(f"launchctl bootstrap failed (exit {code}): {output.strip()[-120:]}")

    def undo(self, ctx, before) -> dict:
        return {"op": "launchd-remove", "plist": str(self.target),
                "created_dirs": [str(item) for item in cs.permissions._missing_dirs(self.target)]}


@cs.undo_handler("launchd-remove")
def _undo_launchd(ctx: dict, effect: dict) -> str | None:
    target = Path(effect["target"])
    raw = cs.permissions._read(target)
    if raw is None:
        return "already gone"
    if cs.sha256(raw) != effect["after_sha256"]:
        return "changed by you, kept"
    _launchctl(ctx, "bootout", str(target))  # not loaded any more is fine
    target.unlink()
    cs.permissions._drop_dirs(effect)
    return None


def _steps(ctx: dict) -> list:
    if cs.os_name(ctx) == "windows":
        argv = launch_argv(ctx)
        return [cs.shortcut_step(ctx, ID, "login-shortcut", LNK,
                                 {"target": argv[0], "arguments": " ".join(argv[1:]), "cwd": str(cs.ctx_home(ctx)),
                                  "description": "EUTONOS Remote Control: start new Claude chats from the web or phone"},
                                 ours=lambda spec: "eutonos.remote_control" in spec.get("arguments", ""))]
    return [LaunchdStep(ctx)]


def status(ctx: dict) -> dict:
    return {"state": "installed", "reason": ""}


def plan(ctx: dict) -> list[dict]:
    effects = cs.permission_effects(dict(ctx, dry_run=True), ID, write=False, only=LAUNCH_ENTRIES, per_project=False,
                                    entries_from={"crosstalk", "core"})  # the same order as apply: settings, then login item
    effects += cs.plan_steps(ctx, _steps(ctx))
    return effects


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    done: list = []
    try:
        wanted = None if effects is None else {item.get("permission_entry") for item in effects} - {None}
        only = LAUNCH_ENTRIES if wanted is None else LAUNCH_ENTRIES & wanted
        if only:
            cs.permission_effects(ctx, ID, write=True, only=only, per_project=False, done=done,
                                  entries_from={"crosstalk", "core"})
        cs.apply_steps(ctx, _steps(ctx), effects, done)
    except cs.ComponentApplyError as exc:
        raise cs.ComponentApplyError(str(exc), exc.effects or done) from exc
    return done


def verify(ctx: dict) -> dict:
    checks = cs.step_checks(ctx, ID, _steps(ctx))
    settings = cs.ctx_home(ctx) / "settings" / "claude-launch.json"
    checks.append({"name": "launch settings", "ok": settings.is_file(),
                   "detail": "H/settings/claude-launch.json is present" if settings.is_file()
                   else "H/settings/claude-launch.json is absent"})
    checks.append({"name": "first run", "ok": True, "detail": FIRST_RUN})
    return cs.verify_result(ID, checks)


def remove(ctx: dict, effects: list) -> dict:
    return cs.remove_effects(ctx, effects)
