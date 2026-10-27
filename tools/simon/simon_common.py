from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from simon_paths import (
    APP_DIR,
    RUNTIME_DIR,
    STATE_FILE,
    COMMAND_FILE,
    HELPER_COMMAND_FILE,
    HELPER_RESULT_FILE,
    HELPER_BRIDGE_STATUS_FILE,
    HELPER_BRIDGE_HISTORY_FILE,
    HELPER_RUN_JOURNAL_FILE,
    HELPER_CAPTURE_ROUTE_HISTORY_FILE,
    HELPER_CAPTURE_ROUTE_TALLY_FILE,
    HELPER_INSTANCE_LOCK_FILE,
    TARGET_LOG_FILE,
    HELPER_LOG_FILE,
)


TARGET_TITLE = "Simon Target"
HELPER_SHORT_NAME = "Simon"
HELPER_EXPANDED_NAME = "Screen Interface Mapping Operations Navigator"
HELPER_TITLE = f"{HELPER_SHORT_NAME}. {HELPER_EXPANDED_NAME}"
DIALOG_TITLE = "Simon Dialog Test"
DIALOG_EXPECTED_NUMBER = "10"
DIALOG_EXPECTED_CHOICE = "Yes"

FIXED_SEQUENCE = [
    "red",
    "green",
    "blue",
    "green",
    "yellow",
    "purple",
    "orange",
    "red",
    "blue",
    "yellow",
    "green",
    "purple",
    "red",
    "orange",
    "blue",
    "green",
    "yellow",
    "red",
    "purple",
    "orange",
]

COLOR_SPECS: dict[str, dict[str, Any]] = {
    "red": {"label": "Red", "hex": "#d62828", "rgb": (214, 40, 40), "fg": "white"},
    "green": {"label": "Green", "hex": "#2a9d3f", "rgb": (42, 157, 63), "fg": "white"},
    "blue": {"label": "Blue", "hex": "#1d4ed8", "rgb": (29, 78, 216), "fg": "white"},
    "yellow": {"label": "Yellow", "hex": "#f9c74f", "rgb": (249, 199, 79), "fg": "black"},
    "purple": {"label": "Purple", "hex": "#8b5cf6", "rgb": (139, 92, 246), "fg": "white"},
    "orange": {"label": "Orange", "hex": "#f97316", "rgb": (249, 115, 22), "fg": "black"},
}

COLOR_ORDER = ["red", "green", "blue", "yellow", "purple", "orange"]


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def now_stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    ensure_runtime_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)
    last_error: OSError | None = None
    for attempt in range(5):
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(path)
            return
        except OSError as exc:
            last_error = exc
            try:
                tmp_path.unlink()
            except OSError:
                pass
            time.sleep(0.03 * (attempt + 1))
    if last_error is not None:
        raise last_error


def append_log(path: Path, line: str) -> None:
    ensure_runtime_dir()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")
