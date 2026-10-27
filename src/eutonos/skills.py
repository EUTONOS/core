"""Durable repository-owned skill discovery and explicit local use receipts.

The catalog names every eligible procedure directly.  This module does not
crawl the repository, traverse relationships, consult RAM, or infer execution.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

from . import tokenslang as ts
from .companion_history import tracking
from .effects import LocalRoot, digest
from .local_lock import local_lock
from .source import safe_path


PROFILE = "eutonos.tokenslang.skills.v1"
SEARCH_PROFILE = "eutonos.tokenslang.skill-search.v1"
USE_PROFILE = "eutonos.tokenslang.skill-use.v1"
RECEIPT_PROFILE = "eutonos.tokenslang.skill-use-receipt.v1"
CATALOG_PATH = "eutonos.skills.json"
USAGE_PATH = ".eutonos-local/skill-usage"
STATUSES = ("candidate", "usable", "stale", "retired")
OUTCOMES = ("succeeded", "failed", "unknown")
RELATIONSHIPS = ("uses_tool", "applies_to", "depends_on", "verified_by", "supersedes")
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
WORD = re.compile(r"[\w.-]+", re.UNICODE)
STOP_WORDS = frozenset("a an the how do does i we me my our to for of in on with this that please can could would should is are it use using help".split())
MAX_RECEIPT_BYTES = 32768


def _text(value: Any, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ts.ReadError("invalid", field + " must be nonempty bounded text.")
    return value


def _strings(value: Any, field: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ts.ReadError("invalid", field + " must be a bounded list.")
    result = []
    seen = set()
    for item in value:
        item = _text(item, field, maximum=1024)
        key = item.casefold()
        if key in seen:
            raise ts.ReadError("ambiguous", "Duplicate " + field + " value: " + item)
        seen.add(key)
        result.append(item)
    return result


def _under(path: str, roots: tuple[str, ...] | list[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _path_list(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 64 or (nonempty and not value):
        raise ts.ReadError("invalid", field + " must be a bounded path list.")
    result = [safe_path(_text(item, field, maximum=1024)) for item in value]
    if len({item.casefold() for item in result}) != len(result):
        raise ts.ReadError("ambiguous", "Duplicate " + field + " path.")
    return result


def _global_exclusions(local: LocalRoot) -> tuple[str, ...]:
    raw = local.read("eutonos.read.json")
    if raw is None:
        return ()
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "The live read catalog must be UTF-8.") from exc
    ts.shape(value, {"profile", "repository_id", "scope_id", "namespace", "owners",
                     "bindings", "excluded_roots", "source_paths"}, {"extensions"})
    ts.require(value["profile"] == ts.CATALOG, "unsupported", "Unsupported TokenSlang read catalog.")
    return tuple(_path_list(value["excluded_roots"], "global excluded_roots"))


def _relationship(value: Any) -> dict[str, list[str]]:
    ts.shape(value, set(RELATIONSHIPS))
    return {kind: _strings(value[kind], "relationship " + kind) for kind in RELATIONSHIPS}


def _evidence(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > 64:
        raise ts.ReadError("invalid", "evidence must be a bounded list.")
    result = []
    for item in value:
        ts.shape(item, {"type", "reference", "summary"})
        result.append({"type": _text(item["type"], "evidence type", maximum=128),
                       "reference": _text(item["reference"], "evidence reference", maximum=1024),
                       "summary": _text(item["summary"], "evidence summary")})
    return result


def _skill(value: Any, source_roots: list[str], exclusions: tuple[str, ...]) -> dict:
    required = {"id", "title", "description", "version", "status", "tools", "tags", "aliases",
                "relationships", "source", "evidence"}
    optional = {"applies_to", "prerequisites", "inputs", "constraints", "expected_outcome",
                "owner", "extensions"}
    ts.shape(value, required, optional)
    item_id = _text(value["id"], "skill id", maximum=128)
    ts.require(bool(ID.fullmatch(item_id)), "invalid", "Skill ID must be a lowercase portable identity.")
    version = _text(value["version"], "skill version", maximum=128)
    ts.require(value["status"] in STATUSES, "unsupported", "Unsupported skill status.")
    ts.shape(value["source"], {"path", "sha256"})
    expected = value["source"]["sha256"]
    ts.require(isinstance(expected, str) and bool(SHA256.fullmatch(expected)),
               "invalid", "Skill source fingerprint must be lowercase SHA-256.")
    path_error = None
    try:
        path = safe_path(_text(value["source"]["path"], "skill source path", maximum=1024))
        if not path.casefold().endswith(".md"):
            raise ts.ReadError("unsupported", "Skill source must be Markdown.")
        matching_roots = [root for root in source_roots if _under(path, [root]) and path != root]
        if len(matching_roots) != 1:
            raise ts.ReadError("denied", "Skill source must be under exactly one configured source root.")
        if _under(path, exclusions):
            raise ts.ReadError("denied", "Skill source is under an excluded root.")
    except ts.ReadError as exc:
        path = value["source"]["path"] if isinstance(value["source"].get("path"), str) else ""
        path_error = {"code": exc.code, "message": str(exc)}
    result = {
        "id": item_id,
        "title": _text(value["title"], "skill title"),
        "description": _text(value["description"], "skill description"),
        "version": version,
        "status": value["status"],
        "tools": _strings(value["tools"], "tools"),
        "tags": _strings(value["tags"], "tags"),
        "aliases": _strings(value["aliases"], "aliases"),
        "relationships": _relationship(value["relationships"]),
        "source": {"path": path, "sha256": expected},
        "evidence": _evidence(value["evidence"]),
    }
    for field in ("applies_to", "prerequisites", "inputs", "constraints"):
        if field in value:
            result[field] = _strings(value[field], field)
    for field in ("expected_outcome", "owner"):
        if field in value:
            result[field] = _text(value[field], field)
    if "extensions" in value:
        result["extensions"] = deepcopy(value["extensions"])
    if path_error:
        result["_path_error"] = path_error
    return result


def _catalog(root: str | Path) -> tuple[LocalRoot, dict | None]:
    local = LocalRoot(root)
    raw = local.read(CATALOG_PATH)
    if raw is None:
        return local, None
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "The skills catalog must be UTF-8.") from exc
    ts.shape(value, {"profile", "source_roots", "excluded_roots", "skills"}, {"extensions"})
    ts.require(value["profile"] == PROFILE, "unsupported", "Unsupported skills catalog profile.")
    source_roots = _path_list(value["source_roots"], "source_roots", nonempty=True)
    for index, left in enumerate(source_roots):
        ts.require(not any(_under(left, [right]) or _under(right, [left])
                           for right in source_roots[index + 1:]),
                   "ambiguous", "Skill source roots must not overlap.")
    configured_exclusions = _path_list(value["excluded_roots"], "excluded_roots")
    exclusions = tuple(dict.fromkeys([*_global_exclusions(local), *configured_exclusions]))
    if not isinstance(value["skills"], list) or len(value["skills"]) > 512:
        raise ts.ReadError("invalid", "skills must be a bounded list.")
    items = [_skill(item, source_roots, exclusions) for item in value["skills"]]
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        duplicate = next(item_id for item_id in ids if ids.count(item_id) > 1)
        raise ts.ReadError("ambiguous", "Duplicate skill ID: " + duplicate)
    return local, {"source_roots": source_roots, "excluded_roots": list(exclusions),
                   "items": items, "sha256": digest(raw)}


def _source(local: LocalRoot, item: dict, exclusions: tuple[str, ...]) -> tuple[dict, bytes | None, str | None]:
    expected = item["source"]["sha256"]
    path = item["source"]["path"]
    if item.get("_path_error"):
        error = item["_path_error"]
        state = "excluded" if error["code"] == "denied" else "invalid"
        return {"path": path, "sha256": expected, "state": state, "current_sha256": None}, None, error["message"]
    try:
        body = LocalRoot(local.root, exclusions).read(path)
        if body is None:
            return {"path": path, "sha256": expected, "state": "missing", "current_sha256": None}, None, "Skill source is missing: " + path
        current = digest(body)
        try:
            body.decode("utf-8")
        except UnicodeError:
            return {"path": path, "sha256": expected, "state": "invalid", "current_sha256": current}, None, "Skill procedure must be UTF-8 Markdown: " + path
        state = "fresh" if current == expected else "stale"
        diagnostic = None if state == "fresh" else "Skill source fingerprint changed: " + path
        return {"path": path, "sha256": expected, "state": state, "current_sha256": current}, body, diagnostic
    except (OSError, UnicodeError, ValueError, TypeError, ts.ReadError) as exc:
        code = getattr(exc, "code", "invalid")
        state = "excluded" if code == "denied" else "invalid" if code in {"invalid", "unsafe-path", "ambiguous", "unsupported"} else "unavailable"
        return {"path": path, "sha256": expected, "state": state, "current_sha256": None}, None, str(exc)


def _instant(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False


def _request(value: Any) -> tuple[dict, str]:
    ts.shape(value, {"profile", "event_id", "skill_id", "skill_version", "skill_sha256", "outcome"}, {"evidence"})
    ts.require(value["profile"] == USE_PROFILE, "unsupported", "Unsupported skill-use profile.")
    event_id = _text(value["event_id"], "event_id", maximum=256)
    skill_id = _text(value["skill_id"], "skill_id", maximum=128)
    ts.require(bool(ID.fullmatch(skill_id)), "invalid", "Invalid skill ID.")
    version = _text(value["skill_version"], "skill_version", maximum=128)
    sha = value["skill_sha256"]
    ts.require(isinstance(sha, str) and bool(SHA256.fullmatch(sha)), "invalid", "Invalid skill SHA-256.")
    ts.require(value["outcome"] in OUTCOMES, "unsupported", "Unsupported skill-use outcome.")
    evidence = _strings(value.get("evidence", []), "usage evidence", maximum=16)
    canonical = {"profile": USE_PROFILE, "event_id": event_id, "skill_id": skill_id,
                 "skill_version": version, "skill_sha256": sha, "outcome": value["outcome"],
                 "evidence": evidence}
    encoded = ts.encoded(canonical)
    ts.require(len(encoded) <= MAX_RECEIPT_BYTES, "overflow", "Skill-use request exceeds 32 KiB.")
    return canonical, hashlib.sha256(encoded).hexdigest()


def _receipt(value: Any, filename: str | None = None) -> dict:
    ts.shape(value, {"profile", "event_id", "request_sha256", "skill_id", "skill_version",
                     "skill_sha256", "outcome", "evidence", "recorded_at", "qualification"})
    ts.require(value["profile"] == RECEIPT_PROFILE, "unsupported", "Unsupported skill-use receipt profile.")
    request = {"profile": USE_PROFILE, "event_id": value["event_id"], "skill_id": value["skill_id"],
               "skill_version": value["skill_version"], "skill_sha256": value["skill_sha256"],
               "outcome": value["outcome"], "evidence": value["evidence"]}
    canonical, request_sha = _request(request)
    ts.require(value["request_sha256"] == request_sha, "conflict", "Receipt differs from its request fingerprint.")
    ts.require(_instant(value["recorded_at"]), "invalid", "Receipt timestamp must include an offset.")
    _text(value["qualification"], "receipt qualification")
    if filename is not None:
        expected_name = hashlib.sha256(canonical["event_id"].encode("utf-8")).hexdigest() + ".json"
        ts.require(filename == expected_name, "conflict", "Receipt filename differs from its event identity.")
    return value


def _read_receipt(path: Path) -> dict:
    info = path.lstat()
    ts.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
               and not getattr(info, "st_file_attributes", 0) & 0x400,
               "unsafe-path", "Usage receipt must be an ordinary singly-linked file.")
    ts.require(info.st_size <= MAX_RECEIPT_BYTES, "overflow", "Usage receipt exceeds 32 KiB.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    ts.require(len(raw) <= MAX_RECEIPT_BYTES, "overflow", "Usage receipt exceeds 32 KiB.")
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("invalid", "Usage receipt must be UTF-8.") from exc
    return _receipt(value, path.name)


def _usage(root: Path, items: list[dict]) -> tuple[dict, dict[str, dict]]:
    try:
        enabled = _tracking_enabled(root)
    except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        coverage = {"status": "partial", "records": None, "invalid_records": None,
                    "diagnostics": ["Usage tracking state is unavailable: " + str(exc)]}
        return coverage, {}
    if not enabled:
        return {"status": "disabled", "records": None, "invalid_records": None, "diagnostics": []}, {}
    directory = root / USAGE_PATH
    diagnostics = []
    receipts = []
    with local_lock(root, create=False):
        if not directory.exists():
            return {"status": "missing", "records": None, "invalid_records": None, "diagnostics": []}, {}
        try:
            info = directory.lstat()
            ts.require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                       "unsafe-path", "Usage store must be an ordinary directory.")
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except (OSError, ts.ReadError) as exc:
            return {"status": "partial", "records": None, "invalid_records": None,
                    "diagnostics": ["Usage store is unavailable: " + str(exc)]}, {}
        for path in entries:
            if not path.name.endswith(".json"):
                diagnostics.append("Unexpected usage-store entry: " + path.name)
                continue
            try:
                receipts.append(_read_receipt(path))
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
                diagnostics.append("Invalid usage receipt " + path.name + ": " + str(exc))
    coverage_status = "partial" if diagnostics else "complete"
    coverage = {"status": coverage_status, "records": len(receipts),
                "invalid_records": len(diagnostics), "diagnostics": diagnostics}
    if diagnostics:
        return coverage, {}
    index = {}
    identities = {(item["id"], item["version"], item["source"]["sha256"]) for item in items}
    for item_id, version, sha in identities:
        selected = [receipt for receipt in receipts if (receipt["skill_id"], receipt["skill_version"], receipt["skill_sha256"])
                    == (item_id, version, sha)]
        index[item_id] = {
            "count": len(selected),
            "success_count": sum(receipt["outcome"] == "succeeded" for receipt in selected),
            "failure_count": sum(receipt["outcome"] == "failed" for receipt in selected),
            "last_used": max((receipt["recorded_at"] for receipt in selected), default=None),
            "coverage": "complete",
        }
    return coverage, index


def _unknown_usage(coverage: str) -> dict:
    return {"count": None, "success_count": None, "failure_count": None,
            "last_used": None, "coverage": coverage}


def project(root: str | Path) -> dict:
    """Project the configured catalog and retained explicit-use coverage."""
    base = {"profile": PROFILE, "status": "available", "items": [], "diagnostics": [],
            "usage_coverage": {"status": "missing", "records": None, "invalid_records": None,
                               "diagnostics": []}}
    try:
        local, catalog = _catalog(root)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        return {**base, "status": "error", "diagnostics": [str(exc)]}
    if catalog is None:
        return {**base, "status": "not-configured"}
    diagnostics = []
    items = []
    exclusions = tuple(catalog["excluded_roots"])
    for definition in catalog["items"]:
        source, _, diagnostic = _source(local, definition, exclusions)
        item = {key: deepcopy(value) for key, value in definition.items() if not key.startswith("_") and key != "source"}
        item["source"] = source
        if diagnostic:
            diagnostics.append(definition["id"] + ": " + diagnostic)
        items.append(item)
    coverage, usage = _usage(local.root, items)
    for item in items:
        item["usage"] = usage.get(item["id"], _unknown_usage(coverage["status"]))
    status = "empty" if not items else "partial" if diagnostics or coverage["status"] == "partial" else "available"
    return {"profile": PROFILE, "status": status, "items": items, "diagnostics": diagnostics,
            "usage_coverage": coverage, "catalog": {"path": CATALOG_PATH, "sha256": catalog["sha256"],
                                                       "source_roots": catalog["source_roots"],
                                                       "excluded_roots": catalog["excluded_roots"]}}


def _terms(value: str) -> list[str]:
    if not isinstance(value, str) or len(value) > 4096:
        raise ts.ReadError("invalid", "query must be bounded text.")
    return list(dict.fromkeys(match.group(0).casefold() for match in WORD.finditer(value)
                              if match.group(0).casefold() not in STOP_WORDS))


def _rank(item: dict, terms: list[str]) -> tuple[int, list[str]] | None:
    fields = {
        "id": [item["id"]], "title": [item["title"]], "aliases": item["aliases"],
        "tags": item["tags"], "description": [item["description"]], "tools": item["tools"],
        "applies_to": [*item.get("applies_to", []), *item["relationships"]["applies_to"]],
    }
    normalized = {key: [value.casefold() for value in values] for key, values in fields.items()}
    matched = []
    score = 0
    weights = {"id": 90, "title": 80, "aliases": 70, "tags": 60,
               "description": 30, "tools": 25, "applies_to": 20}
    for term in terms:
        best = 0
        for field, values in normalized.items():
            if any(term == value for value in values):
                best = max(best, weights[field] + 10)
            elif any(term in value for value in values):
                best = max(best, weights[field])
        if not best:
            return None
        matched.append(term)
        score += best
    return score, matched


def search(root: str | Path, query: str = "", tool: str | None = None, status: str | None = None,
           limit: int = 20, offset: int = 0) -> dict:
    """Search task terms with exact optional tool/status filters and pagination."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ts.ReadError("invalid", "limit must be 1..100 and offset must be nonnegative integers.")
    if tool is not None:
        tool = _text(tool, "tool", maximum=1024)
    if status is not None and status not in STATUSES:
        raise ts.ReadError("unsupported", "Unsupported skill status filter.")
    terms = _terms(query)
    view = project(root)
    rows = []
    for item in view["items"]:
        explicit_tools = [*item["tools"], *item["relationships"]["uses_tool"]]
        if tool is not None and tool.casefold() not in {value.casefold() for value in explicit_tools}:
            continue
        if status is not None and item["status"] != status:
            continue
        ranked = _rank(item, terms)
        if ranked is None:
            continue
        score, matched = ranked
        rows.append({**deepcopy(item), "match": {"score": score, "terms": matched}})
    rows.sort(key=lambda item: (-item["match"]["score"], item["title"].casefold(), item["id"]))
    total = len(rows)
    return {"profile": SEARCH_PROFILE, "outcome": "search", "catalog_status": view["status"],
            "query": query, "tool": tool, "status": status, "offset": offset, "limit": limit,
            "total": total, "items": rows[offset:offset + limit],
            "diagnostics": view["diagnostics"], "usage_coverage": view["usage_coverage"]}


