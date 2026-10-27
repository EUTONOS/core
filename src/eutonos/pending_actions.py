"""Steps a switch could not finish itself: ``<home>/settings/pending-actions.json``.

A toggle (``eutonos-install component ...`` or ``setting ...``) applies what it can mechanically. What it cannot do
(write or remove a memory in an app's own memory, which an agent has to do itself; ask the person to restart an
app; finish a change an app refused) is put here, so an agent can pick it up and mark it done. Workflow pickup reads this file.
Turn-start reminders require installed, enabled role hooks; those remain held on a fresh install:

    eutonos-install pending list [--json] [--harness H]
    eutonos-install pending done ID [--by WHO]
    eutonos-install pending dismiss ID

    {"profile": "eutonos.pending-actions.v1", "updated_at": ISO-UTC,
     "actions": [{"id", "created_at", "source", "harness", "action", "why", "state", "done_at", "done_by", "note"}]}

``source`` says who asked (``component:crosstalk``, ``setting:memory-block``); ``harness`` is ``claude``, ``codex``,
``hermes`` or ``any``; ``action`` is a short imperative an agent can carry out; ``state`` is ``pending``, ``done`` or
``dismissed``. The same ``source`` + ``action`` is never pending twice.

Rules (as ``component_state``): a missing file means nothing is pending; a file that cannot be read or has the wrong
shape raises ``PendingActionsError`` and is never read as "empty". Writes are atomic and run under the machine
lifecycle lock (reentrant, so a toggle can add its entries inside its own lock). ``summary`` is the cheap,
lock-free, byte-capped, never-raising read the role hooks and pickup use.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import home as eutonos_home
from . import setup_checkpoint as sc

PROFILE = "eutonos.pending-actions.v1"
HARNESSES = ("claude", "codex", "hermes", "any")
STATES = ("pending", "done", "dismissed")
HARNESS_NAMES = {"claude": "Claude", "codex": "Codex", "hermes": "Hermes"}
WRITE_WAIT_SECONDS = 30.0
MAX_ACTION_CHARS = 400
MAX_WHY_CHARS = 400
MAX_FIELD_CHARS = 120
MAX_FINISHED_KEPT = 100   # done and dismissed entries kept for the record; older ones drop off
MAX_FILE_BYTES = 512 * 1024


class PendingActionsError(ValueError):
    """The pending-actions file is unreadable or has an unsupported shape (never silently read as empty)."""


class UnknownAction(PendingActionsError):
    """No entry has that id."""


def _base(home=None) -> Path:
    return Path(home) if home is not None else eutonos_home.home()


def path(home=None) -> Path:
    return _base(home) / "settings" / "pending-actions.json"


def _clean(value, limit: int, name: str, *, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise PendingActionsError(f"a pending action needs {name}")
    return text[:limit]


def _check_entry(item, target) -> dict:
    ok = (isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
          and isinstance(item.get("source"), str) and isinstance(item.get("action"), str) and item["action"]
          and item.get("harness") in HARNESSES and item.get("state") in STATES)
    if not ok:
        raise PendingActionsError(f"the pending-actions file has a bad entry: {target}")
    return {"id": item["id"], "created_at": item.get("created_at"), "source": item["source"],
            "harness": item["harness"], "action": item["action"],
            "why": item["why"] if isinstance(item.get("why"), str) else "",
            "state": item["state"], "done_at": item.get("done_at"), "done_by": item.get("done_by"),
            "note": item["note"] if isinstance(item.get("note"), str) else None}


def _parse(raw, target) -> list:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise PendingActionsError(f"the pending-actions file is not valid JSON: {target}") from exc
    items = value.get("actions") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("profile") != PROFILE or not isinstance(items, list):
        raise PendingActionsError(f"the pending-actions file has an unsupported shape: {target}")
    return [_check_entry(item, target) for item in items]


def _read_all(home=None) -> list:
    target = path(home)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PendingActionsError(f"the pending-actions file is unreadable: {target}") from exc
    return _parse(raw, target)


def _write(home, entries: list, now=None) -> None:
    finished = [item for item in entries if item["state"] != "pending"]
    drop = {item["id"] for item in finished[:-MAX_FINISHED_KEPT]} if len(finished) > MAX_FINISHED_KEPT else set()
    kept = [item for item in entries if item["id"] not in drop]
    sc._write_atomic(path(home), {"profile": PROFILE, "updated_at": sc.now_iso(now), "actions": kept})


def _scope(entries: list, harness, state) -> list:
    found = [item for item in entries if state in (None, "all") or item["state"] == state]
    if harness:
        found = [item for item in found if item["harness"] in (harness, "any")]
    return found


def list_actions(home=None, *, harness: str | None = None, state: str | None = "pending") -> list:
    """The entries in `state` (``None`` or ``"all"`` for every state), oldest first. With `harness`, only the
    entries for that app plus the ones for ``any``. An unreadable file raises ``PendingActionsError``."""
    return _scope(_read_all(home), harness, state)


def add(home=None, *, source: str, action: str, why: str = "", harness: str = "any", now=None) -> dict:
    """Record one step for the next agent and return the entry. The same `source` + `action` already pending returns
    that entry instead of a second one."""
    source = _clean(source, MAX_FIELD_CHARS, "a source", required=True)
    action = _clean(action, MAX_ACTION_CHARS, "an action", required=True)
    if harness not in HARNESSES:
        raise PendingActionsError(f"harness must be one of {', '.join(HARNESSES)}, not {harness!r}")
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        entries = _read_all(home)
        for item in entries:
            if item["state"] == "pending" and item["source"] == source and item["action"] == action:
                return item
        stamp = sc.now_iso(now)
        taken = {item["id"] for item in entries}
        number = len(entries)
        while True:
            key = hashlib.sha256(f"{source}\0{action}\0{stamp}\0{number}".encode("utf-8")).hexdigest()[:8]
            if "pa-" + key not in taken:
                break
            number += 1
        entry = {"id": "pa-" + key, "created_at": stamp, "source": source, "harness": harness, "action": action,
                 "why": _clean(why, MAX_WHY_CHARS, "a reason"), "state": "pending", "done_at": None,
                 "done_by": None, "note": None}
        entries.append(entry)
        _write(home, entries, now)
        return entry


def _finish(home, action_id: str, state: str, *, by=None, note=None, now=None) -> dict:
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        entries = _read_all(home)
        for item in entries:
            if item["id"] == action_id:
                if item["state"] != state:
                    item.update(state=state, done_at=sc.now_iso(now), done_by=_clean(by, MAX_FIELD_CHARS, "") or None,
                                note=_clean(note, MAX_WHY_CHARS, "") or None)
                    _write(home, entries, now)
                return item
        raise UnknownAction(f"there is no pending action {action_id}")


def complete(home=None, action_id: str = "", *, by: str | None = None, now=None) -> dict:
    """Mark an entry done (by `by`, for example the chat or agent that carried it out). Idempotent."""
    return _finish(home, action_id, "done", by=by, now=now)


def dismiss(home=None, action_id: str = "", *, note: str | None = None, now=None) -> dict:
    """Mark an entry as not needed any more. Idempotent."""
    return _finish(home, action_id, "dismissed", note=note, now=now)


def dismiss_source(home=None, source: str = "", *, note: str | None = None, now=None) -> list:
    """Dismiss every pending entry from `source` (it described a state the source has since left). Returns their ids."""
    with sc.machine_lock(home, wait=WRITE_WAIT_SECONDS):
        entries = _read_all(home)
        changed = [item for item in entries if item["state"] == "pending" and item["source"] == source]
        for item in changed:
            item.update(state="dismissed", done_at=sc.now_iso(now), note=_clean(note, MAX_WHY_CHARS, "") or None)
        if changed:
            _write(home, entries, now)
        return [item["id"] for item in changed]


def request_memory_step(home=None, *, source: str, harness: str, verb: str, memory: str, instructions: str,
                        why: str = "", now=None) -> dict:
    """The one way a switch asks an agent to add or remove a memory in a harness's own memory (Claude Code
    auto-memory files, Codex memories, Hermes memory), which only that agent can write. `verb` is ``add`` or
    ``remove``; `memory` names the memory; `instructions` is the caller's own wording of what to write or remove (the
    wording is owned by the caller, never invented here). Returns the entry."""
    if harness not in HARNESS_NAMES:
        raise PendingActionsError(f"a memory step needs a harness of {', '.join(HARNESS_NAMES)}, not {harness!r}")
    if verb not in ("add", "remove"):
        raise PendingActionsError("a memory step is to add or to remove")
    where = f"{HARNESS_NAMES[harness]}'s own memory"
    return add(home, source=source, harness=harness, now=now, why=why,
               action=f"{verb.capitalize()} the {_clean(memory, MAX_FIELD_CHARS, 'a memory name', required=True)} "
                      f"memory in {where}. {_clean(instructions, MAX_ACTION_CHARS, 'instructions', required=True)}")


# -------------------------------------------------------------------- cheap reads
def summary(home=None, *, harness: str | None = None, limit: int = 2, line_chars: int = 110) -> dict:
    """What hooks and pickup show: ``{"count", "items": [{"id", "harness", "action"}], "signature", "error"}`` for
    the pending entries of `harness` (and ``any``). Lock-free, byte-capped, and it never raises: a file that cannot be
    read or understood answers ``count`` 0 with ``error`` set and ``signature`` ``"unreadable"``."""
    try:
        target = path(home)
        try:
            with target.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {"count": 0, "items": [], "signature": "", "error": None}
        if len(raw) > MAX_FILE_BYTES:
            raise PendingActionsError("the pending-actions file is too large")
        found = _scope(_parse(raw.decode("utf-8"), target), harness, "pending")
    except Exception as exc:  # a reminder must never raise into its host
        return {"count": 0, "items": [], "signature": "unreadable", "error": f"{type(exc).__name__}: {exc}"[:200]}
    items = []
    for item in found[:max(0, limit)]:
        action = item["action"]
        items.append({"id": item["id"], "harness": item["harness"],
                      "action": action if len(action) <= line_chars else action[:line_chars - 3].rstrip() + "..."})
    signature = hashlib.sha256("\n".join(item["id"] for item in found).encode("utf-8")).hexdigest()[:12] if found else ""
    return {"count": len(found), "items": items, "signature": signature, "error": None}


def pickup_hint(home=None, *, limit: int = 3) -> dict | None:
    """Workflow pickup's ``pending_actions`` key, or ``None`` when nothing is pending and the file is fine."""
    found = summary(home, limit=limit, line_chars=140)
    if found["error"]:
        return {"count": 0, "items": [], "error": found["error"],
                "line": "pending actions: the file could not be read. Run: eutonos-install pending list"}
    if not found["count"]:
        return None
    first = "; ".join(f"{item['id']} [{item['harness']}] {item['action']}" for item in found["items"])
    more = f" (+{found['count'] - len(found['items'])} more)" if found["count"] > len(found["items"]) else ""
    return {"count": found["count"], "items": found["items"],
            "line": f"pending actions: {found['count']} for the next agent. {first}{more}. "
                    "Carry them out, then: eutonos-install pending done ID"}


def delivery_status() -> dict:
    """A queued action is durable, not proof of native turn delivery (hooks remain held)."""
    return {"state": "manual-pickup", "automatic_turn_delivery": "not-guaranteed",
            "reason": "Turn-start reminders require installed and enabled role hooks; fresh installs keep them held.",
            "next_action": "In any chat, including one without a project, run eutonos-install pending list --harness HARNESS; "
                           "carry out the applicable actions, then eutonos-install pending done ID."}
