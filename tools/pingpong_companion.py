"""Small Windows overlay pair for an active Claude <-> Codex ping-pong session
(the owner's name for the two desktop apps messaging each other through
Crosstalk, ``tools/crosstalk/``; the overlay reads and writes Crosstalk's controls
through ``src/eutonos/crosstalk_status.py``).

Modeled on ``tools/codex_context_companion.py``: the same win32 ctypes window
tracking (GetWindowRect/IsIconic/DPI/monitor work rect/SetWindowPos), the same
polling-cadence shape, and the same light/dark palette and startup-registry
pattern.

Docking (owner request, 2026-09-27): each widget sits inside its host app's
composer bar, immediately to the LEFT of the model-picker label -- Claude
Desktop's "Model: Opus 5.5" button, or Codex's bare "GPT-6 Astra High" button
(a live UI Automation probe against both running apps found these exact
button names; see ``_LABEL_PROBE_SCRIPT`` below). A read-only, long-lived
PowerShell helper (``LabelProbe``) finds that button by name pattern via
BoundingRectangle/Name only -- no Invoke, no SetFocus, no value pattern, and
never against the CLI's own claude.exe. It queries at a fixed ~1.5s cadence
and only while ``LabelProbe.start()`` has been called, which ``App`` does
only while a session is visible (ping-pong active) and undoes on hide/quit --
never on an idle machine. Comtypes/uiautomation are not importable in the
project runtime (checked at authoring time), so this is plain ctypes-free
PowerShell + UI Automation, the "long-lived helper process" fallback the
task's own brief allows; a per-poll subprocess would be too heavy. If the
label can't be found (miss, unavailable UI Automation, a layout change), each
overlay keeps its last-known label rect for a short grace window, then falls
back to a fixed inset from the host window's bottom-right corner -- inside
the composer, never a crash, never a single-miss flash.

Two tiny widgets are shown, one docked to Claude Desktop's main window and
one to the Codex (ChatGPT desktop shell) main window. Each is stacked directly
above its own app window and never topmost, so a game or any window covering
the app covers the widget too; win-event hooks (the app's location changes,
and foreground changes) move and restack it within a frame so it stays
attached while the app is dragged, with the label kept as an offset from the
window's bottom-right corner between probe passes. Each shows a
small ping-pong animation (~34x18px at 100% scale, DPI-aware) in the
composer's own text colors; pointing at it turns it into a red stop button,
and clicking anywhere on it stops ping-pong. The stop and the right-click "Resume ping-pong" always run a *fresh*
``python -I -B crosstalk_p0_cli.py halt|resume`` subprocess (started by
``crosstalk_status.owner_control``, CREATE_NO_WINDOW), then re-read Crosstalk's
controls.json to prove the control changed. A widget left running since before a
code change would otherwise run a stale, pre-fix copy of the write logic loaded in
memory; a fresh subprocess always runs the code currently installed on disk. The
background ``ct.pingpong()`` poll (read-only, ~1/second, the journal tail and
controls) stays in-process via the ``ct`` import below -- it is cheap, read-only,
and polled far too often for a subprocess per call to be worth it; only the two
owner-invoked actions (stop, resume) pay the subprocess cost. The right-click menu also shows the
loaded ``eutonos`` package version (``importlib.metadata``), so a stale
in-process import would at least be visible there even though it no longer
affects the stop/resume path.

Usage: pythonw tools/pingpong_companion.py
       python tools/pingpong_companion.py --sample
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk


try:
    from eutonos import crosstalk_status as ct
except Exception:  # pragma: no cover - guarded per the task's own contract note
    ct = None


WINDOW_TITLE = "EUTONOS Ping-Pong"
STARTUP_NAME = "EUTONOS Ping-Pong Companion"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PREFERENCES_KEY = r"Software\EUTONOS\PingPongCompanion"
TRANSPARENT = "#ff00ff"

MOVING_INTERVAL_MS = 16      # Window geometry only, mirrors the template.
IDLE_INTERVAL_MS = 60
BACKGROUND_INTERVAL_MS = 250
ANIMATION_INTERVAL_MS = 33   # ~30 fps.
POLL_SECONDS = 1.0   # Background-thread cadence for ct.pingpong().
TICK_MS = 300        # How often the Tk thread checks the poller's box.
WINDOW_RESCAN_SECONDS = 2.0  # How often each overlay re-enumerates windows.
LABEL_MISS_GRACE_SECONDS = 3.0  # Keep the last-known label rect this long past a miss.
RESIZE_SETTLE_SECONDS = 0.18  # How long a host window's size must stay unchanged
                               # before a resize is considered finished (owner
                               # report, 2026-09-27: Codex's composer re-centers
                               # and its side panel opens/closes mid-drag).

COLORS = {
    "dark": {"bg": "#202124", "surface": "#2b2c2f", "text": "#f5f5f5", "muted": "#abaeb2",
             "accent": "#57b7b3", "border": "#414349", "stop": "#d1352b"},
    "light": {"bg": "#f8f8f7", "surface": "#ffffff", "text": "#242424", "muted": "#717171",
              "accent": "#17837d", "border": "#e8e8e7", "stop": "#c8291f"},
}

# Logical (100%-scale) sizes; ``Overlay._build`` multiplies by the host
# window's own DPI scale to get real canvas pixels, so the widget is a
# constant physical size across monitors instead of shrinking on a
# high-DPI display. Side-by-side layout (pong court, then the stop sign)
# keeps the whole widget's height to one short composer row (~22px at 100%),
# where the old stacked layout (pong above a stop button) needed roughly
# double that.
PONG_SIZE = (34, 18)     # One canvas; turns into a stop button on hover (owner's option D, 2026-09-27).
BALL_RADIUS = 2.2
PADDLE_WIDTH = 1.5
STOP_CORNER = 5.0        # Logical corner radius of the hover stop button.
BALL_MIN_X = 1.0 + PADDLE_WIDTH + BALL_RADIUS + 1.0
BALL_MAX_X = float(PONG_SIZE[0]) - BALL_MIN_X
BALL_SPEED = 46.0  # px/second, logical units.
PADDLE_HALF_HEIGHT = 4.0
WOBBLE_AMPLITUDE = 2.5

LABEL_GAP_PX = 8          # Logical gap between the widget's right edge and the label's left edge.
FALLBACK_INSET_X = 170    # Logical inset from the host window's right edge, no label found.
FALLBACK_INSET_Y = 40     # Logical inset from the host window's bottom edge, no label found.

HALT_TIMEOUT_SECONDS = 20.0
RESUME_TIMEOUT_SECONDS = 20.0
INSTALL_CHECK_SECONDS = 30.0  # How often the running widget checks whether
                              # the installed eutonos package changed since it
                              # started (see ``install_signature``/``App.restart_self``).

TOOLTIP_STOPPED = ("Ping-pong stopped. Letters cancelled; the apps finish their current step "
                    "— use the app's own stop to cut a turn short.")
TOOLTIP_ACTIVE = "Stop ping-pong"

if sys.platform == "win32":
    USER32 = ctypes.WinDLL("user32", use_last_error=True)
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    USER32.GetForegroundWindow.restype = wintypes.HWND
    USER32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    USER32.GetWindowRect.restype = wintypes.BOOL
    USER32.IsIconic.argtypes = (wintypes.HWND,)
    USER32.IsIconic.restype = wintypes.BOOL
    USER32.IsWindow.argtypes = (wintypes.HWND,)
    USER32.IsWindow.restype = wintypes.BOOL
    USER32.IsWindowVisible.argtypes = (wintypes.HWND,)
    USER32.IsWindowVisible.restype = wintypes.BOOL
    USER32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    USER32.SetWindowPos.restype = wintypes.BOOL
    USER32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    USER32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    USER32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    USER32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    USER32.GetDpiForWindow.argtypes = (wintypes.HWND,)
    USER32.GetDpiForWindow.restype = wintypes.UINT
    USER32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    USER32.MonitorFromWindow.restype = wintypes.HANDLE
    USER32.EnumWindows.argtypes = (
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM,
    )
    USER32.EnumWindows.restype = wintypes.BOOL
    USER32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    USER32.GetWindowTextLengthW.restype = ctypes.c_int
    USER32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    USER32.GetWindowTextW.restype = ctypes.c_int
    USER32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    USER32.GetClassNameW.restype = ctypes.c_int
    USER32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
    USER32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    USER32.GetWindow.restype = wintypes.HWND
    WinEventProc = ctypes.WINFUNCTYPE(
        None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
        wintypes.LONG, wintypes.DWORD, wintypes.DWORD,
    )
    USER32.SetWinEventHook.argtypes = (
        wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WinEventProc,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    )
    USER32.SetWinEventHook.restype = wintypes.HANDLE
    USER32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    USER32.UnhookWinEvent.restype = wintypes.BOOL

    KERNEL32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    KERNEL32.OpenProcess.restype = wintypes.HANDLE
    KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    KERNEL32.CloseHandle.restype = wintypes.BOOL
    KERNEL32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    USER32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
    USER32.GetMonitorInfoW.restype = wintypes.BOOL

    HWND_TOP = wintypes.HWND(0)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    GW_HWNDPREV = 3
    WS_EX_TOPMOST = 0x00000008
    EVENT_SYSTEM_FOREGROUND = 0x0003
    EVENT_OBJECT_LOCATIONCHANGE = 0x800B
    WINEVENT_OUTOFCONTEXT = 0x0000
    WINEVENT_SKIPOWNPROCESS = 0x0002
    OBJID_WINDOW = 0
    SWP_NOACTIVATE = 0x0010
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    GW_OWNER = 4
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# -------------------------------------------------------------- crosstalk glue

def default_root() -> Path:
    """Repo root resolved from this script's location, like
    a script under tools/ resolves the repo root from its own location."""
    return Path(__file__).resolve().parents[1]


def call_pingpong(root) -> dict | None:
    """Best-effort, read-only. Returns None (never raises) until the API lands
    or on any transient failure -- the caller treats None as "show nothing".
    Deliberately still in-process (see the module docstring): a background
    poll roughly once a second, where a subprocess per call would be wasteful
    and where "briefly stale" is harmless (only the owner-invoked halt/resume
    actions below need a guaranteed-fresh process)."""
    fn = getattr(ct, "pingpong", None) if ct is not None else None
    if fn is None:
        return None
    try:
        return fn(root)
    except Exception:
        return None


def _console_python() -> Path:
    """The console interpreter beside whichever one is currently running, so
    a stop/resume subprocess's stdout can be captured (pythonw has none)."""
    interpreter = Path(sys.executable)
    name = interpreter.name.lower()
    if name == "pythonw.exe":
        return interpreter.with_name("python.exe")
    if name == "pythonw_d.exe":
        return interpreter.with_name("python_d.exe")
    return interpreter