def get(root: str | Path, skill_id: str, version: str | None = None, sha256: str | None = None) -> dict:
    """Read one usable procedure only when its explicit source is fresh."""
    skill_id = _text(skill_id, "skill id", maximum=128)
    if version is not None:
        version = _text(version, "skill version", maximum=128)
    if sha256 is not None and (not isinstance(sha256, str) or not SHA256.fullmatch(sha256)):
        raise ts.ReadError("invalid", "Invalid requested skill SHA-256.")
    try:
        local, catalog = _catalog(root)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        return {"profile": PROFILE, "outcome": "read", "status": "error", "id": skill_id,
                "diagnostics": [str(exc)]}
    if catalog is None:
        return {"profile": PROFILE, "outcome": "read", "status": "not-configured", "id": skill_id,
                "diagnostics": []}
    item = next((item for item in catalog["items"] if item["id"] == skill_id), None)
    if item is None:
        return {"profile": PROFILE, "outcome": "read", "status": "not-found", "id": skill_id,
                "diagnostics": []}
    base = {"profile": PROFILE, "outcome": "read", "id": skill_id,
            "version": item["version"], "sha256": item["source"]["sha256"], "diagnostics": []}
    if version is not None and version != item["version"]:
        return {**base, "status": "version-mismatch"}
    if sha256 is not None and sha256 != item["source"]["sha256"]:
        return {**base, "status": "hash-mismatch"}
    source, body, diagnostic = _source(local, item, tuple(catalog["excluded_roots"]))
    if item["status"] != "usable":
        return {**base, "status": "not-usable", "skill_status": item["status"], "source": source}
    if source["state"] != "fresh" or body is None:
        return {**base, "status": source["state"], "source": source,
                "diagnostics": [diagnostic] if diagnostic else []}
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return {**base, "status": "invalid", "source": source,
                "diagnostics": ["Skill procedure must be UTF-8 Markdown."]}
    return {**base, "status": "available", "source": source, "body": text,
            "qualification": "Exact fresh procedure source; reading does not execute it or grant authority."}


