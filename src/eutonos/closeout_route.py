"""Closeout phases: required filing stays independent from optional memory."""
from __future__ import annotations

import sqlite3

from . import tokenslang as ts
from .command_registry import CLOSEOUT_FIELDS
from .effects import digest, LocalRoot
from .host import connect, selected_apply
from .managed_write import abort as abort_write, begin as begin_write
from .workflow import (PROFILE as MEMORY_PROFILE, prepare_closeout as prepare_memory,
                       execute_closeout as run_memory, validate_closeout, _shared_closeout_error)

PROFILE = "eutonos.tokenslang.organized-closeout.v1"
QUESTIONS = ["What changed and what was the result?", "Is the work completed, continuing or blocked?", "Did a durable project fact change? Update only its canonical owner."]


_OPTIONAL_RAM_FAILURES = {"error", "failed", "partial", "incomplete", "blocked", "conflict", "unavailable",
                          "denied", "stale", "attention-required"}


def _ram_attention(memory):
    """Describe an optional capture failure without reopening a landed filing."""
    if not isinstance(memory, dict) or memory.get("outcome") not in _OPTIONAL_RAM_FAILURES:
        return None
    detail = memory.get("message", "Optional RAM capture did not complete.")
    recovery = memory.get("next_supported_action") or memory.get("next") or memory.get("retry")
    return {"outcome": "attention-required", "phase": "ram-capture",
            "code": memory.get("code"), "message": detail,
            "next": ("Required work lifecycle filing has already landed. Do not replay lifecycle filing to retry RAM. "
                     + (str(recovery) if recovery else
                        "Resolve or disclose the optional RAM failure using its returned capture details.")),
            "required_filing": "landed", "optional_phase": "RAM capture"}


