"""Crosstalk P0 install helper (stdlib only). Called by install.ps1 / uninstall.ps1; testable offline.

It never reads a credential file and never prints file contents: reports carry paths, SHA-256
hashes, byte counts and TOML table names only.

Commands (each prints one line of JSON):
  render-plugin                      write plugin/hooks/register.mjs from plugin-src/register.mjs.in
  backup                             copy the two config files aside and record their SHA-256
  toml-add --mode M [...]            append the [mcp_servers.crosstalk_p0] block (marked, surgical)
  toml-remove                        remove only that block
  upgrade-snapshot                   snapshot current config/registries and controls hash; retain original backups
  toml-upgrade [--mode M]             relocate executable bindings; refuse permission changes
  rebind-marketplace                 relocate only the known local marketplace, preserving plugin enable state
  verify-upgrade --snapshot PATH      verify settings, controls and refreshed cached hook
  record-installed                   record post-install hashes (to detect later divergence)
  compare                            what changed in the two files since backup (names/counts only)
  restore [--keep-other-changes]     restore the backups and verify hashes (or surgical-only)
  verify                             is the block / plugin entry present (read-only)
  state                              none | backed-up | installed | restored
  permission-rules                   every permission entry (app, rule, feature, why, required, default) and whether its
                                     rule is in settings.json (read-only)
  permissions [--apply-permissions]  what install would add; with the flag, back settings.json up and add only the
                                     missing "allow" entries, recording them (without the flag nothing is written)
  permissions-remove                 uninstall: remove only the rules the installer recorded adding
  relay-folder-remove                delete the retired relay folder (uninstall)
  helper-startup-plan [--helper-script P]     what the "Crosstalk helper" Startup shortcut would be (read-only)
  helper-startup-install [--helper-script P]  create or refresh that one shortcut, pointing at P (does not start the
                                              helper; default P: ~/.eutonos/crosstalk-helper-src if present)
  helper-startup-remove                       delete that one shortcut
  hermes-plan | hermes-install | hermes-remove   the Hermes plugin (slice 3B.12); plan touches nothing

Test-only overrides: --codex-config, --claude-settings, --state-dir, and the CROSSTALK_P0_* env names in
crosstalk_p0_core.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # python -I drops the script directory from sys.path
    sys.path.insert(0, str(HERE))

import crosstalk_p0_core as core  # noqa: E402

TAG = "crosstalk.p0.install.v1"
BEGIN = "# >>> crosstalk-p0 throwaway probe (remove with uninstall.ps1) >>>"
END = "# <<< crosstalk-p0 <<<"
TABLE = "mcp_servers.crosstalk_p0"
APPROVAL_MODES = ("auto", "prompt", "writes", "approve")
PLUGIN_ID = "crosstalk-p0@crosstalk-p0-local"
MARKETPLACE = "crosstalk-p0-local"


class InstallError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# ----------------------------------------------------------------- locations
def codex_config_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    base = os.environ.get("CODEX_HOME")
    return (Path(base) if base else core.home() / ".codex") / "config.toml"


def claude_settings_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else core.home() / ".claude") / "settings.json"


def state_dir(override: str | None = None) -> Path:
    return Path(override) if override else core.device_dir() / "install"


def runtime_python() -> Path:
    # Console python: the Claude plugin and Hermes runs. Codex launches its MCP server without hiding the console,
    # so the Codex server uses runtime_pythonw() (stdio over pipes works there; no window opens).
    return core.eutonos_home() / "runtime" / "Scripts" / "python.exe"


def launcher_path() -> Path:
    """The stable Crosstalk launcher the installed carriers run: ``<EUTONOS home>/bin/eutonos-crosstalk`` (``.exe`` on
    Windows). It is already the Crosstalk CLI, so a carrier calls it with cli ``-`` (IPC5 F6)."""
    return core.eutonos_home() / "bin" / ("eutonos-crosstalk.exe" if os.name == "nt" else "eutonos-crosstalk")


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def manifest_path(directory: Path) -> Path:
    return directory / "manifest.json"


def load_manifest(directory: Path) -> dict | None:
    value = core._read_json(manifest_path(directory), None)
    return value if isinstance(value, dict) else None


def save_manifest(directory: Path, value: dict) -> None:
    core._write_json_atomic(manifest_path(directory), value)


def informational_hashes() -> dict:
    """Plugin registries that `claude plugin ...` rewrites; reported, never required to match."""
    base = core.claude_home() / "plugins"
    return {name: sha256_file(base / name) for name in ("installed_plugins.json", "known_marketplaces.json")}


# ------------------------------------------------------------------- render
def marketplace_dir() -> Path:
    """The stable Claude marketplace folder a materialized kit renders into: ``<EUTONOS home>/marketplace/crosstalk``."""
    return core.eutonos_home() / "marketplace" / "crosstalk"


def plugin_dir() -> Path:
    """The marketplace folder Claude registers: ``H/marketplace/crosstalk`` when this kit runs from ``H/kits``,
    else the kit's own ``claude-plugin`` folder (a checkout, unchanged)."""
    return marketplace_dir() if core.kit_mode() else HERE / "claude-plugin"


