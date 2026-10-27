"""Local companion experiment projection. Explicit logs, metadata only, no model calls.

Codex JSONL is a version-sensitive observation source, not a billing API. The
ignored configuration selects logs; no transcript text or log path is exported.
"""
from datetime import datetime, timezone
import hashlib
import os
import json
from pathlib import Path
from .private_scope import require_shared
from .companion_quality import enrich
from .companion_benchmarks import project_suites
from .companion_history import cached_read, sync, tracking
from .companion_native_sessions import sources as native_sources

CONFIG = "eutonos.companion-results.v1"
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
RATES = [
    {"model": "gpt-6-luna", "label": "Luna", "input": .20, "cached": .02, "output": 1.20},
    {"model": "gpt-6-sol", "label": "Sol", "input": 4., "cached": .40, "output": 20.},
    {"model": "gpt-6-astra", "label": "Astra", "input": 10., "cached": 1., "output": 50.},
]
PRICING = {"checked_at": "2026-09-12", "currency": "USD", "unit": "per million tokens",
           "basis": "Standard API equivalent, not Codex charges",
           "rates": [{**r, "source": "https://developers.openai.com/api/docs/models/" + r["model"]} for r in RATES],
           "limits": "Only observed models are priced. Estimates are withheld above 272K peak input or when model/counters are unknown. Standard API rates exclude cache-write, service-tier and tool fees; actual Codex charges remain unknown. Sol promotional rates last at least through 2026-11-21."}
CREDIT_RATES = [
    {"model": "gpt-6-luna", "label": "Luna", "input": 5., "cached": .5, "output": 30.},
    {"model": "gpt-6-sol", "label": "Sol", "input": 100., "cached": 10., "output": 500.},
    {"model": "gpt-6-astra", "label": "Astra", "input": 250., "cached": 25., "output": 1250.},
]
CREDIT_PRICING = {"checked_at": "2026-09-13", "unit": "credits per million tokens",
                  "basis": "Standard-rate credit estimate, not actual deduction",
                  "rates": [{**rate, "source": "https://learn.chatgpt.com/docs/pricing"} for rate in CREDIT_RATES],
                  "limits": "Whole observed token counters at published standard rates. Cached input and reasoning output are subsets, not additional token buckets. Actual plan, speed tier and deducted credits remain unknown."}


def _counts(value):
    if not isinstance(value, dict):
        return None
    result = {k: value.get(k) for k in FIELDS}
    if any(type(v) is not int or v < 0 for v in result.values()):
        return None
    if result[FIELDS[1]] > result[FIELDS[0]] or result[FIELDS[3]] > result[FIELDS[2]]:
        return None
    return result


def _plus(left, right, sign=1):
    return {k: left[k] + sign * right[k] for k in FIELDS}


def estimate(usage, rate):
    """Cached input and reasoning are subsets, never extra token buckets."""
    u = _counts(usage)
    if u is None:
        return None
    return ((u[FIELDS[0]] - u[FIELDS[1]]) * rate["input"] +
            u[FIELDS[1]] * rate["cached"] + u[FIELDS[2]] * rate["output"]) / 1_000_000


def observed_cost(row):
    models = row.get("models", [])
    rate = next((r for r in RATES if models == [r["model"]]), None)
    if not rate or row.get("state") != "completed" or row.get("max_input") is None or row["max_input"] > 272000:
        return None
    return estimate(row.get("usage"), rate)


def standard_credit_estimate(row):
    rate = next((item for item in CREDIT_RATES if row.get("models") == [item["model"]]), None)
    if not rate or row.get("state") != "completed":
        return None
    return estimate(row.get("usage"), rate)


