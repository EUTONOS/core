"""Require explicit board-record assessment before closeout."""
from copy import deepcopy

from . import boards

FIELDS = ("work_changes", "work_ids")


def assess(values, root):
    raw = values if isinstance(values, dict) else {}
    prior = raw.get("work_assessment")
    supplied = {**(prior if isinstance(prior, dict) else {}),
                **{key: raw[key] for key in FIELDS if key in raw}}
    changes, ids = supplied.get("work_changes"), supplied.get("work_ids", [])
    items = boards.project(root).get("items", [])
    by_id = {item["id"]: item for item in items if item.get("type") in boards.WORK_FORM_TYPES}
    missing, errors = [], []
    if "work_changes" not in supplied:
        missing.append("work_changes")
    elif not isinstance(changes, list) or any(not isinstance(value, str) or not value.strip() for value in changes):
        errors.append("work_changes must list work added during this run; use [] explicitly for none.")
    if not isinstance(ids, list) or any(not isinstance(value, str) or not value.strip() for value in ids):
        errors.append("work_ids must be a list of saved campaign, stage, slice, or Quick work IDs.")
    elif changes and not ids:
        missing.append("work_ids")
    elif not changes and ids:
        errors.append("Explain work_changes when supplying work_ids.")
    else:
        for identity in ids:
            if identity not in by_id:
                errors.append("Work record not found: " + identity)
    return {"outcome": "incomplete" if missing or errors else "verified",
            "prompt": "Did the user add work while this run was active? It should already be on the board. List the additions in work_changes and their saved work_ids, or use [] for none.",
            "work_changes": deepcopy(changes), "work_ids": deepcopy(ids),
            "missing_fields": missing, "errors": errors,
            "next": "Use boards form, batch form and managed batch submit immediately, then repeat closeout. Closeout is a check, not the first time added work should be recorded.",
            "qualification": "Verifies that named work IDs exist; it cannot prove that an agent noticed every scope addition."}


def strip(values):
    if not isinstance(values, dict):
        return values
    return {key: deepcopy(value) for key, value in values.items()
            if key not in (*FIELDS, "work_assessment")}


