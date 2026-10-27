"""Read-only Crosstalk availability for workflow pickup, role routing and settings.

Crosstalk (``tools/crosstalk/``, guide ``docs/Crosstalk/README.md``) owns its
state in a device folder: ``CROSSTALK_P0_DEVICE_DIR`` when that is set, else
``crosstalk-probe`` under the EUTONOS home (``EUTONOS_HOME``, default ``~/.eutonos``). Its ``controls.json`` holds ``messaging``
(``on``/``off``; a missing file means off), ``halt`` and the ``deny`` list.
This module only reads that one file, with the same rules as the core's
``controls_load`` (``crosstalk_p0_core.py``), so it cannot start, resend or
wake anything, never writes, and never imports ``tools/crosstalk``. The
Crosstalk CLI ``status`` command (which also runs a readback and probes the
folder for writability) stays the owner's full status; this is the cheap,
side-effect-free reading a caller can take on every pickup.

Every function is safe to call when Crosstalk is not installed: absence is a
normal answer, never an exception.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import home as eutonos_home

CLI_RELATIVE = Path("tools") / "crosstalk" / "crosstalk_p0_cli.py"
DEVICE_DIR_ENV = "CROSSTALK_P0_DEVICE_DIR"
KIT_CLI = "crosstalk_p0_cli.py"


def device_dir() -> Path:
    override = os.environ.get(DEVICE_DIR_ENV)
    if override:
        return Path(override)
    return eutonos_home.home() / "crosstalk-probe"


def kit_cli_path() -> Path | None:
    """The CLI inside the active Crosstalk kit under the EUTONOS home, or ``None`` (no kit, or it fails verify)."""
    try:
        from . import kits
        kit = kits.active_kit()
        candidate = kit / KIT_CLI if kit is not None else None
        return candidate if candidate is not None and candidate.is_file() else None
    except (OSError, ValueError):
        return None


def cli_path(*roots) -> Path | None:
    """The first checkout root that carries the Crosstalk CLI, else the active kit's CLI, or ``None``.
    A checkout always wins, so checkout mode is unchanged; an installed runtime has no checkout and uses the kit."""
    for root in roots:
        if root is None:
            continue
        candidate = Path(root) / CLI_RELATIVE
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return kit_cli_path()


def controls() -> dict:
    """The device controls, defaulting exactly like the core: a missing, damaged or
    non-object file means messaging off, not halted, nobody denied."""
    try:
        value = json.loads((device_dir() / "controls.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    deny = value.get("deny")
    return {"messaging": "on" if value.get("messaging") == "on" else "off",
            "halt": value.get("halt") is True,
            "deny": [item for item in deny if isinstance(item, str)] if isinstance(deny, list) else []}


def installed() -> bool:
    try:
        return device_dir().is_dir()
    except OSError:
        return False


def status(*roots) -> dict:
    """Compact, JSON-safe availability. ``available`` is true only when the device
    folder and CLI exist, messaging is on and the owner has not halted it."""
    folder_present = installed()
    cli = cli_path(*roots) if roots else None
    ctl = controls()
    if not folder_present:
        reason = "crosstalk-not-installed"
    elif roots and cli is None:
        reason = "crosstalk-cli-missing"
    elif ctl["halt"]:
        reason = "crosstalk-halted"
    elif ctl["messaging"] != "on":
        reason = "crosstalk-messaging-off"
    else:
        reason = None
    return {"installed": folder_present, "available": reason is None, "reason": reason,
            "messaging": ctl["messaging"], "halt": ctl["halt"], "denied": len(ctl["deny"]),
            "device_folder": str(device_dir()),
            "cli": str(cli) if cli is not None else None}


_REASON_TEXT = {
    "crosstalk-not-installed": "Crosstalk is not installed on this device (no device folder).",
    "crosstalk-cli-missing": ("Crosstalk's CLI is not available here: no Crosstalk kit is installed "
                              "(in a EUTONOS source checkout it is tools/crosstalk/crosstalk_p0_cli.py)."),
    "crosstalk-halted": "Crosstalk is halted (owner control); the owner resumes it with `eutonos-crosstalk resume` "
                        "(in a EUTONOS source checkout: crosstalk_p0_cli.py resume).",
    "crosstalk-messaging-off": "Crosstalk messaging is off; the owner turns it on with `eutonos-crosstalk enable` "
                               "(in a EUTONOS source checkout: crosstalk_p0_cli.py enable).",
}


def reason_text(code: str | None) -> str:
    return _REASON_TEXT.get(code or "", "Crosstalk is unavailable.")


def pickup_hint(*roots) -> dict | None:
    """The one compact line workflow pickup folds into its output. ``None`` when
    Crosstalk is not installed here, so pickup's output stays unchanged then."""
    state = status(*roots)
    if not state["installed"]:
        return None
    if state["halt"]:
        text = "HALTED (owner control)"
    elif state["messaging"] != "on":
        text = "messaging OFF"
    else:
        text = "messaging on"
    line = (f"crosstalk: {text}, {state['denied']} denied recipient(s); use crosstalk_list / crosstalk_send "
            "for cross-app work, `eutonos-crosstalk status` for letters and the helper "
            "(in a EUTONOS source checkout: tools/crosstalk/crosstalk_p0_cli.py status)")
    hint = {"messaging": state["messaging"], "halt": state["halt"], "denied": state["denied"],
            "available": state["available"], "reason": state["reason"], "line": line}
    # Slice 3B.17: when the Hermes link is broken or Hermes was updated since Crosstalk last checked, say so (read-only,
    # from <device>/link-hermes.json). A healthy or never-checked link adds nothing.
    try:
        from . import crosstalk_link
        link_line = crosstalk_link.pickup_line()
    except Exception:
        link_line = None
    if link_line:
        hint["hermes_link"] = link_line
        hint["line"] = line + "; " + link_line
    return hint


