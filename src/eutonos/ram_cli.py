"""RAM CLI with friendly discovery plus exact guarded read/plan/apply operations."""
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from . import tokenslang as ts
from . import health
from .effects import Coordinator, FixtureAuthority, LocalRoot
from .host import connect
from .ram import Store, apply, unpack, READ
from .source import safe_path


DESCRIPTION_PROFILE = "eutonos.tokenslang.ram-cli.v1"


def _description():
    return {
        "profile": DESCRIPTION_PROFILE,
        "outcome": "description",
        "default": "Running eutonos-ram with no command describes these choices and performs no read or write.",
        "recent": "eutonos-ram recent --root PATH --limit 10",
        "browse": "eutonos-ram browse --root PATH [--work-id ID] [--query TEXT] [--sort recorded_at|occurred_at|work_id|memory_id|path] [--order asc|desc]",
        "show": "eutonos-ram show --root PATH --path RAM/...json [--revision N]",
        "report": "eutonos-ram report --root PATH [filters] [--output .eutonos-local/reports/name.md]",
        "search": "Use eutonos-ram-relevance find TERMS --root PATH for ranked lexical previews and exact follow-up reads.",
        "dashboard": "Use eutonos-dashboard serve --root PATH and open the Memory page for visual search, browse, and revision details.",
        "raw": "read accepts an exact eutonos.tokenslang.ram-read.v1 JSON request; plan/apply retain their guarded mutation protocol.",
        "writes": "Only report writes a disposable Markdown projection; plan/apply retain their separately guarded effects. Discovery, recent, browse, and show do not write or call a model.",
    }


def _cursor(value):
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4))
        cursor = json.loads(raw.decode("utf-8"))
        ts.shape(cursor, {"key", "offset"})
        return cursor
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ts.ReadError("invalid", "RAM cursor is invalid; start a fresh browse.") from exc


def _cursor_token(value):
    if value is None:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def _summary_request(args):
    value = {
        "profile": READ,
        "operation": "browse",
        "summary_only": True,
        "summary_chars": args.summary_chars,
        "limit": args.limit,
        "max_bytes": 32768,
        "include_archived": args.include_archived,
        "sort": args.sort,
        "order": args.order,
        "time_field": args.time_field,
    }
    for name in ("work_id", "session_id", "query"):
        selected = getattr(args, name)
        if selected:
            value[name] = selected
    if args.from_time or args.to_time:
        ts.require(args.from_time and args.to_time, "invalid", "Supply both --from and --to.")
        value.update({"from": args.from_time, "to": args.to_time})
    cursor = _cursor(args.cursor)
    if cursor:
        value["cursor"] = cursor
    return value


def _summaries(store, args):
    result = store.read(_summary_request(args))
    continuation = result.pop("continuation", None)
    result["next_cursor"] = _cursor_token(continuation)
    result["display"] = {
        "sort": args.sort,
        "order": args.order,
        "returned": len(result.get("items", [])),
    }
    return result


def _show(store, args):
    ts.require(args.path, "invalid", "Show requires --path from a browse result.")
    path = safe_path(args.path)
    ts.require(store.inside(path), "denied", "Memory path is outside the configured RAM store.")
    record = store.load(path, store.root.read)
    request = {
        "profile": READ,
        "operation": "get",
        "identity": record["identity"],
        "include_archived": args.include_archived,
        "max_bytes": 32768,
    }
    if args.revision is not None:
        request["revision"] = args.revision
    return store.read(request)


def _markdown(result):
    lines = [
        "# Disposable RAM report",
        "",
        "Generated: " + datetime.now(timezone.utc).isoformat(),
        "Repository: " + result.get("repository_id", "unknown"),
        "View: " + result.get("view", "unknown"),
        "",
        "This derived report is for browsing. Native RAM records and source owners remain authoritative.",
        "",
    ]
    items = result.get("items", [])
    for item in items:
        identity = item["identity"]
        lines.extend([
            f"## {identity['memory_id']}",
            "",
            f"- Work: `{identity['work_id']}`",
            f"- Session: `{identity['session_id']}`",
            f"- Kind: `{item['kind']}`",
            f"- Revision: `{item['record_revision']}`",
            f"- Recorded: `{item.get('recorded_at') or 'not recorded'}`",
            f"- Occurred: `{item.get('occurred_at') or 'not recorded'}`",
            f"- State: `{item['state']}`; current task changed: `{str(item['current_task_changed']).lower()}`",
            f"- Native record: `{item['path']}`",
            "",
            item["summary"].strip(),
            "",
        ])
    if not items:
        lines.extend(["No matching visible memories were found.", ""])
    if result.get("next_cursor"):
        lines.extend([
            "More results are available. Re-run the same filters with this cursor:",
            "",
            "`" + result["next_cursor"] + "`",
            "",
        ])
    return "\n".join(lines)


