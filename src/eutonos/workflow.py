"""Small task catalog and semantic closeout form over existing EUTONOS APIs."""
from __future__ import annotations

import sqlite3

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window

from .private_scope import require_shared
from . import capture_policy, crosstalk_status, pending_actions, ram, settings, tokenslang as ts
from .command_registry import COMMANDS, CLOSEOUT_FIELDS, EXPERT_ROUTES, GUIDED_COMMANDS, LEGACY_FIELDS, PROFILE
from .command_registry import capabilities, catalog, describe, explain, fingerprint, help_topics
from .effects import LocalRoot, digest
from .source import GitView, safe_path

SOURCE_PIN_POLICY = "committed-context-with-explicit-work-baseline.v1"

def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _child_refs(value):
    """Validate RAM's canonical pinned references without inventing child prose."""
    ts.require(isinstance(value, list), "invalid", "children must be native RAM references.")
    seen = set()
    for child in value:
        ts.shape(child, {"identity", "path", "revision"})
        ts.shape(child["identity"], {"work_id", "session_id", "issuer_id", "memory_id"})
        for part in child["identity"].values():
            ram.slug(part)
        safe_path(child["path"])
        ts.require(type(child["revision"]) is int and child["revision"] >= 1,
                   "invalid", "Child RAM revision must be a positive integer.")
        key = ts.encoded(child["identity"])
        ts.require(key not in seen, "invalid", "Select each child RAM identity once.")
        seen.add(key)
    return deepcopy(value)


def _valid_child_refs(value):
    try:
        _child_refs(value)
        return True
    except (TypeError, ValueError, ts.ReadError):
        return False


def _verify_child_refs(store, children):
    """Require exact current eligible records in this repository's RAM store."""
    for child in _child_refs(children):
        ts.require(store.path(child["identity"]) == child["path"], "wrong-scope",
                   "Child identity does not bind to this repository's RAM path.")
        result = store.read({"profile": ram.READ, "operation": "get", "identity": child["identity"],
                             "revision": child["revision"], "max_bytes": 32768, "limit": 1})
        items = result.get("items", [])
        ts.require(result.get("outcome") == "ok" and result.get("complete") is True and len(items) == 1,
                   "missing", "Child RAM reference has no complete native readback.")
        item = items[0]
        ts.require(item.get("path") == child["path"] and item.get("identity") == child["identity"]
                   and item.get("record_revision") == child["revision"] and item.get("historical") is False,
                   "stale", "Child RAM reference is not its exact current revision.")


def validate_closeout(values):
    supplied = deepcopy(values) if isinstance(values, dict) else {}
    if supplied.get("work_size") == "task":
        supplied["work_size"] = "slice"
    legacy = "what" in supplied and not any(name in supplied for name in ("request", "actions", "result"))
    fields = LEGACY_FIELDS if legacy else CLOSEOUT_FIELDS
    semantic_names = {"work_size", "event", "consolidated_pending_ids", "selected_pending_ids"}
    known_names = {field["name"] for field in fields} | semantic_names
    preserved = {key: deepcopy(value) for key, value in supplied.items() if key in known_names}
    missing, invalid = [], []
    for field in fields:
        name = field["name"]
        if field["required"] and name not in supplied:
            missing.append(field)
            continue
        if name not in supplied:
            continue
        value = supplied[name]
        valid = ((name == "children" and _valid_child_refs(value)) or
                 (field["type"] == "string" and isinstance(value, str) and value.strip()) or
                 (field["type"] == "array[string]" and _strings(value)))
        if valid and name == "changed_paths":
            try:
                for path in value:
                    safe_path(path.replace("\\", "/"))
            except ts.ReadError:
                valid = False
        if not valid:
            invalid.append({**field, "value": deepcopy(value)})
    if supplied.get("work_size", "slice") not in capture_policy.WORK_SIZES:
        invalid.append({"name": "work_size", "type": "enum", "required": False,
                        "options": list(capture_policy.WORK_SIZES), "value": supplied.get("work_size")})
    if supplied.get("event", "turn") not in capture_policy.EVENTS:
        invalid.append({"name": "event", "type": "enum", "required": False,
                        "options": list(capture_policy.EVENTS), "value": supplied.get("event")})
    for name in ("consolidated_pending_ids", "selected_pending_ids"):
        if name in supplied and (not isinstance(supplied[name], list)
                or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                       for item in supplied[name]) or len(set(supplied[name])) != len(supplied[name])):
            invalid.append({"name": name, "type": "array[string]", "required": False,
                            "value": deepcopy(supplied[name])})
    unknown = sorted(set(supplied) - known_names)
    if missing or invalid or unknown:
        return {"profile": PROFILE, "outcome": "incomplete", "command": "closeout",
                "missing_fields": missing, "invalid_fields": invalid, "unknown_fields": unknown,
                "values": preserved, "example": next(item["example"] for item in COMMANDS if item["id"] == "closeout"),
                "next": "Correct the reported fields and submit closeout again."}
    normalized = {"form": "legacy" if legacy else "closeout-v2", "why": supplied["why"].strip(),
                  "checks": [item.strip() for item in supplied.get("checks", [])],
                  "unresolved": [item.strip() for item in supplied.get("unresolved", [])],
                  "changed_paths": (list(dict.fromkeys(item.replace("\\", "/") for item in supplied["changed_paths"]))
                                    if "changed_paths" in supplied else None),
                  "decisions": [item.strip() for item in supplied.get("decisions", [])],
                  "children": deepcopy(supplied.get("children", [])),
                  "work_size": supplied.get("work_size", "slice"), "event": supplied.get("event", "turn"),
                  "consolidated_pending_ids": list(supplied.get("consolidated_pending_ids", [])),
                  "selected_pending_ids": (list(supplied["selected_pending_ids"])
                                           if "selected_pending_ids" in supplied else None)}
    if legacy:
        normalized["what"] = supplied["what"].strip()
    else:
        normalized.update(request=supplied["request"].strip(),
                          actions=[item.strip() for item in supplied["actions"]], result=supplied["result"].strip())
    return {"profile": PROFILE, "outcome": "valid", "command": "closeout", "values": normalized}


