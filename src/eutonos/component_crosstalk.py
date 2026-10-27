"""The ``crosstalk`` component (IPC5 lane B, contract c): letters between Claude, Codex and Hermes chats.

What it adds, each as a recorded Effect:

* the rendered Claude marketplace folder ``H/marketplace/crosstalk`` (the kit's own ``plugin_text`` and
  ``render_plugin``; the folder is generated, so it is rewritten when it differs);
* NOT the Claude plugin registration: the installer's ``register_plugin`` is its one writer (``claude plugin
  marketplace add`` and ``claude plugin install``, recorded as ``app-command`` Effects). This part reads that
  registration back, switches the plugin off and on (``claude plugin disable`` / ``enable``), and still removes the
  ``claude-command`` Effects an earlier version of this part recorded;
* on Windows, the "Crosstalk helper" Startup shortcut to ``H/bin/eutonos-crosstalk-helper`` ``--watch`` (a shortcut
  that is not recognisably EUTONOS's is the person's own and is left alone; macOS follows in IPC8);
* the crosstalk entries of the permissions list through ``permissions.apply``: entries 4 and 6 in ``~/.claude``,
  10 in ``~/.codex`` (the only user-wide writes), 7 in ``H/settings/claude-launch.json``, and per project entry 5
  with its ``.git/info/exclude`` line.

The kit is loaded from ``ctx["kit_path"]`` (it must sit under ``H/kits``, as ``eutonos.kits.materialize`` puts it).
Seams: ``claude_exe`` (path or callable), ``claude_run`` (``argv -> (exit, output)``), ``shortcut``,
``startup_folder``. ``user_home`` and the other keys are described in ``component_support``.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import component_support as cs

ID = "crosstalk"
PLUGIN_ID = "crosstalk-p0@crosstalk-p0-local"
MARKETPLACE = "crosstalk-p0-local"
HELPER_LNK = "Crosstalk helper.lnk"
LAUNCHER = "eutonos-crosstalk-helper"


# ------------------------------------------------------------------------ the Claude app
def claude_exe(ctx: dict) -> str | None:
    value = cs.seam(ctx, "claude_exe")
    if callable(value):
        value = value()
    if value:
        return str(value)
    from . import remote_control
    return remote_control.find_claude(user_home=cs.user_home(ctx))


def claude_dir(ctx: dict) -> Path:
    return cs.user_home(ctx) / ".claude"


def claude_run(ctx: dict, argv: list) -> tuple[int, str]:
    return (cs.seam(ctx, "claude_run") or cs.run_app)(argv)


def _json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def marketplace_known(ctx: dict):
    """The marketplace Claude has registered, read from its own registry file (never from a command)."""
    registry = _json(claude_dir(ctx) / "plugins" / "known_marketplaces.json")
    return {"marketplace": MARKETPLACE} if MARKETPLACE in registry else None


def plugin_enabled(ctx: dict):
    enabled = _json(claude_dir(ctx) / "settings.json").get("enabledPlugins")
    return {"plugin": PLUGIN_ID} if isinstance(enabled, dict) and enabled.get(PLUGIN_ID) else None


def plugin_present(ctx: dict):
    """The plugin is installed in Claude, whether switched on or off (``enabledPlugins`` holds the key either way)."""
    enabled = _json(claude_dir(ctx) / "settings.json").get("enabledPlugins")
    return {"plugin": PLUGIN_ID} if isinstance(enabled, dict) and PLUGIN_ID in enabled else None


@cs.undo_handler("claude-command")
def _undo_claude(ctx: dict, effect: dict) -> str | None:
    """Only an Effect an earlier version of this part recorded carries this undo; the installer records its own."""
    undo = effect["undo"]
    # "plugin" asks whether the plugin is still installed, not whether it is switched on: a part that was turned off
    # leaves it installed but disabled, and uninstall must still remove it.
    probe = {"marketplace": marketplace_known, "plugin": plugin_present}[undo["probe"]]
    if probe(ctx) is None:
        return "already gone"
    exe = claude_exe(ctx)
    if not exe:
        return "the claude command was not found; kept"
    code, _output = claude_run(ctx, [exe] + undo["argv"])
    if code != 0 or probe(ctx) is not None:
        return f"claude {' '.join(undo['argv'][:3])} did not remove it; kept"
    return None


# ------------------------------------------------------------------------ marketplace
class RenderStep(cs.FileStep):
    """``H/marketplace/crosstalk/plugin/hooks/register.mjs``, rendered by the kit for this runtime and CLI."""

    def __init__(self, ctx: dict):
        base = cs.ctx_home(ctx) / "marketplace" / "crosstalk"
        super().__init__(ID, "marketplace", base / "plugin" / "hooks" / "register.mjs", "", replace=True)
        self.base = base

    def _paths(self, ctx):
        """No interpreter and no CLI: a materialized kit renders the stable H/bin launcher (IPC5 F6)."""
        return None, None

    def want(self, ctx):
        install = cs.kit_module(ctx, "crosstalk_p0_install")
        runtime, cli = self._paths(ctx)
        with cs.home_env(ctx):
            return install.plugin_text(runtime, cli).encode("utf-8")

    def write(self, ctx, data) -> None:
        kits = (cs.ctx_home(ctx) / "kits").resolve()
        if kits not in Path(ctx["kit_path"]).resolve().parents:
            raise cs.ComponentError("the Crosstalk kit is not under the EUTONOS home, so the marketplace was not rendered")
        install = cs.kit_module(ctx, "crosstalk_p0_install")
        runtime, cli = self._paths(ctx)
        with cs.home_env(ctx):
            install.render_plugin(runtime, cli)

    def undo(self, ctx, before) -> dict:
        return {"op": "marketplace-remove", "folder": str(self.base)}


@cs.undo_handler("marketplace-remove")
def _undo_marketplace(ctx: dict, effect: dict) -> str | None:
    import shutil
    folder = Path(effect["undo"]["folder"])
    hook = Path(effect["target"])
    try:
        raw = hook.read_bytes()
    except FileNotFoundError:
        return "already gone"
    if cs.sha256(raw) != effect["after_sha256"]:
        return "changed by you, kept"
    shutil.rmtree(folder)  # generated copy of the kit's plugin folder; the kit itself and the Claude registry stay
    return None


# ------------------------------------------------------------------------------ steps
def _helper_ours(spec: dict) -> bool:
    return LAUNCHER in spec.get("target", "") or "crosstalk_helper.py" in spec.get("arguments", "")


def _steps(ctx: dict) -> list:
    steps: list = [RenderStep(ctx)]
    if cs.os_name(ctx) == "windows":
        install = cs.kit_module(ctx, "crosstalk_p0_install")
        launcher = cs.launcher_path(ctx, LAUNCHER)
        steps.append(cs.shortcut_step(
            ctx, ID, "helper-shortcut", install.HELPER_SHORTCUT_NAME,
            {"target": str(launcher), "arguments": "--watch", "cwd": str(launcher.parent),
             "description": install.HELPER_SHORTCUT_DESCRIPTION}, ours=_helper_ours))
    return steps


def status(ctx: dict) -> dict:
    return {"state": "installed", "reason": ""}


def plan(ctx: dict) -> list[dict]:
    """The Effects ``apply`` would produce; writes nothing."""
    steps = _steps(ctx)
    effects = cs.plan_steps(ctx, steps[:1])  # the same order as apply: marketplace, permissions, helper
    effects += cs.permission_effects(dict(ctx, dry_run=True), ID, write=False)
    effects += cs.plan_steps(ctx, steps[1:])
    return effects


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    """Render the marketplace, add the helper shortcut and the permission entries (the installer registers the plugin).
    Idempotent; on a failure the raised ``ComponentApplyError`` carries every Effect already written."""
    done: list = []
    try:
        steps = _steps(ctx)
        cs.apply_steps(ctx, steps[:1], effects, done)  # the marketplace first: the plugin commands read it
        cs.permission_effects(ctx, ID, write=True, only=_only(effects), done=done)
        cs.apply_steps(ctx, steps[1:], effects, done)
    except cs.ComponentApplyError as exc:
        raise cs.ComponentApplyError(str(exc), exc.effects or done) from exc
    return done


def _only(effects):
    if effects is None:
        return None
    return {item.get("permission_entry") for item in effects} - {None}


# ------------------------------------------------------------- switching the part off and on
def _inside_home(ctx: dict, effect: dict) -> bool:
    import os
    target = str(effect.get("target") or "")
    if not target or not os.path.isabs(target):
        return False
    home = os.path.normcase(os.path.abspath(cs.ctx_home(ctx)))
    path = os.path.normcase(os.path.abspath(target))
    return path == home or path.startswith(home + os.sep)


def _is_claude_command(effect: dict) -> bool:
    return (effect.get("undo") or {}).get("op") == "claude-command"


def _messaging(ctx: dict, value: str | None = None) -> str:
    """Read (and with `value`, set) the Crosstalk messaging switch through the kit's own controls code."""
    core = cs.kit_module(ctx, "crosstalk_p0_core")
    with cs.home_env(ctx):
        before = core.controls_load()["messaging"]
        if value is not None:
            core.controls_update(messaging=value)
    return before


