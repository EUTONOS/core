from __future__ import annotations

import ctypes
import hashlib
import secrets
import stat as stat_module
import uuid
from simon_capture import DPI_AWARENESS, enable_process_dpi_awareness
import simon_capture
from simon_capture import BITMAPINFO, BITMAPINFOHEADER, bitmap_to_image, capture_desktop_image, capture_desktop_image_with_details, capture_monitor_image, capture_monitor_image_with_details, capture_region_gdi, capture_window_image, capture_window_image_with_details, capture_window_printwindow, get_virtual_screen_origin
from simon_geometry import image_transform, crop_transform, image_point_to_screen, render_planner_grid, GRID_OFFSET_PRESETS, column_label, grid_cell_bbox, grid_cell_center, grid_cell_point
from ctypes import wintypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from typing import Any

from simon_verification import (expected_change_from_args, expected_change_result, declared_effect,
    canonical_effect_type, validate_expected_change, validate_file_selector, validate_sha256, evaluate_effect,
    effect_verdict, effect_allows_continuation, VISUAL_EFFECTS, MAX_CONTENT_BYTES)
from simon_schema import ARGUMENTS, COMMANDS, command_catalog, validate_arguments
from simon_protocol import result_outcome, result_view
from simon_images import observation_packets, planner_image
from simon_artifacts import cleanup_runtime, register_result_artifacts, track_generated_artifacts, record_generated_artifact, artifact_manifest_lock

# Compatibility exports keep the resident host and existing callers working.
from simon_paths import (
    COMMAND_LOG_FILE,
    COMMAND_REPORT_DIR,
    COMMAND_SCREENSHOT_DIR,
    PLANNER_REQUEST_DIR,
    FAILURE_DIAGNOSTICS_DIR,
    PLANNER_MEMORY_FILE,
    ENVIRONMENT_DIAGNOSTICS_FILE,
    COMMAND_BRIDGE_LOCK_FILE,
)
from simon_protocol import (
    CLI_CONTROL_KEYS,
    DEFAULT_CLI_BRIDGE_TIMEOUT_SECONDS,
    build_cli_parser,
    build_cli_payload,
    command_result,
    default_next_actions,
    make_request_id,
    safe_request_id,
    emit,
    strip_internal,
)
from simon_artifacts import (
    write_bridge_status,
    append_bridge_history,
    append_run_journal,
    read_bridge_history_tail,
    read_lock_file_snapshot,
    record_capture_route,
    load_planner_working_memory,
    save_planner_working_memory,
    planner_request_path,
    write_planner_request,
    track_new_planner_requests,
    record_new_planner_request,
    load_planner_request,
    write_guided_action_report,
    write_planner_step_report,
    write_command_log,
    write_command_report,
    collect_artifact_paths,
    write_command_failure_bundle,
)

from simon_common import (
    APP_DIR,
    COLOR_ORDER,
    COLOR_SPECS,
    DIALOG_TITLE,
    HELPER_BRIDGE_HISTORY_FILE,
    HELPER_CAPTURE_ROUTE_HISTORY_FILE,
    HELPER_CAPTURE_ROUTE_TALLY_FILE,
    HELPER_RUN_JOURNAL_FILE,
    HELPER_BRIDGE_STATUS_FILE,
    HELPER_COMMAND_FILE,
    HELPER_INSTANCE_LOCK_FILE,
    HELPER_SHORT_NAME,
    HELPER_TITLE,
    HELPER_RESULT_FILE,
    RUNTIME_DIR,
    STATE_FILE,
    TARGET_TITLE,
    append_log,
    ensure_runtime_dir,
    now_iso,
    now_stamp,
    read_json,
    write_json_atomic,
)

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageGrab, ImageStat
except ImportError:  # pragma: no cover - reported through command results.
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFont = None
    ImageGrab = None
    ImageStat = None

try:
    import pyautogui
except ImportError:  # pragma: no cover - reported through command results.
    pyautogui = None


COLOR_TOLERANCE = 38
MIN_COLOR_PIXELS = 4500
FOCUS_SETTLE_SECONDS = 0.20
DEFAULT_STALE_SECONDS = 60.0
DEFAULT_BRIDGE_STALE_SECONDS = 20.0
# helper.start waits this long for launch confirmation when neither wait_seconds
# nor an explicit --timeout is given (matches the old CLI-injected 10s timeout).
DEFAULT_HELPER_START_WAIT_SECONDS = 10.0
# Pixel-center normalization should land exactly. Preserve the 2px tolerance
# and one SetCursorPos correction as safety nets until broader input acceptance.
CLICK_CURSOR_TOLERANCE_PX = 2
LAST_CAPTURE_ATTEMPTS: list[dict[str, Any]] = []
_COMMAND_CANCEL_LOCAL = threading.local()
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
BM_CLICK = 0x00F5
BM_SETCHECK = 0x00F1
BM_GETCHECK = 0x00F0
BST_UNCHECKED = 0
BST_CHECKED = 1
CB_SHOWDROPDOWN = 0x014F
LB_SETCURSEL = 0x0186
SW_RESTORE = 9
SW_SHOW = 5
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
ASFW_ANY = 0xFFFFFFFF
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
BACKEND_MESSAGE_TIMEOUT_MS = 1500
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
INPUT_MOUSE = 0


class CommandCancelled(RuntimeError):
    def __init__(self, reason: str = "Command cancelled.") -> None:
        super().__init__(reason)
        self.reason = reason


class CommandCancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._cancelled_epoch: float | None = None

    def cancel(self, reason: str) -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._cancelled_epoch = time.time()
                self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason or "Command cancelled."

    @property
    def cancelled_epoch(self) -> float | None:
        with self._lock:
            return self._cancelled_epoch


@contextmanager
def command_cancellation(token: CommandCancellationToken | None):
    previous = getattr(_COMMAND_CANCEL_LOCAL, "token", None)
    _COMMAND_CANCEL_LOCAL.token = token
    try:
        yield
    finally:
        _COMMAND_CANCEL_LOCAL.token = previous


def command_cancelled_payload(reason: str) -> dict[str, Any]:
    return {
        "failure_type": "HELPER_COMMAND_CANCELLED",
        "cancel_reason": reason,
    }


def check_command_cancelled() -> None:
    token = getattr(_COMMAND_CANCEL_LOCAL, "token", None)
    if token is not None and token.is_cancelled():
        raise CommandCancelled(token.reason)


