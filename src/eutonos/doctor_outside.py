"""Read-only readbacks of the outside-the-repository effects that have no file for the doctor to hash (ISU stage 2).

Each declared permissions entry that lives in the registry, in Windows Installed apps, in the runtime folder or in
Hermes' config gets one readback here, so ``eutonos doctor`` names its state instead of "no readback is available":

  user-path, user-env-home, installed-apps-entry, runtime-pip, hermes-gateway, companion-appearance (a registry value)

and ``declared_report`` turns permissions.json ``outside_effects`` (Startup shortcuts, the scheduled task, Simon skills,
instruction blocks, the plugin registration, the Appearance value) into one readback row each.

Everything goes through an injectable ``ports`` object (``installer_ports.Ports`` on the real machine, ``FakePorts`` in
tests) and the files under the EUTONOS home; nothing is written, no process is started, and no account or credential
file is opened. A row is ``{"state", "reason"}`` with the doctor's states: ok, missing, changed by you, skipped or unknown.
"""
from __future__ import annotations

from pathlib import Path

from . import component_support as cs, install_record as ir, installer_effects as fx, permissions
from . import home as eutonos_home

ENTRIES = ("user-path", "user-env-home", "installed-apps-entry", "runtime-pip", "hermes-gateway", "companion-appearance")
APPEARANCE_VALUES = ("system", "light", "dark")


def _row(state: str, reason: str | None = None) -> dict:
    return {"state": state, "reason": reason}


def default_ports(os_name: str):
    """The real ports, only on the OS they can read (never on Linux CI, where there is no registry)."""
    from . import installer_ports
    return installer_ports.Ports(os_name) if os_name in ("windows", "macos") else None


def _path(ports, layout: ir.Layout, user_home: Path) -> dict:
    bin_dir = str(layout.bin)
    if ports.os_name == "windows":
        value = ports.user_path_get()
        if any(fx._same_entry(part, bin_dir) for part in (value or "").split(";") if part.strip()):
            return _row("ok")
        return _row("missing", f"{bin_dir} is not on the user PATH")
    profile = Path(user_home) / ".zprofile"
    line = f'export PATH="{bin_dir}:$PATH" {fx.PATH_MARK}'
    try:
        found = line in profile.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        found = False
    return _row("ok") if found else _row("missing", f"{profile} has no EUTONOS PATH line")


def _env_home(ports, layout: ir.Layout, user_home: Path) -> dict:
    if ports.os_name != "windows":
        return _row("skipped", "only the Windows user environment is used")
    value = ports.user_env_get(fx.ENV_HOME)
    if value is not None:
        if fx._same_entry(value, str(layout.base)):
            return _row("ok")
        return _row("changed by you", f"EUTONOS_HOME names {value}; a value you set is never changed")
    if fx._same_entry(str(layout.base), str(Path(user_home) / eutonos_home.default().name)):
        return _row("ok", "not needed: this is the default folder")
    return _row("missing", "EUTONOS_HOME is not set for this folder")


def _installed_apps(ports) -> dict:
    if ports.os_name != "windows":
        return _row("skipped", "Installed apps is a Windows list")
    entry = ports.uninstall_entry_get()
    if entry is None:
        return _row("missing", "EUTONOS is not listed in Installed apps (a Setup.exe install adds it)")
    if not entry.get("UninstallString"):
        return _row("changed by you", "the Installed apps entry has no uninstall command")
    return _row("ok", "listed as " + str(entry.get("DisplayName") or "EUTONOS") +
                (f" {entry['DisplayVersion']}" if entry.get("DisplayVersion") else ""))


def _runtime_pip(ports, layout: ir.Layout, record: dict | None) -> dict:
    pin = permissions.entry_by_id(permissions.load(), "runtime-pip")["value"]
    artifact = ((record or {}).get("wheel") or {}).get("artifact_id")
    if not artifact:
        return _row("missing", "there is no active runtime to read")
    site = ports.site_packages(layout.runtimes / artifact)
    if (site / f"pip-{pin['version']}.dist-info").is_dir():
        return _row("ok", f"pip {pin['version']} is in the runtime")
    others = sorted(item.name for item in site.glob("pip-*.dist-info")) if site.is_dir() else []
    if others:
        return _row("changed by you", f"the runtime holds {others[0]}, not the pinned pip {pin['version']}")
    return _row("missing", f"pip {pin['version']} is not in the runtime")