def _prepare(values, context):
    if isinstance(values, dict) and values.get("profile") == PROFILE:
        return values
    if not isinstance(values, dict) or "organization" not in values or values.get("profile") == "eutonos.tokenslang.workflow.v1":
        result = prepare_memory(values, context)
        result["organization"] = {"outcome": "not-assessed", "questions": QUESTIONS,
                                  "note": "Memory preparation does not establish task filing."}
        return result
    from . import closeout_organization as organization
    try:
        reuse = None
        if "shared_answers" in values:
            from .workflow import answer_reuse
            reuse = answer_reuse(values["shared_answers"])
            if reuse["outcome"] != "reviewed":
                return {"profile": PROFILE, "outcome": "incomplete", "phase": "answers",
                        "answer_reuse": reuse, "questions": QUESTIONS,
                        "next": "Correct the grouped semantic answers; valid supplied fields are preserved."}
        plan = organization.prepare(context.get("root", "."), values["organization"])
        result = {"profile": PROFILE, "outcome": "prepared", "command": "closeout", "organization": plan,
                "allowed_paths": plan["paths"], "questions": QUESTIONS,
                "retry_input": values,
                "memory": {"outcome": "not-requested", "reason": "Organization phase; capture can reuse the same summary under effective RAM settings."}}
        if reuse is not None:
            result["answer_reuse"] = reuse
            result["memory"]["reusable_values"] = reuse["values"]
            result["memory"]["answer_fingerprint"] = reuse["fingerprint"]
        return result
    except (OSError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        return {"profile": PROFILE, "outcome": "error", "phase": "organization", "message": getattr(exc, "message", str(exc))}


def _run(values, context):
    if not isinstance(values, dict) or values.get("profile") == "eutonos.tokenslang.workflow.v1" or (values.get("profile") != PROFILE and "organization" not in values):
        prepared = values if isinstance(values, dict) and values.get("profile") == MEMORY_PROFILE \
            and values.get("outcome") in {"prepared", "deferred"} else prepare_memory(values, context)
        result = run_memory(prepared, context) if prepared.get("outcome") in {"prepared", "deferred"} else prepared
        result["organization"] = {"outcome": "not-assessed", "note": "This result covers memory capture only, not complete task filing."}
        if result.get("outcome") in _OPTIONAL_RAM_FAILURES and prepared.get("outcome") in {"prepared", "deferred"}:
            result["prepared_operation"] = prepared
        return result
    from . import closeout_organization as organization
    prepared = _prepare(values, context)
    if prepared.get("outcome") != "prepared":
        return prepared
    plan = prepared["organization"]
    if not plan.get("paths"):
        return {"profile": PROFILE, "command": "closeout", "outcome": "skipped",
                "organization": {"outcome": "no-op", "writes": 0}, "memory": prepared["memory"]}
    attempt = "closeout-organization-" + plan["fingerprint"][:24]
    coordinator = authority = None
    generation = None
    try:
        coordinator, authority = connect(context["root"], context["host"], context["actor"])
        effect = plan["effect"]
        ts.require(digest(ts.encoded(effect)) == plan["fingerprint"], "conflict", "Organization fingerprint differs.")
        paths = sorted(change["path"] for change in effect["changes"])
        ts.require(paths == sorted(prepared["allowed_paths"]), "conflict", "Organization paths differ.")
        try:
            state = coordinator.inspect(attempt)
        except ts.ReadError as exc:
            if exc.code != "missing":
                raise
            state = None
        if state and state["state"] == "paused":
            readback = organization.readback(context["root"], plan)
            if readback.get("outcome") == "applied":
                ts.require(not state["reservations"] and not state["unfinished_effects"], "conflict", "Organization cleanup is incomplete.")
                return {**prepared, "outcome": "already-completed", "organization": {"outcome": "already-completed", "readback": readback}}
            ts.require(context.get("_write_retry_worker") is True and readback.get("outcome") == "pending"
                       and not state["reservations"] and not state["unfinished_effects"],
                       "stale", "Previously organized files changed; prepare a fresh filing scope.")
        if state is None:
            local = LocalRoot(context["root"])
            ts.require(all(digest(local.read(path)) == expected for path, expected in effect["observed"].items()),
                       "stale", "Organization source changed; prepare again before taking reservations.")
        coordinator, authority, generation = begin_write(context["root"], context["host"], context["actor"], attempt, paths)
        coordinator.reserve(authority, attempt, generation, paths)
        applied = selected_apply(coordinator, authority, attempt, generation, attempt, effect, plan["fingerprint"])
        readback = organization.readback(context["root"], plan)
        ts.require(readback.get("outcome") == "applied", "stale", "Organization readback differs from prepared output.")
        expected = {path: digest(authority.root.read(path)) for path in paths}
        coordinator.closeout(authority, attempt, generation, expected,
                             {"completed": "Selected task filing verified.", "next": "Capture optional memory separately if useful.", "pending": None})
        state = coordinator.inspect(attempt)
        ts.require(state["state"] == "paused" and not state["reservations"] and not state["unfinished_effects"],
                   "conflict", "Organization host cleanup is incomplete.")
        return {"profile": PROFILE, "command": "closeout", "outcome": "completed",
                "organization": {"outcome": "completed", "readback": readback, "writes": applied.get("writes"),
                                 "host": {"state": state["state"], "reservations": [], "unfinished_effects": []}},
                "memory": prepared["memory"], **({"answer_reuse": prepared["answer_reuse"]}
                                                    if "answer_reuse" in prepared else {})}
    except (OSError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        from . import write_retry
        if coordinator is not None and context.get("host") and context.get("actor"):
            abort_write(context["root"], context["host"], context["actor"], attempt, coordinator, authority)
        if write_retry.is_busy(exc) and not context.get("_write_retry_worker") and context.get("host") and context.get("actor"):
            # The exact plan cannot overwrite another task. Pause this clean,
            # reservation-free attempt and queue the semantic request so a
            # worker can replan from current bytes after the owner is released.
            try:
                retry_values = prepared.get("retry_input")
                ts.require(isinstance(retry_values, dict), "invalid", "Prepared closeout lacks semantic retry input.")
                safe_context = {key: context[key] for key in ("work_id", "session_id") if key in context}
                queued = write_retry.enqueue(context["host"], context["root"], context["actor"],
                                             "closeout-organization", {"values": retry_values, "context": safe_context})
                queued["worker"] = write_retry.start_worker(context["host"], context["root"])
                return {"profile": PROFILE, "command": "closeout", "outcome": "deferred",
                        "organization": {"outcome": "queued", "reason": "write-owner-busy", "queue": queued},
                        "memory": {"outcome": "not-started"},
                        "next": "No manual retry is required. Inspect the write retry queue if it turns red."}
            except (OSError, ValueError, KeyError, TypeError, ts.ReadError) as queue_exc:
                return {"profile": PROFILE, "outcome": "error", "phase": "organization", "attempt": attempt,
                        "message": getattr(queue_exc, "message", str(queue_exc)),
                        "original_error": getattr(exc, "message", str(exc)),
                        "next": "The write queue could not retain this closeout; preserve the prepared input and retry explicitly.",
                        "memory": {"outcome": "not-started"}}
        return {"profile": PROFILE, "outcome": "error", "phase": "organization", "attempt": attempt,
                "message": getattr(exc, "message", str(exc)), "allowed_paths": prepared.get("allowed_paths", []),
                "next": "Resolve the specific host/source issue and reuse the exact prepared plan; no automatic reset.",
                "memory": {"outcome": "not-started"}}


def _catalog_checked(operation, values, context):
    from . import catalog_closeout, work_closeout
    from .workflow import _context_missing

    # An exact RAM packet is a phase-only recovery input. It must never revisit
    # an already-filed lifecycle batch or re-assess catalog changes.
    if operation is _run and isinstance(values, dict) and values.get("profile") == MEMORY_PROFILE \
            and values.get("outcome") in {"prepared", "deferred"}:
        return _run(values, context)
    if not isinstance(values, dict):
        return {"profile": PROFILE, "command": "closeout", "outcome": "incomplete", "phase": "input",
                "message": "Closeout input must be a JSON object.",
                "catalog_assessment": catalog_closeout.assess(values, context.get("root", ".")),
                "work_assessment": work_closeout.assess(values, context.get("root", ".")),
                "lifecycle_assessment": None, "memory_assessment": validate_closeout(values),
                "missing_context": _context_missing(context),
                "organization": {"outcome": "not-assessed", "questions": QUESTIONS},
                "next": "Supply one closeout JSON object with the required assessment and semantic fields."}
    if isinstance(values, dict) and ("lifecycle_plans" in values or "lifecycle_batch" in values) and isinstance(values.get("values"), dict):
        if values.get("code_delivery") is not None:
            context = {**context, "_code_delivery": values["code_delivery"]}
        values = values["values"]
    lifecycle = work_closeout.lifecycle_assessment(values, context)
    assessment = catalog_closeout.assess(values, context.get("root", "."))
    work_assessment = work_closeout.assess(values, context.get("root", "."))
    clean = work_closeout.strip(catalog_closeout.strip(values))
    clean = {k: v for k, v in clean.items() if k not in {"work_closeout", "work_closeout_reason"}}
    supplied_memory = {key: clean[key] for key in {field["name"] for field in CLOSEOUT_FIELDS} if key in clean}
    explicit_memory = validate_closeout({"request": "Derived from work filing", "actions": ["Derived from work filing"],
                                         "result": "Derived from work filing", "why": "Derived from work filing",
                                         **supplied_memory})
    if clean.get("actions") == []:
        explicit_memory["outcome"] = "incomplete"
        explicit_memory.setdefault("invalid_fields", []).append({"name": "actions", "type": "array[string]",
                                                                    "required": True, "value": []})
        explicit_memory["next"] = "Supply actual actions or omit actions to use the assessed work rows."
    if lifecycle and lifecycle["outcome"] == "verified" and lifecycle["requests"]:
        memory_values = (work_closeout.lifecycle_memory_values(lifecycle["requests"], clean)
                         if explicit_memory["outcome"] == "valid" else None)
    else:
        memory_values = clean
    memory_requested = bool(lifecycle and lifecycle["outcome"] == "verified" and lifecycle["requests"]) or "organization" not in clean
    memory_assessment = (explicit_memory if memory_requested and explicit_memory["outcome"] != "valid" else
                         validate_closeout(memory_values) if memory_requested
                         else {"outcome": "not-requested", "reason": "Organization-only closeout has no RAM phase."})
    missing_context = _context_missing(context)
    context_assessment = {"outcome": "incomplete" if missing_context else "valid",
                          "missing_fields": missing_context,
                          "next": "Supply this checkout's actual host, actor and session ID before closeout; a work ID is optional (closeout creates one when none resolves)."}
    phase = next((name for name, check in (("catalog", assessment), ("work", work_assessment),
                                           ("work-lifecycle", lifecycle), ("memory", memory_assessment),
                                           ("context", context_assessment if memory_requested else None))
                  if check is not None and check["outcome"] not in {"verified", "valid", "not-requested"}), None)
    if phase:
        selected = {"catalog": assessment, "work": work_assessment,
                    "work-lifecycle": lifecycle, "memory": memory_assessment,
                    "context": context_assessment}[phase]
        return {"profile": PROFILE, "command": "closeout", "outcome": "incomplete", "phase": phase,
                "catalog_assessment": assessment, "work_assessment": work_assessment,
                "lifecycle_assessment": lifecycle, "memory_assessment": memory_assessment,
                "context_assessment": context_assessment,
                "organization": {"outcome": "not-assessed", "questions": QUESTIONS,
                                 "note": "Memory preparation does not establish task filing."},
                "missing_context": missing_context, "values": values,
                "next": selected.get("next", "Correct the reported RAM fields before closeout.")}
    if lifecycle and lifecycle["requests"]:
        # Check deterministic RAM size before native filing. RAM availability
        # and source reads remain optional phase failures, not filing gates.
        # Filing changes the board source, so this packet is never reused.
        memory_context = {k: v for k, v in context.items() if k not in {"delivery", "_code_delivery"}}
        memory_preparation = prepare_memory(memory_values, memory_context)
        if memory_preparation.get("outcome") == "incomplete" and memory_preparation.get("code") == "overflow":
            return {"profile": PROFILE, "command": "closeout", "outcome": "incomplete",
                    "phase": "memory-preparation", "catalog_assessment": assessment,
                    "work_assessment": work_assessment, "lifecycle_assessment": lifecycle,
                    "memory_assessment": memory_assessment,
                    "memory_preparation": memory_preparation,
                    "context_assessment": context_assessment,
                    "organization": {"outcome": "not-assessed", "questions": QUESTIONS,
                                     "note": "Memory preparation does not establish task filing."},
                    "missing_context": missing_context, "values": values,
                    "next": memory_preparation.get("next", "Correct the RAM preparation error before filing work.")}
        result = _lifecycle_closeout(operation, values, clean, context, lifecycle)
        if memory_preparation.get("outcome") not in {"prepared", "deferred"}:
            result["memory_preparation"] = memory_preparation
    else:
        result = operation(clean, context)
    result["catalog_assessment"] = assessment
    result["work_assessment"] = work_assessment
    result["memory_assessment"] = memory_assessment
    result["context_assessment"] = context_assessment
    return result


def _lifecycle_closeout(operation, values, clean, context, assessment):
    from . import work_closeout, write_batch, write_retry
    requests = assessment["requests"]
    batch = {"profile": write_batch.PROFILE,
             "operations": [{"kind": "work", "request": request} for request in requests],
             "requested_by": {"kind": "agent-claim", "value": context.get("model", "guided-closeout")},
             'caller_context': {'app': context.get('app') or context.get('harness'),
                                'model': context.get('model'), 'session': context.get('session_id')}}
    code_delivery = context.get('_code_delivery')
    if code_delivery is None and context.get('delivery') is not None:
        from .git_delivery_native import prepare_code
        normalized = write_batch.normalize(batch)
        native_id = write_retry._request_id(__import__('pathlib').Path(context['root']).resolve(),context['actor'],'batch',{'batch':normalized})
        code_delivery = prepare_code(context['root'],context['delivery'],session=context['session_id'],work_id=context['work_id'],
            closeout={'host':context['host'],'actor':context['actor'],'attempt':'write-batch-' + native_id.removeprefix('write-batch-')})
    if operation is _prepare:
        plan = write_batch.plan(context["root"], batch)
        return {"profile": PROFILE, "command": "closeout", "outcome": "prepared",
                "lifecycle_assessment": assessment, "lifecycle_batch": plan,
                **({"code_delivery": code_delivery} if code_delivery is not None else {}),
                "allowed_paths": plan["paths"],
                "values": values, "next": "Run closeout with these same semantic answers and the existing host context. Filing is required; RAM is assessed independently."}
    ts.require(bool(context.get("host") and context.get("actor")), "invalid", "Work closeout requires the existing host and actor.")
    try:
        receipt = write_retry.submit_batch(context['host'], context['root'], context['actor'], batch)
    except ts.ReadError as exc:
        # A frozen native context refusal remains a refusal; report it through
        # the guided closeout result without releasing checked product code.
        return {'profile':PROFILE,'command':'closeout','outcome':'error','phase':'work-lifecycle',
                'code':exc.code,'message':exc.message,'lifecycle':[],
                'organization':{'outcome':'not-started'},'memory':{'outcome':'not-started'},
                'git_delivery':{'state':'unavailable','error':exc.message},
                'next':'Reuse the original bound caller context or inspect the conflicting native request; no replay or code release occurred.'}
    receipts = [receipt]
    if receipt["outcome"] == "error":
        return {"profile": PROFILE, "outcome": "error", "phase": "work-lifecycle", "lifecycle": receipts,
                "memory": {"outcome": "not-started"}, "next": "Inspect the failed atomic filing receipt; no subset is reported complete."}
    pending = receipt["outcome"] != "complete"
    # Reuse every filed item. Native identity/context still govern optional RAM.
    memory_values = work_closeout.lifecycle_memory_values(requests, clean)
    if pending:
        memory = {"outcome": "skipped", "reason": "required-filing-queued", "message": "Board submission is durable. RAM was not captured from a partially filed state."}
        memory_result = None
    else:
        memory_context = {k: v for k, v in context.items() if k not in {"delivery", "_code_delivery"}}
        memory_result = _run(memory_values, memory_context)
        memory = memory_result.get("memory", memory_result)
    attention = None if pending else _ram_attention(memory)
    result = {"profile": PROFILE, "command": "closeout",
              "outcome": "deferred" if pending else "attention-required" if attention else "completed",
              "organization": {"outcome": "queued" if pending else "completed", "receipts": receipts},
              "lifecycle": receipts, "memory": memory,
              "next": ("Work submitted; queue owns retries. Read receipts for completion." if pending else
                       attention["next"] if attention else "Work filing verified. Continue from Current Tasks.")}
    if code_delivery is not None:
        if pending or attention:
            result['git_delivery'] = {'state': 'waiting-native'}
        else:
            from .git_delivery_native import deliver_code, _release_code
            try:
                _release_code(context['root'],code_delivery,context['host'],
                        session=context['session_id'],work_id=context['work_id'],actor=context['actor'],selection=context.get('delivery'))
            except (OSError,ValueError,sqlite3.Error,ts.ReadError) as exc:
                result['git_delivery'] = {'state':'unavailable','error':str(exc)}
                return result
            result['git_delivery'] = deliver_code(context['root'], code_delivery,
                session=context['session_id'], work_id=context['work_id'], selection=context.get('delivery'))
    if attention:
        result["phase"] = "ram-capture"
        result["ram_attention"] = attention
        result["memory_phase"] = memory_result
        prepared_operation = memory_result.get("prepared_operation")
        if prepared_operation is not None:
            result["memory_recovery"] = {
                "phase": "memory", "mode": "exact-prepared-operation",
                "input": prepared_operation,
                "next": "Inspect the original RAM attempt/readback, then run only this prepared RAM packet with workflow run closeout; do not replay work filing."}
        else:
            result["memory_recovery"] = {
                "phase": "memory", "mode": "prepare-memory-only",
                "input": {**memory_values, "catalog_changes": [], "work_changes": [],
                          "work_closeout": [], "work_closeout_reason": "Required filing already landed; RAM-only recovery."},
                "next": "Correct the RAM error, then prepare/run only this memory input; do not replay work filing."}
    return result


def prepare(values, context):
    refusal = _shared_closeout_error(context, PROFILE)
    if refusal is not None:
        return refusal
    return _catalog_checked(_prepare, values, context)


def run(values, context):
    refusal = _shared_closeout_error(context, PROFILE)
    if refusal is not None:
        return refusal
    return _catalog_checked(_run, values, context)
