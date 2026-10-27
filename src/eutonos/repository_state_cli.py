"""Read-only repository-wide work orientation; never integrates source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import repository_state, tokenslang as ts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--summary", action="store_true", help="Compact agent-facing orientation")
    parser.add_argument("--view", choices=("current", "main", "checkout", "source"),
                        help="Shared Current Work, observed Main, or one source; omission retains the legacy comparison")
    parser.add_argument("--source", help="Exact source ID returned by the shared view")
    parser.add_argument("--refresh", action="store_true", help="Reobserve local sources; does not fetch Git remotes")
    args = parser.parse_args(argv)
    try:
        if args.view:
            from . import current_work
            result = current_work.project(Path(args.root), view=args.view, source=args.source, refresh=args.refresh, include_knowledge=not args.summary)
            if args.summary:
                result = current_work.compact(result)
        else:
            if args.source:
                raise ValueError("--source requires --view source")
            result = repository_state.project(Path(args.root), **({"refresh": True} if args.refresh else {}))
            if args.summary:
                result = repository_state.compact(result)
    except (OSError, ValueError, TypeError, ts.ReadError) as exc:
        result = {"profile": "eutonos.repository-state.v1", "status": "unavailable",
                  "reason": str(exc), "next_action": "Inspect current source directly; repository state is unavailable."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
