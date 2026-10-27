"""The dashboard's machine pages (Settings > This computer: Parts, Crosstalk, Claude): one read and the writes behind them.

Setup installs every part and its ticks only enable or disable them; these pages are the same switches in the
dashboard, plus the Crosstalk and Claude switches. It adds no logic of its own: each call goes to the owner that
already has it.

* parts: ``installer.component_status`` / ``installer.set_component``
* Crosstalk messaging, the helper switches, the deny list, the wake budget, the stop, RAM capture and retrieval, linked
  Claude accounts: ``feature_settings``
* update mode (manual, notify or auto): ``update_check``
* pending steps a switch could not finish: ``pending_actions``

``read`` never raises and never writes: each block carries its own outcome, so one being unavailable (EUTONOS not
installed, no Crosstalk kit, an unreadable file) does not hide the others. ``act`` validates one small request and
returns the owner's answer unchanged. The HTTP guard (Host and Origin check, ``X-Eutonos-Settings`` token, the POST
allowlist) lives in ``dashboard_cli``; nothing here is reachable without it.

The EUTONOS home is the usual one (``EUTONOS_HOME`` or ``~/.eutonos``), so a test points it at a temporary folder.
RAM is per project, so its read and write take the selected checkout; with no project it is reported as unavailable.
"""
from __future__ import annotations

from pathlib import Path

PROFILE = "eutonos.dashboard-features.v1"
ACTIONS = ("component", "messaging", "ram", "linked-accounts", "update-mode", "pending", "helper-switch", "everything-off",
           "halt", "deny", "wake-budget")
MAX_PENDING_SHOWN = 100
DONE_BY = "dashboard"


def _guard(reader):
    """One block's answer, or a plain failure for that block alone."""
    try:
        return reader()
    except Exception as exc:  # a damaged file or missing kit must not take the whole page down
        return {"outcome": "failed", "detail": f"{type(exc).__name__}: {exc}"[:300],
                "next_action": "Refresh the page; if it keeps failing, run eutonos-install doctor."}


def _components():
    from . import installer
    found = installer.component_status()
    if found.get("outcome") != "ok":
        return found
    try:
        kinds = {item["id"]: item.get("kind") for item in installer.load_components()}
    except Exception:  # kinds only decide the "always on" wording; the switches themselves do not depend on them
        kinds = {}
    for row in found.get("components", []):
        row["kind"] = kinds.get(row["id"])
    return found


def _pending():
    from . import pending_actions
    try:
        from . import crosstalk_link  # an offered Hermes link repair shows here as one pending action (3B.17)
        crosstalk_link.sync_pending_offer()
    except Exception:
        pass
    try:
        entries = pending_actions.list_actions(state="pending")
    except pending_actions.PendingActionsError as exc:
        return {"outcome": "refused", "detail": str(exc), "actions": [], "count": 0,
                "next_action": "Repair or delete the pending-actions file, then refresh."}
    shown = [{key: item.get(key) for key in ("id", "created_at", "source", "harness", "action", "why")}
             for item in entries[:MAX_PENDING_SHOWN]]
    return {"outcome": "ok", "actions": shown, "count": len(entries)}


def _updates():
    from . import update_check
    return update_check.status()


def read(root=None) -> dict:
    """Everything the page shows. `root` is the selected project's checkout, or None when there is none."""
    from . import feature_settings as fs
    ram = (_guard(lambda: fs.ram_status(root=root)) if root is not None else
           {"outcome": "no-project", "setting": "ram", "detail": "No project is selected. RAM settings belong to one "
            "project, so choose a repository to see them."})
    return {"profile": PROFILE,
            "components": _guard(_components),
            "settings": {fs.MESSAGING: _guard(fs.crosstalk_messaging_status), "ram": ram,
                         fs.LINKED: _guard(fs.linked_accounts_status), "update-mode": _guard(_updates),
                         fs.HELPER: _guard(fs.helper_switches_status),
                         "crosstalk-controls": _guard(fs.crosstalk_controls_status)},
            "pending": _guard(_pending),
            "notes": {"shared_login": fs.SHARED_LOGIN_NOTE, "ram_capture": fs.RAM_CAPTURE_NOTE,
                      "ram_project": fs.RAM_PROJECT_NOTE}}


def _text(value, name, limit=64):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError(f"{name} must be text of 1 to {limit} characters")
    return value


