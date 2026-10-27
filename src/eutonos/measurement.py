"""Optional bounded metadata observations. No transcripts, automatic export or billing inference."""
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import stat
import uuid
import time

from .private_scope import require_shared
from . import tokenslang as ts
from .communication import decode, slug, stamp
from .effects import digest, plan
from .source import safe_path

BATCH = "eutonos.tokenslang.measurement-batch.v1"
CONFIG = "eutonos.tokenslang.measurement-config.v1"
PLAN = "eutonos.tokenslang.measurement-plan.v1"
METHOD = "eutonos.metadata.v1"
OPERATIONS = ("hub.read", "docs.adopt", "nav.build", "nav.maintenance", "ram.capture", "communication.read", "effect.apply", "proof.pickup", "outcome.review")
OUTCOMES = ("ok", "partial", "error", "cancelled", "unknown", "conflict", "stale")
INCIDENTS = ("accepted-outcome", "rework", "re-explanation", "rediscovery", "handoff-recovery")
CAUSES = ("not-saved-in-selection", "not-retrieved", "offered-not-opened", "retrieved-not-reflected", "unknown")


def number(value, integer=False):
    ts.require(value is None or type(value) in ((int,) if integer else (int, float)) and math.isfinite(value) and value >= 0,
               "invalid", "Expected a nonnegative measured amount or unknown.")


def usage(value):
    if value is None:
        return
    ts.shape(value, {"provider", "model", "basis", "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"})
    ts.string(value["provider"]); ts.string(value["basis"])
    if value["model"] is not None: ts.string(value["model"])
    for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"):
        number(value[k], True)
    for subset, total in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        ts.require(value[subset] is None or value[total] is None or value[subset] <= value[total], "invalid", "Usage subset exceeds supplied total.")


def event(value):
    ts.shape(value, {"id", "logical_id", "parent_id", "operation", "level", "outcome", "elapsed_ns", "returned_bytes", "provider_usage", "incident"})
    for k in ("id", "logical_id"):
        slug(value[k])
    if value["parent_id"] is not None: slug(value["parent_id"])
    ts.require(value["operation"] in OPERATIONS and value["level"] in ("entry", "module", "attempt")
               and value["outcome"] in OUTCOMES, "unsupported", "Unsupported metadata category.")
    number(value["elapsed_ns"], True); number(value["returned_bytes"], True); usage(value["provider_usage"])
    incident = value["incident"]
    if incident is not None:
        ts.shape(incident, {"kind", "cause", "basis", "evidence"})
        ts.require(incident["kind"] in INCIDENTS and incident["cause"] in CAUSES, "unsupported", "Unknown outcome classification.")
        ts.string(incident["basis"])
        for ref in ts.sequence(incident["evidence"]):
            ts.shape(ref, {"path", "sha256"}); safe_path(ref["path"]); ts.string(ref["sha256"])


def validate(batch):
    ts.shape(batch, {"profile", "identity", "revision", "method", "selection", "window", "recorded_at", "coverage", "events", "visibility", "state", "instrumentation_ns"})
    number(batch["instrumentation_ns"], True)
    ts.require(batch["profile"] == BATCH and batch["method"] == METHOD, "unsupported", "Unknown measurement schema/method.")
    ts.shape(batch["identity"], {"issuer_id", "batch_id"})
    for part in batch["identity"].values(): slug(part)
    ts.require(type(batch["revision"]) is int and batch["revision"] > 0, "invalid", "Expected a positive batch revision.")
    ts.shape(batch["selection"], {"scope_id", "work_id", "session_id", "attempt_id", "source_commit"})
    for value in batch["selection"].values(): ts.string(value)
    ts.shape(batch["window"], {"start", "end"}); stamp(batch["window"]["start"]); stamp(batch["window"]["end"]); stamp(batch["recorded_at"])
    ts.require(datetime.fromisoformat(batch["window"]["start"]) <= datetime.fromisoformat(batch["window"]["end"]), "invalid", "Reversed observation window.")
    ts.shape(batch["coverage"], {"mode", "extent", "dropped", "limitation"})
    ts.require(batch["coverage"]["mode"] in ("metadata", "off") and batch["coverage"]["extent"] in ("complete-selection", "partial", "not-collected"), "invalid", "Invalid coverage.")
    number(batch["coverage"]["dropped"], True); ts.string(batch["coverage"]["limitation"])
    values = ts.sequence(batch["events"])
    for value in values: event(value)
    ts.require(len({e["id"] for e in values}) == len(values), "conflict", "Duplicate event identity.")
    ts.require(batch["coverage"]["mode"] != "off" or not values and batch["coverage"]["extent"] == "not-collected", "invalid", "Off mode cannot claim observed events.")
    ts.require(batch["coverage"]["extent"] != "complete-selection" or batch["coverage"]["dropped"] == 0, "invalid", "Dropped observations cannot be complete.")
    ts.require(batch["visibility"] in ("public", "private") and batch["state"] in ("active", "suppressed", "archived"), "invalid", "Unknown batch controls.")
    ts.require(len(ts.encoded(batch)) <= ts.MAX_SOURCE_BYTES, "overflow", "Measurement batch exceeds source bound.")


