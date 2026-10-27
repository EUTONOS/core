"""Read-only desktop readiness and explicit, session-scoped screen-saver recovery."""
import ctypes
from ctypes import wintypes as w
import json
import subprocess
import time


def probe():
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.GetThreadDesktop.argtypes = [w.DWORD]
    u.GetThreadDesktop.restype = w.HANDLE
    u.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    u.OpenInputDesktop.restype = w.HANDLE
    u.CloseDesktop.argtypes = [w.HANDLE]
    u.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
    def name(handle):
        buf = ctypes.create_unicode_buffer(256)
        needed = w.DWORD()
        return buf.value if u.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)) else None
    running = w.BOOL()
    ok = u.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0)
    current = name(u.GetThreadDesktop(k.GetCurrentThreadId()))
    handle = u.OpenInputDesktop(0, False, 1)
    try:
        active = name(handle) if handle else None
    finally:
        if handle:
            u.CloseDesktop(handle)
    point = w.POINT()
    cursor_ok = bool(u.GetCursorPos(ctypes.byref(point)))
    return {"screensaver_running": bool(running.value) if ok else None,
            "thread_desktop": current, "input_desktop": active,
            "input_ready": bool(ok and not running.value and current == active == "Default" and cursor_ok)}


def stop_tasks():
    # No caller-supplied process names, PID interpolation, elevation, or session changes.
    script = r'''$ErrorActionPreference = 'Stop'
$session = (Get-Process -Id $PID).SessionId
$stopped = @()
Get-Process | Where-Object { $_.SessionId -eq $session } | ForEach-Object {
    $candidate = $_
    if ($candidate.Path -and [IO.Path]::GetExtension($candidate.Path) -ieq '.scr') {
        $candidate.Kill()
        $candidate.WaitForExit(3000) | Out-Null
        $stopped += $candidate.Id
    }
}
ConvertTo-Json -Compress -InputObject $stopped
'''
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


def recover(probe_fn=probe, stop_fn=stop_tasks, wait_fn=time.sleep):
    before = probe_fn()
    if before["screensaver_running"] is not True:
        return {"before": before, "after": before, "stopped_pids": [], "recovered": before["input_ready"]}
    stopped = stop_fn()
    after = probe_fn()
    for _ in range(10):
        if after["input_ready"]:
            break
        wait_fn(0.2)
        after = probe_fn()
    return {"before": before, "after": after, "stopped_pids": stopped, "recovered": after["input_ready"]}
