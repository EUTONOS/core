"""Crosstalk rename tool (slice 3B.10, stdlib only): give a Claude Desktop chat a real name on the owner's phone.

Why: a chat's Remote Control name on claude.ai changes only when the name is changed through Desktop's own Rename
control with text that differs from the current title. `set_session_title` (titleSource "tool"), turning Remote
Control off and on, copying record files and an unchanged Enter do not reach the phone. A chat whose link was created
before it had a title keeps a random slug there until a real rename.

What it does: `Renamer.rename(local_id, new_title)` runs `crosstalk_rename.ps1`, which uses UI Automation on
Desktop's sidebar ("More options for <title>" -> Rename -> the "Session name" box) and types the new name into that
verified box. It saves the name with titleSource "user". Push semantics: when the new title equals the chat's current
title, it first renames to "<title> ·" (space, middle dot) and then back, because an unchanged name does not reach the
phone; it stops between the two steps on any failure and says honestly which title is left.

Guards (nothing is touched unless all hold; each is also checked again between the two steps of a push):
  * the kill switch CROSSTALK_P0_RENAME is not off; Crosstalk is not halted (messaging off does not matter: renaming is not a letter);
  * Claude Desktop is already the foreground window (this tool never brings it forward);
  * the owner has been idle for the threshold (GetLastInputInfo, default 15 s); between the two steps of a push, no
    owner input since the tool's own last keystroke. The helper's rename-now pass (right after a Desktop load, with
    the owner told and Desktop brought forward by the helper) passes `since_tick` to `rename`, which uses that same
    "no owner input since" test for the first step too, and reads the tick back from `result["input_tick"]` for the
    next chat; every other guard is unchanged;
  * inside the script: the foreground window is Desktop's main window and the keyboard focus is the "Session name"
    edit, checked right before Ctrl+A, before typing and before Enter, and the edit holds the expected text before
    Enter. A failed guard sends only {ESCAPE}, and only while the focus is still in that edit.
It never reads, writes or uses the clipboard and never types anywhere else. After the rename it reads the chat's
Desktop record (title, titleSource and a few flags; never a credential) and requires title == new and titleSource ==
"user". If opening the sidebar menu switched which chat Desktop shows, it selects the previous chat again.

CLI:  python crosstalk_rename.py --session local_<id> [--title TEXT] [--dry-run] [--min-idle SECONDS]
(no --title means "push the current title"). `--dry-run` prints the guard results and the plan and touches nothing.
The Windows behaviour lives behind `Win32Platform`; tests use a fake of the same shape.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
TAG = "crosstalk.rename.v1"
SCRIPT = HERE / "crosstalk_rename.ps1"
LOCAL_ID_RE = re.compile(r"local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
DESKTOP_EXE = "claude.exe"
RENAME_OFF_VALUES = ("off", "0", "false", "no")   # the same value set as crosstalk_focus.FOCUS_OFF_VALUES
FOCUS_OFF_VALUES = RENAME_OFF_VALUES
DEFAULT_MIN_IDLE_S = 15.0
# The owner-idle guard can never be set below this. 2 s is the order of one typing pause: below it the guard would
# pass while the owner is mid-sentence (0 turned it off entirely). Far under the 15 s default, so config still tunes it.
MIN_IDLE_FLOOR_S = 2.0


def clamp_min_idle(value) -> float:
    """A usable idle threshold: at least MIN_IDLE_FLOOR_S; anything that is not a finite number is the default."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MIN_IDLE_S
    if isinstance(value, bool) or not math.isfinite(number):
        return DEFAULT_MIN_IDLE_S
    return max(MIN_IDLE_FLOOR_S, number)
TEMP_SUFFIX = " ·"
MAX_TITLE_CHARS = 200
SCRIPT_TIMEOUT_S = 60.0
VERIFY_TIMEOUT_S = 10.0
VERIFY_STEP_S = 0.5
SETTLE_S = 0.8
LOG_ROTATE_BYTES = 1_000_000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def rename_off() -> bool:
    """Kill switch: CROSSTALK_P0_RENAME=off (or 0, false, no)."""
    return os.environ.get("CROSSTALK_P0_RENAME", "").strip().lower() in RENAME_OFF_VALUES


