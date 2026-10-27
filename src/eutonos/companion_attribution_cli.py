"""Record and inspect explicit request-to-companion relationships."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import assignments
from . import companion_attribution
from .companion_results import project as companion_project


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def summary(root):
    companion = companion_project(root)
    return companion_attribution.project(
        assignments.records(root), companion.get("runs", []),
        receipts=companion_attribution.receipts(root),
        comparisons=companion.get("comparisons", []),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or record exact companion attribution evidence.")
    parser.add_argument("command", choices=("summary", "record"))
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--input", help="Receipt JSON path, or - for stdin.")
    args = parser.parse_args(argv)
    if args.command == "record":
        if not args.input:
            parser.error("record requires --input")
        result = companion_attribution.record(args.root, _load(args.input))
    else:
        result = summary(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
