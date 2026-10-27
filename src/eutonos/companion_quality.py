"""Explicit experiment pairings and attributable output assessments.

Scores are weighted reviewer judgments, not probabilities or automatic proof.
No model calls or source discovery happen in this module.
"""
import hashlib
import json
import math
import re
from datetime import datetime
from . import model_catalog

RUBRIC = {"id": "eutonos.companion-quality.v1", "dimensions": [
    {"id": "correctness", "label": "Correctness", "weight": 40},
    {"id": "fulfillment", "label": "Task fulfillment", "weight": 30},
    {"id": "evidence", "label": "Evidence and honesty", "weight": 20},
    {"id": "clarity", "label": "Clarity and usefulness", "weight": 10}],
    "anchors": "0: fails the criterion; 50: partly meets it with material gaps; 100: meets the task criterion with supporting evidence.",
    "meaning": "Weighted rubric score out of 100, not probability of correctness. Retrospective and contributor reviews are labeled."}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def experiment_fingerprint(e):
    keys = ("id", "task_id", "task_brief", "source_revision", "source_fingerprint", "conditions_id", "mode", "primary_model", "support_model", "baseline_id", "accounting_basis")
    return hashlib.sha256(json.dumps({k: e.get(k) for k in keys if k not in ("baseline_id", "accounting_basis") or k in e}, sort_keys=True).encode()).hexdigest()


def validate_experiment(e):
    required = {"id", "label", "task_id", "task_brief", "mode", "primary_model", "support_model",
                "source_revision", "source_fingerprint", "conditions_id"}
    allowed = required | {"kind", "quality", "output_summary", "baseline_id", "accounting_basis"}
    if not isinstance(e, dict) or not required <= e.keys() or set(e) - allowed:
        raise ValueError("Experiment requires task, source, conditions and explicit model pairing")
    if not all(_text(e[k]) for k in ("id", "label", "task_id", "task_brief", "source_revision", "conditions_id")):
        raise ValueError("Experiment identity and conditions must be nonempty")
    if not model_catalog.is_exact_model_id(e["primary_model"]) or e["mode"] not in {"on", "off"}:
        raise ValueError("Unsupported primary model or companion mode")
    if (e["mode"] == "off" and e["support_model"] is not None) or (e["mode"] == "on" and not model_catalog.is_exact_model_id(e["support_model"])):
        raise ValueError("Companion-off has no helper; companion-on requires a selected helper model")
    if e["source_fingerprint"] is not None and not re.fullmatch(r"[0-9a-f]{64}", e["source_fingerprint"]):
        raise ValueError("Source fingerprint must be SHA-256 or unknown/null")
    if e.get("accounting_basis") not in (None, "unknown", "separate-worker-logs-no-children"):
        raise ValueError("Unsupported accounting basis")
    if "baseline_id" in e and (not _text(e["baseline_id"]) or e["baseline_id"] == e["id"]):
        raise ValueError("Baseline must identify a different actual experiment")
    return e


