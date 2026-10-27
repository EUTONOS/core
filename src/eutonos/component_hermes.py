"""The Hermes component: recorded plugin-folder and exact config before-image.

The CLI enables the plugin and injection. Undo restores config.yaml byte-for-byte only
while it matches the recorded post-command digest; later user edits are preserved.
An in-flight CLI mutation with no completed readback remains incomplete for inspection.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import shutil

from . import component_support as cs

ID = "hermes"
PLUGIN = "crosstalk"
HELD_MAC = "Hermes paths on macOS follow in IPC8"


def hermes_home(ctx: dict) -> Path | None:
    override = cs.seam(ctx, "hermes_home")
    if override:
        return Path(override)
    if cs.os_name(ctx) == "windows":
        return cs.user_home(ctx) / "AppData" / "Local" / "hermes"
    return None


def cli_path(ctx: dict) -> Path | None:
    home = hermes_home(ctx)
    return home / "bin" / "hermes.cmd" if home else None


def found(ctx: dict) -> bool:
    cli = cli_path(ctx)
    return bool(cli and cli.is_file())


def status(ctx: dict) -> dict:
    if found(ctx):
        return {"state": "installed", "reason": ""}
    return {"state": "skipped", "reason": "Hermes was not found" if hermes_home(ctx) else HELD_MAC}


def _folder(ctx: dict) -> Path:
    return hermes_home(ctx) / "plugins" / PLUGIN


def _tree(folder: Path):
    from . import kits
    files = kits.tree_files(folder) if folder.is_dir() else {}
    files.pop("crosstalk.json", None)  # written per machine with a timestamp; not part of the plugin's identity
    return files


def _full_tree(folder: Path):
    from . import kits
    return cs.sha256(cs.canonical(kits.tree_files(folder))) if folder.is_dir() else None


def _source_tree(ctx: dict):
    install = cs.kit_module(ctx, "crosstalk_p0_install")
    with cs.home_env(ctx):
        return _tree(install.hermes_plugin_source())


def _run_install(ctx: dict) -> None:
    install = cs.kit_module(ctx, "crosstalk_p0_install")
    if "backup_config" not in inspect.signature(install.hermes_install).parameters:
        raise OSError("the older Hermes kit lacks reversible config support; update before applying this component")
    python = ""  # a materialized kit points Hermes at the stable H/bin launcher (IPC5 F6)
    try:
        with cs.home_env(ctx):
            report = install.hermes_install(hermes_home(ctx), python, cs.seam(ctx, "hermes_runner"), backup_config=False)
    except Exception as exc:  # the kit's InstallError carries a reason code
        raise OSError(f"hermes install refused: {getattr(exc, 'reason', type(exc).__name__)}") from exc
    if not report.get("ok"):
        raise OSError("hermes install failed: " + str(report.get("reason")))


class FolderStep(cs.StateStep):
    """The plugin folder, identified by its file tree (without ``crosstalk.json``)."""

    force_apply = True  # identical plugin bytes do not prove enable/injection config is correct

    def may_replace(self, ctx, current) -> bool:
        earlier = next((item for item in cs.recorded(ctx, ID) if item.get("effect_id") == self.effect_id), None)
        if earlier:
            if cs.sha256(current) != earlier.get("after_sha256"):
                return False
            undo = earlier.get("undo") or {}
            if "folder_after_full" in undo and _full_tree(_folder(ctx)) != undo["folder_after_full"]:
                return False
            if "config_after" in undo and cs.sha256(cs.permissions._read(hermes_home(ctx) / "config.yaml")) != undo["config_after"]:
                return False
        install = cs.kit_module(ctx, "crosstalk_p0_install")
        with cs.home_env(ctx):
            return install.hermes_plugin_identity(_folder(ctx))["state"] in ("absent", "empty", "ours")

    def undo(self, ctx, before) -> dict:
        earlier = next((item for item in cs.recorded(ctx, ID) if item.get("effect_id") == self.effect_id), None)
        if earlier and "config_before" in (earlier.get("undo") or {}):
            return dict(earlier["undo"], config_pending=True)
        config = hermes_home(ctx) / "config.yaml"
        raw = cs.permissions._read(config)
        saved = cs.ctx_home(ctx) / "install" / "backups" / cs.gen_id(ctx) / (self.effect_id + ".config")
        return dict(super().undo(ctx, before), config=str(config), config_before=cs.sha256(raw),
                    config_after=cs.sha256(raw), config_backup=str(saved), config_pending=True, folder_before=cs.sha256(before), folder_before_full=_full_tree(_folder(ctx)))

    def write_effect(self, ctx, data, effect) -> None:
        try:
            self.write(ctx, data)
        finally:
            # A caught error can safely record what the CLI actually changed. A hard
            # process kill leaves config_pending true and is never guessed away.
            effect["undo"]["config_after"] = cs.sha256(cs.permissions._read(Path(effect["undo"]["config"])))
            effect["undo"]["folder_after_full"] = _full_tree(_folder(ctx))
            effect["undo"]["config_pending"] = False
            if ctx.get("effect_sink"):
                ctx["effect_sink"](effect)

    def backup(self, ctx, before):
        """The replaced EUTONOS folder, copied whole (the kit's swap deletes it); an update keeps the first backup."""
        earlier = next((item for item in cs.recorded(ctx, ID) if item.get("effect_id") == self.effect_id), None)
        if earlier is not None and earlier.get("undo"):
            return earlier.get("backup")
        undo = self.undo(ctx, before)
        raw = cs.permissions._read(Path(undo["config"]))
        if raw is not None:
            Path(undo["config_backup"]).parent.mkdir(parents=True, exist_ok=True)
            cs.permissions._write_atomic(Path(undo["config_backup"]), raw)
        folder = Path(str(self.target))
        if before is None or not folder.is_dir():
            return None
        saved = cs.ctx_home(ctx) / "install" / "backups" / cs.gen_id(ctx) / (self.effect_id + ".folder")
        if saved.exists():
            shutil.rmtree(saved)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, saved)
        return str(saved)