def _flag(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false")
    return value


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or not set(value) <= set(required) | set(optional):
        raise ValueError("The request does not have the expected fields")
    return value


def act(name: str, value, root=None) -> dict:
    """Run one switch. A request that is not well formed raises ``ValueError`` (the server answers 400); everything
    else, including a refusal, comes back as the owner's own answer. `root` is the selected project's checkout
    (only RAM uses it)."""
    from . import feature_settings as fs
    if name == "component":
        _keys(value, ("component", "enabled"))
        from . import installer
        part, enable = _text(value["component"], "component"), _flag(value["enabled"], "enabled")
        return _guard(lambda: installer.set_component(part, enable))
    if name == "messaging":
        _keys(value, ("enabled",))
        enable = _flag(value["enabled"], "enabled")
        return _guard(lambda: fs.set_crosstalk_messaging(enable))
    if name == "helper-switch":
        _keys(value, ("key", "value"))
        key, setting = _text(value["key"], "key", 40), value["value"]
        if isinstance(setting, list):
            if len(setting) > fs.MAX_ALWAYS_REMOTE or any(not isinstance(item, str) or len(item) > 80 for item in setting):
                raise ValueError("A list holds at most " + str(fs.MAX_ALWAYS_REMOTE) + " short entries")
        elif type(setting) not in (bool, int, float, str):
            raise ValueError("value must be on or off, a number, a choice or a list")
        return _guard(lambda: fs.set_helper_switch(key, setting))
    if name == "everything-off":
        _keys(value, ())
        return _guard(fs.turn_everything_off)
    if name == "halt":
        _keys(value, ("halt",))
        stop = _flag(value["halt"], "halt")
        return _guard(lambda: fs.set_halt(stop))
    if name == "deny":
        _keys(value, ("action", "key"))
        if value["action"] not in ("block", "allow"):
            raise ValueError("action must be block or allow")
        action, key = value["action"], _text(value["key"], "key", 120)
        return _guard(lambda: fs.set_deny(action, key))
    if name == "wake-budget":
        _keys(value, (), ("per_chat", "total", "window_s"))
        if not value or any(type(item) is not int for item in value.values()):
            raise ValueError("Say what to set, as whole numbers: per_chat, total, window_s")
        budget = dict(value)
        return _guard(lambda: fs.set_wake_budget(budget))
    if name == "ram":
        _keys(value, (), ("capture", "retrieval"))
        if not value:
            raise ValueError("Say what to set: capture, retrieval or both")
        capture = _flag(value["capture"], "capture") if "capture" in value else None
        retrieval = value.get("retrieval")
        if "retrieval" in value and not (type(retrieval) is bool or retrieval in ("on", "off")):
            raise ValueError("retrieval must be on or off")
        if root is None:
            return {"outcome": "refused", "setting": "ram", "reason": "no-project",
                    "detail": "No project is selected, so there is no project to set RAM for.",
                    "next_action": "Choose a repository, then try again."}
        return _guard(lambda: fs.set_ram(root=root, capture=capture, retrieval=retrieval))
    if name == "linked-accounts":
        _keys(value, ("action",), ("accounts",))
        if value["action"] == "suggest":
            if "accounts" in value:
                raise ValueError("A suggestion takes no accounts")
            return _guard(fs.suggest_linked_accounts)
        if value["action"] != "save" or not isinstance(value.get("accounts"), list) or len(value["accounts"]) > fs.MAX_LINKED:
            raise ValueError(f"Save needs a list of at most {fs.MAX_LINKED} accounts")
        accounts = [_text(item, "an account", 200) for item in value["accounts"]]
        return _guard(lambda: fs.set_linked_accounts(accounts))
    if name == "update-mode":
        _keys(value, ("mode",))
        from . import update_check
        if value["mode"] not in update_check.MODES:
            raise ValueError("mode must be manual, notify or auto")
        return _guard(lambda: update_check.set_mode(value["mode"]))
    if name == "pending":
        _keys(value, ("id", "action"))
        from . import pending_actions
        action_id = _text(value["id"], "id", 40)
        if value["action"] not in ("done", "dismiss"):
            raise ValueError("action must be done or dismiss")
        try:
            if value["action"] == "done":
                entry = pending_actions.complete(action_id=action_id, by=DONE_BY)
            else:
                entry = pending_actions.dismiss(action_id=action_id, note="dismissed from the dashboard")
        except pending_actions.PendingActionsError as exc:
            return {"outcome": "refused", "detail": str(exc), "next_action": "Refresh the list."}
        except Exception as exc:  # most often a busy lifecycle lock
            return {"outcome": "busy" if "busy" in type(exc).__name__.lower() else "failed",
                    "detail": f"{type(exc).__name__}: {exc}"[:300], "next_action": "Try again in a moment."}
        return {"outcome": "done" if value["action"] == "done" else "dismissed", "id": entry["id"], "state": entry["state"]}
    raise ValueError("Unknown feature action")


def project_root(selected, index):
    """The checkout for a project index, or None when there is no such project (a fresh machine selects none)."""
    return Path(selected[index]) if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(selected) else None
