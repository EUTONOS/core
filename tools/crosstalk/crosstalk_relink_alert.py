"""Crosstalk relink alert (slice 3B.18, owner request 2026-10-26, stdlib only): the small EUTONOS window that says
"Enabling Remote Control for your sessions" with "n of m" right after Claude Desktop starts, and goes away by itself
once every session is back.

Two halves, with no new channel between them:
  * the helper side (`default_spawn`): starts this module in its own windowless process, once per Desktop load
    (the helper remembers that in loads.json before it starts the window);
  * the window side (`python -I -B crosstalk_relink_alert.py --folder HELPER_DIR --load LOAD_ID`): a small borderless
    always-on-top window in the bottom-right corner of the work area. It only READS helper/loads.json (the same
    ledger the relink code writes) a few times a second and shows what the load's relink entries say. It writes
    nothing, sends nothing, types nothing and reads no credential.

It never takes keyboard focus and is never the active window, because the owner may be typing. It is a plain Win32
window (ctypes) created WITH WS_EX_NOACTIVATE (a click or a show never activates it), WS_EX_TOOLWINDOW (no taskbar
button, no Alt+Tab entry) and WS_EX_TOPMOST, shown with SW_SHOWNOACTIVATE and sized with SWP_NOACTIVATE; it also
answers WM_MOUSEACTIVATE with MA_NOACTIVATE. A click on it (which still arrives, without activating anything) dismisses
it. Without Windows it cannot show and exits with code 2; the relink itself does not depend on it.

What it shows, from `view_for_load` (a pure function over the ledger, tested without a window):
  working   "Enabling Remote Control for your sessions..."   n of m   (m is unknown until the helper has listed them)
  done      "n of n sessions connected"                       closes after DONE_SHOW_S
  problem   "4 of 5 connected - 1 could not reconnect"        the chat titles; stays until clicked or PROBLEM_SHOW_S
  closed    nothing to do (no entries, a newer load, or the load left the ledger)
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TAG = "crosstalk.relink-alert.v1"
WINDOW_TITLE = "EUTONOS: Remote Control"
LOADS_FILE = "loads.json"
POLL_MS = 300
SHOW_UP_TO_S = 150.0          # still waiting for chats after this long counts as a problem, not as "working"
EMPTY_LIST_WAIT_S = 45.0      # no entry listed this long after the load: nothing is coming, close
DONE_SHOW_S = 1.2             # "n of n connected" stays this long
PROBLEM_SHOW_S = 60.0         # a problem stays this long unless clicked
TITLES_SHOWN = 4
TITLE_MAX = 34
WORKING_TEXT = "Enabling Remote Control for your sessions…"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# entry states that mean "no longer waiting"
_BAD_STATES = ("failed", "expired")
_PENDING_STATES = ("queued", "claimed")
SKIP_ALREADY_ON = "desktop-enabled"   # core.RELINK_SKIP_DESKTOP_ENABLED: Desktop turned that chat on itself


def _title(entry: dict) -> str:
    text = " ".join(str(entry.get("title") or entry.get("session") or "a chat").split())
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


def view_for_load(ledger, load_id: str, now_s: float, shown_s: float | None = None) -> dict:
    """What the window shows for one load, from the loads.json content. Pure: no files, no clock.
    Returns {"state": "working"|"done"|"problem"|"closed", "title": str, "lines": [str], "done": int, "total": int,
    "bad": [titles]}. `shown_s` is when the window first showed (for the give-up time); it defaults to the load's own
    time."""
    loads = ledger.get("loads") if isinstance(ledger, dict) else None
    loads = loads if isinstance(loads, dict) else {}
    load = loads.get(load_id)
    closed = {"state": "closed", "title": "", "lines": [], "done": 0, "total": 0, "bad": []}
    if not isinstance(load, dict):
        return closed
    newest = max(loads, key=lambda key: (loads[key] or {}).get("seen_ns") or 0)
    if newest != load_id:
        return closed  # a newer Desktop load took over: that one has its own window
    entries = [e for e in (load.get("reconnects") or {}).values() if isinstance(e, dict)]
    # a chat the owner turned off (skipped for any other reason) is not part of "your sessions"; neither is one the
    # owner's own halt, messaging-off or deny list refused (that is the owner's choice, not a failure to report)
    counted = [e for e in entries if e.get("state") != "refused"
               and not (e.get("state") == "skipped" and e.get("reason") != SKIP_ALREADY_ON)]
    done = [e for e in counted if e.get("state") == "done" or e.get("state") == "skipped"]
    bad = [e for e in counted if e.get("state") in _BAD_STATES]
    waiting = [e for e in counted if e.get("state") in _PENDING_STATES]
    total = len(counted)
    age = now_s - ((load.get("seen_ns") or 0) / 1e9)
    phase = str(load.get("phase") or "")
    if not counted:
        # not listed yet (still pending), or the load had nothing to relink
        if phase == "pending" and age < EMPTY_LIST_WAIT_S:
            return {"state": "working", "title": WORKING_TEXT, "lines": [], "done": 0, "total": 0, "bad": []}
        return closed
    if waiting:
        started = shown_s if shown_s is not None else (load.get("seen_ns") or 0) / 1e9
        if now_s - started > SHOW_UP_TO_S:
            bad = bad + waiting  # nobody took them in all this time: say so instead of spinning
        else:
            return {"state": "working", "title": WORKING_TEXT, "lines": [f"{len(done)} of {total}"], "done": len(done),
                    "total": total, "bad": []}
    if bad:
        titles = [_title(e) for e in bad]
        head = f"{len(done)} of {total} connected · {len(bad)} could not reconnect"
        shown = titles[:TITLES_SHOWN] + ([f"+{len(titles) - TITLES_SHOWN} more"] if len(titles) > TITLES_SHOWN else [])
        return {"state": "problem", "title": head, "lines": shown + ["Type in them in Desktop to bring the link back. Click to close."],
                "done": len(done), "total": total, "bad": titles}
    return {"state": "done", "title": f"{total} of {total} sessions connected", "lines": [], "done": total,
            "total": total, "bad": []}


def window_action(view: dict, now_s: float, finished_s: float | None) -> tuple[str, float | None]:
    """("show" | "close", finished_s). A done view closes DONE_SHOW_S after it was first seen, a problem view
    PROBLEM_SHOW_S after, a closed view at once, a working view never."""
    state = view.get("state")
    if state == "closed":
        return "close", finished_s
    if state == "working":
        return "show", None
    started = finished_s if finished_s is not None else now_s
    limit = DONE_SHOW_S if state == "done" else PROBLEM_SHOW_S
    return ("close" if now_s - started >= limit else "show"), started


def read_ledger(folder: Path):
    """loads.json as a dict, or None (missing, half-written during an atomic replace, unreadable)."""
    with contextlib.suppress(OSError, ValueError):
        value = json.loads((Path(folder) / LOADS_FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    return None


# ------------------------------------------------------------------ helper side
def default_spawn(folder: Path, load_id: str):
    """Start the window in its own windowless process. Returns the Popen (or raises OSError)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | NO_WINDOW
    python = sys.executable
    if os.name == "nt":
        sibling = Path(sys.executable).with_name("pythonw.exe")
        python = str(sibling) if sibling.is_file() else sys.executable
    return subprocess.Popen([python, "-I", "-B", str(Path(__file__).resolve()), "--folder", str(folder),
                             "--load", load_id], creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------ window side
# A plain Win32 window made with ctypes (stdlib only), not a Tk window. Tk creates its top-level window as an ordinary
# one, which Windows activates for a moment (live probe 2026-10-26: the foreground window changed for ~0.3 s while the
# owner was idle, before any style could be applied). Here the window is CREATED with WS_EX_NOACTIVATE |
# WS_EX_TOOLWINDOW | WS_EX_TOPMOST and shown with SW_SHOWNOACTIVATE, so it is never the active window at any moment.
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
SPI_GETWORKAREA = 0x0030
WM_DESTROY, WM_PAINT, WM_ERASEBKGND, WM_MOUSEACTIVATE, WM_TIMER, WM_LBUTTONDOWN = 0x0002, 0x000F, 0x0014, 0x0021, 0x0113, 0x0201
MA_NOACTIVATE = 3
DT_LEFT, DT_WORDBREAK, DT_NOPREFIX, DT_CALCRECT = 0x0000, 0x0010, 0x0800, 0x0400
BASE_WIDTH, PAD_X, PAD_Y, MARGIN = 340, 14, 10, 16     # in 96-dpi pixels; scaled by the screen's dpi
COLORS = {"background": (0x1B, 0x23, 0x30), "border": (0x33, 0x40, 0x5A), "accent": (0x4F, 0x8C, 0xFF),
          "text": (0xF2, 0xF5, 0xFA), "muted": (0xAA, 0xB4, 0xC3), "problem": (0xFF, 0xB4, 0x54)}


def _rgb(name: str) -> int:
    red, green, blue = COLORS[name]
    return red | (green << 8) | (blue << 16)


class Win32Alert:  # pragma: no cover - needs a desktop
    """The window and its message loop. It polls the ledger on a timer and repaints when the view changes."""

    def __init__(self, folder: Path, load_id: str):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.folder, self.load_id = Path(folder), load_id
        self.user32, self.gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        self.hwnd = None
        self.view = {"state": "working", "title": WORKING_TEXT, "lines": [], "done": 0, "total": 0, "bad": []}
        self.finished_s: float | None = None
        self.started_s = time.time()
        self.last_key = None
        self.fonts: dict = {}
        self.scale = 1.0
        self._declare()

    def _declare(self) -> None:
        ct, wt, user32, gdi32 = self.ct, self.wt, self.user32, self.gdi32
        handle, lres = ct.c_void_p, ct.c_ssize_t
        self.WNDPROC = ct.WINFUNCTYPE(lres, handle, ct.c_uint, ct.c_size_t, lres)

        class WNDCLASSW(ct.Structure):
            _fields_ = [("style", ct.c_uint), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ct.c_int),
                        ("cbWndExtra", ct.c_int), ("hInstance", handle), ("hIcon", handle), ("hCursor", handle),
                        ("hbrBackground", handle), ("lpszMenuName", ct.c_wchar_p), ("lpszClassName", ct.c_wchar_p)]

        class PAINTSTRUCT(ct.Structure):
            _fields_ = [("hdc", handle), ("fErase", ct.c_int), ("rcPaint", wt.RECT), ("fRestore", ct.c_int),
                        ("fIncUpdate", ct.c_int), ("rgbReserved", ct.c_byte * 32)]
        self.WNDCLASSW, self.PAINTSTRUCT = WNDCLASSW, PAINTSTRUCT
        prototypes = [
            (user32.DefWindowProcW, lres, [handle, ct.c_uint, ct.c_size_t, lres]),
            (user32.RegisterClassW, wt.ATOM, [ct.POINTER(WNDCLASSW)]),
            (user32.CreateWindowExW, handle, [wt.DWORD, ct.c_wchar_p, ct.c_wchar_p, wt.DWORD, ct.c_int, ct.c_int, ct.c_int,
                                              ct.c_int, handle, handle, handle, handle]),
            (user32.ShowWindow, ct.c_int, [handle, ct.c_int]),
            (user32.SetWindowPos, ct.c_int, [handle, handle, ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_uint]),
            (user32.SetTimer, ct.c_size_t, [handle, ct.c_size_t, ct.c_uint, handle]),
            (user32.GetMessageW, ct.c_int, [ct.POINTER(wt.MSG), handle, ct.c_uint, ct.c_uint]),
            (user32.TranslateMessage, ct.c_int, [ct.POINTER(wt.MSG)]),
            (user32.DispatchMessageW, lres, [ct.POINTER(wt.MSG)]),
            (user32.PostQuitMessage, None, [ct.c_int]),
            (user32.DestroyWindow, ct.c_int, [handle]),
            (user32.InvalidateRect, ct.c_int, [handle, handle, ct.c_int]),
            (user32.BeginPaint, handle, [handle, ct.POINTER(PAINTSTRUCT)]),
            (user32.EndPaint, ct.c_int, [handle, ct.POINTER(PAINTSTRUCT)]),
            (user32.GetClientRect, ct.c_int, [handle, ct.POINTER(wt.RECT)]),
            (user32.FillRect, ct.c_int, [handle, ct.POINTER(wt.RECT), handle]),
            (user32.FrameRect, ct.c_int, [handle, ct.POINTER(wt.RECT), handle]),
            (user32.DrawTextW, ct.c_int, [handle, ct.c_wchar_p, ct.c_int, ct.POINTER(wt.RECT), ct.c_uint]),
            (user32.GetDC, handle, [handle]),
            (user32.ReleaseDC, ct.c_int, [handle, handle]),
            (user32.LoadCursorW, handle, [handle, handle]),
            (user32.SystemParametersInfoW, ct.c_int, [ct.c_uint, ct.c_uint, handle, ct.c_uint]),
            (gdi32.CreateSolidBrush, handle, [ct.c_uint]),
            (gdi32.CreateFontW, handle, [ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.DWORD, wt.DWORD, wt.DWORD,
                                         wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ct.c_wchar_p]),
            (gdi32.SelectObject, handle, [handle, handle]),
            (gdi32.SetBkMode, ct.c_int, [handle, ct.c_int]),
            (gdi32.SetTextColor, ct.c_uint, [handle, ct.c_uint]),
            (gdi32.GetDeviceCaps, ct.c_int, [handle, ct.c_int]),
            (ct.windll.kernel32.GetModuleHandleW, handle, [ct.c_wchar_p]),
        ]
        for function, result, arguments in prototypes:
            function.restype, function.argtypes = result, arguments

    # ---- drawing
    def _font(self, points: int, bold: bool):
        key = (points, bold)
        if key not in self.fonts:
            height = -int(round(points * 96 / 72 * self.scale))
            self.fonts[key] = self.gdi32.CreateFontW(height, 0, 0, 0, 700 if bold else 400, 0, 0, 0, 1, 0, 0, 5, 0,
                                                     "Segoe UI")
        return self.fonts[key]

    def _blocks(self) -> list[tuple[str, int, bool, str]]:
        view = self.view
        blocks = [("EUTONOS", 8, True, "accent"),
                  (view["title"], 11, True, "problem" if view["state"] == "problem" else "text")]
        if view["lines"]:
            blocks.append(("\n".join(view["lines"]), 10, False, "muted"))
        return blocks

    def _layout(self, hdc) -> tuple[int, int, list]:
        """(width, height, [(text, font, color, rect)]) in device pixels for the current view."""
        ct, wt = self.ct, self.wt
        pad_x, pad_y = int(PAD_X * self.scale), int(PAD_Y * self.scale)
        width = int(BASE_WIDTH * self.scale)
        inner = width - 2 * pad_x
        y, placed = pad_y, []
        for text, points, bold, color in self._blocks():
            font = self._font(points, bold)
            previous = self.gdi32.SelectObject(hdc, font)
            rect = wt.RECT(0, 0, inner, 0)
            self.user32.DrawTextW(hdc, text, -1, ct.byref(rect), DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX)
            self.gdi32.SelectObject(hdc, previous)
            placed.append((text, font, color, wt.RECT(pad_x, y, pad_x + inner, y + rect.bottom)))
            y += rect.bottom + int(3 * self.scale)
        return width, y - int(3 * self.scale) + pad_y, placed

    def _paint(self) -> None:
        ct, wt = self.ct, self.wt
        paint = self.PAINTSTRUCT()
        hdc = self.user32.BeginPaint(self.hwnd, ct.byref(paint))
        try:
            client = wt.RECT()
            self.user32.GetClientRect(self.hwnd, ct.byref(client))
            self.user32.FillRect(hdc, ct.byref(client), self.brushes["background"])
            self.user32.FrameRect(hdc, ct.byref(client), self.brushes["border"])
            self.gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
            _, _, placed = self._layout(hdc)
            for text, font, color, rect in placed:
                previous = self.gdi32.SelectObject(hdc, font)
                self.gdi32.SetTextColor(hdc, _rgb(color))
                self.user32.DrawTextW(hdc, text, -1, ct.byref(rect), DT_LEFT | DT_WORDBREAK | DT_NOPREFIX)
                self.gdi32.SelectObject(hdc, previous)
        finally:
            self.user32.EndPaint(self.hwnd, ct.byref(paint))

    def _place(self) -> None:
        """Size the window to its text and put it in the bottom-right corner of the work area, without activating."""
        ct, wt = self.ct, self.wt
        hdc = self.user32.GetDC(self.hwnd)
        try:
            width, height, _ = self._layout(hdc)
        finally:
            self.user32.ReleaseDC(self.hwnd, hdc)
        area = wt.RECT()
        if not self.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ct.cast(ct.byref(area), ct.c_void_p), 0):
            area = wt.RECT(0, 0, 1280, 720)
        margin = int(MARGIN * self.scale)
        self.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, area.right - width - margin, area.bottom - height - margin,
                                 width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    # ---- the loop
    def _poll(self) -> None:
        now = time.time()
        view = view_for_load(read_ledger(self.folder), self.load_id, now, self.started_s)
        action, self.finished_s = window_action(view, now, self.finished_s)
        if action == "close":
            self.user32.DestroyWindow(self.hwnd)
            return
        key = (view["state"], view["title"], tuple(view["lines"]))
        if key != self.last_key:
            self.last_key, self.view = key, view
            self._place()
            self.user32.InvalidateRect(self.hwnd, None, 1)

    def _proc(self, hwnd, message, wparam, lparam):
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if message == WM_ERASEBKGND:
            return 1  # _paint fills the whole client area
        if message == WM_PAINT:
            self._paint()
            return 0
        if message == WM_TIMER:
            self._poll()
            return 0
        if message == WM_LBUTTONDOWN:
            self.user32.DestroyWindow(hwnd)  # a click dismisses it
            return 0
        if message == WM_DESTROY:
            self.user32.PostQuitMessage(0)
            return 0
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self) -> int:
        ct, wt, user32, gdi32 = self.ct, self.wt, self.user32, self.gdi32
        with contextlib.suppress(Exception):
            user32.SetProcessDPIAware()  # crisp text on a scaled screen; sizes below follow the dpi
        screen = user32.GetDC(None)
        self.scale = max(1.0, gdi32.GetDeviceCaps(screen, 88) / 96.0)  # LOGPIXELSX
        user32.ReleaseDC(None, screen)
        self.brushes = {name: gdi32.CreateSolidBrush(_rgb(name)) for name in ("background", "border")}
        self.proc = self.WNDPROC(self._proc)  # kept on self: a collected callback would crash the process
        instance = ct.windll.kernel32.GetModuleHandleW(None)
        klass = self.WNDCLASSW(0x0003, self.proc, 0, 0, instance, None, user32.LoadCursorW(None, ct.c_void_p(32512)),
                               self.brushes["background"], None, "EutonosRelinkAlert")
        if not user32.RegisterClassW(ct.byref(klass)):
            return 2
        self.hwnd = user32.CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST, "EutonosRelinkAlert",
                                           WINDOW_TITLE, WS_POPUP, 0, 0, 10, 10, None, None, instance, None)
        if not self.hwnd:
            return 2
        self._poll()                               # a closed view never shows at all
        if not user32.IsWindow(self.hwnd):
            return 0
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetTimer(self.hwnd, 1, POLL_MS, None)
        message = wt.MSG()
        while user32.GetMessageW(ct.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ct.byref(message))
            user32.DispatchMessageW(ct.byref(message))
        return 0


def run_window(folder: Path, load_id: str) -> int:  # pragma: no cover - needs a desktop
    """Exit code 0 when it showed and closed, 2 when it could not show (not Windows, or a Win32 call failed)."""
    if os.name != "nt":
        return 2
    try:
        return Win32Alert(folder, load_id).run()
    except Exception:
        return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crosstalk_relink_alert", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", required=True, help="the helper folder that holds loads.json")
    parser.add_argument("--load", required=True, help="the Desktop load this window follows")
    args = parser.parse_args(argv)
    return run_window(Path(args.folder), args.load)


if __name__ == "__main__":
    sys.exit(main())
