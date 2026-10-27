"""RNAVL17G-derived native maps, bounded source inventory and read operations.

See nav_data/PROVENANCE.txt. Mechanical evidence and lexical ranking supply
routes, never semantic acceptance. Reads never instantiate the runner.
"""
from __future__ import annotations

from importlib.resources import files
import re

from .private_scope import require_shared_view
from . import tokenslang as ts
from .effects import digest
from .nav_inventory import inventory
from .source import safe_path

CONFIG = "eutonos.tokenslang.nav-config.v1"
READ = "eutonos.tokenslang.nav-read.v1"
MAPS = ("NAVIGATION.md", "SURFACES.md", "JOURNEYS.md", "SOURCE_FAMILIES.md")
KINDS = {"AREA": ("area", MAPS[0]), "SYS": ("system", MAPS[0]),
         "GAP": ("gap", MAPS[0]), "SURF": ("surface", MAPS[1]),
         "INT": ("interaction", MAPS[1]), "JRN": ("journey", MAPS[2]),
         "SRC": ("source-family", MAPS[3])}
HEADING = re.compile(r"^###\s+`((?:AREA|SYS|GAP|SURF|INT|JRN|SRC)-[a-z0-9][a-z0-9-]*)`\s+(.+?)\s*$", re.M)
FIELD = re.compile(r"^- ([A-Za-z][A-Za-z ]+):\s*(.*?)\s*$", re.M)
REQUIRED = {"Kind", "Aliases", "Summary", "Evidence", "Related", "Review", "Revision"}
REVIEWS = {"candidate", "reviewed", "accepted", "stale", "gap"}
SWEEP_BYTES = files("eutonos").joinpath("nav_data/sweeps.json").read_bytes()
CONTRACTS = ts.loads(SWEEP_BYTES.decode())
SWEEPS = CONTRACTS["sweeps"]


def decode(raw):
    ts.require(raw is not None, "missing", "Required NAV source is absent.")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "NAV requires UTF-8 source.") from exc


def values(field):
    return [s.strip().strip("`") for s in field.split(";") if s.strip().strip("`").lower() not in {"", "none", "n/a"}]


def config(raw):
    value = ts.loads(decode(raw))
    ts.shape(value, {"profile", "repository_id", "scope_id", "namespace", "map_root",
                     "source_roots", "excluded_roots", "context_paths"})
    ts.require(value["profile"] == CONFIG, "unsupported", "Unsupported NAV config.")
    for key in ("repository_id", "scope_id", "namespace"):
        ts.string(value[key])
    safe_path(value["map_root"])
    for key in ("source_roots", "excluded_roots", "context_paths"):
        for path in ts.sequence(value[key]):
            safe_path(path)
    ts.require(value["source_roots"] and value["context_paths"], "invalid", "Explicit source and governing context paths are required.")
    ts.require(not any(under(p, [value["map_root"]]) or under(p, value["excluded_roots"])
                       for p in value["context_paths"]), "denied", "Governing context cannot be NAV output or excluded.")
    return value


def under(path, roots):
    return any(path == p or path.startswith(p + "/") for p in roots)


def parse_maps(bodies):
    """Preserve complete record blocks and preambles, including native extra fields."""
    records, preambles = {}, {}
    for name in MAPS:
        body = decode(bodies[name])
        headings = list(HEADING.finditer(body))
        ts.require(body.count("### `") == len(headings), "invalid", "Malformed native record heading.")
        preambles[name] = body[:headings[0].start()] if headings else body
        for index, match in enumerate(headings):
            rid, title = match.groups()
            ts.require(rid not in records, "ambiguous", "Duplicate native NAV identity.")
            block = body[match.start():headings[index+1].start() if index+1 < len(headings) else len(body)]
            pairs = FIELD.findall(block)
            fields = dict(pairs)
            ts.require(len(fields) == len(pairs) and REQUIRED <= fields.keys(), "invalid", "Missing or duplicate record fields.")
            kind, owner = KINDS[rid.split("-")[0]]
            ts.require(fields["Kind"] == kind and owner == name and fields["Review"] in REVIEWS,
                       "invalid", "Record kind, native owner or review state differs.")
            evidence = values(fields["Evidence"])
            for path in evidence:
                safe_path(path)
            records[rid] = {"id": rid, "title": title, "fields": fields, "map": name,
                            "text": block, "evidence": evidence, "related": values(fields["Related"])}
    for record in records.values():
        ts.require(set(record["related"]) <= records.keys(), "missing", "Unresolved related native ID.")
    return records, preambles


