"""Deterministic request-to-companion attribution from explicit identities.

The projection joins local assignment records, native turn measurements and
host-supplied relationship receipts.  Paths, working directories and temporal
proximity are deliberately ignored as attribution evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from .companion_results import FIELDS, _counts
from .assignment_metrics import model_selection, resolved_worker
from . import settings, model_catalog, tokenslang as ts
from .local_lock import local_lock


PROFILE = "eutonos.companion-attribution.v1"
RECEIPT = "eutonos.companion-attribution-receipt.v1"
RESULT_STATES = {"prepared", "dispatched", "completed", "needs-attention"}
REVIEW_STATES = {"pending", "not-reviewed", "accepted", "rejected", "changes-requested"}
PHASES = ("request_started_at", "dispatched_at", "worker_started_at", "worker_completed_at",
          "review_started_at", "review_completed_at")


def _directory(root):
    return Path(root).resolve() / ".eutonos-local" / "companion-attribution"


def _receipt_path(root, receipt_id):
    ts.string(receipt_id)
    ts.require(all(char.isalnum() or char in "-_" for char in receipt_id),
               "invalid", "Receipt ID may contain only letters, numbers, hyphens and underscores.")
    return _directory(root) / (receipt_id + ".json")


def receipts(root):
    """Read validated local relationship receipts without creating state."""
    directory = _directory(root)
    if not directory.is_dir():
        return []
    with local_lock(root, create=False):
        return [normalize_receipt(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(directory.glob("*.json"))]


def record(root, value):
    """Persist one explicit native relationship receipt idempotently."""
    receipt = normalize_receipt(value)
    path = _receipt_path(root, receipt["receipt_id"])
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with local_lock(root):
        if path.exists():
            current = normalize_receipt(json.loads(path.read_text(encoding="utf-8")))
            if current != receipt:
                raise ts.ReadError("conflict", "Receipt ID already binds a different payload.")
            return {"profile": RECEIPT, "outcome": "idempotent", "receipt": deepcopy(current)}
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
    return {"profile": RECEIPT, "outcome": "recorded", "receipt": deepcopy(receipt)}


def _identity(value, label):
    ts.shape(value, {"thread_id", "turn_id"}, {"basis"})
    if "basis" in value:
        ts.require(value["basis"] == label, "invalid", "Identity basis is not canonical.")
    return {"thread_id": ts.string(value["thread_id"]), "turn_id": ts.string(value["turn_id"]),
            "basis": label}


def _key(value):
    if not isinstance(value, dict):
        return None
    thread, turn = value.get("thread_id"), value.get("turn_id")
    return (thread, turn) if isinstance(thread, str) and thread and isinstance(turn, str) and turn else None


def _time(value):
    if value is None:
        return None
    ts.string(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ts.ReadError("invalid", "Receipt phase timestamps must be ISO 8601.") from exc
    ts.require(parsed.tzinfo is not None, "invalid", "Receipt phase timestamps must include a timezone.")
    return parsed


def normalize_receipt(value):
    """Validate one versioned native relationship receipt without retaining text."""
    ts.shape(value, {"profile", "receipt_id", "request_id", "assignment_id", "primary", "worker",
                     "observed_worker", "result", "review", "phases"})
    ts.require(value["profile"] == RECEIPT, "unsupported", "Unknown companion attribution receipt profile.")
    receipt_id = ts.string(value["receipt_id"])
    request_id = ts.string(value["request_id"])
    assignment_id = ts.string(value["assignment_id"])
    primary = _identity(value["primary"], "host receipt primary identity")
    worker = _identity(value["worker"], "host receipt worker identity")
    ts.require(_key(primary) != _key(worker), "conflict", "Primary and worker identities must differ.")

    ts.shape(value["observed_worker"], {"model", "effort", "basis"})
    observed_worker = {key: ts.string(value["observed_worker"][key])
                       for key in ("model", "effort", "basis")}
    # An observed identity is evidence, not a launch permission or capability claim.
    # Preserve new exact IDs and historical versions after catalog availability moves.
    ts.require(model_catalog.is_exact_model_id(observed_worker["model"])
               and observed_worker["effort"] in settings.EFFORTS,
               "invalid", "Observed worker model or effort is malformed.")
    ts.shape(value["result"], {"status", "basis"})
    ts.require(value["result"]["status"] in RESULT_STATES, "invalid", "Unsupported result status.")
    result = {"status": value["result"]["status"], "basis": ts.string(value["result"]["basis"])}

    ts.shape(value["review"], {"status", "turns", "basis"})
    ts.require(value["review"]["status"] in REVIEW_STATES, "invalid", "Unsupported review status.")
    review_turns = [_identity(item, "host receipt review identity") for item in ts.sequence(value["review"]["turns"])]
    ts.require(len({_key(item) for item in review_turns}) == len(review_turns),
               "ambiguous", "A review turn identity may appear only once per receipt.")
    review = {"status": value["review"]["status"], "turns": review_turns,
              "basis": ts.string(value["review"]["basis"])}

    ts.shape(value["phases"], set(PHASES))
    parsed = [_time(value["phases"][name]) for name in PHASES]
    known = [item for item in parsed if item is not None]
    ts.require(all(left <= right for left, right in zip(known, known[1:])),
               "conflict", "Known receipt phases must be chronological.")
    phases = {name: value["phases"][name] for name in PHASES}
    return {"profile": RECEIPT, "receipt_id": receipt_id, "request_id": request_id,
            "assignment_id": assignment_id, "primary": primary, "worker": worker,
            "observed_worker": observed_worker, "result": result, "review": review, "phases": phases}


def _receipt_facts(receipt):
    value = deepcopy(receipt)
    value.pop("receipt_id", None)
    return value


def _receipt_index(receipts):
    """Collapse exact retries while making reused receipt IDs fail closed."""
    by_id = defaultdict(list)
    for item in receipts:
        receipt = normalize_receipt(item)
        by_id[receipt["receipt_id"]].append(receipt)
    accepted, conflicting_ids, conflicting_pairs, replay_count = [], [], set(), 0
    for receipt_id, members in sorted(by_id.items()):
        fingerprints = {ts.encoded(member) for member in members}
        if len(fingerprints) != 1:
            conflicting_ids.append(receipt_id)
            conflicting_pairs.update((member["request_id"], member["assignment_id"]) for member in members)
            continue
        accepted.append(members[0])
        replay_count += len(members) - 1

    grouped = defaultdict(list)
    for receipt in accepted:
        grouped[(receipt["request_id"], receipt["assignment_id"])].append(receipt)
    resolved, conflicts = {}, []
    for key, members in sorted(grouped.items()):
        facts = defaultdict(list)
        for member in members:
            facts[ts.encoded(_receipt_facts(member))].append(member)
        if len(facts) != 1:
            conflicts.append(key)
            continue
        same = next(iter(facts.values()))
        resolved[key] = same[0]
        replay_count += len(same) - 1
    return resolved, set(conflicts) | conflicting_pairs, conflicting_ids, replay_count


def _exact_row(rows, identity):
    key = _key(identity)
    candidates = rows.get(key, []) if key else []
    return candidates[0] if len(candidates) == 1 else None


def _observed_model(row):
    models = row.get("models") if isinstance(row, dict) else None
    return models[0] if isinstance(models, list) and len(models) == 1 and isinstance(models[0], str) else None


def _seconds(start, end):
    left, right = _time(start), _time(end)
    return (right - left).total_seconds() if left is not None and right is not None else None


def _run_seconds(row):
    if not isinstance(row, dict):
        return None
    try:
        value = _seconds(row.get("start"), row.get("end"))
        return value if value is None or value >= 0 else None
    except ts.ReadError:
        return None


def _sum_counts(values):
    return {name: sum(value[name] for value in values) for name in FIELDS}


def _review_projection(receipt, rows, primary_thread):
    unknown = {"status": None, "basis": None, "overhead": {"turn_count": None,
               "observed_turn_count": 0, "latency_seconds": None, "tokens": None}}
    if receipt is None:
        return unknown
    review = receipt["review"]
    identities = review["turns"]
    matched = []
    for identity in identities:
        row = _exact_row(rows, identity)
        if row is not None and identity["thread_id"] == primary_thread and row.get("role") == "primary":
            matched.append(row)
    durations = [_run_seconds(row) for row in matched]
    counts = [_counts(row.get("usage")) for row in matched]
    complete = len(matched) == len(identities)
    if not identities and review["status"] == "not-reviewed":
        latency, usage = 0.0, dict.fromkeys(FIELDS, 0)
    else:
        latency = sum(durations) if complete and all(item is not None for item in durations) else None
        usage = _sum_counts(counts) if complete and all(item is not None for item in counts) else None
    return {"status": review["status"], "basis": review["basis"],
            "overhead": {"turn_count": len(identities), "observed_turn_count": len(matched),
                         "latency_seconds": latency, "tokens": usage}}


def _latency(receipt, worker_row, review):
    phases = receipt["phases"] if receipt is not None else dict.fromkeys(PHASES)
    endpoint = phases["review_completed_at"] or phases["worker_completed_at"]
    return {"queue_seconds": _seconds(phases["dispatched_at"], phases["worker_started_at"]),
            "worker_phase_seconds": _seconds(phases["worker_started_at"], phases["worker_completed_at"]),
            "observed_worker_turn_seconds": _run_seconds(worker_row),
            "review_phase_seconds": _seconds(phases["review_started_at"], phases["review_completed_at"]),
            "observed_review_turn_seconds": review["overhead"]["latency_seconds"],
            "end_to_end_seconds": _seconds(phases["request_started_at"], endpoint)}


def _savings(worker_row, comparisons):
    experiment = worker_row.get("experiment_id") if isinstance(worker_row, dict) else None
    matches = [item for item in comparisons if item.get("experiment_id") == experiment] if experiment else []
    if not matches:
        return {"status": "unknown", "usd": None, "basis": None, "limits": [
            "No explicit equal-quality paired comparison is linked to this observed worker turn."]}
    if len(matches) != 1:
        return {"status": "withheld", "usd": None, "basis": None,
                "limits": ["Several comparison records claim the same experiment."]}
    comparison = matches[0]
    limits = list(comparison.get("limits") or [])
    value = comparison.get("savings_usd")
    qualified = not limits and isinstance(value, (int, float)) and not isinstance(value, bool)
    return {"status": "qualified" if qualified else "withheld", "usd": value if qualified else None,
            "basis": "Explicit companion-on versus companion-off comparison with accepted quality." if qualified else None,
            "limits": limits or ([] if qualified else ["Comparison does not expose a qualified savings value."])}


def project(assignment_records, native_runs, receipts=(), comparisons=()):
    """Project attribution without writing state or inferring missing relationships."""
    records = list(assignment_records or [])
    runs = list(native_runs or [])
    comparisons = list(comparisons or [])
    rows = defaultdict(list)
    for row in runs:
        key = _key(row)
        if key:
            rows[key].append(row)
    claims = Counter(_key(record.get("attachment")) for record in records if _key(record.get("attachment")))
    receipt_rows, receipt_conflicts, conflicting_ids, replay_count = _receipt_index(list(receipts or []))

    output, request_groups = [], defaultdict(list)
    consumed_receipts = set()
    for record in sorted(records, key=lambda item: (str(item.get("request_id")), str(item.get("assignment_id")))):
        request_id, assignment_id = record.get("request_id"), record.get("assignment_id")
        pair = (request_id, assignment_id)
        receipt = receipt_rows.get(pair)
        receipt_conflict = pair in receipt_conflicts
        primary, attachment = record.get("primary") or {}, record.get("attachment") or {}
        receipt_aligned = bool(receipt and _key(receipt["primary"]) == _key(primary)
                               and _key(receipt["worker"]) == _key(attachment))
        if receipt_aligned:
            consumed_receipts.add(pair)
        elif receipt:
            receipt = None
            receipt_conflict = True

        primary_row = _exact_row(rows, primary)
        worker_row = _exact_row(rows, attachment)
        if claims[_key(attachment)] > 1:
            worker_row = None
        primary_model = _observed_model(primary_row)
        worker_model = _observed_model(worker_row)
        requested = record.get("requested_worker") or {}
        selection = model_selection(record) or {}
        resolved_model = selection.get("resolved")
        # Use this job's saved resolution, never today's moving family target.
        expected_model = resolved_worker(record)
        observed_effort = None
        effort_basis = None
        if receipt and receipt["observed_worker"]["model"] == worker_model:
            observed_effort = receipt["observed_worker"]["effort"]
            effort_basis = receipt["observed_worker"]["basis"]

        worker_usage = _counts(worker_row.get("usage")) if worker_row is not None else None
        review = _review_projection(receipt, rows, primary.get("thread_id"))
        review_usage = review["overhead"]["tokens"]
        combined = _sum_counts([worker_usage, review_usage]) if worker_usage is not None and review_usage is not None else None
        available = []
        if worker_usage is not None:
            available.append("companion")
        if review_usage is not None:
            available.append("primary_review")
        result = {"assignment_status": record.get("state"),
                  "receipt_status": receipt["result"]["status"] if receipt else None,
                  "agreement": record.get("state") == receipt["result"]["status"] if receipt else None,
                  "basis": receipt["result"]["basis"] if receipt else "local assignment lifecycle only"}
        item = {"profile": PROFILE, "key": {"request_id": request_id, "assignment_id": assignment_id},
                "request_id": request_id, "assignment_id": assignment_id,
                "primary": {"thread_id": primary.get("thread_id"), "turn_id": primary.get("turn_id"),
                            "requested_model": primary.get("model"), "observed_model": primary_model,
                            "observation_basis": "exact native thread and turn" if primary_model else None},
                "companion": {"thread_id": attachment.get("thread_id"), "turn_id": attachment.get("turn_id"),
                              "requested_model": requested.get("model"), "requested_effort": requested.get("effort"),
                              "resolved_model": resolved_model,
                              "observed_model": worker_model, "observed_effort": observed_effort,
                              "model_matches_request": worker_model == expected_model if worker_model and expected_model else None,
                              "effort_matches_request": observed_effort == requested.get("effort") if observed_effort else None,
                              "model_basis": "exact native thread and turn" if worker_model else None,
                              "effort_basis": effort_basis},
                "result": result, "review": review,
                "latency_seconds": _latency(receipt, worker_row, review),
                "tokens": {"categories": list(FIELDS), "available_for": available,
                           "companion": worker_usage, "primary_review": review_usage,
                           "combined_known": combined,
                           "basis": "Exact native turn counters; cached input and reasoning output are subsets."},
                "qualified_savings": _savings(worker_row, comparisons),
                "receipt": {"status": "conflict" if receipt_conflict else "observed" if receipt else "unknown",
                            "receipt_id": receipt["receipt_id"] if receipt else None},
                "limits": [message for condition, message in (
                    (primary_row is None, "Primary native turn is unavailable or ambiguous."),
                    (worker_row is None, "Companion native turn is unavailable, ambiguous, or claimed by several assignments."),
                    (receipt is None, "A matching native relationship receipt is unavailable or conflicting."),
                    (observed_effort is None, "Observed companion effort is unavailable."),
                    (worker_usage is None, "Companion token categories are unavailable."),
                    (review["overhead"]["tokens"] is None, "Primary review token overhead is unavailable.")) if condition]}
        output.append(item)
        request_groups[request_id].append(item)

    requests, pair_counts = [], Counter()
    for request_id, items in sorted(request_groups.items(), key=lambda pair: str(pair[0])):
        pairs = sorted({(item["primary"]["observed_model"], item["companion"]["observed_model"])
                        for item in items if item["primary"]["observed_model"] and item["companion"]["observed_model"]})
        for pair in pairs:
            pair_counts[pair] += 1
        observed_helpers = [item["assignment_id"] for item in items if item["companion"]["observed_model"]]
        requests.append({"request_id": request_id, "assignment_count": len(items),
                         "observed_helper_count": len(observed_helpers),
                         "any_helper_observed": bool(observed_helpers),
                         "observed_assignment_ids": observed_helpers,
                         "model_pairs": [{"primary_model": primary, "companion_model": worker}
                                         for primary, worker in pairs]})
    known_pairs = set(receipt_rows)
    orphans = known_pairs - {(record.get("request_id"), record.get("assignment_id")) for record in records}
    return {"profile": PROFILE, "record_count": len(output), "request_count": len(requests),
            "observed_request_count": sum(item["any_helper_observed"] for item in requests),
            "records": output, "requests": requests,
            "model_pair_request_counts": [{"primary_model": pair[0], "companion_model": pair[1],
                                           "request_count": count} for pair, count in sorted(pair_counts.items())],
            "receipts": {"accepted_count": len(consumed_receipts), "replay_count": replay_count,
                         "conflict_count": len(receipt_conflicts),
                         "orphan_count": len(orphans), "conflicting_receipt_ids": conflicting_ids},
            "basis": "Exact request and assignment IDs joined to exact native thread and turn identities.",
            "limits": ["No attribution from paths, working directories or temporal proximity.",
                       "Requested and worker-reported models are not native model observations.",
                       "Savings require an explicit linked equal-quality paired comparison."]}


__all__ = ["PHASES", "PROFILE", "RECEIPT", "normalize_receipt", "project", "receipts", "record"]
