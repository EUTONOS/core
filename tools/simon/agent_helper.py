from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Any

import helperctl as helper_command_router
from simon_common import (
    COLOR_ORDER,
    COLOR_SPECS,
    COMMAND_FILE,
    DIALOG_EXPECTED_CHOICE,
    DIALOG_EXPECTED_NUMBER,
    DIALOG_TITLE,
    HELPER_BRIDGE_STATUS_FILE,
    HELPER_INSTANCE_LOCK_FILE,
    HELPER_COMMAND_FILE,
    HELPER_EXPANDED_NAME,
    HELPER_LOG_FILE,
    HELPER_SHORT_NAME,
    HELPER_RESULT_FILE,
    HELPER_TITLE,
    RUNTIME_DIR,
    STATE_FILE,
    TARGET_TITLE,
    append_log,
    ensure_runtime_dir,
    now_stamp,
    read_json,
    write_json_atomic,
)

try:
    from PIL import ImageGrab
except ImportError:  # pragma: no cover - surfaced in the UI at runtime.
    ImageGrab = None

try:
    import pyautogui
except ImportError:  # pragma: no cover - surfaced in the UI at runtime.
    pyautogui = None


CONFIDENCE_THRESHOLD = 0.80
COLOR_TOLERANCE = 38
MIN_COLOR_PIXELS = 4500
ACTION_SCAN_ATTEMPTS = 3
FOCUS_SETTLE_SECONDS = 0.20
GAUNTLET_CLICK_STEPS = 18
BRIDGE_APP_STALE_SECONDS = 30.0
EXTERNAL_COMMAND_POLL_MS = 100
EXTERNAL_COMMAND_IDLE_POLL_MS = 200
EXTERNAL_COMMAND_TIMEOUT_GRACE_SECONDS = 0.25
DEFAULT_AI_IDLE_EXIT_SECONDS = 600.0
IDLE_POLL_INTERVAL_MS = 5000
FINAL_STATUS_LOCK_TIMEOUT_SECONDS = 0.5
VK_ESCAPE = 0x1B
CURRENT_INSTANCE_LOCK_FD: int | None = None
# Serializes the one terminal "stopped" bridge-status write between the Tk thread and the
# force-exit timer thread so the last record always describes the real stop.
FINAL_STATUS_LOCK = threading.Lock()
# Serializes best-effort capture-route tally updates (read-modify-write of the tally file).
CAPTURE_ROUTE_TALLY_LOCK = threading.Lock()


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
    def bounds_text(self) -> str:
        return f"({self.left}, {self.top}) {self.width}x{self.height}"


@dataclass
class ExternalCommandJob:
    request: dict[str, Any]
    request_id: str
    command: str
    args: dict[str, Any]
    created_epoch: float
    started_epoch: float
    timeout_seconds: float
    generation: int
    token: helper_command_router.CommandCancellationToken
    thread: threading.Thread | None = None
    result: dict[str, Any] | None = None
    preserve_foreground: bool = False
    completed_epoch: float | None = None
    terminal_result_written: bool = False
    timeout_reported: bool = False
    cancel_event: str = ""

    @property
    def deadline_epoch(self) -> float:
        return self.created_epoch + max(0.1, self.timeout_seconds) + EXTERNAL_COMMAND_TIMEOUT_GRACE_SECONDS


@dataclass
class DetectedButton:
    color: str
    bbox: tuple[int, int, int, int]
    center: tuple[int, int]
    pixels: int
    confidence: float


def load_idle_exit_seconds() -> float | None:
    raw = str(os.environ.get("SIMON_HELPER_IDLE_EXIT_SECONDS", "")).strip()
    if not raw:
        return DEFAULT_AI_IDLE_EXIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_AI_IDLE_EXIT_SECONDS
    if value <= 0:
        return None
    return max(30.0, value)


AI_IDLE_EXIT_SECONDS = load_idle_exit_seconds()


def find_window_by_title(exact_title: str) -> WindowInfo | None:
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
        if title != exact_title:
            return True

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        minimized = bool(user32.IsIconic(hwnd))
        matches.append(
            WindowInfo(
                hwnd=hwnd,
                title=title,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
                minimized=minimized,
            )
        )
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    if not matches:
        return None
    visible_matches = [item for item in matches if not item.minimized and item.width > 100 and item.height > 100]
    return visible_matches[0] if visible_matches else matches[0]


def bring_window_to_front(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
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

    def confirm_foreground() -> bool:
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)

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
            time.sleep(0.05)
            if confirm_foreground():
                return True
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
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
            time.sleep(0.05)
            if confirm_foreground():
                return True
        except Exception:
            pass
        foreground_hwnd = int(user32.GetForegroundWindow() or 0)
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground_hwnd, None)) if foreground_hwnd else 0
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        current_thread = int(kernel32.GetCurrentThreadId())
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
                time.sleep(0.05)
                if confirm_foreground():
                    return True
            try:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.05)
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


def get_foreground_window_snapshot() -> dict[str, Any]:
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    return {"hwnd": hwnd, "title": get_window_title(hwnd)}


