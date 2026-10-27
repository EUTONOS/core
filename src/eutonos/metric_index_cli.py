"""CLI for the disposable dashboard metric index."""
import argparse
import json

from .metric_index import status, update


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or refresh the EUTONOS metric index.")
    parser.add_argument("command", choices=("status", "update", "rebuild"))
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    result = status(args.root) if args.command == "status" else update(args.root, rebuild=args.command == "rebuild")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