def comparisons(experiments, runs):
    """Explicit paired samples only; never infer a counterfactual model workload."""
    output = []
    by_id = {e["id"]: e for e in experiments}
    for e in experiments:
        baseline = by_id.get(e.get("baseline_id"))
        if not baseline:
            continue
        gaps = []
        if sum(x.get("baseline_id") == baseline["id"] for x in experiments) > 1: gaps.append("Baseline reused across samples; aggregate savings withheld")
        if e.get("mode") != "on" or baseline.get("mode") != "off": gaps.append("Requires companion-on versus companion-off")
        if not e.get("comparison_key") or e["comparison_key"] != baseline.get("comparison_key"): gaps.append("Task, source, conditions or prospective quality review do not match")
        if e.get("primary_model") != baseline.get("primary_model"): gaps.append("Primary model differs")
        if any(x.get("accounting_basis") != "separate-worker-logs-no-children" for x in (e, baseline)): gaps.append("Parent/child usage overlap is unresolved")
        selected = [[r for r in runs if r["experiment_id"] == x["id"]] for x in (baseline, e)]
        costs = [sum(observed_cost(r) for r in rows) if rows and all(observed_cost(r) is not None for r in rows) else None for rows in selected]
        if None in costs: gaps.append("Complete observed-model costs unavailable")
        scores = [x.get("assessment", {}).get("score") if x.get("assessment") else None for x in (baseline, e)]
        # Equal-or-higher scored output is a narrow sample qualification, not a
        # universal equivalence threshold or a claim about future requests.
        if None in scores or scores[1] < scores[0]: gaps.append("Companion output has not met baseline rubric score")
        output.append({"experiment_id": e["id"], "baseline_id": baseline["id"], "baseline_cost_usd": costs[0],
                       "companion_cost_usd": costs[1], "savings_usd": None if gaps else costs[0] - costs[1],
                       "quality_delta": None if None in scores else scores[1] - scores[0], "limits": gaps,
                       "notes": list(dict.fromkeys((e.get("assessment") or {}).get("limitations", []) + (baseline.get("assessment") or {}).get("limitations", []))),
                       "end": max((r.get("end") or "" for r in selected[1]), default=None)})
    return output


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include its time zone")
    return result