def _existing(final: Path, request_sha: str) -> dict | None:
    if not final.exists():
        return None
    value = _read_receipt(final)
    ts.require(value["request_sha256"] == request_sha, "conflict",
               "The event ID is already bound to different skill-use content.")
    return value


def _ordinary_dir(path: Path, label: str) -> None:
    info = path.lstat()
    ts.require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
               "unsafe-path", label + " must be an ordinary directory.")


def _tracking_enabled(root: Path) -> bool:
    local_state = root / ".eutonos-local"
    locks = local_state / ".locks"
    if local_state.exists():
        _ordinary_dir(local_state, "Local metadata root")
    if locks.exists():
        _ordinary_dir(locks, "Local metadata lock root")
    history = local_state / "companion-history.json"
    if history.exists():
        LocalRoot(root).path(".eutonos-local/companion-history.json")
    return tracking(root)["enabled"]


def record_use(root: str | Path, request: dict) -> dict:
    """Record one caller-asserted use with exact idempotent event identity."""
    canonical, request_sha = _request(request)
    local = LocalRoot(root)
    event_name = hashlib.sha256(canonical["event_id"].encode("utf-8")).hexdigest() + ".json"
    directory = local.root / USAGE_PATH
    final = directory / event_name
    if not _tracking_enabled(local.root):
        return {"profile": RECEIPT_PROFILE, "outcome": "disabled", "receipt": None,
                "qualification": "Usage tracking is disabled; no zero count is asserted."}
    local_state = local.root / ".eutonos-local"
    locks = local_state / ".locks"
    if local_state.exists():
        _ordinary_dir(local_state, "Local metadata root")
    if locks.exists():
        _ordinary_dir(locks, "Local metadata lock root")
    with local_lock(local.root):
        _ordinary_dir(local_state, "Local metadata root")
        _ordinary_dir(locks, "Local metadata lock root")
        if directory.exists():
            _ordinary_dir(directory, "Skill usage store")
        existing = _existing(final, request_sha)
        if existing is not None:
            return {"profile": RECEIPT_PROFILE, "outcome": "already-recorded", "receipt": existing,
                    "path": USAGE_PATH + "/" + event_name}
        selected = get(local.root, canonical["skill_id"], canonical["skill_version"], canonical["skill_sha256"])
        ts.require(selected["status"] == "available", "stale",
                   "Record-use requires the exact usable fresh skill version and hash.")
        if not directory.exists():
            directory.mkdir()
        receipt = {"profile": RECEIPT_PROFILE, **canonical, "request_sha256": request_sha,
                   "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                   "qualification": "Caller-reported use outcome; this receipt does not independently prove execution or success."}
        # Reorder to one canonical object; the profile from the request must not replace the receipt profile.
        receipt["profile"] = RECEIPT_PROFILE
        encoded = ts.encoded(receipt) + b"\n"
        ts.require(len(encoded) <= MAX_RECEIPT_BYTES, "overflow", "Skill-use receipt exceeds 32 KiB.")
        descriptor, temporary = tempfile.mkstemp(prefix=".skill-use-", suffix=".tmp", dir=directory)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, final)
            except FileExistsError:
                existing = _existing(final, request_sha)
                ts.require(existing is not None, "conflict", "Concurrent skill-use receipt is unavailable.")
                return {"profile": RECEIPT_PROFILE, "outcome": "already-recorded", "receipt": existing,
                        "path": USAGE_PATH + "/" + event_name}
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        readback = _read_receipt(final)
        ts.require(readback["request_sha256"] == request_sha, "conflict", "Skill-use readback differs.")
        return {"profile": RECEIPT_PROFILE, "outcome": "recorded", "receipt": readback,
                "path": USAGE_PATH + "/" + event_name}


