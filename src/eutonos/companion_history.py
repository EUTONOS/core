"""Durable metadata-only companion history and source-stat cache."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
import threading

from .local_lock import local_lock

PROFILE = "eutonos.companion-history.v1"
STATE_NAME = "companion-history.json"
TRACKING_PROFILE = "eutonos.companion-tracking.v1"
TRACKING_NAME = "companion-tracking.json"
_CACHE = {}
_CACHE_GUARD = threading.Lock()
_ROW_FIELDS = {"id", "thread_id", "turn_id", "role", "model", "models", "efforts", "experiment_id", "stage", "finding",
               "followup_reads", "source_prefix_sha256", "final_sha256", "usage", "gaps", "coverage", "state",
               "start", "end", "elapsed_seconds", "counter_updates", "tool_calls", "child_inclusion", "handoff_basis",
               "first_input", "max_input", "handoff_tokens", "source_bytes", "quality", "tool_counts", "tool_sequence", "counter_basis",
               "compaction_count", "compaction_usage", "compaction_basis", "project_origin", "request_id"}
_USAGE_FIELDS = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_path(root):
    return Path(root).resolve() / ".eutonos-local" / STATE_NAME


def _tracking_path(path):
    """The small switch file that sits beside a history state file."""
    return path.with_name(TRACKING_NAME)


def _valid_tracking(value):
    return (isinstance(value, dict) and type(value.get("enabled")) is bool
            and isinstance(value.get("windows"), list))


def _read_state(path):
    """Full read (writer/non-hot paths). The small switch file wins over any legacy block."""
    if not path.exists():
        state = {"profile": PROFILE, "tracking": {"enabled": True, "windows": []}, "rows": {}}
    else:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state.get("profile") != PROFILE or not _valid_tracking(state.get("tracking"))
                or not isinstance(state.get("rows"), dict)):
            raise ValueError("Invalid companion history state")
    small = _tracking_path(path)
    if small.exists():
        value = json.loads(small.read_text(encoding="utf-8"))
        if value.get("profile") != TRACKING_PROFILE or not _valid_tracking(value.get("tracking")):
            raise ValueError("Invalid companion tracking state")
        state["tracking"] = value["tracking"]
    return state


def _migrate_tracking(path, state):
    """Writer-path one-shot: copy a legacy tracking block into the small file (caller holds the lock)."""
    if path.exists() and not _tracking_path(path).exists():
        _write_tracking(path, state["tracking"])


def _write_tracking(path, tracking_value):
    _write_state(_tracking_path(path), {"profile": TRACKING_PROFILE, "tracking": tracking_value}, prefix="companion-tracking-")


def _write_state(path, state, prefix="companion-history-"):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _absolute(value):
    if not isinstance(value, str):
        return False
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or bool(PureWindowsPath(value).drive)


def _instant(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _safe_value(value, key=None):
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            safe = _safe_value(item, name)
            if safe is not None:
                result[name] = safe
        return result
    if isinstance(value, list):
        return [safe for item in value if (safe := _safe_value(item, key)) is not None]
    if isinstance(value, str) and key in {"path", "log", "log_path", "source", "source_path"} and _absolute(value):
        return None
    return value


def _metadata(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
        raise ValueError("History row requires a native nonempty id")
    result = _safe_value({key: deepcopy(row[key]) for key in _ROW_FIELDS if key in row})
    result["id"] = row["id"]
    if isinstance(result.get("usage"), dict):
        result["usage"] = {key: result["usage"].get(key) for key in _USAGE_FIELDS if key in result["usage"]}
    return result


def _off_at(windows, started):
    instant = _instant(started)
    if instant is None:
        return False
    for window in windows:
        begin = _instant(window.get("started_at"))
        end = _instant(window.get("ended_at")) if window.get("ended_at") is not None else None
        if begin is not None and instant >= begin and (end is None or instant < end):
            return True
    return False


def set_tracking(root, enabled):
    """Toggle metadata tracking, retaining rows and recording explicit time windows."""
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    path = _state_path(root)
    with local_lock(root):
        state = _read_state(path)
        _migrate_tracking(path, state)
        current = state["tracking"]["enabled"]
        if current != enabled:
            stamp = _now()
            if enabled:
                for window in state["tracking"]["windows"]:
                    if window.get("ended_at") is None:
                        window["ended_at"] = stamp
            else:
                state["tracking"]["windows"].append({"started_at": stamp, "ended_at": None})
            state["tracking"]["enabled"] = enabled
            _write_tracking(path, state["tracking"])
            _write_state(path, state)
        return deepcopy(state["tracking"])


def tracking(root):
    """Read tracking state without creating or changing local metadata."""
    path = _state_path(root)
    with local_lock(root, create=False):
        return deepcopy(_read_state(path)["tracking"])


def tracking_enabled_bounded(root, *, max_bytes=256 * 1024):
    """Read the canonical tracking switch without waiting or retaining new data.

    The switch lives in its own small file so history size never hides it.
    A legacy layout (no small file yet) falls back to a bounded read of the
    history file, which is migrated into the small file by the next writer
    path (set_tracking, sync); the hot path never reads the large file in full.
    Missing state uses the established enabled default. Contention, malformed
    or oversized legacy history returns unknown; callers must not capture then.
    This small hot-path read never invokes status, watch, or history sync.
    """
    try:
        path = _state_path(root)
        with local_lock(root, create=False, timeout=0):
            small = _tracking_path(path)
            legacy = not small.exists()
            if legacy and not path.exists():
                return True
            with (path if legacy else small).open("rb") as stream:
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or value.get("profile") != (PROFILE if legacy else TRACKING_PROFILE):
                return None
            tracking_value = value.get("tracking")
            if not isinstance(tracking_value, dict) or type(tracking_value.get("enabled")) is not bool:
                return None
            return tracking_value["enabled"]
    except Exception:
        return None


def sync(root, rows):
    """Persist whitelisted rows unless tracking is off or their task began during an off window."""
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    path = _state_path(root)
    with local_lock(root):
        state = _read_state(path)
        _migrate_tracking(path, state)
        accepted = changed = 0
        if state["tracking"]["enabled"]:
            for source in rows:
                item = _metadata(source)
                if _off_at(state["tracking"]["windows"], item.get("start")):
                    continue
                accepted += 1
                if state["rows"].get(item["id"]) != item:
                    state["rows"][item["id"]] = item
                    changed += 1
            if changed:
                _write_state(path, state)
        return {"profile": PROFILE, "tracking": deepcopy(state["tracking"]),
                "rows": [deepcopy(state["rows"][key]) for key in sorted(state["rows"])],
                "accepted": accepted, "changed": changed}


def cached_read(reader, path, role, experiment_id, after=None, before=None):
    """Cache one selected metadata read by source stat and selection window."""
    source = Path(path)
    stat = source.stat()
    selection = (str(source.resolve()), role, experiment_id, after, before)
    signature = (stat.st_size, stat.st_mtime_ns)
    with _CACHE_GUARD:
        cached = _CACHE.get(selection)
        if cached is None or cached[0] != signature:
            _CACHE[selection] = (signature, deepcopy(reader(path, role, experiment_id, after, before)))
        return deepcopy(_CACHE[selection][1])


def cache_clear():
    with _CACHE_GUARD:
        _CACHE.clear()


__all__ = ["PROFILE", "cached_read", "cache_clear", "set_tracking", "sync", "tracking"]
