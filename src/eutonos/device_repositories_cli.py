"""Explicit device repository enrollment; no disk discovery or implicit enrollment."""
import argparse
import json
from pathlib import Path
import sys

from . import device_repositories, tokenslang as ts
from .private_scope import require_owner, require_shared


def _load(path):
    value = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text(encoding="utf-8-sig"))
    ts.require(isinstance(value, dict), "invalid", "Request must be an object.")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Compatibility locator only; never enrolls a repository.")
    parser.add_argument("--home", help="Selected device-local EUTONOS home.")
    parser.add_argument("--owner-id", help="Selected personal owner; omitted reads redact personal selection.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read", aliases=["list"])
    form = commands.add_parser("form", help="Return an executable versioned request without writing.")
    form.add_argument("operation", choices=("enroll", "relocate", "withdraw"))
    form.add_argument("--request", help="Optional draft JSON file or - for stdin.")
    for operation in ("enroll", "relocate", "withdraw"):
        child = commands.add_parser(operation)
        child.add_argument("--request", required=True, help="Form request JSON file or - for stdin.")
        child.add_argument("--expected-revision", help="Observed device file hash; omitted means absent.")
    for child in set(commands.choices.values()):
        for name in ("root", "home", "owner-id"):
            child.add_argument("--" + name, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command in {"read", "list"}:
            result = device_repositories.read(home=args.home, owner_id=args.owner_id)
        elif args.command == "form":
            draft = _load(args.request) if args.request else None
            if draft and draft.get("kind", "shared") == "personal":
                ts.require(bool(args.owner_id), "denied", "Personal enrollment requires its selected owner.")
                if draft.get("owner_id") is not None:
                    ts.require(draft["owner_id"] == args.owner_id, "denied", "Personal enrollment requires its selected owner.")
                selected = draft.get("new_locator") if args.operation == "relocate" else draft.get("locator")
                if selected and args.operation == "withdraw":
                    current = device_repositories.read(home=args.home, owner_id=args.owner_id).get("personal")
                    ts.require(isinstance(current, dict) and current.get("owner_id") == args.owner_id and
                               Path(current.get("locator", "")).resolve() == Path(selected).resolve(),
                               "denied", "Personal selection is unavailable.")
                elif selected:
                    require_owner(selected, args.owner_id)
            elif draft:
                for key in ("locator", "new_locator", "associate_with"):
                    if draft.get(key):
                        require_shared(draft[key])
            result = device_repositories.form(args.operation, draft,
                                              home=args.home, owner_id=args.owner_id)
        else:
            value = _load(args.request)
            # Accept both the complete form and its executable request object.
            request = value.get("request", value)
            ts.require(isinstance(request, dict), "invalid", "Request must be an object.")
            expected = args.expected_revision if args.expected_revision is not None else value.get("expected_revision")
            ts.require(request.get("operation") == args.command, "invalid", "Request operation differs from command.")
            if request.get("kind", "shared") == "personal":
                ts.require(bool(args.owner_id) and request.get("owner_id") == args.owner_id,
                           "denied", "Personal enrollment requires its selected owner.")
            result = device_repositories.update(request, expected_revision=expected, home=args.home)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2 if result.get("outcome") in {"error", "conflict", "denied", "unavailable"} else 0
    except (ts.ReadError, OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"outcome": "error", "code": getattr(exc, "code", "invalid"),
                          "message": getattr(exc, "message", "Device repository operation could not be completed.")}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
