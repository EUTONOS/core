"""Local, work-scoped semantic deltas deferred by RAM capture policy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import tokenslang as ts
from .local_lock import local_lock


PROFILE = "eutonos.local-capture-pending.v1"
WORK_SIZES = ("quick", "slice", "campaign", "task")
EVENTS = ("turn", "checkpoint", "milestone")


def evaluate(capture, cadence, work_size="task", event="turn"):
    ts.require(type(capture) is bool, "invalid", "RAM capture setting must be boolean.")
    ts.require(cadence in ("every-turn", "work-sized", "checkpoint", "milestone"),
               "invalid", "Unknown RAM capture cadence.")
    ts.require(work_size in WORK_SIZES and event in EVENTS, "invalid", "Unknown closeout work size or event.")
    eligible = (capture and (cadence == "every-turn"
                or cadence == "work-sized" and (event != "turn" or work_size in {"slice", "task"})
                or cadence == "checkpoint" and event in {"checkpoint", "milestone"}
                or cadence == "milestone" and event == "milestone"))
    reason = ("capture-off" if not capture else "eligible" if eligible else
              "awaiting-work-boundary" if cadence == "work-sized" else
              "awaiting-checkpoint" if cadence == "checkpoint" else "awaiting-milestone")
    return {"capture": capture, "cadence": cadence, "work_size": work_size, "event": event,
            "eligible": eligible, "reason": reason}


def _scope(actor, work_id, session_id):
    for value in (actor, work_id, session_id):
        ts.string(value)
    identity = {"actor": actor, "work_id": work_id, "session_id": session_id}
    key = hashlib.sha256(ts.encoded(identity)).hexdigest()
    return identity, key


def _path(root, key):
    return Path(root).resolve() / ".eutonos-local" / "workflow" / "pending" / (key + ".json")


def _read(path, identity):
    if not path.is_file():
        return {"profile": PROFILE, **identity, "next_order": 1, "entries": {}, "consumed": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ts.ReadError("invalid", "Pending capture continuity is unreadable.") from exc
    ts.shape(value, {"profile", "actor", "work_id", "session_id", "next_order", "entries", "consumed"})
    ts.require(value["profile"] == PROFILE and all(value[key] == identity[key] for key in identity)
               and type(value["next_order"]) is int and value["next_order"] >= 1
               and isinstance(value["entries"], dict) and isinstance(value["consumed"], dict),
               "wrong-scope", "Pending capture continuity has another scope.")
    orders = []
    for entry_id, item in value["entries"].items():
        ts.shape(item, {"order", "content"})
        ts.require(type(item["order"]) is int and item["order"] >= 1, "invalid", "Pending capture order is invalid.")
        orders.append(item["order"])
        ts.require(isinstance(entry_id, str) and entry_id == hashlib.sha256(ts.encoded(item["content"])).hexdigest(),
                   "invalid", "Pending capture entry identity is invalid.")
    ts.require(len(orders) == len(set(orders)) and all(isinstance(key, str) and isinstance(marker, str)
               for key, marker in value["consumed"].items()), "invalid", "Pending capture history is invalid.")
    return value


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def snapshot(root, actor, work_id, session_id):
    identity, key = _scope(actor, work_id, session_id)
    with local_lock(root, create=False):
        value = _read(_path(root, key), identity)
    ordered = sorted(value["entries"].items(), key=lambda item: item[1]["order"])
    return [{"id": entry_id, "content": item["content"]} for entry_id, item in ordered]


def defer(root, actor, work_id, session_id, entry):
    ts.require(isinstance(entry, dict), "invalid", "Pending semantic delta must be an object.")
    identity, key = _scope(actor, work_id, session_id)
    path = _path(root, key)
    entry_id = hashlib.sha256(ts.encoded(entry)).hexdigest()
    with local_lock(root):
        value = _read(path, identity)
        existed = entry_id in value["entries"]
        consumed = entry_id in value["consumed"]
        if not existed and not consumed:
            value["entries"][entry_id] = {"order": value["next_order"], "content": entry}
            value["next_order"] += 1
            _atomic(path, value)
    outcome = "already-consumed" if consumed else "already-pending" if existed else "deferred"
    return {"profile": PROFILE, "outcome": outcome,
            "entry_id": entry_id, "pending_count": len(value["entries"]), "path": str(path)}


def clear(root, actor, work_id, session_id, frozen, marker):
    ts.string(marker)
    identity, key = _scope(actor, work_id, session_id)
    path = _path(root, key)
    with local_lock(root):
        value = _read(path, identity)
        removed = 0
        for item in frozen:
            ts.shape(item, {"id", "content"})
            stored = value["entries"].get(item["id"])
            if stored is not None and stored["content"] == item["content"]:
                del value["entries"][item["id"]]
                value["consumed"][item["id"]] = marker
                removed += 1
        _atomic(path, value)
    return {"removed": removed, "remaining": len(value["entries"])}


__all__ = ["EVENTS", "PROFILE", "WORK_SIZES", "clear", "defer", "evaluate", "snapshot"]