def validate_review(value):
    required = {"experiment_id", "output_summary", "output_refs", "reviewer", "review_kind",
                "rubric_id", "evaluated_at", "dimensions", "limitations", "retrospective", "experiment_fingerprint"}
    if not isinstance(value, dict) or not required <= value.keys() or set(value) - required - {"review_id"}:
        raise ValueError("Incomplete or unsupported review fields")
    if not all(_text(value[k]) for k in ("experiment_id", "output_summary", "reviewer")):
        raise ValueError("Review identity, summary and reviewer are required")
    if value["review_kind"] not in {"contributor", "independent-agent", "human"}:
        raise ValueError("Review must declare who assessed the output")
    if type(value["retrospective"]) is not bool or value["rubric_id"] != RUBRIC["id"]:
        raise ValueError("Review must declare timing and use the selected rubric")
    if not isinstance(value["experiment_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["experiment_fingerprint"]):
        raise ValueError("Review must pin the experiment task and conditions")
    if datetime.fromisoformat(value["evaluated_at"].replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Review timestamp requires a time zone")
    dims = value["dimensions"]
    if not isinstance(dims, dict) or set(dims) != {d["id"] for d in RUBRIC["dimensions"]}:
        raise ValueError("Every rubric dimension must be scored; missing is not zero")
    for d in dims.values():
        if not isinstance(d, dict) or set(d) != {"score", "rationale", "evidence"}:
            raise ValueError("Each score requires rationale and source evidence")
        score = d["score"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("Quality scores must be finite numbers from 0 to 100")
        if not _text(d["rationale"]) or not isinstance(d["evidence"], list) or not d["evidence"] or not all(_text(x) for x in d["evidence"]):
            raise ValueError("Scores cannot be published without rationale and evidence")
    refs = value["output_refs"]
    if not isinstance(refs, list) or not refs:
        raise ValueError("Review requires observed output references")
    ids = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"run_id", "final_sha256"} or not _text(ref["run_id"]) or not re.fullmatch(r"[0-9a-f]{64}", ref["final_sha256"]):
            raise ValueError("Output references require native run identity and final-output hash")
        ids.append(ref["run_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate reviewed output")
    if not isinstance(value["limitations"], list) or not all(_text(x) for x in value["limitations"]):
        raise ValueError("Limitations must be an explicit list")
    body = {k: value[k] for k in required}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if "review_id" in value and value["review_id"] != "review-" + digest[:24]:
        raise ValueError("Review identity does not match its content")
    return {**body, "review_id": "review-" + digest[:24]}


def review_projection(review, runs, experiment):
    r = validate_review(review)
    selected = {row["id"]: row for row in runs if row["experiment_id"] == r["experiment_id"]}
    gaps = []
    if r["experiment_fingerprint"] != experiment_fingerprint(experiment):
        gaps.append("Experiment task, pairing or conditions changed after review")
    for ref in r["output_refs"]:
        row = selected.get(ref["run_id"])
        if not row or row["role"] != "primary" or row["state"] != "completed" or not row.get("final_sha256") or row["final_sha256"] != ref["final_sha256"]:
            gaps.append("Reviewed output is missing, changed or unfinished")
    primary_ids = {k for k, v in selected.items() if v["role"] == "primary"}
    reviewed_ids = {ref["run_id"] for ref in r["output_refs"]}
    full_primary = bool(primary_ids) and primary_ids <= reviewed_ids
    if not full_primary:
        gaps.append("Not all selected primary outputs have been reviewed")
    score = round(sum(r["dimensions"][d["id"]]["score"] * d["weight"] for d in RUBRIC["dimensions"]) / 100, 1)
    return {**r, "score": None if gaps else score, "status": "stale" if gaps else "reviewed",
            "gaps": gaps, "full_primary_coverage": full_primary}


def enrich(experiments, runs, reviews):
    result = []
    for e in experiments:
        item = {**e, "rubric": RUBRIC, "assessment": None, "review_history": [], "pairing_status": "not-configured",
                "observed_models": {}, "comparison_key": None, "comparison_limits": []}
        rows = [r for r in runs if r["experiment_id"] == e["id"]]
        for role in ("primary", "support"):
            item["observed_models"][role] = sorted({m for r in rows if r["role"] == role for m in r["models"]})
        if e.get("primary_model"):
            validate_experiment(e)
            expected = {"primary": [e["primary_model"]], "support": [e["support_model"]] if e["mode"] == "on" else []}
            item["pairing_status"] = "observed" if expected == item["observed_models"] else ("unobserved" if not rows else "mismatch-or-incomplete")
        for review in reviews:
            if review.get("experiment_id") == e["id"]:
                try:
                    item["review_history"].append(review_projection(review, runs, e))
                except (ValueError, KeyError, TypeError):
                    item["review_history"].append({"status": "invalid", "score": None, "gaps": ["Review failed validation"]})
        if item["review_history"]:
            item["assessment"] = item["review_history"][-1]
        assessment = item["assessment"] or {}
        if item["pairing_status"] != "observed": item["comparison_limits"].append("Model pairing not fully observed")
        if not e.get("source_fingerprint"): item["comparison_limits"].append("Exact source snapshot not pinned")
        if not e.get("task_id") or not e.get("conditions_id"): item["comparison_limits"].append("Task/conditions not registered")
        if assessment.get("score") is None or not assessment.get("full_primary_coverage"): item["comparison_limits"].append("Primary outputs not fully reviewed")
        if assessment.get("review_kind") == "contributor" or assessment.get("retrospective"): item["comparison_limits"].append("Review is retrospective or by a contributor")
        if any(r["state"] != "completed" for r in rows): item["comparison_limits"].append("Selected run still unfinished")
        if not item["comparison_limits"]:
            item["comparison_key"] = [e["task_id"], e["source_fingerprint"], e["conditions_id"], RUBRIC["id"]]
        result.append(item)
    return result