def _steps(ctx: dict) -> list:
    folder = _folder(ctx)

    def folder_state(context):
        files = _tree(folder)
        return {"tree": files} if files else None

    plugin = FolderStep(ID, "plugin-folder", "file-create", str(folder), "folder",
                        lambda context: {"tree": _source_tree(context)}, folder_state,
                        lambda context, _value: _run_install(context), "hermes-remove", {"folder": str(folder)})
    return [plugin]


@cs.undo_handler("hermes-remove")
def _undo_hermes(ctx: dict, effect: dict) -> str | None:
    install = cs.kit_module(ctx, "crosstalk_p0_install")
    folder = Path(effect["undo"]["folder"])
    with cs.home_env(ctx):
        identity = install.hermes_plugin_identity(folder)
    undo = effect["undo"]
    # An in-flight command can change both the folder and config. Without the
    # post-command readback, those changes must not be called settled user edits.
    if undo.get("config_pending") and (_full_tree(folder) != undo.get("folder_before_full")
            or cs.sha256(cs.permissions._read(Path(undo["config"]))) != undo["config_before"]):
        return "Hermes command was interrupted before byte readback; inspect before retrying"
    if identity["state"] == "foreign":
        return "the plugin folder is not the EUTONOS plugin; kept"
    folder_digest = None if identity["state"] == "absent" else cs.sha256(cs.canonical({"tree": _tree(folder)}))
    full_now = _full_tree(folder)
    restored_folder = "folder_before_full" in undo and full_now == undo["folder_before_full"]
    if not restored_folder and "folder_after_full" in undo and full_now != undo["folder_after_full"]:
        return "changed by you, kept"
    if undo.get("config_pending") and not restored_folder and full_now != undo.get("folder_after_full"):
        return "Hermes folder write was interrupted before byte readback; inspect before retrying"
    if not restored_folder and identity["state"] != "absent" and folder_digest != effect["after_sha256"]:
        return "changed by you, kept"
    if "config_before" not in undo:
        return "the earlier Hermes effect has no reversible config snapshot; inspect before removing"
    config = Path(undo["config"])
    current = cs.permissions._read(config)
    digest = cs.sha256(current)
    if undo.get("config_pending") and digest != undo["config_before"]:
        return "Hermes config command was interrupted before readback; inspect before retrying"
    if digest not in (undo["config_after"], undo["config_before"]):
        return "Hermes config changed by you, kept"
    raw = None
    if undo["config_before"] is not None:
        raw = cs.permissions._read(Path(undo["config_backup"]))
        if raw is None or cs.sha256(raw) != undo["config_before"]:
            return "the earlier Hermes config backup is missing or changed; kept"
    if restored_folder:
        pass
    elif effect.get("backup") and identity["state"] != "absent":
        reason = _restore_folder(folder, Path(effect["backup"]))
        if reason:
            return reason
    elif not effect.get("backup") and identity["state"] != "absent":
        shutil.rmtree(folder)
    elif effect.get("backup"):
        if not Path(effect["backup"]).is_dir():
            return "the earlier plugin folder's backup is missing, kept"
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(effect["backup"], folder)
    if raw is None:
        config.unlink(missing_ok=True)
    elif current != raw:
        cs.permissions._write_atomic(config, raw)
    return None


def _restore_folder(folder: Path, saved: Path) -> str | None:
    """Swap the copied-aside earlier folder back in for the one EUTONOS installed (built beside it, then renamed)."""
    if not saved.is_dir():
        return "the earlier plugin folder's backup is missing, kept"
    fresh, old = (folder.parent / f".{folder.name}.{word}-{os.getpid()}" for word in ("restore", "replaced"))
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.copytree(saved, fresh)
    os.replace(folder, old)
    try:
        os.replace(fresh, folder)
    except OSError:
        os.replace(old, folder)
        shutil.rmtree(fresh, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return None


def plan(ctx: dict) -> list[dict]:
    return cs.plan_steps(ctx, _steps(ctx)) if found(ctx) else []


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    """Copy and enable the plugin through Hermes' CLI. Idempotent; nothing is written when Hermes is not found."""
    return cs.apply_steps(ctx, _steps(ctx), effects) if found(ctx) else []


def verify(ctx: dict) -> dict:
    if not found(ctx):
        state = status(ctx)
        return cs.verify_result(ID, [{"name": "hermes", "ok": False, "detail": state["reason"]}], state="held")
    install = cs.kit_module(ctx, "crosstalk_p0_install")
    with cs.home_env(ctx):
        identity = install.hermes_plugin_identity(_folder(ctx))
    if identity["state"] == "foreign":  # somebody else's folder of that name: ours is not installed, theirs is kept
        return cs.verify_result(ID, [{"name": "plugin folder", "ok": False, "missing": True,
                                      "detail": "another plugin named crosstalk is there; it is kept as it is"}])
    checks = [{"name": "plugin folder", "ok": identity["state"] == "ours",
               "detail": "the plugin folder is the EUTONOS plugin" if identity["state"] == "ours"
               else f"plugin folder is {identity['state']}"}]
    checks += cs.step_checks(ctx, ID, _steps(ctx))
    return cs.verify_result(ID, checks)


def remove(ctx: dict, effects: list) -> dict:
    return cs.remove_effects(ctx, effects)


def describe(ctx: dict) -> str:
    return json.dumps(status(ctx))
