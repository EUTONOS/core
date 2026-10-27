"""Crosstalk focus gate (stdlib only): the one way Crosstalk may take window focus.

Owner rule (2026-10-13, README "Focus rule"): running chats talk in the background. When a wake or a new chat MUST
take focus, this module does, in order: note what had focus; show a small non-activating overlay ("losing focus to
wake ...") with a countdown; run the wake action; wait for another window to come forward; give focus back; confirm
the wake through Crosstalk itself. Focus events run one at a time across the helper, the plugin CLI and the Codex
waiter (a device-wide lock held until the confirm step ends), so two wakes close together cannot lose one.

What it never does: type, paste, read or touch the clipboard or a composer draft, resend or retry a letter, switch
accounts or read a credential. It records only window handles and process ids, never titles, keys or message text.
Typing that lands in the wrong window during the few seconds is accepted by the owner, who can turn Crosstalk off.

This module has no dependency on the Crosstalk core: the core passes in the lock, the controls check and the log
path (crosstalk_p0_core.focus_wake). The Windows behaviour lives behind `Win32Platform`; tests use a fake of the same
shape. Off Windows (or with CROSSTALK_P0_FOCUS=off) `NullPlatform` runs the action and the confirm only.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import time

TAG = "crosstalk.focus.v1"
DEFAULT_NOTICE_S = 3.0
MAX_NOTICE_S = 30.0
COME_FORWARD_TIMEOUT_S = 10.0
COME_FORWARD_STEP_S = 0.25
SETTLE_S = 1.5
# A late wake (the window came forward after the come-forward wait) is given back only when the owner has been idle
# this long: the owner may already be typing into the window that just came up (fail closed when idle is unreadable).
LATE_RESTORE_MIN_IDLE_S = 2.0
CHAT_SETTLE_S = 0.6
CONFIRM_TIMEOUT_S = 30.0
CONFIRM_STEP_S = 1.0
LOCK_TIMEOUT_S = 60.0
LOG_ROTATE_BYTES = 1_000_000
FOCUS_OFF_VALUES = ("off", "0", "false", "no")

SW_SHOWNOACTIVATE = 4
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000008, 0x00000080, 0x08000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WM_PAINT, WM_NCHITTEST, WM_MOUSEACTIVATE = 0x000F, 0x0084, 0x0021
HTTRANSPARENT, MA_NOACTIVATE, LWA_ALPHA, PM_REMOVE = -1, 3, 0x2, 0x1
OVERLAY_ALPHA = 225          # out of 255: slightly see-through
OVERLAY_FADE_IN_S = 0.6
OVERLAY_FADE_OUT_S = 0.25
# The overlay sits over the window the owner is using when it is a chat app; otherwise mid-screen.
CHAT_APP_EXES = ("claude.exe", "chatgpt.exe", "chatgpt classic.exe", "codex.exe")

# Owner request 2026-10-25: a question the owner must answer (the CLI-login Yes/No box) and the sign-in it opens (the
# `claude auth login` console and the browser page) come to the front, so they are not missed behind other windows.
SW_RESTORE = 9
ASFW_ANY = 0xFFFFFFFF
FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
MESSAGE_BOX_CLASS = "#32770"
CONSOLE_CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS")  # conhost, Windows Terminal
BROWSER_EXES = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
                "librewolf.exe", "waterfox.exe", "iexplore.exe")
SIGN_IN_TITLE_WORDS = ("claude", "anthropic")
DESKTOP_EXE = "claude.exe"
DESKTOP_WINDOW_CLASS = "Chrome_WidgetWin"   # Electron's top-level window class (Chrome_WidgetWin_1)
DESKTOP_WINDOW_TITLE = "Claude"
BOX_WAIT_S = 5.0
BOX_STEP_S = 0.1
LOGIN_FOLLOW_S = 300.0
LOGIN_STEP_S = 0.25
LOGIN_CONSOLE_WAIT_S = 15.0


# ------------------------------------------------------------------ platforms
class NullPlatform:
    """No window handling: the action and the confirm run, nothing else (not Windows, or focus handling off)."""
    available = False

    def foreground(self) -> dict:
        return {"hwnd": 0, "pid": 0}

    def exists(self, hwnd: int, pid: int | None = None) -> bool:
        return False

    def idle_seconds(self):
        """Seconds since the owner's last input, or None when unknown."""
        return None

    def overlay(self, text: str, seconds: float) -> bool:
        return False

    def close_overlay(self) -> None:
        """Take down any notice still up (nothing to do without window handling)."""

    def restore(self, prev: dict, current: dict) -> str:
        return "unavailable"

    def top_windows(self) -> dict:
        """Visible top-level windows: hwnd -> (pid, class name, title)."""
        return {}

    def exe(self, pid: int) -> str:
        return ""

    def allow_any_foreground(self) -> None:
        """Let the next window that asks take the foreground (no-op without window handling)."""

    def bring_forward(self, hwnd: int) -> str:
        return "unavailable"


