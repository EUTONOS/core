"""Bounded, stateless orientation over native readers and reference owners.

Packets and receipts describe delivered context, never authority or comprehension.
No policy applicability is inferred from prose or caller-supplied identity.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from . import current_work, ram_relevance, rule_index, tokenslang as ts
from .private_scope import require_ordinary_root, require_shared
from .ram import LocalRoot
from .source import safe_path

REQUEST = "eutonos.orientation-request.v1"
PACKET = "eutonos.orientation-packet.v1"
RECEIPT = "eutonos.orientation-receipt.v1"
CURSOR = "eutonos.orientation-cursor.v1"
MAX_REQUEST_BYTES = 65536
DEFAULT_BUDGET = 32768
EFFECTS = ["Rebuildable Current Work reader cache and observation metadata may update.",
           "Explicitly configured Shared Work may fetch configured refs when due."]
FALLBACK = ["Read applicable AGENTS.md and complete native onboarding.",
            "Installed: run eutonos doctor and read the shipped checklists (eutonos/checklists/*.md). In a EUTONOS source checkout: "
            "read docs/AGENT_ONBOARDING.md, or reference/docs/AGENT_ONBOARDING.md as upstream guidance.",
            "Use eutonos-workflow pickup --root ROOT if supported (in a EUTONOS source checkout: python tools/workflow.py pickup --root ROOT); "
            "otherwise inspect native owners directly."]
REQUEST_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "profile": {"const": REQUEST}, "packet_profile": {"const": PACKET},
        "index_profile": {"const": rule_index.PROFILE},
        "mode": {"enum": ["cold", "resume"]},
        "budget_bytes": {"type": "integer", "minimum": 1024, "maximum": 131072},
        "work_id": {"type": "string", "minLength": 1},
        "caller": {"type": "object", "additionalProperties": False,
                   "properties": {"actor": {"type": "string"}, "session_id": {"type": "string"}}},
        "receipt": {"type": "object", "required": ["profile", "pins"], "additionalProperties": False,
                    "properties": {"profile": {"const": RECEIPT},
                                   "pins": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}}},
        "cursor": {"type": "object", "required": ["profile", "snapshot", "offset"], "additionalProperties": False,
                   "properties": {"profile": {"const": CURSOR}, "snapshot": {"type": "string"},
                                  "offset": {"type": "integer", "minimum": 0}}},
        "ram": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "required": ["source_id", "path", "revision", "sha256"],
            "additionalProperties": False, "properties": {
                "source_id": {"type": "string"}, "path": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1}, "sha256": {"type": "string"},
                "search_coverage": {"type": "object"}}}},
    },
}
PACKET_SCHEMA = {"type": "object", "required": ["profile", "outcome", "code", "required_context_complete"],
                 "properties": {"profile": {"const": PACKET}, "outcome": {"enum": ["orientation", "attention-required", "error"]},
                                "code": {"type": "string"}, "required_context_complete": {"type": "boolean"},
                                "entries": {"type": "array"}, "continuation": {"type": ["object", "null"]},
                                "receipt": REQUEST_SCHEMA["properties"]["receipt"]}}
PACKET_SCHEMA["properties"].update({key: {"type": "object"} for key in
                                  ("source", "task_owner", "observation", "caller", "delta", "omissions", "availability", "routes")})
PACKET_SCHEMA["properties"].update({key: {"type": "array"} for key in ("selected_work", "selected_work_ids", "gaps", "effects", "fallback")})
PACKET_SCHEMA["properties"]["continuation"] = {"anyOf": [REQUEST_SCHEMA["properties"]["cursor"], {"type": "null"}]}
PACKET_SCHEMA["properties"]["required_references_complete"] = {"type": "boolean"}


def wire_bytes(value):
    """Match escaped JSON and the CLI's native LF/CRLF stdout newline."""
    return len((json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False) + os.linesep).encode("utf-8"))


