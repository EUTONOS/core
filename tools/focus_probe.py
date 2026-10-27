"""Focus-invariance probe (AWR-X stress item S-13).

Cross-app messaging must never move the owner's foreground window, selected chat, or
clipboard -- but until now that was only ever an assumption, never measured.
This tool takes a small, read-only snapshot of exactly the signals that would
change if something silently stole focus or touched the clipboard, and
compares two such snapshots.

It never reads clipboard *contents* -- only the clipboard sequence number
(``GetClipboardSequenceNumber``), which Windows increments every time the
clipboard's content changes, regardless of what that content is.

Usage: python tools/focus_probe.py snapshot --out before.json
       ...do something that might touch focus or the clipboard...
       python tools/focus_probe.py snapshot --out after.json
       python tools/focus_probe.py compare before.json after.json
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


WINDOWS = sys.platform == "win32"

if WINDOWS:
    USER32 = ctypes.WinDLL("user32", use_last_error=True)
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    USER32.GetForegroundWindow.restype = wintypes.HWND
    USER32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    USER32.GetWindowTextLengthW.restype = ctypes.c_int
    USER32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    USER32.GetWindowTextW.restype = ctypes.c_int
    USER32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
    USER32.GetClipboardSequenceNumber.argtypes = ()
    USER32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    KERNEL32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    KERNEL32.OpenProcess.restype = wintypes.HANDLE
    KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    KERNEL32.CloseHandle.restype = wintypes.BOOL
    KERNEL32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ------------------------------------------------------------- win32 reads

def _window_title(hwnd: int) -> str:
    length = USER32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    USER32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_exe_name(pid: int) -> str:
    """Best-effort short exe name (e.g. ``claude.exe``), never the full path --
    the probe only needs to say *which app* is in front, not where it lives."""
    if not pid:
        return ""
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if not KERNEL32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.rsplit("\\", 1)[-1]
    finally:
        KERNEL32.CloseHandle(handle)


def _read_foreground_window_info() -> dict:
    """Read-only: no focus change, no accessibility read, no typing."""
    hwnd = USER32.GetForegroundWindow()
    pid = _window_pid(hwnd)
    return {"hwnd": int(hwnd), "title": _window_title(hwnd), "pid": pid,
            "exe_name": _process_exe_name(pid)}


def _read_clipboard_sequence() -> int:
    """Never opens or reads the clipboard itself -- just the counter Windows
    bumps on every content change, regardless of what changed."""
    return int(USER32.GetClipboardSequenceNumber())


# ------------------------------------------------------------------ snapshot

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def take_snapshot() -> dict:
    """One read-only snapshot of the signals a stray focus/clipboard touch
    would move. On non-Windows this cleanly reports ``supported: false``
    instead of raising or guessing."""
    if not WINDOWS:
        return {"supported": False, "timestamp": _timestamp()}
    return {
        "supported": True,
        "timestamp": _timestamp(),
        "foreground": _read_foreground_window_info(),
        "clipboard_sequence": _read_clipboard_sequence(),
    }


# -------------------------------------------------------------------- diff

def _diff(a: dict, b: dict, prefix: str = "") -> list[str]:
    """Dotted-path field names present in either dict whose values differ.
    Recurses one level into nested dicts (``foreground.*``); anything else is
    compared by plain equality. Order is deterministic (sorted key order) so
    the caller's output is stable across runs."""
    changed: list[str] = []
    for key in sorted(set(a) | set(b)):
        path = f"{prefix}{key}"
        va = a.get(key, "<missing>")
        vb = b.get(key, "<missing>")
        if isinstance(va, dict) and isinstance(vb, dict):
            changed.extend(_diff(va, vb, prefix=f"{path}."))
        elif va != vb:
            changed.append(path)
    return changed


def compare_snapshots(a: dict, b: dict) -> dict:
    """Compare two ``take_snapshot()`` results. ``timestamp`` is always
    ignored -- two snapshots taken moments apart are expected to differ
    there even when nothing else moved. Pure: takes plain dicts, makes no
    win32 calls, so it needs no mocking to test."""
    a_body = {k: v for k, v in a.items() if k != "timestamp"}
    b_body = {k: v for k, v in b.items() if k != "timestamp"}
    fields = _diff(a_body, b_body)
    return {"changed": bool(fields), "fields": fields}


# --------------------------------------------------------------------- CLI

def _load_snapshot(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_snapshot(args) -> int:
    snapshot = take_snapshot()
    text = json.dumps(snapshot, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def cmd_compare(args) -> int:
    a = _load_snapshot(args.a)
    b = _load_snapshot(args.b)
    result = compare_snapshots(a, b)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["changed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="Take one read-only focus/clipboard snapshot")
    snap.add_argument("--out", type=str, default=None, help="Write JSON here instead of stdout")
    snap.set_defaults(func=cmd_snapshot)

    comp = sub.add_parser("compare", help="Compare two snapshot files")
    comp.add_argument("a", help="Earlier snapshot JSON file")
    comp.add_argument("b", help="Later snapshot JSON file")
    comp.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
