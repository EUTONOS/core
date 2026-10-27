"""Read-only diagnosis of supplied workflow execution outcome metadata."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from .private_scope import require_shared
from .workflow import PROFILE as WORKFLOW_PROFILE

PROFILE = "eutonos.workflow-diagnostics.v1"
EVENT_PROFILE = "eutonos.workflow-diagnostic-event.v1"


def _metadata(value):
    if isinstance(value, dict) and value.get("profile") == EVENT_PROFILE:
        required = {"outcome", "command", "phase", "code", "input_fingerprint"}
        if not required <= set(value):
            raise ValueError("Workflow diagnostic event is incomplete")
        return {key: value[key] for key in required}
    if not isinstance(value, dict) or value.get("profile") not in {WORKFLOW_PROFILE, "eutonos.tokenslang.organized-closeout.v1"}:
        raise ValueError("Diagnostics accepts only EUTONOS workflow results")
    receipt = value.get("capture_receipt") if isinstance(value.get("capture_receipt"), dict) else {}
    native = value.get("result") if isinstance(value.get("result"), dict) else {}
    operation = value.get("operation") if isinstance(value.get("operation"), dict) else {}
    if receipt.get("outcome") == "error":
        phase, code = "capture-receipt", receipt.get("code", "unavailable")
    elif native.get("outcome") == "error":
        phase, code = "ram-capture", native.get("code", "unavailable")
    elif value.get("outcome") in {"error", "incomplete", "partial", "skipped", "deferred"}:
        phase, code = value.get("command", "workflow"), value.get("code", value.get("reason", value["outcome"]))
    else:
        phase, code = "complete", value.get("outcome")
    selector = (value.get("retry") or {}).get("selector") if isinstance(value.get("retry"), dict) else None
    selector = selector or operation.get("attempt")
    fingerprint = hashlib.sha256(json.dumps({"command": value.get("command"), "selector": selector,
                                             "values": value.get("values"),
                                             "missing": [item.get("name") for item in value.get("missing_fields", [])
                                                         if isinstance(item, dict)]},
                                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"outcome": value.get("outcome"), "command": value.get("command"), "phase": phase,
            "code": code, "input_fingerprint": fingerprint}


def record(root, value, recorded_at=None):
    require_shared(root)
    metadata = _metadata(value)
    at = recorded_at or datetime.now(timezone.utc).isoformat()
    event = {"profile": EVENT_PROFILE, "recorded_at": at, **metadata}
    directory = Path(root).resolve() / ".eutonos-local" / "workflow" / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = directory / (name + ".json")
    if path.exists():
        return {"outcome": "idempotent", "path": str(path), "event": event}
    temporary = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".workflow-diagnostic-", suffix=".tmp", dir=directory)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(event, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"outcome": "recorded", "path": str(path), "event": event}


def read_events(root, limit=64):
    require_shared(root)
    if type(limit) is not int or limit < 1:
        raise ValueError("Diagnostic event limit must be positive")
    directory = Path(root).resolve() / ".eutonos-local" / "workflow" / "diagnostics"
    if not directory.exists():
        return []
    events = []
    for path in directory.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            _metadata(value)
            events.append(value)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(events, key=lambda item: item["recorded_at"])[-limit:]


def diagnose(value, limit=64):
    results = value.get("results") if isinstance(value, dict) else value
    if not isinstance(results, list) or not results:
        raise ValueError("Supply workflow results in chronological order")
    if type(limit) is not int or limit < 1:
        raise ValueError("Diagnostic window limit must be positive")
    total = len(results)
    items = [_metadata(item) for item in results[-limit:]]
    last = items[-1]
    repeated = 1
    for prior in reversed(items[:-1]):
        if (prior["phase"], prior["code"], prior["input_fingerprint"]) != \
                (last["phase"], last["code"], last["input_fingerprint"]):
            break
        repeated += 1
    code = last["code"]
    if last["phase"] == "complete":
        action = "No workflow recovery is needed."
    elif last["phase"] == "capture-receipt":
        action = "Reuse the same prepared closeout while the native turn remains active."
    elif code in {"denied", "wrong-scope"}:
        action = "Inspect the configured writer, project host identity and exact operation paths; no write grant or renewal is required."
    elif code in {"stale", "conflict"}:
        action = "Inspect the named attempt or prepare again from current source; preserve conflicting bytes."
    elif code in {"not-configured", "invalid-settings"}:
        action = "Use the supported settings or setup route, then retry."
    elif last["outcome"] == "incomplete":
        action = "Fill all reported missing or invalid fields and resubmit once."
    elif last["outcome"] == "skipped":
        action = "Inspect why the operation was skipped; current project closeout does not skip RAM for capture-off settings."
    else:
        action = "Inspect the last workflow result and use its stated next action."
    return {"profile": PROFILE, "outcome": "diagnostic", "observed_results": len(items),
            "coverage": {"available_results": total, "window_limit": limit, "windowed": total > len(items)},
            "last_failed_phase": None if last["phase"] == "complete" else last["phase"],
            "last_code": code, "repeated_identical": repeated,
            "unchanged_input": repeated > 1, "input_fingerprint": last["input_fingerprint"],
            "next_supported_action": action}


__all__ = ["EVENT_PROFILE", "PROFILE", "diagnose", "read_events", "record"]
