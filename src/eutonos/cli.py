"""JSON output from the same library used by all executable adapters."""

from __future__ import annotations

import argparse
import importlib
import json
import sys

from .hub import Hub
from .source import GitView
from . import tokenslang as ts


def _batch_item(value: str, target, required: bool) -> dict:
    """Parse the compact ID=SOURCE form without interpreting source text."""
    item_id, separator, source = value.partition("=")
    ts.require(separator == "=" and item_id.strip() and source.strip(), "invalid",
               "Batch items use ID=RESOURCE_ID or ID=PATH.")
    return {"id": item_id.strip(), "target": target(source.strip()), "required": required}


# Lifecycle verbs (IPC5 lane A): word -> (module, function, leading argv the target expects). Imported lazily, so a
# lane that has not landed yet answers "not built yet" and every other `eutonos` argument behaves as before.
LIFECYCLE = {
    "install": ("eutonos.installer", "main", ["install"]),
    "update": ("eutonos.installer", "main", ["update"]),
    "rollback": ("eutonos.installer", "main", ["rollback"]),
    "patch": ("eutonos.installer", "main", ["patch"]),
    "self-check": ("eutonos.installer", "main", ["self-check"]),
    "uninstall": ("eutonos.installer", "main", ["uninstall"]),
    "doctor": ("eutonos.doctor", "main", []),
    "add-project": ("eutonos.setup_cli", "main", ["add-project"]),
    "remove-project": ("eutonos.repository_lifecycle", "main", []),
}


def lifecycle(argv: list[str]) -> int:
    """Route `eutonos <verb> ...` to its main; a module that is not built yet is reported, never a traceback."""
    module_name, function, lead = LIFECYCLE[argv[0]]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        print(json.dumps({"outcome": "not-built-yet", "command": argv[0], "module": module_name,
                          "message": f"eutonos {argv[0]} is not built yet in this installation ({module_name} is missing)."}),
              file=sys.stderr)
        return 2
    return int(getattr(module, function)(lead + list(argv[1:])) or 0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in LIFECYCLE:
        return lifecycle(argv)
    parser = argparse.ArgumentParser(description="EUTONOS TokenSlang read core (EHF2)")
    parser.add_argument("--root", required=True)
    parser.add_argument("--revision", required=True, help="Resolved once to an exact Git commit")
    parser.add_argument("--view", choices=["git-commit"], default="git-commit")
    parser.add_argument("--catalog", default="eutonos.read.json")
    parser.add_argument("--json-request", action="store_true", help="Read a TokenSlang request from stdin")
    parser.add_argument("operation", nargs="?", choices=list(ts.OPERATIONS))
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ref", help="Registered resource ID in the selected catalog scope")
    target.add_argument("--path", help="Explicit admitted locator-only source")
    parser.add_argument("--ref-item", action="append", default=[], metavar="ID=RESOURCE_ID",
                        help="Required read.batch item; repeat up to 16 times")
    parser.add_argument("--path-item", action="append", default=[], metavar="ID=PATH",
                        help="Required read.batch path item; repeat up to 16 times")
    parser.add_argument("--optional-ref-item", action="append", default=[], metavar="ID=RESOURCE_ID")
    parser.add_argument("--optional-path-item", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--max-bytes", type=int, default=16384)
    parser.add_argument("--request-id", default="cli-read")
    parser.add_argument("--purpose", default="Inspect selected repository source")
    args = parser.parse_args(argv)
    try:
        hub = Hub(GitView(args.root, args.revision), args.catalog)
        batch_args = args.ref_item or args.path_item or args.optional_ref_item or args.optional_path_item
        if args.json_request:
            ts.require(args.operation is None and args.ref is None and args.path is None and not batch_args,
                       "invalid", "Use either a JSON request or operation flags.")
            raw = sys.stdin.buffer.read(ts.MAX_SOURCE_BYTES + 1)
            ts.require(len(raw) <= ts.MAX_SOURCE_BYTES, "overflow", "Request exceeds 128 KiB.")
            value = ts.loads(raw.decode("utf-8"))
        else:
            value = {"profile": ts.PROFILE, "request_id": args.request_id, "operation": args.operation,
                     "purpose": args.purpose, "max_bytes": args.max_bytes}
            if batch_args:
                ts.require(args.operation == "read.batch" and args.ref is None and args.path is None,
                           "invalid", "Batch item flags require read.batch and cannot use --ref or --path.")
                value["items"] = [
                    *[_batch_item(item, hub.ref, True) for item in args.ref_item],
                    *[_batch_item(item, lambda path: {"path": path}, True) for item in args.path_item],
                    *[_batch_item(item, hub.ref, False) for item in args.optional_ref_item],
                    *[_batch_item(item, lambda path: {"path": path}, False) for item in args.optional_path_item],
                ]
            elif args.ref:
                value["target"] = hub.ref(args.ref)
            elif args.path:
                value["target"] = {"path": args.path}
        result = hub.handle(value)
    except (ts.ReadError, OSError, UnicodeError) as exc:
        result = {"profile": ts.PROFILE, "outcome": getattr(exc, "code", "unavailable"),
                  "data": None, "coverage": {"required_complete": False},
                  "diagnostics": [{"code": getattr(exc, "code", "unavailable"),
                                   "message": exc.message if isinstance(exc, ts.ReadError) else "Cannot read the selected local input."}]}
    sys.stdout.buffer.write(ts.encoded(result) + b"\n")
    if (result.get("operation") == "read.batch"
            and result.get("coverage", {}).get("required_complete") is False):
        return 2
    return 0 if result["outcome"] in {"ok", "partial"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