def _report(root, store, args):
    result = _summaries(store, args)
    relative = args.output or (
        ".eutonos-local/reports/ram-"
        + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        + ".md"
    )
    relative = safe_path(relative)
    ts.require(relative.lower().endswith(".md"), "invalid", "RAM reports use a .md output path.")
    parts = Path(relative).parts
    ts.require(len(parts) == 3 and parts[:2] == (".eutonos-local", "reports"),
               "denied", "Disposable reports stay directly in .eutonos-local/reports.")
    reports = root.root / ".eutonos-local" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    output = root.path(relative)
    ts.require(args.replace or not output.exists(), "conflict", "Report already exists; choose another path or --replace.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_markdown(result), encoding="utf-8", newline="\n")
    return {
        "profile": DESCRIPTION_PROFILE,
        "outcome": "reported",
        "output": relative,
        "records": len(result.get("items", [])),
        "complete": result.get("complete"),
        "next_cursor": result.get("next_cursor"),
        "qualification": "Disposable Markdown projection; native RAM and source owners were not changed.",
    }


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=["describe", "recent", "browse", "show", "report", "read", "plan", "apply"],
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="eutonos.ram.json")
    parser.add_argument("--request", help="JSON request/plan file; stdin if omitted for raw read, plan, or apply.")
    parser.add_argument("--actor")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--host", help="Explicit existing external project host directory.")
    parser.add_argument("--coordinator")
    parser.add_argument("--attempt")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--fingerprint", help="Host-selected plan fingerprint; separate from its payload.")
    parser.add_argument("--work-id")
    parser.add_argument("--session-id")
    parser.add_argument("--query")
    parser.add_argument("--time-field", choices=["recorded_at", "occurred_at"], default="recorded_at")
    parser.add_argument("--from", dest="from_time")
    parser.add_argument("--to", dest="to_time")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument(
        "--sort",
        choices=["recorded_at", "occurred_at", "work_id", "memory_id", "path"],
        default="recorded_at",
    )
    parser.add_argument("--order", choices=["asc", "desc"], default="desc")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--summary-chars", type=int, default=600)
    parser.add_argument("--cursor")
    parser.add_argument("--path")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--output")
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        command = args.command or "describe"
        if command == "describe":
            result = _description()
        else:
            local = LocalRoot(args.root)
            if command in {"recent", "browse", "show", "report"}:
                store = Store(local, args.config)
                if command == "show":
                    result = _show(store, args)
                elif command == "report":
                    result = _report(local, store, args)
                else:
                    if command == "recent":
                        args.sort, args.order = "recorded_at", "desc"
                    result = _summaries(store, args)
            else:
                if args.request:
                    with Path(args.request).open("rb") as stream:
                        raw = stream.read()
                else:
                    raw = sys.stdin.buffer.read()
                health.emit("ram-cli-request-growth", len(raw), 1048576,
                            "RAM request/plan crossed the 1 MiB glass ceiling; parsing continued.", unit="bytes")
                value = unpack(raw)
                if command == "apply":
                    ts.require(args.attempt and args.fingerprint, "denied", "Apply requires an attempt and selected fingerprint.")
                    ts.require(not (args.host and args.fixture), "denied", "Choose one host mode.")
                    if args.host:
                        ts.require(not args.coordinator, "denied", "Project host owns the coordinator selection.")
                        coordinator, authority = connect(local.root, args.host, args.actor)
                    else:
                        ts.require(
                            args.fixture and args.coordinator and args.actor,
                            "denied",
                            "Apply requires an explicit fixture or enrolled project host.",
                        )
                        coordinator = Coordinator(args.coordinator)
                        state = coordinator.inspect(args.attempt)
                        authority = FixtureAuthority(
                            local,
                            args.actor,
                            frozenset(r["path"] for r in state["reservations"]),
                            time.time() + 300,
                        )
                    result = apply(
                        coordinator,
                        authority,
                        args.attempt,
                        args.generation,
                        value,
                        args.fingerprint,
                    )
                else:
                    store = Store(local, args.config)
                    result = store.read(value) if command == "read" else store.plan(value, ts.string(args.actor))
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 2 if result.get("outcome") in {"overflow", "error", "incomplete", "conflict"} else 0
    except (ts.ReadError, OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        sys.stdout.buffer.write(ts.encoded({
            "profile": READ,
            "outcome": "error",
            "code": getattr(exc, "code", "invalid"),
            "message": getattr(exc, "message", str(exc)),
        }) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