def answer_reuse(values):
    """Bind reviewed semantic answers for mechanical reuse without granting any effect."""
    candidate = deepcopy(values) if isinstance(values, dict) else values
    if isinstance(candidate, dict) and candidate.get("form") in {"legacy", "closeout-v2"}:
        candidate.pop("form")
        for name in ("changed_paths", "selected_pending_ids"):
            if candidate.get(name) is None:
                candidate.pop(name, None)
    checked = validate_closeout(candidate)
    if checked["outcome"] != "valid":
        return {"profile": "eutonos.closeout-answer-reuse.v1", "outcome": "incomplete",
                "validation": checked, "authorization": "none"}
    normalized = checked["values"]
    fingerprint = hashlib.sha256(ts.encoded(normalized)).hexdigest()
    return {"profile": "eutonos.closeout-answer-reuse.v1", "outcome": "reviewed",
            "fingerprint": fingerprint, "values": normalized,
            "reusable_for": ["organization-summary", "ram-summary"],
            "authorization": "none; each phase retains its own plan, authority, outcome and readback",
            "qualification": "Reuses supplied facts only; it is not a completion, filing, capture or approval receipt."}


def _git(root, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                          env=env, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", **no_window())


def _path_evidence(root, path, source_commit, work_path=None):
    local = LocalRoot(root)
    body = local.read(path)
    committed = _git(root, "cat-file", "-e", source_commit + ":" + path, check=False).returncode == 0
    changed = _git(root, "diff", "--quiet", source_commit, "--", path, check=False).returncode != 0
    untracked = bool(_git(root, "ls-files", "--others", "--exclude-standard", "--", path).stdout.strip())
    if untracked:
        state = "untracked"
    elif committed and body is None:
        state = "deleted"
    elif committed and changed:
        state = "modified"
    elif committed:
        state = "committed-clean"
    elif body is not None:
        state = "added"
    else:
        state = "absent"
    basis_sha = digest(body)
    if path == work_path and body is not None:
        basis_sha = ram.work_hash(body)
    return {"path": path, "state": state, "current_sha256": digest(body),
            "basis_sha256": basis_sha, "at_commit": committed}


def _changed_paths(root):
    tracked = _git(root, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    return sorted(set(path for path in tracked + untracked if path))


def _changed_candidates(root):
    tracked = set(path for path in _git(root, "diff", "--name-only", "-z", "HEAD").stdout.split("\0") if path)
    untracked = set(path for path in _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0") if path)
    return [{"path": path, "status": "untracked" if path in untracked else "changed", "candidate_only": True}
            for path in sorted(tracked | untracked)]


def _draft(root):
    try:
        root = Path(root).resolve()
        commit = _git(root, "rev-parse", "HEAD").stdout.strip()
        ram_note = None if (root / "eutonos.ram.json").is_file() else \
            "RAM is not configured; Git candidates remain available but closeout capture is not configured."
        return {"source_commit": commit, "detected_changed_paths": _changed_candidates(root),
                "note": "Detected paths are candidates from the existing worktree; selection does not attribute them to this task.",
                "ram": ram_note}
    except (AttributeError, KeyError, TypeError, OSError, UnicodeError, ValueError, subprocess.SubprocessError, ts.ReadError) as exc:
        return {"draft_error": {"code": getattr(exc, "code", "unavailable"),
                                "message": getattr(exc, "message", str(exc))}}


def _context_missing(context):
    fields = []
    # work_id is optional: closeout creates a Quick work item when none resolves.
    for name in ("root", "host", "actor", "session_id"):
        if not isinstance(context.get(name), str) or not context[name].strip():
            fields.append({"name": name, "type": "string", "required": True,
                           "description": "Configured workflow identity or path."})
        elif name in {"actor", "session_id"}:
            try:
                ram.slug(context[name])
            except ts.ReadError:
                fields.append({"name": name, "type": "string", "required": True,
                               "description": "Bounded portable RAM identity component."})
    return fields


def _clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _closeout_new_work(root, context, values, supplied):
    """Plan (never write) the Quick work item closeout creates for an unresolved work ID.

    The creation token is derived from actor, session and the supplied reference,
    so prepare and run (and a repeated closeout in the same session) name one
    item. The write itself happens only in execute_closeout.
    """
    import uuid
    from . import boards
    projection = boards.project(root)
    ts.require(projection.get("status") == "available", "unavailable", "Configured board owners are unavailable.")
    available = {owner["path"] for owner in projection.get("owners", []) if owner.get("status") == "available"}
    counts = {}
    for item in projection.get("items", []):
        if item.get("type") == "quick" and item.get("owner_path") in available:
            counts[item["owner_path"]] = counts.get(item["owner_path"], 0) + 1
    ts.require(bool(available), "missing", "No configured board owner can accept new Quick work.")
    # The owner that already holds the most Quick work; ties keep configured order.
    order = [owner["path"] for owner in projection["owners"] if owner["path"] in available]
    owner_path = max(order, key=lambda path: (counts.get(path, 0), -order.index(path)))
    token = str(uuid.uuid5(uuid.NAMESPACE_URL, "eutonos-closeout-work|" + "|".join(
        (context["actor"], context["session_id"], supplied or ""))))
    meaning = _meaning(values)
    request = meaning["request"] or meaning["result"]
    summary = "Created by closeout because no existing work ID applied. " + (
        "Supplied reference: " + supplied + ". " if supplied else "") + "Outcome: " + meaning["result"]
    return {"work_id": "quick-" + token, "owner_path": owner_path, "creation_token": token,
            "supplied_reference": supplied, "title": _clip(request, 120), "summary": _clip(summary, 1000),
            "status": "active" if values["unresolved"] else "completed",
            "will_create": boards.resolve_owner(root, "quick-" + token) is None}


def _create_closeout_work(root, host, actor, creation, caller=None):
    """Create the planned Quick item through the managed batch route unless it exists."""
    from . import boards, write_batch, write_retry
    if boards.resolve_owner(root, creation["work_id"]) is not None:
        return {"outcome": "exists", "work_id": creation["work_id"]}
    owner = creation["owner_path"]
    item = {"type": "quick", "title": creation["title"], "summary": creation["summary"],
            "status": creation["status"], "extensions": {"creation_token": creation["creation_token"]}}
    batch = {"profile": write_batch.PROFILE, "operations": [{"kind": "add", "request": {
        "owner_path": owner, "expected_sha256": digest(LocalRoot(root).read(owner)), "index": 0, "items": [item]}}],
        "requested_by": {"kind": "agent-claim", "value": "guided-closeout"}}
    result = write_retry.submit_batch(host, root, actor, batch)
    ts.require(result.get("outcome") == "complete" and boards.resolve_owner(root, creation["work_id"]) == owner,
               "unavailable", "Closeout could not create its Quick work item: " + str(result.get("message") or result.get("outcome")))
    return {"outcome": "created", "work_id": creation["work_id"], "owner_path": owner}


def _registry_references(root, paths):
    registry = Path(root) / "docs" / "REGISTRY.json"
    if not registry.is_file():
        return []
    try:
        body = registry.read_bytes()
        value = json.loads(body.decode("utf-8"))
        documents = value.get("documents", []) if isinstance(value, dict) else []
        by_path = {item.get("path"): item.get("id") for item in documents if isinstance(item, dict)
                   and isinstance(item.get("path"), str) and isinstance(item.get("id"), str)}
        return [{"path": path, "id": by_path[path], "source": {"path": "docs/REGISTRY.json",
                 "sha256": digest(body), "view": "working-tree"}}
                for path in paths if path in by_path]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []


def _existing_links(root, work_path, work_id=None):
    body = LocalRoot(root).read(work_path)
    if body is None:
        return []
    blocks = list(ram.BLOCK.finditer(body))
    if len(blocks) != 1:
        return []
    try:
        value = ram.unpack(blocks[0].group(1))
        if not isinstance(value, dict):
            return []
        if value.get("profile") == ram.LINKS:
            return list(value.get("memories", [])) if work_id in {None, value.get("work_id")} else []
        if value.get("profile") == ram.LINKS_V2 and isinstance(value.get("links"), list):
            return [memory for entry in value["links"] if isinstance(entry, dict)
                    and work_id in {None, entry.get("work_id")}
                    for memory in entry.get("memories", []) if isinstance(memory, str)]
        return []
    except (UnicodeError, ValueError, ts.ReadError):
        return []


def _meaning(values):
    if values["form"] == "legacy":
        return {"request": None, "actions": [values["what"]], "result": values["what"], "legacy": True}
    return {"request": values["request"], "actions": values["actions"], "result": values["result"], "legacy": False}


def _unique(items):
    return list(dict.fromkeys(item for item in items if item is not None))


def _pending_entry(values, native_identity=None):
    saved = {key: deepcopy(value) for key, value in values.items()
             if key not in {"consolidated_pending_ids", "selected_pending_ids"}}
    return {"profile": "eutonos.semantic-closeout-delta.v1", "values": saved,
            "native_identity": deepcopy(native_identity)}


def _normalized_delta(entry):
    ts.shape(entry, {"profile", "values", "native_identity"})
    ts.require(entry["profile"] == "eutonos.semantic-closeout-delta.v1" and isinstance(entry["values"], dict),
               "invalid", "Pending semantic delta is invalid.")
    value = entry["values"]
    required = {"form", "why", "checks", "unresolved", "changed_paths", "decisions", "work_size", "event"}
    ts.require(required <= set(value) and value["form"] in {"legacy", "closeout-v2"}
               and _strings([value["why"]]) and _strings(value["checks"] or ["none"])
               and isinstance(value["unresolved"], list) and all(isinstance(item, str) for item in value["unresolved"])
               and isinstance(value["changed_paths"], list) and all(isinstance(item, str) for item in value["changed_paths"])
               and isinstance(value["decisions"], list) and all(isinstance(item, str) for item in value["decisions"]),
               "invalid", "Pending semantic delta fields are invalid.")
    ts.require(value["work_size"] in capture_policy.WORK_SIZES and value["event"] in capture_policy.EVENTS,
               "invalid", "Pending semantic delta boundary is invalid.")
    if value["form"] == "legacy":
        ts.require(isinstance(value.get("what"), str) and value["what"].strip(), "invalid", "Pending legacy result is invalid.")
    else:
        ts.require(isinstance(value.get("request"), str) and value["request"].strip()
                   and _strings(value.get("actions")) and isinstance(value.get("result"), str)
                   and value["result"].strip(), "invalid", "Pending closeout result is invalid.")
    return deepcopy(value)


def _combine_values(pending, current):
    consolidated = set(current["consolidated_pending_ids"])
    old_values = [(item["id"], _normalized_delta(item["content"])) for item in pending]
    values = [value for _, value in old_values] + [current]
    narrative = [value for entry_id, value in old_values if entry_id not in consolidated] + [current]
    all_legacy = all(item["form"] == "legacy" for item in narrative)
    combined = {"form": "legacy" if all_legacy else "closeout-v2",
                "why": " | ".join(_unique(item["why"] for item in values)),
                "checks": _unique(check for item in values for check in item["checks"]),
                "unresolved": _unique(risk for item in values for risk in item["unresolved"]),
                "changed_paths": _unique(path for item in values for path in item["changed_paths"]),
                "decisions": _unique(decision for item in values for decision in item["decisions"]),
                "work_size": current["work_size"], "event": current["event"]}
    children = {}
    for value in values:
        for child in _child_refs(value.get("children", [])):
            key = ts.encoded(child["identity"])
            ts.require(key not in children or children[key] == child, "conflict",
                       "Pending closeout contains conflicting child RAM revisions.")
            children[key] = child
    combined["children"] = list(children.values())
    if all_legacy:
        combined["what"] = " | ".join(_unique(item["what"] for item in narrative))
    else:
        combined["request"] = " | ".join(_unique(item.get("request") for item in narrative))
        combined["actions"] = _unique(action for item in narrative for action in
                                       (item["actions"] if item["form"] != "legacy" else [item["what"]]))
        combined["result"] = " | ".join(_unique(item.get("result", item.get("what")) for item in narrative))
    return combined


def _catalog_task_owner(root):
    """Resolve the current live catalog's one eligible Docs work owner."""
    local = LocalRoot(root)
    raw = local.read("eutonos.read.json")
    if raw is None:
        return None
    try:
        catalog = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "The live read catalog must be UTF-8.") from exc
    ts.shape(catalog, {"profile", "repository_id", "scope_id", "namespace", "owners",
                       "bindings", "excluded_roots", "source_paths"}, {"extensions"})
    ts.require(catalog["profile"] == ts.CATALOG, "unsupported", "Unsupported TokenSlang catalog.")
    for key in ("repository_id", "scope_id", "namespace"):
        ts.string(catalog[key])
    ts.shape(catalog["owners"], {"current_task"})
    owner = ts.reference(catalog["owners"]["current_task"])
    ts.require("resource_id" in owner, "unsupported",
               "The current task owner must select one registered Docs binding.")
    ts.require(owner["scope_id"] == catalog["scope_id"] and owner["namespace"] == catalog["namespace"],
               "wrong-scope", "Current task owner is outside the live catalog scope.")
    bindings = ts.sequence(catalog["bindings"])
    for binding in bindings:
        ts.shape(binding, {"resource_id", "path", "kind", "label", "visibility", "state"},
                 {"aliases", "sha256", "start_line", "end_line", "extensions"})
        for key in ("resource_id", "label", "visibility", "state"):
            ts.string(binding[key])
        safe_path(binding["path"])
        ts.require(isinstance(binding["kind"], str) and binding["kind"] in {"docs", "source", "nav", "ram"},
                   "unsupported", "Unsupported catalog provider.")
        for alias in ts.sequence(binding.get("aliases", [])):
            ts.string(alias)
        if "sha256" in binding:
            ts.require(isinstance(binding["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", binding["sha256"])),
                       "invalid", "Invalid catalog source fingerprint.")
        ts.reference({key: binding[key] for key in ("path", "start_line", "end_line") if key in binding})
    excluded_roots = [safe_path(path) for path in ts.sequence(catalog["excluded_roots"])]
    for path in ts.sequence(catalog["source_paths"]):
        safe_path(path)
    matches = [binding for binding in bindings if owner["resource_id"] == binding["resource_id"]
               or owner["resource_id"] in binding.get("aliases", [])]
    ts.require(bool(matches), "missing", "Current task owner is not registered.")
    ts.require(all(binding["visibility"] == "public" and binding["state"] == "active" for binding in matches),
               "denied", "Current task owner is not eligible for public active reading.")
    ts.require(len(matches) == 1, "ambiguous", "Current task owner has competing bindings.")
    binding = matches[0]
    ts.require(binding["kind"] == "docs", "unsupported", "Current task owner must use the Docs adapter.")
    ts.require(not any(binding["path"] == root or binding["path"].startswith(root + "/")
                       for root in excluded_roots), "denied", "Current task owner is under an excluded root.")
    ts.require(all(item["visibility"] == "public" and item["state"] == "active"
                   for item in bindings if item["path"] == binding["path"]),
               "denied", "Current task path has an ineligible binding.")
    return {"path": binding["path"], "binding": binding,
            "same_path_bindings": [item for item in bindings if item["path"] == binding["path"]],
            "identity": {key: catalog[key] for key in ("repository_id", "scope_id", "namespace")},
            "catalog_source": {"path": "eutonos.read.json", "sha256": digest(raw)}}


def pickup(root):
    """Return compact current-task context from explicit local owners."""
    require_shared(root)
    root = Path(root).resolve()
    try:
        commit = _git(root, "rev-parse", "HEAD").stdout.strip()
        catalog_owner = _catalog_task_owner(root)
        task_path = catalog_owner["path"] if catalog_owner else "docs/CURRENT_TASK.md"
        ram_status = {"configured": False}
        if (root / "eutonos.ram.json").is_file():
            store = ram.Store(LocalRoot(root))
            if catalog_owner:
                ts.require(all(store.cfg[key] == catalog_owner["identity"][key]
                               for key in ("repository_id", "scope_id", "namespace")),
                           "wrong-scope", "RAM and live read catalog select different project identities.")
                ts.require(store.cfg["current_task"] == task_path, "conflict",
                           "RAM and live read catalog select different current task owners.")
            else:
                task_path = store.cfg["current_task"]
            effective = settings.status(root)
            ram_status = {"configured": True, "capture": effective["ram"]["capture"],
                          "retrieval": effective["ram"]["retrieval"], "settings_revision": effective["revision"]}
        body = LocalRoot(root).read(task_path)
        ts.require(body is not None, "missing", "Selected task owner is absent.")
        try:
            task_text = body.decode("utf-8")
        except UnicodeError as exc:
            raise ts.ReadError("unsupported", "Selected task owner must be UTF-8.") from exc
        if catalog_owner and "start_line" in catalog_owner["binding"]:
            lines = task_text.splitlines(keepends=True)
            binding = catalog_owner["binding"]
            ts.require(binding["end_line"] <= len(lines), "missing", "Selected task owner range is absent.")
            task_text = "".join(lines[binding["start_line"] - 1:binding["end_line"]])
        if catalog_owner:
            ts.require(all("sha256" not in binding or digest(body) == binding["sha256"]
                           for binding in catalog_owner["same_path_bindings"]), "stale",
                       "Current task binding fingerprint differs from live owner bytes.")
        from . import boards
        work_board = boards.project(root)
        selected = None if work_board.get("work_order", {}).get("configured") else ts.work(task_text)
        references = _registry_references(root, [task_path, *[item.get("path") for item in
            (selected or {}).get("required", []) if isinstance(item, dict) and isinstance(item.get("path"), str)]])
        result = {"profile": PROFILE, "outcome": "pickup", "source_commit": commit,
                "task_owner": {"path": task_path, "sha256": digest(body)}, "selected_task": selected,
                "catalog_source": catalog_owner["catalog_source"] if catalog_owner else None,
                "references": references, "ram": ram_status,
                "work_order": work_board.get("work_order"),
                "current_tasks": [i for identity in work_board.get("work_order", {}).get("current", [])
                                  for i in work_board.get("items", []) if i["id"] == identity],
                "routes": [{"id": item["id"], "supported": item["supported"]} for item in catalog()["commands"]]}
        from .work_attribution import profile_hint
        result['participant_profile'] = profile_hint(root)
        # Cross-checkout orientation is derived evidence, not a replacement for
        # this checkout's canonical owners or permission to integrate anything.
        try:
            from . import repository_state
            result["repository_state"] = repository_state.compact(repository_state.project(root))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["repository_state"] = {"status": "unavailable", "reason": str(exc),
                                          "next_action": "Inspect source directly; do not infer global completion from this checkout."}
        row_items = work_board.get("items", [])
        try:
            from . import current_work, work_lifecycle
            shared = current_work.project(root, include_knowledge=False)
            result["current_work"] = current_work.compact(shared)
            if shared.get("status") in {"available", "partial"} and shared.get("boards", {}).get("items"):
                combined = shared["boards"]
                result["checkout_current_tasks"] = result["current_tasks"]
                result["work_order"] = combined.get("work_order")
                row_items = combined["items"]
                result["current_tasks"] = [item for item in combined["items"]
                    if item.get("status") not in {"completed", "archived", "cancelled", "superseded", "removed"}
                    and (item.get("type") in {"campaign", "slice", "quick", "task"}
                         or work_lifecycle.queued(item))
                    and not item.get("parent_id")]
                result["orientation"] = "Current Work combines observed branches/worktrees. Use each source pin for edits; Main delivery remains separate."
        except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
            result["current_work"] = {"status": "unavailable", "reason": str(exc)}
            result["orientation"] = "Selected-checkout fallback; combined coverage is unknown."
        # The current Work order row (stages and together groups included) and
        # the one-line admission rule; both are derived from the saved order.
        try:
            from . import work_lifecycle
            result["current_row"] = work_lifecycle.row_summary(result.get("work_order"), row_items)
        except (KeyError, TypeError, ValueError, ts.ReadError):
            result["current_row"] = None
        # Slice 3B.6: fold in one compact Crosstalk status line (messaging on/off,
        # owner halt, denied recipients) read from its device controls. Read-only;
        # when Crosstalk is not installed here, or its state cannot be read, pickup's
        # output stays exactly as before and pickup never fails because of it.
        try:
            crosstalk_hint = crosstalk_status.pickup_hint()
        except Exception:  # an optional hint must never turn a valid pickup into a failure
            crosstalk_hint = None
        if crosstalk_hint is not None:
            result["crosstalk"] = crosstalk_hint
        # Steps a component or setting switch could not finish itself (restart an app, add or remove a memory):
        # a count and the first items, only when something is waiting or the list cannot be read.
        try:
            from . import crosstalk_link  # an offered Hermes link repair becomes one pending action (3B.17)
            crosstalk_link.sync_pending_offer()
        except Exception:  # never turns a valid pickup into a failure
            pass
        try:
            pending_hint = pending_actions.pickup_hint()
        except Exception:  # an optional hint must never turn a valid pickup into a failure
            pending_hint = None
        if pending_hint is not None:
            result["pending_actions"] = pending_hint
        # Capability metadata is advisory. A damaged/busy per-user watch must
        # never turn an otherwise valid repository pickup into a boot failure.
        try:
            from . import model_watch
            watch_scan = model_watch.scan_if_due(root=root)
            result["model_watch"] = model_watch.pickup_hint()
            if isinstance(watch_scan, dict) and watch_scan.get("status") == "attention-required":
                result["model_watch"] = {**(result["model_watch"] or {}), "error": "Watch metadata unavailable"}
        except Exception:  # isolated optional metadata, no model or native effects
            result["model_watch"] = {"pending_count": 0, "sources": {}, "error": "Watch metadata unavailable"}
        return result
    except (AttributeError, KeyError, TypeError, OSError, UnicodeError, ValueError, subprocess.SubprocessError, ts.ReadError) as exc:
        return {"profile": PROFILE, "outcome": "error", "code": getattr(exc, "code", "unavailable"),
                "message": getattr(exc, "message", str(exc)), "next": "Use catalog or setup to inspect the missing local owner."}


def _shared_closeout_error(context, profile=PROFILE):
    root = context.get("root", ".") if isinstance(context, dict) else "."
    if not isinstance(root, (str, os.PathLike)) or not str(root).strip():
        root = "."
    try:
        require_shared(root)
    except ts.ReadError as exc:
        return {"profile": profile, "outcome": "error", "code": exc.code, "message": exc.message}
    return None


def prepare_closeout(values, context):
    refusal = _shared_closeout_error(context)
    if refusal is not None:
        return refusal
    checked = validate_closeout(values)
    root_value = context.get("root") if isinstance(context, dict) else None
    effective = policy = None
    if checked["outcome"] == "valid" and isinstance(root_value, str) and root_value.strip():
        try:
            effective = settings.status(root_value)
            # Closeout is project record keeping. Legacy capture toggles and
            # cadence settings remain readable for compatibility, but cannot
            # suppress this required RAM checkpoint.
            policy = capture_policy.evaluate(True, "every-turn",
                                             checked["values"]["work_size"], checked["values"]["event"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return {"profile": PROFILE, "outcome": "error", "code": "invalid-settings", "message": str(exc),
                    "values": checked["values"], "next": "Repair or inspect workflow settings before closeout."}
    missing_context = _context_missing(context if isinstance(context, dict) else {})
    if checked["outcome"] != "valid" or missing_context:
        checked = deepcopy(checked)
        if isinstance(root_value, str) and root_value.strip():
            checked["draft"] = _draft(root_value)
        if missing_context:
            checked["outcome"] = "incomplete"
            checked["missing_context"] = missing_context
            checked.setdefault("values", {})
        return checked
    values, context = checked["values"], deepcopy(context)
    root = Path(context["root"]).resolve()
    try:
        ts.require((root / "eutonos.ram.json").is_file(), "not-configured",
                   "RAM is not configured for this repository; run the supported setup before closeout capture.")
        store = ram.Store(LocalRoot(root))
        source_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
        native = None
        if context.get("native_log") is not None:
            from .companion_capture import native_identity
            native = native_identity(context["native_log"], stage="pre-final")
        supplied = context.get("work_id") if isinstance(context.get("work_id"), str) and context["work_id"].strip() else None
        work_path, work_creation = None, None
        if supplied is not None:
            try:
                ram.slug(supplied)
            except ts.ReadError:
                pass  # a reference that is not a portable ID (for example a chat ref) cannot be an owner key
            else:
                work_path = store.work_owner(supplied)
        if not isinstance(work_path, str):
            # No native owner applies: closeout will create one Quick item (on run only).
            work_creation = _closeout_new_work(root, context, values, supplied)
            work_path = work_creation["owner_path"]
            context["work_id"] = work_creation["work_id"]
        selected_paths = values["changed_paths"]
        if selected_paths is None:
            selected_paths = [path for path in _changed_paths(root) if not store.inside(path)]
            if work_path in selected_paths:
                current = LocalRoot(root).read(work_path)
                committed, _ = GitView(root, source_commit).read(work_path)
                if current is not None and committed is not None and ram.work_hash(current) == ram.work_hash(committed):
                    selected_paths.remove(work_path)
        selected_paths = list(dict.fromkeys(safe_path(path) for path in selected_paths))
        values = {**values, "changed_paths": selected_paths}
        pending = capture_policy.snapshot(root, context["actor"], context["work_id"], context["session_id"]) \
            if work_creation is None else []
        available = {item["id"] for item in pending}
        selected_ids = values["selected_pending_ids"]
        if selected_ids is not None:
            ts.require(set(selected_ids) <= available, "missing", "Selected pending capture ID is absent.")
            pending = [item for item in pending if item["id"] in set(selected_ids)]
        ts.require(set(values["consolidated_pending_ids"]) <= {item["id"] for item in pending}, "missing",
                   "Consolidated pending IDs must be included in this capture.")
        if pending:
            values = _combine_values(pending, values)
            selected_paths = values["changed_paths"]
        children = values.get("children", [])
        _verify_child_refs(store, children)
        evidence = [_path_evidence(root, path, source_commit, work_path) for path in selected_paths]
        meaning = _meaning(values)
        frozen_evidence = [{key: item[key] for key in ("path", "state", "basis_sha256", "at_commit")}
                           for item in evidence]
        basis = {"actor": context["actor"], "work_id": context["work_id"], "session_id": context["session_id"],
                 "source_commit": source_commit, "source_pin_policy": SOURCE_PIN_POLICY,
                 "evidence": frozen_evidence, "native_identity": native, "values": values,
                 "pending": pending, "capture_detail": effective["ram"]["detail"]}
        content_id = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=False).encode("utf-8")).hexdigest()
        identity = {"work_id": context["work_id"], "session_id": context["session_id"],
                    "issuer_id": context["actor"], "memory_id": "closeout-" + content_id[:20]}
        record_path = store.path(identity)
        artifact = root / ".eutonos-local" / "workflow" / ("closeout-" + content_id[:24] + ".json")
        sources = [item["path"] for item in evidence
                   if item["state"] == "committed-clean" and not store.inside(item["path"])]
        baseline_work_anchor = not sources
        if baseline_work_anchor:
            # RAM sources are exact Git evidence. A newly created work owner may
            # exist only in the working tree, so fall back to a durable committed
            # workflow authority rather than mislabeling that owner as committed.
            sources = ["eutonos.ram.json"]
        def section(label, items):
            return label + ": " + ("; ".join(items) if items else "none")
        detail = effective["ram"]["detail"]
        shown_limit = len(evidence) if detail == "detailed" else (10 if detail == "brief" else 20)
        shown = [item["path"] + " (" + item["state"] + ")" for item in evidence[:shown_limit]]
        if len(evidence) > shown_limit:
            shown.append("and " + str(len(evidence) - shown_limit) + " more in frozen operation evidence")
        semantic_lines = (["Legacy what: " + values["what"]] if meaning["legacy"] else
                          ["User request: " + meaning["request"], section("Agent actions", meaning["actions"]),
                           "Outcome: " + meaning["result"]])
        detail_lines = (["Detailed evidence: " + "; ".join(item["path"] + "=" + item["basis_sha256"]
                                                            for item in evidence)] if detail == "detailed" else [])
        summary = "\n".join((*semantic_lines, "Capture detail: " + detail,
                             "Why files changed: " + values["why"],
                             section("Checks", values["checks"]), section("Unresolved", values["unresolved"]),
                             section("Decisions", values["decisions"]),
                             *detail_lines,
                             "Source pins are committed context, not proof of uncommitted changes. " +
                             ("The workflow configuration pin is a historical baseline anchor only. " if baseline_work_anchor else "") +
                             "Working-tree states below are reported observations; exact current-byte hashes are retained " +
                             "in the local frozen closeout artifact, not as portable committed source pins.",
                             "Changed paths observed against committed baseline " +
                             source_commit + ": " + ("; ".join(shown) if shown else "none")))
        if len(summary.encode("utf-8")) > 8192:
            return {"profile": PROFILE, "outcome": "incomplete", "command": "closeout", "code": "overflow",
                    "message": "Combined pending closeout exceeds the RAM 8 KiB summary limit.",
                    "values": values,
                    "pending_preview": {"count": len(pending), "ids": [item["id"] for item in pending],
                                        "route": "workflow pending"},
                    "next": "Consolidate acknowledged pending narratives, or select fewer pending IDs for separate captures. Preserve decisions, risks, checks, and source evidence; pending entries remain saved."}
        semantic = {"profile": ram.WRITE, "operation": "capture", "identity": identity,
                    "expected_revision": None, "reason": values["why"],
                    "experience": {"kind": "milestone" if children else "summary", "summary": summary, "occurred_at": context.get("occurred_at"),
                                   "unfinished": bool(values["unresolved"]),
                                   "actors": [{"id": context["actor"], "attribution": "self-reported"}],
                                   "work_path": work_path, "source_commit": source_commit,
                                   "sources": [{"path": path, "evidence": "supplied"} for path in sources],
                                   "children": [child["path"] for child in children], "skill": None}}
        if children:
            semantic["experience"]["child_revisions"] = deepcopy(children)
        delivery_request = None
        if context.get('delivery') is not None:
            from .git_delivery_native import prepare_code
            delivery_request = prepare_code(root, context['delivery'], session=context['session_id'], work_id=context['work_id'],
                closeout={'host':context['host'],'actor':context['actor'],'attempt':'workflow-closeout-' + content_id[:24]})
        refs = _registry_references(root, [*selected_paths, work_path])
        return {"profile": PROFILE, "outcome": "prepared", "command": "closeout", "values": values,
                "answer_reuse": answer_reuse(values),
                "source_pin_policy": SOURCE_PIN_POLICY, "baseline_work_anchor": baseline_work_anchor,
                "identity": identity, "source_commit": source_commit, "changed_paths": evidence,
                "semantic": semantic, "record_path": record_path, "allowed_paths": sorted([record_path, work_path]),
                "attempt": "workflow-closeout-" + content_id[:24], "artifact": str(artifact),
                "native_identity": native, "references": refs,
                **({"code_delivery": delivery_request} if delivery_request is not None else {}),
                **({"work_creation": work_creation} if work_creation is not None else {}),
                "capture_policy": {**policy, "detail": detail}, "pending": pending,
                "ram_native": {"identity": identity, "path": record_path, "existing": LocalRoot(root).read(record_path) is not None,
                                "existing_work_links": _existing_links(root, work_path, context["work_id"])}}
    except (AttributeError, KeyError, TypeError, OSError, UnicodeError, ValueError, subprocess.SubprocessError, ts.ReadError) as exc:
        return {"profile": PROFILE, "outcome": "error", "code": getattr(exc, "code", "unavailable"),
                "message": getattr(exc, "message", str(exc)), "values": values,
                "next": "Use workflow settings/setup or repair the reported configuration before retrying."}


def execute_closeout(values, context, capture_fn=None):
    refusal = _shared_closeout_error(context)
    if refusal is not None:
        return refusal
    prepared = deepcopy(values) if isinstance(values, dict) and values.get("profile") == PROFILE \
        and values.get("outcome") in {"prepared", "deferred"} else prepare_closeout(values, context)
    if prepared["outcome"] == "deferred":
        try:
            # Older prepared forms deferred capture into local pending state.
            # Materialize that exact delta once, then run the normal current
            # closeout path so it is flushed to RAM under the current policy.
            expected_scope = {"root": str(Path(context["root"]).resolve()), "actor": context["actor"],
                              "work_id": context["work_id"], "session_id": context["session_id"],
                              "native_identity": None}
            if context.get("native_log") is not None:
                from .companion_capture import native_identity
                expected_scope["native_identity"] = native_identity(context["native_log"], stage="pre-final")
            ts.require(isinstance(prepared.get("pending_entry"), dict)
                       and prepared.get("pending_scope") == expected_scope, "wrong-scope",
                       "Deferred closeout is incomplete; prepare again.")
            saved = capture_policy.defer(context["root"], context["actor"], context["work_id"],
                                          context["session_id"], prepared["pending_entry"])
            if saved["outcome"] == "already-consumed":
                return {"profile": PROFILE, "outcome": "already-completed", "command": "closeout",
                        "reason": "pending-delta-already-consumed", "pending": saved,
                        "message": "This deferred closeout was already included in a completed RAM capture; it was not repeated."}
            ts.require(isinstance(prepared.get("values"), dict), "invalid",
                       "Deferred closeout has no semantic values; preserve it and inspect.")
            legacy_values = deepcopy(prepared["values"])
            legacy_values.pop("form", None)
            for name in ("changed_paths", "selected_pending_ids"):
                if legacy_values.get(name) is None:
                    legacy_values.pop(name, None)
            prepared = prepare_closeout(legacy_values, context)
        except (KeyError, TypeError, OSError, ValueError, ts.ReadError) as exc:
            return {"profile": PROFILE, "outcome": "error", "code": getattr(exc, "code", "unavailable"),
                    "message": getattr(exc, "message", str(exc)), "operation": prepared}
    if prepared["outcome"] != "prepared":
        return prepared
    creation = prepared.get("work_creation")
    if creation is not None:
        # Every later identity check and pending/delivery key uses the created ID.
        context = {**context, "work_id": creation["work_id"]}
    try:
        from .companion_ram_capture import capture as native_capture
        required = {"identity", "source_commit", "changed_paths", "semantic", "record_path",
                    "allowed_paths", "attempt", "artifact"}
        ts.require(required <= set(prepared) and isinstance(prepared["semantic"], dict)
                   and isinstance(prepared["changed_paths"], list) and isinstance(prepared["allowed_paths"], list),
                   "invalid", "Prepared closeout is incomplete; run prepare again.")
        identity = prepared["identity"]
        ts.require(identity == prepared["semantic"].get("identity")
                   and identity.get("work_id") == context.get("work_id")
                   and identity.get("session_id") == context.get("session_id")
                   and identity.get("issuer_id") == context.get("actor"),
                   "wrong-scope", "Prepared closeout identity differs from the configured writer/work context.")
        store = ram.Store(LocalRoot(context["root"]))
        expected_record = store.path(identity)
        children = _child_refs(prepared.get("values", {}).get("children", []))
        experience = prepared["semantic"]["experience"]
        ts.require(experience.get("children") == [child["path"] for child in children]
                   and experience.get("kind") == ("milestone" if children else "summary")
                   and experience.get("child_revisions", []) == children,
                   "invalid", "Prepared closeout child references differ from its RAM semantic.")
        _verify_child_refs(store, children)
        ts.require(prepared["record_path"] == expected_record
                   and sorted(prepared["allowed_paths"]) == sorted([expected_record,
                       prepared["semantic"]["experience"]["work_path"]]),
                   "invalid", "Prepared closeout paths differ from the native RAM owner plan.")
        native_log = context.get("native_log")
        if prepared.get("native_identity") is not None or native_log is not None:
            from .companion_capture import native_identity
            ts.require(prepared.get("native_identity") is not None and isinstance(native_log, str),
                       "invalid", "Native pre-final capture must be selected when the closeout is prepared.")
            observed_native = native_identity(native_log, stage="pre-final")
            ts.require(observed_native == prepared["native_identity"], "stale",
                       "Native pre-final identity changed after closeout preparation.")
        artifact = Path(prepared["artifact"])
        if not artifact.exists():
            current_commit = _git(Path(context["root"]).resolve(), "rev-parse", "HEAD").stdout.strip()
            ts.require(current_commit == prepared["source_commit"], "stale",
                       "Repository HEAD changed after closeout preparation; prepare again.")
            work_path = prepared["semantic"]["experience"]["work_path"]
            for expected in prepared["changed_paths"]:
                current = _path_evidence(Path(context["root"]).resolve(), expected["path"], current_commit, work_path)
                ts.require(all(current.get(key) == expected.get(key) for key in ("state", "basis_sha256", "at_commit")),
                           "stale", "Changed path evidence moved after closeout preparation: " + expected["path"])
        created_work = None
        if creation is not None:
            # Only a run writes. The frozen token makes a repeat find the item instead of adding another.
            created_work = _create_closeout_work(Path(context["root"]).resolve(), context["host"],
                                                 context["actor"], creation)
        result = (capture_fn or native_capture)(context["root"], context["host"], context["actor"],
                                                prepared["attempt"], prepared["semantic"],
                                                prepared["allowed_paths"], prepared["artifact"])
        response = {"profile": PROFILE, "outcome": result.get("outcome", "error"), "command": "closeout",
                "operation": {key: prepared[key] for key in ("identity", "source_commit", "changed_paths", "record_path",
                                                               "allowed_paths", "attempt", "artifact")},
                "retry": {"selector": prepared["attempt"], "instruction": "Reuse the original prepared closeout JSON."},
                "result": result,
                **({"work_creation": {**creation, **created_work}} if created_work is not None else {})}
        if children and result.get("outcome") in {"completed", "already-completed"}:
            try:
                ts.require(result.get("current_verified") is True, "stale",
                           "Spawner capture has no verified native terminal result.")
                current_store = ram.Store(LocalRoot(context["root"]))
                _verify_child_refs(current_store, children)
                parent = current_store.read({"profile": ram.READ, "operation": "get", "identity": identity,
                                             "revision": result["readback"]["revision"],
                                             "max_bytes": 32768, "limit": 1})
                items = parent.get("items", [])
                ts.require(parent.get("complete") is True and len(items) == 1, "missing",
                           "Spawner milestone has no complete native readback.")
                item = items[0]
                ts.require(item.get("historical") is False and item.get("path") == expected_record
                           and item.get("identity") == identity and item["experience"]["kind"] == "milestone"
                           and item["experience"]["children"] == children, "stale",
                           "Spawner milestone does not pin the exact requested child RAM references.")
                response["child_links"] = {"outcome": "linked", "current_verified": True,
                                           "parent": {"identity": identity, "path": expected_record,
                                                      "revision": item["record_revision"]},
                                           "children": children}
            except (KeyError, TypeError, OSError, ValueError, ts.ReadError) as exc:
                response["outcome"] = "partial"
                response["child_links"] = {"outcome": "error", "code": getattr(exc, "code", "unavailable"),
                                           "message": getattr(exc, "message", str(exc)),
                                           "current_verified": False,
                                           "next": "Preserve the frozen closeout and child records; inspect exact current revisions before retrying."}
                return response
        from . import write_retry
        if write_retry.is_busy(result) and not context.get("_ram_retry_worker") and context.get("host"):
            try:
                safe_context = {key: context[key] for key in ("work_id", "session_id", "native_log") if key in context}
                queued = write_retry.enqueue(context["host"], context["root"], context["actor"], "ram-closeout",
                                             {"prepared": prepared, "context": safe_context})
                queued["worker"] = write_retry.start_worker(context["host"], context["root"])
                return {**response, "outcome": "deferred", "reason": "write-owner-busy",
                        "write_retry": queued,
                        "message": "RAM capture is durably queued; no manual retry is required unless the queue turns red."}
            except (OSError, ValueError, KeyError, TypeError, ts.ReadError) as queue_exc:
                response["write_retry"] = {"outcome": "error", "message": getattr(queue_exc, "message", str(queue_exc))}
        if result.get("outcome") == "error" and result.get("code") in {"denied", "wrong-scope"}:
            response["next_supported_action"] = (
                "Inspect the configured writer, selected host, and exact operation paths, then reuse this prepared closeout.")
        if native_log is not None and result.get("outcome") in {"completed", "already-completed"}:
            try:
                from . import companion_capture as receipt_api
                revision = result["readback"]["revision"]
                read_request = {"profile": ram.READ, "operation": "get", "identity": prepared["identity"],
                                "revision": revision, "max_bytes": 32768, "limit": 1}
                readback = receipt_api.native_ram_readback(context["root"], read_request)
                ram_reference = receipt_api.verified_ram_reference(readback, read_request)
                template = receipt_api.capture_template(native_log, "pre-final")
                ts.require(all(template[key] == prepared["native_identity"][key]
                               for key in ("thread_id", "turn_id", "stage")),
                           "stale", "Native turn changed while RAM capture completed; receipt was not reassigned.")
                unresolved = prepared["values"]["unresolved"]
                meaning = _meaning(prepared["values"])
                receipt_summary = (("Legacy closeout: " + prepared["values"]["what"]) if meaning["legacy"] else
                                   ("Request: " + meaning["request"] + " Actions: " + "; ".join(meaning["actions"]) +
                                    " Result: " + meaning["result"]))
                receipt_value = {**template, "disposition": "captured", "summary":
                    receipt_summary + " Why files changed: " + prepared["values"]["why"] +
                    " Unresolved: " + ("; ".join(unresolved) if unresolved else "none"),
                    "ram": ram_reference, "assertion": {"native_proof": "verified-readback",
                    "identity_basis": "Current active turn from explicit native lifecycle; exact current RAM owner and revision read back"}}
                response["capture_receipt"] = receipt_api.write_and_read_receipt(context["root"], receipt_value)
            except (KeyError, TypeError, OSError, RuntimeError, ValueError, ts.ReadError) as receipt_error:
                response["outcome"] = "partial"
                response["capture_receipt"] = {"outcome": "error", "code": getattr(receipt_error, "code", "unavailable"),
                                               "message": getattr(receipt_error, "message", str(receipt_error)),
                                               "retry": "Reuse the same prepared closeout while this native turn remains active."}
        verified = result.get("current_verified") is True and response["outcome"] in {"completed", "already-completed"}
        if native_log is not None:
            verified = verified and response.get("capture_receipt", {}).get("outcome") in {"recorded", "idempotent"}
        if verified and prepared.get("pending"):
            response["pending_clear"] = capture_policy.clear(
                context["root"], context["actor"], context["work_id"], context["session_id"],
                prepared["pending"], prepared["identity"]["memory_id"])
        if prepared.get('code_delivery') is not None:
            if verified:
                from .git_delivery_native import deliver_code, _release_code
                try:
                    _release_code(context['root'],prepared['code_delivery'],context['host'],
                        session=context['session_id'],work_id=context['work_id'],actor=context['actor'],selection=context.get('delivery'))
                except (OSError,ValueError,sqlite3.Error,ts.ReadError) as exc:
                    response['git_delivery'] = {'state':'unavailable','error':str(exc)}
                    return response
                response['git_delivery'] = deliver_code(context['root'], prepared['code_delivery'],
                    session=context['session_id'], work_id=context['work_id'], selection=context.get('delivery'))
            else:
                response['git_delivery'] = {'state': 'waiting-native', 'next': 'Recover original native closeout before code publication.'}
        return response
    except (KeyError, TypeError, OSError, RuntimeError, ValueError, ts.ReadError) as exc:
        return {"profile": PROFILE, "outcome": "error", "code": getattr(exc, "code", "unavailable"),
                "message": getattr(exc, "message", str(exc)), "operation": prepared}


__all__ = ["COMMANDS", "CLOSEOUT_FIELDS", "EXPERT_ROUTES", "GUIDED_COMMANDS", "LEGACY_FIELDS", "PROFILE", "answer_reuse",
           "capabilities", "catalog", "describe", "execute_closeout", "explain", "fingerprint", "help_topics",
           "pickup", "prepare_closeout", "validate_closeout"]