def cancellable_sleep(seconds: float, poll_seconds: float = 0.05) -> None:
    remaining = max(0.0, float(seconds))
    if remaining <= 0:
        check_command_cancelled()
        return
    deadline = time.monotonic() + remaining
    while True:
        check_command_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            check_command_cancelled()
            return
        time.sleep(min(max(0.001, poll_seconds), remaining))


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def bounds(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class MonitorInfo:
    handle: int
    left: int
    top: int
    right: int
    bottom: int
    work_left: int
    work_top: int
    work_right: int
    work_bottom: int
    primary: bool
    device_name: str

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass
class DetectedButton:
    color: str
    bbox: tuple[int, int, int, int]
    center: tuple[int, int]
    pixels: int
    confidence: float


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    command = str(args.command)
    pretty = bool(getattr(args, "pretty", False))
    json_file = str(getattr(args, "json_file", "") or "")
    json_value = str(getattr(args, "json", "") or "")

    try:
        json_text = ""
        if json_file:
            json_text = Path(json_file).read_text(encoding="utf-8")
        elif json_value:
            json_text = json_value
        extra = json.loads(json_text) if json_text else {}
        if not isinstance(extra, dict):
            return emit(command_result("ERROR", command, "The --json value must be an object."), pretty=pretty)
    except json.JSONDecodeError as exc:
        return emit(command_result("ERROR", command, f"Could not parse --json: {exc}"), pretty=pretty)

    payload = build_cli_payload(args, extra)
    if bool(getattr(args, "via_app", False)):
        timeout = float(getattr(args, "timeout", DEFAULT_CLI_BRIDGE_TIMEOUT_SECONDS))
        result = send_to_helper_app(command, payload, timeout=timeout)
    else:
        result = dispatch(command, payload)
    return emit(result, pretty=pretty)


def send_to_helper_app(command: str, args: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    ensure_runtime_dir()
    request_id = args.get("bridge_request_id") or make_request_id()
    helper_status = helper_runtime_status()
    skip_precheck = bool(args.get("skip_helper_precheck")) or command == "planner.fast_observe"
    if not skip_precheck and not helper_status["responsive"]:
        return bridge_unresponsive_result(command, request_id, helper_status)
    now = time.time()
    request = {
        "request_id": request_id,
        "command": command,
        "args": {key: value for key, value in args.items() if key not in {"via_app", "timeout", "skip_helper_precheck", "pretty"}},
        "created_at": now_iso(),
        "created_epoch": now,
        "timeout_seconds": timeout,
        "client_pid": os.getpid(),
    }
    lock_fd = acquire_bridge_lock(timeout=min(2.0, max(0.2, timeout / 3)))
    if lock_fd is None:
        return command_result(
            "UNCERTAIN",
            command,
            f"{HELPER_SHORT_NAME} command bridge is busy with another request.",
            data={"request_id": request_id, "helper_command_file": str(HELPER_COMMAND_FILE)},
        )
    try:
        cleanup = cleanup_stale_bridge_request(float(args.get("bridge_stale_seconds") or DEFAULT_BRIDGE_STALE_SECONDS))
        try:
            HELPER_RESULT_FILE.unlink()
        except OSError:
            pass
        busy_deadline = time.time() + min(2.0, max(0.2, timeout / 3))
        while HELPER_COMMAND_FILE.exists() and time.time() < busy_deadline:
            cancellable_sleep(0.05)
        if HELPER_COMMAND_FILE.exists():
            return command_result(
                "UNCERTAIN",
                command,
                f"{HELPER_SHORT_NAME} command bridge is busy with another request.",
                data={
                    "request_id": request_id,
                    "helper_command_file": str(HELPER_COMMAND_FILE),
                    "stale_cleanup": cleanup,
                    "bridge_status": read_json(HELPER_BRIDGE_STATUS_FILE),
                },
            )
        write_bridge_status(
            "queued",
            {
                "request_id": request_id,
                "command": command,
                "client_pid": os.getpid(),
                "created_epoch": now,
                "timeout_seconds": timeout,
                "skip_helper_precheck": skip_precheck,
            },
        )
        write_json_atomic(HELPER_COMMAND_FILE, request)
        append_run_journal(
            "command_issued",
            request_id,
            command,
            "codex",
            {
                "timeout_seconds": timeout,
                "skip_helper_precheck": skip_precheck,
                "args": request.get("args") or {},
            },
        )
        append_bridge_history(
            "queued",
            request_id,
            command,
            {
                "client_pid": os.getpid(),
                "stale_cleanup": cleanup,
                "skip_helper_precheck": skip_precheck,
                "helper_runtime": helper_status,
            },
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = read_json(HELPER_RESULT_FILE)
            if result and result.get("request_id") == request_id:
                elapsed_ms = round((time.time() - now) * 1000, 1)
                append_bridge_history(
                    "client_received_result",
                    request_id,
                    command,
                    {
                        "status": (result.get("result") or {}).get("status"),
                        "completed_at": result.get("completed_at"),
                        "elapsed_ms": elapsed_ms,
                    },
                )
                payload = result.get("result") or command_result("ERROR", command, f"{HELPER_SHORT_NAME} app returned an empty result.")
                append_run_journal(
                    "command_result_received",
                    request_id,
                    command,
                    "codex",
                    {
                        "status": payload.get("status"),
                        "elapsed_ms": elapsed_ms,
                        "completed_at": result.get("completed_at_iso") or result.get("completed_at"),
                    },
                )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                data["bridge_timing"] = {
                    "request_id": request_id,
                    "elapsed_ms": elapsed_ms,
                    "skip_helper_precheck": skip_precheck,
                }
                payload["data"] = data
                return payload
            cancellable_sleep(0.05)
        removed = remove_pending_bridge_request(request_id)
        elapsed_ms = round((time.time() - now) * 1000, 1)
        append_bridge_history("client_timeout", request_id, command, {"removed_pending_request": removed, "elapsed_ms": elapsed_ms})
        append_run_journal(
            "command_timeout",
            request_id,
            command,
            "codex",
            {
                "removed_pending_request": removed,
                "elapsed_ms": elapsed_ms,
                "bridge_status": read_json(HELPER_BRIDGE_STATUS_FILE),
            },
        )
        return command_result(
            "UNCERTAIN",
            command,
            f"Timed out waiting for {HELPER_SHORT_NAME} app result.",
            data={
                "request_id": request_id,
                "helper_command_file": str(HELPER_COMMAND_FILE),
                "removed_pending_request": removed,
                "bridge_status": read_json(HELPER_BRIDGE_STATUS_FILE),
                "bridge_timing": {
                    "request_id": request_id,
                    "elapsed_ms": elapsed_ms,
                    "skip_helper_precheck": skip_precheck,
                },
            },
        )
    finally:
        release_bridge_lock(lock_fd)


def bridge_unresponsive_result(command: str, request_id: str, helper_status: dict[str, Any]) -> dict[str, Any]:
    return command_result(
        "UNCERTAIN",
        command,
        f"{HELPER_SHORT_NAME} app is not responsive; cannot send bridged command.",
        data={
            "request_id": request_id,
            "helper_runtime": helper_status,
            "bridge_status": read_json(HELPER_BRIDGE_STATUS_FILE),
        },
        next_allowed_actions=["helper.bridge_status"],
    )


def acquire_bridge_lock(timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return os.open(str(COMMAND_BRIDGE_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                return None
            cancellable_sleep(0.05)


def release_bridge_lock(lock_fd: int) -> None:
    try:
        os.close(lock_fd)
    except OSError:
        pass
    try:
        COMMAND_BRIDGE_LOCK_FILE.unlink()
    except OSError:
        pass


def cleanup_stale_bridge_request(max_age_seconds: float) -> dict[str, Any]:
    request = read_json(HELPER_COMMAND_FILE)
    if not request:
        return {"removed": False, "reason": "no_pending_request"}
    created_epoch = float(request.get("created_epoch") or 0.0)
    if created_epoch <= 0:
        return {"removed": False, "reason": "pending_request_has_no_created_epoch"}
    age = time.time() - created_epoch
    if age < max_age_seconds:
        return {"removed": False, "reason": "pending_request_not_stale", "age_seconds": round(age, 3)}
    request_id = str(request.get("request_id") or "")
    command = str(request.get("command") or "")
    try:
        HELPER_COMMAND_FILE.unlink()
    except OSError:
        return {"removed": False, "reason": "unlink_failed", "age_seconds": round(age, 3), "request_id": request_id}
    append_bridge_history("stale_request_removed", request_id, command, {"age_seconds": round(age, 3)})
    write_bridge_status("idle", {"stale_request_removed": request_id, "removed_age_seconds": round(age, 3)})
    return {"removed": True, "age_seconds": round(age, 3), "request_id": request_id, "command": command}


def arg_float_or_default(args: dict[str, Any], key: str, default: float) -> float:
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def remove_pending_bridge_request(request_id: str) -> bool:
    request = read_json(HELPER_COMMAND_FILE)
    if not request or str(request.get("request_id") or "") != str(request_id):
        return False
    try:
        HELPER_COMMAND_FILE.unlink()
        return True
    except OSError:
        return False


def helper_runtime_status() -> dict[str, Any]:
    lock_info = read_lock_file_snapshot(HELPER_INSTANCE_LOCK_FILE)
    content = lock_info.get("content") if isinstance(lock_info.get("content"), dict) else {}
    pid = int(content.get("pid") or 0)
    alive = is_process_alive(pid) if pid else False
    status_file = read_json(HELPER_BRIDGE_STATUS_FILE)
    status_data = status_file.get("data") if isinstance(status_file, dict) and isinstance(status_file.get("data"), dict) else {}
    status_pid = int(status_data.get("helper_pid") or 0)
    updated_epoch = float(status_file.get("updated_epoch") or 0.0) if isinstance(status_file, dict) else 0.0
    heartbeat_age_seconds = round(time.time() - updated_epoch, 3) if updated_epoch > 0 else None
    state = str(status_file.get("state") or "") if isinstance(status_file, dict) else ""
    fresh_heartbeat = (
        alive
        and status_pid == pid
        and state == "idle"
        and heartbeat_age_seconds is not None
        and heartbeat_age_seconds <= DEFAULT_BRIDGE_STALE_SECONDS
    )
    return {
        "running": alive,
        "responsive": fresh_heartbeat,
        "pid": pid or None,
        "instance_lock": lock_info,
        "bridge_state": state or None,
        "bridge_status_pid": status_pid or None,
        "heartbeat_age_seconds": heartbeat_age_seconds,
    }


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if not hasattr(ctypes, "windll"):
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    exit_code = wintypes.DWORD()
    try:
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def dispatch(command: str, args: dict[str, Any], handler_override: Any = None) -> dict[str, Any]:
    ensure_runtime_dir()
    warnings, errors = validate_arguments(command, args)
    if not isinstance(args, dict):
        args = {}
    spec = COMMANDS.get(command)
    handler = handler_override or (globals().get(spec["handler"]) if spec else None)
    with track_generated_artifacts() as generated_paths, track_new_planner_requests() as new_planner_request_ids:
        try:
            check_command_cancelled()
            if errors:
                result = command_result("ERROR", command, "Command arguments are invalid.",
                                        data={"failure_type": "INVALID_ARGUMENTS", "errors": errors},
                                        next_allowed_actions=["helper.commands"])
            elif handler is None:
                result = command_result("ERROR", command, f"Unknown command {command!r}.", next_allowed_actions=["helper.commands"])
            else:
                result = handler(args)
        except CommandCancelled as exc:
            result = command_result("UNCERTAIN", command, f"Command cancelled before completion: {exc.reason}",
                                    data=command_cancelled_payload(exc.reason), next_allowed_actions=["helper.bridge_status"])
        except Exception as exc:
            result = command_result("ERROR", command, f"Unhandled command error: {exc}")
    result["command"] = command
    return finish_command_result(command, args, result, new_planner_request_ids, warnings, generated_paths)


def finish_command_result(command: str, args: dict[str, Any], result: dict[str, Any],
                          new_observation_ids: list[str], warnings: list[str], generated_paths: list[Path] | None = None) -> dict[str, Any]:
    # Metadata/validation results do not need another desktop observation.
    if command not in {"helper.commands", "runtime.cleanup"} and not result.get("data", {}).get("errors"):
        result = enrich_result_context(command, args, result, new_observation_ids=new_observation_ids)
    result["outcome"] = result_outcome(result)
    if warnings:
        result["warnings"] = warnings
    try:
        image = planner_image(result, args)
        if image:
            result.setdefault("data", {})["planner_image"] = image
    except (OSError, ValueError) as exc:
        result.setdefault("warnings", []).append(f"Planner image unavailable: {exc}")
    try:
        write_command_log(result)
        if result["status"] in {"ERROR", "FAILED", "UNCERTAIN"}:
            result["failure_bundle"] = str(write_command_failure_bundle(result))
        full = {k: v for k, v in result.items() if k != "image"}
        details = write_command_report(full)
        result["details_path"] = str(details)
        if result["status"] in {"ERROR", "FAILED", "UNCERTAIN"}:
            result["failure_report"] = str(details)
        register_result_artifacts(result, details, generated_paths)
        if command != "runtime.cleanup":
            cleanup_runtime(dry_run=False)
    except OSError as exc:
        result["status"] = "UNCERTAIN"
        result["outcome"] = "blocked"
        result.setdefault("data", {})["evidence_error"] = str(exc)
        return result_view(result, "full")
    return result_view(result, str(args.get("detail") or "minimal"))


def cmd_runtime_cleanup(args: dict[str, Any]) -> dict[str, Any]:
    data = cleanup_runtime(dry_run=bool(args.get("dry_run", True)),
                           max_age_days=float(args.get("max_age_days", 7)),
                           max_groups=int(args.get("max_groups", 100)),
                           max_bytes=int(args.get("max_bytes", 256 * 1024 * 1024)))
    return command_result("UNCERTAIN" if data["errors"] else "SUCCESS", "runtime.cleanup", "Runtime retention evaluated.", data=data)


def cmd_stop_screensaver(args: dict[str, Any]) -> dict[str, Any]:
    from screensaver_control import recover
    check_command_cancelled()
    result = recover()
    return command_result(
        "SUCCESS" if result["recovered"] else "UNCERTAIN",
        "recovery.stop_screensaver",
        "Normal desktop input is available; take a fresh screenshot." if result["recovered"] else
        "Normal desktop input remains unavailable. Unlock may be required; no input was attempted.",
        data=result,
        next_allowed_actions=["diagnose.environment", "observe.screenshot"],
    )


def cmd_diagnose_environment(args: dict[str, Any]) -> dict[str, Any]:
    include_capture = bool(args.get("include_capture", True))
    diagnostics = collect_environment_diagnostics(include_capture=include_capture)
    write_json_atomic(ENVIRONMENT_DIAGNOSTICS_FILE, diagnostics)
    status = "SUCCESS"
    message = "Environment diagnostics collected."
    if diagnostics.get("window_probe", {}).get("visible_titled_window_count", 0) <= 0:
        status = "UNCERTAIN"
        message = "Environment diagnostics collected; no visible titled windows were enumerated."
    return command_result(
        status,
        "diagnose.environment",
        message,
        data={
            "diagnostics": diagnostics,
            "diagnostics_path": str(ENVIRONMENT_DIAGNOSTICS_FILE),
        },
        next_allowed_actions=["helper.bridge_status", "observe.windows", "recovery.capture_grid"],
    )


def cmd_helper_start(args: dict[str, Any]) -> dict[str, Any]:
    if str(args.get("mode") or "normal").lower() == "elevated" and args.get("allow_elevation") is not True:
        return command_result("UNCERTAIN", "helper.start", "Elevated launch requires explicit allow_elevation=true.",
                              data={"failure_type": "ELEVATION_OPT_IN_REQUIRED"}, next_allowed_actions=["helper.start"])
    allow_sandbox = bool(args.get("allow_sandbox", False))
    requested_mode = str(args.get("mode") or "normal").strip().lower()
    if requested_mode == "regular":
        requested_mode = "normal"
    if requested_mode not in {"normal", "elevated"}:
        requested_mode = "normal"
    wait_seconds_value = args.get("wait_seconds")
    if wait_seconds_value is None:
        wait_seconds_value = args.get("timeout") or DEFAULT_HELPER_START_WAIT_SECONDS
    wait_seconds = max(0.0, min(60.0, float(wait_seconds_value)))
    launch_started_epoch = time.time()
    before = collect_environment_diagnostics(include_capture=False)
    before_classification = before.get("classification") if isinstance(before.get("classification"), dict) else {}
    before_runtime = helper_runtime_status()
    if before_runtime.get("responsive"):
        if requested_mode == "elevated":
            return command_result(
                "UNCERTAIN",
                "helper.start",
                "A regular Helper is already running and responsive. Close it with helper.exit before starting the elevated Helper.",
                data={
                    "launch_classification": "regular_helper_already_running",
                    "requested_mode": requested_mode,
                    "before": before,
                    "after_runtime": before_runtime,
                    "recovery_instruction": "Run helper.exit, then helper.start_elevated, approve UAC if prompted, then planner.fast_observe before continuing.",
                    "recommended_recovery_flow": [
                        "helper.exit",
                        "helper.start_elevated",
                        "planner.fast_observe",
                    ],
                },
                next_allowed_actions=["helper.exit", "helper.start_elevated", "helper.bridge_status"],
            )
        return command_result(
            "SUCCESS",
            "helper.start",
            f"{HELPER_SHORT_NAME} is already running and responsive.",
            data={
                "launch_classification": "already_running",
                "requested_mode": requested_mode,
                "before": before,
                "after_runtime": before_runtime,
            },
            next_allowed_actions=["helper.bridge_status", "diagnose.environment", "status.current"],
        )
    if requested_mode == "normal" and before_classification.get("desktop_kind") == "sandbox" and not allow_sandbox:
        return command_result(
            "UNCERTAIN",
            "helper.start",
            "Current process is on the Codex sandbox desktop; refusing to launch the Helper here because it would not see the real interactive desktop.",
            data={
                "launch_classification": "blocked_sandbox_desktop",
                "requested_mode": requested_mode,
                "before": before,
                "recommended_manual_launch": str(APP_DIR / "run_helper.bat"),
                "recommended_validation": "Run helper.bridge_status, then diagnose.environment --via-app from a live Helper.",
            },
            next_allowed_actions=["diagnose.environment", "helper.bridge_status"],
        )
    python_exe = Path(sys.executable)
    script = APP_DIR / "agent_helper.py"
    launch_report: dict[str, Any] = {
        "python_exe": str(python_exe),
        "script": str(script),
        "cwd": str(APP_DIR),
        "allow_sandbox": allow_sandbox,
        "wait_seconds": wait_seconds,
        "requested_mode": requested_mode,
        "launch_started_epoch": launch_started_epoch,
        "launch_started_at": now_iso(),
    }
    clear_stale_helper_launch_reports(launch_started_epoch)
    try:
        if requested_mode == "elevated":
            process = launch_helper_elevated(python_exe, script, launch_report)
        else:
            process = subprocess.Popen(
                [str(python_exe), str(script)],
                cwd=str(APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            launch_report["pid"] = process.pid
    except Exception as exc:
        return command_result(
            "ERROR",
            "helper.start",
            f"Could not start {HELPER_SHORT_NAME}: {exc}",
            data={"launch_classification": "launch_failed", "requested_mode": requested_mode, "before": before, "launch": launch_report},
            next_allowed_actions=["helper.bridge_status", "diagnose.environment"],
        )
    deadline = time.time() + wait_seconds
    after_runtime = helper_runtime_status()
    while time.time() < deadline:
        after_runtime = helper_runtime_status()
        if after_runtime.get("responsive"):
            break
        cancellable_sleep(0.25)
    after = collect_environment_diagnostics(include_capture=False)
    launch_classification = classify_helper_launch(before, after, launch_report, after_runtime)
    status = "SUCCESS" if launch_classification.get("status") == "running_interactive" else "UNCERTAIN"
    return command_result(
        status,
        "helper.start",
        launch_classification.get("message") or f"{HELPER_SHORT_NAME} launch attempted.",
        data={
            "launch_classification": launch_classification,
            "requested_mode": requested_mode,
            "launch": launch_report,
            "before": before,
            "after": after,
            "after_runtime": after_runtime,
            "startup_exit_report": read_fresh_helper_launch_report(RUNTIME_DIR / "agent_helper_last_exit.json", launch_started_epoch),
            "startup_exception_report": read_fresh_helper_launch_report(RUNTIME_DIR / "agent_helper_last_exception.json", launch_started_epoch),
            "elevated_launcher_report": read_elevated_launcher_report(launch_report),
            "elevated_child_report": read_elevated_child_report(launch_report),
        },
        next_allowed_actions=["helper.bridge_status", "diagnose.environment", "status.current"],
    )


def cmd_helper_start_elevated(args: dict[str, Any]) -> dict[str, Any]:
    return cmd_helper_start({**args, "mode": "elevated"})


def launch_helper_elevated(python_exe: Path, script: Path, launch_report: dict[str, Any]) -> subprocess.Popen[Any]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
    elevated_python = python_exe
    launcher_report_path = RUNTIME_DIR / "helper_elevated_launcher.json"
    launcher_script_path = RUNTIME_DIR / "start_helper_elevated.ps1"
    elevated_child_script_path = RUNTIME_DIR / "run_agent_helper_elevated_child.ps1"
    elevated_child_report_path = RUNTIME_DIR / "helper_elevated_child.json"
    elevated_child_output_path = RUNTIME_DIR / "helper_elevated_child_output.txt"
    launcher_stdout_path = RUNTIME_DIR / "helper_elevated_launcher_stdout.txt"
    launcher_stderr_path = RUNTIME_DIR / "helper_elevated_launcher_stderr.txt"
    elevated_child_script = f"""
$ErrorActionPreference = 'Stop'
$reportPath = {json.dumps(str(elevated_child_report_path))}
$outputPath = {json.dumps(str(elevated_child_output_path))}
function Write-ChildReport($report) {{
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}}
$report = [ordered]@{{
    status = 'starting'
    pid = $PID
    python = {json.dumps(str(elevated_python))}
    script = {json.dumps(str(script))}
    cwd = {json.dumps(str(APP_DIR))}
    started_at = (Get-Date).ToString('o')
}}
Write-ChildReport $report
try {{
    Set-Location -LiteralPath {json.dumps(str(APP_DIR))}
    $report.status = 'running_python'
    $report.running_at = (Get-Date).ToString('o')
    Write-ChildReport $report
    & {json.dumps(str(elevated_python))} {json.dumps(str(script))} *> $outputPath
    $report.status = 'python_exited'
    $report.exit_code = $LASTEXITCODE
    $report.completed_at = (Get-Date).ToString('o')
}} catch {{
    $report.status = 'failed'
    $report.error = $_.Exception.Message
    $report.completed_at = (Get-Date).ToString('o')
}}
Write-ChildReport $report
"""
    elevated_child_script_path.write_text(elevated_child_script.strip() + "\n", encoding="utf-8")
    launcher_script = f"""
$ErrorActionPreference = 'Stop'
$reportPath = {json.dumps(str(launcher_report_path))}
function Write-LauncherReport($report) {{
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}}
$report = [ordered]@{{
    status = 'starting'
    powershell = {json.dumps(str(powershell))}
    child_script = {json.dumps(str(elevated_child_script_path))}
    cwd = {json.dumps(str(APP_DIR))}
    started_at = (Get-Date).ToString('o')
}}
Write-LauncherReport $report
try {{
    $childArgs = '-NoProfile -ExecutionPolicy Bypass -File "{str(elevated_child_script_path).replace("'", "''")}"'
    $p = Start-Process -FilePath {json.dumps(str(powershell))} -ArgumentList $childArgs -WorkingDirectory {json.dumps(str(APP_DIR))} -Verb RunAs -WindowStyle Minimized -PassThru
    $report.status = 'started'
    if ($p) {{ $report.elevated_pid = $p.Id }}
    $report.completed_at = (Get-Date).ToString('o')
}} catch {{
    $report.status = 'failed'
    $report.error = $_.Exception.Message
    $report.completed_at = (Get-Date).ToString('o')
}}
Write-LauncherReport $report
"""
    launcher_script_path.write_text(launcher_script.strip() + "\n", encoding="utf-8")
    launch_report["uac_required"] = True
    launch_report["uac_instruction"] = "Approve the Windows UAC prompt to start the elevated Helper."
    launch_report["launcher"] = "powershell script Start-Process -Verb RunAs"
    launch_report["elevated_python_exe"] = str(elevated_python)
    launch_report["elevated_launcher_report_path"] = str(launcher_report_path)
    launch_report["elevated_launcher_script_path"] = str(launcher_script_path)
    launch_report["elevated_child_script_path"] = str(elevated_child_script_path)
    launch_report["elevated_child_report_path"] = str(elevated_child_report_path)
    launch_report["elevated_child_output_path"] = str(elevated_child_output_path)
    launch_report["elevated_launcher_stdout_path"] = str(launcher_stdout_path)
    launch_report["elevated_launcher_stderr_path"] = str(launcher_stderr_path)
    return subprocess.Popen(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher_script_path)],
        cwd=str(APP_DIR),
        stdout=launcher_stdout_path.open("ab"),
        stderr=launcher_stderr_path.open("ab"),
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def clear_stale_helper_launch_reports(launch_started_epoch: float) -> None:
    for path in (RUNTIME_DIR / "agent_helper_last_exit.json", RUNTIME_DIR / "agent_helper_last_exception.json"):
        try:
            if not path.exists() or path.stat().st_mtime >= launch_started_epoch:
                continue
            path.unlink()
        except OSError:
            pass


def cmd_helper_commands(_args: dict[str, Any]) -> dict[str, Any]:
    return command_result("SUCCESS", "helper.commands", "Available commands returned.",
                          data={"commands": list(COMMANDS), "command_schemas": command_catalog(),
                                "target_modes": ARGUMENTS["target"]["choices"]})


def cmd_helper_exit(_args: dict[str, Any]) -> dict[str, Any]:
    return command_result(
        "SUCCESS",
        "helper.exit",
        "Helper exit requested.",
        data={"helper_should_exit": True},
        next_allowed_actions=[],
    )


def cmd_helper_bridge_status(args: dict[str, Any]) -> dict[str, Any]:
    cleanup = cleanup_stale_bridge_request(float(args.get("bridge_stale_seconds") or DEFAULT_BRIDGE_STALE_SECONDS))
    command_file = read_json(HELPER_COMMAND_FILE)
    result_file = read_json(HELPER_RESULT_FILE)
    status_file = read_json(HELPER_BRIDGE_STATUS_FILE)
    lock_info = read_lock_file_snapshot(COMMAND_BRIDGE_LOCK_FILE)
    helper_status = helper_runtime_status()
    data = {
        "cleanup": cleanup,
        "command_file_exists": HELPER_COMMAND_FILE.exists(),
        "pending_command": command_file,
        "result_file_exists": HELPER_RESULT_FILE.exists(),
        "last_result": result_file,
        "bridge_status": status_file,
        "client_lock": lock_info,
        "helper_runtime": helper_status,
        "helper_instance_lock": helper_status["instance_lock"],
        "history_tail": read_bridge_history_tail(10),
    }
    return command_result("SUCCESS", "helper.bridge_status", "Bridge status returned.", data=data)


def cmd_status_current(_args: dict[str, Any]) -> dict[str, Any]:
    data = {
        "timestamp": now_iso(),
        "foreground_window": get_foreground_window_snapshot(),
        "known_windows": {
            "target": window_payload(find_window_by_title(TARGET_TITLE)),
            "helper": window_payload(find_window_by_title(HELPER_TITLE)),
            "dialog": window_payload(find_window_by_title(DIALOG_TITLE)),
        },
        "simon_state": read_json(STATE_FILE),
        "active_context": build_active_context_packet(TARGET_TITLE),
        "working_memory": load_planner_working_memory(),
    }
    return command_result("SUCCESS", "status.current", "Current helper status returned.", data=data)


def cmd_detect_vm_boundary(args: dict[str, Any]) -> dict[str, Any]:
    partial = str(args.get("title") or args.get("title_contains") or "").strip()
    if partial == TARGET_TITLE and not str(args.get("title_contains") or "").strip():
        partial = ""
    scan_debug: dict[str, Any] = {
        "requested_title": partial,
        "probes": [],
    }
    windows = list_windows(partial_title=partial if partial else None)
    scan_debug["initial_window_count"] = len(windows)
    if not partial:
        by_hwnd = {window.hwnd: window for window in windows}
        for probe in ("VM", "VMware", "VirtualBox", "Oracle VM", "Hyper-V", "Virtual Machine", "Remote Desktop"):
            probe_windows = list_windows(partial_title=probe)
            scan_debug["probes"].append({"title_contains": probe, "window_count": len(probe_windows)})
            for window in probe_windows:
                by_hwnd[window.hwnd] = window
        windows = list(by_hwnd.values())
        scan_debug["merged_window_count"] = len(windows)
    vm_windows = []
    scanned_candidates = []
    for window in windows:
        payload = window_payload(window) or {}
        boundary = payload.get("vm_boundary") if isinstance(payload.get("vm_boundary"), dict) else {}
        if boundary.get("detected") or any(token in str(payload.get("title") or "").lower() for token in ("vmware", "virtualbox", "oracle vm", "hyper-v", "remote desktop")):
            scanned_candidates.append({
                "title": payload.get("title"),
                "process_name": payload.get("process_name"),
                "class_name": payload.get("class_name"),
                "vm_boundary": boundary,
            })
        boundary = payload.get("vm_boundary") if isinstance(payload.get("vm_boundary"), dict) else {}
        if boundary.get("detected"):
            vm_windows.append(payload)
    active_context = build_active_context_packet_for_args(args)
    active_boundary = active_context.get("vm_boundary") if isinstance(active_context.get("vm_boundary"), dict) else {}
    return command_result(
        "SUCCESS",
        "detect.vm_boundary",
        "VM boundary scan completed.",
        data={
            "vm_windows": vm_windows,
            "vm_window_count": len(vm_windows),
            "scan_debug": scan_debug,
            "scanned_vm_like_candidates": scanned_candidates,
            "active_context": active_context,
            "active_vm_boundary": active_boundary,
            "default_policy": "informational_only",
        },
        next_allowed_actions=["observe.windows", "planner.fast_observe", "focus.window"],
    )


def cmd_planner_fast_observe(args: dict[str, Any]) -> dict[str, Any]:
    payload = dict(args)
    payload.setdefault("target", "desktop")
    payload.setdefault("columns", 12)
    payload.setdefault("rows", 8)
    payload.setdefault("reason", "Fast planner observe: first screenshot doubles as helper health check.")
    payload.setdefault("failed_command", "planner.fast_observe")
    return cmd_recovery_capture_grid(payload)


def cmd_planner_step(args: dict[str, Any]) -> dict[str, Any]:
    effect, errors = declared_effect(args)
    if errors:
        return command_result("ERROR", "planner.step", "Invalid expected effect.",
                              data={"errors": errors, "failure_type": "ARGUMENT_ERROR", "input_emitted": False})
    if effect:
        args = {**args, "expected_change": effect, "wait_for_change": True,
                "observe_after": {**(args.get("observe_after") or {}), "expected_change": effect, "wait_for_change": True}}
    action_args = args.get("step") if isinstance(args.get("step"), dict) else args
    action_name = str(action_args.get("action") or "").strip()
    if not action_name:
        return command_result("ERROR", "planner.step", "JSON args must include an action name.")
    preview = args.get("preview") is True or action_args.get("preview") is True
    confirmation = args.get("confirm_token") or action_args.get("confirm_token")
    if (preview or confirmation) and action_name != "guided_grid_click":
        return command_result("ERROR", "planner.step", "Preview and confirmation require the guided_grid_click action.",
                              data={"failure_type": "ARGUMENT_ERROR", "input_emitted": False})
    if preview:
        action_args = {**action_args, "preview": True}
    pre_action_capture = capture_planner_step_baseline(args, action_name, action_args)
    if pre_action_capture["status"] not in {"SUCCESS", "SKIPPED"}:
        return pre_action_capture["result"]
    verify_before = args.get("verify_before") if isinstance(args.get("verify_before"), dict) else None
    if verify_before:
        verify_payload = dict(verify_before)
        if preview:
            verify_payload.update(focus_if_needed=False, allow_mouse_activation=False)
        verify_payload.setdefault("screenshot_on_failure", True)
        verify_result = cmd_verify_window_state(verify_payload)
        if verify_result["status"] != "SUCCESS":
            return verify_result
    if effect and not preview:
        if canonical_effect_type(effect) in VISUAL_EFFECTS:
            # verify_before can restore focus; its visual change is not the action.
            visual_baseline = capture_target(build_follow_up_capture_args(args, action_name, action_args, make_request_id()), with_grid=False)
            if visual_baseline["status"] != "SUCCESS":
                return command_result("UNCERTAIN", "planner.step", "Pre-action visual evidence unavailable; action not executed.",
                                      data={"input_emitted": False, "capture_failure": strip_internal(visual_baseline)})
            pre_action_capture["capture"] = visual_baseline
        snapshot = capture_effect_snapshot(effect)
        if snapshot.get("error"):
            return command_result("UNCERTAIN", "planner.step", "Pre-action effect evidence unavailable; action not executed.",
                                  data={"verification": effect_verdict(effect, {"reason": "pre_action_evidence_unavailable"}),
                                        "effect_snapshot": snapshot, "input_emitted": False})
        pre_action_capture.setdefault("capture", {"status": "SUCCESS", "data": {}})["data"]["effect_snapshot"] = snapshot
    action_result = execute_planner_action(action_name, action_args, args)
    if action_result["status"] not in {"SUCCESS", "NEEDS_PLANNER"}:
        return action_result
    if preview:
        return action_result
    if result_outcome(action_result) == "blocked":
        return action_result
    follow_up = capture_planner_follow_up(
        args,
        action_name,
        action_args,
        action_result,
        baseline_capture=pre_action_capture.get("capture"),
    )
    if follow_up["status"] != "SUCCESS":
        return follow_up
    follow_up_data = follow_up["data"]
    result_data = {
        "action": action_name,
        "action_result": strip_internal(action_result),
        "next_observation": follow_up_data,
        "preserve_foreground": True,
        "verification": effect_verdict(effect, follow_up_data.get("change_watch"), requested=bool(effect)),
    }
    if pre_action_capture.get("check"):
        result_data["pre_action_request_check"] = pre_action_capture["check"]
    if pre_action_capture.get("capture") is not None:
        baseline_data = pre_action_capture["capture"].get("data") if isinstance(pre_action_capture["capture"], dict) else {}
        if isinstance(baseline_data, dict):
            result_data["pre_action_baseline"] = {
                "request_id": baseline_data.get("request_id"),
                "raw_screenshot_path": baseline_data.get("raw_screenshot_path"),
                "foreground_window": baseline_data.get("foreground_window"),
                "target_window": baseline_data.get("target_window"),
            }
    report_path = write_planner_step_report(str(follow_up_data.get("request_id") or make_request_id()), result_data)
    result_data["planner_step_report_path"] = str(report_path)
    verified = result_data["verification"]
    return command_result(
        "UNCERTAIN" if effect and not verified["matched"] else "NEEDS_PLANNER",
        "planner.step",
        "Planner step effect verified; next observation captured." if effect_allows_continuation(verified) else "Planner step executed; intended effect needs verification.",
        data=result_data,
        next_allowed_actions=["planner.step", "verify.window_state", "helper.bridge_status", "status.current"],
    )


def cmd_observe_windows(args: dict[str, Any]) -> dict[str, Any]:
    partial = args.get("title") or ""
    windows = [window_payload(window) for window in list_windows(partial_title=partial if partial else None)]
    return command_result("SUCCESS", "observe.windows", "Windows enumerated.", data={"windows": windows})


def cmd_observe_window(args: dict[str, Any]) -> dict[str, Any]:
    window = find_best_window_for_args(args)
    if window is None:
        return command_result("UNCERTAIN", "observe.window", "Window not found.")
    return command_result("SUCCESS", "observe.window", "Window observed.", data={"window": window_payload(window)})


def cmd_observe_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    capture = capture_target(args)
    if capture["status"] != "SUCCESS":
        return capture
    return command_result("SUCCESS", "observe.screenshot", "Screenshot captured.", data=capture["data"])


def cmd_observe_ocr(args: dict[str, Any]) -> dict[str, Any]:
    capture = capture_target(args)
    if capture["status"] != "SUCCESS":
        return capture
    return command_result(
        "UNAVAILABLE", "observe.ocr",
        "OCR is deferred; screenshot was captured for visual fallback.",
        data=capture["data"],
    )


def execute_planner_action(action_name: str, action_args: dict[str, Any], root_args: dict[str, Any]) -> dict[str, Any]:
    title = action_args.get("title") or root_args.get("title") or TARGET_TITLE
    if action_name == "guided_grid_click":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        payload.setdefault("auto_follow_up_capture", False)
        return cmd_recovery_guided_grid_click(payload)
    if action_name == "click_point":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        payload.setdefault("target", action_args.get("target") or "desktop")
        payload.setdefault("coordinate_mode", action_args.get("coordinate_mode") or "screen")
        return cmd_input_click_point(payload)
    if action_name == "click_candidate":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        return cmd_input_click_candidate(payload)
    if action_name == "invoke_candidate":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        return cmd_input_invoke_candidate(payload)
    if action_name == "set_candidate_value":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        return cmd_input_set_candidate_value(payload)
    if action_name == "expand_candidate":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        return cmd_input_expand_candidate(payload)
    if action_name == "select_candidate":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        return cmd_input_select_candidate(payload)
    if action_name == "hotkey":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        payload.setdefault("target", action_args.get("target") or "desktop")
        return cmd_input_hotkey(payload)
    if action_name == "type_text":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        payload.setdefault("target", action_args.get("target") or "desktop")
        return cmd_input_type_text(payload)
    if action_name == "paste_text":
        payload = planner_action_payload(action_args, root_args)
        payload.setdefault("title", title)
        payload.setdefault("target", action_args.get("target") or "desktop")
        return cmd_input_paste_text(payload)
    if action_name == "wait":
        seconds = float(action_args.get("seconds") or root_args.get("seconds") or 0.25)
        cancellable_sleep(max(0.0, seconds))
        return command_result("SUCCESS", "planner.step.wait", "Wait completed.", data={"seconds": seconds})
    return command_result(
        "ERROR",
        "planner.step",
        f"Unsupported planner action {action_name!r}.",
        data={"supported_actions": ["guided_grid_click", "click_candidate", "invoke_candidate", "set_candidate_value", "expand_candidate", "select_candidate", "click_point", "hotkey", "type_text", "paste_text", "wait"]},
    )


def planner_action_payload(action_args: dict[str, Any], root_args: dict[str, Any]) -> dict[str, Any]:
    payload = dict(action_args)
    for key in ("preview", "confirm_token"):
        if key not in payload and key in root_args:
            payload[key] = root_args[key]
    if "allow_vm_control" not in payload and "allow_vm_control" in root_args:
        payload["allow_vm_control"] = bool(root_args.get("allow_vm_control"))
    return payload


def capture_planner_step_baseline(
    root_args: dict[str, Any],
    action_name: str,
    action_args: dict[str, Any],
) -> dict[str, Any]:
    check = verify_planner_step_request_freshness(root_args, action_name, action_args)
    if check["status"] == "FAILED":
        return {"status": "FAILED", "result": check["result"]}
    if check["status"] == "NEEDS_PLANNER":
        return {"status": "NEEDS_PLANNER", "result": check["result"]}
    watch_enabled = bool(root_args.get("wait_for_change", True))
    observe_after = root_args.get("observe_after") if isinstance(root_args.get("observe_after"), dict) else {}
    if not bool(observe_after.get("wait_for_change", watch_enabled)):
        return {"status": "SKIPPED", "check": check.get("data")}
    baseline = capture_target(build_follow_up_capture_args(root_args, action_name, action_args, make_request_id()), with_grid=False)
    if baseline["status"] != "SUCCESS":
        return {
            "status": "FAILED",
            "result": command_result(
                "UNCERTAIN",
                "planner.step",
                "Planner step could not capture a pre-action baseline.",
                data={"capture_failure": strip_internal(baseline), "request_check": check.get("data")},
            ),
        }
    return {"status": "SUCCESS", "capture": baseline, "check": check.get("data")}


def verify_planner_step_request_freshness(
    root_args: dict[str, Any],
    action_name: str,
    action_args: dict[str, Any],
) -> dict[str, Any]:
    if action_name == "guided_grid_click":
        return {"status": "SKIPPED", "data": {"skipped": True, "reason": "guided_grid_click already performs its own stale-screen guard."}}
    planner_request_id = resolve_planner_step_request_id(root_args, action_args)
    if not planner_request_id:
        return {"status": "SKIPPED", "data": {"skipped": True, "reason": "No planner request id was available for pre-action freshness verification."}}
    planner_request = load_planner_request(planner_request_id)
    if planner_request is None:
        return {
            "status": "FAILED",
            "result": command_result(
                "UNCERTAIN",
                "planner.step",
                f"Planner step expected request {planner_request_id!r} for freshness verification, but it was not found.",
                data={"planner_request_id": planner_request_id},
                next_allowed_actions=["planner.fast_observe", "recovery.capture_grid", "helper.bridge_status"],
            ),
        }
    fresh_capture = capture_target(build_capture_args_from_planner_request(planner_request, make_request_id()), with_grid=False)
    if fresh_capture["status"] != "SUCCESS":
        return {
            "status": "FAILED",
            "result": command_result(
                "UNCERTAIN",
                "planner.step",
                "Planner step could not refresh the current screen before acting.",
                data={
                    "planner_request_id": planner_request_id,
                    "capture_failure": strip_internal(fresh_capture),
                },
                next_allowed_actions=["planner.fast_observe", "recovery.capture_grid", "helper.bridge_status"],
            ),
        }
    freshness = verify_planner_screen_unchanged(planner_request, fresh_capture, root_args, command="planner.step")
    if freshness["status"] != "SUCCESS":
        return {"status": "NEEDS_PLANNER", "result": freshness}
    return {
        "status": "SUCCESS",
        "data": {
            "planner_request_id": planner_request_id,
            "verification": freshness.get("data") or {},
            "fresh_capture_request_id": (fresh_capture.get("data") or {}).get("request_id"),
            "fresh_capture_raw_screenshot_path": (fresh_capture.get("data") or {}).get("raw_screenshot_path"),
        },
    }


def resolve_planner_step_request_id(root_args: dict[str, Any], action_args: dict[str, Any]) -> str:
    for key in ("planner_request_id", "based_on_request_id", "observation_request_id"):
        value = action_args.get(key)
        if value:
            return str(value)
    for key in ("planner_request_id", "based_on_request_id", "observation_request_id"):
        value = root_args.get(key)
        if value:
            return str(value)
    if not bool(root_args.get("use_last_observation_request", True)):
        return ""
    memory = load_planner_working_memory()
    return str(memory.get("last_observation_request_id") or "")


def build_capture_args_from_planner_request(planner_request: dict[str, Any], request_id: str) -> dict[str, Any]:
    target = str(planner_request.get("target") or "desktop")
    capture_args: dict[str, Any] = {
        "request_id": request_id,
        "target": target,
        "title": str(planner_request.get("title") or TARGET_TITLE),
    }
    target_window = planner_request.get("target_window") if isinstance(planner_request.get("target_window"), dict) else {}
    hwnd = int(target_window.get("hwnd") or 0)
    if hwnd:
        capture_args["hwnd"] = hwnd
    return capture_args


def build_follow_up_capture_args(
    root_args: dict[str, Any],
    action_name: str,
    action_args: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    observe_after = root_args.get("observe_after") if isinstance(root_args.get("observe_after"), dict) else {}
    default_target = str(root_args.get("follow_up_target") or root_args.get("target") or "foreground_window").strip().lower()
    target_mode = str(observe_after.get("target") or default_target).strip().lower()
    columns = max(1, int(observe_after.get("columns") or root_args.get("columns") or 12))
    rows = max(1, int(observe_after.get("rows") or root_args.get("rows") or 8))
    reason = str(observe_after.get("reason") or f"Planner step {action_name} completed; planner needs the next visual state.")
    capture_args: dict[str, Any] = {
        "request_id": request_id,
        "columns": columns,
        "rows": rows,
        "reason": reason,
        "expected_result": str(observe_after.get("expected_result") or ""),
        "failed_command": "planner.step",
    }
    for key in ("detail", "include_images", "image_max_edge"):
        if key in root_args:
            capture_args[key] = root_args[key]
    if target_mode == "desktop":
        capture_args["target"] = "desktop"
        capture_title = str(observe_after.get("title") or root_args.get("title") or TARGET_TITLE)
    elif target_mode in {"target_window", "target_monitor"}:
        capture_args["target"] = target_mode
        capture_title = str(observe_after.get("title") or action_args.get("title") or root_args.get("title") or TARGET_TITLE)
        hwnd = int(root_args.get("follow_up_hwnd") or action_args.get("hwnd") or 0)
        if hwnd:
            capture_args["hwnd"] = hwnd
    elif target_mode in {"current_dialog", "current_dialog_monitor"}:
        capture_args["target"] = target_mode
        capture_title = str(observe_after.get("title") or action_args.get("title") or root_args.get("title") or "")
        dialog = resolve_current_dialog_window({**root_args, **action_args, **observe_after, "target": target_mode})
        if dialog is not None:
            capture_args["hwnd"] = dialog.hwnd
    else:
        foreground = get_foreground_window_snapshot()
        foreground_hwnd = int(foreground.get("hwnd") or 0)
        foreground_window = window_info_from_hwnd(foreground_hwnd) if foreground_hwnd else None
        if foreground_window is None or foreground_window.minimized or foreground_window.width <= 0 or foreground_window.height <= 0:
            capture_args["target"] = "desktop"
            capture_title = str(observe_after.get("title") or root_args.get("title") or TARGET_TITLE)
        else:
            capture_args["target"] = target_mode if target_mode.endswith("_monitor") else "target_window"
            capture_args["hwnd"] = foreground_window.hwnd
            capture_title = str(observe_after.get("title") or foreground_window.title or root_args.get("title") or TARGET_TITLE)
    capture_args["title"] = capture_title
    return capture_args


def capture_planner_follow_up(
    root_args: dict[str, Any],
    action_name: str,
    action_args: dict[str, Any],
    action_result: dict[str, Any],
    *,
    baseline_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observe_after = root_args.get("observe_after") if isinstance(root_args.get("observe_after"), dict) else {}
    request_id = str(observe_after.get("request_id") or make_request_id())
    question = str(observe_after.get("question") or "What single safe next action should I try from this new state?")
    watch_result = wait_for_planner_follow_up_change(root_args, action_name, action_args, baseline_capture)
    capture_args = build_follow_up_capture_args(root_args, action_name, action_args, request_id)
    capture = capture_target(capture_args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return command_result(
            "UNCERTAIN",
            "planner.step",
            "Planner action completed, but follow-up capture failed.",
            data={"action_result": strip_internal(action_result), "capture_failure": strip_internal(capture)},
        )
    capture_data = capture["data"]
    planner_request = create_planner_request(
        capture_data,
        capture_args,
        failed_command="planner.step",
        question=question,
    )
    capture_data["planner_request_path"] = planner_request["planner_request_path"]
    capture_data["planner_request"] = planner_request
    capture_data["source_action"] = action_name
    capture_data["source_action_status"] = str(action_result.get("status") or "")
    capture_data["foreground_after_capture"] = get_foreground_window_snapshot()
    capture_data["change_watch"] = watch_result
    return command_result("SUCCESS", "planner.step.capture", "Follow-up planner observation captured.", data=capture_data)


def wait_for_planner_follow_up_change(
    root_args: dict[str, Any],
    action_name: str,
    action_args: dict[str, Any],
    baseline_capture: dict[str, Any] | None,
) -> dict[str, Any]:
    observe_after = root_args.get("observe_after") if isinstance(root_args.get("observe_after"), dict) else {}
    enabled = bool(observe_after.get("wait_for_change", root_args.get("wait_for_change", True)))
    expected_change = expected_change_from_args(root_args)
    if not enabled and not expected_change:
        return {"enabled": False, "skipped": True, "reason": "Change watch disabled for this planner step."}
    if baseline_capture is None or baseline_capture.get("status") != "SUCCESS":
        return {"enabled": True, "skipped": True, "reason": "No successful pre-action baseline was available for change watch."}
    if expected_change:
        return wait_for_expected_change(root_args, action_name, action_args, baseline_capture, expected_change)
    settle_seconds = max(0.0, float(observe_after.get("change_settle_seconds") or root_args.get("change_settle_seconds") or 0.15))
    timeout_seconds = max(0.0, float(observe_after.get("change_timeout_seconds") or root_args.get("change_timeout_seconds") or 2.5))
    poll_seconds = max(0.05, float(observe_after.get("change_poll_seconds") or root_args.get("change_poll_seconds") or 0.35))
    if settle_seconds:
        cancellable_sleep(settle_seconds)
    deadline = time.time() + timeout_seconds
    polls: list[dict[str, Any]] = []
    attempts = 0
    last_compare: dict[str, Any] | None = None
    while True:
        attempts += 1
        raw_capture = capture_target(build_follow_up_capture_args(root_args, action_name, action_args, f"{make_request_id()}_watch"), with_grid=False)
        if raw_capture["status"] == "SUCCESS":
            compare = compare_follow_up_state_change(baseline_capture, raw_capture, root_args)
            last_compare = compare
            poll_record = {
                "attempt": attempts,
                "changed": bool(compare.get("changed")),
                "reasons": compare.get("reasons") or [],
                "screenshot_path": (raw_capture.get("data") or {}).get("raw_screenshot_path"),
            }
            polls.append(poll_record)
            if compare.get("changed"):
                return {
                    "enabled": True,
                    "changed": True,
                    "attempts": attempts,
                    "settle_seconds": settle_seconds,
                    "timeout_seconds": timeout_seconds,
                    "poll_seconds": poll_seconds,
                    "reasons": compare.get("reasons") or [],
                    "comparison": compare,
                    "polls": polls[-6:],
                }
        else:
            polls.append({"attempt": attempts, "capture_status": raw_capture.get("status"), "message": raw_capture.get("message")})
        if time.time() >= deadline:
            break
        cancellable_sleep(poll_seconds)
    return {
        "enabled": True,
        "changed": False,
        "attempts": attempts,
        "settle_seconds": settle_seconds,
        "timeout_seconds": timeout_seconds,
        "poll_seconds": poll_seconds,
        "comparison": last_compare or {},
        "polls": polls[-6:],
        "reason": "No visible or backend state change was detected before the change-watch timeout.",
    }




def capture_file_evidence(path: Path, *, content: bool = False) -> dict[str, Any]:
    """Bounded read-only evidence; never return file contents or infer a transition."""
    check_command_cancelled()
    data = {"path": str(path)}
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return {**data, "exists": False}
    except OSError as exc:
        return {**data, "error": str(exc)}
    try:
        info = path.stat()
    except OSError as exc:
        # A present directory entry with an unreadable/broken target is not
        # evidence of deletion, and cannot close a rename transition.
        return {**data, "error": str(exc)}
    data.update(exists=True, size_bytes=info.st_size, modified_epoch=info.st_mtime)
    if not content:
        return data
    if stat_module.S_ISLNK(entry.st_mode) or not stat_module.S_ISREG(info.st_mode):
        return {**data, "error": "Content evidence requires a regular file, not a link or directory."}
    if info.st_size > MAX_CONTENT_BYTES:
        return {**data, "error": f"Content evidence exceeds {MAX_CONTENT_BYTES} byte limit."}

    def signature(item):
        # Windows path stat and fstat can report different ctime meanings.
        # Compare ctime only within its own source; birth time is comparable.
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, getattr(item, "st_birthtime_ns", None)

    try:
        digest = hashlib.sha256()
        read_bytes = 0
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if signature(info) != signature(opened):
                return {**data, "error": "File changed before content read."}
            while True:
                check_command_cancelled()
                chunk = stream.read(min(65536, MAX_CONTENT_BYTES + 1 - read_bytes))
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > MAX_CONTENT_BYTES:
                    return {**data, "error": "File grew beyond content evidence limit."}
                digest.update(chunk)
            check_command_cancelled()
            final_handle = os.fstat(stream.fileno())
            final_path = path.stat()
            if (read_bytes != info.st_size or signature(info) != signature(final_handle) or signature(info) != signature(final_path)
                    or opened.st_ctime_ns != final_handle.st_ctime_ns or info.st_ctime_ns != final_path.st_ctime_ns):
                return {**data, "error": "File changed during content read."}
        # Some filesystems cannot supply a persistent identity. Content state can
        # still be checked, but rename evaluation refuses absent identity.
        identity = [info.st_dev, info.st_ino] if info.st_ino else None
        return {**data, "sha256": digest.hexdigest(), "identity": identity}
    except (OSError, ValueError) as exc:
        return {**data, "error": str(exc)}


def capture_effect_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    """Read only the independent state required by the declared effect."""
    check_command_cancelled()
    kind = canonical_effect_type(spec)
    try:
        if kind == "file_renamed":
            old = resolve_file_path_from_args({"path": spec["old_path"]})
            new = resolve_file_path_from_args({"path": spec["new_path"]})
            if old is None or new is None or os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new)):
                return {"error": "Distinct old and new file paths are required."}
            evidence = {"old": capture_file_evidence(old, content=True), "new": capture_file_evidence(new, content=True)}
            if any(item.get("error") for item in evidence.values()):
                evidence["error"] = "Rename file evidence unavailable."
            return evidence
        if kind.startswith("file_"):
            path = resolve_file_path_from_args(spec)
            if path is None:
                return {"error": "File path unavailable."}
            return capture_file_evidence(path, content=kind in {"file_content", "file_saved"})
        if kind in {"window_close", "dialog_close", "title_change"}:
            hwnd = int(spec["hwnd"])
            alive = bool(ctypes.windll.user32.IsWindow(hwnd))
            if not alive:
                return {"hwnd": hwnd, "alive": False}
            window = window_info_from_hwnd(hwnd)
            pid = get_window_process_id(hwnd)
            if window is None or not pid:
                return {"hwnd": hwnd, "alive": True, "error": "Window identity/title observation unavailable."}
            return {"hwnd": hwnd, "alive": True, "title": window.title, "pid": pid}
        if kind == "foreground_change":
            foreground = get_foreground_window_snapshot()
            return foreground if foreground.get("hwnd") else {"error": "Foreground unavailable."}
        if kind in {"new_window", "dialog_open"}:
            windows = list_windows()
            if not windows:
                return {"error": "No window enumeration evidence available."}
            return {"windows": [{"hwnd": w.hwnd, "title": w.title,
                      "process_name": get_process_name_for_hwnd(w.hwnd)} for w in windows]}
        return {}
    except (OSError, ValueError, TypeError) as exc:
        return {"error": str(exc)}


def wait_for_expected_change(
    root_args: dict[str, Any], action_name: str, action_args: dict[str, Any],
    baseline_capture: dict[str, Any], expected_change: dict[str, Any],
) -> dict[str, Any]:
    errors = validate_expected_change(expected_change)
    if errors:
        return expected_change_result(expected_change, False, 0, 0, 0, "invalid_expected_change", [{"errors": errors}])
    kind = canonical_effect_type(expected_change)
    max_wait_ms = expected_change.get("max_wait_ms", expected_change.get("timeout_ms", 450))
    poll_ms = expected_change.get("poll_ms", 75)
    deadline = time.monotonic() + max_wait_ms / 1000
    baseline_data = baseline_capture.get("data") or {}
    before = baseline_data.get("effect_snapshot")
    # Standalone state predicates require no action-boundary snapshot.
    if before is None and kind in {"file_exists", "file_absent", "file_content"}:
        before = {}
    if before is None and kind not in VISUAL_EFFECTS:
        return expected_change_result(expected_change, False, 0, max_wait_ms, poll_ms, "pre_action_evidence_missing", [])
    polls = []
    previous = baseline_capture
    changed_once = False
    stable_count = 0
    stable_frames = expected_change.get("stable_frames", 2)
    baseline_hwnd = int((baseline_data.get("target_window") or baseline_data.get("foreground_window") or {}).get("hwnd") or 0)
    while True:
        check_command_cancelled()
        matched = False
        reason = "effect_not_observed"
        evidence = {}
        if kind in VISUAL_EFFECTS:
            fresh = capture_target(build_follow_up_capture_args(root_args, action_name, action_args, f"{make_request_id()}_effect"), with_grid=False)
            if fresh["status"] == "SUCCESS":
                compare = compare_capture_images(previous, fresh, root_args)
                valid = not compare.get("comparison_error") and "matched" in compare
                if kind == "same_window_stable":
                    current_hwnd = int(((fresh.get("data") or {}).get("target_window") or (fresh.get("data") or {}).get("foreground_window") or {}).get("hwnd") or 0)
                    previous_hwnd = int(((previous.get("data") or {}).get("target_window") or (previous.get("data") or {}).get("foreground_window") or {}).get("hwnd") or 0)
                    valid = valid and bool(baseline_hwnd and current_hwnd == previous_hwnd == baseline_hwnd)
                stable = valid and compare["matched"] is True
                changed_once = changed_once or (valid and compare["matched"] is False)
                stable_count = stable_count + 1 if stable else 0
                if kind == "any_change":
                    matched = changed_once
                elif kind == "content_loaded":
                    matched = changed_once and stable_count >= stable_frames
                else:
                    matched = stable_count >= stable_frames
                if kind == "same_window_stable":
                    hwnd = int(((fresh.get("data") or {}).get("target_window") or (fresh.get("data") or {}).get("foreground_window") or {}).get("hwnd") or 0)
                    matched = matched and bool(baseline_hwnd and hwnd == baseline_hwnd)
                evidence = {"comparison": compare, "stable_count": stable_count, "changed_once": changed_once}
                reason = "visual_change_observed" if kind == "any_change" else "visual_stability_observed"
                previous = fresh
            else:
                stable_count = 0
                evidence = {"error": "Visual capture unavailable.", "capture_status": fresh["status"]}
        else:
            after = capture_effect_snapshot(expected_change)
            if kind == "dialog_open" and not after.get("error"):
                context = build_active_context_packet(str(action_args.get("title") or root_args.get("title") or ""))
                dialog = context.get("current_dialog") or {}
                after["windows"] = [w for w in after.get("windows", []) if w["hwnd"] == dialog.get("hwnd")]
            matched, reason = evaluate_effect(expected_change, before, after)
            evidence = {"before": before, "after": after}
        polls.append({"attempt": len(polls)+1, "matched": bool(matched), "reason": reason, **evidence})
        if matched or time.monotonic() >= deadline:
            result = expected_change_result(expected_change, bool(matched), len(polls), max_wait_ms, poll_ms,
                                            reason if matched else "expected_change_timeout", polls)
            result["verification"] = effect_verdict(expected_change, result)
            return result
        cancellable_sleep(min(poll_ms / 1000, max(0, deadline-time.monotonic())))


def find_expected_window(expected_change: dict[str, Any], *, exclude_hwnds: set[int] | None = None) -> WindowInfo | None:
    exclude_hwnds = exclude_hwnds or set()
    title_contains = str(expected_change.get("title_contains") or expected_change.get("title") or "").strip().lower()
    process_contains = str(expected_change.get("process_contains") or expected_change.get("process_name") or "").strip().lower()
    for window in list_windows():
        if int(window.hwnd) in exclude_hwnds:
            continue
        if title_contains and title_contains not in window.title.lower():
            continue
        if process_contains and process_contains not in get_process_name_for_hwnd(window.hwnd).lower():
            continue
        return window
    return None


def compare_capture_images(
    baseline_capture: dict[str, Any],
    fresh_capture: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    baseline_data = baseline_capture.get("data") if isinstance(baseline_capture.get("data"), dict) else {}
    return compare_planner_screen({"raw_screenshot_path": baseline_data.get("raw_screenshot_path")}, fresh_capture, args).get("data") or {}


def compare_follow_up_state_change(
    baseline_capture: dict[str, Any],
    fresh_capture: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    baseline_data = baseline_capture.get("data") if isinstance(baseline_capture.get("data"), dict) else {}
    fresh_data = fresh_capture.get("data") if isinstance(fresh_capture.get("data"), dict) else {}
    reasons: list[str] = []
    baseline_foreground = baseline_data.get("foreground_window") if isinstance(baseline_data.get("foreground_window"), dict) else {}
    fresh_foreground = fresh_data.get("foreground_window") if isinstance(fresh_data.get("foreground_window"), dict) else {}
    if int(baseline_foreground.get("hwnd") or 0) != int(fresh_foreground.get("hwnd") or 0):
        reasons.append("foreground_hwnd_changed")
    elif str(baseline_foreground.get("title") or "") != str(fresh_foreground.get("title") or ""):
        reasons.append("foreground_title_changed")
    baseline_context = baseline_data.get("active_context") if isinstance(baseline_data.get("active_context"), dict) else {}
    fresh_context = fresh_data.get("active_context") if isinstance(fresh_data.get("active_context"), dict) else {}
    baseline_active = baseline_context.get("active_window") if isinstance(baseline_context.get("active_window"), dict) else {}
    fresh_active = fresh_context.get("active_window") if isinstance(fresh_context.get("active_window"), dict) else {}
    if int(baseline_active.get("hwnd") or 0) != int(fresh_active.get("hwnd") or 0):
        reasons.append("active_window_changed")
    elif str(baseline_active.get("title") or "") != str(fresh_active.get("title") or ""):
        reasons.append("active_window_title_changed")
    if bool(baseline_context.get("dialog_open")) != bool(fresh_context.get("dialog_open")):
        reasons.append("dialog_state_changed")
    baseline_candidates = baseline_context.get("child_candidates") if isinstance(baseline_context.get("child_candidates"), list) else []
    fresh_candidates = fresh_context.get("child_candidates") if isinstance(fresh_context.get("child_candidates"), list) else []
    if len(baseline_candidates) != len(fresh_candidates):
        reasons.append("candidate_count_changed")
    image_compare = compare_planner_screen(
        {"raw_screenshot_path": baseline_data.get("raw_screenshot_path")},
        fresh_capture,
        args,
    )
    image_details = image_compare.get("data") if isinstance(image_compare.get("data"), dict) else {}
    comparison_error = bool(image_details.get("comparison_error")) or "matched" not in image_details
    image_changed = not comparison_error and image_details.get("matched") is False
    if image_changed:
        reasons.append("image_changed")
    return {
        "changed": bool(reasons),
        "reasons": reasons,
        "image_comparison": image_details,
        "baseline_foreground": baseline_foreground,
        "fresh_foreground": fresh_foreground,
    }


def find_windows_matching_args(args: dict[str, Any]) -> list[WindowInfo]:
    exact_title = str(args.get("exact_title") or args.get("title") or "").strip()
    title_contains = str(args.get("title_contains") or "").strip()
    title_excludes = normalize_string_list(args.get("title_excludes"))
    min_width = int(args.get("min_width") or 0)
    min_height = int(args.get("min_height") or 0)
    allow_minimized = bool(args.get("allow_minimized", False))
    if exact_title:
        candidate_windows = [window for window in list_windows() if window.title == exact_title]
    elif title_contains:
        candidate_windows = list_windows(partial_title=title_contains)
    else:
        candidate_windows = list_windows()
    return [
        window
        for window in candidate_windows
        if window_matches_expectation(
            window,
            title_excludes=title_excludes,
            min_width=min_width,
            min_height=min_height,
            allow_minimized=allow_minimized,
        )
    ]


WINDOW_TITLE_IDENTITY_KEYS = ("exact_title", "title", "title_contains")


def has_window_title_identity(args: dict[str, Any]) -> bool:
    return any(str(args.get(key) or "").strip() for key in WINDOW_TITLE_IDENTITY_KEYS)


def with_default_target_title(args: dict[str, Any]) -> dict[str, Any]:
    """Default to the Simon Target title only when no title identity was given.

    The CLI no longer injects ``--title "Simon Target"`` into every payload, so
    window-resolving commands apply this default themselves. Any explicit
    ``exact_title``, ``title`` or ``title_contains`` wins.
    """
    if has_window_title_identity(args):
        return args
    return {**args, "title": TARGET_TITLE}


def find_best_window_for_args(args: dict[str, Any]) -> WindowInfo | None:
    args = with_default_target_title(args)
    matches = find_windows_matching_args(args)
    if not matches:
        title = str(args.get("title") or "").strip()
        if title:
            matches = list_windows(partial_title=title)
    if not matches:
        return None
    visible = [item for item in matches if not item.minimized and item.width > 100 and item.height > 100]
    return visible[0] if visible else matches[0]


def resolve_current_dialog_window(args: dict[str, Any]) -> WindowInfo | None:
    hwnd = int(
        args.get("dialog_hwnd")
        or args.get("current_dialog_hwnd")
        or args.get("hwnd")
        or ((args.get("current_dialog") or {}).get("hwnd") if isinstance(args.get("current_dialog"), dict) else 0)
        or 0
    )
    if hwnd:
        window = window_info_from_hwnd(hwnd)
        if window is not None:
            children = enumerate_child_controls(window.hwnd)
            if looks_like_dialog(window, children) or bool(args.get("allow_non_dialog_hwnd")):
                return window
    memory = load_planner_working_memory()
    memory_dialog = memory.get("current_dialog") if isinstance(memory.get("current_dialog"), dict) else {}
    memory_hwnd = int(memory_dialog.get("hwnd") or 0)
    if memory_hwnd:
        window = window_info_from_hwnd(memory_hwnd)
        if window is not None:
            children = enumerate_child_controls(window.hwnd)
            if looks_like_dialog(window, children):
                return window
    foreground = get_foreground_window_snapshot()
    foreground_hwnd = int(foreground.get("hwnd") or 0)
    foreground_window = window_info_from_hwnd(foreground_hwnd) if foreground_hwnd else None
    if foreground_window is not None:
        children = enumerate_child_controls(foreground_window.hwnd)
        if looks_like_dialog(foreground_window, children):
            return foreground_window
    return None


def resolve_context_window_for_args(args: dict[str, Any]) -> WindowInfo | None:
    target = str(args.get("target") or "").strip().lower()
    if target in {"current_dialog", "current_dialog_monitor"}:
        return resolve_current_dialog_window(args)
    hwnd = int(args.get("hwnd") or args.get("window_hwnd") or 0)
    if hwnd:
        window = window_info_from_hwnd(hwnd)
        if window is not None:
            return window
    if target in {"target_window", "target_monitor", "current_window"}:
        return resolve_working_window(args)
    foreground = get_foreground_window_snapshot()
    foreground_hwnd = int(foreground.get("hwnd") or 0)
    return window_info_from_hwnd(foreground_hwnd) if foreground_hwnd else None


def resolve_working_window(args: dict[str, Any]) -> WindowInfo | None:
    hwnd = int(
        args.get("hwnd")
        or args.get("window_hwnd")
        or ((args.get("working_window") or {}).get("hwnd") if isinstance(args.get("working_window"), dict) else 0)
        or ((args.get("target_window") or {}).get("hwnd") if isinstance(args.get("target_window"), dict) else 0)
        or 0
    )
    if hwnd:
        window = window_info_from_hwnd(hwnd)
        if window is not None:
            return window
    window = find_best_window_for_args(args)
    if window is not None:
        return window
    memory = load_planner_working_memory()
    current_app = memory.get("current_app") if isinstance(memory.get("current_app"), dict) else {}
    memory_hwnd = int(current_app.get("hwnd") or 0)
    if memory_hwnd:
        window = window_info_from_hwnd(memory_hwnd)
        if window is not None:
            return window
    memory_title = str(current_app.get("title") or "").strip()
    if memory_title:
        matches = list_windows(partial_title=memory_title)
        if matches:
            visible = [item for item in matches if not item.minimized and item.width > 100 and item.height > 100]
            return visible[0] if visible else matches[0]
    return None


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def window_matches_expectation(
    window: WindowInfo,
    *,
    title_excludes: list[str],
    min_width: int,
    min_height: int,
    allow_minimized: bool,
) -> bool:
    title_lower = window.title.lower()
    if any(excluded.lower() in title_lower for excluded in title_excludes):
        return False
    if not allow_minimized and window.minimized:
        return False
    if min_width and window.width < min_width:
        return False
    if min_height and window.height < min_height:
        return False
    return True


def build_window_verify_planner_result(
    args: dict[str, Any],
    verification_data: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    request_id = str(args.get("request_id") or make_request_id())
    capture_args = {
        "target": "desktop",
        "title": str(args.get("title") or args.get("exact_title") or args.get("title_contains") or TARGET_TITLE),
        "request_id": request_id,
        "columns": int(args.get("columns") or 12),
        "rows": int(args.get("rows") or 8),
        "reason": args.get("reason") or message,
        "expected_result": args.get("expected_result") or "",
        "failure_type": "WRONG_STATE",
        "verification": {
            "type": "window_state",
            "expected_exact_title": str(args.get("exact_title") or args.get("title") or ""),
            "expected_title_contains": str(args.get("title_contains") or ""),
            "require_foreground": bool(args.get("require_foreground")),
        },
    }
    capture = capture_target(capture_args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return command_result(
            "FAILED",
            "verify.window_state",
            f"{message} Desktop capture also failed.",
            data={"verification": verification_data, "capture_failure": capture},
        )
    data = capture["data"]
    planner_request = create_planner_request(
        data,
        capture_args,
        failed_command="verify.window_state",
        question="The helper could not verify or restore the expected window state. Diagnose the desktop state and suggest one safe next step.",
    )
    data["planner_request_path"] = planner_request["planner_request_path"]
    data["planner_request"] = planner_request
    data["verification"] = verification_data
    return command_result(
        "NEEDS_PLANNER",
        "verify.window_state",
        message,
        data=data,
        next_allowed_actions=["recovery.guided_grid_click", "recovery.guided_grid_crop", "input.action_sequence", "helper.bridge_status"],
    )


def cmd_verify_window_state(args: dict[str, Any]) -> dict[str, Any]:
    args = with_default_target_title(args)
    require_foreground = bool(args.get("require_foreground"))
    focus_if_needed = bool(args.get("focus_if_needed", require_foreground))
    screenshot_on_failure = bool(args.get("screenshot_on_failure", True))
    allow_window_anchor = bool(args.get("allow_window_anchor", True))
    matches = find_windows_matching_args(args)
    foreground = get_foreground_window_snapshot()
    match_payloads = [window_payload(window) for window in matches]
    verification_data: dict[str, Any] = {
        "matched_windows": match_payloads,
        "match_count": len(matches),
        "foreground_window": foreground,
        "expected_exact_title": str(args.get("exact_title") or args.get("title") or ""),
        "expected_title_contains": str(args.get("title_contains") or ""),
        "title_excludes": normalize_string_list(args.get("title_excludes")),
        "min_width": int(args.get("min_width") or 0),
        "min_height": int(args.get("min_height") or 0),
        "allow_minimized": bool(args.get("allow_minimized", False)),
        "require_foreground": require_foreground,
        "focus_if_needed": focus_if_needed,
    }
    if not matches:
        if screenshot_on_failure:
            return build_window_verify_planner_result(args, verification_data, "Expected window is not open.")
        return command_result("UNCERTAIN", "verify.window_state", "Expected window is not open.", data=verification_data)
    matching_hwnds = {int(window.hwnd) for window in matches}
    if require_foreground and int(foreground.get("hwnd") or 0) not in matching_hwnds:
        if focus_if_needed:
            preferred = next((window for window in matches if not window.minimized and window.width > 100 and window.height > 100), matches[0])
            focus_result = ensure_window_foreground(window_payload(preferred), preferred.title)
            verification_data["focus_attempted"] = True
            verification_data["focus_result"] = focus_result
            if focus_result["status"] == "SUCCESS":
                refreshed_matches = find_windows_matching_args(args)
                refreshed_payloads = [window_payload(window) for window in refreshed_matches]
                refreshed_foreground = focus_result["data"].get("foreground_window") or get_foreground_window_snapshot()
                verification_data["foreground_window"] = refreshed_foreground
                verification_data["matched_windows"] = refreshed_payloads
                verification_data["match_count"] = len(refreshed_matches)
                refreshed_hwnds = {int(window.hwnd) for window in refreshed_matches}
                if int(refreshed_foreground.get("hwnd") or 0) in refreshed_hwnds and refreshed_payloads:
                    verification_data["matched_window"] = refreshed_payloads[0]
                    return command_result("SUCCESS", "verify.window_state", "Expected window is open and focused.", data=verification_data)
                if allow_window_anchor and refreshed_payloads:
                    verification_data["matched_window"] = refreshed_payloads[0]
                    verification_data["foreground_unverified"] = True
                    verification_data["window_anchor_ready"] = True
                    return command_result(
                        "SUCCESS",
                        "verify.window_state",
                        "Expected window is open; using working-window anchor because foreground could not be confirmed.",
                        data=verification_data,
                    )
                if screenshot_on_failure:
                    return build_window_verify_planner_result(args, verification_data, "Expected window focus changed, but the focused window does not meet the expected ready-state rules.")
                return command_result("UNCERTAIN", "verify.window_state", "Expected window focus changed, but the focused window does not meet the expected ready-state rules.", data=verification_data)
            if allow_window_anchor:
                verification_data["matched_window"] = window_payload(preferred)
                verification_data["foreground_unverified"] = True
                verification_data["window_anchor_ready"] = True
                return command_result(
                    "SUCCESS",
                    "verify.window_state",
                    "Expected window is open; using working-window anchor because foreground restore could not be confirmed.",
                    data=verification_data,
                )
            if screenshot_on_failure:
                return build_window_verify_planner_result(args, verification_data, "Expected window is open but could not be restored to foreground.")
            return command_result("UNCERTAIN", "verify.window_state", "Expected window is open but could not be restored to foreground.", data=verification_data)
        if allow_window_anchor and match_payloads:
            verification_data["matched_window"] = match_payloads[0]
            verification_data["foreground_unverified"] = True
            verification_data["window_anchor_ready"] = True
            return command_result(
                "SUCCESS",
                "verify.window_state",
                "Expected window is open; using working-window anchor because foreground could not be confirmed.",
                data=verification_data,
            )
        if screenshot_on_failure:
            return build_window_verify_planner_result(args, verification_data, "Expected window is open but not foreground.")
        return command_result("UNCERTAIN", "verify.window_state", "Expected window is open but not foreground.", data=verification_data)
    verification_data["matched_window"] = match_payloads[0]
    return command_result(
        "SUCCESS",
        "verify.window_state",
        "Expected window state verified.",
        data=verification_data,
    )


def cmd_verify_file_state(args: dict[str, Any]) -> dict[str, Any]:
    errors = validate_file_selector(args)
    if "expected_sha256" in args:
        errors.extend(validate_sha256(args["expected_sha256"]))
    if errors:
        return command_result("ERROR", "verify.file_state", "Invalid file expectation.", data={"errors": errors})
    try:
        path = resolve_file_path_from_args(args)
    except (OSError, ValueError) as exc:
        return command_result("UNCERTAIN", "verify.file_state", "File path resolution unavailable.", data={"error": str(exc)})
    if path is None:
        return command_result("ERROR", "verify.file_state", "A file path is required.")
    should_exist = bool(args.get("should_exist", True))
    data = {**capture_file_evidence(path, content="expected_sha256" in args), "should_exist": should_exist}
    if data.get("error"):
        return command_result("UNCERTAIN", "verify.file_state", "File evidence unavailable.", data=data)
    exists = data["exists"]
    min_size = args.get("min_size_bytes")
    if min_size is not None:
        data["min_size_bytes"] = int(min_size)
        data["min_size_met"] = bool(exists and int(data.get("size_bytes") or 0) >= int(min_size))
    matched = exists == should_exist
    if matched and min_size is not None:
        matched = bool(data.get("min_size_met"))
    if "expected_sha256" in args:
        data["content_matched"] = exists and data.get("sha256") == args["expected_sha256"].lower()
        matched = matched and data["content_matched"]
    if not matched:
        return command_result(
            "UNCERTAIN",
            "verify.file_state",
            "File state did not match expectation.",
            data=data,
            next_allowed_actions=["planner.fast_observe", "planner.step", "observe.screenshot"],
        )
    return command_result("SUCCESS", "verify.file_state", "File state verified.", data=data)


def desktop_known_folder() -> Path:
    """Current user's redirected Desktop; never guess a home/Desktop fallback."""
    shell = ctypes.windll.shell32
    ole = ctypes.windll.ole32
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE: use caller's existing apartment.
        raise OSError(f"Desktop COM initialization failed: 0x{initialized & 0xffffffff:08x}")
    buffer = ctypes.c_void_p()
    try:
        result = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer))
        if result < 0 or not buffer.value:
            raise OSError(f"Desktop Known Folder lookup failed: 0x{result & 0xffffffff:08x}")
        resolved = Path(ctypes.wstring_at(buffer))
        if not resolved.is_absolute():
            raise OSError("Desktop Known Folder returned no absolute path.")
        return resolved
    finally:
        if buffer.value:
            ole.CoTaskMemFree(buffer)
        if initialized >= 0:
            ole.CoUninitialize()


def resolve_file_path_from_args(args: dict[str, Any]) -> Path | None:
    raw_path = args.get("path") or args.get("file_path")
    if not raw_path and args.get("desktop_filename"):
        errors = validate_file_selector(args)
        if errors:
            raise ValueError(" ".join(errors))
        return desktop_known_folder() / args["desktop_filename"]
    if not raw_path:
        return None
    expanded = os.path.expandvars(os.path.expanduser(str(raw_path)))
    return Path(expanded)


def cmd_focus_window(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or TARGET_TITLE)
    window = resolve_working_window(args)
    if window is None:
        return command_result("UNCERTAIN", "focus.window", f"Window matching {title!r} not found.")
    activation_details: dict[str, Any] = {"win32_bring_to_front": bool(bring_window_to_front(window.hwnd))}
    cancellable_sleep(FOCUS_SETTLE_SECONDS)
    foreground = get_foreground_window_snapshot()
    if int(foreground.get("hwnd") or 0) != int(window.hwnd) and bool(args.get("allow_mouse_activation", True)):
        activation_details["titlebar_click_activation"] = activate_window_by_titlebar_click(window)
        foreground = get_foreground_window_snapshot()
    if int(foreground.get("hwnd") or 0) != int(window.hwnd):
        if bool(args.get("allow_window_anchor", True)):
            return command_result(
                "SUCCESS",
                "focus.window",
                "Working window anchor is available, but foreground could not be confirmed.",
                data={
                    "window": window_payload(window),
                    "foreground_window": foreground,
                    "foreground_confirmed": False,
                    "window_anchor_ready": True,
                    "bring_to_front_succeeded": bool(activation_details.get("win32_bring_to_front")),
                    "activation_details": activation_details,
                },
            )
        return command_result(
            "UNCERTAIN",
            "focus.window",
            "Window did not become foreground.",
            data={
                "window": window_payload(window),
                "foreground_window": foreground,
                "bring_to_front_succeeded": bool(activation_details.get("win32_bring_to_front")),
                "activation_details": activation_details,
            },
        )
    return command_result(
        "SUCCESS",
        "focus.window",
        "Window focused.",
        data={
            "window": window_payload(window),
            "foreground_window": foreground,
            "foreground_confirmed": True,
            "window_anchor_ready": True,
            "bring_to_front_succeeded": bool(activation_details.get("win32_bring_to_front")),
            "activation_details": activation_details,
        },
    )


def cmd_input_click_point(args: dict[str, Any], *, before_press=None) -> dict[str, Any]:
    check_command_cancelled()
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    target = str(args.get("target") or "target_window")
    title = str(args.get("title") or TARGET_TITLE)
    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return command_result("ERROR", "input.click_point", "Both --x and --y are required.")
    screen_x = int(float(x))
    screen_y = int(float(y))
    window = None
    if target == "current_dialog":
        window = resolve_current_dialog_window(args)
        if window is None:
            return command_result("UNCERTAIN", "input.click_point", "Current dialog disappeared before click.")
        vm_guardrail = evaluate_vm_boundary_guardrail("input.click_point", args, target_window=window)
        if vm_guardrail is not None:
            return vm_guardrail
        focus = cmd_focus_window({**args, "hwnd": window.hwnd, "allow_window_anchor": True, "allow_non_dialog_hwnd": True})
        if focus["status"] != "SUCCESS":
            return focus
    elif target not in {"desktop", "foreground_window", "active_window"}:
        window = resolve_working_window(args)
        if window is None:
            return command_result("UNCERTAIN", "input.click_point", "Window disappeared before click.")
        vm_guardrail = evaluate_vm_boundary_guardrail("input.click_point", args, target_window=window)
        if vm_guardrail is not None:
            return vm_guardrail
        focus = cmd_focus_window({**args, "hwnd": window.hwnd, "allow_window_anchor": True})
        if focus["status"] != "SUCCESS":
            return focus
    coordinate_mode = str(args.get("coordinate_mode") or "window").strip().lower()
    if coordinate_mode == "window" and window is not None:
        screen_x += window.left
        screen_y += window.top
    guardrail = evaluate_click_guardrails("input.click_point", {**args, "target": target}, screen_x, screen_y, window)
    if guardrail is not None:
        return guardrail
    before = get_foreground_window_snapshot()
    check_command_cancelled()
    click_backend = perform_screen_click(screen_x, screen_y, before_press=before_press) if before_press else perform_screen_click(screen_x, screen_y)
    if click_backend.get("press_guard_error"):
        return command_result("UNCERTAIN", "input.click_point", click_backend["press_guard_error"], data={"failure_type": "WINDOW_MOVED", "click_result": click_backend})
    cancellable_sleep(0.15)
    after = get_foreground_window_snapshot()
    result_data = {
        "target_window": window_payload(window),
        "screen_coordinate": {"x": screen_x, "y": screen_y},
        "foreground_before": before,
        "foreground_after": after,
        "expected_result": args.get("expected_result") or "",
        "failsafe_nudge": failsafe_nudge,
        "click_backend": click_backend,
    }
    if not bool(click_backend.get("click_verified")):
        recovery_payload = input_permission_recovery_payload(click_backend, "input.click_point")
        result_data.update(recovery_payload)
        return command_result(
            "UNCERTAIN",
            "input.click_point",
            result_data["recovery_instruction"],
            data=result_data,
            next_allowed_actions=recovery_payload["recommended_recovery_flow"] + ["diagnose.environment"],
        )
    return command_result(
        "SUCCESS",
        "input.click_point",
        "Clicked one point.",
        data=result_data,
    )


def cmd_input_click_candidate(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    candidate_id = int(args.get("candidate_id") or 0)
    candidate_hwnd = int(args.get("candidate_hwnd") or args.get("hwnd") or 0)
    if candidate_id <= 0 and candidate_hwnd <= 0:
        return command_result("ERROR", "input.click_candidate", "candidate_id or candidate_hwnd is required.")
    active_context = build_active_context_packet_for_args(args)
    candidates = active_context.get("child_candidates") if isinstance(active_context.get("child_candidates"), list) else []
    candidate = find_candidate(candidates, candidate_id, candidate_hwnd)
    if candidate is None:
        return command_result(
            "UNCERTAIN",
            "input.click_candidate",
            "Candidate was not found in the current active window.",
            data={
                "requested_candidate_id": candidate_id or None,
                "requested_candidate_hwnd": candidate_hwnd or None,
                "active_context": active_context,
                "candidate_overlay": create_candidate_overlay_for_active_context(active_context, args),
            },
            next_allowed_actions=["observe.screenshot", "recovery.capture_grid", "planner.step"],
        )
    freshness = validate_candidate_for_click(candidate, active_context, args)
    if freshness["status"] != "SUCCESS":
        return freshness
    bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
    screen_x = int((int(bounds.get("left") or 0) + int(bounds.get("right") or 0)) / 2)
    screen_y = int((int(bounds.get("top") or 0) + int(bounds.get("bottom") or 0)) / 2)
    guardrail = evaluate_click_guardrails("input.click_candidate", args, screen_x, screen_y, candidate)
    if guardrail is not None:
        return guardrail
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    active_hwnd = int(active_window.get("hwnd") or 0)
    if active_hwnd:
        bring_window_to_front(active_hwnd)
        cancellable_sleep(FOCUS_SETTLE_SECONDS)
    before_capture = capture_target(
        {
            "target": "active_window",
            "title": str(active_window.get("title") or args.get("title") or TARGET_TITLE),
            "hwnd": active_hwnd,
            "request_id": f"{make_request_id()}_candidate_before",
        },
        with_grid=False,
    )
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    before = get_foreground_window_snapshot()
    check_command_cancelled()
    click_backend = perform_screen_click(screen_x, screen_y)
    cancellable_sleep(float(args.get("post_click_delay") or 0.25))
    after = get_foreground_window_snapshot()
    after_capture = capture_target(
        {
            "target": "active_window",
            "title": str(active_window.get("title") or args.get("title") or TARGET_TITLE),
            "hwnd": active_hwnd,
            "request_id": f"{make_request_id()}_candidate_after",
        },
        with_grid=False,
    )
    result_data = {
        "candidate": candidate,
        "screen_coordinate": {"x": screen_x, "y": screen_y},
        "foreground_before": before,
        "foreground_after": after,
        "expected_result": args.get("expected_result") or "",
        "failsafe_nudge": failsafe_nudge,
        "click_backend": click_backend,
        "candidate_overlay": create_candidate_overlay_for_active_context(active_context, args),
    }
    if before_capture["status"] == "SUCCESS":
        result_data["before_click_screenshot_path"] = (before_capture.get("data") or {}).get("raw_screenshot_path")
    if after_capture["status"] == "SUCCESS":
        result_data["after_click_screenshot_path"] = (after_capture.get("data") or {}).get("raw_screenshot_path")
    if before_capture["status"] == "SUCCESS" and after_capture["status"] == "SUCCESS":
        result_data["post_action_diff"] = summarize_action_delta(before_capture, after_capture, {"foreground_before": before, "foreground_after": after})
    if not bool(click_backend.get("click_verified")):
        result_data.update(input_permission_recovery_payload(click_backend, "input.click_candidate"))
        return command_result(
            "UNCERTAIN",
            "input.click_candidate",
            result_data["recovery_instruction"],
            data=result_data,
            next_allowed_actions=["input.invoke_candidate", "helper.exit", "helper.start_elevated", "planner.fast_observe", "diagnose.environment"],
        )
    return command_result(
        "SUCCESS",
        "input.click_candidate",
        "Candidate clicked.",
        data=result_data,
        next_allowed_actions=["planner.step", "observe.screenshot", "status.current"],
    )


def cmd_input_invoke_candidate(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    resolved = resolve_candidate_for_backend_action(args, "input.invoke_candidate")
    if resolved["status"] != "SUCCESS":
        return resolved
    data = resolved["data"]
    vm_guardrail = evaluate_vm_boundary_guardrail("input.invoke_candidate", args, target_window=data.get("active_window"))
    if vm_guardrail is not None:
        return vm_guardrail
    candidate = data["candidate"]
    hwnd = int(candidate.get("hwnd") or 0)
    active_hwnd = int((data.get("active_window") or {}).get("hwnd") or 0)
    before = get_foreground_window_snapshot()
    if active_hwnd:
        bring_window_to_front(active_hwnd)
        cancellable_sleep(FOCUS_SETTLE_SECONDS)
    check_command_cancelled()
    invoke_result = send_backend_invoke(hwnd, candidate)
    if not invoke_result.get("ok"):
        result_data = dict(data)
        result_data["backend_invoke"] = invoke_result
        return command_result(
            "UNCERTAIN",
            "input.invoke_candidate",
            "Candidate could not be invoked through the local backend.",
            data=result_data,
            next_allowed_actions=["input.click_candidate", "input.select_candidate", "recovery.capture_grid", "planner.step"],
        )
    cancellable_sleep(float(args.get("post_action_delay") or 0.25))
    after = get_foreground_window_snapshot()
    result_data = dict(data)
    result_data.update(
        {
            "backend_method": invoke_result.get("method") or "",
            "backend_invoke": invoke_result,
            "foreground_before": before,
            "foreground_after": after,
            "expected_result": args.get("expected_result") or "",
        }
    )
    return command_result("SUCCESS", "input.invoke_candidate", "Candidate invoked through backend message.", data=result_data, next_allowed_actions=["planner.step", "observe.screenshot", "status.current"])


def cmd_input_set_candidate_value(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    resolved = resolve_candidate_for_backend_action(args, "input.set_candidate_value")
    if resolved["status"] != "SUCCESS":
        return resolved
    data = resolved["data"]
    vm_guardrail = evaluate_vm_boundary_guardrail("input.set_candidate_value", args, target_window=data.get("active_window"))
    if vm_guardrail is not None:
        return vm_guardrail
    candidate = data["candidate"]
    if str(candidate.get("kind") or "") != "text_field" and not bool(args.get("allow_non_text_field")):
        return command_result(
            "UNCERTAIN",
            "input.set_candidate_value",
            "Candidate does not look like a text field.",
            data=data,
            next_allowed_actions=["input.click_candidate", "planner.step", "recovery.capture_grid"],
        )
    text = str(args.get("text") if args.get("text") is not None else args.get("value") or "")
    text_guardrail = evaluate_text_entry_guardrails("input.set_candidate_value", text, args)
    if text_guardrail is not None:
        return text_guardrail
    hwnd = int(candidate.get("hwnd") or 0)
    before_value = get_control_text(hwnd)
    check_command_cancelled()
    sent = bool(ctypes.windll.user32.SendMessageW(hwnd, WM_SETTEXT, 0, text))
    cancellable_sleep(float(args.get("post_action_delay") or 0.15))
    after_value = get_control_text(hwnd)
    if not sent and after_value != text:
        return command_result(
            "UNCERTAIN",
            "input.set_candidate_value",
            "Backend set-text message did not update the candidate value.",
            data={**data, "before_value": before_value, "after_value": after_value, "text_length": len(text)},
            next_allowed_actions=["input.click_candidate", "input.paste_text", "planner.step"],
        )
    return command_result(
        "SUCCESS",
        "input.set_candidate_value",
        "Candidate value set through backend message.",
        data={**data, "before_value": before_value, "after_value": after_value, "text_length": len(text), "value_changed": before_value != after_value},
        next_allowed_actions=["planner.step", "observe.screenshot", "status.current"],
    )


def cmd_input_expand_candidate(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    resolved = resolve_candidate_for_backend_action(args, "input.expand_candidate")
    if resolved["status"] != "SUCCESS":
        return resolved
    data = resolved["data"]
    vm_guardrail = evaluate_vm_boundary_guardrail("input.expand_candidate", args, target_window=data.get("active_window"))
    if vm_guardrail is not None:
        return vm_guardrail
    candidate = data["candidate"]
    hwnd = int(candidate.get("hwnd") or 0)
    class_name = str(candidate.get("class_name") or "").lower()
    active_hwnd = int((data.get("active_window") or {}).get("hwnd") or 0)
    before = get_foreground_window_snapshot()
    if active_hwnd:
        bring_window_to_front(active_hwnd)
        cancellable_sleep(FOCUS_SETTLE_SECONDS)
    check_command_cancelled()
    if "combo" in class_name:
        ctypes.windll.user32.SendMessageW(hwnd, CB_SHOWDROPDOWN, 1, 0)
        method = "CB_SHOWDROPDOWN"
        backend_invoke: dict[str, Any] = {"ok": True, "method": method}
    else:
        backend_invoke = send_backend_invoke(hwnd, candidate)
        method = str(backend_invoke.get("method") or "")
    if not backend_invoke.get("ok"):
        return command_result(
            "UNCERTAIN",
            "input.expand_candidate",
            "Candidate could not be expanded through the local backend.",
            data={**data, "backend_invoke": backend_invoke},
            next_allowed_actions=["input.click_candidate", "recovery.capture_grid", "planner.step"],
        )
    cancellable_sleep(float(args.get("post_action_delay") or 0.25))
    return command_result(
        "SUCCESS",
        "input.expand_candidate",
        "Candidate expansion requested through backend message.",
        data={**data, "backend_method": method, "backend_invoke": backend_invoke, "foreground_before": before, "foreground_after": get_foreground_window_snapshot()},
        next_allowed_actions=["planner.step", "observe.screenshot", "status.current"],
    )


def cmd_input_select_candidate(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    resolved = resolve_candidate_for_backend_action(args, "input.select_candidate")
    if resolved["status"] != "SUCCESS":
        return resolved
    data = resolved["data"]
    vm_guardrail = evaluate_vm_boundary_guardrail("input.select_candidate", args, target_window=data.get("active_window"))
    if vm_guardrail is not None:
        return vm_guardrail
    candidate = data["candidate"]
    hwnd = int(candidate.get("hwnd") or 0)
    active_hwnd = int((data.get("active_window") or {}).get("hwnd") or 0)
    before = get_foreground_window_snapshot()
    if active_hwnd:
        bring_window_to_front(active_hwnd)
        cancellable_sleep(FOCUS_SETTLE_SECONDS)
    check_command_cancelled()
    method = ""
    class_name = str(candidate.get("class_name") or "").lower()
    if "button" in class_name:
        if args.get("checked") is not None:
            checked = BST_CHECKED if bool(args.get("checked")) else BST_UNCHECKED
            ctypes.windll.user32.SendMessageW(hwnd, BM_SETCHECK, checked, 0)
            method = "BM_SETCHECK"
            backend_invoke: dict[str, Any] = {"ok": True, "method": method}
        else:
            backend_invoke = send_backend_invoke(hwnd, candidate)
            method = str(backend_invoke.get("method") or "")
    elif "listbox" in class_name and args.get("index") is not None:
        ctypes.windll.user32.SendMessageW(hwnd, LB_SETCURSEL, int(args.get("index") or 0), 0)
        method = "LB_SETCURSEL"
        backend_invoke = {"ok": True, "method": method}
    else:
        backend_invoke = send_backend_invoke(hwnd, candidate)
        method = str(backend_invoke.get("method") or "")
    if not backend_invoke.get("ok"):
        return command_result(
            "UNCERTAIN",
            "input.select_candidate",
            "Candidate could not be selected through the local backend.",
            data={**data, "backend_invoke": backend_invoke},
            next_allowed_actions=["input.click_candidate", "recovery.capture_grid", "planner.step"],
        )
    cancellable_sleep(float(args.get("post_action_delay") or 0.2))
    return command_result(
        "SUCCESS",
        "input.select_candidate",
        "Candidate selection requested through backend message.",
        data={**data, "backend_method": method, "backend_invoke": backend_invoke, "checked_state": get_button_check_state(hwnd), "foreground_before": before, "foreground_after": get_foreground_window_snapshot()},
        next_allowed_actions=["planner.step", "observe.screenshot", "status.current"],
    )


def ensure_window_foreground(window_ref: dict[str, Any] | None, expected_title: str = "") -> dict[str, Any]:
    if not isinstance(window_ref, dict):
        return command_result("ERROR", "focus.ensure_window", "No remembered window is available.")
    hwnd = int(window_ref.get("hwnd") or 0)
    title = str(window_ref.get("title") or expected_title or "")
    window = window_info_from_hwnd(hwnd) if hwnd else None
    if window is None and title:
        window = find_best_window_for_args({"title": title})
    if window is None:
        return command_result(
            "UNCERTAIN",
            "focus.ensure_window",
            "Remembered window is no longer available.",
            data={"remembered_window": window_ref},
        )
    activation_details: dict[str, Any] = {"win32_bring_to_front": bool(bring_window_to_front(window.hwnd))}
    cancellable_sleep(FOCUS_SETTLE_SECONDS)
    foreground = get_foreground_window_snapshot()
    if int(foreground.get("hwnd") or 0) != int(window.hwnd):
        activation_details["titlebar_click_activation"] = activate_window_by_titlebar_click(window)
        foreground = get_foreground_window_snapshot()
    if int(foreground.get("hwnd") or 0) != int(window.hwnd):
        return command_result(
            "UNCERTAIN",
            "focus.ensure_window",
            "Remembered window did not become foreground.",
            data={"remembered_window": window_payload(window), "foreground_window": foreground, "activation_details": activation_details},
        )
    return command_result(
        "SUCCESS",
        "focus.ensure_window",
        "Remembered window is foreground.",
        data={"remembered_window": window_payload(window), "foreground_window": foreground, "activation_details": activation_details},
    )


def window_ref_from_action_result(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for key in ("foreground_after", "target_window", "window"):
        value = data.get(key)
        if isinstance(value, dict) and int(value.get("hwnd") or 0):
            return {"hwnd": int(value.get("hwnd") or 0), "title": str(value.get("title") or "")}
    return None




# Targets that do not resolve a specific top-level window; a missing title is
# not defaulted to the Simon Target window for these.
WINDOW_INDEPENDENT_TARGETS = frozenset({"desktop", "foreground_window", "active_window", "current_dialog"})
# Per-step keys forwarded verbatim into the underlying input/observe command.
ACTION_SEQUENCE_FORWARD_KEYS = (
    "hwnd",
    "window_hwnd",
    "dialog_hwnd",
    "current_dialog_hwnd",
    "allow_non_dialog_hwnd",
    "title_excludes",
    "min_width",
    "min_height",
    "allow_minimized",
    "allow_window_anchor",
    "allow_mouse_activation",
    "expected_result",
)
# Guardrail overrides: honored only when explicitly set on the step (or else on
# the sequence). They are never defaulted on.
ACTION_SEQUENCE_OVERRIDE_KEYS = ("allow_path_text", "allow_vm_control")


def build_action_sequence_step_args(action: dict[str, Any], sequence_args: dict[str, Any], kind: str) -> dict[str, Any]:
    target = str(action.get("target") or "desktop")
    payload: dict[str, Any] = {"target": target}
    identity_source = action if has_window_title_identity(action) else sequence_args
    for key in WINDOW_TITLE_IDENTITY_KEYS:
        value = identity_source.get(key)
        if value is not None and str(value).strip():
            payload[key] = value
    if not has_window_title_identity(payload) and target not in WINDOW_INDEPENDENT_TARGETS:
        payload["title"] = TARGET_TITLE
    for key in ACTION_SEQUENCE_FORWARD_KEYS:
        if key in action and action.get(key) is not None:
            payload[key] = action.get(key)
    for key in ACTION_SEQUENCE_OVERRIDE_KEYS:
        if key in action:
            payload[key] = bool(action.get(key))
        elif key in sequence_args:
            payload[key] = bool(sequence_args.get(key))
    if kind == "click":
        payload["x"] = action.get("x")
        payload["y"] = action.get("y")
        payload["coordinate_mode"] = action.get("coordinate_mode") or "screen"
        payload.setdefault("expected_result", "")
    elif kind in {"type_text", "paste_text"}:
        payload["text"] = action.get("text") or ""
    elif kind == "hotkey":
        payload["keys"] = action.get("keys") or ""
    elif kind == "screenshot":
        payload["request_id"] = action.get("request_id") or make_request_id()
    return payload


def action_sequence_step_record(index: int, kind: str, result: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {"index": index, "action": kind, "result": result}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for key in ("foreground_before", "foreground_after"):
        if key in data:
            record[key] = data[key]
    return record


def cmd_input_action_sequence(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    actions = args.get("actions")
    if not isinstance(actions, list):
        return command_result("ERROR", "input.action_sequence", "JSON args must include an actions list.")
    _, errors = validate_arguments("input.action_sequence", args)
    effects = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"Action {index} is not an object.")
            effects.append({})
            continue
        effect, problems = declared_effect(action)
        effects.append(effect)
        errors.extend(f"Action {index}: {error}" for error in problems)
        if "preview" in action or "confirm_token" in action:
            errors.append(f"Action {index}: preview/confirmation is supported only by planner.step guided clicks.")
        if action.get("action") not in {"wait", *ACTION_SEQUENCE_STEP_HANDLERS}:
            errors.append(f"Unsupported action at index {index}.")
    if "preview" in args or "confirm_token" in args:
        errors.append("Sequence preview/confirmation is unsupported; no actions executed.")
    if "expected_change" in args:
        errors.append("Declare expected_change on each sequence action, not on the sequence root.")
    if errors:
        return command_result("ERROR", "input.action_sequence", "Invalid sequence; no actions executed.",
                              data={"errors": errors, "failure_type": "ARGUMENT_ERROR", "input_emitted": False,
                                    "requested_count": len(actions), "attempted_count": 0, "results": []})
    results = []
    attempted = 0
    input_verified = False
    remembered_window = args.get("remembered_window") if isinstance(args.get("remembered_window"), dict) else None
    guard_enabled = bool(args.get("focus_guard"))

    def finish(status, message, verification, stopping_index=None):
        return command_result(status, "input.action_sequence", message, data={
            "action_count": attempted, "requested_count": len(actions), "attempted_count": attempted,
            "stopping_index": stopping_index, "results": results, "verification": verification,
            "foreground_window": get_foreground_window_snapshot(), "remembered_window": remembered_window})

    index = 0
    kind = ""
    effect = {}
    executing = False
    try:
        for index, (action, effect) in enumerate(zip(actions, effects)):
            executing = False
            kind = action["action"]
            check_command_cancelled()
            if bool(action.get("enforce_focus_guard", guard_enabled)) and remembered_window and kind != "wait":
                focus_result = ensure_window_foreground(remembered_window, str(action.get("title") or args.get("title") or ""))
                results.append({"index": index, "action": "focus_guard", "result": focus_result})
                if focus_result["status"] != "SUCCESS":
                    return finish("UNCERTAIN", f"Focus guard stopped action {index}.",
                                  {"verdict": "unmet", "type": "focus_guard", "reason": focus_result.get("message")}, index)
            step_args = build_action_sequence_step_args(action, args, kind)
            watch_args = {**args, **action, "expected_change": effect}
            baseline = {"status": "SUCCESS", "data": {}}
            if effect:
                if canonical_effect_type(effect) in VISUAL_EFFECTS:
                    baseline = capture_target(build_follow_up_capture_args(watch_args, kind, step_args, make_request_id()), with_grid=False)
                    if baseline["status"] != "SUCCESS":
                        return finish("UNCERTAIN", "Pre-action visual evidence unavailable; action not executed.",
                                      effect_verdict(effect, {"reason": "pre_action_evidence_unavailable"}), index)
                snapshot = capture_effect_snapshot(effect)
                if snapshot.get("error"):
                    return finish("UNCERTAIN", "Pre-action effect evidence unavailable; action not executed.",
                                  {**effect_verdict(effect, {"reason": "pre_action_evidence_unavailable"}), "evidence": snapshot}, index)
                baseline["data"]["effect_snapshot"] = snapshot
            check_command_cancelled()
            executing = True
            attempted += 1
            if kind == "wait":
                seconds = float(action.get("seconds", 0.25))
                cancellable_sleep(max(0.0, seconds))
                result = command_result("SUCCESS", "input.wait", "Wait completed.", data={"seconds": seconds})
            else:
                result = ACTION_SEQUENCE_STEP_HANDLERS[kind](step_args)
            record = action_sequence_step_record(index, kind, result)
            results.append(record)
            status = result["status"]
            if status != "SUCCESS":
                verdict = effect_verdict(effect, {"reason": "Action requires intervention; no later step executed."}, requested=False)
                record["verification"] = verdict
                stopped = "UNCERTAIN" if result_outcome(result) == "blocked" else "NEEDS_PLANNER"
                return finish(stopped, f"Action {index} stopped the sequence.", verdict, index)
            should_remember = bool(action.get("remember_target")) or (guard_enabled and not remembered_window and kind != "wait")
            if should_remember:
                remembered_window = window_ref_from_action_result(result)
            if effect:
                watch = wait_for_expected_change(watch_args, kind, step_args, baseline, effect)
                verdict = effect_verdict(effect, watch)
                record["verification"] = verdict
                record["effect_evidence"] = watch
                if not effect_allows_continuation(verdict):
                    return finish("UNCERTAIN" if not verdict["matched"] else "NEEDS_PLANNER",
                                  f"Action {index} effect needs verification; no later step executed.", verdict, index)
                if kind not in {"wait", "screenshot"}:
                    input_verified = True
            elif kind not in {"wait", "screenshot"}:
                verdict = effect_verdict({}, None, requested=False)
                record["verification"] = verdict
                return finish("NEEDS_PLANNER", f"Action {index} executed without effect proof; no later step executed.", verdict, index)
        return finish("SUCCESS", "All requested sequence steps completed with required evidence.",
                      {"verdict": "verified", "type": "sequence", "evidence_kind": "sequence" if input_verified else "command",
                       "matched": True, "reason": "All declared effects verified; observation/wait steps completed."})
    except CommandCancelled as exc:
        verdict = effect_verdict(effect, {"reason": "Cancelled; partial delivery/effect may be unknown. Do not replay automatically."}, requested=False)
        current = next((row for row in reversed(results) if row["index"] == index and row["action"] == kind), None)
        if current is not None:
            current["verification"] = verdict
        elif executing:
            pending = command_result("UNCERTAIN", f"input.{kind}", "Cancelled during execution; delivery unknown.")
            results.append({"index": index, "action": kind, "result": pending, "verification": verdict})
        result = finish("UNCERTAIN", "Sequence cancelled; inspect partial evidence before deciding the next action.", verdict, index)
        result["data"].update(command_cancelled_payload(exc.reason), delivery_uncertain=True)
        return result


def cmd_input_hotkey(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    if pyautogui is None:
        return command_result("ERROR", "input.hotkey", "pyautogui is not installed.")
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    keys = parse_key_chord(str(args.get("keys") or ""))
    if not keys:
        return command_result("ERROR", "input.hotkey", "--keys is required, such as ctrl+shift+d.")
    target = str(args.get("target") or "target_window")
    if target == "current_dialog":
        window = resolve_current_dialog_window(args)
        if window is None:
            return command_result("UNCERTAIN", "input.hotkey", "Current dialog disappeared before hotkey.")
        focus = cmd_focus_window({**args, "hwnd": window.hwnd, "allow_window_anchor": True, "allow_non_dialog_hwnd": True})
        if focus["status"] != "SUCCESS":
            return focus
    elif target not in {"desktop", "foreground_window", "active_window"}:
        window = resolve_working_window(args)
        if window is not None:
            vm_guardrail = evaluate_vm_boundary_guardrail("input.hotkey", args, target_window=window)
            if vm_guardrail is not None:
                return vm_guardrail
        focus = cmd_focus_window(args)
        if focus["status"] != "SUCCESS":
            return focus
    vm_guardrail = evaluate_vm_boundary_guardrail("input.hotkey", args)
    if vm_guardrail is not None:
        return vm_guardrail
    before = get_foreground_window_snapshot()
    check_command_cancelled()
    with keyboard_only_pyautogui_context():
        pyautogui.hotkey(*keys)
    cancellable_sleep(0.15)
    return command_result(
        "SUCCESS",
        "input.hotkey",
        "Hotkey sent.",
        data={"target": target, "keys": keys, "foreground_before": before, "foreground_after": get_foreground_window_snapshot(), "failsafe_nudge": failsafe_nudge},
    )


def cmd_input_type_text(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    if pyautogui is None:
        return command_result("ERROR", "input.type_text", "pyautogui is not installed.")
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    text = str(args.get("text") or "")
    target = str(args.get("target") or "target_window")
    text_guardrail = evaluate_text_entry_guardrails("input.type_text", text, {**args, "target": target})
    if text_guardrail is not None:
        return text_guardrail
    if target == "current_dialog":
        window = resolve_current_dialog_window(args)
        if window is None:
            return command_result("UNCERTAIN", "input.type_text", "Current dialog disappeared before typing.")
        focus = cmd_focus_window({**args, "hwnd": window.hwnd, "allow_window_anchor": True, "allow_non_dialog_hwnd": True})
        if focus["status"] != "SUCCESS":
            return focus
    elif target not in {"desktop", "foreground_window", "active_window"}:
        window = resolve_working_window(args)
        if window is not None:
            vm_guardrail = evaluate_vm_boundary_guardrail("input.type_text", args, target_window=window)
            if vm_guardrail is not None:
                return vm_guardrail
        focus = cmd_focus_window(args)
        if focus["status"] != "SUCCESS":
            return focus
    before = get_foreground_window_snapshot()
    lines = text.splitlines()
    check_command_cancelled()
    with keyboard_only_pyautogui_context():
        if lines:
            for index, line in enumerate(lines):
                check_command_cancelled()
                if line:
                    pyautogui.write(line, interval=0.01)
                if index < len(lines) - 1:
                    check_command_cancelled()
                    pyautogui.press("enter")
        else:
            pyautogui.write(text, interval=0.01)
    cancellable_sleep(0.15)
    return command_result(
        "SUCCESS",
        "input.type_text",
        "Text typed.",
        data={"target": target, "text_length": len(text), "foreground_before": before, "foreground_after": get_foreground_window_snapshot(), "failsafe_nudge": failsafe_nudge},
    )


def cmd_input_paste_text(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    if pyautogui is None:
        return command_result("ERROR", "input.paste_text", "pyautogui is not installed.")
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    text = str(args.get("text") or "")
    target = str(args.get("target") or "target_window")
    text_guardrail = evaluate_text_entry_guardrails("input.paste_text", text, {**args, "target": target})
    if text_guardrail is not None:
        return text_guardrail
    if target == "current_dialog":
        window = resolve_current_dialog_window(args)
        if window is None:
            return command_result("UNCERTAIN", "input.paste_text", "Current dialog disappeared before paste.")
        focus = cmd_focus_window({**args, "hwnd": window.hwnd, "allow_window_anchor": True, "allow_non_dialog_hwnd": True})
        if focus["status"] != "SUCCESS":
            return focus
    elif target not in {"desktop", "foreground_window", "active_window"}:
        window = resolve_working_window(args)
        if window is not None:
            vm_guardrail = evaluate_vm_boundary_guardrail("input.paste_text", args, target_window=window)
            if vm_guardrail is not None:
                return vm_guardrail
        focus = cmd_focus_window(args)
        if focus["status"] != "SUCCESS":
            return focus
    before = get_foreground_window_snapshot()
    try:
        set_clipboard_text(text)
    except Exception as exc:
        return command_result("ERROR", "input.paste_text", f"Could not set clipboard text: {exc}")
    check_command_cancelled()
    with keyboard_only_pyautogui_context():
        pyautogui.hotkey("ctrl", "v")
    cancellable_sleep(0.25)
    return command_result(
        "SUCCESS",
        "input.paste_text",
        "Text pasted.",
        data={"target": target, "text_length": len(text), "foreground_before": before, "foreground_after": get_foreground_window_snapshot(), "failsafe_nudge": failsafe_nudge},
    )


def _action_sequence_click(step_args: dict[str, Any]) -> dict[str, Any]:
    return cmd_input_click_point(step_args)


def _action_sequence_type_text(step_args: dict[str, Any]) -> dict[str, Any]:
    return cmd_input_type_text(step_args)


def _action_sequence_paste_text(step_args: dict[str, Any]) -> dict[str, Any]:
    return cmd_input_paste_text(step_args)


def _action_sequence_hotkey(step_args: dict[str, Any]) -> dict[str, Any]:
    return cmd_input_hotkey(step_args)


def _action_sequence_screenshot(step_args: dict[str, Any]) -> dict[str, Any]:
    return cmd_observe_screenshot(step_args)


# Late-bound wrappers so tests (and future patches) that replace the module
# level cmd_* functions are honored by input.action_sequence.
ACTION_SEQUENCE_STEP_HANDLERS = {
    "click": _action_sequence_click,
    "type_text": _action_sequence_type_text,
    "paste_text": _action_sequence_paste_text,
    "hotkey": _action_sequence_hotkey,
    "screenshot": _action_sequence_screenshot,
}


def cmd_recovery_capture_grid(args: dict[str, Any]) -> dict[str, Any]:
    capture = capture_target(args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return capture
    data = capture["data"]
    request = create_planner_request(data, args, failed_command=str(args.get("failed_command") or "unknown"))
    data["planner_request_path"] = request["planner_request_path"]
    data["planner_request"] = request
    return command_result("NEEDS_PLANNER", "recovery.capture_grid", "Grid screenshot captured.", data=data)


def cmd_desktop_start_search_grid(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    if pyautogui is None:
        return command_result("ERROR", "desktop.start_search_grid", "pyautogui is not installed.")
    query = str(args.get("query") or args.get("text") or "").strip()
    if not query:
        return command_result("ERROR", "desktop.start_search_grid", "A search query is required through --text or JSON query.")
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    before = get_foreground_window_snapshot()
    with keyboard_only_pyautogui_context():
        check_command_cancelled()
        pyautogui.press("esc")
        cancellable_sleep(0.10)
        check_command_cancelled()
        pyautogui.press("win")
        cancellable_sleep(float(args.get("start_settle_seconds") or 0.35))
        check_command_cancelled()
        pyautogui.write(query, interval=0.01)
    cancellable_sleep(float(args.get("search_settle_seconds") or args.get("post_click_delay") or 0.75))
    request_id = str(args.get("request_id") or make_request_id())
    foreground_after_query = get_foreground_window_snapshot()
    foreground_title = str(foreground_after_query.get("title") or "").strip()
    foreground_hwnd = int(foreground_after_query.get("hwnd") or 0)
    helper_foreground = is_helper_window_snapshot(foreground_after_query)
    if helper_foreground:
        foreground_title = ""
        foreground_hwnd = 0
        search_window = find_likely_search_window()
        if search_window is not None:
            foreground_title = search_window.title
            foreground_hwnd = search_window.hwnd
    capture_target_name = "target_window" if foreground_hwnd else "desktop"
    capture_title = foreground_title or str(args.get("title") or TARGET_TITLE)
    capture_args = {
        "target": capture_target_name,
        "title": capture_title,
        "hwnd": foreground_hwnd,
        "request_id": request_id,
        "columns": int(args.get("columns") or 12),
        "rows": int(args.get("rows") or 8),
        "reason": args.get("reason") or f"Windows Start/Search opened with query {query!r}; planner needs next safe action.",
        "expected_result": args.get("expected_result") or f"Search results for {query!r} are visible.",
        "failed_command": args.get("failed_command") or "desktop.start_search_grid",
    }
    capture = capture_target(capture_args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return capture
    data = capture["data"]
    planner_request = create_planner_request(
        data,
        capture_args,
        failed_command="desktop.start_search_grid",
        question=str(args.get("question") or f"Using this Start/Search grid for {query!r}, what single safe action should I try next?"),
    )
    data["planner_request_path"] = planner_request["planner_request_path"]
    data["planner_request"] = planner_request
    result_data = {
        "query": query,
        "foreground_before": before,
        "foreground_after_query": foreground_after_query,
        "helper_foreground_fallback_ignored": helper_foreground,
        "foreground_after_capture": get_foreground_window_snapshot(),
        "capture_target": capture_target_name,
        "capture_title": capture_title,
        "capture": data,
        "failsafe_nudge": failsafe_nudge,
        "preserve_foreground": True,
    }
    return command_result(
        "NEEDS_PLANNER",
        "desktop.start_search_grid",
        "Start/Search query entered and grid captured.",
        data=result_data,
        next_allowed_actions=["recovery.guided_grid_click", "recovery.guided_grid_crop", "input.action_sequence", "helper.bridge_status"],
    )


def cmd_recovery_guided_grid_crop(args: dict[str, Any]) -> dict[str, Any]:
    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        return command_result("ERROR", "recovery.guided_grid_crop", "--request-id is required.")
    cell = str(args.get("cell") or "").strip().upper()
    if not cell:
        return command_result("ERROR", "recovery.guided_grid_crop", "--cell is required.")
    planner_request = load_planner_request(request_id)
    if planner_request is None:
        return command_result("UNCERTAIN", "recovery.guided_grid_crop", f"Planner request {request_id!r} not found.")
    stale = planner_request_staleness(planner_request, arg_float_or_default(args, "max_age_seconds", DEFAULT_STALE_SECONDS))
    if stale:
        return command_result(
            "UNCERTAIN",
            "recovery.guided_grid_crop",
            stale,
            data={"request_id": request_id, "failure_type": "STALE_SCREENSHOT", "planner_request": planner_request},
        )
    title = str(planner_request.get("title") or args.get("title") or TARGET_TITLE)
    current_window = current_window_for_planner(planner_request, title)
    moved = validate_planner_target_window(planner_request, current_window)
    if moved:
        return command_result(
            "UNCERTAIN",
            "recovery.guided_grid_crop",
            moved,
            data={
                "request_id": request_id,
                "failure_type": "WINDOW_MOVED",
                "target_window_at_capture": planner_request.get("target_window"),
                "current_target_window": window_payload(current_window),
            },
        )
    crop_columns = int(args.get("crop_columns") or 0)
    crop_rows = int(args.get("crop_rows") or 0)
    if crop_columns <= 0:
        crop_columns = 0
    if crop_rows <= 0:
        crop_rows = 0
    crop = create_grid_crop_request(
        planner_request,
        cell,
        crop_columns,
        crop_rows,
        str(args.get("reason") or ""),
        include_context=args.get("detail") == "full" or "context" in args.get("include_images", []),
    )
    if crop["status"] != "SUCCESS":
        return crop
    return command_result(
        "NEEDS_PLANNER",
        "recovery.guided_grid_crop",
        "Fine-grid crop created.",
        data=crop["data"],
        next_allowed_actions=["recovery.guided_grid_click", "recovery.guided_grid_crop", "status.current"],
    )


def guided_click_confirmation(args: dict[str, Any], data: dict[str, Any], command: str) -> dict[str, Any] | None:
    """Persist one action-bound, single-use confirmation in its existing request."""
    request = data["planner_request"]
    preview = args.get("preview") is True
    token = str(args.get("confirm_token") or "")
    if not preview and not token:
        return None
    excluded = {"preview", "confirm_token", "detail", "include_images", "image_max_edge", "pretty"}
    bound = {"command": command, "args": {k: v for k, v in args.items() if k not in excluded},
             "screen_coordinate": data["screen_coordinate"], "image_to_screen": request.get("image_to_screen"),
             "capture_timestamp": request.get("capture_timestamp")}
    signature = hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with artifact_manifest_lock():
        stored = load_planner_request(str(request["request_id"]))
        if stored is None:
            return command_result("UNCERTAIN", command, "Planner request disappeared before confirmation.")
        if preview:
            token = secrets.token_urlsafe(24)
            confirmation = {"hash": hashlib.sha256(token.encode()).hexdigest(), "signature": signature,
                            "expires_at": time.time() + 30, "used": False}
            stored["preview_confirmation"] = confirmation
        else:
            confirmation = stored.get("preview_confirmation") or {}
            if (confirmation.get("used") is not False or time.time() > confirmation.get("expires_at", 0)
                    or not secrets.compare_digest(confirmation.get("hash", ""), hashlib.sha256(token.encode()).hexdigest())
                    or confirmation.get("signature") != signature):
                return command_result("UNCERTAIN", command, "Confirmation expired, was used, or does not match this action.",
                                      data={"failure_type": "INVALID_CONFIRMATION"})
            confirmation["used"] = True
        write_planner_request(stored)
    if not preview:
        return None
    source = Path(str(request.get("grid_screenshot_path") or request["raw_screenshot_path"]))
    with Image.open(source) as original:
        image = original.convert("RGB")
    render = request.get("grid_render") or {}
    sx, sy = float(render.get("scale_x", 1)), float(render.get("scale_y", 1))
    mx, my = float(render.get("margin_x", 0)), float(render.get("margin_y", 0))
    x = round(mx + data["grid_coordinate"]["x"] * sx)
    y = round(my + data["grid_coordinate"]["y"] * sy)
    draw = ImageDraw.Draw(image)
    draw.line((x - 18, y, x + 18, y), fill="red", width=2)
    draw.line((x, y - 18, x, y + 18), fill="red", width=2)
    bbox = data.get("cell_bbox") or {}
    draw.rectangle((mx + bbox["left"] * sx, my + bbox["top"] * sy,
                    mx + bbox["right"] * sx, my + bbox["bottom"] * sy), outline="red", width=2)
    path = COMMAND_SCREENSHOT_DIR / f"{safe_request_id(request['request_id'])}_preview.png"
    image.save(path)
    record_generated_artifact(path)
    return command_result("NEEDS_PLANNER", command, "Preview ready. Repeat this action with confirm_token within 30 seconds.",
                          data={"request_id": request["request_id"], "confirm_token": token,
                                "expires_at": confirmation["expires_at"], "screen_coordinate": data["screen_coordinate"],
                                "cell_bbox": bbox, "grid_screenshot_path": str(path), "grid_render": render,
                                "image_to_screen": request.get("image_to_screen"), "input_emitted": False})


def recheck_guided_click(args: dict[str, Any], data: dict[str, Any], command: str) -> dict[str, Any] | None:
    request = data["planner_request"]
    limit = arg_float_or_default(args, "max_age_seconds", DEFAULT_STALE_SECONDS)
    stale = planner_request_staleness(request, min(DEFAULT_STALE_SECONDS, limit) if limit > 0 else DEFAULT_STALE_SECONDS)
    moved = validate_planner_target_window(request, current_window_for_planner(request, str(data["title"])))
    if stale or moved:
        return command_result("UNCERTAIN", command, stale or moved, data={"failure_type": "STALE_SCREENSHOT" if stale else "WINDOW_MOVED"})
    return guided_click_confirmation(args, data, command)


def guided_press_guard(data: dict[str, Any]) -> str:
    request = data["planner_request"]
    return (planner_request_staleness(request, DEFAULT_STALE_SECONDS)
            or validate_planner_target_window(request, current_window_for_planner(request, str(data["title"]))))


def cmd_recovery_guided_grid_click(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    prepared = prepare_guided_grid_click(args, "recovery.guided_grid_click")
    if prepared["status"] != "SUCCESS":
        return prepared
    data = prepared["data"]
    request_id = str(data["request_id"])
    cell = str(data["cell"])
    planner_request = data["planner_request"]
    title = str(data["title"])
    target = str(data["target"])
    coordinate = (int(data["grid_coordinate"]["x"]), int(data["grid_coordinate"]["y"]))
    current_window_payload = data.get("current_target_window") if isinstance(data.get("current_target_window"), dict) else {}
    current_hwnd = int(current_window_payload.get("hwnd") or 0)
    before_capture = capture_target(
        {
            "target": target,
            "title": title,
            "hwnd": current_hwnd,
            "request_id": f"{request_id}_before_click",
        },
        with_grid=True,
    )
    if before_capture["status"] != "SUCCESS":
        return before_capture
    freshness = verify_planner_screen_unchanged(
        planner_request,
        before_capture,
        args,
        command="recovery.guided_grid_click",
    )
    if freshness["status"] != "SUCCESS":
        return freshness
    screen_x = int(data["screen_coordinate"]["x"])
    screen_y = int(data["screen_coordinate"]["y"])
    guardrail = evaluate_click_guardrails("recovery.guided_grid_click", args, screen_x, screen_y, current_window_payload)
    if guardrail is not None:
        return guardrail
    confirmation = recheck_guided_click(args, data, "recovery.guided_grid_click")
    if confirmation is not None:
        return confirmation
    check_command_cancelled()
    click_args = {
        "title": title,
        "target": "desktop",
        "x": screen_x,
        "y": screen_y,
        "coordinate_mode": "screen",
        "expected_result": args.get("expected_result") or planner_request.get("expected_result") or "",
    }
    clicked = cmd_input_click_point(click_args, before_press=lambda: guided_press_guard(data))
    if clicked["status"] != "SUCCESS":
        return clicked
    cancellable_sleep(float(args.get("post_click_delay") or 0.25))
    post_click_actions: list[dict[str, Any]] = []
    text_after_click = str(args.get("text_after_click") or "")
    if text_after_click:
        check_command_cancelled()
        typed = cmd_input_type_text({"target": "desktop", "text": text_after_click})
        post_click_actions.append({"action": "type_text", "result": typed})
        if typed["status"] != "SUCCESS":
            return typed
    keys_after_click = str(args.get("keys_after_click") or "")
    if keys_after_click:
        check_command_cancelled()
        keyed = cmd_input_hotkey({"target": "desktop", "keys": keys_after_click})
        post_click_actions.append({"action": "hotkey", "result": keyed})
        if keyed["status"] != "SUCCESS":
            return keyed
    cancellable_sleep(0.25)
    after_capture = capture_target(
        {
            "target": target,
            "title": title,
            "hwnd": current_hwnd,
            "request_id": f"{request_id}_after_click",
        },
        with_grid=False,
    )
    verification = verify_guided_result(planner_request, args)
    if verification["status"] == "SUCCESS":
        status = "SUCCESS"
    elif verification["status"] == "MANUAL_REQUIRED":
        status = "NEEDS_PLANNER"
    else:
        status = "FAILED"
    result_data = {
        "request_id": request_id,
        "cell": cell,
        "cell_bbox": data.get("cell_bbox"),
        "offset": data.get("offset"),
        "offset_pct": data.get("offset_pct"),
        "target_description": args.get("target_description") or "",
        "expected_result": args.get("expected_result") or planner_request.get("expected_result") or "",
        "grid_coordinate": {"x": coordinate[0], "y": coordinate[1]},
        "screen_coordinate": {"x": screen_x, "y": screen_y},
        "planner_request_path": str(planner_request_path(request_id)),
        "planner_grid_screenshot_path": planner_request.get("grid_screenshot_path"),
        "planner_raw_screenshot_path": planner_request.get("raw_screenshot_path"),
        "before_click_screenshot_path": (before_capture.get("data") or {}).get("raw_screenshot_path"),
        "pre_action_freshness": freshness.get("data") or {},
        "after_click_screenshot_path": (after_capture.get("data") or {}).get("raw_screenshot_path"),
        "click_result": clicked["data"],
        "post_click_actions": post_click_actions,
        "verification": verification,
        "post_action_diff": summarize_action_delta(
            before_capture,
            after_capture,
            clicked.get("data") if isinstance(clicked.get("data"), dict) else {},
        ),
    }
    follow_up = capture_guided_follow_up_observation(
        args,
        planner_request,
        title,
        current_hwnd,
        source_status=status,
    )
    if follow_up is not None:
        result_data["next_observation"] = follow_up
    report_path = write_guided_action_report(request_id, result_data)
    result_data["guided_action_report_path"] = str(report_path)
    if status == "NEEDS_PLANNER":
        return command_result(
            status,
            "recovery.guided_grid_click",
            "Guided grid click executed once; planner verification is required.",
            data=result_data,
            next_allowed_actions=["status.current", "recovery.capture_grid", "helper.commands"],
        )
    if status != "SUCCESS":
        return command_result(
            status,
            "recovery.guided_grid_click",
            "Guided grid click executed once but verification failed.",
            data=result_data,
        )
    return command_result(
        "SUCCESS",
        "recovery.guided_grid_click",
        "Guided grid click executed once and verified.",
        data=result_data,
        next_allowed_actions=["status.current", "recovery.capture_grid", "helper.commands"],
    )


def capture_guided_follow_up_observation(
    args: dict[str, Any],
    planner_request: dict[str, Any],
    title: str,
    current_hwnd: int,
    *,
    source_status: str,
) -> dict[str, Any] | None:
    if not bool(args.get("auto_follow_up_capture", True)):
        return None
    observe_after = args.get("observe_after") if isinstance(args.get("observe_after"), dict) else {}
    request_id = str(observe_after.get("request_id") or make_request_id())
    target = str(observe_after.get("target") or planner_request.get("target") or "foreground_window").strip().lower()
    planner_grid = planner_request.get("grid") if isinstance(planner_request.get("grid"), dict) else {}
    capture_args: dict[str, Any] = {
        "request_id": request_id,
        "target": target,
        "title": str(observe_after.get("title") or title),
        "columns": max(1, int(observe_after.get("columns") or planner_grid.get("columns") or 12)),
        "rows": max(1, int(observe_after.get("rows") or planner_grid.get("rows") or 8)),
        "reason": str(
            observe_after.get("reason")
            or f"Guided grid click finished with status {source_status}; planner needs the next visible state."
        ),
        "expected_result": str(observe_after.get("expected_result") or ""),
        "failed_command": "recovery.guided_grid_click",
    }
    for key in ("detail", "include_images", "image_max_edge"):
        if key in args:
            capture_args[key] = args[key]
    if target in {"target_window", "target_monitor"} and current_hwnd:
        capture_args["hwnd"] = current_hwnd
    capture = capture_target(capture_args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return {
            "status": capture["status"],
            "message": capture.get("message"),
            "data": strip_internal(capture),
        }
    capture_data = capture["data"]
    planner_follow_up = create_planner_request(
        capture_data,
        capture_args,
        failed_command="recovery.guided_grid_click",
        question=str(observe_after.get("question") or "What single safe next action should I try from this new state?"),
    )
    capture_data["planner_request_path"] = planner_follow_up["planner_request_path"]
    capture_data["planner_request"] = planner_follow_up
    capture_data["source_action"] = "guided_grid_click"
    capture_data["source_action_status"] = source_status
    return capture_data


def cmd_recovery_transient_grid_after_click(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    prepared = prepare_guided_grid_click(args, "recovery.transient_grid_after_click")
    if prepared["status"] != "SUCCESS":
        return prepared
    data = prepared["data"]
    request_id = str(data["request_id"])
    target = str(data["target"])
    title = str(data["title"])
    screen_x = int(data["screen_coordinate"]["x"])
    screen_y = int(data["screen_coordinate"]["y"])
    current_window_payload = data.get("current_target_window") if isinstance(data.get("current_target_window"), dict) else {}
    current_hwnd = int(current_window_payload.get("hwnd") or 0)
    before_capture = capture_target(
        {
            "target": target,
            "title": title,
            "hwnd": current_hwnd,
            "request_id": f"{request_id}_before_transient_click",
        },
        with_grid=True,
    )
    if before_capture["status"] != "SUCCESS":
        return before_capture
    freshness = verify_planner_screen_unchanged(
        data["planner_request"],
        before_capture,
        args,
        command="recovery.transient_grid_after_click",
    )
    if freshness["status"] != "SUCCESS":
        return freshness
    guardrail = evaluate_click_guardrails("recovery.transient_grid_after_click", args, screen_x, screen_y, current_window_payload)
    if guardrail is not None:
        return guardrail
    confirmation = recheck_guided_click(args, data, "recovery.transient_grid_after_click")
    if confirmation is not None:
        return confirmation
    click_args = {
        "title": title,
        "target": "desktop",
        "x": screen_x,
        "y": screen_y,
        "coordinate_mode": "screen",
        "expected_result": args.get("expected_result") or data["planner_request"].get("expected_result") or "",
    }
    check_command_cancelled()
    clicked = cmd_input_click_point(click_args, before_press=lambda: guided_press_guard(data))
    if clicked["status"] != "SUCCESS":
        return clicked
    wait_seconds = float(args.get("post_click_delay") or 0.35)
    cancellable_sleep(max(0.0, wait_seconds))
    capture_args = {
        "target": target,
        "title": title,
        "request_id": f"{request_id}_transient",
        "columns": int(args.get("columns") or 12),
        "rows": int(args.get("rows") or 8),
        "reason": args.get("reason") or "Transient UI opened after guided click; planner needs next action.",
        "expected_result": args.get("expected_result") or "",
        "failed_command": args.get("failed_command") or "recovery.transient_grid_after_click",
    }
    capture = capture_target(capture_args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return capture
    capture_data = capture["data"]
    planner_request = create_planner_request(
        capture_data,
        capture_args,
        failed_command=str(capture_args["failed_command"]),
        question=str(args.get("question") or "Using this transient UI grid, what single safe action should I try next?"),
    )
    capture_data["planner_request_path"] = planner_request["planner_request_path"]
    capture_data["planner_request"] = planner_request
    result_data = {
        "parent_request_id": request_id,
        "parent_cell": data["cell"],
        "target_description": args.get("target_description") or "",
        "screen_coordinate": data["screen_coordinate"],
        "click_result": clicked["data"],
        "transient_wait_seconds": wait_seconds,
        "transient_capture": capture_data,
        "foreground_after_capture": get_foreground_window_snapshot(),
        "preserve_foreground": True,
    }
    return command_result(
        "NEEDS_PLANNER",
        "recovery.transient_grid_after_click",
        "Guided click executed and transient grid captured.",
        data=result_data,
        next_allowed_actions=["recovery.guided_grid_click", "recovery.guided_grid_crop", "input.action_sequence", "helper.bridge_status"],
    )


def cmd_simon_detect_state(args: dict[str, Any]) -> dict[str, Any]:
    window = find_window_by_title(str(args.get("title") or TARGET_TITLE))
    state = read_json(STATE_FILE)
    data: dict[str, Any] = {"target_window": window_payload(window), "simon_state": state}
    if window is None:
        return command_result("UNCERTAIN", "simon.detect_state", "Simon Target window not found.", data=data)
    capture = capture_window_image(window)
    if capture is None:
        return command_result("UNCERTAIN", "simon.detect_state", "Could not capture target window.", data=data)
    detected = detect_simon_buttons(capture, window.left, window.top)
    data["detected_buttons"] = detected_payload(detected)
    data["detection_confidence"] = min((button.confidence for button in detected.values()), default=0.0)
    if len(detected) != len(COLOR_ORDER):
        return command_result("UNCERTAIN", "simon.detect_state", "Not all Simon buttons were detected.", data=data)
    return command_result("SUCCESS", "simon.detect_state", "Simon state detected.", data=data)


def cmd_simon_press_next(args: dict[str, Any]) -> dict[str, Any]:
    check_command_cancelled()
    if pyautogui is None:
        return command_result("ERROR", "simon.press_next", "pyautogui is not installed.")
    detect = cmd_simon_detect_state(args)
    if detect["status"] != "SUCCESS":
        return detect
    state = detect["data"].get("simon_state") or {}
    expected = state.get("expected_color")
    if not expected:
        return command_result("UNCERTAIN", "simon.press_next", "Simon target did not report an expected color.", data=detect["data"])
    button = detect["data"]["detected_buttons"].get(expected)
    if not button:
        return command_result("UNCERTAIN", "simon.press_next", f"Expected color {expected!r} was not detected.", data=detect["data"])
    before_index = int(state.get("index", -1))
    focus = cmd_focus_window(args)
    if focus["status"] != "SUCCESS":
        return focus
    center = button["center"]
    check_command_cancelled()
    with keyboard_only_pyautogui_context():
        pyautogui.click(center["x"], center["y"])
    cancellable_sleep(0.25)
    after = read_json(STATE_FILE) or {}
    after_index = int(after.get("index", -1))
    verified = after_index == before_index + 1 and after.get("last_input") == expected and after.get("status") != "Wrong"
    data = {
        "expected_color": expected,
        "clicked": center,
        "before_state": state,
        "after_state": after,
        "target_window": detect["data"].get("target_window"),
    }
    if not verified:
        return command_result("FAILED", "simon.press_next", "Click did not verify expected progress.", data=data)
    return command_result("SUCCESS", "simon.press_next", f"Verified progress {before_index}->{after_index}.", data=data)


def cmd_simon_force_grid_request(args: dict[str, Any]) -> dict[str, Any]:
    state = read_json(STATE_FILE) or {}
    expected = state.get("expected_color")
    if not expected:
        return command_result("UNCERTAIN", "simon.force_grid_request", "Simon target did not report an expected color.")
    capture_args = dict(args)
    capture_args["reason"] = args.get("reason") or f"Forced Simon grid recovery for expected color {expected}."
    capture_args["failed_command"] = "simon.press_next"
    capture_args["expected_result"] = f"Simon progress increments after clicking {expected}."
    capture_args["verification"] = {
        "type": "simon_progress_increment",
        "before_index": int(state.get("index", -1)),
        "expected_color": expected,
    }
    capture = capture_target(capture_args, with_grid=True)
    if capture["status"] != "SUCCESS":
        return capture
    data = capture["data"]
    request = create_planner_request(
        data,
        capture_args,
        failed_command="simon.press_next",
        question=f"Using the grid screenshot, which single cell should be clicked for the Simon color {expected}?",
    )
    request["verification"] = capture_args["verification"]
    request["expected_result"] = capture_args["expected_result"]
    write_planner_request(request)
    data["planner_request_path"] = request["planner_request_path"]
    data["planner_request"] = request
    data["expected_color"] = expected
    return command_result(
        "NEEDS_PLANNER",
        "simon.force_grid_request",
        f"Forced Simon grid recovery request created for {expected}.",
        data=data,
    )


def capture_image_transform(image: Any, method: str, window: dict | None, monitor: dict | None) -> dict:
    bounds = {}
    verified = False
    # Only routes with a known physical pixel origin can authorize input.
    if method in {"window_pillow_all_screens", "window_gdi_region", "window_printwindow"}:
        bounds = (window or {}).get("bounds") or {}
    elif method in {"monitor_pillow_all_screens", "monitor_gdi_region"}:
        bounds = (monitor or {}).get("bounds") or {}
    elif method in {"desktop_pillow_all_screens", "desktop_gdi_virtual_screen"}:
        user32 = ctypes.windll.user32
        bounds = {"left": int(user32.GetSystemMetrics(76)), "top": int(user32.GetSystemMetrics(77)),
                  "width": int(user32.GetSystemMetrics(78)), "height": int(user32.GetSystemMetrics(79))}
    elif method in {"desktop_pillow", "desktop_pyautogui"}:
        bounds = {"left": 0, "top": 0, "width": int(ctypes.windll.user32.GetSystemMetrics(0)),
                  "height": int(ctypes.windll.user32.GetSystemMetrics(1))}
    if bounds:
        verified = image.width == bounds.get("width") and image.height == bounds.get("height")
    if method in {"window_pillow_all_screens", "window_gdi_region"}:
        verified = verified and int(get_foreground_window_snapshot().get("hwnd") or 0) == int((window or {}).get("hwnd") or -1)
    result = image_transform(image.width, image.height, bounds, route=method, verified=verified)
    result["monitor"] = monitor
    result["dpi_awareness"] = DPI_AWARENESS
    return result


def capture_target(args: dict[str, Any], with_grid: bool = False, *, record_route=None) -> dict[str, Any]:
    recorder = record_route or record_capture_route
    started = time.monotonic()
    try:
        return _capture_target_impl(args, with_grid, recorder)
    except CommandCancelled:
        raise
    except Exception as exc:
        recorder("capture_failed", str(args.get("target") or "target_window"), "UNCERTAIN", started,
                 [{"method": "capture_exception", "status": "FAILED", "error": str(exc)}])
        return command_result("UNCERTAIN", "observe.screenshot", f"Capture failed: {exc}")


def _capture_target_impl(args: dict[str, Any], with_grid: bool, recorder) -> dict[str, Any]:
    if ImageGrab is None:
        return command_result("ERROR", "observe.screenshot", "Pillow is not installed.")
    target = str(args.get("target") or "target_window")
    title = str(args.get("title") or TARGET_TITLE)
    request_id = str(args.get("request_id") or make_request_id())
    request_token = safe_request_id(request_id)
    desktop_origin = None
    monitor = None
    capture_started = time.monotonic()
    capture_method = ""
    capture_attempts: list[dict[str, Any]] = []
    synthetic_windows: list[dict[str, Any]] = []
    if target == "target_window":
        hwnd = int(args.get("hwnd") or 0)
        window = window_info_from_hwnd(hwnd) if hwnd else find_best_window_for_args(args)
        if window is None:
            return command_result("UNCERTAIN", "observe.screenshot", f"Window {title!r} not found.")
        image, capture_meta = capture_window_image_with_details(window)
        capture_method = str(capture_meta.get("method") or "")
        capture_attempts = list(capture_meta.get("attempts") or [])
        if image is None:
            image, fallback_meta = capture_fallback_for_window(window, allow_synthetic=True)
            capture_method = str(fallback_meta.get("method") or "")
            capture_attempts.extend(list(fallback_meta.get("attempts") or []))
            synthetic_windows = list(fallback_meta.get("synthetic_windows") or [])
            if image is None:
                error = fallback_meta.get("error") or capture_meta.get("error") or simon_capture.LAST_CAPTURE_ERROR
                detail = f"Could not capture target window: {error}" if error else "Could not capture target window."
                recorder("capture_failed", target, "UNCERTAIN", capture_started, capture_attempts)
                return command_result("UNCERTAIN", "observe.screenshot", detail)
        target_window = window_payload(window)
        monitor = monitor_payload(monitor_for_window(window))
    elif target in {"foreground_window", "active_window", "current_dialog"}:
        window = resolve_context_window_for_args(args)
        if window is None:
            message = "Current dialog not found." if target == "current_dialog" else "Foreground window not found."
            return command_result("UNCERTAIN", "observe.screenshot", message)
        image, capture_meta = capture_window_image_with_details(window)
        capture_method = str(capture_meta.get("method") or "")
        capture_attempts = list(capture_meta.get("attempts") or [])
        if image is None:
            image, fallback_meta = capture_fallback_for_window(window, allow_synthetic=True)
            capture_method = str(fallback_meta.get("method") or "")
            capture_attempts.extend(list(fallback_meta.get("attempts") or []))
            synthetic_windows = list(fallback_meta.get("synthetic_windows") or [])
            if image is None:
                detail = f"Could not capture foreground window: {simon_capture.LAST_CAPTURE_ERROR}" if simon_capture.LAST_CAPTURE_ERROR else "Could not capture foreground window."
                recorder("capture_failed", target, "UNCERTAIN", capture_started, capture_attempts)
                return command_result("UNCERTAIN", "observe.screenshot", detail)
        target_window = window_payload(window)
        monitor = monitor_payload(monitor_for_window(window))
    elif target in {"target_monitor", "foreground_monitor", "active_monitor", "current_dialog_monitor"}:
        if target == "target_monitor":
            hwnd = int(args.get("hwnd") or 0)
            window = window_info_from_hwnd(hwnd) if hwnd else find_best_window_for_args(args)
        elif target == "current_dialog_monitor":
            window = resolve_current_dialog_window(args)
        else:
            window = resolve_context_window_for_args(args)
        if window is None:
            return command_result("UNCERTAIN", "observe.screenshot", f"Window {title!r} not found for monitor capture.")
        monitor_info = monitor_for_window(window)
        if monitor_info is None:
            return command_result("UNCERTAIN", "observe.screenshot", "Monitor for target window could not be determined.")
        image, capture_meta = capture_monitor_image_with_details(monitor_info)
        capture_method = str(capture_meta.get("method") or "")
        capture_attempts = list(capture_meta.get("attempts") or [])
        if image is None:
            image, fallback_meta = capture_window_image_with_details(window)
            capture_method = str(fallback_meta.get("method") or "")
            capture_attempts.extend(list(fallback_meta.get("attempts") or []))
            if image is None:
                image, synthetic_meta = capture_synthetic_visible_windows()
                capture_method = str(synthetic_meta.get("method") or "")
                capture_attempts.extend(list(synthetic_meta.get("attempts") or []))
                synthetic_windows = list(synthetic_meta.get("synthetic_windows") or [])
            if image is None:
                detail = f"Could not capture target monitor: {simon_capture.LAST_CAPTURE_ERROR}" if simon_capture.LAST_CAPTURE_ERROR else "Could not capture target monitor."
                recorder("capture_failed", target, "UNCERTAIN", capture_started, capture_attempts)
                return command_result("UNCERTAIN", "observe.screenshot", detail)
        target_window = window_payload(window)
        monitor = monitor_payload(monitor_info)
        desktop_origin = (monitor_info.left, monitor_info.top) if capture_method.startswith("monitor_") else None
    elif target == "desktop":
        image, capture_meta = capture_desktop_image_with_details()
        capture_method = str(capture_meta.get("method") or "")
        capture_attempts = list(capture_meta.get("attempts") or [])
        if image is None:
            image, synthetic_meta = capture_synthetic_visible_windows()
            capture_method = str(synthetic_meta.get("method") or "")
            capture_attempts.extend(list(synthetic_meta.get("attempts") or []))
            synthetic_windows = list(synthetic_meta.get("synthetic_windows") or [])
            if image is None:
                detail = f"Desktop screenshot failed: {simon_capture.LAST_CAPTURE_ERROR}" if simon_capture.LAST_CAPTURE_ERROR else "Desktop screenshot failed."
                recorder("capture_failed", target, "UNCERTAIN", capture_started, capture_attempts)
                return command_result("UNCERTAIN", "observe.screenshot", detail)
        target_window = None
        desktop_origin = get_virtual_screen_origin() if capture_method.startswith("desktop_") else None
    else:
        return command_result("ERROR", "observe.screenshot", f"Unknown capture target {target!r}.")
    transform = capture_image_transform(image, capture_method, target_window, monitor)
    COMMAND_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = COMMAND_SCREENSHOT_DIR / f"{request_token}_raw.png"
    image.save(raw_path)
    record_generated_artifact(raw_path)
    capture_elapsed_ms = round((time.monotonic() - capture_started) * 1000.0, 1)
    data: dict[str, Any] = {
        "request_id": request_id,
        "target": target,
        "raw_screenshot_path": str(raw_path),
        "screenshot_width": image.width,
        "screenshot_height": image.height,
        "target_window": target_window,
        "monitor": monitor,
        "foreground_window": get_foreground_window_snapshot(),
        "capture_timestamp": now_iso(),
        "image_to_screen": transform,
        "dpi_awareness": DPI_AWARENESS,
        "capture_method": capture_method,
        "capture_route": {
            "method": capture_method,
            "target": target,
            "attempts": capture_attempts,
            "elapsed_ms": capture_elapsed_ms,
        },
    }
    if synthetic_windows:
        data["synthetic_windows"] = synthetic_windows
    active_context = build_active_context_packet_for_capture(title, target_window)
    data["active_context"] = active_context
    data["action_menu"] = build_action_menu(active_context, planner_intent_from_args(args))
    data["planner_coaching"] = build_planner_coaching(active_context, args)
    data["working_memory"] = load_planner_working_memory()
    if desktop_origin is not None:
        data["desktop_origin"] = {"x": desktop_origin[0], "y": desktop_origin[1]}
    if with_grid:
        columns = max(1, int(args.get("columns") or 12))
        rows = max(1, int(args.get("rows") or 8))
        grid_image, render = render_planner_grid(image, columns, rows)
        data["grid_render"] = render
        grid_path = COMMAND_SCREENSHOT_DIR / f"{request_token}_grid.png"
        grid_image.save(grid_path)
        record_generated_artifact(grid_path)
        data["grid_screenshot_path"] = str(grid_path)
        data["grid"] = {
            "columns": columns,
            "rows": rows,
            "cell_width": image.width / columns,
            "cell_height": image.height / rows,
            "coordinate_origin": (
                "synthetic_context"
                if capture_method == "synthetic_visible_windows"
                else "desktop"
                if desktop_origin is not None
                else "target_window"
            ),
        }
    overlay_path = save_candidate_overlay(image, data, request_id) if not with_grid or args.get("detail") == "full" or "candidates" in args.get("include_images", []) else None
    if overlay_path:
        data["candidate_overlay_screenshot_path"] = str(overlay_path)
    recorder(capture_method, target, "SUCCESS", capture_started, capture_attempts)
    return {"status": "SUCCESS", "data": data, "image": image}


def capture_fallback_for_window(window: WindowInfo, allow_synthetic: bool = True) -> tuple[Any | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    monitor = monitor_for_window(window)
    if monitor is not None:
        image, monitor_meta = capture_monitor_image_with_details(monitor)
        attempts.extend(list(monitor_meta.get("attempts") or []))
        if image is not None:
            return image, {"method": monitor_meta.get("method"), "attempts": attempts}
    image, desktop_meta = capture_desktop_image_with_details()
    attempts.extend(list(desktop_meta.get("attempts") or []))
    if image is not None:
        return image, {"method": desktop_meta.get("method"), "attempts": attempts}
    if allow_synthetic:
        image, synthetic_meta = capture_synthetic_visible_windows()
        attempts.extend(list(synthetic_meta.get("attempts") or []))
        if image is not None:
            return image, {
                "method": synthetic_meta.get("method"),
                "attempts": attempts,
                "synthetic_windows": synthetic_meta.get("synthetic_windows") or [],
            }
    return None, {"method": "capture_failed", "attempts": attempts}


def capture_synthetic_visible_windows(limit: int = 12) -> tuple[Any | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    if Image is None or ImageDraw is None:
        attempts.append({"method": "synthetic_visible_windows", "status": "SKIPPED", "error": "Pillow drawing is not available."})
        return None, {"method": "capture_failed", "attempts": attempts}
    windows = [
        window
        for window in list_windows()
        if window.width > 80 and window.height > 40 and not is_helper_window_snapshot(window_payload(window))
    ][:limit]
    if not windows:
        attempts.append({"method": "synthetic_visible_windows", "status": "FAILED", "error": "No visible non-helper windows found."})
        return None, {"method": "capture_failed", "attempts": attempts}
    min_left = min(window.left for window in windows)
    min_top = min(window.top for window in windows)
    max_right = max(window.right for window in windows)
    max_bottom = max(window.bottom for window in windows)
    virtual_width = max(1, max_right - min_left)
    virtual_height = max(1, max_bottom - min_top)
    canvas_width = 1600
    canvas_height = max(600, min(1200, int(canvas_width * virtual_height / virtual_width)))
    scale = min(canvas_width / virtual_width, canvas_height / virtual_height)
    image = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default() if ImageFont else None
    synthetic_windows: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        x1 = int((window.left - min_left) * scale)
        y1 = int((window.top - min_top) * scale)
        x2 = max(x1 + 8, int((window.right - min_left) * scale))
        y2 = max(y1 + 8, int((window.bottom - min_top) * scale))
        thumb = None
        thumb_method = ""
        try:
            thumb = capture_window_printwindow(window)
            thumb_method = "printwindow"
        except Exception as exc:
            thumb_method = f"unavailable: {exc}"
        draw.rectangle((x1, y1, x2, y2), outline=(24, 96, 180), width=3)
        label = f"{index}: {window.title[:70]}"
        draw.rectangle((x1, max(0, y1 - 18), min(canvas_width - 1, x1 + 8 + len(label) * 6), y1), fill=(24, 96, 180))
        draw.text((x1 + 4, max(0, y1 - 16)), label, fill="white", font=font)
        if thumb is not None:
            thumb.thumbnail((max(16, x2 - x1), max(16, y2 - y1)))
            image.paste(thumb.convert("RGB"), (x1, y1))
            draw.rectangle((x1, y1, x2, y2), outline=(24, 96, 180), width=3)
        synthetic_windows.append(
            {
                "index": index,
                "capture_method": thumb_method,
                "window": window_payload(window),
                "synthetic_bounds": {"left": x1, "top": y1, "right": x2, "bottom": y2, "width": x2 - x1, "height": y2 - y1},
            }
        )
    attempts.append({"method": "synthetic_visible_windows", "status": "SUCCESS", "window_count": len(synthetic_windows)})
    return image, {"method": "synthetic_visible_windows", "attempts": attempts, "synthetic_windows": synthetic_windows}


def save_candidate_overlay(image: Any, capture_data: dict[str, Any], request_id: str) -> Path | None:
    active_context = capture_data.get("active_context") if isinstance(capture_data.get("active_context"), dict) else {}
    candidates = active_context.get("child_candidates") if isinstance(active_context.get("child_candidates"), list) else []
    if not candidates or ImageDraw is None:
        return None
    origin = capture_origin_for_candidate_overlay(capture_data)
    overlay_image = image.copy()
    draw_candidate_overlay(overlay_image, candidates, origin)
    overlay_path = COMMAND_SCREENSHOT_DIR / f"{safe_request_id(request_id)}_candidates.png"
    overlay_image.save(overlay_path)
    record_generated_artifact(overlay_path)
    return overlay_path


def capture_origin_for_candidate_overlay(capture_data: dict[str, Any]) -> tuple[int, int]:
    desktop_origin = capture_data.get("desktop_origin") if isinstance(capture_data.get("desktop_origin"), dict) else None
    if desktop_origin:
        return int(desktop_origin.get("x") or 0), int(desktop_origin.get("y") or 0)
    target_window = capture_data.get("target_window") if isinstance(capture_data.get("target_window"), dict) else None
    bounds = target_window.get("bounds") if isinstance(target_window, dict) and isinstance(target_window.get("bounds"), dict) else None
    if bounds:
        return int(bounds.get("left") or 0), int(bounds.get("top") or 0)
    return get_virtual_screen_origin()


def draw_candidate_overlay(image: Any, candidates: list[dict[str, Any]], origin: tuple[int, int]) -> None:
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default() if ImageFont else None
    origin_x, origin_y = origin
    width, height = image.size
    for candidate in candidates:
        bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
        left = int(bounds.get("left") or 0) - origin_x
        top = int(bounds.get("top") or 0) - origin_y
        right = int(bounds.get("right") or 0) - origin_x
        bottom = int(bounds.get("bottom") or 0) - origin_y
        if right <= 0 or bottom <= 0 or left >= width or top >= height:
            continue
        left = max(0, left)
        top = max(0, top)
        right = min(width - 1, right)
        bottom = min(height - 1, bottom)
        label = str(candidate.get("candidate_id") or "?")
        kind = str(candidate.get("kind") or "")
        color = (45, 212, 191) if kind == "text_field" else (250, 204, 21)
        if kind in {"primary_button", "button"}:
            color = (34, 197, 94)
        elif kind in {"dropdown", "link"}:
            color = (96, 165, 250)
        draw.rectangle((left, top, right, bottom), outline=color, width=3)
        text_bbox = draw.textbbox((0, 0), label, font=font) if hasattr(draw, "textbbox") else (0, 0, 24, 14)
        label_w = text_bbox[2] - text_bbox[0] + 8
        label_h = text_bbox[3] - text_bbox[1] + 6
        draw.rectangle((left, top, left + label_w, top + label_h), fill=(17, 24, 39), outline=color, width=1)
        draw.text((left + 4, top + 3), label, fill=(255, 255, 255), font=font)


def detect_simon_buttons(image: Any, offset_x: int, offset_y: int) -> dict[str, DetectedButton]:
    rgb_image = image.convert("RGB")
    width, height = rgb_image.size
    pixels = rgb_image.load()
    results: dict[str, DetectedButton] = {}
    for color in COLOR_ORDER:
        target = COLOR_SPECS[color]["rgb"]
        visited: set[tuple[int, int]] = set()
        best_component: tuple[int, int, int, int, int] | None = None
        for y in range(height):
            for x in range(width):
                if (x, y) in visited:
                    continue
                if not is_color_match(pixels[x, y], target):
                    continue
                component = flood_fill_color_region(pixels, width, height, x, y, target, visited)
                count, min_x, min_y, max_x, max_y = component
                comp_width = max_x - min_x + 1
                comp_height = max_y - min_y + 1
                if count < MIN_COLOR_PIXELS or comp_width < 80 or comp_height < 60:
                    continue
                if best_component is None or count > best_component[0]:
                    best_component = component
        if best_component is None:
            continue
        count, min_x, min_y, max_x, max_y = best_component
        bbox = (offset_x + min_x, offset_y + min_y, offset_x + max_x, offset_y + max_y)
        center = ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)
        component_area = max(1, (max_x - min_x + 1) * (max_y - min_y + 1))
        confidence = min(1.0, max(0.0, (count / component_area) / 0.55))
        results[color] = DetectedButton(color=color, bbox=bbox, center=center, pixels=count, confidence=confidence)
    return results


def is_color_match(rgb: tuple[int, int, int], target: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    distance = math.sqrt((r - target[0]) ** 2 + (g - target[1]) ** 2 + (b - target[2]) ** 2)
    return distance <= COLOR_TOLERANCE


def flood_fill_color_region(
    pixels: Any,
    width: int,
    height: int,
    start_x: int,
    start_y: int,
    target: tuple[int, int, int],
    visited: set[tuple[int, int]],
) -> tuple[int, int, int, int, int]:
    stack = [(start_x, start_y)]
    visited.add((start_x, start_y))
    count = 0
    min_x = max_x = start_x
    min_y = max_y = start_y
    while stack:
        x, y = stack.pop()
        count += 1
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
        for next_x, next_y in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if next_x < 0 or next_y < 0 or next_x >= width or next_y >= height:
                continue
            point = (next_x, next_y)
            if point in visited:
                continue
            if not is_color_match(pixels[next_x, next_y], target):
                continue
            visited.add(point)
            stack.append(point)
    return count, min_x, min_y, max_x, max_y


def list_windows(partial_title: str | None = None) -> list[WindowInfo]:
    user32 = ctypes.windll.user32
    matches: list[WindowInfo] = []
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if partial_title and partial_title.lower() not in title.lower():
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        matches.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
                minimized=bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return matches


def find_likely_search_window() -> WindowInfo | None:
    search_tokens = ("search", "start", "windows input experience")
    for window in list_windows():
        if is_helper_window_snapshot(window_payload(window)):
            continue
        title = window.title.lower()
        class_name = get_class_name(window.hwnd).lower()
        process_name = get_process_name_for_hwnd(window.hwnd).lower()
        if any(token in title for token in search_tokens):
            return window
        if "search" in class_name or "search" in process_name or "searchhost" in process_name:
            return window
    return None


def collect_environment_diagnostics(include_capture: bool = True) -> dict[str, Any]:
    ensure_runtime_dir()
    windows = list_windows()
    foreground = get_foreground_window_snapshot()
    capture_probe: dict[str, Any] = {"attempted": False}
    if include_capture:
        capture_probe = probe_desktop_capture()
    diagnostics = {
        "schema": "helper-environment-diagnostics.v1",
        "timestamp": now_iso(),
        "process": {
            "pid": os.getpid(),
            "ppid": getattr(os, "getppid", lambda: None)(),
            "executable": sys.executable,
            "argv": sys.argv,
            "cwd": str(Path.cwd()),
            "username": os.environ.get("USERNAME") or os.environ.get("USER"),
            "sessionname": os.environ.get("SESSIONNAME"),
            "computername": os.environ.get("COMPUTERNAME"),
        },
        "desktop": desktop_identity_probe(),
        "window_probe": {
            "visible_titled_window_count": len(windows),
            "foreground_window": foreground,
            "sample_windows": [window_payload(window) for window in windows[:12]],
        },
        "capture_probe": capture_probe,
        "helper_runtime": helper_runtime_status(),
        "bridge_status": read_json(HELPER_BRIDGE_STATUS_FILE),
        "instance_lock": read_lock_file_snapshot(HELPER_INSTANCE_LOCK_FILE),
    }
    from screensaver_control import probe
    diagnostics["input_desktop"] = probe()
    diagnostics["classification"] = classify_environment(diagnostics)
    return diagnostics


def classify_environment(diagnostics: dict[str, Any]) -> dict[str, Any]:
    desktop = diagnostics.get("desktop") if isinstance(diagnostics.get("desktop"), dict) else {}
    window_probe = diagnostics.get("window_probe") if isinstance(diagnostics.get("window_probe"), dict) else {}
    capture_probe = diagnostics.get("capture_probe") if isinstance(diagnostics.get("capture_probe"), dict) else {}
    thread_desktop = str(desktop.get("thread_desktop") or "")
    window_station = str(desktop.get("window_station") or "")
    window_count = int(window_probe.get("visible_titled_window_count") or 0)
    capture_status = str(capture_probe.get("status") or "")
    if "CodexSandboxDesktop" in thread_desktop:
        desktop_kind = "sandbox"
    elif window_station == "WinSta0" and thread_desktop.lower() in {"default", "winlogon"}:
        desktop_kind = "interactive"
    elif thread_desktop:
        desktop_kind = "unknown_desktop"
    else:
        desktop_kind = "unknown"
    ready_for_live_ui = desktop_kind == "interactive" and window_count > 0
    input_state = diagnostics.get("input_desktop")
    if input_state is not None and not input_state.get("input_ready"):
        ready_for_live_ui = False
        state = "screensaver_active" if input_state.get("screensaver_running") else "input_desktop_unavailable"
        message = "Input desktop is unavailable. Inspect input_desktop; use recovery.stop_screensaver only for an active screen saver."
    elif ready_for_live_ui:
        state = "interactive_visible"
        message = "Process is on an interactive desktop and can enumerate visible titled windows."
    elif desktop_kind == "sandbox":
        state = "sandbox_isolated"
        message = "Process is on the Codex sandbox desktop, so it cannot see the user's interactive windows."
    elif window_count <= 0:
        state = "no_visible_windows"
        message = "Process is not enumerating visible titled windows."
    else:
        state = "partial"
        message = "Process can enumerate windows, but the desktop classification is not confirmed interactive."
    return {
        "state": state,
        "message": message,
        "desktop_kind": desktop_kind,
        "ready_for_live_ui": ready_for_live_ui,
        "window_count": window_count,
        "capture_status": capture_status or None,
        "thread_desktop": thread_desktop or None,
        "window_station": window_station or None,
    }


def classify_helper_launch(
    before: dict[str, Any],
    after: dict[str, Any],
    launch_report: dict[str, Any],
    after_runtime: dict[str, Any],
) -> dict[str, Any]:
    after_env = after.get("classification") if isinstance(after.get("classification"), dict) else {}
    if after_runtime.get("responsive"):
        if after_env.get("desktop_kind") == "sandbox":
            return {
                "status": "running_sandboxed",
                "message": f"{HELPER_SHORT_NAME} is responsive, but the launcher environment is still sandboxed.",
                "bridge_responsive": True,
            }
        return {
            "status": "running_interactive",
            "message": f"{HELPER_SHORT_NAME} is running and responsive.",
            "bridge_responsive": True,
        }
    if after_runtime.get("instance_lock", {}).get("exists"):
        return {
            "status": "blocked_by_lock_or_dead_heartbeat",
            "message": f"{HELPER_SHORT_NAME} launch attempted, but the bridge is not responsive and an instance lock remains.",
            "bridge_responsive": False,
        }
    launch_started_epoch = float(launch_report.get("launch_started_epoch") or 0.0)
    elevated_launcher_report = read_elevated_launcher_report(launch_report)
    elevated_child_report = read_elevated_child_report(launch_report)
    if isinstance(elevated_child_report, dict):
        child_status = str(elevated_child_report.get("status") or "")
        if child_status in {"failed", "python_exited"}:
            return {
                "status": "elevated_child_exited",
                "message": f"{HELPER_SHORT_NAME} elevated child runner exited before a bridge heartbeat appeared.",
                "bridge_responsive": False,
                "elevated_launcher": elevated_launcher_report,
                "elevated_child": elevated_child_report,
            }
    if isinstance(elevated_launcher_report, dict):
        if elevated_launcher_report.get("status") == "failed":
            return {
                "status": "elevated_launch_failed",
                "message": f"{HELPER_SHORT_NAME} elevated launcher failed before a bridge heartbeat appeared.",
                "bridge_responsive": False,
                "elevated_launcher": elevated_launcher_report,
            }
        if elevated_launcher_report.get("status") == "started":
            return {
                "status": "heartbeat_missing",
                "message": f"{HELPER_SHORT_NAME} elevated process started, but no fresh bridge heartbeat appeared.",
                "bridge_responsive": False,
                "launched_pid": elevated_launcher_report.get("elevated_pid"),
                "elevated_launcher": elevated_launcher_report,
                "elevated_child": elevated_child_report,
                "before_desktop": (before.get("classification") or {}).get("desktop_kind") if isinstance(before.get("classification"), dict) else None,
                "after_desktop": after_env.get("desktop_kind"),
            }
    exit_report = read_fresh_helper_launch_report(RUNTIME_DIR / "agent_helper_last_exit.json", launch_started_epoch)
    exception_report = read_fresh_helper_launch_report(RUNTIME_DIR / "agent_helper_last_exception.json", launch_started_epoch)
    if isinstance(exception_report, dict) and exception_report:
        return {
            "status": "crashed",
            "message": f"{HELPER_SHORT_NAME} launch attempted and an exception report was written.",
            "bridge_responsive": False,
            "exception": exception_report.get("exception"),
        }
    if isinstance(exit_report, dict) and exit_report:
        return {
            "status": "exited",
            "message": f"{HELPER_SHORT_NAME} launch attempted and the app wrote an exit report.",
            "bridge_responsive": False,
            "exit_reason": exit_report.get("reason"),
        }
    return {
        "status": "heartbeat_missing",
        "message": f"{HELPER_SHORT_NAME} launch attempted, but no fresh bridge heartbeat appeared.",
        "bridge_responsive": False,
        "launched_pid": launch_report.get("pid"),
        "before_desktop": (before.get("classification") or {}).get("desktop_kind") if isinstance(before.get("classification"), dict) else None,
        "after_desktop": after_env.get("desktop_kind"),
    }


def read_fresh_helper_launch_report(path: Path, launch_started_epoch: float) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        if launch_started_epoch and path.stat().st_mtime < launch_started_epoch:
            return None
    except OSError:
        return None
    report = read_json(path)
    if not isinstance(report, dict) or not report:
        return None
    return report


def read_elevated_launcher_report(launch_report: dict[str, Any]) -> dict[str, Any] | None:
    report_path = launch_report.get("elevated_launcher_report_path")
    if not report_path:
        return None
    try:
        path = Path(str(report_path))
    except Exception:
        return None
    report = read_json(path)
    if not isinstance(report, dict) or not report:
        return None
    return report


def read_elevated_child_report(launch_report: dict[str, Any]) -> dict[str, Any] | None:
    report_path = launch_report.get("elevated_child_report_path")
    if not report_path:
        return None
    try:
        path = Path(str(report_path))
    except Exception:
        return None
    report = read_json(path)
    if not isinstance(report, dict) or not report:
        return None
    return report


def desktop_identity_probe() -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        return {"available": False, "reason": "ctypes.windll unavailable"}
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    UOI_NAME = 2

    def object_name(handle: int) -> str:
        if not handle:
            return ""
        needed = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
        if needed.value <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(max(1, needed.value // ctypes.sizeof(ctypes.c_wchar)))
        if not user32.GetUserObjectInformationW(handle, UOI_NAME, buffer, needed.value, ctypes.byref(needed)):
            return ""
        return buffer.value

    try:
        window_station = user32.GetProcessWindowStation()
        desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        return {
            "available": True,
            "window_station": object_name(window_station),
            "thread_desktop": object_name(desktop),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def probe_desktop_capture() -> dict[str, Any]:
    simon_capture.LAST_CAPTURE_ERROR = ""
    started = time.time()
    image = capture_desktop_image()
    elapsed_ms = round((time.time() - started) * 1000, 1)
    if image is None:
        return {
            "attempted": True,
            "status": "FAILED",
            "elapsed_ms": elapsed_ms,
            "error": simon_capture.LAST_CAPTURE_ERROR or "desktop capture returned no image",
        }
    try:
        width, height = image.size
    except Exception:
        width = height = None
    return {
        "attempted": True,
        "status": "SUCCESS",
        "elapsed_ms": elapsed_ms,
        "size": {"width": width, "height": height},
    }


def window_info_from_hwnd(hwnd: int) -> WindowInfo | None:
    if not hwnd:
        return None
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return None
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return WindowInfo(
        hwnd=int(hwnd),
        title=buffer.value.strip(),
        left=int(rect.left),
        top=int(rect.top),
        right=int(rect.right),
        bottom=int(rect.bottom),
        minimized=bool(user32.IsIconic(hwnd)),
    )


def find_window_by_title(exact_title: str) -> WindowInfo | None:
    matches = [window for window in list_windows() if window.title == exact_title]
    if not matches:
        return None
    visible = [item for item in matches if not item.minimized and item.width > 100 and item.height > 100]
    return visible[0] if visible else matches[0]


def bring_window_to_front(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    current_thread = int(kernel32.GetCurrentThreadId())

    def confirm_foreground() -> bool:
        foreground = get_foreground_window_snapshot()
        return int(foreground.get("hwnd") or 0) == int(hwnd)

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        try:
            user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass
        if user32.SetForegroundWindow(hwnd):
            cancellable_sleep(0.05)
            if confirm_foreground():
                return True
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.SetForegroundWindow(hwnd)
            cancellable_sleep(0.05)
            if confirm_foreground():
                return True
        finally:
            try:
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
        try:
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
            cancellable_sleep(0.05)
            if confirm_foreground():
                return True
        except Exception:
            pass
        try:
            switch_to_this_window = getattr(user32, "SwitchToThisWindow", None)
            if switch_to_this_window is not None:
                switch_to_this_window(hwnd, True)
                cancellable_sleep(0.05)
                if confirm_foreground():
                    return True
        except Exception:
            pass
        foreground_hwnd = int(user32.GetForegroundWindow() or 0)
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground_hwnd, None)) if foreground_hwnd else 0
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        attached_target = False
        attached_foreground = False
        try:
            if target_thread and target_thread != current_thread:
                attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
            if foreground_thread and foreground_thread != current_thread and foreground_thread != target_thread:
                attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
            user32.BringWindowToTop(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
            if user32.SetForegroundWindow(hwnd):
                cancellable_sleep(0.05)
                if confirm_foreground():
                    return True
            try:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                cancellable_sleep(0.05)
                if confirm_foreground():
                    return True
            except Exception:
                pass
        finally:
            if attached_foreground:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
            if attached_target:
                user32.AttachThreadInput(current_thread, target_thread, False)
    except Exception:
        return False
    return False


def get_window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def window_activation_point(window: WindowInfo) -> tuple[int, int] | None:
    if window.width <= 0 or window.height <= 0:
        return None
    title_bar_height = min(32, max(14, window.height // 18))
    screen_x = window.left + max(40, min(window.width // 2, window.width - 40))
    screen_y = window.top + min(title_bar_height, max(8, window.height - 1))
    return int(screen_x), int(screen_y)


def activate_window_by_titlebar_click(window: WindowInfo) -> dict[str, Any]:
    point = window_activation_point(window)
    if point is None:
        return {"attempted": False, "reason": "window_has_no_clickable_bounds"}
    if pyautogui is None:
        return {"attempted": False, "reason": "pyautogui_unavailable", "point": {"x": point[0], "y": point[1]}}
    failsafe_nudge = nudge_cursor_from_failsafe_corner()
    before = get_foreground_window_snapshot()
    click_result = perform_screen_click(point[0], point[1])
    cancellable_sleep(FOCUS_SETTLE_SECONDS)
    after = get_foreground_window_snapshot()
    return {
        "attempted": True,
        "point": {"x": point[0], "y": point[1]},
        "click_result": click_result,
        "foreground_before": before,
        "foreground_after": after,
        "foreground_confirmed": int(after.get("hwnd") or 0) == int(window.hwnd),
        "failsafe_nudge": failsafe_nudge,
    }


def get_foreground_window_snapshot() -> dict[str, Any]:
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    fallback_source = ""
    if hwnd <= 0:
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            hwnd = int(info.hwndActive or info.hwndFocus or info.hwndCaret or 0)
            if hwnd:
                fallback_source = "GetGUIThreadInfo"
        if hwnd <= 0:
            hwnd = int(user32.GetActiveWindow() or 0)
            if hwnd:
                fallback_source = "GetActiveWindow"
    window = window_info_from_hwnd(hwnd)
    payload = window_payload(window) or {"hwnd": hwnd, "title": get_window_title(hwnd)}
    payload["process_name"] = get_process_name_for_hwnd(hwnd)
    payload["class_name"] = get_class_name(hwnd)
    if fallback_source:
        payload["fallback_source"] = fallback_source
    return payload


def is_helper_window_snapshot(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    title = str(payload.get("title") or "").strip()
    process_name = str(payload.get("process_name") or "").strip().lower()
    class_name = str(payload.get("class_name") or "").strip()
    return (
        title == HELPER_TITLE
        or (process_name == "python.exe" and class_name == "TkTopLevel" and HELPER_SHORT_NAME.lower() in title.lower())
    )


def window_payload(window: WindowInfo | None) -> dict[str, Any] | None:
    if window is None:
        return None
    bounds = getattr(window, "bounds", None)
    if bounds is None:
        bounds = {
            "left": window.left,
            "top": window.top,
            "right": window.right,
            "bottom": window.bottom,
            "width": max(0, window.right - window.left),
            "height": max(0, window.bottom - window.top),
        }
    process_name = get_process_name_for_hwnd(window.hwnd)
    class_name = get_class_name(window.hwnd)
    payload = {
        "hwnd": window.hwnd,
        "title": window.title,
        "bounds": bounds,
        "minimized": window.minimized,
        "process_name": process_name,
        "class_name": class_name,
    }
    payload["vm_boundary"] = detect_vm_boundary(window.title, process_name, class_name)
    return payload


def detect_vm_boundary(title: str, process_name: str = "", class_name: str = "") -> dict[str, Any]:
    title_lower = title.lower()
    process_lower = process_name.lower()
    class_lower = class_name.lower()
    checks = [
        ("vmware", ("vmware.exe", "vmplayer.exe", "vmware-vmx.exe"), ("vmware", "workstation", "vmware player"), ("vmware",)),
        ("virtualbox", ("virtualbox.exe", "virtualboxvm.exe", "vboxheadless.exe"), ("virtualbox", "oracle vm"), ("virtualbox",)),
        ("hyper_v", ("vmconnect.exe",), ("virtual machine connection", "hyper-v"), ("vmconnect",)),
        ("remote_desktop", ("mstsc.exe", "msrdc.exe"), ("remote desktop",), ("remote", "mstsc")),
    ]
    for platform, process_tokens, title_tokens, class_tokens in checks:
        reasons: list[str] = []
        if process_lower in process_tokens or any(token in process_lower for token in process_tokens):
            reasons.append(f"process:{process_name}")
        if any(token in title_lower for token in title_tokens):
            reasons.append(f"title:{title}")
        if any(token in class_lower for token in class_tokens):
            reasons.append(f"class:{class_name}")
        if reasons:
            return {
                "detected": True,
                "platform": platform,
                "reasons": reasons,
                "default_policy": "informational_only",
            }
    return {"detected": False}


def monitor_payload(monitor: MonitorInfo | None) -> dict[str, Any] | None:
    if monitor is None:
        return None
    return {
        "handle": monitor.handle,
        "device_name": monitor.device_name,
        "primary": monitor.primary,
        "bounds": {
            "left": monitor.left,
            "top": monitor.top,
            "right": monitor.right,
            "bottom": monitor.bottom,
            "width": monitor.width,
            "height": monitor.height,
        },
        "work_bounds": {
            "left": monitor.work_left,
            "top": monitor.work_top,
            "right": monitor.work_right,
            "bottom": monitor.work_bottom,
            "width": max(0, monitor.work_right - monitor.work_left),
            "height": max(0, monitor.work_bottom - monitor.work_top),
        },
    }


def get_class_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    user32 = ctypes.windll.user32
    buffer = ctypes.create_unicode_buffer(256)
    if user32.GetClassNameW(hwnd, buffer, len(buffer)) <= 0:
        return ""
    return buffer.value.strip()


def get_window_process_id(hwnd: int) -> int:
    if not hwnd:
        return 0
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def get_process_name_for_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)


def get_process_name_for_hwnd(hwnd: int) -> str:
    return get_process_name_for_pid(get_window_process_id(hwnd))


def control_kind(class_name: str, title: str) -> str:
    lower_class = class_name.lower()
    lower_title = title.lower()
    if "edit" in lower_class or "richedit" in lower_class:
        return "text_field"
    if "combo" in lower_class:
        return "dropdown"
    if "list" in lower_class or "tree" in lower_class:
        return "list"
    if "button" in lower_class:
        if any(token in lower_title for token in ("ok", "save", "open", "yes", "next", "finish")):
            return "primary_button"
        return "button"
    if "static" in lower_class and title:
        return "label"
    if "link" in lower_class or lower_title.endswith("..."):
        return "link"
    return "control"


def child_control_payload(hwnd: int, candidate_id: int) -> dict[str, Any] | None:
    window = window_info_from_hwnd(hwnd)
    if window is None:
        return None
    class_name = get_class_name(hwnd)
    title = get_window_title(hwnd)
    return {
        "candidate_id": candidate_id,
        "hwnd": hwnd,
        "title": title,
        "class_name": class_name,
        "process_name": get_process_name_for_hwnd(hwnd),
        "enabled": bool(ctypes.windll.user32.IsWindowEnabled(hwnd)),
        "visible": bool(ctypes.windll.user32.IsWindowVisible(hwnd)),
        "kind": control_kind(class_name, title),
        "bounds": window_payload(window)["bounds"],
    }


def candidate_inside_window(candidate: dict[str, Any], window_payload_data: dict[str, Any]) -> bool:
    bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
    window_bounds = window_payload_data.get("bounds") if isinstance(window_payload_data.get("bounds"), dict) else {}
    if not bounds or not window_bounds:
        return False
    left = int(bounds.get("left") or 0)
    top = int(bounds.get("top") or 0)
    right = int(bounds.get("right") or 0)
    bottom = int(bounds.get("bottom") or 0)
    win_left = int(window_bounds.get("left") or 0)
    win_top = int(window_bounds.get("top") or 0)
    win_right = int(window_bounds.get("right") or 0)
    win_bottom = int(window_bounds.get("bottom") or 0)
    return left >= win_left and top >= win_top and right <= win_right and bottom <= win_bottom


def enumerate_child_controls(parent_hwnd: int, limit: int = 24) -> list[dict[str, Any]]:
    if not parent_hwnd:
        return []
    user32 = ctypes.windll.user32
    found: list[dict[str, Any]] = []
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if len(found) >= limit:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        payload = child_control_payload(int(hwnd), len(found) + 1)
        if payload is None:
            return True
        bounds = payload.get("bounds") if isinstance(payload.get("bounds"), dict) else {}
        if int(bounds.get("width") or 0) <= 0 or int(bounds.get("height") or 0) <= 0:
            return True
        found.append(payload)
        return True

    user32.EnumChildWindows(parent_hwnd, enum_proc_type(callback), 0)
    return found


def get_gui_focus_payload() -> dict[str, Any] | None:
    user32 = ctypes.windll.user32
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return None
    hwnd = int(info.hwndFocus or 0)
    if hwnd <= 0:
        return None
    payload = child_control_payload(hwnd, 0)
    if payload is None:
        return None
    payload["caret_rect"] = {
        "left": int(info.rcCaret.left),
        "top": int(info.rcCaret.top),
        "right": int(info.rcCaret.right),
        "bottom": int(info.rcCaret.bottom),
    }
    return payload


def looks_like_dialog(window: WindowInfo | None, children: list[dict[str, Any]]) -> bool:
    if window is None:
        return False
    title = window.title.lower()
    if any(token in title for token in ("save", "open", "confirm", "warning", "settings", "options", "dialog", "?")):
        return True
    button_count = sum(1 for child in children if child.get("kind") in {"button", "primary_button"})
    text_fields = sum(1 for child in children if child.get("kind") == "text_field")
    return window.width < 900 and button_count >= 2 and text_fields >= 1


def build_action_menu(active_context: dict[str, Any], planner_intent: str = "") -> dict[str, Any]:
    candidates = active_context.get("child_candidates") if isinstance(active_context.get("child_candidates"), list) else []
    ranked_candidates = rank_candidates(candidates, active_context, planner_intent)
    actionable_candidates = [
        item for item in ranked_candidates if bool(item.get("visible")) and bool(item.get("enabled"))
    ]
    likely_click_targets = [
        item
        for item in actionable_candidates
        if item.get("kind") in {"button", "primary_button", "dropdown", "link", "control"}
    ][:10]
    text_fields = [item for item in actionable_candidates if item.get("kind") == "text_field"][:6]
    primary_buttons = [item for item in likely_click_targets if item.get("kind") == "primary_button"][:4]
    recommended_candidate = next(
        (item for item in actionable_candidates if float(item.get("score") or 0.0) >= 0.55),
        None,
    )
    focused_control = active_context.get("focused_control") if isinstance(active_context.get("focused_control"), dict) else {}
    keyboard_safe = focused_control.get("kind") == "text_field"
    recommendations: list[str] = []
    if recommended_candidate:
        recommendations.append(
            f"Top candidate is {recommended_candidate.get('candidate_id')} ({recommended_candidate.get('label')}, "
            f"{recommended_candidate.get('confidence')} confidence); prefer candidate actions when it matches the intent."
        )
    if primary_buttons:
        recommendations.append("A likely primary button is visible; prefer that over raw coordinate guessing when it matches the intent.")
    if text_fields and not keyboard_safe:
        recommendations.append("A text field is visible but not focused; click or verify focus before typing or pasting.")
    if active_context.get("dialog_open"):
        recommendations.append("A dialog is open; treat filename, location, and action buttons as separate controls.")
    if not likely_click_targets:
        recommendations.append("No obvious child controls were detected from the backend; use a grid crop with surrounding context.")
    elif not recommended_candidate:
        recommendations.append("No candidate reached medium confidence; ask for a finer crop/grid or use a clearly described candidate id.")
    return {
        "keyboard_entry_safe": keyboard_safe,
        "planner_intent": planner_intent,
        "ranked_candidates": ranked_candidates[:18],
        "recommended_candidate": recommended_candidate,
        "likely_click_targets": likely_click_targets,
        "likely_text_fields": text_fields,
        "likely_primary_buttons": primary_buttons,
        "recommended_next_moves": recommendations,
    }


def planner_intent_from_args(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "target_description",
        "expected_result",
        "reason",
        "question_for_planner",
        "intent",
        "text",
        "title",
    ):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    step = args.get("step") if isinstance(args.get("step"), dict) else {}
    for key in ("action", "target_description", "expected_result", "text"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return " | ".join(parts)[:400]


def action_menu_entry(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": candidate.get("rank"),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_hwnd": candidate.get("hwnd"),
        "label": candidate.get("label") or candidate.get("title") or candidate.get("class_name") or f"candidate_{candidate.get('candidate_id')}",
        "kind": candidate.get("kind"),
        "enabled": candidate.get("enabled"),
        "visible": candidate.get("visible"),
        "bounds": candidate.get("bounds"),
        "click_point": candidate_center(candidate),
        "available_actions": candidate_available_actions(candidate),
        "score": candidate.get("score"),
        "confidence": candidate.get("confidence"),
        "score_reasons": candidate.get("score_reasons") or [],
        "risk_flags": candidate.get("risk_flags") or [],
    }


def rank_candidates(candidates: list[dict[str, Any]], active_context: dict[str, Any], planner_intent: str = "") -> list[dict[str, Any]]:
    duplicate_counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate_duplicate_key(candidate)
        if key:
            duplicate_counts[key] = duplicate_counts.get(key, 0) + 1
    scored = [
        action_menu_entry(score_candidate(candidate, active_context, planner_intent, duplicate_counts))
        for candidate in candidates
        if candidate.get("kind") not in {"label"}
    ]
    scored.sort(key=lambda item: (float(item.get("score") or 0.0), str(item.get("label") or "")), reverse=True)
    for index, item in enumerate(scored, start=1):
        item["rank"] = index
    return scored


def score_candidate(
    candidate: dict[str, Any],
    active_context: dict[str, Any],
    planner_intent: str,
    duplicate_counts: dict[str, int],
) -> dict[str, Any]:
    scored = dict(candidate)
    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    if bool(candidate.get("visible")):
        score += 0.18
        reasons.append("visible")
    else:
        risks.append("not_visible")
    if bool(candidate.get("enabled")):
        score += 0.18
        reasons.append("enabled")
    else:
        risks.append("disabled")

    bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
    width = int(bounds.get("right") or 0) - int(bounds.get("left") or 0)
    height = int(bounds.get("bottom") or 0) - int(bounds.get("top") or 0)
    if width > 0 and height > 0:
        score += 0.08
        reasons.append("valid_bounds")
        if width < 20 or height < 12:
            score -= 0.08
            risks.append("tiny_target")
    else:
        score -= 0.25
        risks.append("zero_size")

    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    active_bounds = active_window.get("bounds") if isinstance(active_window.get("bounds"), dict) else {}
    if rect_contains_rect(active_bounds, bounds):
        score += 0.12
        reasons.append("inside_active_window")
    else:
        score -= 0.20
        risks.append("outside_active_window")

    kind = str(candidate.get("kind") or "")
    kind_boost = {
        "primary_button": 0.16,
        "button": 0.12,
        "dropdown": 0.12,
        "text_field": 0.12,
        "link": 0.10,
        "list": 0.07,
        "control": 0.04,
    }.get(kind, 0.02)
    score += kind_boost
    if kind_boost >= 0.10:
        reasons.append(f"useful_kind:{kind}")
    elif kind == "control":
        risks.append("generic_control")

    actions = candidate_available_actions(candidate)
    if any(action != "input.click_candidate" for action in actions):
        score += 0.10
        reasons.append("backend_action_available")

    match_score, match_reasons = candidate_intent_match_score(candidate, planner_intent)
    score += match_score
    reasons.extend(match_reasons)

    duplicate_key = candidate_duplicate_key(candidate)
    if duplicate_key and duplicate_counts.get(duplicate_key, 0) > 1:
        score -= 0.08
        risks.append("duplicate_label")

    score = max(0.0, min(1.0, score))
    scored["label"] = candidate.get("title") or candidate.get("class_name") or f"candidate_{candidate.get('candidate_id')}"
    scored["score"] = round(score, 3)
    scored["confidence"] = candidate_confidence(score)
    scored["score_reasons"] = reasons[:8]
    scored["risk_flags"] = risks[:8]
    return scored


def candidate_intent_match_score(candidate: dict[str, Any], planner_intent: str) -> tuple[float, list[str]]:
    if not planner_intent:
        return 0.0, []
    label = str(candidate.get("title") or candidate.get("class_name") or "").lower()
    intent = planner_intent.lower()
    if not label:
        return 0.0, []
    reasons: list[str] = []
    if label in intent:
        reasons.append("label_in_intent")
        return 0.22, reasons
    label_tokens = meaningful_tokens(label)
    intent_tokens = meaningful_tokens(intent)
    overlap = sorted(label_tokens.intersection(intent_tokens))
    if not overlap:
        return 0.0, []
    reasons.append("intent_token_match:" + ",".join(overlap[:4]))
    return min(0.18, 0.06 * len(overlap)), reasons


def meaningful_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    current = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        else:
            if len(current) >= 2:
                tokens.add("".join(current))
            current = []
    if len(current) >= 2:
        tokens.add("".join(current))
    stop = {"the", "and", "for", "with", "this", "that", "into", "from", "app", "window", "button", "click"}
    return {token for token in tokens if token not in stop}


def candidate_duplicate_key(candidate: dict[str, Any]) -> str:
    label = str(candidate.get("title") or "").strip().lower()
    if not label:
        return ""
    return f"{candidate.get('kind') or ''}:{label}"


def candidate_confidence(score: float) -> str:
    if score >= 0.80:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.35:
        return "low"
    return "very_low"


def candidate_center(candidate: dict[str, Any]) -> dict[str, int]:
    bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
    return {
        "x": int((int(bounds.get("left") or 0) + int(bounds.get("right") or 0)) / 2),
        "y": int((int(bounds.get("top") or 0) + int(bounds.get("bottom") or 0)) / 2),
    }


def candidate_available_actions(candidate: dict[str, Any]) -> list[str]:
    kind = str(candidate.get("kind") or "")
    class_name = str(candidate.get("class_name") or "").lower()
    actions = ["input.click_candidate"]
    if kind in {"button", "primary_button", "link", "control"} or "button" in class_name:
        actions.append("input.invoke_candidate")
    if kind == "text_field" or "edit" in class_name:
        actions.append("input.set_candidate_value")
    if kind == "dropdown" or "combo" in class_name:
        actions.append("input.expand_candidate")
    if kind in {"button", "primary_button", "list", "control"} or any(token in class_name for token in ("button", "listbox", "listview", "treeview")):
        actions.append("input.select_candidate")
    return list(dict.fromkeys(actions))


def find_candidate(candidates: list[dict[str, Any]], candidate_id: int = 0, candidate_hwnd: int = 0) -> dict[str, Any] | None:
    if candidate_hwnd:
        for candidate in candidates:
            if int(candidate.get("hwnd") or 0) == int(candidate_hwnd):
                return candidate
    if candidate_id:
        for candidate in candidates:
            if int(candidate.get("candidate_id") or 0) == int(candidate_id):
                return candidate
    return None


def validate_candidate_for_click(candidate: dict[str, Any], active_context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    candidate_hwnd = int(candidate.get("hwnd") or 0)
    if candidate_hwnd and not ctypes.windll.user32.IsWindow(candidate_hwnd):
        return command_result("UNCERTAIN", "input.click_candidate", "Candidate window handle is no longer valid.", data={"candidate": candidate, "active_context": active_context})
    if not bool(candidate.get("visible", False)):
        return command_result("UNCERTAIN", "input.click_candidate", "Candidate is not visible.", data={"candidate": candidate, "active_context": active_context})
    if not bool(candidate.get("enabled", False)):
        return command_result("UNCERTAIN", "input.click_candidate", "Candidate is disabled.", data={"candidate": candidate, "active_context": active_context})
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    active_bounds = active_window.get("bounds") if isinstance(active_window.get("bounds"), dict) else {}
    candidate_bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
    if not rect_contains_rect(active_bounds, candidate_bounds):
        return command_result(
            "UNCERTAIN",
            "input.click_candidate",
            "Candidate is outside the active window bounds.",
            data={"candidate": candidate, "active_context": active_context},
        )
    expected_kind = str(args.get("expected_kind") or "").strip()
    if expected_kind and str(candidate.get("kind") or "") != expected_kind:
        return command_result(
            "UNCERTAIN",
            "input.click_candidate",
            f"Candidate kind {candidate.get('kind')!r} did not match expected kind {expected_kind!r}.",
            data={"candidate": candidate, "active_context": active_context},
        )
    return command_result("SUCCESS", "input.click_candidate", "Candidate is fresh enough to click.", data={"candidate": candidate})


def resolve_candidate_for_backend_action(args: dict[str, Any], command: str) -> dict[str, Any]:
    candidate_id = int(args.get("candidate_id") or 0)
    candidate_hwnd = int(args.get("candidate_hwnd") or args.get("hwnd") or 0)
    if candidate_id <= 0 and candidate_hwnd <= 0:
        return command_result("ERROR", command, "candidate_id or candidate_hwnd is required.")
    active_context = build_active_context_packet_for_args(args)
    candidates = active_context.get("child_candidates") if isinstance(active_context.get("child_candidates"), list) else []
    candidate = find_candidate(candidates, candidate_id, candidate_hwnd)
    if candidate is None:
        return command_result(
            "UNCERTAIN",
            command,
            "Candidate was not found in the current active window.",
            data={
                "requested_candidate_id": candidate_id or None,
                "requested_candidate_hwnd": candidate_hwnd or None,
                "active_context": active_context,
                "candidate_overlay": create_candidate_overlay_for_active_context(active_context, args),
            },
            next_allowed_actions=["observe.screenshot", "recovery.capture_grid", "planner.step"],
        )
    freshness = validate_candidate_for_click(candidate, active_context, args)
    if freshness["status"] != "SUCCESS":
        freshness["command"] = command
        return freshness
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    return command_result(
        "SUCCESS",
        command,
        "Candidate resolved for backend action.",
        data={
            "candidate": candidate,
            "active_window": active_window,
            "active_context": active_context,
            "candidate_overlay": create_candidate_overlay_for_active_context(active_context, args),
        },
    )


def get_control_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    user32 = ctypes.windll.user32
    length = int(user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0))
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.SendMessageW(hwnd, WM_GETTEXT, len(buffer), buffer)
    return buffer.value


def get_button_check_state(hwnd: int) -> int | None:
    if not hwnd:
        return None
    try:
        return int(ctypes.windll.user32.SendMessageW(hwnd, BM_GETCHECK, 0, 0))
    except Exception:
        return None


def control_local_click_lparam(candidate: dict[str, Any]) -> int:
    bounds = candidate.get("bounds") if isinstance(candidate.get("bounds"), dict) else {}
    width = max(1, int(bounds.get("width") or (int(bounds.get("right") or 0) - int(bounds.get("left") or 0))))
    height = max(1, int(bounds.get("height") or (int(bounds.get("bottom") or 0) - int(bounds.get("top") or 0))))
    x = max(0, min(width - 1, width // 2))
    y = max(0, min(height - 1, height // 2))
    return (y << 16) | (x & 0xFFFF)


def send_message_timeout(hwnd: int, message: int, wparam: int = 0, lparam: int = 0, timeout_ms: int = BACKEND_MESSAGE_TIMEOUT_MS) -> dict[str, Any]:
    if not hwnd:
        return {"ok": False, "reason": "missing_hwnd", "timeout": False}
    check_command_cancelled()
    user32 = ctypes.windll.user32
    result = ctypes.c_ulonglong(0)
    started = time.monotonic()
    ok = bool(
        user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd),
            wintypes.UINT(message),
            wintypes.WPARAM(wparam),
            wintypes.LPARAM(lparam),
            wintypes.UINT(SMTO_BLOCK | SMTO_ABORTIFHUNG),
            wintypes.UINT(max(50, int(timeout_ms))),
            ctypes.byref(result),
        )
    )
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    error = int(ctypes.get_last_error()) if not ok else 0
    return {
        "ok": ok,
        "timeout": not ok,
        "elapsed_ms": elapsed_ms,
        "timeout_ms": timeout_ms,
        "result": int(result.value),
        "last_error": error,
    }


def send_backend_invoke(hwnd: int, candidate: dict[str, Any]) -> dict[str, Any]:
    if not hwnd:
        return {"ok": False, "method": "", "reason": "missing_hwnd"}
    check_command_cancelled()
    class_name = str(candidate.get("class_name") or "").lower()
    if "button" in class_name:
        result = send_message_timeout(hwnd, BM_CLICK)
        result["method"] = "BM_CLICK"
        return result
    lparam = control_local_click_lparam(candidate)
    down = send_message_timeout(hwnd, WM_LBUTTONDOWN, 1, lparam)
    up = send_message_timeout(hwnd, WM_LBUTTONUP, 0, lparam)
    return {
        "ok": bool(down.get("ok") and up.get("ok")),
        "method": "WM_LBUTTONDOWN/UP",
        "down": down,
        "up": up,
        "timeout": bool(down.get("timeout") or up.get("timeout")),
    }


def rect_contains_rect(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    if not outer or not inner:
        return False
    return (
        int(outer.get("left") or 0) <= int(inner.get("left") or 0)
        and int(outer.get("top") or 0) <= int(inner.get("top") or 0)
        and int(outer.get("right") or 0) >= int(inner.get("right") or 0)
        and int(outer.get("bottom") or 0) >= int(inner.get("bottom") or 0)
    )


def create_candidate_overlay_for_active_context(active_context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    hwnd = int(active_window.get("hwnd") or 0)
    if not hwnd:
        return {"status": "UNAVAILABLE", "reason": "No active window hwnd."}
    capture = capture_target(
        {
            "target": "active_window",
            "title": str(active_window.get("title") or args.get("title") or TARGET_TITLE),
            "hwnd": hwnd,
            "request_id": f"{make_request_id()}_candidate_overlay",
        },
        with_grid=False,
    )
    if capture["status"] != "SUCCESS":
        return {"status": capture.get("status"), "reason": capture.get("message")}
    data = capture.get("data") if isinstance(capture.get("data"), dict) else {}
    return {
        "status": "SUCCESS",
        "raw_screenshot_path": data.get("raw_screenshot_path"),
        "candidate_overlay_screenshot_path": data.get("candidate_overlay_screenshot_path"),
    }


def build_planner_coaching(active_context: dict[str, Any], args: dict[str, Any], failure_type: str = "") -> list[str]:
    coaching: list[str] = []
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    title = str(active_window.get("title") or "").lower()
    if active_context.get("dialog_open"):
        coaching.append("A dialog is active. Check labels, buttons, dropdown arrows, and links before repeating the same tactic.")
    if "save" in title or "open" in title:
        coaching.append("Treat the filename field and the location chooser as separate controls.")
        coaching.append("If a save/open button is disabled, verify the field focus and selected location before typing more text.")
    if failure_type == "STALE_SCREENSHOT":
        coaching.append("The screen changed after the planner looked at it. Use the fresh screenshot instead of retrying the old coordinates.")
    focused_control = active_context.get("focused_control") if isinstance(active_context.get("focused_control"), dict) else {}
    if focused_control and focused_control.get("kind") != "text_field":
        coaching.append("The focused control does not look like a text field; avoid typing until the input target is explicit.")
    return coaching


def build_active_context_packet(preferred_title: str = "") -> dict[str, Any]:
    foreground = get_foreground_window_snapshot()
    hwnd = int(foreground.get("hwnd") or 0)
    active_window = window_info_from_hwnd(hwnd)
    return build_context_for_window(active_window, foreground, preferred_title)


def build_active_context_packet_for_capture(preferred_title: str = "", target_window: dict[str, Any] | None = None) -> dict[str, Any]:
    foreground = get_foreground_window_snapshot()
    hwnd = int((target_window or {}).get("hwnd") or 0)
    if hwnd:
        window = window_info_from_hwnd(hwnd)
        if window is not None:
            return build_context_for_window(window, foreground, preferred_title)
    return build_active_context_packet(preferred_title)


def build_active_context_packet_for_args(args: dict[str, Any]) -> dict[str, Any]:
    window = resolve_context_window_for_args(args)
    if window is not None:
        return build_context_for_window(window, get_foreground_window_snapshot(), str(args.get("title") or ""))
    return build_active_context_packet(str(args.get("title") or ""))


def build_context_for_window(
    active_window: WindowInfo | None,
    foreground: dict[str, Any],
    preferred_title: str = "",
) -> dict[str, Any]:
    hwnd = int(active_window.hwnd if active_window else foreground.get("hwnd") or 0)
    active_payload = window_payload(active_window) or foreground
    active_payload["process_name"] = get_process_name_for_hwnd(hwnd)
    active_payload["class_name"] = get_class_name(hwnd)
    monitor = monitor_payload(monitor_for_window(active_window))
    focused_control = get_gui_focus_payload()
    if focused_control and not candidate_inside_window(focused_control, active_payload):
        focused_control = None
    children = enumerate_child_controls(hwnd)
    dialog_open = looks_like_dialog(active_window, children)
    vm_boundary = active_payload.get("vm_boundary") if isinstance(active_payload.get("vm_boundary"), dict) else {"detected": False}
    return {
        "timestamp": now_iso(),
        "expected_title_hint": preferred_title or "",
        "foreground_window": foreground,
        "active_window": active_payload,
        "active_monitor": monitor,
        "focused_control": focused_control,
        "dialog_open": dialog_open,
        "current_dialog": active_payload if dialog_open else None,
        "child_candidates": children,
        "vm_boundary": vm_boundary,
    }


def summarize_action_delta(
    before_capture: dict[str, Any],
    after_capture: dict[str, Any],
    click_data: dict[str, Any],
) -> dict[str, Any]:
    before_data = before_capture.get("data") if isinstance(before_capture.get("data"), dict) else {}
    after_data = after_capture.get("data") if isinstance(after_capture.get("data"), dict) else {}
    before_path = Path(str(before_data.get("raw_screenshot_path") or ""))
    after_path = Path(str(after_data.get("raw_screenshot_path") or ""))
    image_delta: dict[str, Any] = {}
    if before_path.exists() and after_path.exists() and Image is not None and ImageChops is not None and ImageStat is not None:
        try:
            before_image = normalize_compare_image(Image.open(before_path), 512)
            after_image = normalize_compare_image(Image.open(after_path), 512)
            if before_image.size == after_image.size:
                diff = ImageChops.difference(before_image, after_image).convert("L")
                stat = ImageStat.Stat(diff)
                histogram = diff.histogram()
                threshold = 24
                changed_pixels = sum(histogram[threshold + 1 :])
                total_pixels = max(1, diff.width * diff.height)
                image_delta = {
                    "mean_delta": round(float(stat.mean[0]), 4),
                    "changed_ratio": round(changed_pixels / total_pixels, 6),
                    "changed": bool(float(stat.mean[0]) > 0.8 or (changed_pixels / total_pixels) > 0.006),
                }
        except Exception as exc:
            image_delta = {"comparison_error": str(exc)}
    before_foreground = click_data.get("foreground_before") if isinstance(click_data.get("foreground_before"), dict) else {}
    after_foreground = click_data.get("foreground_after") if isinstance(click_data.get("foreground_after"), dict) else {}
    return {
        "foreground_changed": int(before_foreground.get("hwnd") or 0) != int(after_foreground.get("hwnd") or 0),
        "foreground_before": before_foreground,
        "foreground_after": after_foreground,
        "target_window_before": before_data.get("target_window"),
        "target_window_after": after_data.get("target_window"),
        "image_delta": image_delta,
        "no_visible_change_suspected": bool(image_delta) and not image_delta.get("changed", True),
    }


# Commands that only read state. They never rewrite working memory (except to
# record a brand-new planner observation such as a verify.window_state failure
# capture), so they cannot clobber last_action, current_app or failure state.
READ_ONLY_MEMORY_COMMANDS = frozenset(
    {
        "helper.commands",
        "helper.bridge_status",
        "diagnose.environment",
        "status.current",
        "observe.windows",
        "observe.window",
        "detect.vm_boundary",
        "verify.window_state",
        "verify.file_state",
    }
)
# Commands that act on the desktop. A later SUCCESS from one of these clears
# last_failed_action.
ACTION_MEMORY_COMMANDS = frozenset(
    {
        "focus.window",
        "input.action_sequence",
        "input.click_candidate",
        "input.click_point",
        "input.expand_candidate",
        "input.hotkey",
        "input.invoke_candidate",
        "input.paste_text",
        "input.select_candidate",
        "input.set_candidate_value",
        "input.type_text",
        "recovery.guided_grid_click",
        "recovery.transient_grid_after_click",
        "desktop.start_search_grid",
        "planner.step",
        "simon.press_next",
    }
)


def action_result_succeeded(command: str, result: dict[str, Any]) -> bool:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if command in {"planner.step", "input.action_sequence"}:
        if command == "input.action_sequence" and (data.get("verification") or {}).get("evidence_kind") == "command":
            return False
        nested = data.get("action_result")
        return (result.get("status") in {"SUCCESS", "NEEDS_PLANNER"}
                and (not isinstance(nested, dict) or result_outcome(nested) != "blocked")
                and effect_allows_continuation(data.get("verification") or {}))
    status = result.get("status")
    if status == "SUCCESS":
        return True
    if command == "planner.step" and status == "NEEDS_PLANNER":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        action_result = data.get("action_result") if isinstance(data.get("action_result"), dict) else {}
        return action_result.get("status") == "SUCCESS"
    return False


def update_planner_working_memory(
    command: str,
    args: dict[str, Any],
    result: dict[str, Any],
    new_observation_ids: list[str] | None = None,
) -> dict[str, Any]:
    memory = load_planner_working_memory()
    new_observation_id = str(new_observation_ids[-1]) if new_observation_ids else ""
    if command in READ_ONLY_MEMORY_COMMANDS:
        if new_observation_id:
            memory["last_observation_request_id"] = new_observation_id
            memory["updated_at"] = now_iso()
            save_planner_working_memory(memory)
        return memory
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    active_context = data.get("active_context") if isinstance(data.get("active_context"), dict) else build_active_context_packet(str(args.get("title") or ""))
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    current_dialog = active_context.get("current_dialog") if isinstance(active_context.get("current_dialog"), dict) else {}
    if args.get("goal"):
        memory["goal"] = str(args.get("goal"))
    memory["current_app"] = {
        "title": active_window.get("title"),
        "process_name": active_window.get("process_name"),
        "hwnd": active_window.get("hwnd"),
    }
    memory["current_dialog"] = {
        "title": current_dialog.get("title"),
        "hwnd": current_dialog.get("hwnd"),
    } if current_dialog else None
    status = result.get("status")
    memory["last_action"] = {"command": command, "status": status, "timestamp": now_iso()}
    if isinstance(data.get("verification"), dict):
        memory["last_action"]["verification"] = data["verification"]
    is_action = command in ACTION_MEMORY_COMMANDS
    if is_action and action_result_succeeded(command, result):
        memory["last_successful_action"] = memory["last_action"]
        memory.pop("last_failed_action", None)
    elif status == "SUCCESS" and not is_action:
        memory["last_successful_action"] = memory["last_action"]
    elif status in {"FAILED", "ERROR", "UNCERTAIN"} or (status == "NEEDS_PLANNER" and is_action):
        # NEEDS_PLANNER is the normal outcome of an observation command, so it
        # only counts as a failure when an action could not be verified.
        memory["last_failed_action"] = memory["last_action"]
    expected_next = data.get("expected_result") or args.get("expected_result") or ""
    if expected_next:
        memory["expected_next_state"] = expected_next
    if new_observation_id:
        memory["last_observation_request_id"] = new_observation_id
    memory["updated_at"] = now_iso()
    save_planner_working_memory(memory)
    return memory


def enrich_result_context(
    command: str,
    args: dict[str, Any],
    result: dict[str, Any],
    new_observation_ids: list[str] | None = None,
) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    if "active_context" not in data:
        packets = list(observation_packets(data.get("next_observation") or data.get("follow_up_capture") or data))
        context = next((packet["active_context"] for packet in reversed(packets) if isinstance(packet.get("active_context"), dict)), None)
        data["active_context"] = context if context is not None else build_active_context_packet(str(args.get("title") or ""))
    if "action_menu" not in data:
        data["action_menu"] = build_action_menu(data["active_context"], planner_intent_from_args(args))
    if "planner_coaching" not in data:
        data["planner_coaching"] = build_planner_coaching(
            data["active_context"],
            args,
            str(data.get("failure_type") or result.get("status") or ""),
        )
    data["working_memory"] = update_planner_working_memory(command, args, result, new_observation_ids=new_observation_ids)
    return result


def evaluate_text_entry_guardrails(command: str, text: str, args: dict[str, Any]) -> dict[str, Any] | None:
    active_context = build_active_context_packet_for_args(args)
    vm_guardrail = evaluate_vm_boundary_guardrail(command, args, active_context=active_context)
    if vm_guardrail is not None:
        return vm_guardrail
    if not text or bool(args.get("allow_path_text")):
        return None
    active_window = active_context.get("active_window") if isinstance(active_context.get("active_window"), dict) else {}
    title = str(active_window.get("title") or "").lower()
    looks_like_path = ":\\" in text or text.startswith("\\\\") or "/" in text
    if active_context.get("dialog_open") and looks_like_path and ("save" in title or "open" in title):
        return command_result(
            "UNCERTAIN",
            command,
            "Guardrail blocked path text entry into an active save/open dialog until the planner confirms the field or location workflow.",
            data={
                "text_length": len(text),
                "looks_like_path": True,
                "active_context": active_context,
                "planner_coaching": build_planner_coaching(active_context, args),
            },
            next_allowed_actions=["observe.screenshot", "recovery.capture_grid", "verify.window_state"],
        )
    return None


def evaluate_click_guardrails(
    command: str,
    args: dict[str, Any],
    screen_x: int,
    screen_y: int,
    target_window: WindowInfo | dict[str, Any] | None,
) -> dict[str, Any] | None:
    active_context = build_active_context_packet_for_args(args)
    vm_guardrail = evaluate_vm_boundary_guardrail(command, args, active_context=active_context, target_window=target_window)
    if vm_guardrail is not None:
        return vm_guardrail
    current_dialog = active_context.get("current_dialog") if isinstance(active_context.get("current_dialog"), dict) else None
    if str(args.get("target") or "").startswith("current_dialog") and current_dialog:
        bounds = current_dialog.get("bounds") if isinstance(current_dialog.get("bounds"), dict) else {}
        left = int(bounds.get("left") or 0)
        top = int(bounds.get("top") or 0)
        right = int(bounds.get("right") or 0)
        bottom = int(bounds.get("bottom") or 0)
        if not (left <= screen_x <= right and top <= screen_y <= bottom):
            return command_result(
                "UNCERTAIN",
                command,
                "Guardrail blocked a click outside the active dialog bounds.",
                data={
                    "screen_coordinate": {"x": screen_x, "y": screen_y},
                    "active_context": active_context,
                    "planner_coaching": build_planner_coaching(active_context, args),
                },
                next_allowed_actions=["observe.screenshot", "recovery.capture_grid", "recovery.guided_grid_crop"],
            )
    return None


def evaluate_vm_boundary_guardrail(
    command: str,
    args: dict[str, Any],
    *,
    active_context: dict[str, Any] | None = None,
    target_window: WindowInfo | dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compatibility hook: VM detection is informational and never blocks input.

    Legacy allow_vm_control values are accepted but ignored. Ordinary focus,
    freshness, dialog, cursor verification and cancellation checks still apply.
    """
    return None


def monitor_info_from_handle(handle: int) -> MonitorInfo | None:
    if not handle:
        return None
    user32 = ctypes.windll.user32
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(MONITORINFOEXW)
    if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
        return None
    return MonitorInfo(
        handle=int(handle),
        left=int(info.rcMonitor.left),
        top=int(info.rcMonitor.top),
        right=int(info.rcMonitor.right),
        bottom=int(info.rcMonitor.bottom),
        work_left=int(info.rcWork.left),
        work_top=int(info.rcWork.top),
        work_right=int(info.rcWork.right),
        work_bottom=int(info.rcWork.bottom),
        primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
        device_name=str(info.szDevice).rstrip("\x00"),
    )


def monitor_for_window(window: WindowInfo | None) -> MonitorInfo | None:
    if window is None:
        return None
    user32 = ctypes.windll.user32
    handle = int(user32.MonitorFromWindow(window.hwnd, MONITOR_DEFAULTTONEAREST))
    return monitor_info_from_handle(handle)


def detected_payload(detected: dict[str, DetectedButton]) -> dict[str, Any]:
    return {
        color: {
            "bbox": {"left": button.bbox[0], "top": button.bbox[1], "right": button.bbox[2], "bottom": button.bbox[3]},
            "center": {"x": button.center[0], "y": button.center[1]},
            "pixels": button.pixels,
            "confidence": round(button.confidence, 3),
        }
        for color, button in sorted(detected.items())
    }


def create_grid_crop_request(
    parent_request: dict[str, Any],
    cell: str,
    crop_columns: int,
    crop_rows: int,
    reason: str,
    include_context: bool = True,
) -> dict[str, Any]:
    if Image is None:
        return command_result("ERROR", "recovery.guided_grid_crop", "Pillow Image is not installed.")
    parent_grid = parent_request.get("grid") or {}
    bbox = grid_cell_bbox(cell, parent_grid)
    if bbox is None:
        return command_result("ERROR", "recovery.guided_grid_crop", f"Invalid cell {cell!r}.")
    raw_path = Path(str(parent_request.get("raw_screenshot_path") or ""))
    if not raw_path.exists():
        return command_result("UNCERTAIN", "recovery.guided_grid_crop", "Parent raw screenshot is missing.")
    parent_id = str(parent_request["request_id"])
    child_id = f"{make_request_id()}_{cell.upper()}_crop"
    with Image.open(raw_path) as original:
        image = original.copy()
    crop_image = image.crop(bbox)
    COMMAND_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    raw_crop_path = COMMAND_SCREENSHOT_DIR / f"{safe_request_id(child_id)}_raw.png"
    grid_crop_path = COMMAND_SCREENSHOT_DIR / f"{safe_request_id(child_id)}_grid.png"
    crop_image.save(raw_crop_path)
    record_generated_artifact(raw_crop_path)
    scale = 1024 / max(crop_image.size)
    crop_columns = min(max(1, crop_columns or 12), max(1, int(crop_image.width * scale // 64)))
    crop_rows = min(max(1, crop_rows or 8), max(1, int(crop_image.height * scale // 64)))
    transform = crop_transform(parent_request.get("image_to_screen") or {"verified": False}, bbox)
    fine_grid = {
        "columns": max(1, crop_columns),
        "rows": max(1, crop_rows),
        "cell_width": crop_image.width / max(1, crop_columns),
        "cell_height": crop_image.height / max(1, crop_rows),
        "coordinate_origin": "crop",
    }
    rendered, render = render_planner_grid(crop_image, fine_grid["columns"], fine_grid["rows"], zoom=True)
    rendered.save(grid_crop_path)
    record_generated_artifact(grid_crop_path)
    cell_width = int(round(float(parent_grid.get("cell_width") or 0)))
    cell_height = int(round(float(parent_grid.get("cell_height") or 0)))
    context_margin_x = max(12, cell_width)
    context_margin_y = max(12, cell_height)
    context_bbox = (
        max(0, bbox[0] - context_margin_x),
        max(0, bbox[1] - context_margin_y),
        min(image.width, bbox[2] + context_margin_x),
        min(image.height, bbox[3] + context_margin_y),
    )
    context_raw_path = COMMAND_SCREENSHOT_DIR / f"{safe_request_id(child_id)}_context_raw.png"
    context_grid_path = COMMAND_SCREENSHOT_DIR / f"{safe_request_id(child_id)}_context_grid.png"
    context_image = image.crop(context_bbox)
    context_draw = ImageDraw.Draw(context_image)
    highlight = (
        bbox[0] - context_bbox[0],
        bbox[1] - context_bbox[1],
        bbox[2] - context_bbox[0],
        bbox[3] - context_bbox[1],
    )
    context_draw.rectangle(highlight, outline=(255, 64, 64), width=4)
    if include_context:
        context_image.save(context_raw_path)
        record_generated_artifact(context_raw_path)
        context_rendered, _ = render_planner_grid(context_image, fine_grid["columns"], fine_grid["rows"], zoom=True)
        context_rendered.save(context_grid_path)
        record_generated_artifact(context_grid_path)
    child_request = dict(parent_request)
    child_request.pop("preview_confirmation", None)
    child_request.update(
        {
            "schema": "planner-request.v1",
            "request_id": child_id,
            "parent_request_id": parent_id,
            "parent_cell": cell.upper(),
            "status": "NEEDS_PLANNER",
            "reason": reason or f"Fine grid crop for {cell.upper()}.",
            "question_for_planner": f"Using this fine-grid crop of {cell.upper()}, what single safe cell should I click?",
            "raw_screenshot_path": str(raw_crop_path),
            "grid_screenshot_path": str(grid_crop_path),
            "grid": fine_grid,
            "grid_render": render,
            "image_to_screen": transform,
            "screenshot_width": crop_image.width,
            "screenshot_height": crop_image.height,
            "crop": {
                "parent_request_id": parent_id,
                "parent_cell": cell.upper(),
                "offset_x": transform["root_offset_x"],
                "offset_y": transform["root_offset_y"],
                "width": crop_image.width,
                "height": crop_image.height,
            },
            "context": {
                **({"raw_screenshot_path": str(context_raw_path), "grid_screenshot_path": str(context_grid_path)} if include_context else {}),
                "bbox": {
                    "left": context_bbox[0],
                    "top": context_bbox[1],
                    "right": context_bbox[2],
                    "bottom": context_bbox[3],
                    "width": max(0, context_bbox[2] - context_bbox[0]),
                    "height": max(0, context_bbox[3] - context_bbox[1]),
                },
                "focus_cell_bbox": {
                    "left": highlight[0],
                    "top": highlight[1],
                    "right": highlight[2],
                    "bottom": highlight[3],
                },
            },
            "capture_timestamp": parent_request.get("capture_timestamp"),
            "created_at": now_iso(),
        }
    )
    child_request["planner_request_path"] = str(planner_request_path(child_id))
    write_planner_request(child_request)
    return {
        "status": "SUCCESS",
        "data": {
            "request_id": child_id,
            "parent_request_id": parent_id,
            "parent_cell": cell.upper(),
            "raw_crop_screenshot_path": str(raw_crop_path),
            "grid_crop_screenshot_path": str(grid_crop_path),
            "planner_request_path": child_request["planner_request_path"],
            "grid": fine_grid,
            "grid_render": render,
            "image_to_screen": transform,
            "crop": child_request["crop"],
            "context": child_request["context"],
            "planner_request": child_request,
        },
    }


PLANNER_ALLOWED_RECOVERY_COMMANDS = (
    "recovery.guided_grid_click",
    "recovery.guided_grid_crop",
    "recovery.transient_grid_after_click",
    "recovery.capture_grid",
    "input.action_sequence",
    "input.hotkey",
    "input.click_candidate",
    "input.invoke_candidate",
    "input.set_candidate_value",
    "input.expand_candidate",
    "input.select_candidate",
    "planner.step",
    "status.current",
)
GENERIC_PLANNER_RECOVERY_HINT = "Inspect the active app or dialog for related controls before repeating a failed tactic."
SAVE_DIALOG_PLANNER_RECOVERY_HINT = (
    GENERIC_PLANNER_RECOVERY_HINT
    + " For save dialogs, check labels separately from filename fields and look for location dropdowns, "
    "browse/more-options links, enabled/disabled buttons, and alternate navigation paths."
)


def save_open_dialog_active(active_context: dict[str, Any] | None) -> bool:
    if not isinstance(active_context, dict) or not active_context.get("dialog_open"):
        return False
    for key in ("current_dialog", "active_window"):
        window = active_context.get(key)
        if isinstance(window, dict):
            title = str(window.get("title") or "").lower()
            if "save" in title or "open" in title:
                return True
    return False


def planner_recovery_hint_for_context(active_context: dict[str, Any] | None) -> str:
    # The save-dialog coaching is only relevant while a save/open dialog is active.
    return SAVE_DIALOG_PLANNER_RECOVERY_HINT if save_open_dialog_active(active_context) else GENERIC_PLANNER_RECOVERY_HINT


def create_planner_request(
    capture_data: dict[str, Any],
    args: dict[str, Any],
    failed_command: str,
    question: str | None = None,
) -> dict[str, Any]:
    request_id = str(capture_data["request_id"])
    request = {
        "schema": "planner-request.v1",
        "request_id": request_id,
        "status": "NEEDS_PLANNER",
        "failed_command": failed_command,
        "failure_type": args.get("failure_type") or "NONE",
        "reason": args.get("reason") or "Helper requested planner guidance.",
        "question_for_planner": question or "Using the grid screenshot, what single safe action should I try next?",
        "planner_recovery_hint": planner_recovery_hint_for_context(capture_data.get("active_context")),
        "allowed_recovery_commands": list(PLANNER_ALLOWED_RECOVERY_COMMANDS),
        "target": capture_data.get("target"),
        "title": args.get("title") or TARGET_TITLE,
        "grid_screenshot_path": capture_data.get("grid_screenshot_path"),
        "raw_screenshot_path": capture_data.get("raw_screenshot_path"),
        "grid": capture_data.get("grid"),
        "grid_render": capture_data.get("grid_render"),
        "image_to_screen": capture_data.get("image_to_screen"),
        "screenshot_width": capture_data.get("screenshot_width"),
        "screenshot_height": capture_data.get("screenshot_height"),
        "desktop_origin": capture_data.get("desktop_origin"),
        "target_window": capture_data.get("target_window"),
        "monitor": capture_data.get("monitor"),
        "foreground_window": capture_data.get("foreground_window"),
        "active_context": capture_data.get("active_context"),
        "action_menu": capture_data.get("action_menu"),
        "planner_coaching": capture_data.get("planner_coaching"),
        "working_memory": capture_data.get("working_memory") or load_planner_working_memory(),
        "capture_timestamp": capture_data.get("capture_timestamp"),
        "expected_result": args.get("expected_result") or "",
        "verification": args.get("verification") or {"type": "manual_expected_result"},
        "created_at": now_iso(),
    }
    request["planner_request_path"] = str(planner_request_path(request_id))
    write_planner_request(request)
    return request


def planner_request_staleness(planner_request: dict[str, Any], max_age_seconds: float) -> str:
    if max_age_seconds <= 0:
        return ""
    timestamp = str(planner_request.get("capture_timestamp") or planner_request.get("created_at") or "")
    try:
        captured_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return "Planner request has no parseable capture timestamp."
    current_time = datetime.now(captured_at.tzinfo) if captured_at.tzinfo is not None else datetime.now()
    age = (current_time - captured_at).total_seconds()
    if age > max_age_seconds:
        return f"Planner screenshot is stale: {age:.1f}s old exceeds {max_age_seconds:.1f}s."
    return ""


def verify_planner_screen_unchanged(
    planner_request: dict[str, Any],
    fresh_capture: dict[str, Any],
    args: dict[str, Any],
    *,
    command: str,
) -> dict[str, Any]:
    if bool(args.get("skip_screen_unchanged_check", False)) and not (args.get("preview") or args.get("confirm_token")):
        return command_result("SUCCESS", command, "Pre-action screen comparison skipped.", data={"skipped": True})
    comparison = compare_planner_screen(planner_request, fresh_capture, args)
    if comparison["status"] == "SUCCESS":
        return command_result("SUCCESS", command, "Pre-action screen matched planner request.", data=comparison["data"])

    fresh_data = fresh_capture.get("data") if isinstance(fresh_capture.get("data"), dict) else {}
    stale_args = {
        "target": fresh_data.get("target") or planner_request.get("target") or "desktop",
        "title": fresh_data.get("title") or planner_request.get("title") or TARGET_TITLE,
        "reason": "Screen changed after planner saw it; not clicking stale grid coordinates.",
        "failure_type": "STALE_SCREENSHOT",
        "expected_result": args.get("expected_result") or planner_request.get("expected_result") or "",
    }
    fresh_request = create_planner_request(
        fresh_data,
        stale_args,
        failed_command=command,
        question="The screen changed before the helper clicked. Use this fresh grid and choose the next single safe action.",
    )
    fresh_data["planner_request_path"] = fresh_request["planner_request_path"]
    fresh_data["planner_request"] = fresh_request
    return command_result(
        "NEEDS_PLANNER",
        command,
        "Screen changed before action; click was not executed.",
        data={
            "failure_type": "STALE_SCREENSHOT",
            "original_request_id": planner_request.get("request_id"),
            "original_raw_screenshot_path": planner_request.get("raw_screenshot_path"),
            "comparison": comparison["data"],
            "fresh_observation": fresh_data,
        },
        next_allowed_actions=["planner.step", "recovery.guided_grid_click", "recovery.guided_grid_crop", "helper.bridge_status"],
    )


def compare_planner_screen(planner_request: dict[str, Any], fresh_capture: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    native = planner_request.get("image_to_screen")
    fresh = (fresh_capture.get("data") or {}).get("image_to_screen")
    if native and (not fresh or fresh.get("verified") is not True or native.get("source_bounds") != fresh.get("source_bounds")):
        return {"status": "FAILED", "data": {"comparison_error": True, "reason": "Capture source geometry changed or is unverified."}}
    if Image is None or ImageChops is None or ImageStat is None:
        return {
            "status": "FAILED",
            "data": {"comparison_error": True, "reason": "Pillow image comparison tools are unavailable."},
        }
    original_path = Path(str(planner_request.get("raw_screenshot_path") or ""))
    if not original_path.exists():
        return {
            "status": "FAILED",
            "data": {
                "comparison_error": True,
                "reason": "Original planner raw screenshot is missing.",
                "path": str(original_path),
            },
        }
    fresh_image = fresh_capture.get("image")
    if fresh_image is None:
        return {
            "status": "FAILED",
            "data": {"comparison_error": True, "reason": "Fresh capture did not include an image."},
        }
    try:
        original_image = Image.open(original_path)
        fresh_region = comparable_fresh_region(planner_request, fresh_image)
        max_width = int(args.get("compare_width") or 512)
        original_small = normalize_compare_image(original_image, max_width)
        fresh_small = normalize_compare_image(fresh_region, max_width)
        if original_small.size != fresh_small.size:
            return {
                "status": "FAILED",
                "data": {
                    "matched": False,
                    "reason": "Screenshot dimensions changed.",
                    "original_size": list(original_small.size),
                    "fresh_size": list(fresh_small.size),
                },
            }
        diff = ImageChops.difference(original_small, fresh_small).convert("L")
        stat = ImageStat.Stat(diff)
        mean_delta = float(stat.mean[0])
        threshold = int(args.get("changed_pixel_threshold") or 24)
        histogram = diff.histogram()
        changed_pixels = sum(histogram[threshold + 1 :])
        total_pixels = max(1, diff.width * diff.height)
        changed_ratio = changed_pixels / total_pixels
        max_mean_delta = float(args.get("max_mean_delta") or 0.8)
        max_changed_ratio = float(args.get("max_changed_ratio") or 0.006)
        matched = mean_delta <= max_mean_delta and changed_ratio <= max_changed_ratio
        return {
            "status": "SUCCESS" if matched else "FAILED",
            "data": {
                "matched": matched,
                "mean_delta": round(mean_delta, 4),
                "changed_ratio": round(changed_ratio, 6),
                "changed_pixel_threshold": threshold,
                "max_mean_delta": max_mean_delta,
                "max_changed_ratio": max_changed_ratio,
                "original_size": list(original_image.size),
                "fresh_compared_size": list(fresh_region.size),
                "compare_size": list(diff.size),
            },
        }
    except Exception as exc:
        return {
            "status": "FAILED",
            "data": {"comparison_error": True, "reason": f"Screen comparison failed: {exc}"},
        }


def comparable_fresh_region(planner_request: dict[str, Any], fresh_image: Any) -> Any:
    crop = planner_request.get("crop") if isinstance(planner_request.get("crop"), dict) else None
    if not crop:
        return fresh_image
    left = int(crop.get("offset_x") or 0)
    top = int(crop.get("offset_y") or 0)
    width = int(crop.get("width") or 0)
    height = int(crop.get("height") or 0)
    if width <= 0 or height <= 0:
        return fresh_image
    return fresh_image.crop((left, top, left + width, top + height))


def normalize_compare_image(image: Any, max_width: int) -> Any:
    rgb = image.convert("RGB")
    if max_width > 0 and rgb.width > max_width:
        scale = max_width / rgb.width
        size = (max(1, int(round(rgb.width * scale))), max(1, int(round(rgb.height * scale))))
        resampling = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
        rgb = rgb.resize(size, resampling)
    return rgb


def validate_planner_target_window(planner_request: dict[str, Any], current_window: WindowInfo | None) -> str:
    if not planner_request.get("target_window") and str(planner_request.get("target") or "target_window") != "target_window":
        return ""
    captured = planner_request.get("target_window")
    if not captured:
        return "Planner request did not record target window bounds."
    if current_window is None:
        return "Target window is missing before guided click."
    if captured.get("hwnd") and int(captured["hwnd"]) != current_window.hwnd:
        return "Target window identity changed since planner screenshot."
    current = window_payload(current_window)
    captured_bounds = captured.get("bounds") or {}
    current_bounds = (current or {}).get("bounds") or {}
    for key in ["left", "top", "right", "bottom", "width", "height"]:
        if int(captured_bounds.get(key, -999999)) != int(current_bounds.get(key, -999998)):
            return f"Target window moved since planner screenshot: {captured_bounds} -> {current_bounds}."
    return ""


def current_window_for_planner(planner_request: dict[str, Any], title: str) -> WindowInfo | None:
    captured = planner_request.get("target_window") if isinstance(planner_request.get("target_window"), dict) else {}
    hwnd = int(captured.get("hwnd") or 0)
    if hwnd:
        window = window_info_from_hwnd(hwnd)
        if window is not None:
            return window
    return find_window_by_title(title)


def grid_coordinate_to_screen(planner_request: dict[str, Any], coordinate: tuple[int, int], current_window: WindowInfo | None) -> tuple[int, int] | None:
    if validate_planner_target_window(planner_request, current_window):
        return None
    return image_point_to_screen(planner_request.get("image_to_screen") or {}, coordinate)


def prepare_guided_grid_click(args: dict[str, Any], command: str) -> dict[str, Any]:
    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        return command_result("ERROR", command, "--request-id is required.")
    cell = str(args.get("cell") or "").strip().upper()
    if not cell:
        return command_result("ERROR", command, "--cell is required.")
    planner_request = load_planner_request(request_id)
    if planner_request is None:
        return command_result("UNCERTAIN", command, f"Planner request {request_id!r} not found.")
    stale = planner_request_staleness(planner_request, arg_float_or_default(args, "max_age_seconds", DEFAULT_STALE_SECONDS))
    if stale:
        return command_result(
            "UNCERTAIN",
            command,
            stale,
            data={"request_id": request_id, "failure_type": "STALE_SCREENSHOT", "planner_request": planner_request},
        )
    grid = planner_request.get("grid") or {}
    offset = str(args.get("offset") or "center").strip().lower()
    x_pct = args.get("x_pct")
    y_pct = args.get("y_pct")
    if (x_pct is None) != (y_pct is None):
        return command_result("ERROR", command, "--x-pct and --y-pct must be provided together.")
    coordinate = grid_cell_point(cell, grid, offset=offset, x_pct=x_pct, y_pct=y_pct)
    if coordinate is None:
        return command_result("ERROR", command, f"Invalid cell {cell!r}.")
    cell_bbox = grid_cell_bbox(cell, grid)
    title = str(planner_request.get("title") or args.get("title") or TARGET_TITLE)
    current_window = current_window_for_planner(planner_request, title)
    moved = validate_planner_target_window(planner_request, current_window)
    if moved:
        return command_result(
            "UNCERTAIN",
            command,
            moved,
            data={
                "request_id": request_id,
                "failure_type": "WINDOW_MOVED",
                "target_window_at_capture": planner_request.get("target_window"),
                "current_target_window": window_payload(current_window),
            },
        )
    screen_coordinate = grid_coordinate_to_screen(planner_request, coordinate, current_window)
    if screen_coordinate is None:
        return command_result(
            "UNCERTAIN",
            command,
            "Could not map grid coordinate to screen coordinate.",
            data={"request_id": request_id, "failure_type": "UNVERIFIED_GEOMETRY", "grid_coordinate": {"x": coordinate[0], "y": coordinate[1]}},
        )
    return command_result(
        "SUCCESS",
        command,
        "Guided grid click prepared.",
        data={
            "request_id": request_id,
            "cell": cell,
            "planner_request": planner_request,
            "title": title,
            "target": str(planner_request.get("target") or "target_window"),
            "cell_bbox": {
                "left": cell_bbox[0],
                "top": cell_bbox[1],
                "right": cell_bbox[2],
                "bottom": cell_bbox[3],
            }
            if cell_bbox
            else None,
            "offset": offset,
            "offset_pct": {
                "x": round(float(x_pct), 4),
                "y": round(float(y_pct), 4),
            }
            if x_pct is not None and y_pct is not None
            else {"x": GRID_OFFSET_PRESETS.get(offset, GRID_OFFSET_PRESETS["center"])[0], "y": GRID_OFFSET_PRESETS.get(offset, GRID_OFFSET_PRESETS["center"])[1]},
            "grid_coordinate": {"x": coordinate[0], "y": coordinate[1]},
            "screen_coordinate": {"x": screen_coordinate[0], "y": screen_coordinate[1]},
            "current_target_window": window_payload(current_window),
        },
    )


def verify_guided_result(planner_request: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    verification = args.get("verification") if isinstance(args.get("verification"), dict) else None
    if verification is None:
        verification = planner_request.get("verification") if isinstance(planner_request.get("verification"), dict) else {}
    verification_type = str(verification.get("type") or "manual_expected_result")
    if verification_type == "simon_progress_increment":
        after = read_json(STATE_FILE) or {}
        before_index = int(verification.get("before_index", -1))
        expected_color = str(verification.get("expected_color") or "")
        after_index = int(after.get("index", -1))
        passed = after_index == before_index + 1 and after.get("last_input") == expected_color and after.get("status") != "Wrong"
        return {
            "type": verification_type,
            "status": "SUCCESS" if passed else "FAILED",
            "before_index": before_index,
            "after_index": after_index,
            "expected_color": expected_color,
            "after_state": after,
        }
    if verification_type == "window_state":
        verify_args: dict[str, Any] = {
            "exact_title": verification.get("exact_title") or verification.get("title") or "",
            "title_contains": verification.get("title_contains") or "",
            "title_excludes": verification.get("title_excludes") or [],
            "min_width": int(verification.get("min_width") or 0),
            "min_height": int(verification.get("min_height") or 0),
            "allow_minimized": bool(verification.get("allow_minimized", False)),
            "require_foreground": bool(verification.get("require_foreground")),
            "focus_if_needed": bool(verification.get("focus_if_needed", verification.get("require_foreground"))),
            "screenshot_on_failure": False,
        }
        result = cmd_verify_window_state(verify_args)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {
            "type": verification_type,
            "status": "SUCCESS" if result.get("status") == "SUCCESS" else "FAILED",
            "verify_result": strip_internal(result),
            "matched_windows": data.get("matched_windows") or [],
            "foreground_window": data.get("foreground_window") or get_foreground_window_snapshot(),
        }
    return {
        "type": verification_type,
        "status": "MANUAL_REQUIRED",
        "expected_result": args.get("expected_result") or planner_request.get("expected_result") or "",
        "note": "No automatic verification rule is available for this planner request.",
    }


def parse_key_chord(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", "+").split("+") if part.strip()]


def set_clipboard_text(text: str) -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    finally:
        root.destroy()


def nudge_cursor_from_failsafe_corner() -> dict[str, Any]:
    check_command_cancelled()
    if pyautogui is None:
        return {"nudged": False, "reason": "pyautogui_unavailable"}
    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        return {"nudged": False, "reason": "cursor_position_unavailable"}
    width, height = pyautogui.size()
    user32 = ctypes.windll.user32
    virtual_left = int(user32.GetSystemMetrics(76))
    virtual_top = int(user32.GetSystemMetrics(77))
    virtual_width = int(user32.GetSystemMetrics(78))
    virtual_height = int(user32.GetSystemMetrics(79))
    primary_corners = [(0, 0), (0, height - 1), (width - 1, 0), (width - 1, height - 1)]
    virtual_corners = [
        (virtual_left, virtual_top),
        (virtual_left, virtual_top + virtual_height - 1),
        (virtual_left + virtual_width - 1, virtual_top),
        (virtual_left + virtual_width - 1, virtual_top + virtual_height - 1),
    ]
    near_corner = any(abs(point.x - x) <= 8 and abs(point.y - y) <= 8 for x, y in primary_corners + virtual_corners)
    off_primary = point.x < 0 or point.y < 0 or point.x >= width or point.y >= height
    if not near_corner and not off_primary:
        return {"nudged": False, "position": {"x": int(point.x), "y": int(point.y)}}
    safe_x = min(max(100, 0), max(0, width - 1))
    safe_y = min(max(100, 0), max(0, height - 1))
    ctypes.windll.user32.SetCursorPos(int(safe_x), int(safe_y))
    cancellable_sleep(0.05)
    return {
        "nudged": True,
        "from": {"x": int(point.x), "y": int(point.y)},
        "to": {"x": int(safe_x), "y": int(safe_y)},
    }


def read_cursor_position() -> dict[str, Any]:
    user32 = ctypes.windll.user32
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return {"available": False}
    return {"available": True, "x": int(point.x), "y": int(point.y)}


def current_process_elevation() -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        return {"available": False, "reason": "ctypes.windll unavailable"}
    TOKEN_QUERY = 0x0008
    TokenElevation = 20

    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    ctypes.set_last_error(0)
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return {"available": False, "last_error": int(ctypes.get_last_error())}
    try:
        elevation = TOKEN_ELEVATION()
        needed = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        ok = advapi32.GetTokenInformation(
            token,
            TokenElevation,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(needed),
        )
        if not ok:
            return {"available": False, "last_error": int(ctypes.get_last_error())}
        return {"available": True, "is_elevated": bool(elevation.TokenIsElevated)}
    finally:
        kernel32.CloseHandle(token)


def normalized_virtual_mouse_coordinate(screen_x: int, screen_y: int) -> dict[str, Any]:
    user32 = ctypes.windll.user32
    virtual_left = int(user32.GetSystemMetrics(76))
    virtual_top = int(user32.GetSystemMetrics(77))
    virtual_width = max(1, int(user32.GetSystemMetrics(78)))
    virtual_height = max(1, int(user32.GetSystemMetrics(79)))
    # Pixel centers, following Microsoft UIAutomation Input.cs; edges drift on wide virtual desktops.
    normalized_x = int(((int(screen_x) - virtual_left) + .5) * 65536 / virtual_width)
    normalized_y = int(((int(screen_y) - virtual_top) + .5) * 65536 / virtual_height)
    return {
        "x": max(0, min(65535, normalized_x)),
        "y": max(0, min(65535, normalized_y)),
        "virtual_bounds": {
            "left": virtual_left,
            "top": virtual_top,
            "width": virtual_width,
            "height": virtual_height,
        },
    }


def send_input_mouse_event(flags: int, dx: int = 0, dy: int = 0, *, check_cancel: bool = True) -> dict[str, Any]:
    # check_cancel=False is reserved for releasing a button that is already
    # pressed: a cancellation must never leave the mouse button stuck down.
    if check_cancel:
        check_command_cancelled()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    input_item = INPUT()
    input_item.type = INPUT_MOUSE
    input_item.union.mi = MOUSEINPUT(
        dx=int(dx),
        dy=int(dy),
        mouseData=0,
        dwFlags=int(flags),
        time=0,
        dwExtraInfo=None,
    )
    ctypes.set_last_error(0)
    count = int(user32.SendInput(1, ctypes.pointer(input_item), ctypes.sizeof(INPUT)))
    return {"count": count, "last_error": int(ctypes.get_last_error())}


def cursor_offset_px(cursor: dict[str, Any] | None, screen_x: int, screen_y: int) -> int | None:
    """Chebyshev distance between a cursor read-back and the requested point."""
    if not isinstance(cursor, dict) or not bool(cursor.get("available")):
        return None
    cursor_x = cursor.get("x")
    cursor_y = cursor.get("y")
    if cursor_x is None or cursor_y is None:
        return None
    return max(abs(int(cursor_x) - int(screen_x)), abs(int(cursor_y) - int(screen_y)))


def cursor_within_tolerance(
    cursor: dict[str, Any] | None,
    screen_x: int,
    screen_y: int,
    tolerance: int = CLICK_CURSOR_TOLERANCE_PX,
) -> bool:
    offset = cursor_offset_px(cursor, screen_x, screen_y)
    return offset is not None and offset <= int(tolerance)


def perform_screen_click(screen_x: int, screen_y: int, *, before_press=None) -> dict[str, Any]:
    """Move, verify the cursor, then press the left button exactly once.

    1. SendInput absolute move, then read the cursor back. Normalized 0-65535
       virtual-desk coordinates can land 1-2px off, so a read-back within
       CLICK_CURSOR_TOLERANCE_PX is accepted.
    2. Otherwise correct once with SetCursorPos and re-verify.
    3. Only a verified cursor is clicked, with one SendInput down/up pair.
       An unverifiable cursor gets zero clicks (INPUT_CURSOR_UNVERIFIED), and a
       rejected SendInput press gets no blind fallback click (error 5 maps to
       INPUT_PERMISSION_DENIED).
    """
    check_command_cancelled()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetCursorPos.argtypes = (wintypes.INT, wintypes.INT)
    user32.SetCursorPos.restype = wintypes.BOOL
    target_x = int(screen_x)
    target_y = int(screen_y)
    tolerance = int(CLICK_CURSOR_TOLERANCE_PX)
    before_cursor = read_cursor_position()
    normalized = normalized_virtual_mouse_coordinate(target_x, target_y)
    sendinput_move = send_input_mouse_event(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        normalized["x"],
        normalized["y"],
    )
    cancellable_sleep(0.03)
    after_move = read_cursor_position()
    verified_cursor = after_move
    move_method = "sendinput_absolute"
    correction: dict[str, Any] | None = None
    if not cursor_within_tolerance(after_move, target_x, target_y, tolerance):
        check_command_cancelled()
        ctypes.set_last_error(0)
        set_cursor = bool(user32.SetCursorPos(target_x, target_y))
        set_cursor_last_error = int(ctypes.get_last_error())
        cancellable_sleep(0.03)
        after_correction = read_cursor_position()
        correction = {
            "method": "SetCursorPos",
            "reason": "cursor_outside_tolerance_after_sendinput_move",
            "cursor_offset_before_correction_px": cursor_offset_px(after_move, target_x, target_y),
            "set_cursor_succeeded": set_cursor,
            "set_cursor_last_error": set_cursor_last_error,
            "cursor_after_correction": after_correction,
        }
        verified_cursor = after_correction
        move_method = "SetCursorPos_correction"
    cursor_verified = cursor_within_tolerance(verified_cursor, target_x, target_y, tolerance)
    move_injected = int(sendinput_move.get("count") or 0) >= 1 or bool(correction and correction.get("set_cursor_succeeded"))
    sendinput_down: dict[str, Any] = {"count": None, "last_error": None}
    sendinput_up: dict[str, Any] = {"count": None, "last_error": None}
    button_release_recovery: dict[str, Any] | None = None
    press_guard_error = before_press() if cursor_verified and before_press else ""
    if cursor_verified and not press_guard_error:
        # Last cancellation point: send_input_mouse_event checks before the press.
        sendinput_down = send_input_mouse_event(MOUSEEVENTF_LEFTDOWN)
        if int(sendinput_down.get("count") or 0) >= 1:
            # The button is down: always release it, never cancel in between,
            # and never press again.
            time.sleep(0.02)
            sendinput_up = send_input_mouse_event(MOUSEEVENTF_LEFTUP, check_cancel=False)
            if int(sendinput_up.get("count") or 0) < 1:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                button_release_recovery = {
                    "method": "mouse_event_left_up",
                    "reason": "SendInput left-up was not accepted; released the button without pressing again.",
                }
    after_click = read_cursor_position()
    down_injected = int(sendinput_down.get("count") or 0) >= 1
    up_injected = int(sendinput_up.get("count") or 0) >= 1
    click_count = 1 if down_injected else 0
    click_verified = bool(cursor_verified and down_injected and up_injected)
    if cursor_verified:
        injection_succeeded = down_injected
    else:
        # No press was attempted. A move that Windows accepted but that did not
        # land maps to INPUT_CURSOR_UNVERIFIED in input_permission_recovery_payload.
        injection_succeeded = move_injected
    verified_point = (
        {"x": int(verified_cursor.get("x")), "y": int(verified_cursor.get("y"))}
        if cursor_verified
        else None
    )
    return {
        "backend": "sendinput",
        "click_method": "sendinput",
        "click_count": click_count,
        "requested_point": {"x": target_x, "y": target_y},
        "verified_point": verified_point,
        "cursor_offset_px": cursor_offset_px(verified_cursor, target_x, target_y),
        "cursor_tolerance_px": tolerance,
        "move_method": move_method,
        "sendinput_counts": {
            "move": sendinput_move.get("count"),
            "left_down": sendinput_down.get("count"),
            "left_up": sendinput_up.get("count"),
        },
        "sendinput_errors": {
            "move": sendinput_move.get("last_error"),
            "left_down": sendinput_down.get("last_error"),
            "left_up": sendinput_up.get("last_error"),
        },
        "injection_succeeded": bool(injection_succeeded),
        "click_verified": click_verified,
        "cursor_verified": cursor_verified,
        "click_skipped_reason": None if cursor_verified else "cursor_unverified",
        "press_guard_error": press_guard_error,
        "cursor_before": before_cursor,
        "cursor_after_move": after_move,
        "cursor_after_click": after_click,
        "cursor_read_available": bool(verified_cursor.get("available")) if isinstance(verified_cursor, dict) else False,
        "move_fallback_used": correction is not None,
        "fallback_result": correction,
        "button_release_recovery": button_release_recovery,
        "normalized_coordinate": {"x": normalized["x"], "y": normalized["y"]},
        "virtual_bounds": normalized["virtual_bounds"],
    }


def input_permission_recovery_payload(click_backend: dict[str, Any], command: str) -> dict[str, Any]:
    errors = click_backend.get("sendinput_errors") if isinstance(click_backend.get("sendinput_errors"), dict) else {}
    error_values = {int(value) for value in errors.values() if isinstance(value, int)}
    elevation = current_process_elevation()
    is_elevated = bool(elevation.get("available") and elevation.get("is_elevated"))
    if 5 in error_values:
        failure_type = "INPUT_PERMISSION_DENIED"
        instruction = (
            "Regular helper cannot continue because Windows denied synthetic input. "
            "Close the regular helper, start the elevated helper, approve UAC if prompted, "
            "then take a fresh screenshot and reassess before continuing."
        )
        elevated_reason = "SendInput returned Windows error 5 (Access denied)."
        recovery_flow = ["helper.exit", "helper.start_elevated", "planner.fast_observe"]
    elif click_backend.get("injection_succeeded") and not click_backend.get("click_verified"):
        failure_type = "INPUT_CURSOR_UNVERIFIED"
        if is_elevated:
            instruction = (
                "The elevated helper sent input events, but Windows did not move the cursor to the requested coordinate. "
                "This is likely an input-routing/session boundary, not a UAC problem. Capture a fresh screenshot, "
                "diagnose the environment, and try a backend/candidate or app-specific route instead of repeating elevation."
            )
            elevated_reason = "The helper is already elevated; cursor movement still could not be verified."
            recovery_flow = ["planner.fast_observe", "diagnose.environment"]
        else:
            instruction = (
                "The helper sent input events, but Windows did not move the cursor to the requested coordinate. "
                "Close the regular helper, start the elevated helper, approve UAC if prompted, "
                "then take a fresh screenshot and retry the action once."
            )
            elevated_reason = "Input events were accepted, but cursor movement could not be verified."
            recovery_flow = ["helper.exit", "helper.start_elevated", "planner.fast_observe"]
    else:
        failure_type = "INPUT_INJECTION_UNAVAILABLE"
        instruction = "The helper could not prove a usable physical input path. Reassess the desktop state before retrying."
        elevated_reason = ""
        recovery_flow = ["planner.fast_observe", "diagnose.environment"]
    return {
        "failure_type": failure_type,
        "blocked_command": command,
        "recovery_instruction": instruction,
        "recommended_recovery_flow": recovery_flow,
        "elevated_helper_reason": elevated_reason,
        "process_elevation": elevation,
    }


@contextmanager
def keyboard_only_pyautogui_context():
    if pyautogui is None:
        yield
        return
    original_failsafe = bool(pyautogui.FAILSAFE)
    pyautogui.FAILSAFE = False
    try:
        yield
    finally:
        pyautogui.FAILSAFE = original_failsafe


if __name__ == "__main__":
    sys.exit(main())
