"""Optional compact relevance over eligible local RAM records."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .private_scope import require_shared
from . import ram, settings
from . import tokenslang as ts
from .effects import LocalRoot, digest
from .local_lock import local_lock
from .source import GitView, safe_path

PROFILE = "eutonos.ram-relevance.v1"
SEARCH = "eutonos.ram-relevance.search.v1"
GET = "eutonos.ram-relevance.get.v1"
FEEDBACK = "eutonos.ram-relevance.feedback.v1"
FEEDBACK_STORE = "eutonos.ram-relevance-feedback-store.v1"
FEEDBACK_PATH = ".eutonos-local/ram-relevance-feedback.json"
_HASH = re.compile(r"[0-9a-f]{64}")
_TERM = re.compile(r"[^\W_]+", re.UNICODE)


def _catalog(local):
    body = local.read("eutonos.read.json")
    if body is None:
        return None
    value = ts.loads(body.decode("utf-8"))
    ts.shape(value, {"profile", "repository_id", "scope_id", "namespace", "owners",
                     "bindings", "excluded_roots", "source_paths"}, {"extensions"})
    ts.require(value["profile"] == ts.CATALOG, "unsupported", "Unsupported TokenSlang catalog.")
    denied = set()
    for binding in ts.sequence(value["bindings"]):
        ts.shape(binding, {"resource_id", "path", "kind", "label", "visibility", "state"},
                 {"aliases", "sha256", "start_line", "end_line", "extensions"})
        path = safe_path(binding["path"])
        if binding["visibility"] != "public" or binding["state"] != "active":
            denied.add(path)
    return value, tuple(denied), tuple(safe_path(path) for path in ts.sequence(value["excluded_roots"]))


def open_store(root):
    """Open the local RAM store with current catalog visibility controls."""
    require_shared(root)
    local = LocalRoot(Path(root).resolve())
    catalog = _catalog(local)
    store = ram.Store(local, denied_paths=() if catalog is None else catalog[1])
    if catalog is not None:
        value, _, excluded = catalog
        ts.require(all(store.cfg[key] == value[key] for key in ("repository_id", "scope_id", "namespace")),
                   "wrong-scope", "RAM and TokenSlang catalog identities differ.")
        store.relevance_excluded_roots = excluded
    else:
        store.relevance_excluded_roots = ()
    task = local.read(store.cfg["current_task"])
    ts.require(task is not None and store.cfg["current_task"] not in store.denied_paths,
               "denied", "Current Task source is unavailable.")
    ts.require(not _under(store.config_path, store.relevance_excluded_roots)
               and not _under(store.cfg["current_task"], store.relevance_excluded_roots),
               "denied", "RAM configuration or Current Task is excluded by the catalog.")
    return store


def _under(path, roots):
    return any(path == root or path.startswith(root + "/") for root in roots)


def _eligible(store, path, record, read, seen=None):
    store.eligible(path, record, read)
    ts.require(not _under(path, store.relevance_excluded_roots),
               "denied", "RAM record is excluded by the catalog.")
    seen = set() if seen is None else seen
    if path in seen:
        return
    seen.add(path)
    for version in record["versions"][-1:]:
        paths = [version["work"]["path"], *[item["path"] for item in version["sources"]]]
        ts.require(not any(_under(item, store.relevance_excluded_roots) for item in paths),
                   "denied", "RAM relies on an excluded catalog source.")
        for child in version["children"]:
            _eligible(store, child["path"], store.load(child["path"], read), read, seen)


def _paths(store):
    view = GitView(store.root.root, "HEAD")
    selected = set()
    for raw in view.iter_nul("ls-files", "--cached", "--others", "--exclude-standard", "-z",
                             "--", ":(literal)" + store.cfg["store_root"]):
        try:
            path = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ts.ReadError("unsupported", "RAM record path must be UTF-8.") from exc
        if path.endswith(".json") and store.inside(path):
            selected.add(safe_path(path))
    return sorted(selected)


def _stable_reader(store):
    observed = {}
    def read(path):
        body = store.root.read(path)
        value = digest(body)
        ts.require(path not in observed or observed[path] == value,
                   "stale", "RAM or source changed during relevance retrieval.")
        observed[path] = value
        return body
    return read, observed


def _freshness(version, read):
    result = []
    for source in version["sources"]:
        try:
            body = read(source["path"])
            state = "missing" if body is None else "match" if digest(body) == source["sha256"] else "stale"
        except (OSError, ts.ReadError):
            state = "unknown"
        normalized = {"match": "matching", "stale": "changed"}.get(state, state)
        result.append({"path": source["path"], "state": state, "source_freshness": normalized})
    return result


def _terms(value):
    return list(dict.fromkeys(match.group(0).casefold() for match in _TERM.finditer(value)))


def _candidate(path, record, observed, terms):
    version = record["versions"][-1]
    identity = " ".join(str(value) for value in record["identity"].values()) + " " + version["kind"]
    sources = " ".join(source["path"] for source in version["sources"])
    fields = {"summary": version["summary"].casefold(), "identity": identity.casefold(),
              "source-path": sources.casefold()}
    matched = {name: [term for term in terms if term in text] for name, text in fields.items()}
    matched = {name: values for name, values in matched.items() if values}
    if terms and not matched:
        return None
    score = sum(len(matched.get(name, ())) * weight for name, weight in
                (("summary", 5), ("identity", 3), ("source-path", 2)))
    preview = " ".join(version["summary"].split())
    summary_terms = matched.get("summary", [])
    if summary_terms:
        positions = [preview.casefold().find(term) for term in summary_terms]
        first = min(position for position in positions if position >= 0)
        if first > 180:
            preview = "..." + preview[max(0, first - 80):]
    if len(preview) > 280:
        preview = preview[:277].rstrip() + "..."
    return {"path": path, "revision": record["revision"], "sha256": observed[path],
            "content_revision": version["revision"], "identity": record["identity"],
            "kind": version["kind"], "preview": preview,
            "matched_terms": sorted({term for values in matched.values() for term in values}),
            "ranking_basis": [{"field": name, "terms": values} for name, values in matched.items()],
            "score": score}


def _shared_snapshot(root, query, explicit, include_archived, view, source):
    """Rank one Current Work RAM inventory without changing local write APIs."""
    from . import current_work, current_work_knowledge
    inventory = current_work.sources(root)
    usable = [item for item in inventory["sources"] if item.get("status") in
              {None, "observed", "available", "equivalent-ref", "already-on-main"}]
    if view == "current" and not usable:
        local = search_snapshot(root, query, explicit, include_archived, view="local")
        return {**local, "effective_view": "local", "view": view,
                "diagnostics": ["Shared source inventory unavailable; selected local RAM was used."]}
    knowledge = current_work_knowledge.project(root, inventory["sources"],
                                               view=view, source=source, modules=("ram",),
                                               include_archived=include_archived)
    terms = _terms(query)
    candidates = []
    for row in knowledge.get("ram", {}).get("records", ()):
        for variant in row["variants"]:
            if variant["state"] == "archived" and not include_archived:
                continue
            fake = {"identity": variant["identity"], "revision": variant["revision"],
                    "versions": [{"revision": variant["content_revision"],
                                  "summary": variant["summary"], "kind": variant["kind"], "sources": []}]}
            item = _candidate(variant["path"], fake, {variant["path"]: variant["sha256"]}, terms)
            if item is None:
                continue
            item.update(source_id=variant["source_id"], sources=variant["sources"],
                        memory_conflict=row["conflict"], freshness=[],
                        semantic_applicability=ram.applicability())
            candidates.append(item)
    candidates.sort(key=lambda item: (-item["score"], item["path"], item["sha256"]))
    return {"profile": SEARCH, "outcome": "partial" if knowledge["status"] == "partial" else "ok",
            "results": candidates, "total": len(candidates),
            "basis": "source-pinned lexical matches; expand for source freshness and semantic applicability",
            "explicit": explicit, "include_archived": include_archived, "view": view,
            "selected_source": source, "coverage": knowledge["coverage"], "diagnostics": knowledge["diagnostics"]}


def search_snapshot(root, query, explicit=False, include_archived=False, *, view="local", source=None):
    """Freeze one complete eligible result set for stable user-driven paging."""
    ts.require(isinstance(query, str), "invalid", "RAM query must be text.")
    ts.require(type(explicit) is bool and type(include_archived) is bool,
               "invalid", "RAM override/archive selection must be explicit.")
    terms = _terms(query)
    ts.require(bool(terms) or explicit, "incomplete", "Provide a query, or explicitly browse the RAM library.")
    if settings.status(root)["ram"]["retrieval"] == "off" and not explicit:
        return {"profile": SEARCH, "outcome": "disabled", "results": [], "total": 0,
                "basis": "optional RAM retrieval is off"}
    if view != "local":
        return _shared_snapshot(root, query, explicit, include_archived, view, source)
    ts.require(source is None, "invalid", "Local RAM view has no source pin.")
    store = open_store(root)
    read, observed = _stable_reader(store)
    candidates = []
    for path in _paths(store):
        body = read(path)
        if body is None:
            continue
        try:
            record = store.load(path, read)
            _eligible(store, path, record, read)
        except ts.ReadError as exc:
            if exc.code in {"denied", "stale", "missing"}:
                continue
            raise
        if record["controls"]["state"] == "archived" and not include_archived:
            continue
        item = _candidate(path, record, observed, terms)
        if item is not None:
            candidates.append(item)
    candidates.sort(key=lambda item: (-item["score"], item["path"], -item["revision"]))
    freshness_cache = {}
    for item in candidates:
        version = store.load(item["path"], read)["versions"][-1]
        states = []
        for source in version["sources"]:
            key = (source["path"], source["sha256"])
            if key not in freshness_cache:
                freshness_cache[key] = _freshness({"sources": [source]}, read)[0]
            states.append(dict(freshness_cache[key]))
        item["freshness"] = states
        item["semantic_applicability"] = ram.applicability(version.get("applicability"))
    return {"profile": SEARCH, "outcome": "ok", "results": candidates, "total": len(candidates),
            "basis": "lexical matches in summary, stable identity and source paths; freshness is byte comparison, not truth",
            "explicit": explicit, "include_archived": include_archived}


def snapshot_page(value, limit=5, offset=0):
    """Project a page from an already frozen relevance result set."""
    ts.require(isinstance(value, dict) and value.get("profile") == SEARCH
               and isinstance(value.get("results"), list), "invalid", "Invalid RAM search snapshot.")
    ts.require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and offset >= 0,
               "invalid", "RAM page bounds are invalid.")
    items = value["results"]
    page = items[offset:offset + limit]
    return {**value, "results": page, "total": len(items), "offset": offset,
            "page_size": limit,
            "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
            "previous_offset": max(0, offset - limit) if offset > 0 else None}


def search(root, query, limit=5, offset=0, explicit=False, include_archived=False, *, view="local", source=None):
    return snapshot_page(search_snapshot(root, query, explicit, include_archived,
                                         view=view, source=source), limit, offset)


def get(root, path, revision, sha256, explicit=False, include_archived=False, *, view="local", source=None):
    path = safe_path(path)
    ts.require(type(revision) is int and revision >= 1 and isinstance(sha256, str) and bool(_HASH.fullmatch(sha256)),
               "invalid", "Exact RAM expansion needs a record revision and SHA-256.")
    ts.require(type(explicit) is bool and type(include_archived) is bool,
               "invalid", "RAM override/archive selection must be explicit.")
    if settings.status(root)["ram"]["retrieval"] == "off" and not explicit:
        return {"profile": GET, "outcome": "disabled", "path": path,
                "qualification": "optional RAM retrieval is off; use an explicit user-requested read to override"}
    if view != "local":
        from . import current_work, current_work_knowledge
        inventory = current_work.sources(root)
        usable = [item for item in inventory["sources"] if item.get("status") in
                  {None, "observed", "available", "equivalent-ref", "already-on-main"}]
        if view == "current" and not usable:
            local = get(root, path, revision, sha256, explicit, include_archived, view="local")
            return {**local, "effective_view": "local", "view": view}
        ts.require(isinstance(source, str) and source, "invalid", "Exact result source ID required.")
        result = current_work_knowledge.read_ram(root, inventory["sources"], path=path,
                                                 revision=revision, sha256=sha256,
                                                 source_id=source, include_archived=include_archived)
        return {"profile": GET, "outcome": "ok", **result}
    ts.require(source is None, "invalid", "Local RAM view has no source pin.")
    store = open_store(root)
    ts.require(store.inside(path), "wrong-scope", "Record is outside selected RAM storage.")
    read, observed = _stable_reader(store)
    record = store.load(path, read)
    _eligible(store, path, record, read)
    ts.require(record["controls"]["state"] != "archived" or include_archived,
               "archived", "Explicitly include archived history.")
    ts.require(record["revision"] == revision and observed[path] == sha256,
               "stale", "RAM result changed; search again before expansion.")
    version = record["versions"][-1]
    return {"profile": GET, "outcome": "ok", "path": path, "revision": revision,
            "sha256": sha256, "identity": record["identity"], "controls": record["controls"],
            "content": version, "freshness": _freshness(version, read),
            "semantic_applicability": ram.applicability(version.get("applicability")),
            "qualification": "Source freshness compares current bytes. Semantic applicability is separately assessed; neither establishes truth or acceptance."}


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="ram-feedback-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def feedback(root, request):
    ts.shape(request, {"profile", "idempotency_key", "path", "revision", "sha256", "actor", "assessment"},
             {"reason"})
    ts.require(request["profile"] == FEEDBACK, "unsupported", "Unsupported RAM feedback profile.")
    for key in ("idempotency_key", "actor"):
        ts.require(isinstance(request[key], str) and 0 < len(request[key].encode("utf-8")) <= 256,
                   "invalid", "RAM feedback identity is invalid.")
    ts.require(request["assessment"] in {"useful", "irrelevant"}, "unsupported", "Unsupported RAM feedback value.")
    reason = request.get("reason")
    ts.require(reason is None or isinstance(reason, str) and len(reason.encode("utf-8")) <= 1024,
               "invalid", "RAM feedback reason is invalid.")
    # Eligibility and exact record identity are rechecked before metadata is accepted.
    get(root, request["path"], request["revision"], request["sha256"], explicit=True)
    root = Path(root).resolve()
    payload = {key: request[key] for key in ("path", "revision", "sha256", "actor", "assessment")}
    if reason is not None:
        payload["reason"] = reason
    payload_hash = digest(ts.encoded(payload))
    feedback_id = hashlib.sha256(request["idempotency_key"].encode("utf-8")).hexdigest()
    target = root / FEEDBACK_PATH
    with local_lock(root):
        if target.exists():
            ledger = json.loads(target.read_text(encoding="utf-8"))
            ts.require(isinstance(ledger, dict) and ledger.get("profile") == FEEDBACK_STORE
                       and type(ledger.get("revision")) is int and isinstance(ledger.get("entries"), dict),
                       "invalid", "RAM feedback metadata is invalid.")
        else:
            ledger = {"profile": FEEDBACK_STORE, "revision": 0, "entries": {}}
        prior = ledger["entries"].get(feedback_id)
        if prior is not None:
            ts.require(prior["payload_sha256"] == payload_hash, "conflict",
                       "Feedback idempotency key already names different content.")
            return {"profile": FEEDBACK, "outcome": "unchanged", "feedback_id": feedback_id,
                    "revision": ledger["revision"], "path": FEEDBACK_PATH}
        ledger["revision"] += 1
        ledger["entries"][feedback_id] = {**payload, "payload_sha256": payload_hash,
                                            "recorded_at": datetime.now(timezone.utc).isoformat()}
        _atomic(target, ledger)
        return {"profile": FEEDBACK, "outcome": "recorded", "feedback_id": feedback_id,
                "revision": ledger["revision"], "path": FEEDBACK_PATH}


def read_feedback(root, path, revision, sha256, actor=None):
    """Read compact feedback only for one still-eligible exact RAM result."""
    get(root, path, revision, sha256, explicit=True)
    ts.require(actor is None or isinstance(actor, str) and actor, "invalid", "Feedback actor is invalid.")
    target = Path(root).resolve() / FEEDBACK_PATH
    with local_lock(root, create=False):
        if not target.exists():
            return {"profile": FEEDBACK, "outcome": "ok", "entries": [], "total": 0}
        ledger = json.loads(target.read_text(encoding="utf-8"))
        ts.require(isinstance(ledger, dict) and ledger.get("profile") == FEEDBACK_STORE
                   and isinstance(ledger.get("entries"), dict), "invalid", "RAM feedback metadata is invalid.")
        entries = []
        for feedback_id, entry in ledger["entries"].items():
            if (entry.get("path"), entry.get("revision"), entry.get("sha256")) != (path, revision, sha256):
                continue
            if actor is not None and entry.get("actor") != actor:
                continue
            entries.append({"feedback_id": feedback_id, **{key: entry[key] for key in
                           ("actor", "assessment", "recorded_at")}, **({"reason": entry["reason"]} if "reason" in entry else {})})
        entries.sort(key=lambda item: (item["recorded_at"], item["feedback_id"]))
        return {"profile": FEEDBACK, "outcome": "ok", "entries": entries, "total": len(entries),
                "record": {"path": path, "revision": revision, "sha256": sha256}}


def profile_description():
    return {"profile": PROFILE, "outcome": "ok", "writes": [FEEDBACK_PATH],
            "search": {"profile": SEARCH, "query": "navigation scaling", "offset": 0, "limit": 5,
                       "explicit": False, "include_archived": False},
            "browse": {"profile": SEARCH, "query": "", "offset": 0, "limit": 5,
                       "explicit": True, "include_archived": False},
            "get": {"profile": GET, "path": "RAM/work/session/6-issuer--memory.json",
                    "revision": 1, "sha256": "<search result sha256>", "explicit": True,
                    "include_archived": False},
            "feedback": {"profile": FEEDBACK, "idempotency_key": "caller-stable-key",
                         "path": "<search result path>", "revision": 1,
                         "sha256": "<search result sha256>", "actor": "agent-id",
                         "assessment": "useful", "reason": "optional brief reason"},
            "feedback_read": {"profile": GET, "path": "<search result path>", "revision": 1,
                              "sha256": "<search result sha256>", "actor": "optional actor filter"},
            "ranking": "lexical summary/identity/source-path matches; no semantic accuracy claim"}


__all__ = ["FEEDBACK", "GET", "PROFILE", "SEARCH", "feedback", "get", "open_store", "read_feedback",
           "profile_description", "search"]
