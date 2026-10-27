"""Read shared outcome goals and submit guarded native goal forms."""
import argparse
import json
from pathlib import Path
import sys

from . import goal_records, tokenslang as ts
from .private_scope import require_shared


def _load(path):
    value = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text(encoding="utf-8-sig"))
    ts.require(isinstance(value, dict), "invalid", "Request must be an object.")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("--item-id")
    form = commands.add_parser("form", help="Return an executable frozen request without writing.")
    form.add_argument("--owner-path", required=True)
    form.add_argument("--item-id")
    form.add_argument("--request", help="Optional draft JSON file or - for stdin.")
    submit = commands.add_parser("submit")
    submit.add_argument("--request", required=True, help="Request JSON file or - for stdin.")
    submit.add_argument("--host", required=True)
    submit.add_argument("--actor", required=True)
    submit.add_argument("--session", required=True, help="Actual native caller session identity.")
    for child in commands.choices.values():
        child.add_argument("--root", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        require_shared(args.root)
        if args.command == "read":
            result = goal_records.read_shared(args.root, args.item_id)
        elif args.command == "form":
            result = goal_records.form_shared(args.root, args.owner_path, args.item_id,
                                              _load(args.request) if args.request else None)
        else:
            value = _load(args.request)
            result = goal_records.submit_shared(args.root, value.get("request", value),
                                                host=args.host, actor=args.actor, session=args.session)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2 if result.get("outcome") in {"error", "conflict", "partial", "denied"} else 0
    except (ts.ReadError, OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Private roots are rejected before reading request files or owners.
        print(json.dumps({"outcome": "error", "code": getattr(exc, "code", "invalid"),
                          "message": getattr(exc, "message", str(exc))}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