class AgentHelperApp:
    def __init__(self) -> None:
        ensure_runtime_dir()
        self.root = tk.Tk()
        self.root.title(HELPER_TITLE)
        self.root.geometry("760x620")
        self.root.minsize(680, 520)

        self.state = "NOT_READY"
        self.window_info: WindowInfo | None = None
        self.detected: dict[str, DetectedButton] = {}
        self.detection_confidence = 0.0
        self.frozen = False
        self.last_action = "-"
        self.last_verification = "-"
        self.run_start_time: float | None = None
        self.verified_steps = 0
        self.error_count = 0
        self.freeze_count = 0
        self.last_click_coordinate = "-"
        self.last_cursor_restore = "-"
        self.run_result = "No run yet"
        self.text_to_paste = tk.StringVar(value="Codex helper pasted this text")
        self.last_text_result = "-"
        self.last_switch_result = "-"
        self.last_dialog_result = "-"
        self.last_gauntlet_result = "-"
        self.latest_failure_report = "-"
        self.latest_failure_screenshot = "-"
        self.last_foreground = "-"
        self.helper_temporarily_hidden = False
        self.debug_window_visible = True
        self.last_external_command_id = ""
        self.bridge_instance_id = f"agent-helper-{os.getpid()}-{int(time.time())}"
        self.last_bridge_heartbeat = 0.0
        self.last_agent_activity_at = time.monotonic()
        self.last_local_activity_at = time.monotonic()
        self.overlay_window: tk.Toplevel | None = None
        self.overlay_canvas: tk.Canvas | None = None
        self.overlay_text_id: int | None = None
        self.escape_was_down = False
        self.shutdown_started = False
        self.shutdown_reason = ""
        self.shutdown_event = ""
        self.shutdown_requested_at = ""
        self.final_status_written = False
        self.last_capture_route_thread: threading.Thread | None = None
        self.escape_poll_after_id: str | None = None
        self.external_poll_after_id: str | None = None
        self.hide_debug_after_id: str | None = None
        self.idle_poll_after_id: str | None = None
        self.external_command_lock = threading.Lock()
        self.active_external_job: ExternalCommandJob | None = None
        self.external_command_generation = 0

        self.vars = {
            "target_detected": tk.StringVar(value="No"),
            "bounds": tk.StringVar(value="-"),
            "buttons": tk.StringVar(value="0 / 6"),
            "required": tk.StringVar(value="-"),
            "confidence": tk.StringVar(value="0.00"),
            "state": tk.StringVar(value=self.state),
            "last_action": tk.StringVar(value="-"),
            "last_verification": tk.StringVar(value="-"),
            "run_result": tk.StringVar(value=self.run_result),
            "verified_steps": tk.StringVar(value="0 / 20"),
            "freezes_errors": tk.StringVar(value="0 freezes, 0 errors"),
            "last_click": tk.StringVar(value="-"),
            "elapsed": tk.StringVar(value="-"),
            "cursor": tk.StringVar(value="-"),
            "target_text": tk.StringVar(value="-"),
            "target_switch": tk.StringVar(value="-"),
            "target_dialog": tk.StringVar(value="-"),
            "text_result": tk.StringVar(value="-"),
            "switch_result": tk.StringVar(value="-"),
            "dialog_result": tk.StringVar(value="-"),
            "gauntlet_result": tk.StringVar(value="-"),
            "foreground": tk.StringVar(value="-"),
            "failure_report": tk.StringVar(value="-"),
        }

        self._build_ui()
        self._build_overlay()
        self._set_state("NOT_READY", "Open Simon Target, then scan.")
        startup_diagnostics = helper_command_router.collect_environment_diagnostics(include_capture=False)
        write_json_atomic(RUNTIME_DIR / "agent_helper_startup_diagnostics.json", startup_diagnostics)
        helper_command_router.write_bridge_status(
            "idle",
            {
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "note": f"{HELPER_SHORT_NAME} ready for bridged commands.",
                "startup_diagnostics_path": str(RUNTIME_DIR / "agent_helper_startup_diagnostics.json"),
                "environment_classification": startup_diagnostics.get("classification"),
                "desktop": startup_diagnostics.get("desktop"),
                "visible_titled_window_count": startup_diagnostics.get("window_probe", {}).get("visible_titled_window_count"),
            },
        )
        helper_command_router.append_bridge_history(
            "helper_started",
            "",
            "agent_helper",
            {
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "startup_diagnostics_path": str(RUNTIME_DIR / "agent_helper_startup_diagnostics.json"),
                "environment_classification": startup_diagnostics.get("classification"),
                "desktop": startup_diagnostics.get("desktop"),
                "visible_titled_window_count": startup_diagnostics.get("window_probe", {}).get("visible_titled_window_count"),
            },
        )
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._request_shutdown("Debug window close requested.", "window_close"))
        self.hide_debug_after_id = self.root.after(150, self._hide_debug_panel)
        self.escape_poll_after_id = self.root.after(100, self._poll_escape_stop)
        if AI_IDLE_EXIT_SECONDS is not None:
            self.idle_poll_after_id = self.root.after(IDLE_POLL_INTERVAL_MS, self._poll_idle_auto_exit)
        self._poll_external_commands()

    def _mark_activity(self, source: str) -> None:
        now = time.monotonic()
        if source == "agent":
            self.last_agent_activity_at = now
        else:
            self.last_local_activity_at = now

    def _build_ui(self) -> None:
        self.root.configure(bg="#eef2f7")
        root = ttk.Frame(self.root, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(4, weight=1)

        title = ttk.Label(root, text=HELPER_SHORT_NAME, font=("Segoe UI", 22, "bold"))
        title.grid(row=0, column=0, columnspan=2, sticky="w")

        subtitle = ttk.Label(
            root,
            text=f"{HELPER_EXPANDED_NAME}. Double-click the top overlay to reopen this window.",
            font=("Segoe UI", 10),
        )
        subtitle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        state_panel = ttk.LabelFrame(root, text="Visible State Panel", padding=12)
        state_panel.grid(row=2, column=0, sticky="nsew", pady=12, padx=(0, 8))
        controls = ttk.LabelFrame(root, text="Operator Controls", padding=12)
        controls.grid(row=2, column=1, sticky="nsew", pady=12, padx=(8, 0))

        fields = [
            ("Target window detected", "target_detected"),
            ("Target window bounds", "bounds"),
            ("Buttons detected", "buttons"),
            ("Current required color", "required"),
            ("Detection confidence", "confidence"),
            ("Current helper state", "state"),
            ("Foreground window", "foreground"),
            ("Last helper action", "last_action"),
            ("Last verification result", "last_verification"),
            ("Latest failure report", "failure_report"),
        ]
        for row, (label, key) in enumerate(fields):
            ttk.Label(state_panel, text=label, font=("Segoe UI", 9, "bold")).grid(
                row=row, column=0, sticky="w", pady=3
            )
            ttk.Label(state_panel, textvariable=self.vars[key], font=("Segoe UI", 10)).grid(
                row=row, column=1, sticky="w", padx=(16, 0), pady=3
            )
        state_panel.columnconfigure(1, weight=1)

        self.scan_button = tk.Button(controls, text="Rescan Target", height=2, command=self.scan_target)
        self.freeze_button = tk.Button(controls, text="Cancel / Freeze", height=2, command=self.toggle_freeze)
        self.copy_button = tk.Button(controls, text="Copy Log", height=2, command=self.copy_log)
        self.copy_failure_button = tk.Button(
            controls,
            text="Copy Failure Report",
            height=2,
            command=self.copy_failure_report,
        )
        self.hide_window_button = tk.Button(
            controls,
            text="Hide Debug Window",
            height=2,
            command=self._hide_debug_panel,
        )

        for button in [
            self.scan_button,
            self.freeze_button,
            self.copy_button,
            self.copy_failure_button,
            self.hide_window_button,
        ]:
            button.pack(fill="x", pady=5)

        results_frame = ttk.LabelFrame(root, text="Run Summary", padding=12)
        results_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        results_fields = [
            ("Run result", "run_result"),
            ("Verified steps", "verified_steps"),
            ("Freezes / errors", "freezes_errors"),
            ("Last click", "last_click"),
            ("Elapsed time", "elapsed"),
            ("Mouse cursor", "cursor"),
            ("Foreground", "foreground"),
            ("Target text", "target_text"),
            ("Target switch", "target_switch"),
            ("Target dialog", "target_dialog"),
            ("Paste result", "text_result"),
            ("Dialog result", "dialog_result"),
        ]
        for index, (label, key) in enumerate(results_fields):
            row = index // 3
            col = (index % 3) * 2
            ttk.Label(results_frame, text=label, font=("Segoe UI", 9, "bold")).grid(
                row=row, column=col, sticky="w", padx=(0, 8), pady=3
            )
            ttk.Label(results_frame, textvariable=self.vars[key], font=("Segoe UI", 10)).grid(
                row=row, column=col + 1, sticky="w", padx=(0, 24), pady=3
            )
        for col in range(6):
            results_frame.columnconfigure(col, weight=1 if col % 2 == 1 else 0)

        log_frame = ttk.LabelFrame(root, text="Detailed Action Log", padding=8)
        log_frame.grid(row=4, column=0, columnspan=2, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=15, wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _build_overlay(self) -> None:
        overlay = tk.Toplevel(self.root)
        overlay.withdraw()
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        chroma = "#00ff01"
        overlay.configure(bg=chroma)
        try:
            overlay.wm_attributes("-transparentcolor", chroma)
        except tk.TclError:
            overlay.configure(bg="#111827")

        width = 460
        height = 70
        x = max(20, (overlay.winfo_screenwidth() - width) // 2)
        y = 10
        overlay.geometry(f"{width}x{height}+{x}+{y}")

        canvas = tk.Canvas(overlay, width=width, height=height, bg=overlay["bg"], highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_oval(4, 4, width - 4, height - 4, fill="#111827", outline="#f59e0b", width=2)
        text_id = canvas.create_text(
            width // 2,
            height // 2,
            text="ESC TO STOP",
            fill="#f9fafb",
            font=("Segoe UI", 12, "bold"),
        )
        overlay.bind("<Double-Button-1>", lambda _event: self._toggle_debug_panel())
        canvas.bind("<Double-Button-1>", lambda _event: self._toggle_debug_panel())

        self.overlay_window = overlay
        self.overlay_canvas = canvas
        self.overlay_text_id = text_id
        self._refresh_overlay_status()
        overlay.deiconify()

    def _refresh_overlay_status(self) -> None:
        if not self.overlay_canvas or self.overlay_text_id is None:
            return
        self.overlay_canvas.itemconfigure(self.overlay_text_id, text="ESC TO STOP")

    def _toggle_debug_panel(self) -> None:
        self._mark_activity("local")
        if self.debug_window_visible:
            self._hide_debug_panel()
        else:
            self._show_debug_panel()

    def _show_debug_panel(self) -> None:
        self._mark_activity("local")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(100, lambda: self.root.attributes("-topmost", False))
        self.debug_window_visible = True
        self.helper_temporarily_hidden = False

    def _hide_debug_panel(self) -> None:
        self._mark_activity("local")
        self.root.withdraw()
        self.debug_window_visible = False

    def _poll_escape_stop(self) -> None:
        if self.shutdown_started:
            return
        try:
            is_down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
        except Exception:
            is_down = False
        if is_down and not self.escape_was_down:
            self._emergency_stop("Escape key pressed.")
        self.escape_was_down = is_down
        self.escape_poll_after_id = self.root.after(120, self._poll_escape_stop)

    def _poll_idle_auto_exit(self) -> None:
        if self.shutdown_started:
            return
        if AI_IDLE_EXIT_SECONDS is None:
            return
        last_activity = max(self.last_agent_activity_at, self.last_local_activity_at)
        idle_seconds = time.monotonic() - last_activity
        if idle_seconds >= AI_IDLE_EXIT_SECONDS:
            reason = f"No local or AI activity for {int(idle_seconds)} seconds; auto-exiting Helper."
            self._log("Idle Auto Exit", "-", "-", reason, "STOP")
            helper_command_router.append_bridge_history(
                "helper_idle_auto_exit",
                "",
                "agent_helper",
                {"helper_pid": os.getpid(), "instance_id": self.bridge_instance_id, "idle_seconds": round(idle_seconds, 1)},
            )
            self._request_shutdown(reason, "idle_auto_exit", delay_ms=50)
            return
        self.idle_poll_after_id = self.root.after(IDLE_POLL_INTERVAL_MS, self._poll_idle_auto_exit)

    def _emergency_stop(self, reason: str) -> None:
        if not self.frozen:
            self.frozen = True
            self.freeze_count += 1
        self.run_result = "STOPPED"
        self._set_state("UNCERTAIN", reason)
        self._log("Emergency Stop", "-", "-", reason, "STOP")
        self._cancel_active_external_command(reason, "emergency_stop", write_result=True)
        # The terminal "stopped" status is written once by the shutdown path itself; writing it
        # here too was immediately overwritten by "stopping" and left the bridge non-terminal.
        helper_command_router.append_bridge_history(
            "helper_emergency_stopped",
            "",
            "agent_helper",
            {"helper_pid": os.getpid(), "instance_id": self.bridge_instance_id, "note": reason},
        )
        self._request_shutdown(reason, "emergency_stop", delay_ms=50)

    def _request_shutdown(self, reason: str, event: str, delay_ms: int = 0) -> None:
        if self.shutdown_started:
            return
        self.shutdown_started = True
        self.shutdown_reason = reason
        self.shutdown_event = event
        self.shutdown_requested_at = helper_command_router.now_iso()
        # Bookkeeping below is best-effort: a locked/failed runtime file must never prevent the
        # shutdown callback and force-exit timer from being scheduled.
        try:
            self._cancel_active_external_command(reason, event, write_result=True)
        except Exception:
            pass
        try:
            self._log("Shutdown", "-", event, reason, "START")
        except Exception:
            pass
        try:
            helper_command_router.write_bridge_status(
                "stopping",
                {
                    "helper_pid": os.getpid(),
                    "instance_id": self.bridge_instance_id,
                    "note": reason,
                    "event": event,
                    "stopping_at": self.shutdown_requested_at,
                },
            )
        except Exception:
            pass
        try:
            helper_command_router.append_bridge_history(
                "helper_shutdown_requested",
                "",
                "agent_helper",
                {"helper_pid": os.getpid(), "instance_id": self.bridge_instance_id, "event": event, "note": reason},
            )
        except Exception:
            pass
        timer = threading.Timer(2.0, self._force_process_exit_after_shutdown)
        timer.daemon = True
        timer.start()
        self.root.after(delay_ms, self._shutdown_after_emergency_stop)

    def _shutdown_log(self, line: str) -> None:
        try:
            append_log(HELPER_LOG_FILE, line)
        except Exception:
            pass

    def _shutdown_after_emergency_stop(self) -> None:
        try:
            self._shutdown_log(f"{now_stamp()} | STOPPING | Shutdown | expected=- | clicked=destroy | Destroying Helper windows. | START")
            try:
                self._cancel_scheduled_callbacks()
            except Exception:
                pass
            if self.overlay_window is not None:
                try:
                    self.overlay_window.destroy()
                    self._shutdown_log(f"{now_stamp()} | STOPPING | Shutdown | expected=- | clicked=overlay | Overlay window destroyed. | OK")
                except tk.TclError:
                    self._shutdown_log(f"{now_stamp()} | STOPPING | Shutdown | expected=- | clicked=overlay | Overlay window destroy failed. | WARN")
                self.overlay_window = None
                self.overlay_canvas = None
                self.overlay_text_id = None
            try:
                self.root.quit()
                self._shutdown_log(f"{now_stamp()} | STOPPING | Shutdown | expected=- | clicked=root.quit | Tk mainloop quit requested. | OK")
            except tk.TclError:
                self._shutdown_log(f"{now_stamp()} | STOPPING | Shutdown | expected=- | clicked=root.quit | Tk mainloop quit failed. | WARN")
            try:
                self.root.destroy()
                self._shutdown_log(f"{now_stamp()} | STOPPED | Shutdown | expected=- | clicked=root.destroy | Tk root destroyed. | OK")
            except tk.TclError:
                self._shutdown_log(f"{now_stamp()} | STOPPED | Shutdown | expected=- | clicked=root.destroy | Tk root destroy failed. | WARN")
        finally:
            # Every clean shutdown path (helper.exit, Escape, idle exit, watchdog timeout, window
            # close) funnels through _request_shutdown into this method, so the terminal status is
            # written here, after the windows are gone, whatever happened above.
            self._write_final_stopped_status()

    def _write_final_stopped_status(
        self,
        *,
        forced: bool = False,
        fallback_reason: str = "",
        fallback_event: str = "",
    ) -> bool:
        """Write the terminal "stopped" bridge status once. Never raises; waits at most briefly."""
        if not FINAL_STATUS_LOCK.acquire(timeout=FINAL_STATUS_LOCK_TIMEOUT_SECONDS):
            return False
        try:
            if getattr(self, "final_status_written", False):
                return False
            reason = str(getattr(self, "shutdown_reason", "") or fallback_reason or f"{HELPER_SHORT_NAME} stopped.")
            event = str(getattr(self, "shutdown_event", "") or fallback_event or "unknown")
            instance_id = str(getattr(self, "bridge_instance_id", "") or "")
            try:
                stopped_at = helper_command_router.now_iso()
                helper_command_router.write_bridge_status(
                    "stopped",
                    {
                        "helper_pid": os.getpid(),
                        "instance_id": instance_id,
                        "note": reason,
                        "event": event,
                        "stop_reason": reason,
                        "stop_event": event,
                        "stopping_at": getattr(self, "shutdown_requested_at", "") or None,
                        "stopped_at": stopped_at,
                        "stopped_epoch": time.time(),
                        "forced_exit": bool(forced),
                    },
                )
            except Exception as exc:
                try:
                    append_log(
                        HELPER_LOG_FILE,
                        f"{now_stamp()} | STOPPED | Shutdown | expected=- | clicked=bridge_status | Final stopped status write failed: {exc} | WARN",
                    )
                except Exception:
                    pass
                return False
            self.final_status_written = True
            try:
                helper_command_router.append_bridge_history(
                    "helper_stopped",
                    "",
                    "agent_helper",
                    {
                        "helper_pid": os.getpid(),
                        "instance_id": instance_id,
                        "stop_reason": reason,
                        "stop_event": event,
                        "stopped_at": stopped_at,
                        "forced_exit": bool(forced),
                    },
                )
            except Exception:
                pass
            return True
        finally:
            FINAL_STATUS_LOCK.release()

    def _cancel_scheduled_callbacks(self) -> None:
        for after_id in [self.escape_poll_after_id, self.external_poll_after_id, self.hide_debug_after_id, self.idle_poll_after_id]:
            if not after_id:
                continue
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self.escape_poll_after_id = None
        self.external_poll_after_id = None
        self.hide_debug_after_id = None
        self.idle_poll_after_id = None

    def _force_process_exit_after_shutdown(self) -> None:
        try:
            append_log(
                HELPER_LOG_FILE,
                f"{now_stamp()} | STOPPED | Shutdown | expected=- | clicked=process_exit | Force-exit fallback reached. | STOP",
            )
        except Exception:
            pass
        try:
            helper_command_router.append_bridge_history(
                "helper_force_process_exit",
                "",
                "agent_helper",
                {"helper_pid": os.getpid(), "instance_id": self.bridge_instance_id},
            )
        except Exception:
            pass
        # No-op when the Tk shutdown path already wrote the terminal status.
        self._write_final_stopped_status(forced=True)
        release_instance_lock(CURRENT_INSTANCE_LOCK_FD)
        os._exit(0)

    def scan_target(self, return_focus: bool = True, require_foreground: bool = False) -> bool:
        self._mark_activity("local")
        self._set_state("SCANNING", "Scanning for Simon Target.")
        self.window_info = None
        self.detected = {}
        self.detection_confidence = 0.0
        self._refresh_vars()
        self.root.update_idletasks()

        if ImageGrab is None:
            return self._fail_scan("Pillow is not installed. Run: pip install -r requirements.txt")
        if pyautogui is None:
            return self._fail_scan("pyautogui is not installed. Run: pip install -r requirements.txt")

        window = find_window_by_title(TARGET_TITLE)
        if window is None:
            return self._fail_scan("Target window not found.")
        if window.minimized or window.width < 300 or window.height < 250:
            self.window_info = window
            return self._fail_scan("Target window is minimized or too small.")
        self.window_info = window

        self._hide_helper_for_target_view()
        bring_window_to_front(window.hwnd)
        time.sleep(FOCUS_SETTLE_SECONDS)
        refreshed_window = find_window_by_title(TARGET_TITLE)
        if refreshed_window:
            self.window_info = refreshed_window
            window = refreshed_window
        foreground = get_foreground_window_snapshot()
        self.last_foreground = f"{foreground['title'] or '(untitled)'} [{foreground['hwnd']}]"
        if require_foreground and int(foreground["hwnd"]) != int(window.hwnd):
            return self._fail_scan(
                f"Target did not get foreground focus. Foreground={foreground['title'] or '(untitled)'}",
                helper_state="UNCERTAIN",
            )

        state = read_json(STATE_FILE)
        if not state or state.get("app") != "Simon Target":
            return self._fail_scan("Target telemetry file is missing or invalid.")

        if state.get("status") == "Wrong":
            return self._fail_scan("Target reports Wrong. Reset target before continuing.", helper_state="ERROR")
        if state.get("status") == "Complete":
            self._set_state("COMPLETE", "Target sequence is complete.")
            self._refresh_vars()
            if return_focus:
                self._bring_helper_to_front()
            return True
        if not state.get("expected_color"):
            return self._fail_scan("Target did not report a required color.")

        try:
            image = ImageGrab.grab(bbox=(window.left, window.top, window.right, window.bottom), all_screens=True)
        except TypeError:
            image = ImageGrab.grab(bbox=(window.left, window.top, window.right, window.bottom))
        except Exception as exc:
            return self._fail_scan(f"Screenshot failed: {exc}")

        detected = self._detect_buttons(image, window.left, window.top)
        if len(detected) != len(COLOR_ORDER):
            found = ", ".join(sorted(detected)) if detected else "none"
            return self._fail_scan(f"Expected 6 buttons, found {len(detected)} ({found}).", helper_state="UNCERTAIN")
        if self._has_overlap(detected):
            return self._fail_scan("Detected button regions overlap.", helper_state="UNCERTAIN")

        self.detected = detected
        self.detection_confidence = min(button.confidence for button in detected.values())
        if self.detection_confidence < CONFIDENCE_THRESHOLD:
            return self._fail_scan(
                f"Detection confidence below threshold: {self.detection_confidence:.2f}",
                helper_state="UNCERTAIN",
            )

        self.frozen = False
        self._set_state("READY", "All six target buttons detected.")
        self._refresh_vars()
        if return_focus:
            self._bring_helper_to_front()
        return True

    def _detect_buttons(self, image: Any, offset_x: int, offset_y: int) -> dict[str, DetectedButton]:
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
                    r, g, b = pixels[x, y]
                    if not self._is_color_match((r, g, b), target):
                        continue

                    component = self._flood_fill_color_region(pixels, width, height, x, y, target, visited)
                    comp_count, min_x, min_y, max_x, max_y = component
                    comp_width = max_x - min_x + 1
                    comp_height = max_y - min_y + 1
                    if comp_count < MIN_COLOR_PIXELS or comp_width < 80 or comp_height < 60:
                        continue
                    if best_component is None or comp_count > best_component[0]:
                        best_component = component

            if best_component is None:
                continue

            count, min_x, min_y, max_x, max_y = best_component
            bbox = (offset_x + min_x, offset_y + min_y, offset_x + max_x, offset_y + max_y)
            center = ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)
            component_area = max(1, (max_x - min_x + 1) * (max_y - min_y + 1))
            fill_ratio = count / component_area
            # Tk buttons include text, borders, and highlight pixels inside the colored area,
            # so a visually complete button often has about 55-70% exact color fill.
            confidence = min(1.0, max(0.0, fill_ratio / 0.55))
            results[color] = DetectedButton(color=color, bbox=bbox, center=center, pixels=count, confidence=confidence)

        return results

    def _is_color_match(self, rgb: tuple[int, int, int], target: tuple[int, int, int]) -> bool:
        r, g, b = rgb
        distance = math.sqrt((r - target[0]) ** 2 + (g - target[1]) ** 2 + (b - target[2]) ** 2)
        return distance <= COLOR_TOLERANCE

    def _flood_fill_color_region(
        self,
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
                if not self._is_color_match(pixels[next_x, next_y], target):
                    continue
                visited.add(point)
                stack.append(point)

        return count, min_x, min_y, max_x, max_y

    def _has_overlap(self, detected: dict[str, DetectedButton]) -> bool:
        buttons = list(detected.values())
        for index, first in enumerate(buttons):
            for second in buttons[index + 1 :]:
                if self._rects_overlap(first.bbox, second.bbox):
                    return True
        return False

    def _rects_overlap(self, a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
        horizontal = min(a[2], b[2]) - max(a[0], b[0])
        vertical = min(a[3], b[3]) - max(a[1], b[1])
        return horizontal > 8 and vertical > 8

    def _ensure_target_ready_for_action(self, action_name: str) -> bool:
        last_message = "Target was not ready."
        for attempt in range(1, ACTION_SCAN_ATTEMPTS + 1):
            self._log(
                "Pre-action Scan",
                "-",
                "-",
                f"{action_name}: attempt {attempt}/{ACTION_SCAN_ATTEMPTS}",
                "CHECK",
            )
            ok = self.scan_target(return_focus=False, require_foreground=True)
            if ok and self.state == "READY" and not self.frozen:
                self.last_verification = f"{action_name}: target focused and coordinates refreshed"
                return True
            last_message = self.last_verification
            if attempt < ACTION_SCAN_ATTEMPTS:
                self.frozen = False
                time.sleep(0.25)

        self._freeze_error(f"{action_name} aborted after pre-action checks failed: {last_message}")
        return False

    def _confirm_target_attention_before_action(self, action_kind: str, expected: str) -> bool:
        window = find_window_by_title(TARGET_TITLE)
        if window:
            bring_window_to_front(window.hwnd)
            time.sleep(FOCUS_SETTLE_SECONDS)
        foreground = get_foreground_window_snapshot()
        self.last_foreground = f"{foreground['title'] or '(untitled)'} [{foreground['hwnd']}]"
        if not window or not self.window_info:
            self._log("Attention Check", expected, action_kind, "Target window missing immediately before action.", "FAIL")
            return False
        if int(foreground["hwnd"]) != int(window.hwnd):
            self._log(
                "Attention Check",
                expected,
                action_kind,
                f"Foreground mismatch: {foreground['title'] or '(untitled)'}",
                "FAIL",
            )
            return False
        if window.bounds_text != self.window_info.bounds_text:
            self._log(
                "Attention Check",
                expected,
                action_kind,
                f"Bounds changed after scan: {self.window_info.bounds_text}->{window.bounds_text}",
                "FAIL",
            )
            return False
        self.last_verification = "Target foreground and bounds confirmed immediately before action."
        return True

    def press_next(self) -> bool:
        if self.state != "READY" or self.frozen:
            self._log("Press Next refused", "-", "-", "Action controls are frozen or not ready.", "BLOCKED")
            return False

        if not self._ensure_target_ready_for_action("Press Next"):
            return False

        before = read_json(STATE_FILE)
        if not before:
            self._freeze_error("Target state missing before click.")
            return False
        expected = before.get("expected_color")
        before_index = int(before.get("index", -1))
        if expected not in self.detected:
            self._freeze_error(f"Required color {expected!r} was not detected.")
            return False

        center = self.detected[expected].center
        self.last_action = f"Press Next: {expected}"
        self._refresh_vars()
        self.root.update()

        try:
            if not self._confirm_target_attention_before_action("click", expected):
                self._freeze_error(f"Target lost attention before clicking {expected}.", expected, center)
                return False
            previous_position = pyautogui.position()
            self.last_cursor_restore = f"will restore to ({previous_position.x}, {previous_position.y})"
            self._refresh_vars()
            pyautogui.moveTo(center[0], center[1], duration=0.08)
            pyautogui.click()
        except Exception as exc:
            self._freeze_error(f"Click failed: {exc}")
            return False

        time.sleep(0.25)
        after = read_json(STATE_FILE)
        if not after:
            self._freeze_error("Target state missing after click.")
            return False

        verification = self._verify_advance(before, after, expected)
        if not verification["ok"]:
            self._freeze_error(verification["message"], expected, center)
            return False

        try:
            pyautogui.moveTo(previous_position.x, previous_position.y, duration=0.05)
            self.last_cursor_restore = f"restored to ({previous_position.x}, {previous_position.y})"
        except Exception:
            self.last_cursor_restore = "restore skipped"

        self.verified_steps = max(self.verified_steps, int(after.get("index", self.verified_steps)))
        self.last_click_coordinate = f"({center[0]}, {center[1]})"
        self.last_verification = verification["message"]
        self._log(
            "Press Next",
            expected,
            self.last_click_coordinate,
            verification["message"],
            "OK",
        )

        if after.get("status") == "Complete":
            self._set_state("COMPLETE", "Sequence complete.")
        else:
            self._set_state("READY", f"Verified progress {before_index}->{after.get('index')}.")
        self._bring_helper_to_front()
        return True

    def press_full_sequence(self) -> None:
        if self.run_start_time is None or self.state in {"READY", "COMPLETE"}:
            self._start_run()
        self._log("Press Full Sequence", "-", "-", "Starting controlled run.", "START")
        while True:
            self.root.update()
            state = read_json(STATE_FILE)
            if state and state.get("status") == "Complete":
                self._set_state("COMPLETE", "Sequence complete.")
                self.run_result = "PASS - completed sequence"
                self._refresh_vars()
                self._log("Press Full Sequence", "-", "-", "Completed sequence.", "OK")
                self._bring_helper_to_front()
                return
            if self.state != "READY" or self.frozen:
                self._log("Press Full Sequence", "-", "-", "Stopped because helper is not ready.", "STOP")
                return
            if not self.press_next():
                return
            time.sleep(0.12)

    def paste_text_to_target(self) -> bool:
        text = self.text_to_paste.get()
        before = read_json(STATE_FILE) or {}
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
        except Exception as exc:
            self._freeze_error(f"Clipboard update failed: {exc}")
            return False

        if not self._prepare_keyboard_target():
            return False

        try:
            pyautogui.hotkey("ctrl", "shift", "f")
            time.sleep(0.12)
            pyautogui.hotkey("ctrl", "v")
        except Exception as exc:
            self._freeze_error(f"Paste text failed: {exc}")
            return False

        after = self._wait_for_state(lambda payload: payload.get("text_value") == text)
        if not after:
            self._freeze_error("Could not verify pasted text changed target state.")
            return False

        previous_text = before.get("text_value", "")
        self.last_text_result = f"OK - {len(text)} chars pasted"
        self._log(
            "Paste Text",
            "-",
            "keyboard",
            f"text changed from {len(previous_text)} chars to {len(text)} chars",
            "OK",
        )
        self._bring_helper_to_front()
        return True

    def toggle_target_switch_shortcut(self) -> bool:
        if not self._prepare_keyboard_target():
            return False

        before = read_json(STATE_FILE) or {}
        before_value = bool(before.get("switch_enabled", False))
        try:
            pyautogui.hotkey("ctrl", "shift", "s")
        except Exception as exc:
            self._freeze_error(f"Shortcut toggle failed: {exc}")
            return False

        after = self._wait_for_state(lambda payload: bool(payload.get("switch_enabled", False)) != before_value)
        if not after:
            self._freeze_error("Could not verify shortcut toggled the target switch.")
            return False

        after_text = after.get("switch_text", "On" if after.get("switch_enabled") else "Off")
        self.last_switch_result = f"OK - switch {after_text}"
        self._log(
            "Toggle Switch Shortcut",
            "-",
            "ctrl+shift+s",
            f"switch {before_value}->{bool(after.get('switch_enabled', False))}",
            "OK",
        )
        self._bring_helper_to_front()
        return True

    def run_dialog_test(self) -> bool:
        if self.run_start_time is None:
            self._start_run()
        self.last_dialog_result = "RUNNING"
        self._refresh_vars()

        dialog = find_window_by_title(DIALOG_TITLE)
        if dialog is None:
            if not self._prepare_keyboard_target():
                return False
            self._log("Dialog Test", "-", "ctrl+shift+d", "Opening dialog challenge.", "START")
            try:
                pyautogui.hotkey("ctrl", "shift", "d")
            except Exception as exc:
                self._freeze_error(f"Dialog open shortcut failed: {exc}")
                return False
            dialog = self._wait_for_window(DIALOG_TITLE, timeout=3.0)

        if dialog is None:
            self._freeze_error("Dialog test aborted: challenge window did not open.")
            return False
        if not self._focus_window(dialog, "Dialog Test"):
            self._freeze_error("Dialog test aborted: challenge window could not get focus.")
            return False
        dialog = find_window_by_title(DIALOG_TITLE) or dialog

        points = self._dialog_control_points(dialog)
        previous_position: Any | None = None
        try:
            previous_position = pyautogui.position()
            self.last_cursor_restore = f"will restore to ({previous_position.x}, {previous_position.y})"
            self._refresh_vars()

            pyautogui.click(points["entry"][0], points["entry"][1])
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.write(DIALOG_EXPECTED_NUMBER, interval=0.03)
            time.sleep(0.05)
            pyautogui.click(points["yes"][0], points["yes"][1])
            time.sleep(0.05)
            pyautogui.click(points["ok"][0], points["ok"][1])
        except Exception as exc:
            self._freeze_error(f"Dialog test input failed: {exc}")
            return False

        confirmation_method = "clicked OK"
        after = self._wait_for_state(
            lambda payload: bool(payload.get("dialog_passed")) and not bool(payload.get("dialog_open")),
            timeout=0.8,
        )
        if not after:
            try:
                pyautogui.press("enter")
                confirmation_method = "clicked OK, then Return fallback"
            except Exception as exc:
                self._freeze_error(f"Dialog test OK fallback failed: {exc}")
                return False
            after = self._wait_for_state(
                lambda payload: bool(payload.get("dialog_passed")) and not bool(payload.get("dialog_open")),
                timeout=1.5,
            )
        if not after:
            self._freeze_error("Dialog test could not verify passed state after OK.")
            return False

        if previous_position is not None:
            try:
                pyautogui.moveTo(previous_position.x, previous_position.y, duration=0.05)
                self.last_cursor_restore = f"restored to ({previous_position.x}, {previous_position.y})"
            except Exception:
                self.last_cursor_restore = "restore skipped"

        self.last_dialog_result = "OK - entered 10, selected Yes, clicked OK"
        self._log(
            "Dialog Test",
            DIALOG_EXPECTED_NUMBER,
            f"Yes@{points['yes']} OK@{points['ok']}",
            f"dialog passed and closed via {confirmation_method}",
            "OK",
        )
        self._bring_helper_to_front()
        return True

    def run_twenty_step_gauntlet(self) -> None:
        self._reset_results()
        self._start_run()
        self.run_result = "RUNNING - 20-step gauntlet"
        self.last_gauntlet_result = "RUNNING"
        self._refresh_vars()
        self._log(
            "20-Step Gauntlet",
            "-",
            "-",
            f"Reset, press {GAUNTLET_CLICK_STEPS} colors, paste text, run dialog.",
            "START",
        )

        if not self._reset_target_for_run():
            return

        for step in range(GAUNTLET_CLICK_STEPS):
            self.root.update()
            if self.state != "READY" or self.frozen:
                self._log("20-Step Gauntlet", "-", "-", f"Stopped before click {step + 1}.", "STOP")
                return
            if not self.press_next():
                return
            time.sleep(0.12)

        state = read_json(STATE_FILE) or {}
        if int(state.get("index", -1)) != GAUNTLET_CLICK_STEPS:
            self._freeze_error(
                f"Gauntlet expected target index {GAUNTLET_CLICK_STEPS}, got {state.get('index')}."
            )
            return

        if not self.paste_text_to_target():
            return
        if not self.run_dialog_test():
            return

        self.run_result = "PASS - 18 clicks + paste + dialog"
        self.last_gauntlet_result = "OK - 20 actions passed"
        self._refresh_vars()
        self._log("20-Step Gauntlet", "-", "-", "18 clicks, paste, and dialog test passed.", "OK")
        self._bring_helper_to_front()

    def reset_target(self) -> None:
        self._mark_activity("local")
        self._reset_results()
        self._reset_target_for_run()

    def toggle_freeze(self) -> None:
        self._mark_activity("local")
        self.frozen = not self.frozen
        if self.frozen:
            self.freeze_count += 1
            self._set_state("UNCERTAIN", "Manually frozen.")
            self.run_result = "STOPPED"
            self._log("Cancel / Freeze", "-", "-", "Manual freeze enabled.", "OK")
        else:
            self.run_result = "READY"
            self._log("Cancel / Freeze", "-", "-", "Manual freeze cleared. Rescanning.", "OK")
            self.scan_target()
        self._refresh_vars()

    def copy_log(self) -> None:
        self._mark_activity("local")
        text = self.log_text.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("Copy Log", "-", "-", "Log copied to clipboard.", "OK")

    def copy_failure_report(self) -> None:
        self._mark_activity("local")
        if self.latest_failure_report == "-":
            self.root.clipboard_clear()
            self.root.clipboard_append("No failure report has been written yet.")
            self._log("Copy Failure Report", "-", "-", "No failure report available.", "NOOP")
            return
        try:
            report_text = Path(self.latest_failure_report).read_text(encoding="utf-8")
        except OSError as exc:
            report_text = f"Could not read failure report: {exc}"
        self.root.clipboard_clear()
        self.root.clipboard_append(report_text)
        self._log("Copy Failure Report", "-", "-", f"Copied {Path(self.latest_failure_report).name}", "OK")

    def _poll_external_commands(self) -> None:
        if self.shutdown_started:
            return
        self._write_bridge_heartbeat()
        if self.active_external_job is not None:
            self._poll_active_external_job()
            self.external_poll_after_id = self.root.after(EXTERNAL_COMMAND_POLL_MS, self._poll_external_commands)
            return
        request = read_json(HELPER_COMMAND_FILE)
        if request:
            request_id = str(request.get("request_id") or "")
            stale = self._remove_stale_external_request(request)
            if not stale and request_id == self.last_external_command_id:
                helper_command_router.append_bridge_history(
                    "helper_duplicate_request_ignored",
                    request_id,
                    str(request.get("command") or ""),
                    {"helper_pid": os.getpid(), "instance_id": self.bridge_instance_id},
                )
                try:
                    HELPER_COMMAND_FILE.unlink()
                except OSError:
                    pass
            elif not stale:
                self._start_external_command(request)
                self._poll_active_external_job()
                self.external_poll_after_id = self.root.after(EXTERNAL_COMMAND_POLL_MS, self._poll_external_commands)
                return
        self.external_poll_after_id = self.root.after(EXTERNAL_COMMAND_IDLE_POLL_MS, self._poll_external_commands)

    def _start_external_command(self, request: dict[str, Any]) -> ExternalCommandJob:
        request_id = str(request.get("request_id") or helper_command_router.make_request_id())
        command = str(request.get("command") or "")
        args = dict(request.get("args")) if isinstance(request.get("args"), dict) else {}
        helper_started_epoch = time.time()
        request_created_epoch = self._request_epoch(request, "created_epoch", helper_started_epoch)
        timeout_seconds = self._request_timeout_seconds(request)
        self.external_command_generation += 1
        job = ExternalCommandJob(
            request=dict(request),
            request_id=request_id,
            command=command,
            args=args,
            created_epoch=request_created_epoch,
            started_epoch=helper_started_epoch,
            timeout_seconds=timeout_seconds,
            generation=self.external_command_generation,
            token=helper_command_router.CommandCancellationToken(),
        )
        with self.external_command_lock:
            self.active_external_job = job
        self.last_external_command_id = request_id
        self._mark_activity("agent")
        self._log("External Command", "-", command, f"Executing request {request_id}", "START")
        self._write_external_start_records(job)
        self._prepare_external_command_on_ui(job)
        thread = threading.Thread(target=self._run_external_command_worker, args=(job,), name=f"simon-bridge-{request_id}", daemon=True)
        job.thread = thread
        thread.start()
        return job

    def _request_epoch(self, request: dict[str, Any], key: str, default: float) -> float:
        try:
            return float(request.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _request_timeout_seconds(self, request: dict[str, Any]) -> float:
        try:
            timeout = float(request.get("timeout_seconds") or 10.0)
        except (TypeError, ValueError):
            timeout = 10.0
        return max(0.1, min(3600.0, timeout))

    def _external_timing(self, job: ExternalCommandJob, completed_epoch: float) -> dict[str, float]:
        return {
            "queue_wait_ms": round((job.started_epoch - job.created_epoch) * 1000, 1),
            "helper_exec_ms": round((completed_epoch - job.started_epoch) * 1000, 1),
            "helper_total_ms": round((completed_epoch - job.created_epoch) * 1000, 1),
        }

    def _write_external_start_records(self, job: ExternalCommandJob) -> None:
        queue_wait_ms = round((job.started_epoch - job.created_epoch) * 1000, 1)
        helper_command_router.write_bridge_status(
            "executing",
            {
                "request_id": job.request_id,
                "command": job.command,
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "started_at": helper_command_router.now_iso(),
                "queue_wait_ms": queue_wait_ms,
                "timeout_seconds": job.timeout_seconds,
                "worker_boundary": "thread",
            },
        )
        helper_command_router.append_bridge_history(
            "helper_started_request",
            job.request_id,
            job.command,
            {
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "queue_wait_ms": queue_wait_ms,
                "timeout_seconds": job.timeout_seconds,
                "worker_boundary": "thread",
            },
        )
        helper_command_router.append_run_journal(
            "command_started",
            job.request_id,
            job.command,
            "helper",
            {
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "queue_wait_ms": queue_wait_ms,
                "timeout_seconds": job.timeout_seconds,
                "worker_boundary": "thread",
            },
        )

    def _prepare_external_command_on_ui(self, job: ExternalCommandJob) -> None:
        if not self._should_hide_for_external_command(job.command, job.args):
            return
        target = str(job.args.get("target") or "target_window")
        hide_overlay = target in {
            "foreground_window",
            "active_window",
            "current_dialog",
            "foreground_monitor",
            "active_monitor",
            "current_dialog_monitor",
        }
        self._hide_helper_for_target_view(hide_overlay=hide_overlay)

    def _run_external_command_worker(self, job: ExternalCommandJob) -> None:
        preserve_foreground = False
        try:
            with helper_command_router.command_cancellation(job.token):
                result, preserve_foreground = self._execute_external_command_in_worker(job)
        except helper_command_router.CommandCancelled as exc:
            result = helper_command_router.command_result(
                "UNCERTAIN",
                job.command or "external.unknown",
                f"{HELPER_SHORT_NAME} external command cancelled: {exc.reason}",
                data=helper_command_router.command_cancelled_payload(exc.reason),
                next_allowed_actions=["helper.bridge_status"],
            )
        except Exception as exc:
            result = helper_command_router.command_result(
                "ERROR",
                job.command or "external.unknown",
                f"{HELPER_SHORT_NAME} external worker failed: {exc}",
            )
        with self.external_command_lock:
            job.result = result
            job.preserve_foreground = preserve_foreground
            job.completed_epoch = time.time()

    def _execute_external_command_in_worker(self, job: ExternalCommandJob) -> tuple[dict[str, Any], bool]:
        command = job.command
        args = job.args
        helper_command_router.check_command_cancelled()
        if command == "recovery.capture_grid":
            return helper_command_router.dispatch(command, args, handler_override=lambda payload: self._external_capture_grid(payload, ui_prepared=True)), False
        if self._should_hide_for_external_command(command, args):
            window = find_window_by_title(str(args.get("title") or TARGET_TITLE))
            if window:
                bring_window_to_front(window.hwnd)
                helper_command_router.cancellable_sleep(FOCUS_SETTLE_SECONDS)
        helper_command_router.check_command_cancelled()
        result = helper_command_router.dispatch(command, args)
        return result, self._should_preserve_foreground(command, result)

    def _poll_active_external_job(self) -> None:
        job = self.active_external_job
        if job is None:
            return
        if job.terminal_result_written:
            if job.thread is not None and not job.thread.is_alive() and job.result is not None:
                self._record_late_external_result(job)
            return
        if job.result is not None:
            self._complete_external_job(job)
            return
        if job.thread is not None and not job.thread.is_alive():
            job.result = helper_command_router.command_result(
                "ERROR",
                job.command or "external.unknown",
                f"{HELPER_SHORT_NAME} external worker stopped without returning a result.",
                data={"failure_type": "HELPER_WORKER_CRASHED", "request_id": job.request_id},
                next_allowed_actions=["helper.bridge_status"],
            )
            job.completed_epoch = time.time()
            self._complete_external_job(job)
            return
        if time.time() >= job.deadline_epoch:
            self._timeout_external_job(job)

    def _complete_external_job(self, job: ExternalCommandJob) -> None:
        if self.active_external_job is not job or job.generation != self.external_command_generation:
            self._record_late_external_result(job)
            return
        result = job.result or helper_command_router.command_result(
            "ERROR",
            job.command or "external.unknown",
            f"{HELPER_SHORT_NAME} external worker returned no result.",
        )
        completed_epoch = job.completed_epoch or time.time()
        if not job.preserve_foreground and not self.shutdown_started:
            self._bring_helper_to_front()
        self._log(
            "External Command",
            "-",
            job.command,
            f"{result.get('status')}: {result.get('message')}",
            str(result.get("status") or "UNKNOWN"),
        )
        stripped = self._write_external_result(job, result, completed_epoch, "helper_completed_request", "command_completed")
        result_data = stripped.get("data") if isinstance(stripped.get("data"), dict) else {}
        exit_requested = job.command == "helper.exit" or bool(result_data.get("helper_should_exit"))
        if not exit_requested:
            # An exiting Helper must not advertise "idle" (responsive) even briefly; it goes
            # straight to "stopping" and then the terminal "stopped" written by the shutdown path.
            helper_command_router.write_bridge_status(
                "idle",
                {
                    "last_request_id": job.request_id,
                    "last_command": job.command,
                    "last_status": stripped.get("status"),
                    "helper_pid": os.getpid(),
                    "instance_id": self.bridge_instance_id,
                },
            )
        self._remove_external_command_file(job.request_id)
        with self.external_command_lock:
            if self.active_external_job is job:
                self.active_external_job = None
        if exit_requested:
            helper_command_router.append_bridge_history(
                "helper_exit_requested",
                job.request_id,
                job.command,
                {"helper_pid": os.getpid(), "instance_id": self.bridge_instance_id},
            )
            self._request_shutdown("helper.exit requested.", "helper_exit", delay_ms=50)

    def _write_external_result(
        self,
        job: ExternalCommandJob,
        result: dict[str, Any],
        completed_epoch: float,
        history_event: str,
        journal_event: str,
    ) -> dict[str, Any]:
        stripped = helper_command_router.strip_internal(result)
        timing = self._external_timing(job, completed_epoch)
        write_json_atomic(
            HELPER_RESULT_FILE,
            {
                "request_id": job.request_id,
                "completed_at": completed_epoch,
                "completed_at_iso": helper_command_router.now_iso(),
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "result": stripped,
                "timing": timing,
                "worker": {
                    "boundary": "thread",
                    "alive": bool(job.thread and job.thread.is_alive()),
                    "generation": job.generation,
                },
            },
        )
        helper_command_router.append_bridge_history(
            history_event,
            job.request_id,
            job.command,
            {
                "status": stripped.get("status"),
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "helper_exec_ms": timing["helper_exec_ms"],
                "helper_total_ms": timing["helper_total_ms"],
                "worker_alive": bool(job.thread and job.thread.is_alive()),
            },
        )
        helper_command_router.append_run_journal(
            journal_event,
            job.request_id,
            job.command,
            "helper",
            {
                "status": stripped.get("status"),
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "helper_exec_ms": timing["helper_exec_ms"],
                "helper_total_ms": timing["helper_total_ms"],
                "worker_alive": bool(job.thread and job.thread.is_alive()),
            },
        )
        return stripped

    def _timeout_external_job(self, job: ExternalCommandJob) -> None:
        if job.timeout_reported or job.terminal_result_written:
            return
        job.timeout_reported = True
        reason = f"External command exceeded its {job.timeout_seconds:.2f}s timeout."
        job.token.cancel(reason)
        result = helper_command_router.command_result(
            "UNCERTAIN",
            job.command or "external.unknown",
            f"{HELPER_SHORT_NAME} cancelled an external command after its watchdog timeout.",
            data={
                "failure_type": "HELPER_COMMAND_TIMEOUT",
                "request_id": job.request_id,
                "timeout_seconds": job.timeout_seconds,
                "cancel_reason": reason,
            },
            next_allowed_actions=["helper.bridge_status"],
        )
        completed_epoch = time.time()
        job.completed_epoch = completed_epoch
        self._log("External Command", "-", job.command, f"{result.get('status')}: {result.get('message')}", "TIMEOUT")
        self._write_external_result(job, result, completed_epoch, "helper_timed_out_request", "command_timed_out")
        job.terminal_result_written = True
        job.cancel_event = "watchdog_timeout"
        self._remove_external_command_file(job.request_id)
        helper_command_router.write_bridge_status(
            "cancelling",
            {
                "request_id": job.request_id,
                "command": job.command,
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "note": reason,
                "worker_alive": bool(job.thread and job.thread.is_alive()),
                "timeout_seconds": job.timeout_seconds,
            },
        )
        self._request_shutdown(f"{reason} Shutting down Helper to prevent post-timeout input.", "external_command_timeout", delay_ms=50)

    def _cancel_active_external_command(self, reason: str, event: str, *, write_result: bool = True) -> None:
        job = self.active_external_job
        if job is None:
            return
        job.token.cancel(reason)
        job.cancel_event = event
        if not write_result or job.terminal_result_written:
            return
        result = helper_command_router.command_result(
            "UNCERTAIN",
            job.command or "external.unknown",
            f"{HELPER_SHORT_NAME} external command cancelled: {reason}",
            data={
                **helper_command_router.command_cancelled_payload(reason),
                "request_id": job.request_id,
                "cancel_event": event,
            },
            next_allowed_actions=["helper.bridge_status"],
        )
        completed_epoch = time.time()
        job.completed_epoch = completed_epoch
        self._write_external_result(job, result, completed_epoch, "helper_cancelled_request", "command_cancelled")
        job.terminal_result_written = True
        self._remove_external_command_file(job.request_id)
        helper_command_router.write_bridge_status(
            "cancelling",
            {
                "request_id": job.request_id,
                "command": job.command,
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "note": reason,
                "event": event,
                "worker_alive": bool(job.thread and job.thread.is_alive()),
            },
        )

    def _record_late_external_result(self, job: ExternalCommandJob) -> None:
        if job.cancel_event == "late_result_recorded":
            return
        job.cancel_event = "late_result_recorded"
        result = job.result if isinstance(job.result, dict) else {}
        helper_command_router.append_bridge_history(
            "helper_late_result_ignored",
            job.request_id,
            job.command,
            {
                "status": result.get("status"),
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "generation": job.generation,
                "active_generation": self.external_command_generation,
                "terminal_result_written": job.terminal_result_written,
            },
        )

    def _remove_external_command_file(self, request_id: str) -> None:
        request = read_json(HELPER_COMMAND_FILE)
        if request and str(request.get("request_id") or "") not in {"", str(request_id)}:
            return
        try:
            HELPER_COMMAND_FILE.unlink()
        except OSError:
            pass

    def _write_bridge_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self.last_bridge_heartbeat < 2.0:
            return
        self.last_bridge_heartbeat = now
        job = self.active_external_job
        if job is not None:
            worker_alive = bool(job.thread and job.thread.is_alive())
            state = "cancelling" if job.token.is_cancelled() else "executing"
            helper_command_router.write_bridge_status(
                state,
                {
                    "helper_pid": os.getpid(),
                    "instance_id": self.bridge_instance_id,
                    "heartbeat_at": helper_command_router.now_iso(),
                    "request_id": job.request_id,
                    "command": job.command,
                    "started_at_epoch": job.started_epoch,
                    "elapsed_ms": round((time.time() - job.started_epoch) * 1000, 1),
                    "timeout_seconds": job.timeout_seconds,
                    "worker_alive": worker_alive,
                    "worker_boundary": "thread",
                },
            )
            return
        helper_command_router.write_bridge_status(
            "idle",
            {
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
                "heartbeat_at": helper_command_router.now_iso(),
                "last_request_id": self.last_external_command_id,
            },
        )

    def _remove_stale_external_request(self, request: dict[str, Any]) -> bool:
        created_epoch = float(request.get("created_epoch") or 0.0)
        if created_epoch <= 0:
            return False
        age = time.time() - created_epoch
        if age < BRIDGE_APP_STALE_SECONDS:
            return False
        request_id = str(request.get("request_id") or "")
        command = str(request.get("command") or "")
        try:
            HELPER_COMMAND_FILE.unlink()
        except OSError:
            return False
        helper_command_router.append_bridge_history(
            "helper_removed_stale_request",
            request_id,
            command,
            {"age_seconds": round(age, 3), "helper_pid": os.getpid(), "instance_id": self.bridge_instance_id},
        )
        helper_command_router.write_bridge_status(
            "idle",
            {
                "stale_request_removed": request_id,
                "removed_age_seconds": round(age, 3),
                "helper_pid": os.getpid(),
                "instance_id": self.bridge_instance_id,
            },
        )
        return True

    def _execute_external_command(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("command") or "")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        self._mark_activity("agent")
        self._log("External Command", "-", command, f"Executing request {request.get('request_id')}", "START")
        preserve_foreground = False
        try:
            if command == "recovery.capture_grid":
                result = helper_command_router.dispatch(command, args, handler_override=self._external_capture_grid)
                self._log(
                    "External Command",
                    "-",
                    command,
                    f"{result.get('status')}: {result.get('message')}",
                    str(result.get("status") or "UNKNOWN"),
                )
                return result
            if self._should_hide_for_external_command(command, args):
                window = find_window_by_title(str(args.get("title") or TARGET_TITLE))
                if window:
                    bring_window_to_front(window.hwnd)
                    time.sleep(FOCUS_SETTLE_SECONDS)
                self._hide_helper_for_target_view()
            result = helper_command_router.dispatch(command, args)
            preserve_foreground = self._should_preserve_foreground(command, result)
        except Exception as exc:
            result = helper_command_router.command_result(
                "ERROR",
                command or "external.unknown",
                f"{HELPER_SHORT_NAME} external command failed: {exc}",
            )
        finally:
            if not preserve_foreground:
                self._bring_helper_to_front()
        self._log(
            "External Command",
            "-",
            command,
            f"{result.get('status')}: {result.get('message')}",
            str(result.get("status") or "UNKNOWN"),
        )
        return result

    def _should_preserve_foreground(self, command: str, result: dict[str, Any]) -> bool:
        if command.startswith("recovery.transient_"):
            return True
        if command == "desktop.start_search_grid":
            return True
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return bool(data.get("preserve_foreground"))

    def _external_capture_grid(self, args: dict[str, Any], *, ui_prepared: bool = False) -> dict[str, Any]:
        if str(args.get("target") or "target_window") == "target_window":
            hwnd = int(args.get("hwnd") or 0)
            window = helper_command_router.window_info_from_hwnd(hwnd) if hwnd else helper_command_router.find_best_window_for_args(args)
            if window is None:
                return helper_command_router.command_result("UNCERTAIN", "recovery.capture_grid", "Target window not found.")
            helper_command_router.bring_window_to_front(window.hwnd)
            helper_command_router.cancellable_sleep(FOCUS_SETTLE_SECONDS)
        if not ui_prepared:
            self._hide_helper_for_target_view(hide_overlay=True)
            helper_command_router.cancellable_sleep(FOCUS_SETTLE_SECONDS)
        capture = helper_command_router.capture_target(args, with_grid=True, record_route=self._record_capture_route_best_effort)
        if capture["status"] != "SUCCESS":
            return capture
        data = capture["data"]
        request = helper_command_router.create_planner_request(data, args, failed_command=str(args.get("failed_command") or "unknown"))
        data.update(planner_request_path=request["planner_request_path"], planner_request=request)
        return helper_command_router.command_result("NEEDS_PLANNER", "recovery.capture_grid", "Grid screenshot captured by Helper app.", data=data)

    def _record_capture_route_best_effort(
        self,
        method: str,
        target: str,
        status: str,
        started: float,
        attempts: list[dict[str, Any]] | None = None,
    ) -> threading.Thread | None:
        """Tally a capture route without letting the tally fail or slow the capture.

        The tally does file I/O with atomic-replace retries, so it runs on a short daemon thread.
        The elapsed time is frozen here so the thread start does not inflate it.
        """
        elapsed_seconds = max(0.0, time.monotonic() - float(started))
        attempts_copy = [dict(item) if isinstance(item, dict) else item for item in (attempts or [])]

        def record() -> None:
            with CAPTURE_ROUTE_TALLY_LOCK:
                try:
                    helper_command_router.record_capture_route(
                        method,
                        target,
                        status,
                        time.monotonic() - elapsed_seconds,
                        attempts_copy,
                    )
                except Exception as exc:
                    try:
                        append_log(
                            HELPER_LOG_FILE,
                            f"{now_stamp()} | WARN | Capture Route Tally | expected=- | clicked=- | Tally failed: {exc} | WARN",
                        )
                    except Exception:
                        pass

        try:
            thread = threading.Thread(target=record, name="simon-capture-route-tally", daemon=True)
            thread.start()
        except Exception:
            return None
        self.last_capture_route_thread = thread
        return thread

    def _should_hide_for_external_command(self, command: str, args: dict[str, Any]) -> bool:
        target = str(args.get("target") or "target_window")
        title = str(args.get("title") or TARGET_TITLE)
        if target == "desktop":
            return False
        return command.startswith(("observe.", "recovery.", "simon.", "planner.")) and title == TARGET_TITLE

    def _reset_target_for_run(self) -> bool:
        write_json_atomic(COMMAND_FILE, {"command": "reset", "created_at": time.time()})
        time.sleep(0.35)
        self.frozen = False
        self._log("Reset Target", "-", "-", "Sent reset command to target.", "OK")
        return self.scan_target()

    def _wait_for_window(self, title: str, timeout: float = 2.0) -> WindowInfo | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            window = find_window_by_title(title)
            if window is not None and not window.minimized and window.width > 100 and window.height > 100:
                return window
            time.sleep(0.05)
        return None

    def _focus_window(self, window: WindowInfo, action_name: str) -> bool:
        bring_window_to_front(window.hwnd)
        time.sleep(FOCUS_SETTLE_SECONDS)
        refreshed = find_window_by_title(window.title)
        if refreshed is not None:
            window = refreshed
        foreground = get_foreground_window_snapshot()
        self.last_foreground = f"{foreground['title'] or '(untitled)'} [{foreground['hwnd']}]"
        self._refresh_vars()
        if int(foreground["hwnd"]) != int(window.hwnd):
            self._log(
                f"{action_name} Focus Check",
                "-",
                "window",
                f"Foreground mismatch: {foreground['title'] or '(untitled)'}",
                "FAIL",
            )
            return False
        return True

    def _dialog_control_points(self, window: WindowInfo) -> dict[str, tuple[int, int]]:
        return {
            "entry": (window.left + int(window.width * 0.56), window.top + int(window.height * 0.36)),
            "yes": (window.left + int(window.width * 0.15), window.top + int(window.height * 0.47)),
            "ok": (window.left + int(window.width * 0.62), window.top + int(window.height * 0.70)),
        }

    def _prepare_keyboard_target(self) -> bool:
        if pyautogui is None:
            self._freeze_error("pyautogui is not installed. Run: pip install -r requirements.txt")
            return False
        for attempt in range(1, ACTION_SCAN_ATTEMPTS + 1):
            window = find_window_by_title(TARGET_TITLE)
            if window is None or window.minimized:
                self._log(
                    "Keyboard Attention Check",
                    "-",
                    "keyboard",
                    f"Target missing/minimized attempt {attempt}/{ACTION_SCAN_ATTEMPTS}",
                    "FAIL",
                )
                time.sleep(0.25)
                continue
            self.window_info = window
            bring_window_to_front(window.hwnd)
            time.sleep(FOCUS_SETTLE_SECONDS)
            foreground = get_foreground_window_snapshot()
            self.last_foreground = f"{foreground['title'] or '(untitled)'} [{foreground['hwnd']}]"
            if int(foreground["hwnd"]) == int(window.hwnd):
                self._refresh_vars()
                self._log("Keyboard Attention Check", "-", "keyboard", "Target foreground confirmed.", "OK")
                return True
            self._log(
                "Keyboard Attention Check",
                "-",
                "keyboard",
                f"Foreground mismatch attempt {attempt}: {foreground['title'] or '(untitled)'}",
                "FAIL",
            )
            time.sleep(0.25)

        self._freeze_error("Keyboard action aborted: target could not be focused.")
        return False

    def _wait_for_state(self, predicate: Any, timeout: float = 1.5) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = read_json(STATE_FILE)
            if payload and predicate(payload):
                self._refresh_vars()
                return payload
            time.sleep(0.05)
        self._refresh_vars()
        return None

    def _verify_advance(self, before: dict[str, Any], after: dict[str, Any], expected: str) -> dict[str, Any]:
        before_index = int(before.get("index", -1))
        after_index = int(after.get("index", -1))
        after_status = after.get("status")
        last_input = after.get("last_input")

        if after_status == "Wrong":
            return {"ok": False, "message": "Target reports Wrong."}
        if last_input != expected:
            return {"ok": False, "message": f"Last input mismatch: expected {expected}, got {last_input}."}
        if after_status == "Complete" and after_index == before_index + 1:
            return {"ok": True, "message": f"verified progress {before_index}->{after_index}; complete"}
        if after_index != before_index + 1:
            return {"ok": False, "message": f"Progress did not advance: {before_index}->{after_index}."}
        return {"ok": True, "message": f"verified progress {before_index}->{after_index}"}

    def _fail_scan(self, message: str, helper_state: str = "NOT_READY") -> bool:
        self.frozen = True
        self.freeze_count += 1
        self._set_state(helper_state, message)
        self._write_failure_report("scan_failure", message)
        self._refresh_vars()
        self._log("Scan Target", "-", "-", message, helper_state)
        self._bring_helper_to_front()
        return False

    def _freeze_error(
        self,
        message: str,
        expected: str | None = None,
        center: tuple[int, int] | None = None,
    ) -> None:
        self.frozen = True
        self.freeze_count += 1
        self.error_count += 1
        self.run_result = "FAIL - helper froze safely"
        self._set_state("ERROR", message)
        coordinate = "-" if center is None else f"({center[0]}, {center[1]})"
        self._write_failure_report("action_failure", message, expected=expected or "-", coordinate=coordinate)
        self._log("Freeze", expected or "-", coordinate, message, "ERROR")
        self._refresh_vars()
        self._bring_helper_to_front()

    def _write_failure_report(
        self,
        failure_type: str,
        message: str,
        expected: str = "-",
        coordinate: str = "-",
    ) -> None:
        failures_dir = RUNTIME_DIR / "failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{failure_type}"
        screenshot_path = self._save_failure_screenshot(failures_dir / f"{base}.png")
        foreground = get_foreground_window_snapshot()
        payload = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "failure_type": failure_type,
            "message": message,
            "helper_state": self.state,
            "frozen": self.frozen,
            "expected": expected,
            "coordinate": coordinate,
            "target_window": self._window_payload(self.window_info),
            "dialog_window": self._window_payload(find_window_by_title(DIALOG_TITLE)),
            "foreground_window": foreground,
            "detected_buttons": {
                color: {
                    "bbox": button.bbox,
                    "center": button.center,
                    "pixels": button.pixels,
                    "confidence": round(button.confidence, 3),
                }
                for color, button in sorted(self.detected.items())
            },
            "detection_confidence": round(self.detection_confidence, 3),
            "target_state": read_json(STATE_FILE),
            "last_action": self.last_action,
            "last_verification": self.last_verification,
            "run_result": self.run_result,
            "verified_steps": self.verified_steps,
            "screenshot_path": str(screenshot_path) if screenshot_path else None,
            "helper_log_file": str(HELPER_LOG_FILE),
        }
        report_path = failures_dir / f"{base}.json"
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.latest_failure_report = str(report_path)
        self.latest_failure_screenshot = str(screenshot_path) if screenshot_path else "-"

    def _window_payload(self, window: WindowInfo | None) -> dict[str, Any] | None:
        if not window:
            return None
        return {
            "hwnd": window.hwnd,
            "title": window.title,
            "left": window.left,
            "top": window.top,
            "right": window.right,
            "bottom": window.bottom,
            "bounds_text": window.bounds_text,
            "minimized": window.minimized,
        }

    def _save_failure_screenshot(self, path: Path) -> Path | None:
        if ImageGrab is None or self.window_info is None:
            return None
        try:
            image = ImageGrab.grab(
                bbox=(self.window_info.left, self.window_info.top, self.window_info.right, self.window_info.bottom),
                all_screens=True,
            )
        except TypeError:
            try:
                image = ImageGrab.grab(
                    bbox=(self.window_info.left, self.window_info.top, self.window_info.right, self.window_info.bottom)
                )
            except Exception:
                return None
        except Exception:
            return None
        try:
            image.save(path)
        except OSError:
            return None
        return path

    def _set_state(self, state: str, verification: str | None = None) -> None:
        self.state = state
        if verification is not None:
            self.last_verification = verification
        self._refresh_vars()

    def _refresh_vars(self) -> None:
        state = read_json(STATE_FILE)
        required = state.get("expected_color") if state else None
        detected_names = sorted(self.detected)
        self.vars["target_detected"].set("Yes" if self.window_info else "No")
        self.vars["bounds"].set(self.window_info.bounds_text if self.window_info else "-")
        self.vars["buttons"].set(f"{len(self.detected)} / 6 ({', '.join(detected_names) if detected_names else '-'})")
        self.vars["required"].set(required.title() if required else "-")
        self.vars["confidence"].set(f"{self.detection_confidence:.2f}")
        self.vars["state"].set(self.state)
        self.vars["last_action"].set(self.last_action)
        self.vars["last_verification"].set(self.last_verification)
        foreground = get_foreground_window_snapshot()
        if foreground["hwnd"]:
            self.last_foreground = f"{foreground['title'] or '(untitled)'} [{foreground['hwnd']}]"
        state = read_json(STATE_FILE)
        total_steps = len(state.get("sequence", [])) if state else 20
        current_index = int(state.get("index", self.verified_steps)) if state else self.verified_steps
        self.vars["run_result"].set(self.run_result)
        self.vars["verified_steps"].set(f"{max(self.verified_steps, current_index)} / {total_steps}")
        self.vars["freezes_errors"].set(f"{self.freeze_count} freezes, {self.error_count} errors")
        self.vars["last_click"].set(self.last_click_coordinate)
        self.vars["elapsed"].set(self._elapsed_text())
        self.vars["cursor"].set(self.last_cursor_restore)
        target_text = state.get("text_value", "") if state else ""
        switch_text = state.get("switch_text", "-") if state else "-"
        if state and state.get("dialog_open"):
            dialog_text = (
                f"Open ({state.get('dialog_number_value') or '-'}, "
                f"{state.get('dialog_choice') or '-'})"
            )
        else:
            dialog_text = state.get("dialog_status", "-") if state else "-"
        self.vars["target_text"].set(target_text if target_text else "-")
        self.vars["target_switch"].set(switch_text)
        self.vars["target_dialog"].set(dialog_text)
        self.vars["text_result"].set(self.last_text_result)
        self.vars["switch_result"].set(self.last_switch_result)
        self.vars["dialog_result"].set(self.last_dialog_result)
        self.vars["gauntlet_result"].set(self.last_gauntlet_result)
        self.vars["foreground"].set(self.last_foreground)
        self.vars["failure_report"].set(Path(self.latest_failure_report).name if self.latest_failure_report != "-" else "-")
        self._refresh_action_buttons()
        self._refresh_overlay_status()

    def _refresh_action_buttons(self) -> None:
        self.scan_button.configure(state="normal")
        self.freeze_button.configure(text="Resume / Rescan" if self.frozen else "Cancel / Freeze")
        self.hide_window_button.configure(state="normal")

    def _log(self, action: str, expected: str, coordinate: str, verification: str, result: str) -> None:
        line = (
            f"{now_stamp()} | {self.state} | {action} | expected={expected} | "
            f"clicked={coordinate} | {verification} | {result}"
        )
        self.last_action = action
        self.last_verification = verification
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        append_log(HELPER_LOG_FILE, line)
        self._refresh_vars()

    def _start_run(self) -> None:
        self.run_start_time = time.monotonic()
        self.run_result = "RUNNING"
        state = read_json(STATE_FILE)
        self.verified_steps = int(state.get("index", 0)) if state else 0
        self.last_click_coordinate = "-"
        self.last_cursor_restore = "real cursor is moved for each click"
        self._refresh_vars()

    def _reset_results(self) -> None:
        self.run_start_time = None
        self.verified_steps = 0
        self.error_count = 0
        self.freeze_count = 0
        self.last_click_coordinate = "-"
        self.last_cursor_restore = "-"
        self.run_result = "No run yet"
        self.last_dialog_result = "-"
        self.last_gauntlet_result = "-"
        self._refresh_vars()

    def _elapsed_text(self) -> str:
        if self.run_start_time is None:
            return "-"
        elapsed = time.monotonic() - self.run_start_time
        return f"{elapsed:.1f}s"

    def _bring_helper_to_front(self) -> None:
        self.helper_temporarily_hidden = False
        if self.overlay_window is not None:
            self.overlay_window.deiconify()
        self._refresh_overlay_status()

    def _force_helper_front(self) -> None:
        self._show_debug_panel()

    def _hide_helper_for_target_view(self, hide_overlay: bool = False) -> None:
        if hide_overlay and self.overlay_window is not None:
            self.overlay_window.withdraw()
            self.overlay_window.update()
        if not self.debug_window_visible or self.helper_temporarily_hidden:
            return
        self.helper_temporarily_hidden = True
        self.root.withdraw()
        self.root.update()
        time.sleep(0.08)

    def run(self) -> None:
        self.root.mainloop()


def acquire_instance_lock() -> int | None:
    ensure_runtime_dir()
    existing = read_json(HELPER_INSTANCE_LOCK_FILE)
    if existing:
        existing_pid = int(existing.get("pid") or 0)
        if existing_pid and is_process_alive(existing_pid) and helper_heartbeat_is_fresh(existing_pid):
            return None
        try:
            HELPER_INSTANCE_LOCK_FILE.unlink()
        except OSError:
            pass
    try:
        fd = os.open(str(HELPER_INSTANCE_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    payload = {
        "schema": "agent-helper-instance-lock.v1",
        "pid": os.getpid(),
        "created_at": helper_command_router.now_iso(),
        "created_epoch": time.time(),
    }
    os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
    os.fsync(fd)
    return fd


def release_instance_lock(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    release_instance_lock_for_pid(os.getpid())


def release_instance_lock_for_pid(pid: int) -> None:
    lock = read_json(HELPER_INSTANCE_LOCK_FILE)
    if lock and int(lock.get("pid") or 0) == int(pid):
        try:
            HELPER_INSTANCE_LOCK_FILE.unlink()
        except OSError:
            pass


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
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


def helper_heartbeat_is_fresh(pid: int) -> bool:
    status = read_json(HELPER_BRIDGE_STATUS_FILE)
    if not isinstance(status, dict):
        return False
    data = status.get("data") if isinstance(status.get("data"), dict) else {}
    if int(data.get("helper_pid") or 0) != int(pid):
        return False
    if str(status.get("state") or "") != "idle":
        return False
    updated_epoch = float(status.get("updated_epoch") or 0.0)
    if updated_epoch <= 0:
        return False
    return (time.time() - updated_epoch) <= BRIDGE_APP_STALE_SECONDS


def write_startup_exit_report(reason: str, extra: dict[str, Any] | None = None) -> None:
    ensure_runtime_dir()
    report = {
        "schema": "agent-helper-startup-exit.v1",
        "timestamp": helper_command_router.now_iso(),
        "reason": reason,
        "pid": os.getpid(),
        "argv": sys.argv,
        "cwd": str(Path.cwd()),
        "extra": extra or {},
        "environment": helper_command_router.collect_environment_diagnostics(include_capture=False),
    }
    write_json_atomic(RUNTIME_DIR / "agent_helper_last_exit.json", report)
    helper_command_router.append_bridge_history(
        "helper_startup_exit",
        "",
        "agent_helper",
        {"helper_pid": os.getpid(), "reason": reason, "report_path": str(RUNTIME_DIR / "agent_helper_last_exit.json")},
    )


def install_exception_diagnostics() -> None:
    def write_exception_report(kind: str, exc_type: Any, exc_value: BaseException, exc_traceback: Any) -> None:
        import traceback

        ensure_runtime_dir()
        report = {
            "schema": "agent-helper-exception.v1",
            "timestamp": helper_command_router.now_iso(),
            "kind": kind,
            "pid": os.getpid(),
            "argv": sys.argv,
            "cwd": str(Path.cwd()),
            "exception_type": getattr(exc_type, "__name__", str(exc_type)),
            "exception": str(exc_value),
            "traceback": "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
            "environment": helper_command_router.collect_environment_diagnostics(include_capture=False),
        }
        write_json_atomic(RUNTIME_DIR / "agent_helper_last_exception.json", report)
        helper_command_router.append_bridge_history(
            "helper_exception",
            "",
            "agent_helper",
            {"helper_pid": os.getpid(), "kind": kind, "exception": str(exc_value), "report_path": str(RUNTIME_DIR / "agent_helper_last_exception.json")},
        )

    def excepthook(exc_type: Any, exc_value: BaseException, exc_traceback: Any) -> None:
        write_exception_report("main_thread", exc_type, exc_value, exc_traceback)
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = excepthook
    if hasattr(threading, "excepthook"):
        def thread_excepthook(args: Any) -> None:
            write_exception_report("thread", args.exc_type, args.exc_value, args.exc_traceback)
            threading.__excepthook__(args)

        threading.excepthook = thread_excepthook


if __name__ == "__main__":
    install_exception_diagnostics()
    instance_lock_fd = acquire_instance_lock()
    CURRENT_INSTANCE_LOCK_FD = instance_lock_fd
    if instance_lock_fd is None:
        existing = read_json(HELPER_INSTANCE_LOCK_FILE) or {}
        write_startup_exit_report("instance_lock_active", {"instance_lock": existing})
        print(
            f"{HELPER_SHORT_NAME} is already running or the instance lock is active: "
            f"{HELPER_INSTANCE_LOCK_FILE} {existing}",
            file=sys.stderr,
        )
        sys.exit(2)
    atexit.register(lambda: release_instance_lock(instance_lock_fd))
    helper_app: AgentHelperApp | None = None
    mainloop_error: BaseException | None = None
    try:
        helper_app = AgentHelperApp()
        helper_app.run()
    except BaseException as exc:
        mainloop_error = exc
        raise
    finally:
        if helper_app is not None:
            # Normal shutdown paths already wrote "stopped" (this is then a no-op). This covers a
            # mainloop that returned or raised without going through _request_shutdown, e.g. Ctrl+C.
            if mainloop_error is None:
                helper_app._write_final_stopped_status(
                    fallback_reason="Tk mainloop returned without a recorded shutdown.",
                    fallback_event="mainloop_returned",
                )
            else:
                helper_app._write_final_stopped_status(
                    fallback_reason=f"Tk mainloop ended by {type(mainloop_error).__name__}.",
                    fallback_event="mainloop_exception",
                )
        write_startup_exit_report("mainloop_returned", {"instance_lock_pid": os.getpid()})
