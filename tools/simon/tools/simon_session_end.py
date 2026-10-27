"""Project SessionEnd hook: bounded exit of an idle Helper, never launch one."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import helperctl


def main(event: dict | None = None) -> int:
    if event is None:
        try:
            event = json.load(sys.stdin)
        except (ValueError, OSError):
            return 0
    if not isinstance(event, dict) or event.get("hook_event_name") != "SessionEnd":
        return 0
    cwd = Path(str(event.get("cwd") or ""))
    if cwd.resolve() != PROJECT_DIR and not cwd.resolve().is_relative_to(PROJECT_DIR):
        return 0
    status = helperctl.helper_runtime_status()
    if not status.get("responsive") or status.get("bridge_state") != "idle":
        return 0
    result = helperctl.send_to_helper_app("helper.exit", {}, timeout=2.0)
    if result.get("status") != "SUCCESS":
        print("Simon SessionEnd exit was not confirmed; inspect helper.bridge_status.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
