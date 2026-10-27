"""Opt-in, header-only selection of native sessions owned by one project."""
from copy import deepcopy
import json
import os
from pathlib import Path
import threading


_CACHE = {}
_GUARD = threading.Lock()


def _path(value):
    return os.path.normcase(str(Path(value).resolve()))


def _inventory(root):
    directory = Path(root)
    if not directory.is_dir():
        return []
    paths = sorted(path for path in directory.rglob("*.jsonl") if path.is_file())
    return [(path, path.stat().st_size, path.stat().st_mtime_ns) for path in paths]


def _header(path):
    try:
        with path.open("rb") as stream:
            raw = stream.readline()
        event = json.loads(raw)
        if not isinstance(event, dict):
            return None
        payload = event.get("payload")
        if event.get("type") != "session_meta" or not isinstance(payload, dict):
            return None
        thread, cwd = payload.get("id"), payload.get("cwd")
        if not isinstance(thread, str) or not thread or not isinstance(cwd, str) or not cwd:
            return None
        source = payload.get("source")
        parent = payload.get("parent_thread_id")
        spawned = None
        if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
            spawned = source["subagent"].get("thread_spawn")
        child = (isinstance(parent, str) and bool(parent) and isinstance(spawned, dict)
                 and spawned.get("parent_thread_id") == parent and isinstance(spawned.get("agent_path"), str))
        other = isinstance(source, dict) and "subagent" in source and not child
        return {"path": str(path), "thread_id": thread, "cwd": cwd, "parent_thread_id": parent,
                "child": child, "other": other}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def headers(root):
    """Return cached first-line session metadata for one explicitly configured root."""
    key = _path(root)
    inventory = _inventory(root)
    with _GUARD:
        previous = _CACHE.get(key, {})
        current, records = {}, []
        for path, size, modified in inventory:
            name, signature = str(path), (size, modified)
            cached = previous.get(name)
            item = cached[1] if cached is not None and cached[0] == signature else _header(path)
            current[name] = (signature, item)
            if item is not None:
                records.append(item)
        _CACHE[key] = current
    return deepcopy(records)



def sources(native_sessions_root, project_root):
    """Select project roots and their verified spawned descendants without log scans."""
    project = _path(project_root)
    records = headers(native_sessions_root)
    counts = {}
    for item in records:
        counts[item["thread_id"]] = counts.get(item["thread_id"], 0) + 1
    ambiguous = {thread_id for thread_id, count in counts.items() if count > 1}
    excluded = set(ambiguous)
    changed = True
    while changed:
        changed = False
        for item in records:
            if item["child"] and item["parent_thread_id"] in excluded and item["thread_id"] not in excluded:
                excluded.add(item["thread_id"])
                changed = True
    eligible = [item for item in records if item["thread_id"] not in excluded]
    roots = {item["thread_id"] for item in eligible
             if not item["other"] and not item["child"] and _path(item["cwd"]) == project}
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for item in eligible:
            if item["child"] and item["parent_thread_id"] in selected and item["thread_id"] not in selected:
                selected.add(item["thread_id"])
                changed = True
    discovered, unsupported = [], 0
    for item in eligible:
        if item["thread_id"] in selected:
            discovered.append({"path": item["path"], "role": "support" if item["child"] else "primary",
                               "experiment_id": "native-session:" + item["thread_id"]})
        elif item["other"] and (item["parent_thread_id"] in selected or _path(item["cwd"]) == project):
            unsupported += 1
    return {"sources": discovered, "header_count": len(records), "unsupported_subagent_count": unsupported,
            "ambiguous_thread_id_count": len(ambiguous),
            "excluded_thread_ids": sorted(excluded),
            "scope": "Initial session cwd equals this repository, plus verified spawned descendants; no target working-directory filter and no global log discovery."}


def cache_clear():
    with _GUARD:
        _CACHE.clear()


__all__ = ["cache_clear", "headers", "sources"]