def lifecycle_assessment(values, context):
    """Ask for estimates explicitly; never infer them from checkboxes or chat."""
    from . import work_lifecycle as work, tokenslang as ts
    from .effects import LocalRoot
    root = context.get("root", ".")
    if work.load_order(LocalRoot(root)) is None:
        return None
    items = boards.project(root)["items"]
    by_id = {i["id"]: i for i in items if i["type"] in work.WORK}
    selected = context.get("work_id")
    example = {"item_id": selected if selected in by_id else "<work ID>",
               "expected_revision": work.item_revision(by_id[selected]) if selected in by_id else "<revision from work form>",
               "status": "active", "what": "What did you do?", "why": "Why?", "result": "What happened?",
               "checks": ["Checks and any unrun validation."],
               "progress": {"percent": None, "basis": "Estimated effort complete, allowing for unequal stages; null if unknown."}}
    if selected in by_id and "completion_checks" in by_id[selected].get("extensions", {}):
        example["completion_checks"] = deepcopy(by_id[selected]["extensions"]["completion_checks"])
    examples = [example]
    selected_item = by_id.get(selected, {})
    if "source_import" in selected_item.get("extensions", {}):
        example.pop("progress")
        example["status"] = "cancelled"
    selected_stage = selected_item.get("extensions", {}).get("stage_id") if selected_item.get("type") == "slice" else None
    mapped_stage = by_id.get(selected_stage) if isinstance(selected_stage, str) else None
    if mapped_stage and mapped_stage["type"] == "stage":
        examples.append({**deepcopy(example), "item_id": selected_stage,
                         "expected_revision": work.item_revision(mapped_stage), "status": mapped_stage["status"],
                         "what": "File the actual stage checkpoint affected by this slice.",
                         "result": "State completed and remaining stage scope; do not complete the stage from slice completion alone."})
    parent_id = mapped_stage.get("parent_id") if mapped_stage and mapped_stage["type"] == "stage" else selected_item.get("parent_id")
    parent = by_id.get(parent_id)
    if (parent and parent["type"] == "campaign" and parent["status"] not in work.TERMINAL
            and "source_import" not in parent.get("extensions", {})):
        examples.append({**deepcopy(example), "item_id": parent_id, "expected_revision": work.item_revision(parent),
                         "status": parent["status"], "progress": {"percent": None,
                         "basis": "Update the overall campaign estimate after this stage; null if remaining effort is unknown."}})
    rows = values.get("work_closeout")
    missing, errors, requests = [], [], []
    reason = values.get("work_closeout_reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        errors.append("work_closeout_reason must be a nonblank explanation when supplied.")
    if rows is None:
        missing.append("work_closeout")
    elif not isinstance(rows, list):
        errors.append("work_closeout must be an array of answers.")
    elif not rows and not reason:
        missing.append("work_closeout_reason")
    else:
        seen = set()
        for row in rows:
            try:
                ts.shape(row, {"item_id", "expected_revision", "status", "what", "why", "result", "checks"},
                         {"progress", "acceptance_confirmed", "defer_owner_move", "next_action", "lane", "stage_assessment", "execution_recommendation", "completion_checks",
                          "cancellation_reason", "superseded_by"})
                identity = row["item_id"]
                ts.require(identity in by_id and identity not in seen, "invalid", "Select each known work item once.")
                seen.add(identity)
                item = by_id[identity]
                imported = "source_import" in item.get("extensions", {})
                for field in ("what", "why", "result"): ts.string(row[field])
                ts.require(row["status"] in {"active", "blocked", "planned", "deferred", "completed", "cancelled", "superseded"},
                           "invalid", "Select the current work status.")
                if row["status"] in {"cancelled", "superseded"}:
                    ts.require(isinstance(row.get("cancellation_reason"), str) and row["cancellation_reason"].strip(),
                               "invalid", "Cancelled or superseded work requires a cancellation_reason.")
                if row["status"] == "superseded":
                    ts.string(row.get("superseded_by"))
                if imported:
                    ts.require(row["status"] == "cancelled" and "progress" not in row
                               and "completion_checks" not in row, "invalid",
                               "Imported work can only be cancelled without changing progress or provenance.")
                else:
                    ts.require("progress" in row, "invalid", "Closeout requires a progress estimate or null with a basis.")
                    work.progress({"extensions": {"progress": row["progress"]}})
                if "defer_owner_move" in row:
                    ts.require(type(row["defer_owner_move"]) is bool and item["type"] == "campaign" and row["status"] in work.TERMINAL,
                               "invalid", "defer_owner_move must be a boolean on terminal campaign decisions only.")
                ts.require(isinstance(row["checks"], list) and row["checks"] and all(isinstance(c, str) and c.strip() for c in row["checks"]),
                           "invalid", "State the checks, including any explicitly unrun checks.")
                if by_id[identity]["type"] == "slice" and row["status"] == "completed" and "stage_id" in by_id[identity].get("extensions", {}):
                    ts.require(row["expected_revision"] == work.item_revision(by_id[identity]), "stale",
                               "Slice or stage association changed; reread its current work form before completion.")
                adaptive = None
                if "stage_assessment" in row:
                    ts.require(row["status"] == "completed" and item["type"] == "stage", "invalid", "stage_assessment belongs only to stage completion.")
                    adaptive = work.stage_assessment(row["stage_assessment"], item, items)
                recommendation = None
                if "execution_recommendation" in row:
                    ts.require(item["type"] in {"stage", "slice"}, "invalid", "Execution recommendations belong to stages or slices.")
                    recommendation = boards.execution_recommendation(row["execution_recommendation"])
                request = {k: deepcopy(row[k]) for k in ("item_id", "expected_revision", "what", "why", "result", "checks")}
                if not imported:
                    request["progress"] = deepcopy(row["progress"])
                    if "completion_checks" in row:
                        request["completion_checks"] = deepcopy(row["completion_checks"])
                request["operation"] = ({"completed": "complete", "cancelled": "cancel", "superseded": "supersede"}
                                        .get(row["status"], "update"))
                if row["status"] in {"cancelled", "superseded"}:
                    request["why"] = row["cancellation_reason"].strip()
                    if row["status"] == "superseded": request["superseded_by"] = row["superseded_by"]
                elif row["status"] != "completed":
                    request["changes"] = {"status": row["status"]}
                    if "next_action" in row: request["changes"]["next_action"] = row["next_action"]
                if recommendation is not None:
                    request.setdefault("changes", {})["execution_recommendation"] = recommendation
                if adaptive is not None:
                    request["stage_assessment"] = adaptive
                for key in ("acceptance_confirmed", "defer_owner_move", "lane"):
                    if key in row: request[key] = row[key]
                requests.append(request)
            except (TypeError, KeyError, ValueError, ts.ReadError) as exc:
                errors.append(str(exc))
        if selected in by_id and selected not in seen and not values.get("work_closeout_reason"):
            missing.append("work_closeout for " + selected)
        for request in requests:
            item = by_id[request["item_id"]]
            parent_id = item.get("parent_id")
            if item["type"] == "slice" and request["operation"] == "complete" and "stage_id" in item.get("extensions", {}):
                try:
                    stage_id = boards._validate_stage_association(item, {key: ("", value) for key, value in by_id.items()})
                    stage = by_id[stage_id]
                    if stage_id not in seen and "stage closeout for " + stage_id not in missing:
                        missing.append("stage closeout for " + stage_id)
                    if stage["status"] in work.TERMINAL:
                        errors.append("Associated stage " + stage_id + " is terminal; restore the stage through its lifecycle before filing new stage work.")
                    parent_id = stage.get("parent_id")
                except (TypeError, KeyError, ValueError, ts.ReadError) as exc:
                    errors.append(str(exc))
            parent = by_id.get(parent_id)
            if (parent and parent["type"] == "campaign" and parent["status"] not in work.TERMINAL
                    and "source_import" not in parent.get("extensions", {}) and parent_id not in seen):
                field = "campaign progress for " + parent_id
                if field not in missing: missing.append(field)
    return {"outcome": "incomplete" if missing or errors else "verified", "missing_fields": missing, "errors": errors,
            "prompt": "What did you do, why, what was the result, and what percent is this work item or campaign done? Imported work can only be cancelled and must omit progress. If status is cancelled or superseded, also provide cancellation_reason; superseded also requires superseded_by.",
            "cancellation": {"required_for": ["cancelled", "superseded"],
                             "prompt": "Why was this work cancelled or replaced?",
                             "replacement_field": "superseded_by"},
            "stage_assessment": {"prompt": "For each completed slice, assess which stage changed. Save an explicit extensions.stage_id association when applicable; otherwise state in its result that no stage changed. A slice completion never implies stage completion.",
                                 "adaptive_optional": True,
                                 "adaptive_example": {"disposition": "unchanged", "basis": "Stage evidence leaves the next plan unchanged.", "affected_stage_ids": [], "changes": []},
                                 "adaptive_prompt": "Optionally record a completed stage's effect on the next plan; separate explicit update rows make any actual plan changes.",
                                 "standalone_slices": [identity for identity, item in by_id.items()
                                                       if item["type"] == "slice" and "stage_id" not in item.get("extensions", {})
                                                       and (identity == selected or any(row["item_id"] == identity for row in requests))]},
            "example": {"work_closeout": examples}, "requests": requests,
            "next": "Supply work_closeout answers. Use [] plus work_closeout_reason only when no tracked work changed. Estimates do not complete work."}


def lifecycle_memory_values(requests, supplied):
    """Reuse every validated lifecycle answer for local and GitHub RAM alike.

    This only formats supplied facts. Filing and RAM execution retain their own
    receipts. Never truncate a later item to fit: the normal RAM size guard must
    return an explicit incomplete disposition rather than silently lose coverage.
    Agent-supplied request/actions/why/result take precedence; the work rows
    remain the fallback and are retained in the filed board record. Supplied
    checks are kept ahead of row checks without duplicates.
    """
    from . import tokenslang as ts
    ts.require(isinstance(requests, list) and bool(requests), "invalid",
               "Lifecycle memory requires at least one assessed work request.")
    first = requests[0]
    values = {"request": first["what"], "actions": [first["what"]], "why": first["why"],
              "result": first["result"], "checks": deepcopy(first["checks"]), "unresolved": [],
              "work_size": "slice", "event": "turn"}
    if len(requests) > 1:
        def label(row):
            status = "completed" if row["operation"] == "complete" else row.get("changes", {}).get("status", row["operation"])
            return "[" + row["item_id"] + "; " + status + "] "

        values.update(
            request="\n".join(label(row) + row["what"] for row in requests),
            actions=[label(row) + row["what"] for row in requests],
            why="\n".join(label(row) + row["why"] for row in requests),
            result="\n".join(label(row) + row["result"] for row in requests),
            checks=[label(row) + check for row in requests for check in row["checks"]],
        )
    for name in ("request", "why", "result"):
        if isinstance(supplied.get(name), str) and supplied[name].strip():
            values[name] = supplied[name]
    if supplied.get("actions"):
        values["actions"] = deepcopy(supplied["actions"])
    if supplied.get("checks"):
        values["checks"] = list(dict.fromkeys([*supplied["checks"], *values["checks"]]))
    for name in ("changed_paths", "unresolved", "decisions", "work_size", "event", "children"):
        if name in supplied:
            values[name] = deepcopy(supplied[name])
    return values