class Win32Platform(NullPlatform):
    """Win32 through ctypes. Reads the foreground window, shows a no-activate overlay, restores the old window."""
    available = True

    def __init__(self):
        import ctypes
        import ctypes.wintypes as wintypes
        self.ctypes, self.wt = ctypes, wintypes
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = self.u32
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u32.IsWindow.argtypes = [wintypes.HWND]
        u32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
        u32.GetLastInputInfo.restype = wintypes.BOOL
        self.k32.GetTickCount.restype = wintypes.DWORD
        u32.BringWindowToTop.argtypes = [wintypes.HWND]
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.k32.GetCurrentThreadId.restype = wintypes.DWORD
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        u32.IsGUIThread.argtypes = [wintypes.BOOL]
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        self._info_type = LASTINPUTINFO

    def _pid(self, hwnd) -> tuple[int, int]:
        pid = self.wt.DWORD()
        thread = self.u32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid)) if hwnd else 0
        return int(thread or 0), int(pid.value)

    def foreground(self) -> dict:
        hwnd = self.u32.GetForegroundWindow()
        return {"hwnd": int(hwnd or 0), "pid": self._pid(hwnd)[1] if hwnd else 0}

    def exists(self, hwnd: int, pid: int | None = None) -> bool:
        """The window is still there. With `pid` (the process id recorded with the handle) it must also still belong
        to that process: a closed window's handle can be recycled for an unrelated window."""
        if not hwnd or not self.u32.IsWindow(hwnd):
            return False
        return pid is None or self._pid(hwnd)[1] == pid

    def idle_seconds(self):
        info = self._info_type()
        info.cbSize = self.ctypes.sizeof(self._info_type)
        if not self.u32.GetLastInputInfo(self.ctypes.byref(info)):
            return None
        return ((int(self.k32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0

    def overlay(self, text: str, seconds: float) -> bool:
        """A large, slightly see-through notice that fades in and counts down, centred on the chat app the owner is
        using (else mid-screen). It is a native layered window created with WS_EX_NOACTIVATE, shown with
        SW_SHOWNOACTIVATE and click-through, so it never takes the foreground or the keyboard focus (the earlier Tk
        window did: it stopped the owner's typing). Returns True when it was shown; on any error the notice time still
        passes so the wake happens at the same moment."""
        end = time.time() + seconds
        try:
            return _NativeOverlay(self).run(text, end)
        except Exception:
            time.sleep(max(0.0, end - time.time()))
            return False

    def close_overlay(self) -> None:
        """Ask a notice that is still up to end now (it fades out at once). A no-op when none is showing."""
        current = _NativeOverlay._current
        if current is not None:
            current.abort = True

    def restore(self, prev: dict, current: dict) -> str:
        """Plain SetForegroundWindow first, then once the AttachThreadInput fallback. Never loops. Returns "gone"
        (nothing touched) when the handle no longer belongs to the process recorded with it."""
        hwnd = prev["hwnd"]
        if not self.exists(hwnd, prev.get("pid")):
            return "gone"
        if self.u32.SetForegroundWindow(hwnd):
            return "plain"
        fg_thread, _pid = self._pid(current["hwnd"])
        me = self.k32.GetCurrentThreadId()
        attached = bool(fg_thread) and bool(self.u32.AttachThreadInput(me, fg_thread, True))
        try:
            self.u32.BringWindowToTop(hwnd)
            ok = self.u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                self.u32.AttachThreadInput(me, fg_thread, False)
        return "attach" if ok else "failed"

    def top_windows(self) -> dict:
        """Visible top-level windows: hwnd -> (pid, class name, title). Titles are compared in memory only; nothing
        here stores or logs them."""
        ctypes, u32, found = self.ctypes, self.u32, {}

        def visit(hwnd, _lparam):
            if hwnd and u32.IsWindowVisible(hwnd):
                size = max(0, int(u32.GetWindowTextLengthW(hwnd))) + 1
                title, cls = ctypes.create_unicode_buffer(size), ctypes.create_unicode_buffer(256)
                u32.GetWindowTextW(hwnd, title, size)
                u32.GetClassNameW(hwnd, cls, 256)
                found[int(hwnd)] = (self._pid(hwnd)[1], cls.value, title.value)
            return True
        u32.EnumWindows(self._enum_proc(visit), 0)
        return found

    def exe(self, pid: int) -> str:
        """The lower-case file name of the process's program, or "" when it cannot be read."""
        k32, wt = self.k32, self.wt
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf, size = self.ctypes.create_unicode_buffer(520), wt.DWORD(520)
            ok = k32.QueryFullProcessImageNameW(handle, 0, buf, self.ctypes.byref(size))
            return os.path.basename(buf.value).lower() if ok else ""
        finally:
            k32.CloseHandle(handle)

    def allow_any_foreground(self) -> None:
        self.u32.AllowSetForegroundWindow(ASFW_ANY)

    def bring_forward(self, hwnd: int) -> str:
        """Bring a window the owner must deal with to the front. One try, never loops: un-minimise it, then the
        restore() steps (plain SetForegroundWindow, else once through AttachThreadInput). When Windows still keeps it
        back, flash its taskbar button until the owner opens it. Never types or clicks. Returns "plain", "attach",
        "flashed" or "gone"."""
        u32 = self.u32
        if not hwnd or not u32.IsWindow(hwnd):
            return "gone"
        u32.IsGUIThread(True)  # AttachThreadInput needs this thread to have an input queue
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, SW_RESTORE)
        method = self.restore({"hwnd": hwnd, "pid": None}, self.foreground())
        if method in ("plain", "attach") and self.foreground()["hwnd"] == hwnd:
            return method
        wt = self.wt

        class FLASHWINFO(self.ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("hwnd", wt.HWND), ("dwFlags", wt.DWORD), ("uCount", wt.UINT),
                        ("dwTimeout", wt.DWORD)]
        info = FLASHWINFO(self.ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        u32.FlashWindowEx(self.ctypes.byref(info))
        return "flashed"


class _NativeOverlay:
    """The overlay window itself (Win32 through ctypes, no Tk). One instance per notice."""
    _class_name = "CrosstalkFocusNotice"
    _wndproc = None  # kept alive for the process: the registered class points at it
    _current = None
    abort = False

    def __init__(self, platform: "Win32Platform"):
        self.p = platform
        ctypes, wt = platform.ctypes, platform.wt
        self.ctypes, self.wt = ctypes, wt
        self.u32, self.g32, self.k32 = platform.u32, ctypes.WinDLL("gdi32"), platform.k32
        self.lines, self.count = [], ""
        self.font_body = self.font_count = None

    # -- placement
    def _exe(self, pid: int) -> str:
        return self.p.exe(pid)

    def _area(self) -> tuple:
        """(left, top, right, bottom): the foreground chat app's window, else the work area of its monitor."""
        ctypes, wt, u32 = self.ctypes, self.wt, self.u32
        fg = u32.GetForegroundWindow()
        rect = wt.RECT()
        if (fg and self._exe(self.p._pid(fg)[1]) in CHAT_APP_EXES and u32.GetWindowRect(fg, ctypes.byref(rect))
                and rect.right - rect.left > 200 and rect.bottom - rect.top > 150):
            return rect.left, rect.top, rect.right, rect.bottom

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        monitor = u32.MonitorFromWindow(fg, 2)  # MONITOR_DEFAULTTONEAREST (primary when there is no window)
        if monitor and u32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
        return 0, 0, u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)

    # -- window
    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCHITTEST:
            return HTTRANSPARENT  # click-through
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_PAINT:
            self._paint(hwnd)
            return 0
        return self.u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _font(self, height: int, weight: int):
        return self.g32.CreateFontW(-height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")  # CLEARTYPE

    def _paint(self, hwnd):
        ctypes, wt, u32, g32 = self.ctypes, self.wt, self.u32, self.g32

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT), ("fRestore", wt.BOOL),
                        ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]
        ps = PAINTSTRUCT()
        hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            rect = wt.RECT()
            u32.GetClientRect(hwnd, ctypes.byref(rect))
            brush = g32.CreateSolidBrush(0x00241F1C)  # BGR: dark slate
            u32.FillRect(hdc, ctypes.byref(rect), brush)
            g32.DeleteObject(brush)
            g32.SetBkMode(hdc, 1)  # TRANSPARENT
            height = rect.bottom - rect.top
            pad = max(16, height // 9)
            body = wt.RECT(rect.left + pad, rect.top + pad, rect.right - pad, rect.top + int(height * 0.6))
            count = wt.RECT(rect.left + pad, body.bottom, rect.right - pad, rect.bottom - pad // 2)
            for area, words, font, colour, flags in (
                    (body, "\n".join(self.lines), self.font_body, 0x00FFFFFF, 0x0011),  # CENTER|WORDBREAK
                    (count, self.count, self.font_count, 0x0066CCFF, 0x0025)):     # CENTER|VCENTER|SINGLELINE
                old = g32.SelectObject(hdc, font)
                g32.SetTextColor(hdc, colour)
                u32.DrawTextW(hdc, words, -1, ctypes.byref(area), flags)
                g32.SelectObject(hdc, old)
        finally:
            u32.EndPaint(hwnd, ctypes.byref(ps))

    def _bind(self):
        ctypes, wt, u32, k32, g32 = self.ctypes, self.wt, self.u32, self.k32, self.g32
        u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u32.DefWindowProcW.restype = ctypes.c_ssize_t
        u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
        u32.CreateWindowExW.restype = wt.HWND
        u32.RegisterClassExW.argtypes = [ctypes.c_void_p]
        u32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
        u32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
        u32.BeginPaint.restype = wt.HDC
        u32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
        u32.GetClientRect.argtypes = [wt.HWND, ctypes.c_void_p]
        u32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
        u32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HANDLE]
        u32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wt.UINT]
        u32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
        u32.DestroyWindow.argtypes = [wt.HWND]
        u32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        u32.MonitorFromWindow.restype = wt.HANDLE
        u32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
        u32.PeekMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT, wt.UINT]
        u32.TranslateMessage.argtypes = [ctypes.c_void_p]
        u32.DispatchMessageW.argtypes = [ctypes.c_void_p]
        u32.SetWindowRgn.argtypes = [wt.HWND, wt.HANDLE, wt.BOOL]
        g32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        g32.CreateRoundRectRgn.restype = wt.HANDLE
        g32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR]
        g32.CreateFontW.restype = wt.HANDLE
        g32.CreateSolidBrush.argtypes = [wt.COLORREF]
        g32.CreateSolidBrush.restype = wt.HANDLE
        g32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
        g32.SelectObject.restype = wt.HANDLE
        g32.DeleteObject.argtypes = [wt.HANDLE]
        g32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
        g32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k32.GetModuleHandleW.restype = wt.HMODULE

    def _register(self):
        ctypes, wt, u32 = self.ctypes, self.wt, self.u32
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", proc_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                        ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

        def dispatch(hwnd, msg, wparam, lparam):
            current = _NativeOverlay._current
            if current is not None:
                return current._proc(hwnd, msg, wparam, lparam)
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

        if _NativeOverlay._wndproc is None:
            wndproc = proc_type(dispatch)
            klass = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=wndproc,
                                hInstance=self.k32.GetModuleHandleW(None), lpszClassName=self._class_name)
            if not u32.RegisterClassExW(ctypes.byref(klass)) and ctypes.get_last_error() != 1410:  # already there
                raise OSError("RegisterClassExW failed")
            _NativeOverlay._wndproc = wndproc
        _NativeOverlay._current = self

    def run(self, text: str, end: float) -> bool:
        ctypes, wt, u32 = self.ctypes, self.wt, self.u32
        self._bind()
        dpi_old = None
        with contextlib.suppress(Exception):  # sharp text on scaled screens: this thread only, put back afterwards
            u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            dpi_old = u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        hwnd = None
        try:
            self._register()
            left, top, right, bottom = self._area()
            scale = 1.0
            with contextlib.suppress(Exception):
                u32.GetDpiForWindow.argtypes = [wt.HWND]
                scale = max(1.0, (u32.GetDpiForWindow(u32.GetForegroundWindow()) or 96) / 96.0)
            width = int(max(200, min(right - left - 40 * scale, 760 * scale)))
            height = int(max(120, min(bottom - top - 40 * scale, 260 * scale)))
            x = left + (right - left - width) // 2
            y = top + (bottom - top - height) // 2
            self.font_body = self._font(int(27 * scale), 600)
            self.font_count = self._font(int(48 * scale), 700)
            self.lines = [line for line in str(text).splitlines() if line.strip()][:4]
            self.count = f"in {max(1, int(end - time.time() + 0.999))}"
            style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            hwnd = u32.CreateWindowExW(style, self._class_name, "Crosstalk", WS_POPUP, x, y, width, height,
                                       None, None, self.k32.GetModuleHandleW(None), None)
            if not hwnd:
                raise OSError("CreateWindowExW failed")
            radius = int(28 * scale)
            u32.SetWindowRgn(hwnd, self.g32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius), True)
            u32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)  # start invisible, then fade in
            u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)  # shown without taking focus
            shown_at, fade_out_from = time.time(), None
            msg = wt.MSG()
            while True:
                now = time.time()
                while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                if (now >= end or self.abort) and fade_out_from is None:
                    fade_out_from = now
                if fade_out_from is not None:
                    remaining = OVERLAY_FADE_OUT_S - (now - fade_out_from)
                    if remaining <= 0:
                        break
                    alpha = OVERLAY_ALPHA * remaining / OVERLAY_FADE_OUT_S
                else:
                    alpha = OVERLAY_ALPHA * min(1.0, (now - shown_at) / OVERLAY_FADE_IN_S)
                    count = f"in {max(1, int(end - now + 0.999))}"
                    if count != self.count:
                        self.count = count
                        u32.InvalidateRect(hwnd, None, True)
                u32.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, int(alpha))), LWA_ALPHA)
                time.sleep(0.016)
            return True
        finally:
            if hwnd:
                u32.DestroyWindow(hwnd)
            for font in (self.font_body, self.font_count):
                if font:
                    self.g32.DeleteObject(font)
            if _NativeOverlay._current is self:
                _NativeOverlay._current = None
            if dpi_old:
                with contextlib.suppress(Exception):
                    u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(dpi_old))


