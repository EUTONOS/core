"""Zero-write Docs planner. Guarded apply stays with the admitted fixture host."""
import argparse
import sys

from . import tokenslang as ts
from .docs import plan_adoption
from .effects import LocalRoot, digest, validate_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="EUTONOS TokenSlang Docs adoption planner")
    parser.add_argument("--root", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--spec", required=True, help="Repository-relative file of explicit reviewed owner mappings")
    args = parser.parse_args(argv)
    try:
        root = LocalRoot(args.root, tuple(args.exclude))
        raw = root.read(args.spec)
        ts.require(raw is not None, "missing", "Adoption specification is absent.")
        result = plan_adoption(root, ts.loads(raw.decode("utf-8")))
        ts.require(root.read(args.spec) == raw, "stale", "Specification changed during planning.")
        result["effect"]["observed"][args.spec] = digest(raw)
        validate_plan(root, result["effect"])
        result["fingerprint"] = digest(ts.encoded(result["effect"]))
        result = {"outcome": "planned", "writes": 0, "adoption": result}
        code = 0
    except (ts.ReadError, OSError, UnicodeError) as exc:
        result = {"outcome": getattr(exc, "code", "unavailable"), "writes": 0,
                  "message": exc.message if isinstance(exc, ts.ReadError) else "Local input is unavailable."}
        code = 2
    sys.stdout.buffer.write(ts.encoded(result) + b"\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
