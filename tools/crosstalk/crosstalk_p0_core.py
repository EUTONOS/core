"""Crosstalk MVP kit: promoted shared state and delivery logic (stdlib only).

The P0 names and device folder remain compatible with installed state. It never
reads a token or credential file: the only account sources are ``~/.claude.json`` key
``oauthAccount.accountUuid`` and the Codex app-server ``account/read`` method.

Device folder (default ``%USERPROFILE%\\.eutonos\\crosstalk-probe\\``, or ``crosstalk-probe`` under ``EUTONOS_HOME`` when set):
  controls.json    messaging on/off (default OFF), halt, deny list
  directory.json   chats that registered (app, exact native ID, folder, title, account)
  journal.jsonl    append-only rows: register, status, letter state changes
  fault.json       one-shot fault injection for probe 5 (helper killed)

Letter states: journaled -> sending -> accepted -> held|arrived, with exits refused (proven
not sent), blocked:not-running (an asleep Claude chat: the helper wakes it and the mail slot hands the letter over),
expired-unsent (still unsent after the 24-hour mail-slot limit), blocked:other-account (final: undeliverable, never sent later), blocked:unknown-identity (the recipient's
recorded account or the current account is unreadable: not sent, waits and is rechecked; not final), uncertain (never resent
automatically; only native readback promotes it).

Test-only environment overrides (never set by install.ps1): CROSSTALK_P0_DEVICE_DIR,
CROSSTALK_P0_CLAUDE_JSON, CROSSTALK_P0_CLAUDE_HOME, CROSSTALK_P0_CODEX_EXE,
CROSSTALK_P0_CLAUDE_EXE, CROSSTALK_P0_CLAUDE_TIMEOUT_S, CROSSTALK_P0_CLAUDE_DESKTOP_DIR,
CROSSTALK_P0_CODEX_SESSIONS_DIR, CROSSTALK_P0_HERMES_HOME (the Hermes data folder, default %LOCALAPPDATA%\\hermes),
CROSSTALK_P0_CODEX_WAKE=off (no detached Codex waiter is started),
CROSSTALK_P0_FOCUS=off (the focus gate runs the wake and its confirm only: no overlay, no window handling).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

KIT = Path(__file__).resolve().parent
SENDER_PLUGIN = KIT / "claude-plugin" / "sender"
SENDER_COMMAND = "crosstalk-p0-inline"
SENDER_TAG = "crosstalk.p0.sender.v1"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
KEY_RE = re.compile(r"(claude|codex):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
HERMES_ID_RE = re.compile(r"\d{8}_\d{6}_[0-9a-f]{6}\Z")
HERMES_KEY_RE = re.compile(r"hermes:(\d{8}_\d{6}_[0-9a-f]{6})\Z")
LOCAL_ID_RE = re.compile(r"local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
MAX_TEXT_CHARS = 3000
# Asleep Claude chats: the helper's letter wake plus the mail slot (below). There are no relay chats.
WAKE_BACKOFF_S = (60.0, 300.0, 1800.0)  # waits before the 2nd, 3rd and 4th wake attempt for one letter
WAKE_MAX_ATTEMPTS = len(WAKE_BACKOFF_S) + 1  # a wake is not a send, so it may be tried again; a send never is
JOURNALED_GRACE_S = 5.0
HERMES_BUSY_GUIDANCE = ("QUEUED, NOT LOST: the recipient Hermes chat is in the middle of a turn. The Crosstalk plugin "
                        "holds the letter and delivers it once: appended to the result of the chat's next tool call "
                        "(plugin 0.3.0+), or as a new message when the turn ends if no tool call comes first "
                        "(this can take many minutes). It lands by itself; readback shows it. Do NOT resend or replace the letter.")

STALE_SENDING_S = 120.0
CLAUDE_SEND_TIMEOUT_S = 60
CODEX_QUEUE_TIMEOUT_S = 30
READBACK_TIMEOUT_S = 2.0
# A Claude recipient's transcript row can reach disk after the sender sees acceptance: 2026-10-12 journal,
# immediate scans 0.5-0.76 s after the row's timestamp found it about half the time. Re-check once.
CLAUDE_READBACK_RECHECK_S = 2.0
HELD_REREAD_GAP_S = 1.0  # a Codex letter is re-read this long after each "not seen" (3 times)


def held_reread_delays(app: str) -> tuple[float, ...]:
    """Waits before each re-read of a just-accepted letter that readback has not seen yet (3B.15): Claude's
    transcript lags about 2 s, then one more look; Codex and Hermes three looks 1 s apart (the plugin's inject
    returns before Hermes writes the user row: live 2026-10-20, the row landed after the first read)."""
    if app == "claude":
        return (CLAUDE_READBACK_RECHECK_S, HELD_REREAD_GAP_S)
    return (HELD_REREAD_GAP_S,) * 3 if app in ("codex", "hermes") else ()
RECONCILE_READBACK_BUDGET_S = 5.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Last-resort wake of an ASLEEP Codex chat (owner, 2026-10-12): Codex unloads idle chats and a queued letter then
# waits until the chat is opened. No signal outside Codex says loaded/unloaded, so decide by behavior: the letter
# was not picked up and the chat has no turn in progress. Then, once, open codex://threads/<id>.
CODEX_WAKE_DELAY_S = 60.0       # how long a queued letter may sit before the waiter looks at the chat
CODEX_LINK_COOLDOWN_S = 120.0   # one link per chat in this window: letters to one asleep chat share it
CODEX_LINK_POLL_S = 30.0        # after the link opens, how long the waiter watches for the letter to appear
CODEX_LINK_POLL_STEP_S = 3.0
CODEX_WAKE_READBACK_TIMEOUT_S = 10.0  # the waiter is in no hurry: a slow app-server start must not cancel the wake
CODEX_LINK_FORMAT = "codex://threads/{}"
ROLLOUT_LOOKUP_ENTRIES = 50_000
ROLLOUT_LOOKUP_S = 5.0
ROLLOUT_TAIL_BYTES = (256 * 1024, 4 * 1024 * 1024, 32 * 1024 * 1024)  # tail spans read until a turn marker shows
# blocked:other-account is final: an undeliverable letter is reported now, never sent later.
# blocked:unknown-identity is not: nothing was sent, and the letter waits until both accounts are readable.
TERMINAL_STATES = {"arrived", "refused", "cancelled", "replied", "turn-ended-no-reply", "blocked:other-account",
                   "stale-unconfirmed", "expired-unsent"}
ATTEMPTED_STATES = {"sending", "accepted", "held", "arrived", "uncertain", "replied", "turn-ended-no-reply"}
# Hermes endpoint (slice 3B.12): the Crosstalk plugin inside the Hermes Desktop backend claims inbox files.
HERMES_HEARTBEAT_STALE_S = 30.0    # the plugin beats every 5 s; older than this means no listener
HERMES_RESULT_WAIT_S = 20.0        # how long a send waits for the listener to report inject_message's outcome
HERMES_RESULT_POLL_S = 0.1
HERMES_DISCOVER_LIMIT = 50
HERMES_DB_TIMEOUT_S = 2.0
FAULT_POINTS = ("after_journal", "after_sending", "after_native")
FAULT_EXIT = 86


class ProbeError(Exception):
    """A refusal with a stable reason code; never carries secrets."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# ----------------------------------------------------------------- locations
def home() -> Path:
    value = os.environ.get("USERPROFILE")
    if value:
        return Path(value)
    exe = Path(sys.executable).resolve()
    # %USERPROFILE%\.eutonos\runtime\Scripts\python.exe -> parents[3] is the profile
    if exe.parent.name.lower() == "scripts" and exe.parents[2].name.lower() == ".eutonos":
        return exe.parents[3]
    return Path.home()


def eutonos_home() -> Path:
    """The per-user EUTONOS home. Parity copy of ``eutonos.home.home()`` (IPC2 contract a): this kit cannot import
    the ``eutonos`` package, so a parity test (tests/test_home.py) keeps the two identical. ``EUTONOS_HOME`` (absolute)
    wins; then the kit's own place ``<H>/kits/<id>`` or a runtime ``<H>/runtimes/eutonos-*`` gives ``<H>``; otherwise
    ``<user home>/.eutonos``."""
    given = os.environ.get("EUTONOS_HOME")
    if given:
        if not Path(given).is_absolute():
            raise ValueError(f"EUTONOS_HOME must be an absolute path, not {given!r}.")
        return Path(given)
    if KIT.parent.name == "kits":
        return KIT.parent.parent
    try:
        prefix = Path(sys.prefix).resolve()
    except OSError:
        prefix = None
    if prefix is not None and prefix.parent.name == "runtimes" and prefix.name.startswith("eutonos-"):
        return prefix.parent.parent
    return home() / ".eutonos"


def kit_mode() -> bool:
    """True when this kit runs from a materialized copy under ``<EUTONOS home>/kits`` (not from a checkout)."""
    kits = os.path.normcase(str((eutonos_home() / "kits").resolve()))
    return any(os.path.normcase(str(parent)) == kits for parent in KIT.parents)


def device_dir() -> Path:
    override = os.environ.get("CROSSTALK_P0_DEVICE_DIR")
    return Path(override) if override else eutonos_home() / "crosstalk-probe"


def claude_home() -> Path:
    override = os.environ.get("CROSSTALK_P0_CLAUDE_HOME")
    return Path(override) if override else home() / ".claude"


def codex_sessions_dir() -> Path:
    override = os.environ.get("CROSSTALK_P0_CODEX_SESSIONS_DIR")
    if override:
        return Path(override)
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else home() / ".codex") / "sessions"


def claude_json_path() -> Path:
    override = os.environ.get("CROSSTALK_P0_CLAUDE_JSON")
    return Path(override) if override else home() / ".claude.json"


def _exe_argv(exe: str) -> list[str]:
    # Test-only convenience: an override ending in .py is run by this interpreter.
    return [sys.executable, "-I", "-B", exe] if exe.lower().endswith(".py") else [exe]


def claude_exe() -> str | None:
    override = os.environ.get("CROSSTALK_P0_CLAUDE_EXE")
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    candidate = home() / ".local" / "bin" / "claude.exe"
    if candidate.is_file():
        return str(candidate)
    return desktop_bundled_claude()


def desktop_bundled_claude() -> str | None:
    """The newest `claude` that Claude Desktop carries, or None. Windows: Claude-Data/<package>/claude-code/<version>/
    <hash>/claude.exe under %LOCALAPPDATA%; macOS: Application Support/Claude/claude-code/<version>/claude. Parity
    copy of ``eutonos.remote_control.desktop_bundled_claude`` (this kit cannot import ``eutonos``)."""
    found = []
    local = Path(os.environ.get("LOCALAPPDATA") or str(home() / "AppData" / "Local"))
    patterns = [(local / "Claude-Data", "*/claude-code/*/*/claude.exe"),
                (home() / "Library" / "Application Support" / "Claude", "claude-code/*/claude")]
    for base, pattern in patterns:
        try:
            found += [path for path in base.glob(pattern) if path.is_file()]
        except OSError:
            continue

    def key(path: Path):
        parts = [int(piece) if piece.isdigit() else 0 for piece in re.split(r"[.\-]", path.parent.parent.name
                 if path.name == "claude.exe" else path.parent.name)]
        return parts, str(path)

    return str(max(found, key=key)) if found else None


def codex_exe() -> str | None:
    override = os.environ.get("CROSSTALK_P0_CODEX_EXE")
    if override:
        return override if Path(override).is_file() else None
    local = os.environ.get("LOCALAPPDATA") or str(home() / "AppData" / "Local")
    base = Path(local) / "OpenAI" / "Codex" / "bin"
    candidates = []
    if base.is_dir():
        candidates = [p for p in [base / "codex.exe", *sorted(base.glob("*/codex.exe"))] if p.is_file()]
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    return shutil.which("codex")


def child_env(extra: dict | None = None) -> tuple[dict, list[str]]:
    """Environment for a spawned CLI. Fills names a filtered MCP environment may lack.

    Returns (env, names_filled). Values are never logged; only the names filled."""
    env = dict(os.environ)
    filled = []
    base = home()
    for name, value in (("USERPROFILE", str(base)),
                        ("APPDATA", str(base / "AppData" / "Roaming")),
                        ("LOCALAPPDATA", str(base / "AppData" / "Local"))):
        if not env.get(name):
            env[name] = value
            filled.append(name)
    if extra:
        env.update(extra)
    return env, filled


# --------------------------------------------------------------------- files
@contextlib.contextmanager
def locked(name: str = "state", timeout: float = 20.0):
    """Cross-process lock in the device folder. Not re-entrant: never nest."""
    folder = device_dir()
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(folder / f".{name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
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
                    raise TimeoutError("device state lock busy")
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


def _write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def probe_device_write() -> dict:
    """Can this process create and delete a file in the device folder?"""
    folder = device_dir()
    marker = folder / f".write-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        marker.write_text("x", encoding="ascii")
        marker.unlink()
        return {"writable": True, "folder": str(folder)}
    except OSError as error:
        return {"writable": False, "folder": str(folder), "error_kind": type(error).__name__,
                "errno": getattr(error, "errno", None)}


# ------------------------------------------------------------------ controls
def controls_load() -> dict:
    value = _read_json(device_dir() / "controls.json", {})
    if not isinstance(value, dict):
        value = {}
    deny = value.get("deny")
    return {"messaging": "on" if value.get("messaging") == "on" else "off",
            "halt": value.get("halt") is True,
            "deny": [d for d in deny if isinstance(d, str)] if isinstance(deny, list) else [],
            "wake_budget": _wake_budget_clean(value.get("wake_budget"))}


# Wake budget (rework thought experiment 6): how often chats may be woken, per chat and in total, over a rolling
# window. A wake is a Claude letter wake that brings Desktop forward, a Codex headless resume, or a Codex link open.
# Stored in controls so the owner changes it with `crosstalk_p0_cli.py wake-budget`.
WAKE_BUDGET_DEFAULT = {"per_chat": 6, "total": 20, "window_s": 3600}
WAKE_BUDGET_LIMITS = {"per_chat": (0, 1000), "total": (0, 10000), "window_s": (60, 86400)}
WAKE_BATCH_S = 60.0               # a wake for a chat this soon after its last counted wake is the same wake
WAKE_NOTICE_KEY = "wake-budget"
WAKE_NOTICE_TEXT = ("Wake budget reached: letters to asleep chats are waiting unwoken (nothing is lost or sent twice). "
                    "They are woken as the hourly budget frees; the owner can change it with "
                    "crosstalk_p0_cli.py wake-budget.")


def _wake_budget_clean(value) -> dict:
    """The stored budget with every field a whole number inside its limits; anything else is the default."""
    value = value if isinstance(value, dict) else {}
    clean = {}
    for name, default in WAKE_BUDGET_DEFAULT.items():
        low, high = WAKE_BUDGET_LIMITS[name]
        raw = value.get(name)
        clean[name] = raw if isinstance(raw, int) and not isinstance(raw, bool) and low <= raw <= high else default
    return clean


def controls_update(**changes) -> dict:
    with locked("controls"):
        current = controls_load()
        if "messaging" in changes:
            current["messaging"] = "on" if changes["messaging"] == "on" else "off"
        if "halt" in changes:
            current["halt"] = bool(changes["halt"])
        if isinstance(changes.get("wake_budget"), dict):
            asked = {name: value for name, value in changes["wake_budget"].items() if name in WAKE_BUDGET_DEFAULT}
            for name, value in asked.items():
                low, high = WAKE_BUDGET_LIMITS[name]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ProbeError("wake-budget-out-of-range", f"{name} must be a whole number from {low} to {high}")
            current["wake_budget"] = dict(current["wake_budget"], **asked)
        if changes.get("deny_add"):
            current["deny"] = sorted(set(current["deny"]) | {changes["deny_add"]})
        if changes.get("deny_remove"):
            current["deny"] = sorted(set(current["deny"]) - {changes["deny_remove"]})
        _write_json_atomic(device_dir() / "controls.json", current)
        return current


def controls_check(recipient_key: str | None) -> str | None:
    """Reason a send is not allowed right now, or None. Missing file means OFF."""
    controls = controls_load()
    if controls["halt"]:
        return "halted"
    if controls["messaging"] != "on":
        return "messaging-off"
    if recipient_key and recipient_key in controls["deny"]:
        return "denied"
    return None


def desktop_check(recipient_key: str | None = None) -> str | None:
    """Reason a Claude Desktop job (relink, kick, alert, rename, login prompt, switch history, chat copying) may not
    act right now, or None. Halt and the deny list only: `messaging` is for letters (controls_check) and does not
    stop these jobs (owner, 2026-10-26: every Crosstalk feature has its own switch)."""
    controls = controls_load()
    if controls["halt"]:
        return "halted"
    if recipient_key and recipient_key in controls["deny"]:
        return "denied"
    return None


def helper_config() -> dict:
    """The helper's config.json (device folder), {} when missing or unreadable. Read on every call: a dashboard
    change applies at the next tick."""
    value = _read_json(helper_dir() / "config.json", {})
    return value if isinstance(value, dict) else {}


def relink_switched_off() -> bool:
    """Config `relink: false` turns the whole restart relink off (absent = on)."""
    return helper_config().get("relink") is False


# ------------------------------------------------------------- wake budget
# The ledger of wakes lives in `wakes.json` in the device folder and changes only under the state lock, so a wake is
# counted at the moment it is decided and two processes cannot both take the last slot. A wake for a chat inside
# WAKE_BATCH_S of that chat's last counted wake is the same wake (letters batch); it is recorded, not counted again.
def wakes_path() -> Path:
    return device_dir() / "wakes.json"


def _wakes_load() -> dict:
    value = _read_json(wakes_path(), {})
    value = value if isinstance(value, dict) else {}
    events, denied = value.get("events"), value.get("denied")
    return {"events": [e for e in events if isinstance(e, dict) and isinstance(e.get("key"), str)
                       and isinstance(e.get("ns"), int)] if isinstance(events, list) else [],
            "denied": {k: v for k, v in denied.items() if isinstance(k, str) and isinstance(v, int)}
            if isinstance(denied, dict) else {}}


def _wake_counted(event: dict) -> bool:
    return not event.get("batched") and not event.get("refunded")


def wake_budget_claim(key: str, kind: str, now_ns: int | None = None) -> dict:
    """Decide whether chat `key` (claude:<id> or codex:<id>) may be woken now, and count the wake if so. Returns
    {"allowed": bool, "reason": ..., "event": id|None, "batched": bool}. Nothing is launched here. Never nest in
    another state-lock block (it takes the lock)."""
    now = time.time_ns() if now_ns is None else now_ns
    budget = controls_load()["wake_budget"]
    window_ns = int(budget["window_s"] * 1e9)
    with locked("state"):
        data = _wakes_load()
        data["events"] = [e for e in data["events"] if now - e["ns"] < window_ns][-500:]
        data["denied"] = {k: v for k, v in data["denied"].items() if now - v < window_ns}
        counted = [e for e in data["events"] if _wake_counted(e)]
        recent = [e for e in counted if e["key"] == key and 0 <= (now - e["ns"]) / 1e9 < WAKE_BATCH_S]
        event_id = uuid.uuid4().hex[:10]
        if recent:
            data["events"].append({"id": event_id, "key": key, "ns": now, "kind": kind, "batched": True})
            data["denied"].pop(key, None)
            result = {"allowed": True, "reason": "batched", "event": event_id, "batched": True}
        elif sum(1 for e in counted if e["key"] == key) >= budget["per_chat"]:
            data["denied"][key] = now
            result = {"allowed": False, "reason": "wake-budget-chat", "event": None, "batched": False}
        elif len(counted) >= budget["total"]:
            data["denied"][key] = now
            result = {"allowed": False, "reason": "wake-budget-total", "event": None, "batched": False}
        else:
            data["events"].append({"id": event_id, "key": key, "ns": now, "kind": kind})
            data["denied"].pop(key, None)
            result = {"allowed": True, "reason": "counted", "event": event_id, "batched": False}
        _write_json_atomic(wakes_path(), data)
        denied_any = bool(data["denied"])
    _wake_budget_notice(denied_any)
    return result


def wake_budget_refund(event_id: str | None) -> bool:
    """A wake that turned out to wake nothing (the chat was already awake, the app held the chat) gives its slot back."""
    if not event_id:
        return False
    with locked("state"):
        data = _wakes_load()
        for event in data["events"]:
            if event.get("id") == event_id and not event.get("refunded"):
                event["refunded"] = True
                _write_json_atomic(wakes_path(), data)
                return True
    return False


def wake_budget_room(key: str, now_ns: int | None = None) -> bool:
    """Read-only: would a wake for `key` be allowed right now? (A batched wake always is.)"""
    now = time.time_ns() if now_ns is None else now_ns
    budget = controls_load()["wake_budget"]
    window_ns = int(budget["window_s"] * 1e9)
    counted = [e for e in _wakes_load()["events"] if now - e["ns"] < window_ns and _wake_counted(e)]
    if any(e["key"] == key and 0 <= (now - e["ns"]) / 1e9 < WAKE_BATCH_S for e in counted):
        return True
    return (sum(1 for e in counted if e["key"] == key) < budget["per_chat"] and len(counted) < budget["total"])


def wake_budget_status(now_ns: int | None = None) -> dict:
    """What the budget is and how much of it is used (read-only; for status)."""
    now = time.time_ns() if now_ns is None else now_ns
    budget = controls_load()["wake_budget"]
    window_ns = int(budget["window_s"] * 1e9)
    data = _wakes_load()
    counted = [e for e in data["events"] if now - e["ns"] < window_ns and _wake_counted(e)]
    per_chat: dict[str, int] = {}
    for event in counted:
        per_chat[event["key"]] = per_chat.get(event["key"], 0) + 1
    return dict(budget, used_total=len(counted), used_per_chat=per_chat,
                waiting_unwoken=sorted(k for k, v in data["denied"].items() if now - v < window_ns))


def _wake_budget_notice(denied_any: bool) -> None:
    """One helper notice while any chat has been refused a wake by the budget; cleared once the refusals age out of
    the window or the chat is woken. The text never changes, so repeated refusals do not reopen it."""
    with contextlib.suppress(Exception):
        if denied_any:
            helper_notice_set(WAKE_NOTICE_KEY, "wake-budget", WAKE_NOTICE_TEXT)
        else:
            helper_notice_clear(WAKE_NOTICE_KEY)


def wake_budget_notice_sync(now_ns: int | None = None) -> bool:
    """The helper's tick: age out old refusals and clear the notice when none is left. True when a notice is open."""
    now = time.time_ns() if now_ns is None else now_ns
    window_ns = int(controls_load()["wake_budget"]["window_s"] * 1e9)
    with locked("state"):
        data = _wakes_load()
        keep = {k: v for k, v in data["denied"].items() if now - v < window_ns}
        if keep != data["denied"]:
            data["denied"] = keep
            _write_json_atomic(wakes_path(), data)
    _wake_budget_notice(bool(keep))
    return bool(keep)


# ------------------------------------------------------------------- journal
def journal_path() -> Path:
    return device_dir() / "journal.jsonl"


def _append_journal_locked(row: dict) -> None:
    row = dict(row)
    row.setdefault("ts_ns", time.time_ns())
    row.setdefault("pid", os.getpid())
    line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    journal_path().parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(journal_path()), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    written = 0
    try:
        # os.write may write short: loop until the whole line is down, or raise (a missing `sending` row
        # would let the replay guard pass and a letter be sent twice).
        while written < len(line):
            count = os.write(descriptor, line[written:])
            if count <= 0:
                raise OSError("journal append wrote no bytes")
            written += count
        os.fsync(descriptor)
    except BaseException:
        if 0 < written < len(line):
            # Do not let the next row glue onto this partial line.
            with contextlib.suppress(OSError):
                os.write(descriptor, b"\n")
        raise
    finally:
        os.close(descriptor)


def journal_append(row: dict) -> None:
    with locked("state"):
        _append_journal_locked(row)


def journal_rows() -> list[dict]:
    rows = []
    try:
        with journal_path().open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except FileNotFoundError:
        pass
    return rows


def letters_from(rows: list[dict]) -> dict[str, dict]:
    """Fold letter rows: the first row carries the static fields; the last state wins."""
    letters: dict[str, dict] = {}
    for row in rows:
        if row.get("event") != "letter" or not isinstance(row.get("letter_id"), str):
            continue
        merged = letters.setdefault(row["letter_id"], {"history": []})
        merged["history"].append(dict({"state": row.get("state"), "ts_ns": row.get("ts_ns")},
                                      **({"attempt_id": row["attempt_id"]} if row.get("attempt_id") else {}),
                                      **({"readback": True} if not row.get("attempt_id") and row.get("readback_ns") else {})))
        for key, value in row.items():
            if key not in ("event", "ts_ns", "pid"):
                merged[key] = value
        merged["updated_ns"] = row.get("ts_ns")
    return letters


def letters_load() -> dict[str, dict]:
    return letters_from(journal_rows())


# ----------------------------------------------------------------- directory
def directory_load() -> dict:
    value = _read_json(device_dir() / "directory.json", {})
    if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
        return {"version": 1, "entries": {}}
    return value


def directory_register(app: str, native_id: str, fields: dict, journal_event: dict | None = None) -> dict:
    """Insert or update one chat; optionally append a journal row in the same lock."""
    key = f"{app}:{native_id}"
    with locked("state"):
        directory = directory_load()
        entry = directory["entries"].get(key) or {"key": key, "app": app, "id": native_id,
                                                  "first_seen_ns": time.time_ns(), "registrations": 0}
        for name, value in fields.items():
            if name == "account" and isinstance(value, dict) and not value.get("id") \
                    and (entry.get("account") or {}).get("id"):
                continue  # an unreadable account never erases a recorded one; a re-registration only adds
            if value is not None:
                entry[name] = value
        entry["last_seen_ns"] = time.time_ns()
        entry["registrations"] = int(entry.get("registrations", 0)) + 1
        directory["entries"][key] = entry
        _write_json_atomic(device_dir() / "directory.json", directory)
        if journal_event is not None:
            _append_journal_locked(dict(journal_event, entry_key=key))
        return entry


# ----------------------------------------------------------------- accounts
def claude_account() -> dict:
    """Read ONLY oauthAccount.accountUuid from ~/.claude.json. Nothing else is kept."""
    path = claude_json_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"app": "claude", "id": None, "source": "~/.claude.json oauthAccount.accountUuid",
                "error": "unreadable"}
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    value = account.get("accountUuid") if isinstance(account, dict) else None
    del data, account
    ok = isinstance(value, str) and UUID_RE.fullmatch(value) is not None
    return {"app": "claude", "id": value if ok else None,
            "source": "~/.claude.json oauthAccount.accountUuid"}


LOADED_RE = re.compile(r"Loaded (\d+) persisted sessions from (.+?claude-code-sessions[\\/]"
                       r"([0-9a-f-]{36})[\\/]([0-9a-f-]{36}))")
LOG_TAIL_BYTES = 2_000_000


def desktop_log_path() -> Path:
    override = os.environ.get("CROSSTALK_P0_CLAUDE_DESKTOP_LOG")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(home() / "AppData" / "Local")
    return Path(local) / "Claude" / "logs" / "main.log"


def desktop_real_path(path: str) -> str:
    """Claude Desktop is an MSIX package: Windows redirects its %APPDATA% writes to
    %LOCALAPPDATA%\\Packages\\Claude_<id>\\LocalCache\\Roaming, and only processes inside the package see the
    original path (live 2026-10-09: a Codex-app process got FileNotFoundError on the path main.log names).
    Returns the redirected copy when the named path is missing and that copy exists."""
    if Path(path).exists():
        return path
    appdata = os.environ.get("APPDATA") or str(home() / "AppData" / "Roaming")
    local = os.environ.get("LOCALAPPDATA") or str(home() / "AppData" / "Local")
    try:
        relative = Path(path).relative_to(appdata)
        packages = sorted(Path(local, "Packages").glob("Claude_*"))
    except (ValueError, OSError):
        return path
    for package in packages:
        candidate = package / "LocalCache" / "Roaming" / relative
        if candidate.exists():
            return str(candidate)
    return path


def desktop_log_paths() -> list[Path]:
    """main.log first, then its rotated copies main1.log, main2.log, ... (Desktop rotates at ~10 MB and on a
    restart; live 2026-10-09 the fresh main.log named no account for an hour while main1.log did)."""
    path = desktop_log_path()
    rotated = []
    try:
        rotated = sorted((p for p in path.parent.glob(path.stem + "[0-9]*" + path.suffix) if p != path),
                         key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))[:3]
    except OSError:
        pass
    return [path] + rotated


TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def desktop_load_id(line: str) -> str:
    """The id of one Desktop load: a hash of its whole main.log line. The helper and the relink code must
    compute the same id, so both call this."""
    return "LD-" + hashlib.sha256(line.encode("utf-8")).hexdigest()[:12]


