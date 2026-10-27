"""Dashboard feeds and the guarded owner-control action for Crosstalk.

``/api/v1/crosstalk/status`` reads the device controls and the journal tail
(metadata only: ids, from/to keys, state, time -- never a letter body) through
``crosstalk_status``. ``/api/v1/crosstalk/action`` runs Crosstalk's own CLI
(halt, resume, enable, off) and proves the result by re-reading controls.json.
This module never imports ``tools/crosstalk`` and never starts, resends or wakes
anything. Each function returns ``{"ok": False, "error": ...}`` instead of
raising, so no traceback reaches the HTTP layer.
"""
from __future__ import annotations

from pathlib import Path

from . import crosstalk_status

ACTIONS = ("halt", "resume", "enable", "off")


def _summary(state: dict) -> str:
    if not state["installed"]:
        return "Crosstalk not installed on this device"
    parts = ["Crosstalk messaging " + ("on" if state["messaging"] == "on" else "off")]
    if state["halt"]:
        parts.append("HALTED")
    parts.append(f"{state['denied']} denied")
    return ", ".join(parts)


def status(root) -> dict:
    try:
        state = crosstalk_status.status(root)
        letters = crosstalk_status.recent_letters() if state["installed"] else []
        return {"ok": True, "installed": state["installed"], "messaging": state["messaging"],
                "halted": state["halt"], "denied": state["denied"], "available": state["available"],
                "reason": state["reason"], "device_folder": state["device_folder"],
                "cli": state["cli"] or (str(crosstalk_status.cli_path(*crosstalk_status.default_roots()) or "") or None),
                "recent_letters": letters, "summary": _summary(state)}
    except Exception as exc:  # never a traceback out of this route
        return {"ok": False, "error": type(exc).__name__}


def action(root, payload: dict) -> dict:
    try:
        if not isinstance(payload, dict) or set(payload) != {"action"}:
            raise ValueError("crosstalk action takes only an action")
        verb = payload.get("action")
        if verb not in ACTIONS:
            return {"ok": False, "error": "unknown-action"}
        result = crosstalk_status.owner_control(verb, Path(root))
        return {**result, "summary": _summary(crosstalk_status.status(root))}
    except ValueError as exc:
        return {"ok": False, "error": "invalid-action", "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


__all__ = ["ACTIONS", "action", "status"]
