"""The setup checkpoint and the machine lifecycle lock (IPC5 lane A).

The checkpoint is the person and agent half of a lifecycle run (the mechanical half is the install record). It lives
at ``<EUTONOS home>/install/checkpoint.json`` and holds one checklist with its steps:

    {"profile": "eutonos.setup-checkpoint.v1", "checklist": "install"|"uninstall"|"add-repository"|"remove-repository"|"repair",
     "checklist_revision": str, "gen_id": str|null, "root": str|null, "updated_at": ISO-UTC,
     "steps": [{"id", "kind": "mechanical"|"person"|"agent", "runner": "owner"|"agent",
                "state": "pending"|"in_progress"|"done"|"blocked"|"skipped",
                "evidence": [str], "reason": str|null, "next_action": str|null}]}

Writes are atomic and run under the machine lifecycle lock. A ``skipped`` step needs a ``reason``; a step of kind
``person`` or ``agent`` is ``done`` only with at least one evidence string. The doctor only reads this file.

The machine lock serialises install, update, rollback and uninstall (one at a time per machine). It is built on
``local_lock`` under ``<home>/install/``. A lifecycle operation takes it with ``wait=0`` and refuses when it is
busy; checkpoint writes wait for it briefly. Repository operations hold the projects lock, not this one.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path

from . import home as eutonos_home
from .local_lock import local_lock

PROFILE = "eutonos.setup-checkpoint.v1"
CHECKLISTS = ("install", "uninstall", "add-repository", "remove-repository", "repair")
KINDS = ("mechanical", "person", "agent")
RUNNERS = ("owner", "agent")
STATES = ("pending", "in_progress", "done", "blocked", "skipped")
CHECKPOINT_WAIT_SECONDS = 30.0


class CheckpointError(ValueError):
    """A checkpoint request that breaks its rules (state, evidence, reason or unknown step)."""


class LifecycleBusy(RuntimeError):
    """Another install, update, rollback or uninstall holds the machine lock."""


def _base(home=None) -> Path:
    return Path(home) if home is not None else eutonos_home.home()


def install_dir(home=None) -> Path:
    return _base(home) / "install"


def checkpoint_path(home=None) -> Path:
    return install_dir(home) / "checkpoint.json"


@contextmanager
def machine_lock(home=None, *, wait: float | None = 0):
    """Hold the machine lifecycle lock. ``wait=0`` refuses at once (``LifecycleBusy``); a number waits that long;
    ``None`` waits without limit. Reentrant in one thread, so an operation can write its checkpoint inside the lock."""
    try:
        with local_lock(install_dir(home), timeout=wait):
            yield
    except TimeoutError as exc:
        raise LifecycleBusy("another EUTONOS install, update, rollback or uninstall is running") from exc


def now_iso(now=None) -> str:
    value = now or datetime.datetime.now(datetime.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        temporary.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(home=None) -> dict | None:
    """The checkpoint, or ``None`` when absent. A damaged file is an error, never silently replaced."""
    path = checkpoint_path(home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"the setup checkpoint is unreadable: {path}") from exc
    if not isinstance(value, dict) or value.get("profile") != PROFILE or not isinstance(value.get("steps"), list):
        raise CheckpointError(f"the setup checkpoint has an unsupported shape: {path}")
    return value


def _normalize_step(step: dict) -> dict:
    if not isinstance(step, dict) or not isinstance(step.get("id"), str) or not step["id"].strip():
        raise CheckpointError("every step needs an id")
    kind = step.get("kind", "mechanical")
    runner = step.get("runner", "agent")
    state = step.get("state", "pending")
    if kind not in KINDS or runner not in RUNNERS or state not in STATES:
        raise CheckpointError(f"step {step['id']}: kind, runner or state is not allowed")
    evidence = step.get("evidence") or []
    if not isinstance(evidence, list) or not all(isinstance(item, str) and item for item in evidence):
        raise CheckpointError(f"step {step['id']}: evidence must be a list of strings")
    result = {"id": step["id"], "kind": kind, "runner": runner, "state": state, "evidence": list(evidence),
              "reason": step.get("reason"), "next_action": step.get("next_action")}
    _check_rules(result)
    return result


def _check_rules(step: dict) -> None:
    if step["state"] == "skipped" and not (isinstance(step["reason"], str) and step["reason"].strip()):
        raise CheckpointError(f"step {step['id']}: a skipped step needs a reason")
    if step["state"] == "done" and step["kind"] in ("person", "agent") and not step["evidence"]:
        raise CheckpointError(f"step {step['id']}: a done {step['kind']} step needs at least one evidence string")


def start(checklist: str, steps: list[dict], *, checklist_revision: str, gen_id: str | None = None,
          root: str | None = None, home=None, now=None) -> dict:
    """Begin a checklist, replacing any earlier checkpoint. ``steps`` are dicts with at least ``id``; mechanical steps
    that already ran pass ``state: "done"`` and their receipt paths as ``evidence``."""
    if checklist not in CHECKLISTS:
        raise CheckpointError(f"unknown checklist: {checklist}")
    normalized = [_normalize_step(step) for step in steps]
    if len({step["id"] for step in normalized}) != len(normalized):
        raise CheckpointError("step ids must be unique")
    value = {"profile": PROFILE, "checklist": checklist, "checklist_revision": str(checklist_revision),
             "gen_id": gen_id, "root": None if root is None else str(root), "updated_at": now_iso(now),
             "steps": normalized}
    with machine_lock(home, wait=CHECKPOINT_WAIT_SECONDS):
        _write_atomic(checkpoint_path(home), value)
    return value


def record_step(step_id: str, state: str, evidence: list[str] | None = None, reason: str | None = None,
                next_action: str | None = None, *, home=None, now=None) -> dict:
    """Set one step's state. ``evidence`` is added to what the step already holds."""
    with machine_lock(home, wait=CHECKPOINT_WAIT_SECONDS):
        value = read(home)
        if value is None:
            raise CheckpointError("there is no setup checkpoint to record into")
        for step in value["steps"]:
            if step["id"] == step_id:
                break
        else:
            raise CheckpointError(f"unknown step: {step_id}")
        if state not in STATES:
            raise CheckpointError(f"state must be one of {', '.join(STATES)}")
        added = [item for item in (evidence or []) if isinstance(item, str) and item]
        if len(added) != len(evidence or []):
            raise CheckpointError("evidence must be a list of strings")
        updated = dict(step, state=state, evidence=step["evidence"] + [item for item in added if item not in step["evidence"]],
                       reason=reason if reason is not None else step.get("reason"),
                       next_action=next_action if next_action is not None else (None if state == "done" else step.get("next_action")))
        if state == "done":
            updated["next_action"] = None
        _check_rules(updated)
        step.update(updated)
        value["updated_at"] = now_iso(now)
        _write_atomic(checkpoint_path(home), value)
        return value


def summary(value: dict | None) -> dict:
    """Counts by state for a checkpoint (or ``None``)."""
    counts = {state: 0 for state in STATES}
    for step in (value or {}).get("steps", []):
        counts[step["state"]] = counts.get(step["state"], 0) + 1
    return counts


def next_open_step(value: dict | None) -> dict | None:
    """The first step that is not done or skipped, or ``None``."""
    for step in (value or {}).get("steps", []):
        if step["state"] not in ("done", "skipped"):
            return step
    return None