def _run_crosstalk_control(root, command: str) -> dict:
    """Run Crosstalk's own CLI ``halt``/``resume`` as a brand-new subprocess (see the
    module docstring's "fresh process" note; the CLI is chosen and run by
    ``crosstalk_status.owner_control``, which re-reads controls.json to prove the
    control changed) and map the outcome to ``{"ok": bool, ...}``:

    - Crosstalk's status module or CLI is missing, or the process could not start:
      ``{"ok": False, "unavailable": True, "message": ...}``.
    - it ran past the timeout: ``{"ok": False, "timeout": True}``.
    - the CLI ran but the control did not change (or any other refusal):
      ``{"ok": False, "exception": <code>}``.
    - success: ``{"ok": True, "result": <the controls read back>}``.

    Never raises."""
    owner_control = getattr(ct, "owner_control", None) if ct is not None else None
    if owner_control is None:
        return {"ok": False, "unavailable": True, "message": "crosstalk module unavailable"}
    try:
        outcome = owner_control(command, Path(root))
    except Exception as exc:
        return {"ok": False, "exception": type(exc).__name__}
    if not isinstance(outcome, dict):
        return {"ok": False, "exception": "bad-result"}
    if outcome.get("ok"):
        return {"ok": True, "result": outcome.get("controls") or {}}
    error = outcome.get("error") or "crosstalk-error"
    if error == "crosstalk-timeout":
        return {"ok": False, "timeout": True}
    if error in ("crosstalk-cli-missing", "crosstalk-launch-failed"):
        return {"ok": False, "unavailable": True, "message": outcome.get("message") or error}
    return {"ok": False, "exception": error}


def call_halt(root) -> dict:
    """Owner-invoked, never raises -- but unlike ``call_pingpong`` (a background
    poll where "nothing yet" and "it failed" are equally uninteresting) a halt
    is a deliberate stop-button click, so failure must be visible rather than
    collapsed into the same shape as "not called yet" (J11). Runs a fresh
    ``crosstalk_p0_cli.py halt`` every click and reads controls.json back; the
    result always has an ``"ok"`` key. """
    return _run_crosstalk_control(root, "halt")


def summarize_halt_result(summary: dict | None) -> dict:
    """Turn a ``call_halt`` result into ``{"ok": bool, "message": str}`` for the
    overlay. Pure and total: any shape (including ``None``, from a caller that
    hasn't been updated, or a bare exception object slipping through a mock in
    a test) maps to a sensible message instead of raising. Shared with
    ``call_resume``'s outcome via ``summarize_resume_result``, which has its
    own wording but the same ``timeout``/``unavailable``/``exception`` shape."""
    if not isinstance(summary, dict):
        return {"ok": False, "message": "Stop failed: no response from Crosstalk."}
    if summary.get("ok"):
        return {"ok": True, "message": ""}
    if summary.get("timeout"):
        return {"ok": False, "message": f"Stop failed: timed out after {int(HALT_TIMEOUT_SECONDS)}s."}
    if summary.get("unavailable"):
        return {"ok": False, "message": "Stop failed: Crosstalk unavailable."}
    if "exception" in summary:
        return {"ok": False, "message": f"Stop failed: {summary['exception']} while halting."}
    return {"ok": False, "message": "Stop failed: halt did not confirm success."}


def call_resume(root) -> dict:
    """Owner-invoked counterpart of ``call_halt`` (J11 applies here too): runs a
    fresh ``crosstalk_p0_cli.py resume`` every click and reads controls.json back."""
    return _run_crosstalk_control(root, "resume")


def summarize_resume_result(summary: dict | None) -> dict:
    """The ``summarize_halt_result`` counterpart for ``call_resume``."""
    if not isinstance(summary, dict):
        return {"ok": False, "message": "Resume failed: no response from Crosstalk."}
    if summary.get("ok"):
        return {"ok": True, "message": ""}
    if summary.get("timeout"):
        return {"ok": False, "message": f"Resume failed: timed out after {int(RESUME_TIMEOUT_SECONDS)}s."}
    if summary.get("unavailable"):
        return {"ok": False, "message": "Resume failed: Crosstalk unavailable."}
    if "exception" in summary:
        return {"ok": False, "message": f"Resume failed: {summary['exception']} while resuming."}
    return {"ok": False, "message": "Resume failed: did not confirm success."}