_REAL_FOCUS = False  # real window handling is opt-in: only the entry points that legitimately move focus switch it on


def enable_real_focus(enabled: bool = True) -> None:
    """Switch real window handling (the overlay and the restore) on for this process. Called by the helper's --watch
    and by the CLI paths that perform a real wake; imports, tests and stray runs never get Win32."""
    global _REAL_FOCUS
    _REAL_FOCUS = bool(enabled)


def default_platform():
    """NullPlatform unless real focus was enabled (enable_real_focus) on Windows. CROSSTALK_P0_FOCUS=off forces it off."""
    if (not _REAL_FOCUS or os.name != "nt"
            or os.environ.get("CROSSTALK_P0_FOCUS", "").strip().lower() in FOCUS_OFF_VALUES):
        return NullPlatform()
    try:
        return Win32Platform()
    except Exception:
        return NullPlatform()


# ------------------------------------------------------- windows the owner must answer
def forward_own_box(title: str, *, platform=None, timeout_s: float = BOX_WAIT_S, step_s: float = BOX_STEP_S,
                    sleep=time.sleep, classes: tuple = (MESSAGE_BOX_CLASS,)) -> str:
    """Bring this process's message box titled `title` to the front once it shows. Run it on a side thread just before
    MessageBoxW: a box opened by a background process (the helper starts the login prompt detached) otherwise stays
    behind other windows even with MB_SETFOREGROUND. "not-shown" when no such box appeared in time. `classes` names
    other window classes to accept (the rename-now notice is a Tk window, class TkTopLevel)."""
    platform = platform or default_platform()
    if not platform.available:
        return "unavailable"
    me = os.getpid()
    for _attempt in range(max(1, int(timeout_s / step_s)) + 1):
        for hwnd, (pid, cls, text) in platform.top_windows().items():
            if pid == me and cls in classes and text == title:
                return platform.bring_forward(hwnd)
        sleep(step_s)
    return "not-shown"