def _hermes(effects: list[dict]) -> dict:
    found = next((item for item in effects if item.get("component") == "hermes"
                  and isinstance(item.get("undo"), dict) and "config_after" in item["undo"]), None)
    if found is None:
        return _row("skipped", "Hermes was not found when EUTONOS was installed, so nothing was enabled there")
    undo = found["undo"]
    folder, config = Path(undo.get("folder") or found["target"]), Path(undo["config"])
    if not folder.is_dir():
        return _row("missing", "the Hermes plugin folder is not there")
    if cs.sha256(permissions._read(config)) != undo["config_after"]:
        return _row("changed by you", "the Hermes config differs from what EUTONOS wrote (enable or injection may have changed)")
    return _row("ok", "the plugin folder is there and the Hermes config is as EUTONOS wrote it")


def _appearance(ports) -> dict:
    if ports.os_name != "windows":
        return _row("skipped", "the companion Appearance value is a Windows registry value")
    value = ports.appearance_get()
    if value is None:
        return _row("ok", "not set (written only when you choose an Appearance)")
    if value in APPEARANCE_VALUES:
        return _row("ok", f"set to {value}")
    return _row("changed by you", f"holds {value!r}, which is not one of {', '.join(APPEARANCE_VALUES)}")


def readbacks(home, user_home, record: dict | None, effects: list[dict], ports) -> dict[str, dict]:
    """``{permissions entry id: {state, reason}}`` for ``ENTRIES``. ``ports=None`` (an OS with no readable registry)
    reports each as unknown."""
    if ports is None:
        return {name: _row("unknown", "no readback on this operating system") for name in ENTRIES}
    layout = ir.Layout(home)
    result = {}
    for name, reader in (("user-path", lambda: _path(ports, layout, user_home)),
                         ("user-env-home", lambda: _env_home(ports, layout, user_home)),
                         ("installed-apps-entry", lambda: _installed_apps(ports)),
                         ("runtime-pip", lambda: _runtime_pip(ports, layout, record)),
                         ("hermes-gateway", lambda: _hermes(effects)),
                         ("companion-appearance", lambda: _appearance(ports))):
        try:
            result[name] = reader()
        except (OSError, ImportError, ValueError, KeyError) as exc:
            result[name] = _row("unknown", f"readback failed ({type(exc).__name__})")
    return result


def declared_report(items, *, os_name: str, components: list[dict], effect_rows: list[dict], outside: dict) -> list[dict]:
    """One row per declared outside effect (permissions.json ``outside_effects``) with its readback state: the recorded
    effects it names read back byte for byte (``effect_rows`` is the doctor's own effect report), or the ports readback
    named by its ``readback`` field. A declared effect on another OS, or of a part that is not switched on, is skipped."""
    owners = {row.get("id"): row for row in components if isinstance(row, dict)}
    rows = []
    for item in items or []:
        row = {"id": item["id"], "component": item["component"], "kind": item["kind"], "where": item["where"],
               "removal": item["removal"], "effects": []}
        owner = owners.get(item["component"]) or {}
        if os_name not in item["os"]:
            row.update(state="skipped", reason=f"not used on {os_name}")
        elif owner and owner.get("selected") is False:
            row.update(state="skipped", reason=f"the {item['component']} part is not switched on")
        elif item["readback"] != "effect":
            found = outside.get(item["readback"]) or _row("unknown", "no readback is available for this declared effect")
            row.update(state=found["state"], reason=found["reason"])
        else:
            matches = [effect for effect in effect_rows
                       if any(str(effect.get("effect_id") or "").startswith(prefix) for prefix in item["effects"])]
            row["effects"] = [effect["effect_id"] for effect in matches]
            states = {effect.get("state") for effect in matches}
            if not matches:
                row.update(state="missing", reason="no recorded effect for it (it was not written, or the person's own was kept)")
            elif states == {"ok"}:
                row.update(state="ok", reason=None)
            else:
                worst = next(name for name in ("changed by you", "missing", "unknown") if name in states)
                row.update(state=worst, reason=next((effect.get("reason") for effect in matches if effect.get("state") == worst), None))
        rows.append(row)
    return rows
