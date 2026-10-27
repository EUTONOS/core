"""Bounded, source-pinned knowledge inventory for Current Work.

This is an observation of local Git objects and guarded worktree files.  The
selected checkout's visibility controls remain authoritative over every older
candidate; a branch is never allowed to restore hidden RAM or documents.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from .private_scope import require_shared
from . import measurement, nav, ram, repository_state, tokenslang as ts
from .effects import LocalRoot, digest
from .source import safe_path

PROFILE = "eutonos.current-work-knowledge.v1"
# The core inventory admits 512 refs (including Main) and 128 worktrees.
_MAX_SOURCES = 640
_MAX_RECORDS = 2048
_MAX_DOCUMENTS = 2048
_MAX_DIAGNOSTICS = 40
_MAX_BODY = ts.MAX_SOURCE_BYTES
_IDENTITY_KEYS = ("work_id", "session_id", "issuer_id", "memory_id")


def _under(path, roots):
    return any(path == root or path.startswith(root + "/") for root in roots)


def _note(diagnostics, message):
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(str(message)[:240])


def _select(sources, view, source):
    ts.require(view in {"current", "main", "checkout", "source"}, "invalid", "Unknown Current Work view.")
    ts.require(view == "source" or source is None, "invalid", "Source pin needs source view.")
    if view == "source":
        ts.require(isinstance(source, str) and source, "invalid", "Exact source ID required.")
    selected, committed = [], {}
    for item in sources:
        if item.get("status") not in {None, "observed", "available", "equivalent-ref", "already-on-main"}:
            continue
        if (view == "current" or view == "main" and item.get("is_main")
                or view == "checkout" and item.get("is_checkout")
                or view == "source" and item.get("id") == source):
            # Exact immutable commits need only one tree/blob traversal even if
            # several refs name them. Preserve every ref as provenance.
            if view == "current" and item.get("kind") != "working-tree":
                previous = committed.get(item["commit"])
                if previous is not None:
                    previous.setdefault("equivalent_sources", []).append(item)
                    continue
                item = dict(item)
                committed[item["commit"]] = item
            selected.append(item)
    if view == "source":
        ts.require(len(selected) == 1, "missing", "Selected Current Work source is unavailable.")
    return selected


def _pins(source):
    return [{key: item.get(key) for key in ("id", "label", "kind", "ref", "commit", "path")
             if item.get(key) is not None}
            for item in (source, *source.get("equivalent_sources", ()))]


def _json(raw, expected=None):
    ts.require(raw is not None and len(raw) <= _MAX_BODY, "overflow", "Knowledge source exceeds the native bound.")
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Knowledge source must be UTF-8.") from exc
    if expected:
        ts.require(isinstance(value, dict) and value.get("profile") == expected,
                   "unsupported", "Knowledge source profile differs.")
    return value


class _SnapshotRoot:
    def __init__(self, reader, source, root):
        self.reader, self.source, self.root = reader, source, Path(root)
        self.observed = {}

    def read(self, path):
        path = safe_path(path)
        value = self.reader.read(self.source, path)
        ts.require(path not in self.observed or self.observed[path] == digest(value),
                   "stale", "Source changed during knowledge retrieval.")
        self.observed[path] = digest(value)
        return value


class _SnapshotRam(ram.Store):
    def __init__(self, root, paths, denied):
        self._paths = frozenset(paths)
        super().__init__(root, denied_paths=denied)

    def path(self, identity):
        ts.shape(identity, {"work_id", "session_id", "issuer_id", "memory_id"})
        for value in identity.values():
            ram.slug(value)
        flat = self.flat_path(identity)
        nested = safe_path(self.cfg["store_root"] + "/" + identity["work_id"] + "/"
                           + identity["session_id"] + "/" + str(len(identity["issuer_id"]))
                           + "-" + identity["issuer_id"] + "--" + identity["memory_id"] + ".json")
        ts.require(not (flat in self._paths and nested in self._paths), "conflict",
                   "RAM identity has competing flat and nested paths.")
        return nested if nested in self._paths else flat


def _controls(root):
    """Read current local controls on every call, including after cache reuse."""
    require_shared(root)
    local = LocalRoot(root)
    from .ram_relevance import _catalog
    catalog = _catalog(local)
    denied = set(catalog[1]) if catalog else set()
    excluded = set(catalog[2]) if catalog else set()
    nav_raw = local.read("eutonos.nav.json")
    if nav_raw is not None:
        try:
            nav = _json(nav_raw)
            if nav.get("profile") == "eutonos.tokenslang.nav-config.v1":
                excluded.update(safe_path(path) for path in nav.get("excluded_roots", ()))
        except (ts.ReadError, ValueError, KeyError):
            pass
    return local, catalog, frozenset(denied), frozenset(excluded)


def _optional_local_read(local, path):
    """Treat absent ordinary parent directories as absence, never linked paths."""
    path = safe_path(path)
    target = local.root.joinpath(*path.split("/"))
    current = target.parent
    while current != local.root:
        if current.is_symlink():
            raise ts.ReadError("unsafe-path", "Current control path is linked.")
        if not current.exists():
            return None
        current = current.parent
    return local.read(path)


def _current_ram_control(local, identity, config, source_path, include_archived=False):
    """A present local record wins on visibility, including malformed controls."""
    flat = safe_path(config["store_root"] + "/record-" + hashlib.sha256(ts.encoded(identity)).hexdigest() + ".json")
    nested = safe_path(config["store_root"] + "/" + identity["work_id"] + "/" + identity["session_id"]
                       + "/" + str(len(identity["issuer_id"])) + "-" + identity["issuer_id"] + "--"
                       + identity["memory_id"] + ".json")
    paths = {flat, nested, source_path}
    present = []
    for path in paths:
        try:
            body = _optional_local_read(local, path)
        except ts.ReadError:
            return False
        if body is not None:
            present.append(body)
    if not present:
        return True
    for body in present:
        try:
            record = _json(body, ram.RECORD)
            if record["identity"] != identity:
                return False
            control = record["controls"]
            if (control["state"] not in ({"active", "archived"} if include_archived else {"active"})
                    or control["visibility"] != "public"
                    or control["expires_at"] is not None
                    and ram.instant(control["expires_at"]) <= datetime.now(timezone.utc)):
                return False
        except (ts.ReadError, KeyError, TypeError, ValueError):
            return False
    return True


def _eligible(store, path, record, read, local, denied, excluded, seen=None,
              include_archived=False, control_cache=None):
    seen = set() if seen is None else seen
    ts.require(path not in seen, "conflict", "RAM dependency cycle detected.")
    seen.add(path)
    store.eligible(path, record, read)
    ts.require(record["controls"]["state"] != "archived" or include_archived,
               "archived", "Archived RAM needs explicit selection.")
    ts.require(path not in denied and not _under(path, excluded), "denied", "Current catalog hides RAM.")
    control_key = (store.cfg["store_root"], tuple(record["identity"][key] for key in _IDENTITY_KEYS),
                   path, include_archived)
    if control_cache is None:
        current_allowed = _current_ram_control(local, record["identity"], store.cfg, path, include_archived)
    else:
        if control_key not in control_cache:
            control_cache[control_key] = _current_ram_control(
                local, record["identity"], store.cfg, path, include_archived)
        current_allowed = control_cache[control_key]
    ts.require(current_allowed,
               "denied", "Current RAM control hides an older candidate.")
    version = record["versions"][-1]
    linked = [version["work"]["path"], *(item["path"] for item in version["sources"])]
    ts.require(not any(item in denied or _under(item, excluded) for item in linked),
               "denied", "Current catalog hides a RAM source.")
    for child in version["children"]:
        _eligible(store, child["path"], store.load(child["path"], read), read,
                  local, denied, excluded, seen.copy(), include_archived, control_cache)


def _ram_rows(reader, sources, local, catalog, denied, excluded, diagnostics, coverage,
              include_archived=False, control_cache=None):
    current_config = local.read("eutonos.ram.json")
    if current_config is None:
        return {"status": "not-configured", "records": [], "total": 0, "conflicts": []}
    authority = _json(current_config, ram.CONFIG)
    if catalog is not None:
        ts.require(all(catalog[0][key] == authority[key] for key in
                       ("repository_id", "scope_id", "namespace")),
                   "wrong-scope", "Current RAM and catalog identities differ.")
    by_identity = defaultdict(dict)
    checked = 0
    for source in sources:
        try:
            snap = _SnapshotRoot(reader, source, reader.root)
            cfg_raw = snap.read("eutonos.ram.json")
            if cfg_raw is None:
                continue
            cfg = _json(cfg_raw, ram.CONFIG)
            ts.require(all(cfg.get(key) == authority.get(key) for key in
                           ("repository_id", "scope_id", "namespace", "store_root")),
                       "wrong-scope", "Source RAM config belongs to another scope.")
            paths = [p for p in reader.list(source, cfg["store_root"])
                     if p.endswith(".json") and not _under(p, excluded) and p not in denied]
            store = _SnapshotRam(snap, paths, denied)
            for path in paths:
                checked += 1
                if checked > _MAX_RECORDS:
                    coverage["ram_truncated"] = True
                    break
                try:
                    record = store.load(path, snap.read)
                    _eligible(store, path, record, snap.read, local, denied, excluded,
                              include_archived=include_archived, control_cache=control_cache)
                except ts.ReadError as exc:
                    if exc.code not in {"denied", "archived"}:
                        coverage["ram_rejected"] += 1
                    continue
                except (KeyError, TypeError, ValueError):
                    coverage["ram_rejected"] += 1
                    continue
                body = snap.read(path)
                key = tuple(record["identity"][part] for part in
                            ("work_id", "session_id", "issuer_id", "memory_id"))
                sha = digest(body)
                version = record["versions"][-1]
                pins = _pins(source)
                variant = {"path": path, "revision": record["revision"], "sha256": sha,
                           "content_revision": version["revision"], "summary": version["summary"],
                           "kind": version["kind"], "identity": record["identity"],
                           "state": record["controls"]["state"], "visibility": record["controls"]["visibility"],
                           "controls": record["controls"], "unfinished": version["unfinished"],
                           "recorded_at": version["recorded_at"], "occurred_at": version["occurred_at"],
                           "actors": version["actors"], "evidence_sources": version["sources"],
                           "words": len(version["summary"].split()), "bytes": len(body),
                           "versions": [{"revision": v["revision"], "summary": v["summary"],
                                         "recorded_at": v["recorded_at"]} for v in record["versions"]],
                           "source_id": source["id"], "source": pins[0]}
                existing = by_identity[key].get(sha)
                if existing:
                    existing["sources"].extend(pins)
                else:
                    variant["sources"] = pins
                    by_identity[key][sha] = variant
            if coverage["ram_truncated"]:
                break
        except (ts.ReadError, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            coverage["sources_partial"] += 1
            _note(diagnostics, "RAM source " + str(source.get("id")) + ": " + getattr(exc, "code", "unavailable"))
    records, conflicts = [], []
    for identity_key, choices in sorted(by_identity.items()):
        variants = list(choices.values())
        selected = variants[0]
        item = {key: selected[key] for key in
                ("identity", "path", "revision", "sha256", "source_id", "state", "visibility",
                 "controls", "kind", "summary", "unfinished", "recorded_at", "occurred_at",
                 "actors", "evidence_sources", "words", "bytes", "versions")}
        item.update(variants=variants, conflict=len(variants) > 1)
        records.append(item)
        if item["conflict"]:
            conflicts.append({"identity": item["identity"], "variants":
                              [{"path": v["path"], "sha256": v["sha256"], "sources": v["sources"]}
                               for v in variants]})
    return {"status": "partial" if coverage["ram_truncated"] or coverage["ram_rejected"] else "available",
            "records": records, "total": len(records), "conflicts": conflicts,
            "totals": {"records": len(records), "words": sum(item["words"] for item in records),
                       "bytes": sum(item["bytes"] for item in records)},
            "checked_candidates": checked, "qualification":
            "Source-pinned experience records; divergent same-identity content remains visible as a conflict."}


def _document_rows(reader, sources, catalog, denied, excluded, diagnostics, coverage):
    configured = set()
    if catalog:
        configured.update(safe_path(binding["path"]) for binding in catalog[0]["bindings"]
                          if binding["visibility"] == "public" and binding["state"] == "active"
                          and binding["kind"] in {"docs", "nav"})
        configured.update(safe_path(path) for path in catalog[0]["source_paths"])
    variants = defaultdict(dict)
    checked = 0
    rejected_reasons = defaultdict(int)
    for source in sources:
        try:
            paths = set(configured)
            paths.update(reader.list(source, "docs"))
            paths.update(reader.list(source, "NAV"))
            for path in sorted(paths):
                if not path.endswith((".md", ".txt", ".json")) or path in denied or _under(path, excluded):
                    continue
                checked += 1
                if checked > _MAX_DOCUMENTS:
                    coverage["documents_truncated"] = True
                    break
                try:
                    metadata = reader.document(source, path)
                    if metadata is None:
                        continue
                except (ts.ReadError, UnicodeError, ValueError, OSError) as exc:
                    coverage["documents_rejected"] += 1
                    rejected_reasons[getattr(exc, "code", "unavailable")] += 1
                    continue
                sha = metadata["sha256"]
                pins = _pins(source)
                if sha in variants[path]:
                    variants[path][sha]["sources"].extend(pins)
                else:
                    variants[path][sha] = {"path": path, "sha256": sha, "bytes": metadata["total_bytes"],
                                           "status": "observed", "source_id": source["id"], "sources": pins}
            if coverage["documents_truncated"]:
                break
        except (ts.ReadError, OSError, UnicodeError, ValueError) as exc:
            coverage["sources_partial"] += 1
            _note(diagnostics, "Document source " + str(source.get("id")) + ": " + getattr(exc, "code", "unavailable"))
    rows = [{"path": path, "variants": list(values.values()), "conflict": len(values) > 1,
             "status": "different-versions" if len(values) > 1 else "observed"}
            for path, values in sorted(variants.items())]
    if rejected_reasons:
        _note(diagnostics, "Document index skipped bounded candidates: "
              + ", ".join(f"{code}={count}" for code, count in sorted(rejected_reasons.items())))
    return {"status": "partial" if coverage["documents_truncated"] or coverage["documents_rejected"] else "available",
            "items": rows, "total": len(rows), "different_versions": sum(row["conflict"] for row in rows),
            "checked_candidates": checked, "rejection_reasons": dict(rejected_reasons),
            "qualification": "Index contains streamed UTF-8 public documents; skipped candidates are counted by reason."}


def _nav_rows(reader, sources, local, denied, excluded, diagnostics, coverage):
    current_config = local.read("eutonos.nav.json")
    if current_config is None:
        return {"status": "not-configured", "records": 0, "by_kind": {}, "review": {},
                "routes": [], "runs": [], "run_count": 0, "pass_count": 0,
                "history_complete": False}
    authority = nav.config(current_config)
    # The NAV config's own excluded_roots only trim source inventory (the map root is
    # excluded there automatically). A redundant exclusion of the map root must not hide
    # the map files themselves, so map paths are checked against catalog controls only.
    from .ram_relevance import _catalog
    catalog = _catalog(local)
    map_denied = set(catalog[1]) if catalog else set()
    map_excluded = set(catalog[2]) if catalog else set()
    by_id = defaultdict(dict)
    for source in sources:
        try:
            raw = reader.read(source, "eutonos.nav.json")
            if raw is None:
                continue
            config = nav.config(raw)
            ts.require(all(config[key] == authority[key] for key in
                           ("repository_id", "scope_id", "namespace", "map_root")),
                       "wrong-scope", "NAV source belongs to another scope.")
            paths = [config["map_root"] + "/" + name for name in nav.MAPS]
            ts.require(not any(path in map_denied or _under(path, map_excluded) for path in paths),
                       "denied", "Current catalog hides a NAV map.")
            bodies = {name: reader.read(source, config["map_root"] + "/" + name)
                      for name in nav.MAPS}
            records, _ = nav.parse_maps(bodies)
            for record in records.values():
                if any(path in denied or _under(path, excluded) for path in record["evidence"]):
                    continue
                sha = digest(record["text"].encode("utf-8"))
                pins = _pins(source)
                existing = by_id[record["id"]].get(sha)
                if existing:
                    existing["sources"].extend(pins)
                else:
                    by_id[record["id"]][sha] = {
                        "id": record["id"], "title": record["title"],
                        "kind": record["fields"]["Kind"], "summary": record["fields"]["Summary"],
                        "review": record["fields"]["Review"], "evidence": record["evidence"],
                        "related": record["related"], "sha256": sha, "source_id": source["id"],
                        "map_path": config["map_root"] + "/" + record["map"], "sources": pins}
        except (ts.ReadError, OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            coverage["sources_partial"] += 1
            _note(diagnostics, "NAV source " + str(source.get("id")) + ": " + getattr(exc, "code", "unavailable"))
    routes, by_kind, review, conflicts = [], {}, {}, []
    for route_id, versions in sorted(by_id.items()):
        variants = list(versions.values())
        selected = variants[0]
        route = {**selected, "variants": variants, "conflict": len(variants) > 1}
        routes.append(route)
        by_kind[route["kind"]] = by_kind.get(route["kind"], 0) + 1
        review[route["review"]] = review.get(route["review"], 0) + 1
        if route["conflict"]:
            conflicts.append({"id": route_id, "variants": [{"sha256": v["sha256"], "sources": v["sources"]}
                                                        for v in variants]})
    return {"status": "partial" if coverage["sources_partial"] else "available",
            "records": len(routes), "by_kind": by_kind, "review": review, "routes": routes,
            "conflicts": conflicts, "runs": [], "run_count": 0, "pass_count": 0,
            "history_complete": False,
            "qualification": "NAV route content comes from each source's native maps; selected-device run history is separate."}


def _measurement_rows(reader, sources, local, diagnostics, coverage):
    current = local.read("eutonos.measurement.json")
    if current is None:
        return {"status": "not-configured", "event_count": 0, "provider_tokens": None,
                "task_savings": None, "net_benefit": None}
    authority = _json(current, measurement.CONFIG)
    events = {}
    conflicts = []
    checked = 0
    for source in sources:
        try:
            snap = _SnapshotRoot(reader, source, reader.root)
            raw = snap.read("eutonos.measurement.json")
            if raw is None:
                continue
            cfg = _json(raw, measurement.CONFIG)
            ts.require(cfg["scope_id"] == authority["scope_id"]
                       and cfg["store_root"] == authority["store_root"],
                       "wrong-scope", "Measurement source belongs to another scope.")
            store = measurement.Store(snap)
            for path in reader.list(source, cfg["store_root"]):
                if not path.endswith(".json"):
                    continue
                checked += 1
                if checked > _MAX_RECORDS:
                    coverage["measurement_truncated"] = True
                    break
                try:
                    body = snap.read(path)
                    batch = store._checked_batch(path, body)
                    if batch["visibility"] != "public" or batch["state"] != "active":
                        continue
                    current_body = _optional_local_read(local, path)
                    if current_body is not None:
                        try:
                            current_batch = store._checked_batch(path, current_body)
                            if current_batch["visibility"] != "public" or current_batch["state"] != "active":
                                continue
                        except (ts.ReadError, KeyError, TypeError, ValueError):
                            # A malformed current control cannot expose an old copy.
                            continue
                    for event in batch["events"]:
                        key = (batch["identity"]["issuer_id"], batch["identity"]["batch_id"], event["id"])
                        sha = digest(ts.encoded(event))
                        item = events.get(key)
                        if item and item["sha256"] != sha:
                            if not item.get("conflict"):
                                conflicts.append({"identity": {"issuer_id": key[0], "batch_id": key[1], "event_id": key[2]},
                                                  "sources": [item["source_id"], source["id"]]})
                            item["conflict"] = True
                        elif not item:
                            events[key] = {"event": event, "sha256": sha, "source_id": source["id"]}
                except (ts.ReadError, KeyError, TypeError, ValueError):
                    coverage["measurement_rejected"] += 1
            if coverage["measurement_truncated"]:
                break
        except (ts.ReadError, OSError, UnicodeError, ValueError) as exc:
            coverage["sources_partial"] += 1
            _note(diagnostics, "Measurement source " + str(source.get("id")) + ": " + getattr(exc, "code", "unavailable"))
    valid = [value["event"] for value in events.values() if not value.get("conflict")]
    levels = {name: {"count": 0, "elapsed_ns": 0, "returned_bytes": 0,
                     "unknown_durations": 0, "unknown_sizes": 0} for name in ("entry", "module", "attempt")}
    for event in valid:
        level = levels[event["level"]]
        level["count"] += 1
        for field, unknown in (("elapsed_ns", "unknown_durations"), ("returned_bytes", "unknown_sizes")):
            if event[field] is None:
                level[unknown] += 1
            else:
                level[field] += event[field]
    return {"status": "partial" if conflicts or coverage["measurement_truncated"] or coverage["measurement_rejected"] else "available",
            "event_count": len(valid), "conflicts": conflicts, "levels": levels,
            "provider_tokens": None, "task_savings": None, "net_benefit": None,
            "checked_candidates": checked,
            "qualification": "Unique native observation identities only. Private provider usage stays scoped to its native selection; uninstrumented totals remain unknown."}


def project(root, sources, *, view="current", source=None, refresh=False, modules=None,
            include_archived=False):
    """Union bounded knowledge from the selected observed source descriptors.

    ``modules`` is an internal cost control for source-pinned RAM retrieval.
    Full Current Work projections omit it and receive every knowledge module.
    """
    del refresh  # The caller owns source discovery; local controls are always reread.
    ts.require(isinstance(sources, list), "invalid", "Current Work sources must be a list.")
    omitted = max(0, len(sources) - _MAX_SOURCES)
    bounded = sources[:_MAX_SOURCES]
    if omitted and view in {"source", "checkout", "main"}:
        # An exact selected view still works if a future core adds sources
        # beyond today's union bound; the omitted inventory remains visible.
        chosen = next((item for item in sources if isinstance(item, dict) and
                       (view == "source" and item.get("id") == source or
                        view == "checkout" and item.get("is_checkout") or
                        view == "main" and item.get("is_main"))), None)
        if chosen is not None and chosen not in bounded:
            bounded[-1] = chosen
    selected = _select(bounded, view, source)
    modules = frozenset(modules or ("ram", "documents", "nav", "measurement"))
    ts.require(modules <= {"ram", "documents", "nav", "measurement"}, "invalid", "Unknown knowledge module.")
    local, catalog, denied, excluded = _controls(root)
    control_cache = {} if "ram" in modules else None
    coverage = {"sources_input": len(sources), "sources_selected": len(selected),
                "sources_omitted": omitted, "sources_truncated": bool(omitted),
                "sources_partial": 0,
                "ram_truncated": False, "ram_rejected": 0,
                "documents_truncated": False, "documents_rejected": 0,
                "measurement_truncated": False, "measurement_rejected": 0}
    diagnostics = []
    if omitted:
        _note(diagnostics, f"Knowledge source inventory exceeded the {_MAX_SOURCES}-descriptor bound; "
              f"{omitted} descriptors were omitted.")
    result = {"profile": PROFILE, "view": view, "source": source, "coverage": coverage,
              "diagnostics": diagnostics}
    deadline = time.monotonic() + 30
    try:
        with repository_state.SourceReader(root, max_bytes=48 * 1024 * 1024,
                                           time_budget=30, max_files=8192) as reader:
            if "ram" in modules:
                result["ram"] = _ram_rows(reader, selected, local, catalog, denied, excluded,
                                          diagnostics, coverage, include_archived, control_cache)
            if "documents" in modules:
                docs = _document_rows(reader, selected, catalog, denied, excluded, diagnostics, coverage)
                result["documents"] = docs
            if "nav" in modules:
                result["nav"] = _nav_rows(reader, selected, local, denied, excluded, diagnostics, coverage)
            if "measurement" in modules:
                result["measurement"] = _measurement_rows(reader, selected, local, diagnostics, coverage)
    except (ts.ReadError, OSError, UnicodeError, ValueError) as exc:
        coverage["sources_partial"] += len(selected) or 1
        _note(diagnostics, "Knowledge batch incomplete: " + getattr(exc, "code", "unavailable"))
    # Recheck every admitted identity, including milestone children, after the
    # other modules. A control changed during this read cannot expose a branch
    # candidate; a new request will observe the new controls from the start.
    if control_cache and "ram" in result:
        incomplete = False
        for (store_root, identity_values, path, archived), allowed in control_cache.items():
            if not allowed:
                continue
            if time.monotonic() >= deadline:
                incomplete = True
                break
            identity = dict(zip(_IDENTITY_KEYS, identity_values))
            try:
                still_allowed = _current_ram_control(local, identity, {"store_root": store_root}, path, archived)
            except (ts.ReadError, OSError, KeyError, TypeError, ValueError):
                still_allowed = False
            if not still_allowed:
                incomplete = True
                break
        if incomplete or time.monotonic() >= deadline:
            result["ram"].update(status="partial", records=[], total=0, conflicts=[],
                                 totals={"records": 0, "words": 0, "bytes": 0},
                                 qualification="Current RAM controls changed or final check reached the time bound; refresh the source-pinned view.")
            _note(diagnostics, "Current RAM controls changed or final check timed out; refresh the view.")
    for key in ("ram", "documents", "nav", "measurement"):
        if key not in result and key in modules:
            result[key] = {"status": "partial", "qualification": "Bounded source read was incomplete."}
        elif omitted and view == "current" and key in result and result[key].get("status") == "available":
            result[key]["status"] = "partial"
    result["status"] = "partial" if diagnostics or any(coverage[key] for key in coverage if key.endswith("truncated") or key.endswith("rejected") or key == "sources_partial") else "available"
    return result


def read_document(root, sources, *, path, source_id, sha256, offset=0, max_bytes=131072):
    """Expand one UTF-8 page from an exact source and content hash."""
    path = safe_path(path)
    ts.require(isinstance(sha256, str) and len(sha256) == 64, "invalid", "Document hash required.")
    ts.require(type(offset) is int and offset >= 0 and type(max_bytes) is int
               and 1 <= max_bytes <= 131072, "invalid", "Invalid document page offset or size.")
    selected = _select(sources, "source", source_id)[0]
    _, catalog, denied, excluded = _controls(root)
    ts.require(path not in denied and not _under(path, excluded)
               and (path.startswith("docs/") or path.startswith("NAV/")
                    or catalog and path in set(catalog[0]["source_paths"])
                    or catalog and path in {item["path"] for item in catalog[0]["bindings"]
                                            if item["visibility"] == "public" and item["state"] == "active"}),
               "denied", "Document is outside the public knowledge index.")
    with repository_state.SourceReader(root, max_bytes=2 * 1024 * 1024, time_budget=20,
                                       max_files=8192) as reader:
        try:
            page = reader.document(selected, path, offset=offset, max_bytes=max_bytes)
        except UnicodeError as exc:
            raise ts.ReadError("unsupported", "Document must be UTF-8.") from exc
        except IndexError as exc:
            raise ts.ReadError("invalid", str(exc)) from exc
        except ValueError as exc:
            raise ts.ReadError("unavailable", str(exc)) from exc
    ts.require(page is not None and page["sha256"] == sha256, "stale", "Document source changed; refresh the index.")
    return {"path": path, "source_id": source_id, "sha256": sha256, "content": page["content"],
            "offset": page["offset"], "next_offset": page["next_offset"],
            "total_bytes": page["total_bytes"], "eof": page["eof"],
            "status": "source-pinned", "qualification": "Exact observed bytes; correctness and publication remain separate."}


def read_ram(root, sources, *, path, revision, sha256, source_id, include_archived=False):
    """Expand a RAM result only while its source and current controls still match."""
    path = safe_path(path)
    ts.require(type(revision) is int and revision >= 1 and isinstance(sha256, str)
               and len(sha256) == 64, "invalid", "Exact RAM pin is required.")
    selected = _select(sources, "source", source_id)[0]
    local, catalog, denied, excluded = _controls(root)
    with repository_state.SourceReader(root, max_bytes=4 * 1024 * 1024,
                                       time_budget=10, max_files=8192) as reader:
        snap = _SnapshotRoot(reader, selected, reader.root)
        config = _json(snap.read("eutonos.ram.json"), ram.CONFIG)
        authority = _json(local.read("eutonos.ram.json"), ram.CONFIG)
        ts.require(all(config.get(key) == authority.get(key) for key in
                       ("repository_id", "scope_id", "namespace", "store_root")),
                   "wrong-scope", "Source RAM config belongs to another scope.")
        paths = [p for p in reader.list(selected, config["store_root"]) if p.endswith(".json")]
        ts.require(path in paths, "missing", "Pinned RAM path is unavailable.")
        store = _SnapshotRam(snap, paths, denied)
        record = store.load(path, snap.read)
        _eligible(store, path, record, snap.read, local, denied, excluded,
                  include_archived=include_archived)
        ts.require(record["controls"]["state"] != "archived" or include_archived,
                   "archived", "Explicitly include archived history.")
        body = snap.read(path)
        ts.require(record["revision"] == revision and digest(body) == sha256,
                   "stale", "RAM source changed; search again before expansion.")
        version = record["versions"][-1]
        freshness = []
        for item in version["sources"]:
            try:
                source_body = snap.read(item["path"])
                state = "missing" if source_body is None else "match" if digest(source_body) == item["sha256"] else "stale"
            except (ts.ReadError, OSError, ValueError):
                state = "unknown"
            freshness.append({"path": item["path"], "state": state,
                              "source_freshness": {"match": "matching", "stale": "changed"}.get(state, state)})
        return {"path": path, "source_id": source_id, "revision": revision, "sha256": sha256,
                "identity": record["identity"], "controls": record["controls"],
                "content": version, "freshness": freshness,
                "semantic_applicability": ram.applicability(version.get("applicability")),
                "qualification": "Source-pinned bytes; semantic applicability and live acceptance remain separate."}