# ------------------------------------------------------------------ platforms
class NullPlatform:
    """No window handling (not Windows, or CROSSTALK_P0_FOCUS=off): every window guard fails, nothing is touched."""
    available = False

    def foreground(self) -> dict:
        return {"hwnd": 0, "pid": 0, "exe": ""}

    def idle_seconds(self):
        return None

    def last_input_tick(self):
        return None


class Win32Platform(NullPlatform):
    """Foreground window (handle, pid, exe name) and the time of the owner's last input, through ctypes."""
    available = True

    def __init__(self):
        import ctypes
        import ctypes.wintypes as wintypes
        self.ctypes, self.wt = ctypes, wintypes
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u32.GetForegroundWindow.restype = wintypes.HWND
        self.u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.GetTickCount.restype = wintypes.DWORD

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        self._info_type = LASTINPUTINFO

    def _exe(self, pid: int) -> str:
        handle = self.k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf, size = self.ctypes.create_unicode_buffer(520), self.wt.DWORD(520)
            ok = self.k32.QueryFullProcessImageNameW(handle, 0, buf, self.ctypes.byref(size))
            return os.path.basename(buf.value).lower() if ok else ""
        finally:
            self.k32.CloseHandle(handle)

    def foreground(self) -> dict:
        hwnd = self.u32.GetForegroundWindow()
        if not hwnd:
            return {"hwnd": 0, "pid": 0, "exe": ""}
        pid = self.wt.DWORD()
        self.u32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return {"hwnd": int(hwnd), "pid": int(pid.value), "exe": self._exe(int(pid.value))}

    def last_input_tick(self):
        info = self._info_type()
        info.cbSize = self.ctypes.sizeof(self._info_type)
        if not self.u32.GetLastInputInfo(self.ctypes.byref(info)):
            return None
        return int(info.dwTime)

    def idle_seconds(self):
        tick = self.last_input_tick()
        if tick is None:
            return None
        return ((int(self.k32.GetTickCount()) - tick) & 0xFFFFFFFF) / 1000.0


def default_platform():
    """Win32 on Windows unless window handling is off (CROSSTALK_P0_FOCUS=off, the diagnostic and test switch)."""
    if os.name != "nt" or os.environ.get("CROSSTALK_P0_FOCUS", "").strip().lower() in FOCUS_OFF_VALUES:
        return NullPlatform()
    try:
        return Win32Platform()
    except Exception:
        return NullPlatform()


# --------------------------------------------------------------- the script
def run_script(current: str, new: str, timeout_s: float = SCRIPT_TIMEOUT_S) -> dict:
    """Run crosstalk_rename.ps1 for ONE rename. Titles go over as UTF-8 base64. Returns the script's one JSON line
    (outcome, step, reason) plus `exit`; never raises. A timeout or a missing result is `outcome: error`."""
    def b64(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-CurrentB64", b64(current), "-NewB64", b64(new)]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"outcome": "error", "step": "script", "reason": "timeout", "exit": None}
    except OSError as error:
        return {"outcome": "error", "step": "script", "reason": type(error).__name__, "exit": None}
    for line in reversed((done.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                value = json.loads(line)
                if isinstance(value, dict) and "outcome" in value:
                    return {"outcome": str(value.get("outcome")), "step": str(value.get("step")),
                            "reason": str(value.get("reason") or ""), "exit": done.returncode}
    return {"outcome": "error", "step": "script", "reason": "no-result", "exit": done.returncode}


# ---------------------------------------------------------------- the renamer
def temp_title(title: str) -> str:
    """The temporary name of a push: the title with " ·" added (or removed when it already ends that way)."""
    if title.endswith(TEMP_SUFFIX) and len(title) > len(TEMP_SUFFIX):
        return title[:-len(TEMP_SUFFIX)]
    return title + TEMP_SUFFIX


def log_record(path: Path | None, record: dict) -> None:
    """Append one bounded JSON line (ids and outcomes, never titles); rotate at 1 MB. Never raises."""
    if path is None:
        return
    with contextlib.suppress(OSError, ValueError, TypeError):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, str(path) + ".1")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)[:4000] + "\n")


