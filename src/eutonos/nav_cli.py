"""Current-agent NAV CLI. Every mutating command requires an enrolled project host (--host) or a marked fixture host (--fixture)."""
import argparse
from pathlib import Path
import sys
import time

from . import tokenslang as ts
from .effects import Coordinator, FixtureAuthority, LocalRoot
from .nav import NavIndex, READ, decode
from .nav_runner import Session
from .source import GitView
from .host import connect


def main(argv=None):
    parser = argparse.ArgumentParser(description="EUTONOS TokenSlang NAV: committed reads and guarded construction through an enrolled project host (--host) or a marked disposable fixture host (--fixture). validate-report is a read-only, mechanical-only check of a report (same input as submit); it writes nothing, consumes no retry and needs no host.")
    parser.add_argument("command", choices=["read", "status", "maintenance", "host-init", "start", "next", "submit", "effects", "reconcile", "report-template", "validate-report", "history-plan", "history"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", default="eutonos.nav.json")
    parser.add_argument("--revision", default="HEAD", help="Committed read view only: `read` sees committed maps and source at this revision, never saved uncommitted work; status/maintenance/construction observe the local checkout. For saved uncommitted native work use `python -I -B -m eutonos.repository_state_cli --root ROOT --view checkout` (or `--view current`).")
    parser.add_argument("--request", help="UTF-8 JSON request/report file; stdin when omitted.")
    parser.add_argument("--fixture", action="store_true", help="Explicit disposable-fixture admission; marker is also required.")
    parser.add_argument("--host", help="Existing external project host; enroll/start/reserve using eutonos-host.")
    parser.add_argument("--coordinator", help="Existing SQLite owner outside the repository; only host-init creates it.")
    parser.add_argument("--actor")
    parser.add_argument("--attempt")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--run-id")
    parser.add_argument("--mode", choices=["build", "maintenance", "refinement", "correction"], default="build")
    parser.add_argument("--record-id", action="append", default=[], help="Existing stable ID to correct; repeat for up to eight.")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--effect-id", help="Interrupted effect to inspect/reconcile; never auto-restart it.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--cursor", help="JSON continuation returned by history")
    args = parser.parse_args(argv)
    try:
        def payload():
            if args.request:
                with Path(args.request).open("rb") as stream:
                    raw = stream.read(ts.MAX_SOURCE_BYTES + 1)
            else:
                raw = sys.stdin.buffer.read(ts.MAX_SOURCE_BYTES + 1)
            ts.require(len(raw) <= ts.MAX_SOURCE_BYTES, "overflow", "Request/report exceeds 128 KiB.")
            return ts.loads(decode(raw))
        if args.command == "read":
            result = NavIndex(GitView(args.root, args.revision), args.config).handle(payload())
        else:
            session = Session(LocalRoot(args.root), args.config)
            if args.command == "history-plan":
                result = session.history_plan(ts.string(args.run_id))
            elif args.command == "history":
                result = session.history(args.limit, ts.loads(args.cursor) if args.cursor else None)
            elif args.command == "validate-report":
                result = session.check_report(payload())
            elif args.command in {"status", "maintenance", "report-template"}:
                result = getattr(session, args.command.replace("-", "_"))()
            else:
                ts.require(args.attempt, "denied", "Mutation requires a host attempt.")
                ts.require(not (args.host and args.fixture), "denied", "Choose one host mode.")
                if args.host:
                    ts.require(args.command != "host-init" and not args.coordinator,
                               "denied", "Use eutonos-host for project enrollment; its owner selects the coordinator.")
                    coordinator, authority = connect(session.root.root, args.host, args.actor)
                    authority.check(session.paths)
                else:
                    ts.require(args.fixture and args.coordinator and args.actor,
                               "denied", "Mutation requires an enrolled project host or explicit fixture host.")
                    authority = FixtureAuthority(session.root, args.actor, session.paths, time.time() + 300)
                    authority.check(session.paths)
                    owner = Path(args.coordinator).absolute()
                    ts.require(not owner.is_relative_to(session.root.root), "denied", "Coordinator must be outside the fixture.")
                if args.command == "host-init":
                    coordinator = Coordinator.initialize(owner)
                    generation = coordinator.start(authority, args.attempt)
                    coordinator.reserve(authority, args.attempt, generation, session.paths)
                    result = {"outcome": "host-initialized", "attempt": args.attempt, "generation": generation}
                else:
                    if not args.host:
                        coordinator = Coordinator(owner)
                    host = (coordinator, authority, args.attempt, args.generation)
                    if args.command == "effects":
                        inspection = coordinator.inspect(args.attempt)
                        ts.require(inspection["root"] == coordinator._root(authority) and inspection["actor"] == authority.actor,
                                   "wrong-scope", "Attempt belongs to another host scope or actor.")
                        result = {"outcome": "inspected", "attempt": inspection}
                    elif args.command == "start":
                        result = session.start(host, ts.string(args.run_id), args.mode, args.restart, args.record_id)
                    elif args.command == "next":
                        result = session.next(host)
                    elif args.command == "submit":
                        result = session.submit(host, payload())
                    else:
                        result = coordinator.reconcile(authority, args.attempt, args.generation, ts.string(args.effect_id))
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 0 if result.get("outcome") not in {"overflow"} else 2
    except (ts.ReadError, OSError) as exc:
        result = {"profile": READ, "outcome": "error", "code": getattr(exc, "code", "unavailable"),
                  "message": getattr(exc, "message", "Local NAV I/O is unavailable.")}
        if isinstance(exc, OSError):
            result["detail"] = str(exc)
            result["os_error"] = {key: getattr(exc, key) for key in ("errno", "winerror")
                                  if getattr(exc, key, None) is not None}
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
