"""Guarded adaptation of RNAVL17G's dynamic sixteen-sweep guided runner.

Workers return proposals through a current-agent protocol. No model dispatch,
subprocess worker, hidden escalation or source writes are provided here.
"""
from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import re

from . import tokenslang as ts
from .effects import digest, plan
from . import nav

STATE = "eutonos.tokenslang.nav-run.v1"
REPORT = "eutonos.tokenslang.nav-report.v1"
PACKET = "eutonos.tokenslang.nav-packet.v1"
MAX_PACKET = 65536
NO_BASELINE = "no-baseline"
BASELINE_NEXT = ("No baseline run receipt (NAV_RUN.json) exists, and no import command is exposed. The supported way to create one "
                 "is a guarded build cycle: `nav history-plan --run-id UNIQUE`, reserve the planned paths on a host attempt, then "
                 "`nav start --mode build --run-id UNIQUE --host HOST --actor ACTOR --attempt ATTEMPT --generation N` and work the "
                 "queue (`nav next`, `nav report-template`, `nav submit`). `eutonos-nav --help` lists these commands; the long procedure is docs/DEPLOYMENT_STARTUP.md#eutonos-proc-nav-maintain in a EUTONOS source checkout. "
                 "Until then maps are readable but their freshness is unknown.")


def lead(sweep, key, paths=(), records=()):
    return {"id": "RNAVL-lead-" + digest((sweep + "\0" + key).encode())[:18],
            "key": key, "paths": sorted(set(paths)), "recordIDs": sorted(set(records))}


def leads(sweep, records, snapshot):
    """Native selectors, recomputed after every accepted packet."""
    selector, sid = sweep["selector"], sweep["id"]
    if selector in {"source-clusters", "eligible-path-clusters", "parent-source-probes"}:
        groups = {}
        for path in snapshot["paths"]:
            key = "/".join(path.split("/")[:2]) if path.count("/") > 1 else path.split("/")[0]
            groups.setdefault(key, []).append(path)
        return [lead(sid, key + ":" + str(offset), paths[offset:offset+20])
                for key, paths in sorted(groups.items()) for offset in range(0, len(paths), 20)]
    selected = list(records.values())
    kinds = {"source-family-records": {"source-family"}, "architecture-records": {"area", "system", "source-family", "gap"},
             "surface-records": {"surface"}, "surface-interaction-records": {"surface", "interaction"},
             "journey-records": {"journey"}, "parent-surface-records": {"surface"},
             "unresolved-surface-records": {"surface", "interaction", "gap"}}
    if selector in kinds:
        selected = [r for r in selected if r["fields"]["Kind"] in kinds[selector]]
    if selector == "unresolved-surface-records":
        selected = [r for r in selected if r["fields"]["Review"] in {"candidate", "stale", "gap"}]
    if selector == "parent-surface-records":
        children = {cid for r in selected for cid in nav.values(r["fields"].get("Children", ""))}
        selected = [r for r in selected if r["id"] not in children and not nav.values(r["fields"].get("Parent", ""))]
    if selector == "semantic-records-by-source":
        groups = {}
        for r in selected:
            key = r["evidence"][0].split("/")[0] if r["evidence"] else "unlocated"
            groups.setdefault(key, []).append(r)
        return [lead(sid, key + ":" + str(offset), [p for r in group[offset:offset+8] for p in r["evidence"]],
                     [r["id"] for r in group[offset:offset+8]])
                for key, group in sorted(groups.items()) for offset in range(0, len(group), 8)]
    return [lead(sid, r["id"], r["evidence"], [r["id"]]) for r in sorted(selected, key=lambda r: r["id"])]


CHANGE_CAP = 20
QUALIFY_VALIDATE = "Mechanical validation only: no semantic acceptance, no state written, no retry consumed."


def path_changes(before, after, cap=CHANGE_CAP):
    """Bounded, sorted added/removed/modified summary between two {path: hash} maps."""
    before, after = before or {}, after or {}
    groups = {"added": sorted(after.keys() - before.keys()), "removed": sorted(before.keys() - after.keys()),
              "modified": sorted(p for p in before.keys() & after.keys() if before[p] != after[p])}
    out, room = {"total": sum(len(v) for v in groups.values())}, cap
    for key, paths in groups.items():
        out[key] = paths[:room]
        room -= len(out[key])
    out["omitted"] = out["total"] - sum(len(out[k]) for k in groups)
    return out


