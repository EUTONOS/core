"""Optional, native per-requirement completion evidence for board work."""
from copy import deepcopy
import hashlib
import re

from . import tokenslang as ts

STATES = ("pending", "in_progress", "partial", "blocked", "verified")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_FIELDS = {"id", "title", "required", "state", "evidence", "remaining_action",
           "related_work_ids", "verification", "verification_history"}


def _definition(check):
    return hashlib.sha256(ts.encoded({key: check[key] for key in ("id", "title", "required", "evidence")})).hexdigest()


def validate(checks):
    """Validate stored checks, including stamps, without authorizing a write."""
    from . import boards, work_attribution
    ts.require(isinstance(checks, list) and len(checks) <= 40, "invalid",
               "Completion checks must be an array of at most 40 entries.")
    seen = set()
    result = []
    for supplied in checks:
        ts.require(isinstance(supplied, dict), "invalid", "Completion check must be an object.")
        ts.shape(supplied, {"id", "title", "required", "state", "evidence", "related_work_ids"},
                 _FIELDS - {"id", "title", "required", "state", "evidence", "related_work_ids"})
        row = deepcopy(supplied)
        identity = row["id"]
        ts.require(isinstance(identity, str) and _ID.fullmatch(identity) and identity not in seen,
                   "invalid", "Completion check IDs must be unique, stable, and bounded.")
        seen.add(identity)
        ts.require(isinstance(row["title"], str) and 0 < len(row["title"].strip()) <= 500,
                   "invalid", "Completion check title must be nonblank and at most 500 characters.")
        ts.require(type(row["required"]) is bool and row["state"] in STATES,
                   "invalid", "Completion check required/state is invalid.")
        ts.require(isinstance(row["evidence"], list) and len(row["evidence"]) <= 16,
                   "invalid", "Completion check evidence must have at most 16 references.")
        row["evidence"] = boards._refs(row["evidence"])
        related = row["related_work_ids"]
        ts.require(isinstance(related, list) and len(related) <= 16
                   and all(isinstance(v, str) and 0 < len(v.strip()) <= 128 for v in related)
                   and len(related) == len(set(related)), "invalid",
                   "Completion check related work IDs must be unique bounded strings.")
        if "remaining_action" in row:
            ts.require(isinstance(row["remaining_action"], str)
                       and 0 < len(row["remaining_action"].strip()) <= 1000,
                       "invalid", "Completion check remaining action must be bounded text.")
        if row["state"] in {"partial", "blocked"}:
            ts.require(bool(row.get("remaining_action")), "invalid",
                       "Partial or blocked completion check needs a remaining action.")
        verification = row.get("verification")
        if row["state"] == "verified":
            ts.require(bool(row["evidence"]) and isinstance(verification, dict), "invalid",
                       "Verified completion check needs evidence and a writer stamp.")
            ts.shape(verification, {"definition_sha256", "actor"})
            ts.require(verification["definition_sha256"] == _definition(row), "invalid",
                       "Completion check verification no longer matches its definition.")
            work_attribution.validate_snapshot(verification["actor"])
        else:
            ts.require(verification is None, "invalid",
                       "Only verified completion checks may carry a current stamp.")
            row.pop("verification", None)
        history = row.get("verification_history", [])
        ts.require(isinstance(history, list) and len(history) <= 20, "invalid",
                   "Completion check verification history must be bounded.")
        for entry in history:
            ts.shape(entry, {"verification", "invalidated_by", "reason"})
            ts.require(entry["reason"] in {"definition-changed", "state-reset"}, "invalid",
                       "Unknown completion check reset reason.")
            ts.shape(entry["verification"], {"definition_sha256", "actor"})
            ts.require(isinstance(entry["verification"]["definition_sha256"], str)
                       and re.fullmatch(r"[0-9a-f]{64}", entry["verification"]["definition_sha256"]),
                       "invalid", "Historical completion check digest is invalid.")
            work_attribution.validate_snapshot(entry["verification"]["actor"])
            work_attribution.validate_snapshot(entry["invalidated_by"])
        result.append(row)
    return result


def reconcile(proposed, previous=None, actor=None):
    """Apply one managed edit; caller-supplied new verification is never trusted."""
    prior = {row["id"]: row for row in validate(previous or [])}
    ts.require(isinstance(proposed, list), "invalid", "Completion checks must be an array.")
    ts.require(all(isinstance(row, dict) and
                   {"id", "title", "required", "state", "evidence", "related_work_ids"} <= set(row)
                   for row in proposed), "invalid", "Completion check fields are incomplete.")
    ts.require(all(isinstance(row["id"], str) and _ID.fullmatch(row["id"]) for row in proposed),
               "invalid", "Completion check IDs must be bounded strings.")
    ts.require(set(prior) <= {row.get("id") for row in proposed if isinstance(row, dict)},
               "invalid", "Completion check IDs cannot be removed or renamed.")
    rows = []
    for supplied in proposed:
        row = deepcopy(supplied)
        old = prior.get(row.get("id"))
        if old:
            ts.require(row.get("verification_history", old.get("verification_history", []))
                       == old.get("verification_history", []), "denied",
                       "Completion check verification history is writer-owned.")
            row["verification_history"] = deepcopy(old.get("verification_history", []))
            old_stamp = old.get("verification")
            if old_stamp:
                try:
                    changed = _definition(row) != _definition(old)
                except (TypeError, ValueError, KeyError) as exc:
                    raise ts.ReadError("invalid", "Completion check definition is invalid.") from exc
                if changed or row.get("state") != "verified":
                    ts.require(row.get("state") != "verified" and actor is not None, "invalid",
                               "Amend a verified requirement by resetting its state.")
                    ts.require(row.get("verification") in (None, old_stamp), "denied",
                               "Completion check verification is writer-owned.")
                    row.pop("verification", None)
                    row["verification_history"].append({
                        "verification": deepcopy(old_stamp), "invalidated_by": deepcopy(actor),
                        "reason": "definition-changed" if changed else "state-reset"})
                else:
                    ts.require(row.get("verification", old_stamp) == old_stamp, "denied",
                               "Completion check verification is writer-owned.")
                    row["verification"] = deepcopy(old_stamp)
            else:
                ts.require(row.get("verification") is None, "denied",
                           "Completion check verification is writer-owned.")
        else:
            ts.require(not row.get("verification") and not row.get("verification_history"), "denied",
                       "New completion check provenance is writer-owned.")
        if row.get("state") == "verified" and not row.get("verification"):
            ts.require(actor is not None, "invalid", "Verification needs writer attribution.")
            row["verification"] = {"definition_sha256": _definition(row), "actor": deepcopy(actor)}
        rows.append(row)
    return validate(rows)


def summary(checks):
    if checks is None:
        return None
    rows = validate(checks)
    if not rows:
        return None
    result = {"total": len(rows), "required_total": sum(c["required"] for c in rows),
              "required_verified": sum(c["required"] and c["state"] == "verified" for c in rows)}
    for state in STATES:
        result[state] = sum(c["state"] == state for c in rows)
    result["unmet_required"] = result["required_total"] - result["required_verified"]
    return result


def require_complete(item):
    checks = item.get("extensions", {}).get("completion_checks")
    if checks is not None:
        ts.require(not summary(checks) or summary(checks)["unmet_required"] == 0,
                   "invalid", "Required completion checks must be verified before completion.")