class NavIndex:
    """One explicit committed map/source view. No freshness claim about the working tree."""
    def __init__(self, view, config_path="eutonos.nav.json", denied_paths=()):
        require_shared_view(view)
        self.view = view
        self.denied_paths = frozenset(denied_paths)
        self.config_path = safe_path(config_path)
        ts.require(config_path not in self.denied_paths, "denied", "NAV configuration is not public in this Hub.")
        raw, _ = view.read(config_path)
        self.cfg = config(raw)
        ts.require(not under(config_path, [self.cfg["map_root"]]), "invalid", "NAV config must be outside map output.")
        ts.require(not any(self.cfg["map_root"] + "/" + name in self.denied_paths for name in (*MAPS, "NAV_RUN.json")),
                   "denied", "NAV maps or baseline are not public in this Hub.")
        self.bodies = {name: view.read(self.cfg["map_root"] + "/" + name)[0] for name in MAPS}
        self.records, _ = parse_maps(self.bodies)
        self.fingerprint = digest(ts.encoded({"commit": view.commit, "maps": {k: digest(v) for k, v in self.bodies.items()}}))
        self.source_cache = {}
        self.baseline = None
        try:
            raw_state, _ = view.read(self.cfg["map_root"] + "/NAV_RUN.json")
        except ts.ReadError as exc:
            if exc.code != "missing":
                raise
        else:
            state = ts.loads(decode(raw_state))
            ts.require(isinstance(state, dict) and state.get("profile") == "eutonos.tokenslang.nav-run.v1"
                       and isinstance(state.get("baseline"), dict), "unsupported", "Unsupported NAV source baseline.")
            self.baseline = state["baseline"]

    def source(self, path):
        safe_path(path)
        ts.require(path not in self.denied_paths, "denied", "Source is not public in this Hub.")
        ts.require((under(path, self.cfg["source_roots"]) or path in self.cfg["context_paths"])
                   and not under(path, self.cfg["excluded_roots"] + [self.cfg["map_root"]])
                   and path != self.config_path, "denied", "Source is outside the selected NAV scope.")
        return self.view.read(path)

    def handle(self, request):
        result = self._handle(request)
        budget = request.get("max_bytes", ts.MAX_BYTES)
        if len(ts.encoded(result)) > budget:
            return {"profile": READ, "outcome": "overflow", "complete": False,
                    "required_bytes": len(ts.encoded(result)), "source_commit": self.view.commit,
                    "continuation": "Increase max_bytes or read the selected native map/source directly; no content was truncated."}
        return result

    def _handle(self, request):
        ts.shape(request, {"profile", "operation"}, {"target", "query", "limit", "cursor", "max_bytes", "path"})
        ts.require(request["profile"] == READ, "unsupported", "Unsupported TokenSlang NAV read profile.")
        op = request["operation"]
        ts.require(isinstance(op, str) and op in {"describe", "browse", "search", "resolve", "open"}, "unsupported", "Unknown NAV read operation.")
        limit, budget = request.get("limit", 16), request.get("max_bytes", ts.MAX_BYTES)
        ts.require(type(limit) is int and 1 <= limit <= 64 and type(budget) is int and 512 <= budget <= ts.MAX_BYTES,
                   "invalid", "NAV output bounds are invalid.")
        envelope = {"profile": READ, "outcome": "ok", "scope_id": self.cfg["scope_id"], "namespace": self.cfg["namespace"],
                    "source_view": {"commit": self.view.commit, "fingerprint": self.fingerprint},
                    "qualification": "Source routes, not semantic acceptance; live freshness unobserved."}
        if op == "describe":
            return {**envelope, "operations": ["browse", "search", "resolve", "open"], "construction_started": False}
        if op == "open":
            path = ts.string(request.get("path"))
            data, evidence = self.source(path)
            result = {**envelope, "path": path, "source": evidence, "content": decode(data)}
            if len(ts.encoded(result)) > budget:
                return {**envelope, "outcome": "overflow", "complete": False, "direct_source": {"path": path, "commit": self.view.commit}}
            return result
        records = [r for r in self.records.values() if not set(r["evidence"]) & self.denied_paths]
        if op == "resolve":
            ref = request.get("target")
            ts.reference(ref)
            ts.require("resource_id" in ref and ref["scope_id"] == self.cfg["scope_id"] and ref["namespace"] == self.cfg["namespace"],
                       "wrong-scope", "NAV identity requires the selected scope and namespace.")
            key = ref["resource_id"]
            records = [r for r in records if key == r["id"] or key in values(r["fields"]["Aliases"])]
            ts.require(len(records) == 1, "ambiguous" if records else "missing", "Exact NAV reference is not uniquely bound.")
        elif op == "search":
            query = ts.string(request.get("query")).casefold()
            def score(r):
                return sum(weight * (sum(word in value.casefold() for word in query.split()) + 3 * (query in value.casefold()))
                           for weight, value in ((5, r["title"]), (4, r["fields"]["Aliases"]), (2, r["fields"]["Summary"])))
            records = sorted((r for r in records if score(r)), key=lambda r: (-score(r), r["id"]))
        else:
            records.sort(key=lambda r: r["id"])
        offset = 0
        key = digest(ts.encoded({"view": self.fingerprint, "operation": op, "query": request.get("query"), "target": request.get("target")}))
        if "cursor" in request:
            cursor = request["cursor"]
            ts.shape(cursor, {"key", "offset"})
            ts.require(cursor["key"] == key and type(cursor["offset"]) is int and 0 <= cursor["offset"] <= len(records),
                       "stale", "NAV continuation belongs to a different view/query.")
            offset = cursor["offset"]
        items = []
        for r in records[offset:offset+limit]:
            routes = []
            for path in r["evidence"]:
                if path not in self.source_cache:
                    try:
                        _, evidence = self.source(path)
                        self.source_cache[path] = {"path": path, "sha256": evidence["sha256"],
                            "binding": "baseline-unknown" if self.baseline is None else
                            "unchanged-from-run-baseline" if evidence["sha256"] == self.baseline.get(path) else "changed-since-run-baseline"}
                    except ts.ReadError as exc:
                        if exc.code in {"overflow", "unavailable"}:
                            raise
                        self.source_cache[path] = {"path": path, "binding": "unavailable", "diagnostic": exc.code}
                routes.append(self.source_cache[path])
            item = {"reference": {"scope_id": self.cfg["scope_id"], "namespace": self.cfg["namespace"], "resource_id": r["id"]},
                    "title": r["title"], "kind": r["fields"]["Kind"], "summary": r["fields"]["Summary"],
                    "review": r["fields"]["Review"], "revision": r["fields"]["Revision"], "related": r["related"],
                    "map_path": self.cfg["map_root"] + "/" + r["map"], "direct_sources": r["evidence"], "source_bindings": routes}
            if len(ts.encoded({**envelope, "items": items + [item]})) + 220 > budget:
                break
            items.append(item)
        end = offset + len(items)
        return {**envelope, "outcome": "overflow" if not items and offset < len(records) else "ok",
                "items": items, "complete": end == len(records), "total": len(records),
                "continuation": {"key": key, "offset": end} if end < len(records) else None,
                "direct_map_sources": [self.cfg["map_root"] + "/" + m for m in MAPS] if not items and records else []}
