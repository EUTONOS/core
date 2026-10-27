"""Exact native-turn matching for local assignment metadata; no inferred usage."""
from collections import Counter, defaultdict
from copy import deepcopy

from .companion_results import FIELDS, _counts
from . import model_catalog


def model_selection(record):
    """Copy only bounded model provenance, including pre-field routing records."""
    selection = record.get("model_selection")
    if selection is None:
        selection = (record.get("routing") or {}).get("model_selection")
    if not isinstance(selection, dict):
        return None
    names = ("requested", "resolved", "selection_kind", "family", "supported_efforts", "basis")
    if (not all(name in selection for name in names)
            or not model_catalog.is_exact_model_id(selection["resolved"])
            or not isinstance(selection["requested"], str) or len(selection["requested"]) > 128
            or selection["selection_kind"] not in ("pin", "family")
            or selection["family"] not in (*model_catalog.FAMILIES, None)
            or not isinstance(selection["basis"], str) or len(selection["basis"]) > 1024
            or not isinstance(selection["supported_efforts"], (tuple, list))
            or not 0 < len(selection["supported_efforts"]) <= 6
            or not all(effort in ("low", "medium", "high", "xhigh", "max", "ultra")
                       for effort in selection["supported_efforts"])):
        return None
    return {name: deepcopy(selection[name]) for name in names}


def resolved_worker(record):
    requested = (record.get("requested_worker") or {}).get("model")
    if model_catalog.is_exact_model_id(requested):
        return requested
    selection = model_selection(record)
    if (requested in model_catalog.FAMILIES and selection
            and selection["requested"] == requested and selection["selection_kind"] == "family"
            and selection["family"] == requested):
        return selection["resolved"]
    return None


def metadata(records):
    """Exclude briefs, findings and intermediate work from metrics payloads."""
    names = ("assignment_id", "state", "request_id", "primary", "requested_worker", "attachment",
             "created_at", "updated_at")
    output = []
    for record in records:
        item = {name: deepcopy(record.get(name)) for name in names}
        selection = model_selection(record)
        if selection is not None:
            item["model_selection"] = selection
        output.append(item)
    return output


def _key(value):
    if not isinstance(value, dict):
        return None
    thread, turn = value.get("thread_id"), value.get("turn_id")
    return (thread, turn) if isinstance(thread, str) and thread and isinstance(turn, str) and turn else None


def project(records, runs):
    rows = defaultdict(list)
    for row in runs:
        key = _key(row)
        if key:
            rows[key].append(row)
    claims = Counter(_key(record.get("attachment")) for record in records
                     if record.get("state") != "prepared" and _key(record.get("attachment")))
    pairs = defaultdict(list)
    items, requests = [], set()
    for record in records:
        primary, worker = record.get("primary") or {}, record.get("requested_worker") or {}
        exact_worker = resolved_worker(record)
        pk, wk = _key(primary), _key(record.get("attachment"))
        candidates = rows.get(wk, [])
        primary_rows = rows.get(pk, [])
        eligible = record.get("state") in {"dispatched", "completed", "needs-attention"}
        ambiguous = bool(wk and (claims[wk] > 1 or len(candidates) > 1))
        worker_match = (eligible and not ambiguous and len(candidates) == 1
                        and candidates[0].get("state") == "completed"
                        and exact_worker is not None and candidates[0].get("models") == [exact_worker])
        primary_match = (len(primary_rows) == 1 and primary_rows[0].get("models") == [primary.get("model")])
        usage = _counts(candidates[0].get("usage")) if worker_match else None
        exact_pair = bool(worker_match and primary_match)
        item = {"assignment_id": record.get("assignment_id"), "request_id": record.get("request_id"),
                "state": record.get("state"), "requested_primary": primary.get("model"),
                "requested_worker": worker.get("model"), "worker_turn_observed": bool(worker_match),
                "resolved_worker": exact_worker,
                "pair_observed": exact_pair, "usage": usage,
                "attribution": "ambiguous" if ambiguous else "exact-pair" if exact_pair else
                               "worker-only" if worker_match else "unverified"}
        items.append(item)
        if exact_pair:
            request_key = (record.get("request_id"), pk)
            requests.add(request_key)
            pairs[(primary["model"], exact_worker)].append((request_key, usage))
    grouped = []
    for (primary, worker), members in sorted(pairs.items()):
        known = [usage for _, usage in members if usage is not None]
        grouped.append({"primary_model": primary, "helper_model": worker,
                        "observed_request_count": len({key for key, _ in members}),
                        "observed_assignment_count": len(members),
                        "helper_tokens": {key: sum(value[key] for value in known) if len(known) == len(members) else None
                                          for key in FIELDS},
                        "unknown_usage_count": len(members) - len(known)})
    return {"profile": "eutonos.assignment-metrics.v1", "assignment_count": len(records),
            "collected_count": sum(record.get("state") in {"completed", "needs-attention"} for record in records),
            "observed_request_count": len(requests), "observed_worker_turn_count": sum(item["worker_turn_observed"] for item in items),
            "ambiguous_assignment_count": sum(item["attribution"] == "ambiguous" for item in items),
            "unverified_assignment_count": sum(not item["pair_observed"] for item in items),
            "pairs": grouped, "assignments": items,
            "basis": "Exact attached thread and turn IDs matched to already selected native rows and model identities. Requested or self-reported models alone are not observations.",
            "coverage": "Only explicit assignments; this is not the frequency of delegation across all user requests.",
            "account_charge": None}
