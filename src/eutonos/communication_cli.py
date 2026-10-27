"""Explicit message plans, guarded fixture publication and selective local reading."""
import argparse
from pathlib import Path
import sys
import time

from . import communication as messages, tokenslang as ts
from .effects import Coordinator, FixtureAuthority, LocalRoot
from .host import connect


def main(argv=None):
    parser = argparse.ArgumentParser(description="TokenSlang work-linked communication; no notification transport.")
    parser.add_argument("command", choices=["plan", "apply", "read"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", default="eutonos.communication.json")
    parser.add_argument("--request")
    parser.add_argument("--actor")
    parser.add_argument("--attempt")
    parser.add_argument("--coordinator")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--host", help="Explicit existing external project host directory.")
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--fingerprint")
    args = parser.parse_args(argv)
    try:
        if args.request:
            with Path(args.request).open("rb") as stream:
                raw = stream.read(1048577)
        else:
            raw = sys.stdin.buffer.read(1048577)
        ts.require(len(raw) <= 1048576, "overflow", "Communication request exceeds 1 MiB.")
        value = messages.decode(raw)
        local = LocalRoot(args.root)
        if args.command == "apply":
            ts.require(args.attempt and args.fingerprint, "denied", "Apply requires an attempt and selected fingerprint.")
            ts.require(not (args.host and args.fixture), "denied", "Choose one host mode.")
            if args.host:
                ts.require(not args.coordinator, "denied", "Project host owns the coordinator selection.")
                coordinator, authority = connect(local.root, args.host, args.actor)
            else:
                ts.require(args.fixture and args.coordinator and args.actor,
                           "denied", "Apply requires an explicit fixture or enrolled project host.")
                coordinator = Coordinator(args.coordinator)
                state = coordinator.inspect(args.attempt)
                authority = FixtureAuthority(local, args.actor, frozenset(r["path"] for r in state["reservations"]), time.time() + 300)
            result = messages.apply(coordinator, authority, args.attempt, args.generation, value, args.fingerprint)
        else:
            board = messages.Board(local, args.config)
            result = board.read(value) if args.command == "read" else board.plan(value, ts.string(args.actor), ts.string(args.attempt))
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 2 if result.get("outcome") == "overflow" else 0
    except (ts.ReadError, OSError) as exc:
        sys.stdout.buffer.write(ts.encoded({"profile": messages.READ, "outcome": "error", "code": getattr(exc, "code", "unavailable"),
                                          "message": getattr(exc, "message", "Local communication I/O unavailable.")}) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