def _sha(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def guard(root):
    require_ordinary_root(root)
    require_shared(root)


def failure(code, *, budget=DEFAULT_BUDGET):
    # Never echo rejected request data, cached titles, or private paths.
    result = {"profile": PACKET, "outcome": "error", "code": code,
              "required_context_complete": False, "fallback": FALLBACK,
              "next_action": "Inspect native setup/owners and describe orient; no fallback grants authority."}
    if wire_bytes(result) > budget:
        result.pop("fallback")
    return result


def _validate(request):
    ts.require(isinstance(request, dict), "invalid", "Orientation request must be an object.")
    try:
        request_bytes = wire_bytes(request)
    except ValueError as exc:
        raise ts.ReadError("invalid", "Request contains non-finite JSON values.") from exc
    ts.require(request_bytes <= MAX_REQUEST_BYTES,
               "invalid", "Orientation request must be a bounded object.")
    ts.require(not set(request) - set(REQUEST_SCHEMA["properties"]), "unsupported-request", "Unknown request fields.")
    for key, expected in (("profile", REQUEST), ("packet_profile", PACKET), ("index_profile", rule_index.PROFILE)):
        ts.require(request.get(key, expected) == expected, "incompatible-schema", "Unsupported orientation version.")
    ts.require(request.get("mode", "cold") in {"cold", "resume"}, "invalid", "Invalid orientation mode.")
    budget = request.get("budget_bytes", DEFAULT_BUDGET)
    ts.require(type(budget) is int and 1024 <= budget <= 131072, "invalid", "Invalid byte budget.")
    if "work_id" in request:
        ts.string(request["work_id"])
    if "caller" in request:
        caller = request["caller"]
        ts.require(isinstance(caller, dict) and not set(caller) - {"actor", "session_id"}, "invalid", "Invalid caller shape.")
        for value in caller.values():
            ts.string(value)
    if "receipt" in request:
        receipt = request["receipt"]
        ts.require(isinstance(receipt, dict) and set(receipt) == {"profile", "pins"}
                   and receipt["profile"] == RECEIPT and isinstance(receipt["pins"], dict)
                   and all(isinstance(key, str) and (value is None or isinstance(value, str))
                           for key, value in receipt["pins"].items()), "incompatible-receipt", "Invalid receipt.")
    if "cursor" in request:
        cursor = request["cursor"]
        ts.require(isinstance(cursor, dict) and set(cursor) == {"profile", "snapshot", "offset"}
                   and cursor["profile"] == CURSOR and isinstance(cursor["snapshot"], str)
                   and type(cursor["offset"]) is int and cursor["offset"] >= 0,
                   "incompatible-cursor", "Invalid cursor.")
    records = request.get("ram", [])
    ts.require(isinstance(records, list) and len(records) <= 4, "invalid", "At most four exact RAM pins.")
    for pin in records:
        ts.require(isinstance(pin, dict) and {"source_id", "path", "revision", "sha256"} <= set(pin)
                   and not set(pin) - {"source_id", "path", "revision", "sha256", "search_coverage"},
                   "invalid", "Invalid RAM pin.")
        safe_path(pin["path"])
        ts.string(pin["source_id"])
        ts.require(type(pin["revision"]) is int and pin["revision"] >= 1
                   and isinstance(pin["sha256"], str) and len(pin["sha256"]) == 64
                   and all(c in "0123456789abcdef" for c in pin["sha256"])
                   and isinstance(pin.get("search_coverage", {}), dict), "invalid", "Invalid RAM pins/coverage.")
    return budget


def _compose(root, request):
    from .workflow import _catalog_task_owner, _git
    from . import ram
    local = LocalRoot(root)
    owner = _catalog_task_owner(root)  # Invalid/denied catalog never falls back.
    # Apply exactly the strictly validated catalog bytes that selected the owner.
    catalog_raw = local.read("eutonos.read.json") if owner else None
    ts.require(owner is None or catalog_raw is not None and _sha(catalog_raw) == owner["catalog_source"]["sha256"],
               "changed-input", "Catalog changed.")
    config = ts.loads(catalog_raw.decode("utf-8")) if owner else None

    def public_read(path):
        path = safe_path(path)
        if config:
            ts.require(not any(path == excluded or path.startswith(excluded + "/")
                               for excluded in config["excluded_roots"]), "denied", "Excluded reading.")
            ts.require(all(binding["visibility"] == "public" and binding["state"] == "active"
                           for binding in config["bindings"] if binding["path"] == path), "denied", "Ineligible reading.")
        # LocalRoot intentionally refuses implicit parent creation. For a read,
        # absent parents mean missing; existing links/collisions still use its guard.
        for parent in (root / path).parents:
            if parent == root:
                break
            if not parent.exists():
                return None
        return local.read(path)

    task_path = owner["path"] if owner else "docs/CURRENT_TASK.md"
    if local.read("eutonos.ram.json") is not None:
        store = ram.Store(local)
        if owner:
            ts.require(all(store.cfg[key] == owner["identity"][key] for key in owner["identity"])
                       and store.cfg["current_task"] == task_path, "conflict", "RAM/catalog owners differ.")
        else:
            task_path = store.cfg["current_task"]
    body = public_read(task_path)
    if owner:
        ts.require(body is not None, "missing", "Selected owner absent.")
        ts.require(all("sha256" not in binding or _sha(body) == binding["sha256"]
                       for binding in owner["same_path_bindings"]), "stale-owner", "Selected owner changed.")
        if "start_line" in owner["binding"]:
            ts.require(owner["binding"]["end_line"] <= len(body.decode("utf-8").splitlines()), "missing", "Owner range absent.")
    commit = _git(root, "rev-parse", "HEAD", check=False)
    commit = commit.stdout.strip() if commit.returncode == 0 else None
    pins = {"checkout_commit": commit, "catalog": owner["catalog_source"]["sha256"] if owner else None,
            "task_owner:" + task_path: _sha(body) if body is not None else None,
            "rule_index": _sha(rule_index.source_bytes()), "consumer": _sha(Path(__file__).read_bytes())}
    entries, gaps = [], []

    def entry(kind, identity, value, required=False):
        entries.append({"kind": kind, "id": identity, "required": required, "value": value})

    # Preserve canonical tiers and unresolved applicability rather than guess a policy.
    try:
        index = rule_index.load()
    except ValueError as exc:
        raise ts.ReadError("incompatible-index", "Reference index incompatible.") from exc
    registry_raw = public_read("docs/REGISTRY.json")
    pins["control:docs/REGISTRY.json"] = _sha(registry_raw) if registry_raw is not None else None
    try:  # A malformed/duplicate-key registry resolves no registry-routed references; failure stays local.
        registry = ts.loads(registry_raw.decode("utf-8")) if registry_raw is not None else {}
        ts.require(isinstance(registry, dict) and isinstance(registry.get("documents", []), list),
                   "invalid", "Registry must be an object with a documents list.")
    except (ts.ReadError, UnicodeError) as exc:
        registry = {}
        gaps.append({"kind": "registry", "state": getattr(exc, "code", "invalid"), "path": "docs/REGISTRY.json",
                     "next_action": "Repair the native registry; upstream references through it remain unresolved."})
    for row in index["rules"]:
        reference = None
        if row["owner"]["scope"] == "upstream-reference":
            pointer = row["owner"]
            path = ("reference/" + pointer["path"] if (root / "reference").is_dir() else
                    pointer["path"] if any(item.get("path") == pointer["path"] and item.get("id") == pointer["resource_id"]
                                           for item in registry.get("documents", []) if isinstance(item, dict)) else None)
            if path and path.startswith("reference/"):
                guard(root / "reference")
            raw = public_read(path) if path else None
            key = path or "unresolved/" + pointer["path"]
            pins["reference:" + key] = _sha(raw) if raw is not None else None
            reference = {"path": path, "sha256": _sha(raw) if raw is not None else None,
                         "state": "available" if raw is not None else "missing", "view": "working-tree",
                         "scope": "upstream-guidance; never consumer native work authority"}
            if raw is None and row["tier"] != "Optional":
                gaps.append({"kind": "required-reference", "id": row["id"], "state": "missing",
                             "next_action": "Resolve the canonical upstream selection or applicable native adaptation."})
        entry("rule-reference", row["id"], {**row, "source": reference,
              "applicability": "required" if row["tier"] == "Required" else "unassessed",
              "delivery": "reference-only; read the applicable native adaptation/canonical selection"}, row["tier"] != "Optional")
    entry("precedence-references", "rule-precedence", index["precedence"], True)
    for path in ("eutonos.boards.json", "eutonos.ram.json", "eutonos.shared-work.json", ".eutonos-local/shared-work.json"):
        raw = public_read(path)
        pins["control:" + path] = _sha(raw) if raw is not None else None
    for path in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md"):
        raw = public_read(path)
        pins["instruction:" + path] = _sha(raw) if raw is not None else None
        if raw is not None:
            text = raw.decode("utf-8-sig")
            for offset in range(0, len(text), 1024):
                entry("instruction", path + ":" + str(offset), {"path": path, "sha256": _sha(raw),
                      "view": "working-tree", "character_offset": offset, "text": text[offset:offset + 1024]}, True)
        elif path == "AGENTS.md":
            gaps.append({"kind": "root-instructions", "state": "missing", "next_action": "Locate native applicable instructions before dependent work."})
    gaps.append({"kind": "instruction-coverage", "state": "unassessed",
                 "next_action": "Read applicable global, ancestor, nested and harness instructions; root inventory is not complete precedence proof."})
    if body is None:
        gaps.append({"kind": "task-owner", "state": "missing", "path": task_path})
    shared = current_work.project(root, include_knowledge=False,
                                  refresh=bool(request.get("cursor") or request.get("mode") == "resume"))
    board = shared.get("boards", {})
    items = board.get("items", [])
    selected_ids = ([request["work_id"]] if "work_id" in request else board.get("work_order", {}).get("current", []))
    selected = [row for row in items if row.get("id") in selected_ids and row.get("status") != "conflict"]
    for identity in selected_ids:
        if len([row for row in selected if row["id"] == identity]) != 1:
            gaps.append({"kind": "selected-work", "state": "missing-or-conflicting", "id": identity})
    if not selected and not board.get("work_order", {}).get("configured") and body is not None:
        try:
            compatibility = ts.work(body.decode("utf-8"))
            entry("compatibility-work", "legacy-current-task", {"selection": compatibility,
                  "owner": {"path": task_path, "sha256": _sha(body)}, "qualification": "Historical compatibility owner; native Current Work takes precedence."}, True)
        except ts.ReadError:
            gaps.append({"kind": "compatibility-work", "state": "unavailable"})
    for row in selected:
        entry("selected-native-work", row["id"], row, True)
        pins["work:" + row["id"]] = _sha(row)
    for source in shared.get("sources", []):
        entry("source", source["id"], source)
        pins["source:" + source["id"]] = _sha({k: v for k, v in source.items() if k != "label"})
    for conflict in board.get("conflicts", []):
        entry("native-conflict", "conflict:" + _sha(conflict), conflict, True)
    for row in items:
        if row not in selected:
            entry("native-work", row["id"], row)
    for requested in request.get("ram", []):
        # Source view forbids the legacy current->local fallback. Validate returned pins too.
        try:
            result = ram_relevance.get(root, requested["path"], requested["revision"], requested["sha256"],
                                       view="source", source=requested["source_id"])
            # Retrieval off is a settings state, not a missing or substituted source.
            ts.require(result.get("outcome") != "disabled", "disabled", "Optional RAM retrieval is off.")
            ts.require(result.get("outcome") == "ok" and result.get("effective_view") != "local"
                       and all(result.get(key) == requested[key] for key in ("source_id", "path", "revision", "sha256")),
                       "unavailable-source", "Exact RAM source not returned.")
            observed = [source for source in shared.get("sources", []) if source["id"] == result["source_id"]]
            ts.require(len(observed) == 1, "unavailable-source", "RAM source inventory changed.")
            value = {**result, "source": observed[0], "search_coverage": {
                     "provenance": "supplied-unverified", "value": requested.get("search_coverage"),
                     "qualification": "No search was rerun; partial search never proves absence."}}
            entry("ram-experience", result["source_id"] + ":" + result["path"], value)
            pins["ram:" + result["source_id"] + ":" + result["path"]] = _sha(value)
        except (ts.ReadError, OSError, ValueError, UnicodeError) as exc:
            state = getattr(exc, "code", "unavailable")
            gaps.append({"kind": "ram-experience", "state": state, "next_action": (
                "Optional RAM retrieval is off; no record was read or substituted. Overriding needs an explicit user-requested read."
                if state == "disabled" else "Find a permitted exact source again; substituted local content was withheld.")})
    # Coverage remains native metadata, never an absence/completion claim.
    core = {"source": {"root": str(root), "commit": commit, "view": "working-tree"},
            "setup": "catalog-selected" if owner else "unadopted-compatibility",
            "task_owner": {"path": task_path, "sha256": _sha(body) if body is not None else None,
                           "catalog": owner["catalog_source"] if owner else None},
            "selected_work_ids": [row["id"] for row in selected],
            "selected_work": [{key: row.get(key) for key in ("id", "status", "next_action", "owner_path", "record_sha256", "provenance")}
                              | {"checkpoint": "See required selected-native-work entry; not interpreted from prose."} for row in selected],
            "observation": {key: shared.get(key) for key in ("status", "coverage", "freshness", "diagnostics")},
            "gaps": gaps, "effects": EFFECTS,
            "routes": {"packaged": "eutonos-workflow orient --root ROOT --request REQUEST.json",
                       "checkout": "python tools/workflow.py orient --root ROOT --request REQUEST.json",
                       "managed": "python tools/eutonos_project.py workflow orient --root ROOT --request REQUEST.json",
                       "describe": "eutonos-workflow describe orient (in a EUTONOS source checkout: python tools/workflow.py describe orient)",
                       "current_work": "eutonos-workflow expert state --root ROOT --view current --refresh "
                                       "(in a EUTONOS source checkout: python tools/workflow.py expert state --root ROOT --view current --refresh)"},
            "availability": {"source": "importable", "installed": "unknown", "authorized_writes": "not-assessed"}}
    # Refuse mixed local controls/owner/instruction observations, without a new
    # transaction or cache. Source-pinned RAM has its own native read guard.
    guard(root)
    if any(key.startswith("reference:reference/") for key in pins):
        guard(root / "reference")
    for key, expected in pins.items():
        if key.startswith(("control:", "instruction:", "task_owner:", "reference:")) and not key.startswith("reference:unresolved/"):
            raw = public_read(key.split(":", 1)[1])
            ts.require((_sha(raw) if raw is not None else None) == expected, "changed-input", "Local input changed.")
    raw = local.read("eutonos.read.json")
    ts.require((_sha(raw) if raw is not None else None) == pins["catalog"], "changed-input", "Catalog changed.")
    return core, entries, pins


def orient(root, request=None):
    """Compose a useful packet even for unknown callers; never perform native writes."""
    budget = DEFAULT_BUDGET
    try:
        guard(root)  # Before inspecting any supplied context, title or cached state.
        request = {} if request is None else deepcopy(request)
        budget = _validate(request)
        root = Path(root).resolve(strict=True)
        core, entries, pins = _compose(root, request)
        previous = request.get("receipt", {}).get("pins", {})
        changed = sorted(key for key in set(previous) | set(pins) if previous.get(key) != pins.get(key))
        if request.get("mode") == "resume":
            # Optional unchanged source observations can be omitted. Required
            # references/instructions/work are retained even for forged receipts.
            entries = [row for row in entries if row["required"] or row["kind"] != "source"
                       or previous.get("source:" + row["id"]) != pins.get("source:" + row["id"])]
        snapshot = _sha({"core": {k: v for k, v in core.items() if k != "observation"},
                         "entries": entries, "pins": pins,
                         "request": {k: v for k, v in request.items() if k not in {"cursor", "budget_bytes", "caller"}}})
        cursor = request.get("cursor")
        if cursor and cursor["snapshot"] != snapshot:
            return failure("stale-cursor", budget=budget)
        offset = cursor["offset"] if cursor else 0
        ts.require(offset <= len(entries), "invalid-cursor", "Cursor offset out of range.")
        result = {"profile": PACKET, "outcome": "orientation", "code": "partial-context" if core["gaps"] or core["observation"].get("status") != "available" else "ok", **core,
                  "fallback": FALLBACK,
                  "caller": {"confidence": "unverified" if request.get("caller") else "unknown",
                             "supplied": request.get("caller", {}), "qualification": "Supplied IDs and parent environment do not verify caller ownership."},
                  "mode": request.get("mode", "cold"), "delta": {"changed_pins": changed if previous else [],
                     "missing_previous_pins": sorted(set(previous) - set(pins)), "basis": "supplied receipt; not proof of reading"},
                  "receipt": {"profile": RECEIPT, "pins": pins}, "entries": [],
                  "reading_completion": "not-assessed; references and receipt do not prove comprehension",
                  "required_context_complete": False, "omissions": {}, "continuation": None,
                  "qualification": "Context only. Native owners control work, applicability and dependent writes. Publication/integration/acceptance remain separate."}

        def finish(end):
            omitted = entries[:offset] + entries[end:]
            result["omissions"] = {"before_page": offset, "after_page": len(entries) - end,
                                   "required": sum(row["required"] for row in omitted)}
            result["required_references_complete"] = not any(row["required"] for row in omitted)
            result["required_context_complete"] = result["required_references_complete"] and not core["gaps"]
            result["continuation"] = {"profile": CURSOR, "snapshot": snapshot, "offset": end} if end < len(entries) else None
            result["next_action"] = ("Repeat the same request with continuation as cursor; revalidation may invalidate it. Increase budget for an oversized entry."
                                     if end < len(entries) else "Read unresolved required selections and native instruction layers before dependent work.")

        finish(offset)
        if wire_bytes(result) > budget:
            small = failure("budget-too-small", budget=budget)
            small["minimum_core_bytes"] = wire_bytes(result)
            small["next_action"] = "Increase budget_bytes to minimum_core_bytes; native context remains incomplete."
            return small
        end = offset
        while end < len(entries):
            result["entries"].append(entries[end])
            finish(end + 1)
            if wire_bytes(result) > budget:
                result["entries"].pop()
                finish(end)
                if end == offset:
                    result["outcome"], result["code"] = "attention-required", "entry-exceeds-budget"
                break
            end += 1
        # Caller data and native fields may be large: never silently truncate them.
        ts.require(wire_bytes(result) <= budget, "budget-too-small", "Packet exceeded budget.")
        return result
    except (ts.ReadError, OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        return failure(getattr(exc, "code", "unavailable"), budget=budget)