class Meter:
    def __init__(self, enabled=True, max_events=64):
        ts.require(type(enabled) is bool and type(max_events) is int and 1 <= max_events <= 64, "invalid", "Invalid collection bounds.")
        self.enabled, self.max_events = enabled, max_events
        self.events, self.dropped, self.overhead_ns = [], 0, 0
        # Generated logical IDs belong to one Meter; caller IDs retain explicit correlation.
        self.logical_namespace = uuid.uuid4().hex
        self.started = datetime.now(timezone.utc).isoformat()

    def record(self, value):
        if not self.enabled: return
        start = time.perf_counter_ns()
        try:
            event(value)
            ts.require(len(self.events) < self.max_events, "overflow", "Metadata selection is full.")
            ts.require(all(e["id"] != value["id"] for e in self.events), "conflict", "Event identity already observed.")
            self.events.append(ts.loads(ts.encoded(value)))
        except Exception:
            self.dropped += 1
        finally:
            self.overhead_ns += time.perf_counter_ns() - start

    def snapshot(self, identity, selection):
        now = datetime.now(timezone.utc).isoformat()
        batch = {"profile": BATCH, "identity": identity, "revision": 1, "method": METHOD, "selection": selection,
                 "window": {"start": self.started, "end": now}, "recorded_at": now,
                 "coverage": {"mode": "metadata" if self.enabled else "off", "extent": "partial" if self.enabled else "not-collected",
                              "dropped": self.dropped, "limitation": "Only explicitly instrumented calls; no session-wide or provider completeness."},
                 "events": self.events.copy(), "visibility": "public", "state": "active", "instrumentation_ns": self.overhead_ns if self.enabled else None}
        validate(batch)
        return batch


def observe(meter, call, operation="hub.read", level="entry", logical_id=None, parent_id=None):
    """A broken/disabled optional observer must preserve the call's result or exception."""
    # Only the exact native Meter contract is safe to bypass. Duck-typed or
    # subclass observers still receive the fail-open observation attempt.
    if type(meter) is Meter and not meter.enabled:
        return call()
    start, result, outcome = time.perf_counter_ns(), None, "error"
    try:
        result = call()
        outcome = result.get("outcome", "ok") if isinstance(result, dict) else "ok"
        return result
    finally:
        elapsed = time.perf_counter_ns() - start
        if meter is not None:
            try:
                identity = "call-" + str(len(meter.events) + meter.dropped + 1)
                if logical_id is None and type(meter) is Meter:
                    logical = "meter-" + meter.logical_namespace + "-" + identity
                else:
                    logical = logical_id or identity
                meter.record({"id": identity, "logical_id": logical, "parent_id": parent_id,
                              "operation": operation, "level": level, "outcome": outcome if outcome in OUTCOMES else "unknown",
                              "elapsed_ns": elapsed, "returned_bytes": len(ts.encoded(result)) if result is not None else None,
                              "provider_usage": None, "incident": None})
            except Exception:
                # Required audit/effect records are owned elsewhere and are never dropped here.
                try: meter.dropped += 1
                except Exception: pass


