"""Project journal binding and file coordination; no write grants are required."""
import argparse
from pathlib import Path
import sys

from . import tokenslang as ts
from .host import connect, enroll, renew, selected_apply, status


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit EUTONOS local host owner. Keep one owner per project.")
    parser.add_argument("command", choices=["enroll", "renew", "status", "start", "reserve", "inspect", "pause", "resume",
                                            "checkpoint", "apply", "reconcile", "rollback", "release", "closeout", "review-external"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--host", required=True, help="External host directory; never repository-supplied authority.")
    parser.add_argument("--actor")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--drop-path", action="append", default=[], help="Legacy compatibility option; no path grant is issued or revoked.")
    parser.add_argument("--shared", action="store_true", help="Legacy compatibility option; project writes no longer need grants.")
    parser.add_argument("--hours", type=float, default=8, help="Legacy admission metadata; does not expire project record keeping.")
    parser.add_argument("--attempt")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--cursor", type=int, help="Status only: continue a root-scoped attempt inventory before this row cursor.")
    parser.add_argument("--request")
    parser.add_argument("--effect-id")
    parser.add_argument("--fingerprint")
    args = parser.parse_args(argv)
    try:
        def payload():
            if args.request:
                with Path(args.request).open("rb") as stream:
                    raw = stream.read(1048577)
            else:
                raw = sys.stdin.buffer.read(1048577)
            ts.require(len(raw) <= 1048576, "overflow", "Host request exceeds 1 MiB.")
            return ts.loads(raw.decode("utf-8"))
        if args.command == "enroll":
            result = enroll(args.root, args.host, args.actor, args.path, hours=args.hours, attempt=args.attempt, shared=args.shared)
        elif args.command == "renew":
            result = renew(args.root, args.host, args.actor, args.path, args.hours, args.drop_path, attempt=args.attempt, shared=args.shared)
        elif args.command == "status":
            result = status(args.root, args.host, args.actor, args.attempt, args.cursor)
        else:
            coordinator, authority = connect(args.root, args.host, args.actor)
            attempt = ts.string(args.attempt)
            if args.command == "start":
                generation = coordinator.start(authority, attempt)
                result = {"outcome": "started", "generation": generation}
            else:
                state = coordinator.inspect(attempt)
                ts.require(state["root"] == coordinator._root(authority) and state["actor"] == authority.actor,
                           "wrong-scope", "Attempt belongs to another root or actor.")
                host = (authority, attempt, args.generation)
                if args.command == "inspect":
                    result = {"outcome": "inspected", "attempt": state}
                elif args.command in {"pause", "resume"}:
                    result = {"outcome": args.command, "generation": getattr(coordinator, args.command)(*host)}
                elif args.command == "reserve":
                    coordinator.reserve(*host, args.path)
                    result = {"outcome": "reserved", "paths": args.path}
                elif args.command == "checkpoint":
                    coordinator.checkpoint(*host, payload())
                    result = {"outcome": "checkpointed"}
                elif args.command == "apply":
                    result = selected_apply(coordinator, *host, ts.string(args.effect_id), payload(), ts.string(args.fingerprint))
                elif args.command == "closeout":
                    request = payload()
                    ts.shape(request, {"expected", "checkpoint"})
                    result = coordinator.closeout(*host, request["expected"], request["checkpoint"])
                elif args.command == "review-external":
                    request = payload()
                    ts.shape(request, {"expected", "reason"})
                    result = coordinator.review_external(*host, request["expected"], request["reason"])
                elif args.command == "release":
                    result = coordinator.release(*host, payload())
                    result = {"outcome": "released", "result": result}
                else:
                    result = getattr(coordinator, args.command)(*host, ts.string(args.effect_id))
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 0
    except (ts.ReadError, OSError, UnicodeError) as exc:
        sys.stdout.buffer.write(ts.encoded({"outcome": "error", "code": getattr(exc, "code", "unavailable"),
                                          "message": getattr(exc, "message", "Host I/O unavailable.")}) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