def profile_description() -> dict:
    relationships = {kind: [] for kind in RELATIONSHIPS}
    skill = {"id": "example-skill", "title": "Example procedure", "description": "Complete one bounded job.",
             "version": "1.0.0", "status": "usable", "tools": ["example-tool"], "tags": ["example"],
             "aliases": ["example task"], "relationships": relationships,
             "source": {"path": "docs/skills/EXAMPLE.md", "sha256": "<lowercase SHA-256>"},
             "evidence": [{"type": "test", "reference": "tests/test_example.py", "summary": "Focused check passed."}]}
    return {"profile": PROFILE, "outcome": "profile", "catalog": {"profile": PROFILE,
            "source_roots": ["docs/skills"], "excluded_roots": [], "skills": [skill]},
            "use_request": {"profile": USE_PROFILE, "event_id": "caller-event-id", "skill_id": "example-skill",
                            "skill_version": "1.0.0", "skill_sha256": "<source SHA-256>",
                            "outcome": "succeeded", "evidence": ["tests/test_example.py"]},
            "statuses": list(STATUSES), "outcomes": list(OUTCOMES),
            "commands": {"list": "python -m eutonos.skills_cli list --root ROOT",
                         "search": "python -m eutonos.skills_cli search TASK --root ROOT --tool TOOL --status usable",
                         "read": "python -m eutonos.skills_cli read ID --root ROOT --version VERSION --sha256 SHA256",
                         "record_use": "python -m eutonos.skills_cli record-use --root ROOT --request REQUEST.json"},
            "qualification": "Catalog and receipts are repository-local and RAM-independent; relationships are returned without traversal."}


__all__ = ["CATALOG_PATH", "OUTCOMES", "PROFILE", "RECEIPT_PROFILE", "RELATIONSHIPS",
           "SEARCH_PROFILE", "STATUSES", "USE_PROFILE", "get", "profile_description", "project",
           "record_use", "search"]