def summary(batches):
    """Reconcile an explicitly selected snapshot. Current visibility requires reading current owners first."""
    ts.sequence(batches)
    latest = {}
    for batch in batches:
        validate(batch)
        key = (batch["identity"]["issuer_id"], batch["identity"]["batch_id"])
        previous = latest.get(key)
        if previous and previous["revision"] == batch["revision"]:
            ts.require(ts.encoded(previous) == ts.encoded(batch), "conflict", "Same batch revision has competing content.")
        if previous is None or batch["revision"] > previous["revision"]: latest[key] = batch
    selected = [b for b in latest.values() if b["visibility"] == "public" and b["state"] == "active"]
    levels = {level: {"count": 0, "elapsed_ns": 0, "returned_bytes": 0, "unknown_durations": 0, "unknown_sizes": 0} for level in ("entry", "module", "attempt")}
    selected_events = []
    for batch in selected:
        for item in batch["events"]:
            selected_events.append(item)
            dest = levels[item["level"]]; dest["count"] += 1
            for key, unknown in (("elapsed_ns", "unknown_durations"), ("returned_bytes", "unknown_sizes")):
                if item[key] is None: dest[unknown] += 1
                else: dest[key] += item[key]
    logical_counts, failed_logicals = {}, set()
    for item in selected_events:
        logical = item["logical_id"]
        logical_counts[logical] = logical_counts.get(logical, 0) + 1
        if item["outcome"] in {"error", "conflict", "stale"}:
            failed_logicals.add(logical)
    repeated_failures = sum(max(0, logical_counts[logical] - 1) for logical in failed_logicals)
    workflow_efficiency = {
        "outer_invocations": sum(item["level"] == "entry" for item in selected_events),
        "logical_operations": len(logical_counts),
        "repeated_logical_events": sum(max(0, count - 1) for count in logical_counts.values()),
        "repeated_failure_events": repeated_failures,
        "repeated_failure_meaning": "Surplus retained events in a logical group containing error/conflict/stale; not a count of failed retry attempts.",
        "logical_identity_qualification": "New native Meter-generated logical IDs are Meter-scoped; explicit caller logical IDs retain deliberate correlation. Legacy v1 records do not encode generated-versus-explicit provenance, so historical cross-batch logical grouping can remain ambiguous.",
        "returned_bytes": sum(item["returned_bytes"] or 0 for item in selected_events if item["level"] == "entry"),
        "unknown_entry_sizes": sum(item["level"] == "entry" and item["returned_bytes"] is None for item in selected_events),
        "instrumentation_ns": sum(batch["instrumentation_ns"] or 0 for batch in selected),
        "dropped_observations": sum(batch["coverage"]["dropped"] for batch in selected),
        "coverage": "selected metadata only; outer process, transport, agent intervals and uninstrumented reads remain unknown",
    }
    return {"profile": "eutonos.tokenslang.measurement-summary.v1", "evidence": "observed-selected-metadata",
            "inputs": [{"identity": b["identity"], "revision": b["revision"], "coverage": b["coverage"]} for b in selected],
            "levels": levels, "workflow_efficiency": workflow_efficiency,
            "elapsed_meaning": "Sum of observed durations per level; not wall-clock task time. Do not sum levels.",
            "provider_tokens": None, "task_savings": None, "net_benefit": None,
            "exposed_usage": [{"batch": b["identity"], "event_id": e["id"], "level": e["level"], "usage": e["provider_usage"]}
                              for b in selected for e in b["events"] if e["provider_usage"] is not None],
            "limitation": "Selection snapshot only; unprovided later corrections/controls and uninstrumented activity unknown."}


def compare(baseline, actual):
    for value in (baseline, actual):
        ts.shape(value, {"amount", "unit", "boundary", "view", "method"})
        number(value["amount"])
        for key in ("unit", "boundary", "view", "method"): ts.string(value[key])
    ts.require(all(baseline[k] == actual[k] for k in ("unit", "boundary", "view", "method")), "conflict", "Comparison accounting boundaries differ.")
    difference = None if baseline["amount"] is None or actual["amount"] is None else baseline["amount"] - actual["amount"]
    return {"evidence": "compared", "baseline": baseline, "actual": actual, "difference": difference,
            "percentage": None if difference is None or baseline["amount"] == 0 else 100 * difference / baseline["amount"],
            "task_savings": None, "net_benefit": None}