# ----------------------------------------------------------- owner controls
# The dashboard Stop/Resume button and the ping-pong overlay write Crosstalk's own
# controls by running its CLI (halt/resume/enable/off), never by duplicating the
# core's locked, atomic write, then re-read controls.json to prove the result.
# src/eutonos never imports tools/crosstalk.
OWNER_COMMANDS = ("halt", "resume", "enable", "off")
CONTROL_TIMEOUT_SECONDS = 20
RECENT_LETTER_LIMIT = 12
_JOURNAL_TAIL_BYTES = 262144


def cli_python() -> str:
    """The interpreter that runs the Crosstalk CLI: ``EUTONOS_PYTHON`` when it names a
    file, else the installed runtime, else this process's interpreter (the CLI is
    standard library only). Resolved at call time, never at import."""
    override = os.environ.get("EUTONOS_PYTHON")
    if override and Path(override).is_file():
        return override
    runtime = eutonos_home.home() / "runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(runtime) if runtime.is_file() else sys.executable


def run_cli(argv, timeout_seconds=CONTROL_TIMEOUT_SECONDS):
    """Run one CLI command and return ``(returncode, stdout)``. The single place that
    starts a process, so tests replace it."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout_seconds, creationflags=flags)
    return done.returncode, done.stdout


def default_roots() -> list:
    """Checkout roots that may carry the CLI: this source checkout."""
    return [Path(__file__).resolve().parents[2]]


DEGRADED_LOCK_SECONDS = 5.0


def _controls_lock(timeout: float):
    """A context manager on ``<device>/.controls.lock``, the same file and byte lock as the core's ``locked("controls")``."""
    import contextlib

    @contextlib.contextmanager
    def held():
        folder = device_dir()
        folder.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(folder / ".controls.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("device controls lock busy")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(descriptor)

    return held()


# The core's stored wake budget: whole numbers inside these limits, else the default (parity is tested against the kit).
WAKE_BUDGET_DEFAULT = {"per_chat": 6, "total": 20, "window_s": 3600}
WAKE_BUDGET_LIMITS = {"per_chat": (0, 1000), "total": (0, 10000), "window_s": (60, 86400)}


def _wake_budget(value) -> dict:
    value = value if isinstance(value, dict) else {}
    clean = {}
    for name, default in WAKE_BUDGET_DEFAULT.items():
        low, high = WAKE_BUDGET_LIMITS[name]
        raw = value.get(name)
        clean[name] = raw if isinstance(raw, int) and not isinstance(raw, bool) and low <= raw <= high else default
    return clean


def degraded_halt(lock_seconds: float = DEGRADED_LOCK_SECONDS) -> dict:
    """Set ``halt`` true in ``controls.json`` without the kit, for when the active kit is missing or fails verify.

    It follows the core's ``controls_update(halt=True)``: the same ``.controls.lock``, the same normalising
    reading and the same stored fields (messaging on/off, boolean halt, string deny list, wake budget), and one atomic
    replace of the file. It never imports ``tools/crosstalk`` and never starts a process. ``ok`` is true only when
    ``controls()`` reads ``halt`` back as true; it never raises."""
    import tempfile
    path = device_dir() / "controls.json"
    try:
        with _controls_lock(lock_seconds):
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                stored = {}
            stored = stored if isinstance(stored, dict) else {}
            deny = stored.get("deny")
            current = {"messaging": "on" if stored.get("messaging") == "on" else "off", "halt": True,
                       "deny": [item for item in deny if isinstance(item, str)] if isinstance(deny, list) else [],
                       "wake_budget": _wake_budget(stored.get("wake_budget"))}
            data = (json.dumps(current, indent=2, sort_keys=True) + "\n").encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                for attempt in range(8):
                    try:
                        os.replace(temporary, path)
                        break
                    except PermissionError:
                        if attempt == 7:
                            raise
                        time.sleep(0.05)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    except TimeoutError:
        return {"ok": False, "error": "controls-lock-busy", "controls": controls()}
    except OSError as exc:
        return {"ok": False, "error": "controls-write-failed", "detail": type(exc).__name__, "controls": controls()}
    ctl = controls()
    return {"ok": ctl["halt"] is True, "command": "halt", "degraded": True, "controls": ctl}


def owner_control(command: str, *roots) -> dict:
    """Run ``crosstalk_p0_cli.py <command>`` (halt, resume, enable, off) and read the
    controls back. ``ok`` is true only when the re-read controls show the intended
    value; it never raises. Only ``halt`` has a degraded path: when no CLI can be found (no checkout and no
    active kit that passes ``verify``), the CLI cannot start, or it ran but the read-back does not show the halt,
    ``degraded_halt`` writes the control directly and the result says ``degraded: true``. Stop never depends on the kit."""
    if command not in OWNER_COMMANDS:
        return {"ok": False, "error": "unknown-command"}
    cli = cli_path(*roots, *default_roots())
    if cli is None:
        if command == "halt":
            return degraded_halt()
        return {"ok": False, "error": "crosstalk-cli-missing", "message": reason_text("crosstalk-cli-missing")}
    try:
        code, _stdout = run_cli([cli_python(), "-I", "-B", str(cli), command])
    except subprocess.TimeoutExpired:
        return degraded_halt() if command == "halt" else {"ok": False, "error": "crosstalk-timeout",
                                                         "controls": controls()}
    except OSError as exc:
        return degraded_halt() if command == "halt" else {"ok": False, "error": "crosstalk-launch-failed",
                                                         "detail": type(exc).__name__}
    ctl = controls()
    want = {"halt": ctl["halt"] is True, "resume": ctl["halt"] is False,
            "enable": ctl["messaging"] == "on" and ctl["halt"] is False,
            "off": ctl["messaging"] == "off"}[command]
    if command == "halt" and not (code == 0 and want):
        return degraded_halt()
    result = {"ok": code == 0 and want, "command": command, "controls": ctl}
    if not result["ok"]:
        result["error"] = "crosstalk-control-not-applied"
    return result


def recent_letters(limit: int = RECENT_LETTER_LIMIT) -> list:
    """Metadata of the newest letters from the journal tail: id, from/to, state and
    time only. Letter bodies (``message``) and text are never returned."""
    try:
        with open(device_dir() / "journal.jsonl", "rb") as stream:
            end = stream.seek(0, 2)
            start = max(0, end - _JOURNAL_TAIL_BYTES)
            stream.seek(start)
            raw = stream.read()
    except OSError:
        return []
    if start:
        raw = raw.partition(b"\n")[2]
    letters: dict = {}
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("event") != "letter" or not isinstance(row.get("letter_id"), str):
            continue
        item = letters.setdefault(row["letter_id"], {"letter_id": row["letter_id"], "from": None, "to": None,
                                                     "state": None, "ts_ns": None})
        for field in ("from", "to"):
            value = row.get(field)
            if isinstance(value, dict) and value.get("app"):
                ident = value.get("id")
                item[field] = str(value["app"]) + (":" + str(ident) if ident else "")
        if isinstance(row.get("state"), str):
            item["state"] = row["state"]
        if isinstance(row.get("ts_ns"), int):
            item["ts_ns"] = row["ts_ns"]
    ordered = sorted(letters.values(), key=lambda item: item["ts_ns"] or 0, reverse=True)
    return ordered[:max(0, int(limit))]


# --------------------------------------------------- helper and in-transit view
# Read-only readings for the Companion popup. The helper (tools/crosstalk/crosstalk_helper.py)
# keeps a heartbeat in <device>/helper/state.json and open notices in helper/notices.json; these
# mirror the core's helper_alive / helper_notices_load rules without importing tools/crosstalk.
HELPER_STALE_SECONDS = 120.0
WAKE_NOTICE_KEY = "wake-budget"
# Journal states of a letter that is still on its way: written but not yet sent (journaled),
# the native send in progress (sending, waking) or waiting for an asleep chat (blocked:not-running).
# Delivered or held letters are not in transit, and Stop never unsends them.
IN_TRANSIT_STATES = ("journaled", "blocked:not-running", "sending", "uncertain", "waking")
IN_TRANSIT_SECONDS = 3600


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_alive(pid) -> bool:
    """Whether a process exists. Never signals it (os.kill would terminate on Windows)."""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def helper_status(now: float | None = None) -> dict:
    """The helper's liveness: ``alive`` is true only for a fresh heartbeat from a live pid.
    ``ever_ran`` is false when no heartbeat file exists (never started on this device)."""
    state = _read_object(device_dir() / "helper" / "state.json")
    stamp = state.get("ts_ms")
    fresh = isinstance(stamp, (int, float)) and not isinstance(stamp, bool)         and (time.time() if now is None else now) * 1000 - stamp <= HELPER_STALE_SECONDS * 1000
    return {"alive": bool(fresh and _pid_alive(state.get("pid"))), "ever_ran": bool(state),
            "pid": state.get("pid") if type(state.get("pid")) is int else None,
            "ts_ms": stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None}


def wake_budget_notice() -> str | None:
    """The helper's open wake-budget notice text, or ``None``."""
    notices = _read_object(device_dir() / "helper" / "notices.json").get("notices")
    notice = notices.get(WAKE_NOTICE_KEY) if isinstance(notices, dict) else None
    message = notice.get("message") if isinstance(notice, dict) else None
    return message[:400] if isinstance(message, str) and message else None


def in_transit(letters: list, now: float | None = None) -> int:
    """How many of ``letters`` (``recent_letters`` rows) are still on their way."""
    horizon = ((time.time() if now is None else now) - IN_TRANSIT_SECONDS) * 1e9
    return sum(1 for item in letters if item.get("state") in IN_TRANSIT_STATES and (item.get("ts_ns") or 0) >= horizon)


PINGPONG_GRACE_SECONDS = 120
_PINGPONG_SCAN = 50


def pingpong(root=None, grace_seconds: float = PINGPONG_GRACE_SECONDS, now: float | None = None) -> dict:
    """Is a Crosstalk exchange live? Metadata only, read-only, cheap enough to poll
    about once a second (the ping-pong overlay). Active while any letter's newest
    journal row is within ``grace_seconds`` and is not a refusal or a block; the
    overlay stays visible while the owner has halted Crosstalk (``stopped``) so
    Resume remains reachable. ``root`` is accepted for the overlay's call shape and
    unused: Crosstalk's state lives in the device folder, not the project."""
    if not installed():
        return {"active": False, "waiting": 0, "stopped": False, "untrusted": True}
    since_ns = int(((time.time() if now is None else now) - grace_seconds) * 1e9)
    live = [letter for letter in recent_letters(_PINGPONG_SCAN)
            if (letter["ts_ns"] or 0) >= since_ns
            and (letter["state"] == "blocked:not-running"  # asleep chat: the helper's wake is still to come
                 or not (letter["state"] or "").startswith(("refused", "blocked", "expired")))]
    waiting = sum(1 for letter in live if letter["state"] in ("journaled", "blocked:not-running", "sending", "held"))
    return {"active": bool(live), "waiting": waiting, "stopped": controls()["halt"], "untrusted": True}
