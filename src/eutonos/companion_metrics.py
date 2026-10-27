"""Recorded companion-usage aggregates; no log discovery or model calls."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math

from .companion_results import (CREDIT_PRICING, FIELDS, RATES, _counts, estimate,
                                observed_cost, standard_credit_estimate)
from .companion_timeline import project as timeline


def _complete_usage(row):
    return _counts(row.get("usage"))


def _priced_eligible(row):
    peak = row.get("max_input")
    return (row.get("state") == "completed" and _complete_usage(row) is not None
            and type(peak) is int and 0 <= peak <= 272000)


def _tokens(rows):
    valid = [u for row in rows if (u := _complete_usage(row)) is not None]
    return {**{key: sum(u[key] for u in valid) if len(valid) == len(rows) else None for key in FIELDS},
            **{"observed_" + key: sum(u[key] for u in valid) if valid else None for key in FIELDS},
            "measured_run_count": len(valid), "unknown_run_count": len(rows) - len(valid)}


def _cost(rows):
    values = [observed_cost(row) for row in rows]
    known = [value for value in values if value is not None]
    return {"observed_standard_api_estimate_usd": sum(known) if known else None,
            "priced_run_count": len(known), "unpriced_run_count": len(rows) - len(known)}


def _credits(rows):
    values = [standard_credit_estimate(row) for row in rows]
    known = [value for value in values if value is not None]
    return {"standard_rate_credit_estimate": sum(known) if known else None,
            "priced_run_count": len(known), "unpriced_run_count": len(rows) - len(known)}


def _orchestration(rows):
    names = {
        "spawn_agent": "launches", "followup_task": "instructions",
        "send_message": "instructions", "wait_agent": "waits",
        "list_agents": "status_checks", "interrupt_agent": "interrupts",
    }
    counts = dict.fromkeys(("launches", "instructions", "waits", "status_checks", "interrupts"), 0)
    known = 0
    for row in rows:
        tools = row.get("tool_counts")
        if not isinstance(tools, dict) or any(type(v) is not int or v < 0 for v in tools.values()):
            continue
        known += 1
        for name, count in tools.items():
            # Match known native coordination tools only. Opaque exec bodies
            # are not parsed, and similarly named application tools are not peers.
            short = name.removeprefix("collaboration.") if isinstance(name, str) else ""
            if short in names:
                counts[names[short]] += count
    return {"calls": counts, "observed_call_count": sum(counts.values()),
            "known_turn_count": known, "unknown_turn_count": len(rows) - known,
            "attributed_tokens": None,
            "basis": "Recognized native agent coordination calls; opaque wrappers and unregistered activity are not inferred.",
            "token_basis": "Per-turn usage does not isolate orchestration tokens from inherited context, instructions and useful work."}


def _summary(rows):
    completed = [row for row in rows if row.get("state") == "completed"]
    return {"run_count": len(rows), "completed_run_count": len(completed),
            "tokens": _tokens(completed), "credits": _credits(completed),
            "orchestration": _orchestration(completed), **_cost(completed)}


def _group(rows, key, labels):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    output = []
    for identity, items in sorted(groups.items(), key=lambda item: tuple("" if v is None else v for v in item[0])):
        output.append({**dict(zip(labels, identity)), **_summary(items)})
    return output


def _date(row):
    start = row.get("start")
    return start[:10] if isinstance(start, str) and len(start) >= 10 else "unknown"


def _effort(row):
    values = row.get("efforts")
    return values[0] if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str) else "unknown-or-multiple"


def _instant(value):
    if not isinstance(value, str) or not value:
        raise ValueError("A time range requires ISO-8601 timestamps")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A time range must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def window(result, project_index, *, amount=None, unit=None, from_value=None, to_value=None, now=None):
    """Project recorded whole turns by completion time; never prorate counters."""
    if type(project_index) is not int or project_index < 0:
        raise ValueError("project must be a nonnegative integer")
    relative = amount is not None or unit is not None
    custom = from_value is not None or to_value is not None
    if relative and custom:
        raise ValueError("Choose a relative or custom time range")
    lower = upper = None
    if relative:
        if amount is None or unit not in {"minutes", "hours", "days", "weeks"}:
            raise ValueError("Relative ranges require amount and unit")
        try:
            quantity = float(amount)
        except (TypeError, ValueError):
            raise ValueError("Range amount must be a positive number") from None
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Range amount must be a positive finite number")
        upper = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            lower = upper - timedelta(**{unit: quantity})
        except (OverflowError, ValueError):
            raise ValueError("Relative range is outside supported datetime bounds") from None
        shown = int(quantity) if quantity.is_integer() else quantity
        label = f"Last {shown:g} {unit}"
        kind = "relative"
    elif custom:
        if from_value is None or to_value is None:
            raise ValueError("Custom ranges require from and to")
        lower, upper = _instant(from_value), _instant(to_value)
        if lower >= upper:
            raise ValueError("Custom range must be ordered")
        label, kind = "Custom", "custom"
    else:
        label, kind = "All time", "all-time"

    runs = result.get("runs", [])
    completed = [row for row in runs if row.get("state") == "completed"]
    included, unknown_end, out_of_range = [], 0, 0
    for row in completed:
        try:
            ended = _instant(row.get("end"))
        except (ValueError, TypeError):
            unknown_end += 1
            continue
        if lower is not None and not lower <= ended < upper:
            out_of_range += 1
            continue
        included.append(row)
    selected = runs if lower is None else included
    comparisons = result.get("comparisons", [])
    if lower is not None:
        selected_comparisons = []
        for item in comparisons:
            try:
                ended = _instant(item.get("end"))
            except (ValueError, TypeError):
                continue
            if lower <= ended < upper:
                selected_comparisons.append(item)
        comparisons = selected_comparisons
    coverage = result.get("lifetime_metrics", {}).get("coverage", {})
    tracking_state = result.get("tracking", coverage.get("tracking", {"enabled": True, "windows": []}))
    metrics = result.get("lifetime_metrics") if lower is None and result.get("lifetime_metrics") is not None else project(
        selected, result.get("experiments", []), comparisons, result.get("pricing", {}),
        tracking_state,
        source_count=coverage.get("registered_source_count", 0),
        discovered_source_count=coverage.get("discovered_source_count", 0),
        duplicate_turns_excluded=coverage.get("duplicate_turns_excluded", 0),
        benchmark_suites=result.get("benchmark_suites", []), capture=result.get("capture"),
        assignment_records=result.get("assignment_records"))
    return {"project": project_index,
            "range": {"kind": kind, "label": label, "from": _stamp(lower) if lower else None,
                      "to": _stamp(upper) if upper else None,
                      "basis": "Completed turns ending in range; whole-turn usage",
                      "included_turn_count": len(completed) if lower is None else len(included),
                      "unknown_end_count": unknown_end,
                      "incomplete_excluded_count": len(runs) - len(completed),
                      "out_of_range_count": out_of_range},
             "metrics": metrics, "timeline": metrics["timeline"]}


def _counterfactual(rows, model):
    rate = next((item for item in RATES if item["model"] == model), None)
    eligible = [row for row in rows if _priced_eligible(row)]
    # Unlike observed cost, this deliberately reprices every complete recorded
    # vector. It has no claim about what a different model would have consumed.
    estimates = [estimate(row["usage"], rate) for row in eligible] if rate else []
    return {"scenario": f"Same recorded tokens repriced as all {model}", "model": model,
            "estimate_usd": sum(estimates) if len(estimates) == len(eligible) and estimates else None,
            "repriced_run_count": len(eligible), "excluded_run_count": len(rows) - len(eligible),
            "exclusion_rule": "Uses the same completed, valid-counter, 272K-or-less peak-input qualification as observed Standard API estimates.",
            "assumption": "Same observed token vectors; hypothetical pricing only, not measured usage or savings."}


def _helper_workgroups(runs):
    groups = {row.get("experiment_id") for row in runs if isinstance(row.get("experiment_id"), str) and row["experiment_id"]}
    with_helper = {row.get("experiment_id") for row in runs if row.get("role") == "support" and isinstance(row.get("experiment_id"), str) and row["experiment_id"]}
    return {"recorded_workgroup_count": len(groups), "workgroups_with_recorded_helper": len(with_helper),
            "recorded_workgroup_helper_incidence": len(with_helper) / len(groups) if groups else None,
            "basis": "Experiment IDs define recorded workgroups; this is not user-request frequency."}


def _benchmark_pair_map(benchmark_suites, runs, experiment_pairs):
    """Use only explicit projected stage IDs; ambiguity overrides a weaker pairing."""
    by_id = {row.get("id"): row for row in runs}
    result, groups, diagnostics = {}, {}, []
    for suite_index, suite in enumerate(benchmark_suites or []):
        for result_index, item in enumerate(suite.get("results", [])):
            pair = (item.get("primary_model"), item.get("support_model"))
            if not isinstance(pair[0], str) or (pair[1] is not None and not isinstance(pair[1], str)):
                continue
            ids = []
            for stage in (item.get("answer"), item.get("closeout")):
                if isinstance(stage, dict) and isinstance(stage.get("run_ids"), list):
                    ids.extend(stage["run_ids"])
            for run_id in ids:
                row = by_id.get(run_id)
                if row is None:
                    continue
                expected = pair[0] if row.get("role") == "primary" else pair[1] if row.get("role") == "support" else None
                observed = row.get("models")
                experiment_pair = experiment_pairs.get(row.get("experiment_id"))
                group_id = ("benchmark", suite_index, result_index)
                conflict = (expected is None or observed != [expected]
                            or (experiment_pair is not None and experiment_pair != pair)
                            or (run_id in result and (result[run_id] != pair or groups[run_id] != group_id)))
                if conflict:
                    result[run_id] = None
                    groups[run_id] = None
                    diagnostics.append("Benchmark pair association withheld for a conflicting declaration or observed model.")
                elif run_id not in result:
                    result[run_id] = pair
                    groups[run_id] = group_id
    return result, groups, list(dict.fromkeys(diagnostics))


def _relationships(runs, declared_pairs, benchmark_pairs, benchmark_groups):
    """Count only observed, role-validated helper activity by an explicit pair."""
    grouped = defaultdict(lambda: defaultdict(lambda: {"primary": 0, "helper": 0}))
    for row in runs:
        if row.get("state") != "completed":
            continue
        run_id = row.get("id")
        if run_id in benchmark_pairs:
            pair, group_id = benchmark_pairs[run_id], benchmark_groups.get(run_id)
        else:
            experiment_id = row.get("experiment_id")
            pair = declared_pairs.get(experiment_id)
            group_id = ("experiment", experiment_id) if pair and isinstance(experiment_id, str) and experiment_id else None
        if not (isinstance(pair, tuple) and isinstance(pair[0], str) and isinstance(pair[1], str)):
            continue
        role = row.get("role")
        expected = pair[0] if role == "primary" else pair[1] if role == "support" else None
        if expected is None or row.get("models") != [expected]:
            continue
        if group_id is None:
            continue
        item = grouped[pair][group_id]
        if role == "primary":
            item["primary"] += 1
        else:
            item["helper"] += 1
    result = []
    for pair, groups in sorted(grouped.items()):
        assisted = [item for item in groups.values() if item["helper"]]
        if not assisted:
            continue
        result.append({"primary_model": pair[0], "helper_model": pair[1],
                       "completed_primary_turn_count": sum(item["primary"] for item in assisted),
                       "completed_helper_turn_count": sum(item["helper"] for item in assisted),
                       "recorded_assisted_workgroup_count": len(assisted),
                       "workgroup_basis": "Distinct explicit benchmark result membership when available; otherwise experiment IDs. Only groups with observed completed helper turns are counted; this is not user-request frequency."})
    return result


def project(runs, experiments, comparisons, pricing, tracking, source_count=0, duplicate_turns_excluded=0, benchmark_suites=None, capture=None, discovered_source_count=0, assignment_records=None):
    """Return a bounded projection over retained, explicitly registered records."""
    from .assignment_metrics import project as assignments_project
    assignments = assignments_project(assignment_records, runs) if assignment_records is not None else {
        "status": "unavailable", "assignment_count": None,
        "basis": "Assignment metadata was not supplied or could not be validated."}
    pairs = {item.get("id"): (item.get("primary_model"), item.get("support_model")) for item in experiments}
    declared_pairs = {identity: pair for identity, pair in pairs.items() if isinstance(pair[0], str)}
    benchmark_pairs, benchmark_groups, diagnostics = _benchmark_pair_map(benchmark_suites, runs, declared_pairs)
    completed = [row for row in runs if row.get("state") == "completed"]
    dates = _group(runs, lambda row: (_date(row),), ("date",))
    matched = [item for item in comparisons if item.get("savings_usd") is not None]
    start = min((row.get("start") for row in runs if row.get("start")), default=None)
    end = max((row.get("end") for row in runs if row.get("end")), default=None)
    return {"profile": "eutonos.companion-lifetime-metrics.v1",
            "coverage": {"scope": "Project-owned chats, including external-repository tasks and benchmarks, from explicitly registered native logs plus retained history",
                         "status": "partial", "tracking": tracking, "window": {"start": start, "end": end},
                          "registered_source_count": source_count, "recorded_run_count": len(runs),
                          "discovered_source_count": discovered_source_count,
                         "completed_run_count": len(completed),
                         "usage_complete_run_count": sum(_complete_usage(row) is not None for row in completed),
                         "usage_unknown_run_count": sum(_complete_usage(row) is None for row in completed),
                         "duplicate_turns_excluded": duplicate_turns_excluded,
                         "note": "This is not complete lifetime usage: unregistered sessions and unknown counters are excluded, never treated as zero."},
            "tokens": _tokens(completed),
            "credits": _credits(completed),
            "orchestration": _orchestration(completed),
            "helper": {"run_count": sum(row.get("role") == "support" for row in runs),
                       "completed_run_count": sum(row.get("role") == "support" and row.get("state") == "completed" for row in runs),
                       "share_of_recorded_completed_agent_turns": (sum(row.get("role") == "support" and row.get("state") == "completed" for row in runs) / len(completed)) if completed else None,
                       "workgroups": _helper_workgroups(runs)},
            "models": _group(runs, lambda row: (row["models"][0] if isinstance(row.get("models"), list) and len(row["models"]) == 1 else "unknown-or-multiple",), ("model",)),
            "model_roles": _group(runs, lambda row: (row["models"][0] if isinstance(row.get("models"), list) and len(row["models"]) == 1 else "unknown-or-multiple", row.get("role") if row.get("role") in {"primary", "support", "reviewer"} else "unknown-role", _effort(row)), ("model", "role", "effort")),
            "efforts": _group(runs, lambda row: (_effort(row),), ("effort",)),
            "pairs": _group(runs, lambda row: benchmark_pairs[row["id"]] or (None, None) if row["id"] in benchmark_pairs else pairs.get(row.get("experiment_id"), (None, None)), ("primary_model", "helper_model")),
            "relationships": _relationships(runs, declared_pairs, benchmark_pairs, benchmark_groups),
            "assignments": assignments,
            "trends": dates,
            "observed": {**_cost(completed), "matched_comparisons": matched,
                         "matched_savings_usd": sum(item["savings_usd"] for item in matched) if matched else None,
                         "matched_savings_sample_count": len(matched)},
            "counterfactual": _counterfactual(runs, "gpt-6-astra"), "pricing": pricing,
             "credit_pricing": CREDIT_PRICING, "timeline": timeline(runs, capture),
             "diagnostics": diagnostics}
