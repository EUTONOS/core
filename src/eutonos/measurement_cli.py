"""Explicit metadata plans, guarded publication and selected batch reads."""
import argparse
from pathlib import Path
import sys
from . import tokenslang as ts, measurement as m
from .effects import LocalRoot
from .host import connect


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["read", "plan", "apply"])
    p.add_argument("--root", required=True)
    p.add_argument("--request")
    p.add_argument("--actor")
    p.add_argument("--host")
    p.add_argument("--attempt")
    p.add_argument("--generation", type=int, default=1)
    p.add_argument("--fingerprint")
    p.add_argument("--expected-revision", type=int)
    a = p.parse_args(argv)
    try:
        if a.request:
            with Path(a.request).open("rb") as stream: raw = stream.read(1048577)
        else:
            raw = sys.stdin.buffer.read(1048577)
        ts.require(len(raw) <= 1048576, "overflow", "Request exceeds 1 MiB.")
        value = ts.loads(raw.decode("utf-8"))
        store = m.Store(LocalRoot(a.root))
        if a.command == "read": result = store.read(value)
        elif a.command == "plan": result = store.plan(value, a.expected_revision, ts.string(a.actor))
        else:
            owner, authority = connect(a.root, ts.string(a.host), a.actor)
            result = m.apply(owner, authority, ts.string(a.attempt), a.generation, value, ts.string(a.fingerprint))
        sys.stdout.buffer.write(ts.encoded(result) + b"\n")
        return 0
    except (ts.ReadError, OSError, UnicodeError) as exc:
        sys.stdout.buffer.write(ts.encoded({"outcome": "error", "code": getattr(exc, "code", "unavailable"),
                                          "message": getattr(exc, "message", "Metadata I/O unavailable.")}) + b"\n")
        return 2


if __name__ == "__main__": raise SystemExit(main())
