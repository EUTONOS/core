"""Inspect or run queued EUTONOS semantic write retries."""
from __future__ import annotations

import argparse
import json
import time

from . import write_retry as queue


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "run-once", "worker"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--host", required=True)
    parser.add_argument("--idle-seconds", type=int, default=86400)
    args = parser.parse_args(argv)
    if args.action == "status":
        result = queue.status(args.host, args.root)
    elif args.action == "run-once":
        result = queue.run_once(args.host, args.root)
    else:
        deadline = time.monotonic() + max(0, args.idle_seconds)
        result = {"outcome": "idle"}
        while time.monotonic() <= deadline:
            result = queue.run_once(args.host, args.root)
            if result["outcome"] == "complete":
                deadline = time.monotonic() + max(0, args.idle_seconds)
                continue
            if result["outcome"] == "waiting":
                time.sleep(2)
                continue
            if result["outcome"] == "delayed":
                time.sleep(max(0.05, min(60, result.get("wait_seconds", 2))))
                continue
            if result["outcome"] == "busy":
                time.sleep(1)
                continue
            break
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result.get("outcome") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
