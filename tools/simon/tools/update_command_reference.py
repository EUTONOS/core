"""Generate/check the README command reference from Simon's schema owner."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from simon_schema import command_reference

START = "<!-- simon:commands:start -->\n"
END = "<!-- simon:commands:end -->"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = PROJECT_DIR / "README.md"
    source = path.read_text(encoding="utf-8")
    if START not in source:
        source += "\n## Generated command reference\n\n" + START + END + "\n"
    before, remaining = source.split(START, 1)
    old, after = remaining.split(END, 1)
    reference = command_reference()
    if args.check:
        if old != reference:
            print("README command reference is stale; run tools/update_command_reference.py.", file=sys.stderr)
            return 1
        return 0
    path.write_text(before + START + reference + END + after, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
