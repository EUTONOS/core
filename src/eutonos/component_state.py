"""Which EUTONOS parts are switched on: ``<home>/settings/components.json``.

Setup installs every available part; its checkboxes only choose which ones start switched on. This file is the one
place that says whether a part is enabled, so the dashboard, the CLI (``eutonos-install component ...``) and the
doctor all read the same answer through ``component_enabled``.

    {"profile": "eutonos.components.state.v1", "updated_at": ISO-UTC,
     "components": {"<id>": {"enabled": bool, "source": "install"|"toggle", "changed_at": ISO-UTC, "detail": {...}}}}

``source`` says who chose the state: "install" is the Setup choice (or the owner profile) as of the last install or
update, so a later update may re-derive it; "toggle" is the person switching the part with ``component enable`` or
``disable``, which an update and a rollback keep. ``detail`` is the part's own note about how it was switched off (for example Crosstalk records whether it switched
the Claude plugin off or fell back to turning messaging off), so switching it on can undo exactly that.

Rules: a missing file or a missing key means the install-time choice (the caller's ``default``). A file that cannot
be read or has the wrong shape raises ``ComponentStateError``; it is never read as "everything on". Writes are atomic
and run under the machine lifecycle lock.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import home as eutonos_home
from . import setup_checkpoint as sc

PROFILE = "eutonos.components.state.v1"
WRITE_WAIT_SECONDS = 30.0


class ComponentStateError(ValueError):
    """The component state file is unreadable or has an unsupported shape (never silently treated as enabled)."""


def _base(home=None) -> Path:
    return Path(home) if home is not None else eutonos_home.home()


def path(home=None) -> Path:
    return _base(home) / "settings" / "components.json"


def read(home=None) -> dict:
    """``{component_id: {"enabled": bool, "changed_at": str, "detail": dict}}``; ``{}`` when the file is absent."""
    target = path(home)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ComponentStateError(f"the component state file is unreadable: {target}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ComponentStateError(f"the component state file is not valid JSON: {target}") from exc
    items = value.get("components") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("profile") != PROFILE or not isinstance(items, dict):
        raise ComponentStateError(f"the component state file has an unsupported shape: {target}")
    for name, item in items.items():
        if not isinstance(name, str) or not isinstance(item, dict) or not isinstance(item.get("enabled"), bool):
            raise ComponentStateError(f"the component state file has a bad entry for {name!r}: {target}")
    return {name: {"enabled": item["enabled"], "changed_at": item.get("changed_at"),
                   "source": "toggle" if item.get("source") == "toggle" else "install",
                   "detail": item["detail"] if isinstance(item.get("detail"), dict) else {}}
            for name, item in items.items()}


def component_enabled(home, component_id: str, default: bool = True) -> bool:
    """Is the part switched on? A missing file or key answers ``default`` (the install-time choice); an unreadable
    file raises ``ComponentStateError``."""
    entry = read(home).get(component_id)
    return default if entry is None else entry["enabled"]


def _document(items: dict, now=None) -> dict:
    return {"profile": PROFILE, "updated_at": sc.now_iso(now), "components": items}


def write_all(home, entries: dict, *, now=None) -> dict:
    """Replace the whole file with `entries` (``{id: {"enabled": bool, "detail": dict?}}``). Used at install time."""
    stamp = sc.now_iso(now)
    items = {}
    for name, item in sorted(entries.items()):
        items[name] = {"enabled": bool(item["enabled"]), "changed_at": item.get("changed_at") or stamp,
                       "source": "toggle" if item.get("source") == "toggle" else "install",
                       "detail": dict(item.get("detail") or {})}
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        sc._write_atomic(path(home), _document(items, now))
    return items


def set_enabled(home, component_id: str, enabled: bool, *, detail: dict | None = None, source: str = "toggle",
                now=None) -> dict:
    """Record one part's state, keeping every other entry. An unreadable existing file refuses (``ComponentStateError``)."""
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        items = read(home)
        items[component_id] = {"enabled": bool(enabled), "changed_at": sc.now_iso(now),
                               "source": "toggle" if source == "toggle" else "install", "detail": dict(detail or {})}
        sc._write_atomic(path(home), _document(items, now))
        return items[component_id]


def remove_file(home=None) -> bool:
    """Delete the state file (uninstall, or a fresh install replacing a stale one). True when a file was removed."""
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        try:
            path(home).unlink()
            return True
        except FileNotFoundError:
            return False
