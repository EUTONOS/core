"""Read-only command line projection of local delivery state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .delivery_summary import project


def _authority(value):
    return {"yes": True, "no": False, "unknown": None}[value]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read current Git and campaign delivery state without writes.")
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--commit-authority", choices=("yes", "no", "unknown"), default="unknown")
    parser.add_argument("--publish-authority", choices=("yes", "no", "unknown"), default="unknown")
    args = parser.parse_args(argv)
    authority = {
        "commit": {"authorized": _authority(args.commit_authority), "source": "CLI argument"},
        "publish": {"authorized": _authority(args.publish_authority), "source": "CLI argument"},
    }
    print(json.dumps(project(args.root, authority=authority), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
