"""EHF4R portable experience records with current local controls and guarded plans.

RAM describes experience; native work selects tasks. No model, cache, background
capture, implicit promotion or skill execution is implemented here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from pathlib import Path

from .private_scope import require_shared
from . import tokenslang as ts
from . import health
from .effects import LocalRoot, digest, plan, validate_plan
from .source import GitView, safe_path

CONFIG = "eutonos.tokenslang.ram-config.v1"
RECORD = "eutonos.tokenslang.ram-record.v1"
WRITE = "eutonos.tokenslang.ram-write.v1"
PLAN = "eutonos.tokenslang.ram-plan.v1"
READ = "eutonos.tokenslang.ram-read.v1"
LINKS = "eutonos.tokenslang.ram-links.v1"
LINKS_V2 = "eutonos.tokenslang.ram-links.v2"
BLOCK = re.compile(rb"(?m)^```tokenslang-ram-links\r?\n(.*?)^```[ \t]*\r?$", re.S)
APPLICABILITY_STATES = {"unassessed", "revalidated", "superseded", "invalid"}


def applicability(value=None):
    """Normalize an explicit semantic assessment without inferring it from source bytes."""
    if value is None:
        return {"state": "unassessed", "assessed_at": None, "reason": "No explicit semantic assessment is recorded."}
    ts.shape(value, {"state", "assessed_at", "reason"})
    ts.require(value["state"] in APPLICABILITY_STATES, "unsupported", "Unknown RAM applicability state.")
    ts.string(value["reason"])
    if value["state"] == "unassessed":
        ts.require(value["assessed_at"] is None, "invalid", "Unassessed RAM cannot carry an assessment time.")
    else:
        instant(value["assessed_at"])
        ts.require(bool(value["reason"].strip()), "invalid", "A semantic assessment needs a reason.")
    return dict(value)


def unpack(body):
    ts.require(body is not None, "missing", "RAM source is absent.")
    try:
        return ts.loads(body.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "RAM source must be UTF-8.") from exc


def instant(value):
    try:
        stamp = datetime.fromisoformat(ts.string(value))
    except ValueError as exc:
        raise ts.ReadError("invalid", "Time must be an ISO instant.") from exc
    ts.require(stamp.utcoffset() is not None, "invalid", "Time requires its UTC offset.")
    return stamp


def slug(value):
    ts.require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value)),
               "invalid", "Identity component must be a bounded portable name.")
    safe_path(value)
    return value


def work_hash(body):
    # Only RAM's owned link block is excluded; native work/constraints stay authoritative.
    return digest(BLOCK.sub(b"", body).rstrip(b"\r\n"))


def encoded(value):
    body = ts.encoded(value) + b"\n"
    health.emit("ram-record-growth", len(body), ts.MAX_SOURCE_BYTES,
                "RAM record crossed the 128 KiB glass ceiling; preserve it and inspect revision or receipt growth.",
                unit="bytes")
    return body


class Store:
    def __init__(self, root: LocalRoot, config_path="eutonos.ram.json", denied_paths=()):
        require_shared(root)
        self.root, self.config_path = root, safe_path(config_path)
        self.denied_paths = frozenset(denied_paths)
        self.advisories = []
        ts.require(config_path not in self.denied_paths, "denied", "RAM config is not public in this Hub.")
        self.config_bytes = root.read(config_path)
        self.cfg = unpack(self.config_bytes)
        ts.shape(self.cfg, {"profile", "repository_id", "scope_id", "namespace", "store_root", "current_task", "work_owners"})
        ts.require(self.cfg["profile"] == CONFIG, "unsupported", "Unsupported RAM config.")
        for key in ("repository_id", "scope_id", "namespace"):
            ts.string(self.cfg[key])
        for key in ("store_root", "current_task"):
            safe_path(self.cfg[key])
        ts.require(isinstance(self.cfg["work_owners"], dict) and bool(self.cfg["work_owners"]),
                   "invalid", "Bind native work IDs to explicit owner paths.")
        health.add(self.advisories, "ram-owner-growth", len(self.cfg["work_owners"]), 64,
                   "RAM work-owner bindings crossed the 64-entry glass ceiling; inspect retired or duplicate owners.",
                   unit="bindings")
        for work_id, path in self.cfg["work_owners"].items():
            slug(work_id)
            safe_path(path)
        ts.require(all(not self.inside(p) for p in [config_path, self.cfg["current_task"], *self.cfg["work_owners"].values()]),
                   "invalid", "Native work/config must remain outside RAM storage.")

    def inside(self, path):
        return path.startswith(self.cfg["store_root"] + "/") or path == self.cfg["store_root"]

    def work_owner(self, work_id, read=None):
        """Prefer the unique native board owner and retain explicit mappings for legacy work."""
        slug(work_id)
        from . import boards
        board_path = boards.resolve_owner(self.root, work_id, read=read)
        configured = self.cfg["work_owners"].get(work_id)
        ts.require(board_path is None or configured is None or board_path == configured,
                   "conflict", "Native board owner differs from the RAM work-owner mapping.")
        owner = board_path or configured
        if owner is not None:
            safe_path(owner)
            ts.require(not self.inside(owner), "invalid", "Native work owner cannot be inside RAM storage.")
        return owner

    def path(self, identity):
        ts.shape(identity, {"work_id", "session_id", "issuer_id", "memory_id"})
        for value in identity.values():
            slug(value)
        nested = safe_path(self.cfg["store_root"] + "/" + identity["work_id"] + "/" + identity["session_id"]
                           + "/" + str(len(identity["issuer_id"])) + "-" + identity["issuer_id"] + "--" + identity["memory_id"] + ".json")
        flat = self.flat_path(identity)
        # Keep an issued path, but never choose between two copies of one identity.
        flat_exists = (self.root.root / Path(flat)).is_file()
        nested_exists = (self.root.root / Path(nested)).is_file()
        ts.require(not (flat_exists and nested_exists), "conflict",
                   "RAM identity exists in both flat and nested storage.")
        if flat_exists:
            return flat
        if nested_exists:
            return nested
        # Every new identity uses the flat path, including configured work owners.
        return flat

    def flat_path(self, identity):
        """Return the content-addressed path without consulting issued paths."""
        suffix = hashlib.sha256(ts.encoded(identity)).hexdigest()
        return safe_path(self.cfg["store_root"] + "/record-" + suffix + ".json")

    def load(self, path, read=None):
        safe_path(path)
        ts.require(self.inside(path), "wrong-scope", "Record is outside selected RAM storage.")
        value = unpack((read or self.root.read)(path))
        ts.shape(value, {"profile", "scope_id", "namespace", "identity", "revision", "controls", "versions", "receipts"})
        ts.require(value["profile"] == RECORD and value["scope_id"] == self.cfg["scope_id"]
                   and value["namespace"] == self.cfg["namespace"] and self.path(value["identity"]) == path,
                   "wrong-scope", "RAM identity, scope or profile mismatch.")
        ts.require(type(value["revision"]) is int and value["revision"] >= 1, "invalid", "Invalid RAM revision.")
        controls = value["controls"]
        ts.shape(controls, {"state", "visibility", "expires_at", "updated_at", "reason"})
        ts.require(controls["state"] in {"active", "archived", "suppressed", "tombstoned"}
                   and controls["visibility"] in {"public", "private"}, "unsupported", "Unknown RAM controls.")
        instant(controls["updated_at"]); ts.string(controls["reason"])
        if controls["expires_at"] is not None:
            instant(controls["expires_at"])
        ts.require(isinstance(value["versions"], list)
                   and (bool(value["versions"]) or controls["state"] == "tombstoned"), "invalid", "Invalid RAM version history.")
        ts.require(isinstance(value["receipts"], dict), "invalid", "Invalid RAM operation history.")
        health.add(self.advisories, "ram-version-growth", len(value["versions"]), 32,
                   "RAM content revisions crossed the 32-version glass ceiling; inspect repeated correction behavior.",
                   unit="versions")
        health.add(self.advisories, "ram-receipt-growth", len(value["receipts"]), 64,
                   "RAM operation receipts crossed the 64-entry glass ceiling; inspect retry or lifecycle behavior.",
                   unit="receipts")
        for version in value["versions"]:
            ts.shape(version, {"revision", "kind", "summary", "recorded_at", "occurred_at", "unfinished", "creator", "actors",
                               "work", "selection", "sources", "children", "skill", "reason"}, {"applicability"})
            applicability(version.get("applicability"))
            ts.require(type(version["revision"]) is int and 1 <= version["revision"] <= value["revision"], "invalid", "Invalid content revision.")
            self.content(version)
            ts.shape(version["creator"], {"id", "attribution", "model"})
            ts.string(version["creator"]["id"])
            ts.require(version["creator"]["attribution"] == "host-supplied" and version["creator"]["model"] is None,
                       "unsupported", "Unsupported recorder/model attribution.")
            ts.shape(version["work"], {"work_id", "path", "commit", "sha256"}, {"source_state"})
            ts.require(version["work"]["work_id"] == value["identity"]["work_id"], "conflict", "Experience work identity differs.")
            safe_path(version["work"]["path"])
            ts.require(version["work"].get("source_state", "committed") in {"committed", "working-tree"},
                       "unsupported", "Unsupported RAM work-owner source state.")
            ts.shape(version["selection"], {"path", "work_content_sha256"})
            safe_path(version["selection"]["path"])
            for source in ts.sequence(version["sources"]):
                ts.shape(source, {"path", "evidence", "commit", "sha256", "git_blob"}, {"check_path", "check_source"})
                safe_path(source["path"])
                ts.require(isinstance(source["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", source["sha256"])),
                           "invalid", "Invalid RAM source fingerprint.")
            for child in ts.sequence(version["children"]):
                ts.shape(child, {"identity", "path", "revision"})
                safe_path(child["path"])
                ts.require(self.path(child["identity"]) == child["path"], "conflict", "Child identity and binding differ.")
                ts.require(type(child["revision"]) is int and child["revision"] >= 1, "invalid", "Invalid child revision.")
        ts.require([v["revision"] for v in value["versions"]] == sorted(set(v["revision"] for v in value["versions"])),
                   "invalid", "RAM content revisions must be unique and increasing.")
        return value

    @staticmethod
    def content(spec):
        ts.require(spec["kind"] in {"working-context", "summary", "skill-binding", "milestone"}, "unsupported", "Unsupported RAM family.")
        ts.require(isinstance(spec["summary"], str) and bool(spec["summary"]),
                   "invalid", "Experience summary must be nonempty.")
        health.emit("ram-summary-growth", len(spec["summary"].encode()), 8192,
                    "RAM summary crossed the 8 KiB glass ceiling; inspect whether it should be split into sources.",
                    unit="bytes")
        instant(spec["recorded_at"])
        if spec["occurred_at"] is not None:
            instant(spec["occurred_at"])
        ts.require(type(spec["unfinished"]) is bool, "invalid", "Unfinished context must be explicit.")
        ts.string(spec["reason"])
        for actor in ts.sequence(spec["actors"]):
            ts.shape(actor, {"id", "attribution"})
            ts.string(actor["id"])
            ts.require(actor["attribution"] in {"supplied", "self-reported", "host-observed"}, "unsupported", "Unsupported actor attribution.")
        if spec["kind"] == "skill-binding":
            ts.require(spec["skill"] is not None, "invalid", "A skill binding needs a canonical procedure reference.")
            ts.shape(spec["skill"], {"path", "required_tools", "constraints", "verification_path"})
            safe_path(spec["skill"]["path"]); safe_path(spec["skill"]["verification_path"])
            for tool in ts.sequence(spec["skill"]["required_tools"]):
                ts.string(tool)
            ts.string(spec["skill"]["constraints"])
        else:
            ts.require(spec["skill"] is None, "invalid", "Only skill bindings carry procedure metadata.")

    def link(self, body, work_id, path, remove=False):
        blocks = list(BLOCK.finditer(body))
        ts.require(len(blocks) <= 1 and body.count(b"```tokenslang-ram-links") == len(blocks), "ambiguous", "Competing or malformed native RAM link block.")
        value = unpack(blocks[0][1]) if blocks else {"profile": LINKS_V2, "links": []}
        if value.get("profile") == LINKS:
            ts.shape(value, {"profile", "work_id", "memories"})
            ts.string(value["work_id"])
            value = {"profile": LINKS_V2,
                     "links": [{"work_id": value["work_id"], "memories": list(ts.sequence(value["memories"]))}]}
        ts.shape(value, {"profile", "links"})
        ts.require(value["profile"] == LINKS_V2, "unsupported", "Unsupported native RAM link profile.")
        links = ts.sequence(value["links"])
        by_work = {}
        for entry in links:
            ts.shape(entry, {"work_id", "memories"})
            slug(entry["work_id"])
            ts.require(entry["work_id"] not in by_work, "ambiguous", "Duplicate native RAM work link identity.")
            refs = list(ts.sequence(entry["memories"]))
            ts.require(len(refs) == len(set(refs)), "ambiguous", "Duplicate native RAM memory link.")
            for ref in refs:
                safe_path(ref)
            by_work[entry["work_id"]] = refs
        refs = by_work.setdefault(work_id, [])
        if (path in refs) != remove:
            return body
        if remove:
            refs.remove(path)
            if not refs:
                by_work.pop(work_id)
        else:
            refs.append(path)
        value["links"] = [{"work_id": selected, "memories": by_work[selected]}
                          for selected in sorted(by_work)]
        block = b"```tokenslang-ram-links\n" + ts.encoded(value) + b"\n```"
        return body[:blocks[0].start()] + block + body[blocks[0].end():] if blocks else body + b"\n\n" + block + b"\n"

    def plan(self, request, recorder):
        """Zero-write plan. The host binds recorder and separately admits exact paths."""
        ts.shape(request, {"profile", "operation_id", "operation", "identity", "expected_revision", "at", "reason"},
                 {"experience", "controls"})
        ts.require(request["profile"] == WRITE, "unsupported", "Unsupported RAM write profile.")
        ts.string(request["operation_id"]); ts.string(recorder); instant(request["at"]); ts.string(request["reason"])
        operation = request["operation"]
        ts.require(operation in {"capture", "correct", "control", "forget"}, "unsupported", "Unknown RAM mutation.")
        ts.require(("experience" in request) == (operation in {"capture", "correct"})
                   and ("controls" in request) == (operation == "control"), "invalid", "Mutation fields do not match the selected operation.")
        expected = request["expected_revision"]
        ts.require(expected is None or type(expected) is int and expected >= 1, "invalid", "Expected revision is invalid.")
        observed = {}
        io_budget = [0, 0]
        def read(path):
            body = self.root.read(path)
            io_budget[0] += 1; io_budget[1] += len(body or b"")
            health.add(self.advisories, "ram-plan-read-growth", io_budget[0], 512,
                       "RAM plan crossed the read-count glass ceiling; planning continued.", unit="reads")
            health.add(self.advisories, "ram-plan-byte-growth", io_budget[1], 4194304,
                       "RAM plan crossed the observed-byte glass ceiling; planning continued.", unit="bytes")
            health.add(self.advisories, "ram-plan-path-growth", len(observed.keys() | {path}), 64,
                       "RAM plan crossed the observed-path glass ceiling; planning continued.", unit="paths")
            if path in observed:
                ts.require(observed[path] == digest(body), "stale", "RAM input changed during observation.")
            observed[path] = digest(body)
            return body
        ts.require(read(self.config_path) == self.config_bytes, "stale", "RAM config changed; reopen it.")
        path = self.path(request["identity"])
        before = read(path)
        record = self.load(path, read) if before else None
        request_hash = digest(ts.encoded({"request": request, "recorder": recorder}))
        if record and request["operation_id"] in record["receipts"]:
            ts.require(record["receipts"][request["operation_id"]] == request_hash, "conflict", "Logical operation ID already names different content.")
            updates = {}
        else:
            ts.require((record is None) == (operation == "capture") and expected == (record["revision"] if record else None),
                       "conflict", "Record exists or expected revision changed; preserve both observations.")
            if record:
                ts.require(record["controls"]["state"] != "tombstoned", "denied", "Tombstoned identity cannot be restored.")
                record["revision"] += 1
            else:
                record = {"profile": RECORD, "scope_id": self.cfg["scope_id"], "namespace": self.cfg["namespace"],
                          "identity": request["identity"], "revision": 1, "versions": [], "receipts": {},
                          "controls": {"state": "active", "visibility": "public", "expires_at": None,
                                       "updated_at": request["at"], "reason": request["reason"]}}
            updates = {}
            if operation in {"capture", "correct"}:
                spec = request["experience"]
                ts.shape(spec, {"kind", "summary", "recorded_at", "occurred_at", "unfinished", "actors", "work_path",
                                "source_commit", "sources", "children", "skill"}, {"applicability", "child_revisions"})
                assessment = applicability(spec.get("applicability"))
                self.content({**spec, "reason": request["reason"]})
                ts.require(self.work_owner(request["identity"]["work_id"], read=read) == spec["work_path"],
                           "denied", "Native work ID/owner binding differs from RAM config.")
                work = read(spec["work_path"])
                ts.require(work is not None, "missing", "Native work owner is absent.")
                task = read(self.cfg["current_task"])
                ts.require(task is not None, "missing", "Current Task owner is absent.")
                view = GitView(self.root.root, spec["source_commit"])
                ts.require(spec["source_commit"] == view.commit, "invalid", "Experience source requires an exact commit, not a moving ref.")
                try:
                    work_source, _ = view.read(spec["work_path"])
                    work_source_state = "committed"
                except ts.ReadError as exc:
                    if exc.code != "missing":
                        raise
                    # A freshly recorded Quick-work owner can legitimately exist only
                    # in the working tree. Preserve that distinction instead of
                    # pretending the owner was present at source_commit.
                    work_source = work
                    work_source_state = "working-tree"
                sources = []
                for source in ts.sequence(spec["sources"]):
                    ts.shape(source, {"path", "evidence"}, {"check_path"})
                    ts.require(not self.inside(source["path"]), "invalid", "Use child references for RAM dependencies.")
                    ts.require(source["evidence"] in {"supplied", "inspected", "proposed", "verified", "accepted"}, "unsupported", "Unsupported evidence class.")
                    data, evidence = view.read(source["path"])
                    entry = {**source, "commit": view.commit, "sha256": digest(data), "git_blob": evidence["git_blob"]}
                    if source["evidence"] in {"verified", "accepted"}:
                        ts.require("check_path" in source, "invalid", "Verification/acceptance assertion needs its source route.")
                    if "check_path" in source:
                        _, entry["check_source"] = view.read(source["check_path"])
                    sources.append(entry)
                ts.require(sources, "invalid", "A useful experience needs at least one source route.")
                if spec["skill"]:
                    ts.require({spec["skill"]["path"], spec["skill"]["verification_path"]} <= {s["path"] for s in sources},
                               "invalid", "Skill and verification routes must be pinned sources.")
                children = []
                expected_children = spec.get("child_revisions")
                if "child_revisions" in spec:
                    ts.require(isinstance(expected_children, list), "invalid", "Child revisions must be native pinned references.")
                    for expected_child in expected_children:
                        ts.shape(expected_child, {"identity", "path", "revision"})
                        ts.require(self.path(expected_child["identity"]) == expected_child["path"]
                                   and type(expected_child["revision"]) is int and expected_child["revision"] >= 1,
                                   "invalid", "Invalid expected child identity, path or revision.")
                    ts.require([child["path"] for child in expected_children] == spec["children"],
                               "invalid", "Expected child revisions must cover the exact ordered child paths.")
                for child_path in ts.sequence(spec["children"]):
                    ts.require(child_path != path, "invalid", "A memory cannot derive from itself.")
                    child = self.load(child_path, read)
                    self.eligible(child_path, child, read=read)
                    reference = {"identity": child["identity"], "path": child_path, "revision": child["versions"][-1]["revision"]}
                    if expected_children is not None:
                        ts.require(reference == expected_children[len(children)], "stale",
                                   "Child RAM changed from the exact requested identity/revision before planning.")
                    children.append(reference)
                ts.require(spec["kind"] == "milestone" or not children, "invalid", "Only milestone rollups carry child trails.")
                if operation == "correct":
                    ts.require(record["versions"][-1]["work"]["path"] == spec["work_path"], "conflict", "Correction cannot reassign the native work owner.")
                version = {k: spec[k] for k in ("kind", "summary", "recorded_at", "occurred_at", "unfinished", "actors", "skill")}
                version.update(revision=record["revision"], creator={"id": recorder, "attribution": "host-supplied", "model": None},
                               work={"work_id": request["identity"]["work_id"], "path": spec["work_path"], "commit": view.commit,
                                     "sha256": digest(work_source), "source_state": work_source_state},
                               selection={"path": self.cfg["current_task"], "work_content_sha256": work_hash(task)},
                               sources=sources, children=children, reason=request["reason"], applicability=assessment)
                record["versions"].append(version)
                updates[spec["work_path"]] = self.link(work, request["identity"]["work_id"], path,
                    remove=record["controls"]["state"] == "suppressed" or record["controls"]["visibility"] == "private")
            elif operation == "control":
                controls = request["controls"]
                ts.shape(controls, {"state", "visibility", "expires_at"})
                ts.require(controls["state"] in {"active", "archived", "suppressed"} and controls["visibility"] in {"public", "private"},
                           "unsupported", "Unsupported RAM control change.")
                ts.require(not (controls["state"] == "archived" and record["versions"][-1]["unfinished"]),
                           "denied", "Unfinished context is protected from routine archive.")
                if controls["expires_at"] is not None:
                    instant(controls["expires_at"])
                record["controls"] = {**controls, "updated_at": request["at"], "reason": request["reason"]}
            else:
                ts.require(record["controls"]["state"] in {"archived", "suppressed"} and not record["versions"][-1]["unfinished"],
                           "denied", "Archive/suppress completed context before explicit forgetting.")
                record["controls"].update(state="tombstoned", updated_at=request["at"], reason=request["reason"])
            if operation in {"control", "forget"}:
                owner = record["versions"][-1]["work"]["path"]
                ts.require(self.work_owner(record["identity"]["work_id"], read=read) == owner,
                           "conflict", "Native work owner changed; reconcile its binding before editing links.")
                work = read(owner)
                ts.require(work is not None, "missing", "Native owner needed for RAM link maintenance.")
                updates[owner] = self.link(work, record["identity"]["work_id"], path,
                    remove=record["controls"]["state"] in {"suppressed", "tombstoned"} or record["controls"]["visibility"] == "private")
            if operation == "forget":
                record["versions"] = []
            record["receipts"][request["operation_id"]] = request_hash
            health.add(self.advisories, "ram-version-growth", len(record["versions"]), 32,
                       "RAM content revisions crossed the 32-version glass ceiling; planning continued.", unit="versions")
            health.add(self.advisories, "ram-receipt-growth", len(record["receipts"]), 64,
                       "RAM operation receipts crossed the 64-entry glass ceiling; planning continued.", unit="receipts")
            updates[path] = encoded(record)
        effect = plan(self.root, updates, inputs=observed)
        ts.require(all(effect["observed"].get(p) == h for p, h in observed.items()), "stale", "RAM changed while planning.")
        validate_plan(self.root, effect)
        result = {"profile": PLAN, "recorder": recorder, "record_path": path, "effect": effect,
                  "fingerprint": digest(ts.encoded(effect)), "operation_id": request["operation_id"]}
        if self.advisories:
            result["advisories"] = [dict(value) for value in self.advisories]
        return result

    def eligible(self, path, record, read=None, seen=None):
        """Current controls guard every version and recursively derived milestone."""
        ts.require(path not in self.denied_paths, "denied", "RAM record is not public in this Hub.")
        seen = set() if seen is None else seen
        ts.require(path not in seen, "conflict", "RAM dependency cycle detected.")
        health.add(self.advisories, "ram-dependency-depth", len(seen) + 1, 16,
                   "RAM dependency depth crossed the 16-record glass ceiling; validation continued.", unit="records")
        seen = seen | {path}
        c = record["controls"]
        ts.require(c["state"] in {"active", "archived"} and c["visibility"] == "public"
                   and (c["expires_at"] is None or instant(c["expires_at"]) > datetime.now(timezone.utc)),
                   "denied", "RAM record is suppressed, expired, private or forgotten.")
        for version in record["versions"][-1:]:
            ts.require(not any(s["path"] in self.denied_paths for s in version["sources"])
                       and version["work"]["path"] not in self.denied_paths, "denied", "RAM relies on an ineligible Hub source.")
            for child in version["children"]:
                obtained = self.load(child["path"], read)
                self.eligible(child["path"], obtained, read, seen)
                ts.require(obtained["versions"][-1]["revision"] == child["revision"], "stale", "Milestone needs correction after a child changed.")

    def read(self, request):
        ts.shape(request, {"profile", "operation"}, {"identity", "revision", "work_id", "session_id", "include_archived", "query",
                 "time_field", "from", "to", "limit", "cursor", "max_bytes", "summary_only", "summary_chars", "sort", "order"})
        ts.require(request["profile"] == READ and request["operation"] in {"describe", "get", "browse"}, "unsupported", "Unknown RAM read profile/operation.")
        budget, limit = request.get("max_bytes", 32768), request.get("limit", 16)
        ts.require(type(budget) is int and budget >= 512 and type(limit) is int and limit >= 1, "invalid", "Invalid RAM output bounds.")
        health.add(self.advisories, "ram-output-budget-growth", budget, 32768,
                   "RAM output budget crossed the 32 KiB glass ceiling; paged retrieval continued.", unit="bytes")
        health.add(self.advisories, "ram-page-growth", limit, 64,
                   "RAM page size crossed the 64-item glass ceiling; retrieval continued.", unit="items")
        ts.require(type(request.get("include_archived", False)) is bool, "invalid", "Archive selection must be explicit.")
        ts.require(type(request.get("summary_only", False)) is bool, "invalid", "Summary projection selection must be explicit.")
        summary_chars = request.get("summary_chars", 600)
        ts.require(type(summary_chars) is int and 80 <= summary_chars <= 2000,
                   "invalid", "Summary previews must contain 80 to 2000 characters.")
        sort_by, order = request.get("sort", "path"), request.get("order", "asc")
        ts.require(sort_by in {"path", "recorded_at", "occurred_at", "work_id", "memory_id"}
                   and order in {"asc", "desc"}, "invalid", "Select a supported RAM sort and order.")
        observed = {}
        io_budget = [0, 0]
        def read(path):
            body = self.root.read(path)
            io_budget[0] += 1; io_budget[1] += len(body or b"")
            health.add(self.advisories, "ram-read-growth", io_budget[0], 512,
                       "RAM retrieval crossed the read-count glass ceiling; retrieval continued.", unit="reads")
            health.add(self.advisories, "ram-read-byte-growth", io_budget[1], 4194304,
                       "RAM retrieval crossed the observed-byte glass ceiling; retrieval continued.", unit="bytes")
            health.add(self.advisories, "ram-read-path-growth", len(observed.keys() | {path}), 64,
                       "RAM retrieval crossed the observed-path glass ceiling; retrieval continued.", unit="paths")
            ts.require(path not in observed or observed[path] == digest(body), "stale", "RAM changed during retrieval.")
            observed[path] = digest(body)
            return body
        ts.require(read(self.config_path) == self.config_bytes, "stale", "RAM config changed; reopen the store.")
        result = {"profile": READ, "outcome": "ok", "repository_id": self.cfg["repository_id"],
                  "scope_id": self.cfg["scope_id"], "namespace": self.cfg["namespace"],
                  "view": "observed-local-files; publication-unverified", "authorization": "none; consult native Current Task",
                  "verification": "retrieval; evidence labels remain attributed assertions"}
        if request["operation"] == "describe":
            result.update(operations=["get", "browse"], writes=False, models=False, skill_execution=False)
            ts.require(len(ts.encoded(result)) <= budget, "overflow", "RAM description exceeds output budget.")
            return result
        task = read(self.cfg["current_task"])
        ts.require(task is not None and self.cfg["current_task"] not in self.denied_paths, "denied", "Current Task source is unavailable.")
        if request["operation"] == "get":
            paths = [self.path(request.get("identity"))]
        else:
            for key in ("work_id", "session_id"):
                if key in request:
                    slug(request[key])
            # Bounded discovery only. Missing unpushed peer work remains unknown.
            git = GitView(self.root.root, "HEAD")
            selected_root = self.cfg["store_root"]
            raw = git._git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ":(literal)" + selected_root)
            names = raw.decode("utf-8").split("\0")
            paths = sorted({p for p in names if p.endswith(".json") and self.inside(p)})
            if "work_id" in request or "session_id" in request:
                def candidate(path):
                    parts = path[len(selected_root) + 1:].split("/")
                    if len(parts) == 1:  # Flat paths carry no work/session hint.
                        return True
                    return (len(parts) == 3
                            and ("work_id" not in request or parts[0] == request["work_id"])
                            and ("session_id" not in request or parts[1] == request["session_id"]))
                paths = [p for p in paths if candidate(p)]
            health.add(self.advisories, "ram-discovery-growth", len(paths), 256,
                       "RAM discovery crossed the 256-file glass ceiling; stable paging continued.", unit="files")
        selected = []
        time_field = request.get("time_field", "occurred_at")
        ts.require(time_field in {"recorded_at", "occurred_at"}, "invalid", "Select event time or recording time explicitly.")
        ts.require(("from" in request) == ("to" in request), "invalid", "Temporal range needs both bounds.")
        start, end = (instant(request["from"]), instant(request["to"])) if "from" in request else (None, None)
        ts.require(start is None or start < end, "invalid", "Temporal range must increase.")
        query = ts.string(request["query"]).casefold() if "query" in request else None
        for path in paths:
            body = read(path)
            if body is None:
                continue
            if request["operation"] == "browse" and ("work_id" in request or "session_id" in request) \
                    and path.startswith(self.cfg["store_root"] + "/record-"):
                # A flat filename carries no work/session hint. Skip a malformed
                # record only when its stored identity and hash prove it belongs
                # outside this scope; ambiguous or matching corruption still fails.
                try:
                    envelope = unpack(body)
                    identity = envelope.get("identity") if isinstance(envelope, dict) else None
                    if isinstance(identity, dict) and set(identity) == {"work_id", "session_id", "issuer_id", "memory_id"}:
                        for component in identity.values():
                            slug(component)
                        if self.flat_path(identity) == path and any(
                            key in request and request[key] != identity[key] for key in ("work_id", "session_id")
                        ):
                            continue
                except ts.ReadError:
                    pass
            record = self.load(path, read)
            try:
                self.eligible(path, record, read)
            except ts.ReadError as exc:
                if request["operation"] == "get" or exc.code not in {"denied", "stale", "missing"}:
                    raise
                continue  # No private/suppressed metadata or stale derivative summaries.
            if record["controls"]["state"] == "archived" and not request.get("include_archived", False):
                if request["operation"] == "get":
                    raise ts.ReadError("archived", "Explicitly include archived history.")
                continue
            if any(key in request and request[key] != record["identity"][key] for key in ("work_id", "session_id")):
                continue
            version = record["versions"][-1]
            if "revision" in request:
                ts.require(request["operation"] == "get" and type(request["revision"]) is int, "invalid", "Version selection requires an exact get.")
                found = [v for v in record["versions"] if v["revision"] == request["revision"]]
                ts.require(len(found) == 1, "missing", "Requested content revision is absent.")
                version = found[0]
                # Historical derived content still needs current child controls.
                self.eligible(path, {**record, "versions": [version]}, read)
            if query and query not in version["summary"].casefold():
                continue
            stamp = version[time_field]
            if start and (stamp is None or not start <= instant(stamp) < end):
                continue
            source_states = []
            if not request.get("summary_only", False):
                for source in version["sources"]:
                    data = read(source["path"])
                    legacy = "missing" if data is None else "unchanged-bytes" if digest(data) == source["sha256"] else "changed"
                    source_states.append({"path": source["path"], "state": legacy,
                                          "source_freshness": "matching" if legacy == "unchanged-bytes" else legacy})
            freshness_values = [source["source_freshness"] for source in source_states]
            source_freshness = ("unknown" if not freshness_values else "missing" if "missing" in freshness_values else
                                "changed" if "changed" in freshness_values else "matching")
            item = {"identity": record["identity"], "path": path, "record_revision": record["revision"],
                    "record_sha256": observed[path], "source_states": source_states,
                    "source_freshness": source_freshness,
                    "semantic_applicability": applicability(version.get("applicability")),
                    "current_task_changed": self.cfg["current_task"] != version["selection"]["path"]
                        or work_hash(task) != version["selection"]["work_content_sha256"],
                    "historical": version["revision"] != record["versions"][-1]["revision"]}
            if request.get("summary_only", False):
                summary = version["summary"]
                preview = summary if len(summary) <= summary_chars else summary[:summary_chars].rstrip() + "..."
                item.update(summary=preview, summary_truncated=preview != summary, kind=version["kind"],
                            recorded_at=version["recorded_at"], occurred_at=version["occurred_at"],
                            unfinished=version["unfinished"], creator=version["creator"],
                            visibility=record["controls"]["visibility"], state=record["controls"]["state"],
                            source_state_coverage="not-read-in-summary-list")
            else:
                item.update(experience=version, controls=record["controls"])
            selected.append(item)
        def sort_value(item):
            if sort_by == "path":
                return item["path"]
            if sort_by in {"work_id", "memory_id"}:
                return item["identity"][sort_by]
            return item.get(sort_by) if request.get("summary_only", False) else item["experience"].get(sort_by)
        if order == "desc":
            selected.sort(key=lambda item: (sort_value(item) is not None, sort_value(item) or "", item["path"]), reverse=True)
        else:
            selected.sort(key=lambda item: (sort_value(item) is None, sort_value(item) or "", item["path"]))
        if self.advisories:
            result["advisories"] = [dict(value) for value in self.advisories]
        key = digest(ts.encoded({"observed": observed, "request": {k: v for k, v in request.items() if k not in {"cursor", "max_bytes", "limit"}}}))
        offset = 0
        if "cursor" in request:
            ts.shape(request["cursor"], {"key", "offset"})
            offset = request["cursor"]["offset"]
            ts.require(request["cursor"]["key"] == key and type(offset) is int and 0 <= offset <= len(selected), "stale", "RAM continuation changed; retrieve current controls again.")
        items = []
        for item in selected[offset:offset+limit]:
            if len(ts.encoded({**result, "items": items + [item]})) + 220 > budget:
                break
            items.append(item)
        position = offset + len(items)
        result.update(items=items, complete=position == len(selected),
                      outcome="overflow" if not items and position < len(selected) else "ok",
                      continuation={"key": key, "offset": position} if position < len(selected) else None)
        # Fence the whole projection against cooperative edits between reads.
        ts.require(all(digest(self.root.read(p)) == h for p, h in observed.items()), "stale", "RAM controls/source changed during retrieval.")
        if len(ts.encoded(result)) > budget:
            return {"profile": READ, "outcome": "overflow", "complete": False,
                    "continuation": {"key": key, "offset": offset}, "required_bytes": len(ts.encoded(result)),
                    "action": "Increase max_bytes or request one exact identity; no experience was truncated."}
        return result


def apply(coordinator, authority, attempt, generation, value, expected_fingerprint):
    ts.shape(value, {"profile", "recorder", "record_path", "effect", "fingerprint", "operation_id"}, {"advisories"})
    ts.require(value["profile"] == PLAN and value["recorder"] == authority.actor
               and value["fingerprint"] == expected_fingerprint == digest(ts.encoded(value["effect"])),
               "conflict", "RAM plan/recorder differs from the host-selected plan.")
    return coordinator.apply(authority, attempt, generation,
                             "ram-" + digest(ts.encoded([attempt, value["operation_id"], value["fingerprint"]])), value["effect"])