def disable(ctx: dict, effects: list) -> dict:
    """Switch Crosstalk off. Its Startup shortcut and the permission entries outside the EUTONOS home are undone
    exactly as uninstall would; the rendered marketplace and ``H/settings`` stay. The Claude plugin is switched off
    with ``claude plugin disable --scope user`` (it stays installed and registered, so uninstall still removes it);
    if that command is missing or fails, the plugin is left on and Crosstalk messaging is turned off instead.
    ``detail`` is what ``enable`` needs to put it back."""
    outside = [item for item in effects if not _inside_home(ctx, item) and not _is_claude_command(item)]
    result = cs.remove_effects(ctx, outside)
    detail: dict = {}
    notes: list = []
    exe = claude_exe(ctx)
    if plugin_present(ctx) is None:
        detail["plugin"] = "not-registered"
    elif plugin_enabled(ctx) is None:
        detail = dict(ctx.get("toggle_detail") or {}) or {"plugin": "already-disabled"}
    else:
        code, output = claude_run(ctx, [exe, "plugin", "disable", PLUGIN_ID, "--scope", "user"]) if exe else (127, "")
        if code == 0 and plugin_enabled(ctx) is None:
            detail["plugin"] = "disabled"
        else:
            before = _messaging(ctx, "off")
            detail.update({"plugin": "messaging-off", "messaging_before": before})
            notes.append("The Claude plugin could not be switched off (" + (output.strip()[-100:] or "no claude command")
                         + "), so Crosstalk messaging was turned off instead.")
    return {"removed": result["removed"], "kept": result["kept"], "detail": detail, "notes": notes}