def desktop_main_window(platform=None):
    """The handle of Claude Desktop's main window (a visible Chromium top-level window of a claude.exe process, never a
    console: the Claude CLI is also claude.exe), or None. Prefers the one titled "Claude". Read-only; titles are
    compared in memory only."""
    platform = platform or default_platform()
    if not platform.available:
        return None
    found, exes = None, {}
    for hwnd, (pid, cls, text) in platform.top_windows().items():
        if not cls.startswith(DESKTOP_WINDOW_CLASS) or not text.strip():
            continue
        if pid not in exes:
            exes[pid] = platform.exe(pid)
        if exes[pid] != DESKTOP_EXE:
            continue
        if text.strip() == DESKTOP_WINDOW_TITLE:
            return hwnd
        found = found or hwnd
    return found


def follow_sign_in(before: dict, *, platform=None, timeout_s: float = LOGIN_FOLLOW_S, step_s: float = LOGIN_STEP_S,
                   console_wait_s: float = LOGIN_CONSOLE_WAIT_S, sleep=time.sleep) -> dict:
    """After `claude auth login` is launched: bring its console window to the front, then the browser window once its
    title turns to a Claude or Anthropic page. Each at most once. `before` is top_windows() taken just before the
    launch; only a new window or a title that changed since the last look counts, so windows already open stay put.
    The console is looked for only during the first `console_wait_s`. Stops when the browser came up, the console was
    closed, or at `timeout_s`. Returns how each came forward (None = not seen)."""
    platform = platform or default_platform()
    result = {"console": None, "browser": None}
    if not platform.available:
        return result
    console, exes, last = None, {}, dict(before)
    for attempt in range(max(1, int(timeout_s / step_s))):
        sleep(step_s)
        now = platform.top_windows()
        for hwnd, (pid, cls, text) in now.items():
            if hwnd in last and last[hwnd][2] == text:
                continue  # unchanged since the last look
            if result["console"] is None and cls in CONSOLE_CLASSES and (attempt + 1) * step_s <= console_wait_s:
                console, result["console"] = hwnd, platform.bring_forward(hwnd)
            elif result["browser"] is None and any(word in text.lower() for word in SIGN_IN_TITLE_WORDS):
                if pid not in exes:
                    exes[pid] = platform.exe(pid)
                if exes[pid] in BROWSER_EXES:
                    result["browser"] = platform.bring_forward(hwnd)
        if result["browser"] is not None or (console is not None and console not in now):
            break
        last = now
    return result