def eutonos_build_version() -> str:
    """Cheap, best-effort -- shown in the right-click menu so a stale loaded
    build is visible even though it no longer affects the stop/resume path
    (that always runs a fresh subprocess; see the module docstring)."""
    try:
        import importlib.metadata as metadata
        return metadata.version("eutonos")
    except Exception:
        return "unknown"


def install_signature() -> tuple[str, float | None]:
    """Cheap, best-effort fingerprint of "what code is installed right now":
    the ``eutonos`` package version plus the ``crosstalk_status`` module's own mtime.
    The version alone can miss an editable/dev install where the version
    string does not change between an old and a new copy of the code; the
    mtime catches that case too. Compared against the same call made when
    this widget started (see ``App.__init__``/``App._tick_session``) so a
    long-lived widget -- unlike the halt/resume subprocess, which already
    always runs fresh code (see the module docstring) -- can tell that an
    install replaced the code it has had loaded since startup, and restart
    itself onto the new copy (``App.restart_self``)."""
    version = eutonos_build_version()
    mtime: float | None = None
    try:
        module_file = getattr(ct, "__file__", None) if ct is not None else None
        if module_file:
            mtime = Path(module_file).resolve().stat().st_mtime
    except OSError:
        mtime = None
    return (version, mtime)


def relaunch_process() -> bool:
    """Spawn a brand-new, detached process running this same script with the
    same argv -- used by ``App.restart_self`` when ``install_signature()``
    shows the installed package changed since this widget started. Detached
    (its own process group, no console) so it outlives this process exiting
    and does not depend on whatever launched us (a startup-registry run has
    no parent process to keep it alive). Never raises: returns False if the
    relaunch itself could not even be started, so the caller can choose to
    keep running on its old, possibly-stale code rather than exit with
    nothing left in its place."""
    try:
        subprocess.Popen(
            [sys.executable, *sys.argv], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    except OSError:
        return False
    return True


class PingpongPoller:
    """Polls ct.pingpong(root) on a background thread, roughly once a second.
    Never called from the Tk thread -- ``read()`` just returns the last value."""

    def __init__(self, root_path, interval: float = POLL_SECONDS):
        self.root_path = root_path
        self.interval = interval
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            snapshot = call_pingpong(self.root_path)
            with self._lock:
                self._latest = snapshot
            self._stop.wait(self.interval)

    def read(self) -> dict | None:
        with self._lock:
            return self._latest

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------- pure geometry

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    exe_path: str
    visible: bool
    tool_window: bool
    owned: bool
    rect: tuple[int, int, int, int]


def is_claude_desktop_path(exe_path: str) -> bool:
    """The MSIX Claude Desktop package -- never the Claude Code CLI's claude.exe,
    which has no window to find in the first place."""
    if not exe_path:
        return False
    lower = exe_path.lower()
    if not lower.endswith("claude.exe"):
        return False
    return "windowsapps\\claude" in lower or "anthropicclaude" in lower


def is_codex_app_path(exe_path: str) -> bool:
    """The Codex desktop app's process, per tools/codex_context_probe.ps1's own
    ``Get-Process -Name ChatGPT`` -- the app is built on the ChatGPT desktop shell."""
    if not exe_path:
        return False
    name = exe_path.rsplit("\\", 1)[-1].lower()
    return name in ("chatgpt.exe", "codex.exe")


def _area(window: WindowInfo) -> int:
    left, top, right, bottom = window.rect
    return max(0, right - left) * max(0, bottom - top)


def select_window(windows, predicate) -> WindowInfo | None:
    """Pick the largest visible, non-tool, unowned top-level window matching
    ``predicate(exe_path)``. Windows with no title are treated as helper
    windows, never a main chat window. Ties (or several windows of one app)
    resolve to the largest by area; other matches are ignored -- with two
    Claude Desktop windows open, only the bigger one gets a widget."""
    candidates = [
        w for w in windows
        if w.visible and not w.tool_window and not w.owned and w.title
        and predicate(w.exe_path)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda w: (_area(w), w.hwnd), reverse=True)
    return candidates[0]


def compute_label_dock_position(label_rect, size, work_rect, dpi_scale: float = 1.0):
    """Dock ``size`` (already DPI-scaled device pixels) just left of
    ``label_rect`` -- the host app's model-picker button, e.g. Claude
    Desktop's "Model: Opus 5.5" or Codex's "GPT-6 Astra High" (both are live
    UI Automation ``Button`` elements; see ``_LABEL_PROBE_SCRIPT``).
    Vertically centered on the label with a small DPI-scaled gap to its
    left; clamped to ``work_rect`` so it can never land off-screen. A very
    narrow host window can still push the widget hard against (or slightly
    over) the label rather than off the monitor -- covering a few pixels of
    the label is preferred to disappearing."""
    label_left, label_top, _label_right, label_bottom = label_rect
    width, height = size
    gap = LABEL_GAP_PX * dpi_scale
    x = label_left - gap - width
    label_mid_y = (label_top + label_bottom) / 2
    y = label_mid_y - height / 2
    work_left, work_top, work_right, work_bottom = work_rect
    x = min(max(work_left, x), max(work_left, work_right - width))
    y = min(max(work_top, y), max(work_top, work_bottom - height))
    return round(x), round(y)


def label_offset(label: dict, host_rect) -> tuple[int, int, int, int]:
    """One probe line's label rect as an offset from the host window's
    bottom-right corner, using the corner the probe sampled in the same pass
    (``wright``/``wbottom``) so a line taken mid-drag is still exact. Older
    lines without the corner fall back to the current host rect."""
    right = label.get("wright", host_rect[2])
    bottom = label.get("wbottom", host_rect[3])
    return (label["left"] - right, label["top"] - bottom,
            label["right"] - right, label["bottom"] - bottom)


def apply_label_offset(offset, host_rect) -> tuple[int, int, int, int]:
    """Inverse of ``label_offset`` against the host's current rect: lets the
    overlay follow a drag every frame without waiting for the next probe."""
    right, bottom = host_rect[2], host_rect[3]
    return (right + offset[0], bottom + offset[1], right + offset[2], bottom + offset[3])


def rect_size(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """(width, height) of a window rect -- used to tell a pure move (the size
    stays the same) from a resize. Codex's composer is responsive: it stays
    centered with a max width and its side panel opens/closes, so the model
    label's offset from any host corner is only valid for the size it was
    measured at (see ``Overlay._handle_resize``)."""
    left, top, right, bottom = rect
    return (right - left, bottom - top)


def compute_fallback_position(host_rect, size, work_rect):
    """No label found (UI Automation unavailable, a layout change, or a miss
    still inside ``LABEL_MISS_GRACE_SECONDS``): a fixed inset from the host
    window's own bottom-right corner, landing inside its composer row rather
    than floating off the window like the previous top-right-corner scheme.
    Clamped first to the host window itself (so a small window never pushes
    the widget outside it) and then to the monitor work area."""
    left, top, right, bottom = host_rect
    width, height = size
    x = right - FALLBACK_INSET_X - width
    y = bottom - FALLBACK_INSET_Y - height
    x = min(max(left, x), max(left, right - width))
    y = min(max(top, y), max(top, bottom - height))
    work_left, work_top, work_right, work_bottom = work_rect
    x = min(max(work_left, x), max(work_left, work_right - width))
    y = min(max(work_top, y), max(work_top, work_bottom - height))
    return round(x), round(y)


def rounded_rect_points(x0: float, y0: float, x1: float, y1: float,
                        radius: float) -> list[float]:
    """Flat coordinate list for a Tk ``create_polygon(..., smooth=True)``
    rounded rectangle: each corner is a doubled vertex so the spline hugs the
    straight edges and only rounds the corners."""
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


@dataclass(frozen=True)
class AnimationState:
    ball_x: float
    direction: int
    t: float = 0.0


def animate_step(state: AnimationState, dt: float) -> AnimationState:
    """Advance the ball by one frame. Pure and deterministic given ``dt``, so
    it can be driven at any real frame rate and tested without a clock."""
    dt = max(0.0, dt)
    ball_x = state.ball_x + state.direction * BALL_SPEED * dt
    direction = state.direction
    if ball_x >= BALL_MAX_X:
        ball_x = BALL_MAX_X - (ball_x - BALL_MAX_X)
        direction = -1
    elif ball_x <= BALL_MIN_X:
        ball_x = BALL_MIN_X + (BALL_MIN_X - ball_x)
        direction = 1
    ball_x = min(BALL_MAX_X, max(BALL_MIN_X, ball_x))
    return AnimationState(ball_x=ball_x, direction=direction, t=state.t + dt)


class SessionState:
    """Maps raw ``ct.pingpong()`` snapshots to widget visibility.

    A session is "live" while ``active`` or ``stopped`` is true (a halted
    session stays visible with a resume affordance). "Hide until next
    session" hides the widgets for the rest of the current session and
    clears itself automatically the next time a brand-new session goes live
    (a not-live -> live edge), so the choice can never silently swallow a
    later session."""

    def __init__(self):
        self.hidden_until_next_session = False
        self._was_live = False

    def apply(self, snapshot: dict) -> bool:
        live = bool(snapshot.get("active")) or bool(snapshot.get("stopped"))
        if live and not self._was_live and self.hidden_until_next_session:
            self.hidden_until_next_session = False
        self._was_live = live
        if self.hidden_until_next_session:
            return False
        return live

    def hide_until_next_session(self):
        self.hidden_until_next_session = True


# ------------------------------------------------------------ win32 helpers

def process_image_path(pid: int) -> str:
    if not pid:
        return ""
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if KERNEL32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        KERNEL32.CloseHandle(handle)


def enum_windows() -> list[WindowInfo]:
    """Read-only EnumWindows sweep: title, class, owning process's image path,
    visibility, tool-window/owned flags, and raw (possibly off-screen, if the
    window is minimized) rect. No accessibility read, no focus change."""
    results: list[WindowInfo] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        length = USER32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            USER32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        class_buf = ctypes.create_unicode_buffer(256)
        USER32.GetClassNameW(hwnd, class_buf, 256)
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exstyle = USER32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        rect = wintypes.RECT()
        if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
            rect_tuple = (0, 0, 0, 0)
        else:
            rect_tuple = (rect.left, rect.top, rect.right, rect.bottom)
        results.append(WindowInfo(
            hwnd=int(hwnd), pid=int(pid.value), title=title, class_name=class_buf.value,
            exe_path=process_image_path(pid.value), visible=bool(USER32.IsWindowVisible(hwnd)),
            tool_window=bool(exstyle & WS_EX_TOOLWINDOW),
            owned=bool(USER32.GetWindow(hwnd, GW_OWNER)), rect=rect_tuple,
        ))
        return True

    USER32.EnumWindows(EnumWindowsProc(callback), 0)
    return results


def visible_target_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Like codex_context_companion's window_rect: None if the window is gone
    or minimized, so callers can hide the widget outright."""
    if not hwnd or not USER32.IsWindow(hwnd) or USER32.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def monitor_work_rect(hwnd: int) -> tuple[int, int, int, int]:
    monitor = USER32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if monitor and USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1)


def _make_noactivate_toolwindow(hwnd: int) -> None:
    """Clicks on our own overlay must never activate it (WS_EX_NOACTIVATE) or
    show up in the taskbar/Alt+Tab (WS_EX_TOOLWINDOW) -- the hard owner rule
    that a click must not steal focus from, or change the selected chat in,
    Claude or Codex."""
    exstyle = USER32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    USER32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def _frame_hwnd(widget) -> int:
    """The real top-level HWND of a Tk toplevel. ``winfo_id()`` is Tk's inner
    child window, so styles and z-order set on it have no effect on what the
    desktop shows (the 2026-09-27 widget sat under the apps because of this)."""
    try:
        return int(widget.wm_frame(), 16)
    except (tk.TclError, ValueError):
        return widget.winfo_id()


PAD_ALPHA = 0.01  # Click pad opacity: ~2/255, invisible yet hit-testable.


def stack_insert_after(host: int, own: tuple[int, ...] = ()):
    """Insert-after handle that puts a window directly above ``host`` and
    never above anything else: after whatever window is right above the host
    (skipping the widget's own windows), or at the top of the normal
    (non-topmost) band when the host is frontmost."""
    above = USER32.GetWindow(host, GW_HWNDPREV)
    while above and above in own:
        above = USER32.GetWindow(above, GW_HWNDPREV)
    if not above or USER32.GetWindowLongPtrW(above, GWL_EXSTYLE) & WS_EX_TOPMOST:
        return HWND_TOP
    return above


def is_stacked(host: int, frame: int, pad: int) -> bool:
    """True when the z-order is host < drawing < click pad, adjacent."""
    return (USER32.GetWindow(host, GW_HWNDPREV) == frame
            and USER32.GetWindow(frame, GW_HWNDPREV) == pad)


def stack_pair(host: int, frame: int, pad: int, x: int = 0, y: int = 0,
               move: bool = False) -> None:
    """Keep the widget attached in z-order: the click pad directly above the
    app window and the drawing directly under the pad, so any game or window
    covering the app covers the widget too. Optionally moves both."""
    flags = SWP_NOACTIVATE | SWP_NOSIZE | (0 if move else SWP_NOMOVE)
    if is_stacked(host, frame, pad):
        if move:
            USER32.SetWindowPos(pad, HWND_TOP, x, y, 0, 0, flags | SWP_NOZORDER)
            USER32.SetWindowPos(frame, HWND_TOP, x, y, 0, 0, flags | SWP_NOZORDER)
        return
    USER32.SetWindowPos(pad, stack_insert_after(host, (frame, pad)), x, y, 0, 0, flags)
    USER32.SetWindowPos(frame, pad, x, y, 0, 0, flags)


def startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            winreg.QueryValueEx(key, STARTUP_NAME)
        return True
    except FileNotFoundError:
        return False


def set_startup(enabled: bool) -> None:
    if sys.platform != "win32":
        raise OSError("Windows startup is unavailable")
    import winreg
    if enabled:
        interpreter = Path(sys.executable)
        if interpreter.name.lower() == "python.exe":
            candidate = interpreter.with_name("pythonw.exe")
            if candidate.exists():
                interpreter = candidate
        command = subprocess.list2cmdline([str(interpreter), str(Path(__file__).resolve())])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, command)
    else:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, STARTUP_NAME)


def system_theme() -> str:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except OSError:
            pass
    return "light"


TARGETS = {
    "claude": {"label": "Claude Desktop", "predicate": is_claude_desktop_path},
    "codex": {"label": "Codex", "predicate": is_codex_app_path},
}


# ------------------------------------------------------------- label probe

# Read-only UI Automation helper (see the module docstring): loops forever,
# on each ~0.5s cycle (a pass measured 50-90ms) finding each app's own visible main window by process
# name (like tools/codex_context_probe.ps1's own ``Get-Process -Name
# ChatGPT`` -- Claude Desktop's process is named "Claude") and, within it,
# the first Button whose Name matches the model-label pattern found live
# against both running apps (Claude Desktop: "Model: Opus 5.5"; Codex: bare
# "GPT-6 Astra High"), plus the window's own bottom-right corner sampled in
# the same pass so the label can be kept as an offset from that corner. Only
# BoundingRectangle and Name are read -- no
# Invoke, no SetFocus, no value pattern. One JSON line per app per cycle;
# ``LabelProbe`` below only starts this process while a session is visible.
_LABEL_PROBE_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient
$namePattern = [regex]::new('^(Model:\s*)?(Opus|Sonnet|Haiku|Fable|GPT-\d+)\b')
$buttonCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button)
$targets = @(
    @{ key = 'claude'; proc = 'Claude' },
    @{ key = 'codex'; proc = 'ChatGPT' }
)
function Find-Label([string]$procName) {
    $proc = Get-Process -Name $procName -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($null -eq $proc) { return $null }
    try {
        $win = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
        $buttons = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonCond)
        foreach ($el in $buttons) {
            $name = $el.Current.Name
            if ($name -and $namePattern.IsMatch($name)) {
                $r = $el.Current.BoundingRectangle
                if ($r.IsEmpty -or [double]::IsInfinity($r.Left)) { continue }
                $w = $win.Current.BoundingRectangle
                return [pscustomobject]@{
                    hwnd = [int64]$proc.MainWindowHandle
                    wright = [math]::Round($w.Right); wbottom = [math]::Round($w.Bottom)
                    left = [math]::Round($r.Left); top = [math]::Round($r.Top)
                    right = [math]::Round($r.Right); bottom = [math]::Round($r.Bottom)
                }
            }
        }
    } catch { }
    return $null
}
while ($true) {
    foreach ($target in $targets) {
        $label = Find-Label $target.proc
        if ($null -eq $label) {
            Write-Output ('{"key":"' + $target.key + '","found":false}')
        } else {
            $json = '{{"key":"{0}","found":true,"hwnd":{1},"left":{2},"top":{3},"right":{4},"bottom":{5},"wright":{6},"wbottom":{7}}}' -f `
                $target.key, $label.hwnd, $label.left, $label.top, $label.right, $label.bottom, $label.wright, $label.wbottom
            Write-Output $json
        }
    }
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds 500
}
"""


class LabelProbe:
    """Owns the single long-lived PowerShell process behind ``_LABEL_PROBE_SCRIPT``.

    Modeled on ``codex_context_companion.Probe``: one persistent subprocess, a
    background reader thread, latest-result-per-key wins. ``start()``/``stop()``
    are cheap and idempotent; ``App`` calls them from the session
    visibility edge, so the helper (and its UI Automation queries) only exists
    while at least one overlay is visible -- never for an idle session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._results: dict[str, dict] = {}
        self._generation: dict[str, int] = {}
        self.process: subprocess.Popen | None = None

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        try:
            self.process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-Command", _LABEL_PROBE_SCRIPT],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError:
            self.process = None
            return
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        assert process.stdout
        for line in process.stdout:
            if process is not self.process:
                return
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            key = value.get("key") if isinstance(value, dict) else None
            if key:
                with self._lock:
                    self._results[key] = value
                    self._generation[key] = self._generation.get(key, 0) + 1

    def read(self, key: str) -> dict | None:
        with self._lock:
            return self._results.get(key)

    def generation(self, key: str) -> int:
        """Monotonically increasing count of probe lines received for
        ``key``, bumped once per line regardless of whether its content
        changed. Lets a caller detect "a fresh probe pass has landed since I
        last checked" (``Overlay._handle_resize`` uses this to wait for an
        answer that postdates a resize settling, instead of consuming
        whatever was already cached from before it) without needing the
        PowerShell side to add its own timestamp."""
        with self._lock:
            return self._generation.get(key, 0)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
        self.process = None
        with self._lock:
            self._results.clear()
            self._generation.clear()


# -------------------------------------------------------------------- sample

def build_sample(root_path: Path) -> dict:
    windows = enum_windows() if sys.platform == "win32" else []
    found = {}
    for key, cfg in TARGETS.items():
        window = select_window(windows, cfg["predicate"])
        found[key] = None if window is None else {
            "hwnd": window.hwnd, "pid": window.pid, "title": window.title,
            "class_name": window.class_name, "exe_path": window.exe_path,
            "rect": list(window.rect), "visible": window.visible,
            "tool_window": window.tool_window, "owned": window.owned,
        }
    return {"pingpong": call_pingpong(root_path), "windows": found}


# ----------------------------------------------------------------- GUI shell

class Overlay:
    """One tiny widget docked to one target window: placed left of its model
    label and stacked directly above that window, never topmost, so whatever
    covers the app covers the widget too."""

    def __init__(self, app: "App", key: str, config: dict):
        self.app = app
        self.key = key
        self.label = config["label"]
        self.predicate = config["predicate"]
        self.window: WindowInfo | None = None
        self.last_scan = float("-inf")
        self.last_rect = None
        self.last_motion = 0.0
        self.frame = 0
        self._last_pos: tuple[int, int] | None = None
        self._hook = None
        self._hook_pid = 0
        self._hook_proc = None
        self.visible_now = False
        self.stopped_ui = False
        self.failed_ui = False
        self._failure_hide_job = None
        self._last_failure_message = ""
        self.tooltip = None
        self.anim = AnimationState(ball_x=BALL_MIN_X, direction=1, t=0.0)
        self.hovered = False
        self.last_anim_time = time.monotonic()
        self.dpi_scale = 1.0
        self.label_offset: tuple[int, int, int, int] | None = None
        self.last_label_time = float("-inf")
        # Resize handling (owner report, 2026-09-27): Codex's composer is
        # responsive (centered with a max width, side panel opens/closes), so
        # the label's offset from a host corner is only valid for one size --
        # see ``_handle_resize`` below.
        self._last_rect_size: tuple[int, int] | None = None
        self._resize_pending = False
        self._resize_settled = False
        self._resize_last_change = 0.0
        self._resize_probe_baseline = 0
        self._suppress_until_label_found = False

        self.top = tk.Toplevel(app.tk_root)
        self.top.title(f"{WINDOW_TITLE} — {self.label}")
        self.top.overrideredirect(True)
        self.top.withdraw()
        self.top.attributes("-transparentcolor", TRANSPARENT)
        self.top.resizable(False, False)
        self.top.configure(bg=TRANSPARENT)
        # Color-keyed pixels are click-through, so the mostly see-through
        # drawing would only catch a click on a paddle or the ball. A nearly
        # transparent pad over it takes hover and clicks for the whole area.
        self.pad = tk.Toplevel(app.tk_root)
        self.pad.title(f"{WINDOW_TITLE} pad — {self.label}")
        self.pad.overrideredirect(True)
        self.pad.withdraw()
        self.pad.attributes("-alpha", PAD_ALPHA)
        self.pad.configure(bg="#000000", cursor="hand2")
        self.pad.bind("<Button-1>", self._on_stop_click)
        self.pad.bind("<Enter>", self._on_enter)
        self.pad.bind("<Leave>", self._on_leave)
        self.pad.bind("<Button-3>", self._on_menu)
        self.pad_frame = 0
        if sys.platform == "win32":
            # Before _build: it sizes the canvas from dpi_scale, and the tick
            # only rebuilds when the scale changes.
            self.dpi_scale = max(1.0, USER32.GetDpiForWindow(self.top.winfo_id()) / 96)
        self._build()
        self.top.protocol("WM_DELETE_WINDOW", lambda: None)
        self.top.after(MOVING_INTERVAL_MS, self._tick)
        self.top.after(ANIMATION_INTERVAL_MS, self._animate)

    # -- construction -----------------------------------------------------

    def _build(self):
        for child in self.top.winfo_children():
            child.destroy()
        scale = self.dpi_scale
        pong_w = max(1, round(PONG_SIZE[0] * scale))
        pong_h = max(1, round(PONG_SIZE[1] * scale))
        self._pong_px = (pong_w, pong_h)
        self.pong = tk.Canvas(self.top, width=pong_w, height=pong_h, bg=TRANSPARENT,
                              highlightthickness=0, bd=0, cursor="hand2")
        self.pong.pack()
        # The whole widget is the stop button; pointing at it turns the
        # paddles into a red stop button.
        self.pong.bind("<Button-1>", self._on_stop_click)
        self.pong.bind("<Enter>", self._on_enter)
        self.pong.bind("<Leave>", self._on_leave)
        for widget in (self.pong, self.top):
            widget.bind("<Button-3>", self._on_menu)
        self.top.update_idletasks()
        self.size = (self.top.winfo_reqwidth(), self.top.winfo_reqheight())
        self.top.geometry(f"{self.size[0]}x{self.size[1]}")
        self.pad.geometry(f"{self.size[0]}x{self.size[1]}")
        self._render_pong()

    def refresh_theme(self):
        self._render_pong()

    # -- rendering ----------------------------------------------------------

    def _on_enter(self, event=None):
        self.hovered = True
        self._render_pong()
        self._show_tooltip(event)

    def _on_leave(self, event=None):
        self.hovered = False
        self._render_pong()
        self._hide_tooltip(event)

    def _render_pong(self):
        """Option D (owner, 2026-09-27): calm paddles and a small ball in the
        composer's own text colors while agents talk; pointing at it turns
        the whole widget into a red rounded stop button with a white square.
        Stopped rests the ball in the middle in muted gray; a failed halt
        keeps the red button with a white "!" drawn as bars (text would
        fringe against the transparent color key)."""
        p = self.app.palette
        c = self.pong
        c.delete("all")
        scale = self.dpi_scale
        width, height = self._pong_px
        mid_y = height / 2
        if self.failed_ui or (getattr(self, "hovered", False) and not self.stopped_ui):
            inset = 3 * scale
            button = rounded_rect_points(inset, 0, width - inset, height, STOP_CORNER * scale)
            square = 4 * scale
            c.create_polygon(*button, smooth=True, fill=p["stop"], outline="")
            if self.failed_ui:
                bar = 1.2 * scale
                c.create_rectangle(width / 2 - bar, mid_y - 5 * scale, width / 2 + bar,
                                   mid_y + 1.5 * scale, fill="#ffffff", outline="")
                c.create_rectangle(width / 2 - bar, mid_y + 3 * scale, width / 2 + bar,
                                   mid_y + 5 * scale, fill="#ffffff", outline="")
            else:
                c.create_rectangle(width / 2 - square, mid_y - square, width / 2 + square,
                                   mid_y + square, fill="#ffffff", outline="")
            return
        radius = BALL_RADIUS * scale
        if self.stopped_ui:
            ball_x, ball_y = width / 2, mid_y
        else:
            ball_x = self.anim.ball_x * scale
            ball_y = mid_y - WOBBLE_AMPLITUDE * scale * math.sin(self.anim.t * 2.0)
            ball_y = min(height - radius - scale, max(radius + scale, ball_y))
        paddle_color = p["muted"]
        ball_color = p["muted"] if self.stopped_ui else p["text"]
        paddle_half = PADDLE_HALF_HEIGHT * scale
        paddle_w = PADDLE_WIDTH * scale
        c.create_rectangle(scale, mid_y - paddle_half, scale + paddle_w, mid_y + paddle_half,
                           fill=paddle_color, outline="")
        c.create_rectangle(width - scale - paddle_w, mid_y - paddle_half, width - scale,
                           mid_y + paddle_half, fill=paddle_color, outline="")
        c.create_oval(ball_x - radius, ball_y - radius, ball_x + radius, ball_y + radius,
                      fill=ball_color, outline="")

    def _render_stop_button(self):
        """The stop look lives in ``_render_pong`` now; kept so state changes
        that re-render it still have one name to call."""
        self._render_pong()

    # -- interaction ----------------------------------------------------

    def _on_stop_click(self, _event=None):
        if self.stopped_ui:
            return
        self.app.request_halt()

    def _on_menu(self, event):
        menu = tk.Menu(self.top, tearoff=0)
        if self.app.stopped:
            menu.add_command(label="Resume ping-pong", command=self.app.request_resume)
        menu.add_command(label=f"Build: {self.app.build_version}", state="disabled")
        menu.add_command(label="Hide until next session", command=self.app.hide_until_next_session)
        menu.add_checkbutton(label="Start with Windows", variable=self.app.startup_var,
                             command=self.app.toggle_startup)
        menu.add_separator()
        menu.add_command(label="Quit ping-pong companion", command=self.app.quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_tooltip(self, _event=None):
        self._hide_tooltip()
        if self.stopped_ui:
            text = TOOLTIP_STOPPED
        elif self.failed_ui:
            text = self._last_failure_message or "Stop failed."
        else:
            text = TOOLTIP_ACTIVE
        p = self.app.palette
        tip = tk.Toplevel(self.top)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg=p["surface"], fg=p["text"], font=("Segoe UI", 8),
                wraplength=200, justify="left", padx=6, pady=4).pack()
        x = self.top.winfo_rootx()
        y = self.top.winfo_rooty() + self.size[1] + 4
        tip.geometry(f"+{x}+{y}")
        if sys.platform == "win32":
            tip.update_idletasks()
            _make_noactivate_toolwindow(_frame_hwnd(tip))
        self.tooltip = tip

    def _hide_tooltip(self, _event=None):
        self._cancel_failure_hide()
        if self.tooltip is not None:
            self.tooltip.destroy()
            self.tooltip = None

    def destroy_tooltip(self):
        self._hide_tooltip()
        self._cancel_failure_hide()

    def show_halt_result(self, summary: dict):
        """Reflect a ``summarize_halt_result``/``summarize_resume_result``
        outcome (J11, and now shared between the stop button and the
        right-click Resume item). Success clears any previous failure look;
        failure sets the "!" ring (see ``_render_stop_button``) and pops an
        auto-hiding tooltip with the reason -- shown immediately, not only on
        hover, since the owner may not be pointing at the button when their
        click's result comes back."""
        if summary.get("ok"):
            self.clear_failed()
            return
        self.failed_ui = True
        self._last_failure_message = summary.get("message") or "Stop failed."
        self._render_stop_button()
        self._show_failure_tooltip(self._last_failure_message)

    def clear_failed(self):
        if self.failed_ui:
            self.failed_ui = False
            self._render_stop_button()
        self._cancel_failure_hide()

    def _show_failure_tooltip(self, text: str):
        self._hide_tooltip()
        p = self.app.palette
        tip = tk.Toplevel(self.top)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, bg=p["surface"], fg=p["stop"], font=("Segoe UI", 8, "bold"),
                wraplength=200, justify="left", padx=6, pady=4).pack()
        x = self.top.winfo_rootx()
        y = self.top.winfo_rooty() + self.size[1] + 4
        tip.geometry(f"+{x}+{y}")
        if sys.platform == "win32":
            tip.update_idletasks()
            _make_noactivate_toolwindow(_frame_hwnd(tip))
        self.tooltip = tip
        self._cancel_failure_hide()
        self._failure_hide_job = self.top.after(6000, self._hide_tooltip)

    def _cancel_failure_hide(self):
        if self._failure_hide_job is not None:
            try:
                self.top.after_cancel(self._failure_hide_job)
            except (tk.TclError, ValueError):
                pass
            self._failure_hide_job = None

    # -- polling loops ----------------------------------------------------

    def _animate(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last_anim_time)
        self.last_anim_time = now
        if self.visible_now and not self.stopped_ui:
            self.anim = animate_step(self.anim, dt)
            self._render_pong()
        self.top.after(ANIMATION_INTERVAL_MS, self._animate)

    def _sync_ui_state(self):
        snapshot = self.app.snapshot or {}
        stopped = bool(snapshot.get("stopped"))
        changed = stopped != self.stopped_ui
        self.stopped_ui = stopped
        # `pingpong()`'s "stopped" is STOP-file-only: `halt()` writes STOP first
        # and can still fail to mark HALTED or cancel every letter (see
        # `call_halt`'s docstring), so a STOP-only snapshot does not prove the
        # halt finished and must NOT clear a failure ring on its own -- that
        # would hide an incomplete halt. Only a later `call_halt` success
        # (``show_halt_result`` -> ``clear_failed``) does that.
        if changed:
            self._render_pong()
            self._render_stop_button()

    def _rescan(self):
        try:
            windows = enum_windows()
        except OSError:
            windows = []
        self.window = select_window(windows, self.predicate)

    def _handle_resize(self, rect, now: float) -> bool:
        """True while the overlay must stay force-hidden because of an
        in-progress or just-settled host-window resize (see the module's
        2026-09-27 resize note on ``rect_size``/``RESIZE_SETTLE_SECONDS``).

        A pure move (unchanged size) returns False immediately -- no probe
        involved, the existing label-offset-follow keeps working exactly as
        before. A size change hides at once, clearing ``label_offset`` so a
        stale pre-resize offset can never be drawn from. Once the size has
        been unchanged for ``RESIZE_SETTLE_SECONDS``, it waits for a probe
        generation newer than the one seen at that settle point --
        ``LabelProbe`` already polls continuously in the background (see its
        own docstring); this consumes its next fresh answer instead of
        whatever it had cached from before the resize, without this method
        (or the Tk loop) blocking on it itself. A fresh miss (the label is
        genuinely gone, e.g. a narrow window with its side panel open) sets
        ``_suppress_until_label_found`` so the widget stays hidden with no
        offset or fallback-corner carry-over until a later probe (from
        another settle, or just a subsequent poll) actually finds it --
        ordinary steady-state misses keep using ``LABEL_MISS_GRACE_SECONDS``
        as before, this only guards the resize case the owner reported."""
        if rect is None:
            return self._resize_pending or self._suppress_until_label_found
        size = rect_size(rect)
        if self._last_rect_size is None:
            self._last_rect_size = size
        elif size != self._last_rect_size:
            self._last_rect_size = size
            self._resize_last_change = now
            self._resize_pending = True
            self._resize_settled = False
            self._suppress_until_label_found = False
            self.label_offset = None
            return True
        hwnd = self.window.hwnd if self.window else 0
        if self._resize_pending:
            if not self._resize_settled:
                if now - self._resize_last_change < RESIZE_SETTLE_SECONDS:
                    return True
                self._resize_settled = True
                self._resize_probe_baseline = self.app.label_probe.generation(self.key)
                return True
            if self.app.label_probe.generation(self.key) <= self._resize_probe_baseline:
                return True
            label = self.app.label_probe.read(self.key)
            self._resize_pending = False
            self._resize_settled = False
            if isinstance(label, dict) and label.get("found") and label.get("hwnd") == hwnd:
                self.label_offset = label_offset(label, rect)
                self.last_label_time = now
                return False
            self._suppress_until_label_found = True
            return True
        if self._suppress_until_label_found:
            label = self.app.label_probe.read(self.key)
            if isinstance(label, dict) and label.get("found") and label.get("hwnd") == hwnd:
                self.label_offset = label_offset(label, rect)
                self.last_label_time = now
                self._suppress_until_label_found = False
                return False
            return True
        return False

    def _tick(self):
        now = time.monotonic()
        if (now - self.last_scan >= WINDOW_RESCAN_SECONDS
                or self.window is None
                or not USER32.IsWindow(self.window.hwnd)):
            self._rescan()
            self.last_scan = now
        hwnd = self.window.hwnd if self.window else 0
        rect = visible_target_rect(hwnd) if hwnd else None
        resize_blocked = self._handle_resize(rect, now)
        should_show = bool(rect) and self.app.session_visible() and not resize_blocked
        if should_show:
            target_dpi = USER32.GetDpiForWindow(hwnd)
            if target_dpi and abs(target_dpi / 96 - self.dpi_scale) >= 0.05:
                self.dpi_scale = target_dpi / 96
                self._build()
                self._last_pos = None
            label = self.app.label_probe.read(self.key)
            if isinstance(label, dict) and label.get("found") and label.get("hwnd") == hwnd:
                self.label_offset = label_offset(label, rect)
                self.last_label_time = now
            elif now - self.last_label_time >= LABEL_MISS_GRACE_SECONDS:
                self.label_offset = None
            self._sync_ui_state()
            if not self.visible_now:
                self.top.deiconify()
                self.pad.deiconify()
                self.top.update_idletasks()
                self.visible_now = True
                self.frame = _frame_hwnd(self.top)
                self.pad_frame = _frame_hwnd(self.pad)
                for window in (self.frame, self.pad_frame):
                    _make_noactivate_toolwindow(window)
                    USER32.SetWindowPos(window, HWND_NOTOPMOST, 0, 0, 0, 0,
                                         SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE)
                self._last_pos = None
            self._ensure_hook(self.window.pid)
            if rect != self.last_rect:
                self.last_rect = rect
                self.last_motion = now
            self._place(hwnd, rect)
        else:
            if self.visible_now:
                self._hide_tooltip()
                self.top.withdraw()
                self.pad.withdraw()
                self.visible_now = False
                self.hovered = False
            self._unhook()
            self.last_rect = None
            self._last_pos = None
        moving = now - self.last_motion < 0.35
        # Fast cadence while a resize is being handled too (not just a plain
        # move): it's how the fresh probe answer in ``_handle_resize`` gets
        # picked up promptly once it lands, instead of waiting out the slow
        # background poll -- see that method's docstring.
        interval = (MOVING_INTERVAL_MS if moving or resize_blocked else
                    IDLE_INTERVAL_MS if should_show else BACKGROUND_INTERVAL_MS)
        self.top.after(interval, self._tick)

    def _place(self, hwnd, rect):
        """Move and restack the drawing and its click pad with SetWindowPos
        on their real frame windows, so the widget tracks the app with no Tk
        idle round-trip."""
        work_rect = monitor_work_rect(hwnd)
        if self.label_offset is not None:
            label_rect = apply_label_offset(self.label_offset, rect)
            x, y = compute_label_dock_position(label_rect, self.size, work_rect,
                                               dpi_scale=self.dpi_scale)
        else:
            x, y = compute_fallback_position(rect, self.size, work_rect)
        moved = (x, y) != self._last_pos
        if not moved and is_stacked(hwnd, self.frame, self.pad_frame):
            return
        stack_pair(hwnd, self.frame, self.pad_frame, x, y, move=moved)
        self._last_pos = (x, y)

    def follow(self):
        """Win-event path: the app window moved or resized; place at once.
        Still runs ``_handle_resize`` first (a location-change event fires on
        a resize too, not just a move) so a live drag-resize can't sneak a
        placement in against a stale/cleared offset between ``_tick`` polls;
        a resize in progress withdraws here exactly as ``_tick`` would. The
        win-event hook itself is left attached (not ``_unhook``'d) so further
        location-change events during the same drag keep calling this
        promptly instead of falling back to ``_tick``'s own polling."""
        if not self.visible_now or not self.window or not self.frame:
            return
        rect = visible_target_rect(self.window.hwnd)
        if not rect:
            return
        now = time.monotonic()
        if rect != self.last_rect:
            self.last_rect = rect
            self.last_motion = now
        if self._handle_resize(rect, now):
            if self.visible_now:
                self._hide_tooltip()
                self.top.withdraw()
                self.pad.withdraw()
                self.visible_now = False
                self.hovered = False
            return
        self._place(self.window.hwnd, rect)

    def restack(self):
        if self.visible_now and self.window and self.frame:
            stack_pair(self.window.hwnd, self.frame, self.pad_frame)

    def _on_win_event(self, _hook, _event, hwnd, id_object, id_child, _thread, _time):
        try:
            if (id_object == OBJID_WINDOW and id_child == 0 and self.window
                    and hwnd == self.window.hwnd):
                self.follow()
        except Exception:  # A ctypes callback must never raise into user32.
            pass

    def _ensure_hook(self, pid: int):
        """Location-change events from the app's own process only, while the
        widget is shown: moves land within a frame instead of a poll."""
        if self._hook and self._hook_pid == pid:
            return
        self._unhook()
        self._hook_proc = WinEventProc(self._on_win_event)
        self._hook = USER32.SetWinEventHook(
            EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, None,
            self._hook_proc, pid, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        self._hook_pid = pid if self._hook else 0

    def _unhook(self):
        if self._hook:
            USER32.UnhookWinEvent(self._hook)
        self._hook = None
        self._hook_pid = 0
        self._hook_proc = None


class App:
    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.theme = system_theme()
        self.last_theme_check = 0.0
        self.session_state = SessionState()
        self.snapshot: dict | None = None
        self.visible_flag = False
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        self.build_version = eutonos_build_version()
        self.install_signature = install_signature()
        self.last_install_check = time.monotonic()
        self.poller = PingpongPoller(root_path)
        self.label_probe = LabelProbe()
        self.overlays = {key: Overlay(self, key, cfg) for key, cfg in TARGETS.items()}
        self.poller.start()
        # Bringing an app forward lifts it over its widget; put the widget
        # back on top of it at once instead of on the next tick.
        self._foreground_proc = WinEventProc(self._on_foreground)
        self._foreground_hook = USER32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._foreground_proc,
            0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        self.tk_root.after(TICK_MS, self._tick_session)

    def _on_foreground(self, *_args):
        try:
            for overlay in self.overlays.values():
                overlay.restack()
        except Exception:  # A ctypes callback must never raise into user32.
            pass

    @property
    def palette(self):
        return COLORS[self.theme]

    @property
    def stopped(self) -> bool:
        return bool(self.snapshot and self.snapshot.get("stopped"))

    def session_visible(self) -> bool:
        return self.visible_flag

    def _tick_session(self):
        now = time.monotonic()
        if now - self.last_install_check >= INSTALL_CHECK_SECONDS:
            self.last_install_check = now
            if install_signature() != self.install_signature:
                # An install replaced the code this long-lived widget loaded
                # at startup (see the module docstring's "stale copy" note,
                # which used to only cover the halt/resume subprocess -- a
                # widget itself can go stale the same way). Hand off to a
                # fresh process running the current code and stop being this
                # one; if the handoff itself could not even start, keep
                # running on the old code rather than disappear.
                self.restart_self()
                return
        if now - self.last_theme_check >= 2:
            self.last_theme_check = now
            new_theme = system_theme()
            if new_theme != self.theme:
                self.theme = new_theme
                for overlay in self.overlays.values():
                    overlay.refresh_theme()
        snapshot = self.poller.read()
        if snapshot != self.snapshot:
            self._apply_snapshot(snapshot)
        self.tk_root.after(TICK_MS, self._tick_session)

    def restart_self(self):
        if not relaunch_process():
            return
        try:
            self.quit()
        except Exception:
            pass
        os._exit(0)

    def _apply_snapshot(self, snapshot):
        was_visible = self.visible_flag
        self.snapshot = snapshot
        self.visible_flag = self.session_state.apply(snapshot or {})
        if self.visible_flag and not was_visible:
            self.label_probe.start()
        elif was_visible and not self.visible_flag:
            self.label_probe.stop()

    def request_halt(self):
        root_path = self.root_path

        def run():
            outcome = call_halt(root_path)
            summary = summarize_halt_result(outcome)
            fresh = call_pingpong(root_path)
            self.tk_root.after(0, lambda: self._apply_action_result(summary, fresh))

        threading.Thread(target=run, daemon=True).start()

    def request_resume(self):
        root_path = self.root_path

        def run():
            outcome = call_resume(root_path)
            summary = summarize_resume_result(outcome)
            fresh = call_pingpong(root_path)
            self.tk_root.after(0, lambda: self._apply_action_result(summary, fresh))

        threading.Thread(target=run, daemon=True).start()

    def _apply_action_result(self, summary: dict, fresh):
        """Tk-thread side of ``request_halt``/``request_resume`` (J11): apply
        the fresh snapshot as before, then tell every overlay to show or
        clear its failure look for whichever action just ran."""
        self._apply_snapshot(fresh)
        for overlay in self.overlays.values():
            overlay.show_halt_result(summary)

    def hide_until_next_session(self):
        self.session_state.hide_until_next_session()
        self._apply_snapshot(self.snapshot)

    def toggle_startup(self):
        new_value = not bool(self.startup_var.get())
        try:
            set_startup(new_value)
        except OSError:
            self.startup_var.set(not new_value)
            return
        self.startup_var.set(new_value)

    def quit(self):
        self.poller.stop()
        if self._foreground_hook:
            USER32.UnhookWinEvent(self._foreground_hook)
        for overlay in self.overlays.values():
            overlay._unhook()
        self.label_probe.stop()
        for overlay in self.overlays.values():
            overlay.destroy_tooltip()
        self.tk_root.destroy()

    def run(self):
        self.tk_root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="Repo root override (default: parent of tools/)")
    parser.add_argument("--sample", action="store_true",
                        help="Print one read-only pingpong()+window snapshot as JSON and exit")
    parser.add_argument("--install-startup", action="store_true")
    parser.add_argument("--remove-startup", action="store_true")
    args = parser.parse_args()
    root_path = (args.root or default_root()).resolve()

    if args.install_startup or args.remove_startup:
        if args.install_startup == args.remove_startup:
            parser.error("Choose one startup action")
        set_startup(args.install_startup)
        print("enabled" if startup_enabled() else "disabled")
        return

    if args.sample:
        print(json.dumps(build_sample(root_path), separators=(",", ":")))
        return

    if sys.platform != "win32":
        parser.error("This helper requires Windows")
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()
    App(root_path).run()


if __name__ == "__main__":
    main()
