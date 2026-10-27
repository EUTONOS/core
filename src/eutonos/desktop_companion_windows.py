"""Windows geometry, appearance and read-only process/window observation."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
PROBE = Path(__file__).with_name("desktop_companion_probe.ps1")
TRANSPARENT = "#ff00ff"
MOVING_INTERVAL_MS = 16
BACKGROUND_INTERVAL_MS = 100
COLLAPSED_LIFT = 30
PREFERENCES_KEY = r"Software\EUTONOS\DesktopCompanion"
COLORS = {
    "dark": {"bg": "#202124", "surface": "#2b2c2f", "text": "#f5f5f5", "muted": "#abaeb2", "track": "#3b3d40", "accent": "#57b7b3", "border": "#414349"},
    "light": {"bg": "#f8f8f7", "surface": "#ffffff", "text": "#242424", "muted": "#717171", "track": "#e0e2e2", "accent": "#17837d", "border": "#e8e8e7"},
}
POPUP_COLORS = {**COLORS["light"], "bg": "#ffffff", "surface": "#ffffff", "border": "#9ca3af"}

if sys.platform == "win32":
    USER32 = ctypes.WinDLL("user32", use_last_error=True)
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    KERNEL32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    KERNEL32.CreateMutexW.restype = wintypes.HANDLE
    KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    KERNEL32.CloseHandle.restype = wintypes.BOOL
    USER32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    USER32.FindWindowW.restype = wintypes.HWND
    USER32.GetForegroundWindow.restype = wintypes.HWND
    USER32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    USER32.GetWindowRect.restype = wintypes.BOOL
    USER32.IsIconic.argtypes = (wintypes.HWND,)
    USER32.IsIconic.restype = wintypes.BOOL
    USER32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    USER32.SetWindowPos.restype = wintypes.BOOL
    USER32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    USER32.GetAncestor.restype = wintypes.HWND
    USER32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    USER32.GetWindow.restype = wintypes.HWND
    USER32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    USER32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    USER32.GetDpiForWindow.argtypes = (wintypes.HWND,)
    USER32.GetDpiForWindow.restype = wintypes.UINT
    USER32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    USER32.MonitorFromWindow.restype = wintypes.HANDLE

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    USER32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
    USER32.GetMonitorInfoW.restype = wintypes.BOOL
    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    HWND_TOP = wintypes.HWND(0)
    SWP_NOACTIVATE = 0x0010
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOOWNERZORDER = 0x0200
    GW_HWNDPREV = 3
    GA_ROOT = 2

def appearance_mode() -> str:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PREFERENCES_KEY) as key:
                value, _ = winreg.QueryValueEx(key, "Appearance")
            if value in ("system", "light", "dark"):
                return value
        except OSError:
            pass
    return "system"

def set_appearance_mode(value: str) -> None:
    if value not in ("system", "light", "dark"):
        raise ValueError("Unknown appearance")
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PREFERENCES_KEY) as key:
        winreg.SetValueEx(key, "Appearance", 0, winreg.REG_SZ, value)

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

def window_rect(handle: int) -> tuple[int, int, int, int] | None:
    if not handle or USER32.IsIconic(handle):
        return None
    rect = wintypes.RECT()
    if not USER32.GetWindowRect(handle, ctypes.byref(rect)):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect.left, rect.top, rect.right, rect.bottom

def monitor_work_rect(handle: int) -> tuple[int, int, int, int]:
    monitor = USER32.MonitorFromWindow(handle, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if monitor and USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1)

class Probe:
    def __init__(self):
        self.messages: queue.Queue[dict] = queue.Queue(maxsize=3)
        self.process = None
        self.reader = None
        self.reader_error = None
        self.last_start = float("-inf")
        self.ensure_running()

    def ensure_running(self):
        if self.process is not None and self.process.poll() is None:
            if self.reader is not None and self.reader.is_alive():
                return True
            # A live subprocess does not prove its observation pipe is being read.
            # Reap a failed/EOF reader's producer and retry through the same backoff.
            try:
                self.process.terminate()
            except OSError:
                pass
            return False
        if time.monotonic() - self.last_start < 5:
            return False
        self.last_start = time.monotonic()
        self.messages = queue.Queue(maxsize=3)
        self.reader_error = None
        try:
            self.process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(PROBE), "-ParentProcessId", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError:
            self.process = None
            return False
        self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
        self.reader.start()
        # Clear stale UI state until the restarted process identifies a chat.
        return False

    def _read(self, process):
        failure = "probe-stream-ended"
        try:
            if process.stdout is None:
                raise OSError("Missing observation stream")
            for line in process.stdout:
                if process is not self.process:
                    return
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        continue
                    while self.messages.full():
                        self.messages.get_nowait()
                    self.messages.put_nowait(value)
                except (json.JSONDecodeError, queue.Empty, queue.Full):
                    continue
        except Exception as error:
            # Metadata only: never retain subprocess lines or exception text.
            failure = "probe-reader-failed:" + type(error).__name__
        finally:
            if process is self.process:
                self.reader_error = failure
                try:
                    while self.messages.full():
                        self.messages.get_nowait()
                    self.messages.put_nowait({"windows": [], "foreground": 0, "error": failure})
                except (queue.Empty, queue.Full):
                    pass

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

def collapsed_anchor(rect, size):
    left, top, right, bottom = rect
    width, height = size
    return max(left, right - width // 2), max(top, bottom - height - 9 - COLLAPSED_LIFT)


def stack_above_owner(own, owner):
    """Insert beside the owner without raising it or crossing unrelated windows."""
    previous = USER32.GetWindow(owner, GW_HWNDPREV)
    if previous == own:
        return True
    flags = SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE | SWP_NOOWNERZORDER
    return bool(USER32.SetWindowPos(own, previous or HWND_TOP, 0, 0, 0, 0, flags))


class WindowPrimitives:
    def __init__(self, master):
        self.root = tk.Toplevel(master)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", False)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.resizable(False, False)
        self.expanded = False
        self.dpi_scale = max(1.0, USER32.GetDpiForWindow(self.root.winfo_id()) / 96)
        self.window_size = self.geometry_size = (36, 36)
        self.theme_mode = appearance_mode()
        self.theme = system_theme() if self.theme_mode == "system" else self.theme_mode
        self.menu = None
        self.last_rect = None
        self.last_motion = 0.0
        self.codex_was_foreground = False
        self.window_owner = None
        self.popup_offset = (0, 0)
        self.drag_origin = None
        from .desktop_companion_runtime import startup_enabled
        self.startup_var = tk.BooleanVar(master=self.root, value=startup_enabled())
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    @property
    def palette(self):
        return POPUP_COLORS if getattr(self, "expanded", False) else COLORS[self.theme]

    @staticmethod
    def _rounded_fill(canvas, left, top, right, bottom, radius, color):
        canvas.create_rectangle(left + radius, top, right - radius, bottom,
                                fill=color, outline="", tags="surface")
        canvas.create_rectangle(left, top + radius, right, bottom - radius,
                                fill=color, outline="", tags="surface")
        for x in (left, right - 2 * radius):
            for y in (top, bottom - 2 * radius):
                canvas.create_oval(x, y, x + 2 * radius, y + 2 * radius,
                                   fill=color, outline="", tags="surface")

    def _draw_popup_surface(self, event):
        canvas = event.widget
        width, height = event.width, event.height
        canvas.delete("surface")
        if width < 30 or height < 30:
            return
        # Draw the border as a larger rounded shape behind a white inset.
        self._rounded_fill(canvas, 0, 0, width, height, 14,
                           POPUP_COLORS["border"])
        self._rounded_fill(canvas, 1, 1, width - 1, height - 1, 13,
                           POPUP_COLORS["bg"])

    def _rebuild(self):
        # A hidden resize prevents the expanded panel from flashing at the
        # collapsed icon's old lower-right screen position.
        was_visible = self.root.state() != "withdrawn"
        self.root.withdraw()
        self._build()
        self._position()
        self.root.update_idletasks()
        if was_visible and self.handle and self.title and window_rect(self.handle):
            self.root.deiconify()

    def _menu(self, anchor=None):
        """Toggle a small pop-up menu under the hamburger; the panel is untouched."""
        if self.menu is not None:
            self._close_menu()
            return
        p = self.palette
        menu = tk.Toplevel(self.root)
        menu.withdraw()
        menu.overrideredirect(True)
        menu.attributes("-topmost", True)
        menu.configure(bg=p["border"])
        body = tk.Frame(menu, bg=p["bg"])
        body.pack(padx=1, pady=1)
        for entry in self._menu_entries():
            if entry[0] == "header":
                tk.Label(body, text=entry[1], anchor="w", bg=p["bg"], fg=p["muted"],
                         font=("Segoe UI", 8, "bold")).pack(fill="x", padx=12, pady=(6, 2))
            elif entry[0] == "separator":
                tk.Frame(body, bg=p["track"], height=1).pack(fill="x", padx=8, pady=3)
            else:
                self._menu_row(body, *entry[1:])
        self.menu = menu
        menu.update_idletasks()
        width, height = max(190, menu.winfo_reqwidth()), menu.winfo_reqheight()
        left, top = self.root.winfo_rootx() + 12, self.root.winfo_rooty() + 40
        if anchor is not None:
            left, top = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height() + 4
        left = min(max(0, left), max(0, self.root.winfo_screenwidth() - width))
        top = min(max(0, top), max(0, self.root.winfo_screenheight() - height))
        menu.geometry(f"{width}x{height}+{left}+{top}")
        menu.deiconify()
        menu.lift()
        menu.bind("<Escape>", lambda _event: self._close_menu())
        menu.bind("<Button-1>", self._menu_press)
        menu.bind("<FocusOut>", lambda _event: self.root.after(60, self._menu_focus_lost))
        try:
            menu.focus_force()
            menu.grab_set()
        except tk.TclError:
            pass

    def _menu_press(self, event):
        menu = self.menu
        if menu is None:
            return
        inside = (menu.winfo_rootx() <= event.x_root < menu.winfo_rootx() + menu.winfo_width()
                  and menu.winfo_rooty() <= event.y_root < menu.winfo_rooty() + menu.winfo_height())
        if not inside:
            self._close_menu()

    def _menu_focus_lost(self):
        menu = self.menu
        try:
            if menu is not None and menu.winfo_exists() and menu.focus_displayof() is None:
                self._close_menu()
        except tk.TclError:
            self._close_menu()

    def _close_menu(self):
        menu, self.menu = getattr(self, "menu", None), None
        if menu is None:
            return
        try:
            menu.grab_release()
        except tk.TclError:
            pass
        try:
            menu.destroy()
        except tk.TclError:
            pass

    def _menu_row(self, parent, text, selected, action):
        p = self.palette
        row = tk.Frame(parent, bg=p["bg"], cursor="hand2", height=25)
        row.pack(fill="x", padx=4, pady=1)
        row.pack_propagate(False)
        marker = tk.Label(row, text="✓" if selected else "", width=2, anchor="w",
                          bg=p["bg"], fg=p["muted"], font=("Segoe UI", 9))
        marker.pack(side="left", padx=(8, 4))
        label = tk.Label(row, text=text, anchor="w", bg=p["bg"], fg=p["text"],
                         font=("Segoe UI", 9))
        label.pack(side="left", fill="x", expand=True, padx=(0, 12))
        widgets = (row, marker, label)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _event, callback=action: self._menu_choose(callback))
            widget.bind("<Enter>", lambda _event, group=widgets: [w.configure(bg=p["track"]) for w in group])
            widget.bind("<Leave>", lambda _event, group=widgets: [w.configure(bg=p["bg"]) for w in group])

    def _menu_choose(self, callback):
        self._close_menu()
        callback()

    def _set_appearance(self, value):
        try:
            set_appearance_mode(value)
        except OSError:
            return
        self.theme_mode = value
        self.theme = system_theme() if value == "system" else value
        # The expanded panel uses the fixed popup palette; only the collapsed
        # icon follows the appearance, and _expand(False) rebuilds it.
        self._redraw_menu_icon()

    def _expand(self, value):
        self.expanded = value
        self._close_menu()
        self._rebuild()

    def _drag_start(self, event):
        if not self.expanded:
            return
        self.drag_origin = (event.x_root, event.y_root,
                            self.root.winfo_rootx(), self.root.winfo_rooty())

    def _drag_move(self, event):
        if not self.expanded or self.drag_origin is None:
            return
        rect = window_rect(self.handle)
        if not rect:
            return
        mouse_x, mouse_y, initial_x, initial_y = self.drag_origin
        x = initial_x + event.x_root - mouse_x
        y = initial_y + event.y_root - mouse_y
        work_left, work_top, work_right, work_bottom = monitor_work_rect(self.handle)
        width, height = self.window_size
        x = min(max(work_left, x), max(work_left, work_right - width))
        y = min(max(work_top, y), max(work_top, work_bottom - height))
        anchor_x, anchor_y = self._expanded_anchor(rect)
        self.popup_offset = (x - anchor_x, y - anchor_y)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.last_motion = time.monotonic()

    def _drag_end(self, _event):
        self.drag_origin = None

    def _expanded_anchor(self, rect):
        left, top, right, bottom = rect
        width, height = self.window_size
        return max(left, right - width - 12), max(top, bottom - height - 70)

    def _position(self):
        rect = window_rect(self.handle)
        if not rect:
            self.root.withdraw()
            return
        target_dpi = USER32.GetDpiForWindow(self.handle)
        restore_visibility = False
        if target_dpi and abs(target_dpi / 96 - self.dpi_scale) >= 0.05:
            restore_visibility = self.root.state() != "withdrawn"
            self.root.withdraw()
            self.dpi_scale = target_dpi / 96
            self._build()
        width, height = self.window_size
        left, top, right, bottom = rect
        if self.expanded:
            anchor_x, anchor_y = self._expanded_anchor(rect)
            x = anchor_x + self.popup_offset[0]
            y = anchor_y + self.popup_offset[1]
        else:
            # Straddle the app's right border, away from its send button.
            x, y = collapsed_anchor(rect, self.window_size)
        # Clamp to the monitor carrying this app window, including monitors
        # left of or above the primary display.
        work_left, work_top, work_right, work_bottom = monitor_work_rect(self.handle)
        x = min(max(work_left, x), max(work_left, work_right - width))
        y = min(max(work_top, y), max(work_top, work_bottom - height))
        self.root.geometry(f"{self.geometry_size[0]}x{self.geometry_size[1]}+{x}+{y}")
        self.last_rect = rect
        if restore_visibility:
            self.root.update_idletasks()
            self.root.deiconify()

    def _toggle_startup(self):
        from .desktop_companion_runtime import set_startup
        enabled = not bool(self.startup_var.get())
        try:
            set_startup(enabled, self.coordinator.options)
        except OSError:
            return
        self.startup_var.set(enabled)