def _render_into_marketplace(text: str) -> Path:
    """Copy the kit's ``claude-plugin`` folder (without the template folder and any stale rendered hook) to a sibling,
    write the rendered hook there, then swap it into place. Returns the rendered hook path."""
    final = marketplace_dir()
    staging = final.with_name(final.name + f".new-{os.getpid()}")
    retired = final.with_name(final.name + f".old-{os.getpid()}")
    for leftover in (staging, retired):
        shutil.rmtree(leftover, ignore_errors=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(HERE / "claude-plugin", staging, ignore=shutil.ignore_patterns("plugin-src", "register.mjs.tmp-*"))
        hook = staging / "plugin" / "hooks" / "register.mjs"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(text, encoding="utf-8", newline="\n")
        if final.exists():
            os.replace(final, retired)
        os.replace(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)
    return final / "plugin" / "hooks" / "register.mjs"


def _render_defaults(runtime, cli) -> tuple:
    """The interpreter and CLI a hook runs when the caller names none. A materialized kit renders the stable H/bin
    launcher (cli "-"), so the hook survives updates (IPC5 F6); a checkout renders its own interpreter and CLI."""
    kit_run = core.kit_mode()
    launcher = kit_run and not runtime
    runtime = runtime or (str(launcher_path()) if kit_run else str(runtime_python()))
    cli = cli or ("-" if launcher else str(HERE / "crosstalk_p0_cli.py"))
    return runtime, cli


def plugin_text(runtime: str | None, cli: str | None) -> str:
    """The rendered ``register.mjs`` text for this runtime and CLI (pure: writes nothing). The installer component
    uses it to know the hook's hash before it renders."""
    template = HERE / "claude-plugin" / "plugin-src" / "register.mjs.in"
    runtime, cli = _render_defaults(runtime, cli)

    def js(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    text = template.read_text(encoding="utf-8")
    helper_dir = str(core.helper_dir())
    for token, value in (("__CROSSTALK_P0_RUNTIME__", runtime), ("__CROSSTALK_P0_CLI__", cli),
                         ("__CROSSTALK_P0_HELPER__", helper_dir)):
        if token not in text:
            raise InstallError("template-token-missing", token)
        text = text.replace(token, js(value))
    return text


def render_plugin(runtime: str | None = None, cli: str | None = None) -> dict:
    kit_run = core.kit_mode()
    target = plugin_dir() / "plugin" / "hooks" / "register.mjs"
    runtime, cli = _render_defaults(runtime, cli)
    text = plugin_text(runtime, cli)
    helper_dir = str(core.helper_dir())
    if kit_run:
        _render_into_marketplace(text)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            os.replace(temporary, target)  # a reader or a crash never sees a half-written hook
        finally:
            if temporary.exists():
                temporary.unlink()
    return {"ok": True, "rendered": str(target), "runtime": runtime, "cli": cli, "helper_dir": helper_dir,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


# ------------------------------------------------------------ permission entries
# The checked-in permission list (IPC item 13: one list, the installer owns the permissions). Each entry names the
# app, the exact rule, the feature that needs it, why, whether the feature breaks without it, and its default.
# `{kit}` / `{kit_posix}` in a rule are this kit's folder (the owner's script rules are per folder). Both Desktop tool
# rules are narrowed by the plugin's guard hook (set_remote_control: "self" only; send_message: only the fixed
# restart-relink text to a chat on the helper's list), because an MCP allow rule matches the tool name only.
# default "allow": `--apply-permissions` adds it when missing; "off": listed and reported, never added by the installer
# (the owner added it by hand; the IPC1 scan decides whether it ships).
# send-message is "off" here because this installer writes the USER settings file, while permissions.json (the one list)
# declares that rule project-local ("it can message any chat"); the EUTONOS installer writes it there. A test pins
# every id and default here to permissions.json (tests/test_permissions_list.py, KitDriftTests).
PERMISSION_ENTRIES = (
    {"id": "set-remote-control", "app": "claude", "rule": "mcp__ccd_session_mgmt__set_remote_control",
     "feature": "remote-control-self-relink", "required": True, "default": "allow",
     "why": "a chat woken by a letter comes back with Remote Control off; its plugin switches its own back on after "
            "its first turn"},
    {"id": "send-message", "app": "claude", "rule": "mcp__ccd_session_mgmt__send_message",
     "feature": "restart-relink", "required": False, "default": "off",
     "why": "after a Desktop restart one running chat's plugin brings stopped chats' Remote Control back; without it "
            "asleep chats stay off until a letter wakes them"},
    {"id": "install-script", "app": "claude", "rule": "PowerShell(& '{kit}\\install.ps1' *)",
     "feature": "owner-run-install", "required": False, "default": "off",
     "why": "lets a chat run the Crosstalk installer the owner asked for without a prompt per run"},
    {"id": "uninstall-script", "app": "claude", "rule": "PowerShell(& '{kit}\\uninstall.ps1' *)",
     "feature": "owner-run-uninstall", "required": False, "default": "off",
     "why": "lets a chat run the Crosstalk uninstaller the owner asked for without a prompt per run"},
    {"id": "kit-folder-edit", "app": "claude", "rule": "Edit(//{kit_posix}/**)",
     "feature": "kit-maintenance", "required": False, "default": "off",
     "why": "lets a chat edit the Crosstalk kit folder without a prompt per edit"},
)
PERMISSIONS_RECORD = "permissions_added.json"


def permission_entries(kit: Path | None = None) -> list[dict]:
    """PERMISSION_ENTRIES with the kit folder filled into the rules."""
    kit = Path(kit or HERE)
    posix = kit.as_posix()
    if len(posix) > 1 and posix[1] == ":":
        posix = posix[0].lower() + posix[2:]  # C:/x -> c/x, as Claude writes path rules
    return [dict(item, rule=item["rule"].format(kit=str(kit), kit_posix=posix)) for item in PERMISSION_ENTRIES]


def _allow_rules(path: Path):
    """permissions.allow of a settings file as a set, or None when the file cannot be read as settings."""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        rules = ((settings.get("permissions") or {}).get("allow")) if isinstance(settings, dict) else None
        return {rule for rule in rules if isinstance(rule, str)} if isinstance(rules, list) else set()
    except (OSError, ValueError, AttributeError):
        return None


def permission_rules_status(claude_settings: Path | None = None, kit: Path | None = None) -> dict:
    """Every permission entry and whether its rule is in the user's permissions.allow. Read-only; an unreadable file
    reports every entry as unknown (None), never as present. `missing` lists only required rules (what a feature
    cannot work without); `missing_optional` the rest of the missing ones; `would_add` what --apply-permissions adds."""
    path = claude_settings or claude_settings_path()
    allowed = _allow_rules(path)
    entries = [dict(item, present=None if allowed is None else item["rule"] in allowed)
               for item in permission_entries(kit)]
    absent = [item for item in entries if item["present"] is False]
    return {"settings": str(path), "readable": allowed is not None, "entries": entries, "rules": entries,
            "missing": [item["rule"] for item in absent if item["required"]],
            "missing_optional": [item["rule"] for item in absent if not item["required"]],
            "would_add": [item["rule"] for item in absent if item["default"] == "allow"]}


def _settings_text(path: Path) -> tuple[str, dict]:
    try:
        text = path.read_text(encoding="utf-8")
        settings = json.loads(text)
    except (OSError, ValueError):
        raise InstallError("settings-unreadable", str(path))
    if not isinstance(settings, dict):
        raise InstallError("settings-not-an-object", str(path))
    return text, settings


def _write_settings(path: Path, text: str, settings: dict) -> None:
    """Rewrite a settings file keeping its key order, indent and final newline; atomic (temp file, then replace)."""
    indent = 2
    match = re.search(r"\n([ \t]+)\S", text)
    if match:
        indent = "\t" if match.group(1).startswith("\t") else len(match.group(1))
    out = json.dumps(settings, indent=indent, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(out, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _backup_settings(path: Path, directory: Path, label: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"settings.{label}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.json"
    shutil.copy2(path, target)
    return str(target)


def permissions_apply(claude_settings: Path, directory: Path, kit: Path | None = None) -> dict:
    """Install, with --apply-permissions only: add the entries whose default is "allow" and that are missing to
    permissions.allow, after backing the file up, and record exactly what was added so uninstall removes only that.
    Rules already there (the owner's own) are never touched, recorded or removed. Nothing to add writes nothing."""
    text, settings = _settings_text(claude_settings)
    permissions = settings.get("permissions")
    if permissions is not None and not isinstance(permissions, dict):
        raise InstallError("settings-permissions-not-an-object", str(claude_settings))
    allow = (permissions or {}).get("allow")
    if allow is not None and not isinstance(allow, list):
        raise InstallError("settings-allow-not-a-list", str(claude_settings))
    present = {rule for rule in (allow or []) if isinstance(rule, str)}
    add = [item for item in permission_entries(kit) if item["default"] == "allow" and item["rule"] not in present]
    result = {"ok": True, "settings": str(claude_settings), "added": [item["rule"] for item in add], "backup": None}
    if not add:
        return result
    result["backup"] = _backup_settings(claude_settings, directory, "before-permissions-add")
    permissions = settings.setdefault("permissions", {})
    permissions.setdefault("allow", []).extend(item["rule"] for item in add)
    _write_settings(claude_settings, text, settings)
    record = core._read_json(directory / PERMISSIONS_RECORD, {})
    kept = record.get("added") if isinstance(record, dict) and isinstance(record.get("added"), list) else []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    core._write_json_atomic(directory / PERMISSIONS_RECORD, {
        "tag": TAG, "settings": str(claude_settings),
        "added": kept + [{"rule": item["rule"], "id": item["id"], "added_at": now, "backup": result["backup"]}
                         for item in add]})
    return result


def permissions_remove(claude_settings: Path, directory: Path) -> dict:
    """Uninstall: remove only the rules the installer recorded adding (and only while still present), after a backup.
    Nothing recorded means nothing is touched, not even a backup is made."""
    record = core._read_json(directory / PERMISSIONS_RECORD, {})
    recorded = [row["rule"] for row in (record.get("added") if isinstance(record, dict) else None) or []
                if isinstance(row, dict) and isinstance(row.get("rule"), str)]
    result = {"ok": True, "settings": str(claude_settings), "recorded": recorded, "removed": [], "backup": None}
    if not recorded:
        return result
    try:
        text, settings = _settings_text(claude_settings)
    except InstallError:
        result["note"] = "settings file unreadable; the record is kept"
        return result
    allow = (settings.get("permissions") or {}).get("allow") if isinstance(settings.get("permissions"), dict) else None
    if isinstance(allow, list):
        gone = [rule for rule in recorded if rule in allow]
        if gone:
            result["backup"] = _backup_settings(claude_settings, directory, "before-permissions-remove")
            settings["permissions"]["allow"] = [rule for rule in allow if rule not in gone]
            _write_settings(claude_settings, text, settings)
            result["removed"] = gone
    (directory / PERMISSIONS_RECORD).unlink(missing_ok=True)
    return result


def relay_folder_remove() -> dict:
    """Uninstall: delete the retired relay folder (jobs, heartbeats, ticket flag) under the device folder. Only that
    folder; the journal, directory and helper state stay."""
    target = core.device_dir() / "relay"
    existed = target.exists()
    if existed:
        shutil.rmtree(target, ignore_errors=True)
    return {"ok": True, "relay_folder": str(target), "existed": existed, "removed": not target.exists()}


# ------------------------------------------------------- helper Startup shortcut
# The Crosstalk helper (crosstalk_helper.py) starts at Windows login the way the owner's "Claude chat sync" watcher
# does: a Startup-folder shortcut to pythonw.exe (no console window) with the script and --watch. Here it is the
# installed EUTONOS runtime's pythonw, isolated (-I -B), never system Python. The script is the path the installer
# is given (the owner's clean worktree %USERPROFILE%\.eutonos\crosstalk-helper-src, a detached origin/main, because the
# shared checkout carries other chats' edits); without one it is the helper beside this installer. Installing is
# idempotent (the shortcut is created or refreshed in place) and never starts or restarts the helper: the shortcut
# takes effect at the next login, or when the owner starts it.
HELPER_SHORTCUT_NAME = "Crosstalk helper.lnk"
HELPER_SHORTCUT_DESCRIPTION = "Watches Claude Desktop restarts for Crosstalk: restart relink, letter wakes, held letters"
HELPER_SCRIPT_NAME = "crosstalk_helper.py"
_SHORTCUT_SCRIPT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CT_LNK);"
    "$s.TargetPath=$env:CT_TARGET;$s.Arguments=$env:CT_ARGS;$s.WorkingDirectory=$env:CT_CWD;"
    "$s.WindowStyle=1;$s.Description=$env:CT_DESC;$s.Save()")


def runtime_pythonw() -> Path:
    return core.eutonos_home() / "runtime" / "Scripts" / "pythonw.exe"


def default_helper_script() -> Path:
    """The owner's clean helper worktree when it exists, else the helper beside this installer."""
    clean = core.eutonos_home() / "crosstalk-helper-src" / "tools" / "crosstalk" / HELPER_SCRIPT_NAME
    return clean if clean.is_file() else HERE / HELPER_SCRIPT_NAME


def startup_folder(override: str | None = None) -> Path:
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA") or str(core.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def helper_launcher() -> Path | None:
    """The stable ``<EUTONOS home>/bin/eutonos-crosstalk-helper`` launcher (a copy of the active runtime's gui entry
    point), or ``None`` while the installed runtime has not produced one."""
    launcher = core.eutonos_home() / "bin" / ("eutonos-crosstalk-helper" + (".exe" if os.name == "nt" else ""))
    return launcher if launcher.is_file() else None


def helper_startup_plan(startup: str | None = None, pythonw: str | None = None, script: str | None = None) -> dict:
    """What the Startup shortcut would be. Read-only. With no explicit script or interpreter, the stable
    ``H/bin/eutonos-crosstalk-helper`` launcher is preferred when it exists."""
    launcher = helper_launcher() if not script and not pythonw else None
    if launcher is not None:
        folder = startup_folder(startup)
        return {"ok": True, "shortcut": str(folder / HELPER_SHORTCUT_NAME), "target": str(launcher),
                "arguments": "--watch", "working_directory": str(launcher.parent), "window_style": 1,
                "description": HELPER_SHORTCUT_DESCRIPTION, "script": str(launcher), "launcher": True,
                "target_exists": True, "script_exists": True,
                "shortcut_exists": (folder / HELPER_SHORTCUT_NAME).exists()}
    script_path = Path(script) if script else default_helper_script()
    target = pythonw or str(runtime_pythonw())
    folder = startup_folder(startup)
    return {"ok": True, "shortcut": str(folder / HELPER_SHORTCUT_NAME), "target": target,
            "arguments": f'-I -B "{script_path}" --watch', "working_directory": str(script_path.parent),
            "window_style": 1, "description": HELPER_SHORTCUT_DESCRIPTION, "script": str(script_path),
            "target_exists": Path(target).is_file(), "script_exists": script_path.is_file(),
            "shortcut_exists": (folder / HELPER_SHORTCUT_NAME).exists()}


def _create_shortcut_with_powershell(plan: dict) -> None:
    env = dict(os.environ, CT_LNK=plan["shortcut"], CT_TARGET=plan["target"], CT_ARGS=plan["arguments"],
               CT_CWD=plan["working_directory"], CT_DESC=plan["description"])
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_SCRIPT],
                          env=env, capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise InstallError("helper-shortcut-create-failed", (done.stderr or "")[-200:])


def helper_startup_install(startup: str | None = None, pythonw: str | None = None, create=None,
                           script: str | None = None) -> dict:
    """Create or refresh the "Crosstalk helper" Startup shortcut (only that one file; never touches another shortcut)
    so that it points at `script`. `create(plan)` replaces the PowerShell step in tests. Does not start the helper."""
    plan = helper_startup_plan(startup, pythonw, script)
    if not plan["target_exists"] and create is None:
        raise InstallError("helper-runtime-pythonw-missing", plan["target"])
    if not plan["script_exists"]:
        raise InstallError("helper-script-missing", plan["script"])
    Path(plan["shortcut"]).parent.mkdir(parents=True, exist_ok=True)
    (create or _create_shortcut_with_powershell)(plan)
    plan["action"] = "refreshed" if plan["shortcut_exists"] else "created"
    plan["created"] = Path(plan["shortcut"]).exists() if create is None else True
    return plan


def helper_startup_remove(startup: str | None = None) -> dict:
    """Delete the one "Crosstalk helper" shortcut. Does not stop a running helper."""
    path = startup_folder(startup) / HELPER_SHORTCUT_NAME
    existed = path.exists()
    if existed:
        path.unlink()
    return {"ok": True, "removed": existed, "shortcut": str(path)}


# --------------------------------------------------------------------- backup
def backup(codex_config: Path, claude_settings: Path, directory: Path, force: bool = False) -> dict:
    existing = load_manifest(directory)
    if existing and existing.get("state") in ("backed-up", "installed") and not force:
        raise InstallError("backup-already-exists",
                           "an earlier install left its backups; run uninstall.ps1 first (originals are never overwritten)")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = directory / f"backup-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for file_id, path in (("codex-config", codex_config), ("claude-settings", claude_settings)):
        item = {"id": file_id, "path": str(path), "existed": path.is_file(), "sha256": sha256_file(path)}
        if item["existed"]:
            copy = folder / path.name
            shutil.copy2(path, copy)
            item["backup"] = str(copy)
            item["bytes"] = path.stat().st_size
            if sha256_file(copy) != item["sha256"]:
                raise InstallError("backup-hash-mismatch", file_id)
        files.append(item)
    manifest = {"tag": TAG, "state": "backed-up", "stamp": stamp, "files": files,
                "informational_before": informational_hashes()}
    save_manifest(directory, manifest)
    return {"ok": True, "state": "backed-up", "files": [{k: f.get(k) for k in ("id", "path", "existed", "sha256", "backup")}
                                                       for f in files]}


# ----------------------------------------------------------------------- toml
def _parse_toml(text: str):
    try:
        import tomllib
    except ImportError:
        return None
    return tomllib.loads(text)


def build_block(mode: str, runtime: str, server_script: str, startup: int, tool_timeout: int, newline: str) -> str:
    for value in (runtime, server_script):
        if "'" in value or "\n" in value:
            raise InstallError("path-not-toml-literal-safe", "a path contains an apostrophe or newline")
    lines = [BEGIN, f"[{TABLE}]", f"command = '{runtime}'", f"args = ['-I', '-B', '{server_script}']",
             f"startup_timeout_sec = {startup}", f"tool_timeout_sec = {tool_timeout}",
             f'default_tools_approval_mode = "{mode}"',
             "env_vars = ['USERPROFILE', 'APPDATA', 'LOCALAPPDATA']", END]
    return newline.join(lines) + newline


def _retired_in_kit_mode() -> None:
    """A materialized kit never writes the Codex block itself: the EUTONOS installer owns it (permission entry 10)."""
    if core.kit_mode():
        raise InstallError("codex-block-is-permission-entry-10",
                           "the EUTONOS installer writes and removes the Codex block; this kit's TOML writer is retired")


def toml_add(config: Path, mode: str, runtime: str, server_script: str, startup: int = 20,
             tool_timeout: int = 120) -> dict:
    _retired_in_kit_mode()
    if mode not in APPROVAL_MODES:
        raise InstallError("bad-approval-mode", mode)
    raw = config.read_bytes() if config.is_file() else b""
    text = raw.decode("utf-8")
    if BEGIN in text or f"[{TABLE}]" in text:
        raise InstallError("block-already-present")
    parsed = _parse_toml(text) if text.strip() else {}
    if parsed is not None and TABLE.split(".")[1] in (parsed.get("mcp_servers") or {}):
        raise InstallError("server-id-already-in-config")
    newline = "\r\n" if "\r\n" in text else "\n"
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += newline
    if prefix.strip():
        prefix += newline  # one blank line before the marked block
    block = build_block(mode, runtime, server_script, startup, tool_timeout, newline)
    updated = prefix + block
    check = _parse_toml(updated)
    if check is not None:
        server = (check.get("mcp_servers") or {}).get("crosstalk_p0")
        if not isinstance(server, dict) or server.get("default_tools_approval_mode") != mode or \
                server.get("command") != runtime or server.get("args") != ["-I", "-B", server_script]:
            raise InstallError("block-did-not-parse-back")
    config.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.with_name(config.name + ".crosstalk-p0.tmp")
    tmp.write_bytes(updated.encode("utf-8"))
    os.replace(tmp, config)
    return {"ok": True, "config": str(config), "mode": mode, "parsed_back": check is not None,
            "sha256": sha256_file(config)}


def toml_remove(config: Path) -> dict:
    if not config.is_file():
        return {"ok": True, "removed": False, "reason": "config-absent"}
    text = config.read_bytes().decode("utf-8")
    start = text.find(BEGIN)
    if start < 0:
        return {"ok": True, "removed": False, "reason": "block-absent", "sha256": sha256_file(config)}
    end = text.find(END, start)
    if end < 0:
        raise InstallError("block-end-marker-missing")
    end += len(END)
    if text[end:end + 2] == "\r\n":
        end += 2
    elif text[end:end + 1] == "\n":
        end += 1
    head = text[:start]
    # drop the single blank line install put before the block
    for nl in ("\r\n\r\n", "\n\n"):
        if head.endswith(nl):
            head = head[:-len(nl)] + nl[:len(nl) // 2]
            break
    updated = head + text[end:]
    tmp = config.with_name(config.name + ".crosstalk-p0.tmp")
    tmp.write_bytes(updated.encode("utf-8"))
    os.replace(tmp, config)
    return {"ok": True, "removed": True, "sha256": sha256_file(config)}


def upgrade_snapshot(config: Path, settings: Path, directory: Path) -> dict:
    """Keep current configuration separately; never replace original rollback backups."""
    manifest = load_manifest(directory)
    if not manifest or manifest.get("state") != "installed":
        raise InstallError("upgrade-requires-installed-manifest")
    folder = directory / f"upgrade-{time.time_ns()}"
    folder.mkdir(parents=True)
    for name, path in (("config.toml", config), ("settings.json", settings),
                       ("known_marketplaces.json", core.claude_home() / "plugins" / "known_marketplaces.json"),
                       ("installed_plugins.json", core.claude_home() / "plugins" / "installed_plugins.json")):
        if path.is_file():
            shutil.copy2(path, folder / name)
    control = core.device_dir() / "controls.json"
    core._write_json_atomic(folder / "preserved-controls.json", {"sha256": sha256_file(control)})
    return {"ok": True, "snapshot": str(folder)}


def toml_upgrade(config: Path, runtime: str, server: str, mode: str | None = None) -> dict:
    _retired_in_kit_mode()
    text = config.read_bytes().decode("utf-8")
    parsed = _parse_toml(text)
    if parsed is None:
        raise InstallError("upgrade-requires-tomllib")
    current = (parsed.get("mcp_servers") or {}).get("crosstalk_p0")
    if not isinstance(current, dict):
        raise InstallError("upgrade-server-missing")
    if mode is not None and mode != current.get("default_tools_approval_mode"):
        raise InstallError("upgrade-permission-change-refused", "omit -ApprovalMode to preserve the installed mode")
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end <= start:
        raise InstallError("upgrade-marked-block-missing")
    # Change only the two executable bindings; all approvals, env and limits survive.
    replacement = build_block(current.get("default_tools_approval_mode", "prompt"), runtime, server, 20, 120, "\n")
    new_lines = {key: next(line for line in replacement.splitlines() if line.startswith(key + " ="))
                 for key in ("command", "args")}
    block = text[start:end]
    for key, line in new_lines.items():
        block, count = re.subn(r"(?m)^" + key + r"\s*=[^\r\n]*", lambda _: line, block)
        if count != 1:
            raise InstallError("upgrade-binding-not-single-line", key)
    updated = text[:start] + block + text[end:]
    check = _parse_toml(updated)
    expected = {**current, "command": runtime, "args": ["-I", "-B", server]}
    if check["mcp_servers"]["crosstalk_p0"] != expected:
        raise InstallError("upgrade-config-changed-unexpectedly")
    tmp = config.with_name(config.name + ".crosstalk-p0.tmp")
    tmp.write_bytes(updated.encode("utf-8"))
    os.replace(tmp, config)
    return {"ok": True, "mode": current.get("default_tools_approval_mode"), "parsed_back": True}


def verify_upgrade(settings: Path, snapshot: Path) -> dict:
    before = json.loads((snapshot / "settings.json").read_text(encoding="utf-8")) if (snapshot / "settings.json").exists() else {}
    after = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    # CLI marketplace operations may change these two registry declarations only.
    for key in set(before) | set(after):
        if key not in ("enabledPlugins", "extraKnownMarketplaces") and before.get(key) != after.get(key):
            raise InstallError("upgrade-settings-changed", key)
    for key, own in (("enabledPlugins", PLUGIN_ID), ("extraKnownMarketplaces", MARKETPLACE)):
        left, right = dict(before.get(key) or {}), dict(after.get(key) or {})
        left.pop(own, None)
        right.pop(own, None)
        if left != right:
            raise InstallError("upgrade-unrelated-registry-changed", key)
    if before.get("enabledPlugins", {}).get(PLUGIN_ID) != after.get("enabledPlugins", {}).get(PLUGIN_ID):
        raise InstallError("upgrade-plugin-enable-changed")
    controls = core._read_json(snapshot / "preserved-controls.json", None)
    if not isinstance(controls, dict) or controls.get("sha256") != sha256_file(core.device_dir() / "controls.json"):
        raise InstallError("upgrade-controls-changed")
    markets = core._read_json(core.claude_home() / "plugins" / "known_marketplaces.json", {})
    market = markets.get(MARKETPLACE, {})
    if Path(market.get("source", {}).get("path", "")).resolve() != plugin_dir().resolve():
        raise InstallError("upgrade-marketplace-not-relocated")
    installed = core._read_json(core.claude_home() / "plugins" / "installed_plugins.json", {})
    entries = installed.get("plugins", {}).get(PLUGIN_ID, [])
    entries = [entry for entry in entries if entry.get("scope") == "user"]
    if len(entries) != 1:
        raise InstallError("upgrade-user-plugin-not-unique")
    hook = Path(entries[0]["installPath"]) / "hooks" / "register.mjs"
    expected = plugin_dir() / "plugin" / "hooks" / "register.mjs"
    if sha256_file(hook) is None or sha256_file(hook) != sha256_file(expected):
        raise InstallError("upgrade-plugin-cache-stale", "restart is insufficient until the cached hook matches the tracked build")
    return {"ok": True, "permissions_preserved": True, "plugin_cache_verified": True}


def rebind_marketplace(settings: Path) -> dict:
    """Relocate one known directory source without unregistering its plugin."""
    registry_path = core.claude_home() / "plugins" / "known_marketplaces.json"
    registry = core._read_json(registry_path, None)
    installed = core._read_json(core.claude_home() / "plugins" / "installed_plugins.json", None)
    value = core._read_json(settings, None)
    if not all(isinstance(item, dict) for item in (registry, installed, value)):
        raise InstallError("upgrade-registry-unreadable")
    entries = (installed.get("plugins") or {}).get(PLUGIN_ID, [])
    if not isinstance(entries, list) or len([entry for entry in entries if isinstance(entry, dict) and entry.get("scope") == "user"]) != 1:
        raise InstallError("upgrade-user-plugin-not-unique")
    market = registry.get(MARKETPLACE)
    if not isinstance(market, dict) or not isinstance(market.get("source"), dict) or market["source"].get("source") != "directory":
        raise InstallError("upgrade-marketplace-schema-unknown")
    old_path = market["source"].get("path")
    if not isinstance(old_path, str) or not old_path or Path(market.get("installLocation", "")).resolve() != Path(old_path).resolve():
        raise InstallError("upgrade-marketplace-path-conflict")
    declarations = value.get("extraKnownMarketplaces", {})
    if not isinstance(declarations, dict):
        raise InstallError("upgrade-settings-marketplace-schema-unknown")
    declaration = declarations.get(MARKETPLACE)
    if declaration is not None:
        source = declaration.get("source") if isinstance(declaration, dict) else None
        if not isinstance(source, dict) or source.get("source") != "directory" or Path(source.get("path", "")).resolve() != Path(old_path).resolve():
            raise InstallError("upgrade-settings-marketplace-path-conflict")
    # Refuse a different catalog/plugin before changing any existing binding.
    target = plugin_dir()
    catalog = core._read_json(target / ".claude-plugin" / "marketplace.json", {})
    plugin = core._read_json(target / "plugin" / ".claude-plugin" / "plugin.json", {})
    if catalog.get("name") != MARKETPLACE or plugin.get("name") != "crosstalk-p0":
        raise InstallError("upgrade-target-plugin-identity-mismatch")
    market["source"]["path"] = str(target)
    market["installLocation"] = str(target)
    core._write_json_atomic(registry_path, registry)
    if declaration is not None:
        declaration["source"]["path"] = str(target)
        core._write_json_atomic(settings, value)
    return {"ok": True, "marketplace": MARKETPLACE, "relocated": str(target)}


# ---------------------------------------------------------------- state steps
def record_installed(directory: Path, mode: str | None = None) -> dict:
    manifest = load_manifest(directory)
    if not manifest:
        raise InstallError("no-manifest")
    for item in manifest["files"]:
        item["sha256_after_install"] = sha256_file(Path(item["path"]))
    manifest["state"] = "installed"
    if mode:
        manifest["approval_mode"] = mode
    manifest["informational_after"] = informational_hashes()
    save_manifest(directory, manifest)
    return {"ok": True, "state": "installed", "approval_mode": manifest.get("approval_mode"),
            "files": [{k: f.get(k) for k in ("id", "sha256", "sha256_after_install")} for f in manifest["files"]]}


def _toml_table_names(text: str) -> list[str]:
    return re.findall(r"^\s*\[\[?([^\]\r\n]+)\]\]?\s*$", text, flags=re.M)


def compare(directory: Path) -> dict:
    """Names and counts only: how the two files differ from the backup now."""
    manifest = load_manifest(directory)
    if not manifest:
        raise InstallError("no-manifest")
    report = []
    for item in manifest["files"]:
        path = Path(item["path"])
        now = sha256_file(path)
        entry = {"id": item["id"], "path": item["path"], "unchanged_since_backup": now == item.get("sha256"),
                 "changed_since_install": now != item.get("sha256_after_install") if item.get("sha256_after_install") else None}
        if item["id"] == "codex-config" and path.is_file():
            old = Path(item["backup"]).read_text(encoding="utf-8").splitlines() if item.get("backup") else []
            new = path.read_text(encoding="utf-8").splitlines()
            diff = [line for line in difflib.unified_diff(old, new, lineterm="", n=0)
                    if line[:1] in "+-" and line[:3] not in ("+++", "---")]
            entry["lines_added"] = sum(1 for l in diff if l.startswith("+"))
            entry["lines_removed"] = sum(1 for l in diff if l.startswith("-"))
            entry["tables_added"] = sorted(set(_toml_table_names("\n".join(new))) - set(_toml_table_names("\n".join(old))))
            entry["tables_removed"] = sorted(set(_toml_table_names("\n".join(old))) - set(_toml_table_names("\n".join(new))))
        if item["id"] == "claude-settings" and path.is_file():
            try:
                new = json.loads(path.read_text(encoding="utf-8"))
                old = json.loads(Path(item["backup"]).read_text(encoding="utf-8")) if item.get("backup") else {}
            except (OSError, ValueError):
                new = old = None
            if isinstance(new, dict) and isinstance(old, dict):
                entry["top_level_keys_added"] = sorted(set(new) - set(old))
                entry["top_level_keys_removed"] = sorted(set(old) - set(new))
                entry["top_level_keys_changed"] = sorted(k for k in set(new) & set(old) if new[k] != old[k])
                old_allow = ((old.get("permissions") or {}).get("allow") or []) if isinstance(old.get("permissions"), dict) else []
                new_allow = ((new.get("permissions") or {}).get("allow") or []) if isinstance(new.get("permissions"), dict) else []
                entry["permissions_allow_added"] = [r for r in new_allow if r not in old_allow and isinstance(r, str)][:40]
                entry["enabled_plugins_now"] = sorted(k for k, v in (new.get("enabledPlugins") or {}).items() if v) \
                    if isinstance(new.get("enabledPlugins"), dict) else []
        report.append(entry)
    return {"ok": True, "state": manifest.get("state"), "files": report}


def restore(directory: Path, keep_other_changes: bool = False) -> dict:
    manifest = load_manifest(directory)
    if not manifest:
        raise InstallError("no-manifest")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = []
    for item in manifest["files"]:
        path = Path(item["path"])
        current = sha256_file(path)
        row = {"id": item["id"], "path": item["path"], "pre_install_sha256": item.get("sha256"),
               "hash_before_restore": current}
        diverged = (current != item.get("sha256")) and current is not None
        if keep_other_changes:
            row.update(action="kept", hash_after=current, matches_pre_install=current == item.get("sha256"),
                       note="surgical removal only; byte-identical restore was not requested")
            results.append(row)
            continue
        if diverged:
            keep = directory / f"diverged-{stamp}"
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, keep / path.name)
            row["diverged_copy"] = str(keep / path.name)
        if item.get("existed"):
            shutil.copy2(item["backup"], path)
            row["action"] = "restored-from-backup"
        elif path.is_file():
            path.unlink()
            row["action"] = "removed-file-that-did-not-exist-before"
        else:
            row["action"] = "nothing-to-do"
        after = sha256_file(path)
        row.update(hash_after=after, matches_pre_install=after == item.get("sha256"))
        results.append(row)
    ok = all(r["matches_pre_install"] for r in results) if not keep_other_changes else None
    manifest["state"] = "restored" if (ok or keep_other_changes) else "restore-mismatch"
    manifest["restored_at"] = stamp
    save_manifest(directory, manifest)
    return {"ok": ok is not False, "state": manifest["state"], "hashes_match_pre_install": ok, "files": results,
            "informational_before": manifest.get("informational_before"),
            "informational_now": informational_hashes(), "journal_kept": str(core.journal_path())}


def verify_installed(codex_config: Path | None = None, claude_settings: Path | None = None) -> dict:
    codex_config = codex_config or codex_config_path()
    claude_settings = claude_settings or claude_settings_path()
    value = {"codex_block_present": False, "claude_plugin_enabled": None,
             "permission_rules": permission_rules_status(claude_settings)}
    try:
        text = codex_config.read_text(encoding="utf-8")
        value["codex_block_present"] = BEGIN in text and f"[{TABLE}]" in text
        parsed = _parse_toml(text)
        if parsed is not None:
            server = (parsed.get("mcp_servers") or {}).get("crosstalk_p0")
            value["codex_server"] = {"mode": server.get("default_tools_approval_mode"),
                                     "command_exists": Path(server.get("command", "")).is_file(),
                                     "script_exists": any(Path(a).is_file() for a in server.get("args", []))} \
                if isinstance(server, dict) else None
    except OSError:
        value["codex_config_readable"] = False
    try:
        settings = json.loads(claude_settings.read_text(encoding="utf-8"))
        plugins = settings.get("enabledPlugins") if isinstance(settings, dict) else None
        value["claude_plugin_enabled"] = bool(isinstance(plugins, dict) and plugins.get(PLUGIN_ID))
        markets = settings.get("extraKnownMarketplaces") if isinstance(settings, dict) else None
        value["claude_marketplace_declared"] = bool(isinstance(markets, dict) and MARKETPLACE in markets)
    except (OSError, ValueError):
        value["claude_settings_readable"] = False
    return value


# ----------------------------------------------------------- Hermes plugin (3B.12)
# Hermes Desktop is one local install with no account concept. The plugin folder is copied into
# <HERMES_HOME>/plugins/crosstalk and enabled through Hermes' own CLI; nothing here edits config.yaml by hand
# (it is only copied aside once). Test-only: --hermes-home and the CROSSTALK_P0_HERMES_HOME environment name.
HERMES_PLUGIN_NAME = "crosstalk"
HERMES_CONFIG_KEY = "plugins.entries.crosstalk.allow_gateway_injection"
HERMES_CONFIG_BACKUP = "config.yaml.bak-crosstalk"
# Identity proof for an existing <home>/plugins/crosstalk: the folder must carry these (see hermes_plugin_identity).
HERMES_MANIFEST_MARKER = "EUTONOS Crosstalk"
HERMES_CODE_MARKER = "EUTONOS Crosstalk plugin for Hermes"
HERMES_CODE_TAG = "crosstalk.hermes.plugin.v1"


def hermes_plugin_source() -> Path:
    return HERE / "hermes-plugin" / HERMES_PLUGIN_NAME


def hermes_cli_path(hermes_home: Path) -> Path:
    return hermes_home / "bin" / "hermes.cmd"


def _hermes_steps(hermes_home: Path, python: str) -> list[dict]:
    cli = str(hermes_cli_path(hermes_home))
    target = hermes_home / "plugins" / HERMES_PLUGIN_NAME
    return [
        {"step": "backup-config", "action": f"copy {hermes_home / 'config.yaml'} to {hermes_home / HERMES_CONFIG_BACKUP} "
                                             "(only when the backup does not exist yet)"},
        {"step": "copy-plugin", "action": f"build {hermes_plugin_source()} as a temporary sibling of {target} and swap it "
                                          "in (an existing folder is replaced only when it proves it is this plugin; "
                                          "otherwise refuse unless --force, which keeps it renamed aside)"},
        {"step": "write-config-file", "action": f"write {target / 'crosstalk.json'} with python={python}, "
                                                f"cli={HERE / 'crosstalk_p0_cli.py'}, device_dir={core.device_dir()}"},
        {"step": "enable-plugin", "command": [cli, "plugins", "enable", HERMES_PLUGIN_NAME]},
        {"step": "allow-injection", "command": [cli, "config", "set", HERMES_CONFIG_KEY, "true"]},
        {"step": "restart", "action": "restart Hermes Desktop (its backend) so the plugin loads; nothing is restarted here"},
    ]


def _hermes_version_text(home: Path) -> str | None:
    """The installed Hermes version (read from its install stamp; local files only), for the plan and the report."""
    try:
        info = core.hermes_version_info(core.hermes_source_root(home=home))
    except OSError:
        info = None
    return info["fingerprint"] if info else None


def hermes_plan(hermes_home: Path | None = None, python: str | None = None) -> dict:
    """The exact install and remove steps. Read-only: touches nothing."""
    home = hermes_home or core.hermes_home()
    python = python or str(runtime_python())
    return {"ok": True, "hermes_home": str(home), "plugin_source": str(hermes_plugin_source()),
            "plugin_target": str(home / "plugins" / HERMES_PLUGIN_NAME), "python": python,
            "python_exists": Path(python).is_file(), "hermes_cli": str(hermes_cli_path(home)),
            "hermes_cli_exists": hermes_cli_path(home).is_file(),
            "installed": (home / "plugins" / HERMES_PLUGIN_NAME / "__init__.py").is_file(),
            "plugin_version_to_install": core._source_plugin_version(),
            "hermes_version": _hermes_version_text(home),
            "target_identity": hermes_plugin_identity(home / "plugins" / HERMES_PLUGIN_NAME),
            "install_steps": _hermes_steps(home, python),
            "remove_steps": [{"command": [str(hermes_cli_path(home)), "plugins", "disable", HERMES_PLUGIN_NAME]},
                             {"command": [str(hermes_cli_path(home)), "config", "set", HERMES_CONFIG_KEY, "false"]},
                             {"action": f"delete {home / 'plugins' / HERMES_PLUGIN_NAME} only when it proves it is this "
                                        f"plugin (otherwise nothing is touched); {home / 'crosstalk'} (letters, "
                                        "heartbeat) and config.yaml stay"}]}


def _hermes_run(runner, command: list[str], home: Path, explicit_home: bool) -> dict:
    env = dict(os.environ, HERMES_HOME=str(home)) if explicit_home else None
    done = runner(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                  errors="replace", timeout=120, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"command": command, "exit": done.returncode, "stderr_tail": (done.stderr or "").strip()[-200:]}


def _is_link(path: Path) -> bool:
    """A symlink or (Windows) junction: never followed, copied over or deleted as if it were this plugin's folder."""
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def hermes_plugin_identity(folder: Path) -> dict:
    """Does `folder` prove it is this EUTONOS Crosstalk Hermes plugin? Reads two small files, never executes them.

    state: absent | empty | ours | foreign. `ours` needs plugin.yaml to name the plugin `crosstalk` and carry the
    EUTONOS description, and __init__.py to carry this plugin's docstring and its TAG constant. A hand-made or other
    vendor's folder of the same generic name fails at least one of these, as do links and plain files."""
    if not os.path.lexists(folder):
        return {"state": "absent"}
    if _is_link(folder) or not folder.is_dir():
        return {"state": "foreign", "why": "not-a-real-directory"}
    try:
        if not any(folder.iterdir()):
            return {"state": "empty"}
    except OSError:
        return {"state": "foreign", "why": "unreadable"}

    def head(name: str, limit: int) -> str | None:
        path = folder / name
        try:
            if _is_link(path) or not path.is_file():
                return None
            with path.open("rb") as stream:
                return stream.read(limit).decode("utf-8", errors="replace")
        except OSError:
            return None

    manifest, code = head("plugin.yaml", 65536), head("__init__.py", 262144)
    missing = []
    if manifest is None or not re.search(r"(?m)^name:\s*[\"']?crosstalk[\"']?\s*$", manifest):
        missing.append("plugin.yaml-name")
    if manifest is None or HERMES_MANIFEST_MARKER not in manifest:
        missing.append("plugin.yaml-marker")
    if code is None or HERMES_CODE_MARKER not in code or HERMES_CODE_TAG not in code:
        missing.append("init-marker")
    return {"state": "foreign", "why": "missing:" + ",".join(missing)} if missing else {"state": "ours"}


def _hermes_backup_config(home: Path, report: dict) -> None:
    config, backup_path = home / "config.yaml", home / HERMES_CONFIG_BACKUP
    if config.is_file() and not backup_path.exists():
        shutil.copy2(config, backup_path)
        report["steps"].append({"step": "backup-config", "backup": str(backup_path),
                                "sha256": sha256_file(backup_path)})
    else:
        report["steps"].append({"step": "backup-config", "skipped": "config missing or backup already exists"})


def _hermes_swap_in(source: Path, target: Path, config_value: dict, keep_old: bool) -> dict:
    """Build the new plugin folder beside the target, then swap it in. Old files never survive into the new
    folder. A foreign old folder (keep_old) is renamed aside, never deleted; our own is removed after the swap."""
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    fresh = parent / f".{target.name}.new-{stamp}"
    old = parent / f".{target.name}.old-{stamp}"
    try:
        shutil.copytree(source, fresh, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        core._write_json_atomic(fresh / "crosstalk.json", config_value)
        had_old = os.path.lexists(target)
        if had_old:
            os.replace(target, old)
        try:
            os.replace(fresh, target)
        except OSError:
            if had_old:
                os.replace(old, target)  # put the previous folder back
            raise
    finally:
        if fresh.exists():
            shutil.rmtree(fresh, ignore_errors=True)
    result = {"replaced": had_old, "files": sum(1 for p in target.rglob("*") if p.is_file())}
    if had_old:
        if keep_old:
            result["previous_kept_at"] = str(old)
        else:
            shutil.rmtree(old)
    return result


def hermes_install(hermes_home: Path | None = None, python: str | None = None, runner=None,
                   force: bool = False, backup_config: bool = True) -> dict:
    """Copy and enable the plugin. `runner` replaces subprocess.run in tests. Stops at the first failed step.

    An existing <home>/plugins/crosstalk is replaced only when it proves it is this plugin (hermes_plugin_identity);
    any other content there makes the install refuse unless `force`, which keeps it renamed aside."""
    explicit = hermes_home is not None or bool(os.environ.get("CROSSTALK_P0_HERMES_HOME"))
    home = hermes_home or core.hermes_home()
    launcher = core.kit_mode() and not python  # a materialized kit points Hermes at the stable launcher (IPC5 F6)
    python = python or str(launcher_path() if launcher else runtime_python())
    runner = runner or subprocess.run
    source, cli = hermes_plugin_source(), hermes_cli_path(home)
    if not (source / "__init__.py").is_file():
        raise InstallError("hermes-plugin-source-missing", str(source))
    if not Path(python).is_file():
        raise InstallError("runtime-python-missing", python)
    if not cli.is_file():
        raise InstallError("hermes-cli-missing", str(cli))
    target = home / "plugins" / HERMES_PLUGIN_NAME
    identity = hermes_plugin_identity(target)
    if identity["state"] == "foreign" and not force:
        raise InstallError("hermes-plugin-folder-not-ours",
                           f"{target} exists and does not prove it is the EUTONOS Crosstalk plugin "
                           f"({identity.get('why')}); nothing was changed. Move it away or pass --force")
    report = {"ok": True, "hermes_home": str(home), "steps": []}
    if backup_config:  # the component installer already owns an exact before-image under H/install/backups
        _hermes_backup_config(home, report)
    swapped = _hermes_swap_in(
        source, target,
        {"python": python, "cli": "-" if launcher else str(HERE / "crosstalk_p0_cli.py"),
         "device_dir": str(core.device_dir()),
         "installed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        keep_old=identity["state"] == "foreign")
    report["steps"].append(dict(swapped, step="copy-plugin", target=str(target), previous=identity["state"]))
    for name, command in (("enable-plugin", [str(cli), "plugins", "enable", HERMES_PLUGIN_NAME]),
                          ("allow-injection", [str(cli), "config", "set", HERMES_CONFIG_KEY, "true"])):
        result = _hermes_run(runner, command, home, explicit)
        report["steps"].append(dict(result, step=name))
        if result["exit"] != 0:
            report["ok"] = False
            report["reason"] = name + "-failed"
            return report
    # Remember which Hermes version the plugin was installed for: a later Hermes update is flagged until a
    # self-test passes on the new version (best effort; the install itself already succeeded).
    recorded = core.hermes_link_record_install(core._source_plugin_version(), home)
    report["hermes_version_recorded"] = (((recorded or {}).get("version") or {}).get("installed") or {}).get("fingerprint")
    report["next"] = ("restart Hermes Desktop so its backend loads the plugin, then run hermes-selftest "
                      "(or hermes-status from a chat)")
    return report


def hermes_remove(hermes_home: Path | None = None, runner=None) -> dict:
    """Disable the plugin, switch its injection setting off and delete its folder, after proving the folder is ours.

    Another vendor's `crosstalk` folder (or a link) is left completely alone, with no step run. Letters and
    heartbeat stay; config.yaml is copied aside once first and only the one injection key is set back to false."""
    explicit = hermes_home is not None or bool(os.environ.get("CROSSTALK_P0_HERMES_HOME"))
    home = hermes_home or core.hermes_home()
    runner = runner or subprocess.run
    cli = hermes_cli_path(home)
    target = home / "plugins" / HERMES_PLUGIN_NAME
    identity = hermes_plugin_identity(target)
    report = {"ok": True, "hermes_home": str(home), "steps": []}
    if identity["state"] == "foreign":
        report.update(ok=False, reason="hermes-plugin-folder-not-ours", target=str(target), why=identity.get("why"))
        return report
    _hermes_backup_config(home, report)
    if cli.is_file():
        for name, command in (("disable-plugin", [str(cli), "plugins", "disable", HERMES_PLUGIN_NAME]),
                              ("reset-injection", [str(cli), "config", "set", HERMES_CONFIG_KEY, "false"])):
            result = _hermes_run(runner, command, home, explicit)
            report["steps"].append(dict(result, step=name))
            if result["exit"] != 0:
                report["ok"] = False
                report["reason"] = name + "-failed"
                return report
    else:
        report["steps"].append({"step": "disable-plugin", "skipped": "hermes cli not found"})
        report["steps"].append({"step": "reset-injection", "skipped": "hermes cli not found",
                                "still_set": HERMES_CONFIG_KEY})
    existed = identity["state"] != "absent"
    if existed:
        shutil.rmtree(target)
    report["steps"].append({"step": "delete-plugin", "existed": existed, "target": str(target)})
    report["left_in_place"] = [str(home / "crosstalk"), str(home / "config.yaml")]
    return report


# ------------------------------------------------------------------------ cli
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crosstalk_p0_install")
    parser.add_argument("--codex-config")
    parser.add_argument("--claude-settings")
    parser.add_argument("--state-dir")
    parser.add_argument("--hermes-home", help="tests only: Hermes data folder override")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render-plugin")
    render.add_argument("--runtime")
    render.add_argument("--cli")
    back = sub.add_parser("backup")
    back.add_argument("--force", action="store_true")
    add = sub.add_parser("toml-add")
    add.add_argument("--mode", required=True, choices=APPROVAL_MODES)
    add.add_argument("--runtime")
    add.add_argument("--server")
    add.add_argument("--startup", type=int, default=20)
    add.add_argument("--tool-timeout", type=int, default=120)
    sub.add_parser("toml-remove")
    sub.add_parser("upgrade-snapshot")
    sub.add_parser("rebind-marketplace")
    up = sub.add_parser("toml-upgrade")
    up.add_argument("--mode", choices=APPROVAL_MODES)
    vu = sub.add_parser("verify-upgrade")
    vu.add_argument("--snapshot", required=True)
    rec = sub.add_parser("record-installed")
    rec.add_argument("--mode")
    sub.add_parser("compare")
    res = sub.add_parser("restore")
    res.add_argument("--keep-other-changes", action="store_true")
    sub.add_parser("verify")
    sub.add_parser("state")
    sub.add_parser("permission-rules")
    perms = sub.add_parser("permissions")
    perms.add_argument("--apply-permissions", action="store_true",
                       help="write settings.json (backup first, only missing entries); default is to show the plan")
    sub.add_parser("permissions-remove")
    sub.add_parser("relay-folder-remove")
    for name in ("hermes-plan", "hermes-install", "hermes-remove"):
        item = sub.add_parser(name)
        if name == "hermes-install":
            item.add_argument("--force", action="store_true",
                              help="replace a plugins/crosstalk folder that is not this plugin (kept renamed aside)")
    for name in ("helper-startup-plan", "helper-startup-install", "helper-startup-remove"):
        item = sub.add_parser(name)
        item.add_argument("--startup-folder", help="tests only: Startup folder override")
        if name != "helper-startup-remove":
            item.add_argument("--helper-script", help="the crosstalk_helper.py the shortcut starts")
    args = parser.parse_args(argv)
    codex = codex_config_path(args.codex_config)
    settings = claude_settings_path(args.claude_settings)
    directory = state_dir(args.state_dir)
    try:
        if args.command == "render-plugin":
            value = render_plugin(args.runtime, args.cli)
        elif args.command == "backup":
            value = backup(codex, settings, directory, args.force)
        elif args.command == "toml-add":
            value = toml_add(codex, args.mode, args.runtime or str(runtime_pythonw()),
                             args.server or str(HERE / "codex_tool_server.py"), args.startup, args.tool_timeout)
        elif args.command == "toml-remove":
            value = toml_remove(codex)
        elif args.command == "upgrade-snapshot":
            value = upgrade_snapshot(codex, settings, directory)
        elif args.command == "rebind-marketplace":
            value = rebind_marketplace(settings)
        elif args.command == "toml-upgrade":
            value = toml_upgrade(codex, str(runtime_pythonw()), str(HERE / "codex_tool_server.py"), args.mode)
        elif args.command == "verify-upgrade":
            value = verify_upgrade(settings, Path(args.snapshot))
        elif args.command == "record-installed":
            value = record_installed(directory, args.mode)
        elif args.command == "compare":
            value = compare(directory)
        elif args.command == "restore":
            value = restore(directory, args.keep_other_changes)
        elif args.command == "permission-rules":
            value = dict(permission_rules_status(settings), ok=True)
        elif args.command == "relay-folder-remove":
            value = relay_folder_remove()
        elif args.command == "permissions":
            value = dict(permission_rules_status(settings), ok=True, applied=False)
            if args.apply_permissions:
                value.update(permissions_apply(settings, directory), applied=True)
                value["after"] = permission_rules_status(settings)["would_add"]
        elif args.command == "permissions-remove":
            value = permissions_remove(settings, directory)
        elif args.command == "helper-startup-plan":
            value = helper_startup_plan(args.startup_folder, script=args.helper_script)
        elif args.command == "helper-startup-install":
            value = helper_startup_install(args.startup_folder, script=args.helper_script)
        elif args.command == "helper-startup-remove":
            value = helper_startup_remove(args.startup_folder)
        elif args.command == "hermes-plan":
            value = hermes_plan(Path(args.hermes_home) if args.hermes_home else None)
        elif args.command == "hermes-install":
            value = hermes_install(Path(args.hermes_home) if args.hermes_home else None, force=args.force)
        elif args.command == "hermes-remove":
            value = hermes_remove(Path(args.hermes_home) if args.hermes_home else None)
        elif args.command == "state":
            manifest = load_manifest(directory)
            value = {"ok": True, "state": manifest.get("state") if manifest else "none",
                     "approval_mode": manifest.get("approval_mode") if manifest else None}
        else:
            value = verify_installed(codex, settings)
            value["ok"] = True
        value.setdefault("tag", TAG)
        print(json.dumps(value, sort_keys=True))
        return 0 if value.get("ok") is not False else 3
    except InstallError as error:
        print(json.dumps({"tag": TAG, "ok": False, "reason": error.reason, "detail": error.detail}))
        return 2
    except Exception as error:
        print(json.dumps({"tag": TAG, "ok": False, "reason": "install-helper-exception",
                          "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