# ------------------------------------------------------------------------ log
def log_record(path: Path, record: dict) -> None:
    """Append one bounded JSON line; rotate at LOG_ROTATE_BYTES. Never raises."""
    with contextlib.suppress(OSError, ValueError, TypeError):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, str(path) + ".1")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)[:4000] + "\n")


def notice_seconds(value) -> float:
    """focus_notice_seconds from the helper config: a number of seconds, 0 for no overlay, default 3."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_NOTICE_S
    return min(MAX_NOTICE_S, max(0.0, float(value)))


# ------------------------------------------------------------------ the gate
def _owner_idle(platform) -> bool:
    """True only when the owner's idle time is readable and at least LATE_RESTORE_MIN_IDLE_S (fails closed)."""
    try:
        idle = platform.idle_seconds()
    except Exception:
        return False
    return isinstance(idle, (int, float)) and not isinstance(idle, bool) and idle >= LATE_RESTORE_MIN_IDLE_S


def _launched(result) -> bool:
    """True only when the action clearly reports success. A missing `launched` key is NOT success: every real launcher
    (launch_desktop_show, the Codex link opener) returns {"launched": bool, ...}, so absence means a malformed result."""
    if isinstance(result, dict):
        return bool(result.get("launched", False))
    return bool(result)


def focus_wake(target_key: str, action, confirm, *, text: str, kind: str, controls_check, lock, log_path: Path,
               platform=None, notice_s=DEFAULT_NOTICE_S, restore: bool = True, sleep=time.sleep,
               come_forward_timeout_s: float = COME_FORWARD_TIMEOUT_S, come_forward_step_s: float = COME_FORWARD_STEP_S,
               settle_s: float = SETTLE_S, confirm_timeout_s: float = CONFIRM_TIMEOUT_S,
               confirm_step_s: float = CONFIRM_STEP_S, lock_timeout_s: float = LOCK_TIMEOUT_S,
               awake=None, chat_focus=None, chat_settle_s: float = CHAT_SETTLE_S) -> dict:
    """One focus event for one wake. `action()` starts the wake and returns a dict (key `launched`) or raises;
    `confirm(start_ns)` returns true once Crosstalk itself sees the wake happened. `controls_check(key)` is the
    core's halt/off/deny check, `lock(timeout)` a context manager for the device-wide focus lock. Returns the record
    that was logged plus `action_result` (the action's own return value, not logged). Never retries, never resends.

    `awake()` (optional) returns true when the target is already running: nothing is shown or launched. `chat_focus`
    (optional) has `current()` (the chat selected inside the app, or None) and `select(chat)`: when the wake switches
    the app to another chat, the gate selects the owner's chat again straight away, before the window is given back."""
    started = time.monotonic()
    record = {"tag": TAG, "ts_ns": time.time_ns(), "target": target_key, "kind": kind, "refused": None,
              "overlay": False, "overlay_shown": False, "focus_moved": False,
              "overlay_without_move": False, "chat_moved": False, "launched": False, "came_forward": False, "restored": False,
              "restore_method": None, "chat_restored": None, "chat_restore_skipped": None, "confirmed": False,
              "error": None, "reason": None, "late_forward": False}

    def chat_now():
        try:
            return chat_focus.current() if chat_focus is not None else None
        except Exception:
            return None
    platform = platform if platform is not None else default_platform()
    result = None

    def finish() -> dict:
        if record["overlay"]:
            with contextlib.suppress(Exception):
                platform.close_overlay()  # nothing lingers once the gate is done
        record["overlay_shown"] = bool(record["overlay"])
        record["focus_moved"] = bool(record["came_forward"] or record["chat_moved"] or record["restored"])
        record["overlay_without_move"] = record["overlay_shown"] and not record["focus_moved"]
        record["elapsed_s"] = round(time.monotonic() - started, 2)
        log_record(log_path, record)
        return dict(record, action_result=result)

    try:
        context = lock(lock_timeout_s)
        context.__enter__()
    except TimeoutError:
        record["refused"] = "focus-lock-busy"
        return finish()
    except Exception as error:
        record["refused"] = "focus-lock-error:" + type(error).__name__
        return finish()
    try:
        refusal = controls_check(target_key)
        if refusal:
            record.update(refused=refusal, reason="controls-before-overlay")
            return finish()
        if awake is not None:
            try:
                running = bool(awake())
            except Exception:
                running = False
            if running:  # a running chat talks in the background: no overlay, no focus, nothing launched
                record.update(confirmed=True, reason="already-awake")
                return finish()
        prev = platform.foreground() if platform.available else {"hwnd": 0, "pid": 0}
        record["before"] = {"hwnd": prev["hwnd"], "pid": prev["pid"]}
        chat_before = chat_now()
        seconds = notice_seconds(notice_s)
        if seconds > 0 and platform.available:
            record["overlay"] = bool(platform.overlay(text, seconds))
        refusal = controls_check(target_key)  # immediately before acting: a halt during the countdown still stops it
        if refusal:
            record.update(refused=refusal, reason="controls-at-action")
            return finish()
        start_ns = time.time_ns()
        try:
            result = action()
        except Exception as error:
            record["error"] = "action-failed:" + type(error).__name__
            result = {"launched": False, "error": type(error).__name__}
        record["launched"] = _launched(result)
        if not record["launched"]:
            record["error"] = record["error"] or "not-launched"
            return finish()
        came, chat_moved = None, False
        watch_window = platform.available and bool(prev["hwnd"])

        def restore_chat() -> None:
            """Put the owner back on the chat they were reading. Controls are rechecked right before the select (a
            halt or off since the last check skips it); an unchanged chat needs nothing."""
            chat_after = chat_now()
            if not restore:
                record["chat_restored"] = False
            elif chat_after and chat_after != chat_before:
                refusal_now = controls_check(target_key)
                if refusal_now:
                    record["chat_restored"] = False
                    record["chat_restore_skipped"] = "controls:" + str(refusal_now)
                    return
                try:  # put the owner back on the chat they were reading before the window goes back
                    chat_focus.select(chat_before)
                    record["chat_restored"] = True
                    sleep(max(0.0, chat_settle_s))
                except Exception as error:
                    record["chat_restored"] = False
                    record["error"] = "chat-restore-failed:" + type(error).__name__
            else:
                record["chat_restored"] = bool(record["chat_restored"])  # an earlier restore stays recorded

        def restore_window(late: bool) -> None:
            current = platform.foreground()
            if not restore:
                record["reason"] = "restore-off"
            elif not platform.exists(prev["hwnd"], prev.get("pid")):
                record["reason"] = "previous-window-gone"
            elif current["hwnd"] != came["hwnd"]:
                record["reason"] = "owner-switched-windows"  # someone chose another window: leave it alone
            elif late and not _owner_idle(platform):
                record["reason"] = "late-restore-owner-active"  # may be typing into this window: leave it alone
            else:
                record["restore_method"] = platform.restore(prev, current)
                record["restored"] = record["restore_method"] in ("plain", "attach")
                if record["restore_method"] == "gone":
                    record["reason"] = "previous-window-gone"

        if watch_window or chat_before:
            # Wait for the wake to show: another window comes forward, or (owner already in the app) the app
            # switches its selected chat. Either one ends the wait.
            for _ in range(max(1, round(come_forward_timeout_s / max(come_forward_step_s, 0.001)))):
                if watch_window:
                    now = platform.foreground()
                    if now["hwnd"] and now["hwnd"] != prev["hwnd"]:
                        came = now
                if chat_before:
                    chat_after = chat_now()
                    chat_moved = bool(chat_after) and chat_after != chat_before
                    record["chat_moved"] = record["chat_moved"] or chat_moved
                if came is not None or chat_moved:
                    break
                sleep(come_forward_step_s)
            record["came_forward"] = came is not None
            if came is not None or chat_moved:
                sleep(max(0.0, settle_s))
            if chat_before:
                restore_chat()
            if came is not None:
                restore_window(False)

        def late_check() -> None:
            """The wake can come forward after the come-forward wait (a cold `--resume`): keep watching the foreground
            through the confirm phase and give focus back once, the same way, when a different window shows up."""
            nonlocal came
            if came is not None or not watch_window:
                return
            now = platform.foreground()
            if not now["hwnd"] or now["hwnd"] == prev["hwnd"]:
                return
            came = now
            record["came_forward"] = True
            record["late_forward"] = True
            sleep(max(0.0, settle_s))
            if chat_before:
                restore_chat()
            restore_window(True)

        for _ in range(max(1, round(confirm_timeout_s / max(confirm_step_s, 0.001)))):
            sleep(confirm_step_s)
            late_check()
            try:
                if confirm(start_ns):
                    record["confirmed"] = True
                    break
            except Exception as error:
                record["error"] = "confirm-failed:" + type(error).__name__
                break
        late_check()  # one last look: the window may have come forward just as the confirm ended
        return finish()
    finally:
        with contextlib.suppress(Exception):
            context.__exit__(None, None, None)