def describe_changes(changes):
    if not changes["total"]:
        return "none"
    parts = [f"{k} {', '.join(changes[k])}" for k in ("added", "removed", "modified") if changes[k]]
    return "; ".join(parts) + (f"; and {changes['omitted']} more" if changes["omitted"] else "")


class Session:
    def __init__(self, root, config_path="eutonos.nav.json"):
        self.root = root
        self.config_path = nav.safe_path(config_path)
        self.config_bytes = root.read(config_path)
        self.cfg = nav.config(self.config_bytes)
        ts.require(not nav.under(config_path, [self.cfg["map_root"]]), "invalid", "Config must be outside NAV output.")
        self.names = {name: self.cfg["map_root"] + "/" + name for name in nav.MAPS}
        self.state_path = self.cfg["map_root"] + "/NAV_RUN.json"
        self.history_path = self.cfg["map_root"] + "/NAV_HISTORY.json"
        self.paths = frozenset([*self.names.values(), self.state_path, self.history_path])

    def observe(self, bootstrap=False):
        ts.require(self.root.read(self.config_path) == self.config_bytes, "stale", "NAV configuration changed; reopen the session.")
        source = nav.inventory(self.root, self.cfg, self.config_path)
        originals = {name: self.root.read(path) for name, path in self.names.items()}
        if any(body is None for body in originals.values()):
            ts.require(bootstrap and all(body is None for body in originals.values()), "missing", "NAV is not initialized: not all four native maps exist. Supply all four, or bootstrap an empty NAV directory with a guarded `nav start --mode build`.")
            bodies = {name: files("eutonos").joinpath("nav_data/" + name.removesuffix(".md") + ".template.txt").read_bytes()
                      .replace(b"{{GOVERNING_SOURCE}}", self.cfg["context_paths"][0].encode()) for name in nav.MAPS}
        else:
            bodies = originals.copy()
        records, preambles = nav.parse_maps(bodies)
        raw_state = self.root.read(self.state_path)
        state = ts.loads(nav.decode(raw_state)) if raw_state else None
        if state:
            ts.require(isinstance(state, dict) and state.get("profile") == STATE, "unsupported", "Unsupported NAV run state.")
        observed = {self.config_path: digest(self.config_bytes), self.state_path: digest(raw_state),
                    self.history_path: digest(self.root.read(self.history_path)),
                    **{self.names[k]: digest(v) for k, v in originals.items()}}
        return source, bodies, records, preambles, state, observed

    def persist(self, host, source, observed, updates):
        coordinator, authority, attempt, generation = host
        ts.require(authority.root.root == self.root.root, "wrong-scope", "NAV host selected another root.")
        effect = plan(self.root, updates, inputs=observed)
        ts.require(effect["observed"] == observed, "stale", "NAV inputs changed during planning.")
        def check():
            ts.require(self.root.read(self.config_path) == self.config_bytes, "stale",
                       "NAV configuration changed; preserve the packet and explicitly restart.")
            now = nav.inventory(self.root, self.cfg, self.config_path)
            ts.require(now["fingerprint"] == source["fingerprint"], "stale",
                       "NAV source changed; preserve the packet and explicitly restart. Changed source: "
                       + describe_changes(path_changes(source["paths"], now["paths"])) + ".")
        return coordinator.apply(authority, attempt, generation,
                                 "nav-" + digest(ts.encoded({"attempt": attempt, "plan": effect})), effect, precondition=check)

    def no_baseline(self, source, bodies, records):
        """Maps exist but no baseline run does: report what is present, claim nothing fresh."""
        return {"outcome": NO_BASELINE, "initialized": "maps-present-no-baseline-run", "maps": sorted(bodies),
                "records": len(records), "baselineRun": None, "sourceFingerprint": source["fingerprint"],
                "sourceCurrent": "unknown", "mapsCurrent": "unknown", "changedPaths": "unknown",
                "affectedRecords": "unknown", "semanticAcceptance": "unproven",
                "providerInvocations": "host-owned; not collected", "next": BASELINE_NEXT,
                "qualification": "Maps parsed; without a baseline run, freshness and semantic completeness are unknown."}

    def maintenance(self):
        source, bodies, records, _, state, _ = self.observe()
        if state is None:
            return self.no_baseline(source, bodies, records)
        previous = state["baseline"]
        changed = sorted({p for p in source["paths"].keys() | previous.keys() if source["paths"].get(p) != previous.get(p)}
                         | set(state.get("debtPaths", [])))
        affected = {r["id"] for r in records.values() if set(r["evidence"]) & set(changed)} | set(state.get("debtRecords", []))
        if set(changed) & set(self.cfg["context_paths"]):
            affected.update(records)
        # Reverse reference closure nominates review, not proven semantic impact.
        while True:
            expanded = affected | {r["id"] for r in records.values() if set(r["related"]) & affected}
            if expanded == affected:
                break
            affected = expanded
        mapped = {p for r in records.values() for p in r["evidence"]}
        return {"outcome": "review-needed" if changed or affected else "no-op", "changedPaths": changed,
                "affectedRecords": sorted(affected & records.keys()), "unmappedPaths": sorted(set(changed) - mapped),
                "sourceFingerprint": source["fingerprint"], "qualification": "Known evidence and reference impact only; semantic completeness unproven."}

    @staticmethod
    def bound(state):
        # Durable progress is not a model response. Its source baseline and
        # receipts must survive growth; packet/read budgets are enforced below.
        return ts.encoded(state) + b"\n"

    def packet(self, state, sweep, issued, source, bodies, records):
        """Build one bounded packet without embedding every governing document.

        Exact references retain the complete obligation when source/record text
        will not fit. No excerpt is presented as a complete governing document.
        """
        context = [{"path": p, "sha256": source["paths"][p], "content": None,
                    "contentStatus": "source-reference", "requiredRead": {
                        "path": p, "sha256": source["paths"][p], "view": source["view"]}}
                   for p in self.cfg["context_paths"]]
        record_context = [{"id": rid, "path": self.names[records[rid]["map"]],
                           "sha256": digest(bodies[records[rid]["map"]]), "text": None,
                           "contentStatus": "source-reference"}
                          for rid in sorted({rid for item in issued for rid in item["recordIDs"]})]
        packet = {"profile": PACKET,
                  "packetID": "RNAVL-packet-" + digest(ts.encoded([state["runID"], sweep["id"], issued]))[:24],
                  "sweepID": sweep["id"], "sweep": sweep,
                  "sourceFingerprint": source["fingerprint"], "sourceCommit": source["commit"], "runID": state["runID"],
                  "leads": issued, "requiredContext": context, "rules": nav.CONTRACTS["globalRules"],
                  "recordContext": record_context,
                  "sourceRoutes": [{"path": p, "sha256": source["paths"].get(p),
                                    "bytes": source.get("sizes", {}).get(p),
                                    "cluesStatus": "source-reference", "clues": []}
                                   for p in sorted({p for item in issued for p in item["paths"]})],
                  "reportProfile": REPORT, "retry": 0,
                  "qualification": "Read current source. Source-reference entries require direct reading of the exact file/record (in ranges when needed); their text is not included. Report governing source reads in inspectedPaths and referenced record reads in inspectedRecordIDs. Propose record text only; no model/provider or source-write grant."}
        if len(ts.encoded(packet)) > MAX_PACKET:
            return packet
        for item in context:
            remaining = MAX_PACKET - len(ts.encoded(packet))
            size = source.get("sizes", {}).get(item["path"])
            if size is None or size > remaining:
                continue
            item["content"] = source["contents"][item["path"]]
            item["contentStatus"] = "inline"
            if len(ts.encoded(packet)) > MAX_PACKET:
                item["content"], item["contentStatus"] = None, "source-reference"
        for item in record_context:
            item["text"], item["contentStatus"] = records[item["id"]]["text"], "inline"
            if len(ts.encoded(packet)) > MAX_PACKET:
                item["text"], item["contentStatus"] = None, "source-reference"
        for item in packet["sourceRoutes"]:
            remaining = MAX_PACKET - len(ts.encoded(packet))
            if item["bytes"] is None or item["bytes"] > remaining:
                continue
            item["clues"] = re.findall(r"(?m)^\s*(?:def|class|function|export|import|from)\s+[^\r\n]{1,120}", source["contents"].get(item["path"], ""))[:8]
            item["cluesStatus"] = "inline"
            if len(ts.encoded(packet)) > MAX_PACKET:
                item["clues"], item["cluesStatus"] = [], "source-reference"
        return packet

    def start(self, host, run_id, mode="build", restart=False, record_ids=None):
        ts.string(run_id)
        ts.require(mode in {"build", "maintenance", "refinement", "correction"}, "invalid", "Unknown NAV cycle mode.")
        source, bodies, records, _, old, observed = self.observe(bootstrap=True)
        selected = sorted(set(record_ids or []))
        ts.require((mode == "correction" and 0 < len(selected) <= 8 and set(selected) <= records.keys())
                   or (mode != "correction" and not selected), "invalid", "Correction requires 1..8 existing record IDs; other modes take none.")
        if old and old["runID"] == run_id:
            ts.require(old.get("correctionRecords", []) == selected, "conflict", "Run ID already selects different records.")
            self.current(old, source, bodies)
            return {"outcome": "already-started", "runID": run_id}
        ts.require(not old or restart or old["status"] == "queue-complete", "conflict", "Unfinished NAV cycle requires explicit restart and archival.")
        impact = self.maintenance() if old and mode == "maintenance" else None
        if impact and impact["outcome"] == "no-op":
            return impact
        ts.require(mode != "maintenance" or old, "missing", "Maintenance needs a prior source baseline.")
        state = {"profile": STATE, "runID": run_id, "mode": mode, "cycle": old["cycle"] + 1 if old else 1,
                 "sourceFingerprint": source["fingerprint"], "sourceCommit": source["commit"], "baseline": source["paths"],
                 "contractsFingerprint": digest(nav.SWEEP_BYTES), "configFingerprint": digest(self.config_bytes),
                 "maps": {k: digest(v) for k, v in bodies.items()}, "sweepIndex": 0, "closedSweeps": [],
                 "receipts": [], "activePacket": None, "status": "running", "debtPaths": [], "debtRecords": [],
                 "maintenance": impact, "correctionRecords": selected, "priorDebt": {"paths": old.get("debtPaths", []) if old else [],
                                                       "records": old.get("debtRecords", []) if old else []}}
        updates = {self.names[k]: v for k, v in bodies.items()}
        from . import nav_history
        history_updates, history_observed = nav_history.prepare(self.root, self.history_path, old, run_id)
        updates.update(history_updates)
        observed.update(history_observed)
        if old:
            if old["status"] != "queue-complete":
                # An abandoned queue has not accounted for its remaining source.
                # Preserve that scope as debt; the new cycle can explicitly resolve it.
                state["priorDebt"]["paths"] = sorted(set(state["priorDebt"]["paths"]) | set(old["baseline"]))
                state["priorDebt"]["records"] = sorted(set(state["priorDebt"]["records"]) | set(records))
        updates[self.state_path] = self.bound(state)
        effect = self.persist(host, source, observed, updates)
        result = {"outcome": "started", "runID": run_id, "effect": effect}
        if old and old["status"] != "queue-complete":
            result["restarted"] = {"archivedRunID": old["runID"], "acceptedReceipts": len(old.get("receipts", [])),
                                   "closedSweeps": list(old.get("closedSweeps", [])),
                                   "changedSinceOldBaseline": path_changes(old["baseline"], source["paths"]),
                                   "qualification": "No receipts carry forward; re-inspect before reusing earlier work."}
        return result

    def history_plan(self, run_id):
        from . import nav_history
        source, bodies, records, _, old, observed = self.observe(bootstrap=True)
        updates, _ = nav_history.prepare(self.root, self.history_path, old, run_id)
        return {"outcome": "planned", "paths": sorted(set(updates) | self.paths),
                "qualification": "Read-only plan. Reserve these exact paths through the selected host before start. No admission or grant renewal is needed."}

    def history(self, limit=10, cursor=None):
        from . import nav_history
        return nav_history.page(self.root, self.history_path, limit, cursor)

    def current(self, state, source, bodies):
        ts.require(state is not None, "missing", "No NAV run exists.")
        changed = []
        if state["sourceFingerprint"] != source["fingerprint"]:
            changed.append("source (" + describe_changes(path_changes(state["baseline"], source["paths"])) + ")")
        maps = {k: digest(v) for k, v in bodies.items()}
        if state["maps"] != maps:
            changed.append("maps (" + ", ".join(self.names.get(k, k) for k in sorted(maps.keys() | state["maps"].keys())
                                                if maps.get(k) != state["maps"].get(k)) + ")")
        if state["contractsFingerprint"] != digest(nav.SWEEP_BYTES):
            changed.append("contracts")
        if state["configFingerprint"] != digest(self.config_bytes):
            changed.append("config")
        ts.require(not changed, "stale", "Run source, maps or contracts changed; inspect and explicitly restart, preserving history. Changed: "
                   + "; ".join(changed) + ".")

    def sweeps(self, state):
        if state["mode"] == "correction":
            return [{"id": "C1-record-correction", "selector": "correction", "batchSize": 8,
                     "mapFiles": list(nav.MAPS), "candidateKinds": [k[0] for k in nav.KINDS.values()],
                     "objective": "Correct only the explicitly selected existing records; no discovery or acceptance claim.",
                     "instructions": ["Inspect source, preserve IDs and unrelated records; retain unresolved findings."]}]
        if state["mode"] != "maintenance":
            return nav.SWEEPS
        return [{"id": "M1-affected-review", "pass": 1, "selector": "maintenance", "batchSize": 5,
                 "mapFiles": list(nav.MAPS), "candidateKinds": [k[0] for k in nav.KINDS.values()],
                 "objective": "Review only changed evidence, known dependent records and unmapped changed paths.",
                 "instructions": ["Retain native IDs across moves; source-inspect impact; retain unresolved debt."]}]

    def next(self, host):
        source, bodies, records, _, state, observed = self.observe()
        self.current(state, source, bodies)
        if state["activePacket"]:
            return {"outcome": "packet", "packet": state["activePacket"], "replayed": True}
        if state["status"] == "queue-complete":
            return self.status()
        sweeps = self.sweeps(state)
        while state["sweepIndex"] < len(sweeps):
            sweep = sweeps[state["sweepIndex"]]
            if state["mode"] == "correction":
                pending = [lead(sweep["id"], rid, records[rid]["evidence"], [rid]) for rid in state["correctionRecords"]]
            elif state["mode"] == "maintenance":
                impact = state["maintenance"]
                pending = [lead(sweep["id"], rid, records[rid]["evidence"], [rid]) for rid in impact["affectedRecords"] if rid in records]
                pending += [lead(sweep["id"], p, [p]) for p in impact["unmappedPaths"]]
            else:
                pending = leads(sweep, records, source)
            done = {lid for receipt in state["receipts"] if receipt["sweepID"] == sweep["id"] for lid in receipt["leadIDs"]}
            pending = [l for l in pending if l["id"] not in done]
            if not pending:
                state["closedSweeps"].append(sweep["id"])
                state["sweepIndex"] += 1
                continue
            issued = pending[:min(sweep["batchSize"], 20)]
            packet = self.packet(state, sweep, issued, source, bodies, records)
            while len(ts.encoded(packet)) > MAX_PACKET and len(issued) > 1:
                issued = issued[:max(1, len(issued) // 2)]
                packet = self.packet(state, sweep, issued, source, bodies, records)
            ts.require(len(ts.encoded(packet)) <= MAX_PACKET, "overflow",
                       "One lead's reference metadata exceeds the packet budget; inspect its exact sources directly. No lead was consumed or truncated.")
            state["activePacket"] = packet
            self.persist(host, source, observed, {self.state_path: self.bound(state)})
            return {"outcome": "packet", "packet": packet, "replayed": False}
        state["status"] = "queue-complete"
        # An interrupted predecessor's unresolved debt cannot disappear on restart.
        seen_paths = {p for r in state["receipts"] for p in r["resolvedPaths"]}
        seen_records = {p for r in state["receipts"] for p in r["resolvedRecords"]}
        state["debtPaths"] = sorted(set(state["debtPaths"]) | (set(state["priorDebt"]["paths"]) - seen_paths))
        state["debtRecords"] = sorted(set(state["debtRecords"]) | (set(state["priorDebt"]["records"]) - seen_records))
        self.persist(host, source, observed, {self.state_path: self.bound(state)})
        return self.status()

    def report_template(self):
        """Read-only skeleton: no inspection or successful disposition is invented."""
        source, bodies, _, _, state, _ = self.observe()
        self.current(state, source, bodies)
        packet = state["activePacket"]
        ts.require(packet is not None, "missing", "Run nav next to issue a packet first.")
        return {"profile": REPORT, **{k: packet[k] for k in ("packetID", "sweepID", "sourceFingerprint")},
                "inspectedPaths": [], "inspectedRecordIDs": [], "leadDispositions": [
                    {"leadID": item["id"], "disposition": "unresolved", "reason": "Not yet reviewed: " + item["key"]}
                    for item in packet["leads"]], "proposedRecords": [],
                "omissionSearch": "Not yet performed", "summary": "Draft; fill findings after source inspection"}

    def status(self):
        source, bodies, records, _, state, _ = self.observe()
        if state is None:
            return self.no_baseline(source, bodies, records)
        return {"outcome": state["status"], "runID": state["runID"], "mode": state["mode"],
                "closedSweeps": state["closedSweeps"], "activePacket": state["activePacket"]["packetID"] if state["activePacket"] else None,
                "sourceCurrent": state["sourceFingerprint"] == source["fingerprint"],
                "mapsCurrent": state["maps"] == {k: digest(v) for k, v in bodies.items()},
                "records": len(records), "unresolvedPaths": state["debtPaths"], "unresolvedRecords": state["debtRecords"],
                "semanticAcceptance": "unproven", "providerInvocations": "host-owned; not collected"}

    def validate_report(self, report, packet, source, bodies, records, preambles, mode):
        ts.shape(report, {"profile", "packetID", "sweepID", "sourceFingerprint", "inspectedPaths", "leadDispositions",
                          "proposedRecords", "omissionSearch", "summary"}, {"inspectedRecordIDs"})
        ts.require(report["profile"] == REPORT, "unsupported", "Unsupported NAV report.")
        ts.string(report["summary"])
        ts.string(report["omissionSearch"])
        inspected = set(ts.sequence(report["inspectedPaths"]))
        ts.require(inspected <= source["paths"].keys(), "missing", "Reported inspection is outside obtained source.")
        referenced_context = {item["path"] for item in packet["requiredContext"]
                              if item.get("contentStatus") == "source-reference"}
        ts.require(referenced_context <= inspected, "invalid",
                   "Report inspection of required context supplied by exact source reference.")
        inspected_records = set(ts.sequence(report.get("inspectedRecordIDs", [])))
        ts.require(inspected_records <= records.keys(), "missing", "Reported record inspection is outside current NAV maps.")
        referenced_records = {item["id"] for item in packet["recordContext"]
                              if item.get("contentStatus") == "source-reference"}
        ts.require(referenced_records <= inspected_records, "invalid",
                   "Report inspectedRecordIDs for NAV records supplied by exact source reference.")
        dispositions = ts.sequence(report["leadDispositions"])
        by_id = {}
        for disposition in dispositions:
            ts.shape(disposition, {"leadID", "disposition", "reason"})
            ts.string(disposition["reason"])
            ts.require(disposition["leadID"] not in by_id and disposition["disposition"] in nav.CONTRACTS["reportSchema"]["leadDispositions"],
                       "invalid", "Duplicate or unsupported lead disposition.")
            by_id[disposition["leadID"]] = disposition["disposition"]
        expected = {l["id"] for l in packet["leads"]}
        ts.require(set(by_id) == expected, "invalid",
                   "Lead mismatch; missing=" + str(sorted(expected - set(by_id))) + "; unexpected=" + str(sorted(set(by_id) - expected)))
        for l in packet["leads"]:
            if by_id[l["id"]] in {"confirmed", "covered", "candidate"}:
                ts.require(set(l["paths"]) & inspected, "invalid", "Positive disposition needs inspected current source.")
        proposed, changed = dict(records), set()
        for proposal in ts.sequence(report["proposedRecords"]):
            ts.shape(proposal, {"recordID", "action", "mapFile", "evidencePaths", "text"})
            rid, action, owner = proposal["recordID"], proposal["action"], proposal["mapFile"]
            ts.string(rid)
            ts.require(rid not in changed and action in {"add", "revise", "remove"}, "invalid", "Duplicate or unsupported record action.")
            changed.add(rid)
            ts.require(owner in packet["sweep"]["mapFiles"], "denied", "Proposal exceeds this sweep's map scope.")
            ts.require((rid in records) == (action != "add"), "conflict", "Record action does not match existing identity.")
            if mode == "correction":
                ts.require(action == "revise" and rid in {r for l in packet["leads"] for r in l["recordIDs"]},
                           "denied", "Correction can only revise selected existing records.")
            if rid in records:
                ts.require(records[rid]["map"] == owner, "conflict", "Native identity cannot change map owner.")
            if action == "remove":
                ts.require(mode == "refinement" and packet["sweepID"] == "P6-semantic-reconciliation"
                           and any(rid in l["recordIDs"] for l in packet["leads"]) and proposal["text"] == ""
                           and set(records[rid]["evidence"]) & inspected, "denied", "Removal requires final refinement of an issued source-inspected record.")
                proposed.pop(rid)
                continue
            text = proposal["text"]
            ts.require(isinstance(text, str) and len(text.encode()) <= 32768, "invalid", "Record proposal exceeds 32 KiB.")
            # Parse in the complete map context so relations retain native semantics.
            match = list(nav.HEADING.finditer(text))
            ts.require(len(match) == 1 and match[0].start() == 0 and match[0].group(1) == rid,
                       "invalid", "One exact native record per proposal is required.")
            fields = dict(nav.FIELD.findall(text))
            paths = ts.sequence(proposal["evidencePaths"])
            ts.require(paths and set(paths) <= inspected and set(nav.values(fields.get("Evidence", ""))) == set(paths),
                       "invalid", "Proposal evidence must match inspected source paths.")
            if mode == "maintenance":
                issued_records = {r for l in packet["leads"] for r in l["recordIDs"]}
                issued_paths = {p for l in packet["leads"] for p in l["paths"]}
                ts.require((action == "revise" and rid in issued_records)
                           or (action == "add" and bool(set(paths) & issued_paths)),
                           "denied", "Maintenance proposals must address this packet's affected records or changed paths.")
            ts.require(fields.get("Kind") in packet["sweep"]["candidateKinds"] and fields.get("Review") in {"candidate", "gap"}
                       and fields.get("Revision") == source["fingerprint"], "invalid", "Proposals require current source fingerprint and provisional review state.")
            if rid in records:
                for key in records[rid]["fields"].keys() - nav.REQUIRED:
                    ts.require(key in fields, "invalid", "Revision must preserve native extra fields explicitly.")
            proposed[rid] = {"text": text.rstrip() + "\n\n", "map": owner}
        output = {name: (preambles[name] + "".join(r["text"] for r in proposed.values() if r["map"] == name)).rstrip("\r\n").encode() + b"\n"
                  for name in nav.MAPS}
        nav.parse_maps(output)  # Includes incoming relation checks for removals.
        return output, by_id

    def _admit_report(self, report):
        """Read-only checks shared by submit and check_report, in submit's order."""
        ts.require(isinstance(report, dict), "invalid", "Expected NAV report object.")
        source, bodies, records, preambles, state, observed = self.observe()
        self.current(state, source, bodies)
        report_hash = digest(ts.encoded(report))
        for receipt in state["receipts"]:
            if receipt["packetID"] == report.get("packetID"):
                ts.require(receipt["reportHash"] == report_hash, "conflict", "Closed packet is bound to another report.")
                return {"outcome": "already-submitted", "receipt": receipt}, None
        packet = state["activePacket"]
        ts.require(packet is not None and all(report.get(k) == packet[k] for k in ("packetID", "sweepID", "sourceFingerprint")),
                   "stale", "Report does not match the active source-bound packet.")
        return None, (source, bodies, records, preambles, state, observed, report_hash, packet)

    def check_report(self, report):
        """Read-only mechanical validation: writes nothing, consumes no retry, needs no host."""
        done, ctx = self._admit_report(report)
        if done:
            return {"outcome": "already-submitted", "valid": True, "receipt": done["receipt"], "mechanicalOnly": True}
        source, bodies, records, preambles, state, _, _, packet = ctx
        try:
            _, by_id = self.validate_report(report, packet, source, bodies, records, preambles, state["mode"])
        except (ts.ReadError, TypeError, KeyError) as exc:
            code = exc.code if isinstance(exc, ts.ReadError) else "invalid"
            message = exc.message if isinstance(exc, ts.ReadError) else "Malformed report value."
            return {"outcome": "invalid", "valid": False, "code": code, "message": message, "mechanicalOnly": True,
                    "qualification": QUALIFY_VALIDATE + " Submit would record this as an unresolved diagnostic and consume the packet retry."}
        unresolved = sorted(i for i, d in by_id.items() if d == "unresolved")
        return {"outcome": "valid", "valid": True, "packetID": packet["packetID"], "unresolvedLeadIDs": unresolved,
                "mechanicalOnly": True, "qualification": QUALIFY_VALIDATE
                + (" Unresolved leads would still trigger a retry or become review debt on submit." if unresolved else "")}

    def submit(self, host, report):
        done, ctx = self._admit_report(report)
        if done:
            return done
        source, bodies, records, preambles, state, observed, report_hash, packet = ctx
        failure = None
        try:
            output, by_id = self.validate_report(report, packet, source, bodies, records, preambles, state["mode"])
        except (ts.ReadError, TypeError, KeyError) as exc:
            failure = exc.message if isinstance(exc, ts.ReadError) else "Malformed report value."
            output, by_id = bodies, {l["id"]: "unresolved" for l in packet["leads"]}
        unresolved = [l for l in packet["leads"] if by_id[l["id"]] == "unresolved"]
        if unresolved and packet["retry"] < 1:
            packet["retry"] += 1
            packet["repair"] = {"leadIDs": [l["id"] for l in unresolved], "reason": failure or "Resolve remaining leads or explicitly retain the uncertainty.",
                                "resubmit": "Return the complete packet report; no map proposals have been applied."}
            self.persist(host, source, observed, {self.state_path: self.bound(state)})
            return {"outcome": "retry", "packet": packet}
        unresolved_paths = {p for l in unresolved for p in l["paths"]}
        unresolved_records = {r for l in unresolved for r in l["recordIDs"]}
        resolved_paths = {p for l in packet["leads"] if l not in unresolved for p in l["paths"]} - unresolved_paths
        resolved_records = {r for l in packet["leads"] if l not in unresolved for r in l["recordIDs"]} - unresolved_records
        # Later passes answer different questions: they cannot erase an earlier
        # unresolved disposition just because they inspected the same source.
        state["debtPaths"] = sorted(set(state["debtPaths"]) | unresolved_paths)
        state["debtRecords"] = sorted(set(state["debtRecords"]) | unresolved_records)
        receipt = {"packetID": packet["packetID"], "sweepID": packet["sweepID"], "reportHash": report_hash,
                   "leadIDs": [l["id"] for l in packet["leads"]], "dispositions": by_id,
                   "resolvedPaths": sorted(resolved_paths), "resolvedRecords": sorted(resolved_records),
                   "inspectedPaths": report.get("inspectedPaths", []) if not failure else [],
                   "inspectedRecordIDs": report.get("inspectedRecordIDs", []) if not failure else [],
                   "omissionSearch": report.get("omissionSearch", "") if not failure else "Invalid report; omission search unverified.",
                   "proposedRecordIDs": [p["recordID"] for p in report["proposedRecords"]] if not failure else [],
                   "diagnostic": failure, "summary": report.get("summary", "Invalid report closed unresolved.")[:1024] if isinstance(report.get("summary", ""), str) else "Invalid summary."}
        state["receipts"].append(receipt)
        state["activePacket"] = None
        state["maps"] = {k: digest(v) for k, v in output.items()}
        updates = {self.names[k]: v for k, v in output.items()}
        updates[self.state_path] = self.bound(state)
        self.persist(host, source, observed, updates)
        return {"outcome": "closed-unresolved" if unresolved else "submitted", "receipt": receipt}

    def run(self, host, provider, max_packets=32):
        """Explicit host-selected provider seam; failure leaves the issued packet intact."""
        ts.require(type(max_packets) is int and 1 <= max_packets <= 64, "invalid", "Runner packet budget is 1..64.")
        for _ in range(max_packets):
            issued = self.next(host)
            if issued["outcome"] != "packet":
                return issued
            try:
                report = provider(deepcopy(issued["packet"]))
            except Exception:
                return {"outcome": "provider-failed", "packetID": issued["packet"]["packetID"], "activePacketPreserved": True}
            if report is None:
                return {"outcome": "provider-cancelled", "packetID": issued["packet"]["packetID"], "activePacketPreserved": True}
            self.submit(host, report)
        return {"outcome": "limit-reached", "status": self.status()}