class Renamer:
    """One rename (or push) at a time. Every outside effect is injected: `platform` (window facts), `records`
    (local id -> {title, titleSource, remote, archived, scheduled, session} or None), `run_script`, `chat_focus`
    (`current()` / `select(local_id)`), `controls_check` (-> None or the reason Crosstalk is halted or the chat denied) and `sleep`."""

    def __init__(self, *, platform=None, records=None, run_script=run_script, chat_focus=None, controls_check=None,
                 sleep=time.sleep, min_idle_s: float = DEFAULT_MIN_IDLE_S, log_path: Path | None = None,
                 verify_timeout_s: float = VERIFY_TIMEOUT_S, verify_step_s: float = VERIFY_STEP_S,
                 settle_s: float = SETTLE_S):
        self.platform = platform if platform is not None else default_platform()
        self.records = records or (lambda local_id: None)
        self.run_script = run_script
        self.chat_focus = chat_focus
        self.controls_check = controls_check
        self.sleep = sleep
        self.min_idle_s = clamp_min_idle(min_idle_s)
        self.log_path = log_path
        self.verify_timeout_s, self.verify_step_s, self.settle_s = verify_timeout_s, verify_step_s, settle_s

    # ---- guards
    def guards(self, since_tick=None) -> dict:
        """Evaluate every guard (no short circuit, so a dry run shows them all). `since_tick` is the owner-input
        clock reading taken right after this tool's own last keystroke: with it the idle test becomes "no owner input
        since" (the tool's own typing would otherwise look like the owner being active)."""
        checks: dict[str, dict] = {}
        checks["kill_switch"] = {"ok": not rename_off()}
        reason = None
        try:
            reason = self.controls_check() if self.controls_check else None
        except Exception:
            reason = "controls-unreadable"
        checks["controls"] = {"ok": reason is None, "reason": reason}
        checks["platform"] = {"ok": bool(self.platform.available)}
        exe = ""
        if self.platform.available:
            with contextlib.suppress(Exception):
                exe = str(self.platform.foreground().get("exe") or "").lower()
        checks["desktop_in_front"] = {"ok": exe == DESKTOP_EXE}
        if since_tick is None:
            idle = None
            with contextlib.suppress(Exception):
                idle = self.platform.idle_seconds()
            checks["owner_idle"] = {"ok": idle is not None and idle >= self.min_idle_s,
                                    "idle_s": None if idle is None else round(idle, 1), "need_s": self.min_idle_s}
        else:
            tick = None
            with contextlib.suppress(Exception):
                tick = self.platform.last_input_tick()
            checks["owner_idle"] = {"ok": tick is not None and tick == since_tick, "mode": "no-input-since"}
        failing = next((name for name, row in checks.items() if not row["ok"]), None)
        why = {"kill_switch": "rename-off", "controls": "controls:" + str(reason), "platform": "no-window-platform",
               "desktop_in_front": "desktop-not-in-front",
               "owner_idle": "owner-input" if since_tick is not None else "owner-active"}
        return {"ok": failing is None, "reason": why[failing] if failing else None, "checks": checks}

    # ---- helpers
    def _result(self, local_id: str, **fields) -> dict:
        return dict({"tag": TAG, "ok": False, "outcome": "refused", "local": local_id, "push": False,
                     "attempted": False, "temp_state": False, "verified": False, "steps": [], "reason": None,
                     "chat_restore_needed": None, "chat_restored": None, "input_tick": None}, **fields)

    def _wait_for(self, local_id: str, title: str):
        """Poll the Desktop record until its title is `title`; return the last record seen."""
        record = None
        for _ in range(max(1, math.ceil(self.verify_timeout_s / max(self.verify_step_s, 0.001)))):
            with contextlib.suppress(Exception):
                record = self.records(local_id)
            if record and record.get("title") == title:
                return record
            self.sleep(self.verify_step_s)
        with contextlib.suppress(Exception):
            record = self.records(local_id)
        return record

    def _chat_now(self):
        try:
            return self.chat_focus.current() if self.chat_focus is not None else None
        except Exception:
            return None

    # ---- the rename
    def rename(self, local_id: str, new_title: str, *, dry_run: bool = False, since_tick=None) -> dict:
        """`since_tick`: the owner-input clock reading the caller took after its last keystroke; the first guard then
        asks "no owner input since" instead of "owner idle for the threshold" (the helper's rename-now pass)."""
        result = self._result(local_id)
        try:
            return self._rename(result, local_id, new_title, dry_run, since_tick)
        finally:
            if result["attempted"] or dry_run:
                log_record(self.log_path, {key: result[key] for key in (
                    "tag", "local", "outcome", "reason", "push", "attempted", "temp_state", "verified",
                    "chat_restore_needed", "chat_restored")} | {"ts_ns": time.time_ns(), "dry_run": dry_run,
                                                                 "steps": [{k: s.get(k) for k in ("outcome", "step")}
                                                                           for s in result["steps"]]})

    def _refuse(self, result: dict, reason: str) -> dict:
        result.update(outcome="refused", reason=reason)
        return result

    def _rename(self, result: dict, local_id: str, new_title: str, dry_run: bool, since_tick=None) -> dict:
        if not isinstance(local_id, str) or not LOCAL_ID_RE.fullmatch(local_id):
            return self._refuse(result, "not-a-desktop-chat-id")
        if (not isinstance(new_title, str) or not new_title.strip() or len(new_title) > MAX_TITLE_CHARS
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in new_title)):
            return self._refuse(result, "bad-title")
        gate = self.guards(since_tick=since_tick)
        result["guards"] = gate["checks"]
        if not gate["ok"]:
            return self._refuse(result, gate["reason"])
        record = None
        with contextlib.suppress(Exception):
            record = self.records(local_id)
        if not record:
            return self._refuse(result, "no-desktop-record")
        current = record.get("title")
        if not isinstance(current, str) or not current.strip():
            return self._refuse(result, "chat-has-no-title")
        push = new_title == current
        plan = [(current, temp_title(current)), (temp_title(current), new_title)] if push else [(current, new_title)]
        result["push"] = push
        result["plan_steps"] = len(plan)
        if dry_run:
            result.update(ok=True, outcome="dry-run")
            return result
        chat_before = self._chat_now()
        last_tick = None
        try:
            for index, (frm, to) in enumerate(plan):
                if index > 0:  # between the two renames: Desktop still in front, owner has not touched anything
                    again = self.guards(since_tick=last_tick)
                    if not again["ok"]:
                        result.update(outcome="failed", reason="between-steps:" + str(again["reason"]))
                        break
                result["attempted"] = True
                run = self.run_script(frm, to)
                with contextlib.suppress(Exception):
                    last_tick = self.platform.last_input_tick()
                step = {"outcome": run.get("outcome"), "step": run.get("step"), "reason": run.get("reason")}
                if run.get("outcome") != "renamed":
                    result["steps"].append(step)
                    result.update(outcome="failed", reason=f"script:{run.get('outcome')}:{run.get('step')}:"
                                                           f"{run.get('reason')}")
                    break
                self.sleep(self.settle_s)
                seen = self._wait_for(local_id, to)
                step["verified"] = bool(seen and seen.get("title") == to and seen.get("titleSource") == "user")
                result["steps"].append(step)
                if not step["verified"]:
                    result.update(outcome="failed", reason="verify-failed:record-does-not-show-the-new-title")
                    break
                if index == len(plan) - 1:
                    result.update(ok=True, outcome="renamed", verified=True)
        finally:
            result["input_tick"] = last_tick
            if result["attempted"]:
                final = None
                with contextlib.suppress(Exception):
                    final = self.records(local_id)
                shown = final.get("title") if final else None
                result["temp_state"] = bool(push and not result["ok"] and shown is not None and shown != current)
                result["title_left"] = ("as-requested" if result["ok"] else "unchanged" if shown == current
                                        else "temporary" if result["temp_state"] else "unknown")
                self._restore_chat(result, chat_before, last_tick)
        return result

    def _restore_chat(self, result: dict, before, last_tick) -> None:
        """If opening the menu switched which chat Desktop shows, select the previous one again (only while the
        owner has not typed or clicked since this tool's last keystroke)."""
        if not before:
            return
        self.sleep(self.settle_s)
        after = self._chat_now()
        if not after or after == before:
            result["chat_restore_needed"] = False
            return
        result["chat_restore_needed"] = True
        try:
            tick = self.platform.last_input_tick()
        except Exception:
            tick = None
        if tick is None or last_tick is None:  # fail closed: cannot prove the owner has not touched anything
            result["chat_restored"] = False
            result["chat_restore_skipped"] = "input-unreadable"
            return
        if tick != last_tick:
            result["chat_restored"] = False
            result["chat_restore_skipped"] = "owner-input"
            return
        try:
            self.chat_focus.select(before)
            result["chat_restored"] = True
        except Exception as error:
            result["chat_restored"] = False
            result["chat_restore_skipped"] = "select-failed:" + type(error).__name__


