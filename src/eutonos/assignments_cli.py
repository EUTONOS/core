"""Local delegate-and-collect assignment records; never dispatches a worker."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import assignments
from . import tokenslang as ts


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("profile", "create", "attach", "collect", "list", "get", "packet"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--request", help="JSON request file, or - for explicit stdin.")
    args = parser.parse_args(argv)
    try:
        value = _load(args.request)
        if args.command == "profile":
            result = assignments.profile_description()
        elif args.command == "create":
            result = assignments.create(args.root, value)
        elif args.command == "attach":
            result = assignments.attach(args.root, value)
        elif args.command == "collect":
            result = assignments.collect(args.root, value)
        elif args.command == "list":
            ts.shape(value, set(), {"offset", "limit"})
            result = assignments.list_records(args.root, value.get("offset", 0), value.get("limit", 20))
        else:
            ts.shape(value, {"assignment_id"})
            result = (assignments.get if args.command == "get" else assignments.packet)(
                args.root, value["assignment_id"])
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("outcome") not in {"incomplete", "conflict", "approval-required", "invalid-settings"} else 2
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ts.ReadError) as exc:
        print(json.dumps({"profile": assignments.PROFILE, "outcome": "error",
                          "code": getattr(exc, "code", "invalid"), "message": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
