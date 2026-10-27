"""Search, expand, or annotate eligible RAM without rewriting RAM records."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from . import ram_relevance as relevance
from . import tokenslang as ts


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("profile", "search", "query", "find", "get", "feedback", "feedback-read"))
    parser.add_argument("terms", nargs="?")
    parser.add_argument("--root", default=".")
    parser.add_argument("--request", help="JSON request file, or - for explicit stdin.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--explicit", action="store_true")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--view", choices=("local", "current", "main", "checkout", "source"),
                        help="Current Work source scope; defaults to current in native repositories.")
    parser.add_argument("--source", help="Exact source ID for source search or pinned get.")
    args = parser.parse_args(argv)
    try:
        value = _load(args.request)
        view = args.view or ("current" if (Path(args.root) / "eutonos.boards.json").is_file() else "local")
        if args.command == "profile":
            result = relevance.profile_description()
        elif args.command == "find":
            result = relevance.search(args.root, args.terms or "", args.limit, args.offset,
                                      args.explicit, args.include_archived, view=view, source=args.source)
        elif args.command in {"search", "query"}:
            ts.shape(value, {"profile", "query"}, {"limit", "offset", "explicit", "include_archived",
                                                    "view", "source"})
            ts.require(value["profile"] == relevance.SEARCH, "unsupported", "Unsupported RAM search profile.")
            result = relevance.search(args.root, value["query"], value.get("limit", 5), value.get("offset", 0),
                                      value.get("explicit", False), value.get("include_archived", False),
                                      view=value.get("view", view), source=value.get("source", args.source))
        elif args.command in {"get", "feedback-read"}:
            ts.shape(value, {"profile", "path", "revision", "sha256"},
                     {"explicit", "include_archived", "view", "source"} if args.command == "get" else {"actor"})
            ts.require(value["profile"] == relevance.GET, "unsupported", "Unsupported RAM get profile.")
            result = (relevance.get(args.root, value["path"], value["revision"], value["sha256"],
                                    value.get("explicit", False), value.get("include_archived", False),
                                    view=value.get("view", view), source=value.get("source", args.source))
                      if args.command == "get" else
                      relevance.read_feedback(args.root, value["path"], value["revision"], value["sha256"],
                                              value.get("actor")))
        else:
            result = relevance.feedback(args.root, value)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("outcome") not in {"error", "incomplete", "conflict"} else 2
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ts.ReadError) as exc:
        print(json.dumps({"profile": relevance.PROFILE, "outcome": "error",
                          "code": getattr(exc, "code", "invalid"), "message": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