# --------------------------------------------------------- Desktop's records
def desktop_records(core):
    """records(local_id) reader over Desktop's per-chat record files in the ACTIVE account folder (else, when the
    active account is unknown, any account folder): only title, titleSource and a few flags are returned."""
    def read(local_id: str):
        folders = []
        active = None
        with contextlib.suppress(Exception):
            active = core.desktop_active_account()
        if active and active.get("folder"):
            folders.append(Path(active["folder"]))
        else:
            with contextlib.suppress(Exception):
                folders = sorted(Path(p).parent for p in Path(core.desktop_sessions_base()).glob(f"*/*/{local_id}.json"))
        for folder in folders:
            row = core._read_json(folder / f"{local_id}.json", None)
            if isinstance(row, dict):
                return {"title": row.get("title"), "titleSource": row.get("titleSource"),
                        "remote": row.get("remoteControlUserEnabled"), "archived": row.get("isArchived") is True,
                        "scheduled": bool(row.get("scheduledTaskId")), "session": row.get("cliSessionId")}
        return None
    return read


def default_renamer(core, **options) -> Renamer:
    """A Renamer wired to the Crosstalk core: its controls check (halt and deny; renaming is not a letter, so
    messaging off does not stop it), device folder and Desktop chat focus."""
    options.setdefault("chat_focus", core.DesktopChatFocus() if os.name == "nt" else None)
    options.setdefault("log_path", core.device_dir() / "rename" / "log.jsonl")
    return Renamer(records=desktop_records(core), controls_check=lambda: core.desktop_check(None), **options)


# ----------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crosstalk_rename", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", required=True, help="Desktop chat id, local_<uuid>")
    parser.add_argument("--title", help="new title (default: push the chat's current title to the phone)")
    parser.add_argument("--dry-run", action="store_true", help="print the guards and the plan; touch nothing")
    parser.add_argument("--min-idle", type=float, default=DEFAULT_MIN_IDLE_S,
                        help=f"owner idle seconds required (never below {MIN_IDLE_FLOOR_S:g})")
    args = parser.parse_args(argv)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import crosstalk_p0_core as core
    renamer = default_renamer(core, min_idle_s=clamp_min_idle(args.min_idle))
    title = args.title
    if title is None:
        record = renamer.records(args.session) if LOCAL_ID_RE.fullmatch(args.session) else None
        if not record or not isinstance(record.get("title"), str) or not record["title"].strip():
            # not in the active account's folder, not a chat id, or untitled: nothing to push
            result = renamer._refuse(renamer._result(args.session),
                                     "no-desktop-record" if not record else "chat-has-no-title")
            sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            return 1
        title = record.get("title") if isinstance(record.get("title"), str) else ""
    result = renamer.rename(args.session, title, dry_run=args.dry_run)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