class Store:
    def __init__(self, root, config_path="eutonos.measurement.json"):
        require_shared(root)
        self.root, self.config_path = root, safe_path(config_path)
        self.config_bytes = root.read(config_path); self.cfg = decode(self.config_bytes)
        ts.shape(self.cfg, {"profile", "scope_id", "store_root", "mode", "retention", "export"})
        ts.require(self.cfg["profile"] == CONFIG and self.cfg["mode"] in ("metadata", "off")
                   and self.cfg["retention"] == "explicit-owner-controls" and self.cfg["export"] == "explicit-guarded-publication",
                   "unsupported", "Unsupported measurement policy.")
        ts.string(self.cfg["scope_id"]); safe_path(self.cfg["store_root"])
        ts.require(not config_path.startswith(self.cfg["store_root"] + "/") and config_path != self.cfg["store_root"], "invalid", "Policy must remain outside observations.")

    def _current(self):
        ts.require(self.root.read(self.config_path) == self.config_bytes, "stale", "Measurement policy changed.")

    def path(self, identity):
        ts.shape(identity, {"issuer_id", "batch_id"})
        return self.cfg["store_root"] + "/" + slug(identity["issuer_id"]) + "/" + slug(identity["batch_id"]) + ".json"

    def _checked_batch(self, path, raw):
        ts.require(raw is not None, "missing", "Measurement record is absent.")
        ts.require(len(raw) <= ts.MAX_SOURCE_BYTES, "overflow", "Measurement source exceeds batch bound.")
        batch = decode(raw)
        validate(batch)
        ts.require(self.path(batch["identity"]) == path and batch["selection"]["scope_id"] == self.cfg["scope_id"],
                   "wrong-scope", "Batch identity/scope mismatch.")
        return batch

    def plan(self, batch, expected_revision, actor):
        validate(batch); ts.string(actor)
        ts.require(expected_revision is None or type(expected_revision) is int and expected_revision > 0, "invalid", "Expected revision must be positive or absent.")
        ts.require(batch["selection"]["scope_id"] == self.cfg["scope_id"] and batch["coverage"]["mode"] == self.cfg["mode"], "wrong-scope", "Batch differs from selected scope/policy.")
        self._current()
        path = self.path(batch["identity"]); previous = self.root.read(path)
        old = decode(previous) if previous else None
        if old:
            validate(old)
            ts.require(old["identity"] == batch["identity"] and old["selection"]["scope_id"] == self.cfg["scope_id"],
                       "wrong-scope", "Existing batch owner differs from the selected path/scope.")
        body = ts.encoded(batch) + b"\n"
        same = old is not None and ts.encoded(old) == ts.encoded(batch)
        ts.require(same or expected_revision == (old["revision"] if old else None)
                   and batch["revision"] == (old["revision"] + 1 if old else 1), "conflict", "Batch correction needs current revision.")
        value = plan(self.root, {} if same else {path: body}, inputs=[self.config_path, path])
        ts.require(value["observed"][path] == digest(previous) and value["observed"][self.config_path] == digest(self.config_bytes), "stale", "Measurement source changed during planning.")
        return {"profile": PLAN, "actor": actor, "effect": value, "fingerprint": digest(ts.encoded(value))}

    def read(self, identities):
        paths = [self.path(i) for i in ts.sequence(identities)]
        observed = {p: self.root.read(p) for p in paths}
        self._current()
        batches = [self._checked_batch(path, raw) for path, raw in observed.items()]
        result = summary(batches)
        ts.require(all(self.root.read(p) == raw for p, raw in observed.items()), "stale", "Measurement controls changed during retrieval.")
        self._current()
        return result

    def _store_directory(self):
        selected_root = getattr(self.root, "root", None)
        ts.require(isinstance(selected_root, Path), "unsupported", "Checked Measurement discovery requires a local selected root.")
        current = selected_root
        for part in self.cfg["store_root"].split("/"):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return None
            ts.require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)
                       and stat.S_ISDIR(info.st_mode), "unsafe-path", "Measurement store root must be an ordinary directory.")
        return current

    def _discovery_paths(self, max_candidates):
        base = self._store_directory()
        if base is None:
            return [], 0, 0, False
        # Reuse the selected reader's exclusions, root identity and ordinary-parent checks.
        self.root.path(self.cfg["store_root"] + "/.measurement-discovery")
        paths, unsafe, unavailable, scanned = [], 0, 0, 0
        pending = [base]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if scanned >= max_candidates:
                            return sorted(paths), unsafe, unavailable, True
                        scanned += 1
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            unavailable += 1
                            continue
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            unsafe += 1
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                        elif entry.name.endswith(".json"):
                            rel = Path(entry.path).relative_to(self.root.root).as_posix()
                            try:
                                paths.append(safe_path(rel))
                            except ts.ReadError as exc:
                                # Discovered names are strings, but overlength locators
                                # fail TokenSlang admissibility before path-shape checks.
                                if exc.code not in {"unsafe-path", "invalid"}:
                                    raise
                                unsafe += 1
            except OSError:
                unavailable += 1
        return sorted(paths), unsafe, unavailable, False

    def project(self, max_records=512, max_diagnostics=32, max_candidates=4096):
        """Checked, bounded retained-history projection for read-only consumers."""
        ts.require(type(max_records) is int and 0 < max_records <= 4096
                   and type(max_diagnostics) is int and 0 < max_diagnostics <= 256
                   and type(max_candidates) is int and 0 < max_candidates <= 16384,
                   "invalid", "Invalid Measurement projection bound.")
        self._current()
        paths, unsafe_paths, unavailable_paths, discovery_truncated = self._discovery_paths(max_candidates)
        batches, observed, diagnostics = [], {}, []
        rejected, filtered, checked = unsafe_paths + unavailable_paths, 0, 0
        diagnostic_truncated = False

        def note(code):
            nonlocal diagnostic_truncated
            message = "Skipped a malformed or inadmissible Measurement record during checked projection (" + str(code) + ")."
            if len(diagnostics) < max_diagnostics:
                diagnostics.append(message)
            else:
                diagnostic_truncated = True

        if unsafe_paths:
            note("unsafe-path")
        if unavailable_paths:
            note("unavailable")

        truncated = False
        for path in paths:
            checked += 1
            try:
                raw = self.root.read(path)
                observed[path] = raw
                batch = self._checked_batch(path, raw)
            except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
                rejected += 1
                note(getattr(exc, "code", "invalid"))
                continue
            if batch["visibility"] != "public" or batch["state"] != "active":
                filtered += 1
                continue
            if len(batches) >= max_records:
                truncated = True
                break
            batches.append(batch)

        self._current()
        result = summary(batches)
        ts.require(all(self.root.read(path) == raw for path, raw in observed.items()),
                   "stale", "Measurement records or controls changed during checked projection.")
        self._current()

        partial = truncated or discovery_truncated or rejected > 0 or diagnostic_truncated
        if self.cfg["mode"] == "off":
            qualification = ("Collection is off; checked retained active history is projected read-only. "
                             if batches else "Collection is off; no checked retained active history was selected. ")
            qualification += "This read does not create new observations."
        else:
            qualification = "Checked active retained Measurement history from the selected store; uninstrumented activity remains unknown."
        coverage = {
            "state": "partial" if partial else "complete",
            "selected_records": len(batches),
            "checked_candidates": checked,
            "filtered_records": filtered,
            "rejected_records": rejected,
            "selection_limit": max_records,
            "truncated": truncated,
            "discovery_truncated": discovery_truncated,
            "candidate_limit": max_candidates,
            "diagnostics_truncated": diagnostic_truncated,
            "collection_mode": self.cfg["mode"],
            "retained_history": bool(batches),
            "qualification": qualification,
        }
        return {"summary": result, "coverage": coverage, "diagnostics": diagnostics}


def apply(coordinator, authority, attempt, generation, value, expected_fingerprint):
    ts.shape(value, {"profile", "actor", "effect", "fingerprint"})
    ts.require(value["profile"] == PLAN and value["actor"] == authority.actor
               and value["fingerprint"] == expected_fingerprint == digest(ts.encoded(value["effect"])), "conflict", "Measurement host/plan mismatch.")
    return coordinator.apply(authority, attempt, generation, "measurement-" + value["fingerprint"], value["effect"])