def read_turns(path, role, experiment_id, after=None, before=None):
    lower, upper = _time(after) if after else None, _time(before) if before else None
    if lower and upper and lower >= upper:
        raise ValueError("Time window must be increasing")
    previous = None
    thread = None
    model = None
    effort = None
    active = None
    rows = []
    diagnostics = []
    digest = hashlib.sha256()
    offset = 0
    zero = dict.fromkeys(FIELDS, 0)
    unreflected = zero.copy()
    unreflected_responses = []
    compactions = {}
    previous_last = None
    with Path(path).open("rb") as stream:
        end = Path(path).stat().st_size
        while stream.tell() < end:
            raw = stream.readline(end - stream.tell())
            if not raw.endswith(b"\n"):
                diagnostics.append("Trailing partial event deferred until the next refresh.")
                break
            digest.update(raw)
            offset += len(raw)
            try:
                event = json.loads(raw)
                p = event.get("payload") or {}
                kind, when = event.get("type"), event.get("timestamp")
                if not isinstance(p, dict):
                    continue
                if kind == "session_meta":
                    if thread and thread != p.get("id"):
                        raise ValueError("Native thread identity changed")
                    thread = p.get("id")
                elif kind == "turn_context":
                    model = p.get("model")
                    candidate_effort = p.get("effort")
                    if not isinstance(candidate_effort, str) or not candidate_effort:
                        mode = p.get("collaboration_mode")
                        settings = mode.get("settings") if isinstance(mode, dict) else None
                        candidate_effort = settings.get("reasoning_effort") if isinstance(settings, dict) else None
                    effort = candidate_effort if isinstance(candidate_effort, str) and candidate_effort else None
                elif kind == "event_msg" and p.get("type") == "task_started":
                    if active:
                        active["state"] = "interrupted"
                    model = None
                    effort = None
                    active = {"id": str(thread) + ":" + str(p.get("turn_id") or offset),
                              "thread_id": thread, "turn_id": p.get("turn_id"),
                              "experiment_id": experiment_id, "role": role, "start": when,
                              "end": None, "state": "running", "models": [], "efforts": [],
                              "usage": dict.fromkeys(FIELDS, 0),
                              "first_input": None, "max_input": None, "handoff_tokens": None,
                              "tool_calls": 0, "tool_counts": {}, "tool_sequence": [],
                              "counter_updates": 0, "counter_basis": "Native cumulative deltas", "gaps": [], "_final": False}
                    active.update(compaction_count=0, compaction_usage=zero.copy(),
                                  compaction_basis="Native response usage; reconciled with turn and neighboring counters",
                                  _pending_compaction=None, _compaction_unknown=False)
                    active["final_sha256"] = None
                    rows.append(active)
                elif kind == "response_item" and active:
                    if p.get("type") in ("function_call", "custom_tool_call"):
                        active["tool_calls"] += 1
                        name = p.get("name")
                        name = name if isinstance(name, str) and name else "unknown"
                        active["tool_counts"][name] = active["tool_counts"].get(name, 0) + 1
                        active["tool_sequence"].append(name)
                    if p.get("type") == "message" and p.get("role") == "assistant" and p.get("phase") in ("final_answer", "final"):
                        # Observe phase only. Do not copy final or reasoning text.
                        active["_final"] = True
                        active["final_sha256"] = hashlib.sha256(json.dumps(p.get("content"), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                elif kind == "compacted":
                    # Only usage/identity metadata is inspected; compacted text
                    # and replacement history are never projected.
                    record = p.get("latest_token_usage_record")
                    record = record if isinstance(record, dict) else {}
                    response = record.get("response_id")
                    usage = _counts(record.get("usage"))
                    turn_usage = _counts(record.get("turn_token_usage"))
                    thread_usage = _counts(record.get("thread_token_usage"))
                    identity = tuple(record.get(k) for k in ("thread_id", "turn_id", "session_id", "root_turn_id"))
                    signature = (identity, usage, turn_usage, thread_usage)
                    if isinstance(response, str) and response and response in compactions:
                        prior_signature, owner = compactions[response]
                        if signature != prior_signature:
                            for affected in (owner, active):
                                if affected is not None:
                                    affected["gaps"].append("Conflicting duplicate compaction response")
                                    affected["_compaction_unknown"] = True
                        continue
                    if active is None:
                        diagnostics.append("Compaction outside an active turn could not be attributed.")
                        continue
                    active["compaction_count"] += 1
                    if isinstance(response, str) and response:
                        compactions[response] = (signature, active)
                    valid = (isinstance(response, str) and bool(response) and usage is not None
                             and turn_usage is not None and thread_usage is not None
                             and bool(thread) and bool(active["turn_id"])
                             and identity[:2] == (thread, active["turn_id"])
                             and identity[2] in (None, thread)
                             and identity[3] in (None, active["turn_id"])
                             and _counts(_plus(thread_usage, turn_usage, -1)) is not None)
                    if not valid:
                        active["gaps"].append("Missing or ambiguous compaction usage identity")
                        active["_compaction_unknown"] = True
                        continue
                    active["compaction_usage"] = _plus(active["compaction_usage"], usage)
                    active["max_input"] = max(active["max_input"] or 0, usage["input_tokens"])
                    if model and model not in active["models"]:
                        active["models"].append(model)
                    if effort and effort not in active["efforts"]:
                        active["efforts"].append(effort)
                    if active["_pending_compaction"] or previous is None or active["gaps"]:
                        active["gaps"].append("Compaction counter attribution is ambiguous")
                    elif turn_usage == _plus(active["usage"], usage):
                        # Provisionally include a separately recorded response.
                        # The next native counter must prove whether it omitted
                        # this response or already incorporated it.
                        active["usage"] = _plus(active["usage"], usage)
                        unreflected = _plus(unreflected, usage)
                        unreflected_responses.append(usage)
                        active["_pending_compaction"] = (usage, False)
                    elif turn_usage == active["usage"] and previous_last == usage:
                        active["_pending_compaction"] = (usage, True)
                    else:
                        active["gaps"].append("Compaction turn total does not reconcile")
                elif kind == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info") or {}
                    current = _counts(info.get("total_token_usage"))
                    last = _counts(info.get("last_token_usage"))
                    if active:
                        pending = active["_pending_compaction"]
                        adjustment = zero
                        delta = _plus(current, previous, -1) if current is not None and previous is not None else None
                        if pending:
                            compact_usage, included = pending
                            if delta == zero and last == zero:
                                pass
                            elif not included and delta == compact_usage and last in (zero, compact_usage):
                                adjustment = compact_usage
                                unreflected = _plus(unreflected, compact_usage, -1)
                                unreflected_responses.remove(compact_usage)
                            else:
                                active["gaps"].append("Neighboring compaction counters are ambiguous")
                            active["_pending_compaction"] = None
                        elif any(unreflected.values()) and delta is not None and delta != zero and _counts(delta) is not None:
                            # A later native counter can absorb earlier omitted
                            # compactor usage. Subtract only an exact complete
                            # vector match; otherwise do not guess attribution.
                            if last is not None and delta == _plus(last, unreflected):
                                adjustment = unreflected
                                unreflected = zero.copy()
                                unreflected_responses.clear()
                            elif delta == last and delta in unreflected_responses:
                                active["gaps"].append("Delayed compaction response or identical new usage is ambiguous")
                            elif delta != last:
                                active["gaps"].append("Possible delayed compaction inclusion is ambiguous")
                        if model and model not in active["models"]:
                            active["models"].append(model)
                        if effort and effort not in active["efforts"]:
                            active["efforts"].append(effort)
                        if current is None:
                            active["gaps"].append("Missing or invalid native counters")
                        elif current != previous:
                            delta = {k: current[k] - (previous[k] if previous else 0) for k in FIELDS}
                            # Resumed workers can begin a new counter epoch. Only
                            # accept a reset at the first observed update when the
                            # entire cumulative vector equals native last usage.
                            # Mid-turn resets or ambiguous deltas remain unknown.
                            if (previous is not None and len(rows) > 1
                                    and active["counter_updates"] == 0 and not active["gaps"]
                                    and _counts(delta) is None and last == current):
                                delta = current
                                unreflected = zero.copy()
                                unreflected_responses.clear()
                                active["counter_basis"] = "Resumed-turn counter reset; initial total equals native last usage"
                            # A native new thread starts with cumulative zero; a
                            # missing initial baseline in a later turn is unknown.
                            if previous is None and len(rows) > 1:
                                active["gaps"].append("Missing cumulative baseline")
                            elif _counts(delta) is None:
                                active["gaps"].append("Counter reset or invalid delta")
                            else:
                                delta = _plus(delta, adjustment, -1)
                                for k in FIELDS:
                                    active["usage"][k] += delta[k]
                                active["counter_updates"] += 1
                            if last:
                                if active["first_input"] is None:
                                    active["first_input"] = last["input_tokens"]
                                active["max_input"] = max(active["max_input"] or 0, last["input_tokens"])
                                if active["_final"]:
                                    active["handoff_tokens"] = last["output_tokens"] - last["reasoning_output_tokens"]
                                    active["_final"] = False
                    previous = current
                    previous_last = last
                elif kind == "event_msg" and p.get("type") == "task_complete" and active:
                    if p.get("turn_id") and active["turn_id"] and p["turn_id"] != active["turn_id"]:
                        diagnostics.append("Completion identity mismatch; event was not assigned to the active turn.")
                        continue
                    active["end"], active["state"] = when, "completed"
                    active["source_prefix_sha256"], active["source_bytes"] = digest.hexdigest(), offset
                    active = None
            except (ValueError, TypeError, KeyError) as exc:
                diagnostics.append("An invalid event was skipped.")
                if active:
                    active["gaps"].append("Unparseable event")
                previous = None
    result = []
    for row in rows:
        row.pop("_final", None)
        if row.pop("_pending_compaction", None):
            row["gaps"].append("Missing following compaction counter")
        if row.pop("_compaction_unknown", False):
            row["compaction_usage"] = dict.fromkeys(FIELDS)
        if row["compaction_count"]:
            if row["gaps"]:
                row["compaction_basis"] = "Native compaction metadata; combined counter attribution unavailable"
                row["counter_basis"] += "; compaction attribution unavailable"
            else:
                row["counter_basis"] += "; reconciled native compaction responses"
        if lower and (not row["start"] or _time(row["start"]) < lower):
            continue
        if upper and (not row["start"] or _time(row["start"]) >= upper):
            continue
        if row["gaps"] or not row["counter_updates"]:
            row["usage"] = dict.fromkeys(FIELDS)
        row["elapsed_seconds"] = None
        if row["start"] and row["end"]:
            row["elapsed_seconds"] = (datetime.fromisoformat(row["end"].replace("Z", "+00:00")) - datetime.fromisoformat(row["start"].replace("Z", "+00:00"))).total_seconds()
        row["coverage"] = "partial"
        row["handoff_basis"] = "Final generation output minus reasoning; primary serialization may differ"
        row["child_inclusion"] = "unknown"
        row.setdefault("source_prefix_sha256", digest.hexdigest())
        row.setdefault("source_bytes", offset)
        result.append(row)
    return result, diagnostics


def project(root):
    require_shared(root)
    root = Path(root)
    config_path = root / ".eutonos-local" / "companion-results.json"
    result = {"status": "not-configured", "runs": [], "experiments": [], "diagnostics": [],
              "pricing": PRICING, "credit_pricing": CREDIT_PRICING,
              "actual_charge_usd": None, "actual_codex_credits": None,
              "measured_savings_usd": None, "capture": {"status": "unavailable"}}
    from .assignments import records as assignment_records
    from .assignment_metrics import metadata as assignment_metadata
    from .tokenslang import ReadError
    try:
        result["assignment_records"] = assignment_metadata(assignment_records(root))
    except (OSError, ValueError, TypeError, KeyError, ReadError) as exc:
        result["assignment_records"] = None
        result["diagnostics"].append("Assignment metadata unavailable: " + str(exc))
    if not config_path.exists():
        from .companion_metrics import project as lifetime_metrics
        result["lifetime_metrics"] = lifetime_metrics([], [], [], PRICING, tracking(root),
                                                     assignment_records=result["assignment_records"])
        return result
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if cfg.get("profile") != CONFIG:
        raise ValueError("Invalid companion results configuration")
    result["status"] = "available"
    result["experiments"] = cfg.get("experiments", [])
    tracking_state = tracking(root)
    explicit_sources = cfg.get("sources", [])
    selected_sources = list(explicit_sources) if tracking_state["enabled"] else []
    discovery = {"status": "disabled", "source_root_configured": False, "discovered_source_count": 0,
                 "unsupported_subagent_count": 0, "ambiguous_thread_id_count": 0,
                 "excluded_retained_turn_count": 0,
                 "scope": "Native session discovery is opt-in through native_sessions_root."}
    excluded_native_threads = set()
    native_root = cfg.get("native_sessions_root")
    if not tracking_state["enabled"]:
        discovery["status"] = "tracking-off"
    elif native_root is not None:
        if not isinstance(native_root, str) or not native_root:
            discovery["status"] = "invalid-config"
            result["diagnostics"].append("Native session discovery configuration is invalid; no sessions were traversed.")
        else:
            try:
                found = native_sources(native_root, root)
                explicit_paths = {os.path.normcase(str(Path(source["path"]).resolve())) for source in explicit_sources
                                  if isinstance(source, dict) and isinstance(source.get("path"), str)}
                discovered = [source for source in found["sources"] if os.path.normcase(str(Path(source["path"]).resolve())) not in explicit_paths]
                selected_sources.extend({**source, "origin_basis": "native-project-session-origin"} for source in discovered)
                excluded_native_threads = set(found["excluded_thread_ids"])
                discovery = {"status": "enabled", "source_root_configured": True,
                             "discovered_source_count": len(discovered),
                             "unsupported_subagent_count": found["unsupported_subagent_count"],
                             "ambiguous_thread_id_count": found["ambiguous_thread_id_count"],
                             "excluded_retained_turn_count": 0,
                             "header_count": found["header_count"], "scope": found["scope"]}
                if found["ambiguous_thread_id_count"]:
                    result["diagnostics"].append(
                        "Ambiguous native thread identities were excluded from project session discovery.")
            except (OSError, ValueError, TypeError, KeyError):
                discovery = {"status": "unavailable", "source_root_configured": True, "discovered_source_count": 0,
                             "unsupported_subagent_count": 0, "ambiguous_thread_id_count": 0,
                             "excluded_retained_turn_count": 0,
                             "scope": "Configured native session headers were unavailable or invalid; no session log was selected."}
                result["diagnostics"].append("Native session headers are unavailable or invalid; usage was not replaced with zero.")
    result["native_session_discovery"] = discovery
    seen = set()
    duplicate_turns_excluded = 0
    for source in selected_sources:
        try:
            rows, diagnostics = cached_read(read_turns, source["path"], source["role"], source["experiment_id"], source.get("after"), source.get("before"))
            result["diagnostics"].extend(diagnostics)
            for row in rows:
                if row["id"] in seen:
                    duplicate_turns_excluded += 1
                    result["diagnostics"].append("Duplicate native turn excluded; each turn belongs to one selected experiment.")
                    continue
                seen.add(row["id"])
                annotation = cfg.get("annotations", {}).get(row["id"], {})
                row["stage"] = annotation.get("stage", "Unclassified")
                row["finding"] = annotation.get("finding", "No assessment recorded")
                row["followup_reads"] = annotation.get("followup_reads", [])
                row["quality"] = annotation.get("quality", "unreviewed")
                basis = source.get("origin_basis", "explicit-source-registration")
                row["project_origin"] = {"scope": "current-project", "basis": basis,
                                         "note": "Selected through explicit project registration; target working directory is not used." if basis == "explicit-source-registration"
                                                 else "Initial native session cwd matched this project, or this session is its verified spawned descendant."}
                if isinstance(annotation.get("request_id"), str) and annotation["request_id"]:
                    row["request_id"] = annotation["request_id"]
                result["runs"].append(row)
        except (OSError, ValueError, TypeError, KeyError):
            result["diagnostics"].append("A selected native log is unavailable or invalid; usage was not replaced with zero.")
    history = sync(root, result["runs"])
    retained = history["rows"]
    excluded_retained = [row for row in retained
                         if row.get("thread_id") in excluded_native_threads
                         and row.get("project_origin", {}).get("basis") == "native-project-session-origin"]
    discovery["excluded_retained_turn_count"] = len(excluded_retained)
    if excluded_retained:
        result["diagnostics"].append(
            "Retained auto-discovered turns with ambiguous native identities were withheld from this projection.")
    result["runs"] = sorted((row for row in retained if row not in excluded_retained),
                            key=lambda r: (r.get("start") or "", r["id"]))
    result["tracking"] = history["tracking"]
    for row in result["runs"]:
        if "project_origin" not in row:
            row["project_origin"] = {"scope": "current-project", "basis": "legacy-retained-registration",
                                     "note": "Accepted retained record from this project's historical registration; native project origin was not recorded."}
        row["api_estimate_usd"] = observed_cost(row)
        row["standard_credit_estimate"] = standard_credit_estimate(row)
    result["experiments"] = enrich(result["experiments"], result["runs"], cfg.get("reviews", []))
    result["comparisons"] = comparisons(result["experiments"], result["runs"])
    try:
        result["benchmark_suites"] = project_suites(root, result["runs"])
    except (OSError, ValueError, TypeError, KeyError):
        result["benchmark_suites"] = []
        result["diagnostics"].append("Benchmark suite metadata is unavailable or invalid.")
    capture = root / ".eutonos-local" / "capture"
    if capture.is_dir():
        receipts = []
        for path in sorted(capture.glob("*.json")):
            try:
                receipts.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                result["diagnostics"].append("A capture receipt is unreadable.")
        result["capture"] = {"status": "observed", "receipts": receipts}
    # This remains a local projection: only explicitly registered logs are read,
    # while history retains already-observed rows when a source later disappears.
    from .companion_metrics import project as lifetime_metrics
    result["lifetime_metrics"] = lifetime_metrics(
        result["runs"], result["experiments"], result["comparisons"], result["pricing"], result["tracking"],
        source_count=len(explicit_sources), discovered_source_count=discovery["discovered_source_count"], duplicate_turns_excluded=duplicate_turns_excluded,
        benchmark_suites=result["benchmark_suites"], capture=result["capture"],
        assignment_records=result["assignment_records"])
    result["diagnostics"].extend(result["lifetime_metrics"]["diagnostics"])
    return result
