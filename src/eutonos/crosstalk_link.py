"""Read-only view of the Crosstalk link to Hermes, and the one repair offer it can raise (slice 3B.17).

The Crosstalk kit (``tools/crosstalk``, guide ``docs/Crosstalk/README.md``, Hermes section) judges the link to
Hermes: whether the plugin loaded, whether it found the host that carries chats, whether Hermes was updated since
Crosstalk last checked, and whether the Hermes internals the plugin relies on are still there. It keeps the result in
``<device folder>/link-hermes.json`` (``crosstalk.link.v1``). This module only READS that file (and Hermes'
``install-stamp.json``), like ``crosstalk_status``, and never imports ``tools/crosstalk``. The kit never imports
``eutonos`` either, so the one thing the kit cannot do itself, record a pending action, is done here:
``sync_pending_offer`` turns an offered repair into exactly one pending action (``pending_actions`` rules: never a
second one for the same offer) and dismisses it when the link is repaired or a repair chat was started.

Consumers: workflow pickup (a line under ``crosstalk``), the doctor (a readback row), the dashboard Features page and
pickup (the pending action) and the Harness policy watch (``watch_*``: "Hermes internals Crosstalk uses changed", a
local, no-network item beside the official-documentation ones). Every function is safe when Crosstalk or Hermes is
absent: absence is a normal answer, never an exception.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import crosstalk_status

LINK_FILE = "link-hermes.json"
LINK_PROFILE = "crosstalk.link.v1"
OFFER_SOURCE = "crosstalk:link-repair:hermes"
WATCH_SOURCE = "hermes_internals"
WATCH_ID_PREFIX = "hermes-internals-"
WATCH_ACK_FILE = "hermes-internals-ack.json"
OFFER_STATES = ("offered", "start-failed")       # a repair the owner can still act on
CLOSED_STATES = ("resolved", "started", "starting", "off")


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def link_record() -> dict:
    """The kit's link record, or ``{}`` when there is none (Hermes never checked) or it is not ours."""
    value = _read_object(crosstalk_status.device_dir() / LINK_FILE)
    return value if value.get("profile") == LINK_PROFILE else {}


