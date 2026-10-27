"""Opt-in Codex JSONL usage snapshots; no model calls or transcript export.

Pilot adapter for event_msg/token_count total_token_usage and turn_context.model.
The JSONL format is version-sensitive. Output is evidence, not a billing receipt.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
PROFILE = "eutonos.companion-usage-checkpoint.v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def counters(info):
    raw = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        return None
    result = {key: raw.get(key) for key in FIELDS}
    if any(value is not None and (type(value) is not int or value < 0) for value in result.values()):
        raise ValueError("Invalid native token counter")
    for sub, total in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if result[sub] is not None and result[total] is not None and result[sub] > result[total]:
            raise ValueError("Native token subset exceeds total")
    return result


def collect(log, role, stage, checkpoint=None):
    """Read an explicit log; hash its complete prefix to fence rotation/rewrites.

    A checkpoint is a local cursor, not a new RAM or measurement database. No
    source message, tool argument, response or reasoning text enters the output.
    """
    started = time.perf_counter_ns()
    log = Path(log).resolve()
    if checkpoint:
        if checkpoint.get("profile") != PROFILE or checkpoint["role"] != role:
            raise ValueError("Checkpoint profile/role mismatch")
        if Path(checkpoint["log"]).resolve() != log:
            raise ValueError("Checkpoint belongs to another log")
    offset = checkpoint["offset"] if checkpoint else 0
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid checkpoint offset")
    digest = hashlib.sha256()
    previous = checkpoint["last_counter"] if checkpoint else None
    identity = checkpoint["thread_id"] if checkpoint else None
    model = checkpoint["model"] if checkpoint else None
    models = set()
    totals = dict.fromkeys(FIELDS, 0)
    missing = set()
    gaps, updates, duplicates, calls = 0, 0, 0, 0
    pending_bytes = 0
    with log.open("rb") as stream:
        remaining = offset
        while remaining:
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("Source truncated; take a new baseline")
            digest.update(block)
            remaining -= len(block)
        if checkpoint and digest.hexdigest() != checkpoint["prefix_sha256"]:
            raise ValueError("Source prefix changed; take a new baseline")
        # Freeze this read's boundary even if an agent keeps appending.
        end = log.stat().st_size
        while stream.tell() < end:
            line = stream.readline(end - stream.tell())
            if not line.endswith(b"\n"):
                pending_bytes = len(line)
                break
            digest.update(line)
            offset += len(line)
            try:
                event = json.loads(line)
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = event.get("type")
                if kind == "session_meta":
                    found = payload.get("id")
                    if identity and found != identity:
                        raise ValueError("Session identity changed")
                    identity = found
                elif kind == "turn_context":
                    model = payload.get("model")
                elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
                    calls += 1
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    current = counters(payload.get("info"))
                    if current is None:
                        gaps += 1
                        previous = None
                        continue
                    if model:
                        models.add(model)
                    if current == previous:
                        duplicates += 1
                        continue
                    updates += 1
                    if previous is None:
                        gaps += 1
                    elif any(current[k] is not None and previous[k] is not None and current[k] < previous[k] for k in FIELDS):
                        gaps += 1
                    else:
                        for key in FIELDS:
                            if current[key] is None or previous[key] is None:
                                missing.add(key)
                            else:
                                totals[key] += current[key] - previous[key]
                    previous = current
            except (json.JSONDecodeError, UnicodeError, AttributeError, TypeError):
                gaps += 1
                previous = None
    if not identity:
        raise ValueError("No session_meta identity; unsupported source")
    captured = now()
    state = {"profile": PROFILE, "log": str(log), "thread_id": identity,
             "role": role, "stage": stage, "offset": offset,
             "prefix_sha256": digest.hexdigest(), "last_counter": previous,
             "model": model, "captured_at": captured}
    usage = {key: None if gaps or key in missing or updates == 0 else totals[key] for key in FIELDS}
    # A no-change interval with valid endpoints is an observed zero increment,
    # but it does not prove no unreported/in-flight model work.
    if checkpoint and not gaps and updates == 0 and previous is not None:
        usage = {key: 0 if previous[key] is not None else None for key in FIELDS}
    for sub, total in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if usage[sub] is not None and usage[total] is not None and usage[sub] > usage[total]:
            gaps += 1
            usage = dict.fromkeys(FIELDS)
    report = {"profile": "eutonos.companion-usage-observation.v1", "thread_id": identity,
              "role": role, "stage": stage, "runtime_models": sorted(models),
              "model_evidence": "Native turn_context configuration, not independent backend execution confirmation",
              "source_window": {"start_offset": checkpoint["offset"] if checkpoint else 0,
                                "end_offset": offset, "prefix_sha256": digest.hexdigest()},
              "window": {"start": checkpoint["captured_at"] if checkpoint else None, "end": captured},
              "provider_usage": usage, "last_native_counter": previous,
              "coverage": "partial", "counter_gaps": gaps, "counter_updates": updates,
              "duplicate_snapshots": duplicates, "tool_calls": calls,
              "pending_line_bytes": pending_bytes,
              "instrumentation_ns": time.perf_counter_ns() - started,
              "child_inclusion": "unknown", "actual_credits": None,
              "limitation": "Observed native counter increments only. Reporting lag, unreported work and child inclusion are unverified; do not sum parent and helper. No transcript text retained."}
    report["observation_id"] = "usage-" + hashlib.sha256(json.dumps(
        [identity, role, stage, report["source_window"]], sort_keys=True).encode()).hexdigest()[:24]
    if checkpoint:
        report["elapsed_seconds"] = (datetime.fromisoformat(captured) - datetime.fromisoformat(checkpoint["captured_at"])).total_seconds()
    return state, report


def measurement_batch(report, scope, work, commit, issuer, batch_id):
    """Candidate for the existing guarded measurement interface; no publication."""
    end = report["window"]["end"]
    return {"profile": "eutonos.tokenslang.measurement-batch.v1",
            "identity": {"issuer_id": issuer, "batch_id": batch_id}, "revision": 1,
            "method": "eutonos.metadata.v1",
            "selection": {"scope_id": scope, "work_id": work, "session_id": report["thread_id"], "attempt_id": batch_id, "source_commit": commit},
            "window": {"start": report["window"]["start"] or end, "end": end},
            "recorded_at": end,
            "coverage": {"mode": "metadata", "extent": "partial", "dropped": report["counter_gaps"],
                         "limitation": report["limitation"] + " Role=" + report["role"] + "; stage=" + report["stage"]},
            "events": [{"id": batch_id, "logical_id": report["observation_id"], "parent_id": None,
                        "operation": "proof.pickup", "level": "attempt", "outcome": "partial",
                        "elapsed_ns": None, "returned_bytes": None,
                        "provider_usage": {"provider": "codex", "model": report["runtime_models"][0] if len(report["runtime_models"]) == 1 else None,
                                           "basis": "codex-jsonl-total-counter-delta-v1; child inclusion unknown",
                                           **report["provider_usage"]}, "incident": None}],
            "visibility": "private", "state": "active", "instrumentation_ns": report["instrumentation_ns"]}


def write(path, value):
    # Refuse overwrite: baselines and evidence should not disappear accidentally.
    with Path(path).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="Save a private baseline for one explicit log")
    start.add_argument("--log", required=True)
    start.add_argument("--role", required=True)
    start.add_argument("--stage", default="work")
    start.add_argument("--output", required=True)
    finish = sub.add_parser("report", help="Read increments since a baseline; does not modify it")
    finish.add_argument("--checkpoint", required=True)
    finish.add_argument("--output", required=True)
    finish.add_argument("--next-checkpoint")
    export = sub.add_parser("batch", help="Create a private native measurement candidate")
    export.add_argument("--report", required=True)
    for name in ("scope", "work", "commit", "issuer", "batch-id", "output"):
        export.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            state, _ = collect(args.log, args.role, args.stage)
            write(args.output, state)
        elif args.command == "report":
            checkpoint = json.loads(Path(args.checkpoint).read_text(encoding="utf-8"))
            state, report = collect(checkpoint["log"], checkpoint["role"], checkpoint["stage"], checkpoint)
            if args.next_checkpoint and Path(args.next_checkpoint).exists():
                raise ValueError("Next checkpoint already exists")
            write(args.output, report)
            if args.next_checkpoint:
                write(args.next_checkpoint, state)
        else:
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
            if report.get("profile") != "eutonos.companion-usage-observation.v1":
                raise ValueError("Unsupported report")
            write(args.output, measurement_batch(report, args.scope, args.work, args.commit, args.issuer, args.batch_id))
        print(json.dumps({"outcome": "ok", "output": str(Path(args.output).resolve())}))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"outcome": "error", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