def enable(ctx: dict) -> dict:
    """Undo what ``disable`` did to the Claude plugin or to messaging, before ``apply`` puts the rest back."""
    detail = ctx.get("toggle_detail") or {}
    if plugin_present(ctx) is not None and plugin_enabled(ctx) is None:
        exe = claude_exe(ctx)
        if not exe:
            raise cs.ComponentError("the claude command was not found, so the Crosstalk plugin was not switched on")
        code, output = claude_run(ctx, [exe, "plugin", "enable", PLUGIN_ID, "--scope", "user"])
        if code != 0:
            raise cs.ComponentError(f"claude plugin enable failed (exit {code}): {output.strip()[-120:]}")
    if detail.get("plugin") == "messaging-off" and detail.get("messaging_before") == "on":
        _messaging(ctx, "on")
    return {"detail": {}}


def verify(ctx: dict) -> dict:
    checks = []
    kit = ctx.get("kit_path")
    from . import kits
    checks.append({"name": "kit", "ok": bool(kit) and kits.verify(Path(kit)),
                   "detail": "the Crosstalk kit verifies" if kit and kits.verify(Path(kit)) else "no verified kit"})
    checks += cs.step_checks(ctx, ID, _steps(ctx))
    if claude_exe(ctx):  # read back what the installer registered, from Claude's own files
        checks.append({"name": "claude-marketplace", "ok": marketplace_known(ctx) is not None,
                       "detail": "present" if marketplace_known(ctx) is not None else "absent"})
        checks.append({"name": "claude-plugin", "ok": plugin_enabled(ctx) is not None,
                       "detail": "present" if plugin_enabled(ctx) is not None else "absent"})
    missing = cs.permission_missing(ctx, ID)
    checks.append({"name": "permission entries", "ok": not missing,
                   "detail": "all present" if not missing else "missing: " + ", ".join(missing)})
    if cs.os_name(ctx) != "windows":
        checks.append({"name": "helper", "ok": True, "detail": "the helper on macOS follows in IPC8"})
    return cs.verify_result(ID, checks)


def remove(ctx: dict, effects: list) -> dict:
    """Undo the Effects in reverse order: shortcut, plugin, marketplace, then the permission entries, each only
    while it still holds what was written. The kit, the device folder and the person's letters stay."""
    return cs.remove_effects(ctx, effects)
