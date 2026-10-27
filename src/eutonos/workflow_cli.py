"""JSON and expert-routing CLI for common EUTONOS tasks."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sqlite3
import sys

from .workflow import PROFILE, pickup
from .closeout_route import prepare as prepare_closeout, run as execute_closeout
from .command_registry import (EXPERT_ROUTES, GUIDED_COMMANDS, catalog, capabilities,
                               describe, explain, help_topics)
from .workflow_diagnostics import diagnose, read_events, record
from . import settings, tokenslang as ts
from .private_scope import require_shared
from . import orientation


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def _emit(value):
    # JSON escapes preserve Unicode values on legacy Windows console encodings.
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def _closeout_attention(result):
    """Inspect current phase results, never historical inputs or frozen plans."""
    failed = {"error", "failed", "partial", "incomplete", "blocked", "conflict", "unavailable", "denied",
              "stale", "historical-prior-completion", "attention-required"}
    phases = [("closeout", result)]
    phases.extend((name, result[name]) for name in
                  ("organization", "memory", "result", "capture_receipt", "pending_clear", "diagnostic_event")
                  if isinstance(result.get(name), dict))
    issues = []
    for phase, value in phases:
        if value.get("outcome") not in failed:
            continue
        issue = {"phase": value.get("phase", phase), "outcome": value["outcome"]}
        for field in ("code", "message"):
            if field in value:
                issue[field] = value[field]
        issue["next_action"] = (value.get("next_supported_action") or value.get("next") or
                                value.get("retry") or result.get("next_supported_action") or
                                result.get("next") or
                                "Inspect this phase's returned details and resolve its cause within existing authority.")
        issues.append(issue)
    delivery = result.get("git_delivery")
    if not isinstance(delivery, dict) and isinstance(result.get("memory"), dict):
        delivery = result["memory"].get("git_delivery")
    if isinstance(delivery, dict) and delivery.get("state") in {
            "unavailable", "blocked", "failed", "commit-uncertain", "push-uncertain", "paused"}:
        issues.append({"phase": "git-delivery", "outcome": delivery["state"],
                       "message": delivery.get("error", "Selected code delivery needs attention."),
                       "next_action": delivery.get("next", "Inspect the original delivery request and recover that phase; do not replay native closeout.")})
    previous = result.get("previous_operation")
    if isinstance(previous, dict) and previous.get("state") in {"requires-readback", "frozen-or-interrupted-unknown"}:
        issues.append({"phase": "previous_operation", "outcome": previous["state"],
                       "message": "An earlier closeout effect still requires recovery evidence; the current capture setting does not resolve it.",
                       "next_action": (result.get("next_supported_action") or
                                       "Inspect the existing operation and complete its supported readback or recovery path.")})
    return {
        "required": bool(issues), "issues": issues,
        "before_final": (
            "Handle each unresolved closeout error before ending the turn: recover within existing authority and verify readback, "
            "or explicitly tell the user what failed, its impact, and the remaining recovery step. "
            "Optional phase failures also require disclosure. Preserve successful phase results; do not claim overall success, "
            "blindly retry unchanged failures, or reset protection to hide an error."
            if issues else "No failed closeout phase was reported. Describe only the outcomes actually verified."),
        "qualification": "CLI guidance and exit status do not prove that the agent recovered or informed the user.",
    }


def _delivery_cadence(root="."):
    from .git_delivery_native import guidance
    mode = guidance(root)
    return {
        **mode,
        "question": "Is this a coherent checked milestone that should be committed under existing authority?",
        "next": mode["next"],
        "qualification": "This reminder does not grant commit, integration, push, merge, deployment, or release authority.",
    }


def _compact_queue(queue):
    requests = queue.get("requests", []) if isinstance(queue, dict) else []
    return {
        "signal": queue.get("signal", "green"),
        "label": queue.get("label", "Ready"),
        "open": [{key: item.get(key) for key in ("id", "kind", "state", "updated_at", "error") if item.get(key) is not None}
                 for item in requests if item.get("state") in {"pending", "waiting", "working", "waiting-integration", "building"}],
        "recent_terminal": [{key: item.get(key) for key in ("id", "kind", "state", "updated_at", "error") if item.get(key) is not None}
                            for item in requests if item.get("state") in {"complete", "superseded", "deferred", "failed"}][:20],
    }


def _completion_inbox(host, root):
    """Project-scoped receipts for required writes."""
    from . import write_retry
    writes = _compact_queue(write_retry.status(host, root))
    attention = writes["signal"] == "red"
    waiting = writes["signal"] == "yellow"
    return {
        "profile": "eutonos.closeout-completion-inbox.v1",
        "outcome": "attention-required" if attention else "waiting" if waiting else "clear",
        "required_writes": writes,
        "message": ("A queued operation needs attention; inspect its receipt before claiming that phase landed."
                    if attention else "Durable work remains queued; no manual rerun is needed."
                    if waiting else "Queues are clear; recent terminal receipts are retained below."),
    }


def _closeout_receipt(result):
    attention = result["closeout_attention"]
    if attention["required"]:
        return {"profile": "eutonos.closeout-receipt.v1", "state": "attention-required",
                "may_finish_turn": False,
                "message": "Closeout needs recovery or an explicit blocker report before the turn ends.",
                "safe_claim": "Report the successful phases separately and identify each unresolved phase."}
    pending_ids = []
    completed_ids = []
    memory = result.get("memory") if isinstance(result.get("memory"), dict) else {}
    for value in (result.get("write_retry"), result.get("organization", {}).get("queue"),
                  memory.get("write_retry")):
        if isinstance(value, dict) and value.get("request_id"):
            if value.get("outcome") in {"queued", "requeued", "already-queued"}:
                pending_ids.append(value["request_id"])
            elif value.get("outcome") == "complete":
                completed_ids.append(value["request_id"])
    for value in result.get("lifecycle", []):
        if isinstance(value, dict) and value.get("request_id"):
            if value.get("outcome") in {"queued", "requeued", "already-queued"}:
                pending_ids.append(value["request_id"])
            elif value.get("outcome") == "complete":
                completed_ids.append(value["request_id"])
    if pending_ids:
        ram_queued_after_filing = (isinstance(memory.get("write_retry"), dict)
                                   and memory["write_retry"].get("outcome") in {"queued", "requeued", "already-queued"}
                                   and result.get("organization", {}).get("outcome") == "completed")
        return {"profile": "eutonos.closeout-receipt.v1", "state": "submitted",
                "may_finish_turn": True, "request_ids": list(dict.fromkeys([*completed_ids, *pending_ids])),
                "message": ("Native filing completed; optional RAM capture is durably queued. The agent may report both phase states without replaying filing."
                            if ram_queued_after_filing else
                            "Required closeout entries were durably submitted. The agent may report this and end the turn; no waiting or manual rerun is required while the queue stays yellow."),
                "safe_claim": ("Native work filing landed; RAM capture is queued and has not yet been verified as landed."
                               if ram_queued_after_filing else
                               "The listed closeout entries were durably submitted; queued writes have not all been verified as landed yet."),
                "follow_up": "Use workflow receipts --host HOST --root ROOT or the next pickup with --host HOST to confirm terminal readback."}
    if result.get("outcome") == "deferred":
        if isinstance(result.get("pending"), dict):
            return {"profile": "eutonos.closeout-receipt.v1", "state": "pending-retained",
                    "may_finish_turn": True,
                    "message": "The semantic closeout delta was durably retained as local pending continuity; no RAM article or queued host write is claimed.",
                    "safe_claim": "Closeout continuity was retained locally for a later eligible capture; it was not submitted as a RAM write."}
        return {"profile": "eutonos.closeout-receipt.v1", "state": "deferred-not-persisted",
                "may_finish_turn": False,
                "message": "Capture policy deferred this prepared delta, but it has not yet been persisted as pending continuity or submitted as a write.",
                "safe_claim": "The closeout is prepared and deferred; run closeout before claiming durable retention or submission."}
    if result.get("outcome") in {"completed", "already-completed"}:
        return {"profile": "eutonos.closeout-receipt.v1", "state": "landed",
                "may_finish_turn": True,
                **({"request_ids": list(dict.fromkeys(completed_ids))} if completed_ids else {}),
                "message": "Closeout completed without a required queued write.",
                "safe_claim": "Closeout records landed for the phases reported as completed."}
    if result.get("outcome") == "skipped":
        return {"profile": "eutonos.closeout-receipt.v1", "state": "skipped",
                "may_finish_turn": True,
                "message": "Closeout was explicitly skipped without starting a new required write.",
                "safe_claim": "The closeout phase was skipped; no landed write is claimed for that phase."}
    return {"profile": "eutonos.closeout-receipt.v1", "state": "ready-to-run",
            "may_finish_turn": False, "message": "This is a prepared closeout packet; run it before reporting closeout."}


def main(argv=None, *, default_root="."):
    parser = argparse.ArgumentParser(description="Task-oriented EUTONOS workflow catalog and closeout form.")
    parser.add_argument("--root", default=default_root,
                        help="Selected checkout; explicit relative paths use the caller's working directory.")
    sub = parser.add_subparsers(dest="action")
    listing = sub.add_parser("catalog", aliases=["index"], help="Search the shared task index.")
    listing.add_argument("query", nargs="?")
    listing.add_argument("--root", default=argparse.SUPPRESS)
    help_parser = sub.add_parser("help", help="Read concise task help without opening a pager.")
    help_parser.add_argument("query", nargs="?")
    help_parser.add_argument("--root", default=argparse.SUPPRESS)
    for action in ("describe", "guide"):
        detail = sub.add_parser(action, help="Explain one task, its input and supported next steps; no execution.")
        detail.add_argument("command")
        detail.add_argument("--root", default=argparse.SUPPRESS)
    glossary = sub.add_parser("explain", help="Look up one EUTONOS term and its owner reference.")
    glossary.add_argument("term")
    glossary.add_argument("--root", default=argparse.SUPPRESS)
    capability_parser = sub.add_parser("capabilities", help="List packaged support, without implying configuration or permission.")
    capability_parser.add_argument("--root", default=argparse.SUPPRESS)
    menu = sub.add_parser("menu", help="Explicit TTY-only task chooser; it never executes a task.")
    menu.add_argument("--root", default=argparse.SUPPRESS)
    pickup_parser = sub.add_parser("pickup", help="Read compact selected-task context.")
    pickup_parser.add_argument("--root", default=argparse.SUPPRESS)
    pickup_parser.add_argument("--host", help="Existing external host; includes durable closeout receipts when supplied.")
    pickup_parser.add_argument("--goal", help="Optional selected goal ID: adds its bounded context. Without it pickup is unchanged.")
    pickup_parser.add_argument("--personal-root", help="With --goal: read a private goal/project from this explicit personal root instead of a shared goal.")
    pickup_parser.add_argument("--owner-id", help="Personal owner required with --personal-root.")
    pickup_parser.add_argument("--goal-detail", choices=("brief", "full"), default="brief")
    orient_parser = sub.add_parser("orient", help="Compose bounded cold-entry or source-pinned resume context.")
    orient_parser.add_argument("--root", default=argparse.SUPPRESS)
    orient_parser.add_argument("--request", help="Versioned orientation JSON file, or - for stdin; defaults to cold entry.")
    receipts = sub.add_parser("receipts", help="Read durable closeout receipts.")
    receipts.add_argument("--host", required=True)
    receipts.add_argument("--root", default=argparse.SUPPRESS)
    pending = sub.add_parser("pending", help="Read local deferred closeout notes for one worker context.")
    pending.add_argument("--context", required=True)
    pending.add_argument("--entry", help="Expand one exact pending entry ID.")
    pending.add_argument("--offset", type=int, default=0)
    pending.add_argument("--limit", type=int, default=10)
    pending.add_argument("--root", default=argparse.SUPPRESS)
    diagnostic = sub.add_parser("diagnose", help="Diagnose supplied workflow outcome metadata.")
    diagnostic.add_argument("--input", help="Optional workflow-results JSON file, or - for explicit stdin; defaults to local CLI events.")
    diagnostic.add_argument("--limit", type=int, default=64, help="Positive display window; history is not deleted.")
    diagnostic.add_argument("--root", default=argparse.SUPPRESS)
    for action in ("prepare", "run"):
        form = sub.add_parser(action)
        form.add_argument("command", choices=GUIDED_COMMANDS)
        form.add_argument("--input")
        form.add_argument("--context", help="JSON file with host, actor, work_id and session_id; use - for stdin.")
        form.add_argument("--native-log", help="Explicit native log for an active pre-final capture receipt.")
        form.add_argument("--host")
        form.add_argument("--actor")
        form.add_argument("--work-id")
        form.add_argument("--session-id")
        form.add_argument("--root", default=argparse.SUPPRESS)
    expert = sub.add_parser("expert")
    expert.add_argument("route", choices=sorted(EXPERT_ROUTES))
    expert.add_argument("argv", nargs=argparse.REMAINDER)
    expert.add_argument("--root", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.action in {None, "catalog", "index"}:
        _emit(catalog(getattr(args, "query", None))); return 0
    if args.action in {"help", "describe", "guide", "explain", "capabilities"}:
        if args.action == "help":
            result = help_topics(args.query)
        elif args.action == "explain":
            result = explain(args.term)
        elif args.action == "capabilities":
            result = capabilities()
        else:
            result = describe(args.command)
            if args.action == "guide" and result["outcome"] != "error":
                result = {**result, "outcome": "guide", "execution": "not-started"}
        _emit(result)
        return 2 if result["outcome"] == "error" else 0
    if args.action == "orient":
        try:
            orientation.guard(args.root)  # Privacy precedes request-file or stdin access.
            if args.request:
                if args.request == "-":
                    text = sys.stdin.read(orientation.MAX_REQUEST_BYTES + 1)
                else:
                    with Path(args.request).open("r", encoding="utf-8") as stream:
                        text = stream.read(orientation.MAX_REQUEST_BYTES + 1)
                ts.require(len(text.encode("utf-8")) <= orientation.MAX_REQUEST_BYTES,
                           "invalid", "Orientation request is too large.")
                request = ts.loads(text)
            else:
                request = {}
            result = orientation.orient(args.root, request)
        except (OSError, ValueError, UnicodeError, ts.ReadError) as exc:
            result = orientation.failure(getattr(exc, "code", "invalid"))
        _emit(result)
        return 0 if result["outcome"] == "orientation" else 2
    if args.action == "pickup":
        result = pickup(args.root)
        host = args.host
        if not host:
            # No --host: use the registered per-project host (eutonos-setup add-project). No entry: as before.
            from . import projects
            host = projects.resolve_host(args.root)
        if host and result.get("outcome") == "pickup":
            try:
                result["completion_inbox"] = _completion_inbox(host, args.root)
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, sqlite3.Error, ts.ReadError) as exc:
                result["completion_inbox"] = {"outcome": "attention-required", "message": str(exc)}
        goal_failed = False
        if args.goal or args.personal_root or args.owner_id:
            # Explicit selection only; public pickup above never reads private scope.
            from . import goal_context
            try:
                ts.require(bool(args.goal), "invalid", "Select the goal ID with --goal.")
                if args.personal_root:
                    result["personal_goal_context"] = goal_context.context(
                        args.personal_root, "personal", args.goal, args.owner_id, detail=args.goal_detail)
                else:
                    ts.require(not args.owner_id, "invalid", "--owner-id applies only with --personal-root.")
                    result["goal_context"] = goal_context.context(args.root, "shared", args.goal, detail=args.goal_detail)
            except (OSError, UnicodeError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
                goal_failed = True
                key = "personal_goal_context" if args.personal_root else "goal_context"
                result[key] = {"profile": goal_context.PROFILE, "outcome": "error",
                               "code": getattr(exc, "code", "invalid"),
                               "message": ("Personal operation could not be completed for the selected owner."
                                           if args.personal_root else getattr(exc, "message", str(exc)))}
        _emit(result)
        return 0 if result["outcome"] == "pickup" and not goal_failed else 2
    if args.action == "receipts":
        try:
            result = _completion_inbox(args.host, args.root)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, sqlite3.Error, ts.ReadError) as exc:
            result = {"profile": "eutonos.closeout-completion-inbox.v1", "outcome": "attention-required",
                      "message": str(exc)}
        _emit(result)
        return 2 if result["outcome"] == "attention-required" else 0
    if args.action == "pending":
        from . import capture_policy
        try:
            context = _load(args.context)
            if args.offset < 0 or args.limit <= 0:
                raise ValueError("Offset must be nonnegative and limit positive")
            entries = capture_policy.snapshot(args.root, context["actor"], context["work_id"], context["session_id"])
            if args.entry:
                selected = [entry for entry in entries if entry["id"] == args.entry]
                if not selected:
                    raise ValueError("Pending entry is absent or already captured")
            else:
                selected = [{"id": entry["id"], "preview": json.dumps(entry["content"], ensure_ascii=False)[:450],
                             "expand": "pending --context CONTEXT --entry " + entry["id"]}
                            for entry in entries[args.offset:args.offset + args.limit]]
            _emit({"profile": PROFILE, "outcome": "pending", "total": len(entries), "entries": selected,
                   "next_offset": args.offset + args.limit if not args.entry and args.offset + args.limit < len(entries) else None,
                   "qualification": "Local deferred notes, not verified RAM or model cache."})
            return 0
        except (OSError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
            _emit({"profile": PROFILE, "outcome": "error", "message": str(exc)})
            return 2
    if args.action == "diagnose":
        try:
            source = _load(args.input) if args.input else read_events(args.root, args.limit)
            result = diagnose(source, args.limit)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            result = {"profile": PROFILE, "outcome": "error", "code": "invalid", "message": str(exc)}
        _emit(result)
        return 0 if result["outcome"] == "diagnostic" else 2
    if args.action == "menu":
        if not sys.stdin.isatty():
            _emit({"profile": PROFILE, "outcome": "error", "code": "noninteractive",
                   "message": "The menu requires a TTY. Use catalog, prepare, run, or expert for noninteractive work."})
            return 2
        choices = catalog()["commands"]
        sys.stderr.write("\n".join(str(index) + ") " + item["label"] for index, item in enumerate(choices, 1)) + "\nSelect: ")
        sys.stderr.flush()
        selected = sys.stdin.readline().strip()
        if not selected.isdigit() or not 1 <= int(selected) <= len(choices):
            _emit({"profile": PROFILE, "outcome": "error", "code": "invalid", "message": "Select a listed number."})
            return 2
        chosen = choices[int(selected) - 1]
        _emit({"profile": PROFILE, "outcome": "selected", "command": chosen,
               "guidance": describe(chosen["id"]), "execution": "not-started"})
        return 0
    if args.action == "expert":
        forwarded = list(args.argv)
        # Protect request-file loaders before generic modules receive the root.
        # Explicit personal/device selection and packaged help/forms stay usable.
        metadata = not forwarded or forwarded[0] == "profile" or any(arg in {"--help", "-h"} for arg in forwarded)
        if args.route not in {"personal", "device", "goal-context"} and not metadata:
            selected_root = args.root
            for index, argument in enumerate(forwarded):
                if argument.startswith("--root="):
                    selected_root = argument.split("=", 1)[1]
                elif argument == "--root" and index + 1 < len(forwarded):
                    selected_root = forwarded[index + 1]
            try:
                require_shared(selected_root or ".")
            except ts.ReadError as exc:
                _emit({"profile": PROFILE, "outcome": "error", "code": exc.code, "message": exc.message})
                return 2
        module = importlib.import_module(EXPERT_ROUTES[args.route])
        if not any(arg == "--root" or arg.startswith("--root=") for arg in forwarded):
            forwarded.extend(["--root", args.root])
        if args.route == "eutonos" and "--revision" not in forwarded:
            forwarded.extend(["--revision", "HEAD"])
        return module.main(forwarded)
    context = {"root": args.root}
    try:
        require_shared(args.root or ".")
        if args.context == "-" and args.input == "-":
            raise ValueError("Input and context cannot both read stdin")
        context = _load(args.context) if args.context else {}
        if not isinstance(context, dict):
            raise ValueError("Context must be a JSON object")
        if context.get("root"):
            require_shared(context["root"])
        context.update({key: value for key, value in {"root": args.root, "host": args.host, "actor": args.actor,
                        "work_id": args.work_id, "session_id": args.session_id,
                        "native_log": args.native_log}.items() if value is not None})
        context.setdefault("root", ".")
        require_shared(context["root"])
        values = _load(args.input)
        result = prepare_closeout(values, context) if args.action == "prepare" else execute_closeout(values, context)
    except ts.ReadError as exc:
        result = {"profile": PROFILE, "outcome": "error", "code": exc.code, "message": exc.message}
        result["closeout_attention"] = _closeout_attention(result)
        result["closeout_receipt"] = _closeout_receipt(result)
        _emit(result)
        return 2
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result = {"profile": PROFILE, "outcome": "error", "code": "invalid", "message": str(exc)}
    if args.action == "run":
        try:
            if settings.status(context["root"])["tracking"]["enabled"]:
                recorded = record(context["root"], result)
                result["diagnostic_event"] = {"outcome": recorded["outcome"], "path": recorded["path"]}
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            result["diagnostic_event"] = {"outcome": "error", "message": str(exc)}
        try:
            from .metric_index import update as update_metric_index
            result["metric_index"] = update_metric_index(context["root"])
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            result["metric_index"] = {"outcome": "warning", "message": str(exc),
                                      "next": "Run metric-index update later; closeout work remains valid."}
    result["closeout_attention"] = _closeout_attention(result)
    result["closeout_receipt"] = _closeout_receipt(result)
    if args.action == "run" and context.get("closeout_debt") is not None:
        from . import closeout_debt
        try:
            result["closeout_debt"] = closeout_debt.publish(context["root"], context, result, values)
        except (OSError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
            result["closeout_debt"] = {"outcome": "unverified", "message": str(exc),
                                      "qualification": "Successful phases stand; this turn checkpoint was not verified."}
    result["delivery_cadence"] = _delivery_cadence(context.get("root", "."))
    _emit(result)
    return 0 if not result["closeout_attention"]["required"] and result["outcome"] in {
        "prepared", "completed", "already-completed", "skipped", "deferred"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
