"""Search and read durable EUTONOS skills, or record an explicit use receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import skills
from . import tokenslang as ts


def _load(path: str) -> dict:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("profile")
    listed = commands.add_parser("list")
    searched = commands.add_parser("search")
    searched.add_argument("query", nargs="?", default="")
    read = commands.add_parser("read")
    read.add_argument("skill_id")
    read.add_argument("--version")
    read.add_argument("--sha256")
    record = commands.add_parser("record-use")
    record.add_argument("--request", required=True, help="JSON request file, or - for explicit stdin.")
    for child in commands.choices.values():
        child.add_argument("--root", default=argparse.SUPPRESS)
    for child in (listed, searched):
        child.add_argument("--tool")
        child.add_argument("--status", choices=skills.STATUSES)
        child.add_argument("--limit", type=int, default=20)
        child.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            result = skills.profile_description()
        elif args.command in {"list", "search"}:
            query = "" if args.command == "list" else args.query
            result = skills.search(args.root, query, args.tool, args.status, args.limit, args.offset)
        elif args.command == "read":
            result = skills.get(args.root, args.skill_id, args.version, args.sha256)
        else:
            result = skills.record_use(args.root, _load(args.request))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        failed = result.get("status") in {"error", "not-configured", "not-found", "version-mismatch",
                                                 "hash-mismatch", "not-usable", "missing", "stale",
                                                 "excluded", "invalid", "unavailable"}
        return 2 if failed or result.get("outcome") in {"error", "conflict"} else 0
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        print(json.dumps({"profile": skills.PROFILE, "outcome": "error",
                          "code": getattr(exc, "code", "invalid"), "message": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