def hermes_home() -> Path:
    override = os.environ.get("CROSSTALK_P0_HERMES_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"


def installed_hermes_version(source_root=None) -> dict | None:
    """``{version, commit, fingerprint}`` from the installed Hermes' install-stamp.json (local file only)."""
    override = os.environ.get("CROSSTALK_P0_HERMES_SOURCE")
    root = Path(source_root) if source_root else Path(override) if override else hermes_home() / "hermes-agent"
    stamp = _read_object(root / "install-stamp.json")
    version = stamp.get("displayVersion") or stamp.get("baseVersion")
    if not version:
        return None
    commit = str(stamp["commit"])[:12] if stamp.get("commit") else None
    text = str(version)[:40]
    return {"version": text, "commit": commit, "fingerprint": f"{text}+{commit}" if commit else text}


def version_notice(record: dict | None = None) -> dict | None:
    """``{from, to, text}`` when Hermes' installed version differs from the one Crosstalk last checked. ``None`` when
    equal, or when no version was ever recorded (nothing to compare: no false alarm)."""
    record = link_record() if record is None else record
    checked = (record.get("version") or {}).get("checked")
    now = installed_hermes_version()
    if not isinstance(checked, dict) or not now or checked.get("fingerprint") == now["fingerprint"]:
        return None
    before, after = checked.get("version"), now["version"]
    if before == after:
        before, after = f"{before} ({checked.get('commit')})", f"{after} ({now['commit']})"
    return {"from": checked.get("version"), "to": now["version"],
            "text": f"Hermes updated from {before} to {after} since Crosstalk was last checked"}


def repair_mode() -> str:
    """ask (default), auto or off: the helper's ``link_repair_offer`` setting (the kit owns it)."""
    value = _read_object(crosstalk_status.device_dir() / "helper" / "config.json").get("link_repair_offer")
    return value if value in ("ask", "auto", "off") else "ask"


def _problems(record: dict) -> list:
    repair = record.get("repair") or {}
    if repair.get("state") in OFFER_STATES + ("starting", "started"):
        return [item for item in repair.get("reasons") or [] if isinstance(item, dict) and item.get("text")]
    return []


def status() -> dict | None:
    """A compact reading for pickup and the doctor, or ``None`` when Crosstalk never looked at Hermes."""
    record = link_record()
    if not record:
        return None
    diagnosis = record.get("diagnosis") or {}
    repair = record.get("repair") or {}
    contract = record.get("contract") or {}
    selftest = record.get("selftest") or {}
    return {"state": diagnosis.get("state"), "message": diagnosis.get("message"),
            "hermes_version": ((record.get("version") or {}).get("current") or {}).get("version"),
            "checked_version": ((record.get("version") or {}).get("checked") or {}).get("version"),
            "version_notice": version_notice(record), "problems": _problems(record),
            "contract_missing": [item.get("id") for item in contract.get("missing") or [] if isinstance(item, dict)],
            "selftest": {key: selftest.get(key) for key in ("ok", "utc", "inconclusive")} if selftest else None,
            "repair": {key: repair.get(key) for key in ("state", "mode", "prompt_path", "offer_no")},
            "updated_utc": record.get("updated_utc")}


def pickup_line() -> str | None:
    """One sentence for workflow pickup, only when the link needs attention (broken, or Hermes updated since the
    last check). A healthy or never-checked link adds nothing."""
    found = status()
    if not found:
        return None
    parts = []
    if found["problems"]:
        parts.append("the Hermes link is BROKEN: " + found["problems"][0]["text"])
        state = found["repair"]["state"]
        parts.append({"offered": "a repair is offered (a pending action; `hermes-repair` shows it)",
                      "started": "a repair chat was started", "starting": "a repair chat is starting"}.get(state, "")
                     or "")
    if found["version_notice"]:
        parts.append(found["version_notice"]["text"] + " (run `hermes-selftest`)")
    if found["contract_missing"] and not found["problems"]:
        parts.append("Hermes internals Crosstalk uses changed: " + ", ".join(found["contract_missing"][:4]))
    parts = [part for part in parts if part]
    return ("hermes link: " + "; ".join(parts)) if parts else None


def doctor_row() -> dict:
    """The doctor readback: attention is True when the link is broken or Hermes changed under it."""
    found = status()
    if not found:
        return {"present": False, "attention": False, "detail": "no Hermes link record (Crosstalk has not checked Hermes)"}
    line = pickup_line()
    return {"present": True, "attention": bool(line), "state": found["state"], "hermes_version": found["hermes_version"],
            "checked_version": found["checked_version"], "repair": found["repair"]["state"],
            "contract_missing": found["contract_missing"], "detail": line or "the Hermes link is fine"}


# ------------------------------------------------------------ the repair offer
def _offer_text(record: dict) -> tuple[str, str]:
    repair = record.get("repair") or {}
    cli = crosstalk_status.cli_path(*crosstalk_status.default_roots())
    command = f"python {cli}" if cli else "eutonos-crosstalk"
    reason = (_problems(record) or [{"text": "the link is broken"}])[0]["text"]
    ref = f"[ref {repair.get('key')}-{repair.get('offer_no')}]"
    prompt = repair.get("prompt_path") or "the repair prompt file"
    why = (f"Letters to Hermes chats are failing or at risk: {reason}")[:380]
    harnesses = [item.get("app") for item in repair.get("harnesses") or [] if isinstance(item, dict)]
    start = " or ".join(f"`{command} hermes-repair --start {app}`" for app in harnesses) or "(no repair-capable app installed)"
    action = (f"Fix the Crosstalk link to Hermes {ref}: start a repair chat with {start}, or copy {prompt} "
              "into a chat by hand")
    if len(action) > 400:
        action = (f"Fix the Crosstalk link to Hermes {ref}: run `{command} hermes-repair` for the choices, or copy "
                  f"{prompt} into a chat by hand")
    return action, why


def sync_pending_offer(home=None) -> dict:
    """Keep ONE pending action in step with the kit's repair offer. Never raises. Offered (or a failed start in auto
    mode) adds it once; the same offer is never added twice, even after the owner dismissed or completed it. A
    resolved link, a started repair chat or the setting being off dismisses a pending one. Returns what it did."""
    try:
        from . import pending_actions
        record = link_record()
        repair = record.get("repair") or {}
        state = repair.get("state")
        if state in OFFER_STATES and repair.get("mode") != "off":
            action, why = _offer_text(record)
            known = pending_actions.list_actions(home, state="all")
            ref = f"[ref {repair.get('key')}-{repair.get('offer_no')}]"
            if any(item["source"] == OFFER_SOURCE and ref in item["action"] for item in known):
                return {"did": "none", "state": state}
            entry = pending_actions.add(home, source=OFFER_SOURCE, action=action, why=why, harness="any")
            return {"did": "offered", "id": entry["id"], "state": state}
        if state in CLOSED_STATES:
            closed = pending_actions.dismiss_source(home, OFFER_SOURCE, note=f"the repair is {state}")
            return {"did": "dismissed" if closed else "none", "ids": closed, "state": state}
        return {"did": "none", "state": state}
    except Exception as error:  # an optional offer must never break pickup or the dashboard
        return {"did": "error", "error": type(error).__name__}


# ------------------------------------------------------ Harness policy watch
def _first_seen(record: dict) -> float:
    contract = record.get("contract") or {}
    for value in (contract.get("first_seen_utc"), contract.get("utc")):
        try:
            stamp = datetime.fromisoformat(str(value))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return float(int(stamp.timestamp()))
        except ValueError:
            continue
    return float(int(time.time()))


def _ack_path() -> Path:
    from . import model_catalog
    return model_catalog.watch_home() / WATCH_ACK_FILE


def watch_items() -> list:
    """The watch item for changed Hermes internals: ``[]`` when none are missing or the change was acknowledged.
    It is local and no-network: the kit compared the plugin's declared internals with the installed Hermes source."""
    record = link_record()
    contract = record.get("contract") or {}
    missing = [item for item in contract.get("missing") or [] if isinstance(item, dict) and item.get("id")]
    if not missing:
        return []
    fingerprint = str(contract.get("fingerprint") or "unknown")
    joined = fingerprint + chr(10) + chr(10).join(sorted(item["id"] for item in missing))
    digest = hashlib.sha256(joined.encode()).hexdigest()
    item_id = WATCH_ID_PREFIX + digest[:12]
    if item_id in _read_object(_ack_path()).get("ids", []):
        return []
    names = ", ".join(item["id"] for item in missing[:5]) + (" ..." if len(missing) > 5 else "")
    version = ((record.get("version") or {}).get("current") or {}).get("version")
    return [{"id": item_id, "source": WATCH_SOURCE, "kind": "changed",
             "key": f"Hermes internals Crosstalk uses changed ({names})" + (f"; Hermes {version}" if version else ""),
             "before": None, "after": None, "first_seen": _first_seen(record), "updated_at": _first_seen(record)}]


def watch_owns(item_id) -> bool:
    return isinstance(item_id, str) and item_id.startswith(WATCH_ID_PREFIX)


def watch_ack(ids) -> list:
    """Mark these Hermes-internals items reviewed (remembered in the watch folder; the kit's record is untouched,
    so a NEW change in the internals raises a new id). Returns the ids that were acknowledged."""
    wanted = {item for item in ids if watch_owns(item)}
    current = {item["id"] for item in watch_items()}
    done = sorted(wanted & current)
    if done:
        path = _ack_path()
        known = [item for item in _read_object(path).get("ids", []) if isinstance(item, str)]
        known = (known + [item for item in done if item not in known])[-64:]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        try:
            temporary.write_text(json.dumps({"ids": known}), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return done