def desktop_log_time(line: str) -> float | None:
    """The line's own timestamp (local time) as epoch seconds, or None."""
    stamp = TS_RE.match(line)
    if stamp:
        with contextlib.suppress(ValueError, OverflowError):
            return time.mktime(time.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S"))
    return None


def _scan_desktop_log_full(path: Path) -> tuple[bool, dict | None, dict | None]:
    """(said anything, active account, newest load) from one log's tail. The newest load is the last whole
    'Loaded N persisted sessions' line with N > 0, unless a later logout line makes the account unknown."""
    try:
        size = path.stat().st_size
        start = max(0, size - LOG_TAIL_BYTES)
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return False, None, None
    spoke, active, load = False, None, None
    lines = data.splitlines()
    whole = data.endswith((b"\n", b"\r"))
    for index, raw in enumerate(lines):
        line = raw.decode("utf-8", "replace")
        if "[LocalSessionManager] Account logged out" in line:
            spoke, active, load = True, None, None
            continue
        match = LOADED_RE.search(line)
        if match and int(match.group(1)) > 0:
            spoke = True
            active = {"id": match.group(3).lower(), "org": match.group(4).lower(), "folder": match.group(2)}
            # A cut first line or an unfinished last line would hash differently from the whole line.
            cut = (start > 0 and index == 0) or (not whole and index == len(lines) - 1)
            load = None if cut else dict(active, load_id=desktop_load_id(line), log_s=desktop_log_time(line))
    return spoke, active, load


def _scan_desktop_log(path: Path) -> tuple[bool, dict | None]:
    """(said anything, active account) from one log's tail."""
    spoke, active, _load = _scan_desktop_log_full(path)
    return spoke, active


REMOTE_EVENT_RE = re.compile(r"(Enabling|Disabling) remote control for session (local_[0-9a-f-]{36})")
USER_TITLE_RE = re.compile(r"Updated session (local_[0-9a-f-]{36}): \{.*titleSource: 'user'")


def desktop_remote_runs() -> list[dict]:
    """Desktop's runs, oldest first, from the bounded tails of main.log and its rotated copies. A run starts at a
    'Loaded N persisted sessions' line with N > 0 and lasts to the next such line. Each run has the load_id and
    account id of its start line and `remote`: {Desktop chat id: True|False} from the LAST 'Enabling' or
    'Disabling remote control for session local_<id>' line of that run, and `named`: the chat ids given a title
    through Desktop's own rename ('Updated session ... titleSource: 'user'') after their last Enabling line of the
    run; that rename reached the remote session (live 2026-10-24, phone and a second computer). Read-only; reads at
    most LOG_TAIL_BYTES per file, never a whole log."""
    runs: list[dict] = []
    for path in reversed(desktop_log_paths()):  # rotated copies are older than main.log
        try:
            size = path.stat().st_size
            start = max(0, size - LOG_TAIL_BYTES)
            with open(path, "rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:
            continue
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # the first line of a tail read is cut
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            match = LOADED_RE.search(line)
            if match and int(match.group(1)) > 0:
                runs.append({"load_id": desktop_load_id(line), "account": match.group(3).lower(), "remote": {},
                             "named": set()})
                continue
            event = REMOTE_EVENT_RE.search(line)
            if event and runs:
                runs[-1]["remote"][event.group(2)] = event.group(1) == "Enabling"
                runs[-1]["named"].discard(event.group(2))
                continue
            renamed = USER_TITLE_RE.search(line)
            if renamed and runs and runs[-1]["remote"].get(renamed.group(1)) is True:
                runs[-1]["named"].add(renamed.group(1))
    return runs


def remote_on_before_load(load_id: str, account_id: str) -> set[str]:
    """Desktop chat ids whose Remote Control was ON at the end of the most recent EARLIER run of the same account
    as this load (the run before it, skipping other accounts' runs). When the log tails hold no earlier run of that
    account, the most recent earlier run of the other account answers: the owner's chat sync keeps both accounts'
    chat lists as copies under the same Desktop chat ids (live 2026-10-13: a switch left a chat that Desktop's
    account preference had turned on without a reconnect). Empty when the load or any earlier run is not in the
    log tails: unknown stays unknown, never a guess."""
    runs = desktop_remote_runs()
    index = next((i for i, run in enumerate(runs) if run["load_id"] == load_id), None)
    if index is None or index == 0:
        return set()
    earlier = runs[:index]
    run = next((run for run in reversed(earlier) if run["account"] == account_id.lower()), earlier[-1])
    return {local for local, on in run["remote"].items() if on}


def remote_enabled_since_load(load_id: str) -> set[str]:
    """Desktop chat ids that Desktop itself turned Remote Control ON for after this load line (the last
    'Enabling/Disabling remote control for session' line of the load's own run says Enabling). Those chats got
    their link back without help. Empty when the load is not in the log tails or the log is unreadable: unknown
    stays unknown, so the caller must not skip anything (a reconnect for an already-on chat is harmless)."""
    run = next((run for run in desktop_remote_runs() if run["load_id"] == load_id), None)
    return {local for local, on in run["remote"].items() if on} if run else set()


def desktop_active_account() -> dict | None:
    """Claude Desktop's active account, from main.log (same method as the owner's chat sync): the last
    'Loaded N persisted sessions from <...claude-code-sessions\\<account>\\<org>>' line names it. An empty
    folder (N == 0) is the in-between folder Desktop passes through on a switch and is skipped; a later
    'Account logged out' line makes the answer unknown. A log that says neither (fresh after rotation) defers
    to the newest rotated copy that does. Read-only; returns None when no log says anything."""
    active = None
    for path in desktop_log_paths():
        spoke, active = _scan_desktop_log(path)
        if spoke:
            break
    if active:
        active["folder"] = desktop_real_path(active["folder"])
    return active


def current_account() -> dict:
    """The account chats are checked against: Desktop's active account, else ~/.claude.json (which a CLI
    login rewrites). `source` records which one answered."""
    active = desktop_active_account()
    if active and UUID_RE.fullmatch(active["id"]):
        return {"app": "claude", "id": active["id"], "source": "claude-desktop main.log",
                "folder": active["folder"]}
    fallback = claude_account()
    return dict(fallback, source="fallback " + str(fallback.get("source")))


def account_check() -> dict:
    """Compare Claude Desktop's active account with the Claude CLI login (detect and tell only; read-only,
    IDs only). "unknown" on either side is never "match": chats created through the CLI and letters routed
    by the CLI login land on the CLI account, so a mismatch means they land on the wrong one."""
    desktop = desktop_active_account()
    desktop_id = desktop["id"] if desktop and UUID_RE.fullmatch(desktop["id"]) else None
    cli_id = claude_account().get("id")
    result = {"state": "unknown", "desktop": desktop_id, "cli": cli_id}
    if desktop_id and cli_id and desktop_id == cli_id:
        result.update(state="match", message="Claude Desktop and the Claude CLI are signed in to the same account.")
    elif desktop_id and cli_id:
        result.update(state="mismatch", command="claude auth login",
                      message=f"Claude Desktop is on account {desktop_id} but the Claude CLI is logged in as "
                              f"{cli_id}; run `claude auth login` with the Desktop account.")
    else:
        missing = " and ".join(name for name, value in (("Desktop", desktop_id), ("CLI", cli_id)) if not value)
        result["message"] = f"Account match unknown: could not read the {missing} account ID."
    return result


# ------------------------------------------------- CLI login prompt on mismatch
# Owner request 2026-10-13: when Claude Desktop and the Claude CLI are on different accounts, ask whether to log the
# CLI in to the Desktop account. Desktop signs its chats in through its own channel (no reusable token reaches
# them), so the CLI login cannot be copied from Desktop; the owner finishes `claude auth login` in the browser.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){1,8}\Z")
LOGIN_PROMPT_TITLE = "Crosstalk: Claude CLI account"


def accounts_path() -> Path:
    return device_dir() / "accounts.json"


def desktop_account_emails() -> dict[str, str]:
    """Desktop account ID -> email, as remembered by remember_desktop_account."""
    value = _read_json(accounts_path(), {})
    emails = value.get("emails") if isinstance(value, dict) else None
    if not isinstance(emails, dict):
        return {}
    return {k: v for k, v in emails.items() if isinstance(k, str) and isinstance(v, str) and EMAIL_RE.fullmatch(v)}


def remember_desktop_account(env=None) -> bool:
    """Claude Desktop starts each chat's CLI with CLAUDE_CODE_ACCOUNT_UUID and CLAUDE_CODE_USER_EMAIL, and a Crosstalk
    command run from such a chat inherits them. Record that pair in the device folder so the login prompt can name and
    pre-fill the Desktop account's email. The ID and email only, never a token. True when it wrote."""
    env = os.environ if env is None else env
    account = str(env.get("CLAUDE_CODE_ACCOUNT_UUID") or "").lower()
    email = str(env.get("CLAUDE_CODE_USER_EMAIL") or "")
    if not (UUID_RE.fullmatch(account) and EMAIL_RE.fullmatch(email)):
        return False
    known = desktop_account_emails()
    if known.get(account) == email:
        return False
    known[account] = email
    _write_json_atomic(accounts_path(), {"version": 1, "emails": known})
    return True


def standalone_cli_env() -> dict:
    """The environment for the owner's own `claude auth login`: without Desktop's per-chat CLAUDE_CODE_* and
    ANTHROPIC_* settings, so it signs in the standalone CLI and not a Desktop-hosted session."""
    env = child_env()[0]
    for name in list(env):
        if name.upper().startswith(("CLAUDE_CODE_", "ANTHROPIC_")) or name.upper() == "CLAUDECODE":
            del env[name]
    return env


def login_prompt_text(email: str | None) -> str:
    account = f"the account you use in Claude Desktop ({email})" if email else "the account you use in Claude Desktop"
    return ("Crosstalk uses the Claude CLI to wake and create Claude chats, but the CLI is signed in to a different "
            "account than Claude Desktop.\n\n"
            f"Would you like to log the CLI in to {account}?\n\n"
            "Yes opens a window running `claude auth login`. Finish the sign-in in your browser with that account.")


def login_argv(exe: str, email: str | None) -> list[str]:
    return [exe, "auth", "login"] + (["--email", email] if email else [])


def _ask_yes_no(title: str, text: str) -> bool:
    """A Yes/No box brought to the front (owner, 2026-10-25: it must not open unseen behind other windows). The box
    comes from a detached background process, which Windows keeps from taking the foreground even with
    MB_SETFOREGROUND, so a side thread brings it forward once it shows (crosstalk_focus.forward_own_box)."""
    if sys.platform != "win32":
        return False
    import ctypes
    import threading
    focus = _focus_module()
    threading.Thread(target=focus.forward_own_box, args=(title,), daemon=True, name="crosstalk-login-box").start()
    flags = 0x04 | 0x20 | 0x10000 | 0x40000  # MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags) == 6  # IDYES


def _launch_login_console(argv: list[str]) -> None:
    """Open `claude auth login` in its own console, then bring that console and the browser sign-in page it opens to
    the front (crosstalk_focus.follow_sign_in; owner, 2026-10-25). Runs in the prompt's own process, so following the
    sign-in for a few minutes blocks nothing else."""
    focus = _focus_module()
    platform = focus.default_platform()
    before = platform.top_windows()
    platform.allow_any_foreground()  # the owner just clicked Yes, so this process may pass the foreground on
    subprocess.Popen(argv, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), env=standalone_cli_env(),
                     close_fds=True)
    focus.log_record(focus_log_path(), {"tag": focus.TAG, "kind": "sign-in", "at_ns": time.time_ns(),
                                        **focus.follow_sign_in(before, platform=platform)})


def account_login_prompt(ask=None, launch=None) -> dict:
    """Ask the owner (a Yes/No box) whether to log the CLI in to the Desktop account; on Yes open `claude auth login`
    in a console, pre-filled with the Desktop account's email when known. Only on a mismatch; never logs in or reads
    a credential itself. Blocks until answered: the helper runs it in its own process (spawn_login_prompt). The box,
    the console and the browser page come to the front when real focus handling is on (cmd_account_login)."""
    check = account_check()
    if check["state"] != "mismatch":
        return {"ok": True, "asked": False, "state": check["state"]}
    exe = claude_exe()
    if not exe:
        return {"ok": False, "asked": False, "reason": "claude-cli-not-found"}
    email = desktop_account_emails().get(str(check["desktop"]).lower())
    marker = login_prompt_marker_path()
    _write_json_atomic(marker, {"pid": os.getpid(), "ts_ms": int(time.time() * 1000)})
    try:  # the helper's rename-now pass waits while this file says the box (or the sign-in after Yes) is open
        if not (ask or _ask_yes_no)(LOGIN_PROMPT_TITLE, login_prompt_text(email)):
            return {"ok": True, "asked": True, "answer": "no"}
        (launch or _launch_login_console)(login_argv(exe, email))
        return {"ok": True, "asked": True, "answer": "yes", "launched": True, "email_prefilled": bool(email)}
    finally:
        with contextlib.suppress(OSError):
            marker.unlink()


LOGIN_PROMPT_OPEN_MAX_S = 1800.0   # a marker older than this is not trusted (a box left open all day blocks nothing)


def login_prompt_marker_path() -> Path:
    return helper_dir() / "login-prompt-open.json"


def login_prompt_open(now: float | None = None) -> bool:
    """True while the CLI-login Yes/No box (and the sign-in it opens) is up: its process wrote a marker with its pid
    and removes it when done. A dead pid or an old marker does not count."""
    row = _read_json(login_prompt_marker_path(), None)
    if not isinstance(row, dict) or not pid_alive(row.get("pid")):
        return False
    stamp = row.get("ts_ms")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return False
    return ((now if now is not None else time.time()) * 1000 - stamp) / 1000.0 <= LOGIN_PROMPT_OPEN_MAX_S


def spawn_login_prompt() -> None:
    """Run account_login_prompt in its own windowless process (the message box blocks until answered)."""
    cli = Path(__file__).resolve().parent / "crosstalk_p0_cli.py"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | NO_WINDOW
    subprocess.Popen([windowless_python(), "-I", "-B", str(cli), "account-login"], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def desktop_lookup(account: dict, session_id: str) -> tuple[str, tuple[str, dict] | None]:
    """Read-only: ("found", (Desktop chat ID local_<uuid> = the file name, its record)) for the
    <active account folder>\\local_*.json whose cliSessionId is this session; ("absent", None) when every
    record was read and none matches; ("unreadable", None) when this process could not list or read them
    (live 2026-10-09: the Codex app's processes saw no record that Desktop chats see)."""
    folder = account.get("folder")
    if not folder or not UUID_RE.fullmatch(session_id):
        return "absent", None
    failed = False
    try:
        for path in sorted(Path(folder).glob("local_*.json"))[:5000]:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt record may be the very one for this chat: unreadable, never "absent".
                failed = True
                continue
            if isinstance(row, dict) and row.get("cliSessionId") == session_id:
                return "found", (path.stem, row)
    except OSError:
        failed = True
    if not failed and not Path(folder).is_dir():
        failed = True
    return ("unreadable" if failed else "absent"), None


def desktop_record(account: dict, session_id: str) -> tuple[str, dict] | None:
    return desktop_lookup(account, session_id)[1]


def desktop_has_chat(account: dict, session_id: str) -> bool:
    return desktop_record(account, session_id) is not None


def desktop_sessions_base() -> Path:
    """Claude Desktop's claude-code-sessions folder (<account>/<org>/local_*.json below it). No reads."""
    appdata = os.environ.get("APPDATA") or str(home() / "AppData" / "Roaming")
    override = os.environ.get("CROSSTALK_P0_CLAUDE_DESKTOP_DIR")
    return Path(override) if override else Path(desktop_real_path(str(Path(appdata) / "Claude" / "claude-code-sessions")))


def claude_desktop_account_folders() -> list[dict]:
    """Names (account UUIDs) of Claude Desktop's per-account chat folders. No file reads."""
    base = desktop_sessions_base()
    current = claude_account()["id"]
    found = []
    try:
        for child in sorted(base.iterdir()):
            if child.is_dir() and UUID_RE.fullmatch(child.name):
                found.append({"account_id": child.name, "mtime_ns": child.stat().st_mtime_ns,
                              "matches_claude_json": child.name == current})
    except OSError:
        pass
    return found


class AppServer:
    """Minimal JSONL client for `codex app-server` (stdio).

    Read callers use account/read and thread reads. Detached creation and delivery workers own their private
    writing server for the full turn lifetime; closing a server kills its running work."""

    def __init__(self, exe: str, kill_on_close: bool = False):
        self.process = subprocess.Popen(
            _exe_argv(exe) + ["app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, env=child_env()[0])
        self.lines: queue.Queue = queue.Queue()
        self._next = 0
        # The headless Codex wake runs the server inside a Windows job object that kills it when the waiter's
        # handle closes (a crash included), so a dead waiter never leaves the chat locked. job_ok is False when
        # that protection was asked for on Windows and could not be set up.
        self.job = _kill_on_close_job(self.process) if kill_on_close else None
        self.job_ok = not (kill_on_close and os.name == "nt") or self.job is not None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for raw in self.process.stdout:
                try:
                    self.lines.put(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
        finally:
            self.lines.put(None)

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def send_request(self, method: str, params: dict) -> int:
        """Send a request without waiting; the caller reads the response from `lines` by the returned id."""
        self._next += 1
        self.send({"method": method, "id": self._next, "params": params})
        return self._next

    def reply(self, request_id, result=None, error: dict | None = None) -> None:
        """Answer a request the server sent to us."""
        self.send({"id": request_id, "error": error} if error is not None else {"id": request_id, "result": result or {}})

    def request(self, method: str, params: dict, timeout: float):
        self._next += 1
        request_id = self._next
        self.send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            try:
                message = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(method)
            if message is None:
                raise ProbeError("app-server-exited")
            if message.get("id") == request_id:
                if "error" in message:
                    raise ProbeError("app-server-error", clip_reason(message["error"].get("message", "")))
                return message.get("result")

    def start(self, experimental: bool, timeout: float, client_name: str = "crosstalk_p0_probe"):
        title = "Crosstalk P0 probe" if client_name == "crosstalk_p0_probe" else client_name.replace("_", " ").capitalize()
        params = {"clientInfo": {"name": client_name, "title": title, "version": "0.0.1"}}
        if experimental:
            params["capabilities"] = {"experimentalApi": True}
        self.request("initialize", params, timeout)
        self.send({"method": "initialized", "params": {}})

    def close(self):
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        with contextlib.suppress(OSError):
            self.process.kill()
        with contextlib.suppress(Exception):
            self.process.wait(timeout=5)
        if self.job is not None:
            with contextlib.suppress(Exception):
                self.job[0].CloseHandle(self.job[1])
            self.job = None


def _kill_on_close_job(process):
    """Windows only: a job object with kill-on-close holding `process`. Returns (kernel32, handle) or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Counters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))                 or not kernel32.AssignProcessToJobObject(handle, int(process._handle)):
            kernel32.CloseHandle(handle)
            return None
        return kernel32, handle
    except Exception:
        return None


_ACCOUNT_CACHE: dict = {"at": 0.0, "value": None}


def codex_account(timeout: float = 20.0, use_cache: bool = True) -> dict:
    """Codex account via the documented app-server `account/read` (refreshToken:false).

    Stable shape: {type, email, planType}; only the email's hash is kept. The
    experimental `workspaceRouting.chatgptAccountId` is requested through the
    experimentalApi opt-in and kept when present."""
    if use_cache and _ACCOUNT_CACHE["value"] and time.monotonic() - _ACCOUNT_CACHE["at"] < 300:
        return dict(_ACCOUNT_CACHE["value"])
    exe = codex_exe()
    base = {"app": "codex", "id": None, "source": "codex app-server account/read"}
    if not exe:
        return dict(base, error="codex-cli-unavailable")
    for experimental in (True, False):
        server = None
        try:
            server = AppServer(exe)
            server.start(experimental, timeout)
            result = server.request("account/read", {"refreshToken": False}, timeout)
        except (OSError, TimeoutError, ProbeError) as error:
            if experimental:
                continue
            return dict(base, error=getattr(error, "reason", type(error).__name__))
        finally:
            if server:
                server.close()
        account = result.get("account") if isinstance(result, dict) else None
        routing = result.get("workspaceRouting") if isinstance(result, dict) else None
        value = dict(base, experimental_opt_in=experimental,
                     requires_openai_auth=result.get("requiresOpenaiAuth") if isinstance(result, dict) else None)
        if isinstance(account, dict):
            value["type"] = account.get("type")
            value["plan"] = account.get("planType")
            email = account.get("email")
            if isinstance(email, str) and email:
                value["email_sha256_12"] = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]
        else:
            value["type"] = None
        if isinstance(routing, dict) and isinstance(routing.get("chatgptAccountId"), str):
            value["id"] = routing["chatgptAccountId"]
            value["id_source"] = "account/read workspaceRouting.chatgptAccountId (experimental)"
        _ACCOUNT_CACHE.update(at=time.monotonic(), value=dict(value))
        return value
    return dict(base, error="account-read-failed")


# --------------------------------------------------------- native readbacks
def pid_alive(pid: int) -> bool:
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
    # Never os.kill on Windows: any signal terminates the process.
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


def claude_registration(session_id: str) -> dict | None:
    """Native live-session registration from ~/.claude/sessions/*.json (never *.key).

    Returns {live, name, status, cwd, entrypoint} or None when the inventory is unreadable."""
    folder = claude_home() / "sessions"
    try:
        paths = sorted(folder.glob("*.json"))[:512]
    except OSError:
        return None
    if not folder.is_dir():
        return None
    found = None
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("sessionId") == session_id:
            found = {"live": pid_alive(row.get("pid")), "name": row.get("name") if isinstance(row.get("name"), str) else None,
                     "status": row.get("status") if isinstance(row.get("status"), str) else None,
                     "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                     "entrypoint": row.get("entrypoint") if isinstance(row.get("entrypoint"), str) else None}
            if found["live"]:
                break
    return found or {"live": False, "name": None, "status": None, "cwd": None, "entrypoint": None}


def claude_transcript_path(session_id: str) -> Path | None:
    if not UUID_RE.fullmatch(session_id):
        return None
    try:
        for match in (claude_home() / "projects").glob(f"*/{session_id}.jsonl"):
            return match
    except OSError:
        pass
    return None


def _has_ref(text: str, marker: str) -> bool:
    """A bare framed letter ending in its exact ref proves input, never a mention.
    Desktop merges letters that queued behind a busy turn into one input, so a ref
    followed directly by the next letter's header counts too (3B.4, 2026-10-18)."""
    text = text.strip()
    ref = f"\nref {marker}"
    return text.startswith("Crosstalk message from ") and (
        text.endswith(ref) or f"{ref}\nCrosstalk message from " in text)


def claude_arrivals(session_id: str, marker: str, offset: int = 0,
                    timeout: float = READBACK_TIMEOUT_S) -> tuple[int | None, str]:
    """Count distinct user rows (by uuid) in the recipient transcript containing marker.

    Hidden attachments and queue bookkeeping do not prove visible arrival. The
    receiving plugin re-posts the letter as a normal user input; only that counts.
    Returns (count, note); count is None when the transcript is unavailable or capped.
    Rows, not text occurrences: tool results and assistant rows are excluded."""
    path = claude_transcript_path(session_id)
    if path is None:
        return None, "transcript-not-found"
    try:
        deadline = time.monotonic() + timeout
        size = path.stat().st_size
        start = offset if 0 <= offset <= size else 0
        seen = set()
        total = 0
        with path.open("rb") as stream:
            stream.seek(start)
            for raw in stream:
                if time.monotonic() >= deadline:
                    return None, "transcript-readback-timeout"
                total += len(raw)
                if total > 64 * 1024 * 1024:
                    return None, "transcript-scan-capped"
                if marker.encode("utf-8") not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("type") == "user" and not row.get("isMeta"):
                    message = row.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                else:
                    continue
                texts = [content] if isinstance(content, str) else [
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"] if isinstance(content, list) else []
                if any(_has_ref(text, marker) for text in texts):
                    seen.add(row.get("uuid") or hashlib.sha256(raw).hexdigest())
        return len(seen), "complete-scan"
    except OSError:
        return None, "transcript-unreadable"


ROLLOUT_SCAN_CHUNK = 8 * 1024 * 1024
ROLLOUT_SCAN_MAX_BYTES = 512 * 1024 * 1024  # most one readback reads from a rollout (rows with images make them huge)
ROLLOUT_TAIL_SCAN_BYTES = 256 * 1024 * 1024  # a letter with no recorded offset: only this much of the end is scanned
ROLLOUT_LINE_WINDOW = 256 * 1024             # a row holding the marker must fit this window to count as an input row


def _codex_user_input_texts(row) -> tuple[str, list[str], str | None]:
    """(kind, texts, id) when row is a genuine Codex user-input row, else ("", [], None). Verified shapes
    (2026-10-20, real rollouts): response_item/message/role user with input_text blocks, and the event
    item_completed whose item is a UserMessage with text blocks (the same input twice), plus the older
    event_msg user_message. Assistant, tool and developer rows are never input, so a row that only quotes the
    letter id does not count."""
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        return "", [], None
    kind, inner = row.get("type"), payload.get("type")
    if kind == "response_item" and inner == "message" and payload.get("role") == "user":
        blocks = payload.get("content")
        return "response", ([b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "input_text"
                             and isinstance(b.get("text"), str)] if isinstance(blocks, list) else []), None
    if kind == "event_msg" and inner == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "UserMessage" and isinstance(item.get("content"), list):
            return "item", [b.get("text", "") for b in item["content"] if isinstance(b, dict) and b.get("type") == "text"
                            and isinstance(b.get("text"), str)], item.get("id") if isinstance(item.get("id"), str) else None
    if kind == "event_msg" and inner == "user_message" and isinstance(payload.get("message"), str):
        return "event", [payload["message"]], None
    return "", [], None


def codex_rollout_arrivals(path: Path, marker: str, offset: int | None = None,
                           timeout: float = READBACK_TIMEOUT_S) -> tuple[int | None, str]:
    """Count the distinct user inputs in a Codex rollout file that are this letter. Never loads the file: it reads
    8 MB chunks, looks for the marker bytes, and parses only the one row around a hit (rollouts reach hundreds of
    MB because of image rows). With `offset` (the file size when the letter was sent) it scans forward from there;
    without one it scans only the last ROLLOUT_TAIL_SCAN_BYTES. Bounded by bytes and time; (None, note) when a
    bound stopped it. A zero from a tail scan only means "not seen in the tail"."""
    needle = marker.encode("utf-8")
    try:
        deadline = time.monotonic() + timeout
        size = path.stat().st_size
        known = isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= size
        start = offset if known else max(0, size - ROLLOUT_TAIL_SCAN_BYTES)
        if size - start > ROLLOUT_SCAN_MAX_BYTES:
            return None, "rollout-scan-capped"
        counts: dict[str, set] = {}
        seen_lines: set[int] = set()
        with path.open("rb") as stream:
            position, carry = start, b""
            while position < size:
                if time.monotonic() >= deadline:
                    return None, "rollout-readback-timeout"
                stream.seek(position)
                chunk = stream.read(min(ROLLOUT_SCAN_CHUNK, size - position))
                if not chunk:
                    break
                data = carry + chunk
                base = position - len(carry)
                hit = data.find(needle)
                while hit >= 0:
                    absolute = base + hit
                    lo = max(0, absolute - ROLLOUT_LINE_WINDOW)
                    stream.seek(lo)
                    window = stream.read(absolute - lo + len(needle) + ROLLOUT_LINE_WINDOW)
                    local = absolute - lo
                    left = window.rfind(b"\n", 0, local)
                    right = window.find(b"\n", local)
                    # No newline either side inside the window: an oversized row, never a letter input row.
                    if (left >= 0 or lo == 0) and (right >= 0 or lo + len(window) >= size):
                        line_start = lo + left + 1
                        if line_start not in seen_lines:
                            seen_lines.add(line_start)
                            raw = window[left + 1: right if right >= 0 else len(window)]
                            try:
                                row = json.loads(raw)
                            except ValueError:
                                row = None
                            kind, texts, item_id = _codex_user_input_texts(row)
                            if any(_has_ref(text, marker) for text in texts):
                                counts.setdefault(kind, set()).add(item_id or line_start)
                    hit = data.find(needle, hit + 1)
                carry = data[-(len(needle) - 1):] if len(needle) > 1 else b""
                position += len(chunk)
        # The same input is written as several row kinds: the best-covered kind is the count, never their sum.
        return (max((len(v) for v in counts.values()), default=0),
                "rollout-scan" if known else "rollout-tail-scan")
    except OSError:
        return None, "rollout-unreadable"


def codex_arrivals(thread_id: str, marker: str, timeout: float = READBACK_TIMEOUT_S,
                   offset: int | None = None) -> tuple[int | None, str]:
    """Count user inputs on a Codex chat that are this letter. The rollout file is read directly (bounded scan,
    see codex_rollout_arrivals): app-server thread/read with includeTurns loads the whole thread and times out
    on a rollout of hundreds of MB (live 2026-10-20: two letters that had landed stayed held for that reason).
    thread/read stays only as the fallback when no rollout file is found (it is bounded by the timeout)."""
    path = codex_rollout_path(thread_id)
    if path is not None:
        return codex_rollout_arrivals(path, marker, offset, timeout)
    exe = codex_exe()
    if not exe:
        return None, "codex-cli-unavailable"
    server = None
    try:
        deadline = time.monotonic() + timeout
        server = AppServer(exe)
        server.start(False, max(0.001, deadline - time.monotonic()))
        result = server.request("thread/read", {"threadId": thread_id, "includeTurns": True},
                                max(0.001, deadline - time.monotonic()))
    except (OSError, TimeoutError, ProbeError) as error:
        return None, getattr(error, "reason", type(error).__name__)
    finally:
        if server:
            server.close()
    thread = result.get("thread") if isinstance(result, dict) else None
    turns = thread.get("turns") if isinstance(thread, dict) else None
    if not isinstance(turns, list):
        return None, "thread-turns-unavailable"
    seen = set()
    for turn_index, turn in enumerate(turns):
        for item_index, item in enumerate((turn.get("items") or []) if isinstance(turn, dict) else []):
            if isinstance(item, dict) and item.get("type") == "userMessage":
                texts = [block.get("text", "") for block in (item.get("content") or [])
                         if isinstance(block, dict) and block.get("type") == "text"]
                if any(_has_ref(text, marker) for text in texts):
                    seen.add(item.get("id") or (turn.get("id", turn_index), item_index))
    return len(seen), "thread-read"


def arrivals(letter: dict, timeout: float = READBACK_TIMEOUT_S) -> tuple[int | None, str]:
    recipient = letter.get("to") or {}
    marker = letter.get("letter_id", "")
    if recipient.get("app") == "claude":
        return claude_arrivals(recipient.get("id", ""), marker, int(letter.get("recipient_offset") or 0), timeout)
    if recipient.get("app") == "codex":
        offset = letter.get("recipient_offset")
        return codex_arrivals(recipient.get("id", ""), marker, timeout, offset if isinstance(offset, int) else None)
    if recipient.get("app") == "hermes":
        return hermes_arrivals(recipient.get("id", ""), marker, timeout)
    return None, "unknown-recipient-app"


# ------------------------------------------------------------ Hermes endpoint
# Hermes (NousResearch hermes-agent Desktop) is one local install with no account concept. Its chats are read from
# <HERMES_HOME>/state.db, opened read-only. Delivery goes through the Crosstalk plugin inside the Hermes Desktop
# backend: the core writes <HERMES_HOME>/crosstalk/inbox/<letter>.json, the plugin claims it, re-checks controls.json
# and calls ctx.inject_message(text, "user", session_key=<chat id>), then writes done/<letter>.result.json.
# inject_message returned True = the host accepted it (a busy chat queues it); False = not delivered (chat not open).
def hermes_home() -> Path:
    override = os.environ.get("CROSSTALK_P0_HERMES_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(home() / "AppData" / "Local")
    return Path(local) / "hermes"


def hermes_dir() -> Path:
    return hermes_home() / "crosstalk"


def hermes_installed() -> bool:
    """True when the Crosstalk plugin folder or its runtime folder exists. Discovery stays off otherwise, so a
    machine (or a test) without the plugin never reads Hermes' database."""
    return (hermes_home() / "plugins" / "crosstalk").is_dir() or hermes_dir().is_dir()


def hermes_db_connect(timeout: float = HERMES_DB_TIMEOUT_S):
    """Read-only connection to Hermes' state.db, or None when the file is absent. Never writes."""
    path = hermes_home() / "state.db"
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    connection.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + timeout
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    return connection


def hermes_sessions(session_id: str | None = None, limit: int = HERMES_DISCOVER_LIMIT) -> tuple[list[dict] | None, str]:
    """Recent non-archived, non-hidden Hermes chats (or one chat by exact id). (None, note) = unreadable = unknown."""
    try:
        connection = hermes_db_connect()
    except (sqlite3.Error, OSError) as error:
        return None, "state-db-unreadable:" + type(error).__name__
    if connection is None:
        return None, "state-db-not-found"
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "id" not in columns:
            return None, "state-db-schema-unknown"
        wanted = [c for c in ("id", "source", "title", "profile_name") if c in columns]
        where = ["COALESCE(archived,0)=0" if "archived" in columns else "1=1",
                 "COALESCE(hidden,0)=0" if "hidden" in columns else "1=1"]
        order = [c for c in ("last_activity_at", "started_at") if c in columns]
        query = f"SELECT {', '.join(wanted)} FROM sessions WHERE {' AND '.join(where)}"
        params: list = []
        if session_id is not None:
            query += " AND id = ?"
            params.append(session_id)
        if order:
            query += f" ORDER BY COALESCE({', '.join(order)}) DESC"
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
        rows = []
        for row in connection.execute(query, params):
            item = dict(zip(wanted, row))
            if isinstance(item.get("id"), str) and HERMES_ID_RE.fullmatch(item["id"]):
                rows.append(item)
        return rows, "state-db-read"
    except (sqlite3.Error, OSError) as error:
        return None, "state-db-unreadable:" + type(error).__name__
    finally:
        connection.close()


def _hermes_fields(item: dict) -> dict:
    title = item.get("title")
    title = " ".join(title.split())[:80] if isinstance(title, str) and title.strip() else None
    return {"title": title,
            "title_source": "hermes state.db sessions.title (display only, never used to select)" if title else None,
            "hermes_source": item.get("source") if isinstance(item.get("source"), str) else None,
            "profile": item.get("profile_name") if isinstance(item.get("profile_name"), str) else None,
            "project_folder": None, "source": "hermes-state-db"}


REASON_MAX = 500  # one bound for every refusal/uncertain reason: readable, still bounded in the journal and status


def clip_reason(text, limit: int = REASON_MAX) -> str:
    """Cut a reason to `limit` characters with a visible ellipsis, so a cut is never mistaken for the whole text."""
    text = str(text if text is not None else "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


CREATOR_TITLE_SOURCE = "owner/creator supplied at creation (display only, never used to select)"


def _creator_title_kept(entry: dict | None) -> bool:
    return bool(entry and str(entry.get("title_source") or "").startswith("owner/creator supplied")
                and entry.get("title"))


def hermes_discover(session_id: str | None = None) -> dict:
    """Register or refresh Hermes chats from state.db: the recent ones, or one exact id. Never raises.
    Unreadable state.db is unknown: nothing is removed. Writes the directory only when something changed."""
    if not hermes_installed():
        return {"read": False, "note": "hermes-plugin-not-installed", "registered": 0}
    try:
        rows, note = hermes_sessions(session_id)
        if rows is None:
            return {"read": False, "note": note, "registered": 0}
        changed = 0
        with locked("state"):
            directory = directory_load()
            for item in rows:
                key = "hermes:" + item["id"]
                fields = {k: v for k, v in _hermes_fields(item).items() if v is not None}
                entry = directory["entries"].get(key)
                if _creator_title_kept(entry):
                    # Hermes auto-titles a chat after its first turn; a creator-supplied title stays.
                    fields.pop("title", None)
                    fields.pop("title_source", None)
                if entry is None:
                    entry = {"key": key, "app": "hermes", "id": item["id"], "first_seen_ns": time.time_ns(),
                             "registrations": 0}
                elif all(entry.get(k) == v for k, v in fields.items()):
                    continue
                entry.update(fields)
                entry["last_seen_ns"] = time.time_ns()
                entry["registrations"] = int(entry.get("registrations", 0)) + 1
                directory["entries"][key] = entry
                changed += 1
            if changed:
                _write_json_atomic(device_dir() / "directory.json", directory)
        return {"read": True, "note": note, "seen": len(rows), "registered": changed}
    except Exception as error:  # discovery must never break a list or a send
        return {"read": False, "note": "discovery-error:" + type(error).__name__, "registered": 0}


def register_hermes(session_id: str, source: str = "tool-call") -> dict:
    """Register the calling Hermes chat (its title from state.db when readable). Raises ProbeError on a bad id."""
    if not isinstance(session_id, str) or not HERMES_ID_RE.fullmatch(session_id):
        raise ProbeError("invalid-session-id")
    rows, note = hermes_sessions(session_id, 1) if hermes_installed() else (None, "hermes-plugin-not-installed")
    fields = _hermes_fields(rows[0]) if rows else {"project_folder": None}
    fields = {k: v for k, v in fields.items() if v is not None or k == "project_folder"}
    if _creator_title_kept(directory_load()["entries"].get("hermes:" + session_id)):
        fields.pop("title", None)
        fields.pop("title_source", None)
    fields["source"] = source
    fields["title_read"] = note
    event = {"event": "status", "app": "hermes", "caller": {"session_id": session_id, "source": source}}
    return directory_register("hermes", session_id, fields, event)


PENDING_CREATE_SOURCE = "create-hermes-pending"


def register_hermes_pending(session_id: str, title: str) -> dict:
    """Called by the create connector right after Hermes made the chat and BEFORE its first prompt is submitted,
    so a Crosstalk letter sent in that first turn already carries the creator title (Hermes auto-titles later).
    The entry is marked pending; create_hermes overwrites it on success and removes it on any failure."""
    if not isinstance(session_id, str) or not HERMES_ID_RE.fullmatch(session_id):
        raise ProbeError("invalid-session-id")
    clean = " ".join(str(title).split())[:80]
    if not clean:
        raise ProbeError("title-invalid")
    fields = {"project_folder": None, "title": clean, "hermes_source": "desktop", "source": PENDING_CREATE_SOURCE,
              "title_source": CREATOR_TITLE_SOURCE}
    event = {"event": "register", "app": "hermes", "caller": {"session_id": session_id, "source": PENDING_CREATE_SOURCE}}
    return directory_register("hermes", session_id, fields, event)


def _drop_pending_create(session_id: str) -> None:
    """A failed create must not leave a live-looking entry: remove only the entry the connector pre-registered."""
    try:
        with locked("state"):
            directory = directory_load()
            entry = directory["entries"].get("hermes:" + session_id)
            if entry and entry.get("source") == PENDING_CREATE_SOURCE:
                del directory["entries"]["hermes:" + session_id]
                _write_json_atomic(device_dir() / "directory.json", directory)
    except Exception:
        pass


def hermes_heartbeat() -> dict | None:
    """The plugin's heartbeat file. It is replaced atomically every 5 s, and on Windows a read can collide with the
    replace (PermissionError), so a failed read is retried briefly before it counts as missing."""
    path = hermes_dir() / "heartbeat.json"
    for attempt in range(5):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.03 * (attempt + 1))
    return None


def hermes_listener_alive(now: float | None = None) -> bool:
    """A fresh heartbeat from a live process that hosts chats. Missing or unreadable means not running."""
    beat = hermes_heartbeat()
    if not beat or beat.get("hosts_chats") is not True or not pid_alive(beat.get("pid")):
        return False
    try:
        stamp = datetime.fromisoformat(str(beat.get("utc")))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return abs((now if now is not None else time.time()) - stamp.timestamp()) <= HERMES_HEARTBEAT_STALE_S


# ------------------------------------------------------ Hermes link health (slice 3B.17)
# Letters to Hermes pass through a plugin that depends on Hermes internals. When a Hermes update moves them, the
# plugin used to go silent and every letter was refused "hermes-not-running", which named the wrong cause (2026-10-26).
# Now the plugin writes a status record per process (<hermes>/crosstalk/status/<pid>.json), and the core tells four
# situations apart from all fresh records together: Hermes not running, plugin not loaded, plugin loaded but no host
# found (incompatible), plugin hosting but not listening. The link's durable state (versions, last self-test, the
# contract check, the repair offer) lives in <device>/link-hermes.json, which src/eutonos/crosstalk_link.py reads
# (pickup, doctor, dashboard, harness watch). The kit never imports eutonos, so that module also turns an offered
# repair into one pending action.
HERMES_STATUS_PROFILE = "crosstalk.hermes.status.v1"
HERMES_STATUS_FRESH_S = 90.0       # a plugin status record counts for this long (the plugin refreshes every 30 s)
HERMES_START_GRACE_S = 45.0        # a plugin that loaded this recently may not have seen its host publish yet
HERMES_PROBE_WAIT_S = 15.0
HERMES_PROBE_POLL_S = 0.2
HERMES_PROBE_ROUTE_IDS = 8
HERMES_PLUGIN_SOURCE = KIT / "hermes-plugin" / "crosstalk" / "__init__.py"
HERMES_FILE_READ_MAX = 8_000_000
LINK_PROFILE = "crosstalk.link.v1"
LINK_REPAIR_MODES = ("ask", "auto", "off")
LINK_REPAIR_DEFAULT = "ask"
LINK_BROKEN_STATES = ("plugin-incompatible", "plugin-not-listening", "plugin-not-loaded")
HERMES_LINK_REASONS = {"not-running": "hermes-not-running", "starting": "hermes-starting",
                       "plugin-not-loaded": "hermes-plugin-not-loaded",
                       "plugin-incompatible": "hermes-plugin-incompatible",
                       "plugin-not-listening": "hermes-plugin-not-listening"}
REPAIR_DEFAULTS = {"claude": ("sonnet", "high"), "codex": ("gpt-6-luna", "xhigh")}
REPAIR_TITLE = "Repair the Crosstalk link to Hermes"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except ValueError:
        return None


def hermes_status_records(now: float | None = None) -> list[dict]:
    """The plugin's fresh status records from live processes, newest first. A record is evidence that the plugin
    loaded in that process; it is ignored once its process is gone or it has not been refreshed for a while."""
    now = time.time() if now is None else now
    try:
        files = list((hermes_dir() / "status").glob("*.json"))
    except OSError:
        return []
    found = []
    for path in files:
        value = _read_json(path, None)
        if not isinstance(value, dict) or value.get("profile") != HERMES_STATUS_PROFILE:
            continue
        stamp = _parse_utc(value.get("utc"))
        if stamp is None or abs(now - stamp) > HERMES_STATUS_FRESH_S or not pid_alive(value.get("pid")):
            continue
        loaded = _parse_utc(value.get("loaded_utc")) or stamp
        found.append(dict(value, age_s=round(now - stamp, 1), loaded_age_s=round(now - loaded, 1)))
    found.sort(key=lambda record: record["age_s"])
    return found


def hermes_running_pids() -> list[int]:
    """Hermes processes that are alive, found WITHOUT the plugin: the gateway's pid file and live `serve` rows of
    spawn-ledger.json. Only pids are read."""
    home = hermes_home()
    pids = []
    try:
        text = (home / "gateway.pid").read_text(encoding="utf-8").strip()
        try:
            value = json.loads(text)
            value = value.get("pid") if isinstance(value, dict) else value
        except ValueError:
            value = None
        if type(value) is int:
            pids.append(value)
    except OSError:
        pass
    rows = _read_json(home / "spawn-ledger.json", [])
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("purpose") == "serve" and type(row.get("pid")) is int:
            pids.append(row["pid"])
    return sorted({pid for pid in pids if pid_alive(pid)})


def hermes_plugin_installed_version() -> str | None:
    """PLUGIN_VERSION of the plugin folder installed into Hermes, read as text (never imported)."""
    try:
        text = (hermes_home() / "plugins" / "crosstalk" / "__init__.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.search(r'^PLUGIN_VERSION = "([^"]+)"', text, re.M)
    return found.group(1) if found else None


def _version_tuple(value) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", str(value or ""))[:4])


def _probe_summary(records: list[dict]) -> list[dict]:
    """What the host checks found across all records: one row per check name, found if any process found it."""
    merged: dict[str, bool] = {}
    for record in records:
        for probe in record.get("probes") or []:
            if isinstance(probe, dict) and isinstance(probe.get("name"), str):
                merged[probe["name"]] = merged.get(probe["name"], False) or probe.get("found") is True
    return [{"name": name, "found": found} for name, found in sorted(merged.items())]


def hermes_link_diagnosis(now: float | None = None) -> dict:
    """Why a letter to Hermes can or cannot go, judged across ALL fresh plugin status records.

    Hermes loads the plugin in several processes and only one hosts chats, so one process finding no host is normal;
    only "every fresh record found none" means the plugin cannot find its host. Evidence windows: a status record
    counts for HERMES_STATUS_FRESH_S and only from a live pid, the listener heartbeat for HERMES_HEARTBEAT_STALE_S,
    and a process that loaded the plugin less than HERMES_START_GRACE_S ago keeps the answer at "starting", not incompatible.

    state: ok | not-running | starting | plugin-not-loaded | plugin-incompatible | plugin-not-listening."""
    now = time.time() if now is None else now
    records = hermes_status_records(now)
    hosting = [record for record in records if record.get("host")]
    pids = hermes_running_pids()
    installed = hermes_plugin_installed_version()
    out = {"hermes_running": bool(pids or records), "hermes_pids": pids[:4], "status_records": len(records),
           "hosting_records": len(hosting),
           "plugin_versions": sorted({str(record.get("plugin_version")) for record in records}),
           "hermes_versions": sorted({str(record.get("hermes_version")) for record in records if record.get("hermes_version")}),
           "host": hosting[0].get("host") if hosting else None, "checks": _probe_summary(records),
           "installed_plugin_version": installed, "utc": _utc_now()}
    if hermes_listener_alive(now):
        state = "ok"
    elif not pids and not records:
        state = "not-running"
    elif hosting:
        state = "plugin-not-listening"
    elif not records:
        state = "plugin-not-loaded"
    elif any(record["loaded_age_s"] <= HERMES_START_GRACE_S for record in records):
        state = "starting"
    else:
        state = "plugin-incompatible"
    out["state"] = state
    out["reason"] = None if state == "ok" else HERMES_LINK_REASONS[state]
    out["message"] = _diagnosis_message(state, out)
    return out


def _diagnosis_message(state: str, facts: dict) -> str:
    checks = ", ".join(f"{item['name']}: {'found' if item['found'] else 'not found'}" for item in facts["checks"])
    if state == "ok":
        return "The Crosstalk plugin is hosting Hermes chats and its listener is answering."
    if state == "not-running":
        return ("Hermes Desktop is not running: no live Hermes process was found and the Crosstalk plugin has not "
                "reported in.")
    if state == "starting":
        return ("Hermes is starting: the Crosstalk plugin has only just loaded and has not found its chat host yet. "
                "Try again in a minute.")
    if state == "plugin-not-loaded":
        installed = facts.get("installed_plugin_version")
        text = ("Hermes is running but the Crosstalk plugin has not reported in. It may not be installed or enabled, "
                "or Hermes has not been restarted since it was installed.")
        if installed and _version_tuple(installed) < (0, 4, 0):
            text += (f" The installed plugin ({installed}) is older than 0.4.0 and cannot report why it is silent: "
                     "it may be loaded but unable to find its host after a Hermes update.")
        elif not installed:
            text += " No Crosstalk plugin folder was found in Hermes' plugins folder."
        return text
    if state == "plugin-incompatible":
        return (f"The Crosstalk plugin is loaded in {facts['status_records']} Hermes process(es) (plugin "
                f"{', '.join(facts['plugin_versions'])}; Hermes {', '.join(facts['hermes_versions']) or 'unknown'}) but none "
                f"of them found the host that carries chats ({checks or 'no checks recorded'}). Hermes has probably "
                "changed internals the plugin relies on, for example after an update.")
    return ("The Crosstalk plugin found its chat host in Hermes but its listener is not answering: the heartbeat is "
            "missing or stale. Look at crosstalk/listener-error.json in the Hermes folder.")


# ---- the Hermes version and the internals contract
def hermes_source_root(records: list[dict] | None = None, home: Path | None = None) -> Path | None:
    """The installed Hermes source folder (local files only). CROSSTALK_P0_HERMES_SOURCE overrides (tests), then the
    folder a live plugin process reported, then <hermes home>/hermes-agent."""
    override = os.environ.get("CROSSTALK_P0_HERMES_SOURCE")
    if override:
        return Path(override) if Path(override).is_dir() else None
    for record in records if records is not None else hermes_status_records():
        where = record.get("hermes_source")
        if isinstance(where, str) and where and Path(where).is_dir():
            return Path(where)
    candidate = (home or hermes_home()) / "hermes-agent"
    return candidate if candidate.is_dir() else None


def hermes_version_info(root: Path | None = None) -> dict | None:
    """{version, commit, fingerprint, source_root} from Hermes' install-stamp.json, or None when unreadable."""
    root = root or hermes_source_root()
    if root is None:
        return None
    stamp = _read_json(root / "install-stamp.json", None)
    if not isinstance(stamp, dict):
        return None
    version = stamp.get("displayVersion") or stamp.get("baseVersion")
    if not version:
        return None
    commit = str(stamp["commit"])[:12] if stamp.get("commit") else None
    return {"version": str(version)[:40], "commit": commit,
            "fingerprint": f"{str(version)[:40]}+{commit}" if commit else str(version)[:40], "source_root": str(root)}


def hermes_contract_entries() -> list[dict]:
    """HERMES_CONTRACT from the plugin source (ast.literal_eval: the plugin is never imported). Bad entries are dropped."""
    import ast
    for path in (HERMES_PLUGIN_SOURCE, hermes_home() / "plugins" / "crosstalk" / "__init__.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "HERMES_CONTRACT"
                                                      for t in node.targets)):
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    break
                good = []
                for item in value if isinstance(value, (list, tuple)) else []:
                    if (isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("file"), str)
                            and isinstance(item.get("needs"), (list, tuple)) and item["needs"]
                            and all(isinstance(n, str) and n for n in item["needs"])
                            and not Path(item["file"]).is_absolute() and ".." not in Path(item["file"]).parts):
                        good.append({"id": item["id"], "file": item["file"], "needs": list(item["needs"]),
                                     "uses": str(item.get("uses") or "plugin"), "what": str(item.get("what") or "")})
                if good:
                    return good
    return []


def hermes_contract_check(root: Path | None = None) -> dict:
    """Check the plugin's declared Hermes internals against the INSTALLED Hermes source: each file must exist and
    still contain each text. Local files only, read-only, bounded. `missing` entries mean "this internal changed:
    look" (a moved internal and a removed one look the same); `unchecked` says why nothing could be judged."""
    entries = hermes_contract_entries()
    root = root or hermes_source_root()
    info = hermes_version_info(root) if root else None
    result = {"utc": _utc_now(), "ok": None, "checked": 0, "missing": [], "unchecked": None,
              "source_root": str(root) if root else None, "fingerprint": info["fingerprint"] if info else None,
              "contract_sha": hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:12]}
    if not entries:
        result["unchecked"] = "the plugin's internals list could not be read"
        return result
    if root is None or not Path(root).is_dir():
        result["unchecked"] = "the installed Hermes source folder was not found"
        return result
    for entry in entries:
        target = Path(root) / entry["file"]
        try:
            with target.open("rb") as stream:
                text = stream.read(HERMES_FILE_READ_MAX).decode("utf-8", errors="replace")
        except OSError as error:
            problem = "file-missing" if isinstance(error, FileNotFoundError) else "file-unreadable"
            result["missing"].append({"id": entry["id"], "what": entry["what"], "uses": entry["uses"],
                                      "file": entry["file"], "problem": problem, "needs": entry["needs"]})
            result["checked"] += 1
            continue
        result["checked"] += 1
        absent = [need for need in entry["needs"] if need not in text]
        if absent:
            result["missing"].append({"id": entry["id"], "what": entry["what"], "uses": entry["uses"],
                                      "file": entry["file"], "problem": "text-missing", "needs": absent})
    result["ok"] = not result["missing"]
    return result


# ---- the durable link record
def hermes_link_path() -> Path:
    return device_dir() / "link-hermes.json"


def hermes_link_load() -> dict:
    value = _read_json(hermes_link_path(), {})
    if isinstance(value, dict) and value.get("profile") == LINK_PROFILE:
        return value
    return {"profile": LINK_PROFILE, "endpoint": "hermes"}


def _link_mutate(change) -> dict:
    with locked("hermes-link", timeout=10.0):
        data = hermes_link_load()
        change(data)
        data["updated_utc"] = _utc_now()
        _write_json_atomic(hermes_link_path(), data)
        return data


def _version_row(info: dict, source: str) -> dict:
    return {"version": info["version"], "commit": info["commit"], "fingerprint": info["fingerprint"],
            "utc": _utc_now(), "source": source}


def hermes_link_record_install(plugin_version: str | None = None, home: Path | None = None) -> dict | None:
    """Called by hermes-install: remember the Hermes version the plugin was installed for. It is also the baseline a
    later "Hermes updated" notice compares against. Best effort; never blocks an install."""
    try:
        info = hermes_version_info(hermes_source_root(home=home))

        def change(data):
            version = data.setdefault("version", {})
            data["installed_plugin_version"] = plugin_version
            data["installed_utc"] = _utc_now()
            if info:
                version["installed"] = _version_row(info, "install")
                version["checked"] = _version_row(info, "install")
                version["current"] = _version_row(info, "install")
        return _link_mutate(change)
    except (OSError, TimeoutError, ValueError):
        return None


def hermes_version_notice(link: dict | None = None, info: dict | None = None) -> dict | None:
    """{from, to, text} when Hermes' installed version differs from the one Crosstalk last checked (an install, or a
    self-test that passed). None when it is the same, or when nothing was ever recorded (no false alarm)."""
    link = link if link is not None else hermes_link_load()
    info = info if info is not None else hermes_version_info()
    checked = (link.get("version") or {}).get("checked")
    if not isinstance(checked, dict) or not info or checked.get("fingerprint") == info["fingerprint"]:
        return None
    before, after = checked.get("version"), info["version"]
    if before == after:  # same release number, different build
        before, after = f"{before} ({checked.get('commit')})", f"{after} ({info['commit']})"
    return {"from": checked.get("version"), "to": info["version"],
            "text": f"Hermes updated from {before} to {after} since Crosstalk was last checked"}


# ---- repair offer
def link_repair_mode() -> str:
    """ask (default): record one offer for the owner; auto: start the repair chat; off: only diagnose."""
    value = _read_json(helper_dir() / "config.json", {})
    mode = value.get("link_repair_offer") if isinstance(value, dict) else None
    return mode if mode in LINK_REPAIR_MODES else LINK_REPAIR_DEFAULT


def hermes_repair_set_mode(mode: str) -> dict:
    if mode not in LINK_REPAIR_MODES:
        raise ProbeError("invalid-repair-mode", "one of " + ",".join(LINK_REPAIR_MODES))
    path = helper_dir() / "config.json"
    with locked("helper-config", timeout=10.0):
        value = _read_json(path, {})
        value = value if isinstance(value, dict) else {}
        value["link_repair_offer"] = mode
        _write_json_atomic(path, value)
    return {"ok": True, "link_repair_offer": mode, "config": str(path)}


def installed_harnesses() -> list[dict]:
    """Harnesses a repair chat can be started in, each with the model and effort a repair chat gets."""
    found = []
    for app, finder in (("claude", claude_exe), ("codex", codex_exe)):
        try:
            present = bool(finder())
        except Exception:
            present = False
        if present:
            found.append({"app": app, "model": REPAIR_DEFAULTS[app][0], "effort": REPAIR_DEFAULTS[app][1]})
    return found


def repair_prompt_path() -> Path:
    return eutonos_home() / "repair" / "hermes-link-repair.md"


def _checkout_root() -> Path | None:
    """The EUTONOS checkout this kit runs from, or None for a materialized kit (no checkout)."""
    if kit_mode():
        return None
    root = KIT.parent.parent
    return root if (root / "tools" / "test_shards.py").is_file() else None


def hermes_repair_prompt(data: dict) -> str:
    """The self-contained brief for whoever repairs the link: diagnosis, versions, failing internals, paths, how to
    test, and what NOT to do. Plain text; no secrets are read to build it."""
    diag = data.get("diagnosis") or {}
    version = data.get("version") or {}
    current, checked = version.get("current") or {}, version.get("checked") or {}
    contract, selftest = data.get("contract") or {}, data.get("selftest") or {}
    repair = data.get("repair") or {}
    checkout = _checkout_root()
    cli = (f"python {checkout / 'tools' / 'crosstalk' / 'crosstalk_p0_cli.py'}" if checkout else "eutonos-crosstalk")
    lines = ["# Repair the Crosstalk link to Hermes", "",
             f"Written by Crosstalk on {_utc_now()}. The owner's letters to Hermes chats are failing or at risk, "
             "and Crosstalk found the cause on the Hermes side of the link.", "", "## What is wrong", ""]
    lines += [f"- {item.get('text')}" for item in repair.get("reasons") or []] or ["- (no reason recorded)"]
    lines += ["", "## Evidence", "",
              f"- Hermes now: {current.get('version') or 'unknown'}"
              f"{' (build ' + current['commit'] + ')' if current.get('commit') else ''}",
              f"- Hermes when Crosstalk last checked: {checked.get('version') or 'never'}"
              f"{' (build ' + checked['commit'] + ')' if checked.get('commit') else ''}"
              f"{' via ' + str(checked.get('source')) if checked.get('source') else ''}",
              f"- Crosstalk plugin: installed {data.get('installed_plugin_version') or diag.get('installed_plugin_version') or 'unknown'}"
              f", in the kit source {_source_plugin_version() or 'unknown'}",
              f"- Diagnosis ({diag.get('utc')}): {diag.get('state')}: {diag.get('message')}"]
    if diag.get("checks"):
        lines.append("- Host checks the plugin ran, across all Hermes processes: "
                     + "; ".join(f"{c['name']}: {'found' if c['found'] else 'NOT found'}" for c in diag["checks"]))
    if selftest:
        lines.append(f"- Last self-test ({selftest.get('utc')}): {'passed' if selftest.get('ok') else 'FAILED'}")
        lines += [f"  - {s.get('name')}: {'ok' if s.get('ok') else 'FAILED' if s.get('ok') is False else 'skipped'}"
                  f"{' - ' + str(s['detail']) if s.get('detail') else ''}" for s in selftest.get("steps") or []]
    if contract.get("missing"):
        lines += ["", "## Hermes internals the plugin relies on that were not found", ""]
        for item in contract["missing"]:
            lines.append(f"- {item['id']} ({item['uses']}): {item['what']}. In `{item['file']}` "
                         f"{'the file is gone' if item['problem'] == 'file-missing' else 'this text is gone: ' + ', '.join(item['needs'])}")
    plugin_source = HERMES_PLUGIN_SOURCE
    lines += ["", "## Where things are", "",
              f"- Plugin source (edit this): {plugin_source}",
              f"- Installed plugin copy (do not edit, the installer owns it): {hermes_home() / 'plugins' / 'crosstalk'}",
              f"- Installed Hermes source (read only): {contract.get('source_root') or current.get('source_root') or hermes_source_root() or 'not found'}",
              f"- Plugin runtime files and status records: {hermes_dir()}",
              f"- The plugin lists every Hermes internal it relies on in HERMES_CONTRACT (top of the plugin source).",
              f"- Crosstalk guide: {(checkout / 'docs' / 'Crosstalk' / 'README.md') if checkout else 'docs/Crosstalk/README.md in the EUTONOS checkout'} (Hermes section)",
              "", "## How to repair", "",
              "1. Read the failing internals above against the installed Hermes source (read only) and find what "
              "replaced them: for example how the process that hosts Desktop chats now publishes its message injector, "
              "or how a session id maps to its route.",
              "2. Change the plugin source so it finds the new host (`_host_probe`, `_inject_key`, `_inject`, "
              "`_session_open`), update HERMES_CONTRACT to match, and bump PLUGIN_VERSION.",
              "3. Run the tests from the EUTONOS checkout: `python tools/test_shards.py 1 --only test_crosstalk_hermes*` "
              "(never set PYTHONPATH).",
              f"4. The fixed plugin only takes effect after the owner reinstalls it and restarts Hermes. Afterwards "
              f"`{cli} hermes-selftest` proves the link without sending a letter.",
              "", "## Constraints", "",
              "- Do not restart Hermes, Codex or Claude Desktop, and do not reinstall, enable or disable the plugin "
              "(no hermes-install or hermes plugins command) without the owner.",
              "- Do not open credential or secret files (auth.json, .env, auth.lock, config.yaml and its backups, "
              ".credentials.json, key files). Reading Hermes .py source is fine.",
              "- Do not send real Crosstalk letters to test; use hermes-selftest.",
              "- Work on a branch, run the tests above, and leave commit, push and merge to the owner's normal process.",
              "- Report what you found, what you changed, and what the owner must do (reinstall, restart).", ""]
    return "\n".join(lines)


def _source_plugin_version() -> str | None:
    try:
        text = HERMES_PLUGIN_SOURCE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = re.search(r'^PLUGIN_VERSION = "([^"]+)"', text, re.M)
    return found.group(1) if found else None


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _link_problems(data: dict) -> list[dict]:
    """The reasons the link counts as broken right now: [{id, text}], empty when it is healthy or unknowable."""
    problems = []
    diag = data.get("diagnosis") or {}
    state = diag.get("state")
    if state in ("plugin-incompatible", "plugin-not-listening") or (
            state == "plugin-not-loaded" and diag.get("installed_plugin_version")):
        problems.append({"id": "diagnosis:" + state, "text": diag.get("message") or state})
    current = (data.get("version") or {}).get("current") or {}
    selftest = data.get("selftest") or {}
    if (selftest.get("ok") is False and not selftest.get("inconclusive")
            and selftest.get("fingerprint") == current.get("fingerprint")):
        failed = next((s for s in selftest.get("steps") or [] if s.get("ok") is False), {})
        problems.append({"id": "selftest:" + str(failed.get("name")),
                         "text": f"The self-test failed at {failed.get('name')}: {failed.get('detail')}"})
    contract = data.get("contract") or {}
    if contract.get("missing") and contract.get("fingerprint") == current.get("fingerprint"):
        names = ", ".join(item["id"] for item in contract["missing"])
        problems.append({"id": "contract:" + ",".join(sorted(item["id"] for item in contract["missing"])),
                         "text": f"Hermes internals the plugin relies on changed or are missing ({names})"})
    return problems


def _repair_step(data: dict) -> dict | None:
    """Keep the one repair offer in step with the problems. Returns {"start": app} when the mode is auto and a
    chat should be started now. A cause that is already offered is never offered twice; a resolved one clears."""
    mode = link_repair_mode()
    repair = data.setdefault("repair", {})
    repair["mode"] = mode
    problems = _link_problems(data)
    if not problems:
        if repair.get("state") in ("offered", "started", "starting", "start-failed"):
            repair.update(state="resolved", resolved_utc=_utc_now())
        return None
    current = (data.get("version") or {}).get("current") or {}
    key = hashlib.sha256((current.get("fingerprint") or "") .encode() + b"\n" +
                         "\n".join(sorted(p["id"] for p in problems)).encode()).hexdigest()[:8]
    state = repair.get("state")
    if state in ("offered", "started", "starting", "start-failed") or (state == "off" and mode == "off"):
        # The same break is still open: it keeps ONE offer (one key, one pending action) however its evidence grows
        # (a failed self-test adds to the reasons). A new offer comes only after the link was seen repaired.
        repair["reasons"] = problems
        if state == "offered":
            _write_text_atomic(repair_prompt_path(), hermes_repair_prompt(data))
        return None
    harnesses = installed_harnesses()
    repair.update(key=key, offer_no=int(repair.get("offer_no") or 0) + 1, reasons=problems, harnesses=harnesses,
                  offered_utc=_utc_now(), started=None, resolved_utc=None,
                  prompt_path=None if mode == "off" else str(repair_prompt_path()))
    if mode == "off":
        repair["state"] = "off"
        return None
    _write_text_atomic(repair_prompt_path(), hermes_repair_prompt(data))
    repair["state"] = "offered"
    if mode == "auto" and harnesses:
        repair["state"] = "starting"  # recorded before anything starts, so a crash never starts a second chat
        return {"start": harnesses[0]["app"]}
    return None


def hermes_link_refresh(*, contract: dict | None = None, selftest: dict | None = None) -> dict:
    """Bring link-hermes.json up to date from live evidence, keep the single repair offer in step, and return the
    record. `contract` / `selftest` are results to store (None keeps what is stored). Never sends or resends."""
    diag = hermes_link_diagnosis()
    info = hermes_version_info()
    action: dict = {}

    def change(data):
        version = data.setdefault("version", {})
        if info:
            version["current"] = _version_row(info, "observed")
        data["diagnosis"] = diag
        if contract is not None:
            old = data.get("contract") or {}
            same = old.get("fingerprint") == contract.get("fingerprint") and old.get("missing") == contract.get("missing")
            data["contract"] = dict(contract, first_seen_utc=old.get("first_seen_utc") if same and old.get("first_seen_utc")
                                    else contract.get("utc"))
        if selftest is not None:
            data["selftest"] = selftest
            if selftest.get("ok") and info:
                version["checked"] = _version_row(info, "selftest")
        elif diag["state"] == "ok" and info and not version.get("checked"):
            version["checked"] = _version_row(info, "first-seen-healthy")
        action.update(_repair_step(data) or {})
    data = _link_mutate(change)
    if action.get("start"):
        data = _auto_start_repair(action["start"])
    return data


def _auto_start_repair(app: str) -> dict:
    """Auto mode: start the repair chat once for the recorded offer. A failure is recorded and not retried."""
    try:
        result = hermes_repair_start(app, show=False, _auto=True)
        outcome = {"state": "started", "started": result.get("started")}
    except ProbeError as error:
        outcome = {"state": "start-failed", "started": {"app": app, "utc": _utc_now(), "error": error.reason}}
    except Exception as error:
        outcome = {"state": "start-failed", "started": {"app": app, "utc": _utc_now(), "error": type(error).__name__}}

    def change(data):
        data.setdefault("repair", {}).update(outcome)
    return _link_mutate(change)


def hermes_repair_start(app: str, model: str | None = None, effort: str | None = None, show: bool = True,
                        _auto: bool = False) -> dict:
    """Start a repair chat in an installed harness through Crosstalk, with the repair prompt file as its first
    message. The owner picks the harness (or the mode is auto). Refuses an app that is not installed."""
    link = hermes_link_load()
    repair = link.get("repair") or {}
    path = Path(repair.get("prompt_path") or repair_prompt_path())
    if not path.is_file():
        raise ProbeError("no-repair-prompt", "no repair has been offered (no repair prompt file); run hermes-selftest first")
    if app not in REPAIR_DEFAULTS:
        raise ProbeError("invalid-app", "claude or codex")
    if app not in [item["app"] for item in installed_harnesses()]:
        raise ProbeError(f"{app}-not-installed", f"{app} is not installed on this machine, so no repair chat was started")
    if repair.get("state") in ("starting", "started") and not _auto and repair.get("started", {}) and \
            (repair.get("started") or {}).get("app") and not (repair.get("started") or {}).get("error"):
        raise ProbeError("repair-already-started", "a repair chat was already started for this problem; check "
                                                   "crosstalk_list and never start a second one")
    prompt = path.read_text(encoding="utf-8")
    model = model or REPAIR_DEFAULTS[app][0]
    effort = effort or REPAIR_DEFAULTS[app][1]
    folder_path = _checkout_root() or (eutonos_home() / "repair")
    folder_path.mkdir(parents=True, exist_ok=True)
    folder = str(folder_path)

    def mark(state, extra):
        def change(data):
            data.setdefault("repair", {}).update(state=state, started=extra)
        _link_mutate(change)
    mark("starting", {"app": app, "model": model, "effort": effort, "utc": _utc_now(), "folder": folder, "result": None})
    try:
        if app == "claude":
            result = create_claude(folder, model, effort, "auto", prompt, title=REPAIR_TITLE, show=show)
        else:
            result = create_codex(folder, model, effort, prompt, title=REPAIR_TITLE)
    except ProbeError as error:
        mark("start-failed", {"app": app, "model": model, "effort": effort, "utc": _utc_now(), "error": error.reason})
        raise
    summary = {key: result.get(key) for key in ("ok", "state", "key", "session_id", "thread_id", "operation_id")
               if isinstance(result, dict) and result.get(key) is not None}
    started = {"app": app, "model": model, "effort": effort, "utc": _utc_now(), "folder": folder, "result": summary}
    mark("started", started)
    return {"ok": True, "started": started, "prompt_path": str(path), "result": result}


# ---- self-test
def _hermes_probe(wait_s: float) -> dict:
    """Ask the process that hosts Hermes chats to prove its inbox loop and route lookup, injecting nothing."""
    base = hermes_dir()
    probe_id = "probe-" + uuid.uuid4().hex[:12]
    try:
        for name in ("probe", "probe-done", "cancelled"):
            (base / name).mkdir(parents=True, exist_ok=True)
        entries = directory_load()["entries"]
        chats = sorted((entry for key, entry in entries.items() if key.startswith("hermes:")),
                       key=lambda entry: entry.get("last_seen_ns") or 0, reverse=True)
        ids = [entry["id"] for entry in chats[:HERMES_PROBE_ROUTE_IDS] if isinstance(entry.get("id"), str)]
        part, inbox = base / "probe" / f"{probe_id}.json.part", base / "probe" / f"{probe_id}.json"
        part.write_text(json.dumps({"probe_id": probe_id, "utc": _utc_now(), "session_ids": ids}), encoding="utf-8")
        os.replace(part, inbox)
    except (OSError, ValueError) as error:
        return {"answered": False, "detail": "could not write the probe file: " + type(error).__name__}
    result_path = base / "probe-done" / f"{probe_id}.result.json"
    deadline = time.monotonic() + wait_s
    answer = None
    while True:
        answer = _read_json(result_path, None)
        if isinstance(answer, dict) or time.monotonic() >= deadline:
            break
        time.sleep(HERMES_PROBE_POLL_S)
    if not isinstance(answer, dict):
        with contextlib.suppress(OSError):
            os.replace(inbox, base / "cancelled" / f"{probe_id}.json")
        return {"answered": False, "detail": f"no process answered the probe within {wait_s:g} s (a plugin older than "
                                             "0.4.0 cannot answer; neither can one whose listener loop is stuck)"}
    with contextlib.suppress(OSError):
        result_path.unlink()
    return {"answered": True, "answer": answer}


def hermes_selftest(wait_s: float | None = None) -> dict:
    """Prove the Hermes link without sending a letter or injecting anything into any chat.

    Steps: Hermes running, plugin hosting (judged from the status records and heartbeat), the hosting process
    answers a probe (its inbox loop runs), the route lookup works (for a known open chat when one exists), and the
    contract check of the plugin's declared Hermes internals against the installed Hermes source. A pass records the
    Hermes version as checked; a failure opens the repair offer. Not running or just starting is inconclusive."""
    wait_s = HERMES_PROBE_WAIT_S if wait_s is None else wait_s
    diag = hermes_link_diagnosis()
    steps = []

    def step(name, ok, detail=None):
        steps.append({"name": name, "ok": ok, "detail": detail})

    step("hermes-running", diag["hermes_running"],
         None if diag["hermes_running"] else diag["message"])
    step("plugin-hosting", diag["state"] == "ok", diag["message"] if diag["state"] != "ok" else
         f"host {diag.get('host') or 'heartbeat'}; plugin {', '.join(diag['plugin_versions']) or 'older than 0.4.0'}")
    plugin_version, route = None, None
    if diag["state"] == "ok":
        probe = _hermes_probe(HERMES_PROBE_WAIT_S if wait_s is None else wait_s)
        step("probe-answered", probe.get("answered"), probe.get("detail") or "the hosting process answered; nothing was injected")
        if probe.get("answered"):
            answer = probe["answer"]
            plugin_version, route = answer.get("plugin_version"), answer.get("route") or {}
            api_ok = route.get("api") == "ok" and not route.get("errors")
            note = (f"{len(route.get('open') or [])} of {route.get('tried', 0)} known chat(s) open"
                    if api_ok else str(route.get("api")))
            step("route-lookup", api_ok, note)
    contract = hermes_contract_check()
    step("contract", None if contract["ok"] is None else contract["ok"],
         contract["unchecked"] or (f"{contract['checked']} internals present" if contract["ok"] else
                                   "missing: " + ", ".join(item["id"] for item in contract["missing"])))
    inconclusive = diag["state"] in ("not-running", "starting")
    ok = all(item["ok"] is not False for item in steps) and not inconclusive
    info = hermes_version_info()
    summary = {"ok": ok, "inconclusive": inconclusive, "utc": _utc_now(), "steps": steps, "host": diag.get("host"),
               "plugin_version": plugin_version, "fingerprint": info["fingerprint"] if info else None}
    link = hermes_link_refresh(contract=contract, selftest=summary)
    return {"ok": ok, "inconclusive": inconclusive, "steps": steps, "diagnosis": diag, "contract": contract,
            "hermes": info, "version_notice": hermes_version_notice(link, info),
            "repair": {key: (link.get("repair") or {}).get(key) for key in
                       ("state", "mode", "prompt_path", "harnesses", "started")},
            "link_file": str(hermes_link_path()),
            "note": "no letter was sent and nothing was injected into any chat"}


def hermes_link_pass(now: float | None = None) -> dict:
    """The helper's Hermes link pass (about once a minute): refresh the record, check the internals when Hermes or
    the plugin's list changed, and run the self-test ONCE for a Hermes version change or a newly incompatible
    status (remembered, so it never repeats for the same cause)."""
    if not hermes_installed():
        return {"skipped": "hermes-not-installed"}
    info = hermes_version_info()
    link = hermes_link_load()
    stored = link.get("contract") or {}
    contract = None
    entries_sha = hashlib.sha256(json.dumps(hermes_contract_entries(), sort_keys=True).encode()).hexdigest()[:12]
    if (stored.get("fingerprint") != (info["fingerprint"] if info else None) or stored.get("contract_sha") != entries_sha
            or not stored.get("utc")):
        contract = hermes_contract_check()
    record = hermes_link_refresh(contract=contract)
    diag = record.get("diagnosis") or {}
    fingerprint = info["fingerprint"] if info else None
    checked = ((record.get("version") or {}).get("checked") or {}).get("fingerprint")
    trigger = None
    if diag.get("state") in ("ok", "plugin-incompatible", "plugin-not-listening") and fingerprint and checked and checked != fingerprint:
        trigger = "version:" + fingerprint
    elif diag.get("state") in ("plugin-incompatible", "plugin-not-listening"):
        trigger = f"diagnosis:{diag['state']}:{fingerprint}"
    ran = None
    if trigger and (record.get("auto") or {}).get("selftest_for") != trigger:
        ran = hermes_selftest()
        if not ran.get("inconclusive"):
            def change(data):
                data["auto"] = {"selftest_for": trigger, "utc": _utc_now(), "ok": ran["ok"]}
            record = _link_mutate(change)
    return {"state": diag.get("state"), "trigger": trigger, "selftest_ran": ran is not None,
            "selftest_ok": ran["ok"] if ran else None, "repair": (record.get("repair") or {}).get("state"),
            "version_notice": hermes_version_notice(record, info)}


def hermes_link_summary() -> dict:
    """A compact read for `hermes-status`: the diagnosis now plus what the record holds."""
    link = hermes_link_load()
    diag = hermes_link_diagnosis()
    return {"diagnosis": diag, "version_notice": hermes_version_notice(link),
            "checked_version": ((link.get("version") or {}).get("checked") or {}).get("version"),
            "selftest": {key: (link.get("selftest") or {}).get(key) for key in ("ok", "utc", "inconclusive")},
            "contract_missing": [item["id"] for item in (link.get("contract") or {}).get("missing") or []],
            "repair": {key: (link.get("repair") or {}).get(key) for key in ("state", "mode", "prompt_path")}}


def _letter_link_repair(diag: dict) -> str | None:
    """A letter was refused because the link is broken: bring the record and the repair offer up to date (best
    effort, never raises) and say where the offer stands."""
    try:
        return ((hermes_link_refresh().get("repair") or {}).get("state"))
    except Exception:
        return None


# Plugin 0.3.0 appends a parked letter to a running turn's tool result under this head (the stored row is role
# 'tool'); a tool row counts as an arrival only with this head plus the framed letter's header and ref line.
HERMES_MIDTURN_HEAD = "[Crosstalk: letter delivered mid-turn, at this tool call]"


def hermes_arrivals(session_id: str, marker: str, timeout: float = READBACK_TIMEOUT_S) -> tuple[int | None, str]:
    """Count user rows of that Hermes chat holding the framed letter's ref line (read-only, bounded)."""
    try:
        connection = hermes_db_connect(timeout)
    except (sqlite3.Error, OSError) as error:
        return None, "state-db-unreadable:" + type(error).__name__
    if connection is None:
        return None, "state-db-not-found"
    try:
        found = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? "
            "AND (role = 'user' OR (role = 'tool' AND instr(content, ?) > 0)) "
            "AND instr(content, ?) > 0 AND instr(content, ?) > 0",
            (session_id, HERMES_MIDTURN_HEAD, "ref " + marker, HEADER_PREFIX)).fetchone()
        return int(found[0]), "state-db-read"
    except (sqlite3.Error, OSError, TypeError, ValueError) as error:
        return None, "state-db-unreadable:" + type(error).__name__
    finally:
        connection.close()


def deliver_hermes(letter_id: str, session_id: str, message: str) -> tuple[str, dict]:
    """Hand one framed letter to the Hermes plugin's inbox and wait for its inject_message outcome.

    accepted = the host accepted it; refused = proven not sent (listener down and the unclaimed file withdrawn,
    the plugin's own control check, or inject_message returned False); uncertain = claimed or unreadable, never
    resent."""
    if not hermes_listener_alive():
        diag = hermes_link_diagnosis()
        if diag["state"] in ("ok", "not-running"):  # ok: the heartbeat caught up since the first look
            if diag["state"] == "not-running":
                return "refused", {"reason": "hermes-not-running"}
        else:
            return "refused", {"reason": diag["reason"], "link_state": diag["state"],
                               "link_detail": diag["message"], "link_repair": _letter_link_repair(diag)}
    base = hermes_dir()
    try:
        for name in ("inbox", "work", "done", "cancelled"):
            (base / name).mkdir(parents=True, exist_ok=True)
    except OSError as error:  # nothing was written for the listener: proven not sent
        return "refused", {"reason": "hermes-inbox-write-failed:" + type(error).__name__}
    inbox = base / "inbox" / f"{letter_id}.json"
    part = base / "inbox" / f"{letter_id}.json.part"
    try:
        part.write_text(json.dumps({"letter_id": letter_id, "session_key": session_id, "text": message},
                                   ensure_ascii=False), encoding="utf-8")
        os.replace(part, inbox)
    except OSError as error:
        with contextlib.suppress(OSError):
            part.unlink()
        return "refused", {"reason": "hermes-inbox-write-failed:" + type(error).__name__}
    result_path = base / "done" / f"{letter_id}.result.json"
    deadline = time.monotonic() + HERMES_RESULT_WAIT_S
    while True:
        result = _read_json(result_path, None)
        if isinstance(result, dict) or time.monotonic() >= deadline:
            break
        time.sleep(HERMES_RESULT_POLL_S)
    if not isinstance(result, dict):
        try:
            os.replace(inbox, base / "cancelled" / f"{letter_id}.json")
        except OSError:
            return "uncertain", {"reason": "hermes-claimed-no-result"}
        return "refused", {"reason": "hermes-not-running"}
    extra = {"hermes_pid": result.get("pid") if type(result.get("pid")) is int else None}
    if isinstance(result.get("refused"), str) and result["refused"]:
        return "refused", dict(extra, reason=clip_reason(result["refused"]))
    if result.get("deferred") == "session-busy" and result.get("returned") is None and not result.get("error"):
        # The chat is mid-turn: the plugin parked the letter and injects it once when the turn ends. Accepted
        # (held until readback sees it), never refused, never resent.
        return "accepted", dict(extra, hermes_deferred=True)
    if result.get("returned") is True and not result.get("error"):
        return "accepted", extra
    if result.get("returned") is False and not result.get("error"):
        return "refused", dict(extra, reason="hermes-chat-not-open")
    return "uncertain", dict(extra, reason="hermes-inject-outcome-unverified:" + clip_reason(result.get("error") or ""))


# ---------------------------------------------------------------- registration
_SCRATCH = re.compile(r"[\\/]claude[\\/]scratch-workspaces[\\/]([0-9a-f-]{36})[\\/]([0-9a-f-]{36})[\\/]", re.I)


def classify_cwd(cwd) -> dict:
    if not isinstance(cwd, str) or not cwd:
        return {"cwd_kind": "unknown", "project_folder": None}
    match = _SCRATCH.search(cwd)
    if match:
        return {"cwd_kind": "scratch-no-folder", "project_folder": None,
                "account_from_cwd": match.group(1).lower(), "organization_from_cwd": match.group(2).lower()}
    return {"cwd_kind": "project", "project_folder": cwd}


def register_claude(facts: dict, source: str) -> dict:
    """Register a Claude session from Mod-reported facts. Raises ProbeError on bad input."""
    session_id = facts.get("sessionId")
    if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
        raise ProbeError("invalid-session-id")
    cwd = facts.get("cwd") if isinstance(facts.get("cwd"), str) else None
    registration = claude_registration(session_id)
    account = current_account()
    account.pop("folder", None)
    fields = {"project": classify_cwd(cwd), "cwd": cwd,
              "root": facts.get("root") if isinstance(facts.get("root"), str) else None,
              "repo": facts.get("repo") if isinstance(facts.get("repo"), dict) else None,
              "surfaces": facts.get("surfaces") if isinstance(facts.get("surfaces"), list) else None,
              "version": facts.get("version") if isinstance(facts.get("version"), str) else None,
              "start_event": facts.get("startEvent") if isinstance(facts.get("startEvent"), dict) else None,
              "account": account, "source": source}
    fields["project_folder"] = fields["project"].get("project_folder")
    have = (directory_load()["entries"].get(f"claude:{session_id}") or {})
    kept = str(have.get("title_source") or "").startswith("owner/creator supplied") and have.get("title")
    if registration is not None and not (kept and not facts.get("title")):
        # a creator-supplied title is never overwritten by a later hook registration's native name
        fields["title"] = registration.get("name")
        fields["title_source"] = "~/.claude/sessions/<pid>.json name (display only, never used to select)"
    if registration is not None:
        fields["native_registration"] = {k: registration[k] for k in ("live", "status", "entrypoint")}
    if isinstance(facts.get("title"), str) and facts["title"].strip():
        fields["title"] = " ".join(facts["title"].split())[:80]
        fields["title_source"] = "owner/creator supplied at creation (display only, never used to select)"
    event = {"event": "register" if source in ("session.start", "create-claude") else "status", "app": "claude",
             "caller": {"session_id": session_id, "cwd": cwd, "source": source,
                        "tool_use_id": facts.get("toolUseId") if isinstance(facts.get("toolUseId"), str) else None}}
    return directory_register("claude", session_id, fields, event)


def codex_caller(meta) -> dict | None:
    """Native caller identity from MCP _meta, or None when absent or inconsistent."""
    if not isinstance(meta, dict):
        return None
    thread = meta.get("threadId")
    if not isinstance(thread, str) or not UUID_RE.fullmatch(thread):
        return None
    turn = meta.get("x-codex-turn-metadata")
    turn = turn if isinstance(turn, dict) else {}
    if isinstance(turn.get("thread_id"), str) and turn["thread_id"] != thread:
        return None

    def keep(source, name):
        value = source.get(name)
        return value if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 80 else None

    return {"thread_id": thread, "session_id": keep(meta, "sessionId"), "window_id": keep(meta, "windowId"),
            "call_id": keep(meta, "callId"), "turn_id": keep(turn, "turn_id"),
            "codex_version": keep(turn, "codex_version"), "model": keep(turn, "model"),
            "reasoning_effort": keep(turn, "reasoning_effort"), "sandbox": keep(turn, "sandbox"),
            "sandbox_mode": keep(turn, "sandbox_mode"),
            "turn_started_at_unix_ms": keep(turn, "turn_started_at_unix_ms"),
            "meta_keys": sorted(str(k) for k in meta), "turn_meta_keys": sorted(str(k) for k in turn),
            "meta_sha256": hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()}


CODEX_META_MAX_BYTES = 1 << 20


def codex_discover(thread_id: str) -> dict:
    """Register one exact Codex thread that never called a Crosstalk tool, from its rollout's first line
    (session_meta: id, cwd, creator_account_id). Never raises. A missing or unreadable rollout, or a
    session_meta without a matching id or an account, registers nothing (unknown, never absence). The
    account is the thread's creator account, so the normal account check still applies at send."""
    if not isinstance(thread_id, str) or not UUID_RE.fullmatch(thread_id):
        return {"read": False, "note": "invalid-thread-id", "registered": 0}
    try:
        path = codex_rollout_path(thread_id)
        if path is None:
            return {"read": False, "note": "rollout-not-found", "registered": 0}
        with open(path, "rb") as handle:
            first = handle.readline(CODEX_META_MAX_BYTES)
        row = json.loads(first.decode("utf-8"))
        meta = row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None
        if not isinstance(meta, dict) or str(meta.get("id", "")).lower() != thread_id:
            return {"read": False, "note": "session-meta-mismatch", "registered": 0}
        account_id = meta.get("creator_account_id")
        if not isinstance(account_id, str) or not account_id:
            return {"read": False, "note": "session-meta-no-account", "registered": 0}
        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
        fields = {"project_folder": cwd, "project_folder_source": "rollout session_meta cwd" if cwd else "unknown",
                  "source": "rollout-discovery",
                  "account": {"app": "codex", "id": account_id.lower(),
                              "source": "rollout session_meta creator_account_id"}}
        event = {"event": "discover", "app": "codex", "thread_id": thread_id, "rollout": path.name}
        directory_register("codex", thread_id, fields, event)
        return {"read": True, "note": "registered", "registered": 1}
    except Exception as error:  # discovery must never break a send
        return {"read": False, "note": "discovery-error:" + type(error).__name__, "registered": 0}


CODEX_JOIN_BACKFILL_MS = 7 * 24 * 3600 * 1000
CODEX_JOIN_BATCH = 500
CODEX_JOIN_DB_TIMEOUT_S = 0.25
# Internal or delegated threads that are not chats the owner or Crosstalk talks to: Guardian reviews,
# subagent and agent-spawned/forked threads. Owner-created Desktop chats and Crosstalk-created chats have a
# NULL or "user" thread_source (checked read-only against the real DB, 2026-10-20); headless `exec` runs are not chats.
CODEX_JOIN_SKIP_SOURCES = ("guardian_review", "subagent", "agent_created_thread", "agent_forked_thread")


def codex_state_db_path() -> Path:
    override = os.environ.get("CROSSTALK_P0_CODEX_STATE_DB")
    if override:
        return Path(override)
    codex_home = os.environ.get("CODEX_HOME")
    return (Path(codex_home) if codex_home else home() / ".codex") / "state_5.sqlite"


def _codex_join_title(row_name, row_title) -> str | None:
    for value in (row_name, row_title):
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:80]
    return None


def codex_join_scan(now_ms: int | None = None) -> dict:
    """Register new Codex chats from Codex's own state DB (read-only), so a chat joins the directory when it
    is created, not when it first calls a Crosstalk tool. Cheap per tick: one indexed query past a watermark
    (max updated_at_ms seen, kept in codex-join.json; first run backfills non-archived threads updated in the
    last 7 days). A title is refreshed (display only, never used to select). Archived threads are never added
    and no entry is ever deleted. A missing, locked or changed-schema DB skips the tick; never raises, never
    writes a Codex file."""
    summary = {"read": False, "registered": 0, "refreshed": 0}
    try:
        db_path = codex_state_db_path()
        if not db_path.is_file():
            summary["note"] = "no-state-db"
            return summary
        state_path = device_dir() / "codex-join.json"
        state = _read_json(state_path, {})
        watermark = state.get("watermark_ms") if isinstance(state, dict) else None
        if not isinstance(watermark, int):
            watermark = int(now_ms if now_ms is not None else time.time() * 1000) - CODEX_JOIN_BACKFILL_MS
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=CODEX_JOIN_DB_TIMEOUT_S)
        try:
            connection.execute("PRAGMA query_only = 1")
            marks = ",".join("?" * len(CODEX_JOIN_SKIP_SOURCES))
            rows = connection.execute(
                "SELECT id, cwd, title, name, creator_account_id, updated_at_ms, source, thread_source FROM threads "
                "WHERE archived = 0 AND updated_at_ms > ? AND agent_role IS NULL AND agent_path IS NULL "
                f"AND (thread_source IS NULL OR thread_source NOT IN ({marks})) "
                "ORDER BY updated_at_ms LIMIT ?", (watermark, *CODEX_JOIN_SKIP_SOURCES, CODEX_JOIN_BATCH)).fetchall()
            summary["read"] = True
        finally:
            connection.close()
        if not rows:
            return summary
        entries = directory_load()["entries"]
        newest = watermark
        for thread_id, cwd, title, name, account_id, updated, source, _thread_source in rows:
            newest = max(newest, updated) if isinstance(updated, int) else newest
            if source == "exec" or not isinstance(thread_id, str) or not UUID_RE.fullmatch(thread_id.lower()):
                continue
            thread_id = thread_id.lower()
            have = entries.get("codex:" + thread_id)
            text = _codex_join_title(name, title)
            folder = cwd[4:] if isinstance(cwd, str) and cwd.startswith("\\\\?\\") else cwd
            has_account = isinstance(account_id, str) and account_id
            if have is not None:
                stale = (text and text != have.get("title")) \
                    or (has_account and not (have.get("account") or {}).get("id")) \
                    or (folder and not have.get("project_folder"))
                if not stale:
                    continue
            elif not has_account:
                continue  # unknown account registers nothing, as codex_discover
            fields = {}
            if text:  # an unnamed thread never blanks a title already known
                fields.update(title=text, title_source="Codex state DB name/title (display only, never used to select)")
            if isinstance(folder, str) and folder:
                fields.update(project_folder=folder, project_folder_source="Codex state DB cwd")
            if has_account:
                fields["account"] = {"app": "codex", "id": account_id.lower(),
                                     "source": "Codex state DB creator_account_id"}
            if have is None:
                fields["source"] = "codex-thread-discovery"
            directory_register("codex", thread_id, fields,
                               {"event": "discover" if have is None else "retitle", "app": "codex",
                                "thread_id": thread_id, "via": "state-db"})
            summary["refreshed" if have is not None else "registered"] += 1
        if newest > watermark:
            _write_json_atomic(state_path, {"watermark_ms": newest})
    except Exception as error:  # a join scan must never break the helper
        summary["note"] = "join-error:" + type(error).__name__
    return summary


def register_codex(caller: dict, server: dict, account: dict | None = None) -> dict:
    fields = {"project_folder": None, "project_folder_source": "unknown: MCP gives no chat cwd",
              "server_cwd": server.get("cwd"), "title": None, "account": account,
              "native": {k: caller.get(k) for k in ("codex_version", "model", "window_id", "sandbox_mode")},
              "source": "tool-call"}
    event = {"event": "status", "app": "codex", "caller": caller, "server": server}
    entry = directory_register("codex", caller["thread_id"], fields, event)
    if not (entry.get("account") or {}).get("id") and codex_discover(caller["thread_id"]).get("registered"):
        entry = directory_load()["entries"].get(entry["key"], entry)  # account and cwd from the rollout
    return entry


# ------------------------------------------------------------------- faults
def fault_hit(point: str) -> bool:
    """One-shot fault injection (probe 5). Consumes a count from fault.json."""
    if point not in FAULT_POINTS:
        return False
    with locked("fault"):
        path = device_dir() / "fault.json"
        value = _read_json(path, {})
        if not isinstance(value, dict) or int(value.get(point, 0) or 0) <= 0:
            return False
        value[point] = int(value[point]) - 1
        _write_json_atomic(path, value)
    return True


def die_if_fault(point: str) -> None:
    if fault_hit(point):
        sys.stdout.flush()
        os._exit(FAULT_EXIT)  # simulates the helper being killed at exactly this point


def fault_set(point: str, count: int = 1) -> dict:
    if point not in FAULT_POINTS:
        raise ProbeError("unknown-fault-point")
    with locked("fault"):
        path = device_dir() / "fault.json"
        value = _read_json(path, {})
        value = value if isinstance(value, dict) else {}
        value[point] = int(count)
        _write_json_atomic(path, value)
        return value


def fault_clear() -> None:
    with locked("fault"):
        _write_json_atomic(device_dir() / "fault.json", {})


# ------------------------------------------------------------------ letters
HEADER_PREFIX = "Crosstalk message from "
APP_LABELS = {"claude": "Claude", "codex": "Codex", "hermes": "Hermes"}


def frame_message(letter_id: str, sender_app: str, sender_name: str, text: str) -> str:
    """One header line (owner, 2026-10-07): 'Crosstalk message from <App> (<chat name>)'.

    The last line 'ref <letter ID>' (owner, 2026-10-08) is the marker readback counts arrivals by."""
    return f"{HEADER_PREFIX}{APP_LABELS.get(sender_app, sender_app)} ({sender_name})\n{text}\nref {letter_id}"


def sender_name(sender_key: str) -> str:
    """The sender chat's directory title; its directory key when it has none (Codex gives no title)."""
    title = (directory_load()["entries"].get(sender_key) or {}).get("title")
    title = " ".join(title.split()) if isinstance(title, str) else ""
    return title[:80] if title else sender_key


def resolve_recipient(raw, sender_app: str) -> dict:
    """Exact directory entry for `to`. Titles, paths and anything else are refused."""
    if not isinstance(raw, str):
        raise ProbeError("to-must-be-exact-id")
    candidate = raw.strip().lower()
    entries = directory_load()["entries"]
    if KEY_RE.fullmatch(candidate) or HERMES_KEY_RE.fullmatch(candidate):
        key = candidate
    elif HERMES_ID_RE.fullmatch(candidate):
        key = "hermes:" + candidate
    elif UUID_RE.fullmatch(candidate):
        matches = [k for k in entries if k.endswith(":" + candidate)]
        if not matches and codex_discover(candidate).get("registered"):
            matches = ["codex:" + candidate]
            entries = directory_load()["entries"]
        if len(matches) != 1:
            raise ProbeError("recipient-unknown-or-ambiguous")
        key = matches[0]
    else:
        raise ProbeError("to-must-be-exact-id")
    if key.startswith("hermes:") and key not in entries:
        hermes_discover(key.split(":", 1)[1])  # a Hermes chat need not register first; state.db is read (read-only)
        entries = directory_load()["entries"]
    if key.startswith("codex:") and not ((entries.get(key) or {}).get("account") or {}).get("id"):
        # Nor a Codex chat, or one registered while Codex could not report its account (3B.11
        # baseline, 2026-10-18): its rollout's session_meta is read (read-only).
        codex_discover(key.split(":", 1)[1])
        entries = directory_load()["entries"]
    entry = entries.get(key)
    if entry is None:
        raise ProbeError("recipient-not-in-directory")
    if entry.get("app") == sender_app:
        raise ProbeError("same-app-recipient")
    return entry


def binding_state(entry: dict) -> dict:
    """Compare a Claude recipient's retained account with the current one. `reachable` is True when they match
    or when Desktop holds the same chat (cliSessionId) under the active account; `refresh` says the stored
    binding should then be updated to the current account. `unknown` is True when the recorded or the current
    account ID is missing and Desktop cannot vouch for the chat: that is never reachable."""
    recorded = (entry.get("account") or {}).get("id")
    current = current_account()
    if recorded and current.get("id") and recorded == current["id"]:
        return {"reachable": True, "refresh": False, "unknown": False, "recorded": recorded, "current": current}
    if current.get("id") and current["source"].startswith("claude-desktop") and desktop_has_chat(current, entry["id"]):
        return {"reachable": True, "refresh": True, "unknown": False, "recorded": recorded, "current": current}
    unknown = not (recorded and current.get("id"))
    if not unknown and current["source"].startswith("claude-desktop") \
            and desktop_lookup(current, entry["id"])[0] == "unreadable":
        # Both IDs are known and differ, but Desktop's records could not all be read (a corrupt or hidden
        # record may be this chat's): recoverable unknown-identity, not the final other-account.
        unknown = True
    return {"reachable": False, "refresh": False, "unknown": unknown,
            "recorded": recorded, "current": current}


def codex_current(fresh: bool = False) -> dict:
    """The current Codex account (cached for a few minutes unless `fresh`); an app-server failure is an
    unknown account."""
    try:
        return codex_account(timeout=15.0, use_cache=not fresh) if fresh else codex_account(timeout=15.0)
    except Exception as error:
        return {"app": "codex", "id": None, "error": type(error).__name__}


def codex_binding_state(entry: dict, current: dict | None = None) -> dict:
    """Compare a Codex recipient's recorded account with the current one: "match", "mismatch" or "unknown".
    IDs are compared when both sides have one; otherwise the email hash is, when both sides have that; any
    other combination is unknown and never a match."""
    recorded = entry.get("account") or {}
    current = current if current is not None else codex_current()
    state = "unknown"
    for name in ("id", "email_sha256_12"):
        if recorded.get(name) and current.get(name):
            state = "match" if recorded[name] == current[name] else "mismatch"
            break
    return {"state": state, "recorded": recorded.get("id"), "current": current.get("id")}


def block_reason(entry: dict, codex_now: dict | None = None) -> str | None:
    """blocked:other-account / blocked:unknown-identity / blocked:not-running (the last for a Claude recipient
    only), else None. An account that cannot be read on either side blocks; it is never treated as a match."""
    if entry.get("app") == "codex":
        state = codex_binding_state(entry, codex_now)["state"]
        return {"mismatch": "blocked:other-account", "unknown": "blocked:unknown-identity"}.get(state)
    if entry.get("app") == "hermes":
        # One local install, no account concept: only "is the plugin's listener up" matters (a hint in the list;
        # a send refuses as hermes-not-running instead of waiting, see deliver_hermes).
        return None if hermes_listener_alive() else "blocked:not-running"
    if entry.get("app") != "claude":
        return None
    binding = binding_state(entry)
    if not binding["reachable"]:
        return "blocked:unknown-identity" if binding["unknown"] else "blocked:other-account"
    registration = claude_registration(entry["id"])
    if registration is not None and not registration["live"]:
        return "blocked:not-running"
    return None


def refresh_binding_locked(entry: dict) -> None:
    """Call only while holding the state lock. Updates the stored account of a recipient that Desktop shows
    under the active account, and journals it. Reads Desktop data only; never writes there."""
    state = binding_state(entry)
    if not state["refresh"]:
        return
    directory = directory_load()
    stored = directory["entries"].get(entry["key"])
    if stored is None:
        return
    new_account = {k: v for k, v in state["current"].items() if k != "folder"}
    stored["account"] = new_account
    _write_json_atomic(device_dir() / "directory.json", directory)
    _append_journal_locked({"event": "binding-refresh", "entry_key": entry["key"], "from_account": state["recorded"],
                            "to_account": new_account["id"], "source": new_account["source"],
                            "reason": "desktop-has-chat-under-active-account"})


# ------------------------------------------------------------ pre-send guard
# One function every route calls immediately before its native effect (slice 1 of the rework, 2026-10-19).
# Routes: claude-session-send, codex-queue and hermes-inject (attempt_delivery) and mail-slot (the hand-out to
# the recipient chat itself). The old desktop-relay route is gone; its name stays only in old journal rows.
MESSAGE_FRAME_OVERHEAD = 300   # header line (prefix, app, 80-char name) plus the trailing `ref <id>` line


def pre_send_guard(letter_id: str, route: str, *, attempt_id: str | None = None,
                   zero_arrivals_proven: bool = False) -> str | None:
    """Reason this letter must not be sent right now, or None. Reads everything fresh; takes no lock (callers
    may hold the state lock) and writes nothing: the caller journals a refusal.

    Checks, in order: controls (halt, messaging off, deny for that recipient); sender is not the target; payload
    cap; letter framing (`Crosstalk message from ` header line, last line `ref <letter id>`); one attempt (any
    attempted row other than `attempt_id`'s own refuses; the single exception is the mail slot, which may hand
    out an uncertain letter again once readback proved zero arrivals); account recheck (Claude: Desktop's
    current account; Codex: a fresh app-server read, never the cached one; Hermes has no account; the mail
    slot's caller is the recipient chat itself, whose unknown identity is exactly the case it serves)."""
    letter = letters_load().get(letter_id) or {}
    recipient = letter.get("to") or {}
    key = f"{recipient.get('app')}:{recipient.get('id')}"
    refusal = controls_check(key)
    if refusal:
        return refusal
    sender = letter.get("from") or {}
    if f"{sender.get('app')}:{sender.get('id')}" == key:
        return "sender-is-recipient"
    message = letter.get("message")
    if not isinstance(message, str) or len(message) > MAX_TEXT_CHARS + MESSAGE_FRAME_OVERHEAD or (
            isinstance(letter.get("text_chars"), int) and letter["text_chars"] > MAX_TEXT_CHARS):
        return "text-too-long"
    if not message.startswith(HEADER_PREFIX) or "\n" not in message or not message.endswith("\nref " + letter_id):
        return "letter-framing-invalid"
    # A readback row (readback_ns, no attempt_id) only re-records or annotates an existing state; it is never an
    # attempt of its own (a concurrent check_arrival once made the sender refuse itself, 2026-10-20).
    attempted = [row for row in letter.get("history", []) if row.get("state") in ATTEMPTED_STATES
                 and not row.get("readback") and not (attempt_id and row.get("attempt_id") == attempt_id)]
    if attempted and not (route == "mail-slot" and zero_arrivals_proven):
        return "already-attempted"
    if route == "mail-slot":
        return None
    app = recipient.get("app")
    entry = directory_load()["entries"].get(key) or {"app": app, "id": recipient.get("id"), "key": key, "account": {}}
    blocked = block_reason(entry, codex_current(fresh=True) if app == "codex" else None)
    if blocked in ("blocked:other-account", "blocked:unknown-identity"):
        return "account-recheck:" + blocked.split(":", 1)[1]
    return None


def account_mismatch_detail(entry: dict, codex_now: dict | None = None) -> dict:
    """Registered vs current account ids for a blocked:other-account or blocked:unknown-identity result (ids
    only). Claude recipients are checked against the current Claude account, Codex ones against Codex's."""
    if entry.get("app") == "codex":
        binding = codex_binding_state(entry, codex_now)
        return {"registered_account": binding["recorded"], "current_account": binding["current"],
                "account_source": "codex app-server account/read", "recipient": entry.get("key")}
    current = current_account()
    return {"registered_account": (entry.get("account") or {}).get("id"),
            "current_account": current["id"], "account_source": current["source"],
            "recipient": entry.get("key")}


def _letter_row(letter_id: str, state: str, **extra) -> dict:
    return dict({"event": "letter", "letter_id": letter_id, "state": state}, **extra)


OWNER_REPORT = ("This letter is not confirmed arrived: tell the owner its state and reason in YOUR chat "
                "now; do not leave it only in a file or log.")

GUIDANCE = {
    "held": "QUEUED, NOT YET CONFIRMED: the recipient's app accepted this letter, but Crosstalk has not seen it "
            "appear in the chat yet (the chat may be busy, or the check could not read it). It normally lands by "
            "itself. Do NOT resend or replace it. Crosstalk keeps checking; if it is still not confirmed after "
            "5 minutes you get one short notice, and after 24 hours it is retired as unconfirmed.",
    "blocked:other-account": "UNDELIVERABLE, NOT SENT: the recipient chat belongs to a different account "
                             "than the one signed in to now (Claude Desktop for a Claude chat, the Codex app for a "
                             "Codex chat), so it cannot receive letters. "
                             "This letter is final and will not be sent later. If the owner switches back to "
                             "that account, send a new letter.",
    "blocked:unknown-identity": "NOT SENT: Crosstalk cannot tell whether the recipient chat is on the account that is "
                                "signed in now, because the recipient's recorded account or the current account could "
                                "not be read. To fix it, the owner opens Claude Desktop (or the Codex app for a Codex "
                                "recipient) and signs in so the account is readable, then opens the recipient chat or "
                                "runs status so it records its account. The letter waits and is rechecked at the next "
                                "reconcile (a Claude recipient also receives it once when that chat itself starts); it "
                                "will not be resent blindly. Do NOT resend.",
    "blocked:not-running": "NOT SENT YET: the recipient chat is asleep. The letter waits in that chat's mail slot and is "
                           "delivered once, when the chat next opens (within 24 hours); status then shows it arrived. "
                           "Do NOT resend.",
    "expired-unsent": "NOT SENT, EXPIRED: the recipient chat did not open within the mail slot's 24 hours, so this letter "
                      "was never delivered and will not be sent later. Send a new letter if it still matters.",
    "sending": "The native send is in progress or its outcome is unknown. Do NOT resend; check readback.",
    "uncertain": "Native leg outcome unknown. Do NOT resend; run status/reconcile for readback. A Claude recipient "
                 "verifies this letter against its transcript when it next opens and shows it then if it never arrived.",
    "blocked:policy": "An organization policy blocks the tool. Nothing was sent.",
}


def send_letter(from_app: str, from_id: str, to_raw, text, *, from_turn: str | None = None,
                emit=None) -> dict:
    """Journal first, check controls, then deliver once. Never raises on a refusal."""
    emit = emit or (lambda row: None)
    letter_id = "CTP0-" + uuid.uuid4().hex[:12]
    sender_key = f"{from_app}:{from_id}"
    base = {"from": {"app": from_app, "id": from_id, "turn_id": from_turn}}
    try:
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ProbeError("text-invalid")
        if len(text) > MAX_TEXT_CHARS:
            raise ProbeError("text-too-long", f"limit {MAX_TEXT_CHARS} characters")
        recipient = resolve_recipient(to_raw, from_app)
    except ProbeError as error:
        journal_append(_letter_row(letter_id, "refused", sent=False, reason=error.reason, **base))
        return {"ok": False, "letter_id": letter_id, "state": "refused", "sent": False, "reason": error.reason,
                "detail": error.detail, "report_to_owner": True, "guidance": OWNER_REPORT}
    message = frame_message(letter_id, from_app, sender_name(sender_key), text)
    journal_append(_letter_row(letter_id, "journaled", to={"app": recipient["app"], "id": recipient["id"]},
                               message=message, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                               text_chars=len(text), created_ns=time.time_ns(), **base))
    emit({"event": "journaled", "letter_id": letter_id, "state": "journaled"})
    die_if_fault("after_journal")
    return attempt_delivery(letter_id)


REASON_GUIDANCE = {
    "hermes-chat-not-open": "NOT SENT: that Hermes chat is not open in Hermes Desktop (a closed or lazy chat cannot take "
                            "a letter). The owner opens that Hermes chat; then send a NEW letter.",
    "hermes-not-running": "NOT SENT: Hermes Desktop is not running (no live Hermes process, and the Crosstalk plugin "
                          "has not reported in). The owner starts Hermes Desktop; then send a NEW letter.",
    "hermes-starting": "NOT SENT: Hermes is still starting and the Crosstalk plugin has not found its chat host yet. "
                       "Wait a minute, then send a NEW letter.",
    "hermes-plugin-not-loaded": "NOT SENT: Hermes is running but the Crosstalk plugin has not reported in (not installed or "
                                "enabled, or Hermes not restarted since). The owner reinstalls or enables the plugin and "
                                "restarts Hermes; then send a NEW letter.",
    "hermes-plugin-incompatible": "NOT SENT: the Crosstalk plugin is loaded in Hermes but cannot find the part of Hermes "
                                  "that carries chats, probably because a Hermes update changed it. The plugin needs "
                                  "repair (see link_detail); this is not a Hermes-not-running problem.",
    "hermes-plugin-not-listening": "NOT SENT: the Crosstalk plugin found its chat host in Hermes but its listener is not "
                                   "answering. The plugin needs attention (see link_detail).",
}
REPAIR_SENTENCE = {"offered": " A repair has been offered to the owner (see pending actions).",
                   "starting": " A repair chat is being started.", "started": " A repair chat has been started."}


def _result(letter: dict, extra: dict | None = None) -> dict:
    state = letter.get("state")
    accepted = state in ("accepted", "held", "arrived", "replied")
    sent = True if accepted else False if state in ("refused", "expired-unsent") or str(state).startswith("blocked") else None
    result = dict({"ok": accepted, "letter_id": letter.get("letter_id"), "state": state, "sent": sent,
                   "transport_accepted": accepted, "arrived": state in ("arrived", "replied"),
                   "arrivals": letter.get("arrivals"), "readback_note": letter.get("readback_note"),
                   "reason": letter.get("reason"), "route": letter.get("route"),
                   "guidance": (REASON_GUIDANCE.get(str(letter.get("reason"))) if state == "refused" else None)
                   or GUIDANCE.get(str(state))}, **(extra or {}))
    if letter.get("link_state"):
        result["link_state"] = letter.get("link_state")
        result["link_detail"] = letter.get("link_detail")
        repair = REPAIR_SENTENCE.get(str(letter.get("link_repair")))
        if repair and result.get("guidance"):
            result["guidance"] += repair
    if state not in ("arrived", "replied"):
        result["report_to_owner"] = True
        result["guidance"] = ((result["guidance"] + " ") if result["guidance"] else "") + OWNER_REPORT
    return result


def attempt_delivery(letter_id: str) -> dict:
    """Claim once, deliver, then distinguish transport acceptance from native arrival."""
    # Read the Codex account before the lock: it may start a subprocess, which must not hold the state lock.
    peek = letters_load().get(letter_id) or {}
    codex_now = codex_current() if (peek.get("to") or {}).get("app") == "codex" else None
    codex_offset = None
    if (peek.get("to") or {}).get("app") == "codex":
        # The rollout's size now: readback scans forward from here, never the whole (possibly huge) file. The
        # lookup walks folders, so it runs before the state lock. Absent = unknown = readback scans a bounded tail.
        with contextlib.suppress(OSError, ValueError):
            rollout = codex_rollout_path(peek["to"]["id"])
            codex_offset = rollout.stat().st_size if rollout is not None else None
    # 1. Claim under the lock: re-read state, re-check blockers and controls, write `sending`.
    with locked("state"):
        letter = letters_load().get(letter_id)
        if letter is None:
            return {"ok": False, "letter_id": letter_id, "state": "unknown", "sent": False, "reason": "no-such-letter"}
        state = letter.get("state")
        if (state not in ("journaled", "blocked:not-running", "blocked:unknown-identity") or
                any(row.get("state") in ATTEMPTED_STATES for row in letter.get("history", []))):
            return _result(letter, {"note": "no delivery attempted; letter is not waiting"})
        recipient = letter["to"]
        entry = directory_load()["entries"].get(f"{recipient['app']}:{recipient['id']}") or {
            "app": recipient["app"], "id": recipient["id"], "key": f"{recipient['app']}:{recipient['id']}"}
        refusal = controls_check(entry["key"])
        if refusal:
            _append_journal_locked(_letter_row(letter_id, "refused", sent=False, reason=refusal))
            return _result(dict(letter, state="refused", reason=refusal))
        # Hermes is never "blocked": without a live listener deliver_hermes refuses (proven not sent).
        blocked = None if recipient["app"] == "hermes" else block_reason(entry, codex_now)
        if blocked is None and recipient["app"] == "claude":
            refresh_binding_locked(entry)
        if blocked:
            if blocked != state:
                _append_journal_locked(_letter_row(letter_id, blocked, sent=False, reason=blocked.split(":", 1)[1]))
            detail = (account_mismatch_detail(entry, codex_now)
                      if blocked in ("blocked:other-account", "blocked:unknown-identity") else None)
            if blocked == "blocked:not-running" and recipient["app"] == "claude":
                detail = not_running_detail(entry)  # the helper's letter wake is the only way this chat is woken
            if detail and "registered_account" in detail:
                detail["guidance"] = (GUIDANCE[blocked] + f" Reason: recipient account is "
                                      f"{detail['registered_account'] or 'unknown'}; the current "
                                      f"{APP_LABELS.get(recipient['app'], recipient['app'])} account is "
                                      f"{detail['current_account'] or 'unknown'}.")
            return _result(dict(letter, state=blocked, reason=blocked.split(":", 1)[1]), detail)
        else:
            offset = 0
            if recipient["app"] == "claude":
                path = claude_transcript_path(recipient["id"])
                offset = path.stat().st_size if path is not None else 0
            elif recipient["app"] == "codex":
                offset = codex_offset
            route = {"claude": "claude-session-send", "hermes": "hermes-inject"}.get(recipient["app"], "codex-queue")
            attempt_id = uuid.uuid4().hex[:12]
            _append_journal_locked(_letter_row(letter_id, "sending", attempt_id=attempt_id, route=route,
                                               sending_ns=time.time_ns(),
                                               **({"recipient_offset": offset} if offset is not None else {})))
    die_if_fault("after_sending")
    # 2. Controls checked again immediately before the native leg (no lock held).
    late = pre_send_guard(letter_id, route, attempt_id=attempt_id)
    if late:
        journal_append(_letter_row(letter_id, "refused", sent=False, reason=late, attempt_id=attempt_id))
        return {"ok": False, "letter_id": letter_id, "state": "refused", "sent": False, "reason": late,
                "report_to_owner": True, "guidance": OWNER_REPORT}
    # 3. Native leg.
    if recipient["app"] == "claude":
        state, extra = deliver_claude(letter_id, recipient["id"], letter["message"])
    elif recipient["app"] == "hermes":
        state, extra = deliver_hermes(letter_id, recipient["id"], letter["message"])
    else:
        state, extra = deliver_codex(recipient["id"], letter["message"])
    die_if_fault("after_native")
    with locked("state"):
        current = letters_load().get(letter_id) or {}
        if current.get("state") in TERMINAL_STATES:
            return _result(current)  # readback already settled it; do not downgrade
        _append_journal_locked(_letter_row(letter_id, state, attempt_id=attempt_id,
                                           sent=True if state == "accepted" else False if state == "refused" else None,
                                           **extra))
    if state == "accepted":
        # One short readback; a paused native queue may remain held indefinitely.
        # Transport acceptance is durable and cannot authorize another send.
        count, _ = check_arrival(letter_id)
        # A letter that lands a moment later must be reported arrived, not held: re-read a few times over ~2-3 s.
        # Only a clean "not seen" (0) is re-read; an unavailable readback (None) would just time out again.
        for delay in held_reread_delays(recipient["app"]):
            if count != 0:
                break
            time.sleep(delay)
            count, _ = check_arrival(letter_id, quiet_unseen=True)
        start_codex_wake(letter_id)
    final = letters_load()[letter_id]
    if recipient["app"] == "codex" and final.get("state") == "held":
        return _result(final, codex_held_detail(recipient["id"]))
    if recipient["app"] == "hermes" and final.get("state") == "held" and extra.get("hermes_deferred"):
        return _result(final, {"recipient_activity": "in-progress", "guidance": HERMES_BUSY_GUIDANCE})
    return _result(final)


CODEX_BUSY_GUIDANCE = ("QUEUED, NOT LOST: the recipient Codex chat is in the middle of a turn, and Codex runs a queued "
                       "letter only when that turn ends (this can take many minutes for a long agent turn). It lands "
                       "by itself then; readback shows it. Do NOT resend or replace the letter.")


def codex_held_detail(thread: str) -> dict:
    """Extra result fields for a held Codex letter: the recipient's turn activity, and for a chat that is mid-turn
    the guidance that the queue delivers at turn end (the generic held text says to open the chat, which does not
    help a busy one). Best effort; any failure adds only `recipient_activity: unknown`."""
    try:
        activity, _note = codex_thread_activity(thread)
    except Exception:
        activity = "unknown"
    extra = {"recipient_activity": activity}
    if activity == "in-progress":
        extra["guidance"] = CODEX_BUSY_GUIDANCE
    return extra


def check_arrival(letter_id: str, timeout: float = READBACK_TIMEOUT_S,
                  quiet_unseen: bool = False) -> tuple[int | None, str]:
    """Read native history once; advance acceptance/uncertainty without any delivery. `quiet_unseen` journals
    only an arrival: the send path's quick re-reads must not add a held row each."""
    letter = letters_load().get(letter_id) or {}
    try:
        count, note = arrivals(letter, timeout=timeout)
    except (OSError, ValueError, sqlite3.Error, ProbeError) as error:
        # A readback failure cannot erase durable transport acceptance or invite replay.
        count, note = None, "readback-error:" + type(error).__name__
    with locked("state"):
        current = letters_load().get(letter_id) or {}
        state = current.get("state")
        if state not in ("accepted", "held", "sending", "uncertain"):
            return count, note
        if count is not None and count >= 1:
            _append_journal_locked(_letter_row(letter_id, "arrived", sent=True, via="readback",
                                               reason=None, arrivals=count, readback_note=note,
                                               readback_ns=time.time_ns()))
        elif quiet_unseen and state in ("accepted", "held"):
            pass
        elif state in ("accepted", "held"):
            reason = "native-input-not-observed" if count == 0 else "readback-unavailable"
            _append_journal_locked(_letter_row(letter_id, "held", sent=True, reason=reason,
                                               arrivals=count, readback_note=note,
                                               readback_ns=time.time_ns()))
        else:
            # Persist when checked so unavailable rows cannot monopolize a bounded pass.
            carry = {"attempt_id": current["attempt_id"]} if current.get("attempt_id") else {}
            sending_ns = current.get("sending_ns") or next((row.get("ts_ns") for row in
                current.get("history", []) if row.get("state") == "sending"), current.get("updated_ns"))
            _append_journal_locked(_letter_row(letter_id, state, arrivals=count, readback_note=note,
                                               readback_ns=time.time_ns(), sending_ns=sending_ns, **carry))
    return count, note


# --------------------------------------------------------- asleep Claude chats
# A headless sender reaches only a running Claude chat. A letter to an asleep one stays `blocked:not-running`
# (never sent): the helper's letter wake starts the chat (`claude --desktop --resume`, through the focus gate) and
# the chat's own plugin then takes the letter from its mail slot, which is the only route. There are no relay
# chats, jobs or heartbeats. The wake is not a send, so the helper may retry it; the letter goes out once.
def desktop_hosted_session(session_id) -> bool:
    """True when this session is a live chat that Claude Desktop hosts (its native registration says so)."""
    if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
        return False
    registration = claude_registration(session_id)
    return bool(registration and registration["live"] and registration.get("entrypoint") == "claude-desktop")


def wakeable_desktop_id(found: tuple[str, dict] | None) -> str | None:
    if not found or not LOCAL_ID_RE.fullmatch(found[0]):
        return None
    # Desktop refuses messages to archived chats and routine (scheduled-task) runs (live, 2026-10-09).
    if found[1].get("isArchived") is True or found[1].get("scheduledTaskId"):
        return None
    return found[0]


def wake_target(entry: dict) -> str | None:
    """The Desktop chat ID the helper can wake for a not-running Claude recipient, or None (not a Claude chat,
    Desktop does not list it on the signed-in account, archived, or a routine run). Read-only."""
    if entry.get("app") != "claude":
        return None
    current = current_account()
    if not current["source"].startswith("claude-desktop"):
        return None
    status, found = desktop_lookup(current, entry["id"])
    return wakeable_desktop_id(found) if status != "unreadable" else None


def not_running_detail(entry: dict) -> dict:
    """Extra result fields for a letter to an asleep Claude chat: whether the helper (which wakes it) is running."""
    running = helper_alive()
    wakeable = wake_target(entry) is not None
    if wakeable and running:
        how = ("The Crosstalk helper will wake it (Claude Desktop comes forward for a few seconds, then the owner is "
               "put back) and the letter arrives once through its mail slot.")
    elif wakeable:
        how = ("The Crosstalk helper is NOT running, so nothing will wake it: tell the owner to start the helper "
               "(Startup shortcut) or to open the recipient chat.")
    else:
        how = ("It cannot be woken now: Desktop does not list it on the signed-in account, or it is archived or a "
               "routine run. The owner opens it to deliver.")
    return {"helper_running": running, "wakeable": wakeable,
            "guidance": GUIDANCE["blocked:not-running"] + " " + how}


def relay_retire_inflight() -> list[str]:
    """Upgrade (once): a letter an old relay still held at cutover (`waking`, or `sending` on route desktop-relay)
    goes back to blocked:not-running. Nothing is sent: the mail slot reads the transcript first and its young-send
    guard keeps a wake that may still be landing. Returns the letter IDs moved; idempotent."""
    moved = []
    with locked("state"):
        for letter_id, letter in letters_load().items():
            state = letter.get("state")
            if state == "waking" or (state == "sending" and letter.get("route") == "desktop-relay"):
                _append_journal_locked(_letter_row(letter_id, "blocked:not-running", sent=False,
                                                   reason="relay-retired"))
                moved.append(letter_id)
    return moved


# ------------------------------------------------------------------- mail slot
# One durable idea of the earlier file-based design, kept: a letter waits where the recipient will look. A recipient chat
# pulls its own pending letters when it starts (any start: the helper's letter wake, the owner opening it, a
# Desktop restart) and posts them visibly, after reading its transcript so a letter is never shown twice. So
# "asleep" and "outcome uncertain" both mean "arrives when the chat next opens", and an uncertain letter is
# verified against the transcript before it is shown, never resent blind.
MAIL_SLOT_TTL_S = 24 * 3600.0
MAIL_SLOT_IN_FLIGHT_S = 90.0   # a send this young (any route) may still be landing through session.receive
MAIL_SLOT_PULLED_S = 120.0     # a letter handed out this recently is not handed out again
MAIL_SLOT_STATES = ("blocked:not-running", "blocked:unknown-identity", "sending", "accepted", "held", "uncertain")


def mail_slot_pull(session_id) -> dict:
    """Hand the recipient chat (the caller: identity is its own session ID) each letter waiting for it, once.
    Each handed letter becomes `sending` on route mail-slot; the ordinary readback promotes it to arrived
    when the posted letter shows in the transcript, and a post that never shows ends uncertain and is
    offered again at the next start."""
    if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
        return {"ok": False, "reason": "invalid-session-id", "letters": []}
    handed, skipped = [], []
    with locked("state"):
        now = time.time_ns()
        transcript = claude_transcript_path(session_id)
        for letter in sorted(letters_load().values(), key=lambda row: row.get("created_ns") or 0):
            letter_id, state, to = letter.get("letter_id"), letter.get("state"), letter.get("to") or {}
            if to.get("app") != "claude" or to.get("id") != session_id or state not in MAIL_SLOT_STATES:
                continue
            if (now - int(letter.get("created_ns") or 0)) / 1e9 > MAIL_SLOT_TTL_S:
                skipped.append({"letter_id": letter_id, "why": "older-than-mail-slot-ttl"})
                continue
            if letter.get("pulled_ns") and (now - int(letter["pulled_ns"])) / 1e9 < MAIL_SLOT_PULLED_S:
                skipped.append({"letter_id": letter_id, "why": "pulled-recently"})
                continue
            count, note = claude_arrivals(session_id, letter_id, 0)
            if count is None:
                # Readback unavailable proves nothing: hand a letter out only when zero arrivals is proven.
                skipped.append({"letter_id": letter_id, "why": "readback-unavailable"})
                continue
            if count:
                _append_journal_locked(_letter_row(letter_id, "arrived", sent=True, via="mail-slot-readback",
                                                   reason=None, arrivals=count, readback_note=note, readback_ns=now))
                skipped.append({"letter_id": letter_id, "why": "already-in-transcript"})
                continue
            # A send this young, whatever its route, may still land: a chat that starts while a running-chat send
            # is in flight must not get the letter a second time (the transcript can lag the native accept).
            started = int(letter.get("sending_ns") or 0)
            if started and (now - started) / 1e9 < MAIL_SLOT_IN_FLIGHT_S:
                skipped.append({"letter_id": letter_id, "why": "send-in-flight"})
                continue
            # Readback proved zero arrivals above: the one case where an attempted letter may go out again.
            refusal = pre_send_guard(letter_id, "mail-slot", zero_arrivals_proven=True)
            if refusal:
                skipped.append({"letter_id": letter_id, "why": refusal})
                continue
            attempt_id = uuid.uuid4().hex[:12]
            _append_journal_locked(_letter_row(letter_id, "sending", sent=None, attempt_id=attempt_id, route="mail-slot",
                                               recipient_offset=transcript.stat().st_size if transcript else 0,
                                               sending_ns=now, pulled_ns=now, pulled_session=session_id, reason=None))
            handed.append({"letter_id": letter_id, "attempt_id": attempt_id, "text": letter["message"]})
    return {"ok": True, "letters": handed, "skipped": skipped[:20]}


def deliver_claude(letter_id: str, target: str, message: str) -> tuple[str, dict]:
    """Restricted live-session sender: a throwaway `claude -p` runs one Mod command that calls
    $.session.send({to:{sessionId}, text}). Zero model turns. Pattern proven in tool4/create3."""
    exe = claude_exe()
    if exe is None or not (SENDER_PLUGIN / "hooks" / "register.mjs").is_file():
        return "refused", {"reason": "claude-cli-or-sender-plugin-unavailable"}
    caller = str(uuid.uuid4())
    payload = {"tag": SENDER_TAG, "caller": caller, "target": target, "letter": letter_id, "text": message}
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(encoded) > 30000:  # a Windows environment value is limited to 32767 characters
        return "refused", {"reason": "payload-too-large-for-sender"}
    env, filled = child_env({"CROSSTALK_P0_ROLE": "sender", "CROSSTALK_P0_PAYLOAD": encoded})
    argv = _exe_argv(exe) + ["-p", "/" + SENDER_COMMAND, "--session-id", caller,
                             "--plugin-dir", str(SENDER_PLUGIN), "--output-format", "json"]
    try:
        with tempfile.TemporaryDirectory(prefix="crosstalk-p0-sender-") as cwd:
            done = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=float(os.environ.get("CROSSTALK_P0_CLAUDE_TIMEOUT_S") or CLAUDE_SEND_TIMEOUT_S),
                                  creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "uncertain", {"reason": "sender-timeout", "sender_session": caller}
    except FileNotFoundError:
        return "refused", {"reason": "claude-cli-not-launched"}
    except OSError as error:
        return "uncertain", {"reason": "sender-os-error:" + type(error).__name__, "sender_session": caller}
    extra = {"sender_session": caller, "sender_exit": done.returncode, "env_filled": filled}
    try:
        outer = json.loads(done.stdout)
        raw = outer.get("result") if isinstance(outer, dict) else None
        inner = json.loads(raw[raw.index("{"):]) if isinstance(raw, str) and "{" in raw else {}
    except ValueError:
        return "uncertain", dict(extra, reason="sender-output-unparseable")
    if not isinstance(outer, dict) or not isinstance(inner, dict):
        return "uncertain", dict(extra, reason="sender-output-unparseable")
    outer_ok = (done.returncode == 0 and outer.get("is_error") is False and outer.get("num_turns") == 0
                and outer.get("session_id") == caller and inner.get("tag") == SENDER_TAG)
    if not outer_ok:
        return "uncertain", dict(extra, reason="sender-receipt-unverified")
    extra["sender_version"] = inner.get("version") if isinstance(inner.get("version"), str) else None
    status = inner.get("status")
    if status == "not-attempted":  # the sender's own preflight stopped before any native call
        return "refused", dict(extra, reason="sender-guard:" + clip_reason(inner.get("reason", "")))
    if inner.get("letter") != letter_id:
        return "uncertain", dict(extra, reason="sender-letter-mismatch")
    if status == "queued" and inner.get("isDelivered") is True:
        return "accepted", extra
    if status == "refused" and inner.get("isDelivered") is False:
        return "refused", dict(extra, reason="native-not-delivered:" + clip_reason(inner.get("reason", "")))
    return "uncertain", dict(extra, reason="sender-outcome-unverified")


def deliver_codex(thread: str, message: str) -> tuple[str, dict]:
    """A letter to a Codex chat. First a private app-server tries to resume the chat and run the letter as a
    headless turn (an asleep chat); when that sent nothing (the app holds the chat, the resume failed, ...) the
    letter goes by `codex queue --thread <exact id> --message <text>` as in tool4."""
    exe = codex_exe()
    if exe is None:
        return "refused", {"reason": "codex-cli-unavailable"}
    fallback = None
    if codex_headless_enabled():
        settled, why = codex_headless_try(thread, message)
        if settled is not None:
            return settled
        fallback = why
    state, extra = _deliver_codex_queue(exe, thread, message)
    return (state, dict(extra, codex_headless_fallback=fallback)) if fallback else (state, extra)


def _deliver_codex_queue(exe: str, thread: str, message: str) -> tuple[str, dict]:
    env, filled = child_env()
    try:
        done = subprocess.run(_exe_argv(exe) + ["queue", "--thread", thread, "--message", message],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CODEX_QUEUE_TIMEOUT_S, env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "uncertain", {"reason": "codex-queue-timeout"}
    except FileNotFoundError:
        return "refused", {"reason": "codex-cli-not-launched"}
    except OSError as error:
        return "uncertain", {"reason": "codex-queue-os-error:" + type(error).__name__}
    match = re.search(r"Queued message ([0-9a-f-]{36}) for thread " + re.escape(thread), done.stdout or "")
    extra = {"codex_exit": done.returncode, "env_filled": filled}
    if done.returncode == 0 and match and UUID_RE.fullmatch(match.group(1)):
        return "accepted", dict(extra, queue_id=match.group(1))
    return "uncertain", dict(extra, reason="codex-queue-unconfirmed",
                             stderr_tail=(done.stderr or "").strip()[-200:])


# ------------------------------------------------------ last-resort Codex wake
# Codex unloads idle chats; a letter queued to an ASLEEP chat then waits until the chat is opened. After a Codex
# letter is accepted and still held, ONE detached waiter (CLI `codex-wake`) looks again after CODEX_WAKE_DELAY_S.
# Only if the letter is still not in the chat and its rollout shows no turn in progress does the waiter open
# codex://threads/<id> once: Codex resumes the chat and the queued letter runs. The waiter never sends, resends
# or replaces a letter, and any failure here leaves the letter's state and result exactly as they were.
def _note_letter(letter_id: str, **fields) -> bool:
    """Journal extra fields on a still-pending letter without changing its state. The state is re-read under
    the lock, so a letter that arrived meanwhile is never downgraded."""
    with locked("state"):
        state = (letters_load().get(letter_id) or {}).get("state")
        if state not in ("accepted", "held"):
            return False
        current = letters_load().get(letter_id) or {}
        extra = {"attempt_id": current["attempt_id"]} if current.get("attempt_id") else {}
        _append_journal_locked(_letter_row(letter_id, state, **extra, **fields))
        return True


def open_codex_link(url: str) -> None:
    """Hand a codex:// link to Windows (ShellExecute); it brings Codex and that chat to the front."""
    os.startfile(url)  # Windows only; default_link_opener() says when it is not available


def default_link_opener():
    return open_codex_link if hasattr(os, "startfile") else None


def codex_rollout_path(thread_id: str) -> Path | None:
    """The thread's rollout file (sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl; newest if several), or None
    when absent, compressed, or the bounded walk did not finish."""
    if not UUID_RE.fullmatch(thread_id):
        return None
    suffix = f"-{thread_id}.jsonl"
    deadline = time.monotonic() + ROLLOUT_LOOKUP_S
    budget = ROLLOUT_LOOKUP_ENTRIES
    folders, matches = [codex_sessions_dir()], []
    try:
        while folders:
            with os.scandir(folders.pop()) as entries:
                for entry in entries:
                    budget -= 1
                    if budget < 0 or time.monotonic() >= deadline:
                        return None
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(entry.path)
                    elif entry.name.startswith("rollout-") and entry.name.endswith(suffix):
                        matches.append((entry.stat().st_mtime, entry.path))
    except OSError:
        return None
    return Path(max(matches)[1]) if matches else None


def codex_thread_activity(thread_id: str) -> tuple[str, str]:
    """("in-progress" | "idle" | "unknown", note) from the tail of the thread's rollout. The last turn marker
    wins: task_started means a turn is running; task_complete or turn_aborted (Codex's interrupt) means it
    ended. A missing, unreadable or partial rollout, or one with no marker, is "unknown" (never idle)."""
    path = codex_rollout_path(thread_id)
    if path is None:
        return "unknown", "rollout-not-found"
    try:
        size = path.stat().st_size
        for span in ROLLOUT_TAIL_BYTES:
            start = max(0, size - span)
            with path.open("rb") as stream:
                stream.seek(start)
                data = stream.read()
            lines = data.splitlines()
            if start > 0 and lines:
                lines = lines[1:]  # the first line of a tail read is cut
            if data and not data.endswith(b"\n") and lines:
                try:
                    json.loads(lines[-1])
                except ValueError:
                    return "unknown", "rollout-tail-partial"  # a writer is mid-line: do not guess
            last = None
            for raw in lines:
                if b'"task_started"' not in raw and b'"task_complete"' not in raw and b'"turn_aborted"' not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                payload = row.get("payload") if isinstance(row, dict) and row.get("type") == "event_msg" else None
                kind = payload.get("type") if isinstance(payload, dict) else None
                if kind == "task_started":
                    last = "in-progress"
                elif kind in ("task_complete", "turn_aborted"):
                    last = "idle"
            if last:
                return last, "rollout-tail"
            if start == 0:
                break
    except OSError:
        return "unknown", "rollout-unreadable"
    return "unknown", "no-turn-marker"


def windowless_python() -> str:
    """The interpreter for a detached child. DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW, and a console
    python.exe (or a venv's launcher stub that starts the real one) then opens a visible console window; the
    sibling pythonw.exe has none (live 2026-10-18: a window titled with the runtime python.exe after each letter)."""
    if os.name == "nt":
        sibling = Path(sys.executable).with_name("pythonw.exe")
        if sibling.is_file():
            return str(sibling)
    return sys.executable


def codex_wake_argv(letter_id: str) -> list[str]:
    return [windowless_python(), "-I", "-B", str(KIT / "crosstalk_p0_cli.py"), "codex-wake", letter_id]


def _spawn_detached(argv: list[str]) -> int:
    """Start a detached child and return its pid. It outlives this process and shows no window."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                 cwd=tempfile.gettempdir(), env=child_env()[0])
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if breakaway:
            # Leave a host job that kills its processes when the helper ends; a job that forbids breakaway
            # refuses the whole CreateProcess (nothing started), so start inside the job instead.
            try:
                return subprocess.Popen(argv, creationflags=flags | breakaway, **quiet).pid
            except PermissionError:
                pass
        return subprocess.Popen(argv, creationflags=flags, **quiet).pid
    return subprocess.Popen(argv, start_new_session=True, **quiet).pid


def spawn_codex_wake(letter_id: str) -> int:
    """Start the detached waiter and return its pid. It outlives this helper and shows no window."""
    return _spawn_detached(codex_wake_argv(letter_id))


def start_codex_wake(letter_id: str) -> bool:
    """After the first readback: start ONE waiter for a Codex letter that is still held. Never raises, and a
    failure changes neither the letter's state nor the caller's result."""
    try:
        if os.environ.get("CROSSTALK_P0_CODEX_WAKE", "").strip().lower() in ("off", "0", "false", "no"):
            return False
        letter = letters_load().get(letter_id) or {}
        if (letter.get("to") or {}).get("app") != "codex" or letter.get("state") != "held" \
                or letter.get("codex_wake_spawned_ns") or letter.get("codex_headless"):
            return False  # a headless letter has its own turn: never open the app link over it
        pid = spawn_codex_wake(letter_id)
        _note_letter(letter_id, codex_wake_spawned_ns=time.time_ns(), codex_wake_pid=pid)
        return True
    except Exception:
        return False


CODEX_WAKE_STRAND_GRACE_S = 120.0   # a waiter with no outcome this long past its delay + link watch is presumed gone
CODEX_WAKE_SKIP_RETRY_S = 600.0     # a waiter that skipped (turn in progress, unreadable) is tried again after this
CODEX_WAKE_MAX_RESPAWNS = 6         # waiters re-spawned per letter, ever (journaled as codex_wake_respawns)
CODEX_WAKE_RETRYABLE_SKIPS = ("skipped:in-progress", "skipped:unknown", "skipped:readback-unavailable",
                              "skipped:headless-turn-running", "skipped:waiter-exit")


def codex_wake_stranded_candidates(now_ns: int | None = None) -> list[str]:
    """Held Codex letters whose link waiter is gone without opening the link (oldest first). Either the waiter was
    spawned and journaled no outcome and its pid is dead (or it is far older than delay + link watch), or it
    journaled a retryable skip (the chat's turn was in progress, the readback was unavailable) and has not been
    tried again for CODEX_WAKE_SKIP_RETRY_S. Never a headless letter, never one whose link was already claimed,
    never past CODEX_WAKE_MAX_RESPAWNS, never older than the mail slot's lifetime. Read-only."""
    now = time.time_ns() if now_ns is None else now_ns
    bound_ns = int((CODEX_WAKE_DELAY_S + CODEX_LINK_POLL_S + CODEX_WAKE_STRAND_GRACE_S) * 1e9)
    found = []
    for letter in sorted(letters_load().values(), key=lambda row: row.get("created_ns") or 0):
        to = letter.get("to") or {}
        spawned = int(letter.get("codex_wake_spawned_ns") or 0)
        if (to.get("app") != "codex" or letter.get("state") not in ("accepted", "held") or not spawned
                or letter.get("codex_headless") or letter.get("codex_link_ns")
                or (now - int(letter.get("created_ns") or 0)) / 1e9 > MAIL_SLOT_TTL_S
                or int(letter.get("codex_wake_respawns") or 0) >= CODEX_WAKE_MAX_RESPAWNS):
            continue
        gone = not pid_alive(letter.get("codex_wake_pid")) or now - spawned > bound_ns
        outcome_ns = int(letter.get("codex_wake_ns") or 0)
        wake = letter.get("codex_wake")
        if outcome_ns < spawned or wake in ("retry-spawned", "respawned"):
            if gone and now - spawned > bound_ns // 4:  # no outcome: the waiter died or was killed silently
                found.append(letter["letter_id"])
        elif wake in CODEX_WAKE_RETRYABLE_SKIPS and gone and (now - outcome_ns) / 1e9 >= CODEX_WAKE_SKIP_RETRY_S:
            found.append(letter["letter_id"])
    return found


def codex_wake_respawn(letter_id: str) -> bool:
    """Start ONE new link waiter for a letter whose waiter is gone (see codex_wake_stranded_candidates). Never sends,
    queues or resends: the waiter only reads back and opens the chat's link under the same checks, claim, cooldown
    and wake budget as the first one. Journals the retry. Never raises."""
    try:
        if controls_check(None) or letter_id not in codex_wake_stranded_candidates():
            return False
        letter = letters_load().get(letter_id) or {}
        tries = int(letter.get("codex_wake_respawns") or 0) + 1
        previous = letter.get("codex_wake") or "no-outcome"
        pid = spawn_codex_wake(letter_id)
        _note_letter(letter_id, reason="codex-wake-respawned:" + previous, codex_wake="respawned",
                     codex_wake_spawned_ns=time.time_ns(), codex_wake_pid=pid, codex_wake_respawns=tries)
        return True
    except Exception:
        return False


def codex_budget_retry_candidates(now_ns: int | None = None) -> list[str]:
    """Letters to a Codex chat whose link open was refused by the wake budget and whose chat has room in the budget
    again (oldest first, never older than the mail slot's lifetime). Read-only."""
    now = time.time_ns() if now_ns is None else now_ns
    found = []
    for letter in sorted(letters_load().values(), key=lambda row: row.get("created_ns") or 0):
        to = letter.get("to") or {}
        if (to.get("app") == "codex" and letter.get("state") in ("accepted", "held")
                and letter.get("codex_wake") == "skipped:wake-budget" and not letter.get("codex_headless")
                and (now - int(letter.get("created_ns") or 0)) / 1e9 <= MAIL_SLOT_TTL_S
                and wake_budget_room(f"codex:{to.get('id')}", now)):
            found.append(letter["letter_id"])
    return found


def codex_budget_retry(letter_id: str) -> bool:
    """Start the link waiter again for a letter the budget held back. Never sends or resends: the waiter only reads
    back and opens the chat's link under the same checks, claims and budget as the first one. Never raises."""
    try:
        if controls_check(None) or letter_id not in codex_budget_retry_candidates():
            return False
        pid = spawn_codex_wake(letter_id)
        _note_letter(letter_id, codex_wake="retry-spawned", codex_wake_spawned_ns=time.time_ns(), codex_wake_pid=pid)
        return True
    except Exception:
        return False


def _claim_codex_link(letter_id: str, thread: str) -> tuple[bool, str]:
    """Under the state lock: may this letter's waiter open the chat's link now? On yes the claim is journaled
    first, so a second waiter, a retry or a later letter to the same chat cannot open another link."""
    with locked("state"):
        letters = letters_load()
        letter = letters.get(letter_id) or {}
        recipient = letter.get("to") or {}
        state = letter.get("state")
        if state not in ("accepted", "held"):
            return False, f"state-{state}"
        if recipient.get("app") != "codex" or recipient.get("id") != thread or not UUID_RE.fullmatch(thread):
            return False, "recipient-mismatch"
        if letter.get("codex_link_ns"):
            return False, "link-already-claimed"
        refusal = controls_check(f"codex:{thread}")
        if refusal:
            return False, refusal
        now = time.time_ns()
        for other in letters.values():
            there = other.get("to") or {}
            if other.get("letter_id") != letter_id and there.get("app") == "codex" and there.get("id") == thread \
                    and other.get("codex_link_ns") and (now - int(other["codex_link_ns"])) / 1e9 < CODEX_LINK_COOLDOWN_S:
                return False, "link-cooldown"
        _append_journal_locked(_letter_row(letter_id, state, reason="codex-link-claimed", codex_link_ns=now))
        return True, "claimed"


def _wake_report(letter_id: str, wake: str, **extra) -> dict:
    letter = letters_load().get(letter_id) or {}
    return dict({"ok": True, "letter_id": letter_id, "wake": wake, "state": letter.get("state"),
                 "arrivals": letter.get("arrivals")}, **extra)


def codex_wake(letter_id: str, delay_s: float = CODEX_WAKE_DELAY_S, *, sleep=time.sleep, opener=None,
               poll_s: float = CODEX_LINK_POLL_S, poll_step_s: float = CODEX_LINK_POLL_STEP_S) -> dict:
    """The waiter, with a guaranteed outcome: whatever way the body ends (a path that journals nothing, an
    exception), a letter still pending gets a `codex_wake` outcome, so the helper can tell a finished waiter from a
    vanished one (a waiter killed outright journals nothing; the helper re-spawns it, codex_wake_respawn)."""
    started = time.time_ns()
    report, failure = None, None
    try:
        report = _codex_wake_body(letter_id, delay_s, sleep=sleep, opener=opener, poll_s=poll_s,
                                  poll_step_s=poll_step_s)
        return report
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            letter = letters_load().get(letter_id) or {}
            if letter.get("state") in ("accepted", "held") and int(letter.get("codex_wake_ns") or 0) < started:
                if failure is not None:
                    wake, detail = "skipped:waiter-exit", type(failure).__name__
                else:
                    wake, detail = (report or {}).get("wake") or "skipped:waiter-exit", None
                _note_letter(letter_id, reason="codex-wake-" + wake, codex_wake=wake, codex_wake_ns=time.time_ns(),
                             codex_wake_detail=detail)
        except Exception:
            pass


def _codex_wake_body(letter_id: str, delay_s: float, *, sleep, opener, poll_s: float, poll_step_s: float) -> dict:
    """The detached waiter for one accepted Codex letter. Waits, reads back once, and only for a letter still
    not in the chat, in a chat with no turn in progress, opens its link (once per letter, once per chat per
    cooldown, never against the controls) and then watches for the arrival. Never queues or sends anything.
    Every outcome is journaled on the still-pending letter as `codex_wake` (state unchanged)."""
    def skip(why: str, reason: str | None = None, detail: str | None = None, **fields) -> dict:
        wake = "skipped:" + why
        _note_letter(letter_id, reason=reason or "codex-wake-" + wake, codex_wake=wake,
                     codex_wake_ns=time.time_ns(), codex_wake_detail=detail, **fields)
        return _wake_report(letter_id, wake, detail=detail) if detail else _wake_report(letter_id, wake)

    recipient = (letters_load().get(letter_id) or {}).get("to") or {}
    thread = recipient.get("id")
    if recipient.get("app") != "codex" or not isinstance(thread, str) or not UUID_RE.fullmatch(thread):
        return _wake_report(letter_id, "skipped:not-a-codex-letter")
    if (letters_load().get(letter_id) or {}).get("codex_headless"):
        return _wake_report(letter_id, "skipped:headless-turn")  # our own waiter runs it; never open the app over it
    sleep(max(0.0, delay_s))
    count, _note = check_arrival(letter_id, timeout=CODEX_WAKE_READBACK_TIMEOUT_S)
    state = (letters_load().get(letter_id) or {}).get("state")
    if state not in ("accepted", "held"):
        return _wake_report(letter_id, f"done:letter-{state}")
    if count != 0:
        return skip("readback-unavailable")  # not provably absent from the chat: a link could be for nothing
    if _claim_live(_claims_load().get(thread)):
        return skip("headless-turn-running")  # one of our own turns holds this chat: the link would open over it
    activity, detail = codex_thread_activity(thread)
    if activity != "idle":
        return skip(activity, detail=detail)
    opener = opener or default_link_opener()
    if opener is None:
        return skip("unsupported-platform", reason="codex-link-unsupported-platform")
    # Account recheck at the open boundary, from a fresh read (never the cached one): a different or unreadable
    # Codex account means no link is opened (the letter stays held, never resent).
    entry = directory_load()["entries"].get(f"codex:{thread}") or {"app": "codex", "id": thread, "key": f"codex:{thread}"}
    try:
        fresh = codex_account(use_cache=False)
    except Exception as error:
        fresh = {"app": "codex", "id": None, "error": type(error).__name__}
    account = codex_binding_state(entry, fresh)["state"]
    if account != "match":
        return skip("account-" + account, reason="codex-link-not-opened:account-" + account,
                    codex_link_result="refused:account-" + account)

    def poll() -> None:
        for _ in range(max(1, round(poll_s / max(poll_step_s, 0.001)))):
            sleep(max(0.0, poll_step_s))
            count, _note = check_arrival(letter_id)
            if count or (letters_load().get(letter_id) or {}).get("state") not in ("accepted", "held"):
                break

    # The link open is a wake: counted when decided. Over budget the letter stays queued and unwoken; the helper
    # tries again when the budget frees. A claim that turns out not to open anything gives its slot back.
    budget = wake_budget_claim(f"codex:{thread}", "codex-link")
    if not budget["allowed"]:
        return skip("wake-budget", reason="codex-wake-budget:" + budget["reason"])
    claimed, why = _claim_codex_link(letter_id, thread)
    if not claimed:
        wake_budget_refund(budget["event"])
    if claimed:
        def open_link() -> dict:
            try:
                opener(CODEX_LINK_FORMAT.format(thread))
                result = "opened"
            except Exception as error:
                result = "open-failed:" + type(error).__name__
            opened = result == "opened"
            _note_letter(letter_id, reason="codex-link-" + ("opened" if opened else "open-failed"),
                         codex_wake="link-opened" if opened else "skipped:" + result, codex_wake_ns=time.time_ns(),
                         codex_link_result=result, codex_link_opened_ns=time.time_ns())
            return {"launched": opened, "result": result}

        def arrived(_start_ns: int) -> bool:
            count, _note = check_arrival(letter_id)
            return bool(count) or (letters_load().get(letter_id) or {}).get("state") not in ("accepted", "held")

        # The one focus-taking step: notice, link open, give focus back, then confirm by the letter's own arrival
        # readback. The gate rechecks the controls for this exact chat right before the open.
        focus = focus_wake(f"codex:{thread}", open_link, arrived, text="losing focus to wake Codex chat",
                           kind="codex-link", lock_timeout_s=300.0, confirm_timeout_s=poll_s,
                           confirm_step_s=poll_step_s, sleep=sleep)
        if focus.get("refused"):
            late = focus["refused"]
            return skip(late, reason="codex-link-not-opened:" + late, codex_link_result="refused:" + late)
        if not focus.get("launched"):
            result = (focus.get("action_result") or {}).get("result") or "open-failed"
            return _wake_report(letter_id, "skipped:" + result)
    elif why == "link-cooldown":
        skip(why)  # another letter's link woke this chat moments ago: this letter shares it and only watches
        poll()
    else:
        if why == "link-already-claimed":
            return _wake_report(letter_id, "skipped:" + why)  # nothing to add to the claim already journaled
        return skip(why)
    return _wake_report(letter_id, "link-opened" if claimed else "skipped:link-cooldown")


# ---------------------------------------------- asleep Codex chat: resume first (rework slice 3)
# A letter to a Codex chat the app has NOT loaded goes in through a private `codex app-server`: thread/resume, then
# the letter itself as the turn/start input (one sender; the turn id is the receipt), run with the chat's own model,
# effort, folder, sandbox and approval settings. A chat the app holds refuses the resume (`already has an active
# writer`) before any effect, and the letter goes by `codex queue` as before. The connection must outlive the
# sender, so a detached waiter (CLI `codex-headless`) owns the server. Nobody watches a headless turn, so the
# waiter declines every request the server sends back, enforces a hard deadline, rechecks the controls every few
# seconds and interrupts on halt, messaging off or deny. One claim per chat (state lock) keeps a second letter from
# mistaking our own running turn for the app. The server runs in a kill-on-close job object.
CODEX_HEADLESS_CLIENT = "crosstalk_codex_wake"
CODEX_CLAIM_STALE_S = 30.0          # a claim whose waiter has not beaten for this long has no waiter
CODEX_CLAIM_WAIT_S = 25.0           # a second letter waits this long for the first waiter, then is refused unsent
CODEX_HEADLESS_START_S = 90.0       # how long the sender waits for the waiter to resume and start the turn
CODEX_HEADLESS_POLL_S = 0.25
CODEX_HEADLESS_DEADLINE_S = 900.0   # hard limit for one headless turn
CODEX_HEADLESS_CONTROLS_S = 3.0     # controls and heartbeat cadence
CODEX_HEADLESS_INTERRUPT_WAIT_S = 20.0
CODEX_HEADLESS_REQUEST_S = 45.0
CODEX_CLEANUP_MAX_TRIES = 3
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_POLICIES = ("untrusted", "on-failure", "on-request", "never")


def codex_claims_path() -> Path:
    return device_dir() / "codex-claims.json"


def _claims_load() -> dict:
    """The claims file. A lock-free read can meet a writer's atomic replace (Windows then refuses the open for a
    moment), so a failed read is retried; it never reports an absent claim for a read that merely collided."""
    value = {}
    for _attempt in range(10):
        try:
            value = json.loads(codex_claims_path().read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            time.sleep(0.02)
    return value if isinstance(value, dict) else {}


def _claims_mutate(mutate):
    """Run mutate(claims) under the state lock and save the result; returns mutate's value. Never nest."""
    with locked("state"):
        claims = _claims_load()
        value = mutate(claims)
        _write_json_atomic(codex_claims_path(), claims)
        return value


def _claim_live(claim, now_ns: int | None = None, stale_s: float = CODEX_CLAIM_STALE_S) -> bool:
    if not isinstance(claim, dict) or claim.get("released"):
        return False
    return ((now_ns or time.time_ns()) - int(claim.get("beat_ns") or 0)) / 1e9 < stale_s


def _claim_update(thread: str, letter_id: str, expect=None, **fields) -> bool:
    """Change this letter's own claim, only while it is unreleased and (when given) in one of the `expect` phases."""
    def mutate(claims):
        claim = claims.get(thread)
        if not claim or claim.get("letter_id") != letter_id or claim.get("released"):
            return False
        if expect is not None and claim.get("phase") not in expect:
            return False
        claim.update(fields)
        if "beat_ns" not in fields:
            claim["beat_ns"] = time.time_ns()
        return True
    return _claims_mutate(mutate)


def codex_headless_enabled() -> bool:
    return os.environ.get("CROSSTALK_P0_CODEX_HEADLESS", "").strip().lower() not in ("off", "0", "false", "no")


def _letter_id_of(message: str) -> str | None:
    match = re.search(r"\nref (CTP0-[0-9a-f]{12})\Z", message) if isinstance(message, str) else None
    return match.group(1) if match else None


def _headless_note(letter_id: str, **fields) -> None:
    """Journal extra fields on a letter without changing its state (any state, including a settled one)."""
    with locked("state"):
        letter = letters_load().get(letter_id)
        if letter:
            extra = {"attempt_id": letter["attempt_id"]} if letter.get("attempt_id") else {}
            _append_journal_locked(_letter_row(letter_id, letter.get("state"), **extra, **fields))


def codex_turn_aborted_since(thread_id: str, since_ns: int) -> bool:
    """True when the thread's rollout tail holds a turn_aborted event stamped after since_ns (a turn was stopped).
    Unreadable or unstamped means False here; the caller's own flags carry the rest."""
    path = codex_rollout_path(thread_id)
    if path is None:
        return False
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(max(0, size - ROLLOUT_TAIL_BYTES[1]))
            data = stream.read()
    except OSError:
        return False
    for raw in data.splitlines():
        if b'"turn_aborted"' not in raw:
            continue
        try:
            row = json.loads(raw)
            payload = row.get("payload") if row.get("type") == "event_msg" else None
            if not isinstance(payload, dict) or payload.get("type") != "turn_aborted":
                continue
            stamp = datetime.fromisoformat(str(row.get("timestamp")).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if stamp.timestamp() * 1e9 > since_ns:
            return True
    return False


def _snake_to_camel(name: str) -> str:
    head, *rest = re.split(r"[-_]", name)
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _sandbox_mode(value) -> str | None:
    kind = value.get("type") if isinstance(value, dict) else value
    if not isinstance(kind, str):
        return None
    mode = re.sub(r"(?<!^)(?=[A-Z])", "-", kind).replace("_", "-").lower()
    return mode if mode in SANDBOX_MODES else None


def _sandbox_policy(value) -> dict | None:
    """The turn/start `sandboxPolicy` object (camelCase tag and keys) for a rollout or server sandbox value."""
    mode = _sandbox_mode(value)
    if mode is None:
        return None
    extra = {_snake_to_camel(key): item for key, item in value.items() if key != "type"} if isinstance(value, dict) else {}
    return dict(extra, type=_snake_to_camel(mode))


def codex_thread_settings(thread_id: str) -> dict:
    """The chat's own settings from the last `turn_context` in its rollout: model, effort, cwd, approval, sandbox
    (any may be missing). A headless turn started without them would take the CLI's defaults instead."""
    path = codex_rollout_path(thread_id)
    if path is None:
        return {}
    last = None
    try:
        size = path.stat().st_size
        for span in ROLLOUT_TAIL_BYTES:
            with path.open("rb") as stream:
                stream.seek(max(0, size - span))
                data = stream.read()
            for raw in data.splitlines():
                if b'"turn_context"' in raw:
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("type") == "turn_context" and isinstance(row.get("payload"), dict):
                        last = row["payload"]
            if last is not None or span >= size:
                break
    except OSError:
        return {}
    if last is None:
        return {}
    return {"model": last.get("model"), "effort": last.get("effort"), "cwd": last.get("cwd"),
            "approval": last.get("approval_policy"), "sandbox": last.get("sandbox_policy")}


def _settings_with_resume(settings: dict, resumed) -> dict:
    """Fill what the rollout lacked from the thread/resume response (model, reasoningEffort, cwd, approvalPolicy,
    sandbox); the rollout's own values win."""
    resumed = resumed if isinstance(resumed, dict) else {}
    merged = dict(settings)
    for name, source in (("model", "model"), ("effort", "reasoningEffort"), ("cwd", "cwd"),
                         ("approval", "approvalPolicy"), ("sandbox", "sandbox")):
        if not merged.get(name) and resumed.get(source):
            merged[name] = resumed[source]
    return merged


def _settings_missing(settings: dict) -> list[str]:
    missing = [name for name in ("model", "cwd") if not isinstance(settings.get(name), str) or not settings[name]]
    if settings.get("effort") not in EFFORTS:
        missing.append("effort")
    if settings.get("approval") not in APPROVAL_POLICIES:
        missing.append("approval")
    if _sandbox_mode(settings.get("sandbox")) is None:
        missing.append("sandbox")
    if not missing and not Path(settings["cwd"]).is_dir():
        missing.append("cwd-not-a-folder")
    return missing


def _decline_server_request(server, message: dict, tally: dict) -> None:
    """Answer a request the server sent us. Never approves: approvals are declined, questions and elicitations
    are declined, anything else is an error. Counts them in tally."""
    method = str(message.get("method") or "")
    request_id = message.get("id")
    tally["declined"] = tally.get("declined", 0) + 1
    methods = tally.setdefault("methods", [])
    if method[:60] not in methods and len(methods) < 8:
        methods.append(method[:60])
    try:
        if method == "item/permissions/requestApproval":
            server.reply(request_id, {"permissions": {}})  # its reply type has no decision: an empty grant grants nothing
        elif "requestApproval" in method:
            server.reply(request_id, {"decision": "decline"})
        elif method in ("execCommandApproval", "applyPatchApproval"):
            server.reply(request_id, {"decision": "denied"})
        elif "elicitation" in method.lower():
            server.reply(request_id, {"action": "decline"})
        else:
            server.reply(request_id, error={"code": -32601, "message": "declined: no one is watching this headless turn"})
    except OSError:
        pass


def _headless_call(server, method: str, params: dict, timeout: float, backlog: list, tally: dict):
    """One request on the headless connection. Server requests that arrive meanwhile are declined, notifications
    are kept in backlog for the turn loop, and a response to anything else is ignored."""
    request_id = server.send_request(method, params)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(method)
        try:
            message = server.lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if message is None:
            raise ProbeError("app-server-exited")
        if not isinstance(message, dict):
            continue
        if "method" in message and "id" in message:
            _decline_server_request(server, message, tally)
        elif "method" in message:
            backlog.append(message)
        elif message.get("id") == request_id:
            if "error" in message:
                problem = message["error"] if isinstance(message["error"], dict) else {}
                raise ProbeError("app-server-error", str(problem.get("message", ""))[:200])
            return message.get("result")


def _take_claim(claims: dict, thread: str, letter_id: str) -> str:
    claim = claims.get(thread)
    if _claim_live(claim):
        return "same-letter" if claim.get("letter_id") == letter_id else "busy"
    now = time.time_ns()
    claims[thread] = {"letter_id": letter_id, "phase": "spawning", "started_ns": now, "beat_ns": now}
    return "taken"


def spawn_codex_headless(letter_id: str, cleanup: bool = False) -> int:
    """Start the detached headless waiter (or, with cleanup, the bookkeeping-only pass) and return its pid."""
    argv = [windowless_python(), "-I", "-B", str(KIT / "crosstalk_p0_cli.py"), "codex-headless", letter_id]
    return _spawn_detached(argv + (["--cleanup"] if cleanup else []))


def codex_headless_try(thread: str, message: str) -> tuple[tuple[str, dict] | None, str]:
    """Try to deliver through a resumed headless turn. Returns (result, why): a (state, extra) result when this
    path settled the letter (accepted, refused or uncertain), or (None, why) when nothing was sent and the caller
    should use `codex queue` (the app holds the chat, the resume failed, no settings, ...)."""
    letter_id = _letter_id_of(message)
    letter = letters_load().get(letter_id) if letter_id else None
    if not letter or (letter.get("to") or {}).get("id") != thread or letter.get("message") != message:
        return None, "letter-not-identified"
    if letter.get("codex_headless_owner_interrupted") or codex_turn_aborted_since(thread, int(letter.get("sending_ns") or 0)):
        return None, "owner-interrupted"  # held letters are never resumed automatically
    deadline = time.monotonic() + CODEX_CLAIM_WAIT_S
    while True:
        got = _claims_mutate(lambda claims: _take_claim(claims, thread, letter_id))
        if got == "taken":
            break
        if got == "same-letter":
            return ("uncertain", {"reason": "codex-headless-claim-exists"}), "claim-exists"
        if time.monotonic() >= deadline:
            # Our own waiter still runs a turn in that chat: never queue and open the app over it. Nothing was sent.
            return ("refused", {"reason": "codex-wake-busy"}), "claim-busy"
        time.sleep(CODEX_HEADLESS_POLL_S)
    # The wake is decided here, so it is counted here. Over budget nothing is resumed: the claim is released and the
    # letter goes by `codex queue`, where it waits for the chat to be opened (the helper retries the link open).
    wake = wake_budget_claim(f"codex:{thread}", "codex-headless")
    if not wake["allowed"]:
        _claim_update(thread, letter_id, phase="refused", why=wake["reason"], released=True)
        return None, wake["reason"]
    try:
        pid = spawn_codex_headless(letter_id)
    except Exception as error:
        _claim_update(thread, letter_id, phase="spawn-failed", released=True)
        return None, "spawn-failed:" + type(error).__name__
    def outcome(claim: dict):
        """(result, why) once the waiter has said what happened, else None."""
        phase = claim.get("phase")
        if claim.get("turn_id") and phase in ("turn-started", "ended"):  # a short turn may already be over
            return ("accepted", {"codex_headless": True, "codex_turn_id": claim.get("turn_id"), "codex_headless_pid": pid,
                                 "codex_headless_model": claim.get("model"),
                                 "codex_headless_effort": claim.get("effort")}), phase
        if phase == "turn-sent-unconfirmed":
            return ("uncertain", {"reason": "codex-headless-turn-unconfirmed", "codex_headless": True}), phase
        if phase == "refused":
            return ("refused", {"reason": str(claim.get("why") or "codex-headless-refused")}), phase
        if phase == "fallback":
            why = str(claim.get("why") or "fallback")
            if why == "app-held":  # the app has the chat awake: nothing was woken, so the slot is given back
                wake_budget_refund(wake["event"])
            return None, why
        return False

    end = time.monotonic() + CODEX_HEADLESS_START_S
    while time.monotonic() < end:
        claim = _claims_load().get(thread) or {}
        if claim.get("letter_id") == letter_id:
            settled = outcome(claim)
            if settled is not False:
                return settled
        elif claim:
            break  # another letter owns the claim now
        time.sleep(CODEX_HEADLESS_POLL_S)

    def give_up(claims):
        claim = claims.get(thread)
        if claim and claim.get("letter_id") == letter_id and claim.get("phase") in ("spawning", "starting"):
            claim.update(phase="abandoned", released=True)  # the waiter checks this right before turn/start
            return {}
        return dict(claim) if claim and claim.get("letter_id") == letter_id else {"phase": "lost"}

    last = _claims_mutate(give_up)
    if not last:
        return None, "headless-start-timeout"
    settled = outcome(last)
    if settled is not False:
        return settled
    return ("uncertain", {"reason": "codex-headless-start-unconfirmed", "codex_headless": True}), "start-unconfirmed"


def codex_headless_wake(letter_id: str) -> dict:
    """The detached waiter for one letter to an asleep Codex chat. Starts a private app-server, resumes the chat,
    starts one turn with the letter as input, and keeps the connection until the turn ends, the deadline passes or
    the controls say stop. Sends nothing else. Every outcome ends in the claim's phase and journal notes."""
    letter = letters_load().get(letter_id) or {}
    thread = (letter.get("to") or {}).get("id")
    if (letter.get("to") or {}).get("app") != "codex" or not isinstance(thread, str) or not UUID_RE.fullmatch(thread):
        return {"ok": False, "wake": "skipped:not-a-codex-letter"}
    key = f"codex:{thread}"
    backlog: list = []
    tally: dict = {}
    server = None

    def claim_set(expect=None, **fields) -> bool:
        return _claim_update(thread, letter_id, expect, **fields)

    def stop(phase: str, **fields) -> dict:
        """End this wake: kill the server (and with it any turn), then publish the phase. The claim is released
        in the same write, so the sender and the next letter see the phase and a free chat together."""
        nonlocal server
        if server is not None:
            server.close()
            server = None
        claim_set(phase=phase, released=True, **fields)
        return {"ok": False, "wake": phase, **fields}

    if not claim_set(expect=("spawning",), phase="starting", pid=os.getpid()):
        return {"ok": False, "wake": "skipped:claim-not-ours"}
    try:
        refusal = controls_check(key)
        if refusal:
            return stop("refused", why=refusal)
        exe = codex_exe()
        if not exe:
            return stop("fallback", why="codex-cli-unavailable")
        settings = codex_thread_settings(thread)
        try:
            server = AppServer(exe, kill_on_close=True)
            if not getattr(server, "job_ok", True):
                return stop("fallback", why="job-object-unavailable")
            server.start(True, 30, CODEX_HEADLESS_CLIENT)
            resume = {"threadId": thread}
            if settings.get("model"):
                resume["model"] = settings["model"]
            if isinstance(settings.get("cwd"), str):
                resume["cwd"] = settings["cwd"]
            if settings.get("approval") in APPROVAL_POLICIES:
                resume["approvalPolicy"] = settings["approval"]
            if _sandbox_mode(settings.get("sandbox")):
                resume["sandbox"] = _sandbox_mode(settings["sandbox"])
            resumed = _headless_call(server, "thread/resume", resume, CODEX_HEADLESS_REQUEST_S, backlog, tally)
        except ProbeError as error:
            held = "writer" in str(error.detail).lower()
            return stop("fallback", why="app-held" if held else f"resume-failed:{error.reason}:{error.detail[:60]}")
        except (OSError, TimeoutError) as error:
            return stop("fallback", why="resume-failed:" + type(error).__name__)
        settings = _settings_with_resume(settings, resumed)
        missing = _settings_missing(settings)
        if missing:
            return stop("fallback", why="settings-unavailable:" + ",".join(missing))
        _headless_note(letter_id, codex_headless_resumed_ns=time.time_ns(), codex_headless_model=settings["model"],
                       codex_headless_effort=settings["effort"], codex_headless_sandbox=_sandbox_mode(settings["sandbox"]),
                       codex_headless_approval=settings["approval"])
        # The pre-send guard runs right before turn/start; the claim moves to turn-sending in the same breath, so a
        # sender that gave up (phase abandoned) stops us here and one that did not can no longer fall back.
        fresh = letters_load().get(letter_id) or {}
        late = pre_send_guard(letter_id, fresh.get("route") or "codex-queue", attempt_id=fresh.get("attempt_id"))
        if late:
            return stop("refused", why=late)
        if not claim_set(expect=("starting",), phase="turn-sending"):
            return stop("abandoned")
        params = {"threadId": thread, "input": [{"type": "text", "text": fresh.get("message")}], "model": settings["model"],
                  "effort": settings["effort"], "cwd": settings["cwd"], "approvalPolicy": settings["approval"],
                  "sandboxPolicy": _sandbox_policy(settings["sandbox"])}
        try:
            turn = _headless_call(server, "turn/start", params, CODEX_HEADLESS_REQUEST_S, backlog, tally)
        except ProbeError as error:
            if error.reason == "app-server-error":
                return stop("fallback", why="turn-start-refused:" + error.detail[:80])  # the server answered: no turn
            return stop("turn-sent-unconfirmed", why=error.reason)
        except (OSError, TimeoutError) as error:
            return stop("turn-sent-unconfirmed", why=type(error).__name__)
        turn_id = ((turn or {}).get("turn") or {}).get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            return stop("turn-sent-unconfirmed", why="turn-id-missing")
        claim_set(phase="turn-started", turn_id=turn_id, model=settings["model"], effort=settings["effort"])
        _headless_note(letter_id, codex_headless=True, codex_turn_id=turn_id, codex_headless_started_ns=time.time_ns())
        result = _headless_turn_loop(server, thread, turn_id, letter_id, key, backlog, tally, claim_set)
        server.close()
        server = None
        claim_set(phase="ended", released=True, status=result["status"])
        _headless_note(letter_id, codex_headless_end=result["status"], codex_headless_interrupted_by=result["interrupted_by"],
                       codex_headless_declined=tally.get("declined", 0), codex_headless_declined_methods=tally.get("methods"),
                       codex_headless_ended_ns=time.time_ns(),
                       **({"codex_headless_owner_interrupted": True} if result["owner_interrupted"] else {}))
        with contextlib.suppress(Exception):
            check_arrival(letter_id, timeout=CODEX_WAKE_READBACK_TIMEOUT_S)
        return {"ok": True, "wake": "ended", "letter_id": letter_id, **result, "declined": tally.get("declined", 0)}
    except Exception as error:
        # Closing the server kills the turn it ran; the claim's missing heartbeat tells the helper to clean up.
        if server is not None:
            server.close()
        _headless_note(letter_id, codex_headless_error=type(error).__name__)
        return {"ok": False, "wake": "error:" + type(error).__name__}


def _headless_turn_loop(server, thread: str, turn_id: str, letter_id: str, key: str, backlog: list, tally: dict,
                        claim_set) -> dict:
    started = time.monotonic()
    last_check = -CODEX_HEADLESS_CONTROLS_S
    interrupted_by, interrupt_at, status = None, 0.0, None
    pending = list(backlog)

    def handle(message) -> str | None:
        if "method" in message and "id" in message:
            _decline_server_request(server, message, tally)
            return None
        if message.get("method") == "turn/completed":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            done = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            if params.get("threadId") in (None, thread) and done.get("id") in (None, turn_id):
                return str(done.get("status") or "completed")
        return None

    while status is None:
        now = time.monotonic()
        if now - last_check >= CODEX_HEADLESS_CONTROLS_S:
            last_check = now
            claim_set()  # heartbeat
            if interrupted_by is None:
                reason = controls_check(key)
                if reason:
                    time.sleep(0.1)  # a read that collided with a controls write looks like "off": look twice
                    reason = controls_check(key)
                reason = reason or ("deadline" if now - started > CODEX_HEADLESS_DEADLINE_S else None)
                if reason:
                    interrupted_by, interrupt_at = reason, now
                    server.send_request("turn/interrupt", {"threadId": thread, "turnId": turn_id})
                    _headless_note(letter_id, codex_headless_interrupt=reason, codex_headless_interrupt_ns=time.time_ns())
            elif now - interrupt_at > CODEX_HEADLESS_INTERRUPT_WAIT_S:
                status = "interrupt-unanswered"
                break
        if pending:
            status = handle(pending.pop(0))
            continue
        try:
            message = server.lines.get(timeout=min(1.0, CODEX_HEADLESS_CONTROLS_S))
        except queue.Empty:
            continue
        if message is None:
            status = "server-exited"
        elif isinstance(message, dict):
            status = handle(message)
    # A turn stopped without our own interrupt is the owner's stop: it is never resumed or resent.
    return {"status": status, "interrupted_by": interrupted_by,
            "owner_interrupted": status == "interrupted" and interrupted_by is None}


def codex_headless_cleanup(letter_id: str) -> dict:
    """Bookkeeping for a claim whose waiter died: never turn/start, never a resend. If the owner stopped the turn,
    the chat is not touched. Otherwise a fresh resume that succeeds proves the dead turn is over (killing our server
    mid-turn leaves it with no end marker, Qualification deciding test 2), so the claim is released and the letter
    read back; a chat the app now holds is released the same way."""
    letter = letters_load().get(letter_id) or {}
    thread = (letter.get("to") or {}).get("id")
    if not isinstance(thread, str) or not UUID_RE.fullmatch(thread):
        return {"ok": False, "cleanup": "skipped:not-a-codex-letter"}
    taken: dict = {}

    def take(claims):
        claim = claims.get(thread)
        if not claim or claim.get("letter_id") != letter_id or claim.get("released") or _claim_live(claim):
            return False
        claim.update(phase="cleanup", beat_ns=time.time_ns(), cleanup_tries=int(claim.get("cleanup_tries") or 0) + 1)
        taken.update(claim)
        return True

    if not _claims_mutate(take):
        return {"ok": False, "cleanup": "skipped:no-stale-claim"}

    def release(note: str) -> dict:
        _claim_update(thread, letter_id, phase="ended", released=True, status=note)
        _headless_note(letter_id, codex_headless_end=note, codex_headless_ended_ns=time.time_ns())
        with contextlib.suppress(Exception):
            check_arrival(letter_id, timeout=CODEX_WAKE_READBACK_TIMEOUT_S)
        return {"ok": True, "cleanup": note}

    def retry(why: str) -> dict:
        if int(taken.get("cleanup_tries") or 1) >= CODEX_CLEANUP_MAX_TRIES:
            _claim_update(thread, letter_id, phase="ended", released=True, status="cleanup-gave-up")
            _headless_note(letter_id, codex_headless_end="cleanup-gave-up", codex_headless_error=why)
            return {"ok": False, "cleanup": "gave-up:" + why}
        _claim_update(thread, letter_id, phase="turn-started", beat_ns=0)  # stale again: the next tick retries
        return {"ok": False, "cleanup": "retry:" + why}

    if letter.get("codex_headless_owner_interrupted") or codex_turn_aborted_since(thread, int(taken.get("started_ns") or 0)):
        _headless_note(letter_id, codex_headless_owner_interrupted=True)
        return release("owner-interrupted")
    exe = codex_exe()
    if not exe:
        return retry("codex-cli-unavailable")
    server = None
    try:
        server = AppServer(exe, kill_on_close=True)
        if not getattr(server, "job_ok", True):
            return retry("job-object-unavailable")
        server.start(True, 30, CODEX_HEADLESS_CLIENT)
        _headless_call(server, "thread/resume", {"threadId": thread}, CODEX_HEADLESS_REQUEST_S, [], {})
        verdict = "no-end-marker-resume-ok"
    except ProbeError as error:
        if "writer" not in str(error.detail).lower():
            return retry(error.reason)
        verdict = "app-held"
    except (OSError, TimeoutError) as error:
        return retry(type(error).__name__)
    finally:
        if server is not None:
            server.close()
    return release(verdict)


def codex_claims_needing_cleanup(stale_s: float = 2 * CODEX_CLAIM_STALE_S) -> list[str]:
    """Letter IDs of claims whose waiter is gone (no heartbeat). A claim that never reached turn-sending is
    simply released (nothing was sent). The helper runs codex-headless --cleanup for the rest; this sends nothing."""
    now = time.time_ns()
    found, pre_send = [], []
    for thread, claim in _claims_load().items():
        if not isinstance(claim, dict) or claim.get("released") or _claim_live(claim, now, stale_s):
            continue
        (pre_send if claim.get("phase") in ("spawning", "starting", "abandoned") else found).append((thread, claim.get("letter_id")))
    if pre_send:
        def release(claims):
            for thread, letter_id in pre_send:
                if claims.get(thread, {}).get("letter_id") == letter_id:
                    claims[thread].update(released=True, phase="abandoned")
        _claims_mutate(release)
    return [letter_id for _thread, letter_id in found if isinstance(letter_id, str)]


# ---------------------------------------------------------------- reconcile
AGED_LETTER_S = MAIL_SLOT_TTL_S      # older than this, a held/uncertain letter has almost surely stopped moving
AGED_RECHECK_S = 6 * 3600.0          # ...so it is read back at most this often, leaving the budget to new letters


def _aged_recheck_later(letter: dict) -> bool:
    """True for an old held/uncertain letter that was read back recently: it waits, still never resent."""
    now = time.time_ns()
    created = int(letter.get("created_ns") or 0)
    checked = int(letter.get("readback_ns") or 0)
    return bool(created and checked and (now - created) / 1e9 > AGED_LETTER_S and
                (now - checked) / 1e9 < AGED_RECHECK_S)


def _mail_slot_expired_unsent(letter: dict) -> bool:
    """A letter still blocked past the mail slot's 24 hours was never sent and never will be: end it as
    expired-unsent. Only for a letter with no attempt in its history (an attempted one may have landed). Writes
    the journal row (a state change, not a send), so the helper's readback-only pass does it too."""
    created = int(letter.get("created_ns") or 0)
    if not created or (time.time_ns() - created) / 1e9 <= MAIL_SLOT_TTL_S or any(
            row.get("state") in ATTEMPTED_STATES for row in letter.get("history", [])):
        return False
    with locked("state"):
        current = letters_load().get(letter.get("letter_id")) or {}
        if not str(current.get("state")).startswith("blocked:") or current.get("state") == "blocked:other-account":
            return False
        _append_journal_locked(_letter_row(letter["letter_id"], "expired-unsent", sent=False,
                                           reason="mail-slot-24h-limit"))
    return True


def reconcile(resend: bool = True, grace_s: float = JOURNALED_GRACE_S, stale_s: float = STALE_SENDING_S) -> dict:
    """Idempotent pass. Readback promotes sending/uncertain rows; only a never-attempted
    (`journaled`) or cleanly blocked letter is ever sent, and only through the one-shot claim."""
    report = {"checked": 0, "readback_checked": 0, "readback_deferred": 0,
              "promoted": [], "resent": [], "waiting": [], "unchanged": []}
    relay_retire_inflight()  # upgrade from the relay design: once per old in-flight letter
    deadline = time.monotonic() + RECONCILE_READBACK_BUDGET_S
    # Least recently checked first; old unavailable rows cannot starve later held work.
    for letter in sorted(letters_load().values(), key=lambda row: row.get("readback_ns") or 0):
        state = letter.get("state")
        letter_id = letter.get("letter_id")
        if state in TERMINAL_STATES or "to" not in letter:
            continue
        report["checked"] += 1
        if state.startswith("blocked:") and _mail_slot_expired_unsent(letter):
            report["waiting"].append({"letter_id": letter_id, "state": "expired-unsent"})
            continue
        if state in ("accepted", "held", "uncertain") and _aged_recheck_later(letter):
            report["waiting"].append({"letter_id": letter_id, "state": state, "note": "aged-recheck-later"})
            continue
        if state in ("accepted", "held", "sending", "uncertain"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report["readback_deferred"] += 1
                report["waiting"].append({"letter_id": letter_id, "state": state,
                                           "note": "readback-budget-exhausted"})
                continue
            report["readback_checked"] += 1
            count, note = check_arrival(letter_id, timeout=min(remaining, READBACK_TIMEOUT_S))
            if count is not None and count >= 1:
                report["promoted"].append({"letter_id": letter_id, "state": "arrived", "arrivals": count})
            else:
                state = (letters_load().get(letter_id) or letter).get("state")
                sending_ns = letter.get("sending_ns") or next((row.get("ts_ns") for row in
                    letter.get("history", []) if row.get("state") == "sending"), letter.get("updated_ns"))
                age = (time.time_ns() - int(sending_ns or 0)) / 1e9
                if state == "sending" and age > stale_s:
                    # The helper died mid-attempt and readback cannot prove the letter never arrived
                    # (a queued message may land later). Never resend; show it as uncertain.
                    with locked("state"):
                        if (letters_load().get(letter_id) or {}).get("state") == "sending":
                            _append_journal_locked(_letter_row(letter_id, "uncertain", sent=None,
                                                               reason=("mail-slot-post-not-observed" if letter.get("route") == "mail-slot"
                                                                       else "helper-ended-mid-attempt"),
                                                               arrivals=count, readback_note=note))
                    state = "uncertain"
                report["unchanged"].append({"letter_id": letter_id, "state": state, "arrivals": count, "note": note})
        elif state == "journaled" or state.startswith("blocked:"):
            age = (time.time_ns() - int(letter.get("updated_ns") or 0)) / 1e9
            if not resend:
                report["waiting"].append({"letter_id": letter_id, "state": state})
            elif state == "journaled" and age < grace_s:
                report["waiting"].append({"letter_id": letter_id, "state": state, "note": "within grace window"})
            else:
                outcome = attempt_delivery(letter_id)
                (report["resent"] if outcome.get("state") in ("accepted", "held", "arrived", "uncertain", "refused")
                 else report["waiting"]).append({"letter_id": letter_id, "state": outcome.get("state"),
                                                 "reason": outcome.get("reason")})
    return report


def mark_stale(older_than_hours: float = 24.0, by: str = "owner") -> dict:
    """Retire accepted/held/uncertain letters older than the cutoff as `stale-unconfirmed`
    (issue 12). It only writes a journal row: nothing is ever resent, and a letter that
    readback already settled is left alone. `by` ("owner" for the CLI, "auto" for the helper's 24 h
    retirement) only names the actor in the reason."""
    cutoff = time.time_ns() - int(older_than_hours * 3600 * 1e9)
    marked = []
    with locked("state"):
        for letter_id, letter in letters_load().items():
            if letter.get("state") not in ("accepted", "held", "uncertain") or "to" not in letter:
                continue
            first = next((h.get("ts_ns") for h in letter.get("history", []) if h.get("ts_ns")), None)
            if (letter.get("created_ns") or first or 0) > cutoff:
                continue
            _append_journal_locked(_letter_row(letter_id, "stale-unconfirmed", sent=None,
                                               reason=f"{by}-retired-older-than-{older_than_hours:g}h:"
                                                      f"was-{letter.get('state')}"))
            marked.append(letter_id)
    return {"marked": marked, "older_than_hours": older_than_hours}


# ------------------------------------------------------- unconfirmed-letter notices (3B.15)
# A letter stuck accepted/held/uncertain used to be followed by nothing. Now the helper tells the letter's SENDER
# once, after NUDGE_AFTER_S, and once more when the letter is retired after 24 h. A notice is a NEW letter to the
# sender (never a resend of the original, never to the recipient). Its framing names Crosstalk itself as the
# sender ("Crosstalk message from Crosstalk (delivery notice)"): the same header every route already carries and
# every readback already counts, so it needs no new delivery path, and it goes through pre_send_guard (halt, off,
# deny, account recheck) like any letter. It is never a chat's voice: from = {app: crosstalk, id: helper}.
NUDGE_AFTER_S = 300.0
NUDGE_RETIRE_AFTER_H = 24.0
NUDGE_READBACK_TIMEOUT_S = RECONCILE_READBACK_BUDGET_S
NOTICE_APP = "crosstalk"
NOTICE_SENDER_ID = "helper"
NOTICE_NAME = "delivery notice"
NUDGE_STATES = ("accepted", "held", "uncertain")
NOTICE_FALLBACK_PREFIX = "letter-unconfirmed:"
APP_LABELS[NOTICE_APP] = "Crosstalk"


def is_notice_letter(letter: dict) -> bool:
    return (letter.get("from") or {}).get("app") == NOTICE_APP or bool(letter.get("notice_for"))


def _recipient_label(letter: dict) -> str:
    to = letter.get("to") or {}
    entry = directory_load()["entries"].get(f"{to.get('app')}:{to.get('id')}") or {}
    title = " ".join(entry["title"].split())[:60] if isinstance(entry.get("title"), str) else ""
    label = APP_LABELS.get(to.get("app"), str(to.get("app")))
    return f"{label} chat \"{title}\" ({to.get('id')})" if title else f"{label} chat {to.get('id')}"


def notice_text(letter: dict, kind: str) -> str:
    where = _recipient_label(letter)
    if kind == "retired":
        return (f"Your letter {letter.get('letter_id')} to {where} was never confirmed arrived and has been retired "
                "after 24 hours (state stale-unconfirmed). It was not resent. If it still matters, tell the owner "
                "and send a new letter.")
    return (f"Your letter {letter.get('letter_id')} to {where} is {letter.get('state')} and not confirmed yet. Do not "
            "resend it: it may still land (a busy chat takes it when its turn ends), or it will be retired as "
            "unconfirmed after 24 hours.")


def send_notice(letter: dict, kind: str) -> dict:
    """Send the sender of `letter` one notice as a new Crosstalk letter. Returns attempt_delivery's result."""
    sender = letter["from"]
    notice_id = "CTP0-" + uuid.uuid4().hex[:12]
    text = notice_text(letter, kind)
    message = frame_message(notice_id, NOTICE_APP, NOTICE_NAME, text)
    journal_append(_letter_row(notice_id, "journaled", to={"app": sender["app"], "id": sender["id"]}, message=message,
                               text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_chars=len(text),
                               created_ns=time.time_ns(), notice_for=letter.get("letter_id"), notice_kind=kind,
                               **{"from": {"app": NOTICE_APP, "id": NOTICE_SENDER_ID, "turn_id": None}}))
    return attempt_delivery(notice_id)


def _notice_sender_key(letter: dict) -> str | None:
    sender = letter.get("from") or {}
    if sender.get("app") in ("claude", "codex", "hermes") and isinstance(sender.get("id"), str) and sender["id"]:
        return f"{sender['app']}:{sender['id']}"
    return None


def _notice_wake_ok(key: str) -> bool:
    """A notice to an asleep sender wakes it, and a wake spends the wake budget: no room, no notice now."""
    app, native = key.split(":", 1)
    if app == "claude":
        registration = claude_registration(native)
        if registration is not None and registration["live"]:
            return True
    elif app == "hermes":
        return True
    return wake_budget_room(key)


def _claim_notice(letter_id: str, field: str, states: tuple[str, ...]) -> bool:
    """Under the state lock, record that this letter's notice is being sent. False when its state moved on or the
    notice was already claimed: that is what makes it exactly one notice per letter, even across helper restarts."""
    with locked("state"):
        current = letters_load().get(letter_id) or {}
        if current.get("state") not in states or current.get(field):
            return False
        _append_journal_locked(_letter_row(letter_id, current["state"], **{field: time.time_ns()}))
        return True


def _notice_or_fallback(letter: dict, kind: str) -> dict:
    result = send_notice(letter, kind)
    if result.get("state") not in ("accepted", "held", "arrived", "replied", "blocked:not-running"):
        # The sender's app could not take it (refused, other account, ...): keep it visible as a helper notice.
        with contextlib.suppress(Exception):
            helper_notice_set(NOTICE_FALLBACK_PREFIX + str(letter.get("letter_id")), "letter-unconfirmed",
                              notice_text(letter, kind))
    return result


def nudge_unconfirmed(now_ns: int | None = None, readback_timeout: float = NUDGE_READBACK_TIMEOUT_S) -> dict:
    """The helper's pass over letters that never arrived. At most ONE notice is sent per call (a send can take a
    while). Order: first retire what is older than 24 h (a journal row, like mark_stale), then send final notices
    for retired letters, then the one-time nudge for letters older than NUDGE_AFTER_S whose fresh readback still
    does not find them. Never resends or alters a letter; a notice is never about a notice; halt, messaging off,
    deny and the wake budget stop a notice (it is then tried again later, never counted as sent)."""
    now = time.time_ns() if now_ns is None else now_ns
    report: dict = {"retired": [], "nudged": [], "final": [], "skipped": []}
    report["retired"] = mark_stale(NUDGE_RETIRE_AFTER_H, by="auto")["marked"]
    letters = letters_load()
    for key in list(helper_notices_load()["notices"]):  # fallback notices end with their letter's pending state
        if key.startswith(NOTICE_FALLBACK_PREFIX) and (letters.get(key[len(NOTICE_FALLBACK_PREFIX):]) or {}).get(
                "state") in (None, "arrived", "replied", "refused", "cancelled"):
            helper_notice_clear(key)
    blocked = controls_check(None)
    if blocked:
        report["blocked"] = blocked
        return report
    ordered = sorted(letters.values(), key=lambda row: row.get("created_ns") or 0)
    for kind, field, states in (("retired", "final_notice_ns", ("stale-unconfirmed",)),
                                ("nudge", "nudged_ns", NUDGE_STATES)):
        for letter in ordered:
            letter_id, state = letter.get("letter_id"), letter.get("state")
            if state not in states or "to" not in letter or is_notice_letter(letter) or letter.get(field):
                continue
            if kind == "retired":
                if not str(letter.get("reason") or "").startswith("auto-retired"):
                    continue  # an owner-retired letter (CLI mark-stale) gets no notice
            else:
                started = int(letter.get("sending_ns") or letter.get("created_ns") or 0)
                if not started or (now - started) / 1e9 <= NUDGE_AFTER_S:
                    continue
            sender_key = _notice_sender_key(letter)
            if sender_key is None:
                report["skipped"].append({"letter_id": letter_id, "why": "no-sender-chat"})
                continue
            if controls_check(sender_key):
                report["skipped"].append({"letter_id": letter_id, "why": controls_check(sender_key)})
                continue
            if not _notice_wake_ok(sender_key):
                report["skipped"].append({"letter_id": letter_id, "why": "wake-budget"})
                continue
            if kind == "nudge":
                count, _note = check_arrival(letter_id, readback_timeout)  # one more look before bothering the sender
                if count is not None and count >= 1:
                    continue
                if (letters_load().get(letter_id) or {}).get("state") not in states:
                    continue
            if not _claim_notice(letter_id, field, states):
                continue
            result = _notice_or_fallback(letters_load().get(letter_id) or letter, kind)
            report["nudged" if kind == "nudge" else "final"].append(
                {"letter_id": letter_id, "notice_state": result.get("state"), "to": sender_key})
            return report
    return report


def letter_summary(limit: int = 10) -> dict:
    letters = letters_load()
    counts: dict = {}
    for letter in letters.values():
        counts[letter.get("state")] = counts.get(letter.get("state"), 0) + 1
    recent = sorted(letters.values(), key=lambda item: item.get("updated_ns") or 0, reverse=True)[:limit]
    pending = [l for l in letters.values() if l.get("state") in ("accepted", "held", "sending", "uncertain")]
    return {"counts": counts, "pending_count": len(pending), "pending_truncated": len(pending) > 50,
            "pending": [{"letter_id": l["letter_id"], "state": l.get("state"),
                         "reason": l.get("reason"), "arrivals": l.get("arrivals"),
                         "readback_note": l.get("readback_note"), "readback_ns": l.get("readback_ns")}
                        for l in pending[:50]],
            "recent": [{"letter_id": l["letter_id"], "state": l.get("state"),
                                          "to": l.get("to"), "reason": l.get("reason"),
                                          "arrivals": l.get("arrivals"), "readback_note": l.get("readback_note"),
                                          "guidance": GUIDANCE.get(l.get("state"))} for l in recent]}


def list_directory(self_key: str | None = None) -> list[dict]:
    hermes_discover()  # best effort and read-only; unreadable state.db changes nothing
    current_claude = current_account()["id"]
    codex_now = None
    rows = []
    for key, entry in sorted(directory_load()["entries"].items()):
        item = {"key": key, "app": entry.get("app"), "title": entry.get("title"),
                "project_folder": entry.get("project_folder"), "cwd_kind": (entry.get("project") or {}).get("cwd_kind"),
                "account_id": (entry.get("account") or {}).get("id"), "last_seen_ns": entry.get("last_seen_ns"),
                "self": key == self_key}
        if entry.get("app") == "codex" and codex_now is None:
            codex_now = codex_current()
        hint = block_reason(entry, codex_now) if entry.get("app") in ("claude", "codex", "hermes") else None
        if hint == "blocked:not-running" and wake_target(entry):
            hint = "sendable:wakes-chat"  # the helper's letter wake starts it (runs a turn there)
        item["delivery_hint"] = hint or "sendable"
        rows.append(item)
    return rows[:200] if current_claude is not None or rows else rows


# ---------------------------------------------- chat-facing compact list and create
# What a chat's list tool returns: compact, most recently seen first, capped, so it stays far below the plugin
# helper's 32 KB output limit however large the directory grows. The full `list` command keeps every field.
LIST_DEFAULT_LIMIT = 40
LIST_MAX_LIMIT = 100
LIST_TITLE_MAX = 60
LIST_FOLDER_MAX = 40
LIST_QUERY_MAX = 80


def compact_list(entries, self_key=None, query=None, app=None, limit=None) -> dict:
    """Compact chat-facing view of list_directory rows. Titles are display-only (never a way to pick a chat)."""
    if app is not None and app not in ("claude", "codex", "hermes"):
        raise ProbeError("invalid-app", "claude, codex or hermes")
    if query is not None and not isinstance(query, str):
        raise ProbeError("invalid-query")
    if limit is None:
        limit = LIST_DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ProbeError("invalid-limit", f"1 to {LIST_MAX_LIMIT}")
    limit = min(limit, LIST_MAX_LIMIT)
    needle = " ".join(query.split())[:LIST_QUERY_MAX].casefold() if query else ""
    rows = []
    for item in entries:
        folder = item.get("project_folder")
        folder_name = Path(folder).name if isinstance(folder, str) and folder else None
        title = " ".join(str(item.get("title") or "").split())
        if app and item.get("app") != app:
            continue
        key = str(item.get("key"))
        if needle and needle not in (title + "\n" + (folder_name or "") + "\n" + key).casefold():
            continue
        row = {"key": key, "app": item.get("app")}
        if title:
            row["title"] = title if len(title) <= LIST_TITLE_MAX else title[:LIST_TITLE_MAX - 1] + "…"
        if folder_name:
            row["folder"] = folder_name[:LIST_FOLDER_MAX]
        row["hint"] = item.get("delivery_hint")
        if item.get("self") or key == self_key:
            row["self"] = True
        rows.append((item.get("last_seen_ns") or 0, row))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    shown = [row for _, row in rows[:limit]]
    return {"total": len(rows), "shown": len(shown), "more": len(rows) - len(shown), "entries": shown}


def chat_list_view(self_key, query=None, app=None, limit=None) -> dict:
    """The value of every chat-facing list tool (Claude, Codex, Hermes)."""
    view = compact_list(list_directory(self_key), self_key, query, app, limit)
    view["ok"] = True
    view["self"] = self_key
    view["controls"] = controls_load()
    view["note"] = ("Select chats by the exact key only; titles are display-only. "
                    "Narrow with query/app; 'more' counts hidden rows.")
    return view


CHAT_CREATE_APPS = ("claude", "codex", "hermes")
CHAT_CREATE_TITLE_MAX = 80
CHAT_CREATE_PROMPT_MAX = 20000
CHAT_CREATE_FIELDS = ("app", "folder", "model", "effort", "title", "prompt", "provider")


CREATE_REFUSED_BEFORE_START = ("unknown-model",)
CHAT_ORIGIN_KEY_RE =re.compile(r"(claude|codex|hermes):[A-Za-z0-9._-]{1,80}\Z")


def chat_origin_line(caller_key) -> str:
    """The short line appended to a created chat's first prompt so it knows who opened it, or "" when the
    caller's directory key is unknown or malformed. (The Hermes plugin carries a copy of this wording.)"""
    if not isinstance(caller_key, str) or not CHAT_ORIGIN_KEY_RE.fullmatch(caller_key):
        return ""
    app = caller_key.split(":", 1)[0].capitalize()
    return (f"\n\n(Crosstalk: this chat was opened by {app} chat {caller_key}. "
            "To reply to it, send a Crosstalk letter to that key.)")


def chat_create_argv(app, params, prompt_file, wait_seconds=None) -> list[str]:
    """CLI arguments (after the script path) for one chat-facing create call. The prompt is only ever a file."""
    if app not in CHAT_CREATE_APPS:
        raise ProbeError("invalid-app", "claude, codex or hermes")
    argv = ["create-" + app]
    if app in ("claude", "codex"):
        argv += ["--folder", str(params.get("folder")), "--model", str(params.get("model")),
                 "--effort", str(params.get("effort"))]
    else:
        if params.get("model"):
            argv += ["--model", str(params["model"])]
        if params.get("provider"):
            argv += ["--provider", str(params["provider"])]
        if params.get("effort"):
            argv += ["--effort", str(params["effort"])]
        if wait_seconds is not None:
            argv += ["--wait-seconds", str(wait_seconds)]
    if params.get("title"):
        argv += ["--title", str(params["title"])[:CHAT_CREATE_TITLE_MAX]]
    argv += ["--prompt-file", str(prompt_file)]
    return argv


def chat_create_refusal(app, params) -> str | None:
    """A short reason when the arguments cannot make a chat (nothing is started); validation proper is the CLI's."""
    if app not in CHAT_CREATE_APPS:
        return "invalid-app"
    extra = sorted(set(params) - set(CHAT_CREATE_FIELDS))
    if extra:
        return "unexpected-fields:" + clip_reason(",".join(extra))
    for name in ("folder", "model", "effort", "title", "provider"):
        if params.get(name) is not None and not isinstance(params.get(name), str):
            return "invalid-" + name
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt) > CHAT_CREATE_PROMPT_MAX:
        return "prompt-invalid"
    if app in ("claude", "codex"):
        if not params.get("model"):
            return "model-required"
        if not params.get("effort"):
            return "effort-required"
        if params.get("provider"):
            return "provider-only-for-hermes"
    return None


def caller_project_folder(key) -> str | None:
    """The calling chat's own registered project folder, the default for a new chat's folder."""
    entry = directory_load()["entries"].get(key) if key else None
    folder = (entry or {}).get("project_folder")
    return folder if isinstance(folder, str) and Path(folder).is_absolute() and Path(folder).is_dir() else None


def chat_create(app, params, caller_key=None, wait_s=50.0) -> dict:
    """Run the create-<app> CLI command once, prompt through a UTF-8 temp file, and wait up to wait_s for it.

    Never retried. If the command is still running after wait_s it is left running (detached) and the result is
    state "started", not a failure: a Claude create waits for the new chat's first turn and can take minutes.
    A finished command's own JSON is returned (key, reason, ...); an unreadable result is "uncertain"."""
    refusal = chat_create_refusal(app, params)
    if refusal:
        return {"ok": False, "state": "refused", "created": False, "reason": refusal}
    params = dict(params)
    origin = chat_origin_line(caller_key)
    if origin:
        if len(params["prompt"]) + len(origin) > CHAT_CREATE_PROMPT_MAX:
            return {"ok": False, "state": "refused", "created": False, "reason": "prompt-too-long-for-origin-line",
                    "detail": f"keep the prompt under {CHAT_CREATE_PROMPT_MAX - len(origin)} characters"}
        params["prompt"] += origin
    if app in ("claude", "codex") and not params.get("folder"):
        params["folder"] = caller_project_folder(caller_key)
        if not params["folder"]:
            return {"ok": False, "state": "refused", "created": False, "reason": "folder-required",
                    "detail": "give an absolute existing folder; this chat's own folder is not registered"}
    prompt_handle, prompt_path = tempfile.mkstemp(prefix="crosstalk-create-", suffix=".txt")
    out_handle, out_path = tempfile.mkstemp(prefix="crosstalk-create-out-", suffix=".json")
    try:
        with os.fdopen(prompt_handle, "wb") as stream:
            stream.write(params["prompt"].encode("utf-8"))
        # A console python.exe with no window (pythonw has no stdout to write the result to), outside any host
        # job that would kill a still-running create when this process exits.
        argv = [sys.executable, "-I", "-B", str(KIT / "crosstalk_p0_cli.py")] + chat_create_argv(
            app, params, prompt_path, wait_seconds=30 if app == "hermes" else None)
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW) if os.name == "nt" else 0
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0) if os.name == "nt" else 0
        try:
            with os.fdopen(out_handle, "wb") as out_stream:
                out_handle = None
                options = dict(stdin=subprocess.DEVNULL, stdout=out_stream, stderr=subprocess.DEVNULL,
                               cwd=tempfile.gettempdir(), env=child_env()[0])
                try:
                    proc = subprocess.Popen(argv, creationflags=flags | breakaway, **options)
                except PermissionError:
                    proc = subprocess.Popen(argv, creationflags=flags, **options)
        except OSError as error:
            return {"ok": False, "state": "refused", "created": False, "reason": "create-not-launched",
                    "detail": type(error).__name__}
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            out_path = prompt_path = None  # the running command may still read/write them; leave both
            return {"ok": True, "state": "started", "created": None, "pending": True, "app": app,
                    "guidance": "The new chat is still being created and was NOT retried. Do not call create "
                                "again. Check later with list (query by title or folder, newest first) or status."}
        try:
            lines = [line for line in Path(out_path).read_text(encoding="utf-8", errors="replace").splitlines()
                     if line.strip().startswith("{")]
            result = json.loads(lines[-1])
        except (OSError, ValueError, IndexError):
            result = None
        if not isinstance(result, dict):
            return {"ok": False, "state": "uncertain", "created": None, "reason": "create-output-unreadable",
                    "guidance": "A chat may exist. Do not create another; check list and tell the owner."}
        keep = {name: result.get(name) for name in ("ok", "key", "reason", "detail", "thread_id", "operation_id",
                                                    "stored_session_id", "requested", "turn_status", "title",
                                                    "folder") if name in result}
        reason = str(result.get("reason") or "")
        maybe = any(result.get(n) for n in ("thread_id", "operation_id", "stored_session_id")) or \
            reason.endswith("uncertain") or reason in ("create-startup-unconfirmed", "create-run-failed",
                                                       "create-output-not-json")
        if reason in CREATE_REFUSED_BEFORE_START and not result.get("thread_id"):
            maybe = False  # refused before thread/start: no chat can exist
        keep["state"] = "created" if result.get("ok") is True else ("uncertain" if maybe else "refused")
        keep["created"] = {"created": True, "uncertain": None, "refused": False}[keep["state"]]
        if keep["state"] == "uncertain":
            keep["guidance"] = ("A chat may exist. Do NOT create another; check list and tell the owner. For Codex, "
                                "create-codex-status with the operation_id reads the receipt.")
        return keep
    finally:
        if out_handle is not None:
            os.close(out_handle)
        for path in (prompt_path, out_path):
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)


# ------------------------------------------------------------ helper support
# The Crosstalk helper (crosstalk_helper.py) watches Claude Desktop restarts and keeps what it can safely fix.
# Its data and every exactly-once decision live here, under the same state lock as letters:
#   helper/loads.json    log offset + one entry per Desktop load: the restart relink list (never letters)
#   helper/notices.json  open notices (kind, message, since); the plugin shows them at session start
#   helper/state.json    the helper's heartbeat and its letter-wake attempts
#   helper/relink-open.json   exists while relink entries wait: the plugin polls it (no process per poll)
#   helper/self_relink.json   one-shot ledger of the self relink after a letter wake
# A relink entry is NOT a letter: it is never journaled and never has a letter state. After a Desktop restart the
# helper lists the chats whose Remote Control was on; ONE running chat's plugin hook claims the list and, per chat,
# sends the fixed RECONNECT_TEXT through Desktop's own send_message (no model turn): Desktop starts the stopped chat
# and its Remote Control comes back by itself. A guard hook in the plugin denies every other use of these tools.
RELINK_TTL_S = 1800.0            # a queued relink entry nobody claimed in this long expires, never retried
RELINK_CLAIM_STALE_S = 300.0     # a claimed entry the plugin never reported on has failed
RELINK_CLAIM_MAX = 20            # entries handed to one claimant at once
RELINK_SELF_WINDOW_S = 900.0     # a letter wake this recent makes a chat's first turn the moment to relink itself
RELINK_SKIP_DESKTOP_ENABLED = "desktop-enabled"  # the entry's reason when Desktop turned that chat's Remote Control on itself
HELPER_STALE_S = 120.0           # a helper heartbeat older than this means the helper is not running
LOADS_KEPT = 20
SET_REMOTE_CONTROL_TOOL = "mcp__ccd_session_mgmt__set_remote_control"
SEND_MESSAGE_TOOL = "mcp__ccd_session_mgmt__send_message"
RECONNECT_TEXT = ("Crosstalk reconnect: Claude Desktop restarted or switched accounts, so this one message brings "
                  "this chat's Remote Control link back. No action needed: answer ok here and do not message any chat back.")


def helper_dir() -> Path:
    return device_dir() / "helper"


def helper_loads_load() -> dict:
    value = _read_json(helper_dir() / "loads.json", {})
    value = value if isinstance(value, dict) else {}
    if not isinstance(value.get("loads"), dict):
        value["loads"] = {}
    if not isinstance(value.get("log"), dict):
        value["log"] = {}
    value["version"] = 1
    return value


def helper_loads_update(mutator):
    """Run mutator(data) under the state lock and persist the ledger when it changed. The mutator must not
    take a lock. Returns the mutator's result."""
    with locked("state"):
        data = helper_loads_load()
        before = json.dumps(data, sort_keys=True)
        result = mutator(data)
        if len(data["loads"]) > LOADS_KEPT:
            ordered = sorted(data["loads"].items(), key=lambda item: item[1].get("seen_ns") or 0)
            for load_id, _ in ordered[:len(data["loads"]) - LOADS_KEPT]:
                del data["loads"][load_id]
        if json.dumps(data, sort_keys=True) != before:
            _write_json_atomic(helper_dir() / "loads.json", data)
        return result


def relink_flag_path() -> Path:
    return helper_dir() / "relink-open.json"


def relink_owner_state(session_id) -> str:
    """The owner's stored Remote Control choice for this chat from Desktop's own record: "off" (the owner turned it
    off: never turn it on), "ok" (on, or never decided) or "unknown" (the record cannot be read). Read-only."""
    found = desktop_record(current_account(), session_id) if isinstance(session_id, str) else None
    if not found:
        return "unknown"
    return "off" if found[1].get("remoteControlUserEnabled") is False else "ok"


def relink_enqueue(load_id: str, desktop_id: str, session_id: str, title: str | None = None) -> dict:
    """List ONE chat for the restart relink of one load. Never a second entry for the same chat and load, and never
    against the controls (halt, off, or a denied chat) or a stored owner "off". Returns {queued, why}."""
    if not LOCAL_ID_RE.fullmatch(desktop_id or "") or not UUID_RE.fullmatch(session_id or ""):
        return {"queued": False, "why": "invalid-id"}
    with locked("state"):
        refusal = desktop_check(f"claude:{session_id}") or ("relink-off" if relink_switched_off() else None)
        if refusal:
            return {"queued": False, "why": refusal}
        if relink_owner_state(session_id) == "off":
            return {"queued": False, "why": "owner-off"}
        data = helper_loads_load()
        load = data["loads"].get(load_id)
        if not load:
            return {"queued": False, "why": "unknown-load"}
        if desktop_id in load.setdefault("reconnects", {}):
            return {"queued": False, "why": "already-handled-for-this-load"}
        load["reconnects"][desktop_id] = {"state": "queued", "attempt_id": uuid.uuid4().hex[:12],
                                          "session": session_id, "title": title, "queued_ns": time.time_ns()}
        _write_json_atomic(helper_dir() / "loads.json", data)
        return {"queued": True}


def _relink_entry(data: dict, desktop_id):
    """(load, entry) for a chat's newest relink entry, or None."""
    for load in sorted(data["loads"].values(), key=lambda item: item.get("seen_ns") or 0, reverse=True):
        entry = (load.get("reconnects") or {}).get(desktop_id)
        if isinstance(entry, dict):
            return load, entry
    return None


def _relink_flag_sync(data: dict) -> None:
    queued = any(e.get("state") == "queued" for load in data["loads"].values()
                 for e in (load.get("reconnects") or {}).values())
    if not queued:
        with contextlib.suppress(OSError):
            relink_flag_path().unlink()


def relink_claim(session_id) -> dict:
    """A running Desktop chat's plugin claims the restart relink list: every queued entry of the newest load goes to
    it at once (at most RELINK_CLAIM_MAX), each marked `claimed` under the state lock so nobody else gets it. The
    claimant is never on its own list. Returns {jobs: [{to, session, attempt_id, text}]}; sends nothing itself."""
    if not desktop_hosted_session(session_id):
        return {"ok": True, "jobs": [], "reason": "not-a-desktop-chat"}
    refusal = desktop_check(None) or ("relink-off" if relink_switched_off() else None)
    if refusal:
        return {"ok": True, "jobs": [], "reason": refusal}

    def mutate(data):
        now = time.time_ns()
        newest = max(data["loads"], key=lambda key: data["loads"][key].get("seen_ns") or 0, default=None)
        jobs = []
        enabled: set | None = None  # chats Desktop turned Remote Control on after the load; read once, only when needed
        for load_id, load in data["loads"].items():
            for desktop_id, entry in (load.get("reconnects") or {}).items():
                if entry.get("state") != "queued":
                    continue
                denied = desktop_check(f"claude:{entry.get('session')}")
                if load_id != newest:
                    entry.update(state="expired", done_ns=now, reason="superseded-by-newer-load")
                elif (now - int(entry.get("queued_ns") or 0)) / 1e9 > RELINK_TTL_S:
                    entry.update(state="expired", done_ns=now, reason="relink-expired")
                elif denied:
                    entry.update(state="refused", done_ns=now, reason=denied)
                elif entry.get("session") == session_id or len(jobs) >= RELINK_CLAIM_MAX:
                    continue  # a chat never relinks itself; another claimant (or a later call) takes it
                elif relink_owner_state(entry.get("session")) != "ok":
                    entry.update(state="skipped", done_ns=now, reason="owner-off-or-unreadable")
                elif desktop_id in (enabled if enabled is not None else (enabled := remote_enabled_since_load(load_id))):
                    # The list is made as soon as Desktop has loaded, before anyone has typed. A chat the owner opened
                    # since then got its Remote Control from Desktop itself (its own "Enabling remote control" line):
                    # it needs no reconnect text, and is never sent a second relink.
                    entry.update(state="skipped", done_ns=now, reason=RELINK_SKIP_DESKTOP_ENABLED)
                else:
                    entry.update(state="claimed", claimed_ns=now, claim_session=session_id)
                    jobs.append({"to": desktop_id, "session": entry.get("session"), "attempt_id": entry["attempt_id"],
                                 "text": RECONNECT_TEXT})
        _relink_flag_sync(data)
        return {"ok": True, "jobs": jobs}

    return helper_loads_update(mutate)


def relink_done(session_id, desktop_id, attempt_id, delivered, reason=None) -> dict:
    """The plugin's report for one claimed entry. Recorded once, never retried, no readback and no journal row."""
    note = str(reason or "")[:1000]

    def mutate(data):
        found = _relink_entry(data, desktop_id)
        entry = found[1] if found else None
        if not entry or entry.get("state") != "claimed" or entry.get("claim_session") != session_id \
                or entry.get("attempt_id") != attempt_id:
            return {"ok": False, "reason": "no-matching-claim"}
        entry.update(state="done" if delivered is True else "failed", done_ns=time.time_ns(),
                     delivered=delivered if isinstance(delivered, bool) else None,
                     reason=None if delivered is True else (note or "not-delivered"))
        return {"ok": True, "state": entry["state"]}

    return helper_loads_update(mutate)


def relink_sweep(halt_reason: str | None = None) -> dict:
    """Helper tick: expire stale queued entries, fail claims never reported on and, when controls block, refuse every
    queued entry (halt stops the relink). Sends nothing."""
    def mutate(data):
        now = time.time_ns()
        changed = {"expired": 0, "refused": 0, "failed": 0}
        for load in data["loads"].values():
            for entry in (load.get("reconnects") or {}).values():
                state = reason = None
                if entry.get("state") == "queued":
                    if halt_reason:
                        state, reason = "refused", halt_reason
                    elif (now - int(entry.get("queued_ns") or 0)) / 1e9 > RELINK_TTL_S:
                        state, reason = "expired", "relink-expired"
                elif entry.get("state") == "claimed" and \
                        (now - int(entry.get("claimed_ns") or 0)) / 1e9 > RELINK_CLAIM_STALE_S:
                    state, reason = "failed", "claim-never-reported"
                if state:
                    entry.update(state=state, done_ns=now, reason=reason)
                    changed[state] += 1
        _relink_flag_sync(data)
        return changed

    return helper_loads_update(mutate)


def relink_guard(session_id, tool, args) -> dict:
    """The plugin's guard hook asks before any call of the two Desktop tools Crosstalk's allow rules cover (it sees the
    model's own calls as well as the plugin's). Returns {allow, reason}.
    set_remote_control: only {session_id: "self", enabled: true}, and never for a chat whose stored choice is off.
    send_message: only the fixed RECONNECT_TEXT to a chat this session claimed on the relink list."""
    args = args if isinstance(args, dict) else {}
    if tool == SET_REMOTE_CONTROL_TOOL:
        if set(args) - {"session_id", "enabled"} or args.get("session_id") != "self" or args.get("enabled") is not True:
            return {"ok": True, "allow": False, "reason": "Crosstalk guard: only enabling Remote Control for this chat "
                                                          "itself (session_id \"self\") is allowed."}
        if relink_owner_state(session_id) == "off":
            return {"ok": True, "allow": False, "reason": "Crosstalk guard: the owner turned Remote Control off for "
                                                          "this chat."}
        return {"ok": True, "allow": True}
    if tool == SEND_MESSAGE_TOOL:
        target = args.get("session_id")
        found = _relink_entry(helper_loads_load(), target) if isinstance(target, str) else None
        entry = found[1] if found else None
        if (entry and entry.get("state") == "claimed" and entry.get("claim_session") == session_id
                and args.get("message") == RECONNECT_TEXT and set(args) <= {"session_id", "message"}):
            return {"ok": True, "allow": True}
        return {"ok": True, "allow": False, "reason": "Crosstalk guard: send_message is allowed only for the restart "
                                                      "relink (fixed text, chat on the helper's relink list)."}
    return {"ok": True, "allow": True}


def _self_relink_ledger() -> dict:
    value = _read_json(helper_dir() / "self_relink.json", {})
    return value if isinstance(value, dict) else {}


def self_relink_ask(session_id) -> dict:
    """After a letter wake the woken chat's plugin asks, at the end of that chat's first turn, whether it may switch
    its own Remote Control on (the tool refuses a chat that has not run a turn). Yes only for a chat the helper's
    letter wake started within RELINK_SELF_WINDOW_S, whose controls allow it, whose stored choice is not off, once
    per wake (recorded before anything is called, so a denial is never retried)."""
    if not desktop_hosted_session(session_id):
        return {"ok": True, "relink": False, "reason": "not-a-desktop-chat"}
    refusal = desktop_check(f"claude:{session_id}") or ("relink-off" if relink_switched_off() else None)
    if refusal:
        return {"ok": True, "relink": False, "reason": refusal}
    now = time.time_ns()
    wakes = [int(row.get("ns") or 0) for row in (helper_state_load().get("letter_wakes") or {}).values()
             if isinstance(row, dict) and row.get("session") == session_id]
    woken = max(wakes, default=0)
    if not woken or (now - woken) / 1e9 > RELINK_SELF_WINDOW_S:
        return {"ok": True, "relink": False, "reason": "not-letter-woken"}
    owner = relink_owner_state(session_id)
    if owner != "ok":
        return {"ok": True, "relink": False, "reason": "owner-" + owner}
    with locked("state"):
        ledger = _self_relink_ledger()
        if int((ledger.get(session_id) or {}).get("wake_ns") or 0) >= woken:
            return {"ok": True, "relink": False, "reason": "already-handled-for-this-wake"}
        ledger[session_id] = {"wake_ns": woken, "asked_ns": now, "attempt_id": uuid.uuid4().hex[:12]}
        for old in sorted(ledger, key=lambda key: ledger[key].get("asked_ns") or 0)[:-50]:
            del ledger[old]
        _write_json_atomic(helper_dir() / "self_relink.json", ledger)
        return {"ok": True, "relink": True, "attempt_id": ledger[session_id]["attempt_id"]}


def self_relink_done(session_id, attempt_id, outcome, reason=None) -> dict:
    """The plugin's note of how its one self relink went (on, denied by the permission check, failed). Logged only."""
    with locked("state"):
        ledger = _self_relink_ledger()
        row = ledger.get(session_id)
        if not isinstance(row, dict) or row.get("attempt_id") != attempt_id:
            return {"ok": False, "reason": "no-matching-ask"}
        row.update(outcome=str(outcome)[:40], reason=str(reason or "")[:300], done_ns=time.time_ns())
        _write_json_atomic(helper_dir() / "self_relink.json", ledger)
    return {"ok": True}


TERMINAL_PHASES = ("done", "superseded")


def load_phase_terminal(phase) -> bool:
    return phase in TERMINAL_PHASES or str(phase).startswith(("skipped", "expired", "failed"))


# --- notices and the helper's heartbeat
def helper_notices_load() -> dict:
    value = _read_json(helper_dir() / "notices.json", {})
    if not isinstance(value, dict) or not isinstance(value.get("notices"), dict):
        return {"version": 1, "notices": {}}
    return value


def helper_notice_set(key: str, kind: str, message: str) -> None:
    """Open or update a notice; `since` stays the first time it was set. Helper process only."""
    data = helper_notices_load()
    now = time.time_ns()
    old = data["notices"].get(key) or {}
    notice = {"kind": kind, "message": message[:400], "since_ns": old.get("since_ns") or now}
    if old.get("kind") == kind and old.get("message") == notice["message"]:
        return
    data["notices"][key] = notice
    _write_json_atomic(helper_dir() / "notices.json", data)


def helper_notice_clear(key: str) -> None:
    data = helper_notices_load()
    if key in data["notices"]:
        del data["notices"][key]
        _write_json_atomic(helper_dir() / "notices.json", data)


def helper_state_load() -> dict:
    value = _read_json(helper_dir() / "state.json", {})
    return value if isinstance(value, dict) else {}


def helper_alive(state: dict | None = None) -> bool:
    state = helper_state_load() if state is None else state
    stamp = state.get("ts_ms")
    return bool(isinstance(stamp, (int, float)) and time.time() * 1000 - stamp <= HELPER_STALE_S * 1000
                and pid_alive(state.get("pid")))


def helper_notices_active() -> list[dict]:
    """Open notices, oldest first, plus a virtual one when a helper that ran before has stopped."""
    state = helper_state_load()
    rows = [{"key": key, **value} for key, value in helper_notices_load()["notices"].items()]
    if state and not helper_alive(state):
        rows.append({"key": "helper-not-running", "kind": "helper-not-running", "since_ns": None,
                     "message": "The Crosstalk helper is not running, so restarts, held letters and asleep chats "
                                "are not being watched. Start it (Startup shortcut) or run its --watch mode."})
    return sorted(rows, key=lambda row: row.get("since_ns") or 0)


def helper_summary() -> dict:
    """Small, JSON-safe view for status and claude-status."""
    state = helper_state_load()
    loads = sorted(helper_loads_load()["loads"].items(), key=lambda item: item[1].get("seen_ns") or 0, reverse=True)
    return {"alive": helper_alive(state), "ever_ran": bool(state), "pid": state.get("pid"),
            "ts_ms": state.get("ts_ms"), "version": state.get("version"), "last_tick": state.get("last_tick"),
            "notices": helper_notices_active()[:10],
            "loads": [{"load_id": load_id, "account": load.get("account"), "phase": load.get("phase"),
                       "reconnects": {name: sum(1 for e in (load.get("reconnects") or {}).values()
                                                if e.get("state") == name)
                                      for name in {e.get("state") for e in (load.get("reconnects") or {}).values()}}}
                      for load_id, load in loads[:3]]}


# ------------------------------------------------------------------ focus gate
# Every focus-taking wake goes through crosstalk_focus.focus_wake (owner rule, README "Focus rule"). The module is
# loaded by path and has no dependency on this one: the lock, the controls check and the log path are passed in.
FOCUS_SLEEP = time.sleep           # tests replace this so a confirm timeout does not really wait
FOCUS_LOCK_NAME = "focus"
FOCUS_LOCK_TIMEOUT_S = 60.0


def _focus_module():
    module = sys.modules.get("crosstalk_focus")
    if module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("crosstalk_focus", KIT / "crosstalk_focus.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["crosstalk_focus"] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop("crosstalk_focus", None)
            raise
    return module


def enable_real_focus() -> None:
    """Opt this process in to real focus handling (overlay, restore). Only real wake entry points call this."""
    _focus_module().enable_real_focus(True)


def focus_log_path() -> Path:
    return device_dir() / "focus" / "log.jsonl"


def focus_wake(target_key: str, action, confirm, *, text: str, kind: str, platform=None,
               lock_timeout_s: float | None = None, check=None, **options) -> dict:
    """The focus gate. Config keys in the helper's config.json: `focus_notice_seconds` (default 3, 0 = no overlay) and
    `focus_restore` (default true; false = never give focus back). `check` is the controls check the gate asks
    before, during and immediately before acting: controls_check (letters; the default) or desktop_check (the
    relink kick, which is not a letter). See crosstalk_focus.focus_wake."""
    focus = _focus_module()
    config = _read_json(helper_dir() / "config.json", {})
    config = config if isinstance(config, dict) else {}
    options.setdefault("notice_s", focus.notice_seconds(config.get("focus_notice_seconds", focus.DEFAULT_NOTICE_S)))
    options.setdefault("restore", config.get("focus_restore") is not False)
    options.setdefault("sleep", FOCUS_SLEEP)
    if target_key.startswith("claude:") and os.name == "nt":
        options.setdefault("chat_focus", DesktopChatFocus())  # put the owner back on the Desktop chat they were on
    wait = FOCUS_LOCK_TIMEOUT_S if lock_timeout_s is None else lock_timeout_s
    return focus.focus_wake(target_key, action, confirm, text=text, kind=kind,
                            controls_check=check or controls_check,
                            lock=lambda timeout: locked(FOCUS_LOCK_NAME, timeout=timeout), log_path=focus_log_path(),
                            platform=platform, lock_timeout_s=wait, **options)


FOCUS_NOTICE_TAIL = ".\nClaude will come forward briefly, then you'll be put back."
DESKTOP_FOCUSED_RE = re.compile(r"setFocusedSession: sessionId=(local_[0-9a-f-]{36}|null)")
DESKTOP_FOCUS_TAIL_BYTES = 256_000


class DesktopChatFocus:
    """Which chat Claude Desktop has selected, and selecting one again. Desktop logs every switch in main.log as
    `LocalSessions.setFocusedSession: sessionId=local_<uuid>` (a `null` line comes first on each switch); the
    chat's own `claude://claude.ai/epitaxy/<local id>` link selects it (it carries no prompt). Read-only apart from
    opening that link."""

    def current(self) -> str | None:
        path = desktop_log_path()
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - DESKTOP_FOCUS_TAIL_BYTES))
                tail = stream.read().decode("utf-8", "replace")
        except OSError:
            return None
        hits = DESKTOP_FOCUSED_RE.findall(tail)
        return hits[-1] if hits and hits[-1] != "null" else None

    def select(self, local_id: str) -> None:
        if not isinstance(local_id, str) or not re.fullmatch(r"local_[0-9a-f-]{36}", local_id):
            raise ValueError("not a Desktop chat id")
        os.startfile(f"claude://claude.ai/epitaxy/{local_id}")  # noqa: S606 (fixed scheme, validated id)


def claude_checked_in(session_id: str, since_ns: int) -> bool:
    """Has this Claude chat checked in with Crosstalk since `since_ns`? Its directory entry's last_seen_ns (its plugin
    registers on start) is newer. Read-only."""
    if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
        return False
    entry = directory_load()["entries"].get(f"claude:{session_id}") or {}
    seen = entry.get("last_seen_ns")
    if isinstance(seen, (int, float)) and seen >= since_ns:
        return True
    return False


# ------------------------------------------------------------ create a Claude chat
CREATE_TIMEOUT_S = 900
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def launch_settings_path() -> Path:
    """``<EUTONOS home>/settings/claude-launch.json``: the per-launch settings file (permissions entries 7 and 8,
    ``autoMode.allow`` only) that Claude chats started here receive through ``--settings`` (IPC contract a)."""
    return eutonos_home() / "settings" / "claude-launch.json"


def create_claude_argv(exe: str, session_id: str, model: str, effort: str, permission_mode: str,
                       title: str | None = None, settings: str | None = None) -> list[str]:
    """Argument list for `claude -p`; the prompt goes on stdin so no shell or argv quoting is involved. A title is
    passed as the session's own display name (`--name`), so the native registration carries it too. `settings` is the
    per-launch settings file (`--settings`), added only when the caller names one."""
    argv = _exe_argv(exe) + ["-p", "--session-id", session_id, "--model", model, "--effort", effort,
                             "--permission-mode", permission_mode, "--output-format", "json"]
    if title:
        argv += ["--name", title]
    if settings:
        argv += ["--settings", settings]
    return argv


def show_claude_argv(exe: str, session_id: str) -> list[str]:
    return _exe_argv(exe) + ["--desktop", "--resume", session_id]


def claude_logged_in(exe: str) -> bool:
    try:
        done = subprocess.run(_exe_argv(exe) + ["auth", "status"], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, env=child_env()[0],
                              creationflags=NO_WINDOW)
        value = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return isinstance(value, dict) and value.get("loggedIn") is True


def verify_created(result, session_id: str, model: str) -> dict:
    """Check the claude -p JSON result and the persisted first turn. Raises ProbeError on any failure."""
    if not isinstance(result, dict):
        raise ProbeError("create-result-not-object")
    if result.get("is_error") is True:
        raise ProbeError("create-is-error", clip_reason(result.get("terminal_reason") or ""))
    if result.get("session_id") != session_id:
        raise ProbeError("create-session-id-mismatch")
    path = claude_transcript_path(session_id)
    if path is None:
        raise ProbeError("create-transcript-missing")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise ProbeError("create-transcript-unreadable")
    usage = result.get("modelUsage")
    used = sorted(usage) if isinstance(usage, dict) else []
    if not (model in text or any(name in text for name in used)):
        raise ProbeError("create-model-not-in-transcript", model[:60])
    return {"model_in_transcript": True, "first_turn_persisted": True, "models_used": used,
            "transcript": str(path)}


def launch_desktop_show(exe: str, session_id: str) -> dict:
    """Detached `claude --desktop --resume`. It refuses captured output, so nothing is redirected and
    success is NOT verifiable from here (launched means only that the process started)."""
    argv = show_claude_argv(exe, session_id)
    try:
        if os.name == "nt":
            # The new console is created hidden (SW_HIDE): live 2026-10-13, no console window showed and Desktop
            # logged "Resume deep link ... warmed successfully".
            hidden = subprocess.STARTUPINFO()
            hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            hidden.wShowWindow = 0  # SW_HIDE
            subprocess.Popen(argv, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden, close_fds=True,
                             env=child_env()[0])
            method = "CREATE_NEW_CONSOLE"
        else:
            subprocess.Popen(argv, start_new_session=True, env=child_env()[0])
            method = "start_new_session"
    except OSError as error:
        return {"launched": False, "method": None, "error": type(error).__name__}
    return {"launched": True, "method": method, "verified": False}


def _auto_mode_supported(model_id) -> bool:
    """Parity copy of eutonos.model_catalog.auto_mode_supported (the kit must not import eutonos; owner, 2026-10-22:
    Haiku 5.5 and later can run auto mode, Haiku 4.5 and older and claude-3 models cannot). An unrecognised Claude ID
    is False. tests/test_model_catalog.py keeps the two equal."""
    if not isinstance(model_id, str):
        return False
    name = re.sub(r"\[[^\]]*\]$", "", model_id.strip().lower())
    if name in ("sonnet", "opus", "fable", "haiku"):
        return True
    match = re.fullmatch(r"claude-([a-z][a-z0-9]*)-(\d+(?:-\d+)*)", name)
    if not match:
        return False
    parts = match.group(2).split("-")
    if re.fullmatch(r"\d{8}", parts[-1]):
        parts.pop()
    if not parts:
        return False
    major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    family = match.group(1)
    if family in ("sonnet", "opus"):
        return major >= 4
    if family == "fable":
        return True
    if family == "haiku":
        return major > 5 or (major == 5 and minor >= 5)
    return False


def _auto_mode_refused(model_id) -> bool:
    """Parity copy of eutonos.model_catalog.auto_mode_refused."""
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    name = model_id.strip().lower()
    return (name.startswith("claude-") or "haiku" in name) and not _auto_mode_supported(model_id)


def create_claude(folder, model, effort, permission_mode, prompt, title=None, show=True) -> dict:
    """Create a Claude chat through the CLI, verify its first turn, register it, optionally show it."""
    refusal = controls_check(None)
    if refusal:
        raise ProbeError("controls-" + refusal)
    if not isinstance(folder, str) or not Path(folder).is_absolute() or not Path(folder).is_dir():
        raise ProbeError("folder-must-be-existing-absolute-dir")
    for name, value in (("model", model), ("permission-mode", permission_mode)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._\[\]-]{1,80}", value):
            raise ProbeError("invalid-" + name)
    if _auto_mode_refused(model):  # parity with role_router._refuse_haiku: refuse a model that cannot run auto mode
        raise ProbeError("model-haiku-refused", f"Claude model {model} cannot run auto mode, so it is not created "
                                                "through Crosstalk")
    if effort not in EFFORTS:
        raise ProbeError("invalid-effort", "one of " + ",".join(EFFORTS))
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ProbeError("prompt-invalid")
    check = account_check()
    if check["state"] == "mismatch":
        raise ProbeError("account-mismatch", f"{check['message']} No chat was created.")
    exe = claude_exe()
    if not exe:
        raise ProbeError("claude-cli-not-found")
    if not claude_logged_in(exe):
        raise ProbeError("claude-not-logged-in", "run claude auth login, then retry")
    session_id = str(uuid.uuid4())
    title = " ".join(title.split())[:80] if isinstance(title, str) and title.strip() else None
    launch = launch_settings_path()
    argv = create_claude_argv(exe, session_id, model, effort, permission_mode, title,
                              str(launch) if launch.is_file() else None)
    try:
        done = subprocess.run(argv, input=prompt, cwd=folder, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CREATE_TIMEOUT_S, env=child_env()[0],
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProbeError("create-run-failed", type(error).__name__)
    try:
        result = json.loads(done.stdout)
    except ValueError:
        raise ProbeError("create-output-not-json", f"exit {done.returncode}")
    verified = verify_created(result, session_id, model)
    entry = register_claude({"sessionId": session_id, "cwd": folder, "title": title}, "create-claude")
    value = {"ok": True, "session_id": session_id, "key": entry["key"], "folder": folder,
             "requested": {"model": model, "effort": effort, "permission_mode": permission_mode},
             "verified": verified, "effort_verified": False,
             "account_id": (entry.get("account") or {}).get("id"), "account_check": check,
             "title_set": bool(title) and entry.get("title") == title}
    if title:
        value["title_note"] = ("directory title recorded and kept across later registrations; passed to the CLI as "
                               "--name. Claude Desktop's own sidebar name is not set by Crosstalk and may differ")
    if show:  # through the focus gate: notice, open, put the owner's window and chat back, confirm
        focus = focus_wake(f"claude:{session_id}", lambda: launch_desktop_show(exe, session_id),
                           lambda start_ns: claude_checked_in(session_id, start_ns),
                           text="Crosstalk: opening a new Claude chat" + (f" \"{str(title)[:40]}\"" if title else "")
                           + FOCUS_NOTICE_TAIL, kind="create-claude")
        shown = dict(focus.get("action_result") or {"launched": False,
                                                     "error": focus.get("refused") or focus.get("error")})
        shown["focus"] = {key: focus.get(key) for key in ("overlay", "came_forward", "restored", "chat_restored",
                                                            "confirmed", "elapsed_s")}
        value["show"] = shown
    else:
        value["show"] = {"launched": False, "method": None}
    return value


# ----------------------------------------------------------- create a Codex chat
CREATE_CODEX_CLIENT = "crosstalk_create_codex"
CODEX_ROLLOUT_WAIT_S = 15


def _create_codex_fail(reason: str, detail: str, thread_id: str | None) -> ProbeError:
    """A ProbeError that names an already-created thread, so nobody makes a duplicate."""
    error = ProbeError(reason, (f"thread_id={thread_id} " if thread_id else "") + detail)
    error.thread_id = thread_id
    return error


def codex_rollout_facts(path, thread_id: str, model: str, effort: str, *, require_completed=True, wait_s=None) -> dict:
    """Read the persisted rollout: session_meta, the first turn_context and whether the first turn finished.

    Startup verification explicitly permits an unfinished turn. Completion verification still requires its
    terminal event. Waits briefly for persistence; this is never an assignment-duration deadline."""
    deadline = time.monotonic() + (CODEX_ROLLOUT_WAIT_S if wait_s is None else wait_s)
    while True:
        meta = context = None
        finished = False
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace") if isinstance(path, str) else ""
        except OSError:
            text = ""
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta" and meta is None:
                meta = payload
            elif row.get("type") == "turn_context" and context is None:
                context = payload
            elif row.get("type") == "event_msg" and payload.get("type") == "task_complete":
                finished = True
        if meta is not None and context is not None and (finished or not require_completed):
            break
        if time.monotonic() >= deadline:
            raise _create_codex_fail("create-rollout-incomplete",
                                     f"session_meta={meta is not None} turn_context={context is not None} "
                                     f"first_turn_finished={finished}", thread_id)
        time.sleep(0.5)
    if meta.get("id") != thread_id:
        raise _create_codex_fail("create-rollout-thread-mismatch", "", thread_id)
    if context.get("model") != model or context.get("effort") != effort:
        raise _create_codex_fail("create-rollout-model-effort-mismatch",
                                 f"rollout model={str(context.get('model'))[:60]} effort={str(context.get('effort'))[:20]}",
                                 thread_id)
    return {"model": context.get("model"), "effort": context.get("effort"), "rollout": str(path),
            "turn_id": context.get("turn_id"),
            "source": meta.get("source"), "originator": meta.get("originator"),
            "cli_version": meta.get("cli_version"), "first_turn_persisted": True,
            "first_turn_completed": finished}


CREATE_CODEX_STARTUP_S = 120.0  # caller wait only; never a task lifetime limit


def _create_codex_path(operation: str) -> Path:
    if not isinstance(operation, str) or not UUID_RE.fullmatch(operation):
        raise ProbeError("invalid-create-operation")
    return device_dir() / "create-codex" / (operation + ".json")


def create_codex_status(operation: str) -> dict:
    """Read the startup/outcome receipt. Reading never restarts a worker or replays input."""
    path = _create_codex_path(operation)
    for attempt in range(20):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError:
            # Windows refuses a read while the worker's os.replace swaps the receipt in; it settles in milliseconds.
            if attempt == 19:
                raise
            time.sleep(0.025)
    if value.get("assignment_state") in ("starting", "start-unconfirmed", "accepted-unverified", "running"):
        limit = 180.0 if value["assignment_state"] == "starting" else CODEX_CLAIM_STALE_S
        stale = (time.time_ns() - int(value.get("heartbeat_ns") or 0)) / 1e9 > limit
        live = pid_alive(value.get("worker_pid"))
        value["worker_observation"] = "not-running" if not live else ("stale" if stale else "live")
        if not live or stale:
            value.update(ok=False, recorded_assignment_state=value["assignment_state"], assignment_state="unknown",
                         reason="create-worker-" + value["worker_observation"],
                         detail="worker observation is incomplete; inspect native thread, never replay automatically")
    return {key: item for key, item in value.items() if key != "request"}


def _create_codex_update(operation: str, **fields) -> dict:
    with locked("state"):
        path = _create_codex_path(operation)
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update(fields)
        _write_json_atomic(path, value)
    return value


def _create_codex_observe(operation: str, **fields) -> dict:
    """A receipt failure after input may have been accepted must not terminate the assignment.

    Its last heartbeat becomes stale if storage stays unavailable. Status then reports unknown; recovery never
    replays the input. Setup writes before turn/start remain required and fail before the assignment starts.
    """
    try:
        return _create_codex_update(operation, **fields)
    except OSError:
        return {"operation_id": operation, "receipt_write_failed": True, **fields}


CONFIG_KEY_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")


def _validate_codex_config(config):
    """Extra thread/start config overrides (e.g. memories.use_memories=false): plain keys, scalar or string-list values, never the
    effort, which --effort owns. None means no overrides."""
    if config is None:
        return None
    if not isinstance(config, dict) or len(config) > 8:
        raise ProbeError("config-invalid", "at most 8 KEY=VALUE overrides")
    for key, value in config.items():
        if not isinstance(key, str) or not CONFIG_KEY_RE.fullmatch(key) or key == "model_reasoning_effort":
            raise ProbeError("config-invalid", f"key {str(key)[:60]!r} is not allowed")
        short = lambda v: isinstance(v, (bool, int)) or isinstance(v, str) and len(v) <= 200
        if not (short(value) or isinstance(value, list) and len(value) <= 8
                and all(isinstance(v, str) and short(v) for v in value)):
            raise ProbeError("config-invalid", f"value for {key} must be a short bool, int, string or string list")
    return dict(config)


def _validate_create_codex(folder, model, effort, prompt, config=None):
    _validate_codex_config(config)
    refusal = controls_check(None)
    if refusal:
        raise ProbeError("controls-" + refusal)
    if not isinstance(folder, str) or not Path(folder).is_absolute() or not Path(folder).is_dir():
        raise ProbeError("folder-must-be-existing-absolute-dir")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._\[\]-]{1,80}", model):
        raise ProbeError("invalid-model")
    if effort not in EFFORTS:
        raise ProbeError("invalid-effort", "one of " + ",".join(EFFORTS))
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ProbeError("prompt-invalid")
    exe = codex_exe()
    if not exe:
        raise ProbeError("codex-cli-not-found")
    return exe


def create_codex(folder, model, effort, prompt, title=None, config=None) -> dict:
    """Start one detached creator and wait only for startup evidence, never assignment completion.

    The operation receipt survives the caller. A timeout is uncertain and never cancels or retries the worker.
    Native Desktop project association/visibility is separate from the verified thread and directory entry.
    """
    _validate_create_codex(folder, model, effort, prompt, config)
    operation = str(uuid.uuid4())
    path = _create_codex_path(operation)
    _write_json_atomic(path, {"operation_id": operation, "assignment_state": "queued", "ok": False,
                              "request": {"folder": folder, "model": model, "effort": effort,
                                          "prompt": prompt, "title": title,
                                          **({"config": dict(config)} if config else {})}})
    try:
        _spawn_detached([windowless_python(), "-I", "-B", str(KIT / "crosstalk_p0_cli.py"),
                         "codex-create-worker", operation])
    except OSError as error:
        _create_codex_update(operation, assignment_state="spawn-failed", reason="create-worker-spawn-failed")
        failure = _create_codex_fail("create-worker-spawn-failed", type(error).__name__, None)
        failure.operation_id = operation
        raise failure
    deadline = time.monotonic() + CREATE_CODEX_STARTUP_S
    value = {}
    while True:
        try:
            value = create_codex_status(operation)
        except OSError as error:
            failure = _create_codex_fail("create-startup-unconfirmed",
                                         f"operation_id={operation}; receipt unreadable ({type(error).__name__}); do not retry",
                                         value.get("thread_id"))
            failure.operation_id = operation
            raise failure
        if value.get("ok"):
            return value
        if value.get("assignment_state") in ("failed", "uncertain", "refused", "unknown", "interrupted"):
            failure = _create_codex_fail(value.get("reason") or "create-failed", value.get("detail") or "",
                                          value.get("thread_id"))
            failure.operation_id = operation
            raise failure
        if time.monotonic() >= deadline:
            failure = _create_codex_fail("create-startup-unconfirmed",
                                         f"operation_id={operation}; worker may still run; inspect its receipt, do not retry",
                                         value.get("thread_id"))
            failure.operation_id = operation
            raise failure
        time.sleep(0.05)


def codex_create_worker(operation: str) -> dict:
    """Claim one request once. A crashed/uncertain operation is inspected, never automatically replayed."""
    path = _create_codex_path(operation)
    with locked("state"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("assignment_state") != "queued":
            return {"ok": False, "reason": "create-operation-already-claimed", "operation_id": operation}
        request = value.pop("request")
        value.update(assignment_state="starting", worker_pid=os.getpid(), heartbeat_ns=time.time_ns())
        _write_json_atomic(path, value)
    try:
        return _run_create_codex(**request, operation=operation)
    except Exception as error:
        known = create_codex_status(operation)
        reason = getattr(error, "reason", "create-worker-failed")
        uncertain = "uncertain" in reason or reason in ("create-turn-start-failed", "create-thread-id-missing")
        state = "interrupted" if known.get("interrupted_by") or known.get("turn_status") == "interrupted" else (
            "uncertain" if uncertain else "failed")
        _create_codex_update(operation, ok=False, assignment_state=state,
                             reason=reason, detail=getattr(error, "detail", type(error).__name__),
                             thread_id=getattr(error, "thread_id", None) or known.get("thread_id"))
        return create_codex_status(operation)


def _project_path_key(path) -> str | None:
    """Comparable form of a project root or folder: extended-length prefix stripped, backslashes, lower case, no trailing separator."""
    bs = chr(92)
    if not isinstance(path, str) or not path.strip():
        return None
    text = path.strip().replace("/", bs)
    if text.startswith(bs * 2 + "?" + bs + "UNC" + bs):
        text = bs * 2 + text[8:]
    elif text.startswith(bs * 2 + "?" + bs):
        text = text[4:]
    text = text.rstrip(bs)
    return text.lower() or None


def _codex_model_check(server, model) -> tuple:
    """Ask the app-server's `model/list` (ModelListParams: cursor, includeHidden, limit; ModelListResponse:
    data[] of Model with `id` and `model`, nextCursor) whether `model` is offered. Returns (known, ids, note):
    known is True/False, or None when the list could not be read (creation then proceeds as before)."""
    ids, cursor = [], None
    try:
        for _ in range(20):
            params = {"includeHidden": True}
            if cursor:
                params["cursor"] = cursor
            page = server.request("model/list", params, 30)
            page = page if isinstance(page, dict) else {}
            for item in page.get("data") or []:
                if isinstance(item, dict):
                    for field in ("id", "model"):
                        if isinstance(item.get(field), str) and item[field] not in ids:
                            ids.append(item[field])
            cursor = page.get("nextCursor")
            if not cursor:
                return (model in ids if ids else None), ids, ("model list empty" if not ids else None)
        return None, ids, "model list unfinished"
    except ProbeError as error:
        return None, ids, "model-list-" + error.reason
    except (TimeoutError, OSError):
        return None, ids, "model-list-unavailable"


def _codex_project_for_folder(server, folder) -> tuple:
    """Read-only project/list (all pages); return (project_id|None, state). Never creates a project."""
    want = _project_path_key(folder)
    cursor = None
    try:
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            page = server.request("project/list", params, 30)
            page = page if isinstance(page, dict) else {}
            for project in page.get("data") or []:
                if not isinstance(project, dict) or not isinstance(project.get("id"), str):
                    continue
                for root in project.get("roots") or []:
                    if want and isinstance(root, dict) and _project_path_key(root.get("path")) == want:
                        return project["id"], "assigned"
            cursor = page.get("nextCursor")
            if not cursor:
                return None, "none-for-folder"
        return None, "unknown"
    except ProbeError as error:
        return None, ("unsupported" if error.reason == "app-server-error" else "unknown")
    except (TimeoutError, OSError):
        return None, "unknown"


def codex_assign_project(thread_id: str, folder: str | None = None) -> dict:
    """Assign an existing Codex thread to the app-server project whose root is the folder (thread/metadata/update).

    Refuses when no project matches; touches only this thread. Desktop sidebar visibility stays unverified."""
    refusal = controls_check("codex:" + str(thread_id))
    if refusal:
        raise ProbeError("controls-" + refusal)
    if not isinstance(thread_id, str) or not UUID_RE.fullmatch(thread_id):
        raise ProbeError("invalid-thread-id")
    if folder is None:
        entry = directory_load()["entries"].get("codex:" + thread_id) or {}
        folder = entry.get("project_folder")
    if not isinstance(folder, str) or not folder:
        raise ProbeError("folder-unknown", "give --folder; the directory has no project_folder for this thread")
    exe = codex_exe()
    if not exe:
        raise ProbeError("codex-cli-not-found")
    server = AppServer(exe, kill_on_close=True)
    try:
        server.start(True, 30, CREATE_CODEX_CLIENT)
        project_id, state = _codex_project_for_folder(server, folder)
        if project_id is None:
            return {"ok": False, "reason": "no-project-for-folder" if state == "none-for-folder" else "project-lookup-" + state,
                    "project_assignment": state, "folder": folder, "thread_id": thread_id}
        server.request("thread/metadata/update", {"threadId": thread_id, "projectId": project_id}, 30)
        return {"ok": True, "project_id": project_id, "project_assignment": "assigned", "thread_id": thread_id,
                "folder": folder, "desktop_integration": "unverified"}
    finally:
        server.close()


def _run_create_codex(folder, model, effort, prompt, title=None, config=None, *, operation) -> dict:
    """One worker owns the server for the whole assignment; no task-duration deadline."""
    exe = _validate_create_codex(folder, model, effort, prompt, config)
    clean_title = " ".join(title.split())[:80] if isinstance(title, str) and title.strip() else None
    thread_id = None
    server = None
    try:
        server = AppServer(exe, kill_on_close=True)
        if not getattr(server, "job_ok", True):
            raise ProbeError("create-job-object-unavailable")
        server.start(True, 30, CREATE_CODEX_CLIENT)
        refusal = controls_check(None)
        if refusal:
            raise ProbeError("controls-" + refusal)
        known_model, model_ids, model_note = _codex_model_check(server, model)
        if known_model is False:
            # Refuse before thread/start: an unknown model would otherwise leave a dead chat behind.
            raise ProbeError("unknown-model", f"{model[:60]} is not offered by this Codex; available: "
                             + ", ".join(model_ids[:12]) + (" ..." if len(model_ids) > 12 else ""))
        project_id, project_assignment = _codex_project_for_folder(server, folder)
        start_params = {"cwd": folder, "model": model,
                        "allowProviderModelFallback": False, "ephemeral": False,
                        "config": {**(config or {}), "model_reasoning_effort": effort}}
        try:
            try:
                started = server.request("thread/start", dict(start_params, **({"projectId": project_id} if project_id else {})), 60)
            except ProbeError as error:
                if not (project_id and error.reason == "app-server-error"):
                    raise
                # A JSON-RPC error response proves no thread was made; retry once without the project.
                project_id, project_assignment = None, "unsupported"
                started = server.request("thread/start", start_params, 60)
        except (TimeoutError, ProbeError) as error:
            if isinstance(error, ProbeError) and error.reason == "app-server-error":
                raise  # the server refused the request: no thread was made
            raise ProbeError("create-thread-start-uncertain",
                             "a thread may exist; check thread/list before any retry, do not create a second")
        started = started if isinstance(started, dict) else {}
        thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not UUID_RE.fullmatch(thread_id):
            raise ProbeError("create-thread-id-missing", "a thread may exist; check thread/list, do not create a second")
        _create_codex_update(operation, thread_id=thread_id, key="codex:" + thread_id)
        if started.get("model") != model or started.get("reasoningEffort") != effort:
            raise _create_codex_fail("create-model-effort-refused",
                                     f"requested {model}/{effort}, server returned "
                                     f"{str(started.get('model'))[:60]}/{str(started.get('reasoningEffort'))[:20]}; "
                                     "the thread exists and no turn was started",
                                     thread_id)
        refusal = controls_check("codex:" + thread_id)
        if refusal:
            raise _create_codex_fail("controls-" + refusal, "no turn started", thread_id)
        title_set, title_note = False, ("no title given" if clean_title is None else None)
        if clean_title is not None:
            try:
                _headless_call(server, "thread/name/set", {"threadId": thread_id, "name": clean_title}, 30, [], {})
                title_set = True
            except (TimeoutError, ProbeError) as error:
                title_note = getattr(error, "reason", type(error).__name__)
        account = codex_account(use_cache=False)
        fields = {"project_folder": folder, "project_folder_source": "create-codex", "title": clean_title,
                  "account": account, "source": "create-codex",
                  "native": {"model": model, "effort": effort, "source": thread.get("source")}}
        event = {"event": "register", "app": "codex",
                 "caller": {"thread_id": thread_id, "cwd": folder, "source": "create-codex"}}
        entry = directory_register("codex", thread_id, fields, event)
        value = {"thread_id": thread_id, "key": entry["key"], "folder": folder,
                 "requested": {"model": model, "effort": effort, **({"config": dict(config)} if config else {})},
                 "account_id": (entry.get("account") or {}).get("id"), "title_set": title_set,
                 "desktop_integration": "unverified", "project_id": project_id,
                 "project_assignment": project_assignment}
        if title_note:
            value["title_note"] = title_note
        if model_note:
            value["model_check_note"] = model_note
        _create_codex_update(operation, **value)
        refusal = controls_check("codex:" + thread_id)
        if refusal:
            raise _create_codex_fail("controls-" + refusal, "no turn started", thread_id)
        backlog, tally = [], {}
        try:
            turn = _headless_call(server, "turn/start", {"threadId": thread_id, "model": model, "effort": effort,
                                                       "input": [{"type": "text", "text": prompt}]}, 60, backlog, tally)
        except (OSError, TimeoutError, ProbeError) as error:
            if isinstance(error, ProbeError) and error.reason in ("app-server-error", "app-server-exited"):
                reason = "create-turn-start-refused" if error.reason == "app-server-error" else "create-server-exited"
                raise _create_codex_fail(reason, error.reason, thread_id)
            # A lost acknowledgement does not prove the turn was refused. Keep this same connection alive,
            # recover its identity from notifications/persistence, and never resend or kill it for a wait expiry.
            _create_codex_observe(operation, turn_start_uncertain=True,
                                 startup_note="turn/start acknowledgement missing; monitoring without retry")
            turn = None
        turn_id = ((turn or {}).get("turn") or {}).get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = None
        value.update(assignment_state="accepted-unverified" if turn_id else "start-unconfirmed", turn_id=turn_id)
        _create_codex_observe(operation, **value)
        verified = None
        startup_announced = False
        final_text = other_text = None
        turn_error = None
        interrupted_by, interrupt_at, last_check = None, 0.0, -CODEX_HEADLESS_CONTROLS_S
        while True:
            now = time.monotonic()
            if now - last_check >= CODEX_HEADLESS_CONTROLS_S:
                last_check = now
                _create_codex_observe(operation, heartbeat_ns=time.time_ns())
                if interrupted_by is None:
                    refusal = controls_check("codex:" + thread_id)
                    if refusal:
                        time.sleep(0.1)
                        refusal = controls_check("codex:" + thread_id)
                    if refusal:
                        interrupted_by, interrupt_at = refusal, now
                        _create_codex_observe(operation, interrupted_by=refusal)
                        if turn_id is None:
                            # No invented protocol parameters: an explicit owner control cancels our private
                            # server even when the accepted turn's ID could not yet be observed.
                            raise _create_codex_fail("controls-" + refusal, "unconfirmed turn cancelled", thread_id)
                        server.send_request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                elif now - interrupt_at > CODEX_HEADLESS_INTERRUPT_WAIT_S:
                    raise _create_codex_fail("create-interrupt-unanswered", interrupted_by, thread_id)
                if verified is None:
                    try:
                        verified = codex_rollout_facts(thread.get("path"), thread_id, model, effort,
                                                       require_completed=False, wait_s=0)
                    except ProbeError as error:
                        if error.reason != "create-rollout-incomplete":
                            raise  # an observed identity/model mismatch refuses; delayed evidence never kills a turn
                if verified is not None and turn_id is None:
                    observed_id = verified.get("turn_id")
                    if isinstance(observed_id, str) and observed_id:
                        turn_id = observed_id
                if verified is not None and turn_id is not None and not startup_announced:
                    observed = _create_codex_observe(operation, ok=True, assignment_state="running", turn_id=turn_id,
                                                     verified=verified)
                    startup_announced = not observed.get("receipt_write_failed")
            try:
                message = backlog.pop(0) if backlog else server.lines.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                raise _create_codex_fail("create-server-exited", "before turn/completed", thread_id)
            method = message.get("method") if isinstance(message, dict) else None
            if not isinstance(method, str):
                continue
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if "id" in message:
                _decline_server_request(server, message, tally)
                continue
            if params.get("threadId") != thread_id:
                continue
            if method in ("turn/started", "turn/completed") and turn_id is None:
                observed_id = (params.get("turn") or {}).get("id")
                if isinstance(observed_id, str) and observed_id:
                    turn_id = observed_id
                    _create_codex_observe(operation, turn_id=turn_id)
            if method == "error":
                problem = params.get("error") if isinstance(params.get("error"), dict) else {}
                turn_error = " ".join(str(problem.get("message") or "error").split())[:160]
                if params.get("willRetry") is False:
                    raise _create_codex_fail("create-turn-failed-uncertain",
                                             f"the first turn failed after the thread was created: {turn_error}",
                                             thread_id)
            if method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    if item.get("phase") == "final_answer":
                        final_text = item["text"]
                    elif item.get("phase") != "commentary":
                        other_text = item["text"]
            elif method == "turn/completed":
                done = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if turn_id and done.get("id") not in (None, turn_id):
                    continue
                if done.get("status") != "completed":
                    _create_codex_observe(operation, turn_status=done.get("status"),
                                         owner_interrupted=done.get("status") == "interrupted" and interrupted_by is None)
                    problem = done.get("error") if isinstance(done.get("error"), dict) else {}
                    raise _create_codex_fail("create-turn-not-completed",
                                             f"status={str(done.get('status'))[:30]} "
                                             f"{str(problem.get('message') or turn_error or '')[:160]}", thread_id)
                break
        reply = final_text if final_text is not None else other_text
        reply_phase = "final_answer" if final_text is not None else ("unlabelled" if other_text is not None else None)
        verified = codex_rollout_facts(thread.get("path"), thread_id, model, effort)
        verified.update(first_reply=reply, reply_phase=reply_phase)
        server.close()
        server = None
        return _create_codex_observe(operation, ok=True, assignment_state="completed", turn_status="completed", verified=verified,
                                    declined_requests=tally.get("declined", 0))
    except ProbeError as error:
        if thread_id and not getattr(error, "thread_id", None):
            error.thread_id = thread_id
            error.detail = f"thread_id={thread_id} {error.detail}".strip()
        raise
    except (OSError, TimeoutError, ValueError, KeyError, TypeError) as error:
        raise _create_codex_fail("create-failed", type(error).__name__, thread_id)
    finally:
        if server is not None:
            server.close()


# ---------------------------------------------------------- create a Hermes chat
# Hermes Desktop has no CLI or REST route that makes a chat; the live `hermes serve` backend does, over its
# /api/ws JSON-RPC (session.create + prompt.submit, verified in the Hermes source 2026-10-14). The work is done by
# crosstalk_hermes_connector.py, run with the Hermes venv python (it has `websockets`), which holds the session
# token in its own memory only. This process never sees, stores or journals that token.
HERMES_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")  # hermes_constants + none
HERMES_CONNECTOR = Path(__file__).resolve().parent / "crosstalk_hermes_connector.py"
CREATE_HERMES_DEFAULT_WAIT_S = 300
CREATE_HERMES_SPAWN_SLACK_S = 90
HERMES_MODEL_RE = re.compile(r"[A-Za-z0-9._:/@\[\]+-]{1,120}\Z")
HERMES_PROVIDER_RE = re.compile(r"[A-Za-z0-9._:-]{1,60}\Z")


def hermes_python() -> Path | None:
    """The Hermes venv's python (it carries `websockets`). CROSSTALK_P0_HERMES_PYTHON overrides (tests)."""
    override = os.environ.get("CROSSTALK_P0_HERMES_PYTHON")
    candidate = Path(override) if override else hermes_home() / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    return candidate if candidate.is_file() else None


def create_hermes(prompt, title=None, model=None, provider=None, effort=None,
                  wait_s=CREATE_HERMES_DEFAULT_WAIT_S) -> dict:
    """Create a Hermes Desktop chat with a first prompt and register hermes:<stored_session_id>.

    One session.create and one prompt.submit are ever sent, never a retry: once the chat may exist every failure
    names it (stored_session_id in the ProbeError detail and attribute) so nobody makes a duplicate. With no
    model, none is sent and Hermes uses its configured default. Effort is checked against Hermes' own list before
    anything connects."""
    refusal = controls_check(None)
    if refusal:
        raise ProbeError("controls-" + refusal)
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ProbeError("prompt-invalid")
    if effort is not None and effort not in HERMES_EFFORTS:
        raise ProbeError("invalid-effort", "one of " + ",".join(HERMES_EFFORTS))
    if model is not None and (not isinstance(model, str) or not HERMES_MODEL_RE.fullmatch(model)):
        raise ProbeError("invalid-model")
    if provider is not None:
        if model is None:
            raise ProbeError("provider-needs-model", "Hermes applies a provider only together with a model")
        if not isinstance(provider, str) or not HERMES_PROVIDER_RE.fullmatch(provider):
            raise ProbeError("invalid-provider")
    if not isinstance(wait_s, (int, float)) or isinstance(wait_s, bool) or not 10 <= wait_s <= 3600:
        raise ProbeError("invalid-wait", "10 to 3600 seconds")
    clean_title = " ".join(title.split())[:80] if isinstance(title, str) and title.strip() else None
    python = hermes_python()
    if python is None:
        raise ProbeError("hermes-python-not-found", "the Hermes venv python is missing; is Hermes installed?")
    if not HERMES_CONNECTOR.is_file():
        raise ProbeError("hermes-connector-missing")
    request = {"hermes_home": str(hermes_home()), "prompt": prompt, "title": clean_title, "model": model,
               "provider": provider, "effort": effort, "wait_s": wait_s, "idempotency_key": uuid.uuid4().hex}
    if clean_title:  # the connector runs this (plus --session ID) before the first prompt is submitted
        request["pre_register_argv"] = [sys.executable, "-I", "-B", str(Path(__file__).with_name("crosstalk_p0_cli.py")),
                                        "hermes-pre-register", "--title=" + clean_title]
    try:
        done = subprocess.run([str(python), "-I", "-B", str(HERMES_CONNECTOR)], input=json.dumps(request),
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=wait_s + CREATE_HERMES_SPAWN_SLACK_S, env=child_env()[0],
                              creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ProbeError("create-hermes-uncertain",
                         "the connector timed out; a chat may exist, check Hermes Desktop before any retry")
    except OSError as error:
        raise ProbeError("create-run-failed", type(error).__name__)
    try:
        result = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ProbeError("create-hermes-output-not-json",
                         f"exit {done.returncode}; a chat may exist, check Hermes Desktop before any retry")
    if not isinstance(result, dict):
        raise ProbeError("create-hermes-output-not-json", f"exit {done.returncode}")
    stored = result.get("stored_session_id")
    has_id = isinstance(stored, str) and bool(HERMES_ID_RE.fullmatch(stored))
    if result.get("ok") is not True:
        reason = result.get("reason")
        if not (isinstance(reason, str) and re.fullmatch(r"[a-z0-9-]{1,60}", reason)):
            reason = "create-hermes-failed"
        error = ProbeError(reason, (f"stored_session_id={stored} the chat exists, do not create a second; "
                                    if has_id else "") + str(result.get("detail") or "")[:300])
        if has_id:
            error.stored_session_id = stored
            _drop_pending_create(stored)
        raise error
    if not has_id:
        raise ProbeError("create-hermes-session-id-missing",
                         "a chat may exist; check Hermes Desktop, do not create a second")
    fields = {"project_folder": None, "title": clean_title, "hermes_source": "desktop", "source": "create-hermes",
              "title_source": CREATOR_TITLE_SOURCE if clean_title else None,
              "native": {"model": model, "provider": provider, "effort": effort}}
    event = {"event": "register", "app": "hermes", "caller": {"session_id": stored, "source": "create-hermes"}}
    entry = directory_register("hermes", stored,
                               {k: v for k, v in fields.items() if v is not None or k == "project_folder"}, event)
    rows, note = hermes_sessions(stored, 1) if hermes_installed() else (None, "hermes-plugin-not-installed")
    value = {"ok": True, "session_id": stored, "key": entry["key"],
             "requested": {"model": model, "provider": provider, "effort": effort},
             "reported": result.get("reported"), "first_turn": result.get("turn"),
             "turn_status": result.get("turn_status"),
             "state_db": {"row_found": bool(rows) if rows is not None else None, "note": note},
             "effort_verified": False,
             "injectable_note": "the backend reaps a disconnected chat after about 20 s; the Crosstalk plugin "
                                "reloads a closed chat itself when a letter arrives (needs the updated plugin)"}
    if clean_title:
        value["title"] = clean_title
    return value
