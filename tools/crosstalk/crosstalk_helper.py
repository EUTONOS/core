"""Crosstalk helper: a small watcher started at login (stdlib + crosstalk_p0_core).

What it does, each tick (about 15 s; between ticks a light look every 2 s, or every 0.5 s while a Desktop load is
being relinked, see Helper.fast_step: one stat of main.log, so a Desktop start is seen within about a second):
  a. readback-only reconcile (reconcile(resend=False)) so held letters settle without anyone calling status;
  b. follows Claude Desktop's main.log for "Loaded N persisted sessions from <account folder>" lines. Each one is a
     Desktop load (app start or account switch). A load is handled once; it is remembered in helper/loads.json;
  c. restart relink: as soon as a load is ready (LOAD_READY_MIN_S, about 2 s after the load line, once the account
     folder holds chat records; this replaced the fixed 30 s settle wait, because Desktop does not turn Remote
     Control on for stopped chats by itself after a start, so nothing else needs to settle), lists ONE entry per
     chat on the active account whose Remote Control was on at the end of that account's previous Desktop run (last
     Enabling/Disabling line in main.log), or whose stored record has remoteControlUserEnabled true, or that
     config.json lists in always_remote; never one whose stored flag is false (the owner's "off"), and none that
     main.log shows Desktop re-enabled after the load (a chat merely running since the load, e.g. warmed by Desktop,
     still gets one). The helper sends nothing itself: ONE running chat's plugin hook claims the list
     (core.relink_claim, which also skips, at claim time, a chat Desktop enabled since the list was made, so a chat
     the owner opened in the meantime is never relinked twice) and sends the fixed reconnect text through Desktop's
     send_message, with no model turn and no relay chat. While entries wait it keeps helper/relink-open.json for the
     plugin to poll (once a second). Without the installer's send_message permission rule the plugin does nothing
     and a notice says so. An entry is never retried. After a restart every chat may be stopped, so nobody polls:
     when the list is still wholly unclaimed RELINK_KICK_AFTER_S (10 s) after it was made, the helper opens ONE
     listed chat (most recently active) through the focus gate, whose plugin then claims the rest (Helper.relink_kick;
     at most RELINK_KICK_MAX opens per load, wake budget counted, config `relink_kick: false` turns it off);
  c2. relink alert (slice 3B.18, owner request 2026-10-26): once per load, as soon as the load line shows chats to
     relink, the helper starts crosstalk_relink_alert.py in its own windowless process: a small always-on-top corner
     window "Enabling Remote Control for your sessions..." with "n of m", driven read-only by loads.json, that never
     takes focus (a native Win32 window with WS_EX_NOACTIVATE, shown without activation) and closes itself when the
     relink is done. If some chat could not reconnect it says so ("4 of 5 connected, 1 could not reconnect"), names
     it, and stays until clicked or about a minute. It is remembered in loads.json (`alert`) before it starts, so a
     window the owner clicked away is not shown again for that load. Config `relink_alert: false` turns it off. Halt
     or config `relink: false`: no window is started (the relink refuses every entry, so nothing would be
     announced; a window already up closes itself when the entries leave the working state). Switching Crosstalk
     messaging off does not stop it (messaging is for letters only);
  d. health: heartbeat, account check, plugin version, the permission rules, Remote Control flags that differ between
     the two account folders. Each one is a notice that clears when it is resolved. On an account mismatch it also asks
     the owner, once per (Desktop, CLI) account pair, whether to log the CLI in to the Desktop account (a Yes/No box
     in its own process; Yes opens `claude auth login` for the owner to finish). Config `login_prompt: false` turns
     this off;
  e. letter wake (the only route to an asleep Claude chat): a letter waits for a Claude chat that is not running
     (blocked:not-running, held, or an uncertain mail-slot letter; a Desktop record in the active account folder, not
     archived, not a routine, not older than the mail slot's 24 h). The helper wakes that chat through the focus gate,
     at most one chat per tick. A wake is not a send, so it is retried with backoff (core.WAKE_BACKOFF_S, 4 attempts
     per letter, each remembered in state.json before anything is launched); several letters to one chat share a wake.
     Every wake is counted against the wake budget in controls (core.wake_budget_claim: per chat and in total per
     hour); a chat over budget is skipped without using an attempt and one notice says its letters wait unwoken.
     The letter is never sent or resent: the woken chat takes it from its mail slot, and after its first turn its
     plugin switches the chat's own Remote Control back on. Config `letter_wake: false` turns this off;
  f. halt stops every effect (wakes, relink, kick, alert, rename, login prompt, switch history, chat copying) and
     refuses every relink entry. Messaging off stops only what is a letter (sends, creates, notices, letter wakes,
     the Codex passes, the nudge); the Desktop jobs have their own switches below (core.desktop_check: halt and the
     deny list, never messaging). Readback continues: it only reads (see reconcile).
     Config switches (each absent = on, except chat_copy): `relink`, `relink_kick`, `relink_alert`, `letter_wake`,
     `rename_titles`, `rename_now`, `login_prompt`, `history_fix`, `chat_copy` (absent = OFF),
     `chat_copy_deletions`, `chat_copy_snapshots`; a change applies at the next tick.
  g. rename (slice 3B.10): at most ONE chat per tick whose Remote Control is on, with a title whose titleSource is
     "auto" or "tool", or whose newest Remote Control session (bridgeSessionIds) has not had its name yet (a
     relink, e.g. after an account switch, shows a generated name even for an owner-given title), gets a real
     rename through crosstalk_rename (it types only into Desktop's verified rename box). Only when Claude Desktop
     is already in front and the owner is idle; it never brings a window forward. A small ledger remembers
     attempts per (chat, title) and per (chat, remote session): done once, 10 min back-off after a failure, three
     failures end it. Every skipped tick says nothing, but a CHANGE of the skip reason (desktop-not-in-front,
     owner-active, nothing-to-rename, controls, ...) is one line in helper_log.txt, so a delay can be explained;
  g2. rename-now pass (owner request 2026-10-25): after a Desktop load (app start or account switch) Desktop gives
     every chat a new remote session within about a minute, and the phone shows generated names until the rename in
     g. Waiting for the owner to be idle with Desktop in front took 1 h 45 min, so once per load, when the load is at
     least RENAME_NOW_AFTER_S old, its relink list is finished (or RENAME_NOW_RELINK_WAIT_S has passed), the list of
     rename candidates has not changed for RENAME_NOW_QUIET_S, and the CLI-login Yes/No prompt is not open, the helper
     (1) shows a notice window in front ("turning on remote access and fixing chat names, please don't type or click
     until this closes", one Cancel button; crosstalk_rename_notice, its own process), (2) brings Claude Desktop to
     the front (the explicit exception to the never-bring-forward rule, like the login prompt: the owner has been
     told), and (3) renames every candidate back to back with the same verified renamer, using "no owner input since
     my last keystroke" instead of the owner-idle wait. Any owner input, Cancel, halt, the rename kill
     switch, config `rename_titles: false` or `rename_now: false`, a failed guard, two failed renames in a row or
     RENAME_NOW_MAX_PASS_S ends the pass; whatever is left falls to the idle path in g. The ledger rules of g apply
     (done once, back-off, three failures end it). The pass is remembered in loads.json, so it runs at most once per
     load; it blocks the tick (bounded; the heartbeat is refreshed after every chat). Without window handling (not
     Windows, --once, CROSSTALK_P0_FOCUS=off) it does nothing;
  h. unconfirmed letters (slice 3B.15): a letter that stays accepted/held/uncertain for core.NUDGE_AFTER_S (5 min)
     and that one more readback still does not find gets exactly ONE short notice to its SENDER; after 24 h it is
     retired as stale-unconfirmed (journal only) with one final notice. A notice is a NEW Crosstalk letter whose
     header names Crosstalk itself ("Crosstalk message from Crosstalk (delivery notice)"); it never goes to the
     recipient, is never about another notice, and goes through the same pre-send guard (halt, messaging off, deny,
     account recheck) and the wake budget. If the sender's app cannot take it, a helper notice carries it instead.
  i. switch history (slice 3B.16, opt-in: config `switch_history` lists the person's own linked Claude accounts by
     email or account ID): right after a load that switches between two linked accounts, and before Desktop restarts
     the chats, cleans the transcript of each restart
     relink candidate that is not running so its relinked Remote Control session gets the chat's history
     (crosstalk_switch_history). Once per load, results in loads.json; halt skips it, so does config
     `history_fix: false` (absent = on, and it acts only with a non-empty `switch_history`).
  j. chat copying (slice 3B.19, default OFF: config `chat_copy: true`; needs `switch_history` linked accounts):
     crosstalk_chat_copy copies chat records from the account folder Desktop has loaded to the other linked
     account's folder (never into the active one, never creating a folder), mirrors a deletion only through
     Desktop's own deleted marker (the record goes to <device>/helper/chat-copy/trash), and keeps a daily snapshot
     (7 kept). Halt pauses it. It refuses to run while the personal copier's Startup shortcut exists.

Timing from the Desktop load line (fast path, a chat running): load seen <= 1 s (log stat every 0.5-2 s), list made
at 2 s, claimed within about 1-2 s by a running chat's plugin (polls every second; Desktop itself warms the focused
chat within seconds), one send per chat at about 2 s each, run in parallel by the plugin. The kick (no chat running)
opens one chat 10 s after the list.

A refusal of the focus gate for a busy or failing focus lock (nothing was launched) is transient: it counts as an
attempt and the backoff retries it. A genuine or uncertain launch failure is retried the same way, because a wake
never sends the letter.

It never resends or replaces a letter (the only letters it sends are the notices in h, which are new and go to the
sender of an unconfirmed letter), never switches accounts, never opens a Codex link and never
reads a credential. The only focus it moves is through the focus gate: the wake of an asleep chat for a waiting
letter (e), and the rename-now pass (g2), which brings its notice and then Claude Desktop forward once per load.
The idle rename (g) moves no window.

Usage (installed runtime, isolated):
  python -I -B crosstalk_helper.py --status | --once | --watch
Single instance: --watch and --once take a lock; a second one exits.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # python -I drops the script directory from sys.path
    sys.path.insert(0, str(HERE))

import crosstalk_p0_core as core  # noqa: E402
import crosstalk_chat_copy as chat_copying  # noqa: E402
import crosstalk_rename as renaming  # noqa: E402
import crosstalk_relink_alert as relink_alert  # noqa: E402
import crosstalk_rename_notice as rename_notice  # noqa: E402
import crosstalk_switch_history as switch_history  # noqa: E402

HELPER_VERSION = "0.2.0"
TAG = "crosstalk.helper.v1"
TICK_S = 15.0
FAST_FOLLOW_S = 0.5                # while a Desktop load is being relinked: follow main.log and the relink list this often
IDLE_LOG_POLL_S = 2.0              # otherwise: look at main.log's size this often (one stat) so a restart is seen at once
FAST_FOLLOW_MAX_S = 180.0          # a load keeps the fast follow this long at most
LOAD_READY_MIN_S = 2.0             # a load line must be this old: an account switch's next load line follows within moments
LOAD_READY_MAX_WAIT_S = 30.0       # ... and the account folder must hold chat records, but never wait longer than this
ALERT_MAX_AGE_S = 60.0             # a load older than this when the helper decides gets no alert
LOAD_FIRST_START_MAX_AGE_S = 900   # on first start only a load younger than this is acted on
LOAD_PENDING_MAX_S = 900           # a load blocked by controls this long is dropped, not acted on later
RECONCILE_EVERY_S = 30.0
NUDGE_EVERY_S = 60.0               # the unconfirmed-letter pass (nudge, final notice, 24 h retirement)
HEALTH_EVERY_S = 60.0
ACCOUNT_UNKNOWN_NOTICE_AFTER_S = 90.0  # an account switch passes through "unknown" for a moment: no notice for that
LOG_ROTATE_BYTES = 1_000_000
LOG_FOLLOW_MAX_BYTES = 4_000_000   # one tick reads at most this much of main.log; older bytes are skipped (tail kept)
TRANSIENT_REFUSALS = ("focus-lock-busy", "focus-lock-error")  # the gate refused before anything was launched
RELINK_RULE = "mcp__ccd_session_mgmt__send_message"
RELINK_KICK_AFTER_S = 10.0         # a relink list no running chat claimed in this long: the helper opens one listed chat
RELINK_KICK_RETRY_S = 30.0         # ... and tries again this long after an open that still left the list unclaimed
RELINK_FLAG_REFRESH_S = 30.0       # the plugin's flag file is rewritten at most this often (the plugin accepts 120 s)
RELINK_KICK_MAX = 3                # open attempts per load
LOG_FINGERPRINT_BYTES = 256
RENAME_LEDGER_FILE = "rename_ledger.json"
RENAME_BACKOFF_S = 600.0           # after a failed rename attempt for one (chat, title): wait this long
RENAME_MAX_FAILS = 3               # ... and give up after this many failures
RENAME_SCAN_EVERY_S = 45.0         # an empty scan of the record files is not repeated sooner
RENAME_LEDGER_KEEP = 400
RENAME_SOURCES = ("auto", "tool")  # titleSource values that never reached the phone as a real rename
RENAME_NOW_AFTER_S = 90.0          # a load must be this old: Desktop turns Remote Control on for each chat within ~1 min
RENAME_NOW_QUIET_S = 45.0          # ... and the rename candidates unchanged for this long
RENAME_NOW_RELINK_WAIT_S = 300.0   # wait this long (from the load) for the restart relink to finish first
RENAME_NOW_MAX_AGE_S = 1200.0      # a load older than this gets no pass (the idle path takes over)
RENAME_NOW_MAX_PASS_S = 600.0      # one pass never runs longer than this
RENAME_NOW_MAX_FAILS = 2           # failed renames in a row that end a pass
RENAME_NOW_FRONT_WAIT_S = 3.0      # how long Desktop gets to come forward
LOGIN_PROMPT_SPAWN_GRACE_S = 30.0  # a login prompt the helper just started counts as open before its marker exists
RENAME_NOW_GUARD_REASONS = ("rename-off", "no-window-platform", "desktop-not-in-front", "owner-active", "owner-input")


def _terminal(phase) -> bool:
    return core.load_phase_terminal(phase)


# ----------------------------------------------------------------- log file
class HelperLog:
    """Size-rotated plain text log in the helper folder (same idea as the chat sync watcher)."""

    def __init__(self):
        self.path = core.helper_dir() / "helper_log.txt"

    def say(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(self.path, str(self.path) + ".1")
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")


# ------------------------------------------------------------------ helper
class Helper:
    def __init__(self, clock=time.time, log: HelperLog | None = None):
        self.clock = clock
        self.log = log or HelperLog()
        self.started_s = clock()
        self.last_reconcile_s = 0.0
        self.last_health_s = 0.0
        self.account_unknown_since_s: float | None = None
        self.renamer = None                # tests inject a fake; None builds the real one when first needed
        self.last_rename_scan_s = 0.0
        self.last_nudge_s = 0.0
        self.sleep = time.sleep
        self.notice_factory = None         # tests inject a fake; None builds the real notice window handle
        self.window_platform_override = None  # tests inject a fake focus platform
        self.skip_reasons: dict = {}       # last logged skip reason per pass (log only a change)
        self.rename_now_keys: frozenset = frozenset()
        self.rename_now_keys_since_s = 0.0
        self.alert_spawn = None            # tests inject a fake (folder, load_id); None starts the real window process
        self.alert_empty: set = set()      # loads whose first look found nothing to relink (no alert for them)
        self.in_flight = False             # a Desktop load is being relinked: follow main.log and the list fast
        self.log_size_seen: int | None = None
        self.flag_written_s = 0.0
        self.copier = None                 # the chat copier (job j), built when first needed
        self.copy_seen_account = None      # the account the previous pass read: copy only when two passes agree
        self.startup_dir = None            # tests inject a temp folder; None = the real per-user Startup folder

    # ---- config and state
    def config(self) -> dict:
        value = core._read_json(core.helper_dir() / "config.json", {})
        return value if isinstance(value, dict) else {}

    def state(self) -> dict:
        return core.helper_state_load()

    def write_state(self, **fields) -> None:
        state = self.state()
        state.update(pid=os.getpid(), ts_ms=int(self.clock() * 1000), version=HELPER_VERSION, **fields)
        state.setdefault("started_ns", int(self.started_s * 1e9))
        core._write_json_atomic(core.helper_dir() / "state.json", state)

    # ---- (b) follow main.log
    def follow_log(self) -> int:
        """Read new lines of Desktop's main.log. Records each load line once. Returns the loads recorded."""
        path = core.desktop_log_path()
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        ledger = core.helper_loads_load()
        log = ledger["log"]
        first_start = "offset" not in log
        offset = int(log.get("offset") or 0)
        fingerprint = None
        if size >= LOG_FINGERPRINT_BYTES:
            with contextlib.suppress(OSError):
                with path.open("rb") as stream:
                    fingerprint = hashlib.sha256(stream.read(LOG_FINGERPRINT_BYTES)).hexdigest()[:16]
        if first_start:
            offset = max(0, size - core.LOG_TAIL_BYTES)
        elif log.get("path") != str(path) or size < offset or \
                (fingerprint and log.get("fp") and fingerprint != log["fp"]):
            offset = 0  # rotated or replaced: the new file is all new
        if size == offset and not first_start and log.get("path") == str(path):
            return 0
        bounded = size - offset > LOG_FOLLOW_MAX_BYTES  # a rotation, a replaced file or a long gap: keep the tail only
        if bounded:
            offset = size - LOG_FOLLOW_MAX_BYTES
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(max(0, size - offset))
        except OSError:
            return 0
        cut = data.rfind(b"\n") + 1
        lines = data[:cut].splitlines()
        if (first_start or bounded) and offset > 0 and lines:
            lines = lines[1:]  # the first line of a tail read is cut
        now_s = self.clock()
        found = []
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            match = core.LOADED_RE.search(line)
            if not match or int(match.group(1)) == 0:
                continue  # an empty folder is the in-between one Desktop passes through on a switch
            found.append({"load_id": core.desktop_load_id(line),
                          "account": match.group(3).lower(), "org": match.group(4).lower(),
                          "folder": match.group(2), "log_s": core.desktop_log_time(line)})
        new_log = {"path": str(path), "offset": offset + cut, "fp": fingerprint or log.get("fp"),
                   "updated_ns": int(now_s * 1e9)}

        def mutate(data):
            data["log"] = new_log
            added = 0
            for item in found:
                if item["load_id"] in data["loads"]:
                    continue
                phase = "pending"
                if first_start and (item["log_s"] is None or now_s - item["log_s"] > LOAD_FIRST_START_MAX_AGE_S):
                    phase = "skipped:too-old-at-first-start"
                data["loads"][item["load_id"]] = {
                    "account": item["account"], "org": item["org"], "folder": item.get("folder"),
                    "log_s": item["log_s"],
                    "seen_ns": int(now_s * 1e9) + added, "phase": phase, "reconnects": {}}
                added += 1
            return added

        added = core.helper_loads_update(mutate)
        if added:
            self.log.say(f"recorded {added} Desktop load(s) from main.log")
        return added

    # ---- (c) handle loads
    def candidates(self, load: dict, active: dict, load_id: str | None = None) -> list[dict]:
        """Active-account chats that need a restart relink for this load: Remote Control was ON at the end of the
        previous run of this account (Desktop's main.log), or the stored per-chat flag is true, or the chat is in the
        config's `always_remote` list; minus a stored flag that is false and minus chats whose Remote Control Desktop's
        main.log shows turned on after the load (a merely running or warmed chat is not skipped). The stored flag alone is not Desktop's rule
        (live 2026-10-13: global default and auto-enable turn it on for chats whose flag is blank)."""
        folder = Path(active["folder"])
        was_on = core.remote_on_before_load(load_id or "", active["id"])
        always = self.config().get("always_remote")
        was_on |= {item for item in always if isinstance(item, str)} if isinstance(always, list) else set()
        # A running process is NOT proof of a live link: a chat Desktop warmed or resumed after the restart starts
        # without Remote Control (live 2026-10-19). Only Desktop's own "Enabling remote control" line after the
        # load skips a chat; an unreadable log skips none.
        enabled_since = core.remote_enabled_since_load(load_id or "")
        found = []
        try:
            paths = sorted(folder.glob("local_*.json"))[:5000]
        except OSError:
            return []
        for path in paths:
            if not core.LOCAL_ID_RE.fullmatch(path.stem):
                continue
            record = core._read_json(path, None)
            if not isinstance(record, dict) or record.get("remoteControlUserEnabled") is False:
                continue
            if record.get("remoteControlUserEnabled") is not True and path.stem not in was_on:
                continue
            if record.get("isArchived") is True or record.get("scheduledTaskId"):
                continue
            session = record.get("cliSessionId")
            if not isinstance(session, str) or not core.UUID_RE.fullmatch(session):
                continue
            if core.desktop_check(f"claude:{session}") == "denied":
                continue
            if path.stem in enabled_since:
                continue  # Desktop itself turned its Remote Control back on after the load
            found.append({"desktop_id": path.stem, "session": session, "title": record.get("title")})
        return found

    @staticmethod
    def load_not_ready(age_s: float, folder_has_records: bool) -> str | None:
        """Why a load's relink list must wait, or None when Desktop is ready. This replaced the fixed 30 s settle
        wait. The load line says Desktop has read its persisted sessions; what is left is a short moment for the log
        to show a following load line (an account switch passes through an in-between load) and for the chat records
        to be on disk. Desktop does NOT turn Remote Control on for stopped chats by itself after a start (main.log,
        2026-10-26: no "Enabling remote control" line before the relink's own first send), so nothing else settles."""
        if age_s < LOAD_READY_MIN_S:
            return "settling"
        if not folder_has_records and age_s < LOAD_READY_MAX_WAIT_S:
            return "no-records"
        return None

    @staticmethod
    def folder_has_records(folder) -> bool:
        try:
            return next(iter(Path(folder).glob("local_*.json")), None) is not None
        except OSError:
            return False

    @staticmethod
    def load_in_flight(loads: dict, now_s: float) -> bool:
        """Is the newest Desktop load still being handled (pending, or relink entries queued or claimed) and young
        enough for the fast follow? Pure: the caller reads the ledger and passes it in."""
        newest = max(loads.values(), key=lambda item: item.get("seen_ns") or 0, default=None)
        if not newest or now_s - (newest.get("seen_ns") or 0) / 1e9 > FAST_FOLLOW_MAX_S:
            return False
        phase = newest.get("phase")
        if phase == "pending":
            return True
        return phase == "enqueued" and any(e.get("state") in ("queued", "claimed")
                                          for e in (newest.get("reconnects") or {}).values())

    def refresh_in_flight(self) -> bool:
        self.in_flight = self.load_in_flight(core.helper_loads_load()["loads"], self.clock())
        return self.in_flight

    def follow_delay(self) -> float:
        """How long the watch loop sleeps before its next light look."""
        return FAST_FOLLOW_S if self.in_flight else IDLE_LOG_POLL_S

    def log_changed(self) -> bool:
        """One stat of Desktop's main.log: did it grow (or get replaced) since the last look?"""
        try:
            size = core.desktop_log_path().stat().st_size
        except OSError:
            return False
        changed = self.log_size_seen is None or size != self.log_size_seen
        self.log_size_seen = size
        return changed

    def fast_step(self) -> dict:
        """The light look the watch loop makes between ticks: when main.log changed (a Desktop start) or a load is
        still being relinked, follow the log, handle loads and keep the relink list and flag current. The full tick
        (reconcile, wakes, nudges, rename, health) stays on its own TICK_S clock."""
        if not (self.log_changed() or self.in_flight):
            return {}
        summary: dict = {}
        with contextlib.suppress(Exception):
            summary["loads_recorded"] = self.follow_log()
        try:
            history = self.restore_switch_history()
            if history:
                summary["switch_history"] = history
        except Exception as error:
            self.log.say(f"restore_switch_history failed: {error!r}")
        try:
            summary["loads"] = self.process_loads()
        except Exception as error:
            self.log.say(f"process_loads failed: {error!r}")
        self.refresh_in_flight()
        return summary

    def process_loads(self) -> dict:
        now = self.clock()
        report = {"queued": 0, "handled": []}
        blocked = core.desktop_check(None)  # halt (and the deny list per chat); messaging is for letters only
        relink_off = self.config().get("relink") is False
        relink_blocked = blocked or ("relink-off" if relink_off else None)
        ledger = core.helper_loads_load()
        loads = ledger["loads"]
        newest = max((load.get("seen_ns") or 0 for load in loads.values()), default=0)
        active = core.desktop_active_account()
        for load_id, load in sorted(loads.items(), key=lambda item: item[1].get("seen_ns") or 0):
            phase = load.get("phase")
            if _terminal(phase):
                continue
            seen_s = (load.get("seen_ns") or 0) / 1e9
            if (load.get("seen_ns") or 0) < newest:
                self.set_phase(load_id, "superseded")
                continue
            age = now - seen_s
            if age < LOAD_READY_MIN_S:  # the list waits only for the log to say whether another load follows
                if phase == "pending":
                    self.ensure_alert(load_id, load, active, relink_blocked, age)
                continue
            if not active or active.get("id") != load.get("account"):
                self.set_phase(load_id, "skipped:not-active-account")
                continue
            if blocked:
                if now - seen_s > LOAD_PENDING_MAX_S:
                    self.set_phase(load_id, "expired:" + blocked)
                continue
            if phase == "pending":
                if self.load_not_ready(age, self.folder_has_records(active["folder"])):
                    self.ensure_alert(load_id, load, active, relink_blocked, age)
                    continue
                queued = 0
                if not relink_off:  # config `relink: false`: the load is still marked handled (rename-now needs that)
                    for chat in self.candidates(load, active, load_id):
                        result = core.relink_enqueue(load_id, chat["desktop_id"], chat["session"], chat.get("title"))
                        queued += 1 if result.get("queued") else 0
                        if result.get("why") in ("halted", "relink-off"):
                            break
                report["queued"] += queued
                self.mark_listed(load_id)
                self.log.say(f"load {load_id}: listed {queued} chat(s) for the restart relink "
                             f"({age:.1f} s after the load line)" + (" (relink is switched off)" if relink_off else ""))
                if queued:
                    self.ensure_alert(load_id, load, active, relink_blocked, age, listed=queued)
            report["handled"].append(load_id)
        core.relink_sweep(relink_blocked)
        self.finish_loads()
        self.sync_relink_flag()
        if not relink_blocked:
            kick = self.relink_kick()
            if kick:
                report["kick"] = kick
        return report

    # ---- the relink alert (slice 3B.18)
    def ensure_alert(self, load_id: str, load: dict, active, blocked, age_s: float, listed: int | None = None) -> str:
        """Show the relink alert once for this load: right away when the load line shows chats to relink (counted
        from the same candidate list the relink uses), or once the list was made. Not when config `relink_alert` is
        false, when halt (or `relink: false`) blocks the relink (nothing would happen, so nothing is announced), for a
        load older than ALERT_MAX_AGE_S, or for another account's load. Returns why not, or the start outcome."""
        if load.get("alert") is not None:
            return "already"
        if self.config().get("relink_alert") is False:
            return "config-off"
        if blocked:
            return "controls"
        if age_s > ALERT_MAX_AGE_S:
            return "too-old"
        if not active or active.get("id") != load.get("account"):
            return "not-active-account"
        if listed is None:  # the early look: nothing is queued yet, so count the chats the list will hold
            if load_id in self.alert_empty:
                return "nothing-to-relink"
            if not self.candidates(load, active, load_id):
                self.alert_empty.add(load_id)
                return "nothing-to-relink"
        return self.start_alert(load_id)

    def start_alert(self, load_id: str) -> str:
        """Start the window process. It is remembered in loads.json BEFORE it starts, so a window the owner clicked
        away is never started again for the same load."""
        stamp = int(self.clock() * 1e9)
        if not self._mark_load(load_id, "alert", {"state": "starting", "ns": stamp}, only_if_unset=True):
            return "already"
        outcome = "started"
        try:
            if self.alert_spawn is not None:
                self.alert_spawn(core.helper_dir(), load_id)
            elif not getattr(self.window_platform(), "available", False):
                outcome = "no-window-platform"  # --once, a test or a stray run: real windows are opt-in
            else:
                relink_alert.default_spawn(core.helper_dir(), load_id)
        except OSError as error:
            outcome = "spawn-failed:" + type(error).__name__
            self.log.say(f"relink alert did not start: {error!r}")
        self._mark_load(load_id, "alert", {"state": outcome, "ns": stamp})
        if outcome == "started":
            self.log.say(f"load {load_id}: relink alert shown")
        return outcome

    def _mark_load(self, load_id: str, key: str, value, only_if_unset: bool = False) -> bool:
        def mutate(data):
            load = data["loads"].get(load_id)
            if load is None or (only_if_unset and load.get(key) is not None):
                return False
            load[key] = value
            return True
        return bool(core.helper_loads_update(mutate))

    def mark_listed(self, load_id: str) -> None:
        """The list was made now (phase enqueued): the kick's clock starts here, not at the load line."""
        stamp = int(self.clock() * 1e9)

        def mutate(data):
            load = data["loads"].get(load_id)
            if load and load.get("phase") != "enqueued" and not _terminal(load.get("phase")):
                load["phase"] = "enqueued"
                load["listed_ns"] = stamp
        core.helper_loads_update(mutate)

    # ---- (i) switch history
    @staticmethod
    def chat_running(session: str, command_lines: list[str]) -> bool:
        registration = core.claude_registration(session) or {}
        return bool(registration.get("live")) or any(session in line for line in command_lines)

    @staticmethod
    def linked_accounts(listed) -> set[str]:
        """The account IDs config `switch_history` links: each entry is a Claude account ID or the email Crosstalk
        recorded for one (accounts.json). An email never seen in a Desktop chat links nothing yet."""
        if not isinstance(listed, list):
            return set()
        wanted = {str(item).strip().lower() for item in listed if isinstance(item, str) and item.strip()}
        emails = core.desktop_account_emails()
        return {account.lower() for account, email in emails.items() if email.lower() in wanted} \
            | {item for item in wanted if core.UUID_RE.fullmatch(item)}

    def restore_switch_history(self) -> dict | None:
        """Once per newest load, when it is a switch between two linked accounts: clean the relink candidates'
        transcripts before the relink restarts them (the list is made LOAD_READY_MIN_S after the load line, and this
        runs in the fast follow ahead of it). A switch to or from any other account is left alone."""
        config = self.config()
        linked = self.linked_accounts(config.get("switch_history"))
        if not linked or config.get("history_fix") is False or core.controls_load()["halt"]:
            return None
        loads = core.helper_loads_load()["loads"]
        if not loads:
            return None
        load_id = max(loads, key=lambda key: loads[key].get("seen_ns") or 0)
        load = loads[load_id]
        if load.get("phase") != "pending" or "history" in load:
            return None
        earlier = [item for item in loads.values() if (item.get("seen_ns") or 0) < (load.get("seen_ns") or 0)]
        previous = max(earlier, key=lambda item: item.get("seen_ns") or 0) if earlier else None
        active = core.desktop_active_account()
        report: dict = {"load": load_id, "chats": {}}
        if not previous:
            report["outcome"] = "skipped:no-previous-load"
        elif previous.get("account") == load.get("account"):
            report["outcome"] = "skipped:not-a-switch"
        elif str(previous.get("account")).lower() not in linked or str(load.get("account")).lower() not in linked:
            report["outcome"] = "skipped:not-linked"
        elif not active or active.get("id") != load.get("account") or not load.get("org"):
            report["outcome"] = "skipped:not-active-account"
        else:
            report["outcome"] = "done"
        self._set_history(load_id, dict(report, started_ns=int(self.clock() * 1e9)))
        if report["outcome"] != "done":
            return report
        cli_login = str(core.claude_account().get("id") or "").lower() or None
        new = (load["account"], load["org"])
        before = (previous["account"], previous["org"]) if previous.get("org") else None
        backups = core.helper_dir() / "history-backups"
        command_lines = switch_history.claude_command_lines()
        for chat in self.candidates(load, active, load_id):
            transcript = core.claude_transcript_path(chat["session"])
            if transcript is None:
                result = {"outcome": "skipped:no-transcript"}
            else:
                try:
                    result = switch_history.restore_chat(transcript, chat["session"], cli_login, new, before,
                                                         backups,
                                                         lambda sid: self.chat_running(sid, command_lines))
                except OSError as error:
                    result = {"outcome": "failed", "error": type(error).__name__}
            report["chats"][chat["desktop_id"]] = result
        self._set_history(load_id, report)
        cleaned = sum(1 for result in report["chats"].values() if result.get("outcome") == "cleaned")
        self.log.say(f"load {load_id}: switch history cleaned {cleaned} of {len(report['chats'])} chat(s): "
                     + ", ".join(f"{local}={result.get('outcome')}" for local, result in report["chats"].items()))
        return report

    def _set_history(self, load_id: str, report: dict) -> None:
        def mutate(data):
            target = data["loads"].get(load_id)
            if target is not None:
                target["history"] = report
        core.helper_loads_update(mutate)

    # ---- (j) chat copying between the linked accounts (slice 3B.19; default off)
    def chat_copy(self) -> dict:
        """Copy the active linked account's chat list into the other linked account's folder (crosstalk_chat_copy).
        Off unless config `chat_copy` is true; needs two linked accounts (`switch_history`); halt pauses it; refuses
        while the personal copier's Startup shortcut exists. It copies only after two passes in a row read the same
        loaded account (an account switch passes through an in-between folder). Messaging is not consulted."""
        def skip(reason: str) -> dict:
            self.note_skip("chat-copy", reason)
            return {"ran": False, "skipped": reason}
        config = self.config()
        if config.get("chat_copy") is not True:
            return {"ran": False, "skipped": "config-off"}  # the default: says nothing, reads nothing
        if core.desktop_check(None):
            return skip("halted")
        if chat_copying.personal_copier_present(self.startup_dir):
            return skip("personal-copier-present")  # one copier only: remove the personal Startup shortcut first
        active = core.desktop_active_account()
        previous, self.copy_seen_account = self.copy_seen_account, (active or {}).get("id")
        if not active:
            return skip("no-active-account")
        linked = self.linked_accounts(config.get("switch_history"))
        if len(linked) < 2:
            return skip("needs-two-linked-accounts")
        if self.copier is None:
            self.copier = chat_copying.ChatCopier(
                core.helper_dir() / "chat-copy", clock=self.clock, say=self.log.say,
                halted=lambda: core.desktop_check(None))
        for load in sorted(core.helper_loads_load()["loads"].values(),  # every folder the helper has seen Desktop load
                           key=lambda item: item.get("seen_ns") or 0):
            if load.get("folder") and load.get("account") in linked:
                self.copier.remember(load["account"], load.get("org"), core.desktop_real_path(load["folder"]))
        if previous != active.get("id"):
            return skip("account-settling")
        report = self.copier.run(active, linked, deletions=config.get("chat_copy_deletions") is not False,
                                 snapshots=config.get("chat_copy_snapshots") is not False)
        if report.get("state") == "ran":
            self.note_skip("chat-copy", None)
            return dict(report, ran=True)
        return skip(str(report.get("reason") or report.get("state")))

    def relink_kick(self) -> dict | None:
        """After a restart every chat is stopped, so no plugin may be running to claim the relink list (live
        2026-10-20: all six entries expired unclaimed). When the newest load's list is still wholly unclaimed
        RELINK_KICK_AFTER_S after it was listed, open ONE listed chat (the most recently active one) through the focus
        gate. Its plugin starts, claims the list and sends every other chat the reconnect text; those chats start and
        one of them claims the opener's own entry. At most RELINK_KICK_MAX opens per load, each remembered in
        loads.json before the launch and counted against the wake budget. Config `relink_kick: false` turns it off."""
        if self.config().get("relink_kick") is False or self.config().get("relink") is False:
            return None
        now = self.clock()
        loads = core.helper_loads_load()["loads"]
        if not loads:
            return None
        load_id = max(loads, key=lambda key: loads[key].get("seen_ns") or 0)
        load = loads[load_id]
        entries = load.get("reconnects") or {}
        if load.get("phase") != "enqueued" or not entries:
            return None
        queued = {local: e for local, e in entries.items() if e.get("state") == "queued"}
        if not queued or any(e.get("state") in ("claimed", "done", "failed") for e in entries.values()):
            return None  # nothing waits, or a claimant took the list
        kick = load.get("kick") if isinstance(load.get("kick"), dict) else {}
        attempts = int(kick.get("attempts") or 0)
        if attempts >= RELINK_KICK_MAX:
            return None
        listed_s = (load.get("listed_ns") or (load.get("seen_ns") or 0) + int(LOAD_READY_MIN_S * 1e9)) / 1e9
        due_s = listed_s + RELINK_KICK_AFTER_S if not attempts else int(kick.get("ns") or 0) / 1e9 + RELINK_KICK_RETRY_S
        if now < due_s:
            return None
        active = core.desktop_active_account()
        if not active or active.get("id") != load.get("account"):
            return None
        tried = set(kick.get("tried") or [])
        choices = []
        for local, entry in queued.items():
            session = entry.get("session")
            if not isinstance(session, str) or core.desktop_check(f"claude:{session}"):
                continue
            record = core._read_json(Path(active["folder"]) / f"{local}.json", None)
            if not isinstance(record, dict) or not core.wakeable_desktop_id((local, record)):
                continue
            # an untried chat first, then the most recently active one
            choices.append((local in tried, -int(record.get("lastActivityAt") or 0), local, session, entry.get("title")))
        if not choices:
            return None
        _, _, local, session, title = min(choices)
        budget = core.wake_budget_claim(f"claude:{session}", "claude-relink-kick", int(now * 1e9))
        if not budget["allowed"]:
            return {"load": load_id, "to": local, "launched": False, "error": budget["reason"]}

        def mutate(data):
            target = data["loads"].get(load_id)
            if target is not None:
                target["kick"] = {"attempts": attempts + 1, "ns": int(now * 1e9),
                                  "tried": sorted(tried | {local}), "last": local}
        core.helper_loads_update(mutate)
        shown = self.focus_open(session, title, "relink-kick", check=core.desktop_check)
        if shown.get("method") == "already-awake":
            core.wake_budget_refund(budget["event"])
        self.log.say(f"load {load_id}: no running chat claimed the relink list; opened chat {local} "
                     f"(attempt {attempts + 1}/{RELINK_KICK_MAX}): launched={bool(shown.get('launched'))} "
                     f"focus={shown.get('focus')} error={shown.get('error')}")
        return {"load": load_id, "to": local, "attempt": attempts + 1, "launched": bool(shown.get("launched")),
                "error": shown.get("error")}

    def focus_open(self, session: str, title, kind: str, check=None) -> dict:
        """Open one Claude chat in Desktop through the focus gate (`claude --desktop --resume`, hidden console).
        The gate notes the foreground window, shows the notice, gives focus back and confirms through Crosstalk
        itself (the chat's directory check-in). `check` is the controls check the gate asks: letters leave it unset
        (controls_check); the relink kick passes desktop_check. Returns the launch's own result shape."""
        exe = core.claude_exe()
        if not exe:
            return {"launched": False, "error": "claude-cli-not-found"}
        focus = core.focus_wake(
            f"claude:{session}", lambda: core.launch_desktop_show(exe, session),
            lambda start_ns: core.claude_checked_in(session, start_ns),
            text="Crosstalk: waking a Claude chat" + (f" \"{str(title)[:40]}\"" if title else "")
            + core.FOCUS_NOTICE_TAIL, kind=kind,
            awake=lambda: bool((core.claude_registration(session) or {}).get("live")), check=check)
        if focus.get("refused"):
            refused = str(focus["refused"])
            return {"launched": False, "error": "focus-refused:" + refused,
                    "transient": refused.startswith(TRANSIENT_REFUSALS)}  # nothing was launched; may be tried again
        if focus.get("reason") == "already-awake":  # it is running: nothing to open, and that is not a failure
            return {"launched": True, "method": "already-awake", "focus": {"reason": "already-awake"}}
        shown = dict(focus.get("action_result") or {"launched": False, "error": focus.get("error") or "not-launched"})
        shown["focus"] = {key: focus.get(key) for key in ("overlay", "came_forward", "restored", "restore_method",
                                                            "chat_restored", "confirmed", "elapsed_s", "reason")}
        return shown

    # ---- (e) wake an asleep Claude chat for a waiting letter (the only route to one)
    def letter_wakes(self) -> dict:
        value = self.state().get("letter_wakes")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def wake_due(row: dict | None, now_ns: int) -> bool:
        """May a wake be tried for this letter now? No row = never tried. After attempt k the next one waits
        WAKE_BACKOFF_S[k-1]; after WAKE_MAX_ATTEMPTS attempts the letter just waits in the mail slot."""
        if not row:
            return True
        attempts = int(row.get("attempts") or 1)  # a row from before the backoff counts as its one attempt
        if attempts >= core.WAKE_MAX_ATTEMPTS:
            return False
        return (now_ns - int(row.get("ns") or 0)) / 1e9 >= core.WAKE_BACKOFF_S[attempts - 1]

    def waiting_letter_chats(self, active: dict) -> dict[str, dict]:
        """session -> {desktop_id, title, letters} for letters to a Claude chat that is not running (blocked:not-running,
        held, or an uncertain mail-slot letter), whose Desktop record is in the active account folder (not archived,
        not a routine) and whose next wake attempt is due. Letters older than the mail slot's lifetime are not worth
        a wake."""
        done = self.letter_wakes()
        now_ns = int(self.clock() * 1e9)
        found: dict[str, dict] = {}
        skipped: set[str] = set()
        for letter in sorted(core.letters_load().values(), key=lambda row: row.get("created_ns") or 0):
            to = letter.get("to") or {}
            letter_id, session = letter.get("letter_id"), to.get("id")
            state = letter.get("state")
            waiting = state in ("blocked:not-running", "held") or (state == "uncertain" and letter.get("route") == "mail-slot")
            if to.get("app") != "claude" or not waiting:
                continue
            if not isinstance(session, str) or not core.UUID_RE.fullmatch(session) or session in skipped:
                continue
            if not self.wake_due(done.get(letter_id), now_ns):
                continue
            if (now_ns - int(letter.get("created_ns") or 0)) / 1e9 > core.MAIL_SLOT_TTL_S:
                continue
            if session not in found:
                registration = core.claude_registration(session)
                desktop_id = None
                if registration is None or not registration["live"]:  # running: not asleep, --resume would refuse
                    desktop_id = core.wakeable_desktop_id(core.desktop_record({"folder": active["folder"]}, session))
                if not desktop_id or core.controls_check(f"claude:{session}"):
                    skipped.add(session)
                    continue
                record = core._read_json(Path(active["folder"]) / f"{desktop_id}.json", {})
                found[session] = {"desktop_id": desktop_id, "letters": [],
                                  "title": record.get("title") if isinstance(record, dict) else None}
            found[session]["letters"].append(letter_id)
        return found

    def wake_for_waiting_letters(self) -> dict:
        """At most one chat wake per tick. Each attempt per letter is remembered in state.json before anything is
        launched, so a crash or a failed wake only counts, never loops; the backoff in wake_due spaces the retries.
        Several letters to one chat share the wake. Nothing here sends or resends a letter: the woken chat takes its
        letters from its mail slot when it starts."""
        report = {"woken": []}
        if self.config().get("letter_wake") is False or core.controls_check(None):
            return report
        active = core.desktop_active_account()
        if not active:
            return report
        waiting = self.waiting_letter_chats(active)
        if not waiting:
            return report
        # The wake budget (per chat and in total) decides before anything is written or launched. A chat over its
        # budget is skipped without using up a wake attempt: its letters just wait, and the next tick asks again.
        stamp = int(self.clock() * 1e9)
        session = chat = budget = None
        for name, candidate in waiting.items():
            budget = core.wake_budget_claim(f"claude:{name}", "claude-letter", stamp)
            if budget["allowed"]:
                session, chat = name, candidate
                break
            report.setdefault("budget_waiting", []).append(name)
            if budget["reason"] == "wake-budget-total":
                break  # no chat can be woken now
        if session is None:
            return report
        wakes = dict(self.letter_wakes())
        for letter_id in chat["letters"]:
            wakes[letter_id] = {"session": session, "ns": stamp,
                                "attempts": int((wakes.get(letter_id) or {}).get("attempts") or 0) + 1}
        for old in sorted(wakes, key=lambda key: wakes[key].get("ns") or 0)[:-200]:
            del wakes[old]
        self.write_state(letter_wakes=wakes)
        shown = self.focus_open(session, chat.get("title"), "letter-wake")
        if shown.get("method") == "already-awake":
            core.wake_budget_refund(budget["event"])  # it woke by itself first: nothing was woken, give the slot back
        report["woken"].append({"session": session, "letters": len(chat["letters"]),
                                "launched": bool(shown.get("launched")),
                                "attempts": max(wakes[letter_id]["attempts"] for letter_id in chat["letters"])})
        self.log.say(f"woke Claude chat {chat['desktop_id']} for {len(chat['letters'])} waiting letter(s): "
                     f"launched={bool(shown.get('launched'))} focus={shown.get('focus')} error={shown.get('error')}")
        return report

    def restart_codex_cleanup(self) -> dict:
        """Bookkeeping for headless Codex turns whose waiter died: one detached cleanup pass per such letter. It never
        sends or resends a letter (the pass only resumes the chat to prove the dead turn is over, then reads back).
        Halt or messaging off skips it, like every letter wake."""
        report = {"cleanup": []}
        if core.controls_check(None):
            return report
        for letter_id in core.codex_claims_needing_cleanup():
            core.spawn_codex_headless(letter_id, cleanup=True)
            report["cleanup"].append(letter_id)
            self.log.say(f"started the cleanup pass for headless Codex letter {letter_id} (its waiter is gone)")
        return report

    def retry_budget_codex_wakes(self) -> dict:
        """A Codex letter whose link open the wake budget held back is tried again once its chat has room in the budget:
        one detached waiter per tick, which reads back and opens the link under the same checks. Sends nothing."""
        report = {"retried": []}
        if core.controls_check(None):
            return report
        for letter_id in core.codex_budget_retry_candidates(int(self.clock() * 1e9)):
            if core.codex_budget_retry(letter_id):
                report["retried"].append(letter_id)
                self.log.say(f"the wake budget has room again: link waiter restarted for Codex letter {letter_id}")
                break
        return report

    def retry_stranded_codex_wakes(self) -> dict:
        """A held Codex letter whose link waiter vanished (no outcome journaled, pid dead) or skipped because the chat's
        turn was in progress is tried again: one detached waiter per tick, bounded per letter. Sends nothing."""
        report = {"respawned": []}
        if core.controls_check(None):
            return report
        for letter_id in core.codex_wake_stranded_candidates(int(self.clock() * 1e9)):
            if core.codex_wake_respawn(letter_id):
                report["respawned"].append(letter_id)
                self.log.say(f"link waiter gone without opening the chat: restarted for Codex letter {letter_id}")
                break
        return report

    def nudge_letters(self) -> dict:
        """Letters that sit accepted/held/uncertain: tell the SENDER once after core.NUDGE_AFTER_S (5 min) when a
        fresh readback still does not find the letter, and retire after 24 h with one final notice. The notice is a
        new Crosstalk letter to the sender, never a resend and never sent to the recipient (core.nudge_unconfirmed)."""
        if self.clock() - self.last_nudge_s < NUDGE_EVERY_S:
            return {"skipped": "not-due"}
        self.last_nudge_s = self.clock()
        report = core.nudge_unconfirmed(int(self.clock() * 1e9))
        for kind in ("nudged", "final", "retired"):
            if report.get(kind):
                self.log.say(f"unconfirmed letters {kind}: {report[kind]}")
        return report

    def wake_notice(self) -> None:
        """A letter whose wake attempts are used up waits in the mail slot (or ends expired-unsent at 24 h): say so.
        The wake budget's notice is aged out here too (it is opened when a wake is refused)."""
        with contextlib.suppress(Exception):
            core.wake_budget_notice_sync(int(self.clock() * 1e9))
        done = self.letter_wakes()
        stuck = [lid for lid, row in done.items() if int((row or {}).get("attempts") or 1) >= core.WAKE_MAX_ATTEMPTS
                 and (core.letters_load().get(lid) or {}).get("state") in ("blocked:not-running", "held", "uncertain")]
        if stuck:
            core.helper_notice_set("wake-gave-up", "wake-gave-up",
                                   f"{len(stuck)} letter(s) could not wake their chat after {core.WAKE_MAX_ATTEMPTS} "
                                   "tries. They wait in the chat's mail slot and arrive when the chat is opened "
                                   "(within 24 hours, then they end unsent).")
        else:
            core.helper_notice_clear("wake-gave-up")

    def set_phase(self, load_id: str, phase: str) -> None:
        def mutate(data):
            load = data["loads"].get(load_id)
            if load and load.get("phase") != phase and not _terminal(load.get("phase")):
                load["phase"] = phase
        core.helper_loads_update(mutate)

    def finish_loads(self) -> None:
        """An enqueued load is done once none of its relink entries is still queued or claimed."""
        def mutate(data):
            for load in data["loads"].values():
                if load.get("phase") == "enqueued" and not any(
                        e.get("state") in ("queued", "claimed") for e in (load.get("reconnects") or {}).values()):
                    load["phase"] = "done"
        core.helper_loads_update(mutate)

    def sync_relink_flag(self) -> None:
        """helper/relink-open.json lets the plugin poll for work with a file read instead of a process: it exists
        only while an entry of the newest load is queued and the controls allow it."""
        flag = core.relink_flag_path()
        queued = False
        if not core.desktop_check(None) and not core.relink_switched_off():
            loads = core.helper_loads_load()["loads"]
            newest = max(loads.values(), key=lambda item: item.get("seen_ns") or 0, default=None)
            queued = bool(newest and any(e.get("state") == "queued" for e in (newest.get("reconnects") or {}).values()))
        if queued:  # written when it is missing and refreshed rarely: the plugin polls it every second
            now = self.clock()
            if not flag.exists() or now - self.flag_written_s >= RELINK_FLAG_REFRESH_S:
                core._write_json_atomic(flag, {"ts_ms": int(now * 1000)})
                self.flag_written_s = now
        else:
            with contextlib.suppress(OSError):
                flag.unlink()

    # ---- (g) real names on the phone
    def get_renamer(self):
        if self.renamer is None:
            idle = self.config().get("rename_min_idle_s")
            options = {"min_idle_s": float(idle)} if isinstance(idle, (int, float)) and not isinstance(idle, bool) else {}
            self.renamer = renaming.default_renamer(core, **options)
        return self.renamer

    def rename_ledger(self) -> dict:
        value = core._read_json(core.helper_dir() / RENAME_LEDGER_FILE, {})
        value = value if isinstance(value, dict) else {}
        for key in ("chats", "restore"):
            if not isinstance(value.get(key), dict):
                value[key] = {}
        return value

    @staticmethod
    def rename_key(local_id: str, title: str, link: str | None = None) -> str:
        key = f"{local_id}|{hashlib.sha256(title.encode('utf-8')).hexdigest()[:12]}"
        return f"{key}|{link}" if link else key

    @staticmethod
    def link_key(local_id: str, link: str) -> str:
        """This remote session got the chat's name once (any title: a later rename by the owner reaches it anyway)."""
        return f"{local_id}|link|{link}"

    @staticmethod
    def remote_link(record: dict) -> str | None:
        """The chat's newest Remote Control session: Desktop appends a new one to bridgeSessionIds on every relink
        that cannot reattach (live 2026-10-24: an account switch gave each chat a new session_..., which claude.ai
        shows under a generated name with no earlier messages)."""
        links = record.get("bridgeSessionIds")
        link = links[-1] if isinstance(links, list) and links else None
        return link if isinstance(link, str) and link.startswith("session_") else None

    @staticmethod
    def rename_due(row: dict | None, now_s: float) -> bool:
        """May this ledger row be tried now? No row = never tried. Done rows and given-up rows are not due."""
        if not row:
            return True
        if row.get("ok_ns") or int(row.get("fails") or 0) >= RENAME_MAX_FAILS:
            return False
        return now_s - (row.get("last_fail_ns") or 0) / 1e9 >= RENAME_BACKOFF_S

    @staticmethod
    def remote_on_now(record: dict, local_id: str, logged: dict) -> bool:
        """Remote Control on for this chat now: the stored flag, else the last Enabling/Disabling line Desktop logged
        for it in its current run (a stored false always wins)."""
        flag = record.get("remoteControlUserEnabled")
        if flag is False:
            return False
        return flag is True or logged.get(local_id) is True

    def rename_candidates(self, active: dict) -> list[dict]:
        folder = Path(active["folder"])
        logged: dict = {}
        named: set = set()
        with contextlib.suppress(Exception):
            mine = [run for run in core.desktop_remote_runs() if run["account"] == str(active["id"]).lower()]
            logged, named = (mine[-1]["remote"], mine[-1]["named"]) if mine else ({}, set())
        found = []
        try:
            paths = sorted(folder.glob("local_*.json"))[:5000]
        except OSError:
            return []
        for path in paths:
            if not core.LOCAL_ID_RE.fullmatch(path.stem):
                continue
            record = core._read_json(path, None)
            if not isinstance(record, dict) or record.get("isArchived") is True or record.get("scheduledTaskId"):
                continue
            title = record.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            link = self.remote_link(record)
            if record.get("titleSource") in RENAME_SOURCES:
                key = self.rename_key(path.stem, title, link)
            elif link and path.stem not in named:  # an owner name reaches only the remote session that existed then;
                key = self.link_key(path.stem, link)   # a Desktop rename since this run's relink already reached it
            else:
                continue
            if not self.remote_on_now(record, path.stem, logged):
                continue
            session = record.get("cliSessionId")
            if isinstance(session, str) and core.desktop_check(f"claude:{session}") == "denied":
                continue
            found.append({"local": path.stem, "title": title, "key": key, "link": link})
        return found

    def rename_pending_restore(self, ledger: dict, renamer, now_s: float) -> dict | None:
        """A chat a failed push left with the temporary name: put its title back, but only while the record still
        shows exactly that temporary name (a name the owner has typed since is never overwritten)."""
        for local_id, row in sorted(ledger["restore"].items()):
            if not self.rename_due(row, now_s):
                continue
            record = None
            with contextlib.suppress(Exception):
                record = renamer.records(local_id)
            title = row.get("title")
            if not record or not isinstance(title, str) or record.get("title") != renaming.temp_title(title):
                ledger["restore"].pop(local_id, None)  # fixed already, or the owner chose another name
                core._write_json_atomic(core.helper_dir() / RENAME_LEDGER_FILE, ledger)
                continue
            return {"local": local_id, "title": title, "key": None, "restore": True}
        return None

    def note_skip(self, source: str, reason: str | None) -> None:
        """One log line when the skip reason of a pass CHANGES (never one per tick). `None` (it did something) resets,
        so the next skip is logged again. A throttled scan says nothing new."""
        if reason == "scan-throttled" or self.skip_reasons.get(source) == reason:
            return
        self.skip_reasons[source] = reason
        if reason is not None:
            self.log.say(f"{source} skipped: {reason}")

    def rename_chats(self) -> dict:
        report = self._rename_chats()
        self.note_skip("rename", report.get("skipped"))
        return report

    def _log_rename(self, pick: dict, result: dict, source: str = "rename") -> None:
        self.log.say(f"{source} {pick['local']}: outcome={result.get('outcome')} reason={result.get('reason')} "
                     f"push={result.get('push')} temp_state={result.get('temp_state')} "
                     f"title_left={result.get('title_left')} "
                     f"chat_restore_needed={result.get('chat_restore_needed')} "
                     f"chat_restored={result.get('chat_restored')}")

    def _rename_chats(self) -> dict:
        """At most one rename attempt per tick, and only while Desktop is already in front and the owner is idle.
        A temporary " ." title left by a failed second step of a push is put right first."""
        report: dict = {"attempted": 0}
        if self.config().get("rename_titles") is False:
            return dict(report, skipped="config-off")
        if core.desktop_check(None):
            return dict(report, skipped="controls")
        renamer = self.get_renamer()
        gate = renamer.guards()
        if not gate["ok"]:
            return dict(report, skipped=gate["reason"])
        active = core.desktop_active_account()
        if not active:
            return dict(report, skipped="no-active-account")
        now_s = self.clock()
        ledger = self.rename_ledger()
        pick = self.rename_pending_restore(ledger, renamer, now_s)
        if pick is None:
            if now_s - self.last_rename_scan_s < RENAME_SCAN_EVERY_S:
                return dict(report, skipped="scan-throttled")
            pick = next((c for c in self.rename_candidates(active)
                         if self.rename_due(ledger["chats"].get(c["key"]), now_s)), None)
            if pick is None:
                self.last_rename_scan_s = now_s  # an empty scan is not repeated every tick
                return dict(report, skipped="nothing-to-rename")
        result = renamer.rename(pick["local"], pick["title"])
        report.update(local=pick["local"], outcome=result.get("outcome"),
                      attempted=1 if result.get("attempted") else 0)
        if result.get("attempted"):
            self.rename_record(pick, result, now_s)
            self._log_rename(pick, result)
        return report

    def rename_record(self, pick: dict, result: dict, now_s: float) -> None:
        ledger = self.rename_ledger()
        stamp = int(now_s * 1e9)
        restoring = bool(pick.get("restore"))
        rows, key = (ledger["restore"], pick["local"]) if restoring else (ledger["chats"], pick["key"])
        row = dict(rows.get(key) or {})
        if result.get("ok"):
            row.update(ok_ns=stamp)
        else:
            row.update(fails=int(row.get("fails") or 0) + 1, last_fail_ns=stamp)
        if restoring:
            row["title"] = pick["title"]
            if result.get("ok"):
                rows.pop(key, None)
            else:
                rows[key] = row
        else:
            rows[key] = row
            if result.get("ok") and pick.get("link"):  # its new user title must not trigger a second push
                rows[self.link_key(pick["local"], pick["link"])] = {"ok_ns": stamp}
            if result.get("temp_state"):  # the push left this chat with the temporary name
                ledger["restore"][pick["local"]] = {"title": pick["title"], "fails": 0}
        chats = ledger["chats"]
        for old in sorted(chats, key=lambda k: chats[k].get("ok_ns") or chats[k].get("last_fail_ns") or 0)[
                :-RENAME_LEDGER_KEEP]:
            del chats[old]
        core._write_json_atomic(core.helper_dir() / RENAME_LEDGER_FILE, ledger)
        stuck = [row for row in ledger["restore"].values() if int(row.get("fails") or 0) >= RENAME_MAX_FAILS]
        if stuck:
            core.helper_notice_set("rename-temp-title", "rename-temp-title",
                                   f"{len(stuck)} chat(s) were left with a temporary name ending in a middle dot "
                                   "after a failed rename. Rename them by hand in Claude Desktop.")
        else:
            core.helper_notice_clear("rename-temp-title")

    # ---- (g2) rename-now pass: right after a Desktop load, with the owner told and Desktop brought forward
    def login_prompt_open(self, now_s: float) -> bool:
        """The CLI-login Yes/No box (or the sign-in after Yes) is up, or the helper just started it."""
        spawned = self.state().get("login_prompt_spawn_ns")
        if isinstance(spawned, (int, float)) and not isinstance(spawned, bool) \
                and 0 <= now_s - spawned / 1e9 < LOGIN_PROMPT_SPAWN_GRACE_S:
            return True
        return core.login_prompt_open(now_s)

    def window_platform(self):
        if self.window_platform_override is not None:
            return self.window_platform_override
        return core._focus_module().default_platform()

    def _mark_rename_now(self, load_id: str, value: dict, only_if_unset: bool = False) -> bool:
        def mutate(data):
            load = data["loads"].get(load_id)
            if load is None or (only_if_unset and load.get("rename_now") is not None):
                return False
            load["rename_now"] = value
            return True
        return bool(core.helper_loads_update(mutate))

    def rename_now(self) -> dict:
        """The once-per-load rename-now pass (see the module docstring, g2). Returns {"ran": False, "skipped": why}
        or the pass report. Cheap when nothing is due: a few file reads."""
        def skip(reason: str) -> dict:
            self.note_skip("rename-now", reason)
            return {"ran": False, "skipped": reason}
        config = self.config()
        if config.get("rename_now") is False or config.get("rename_titles") is False:
            return skip("config-off")
        if renaming.rename_off():
            return skip("rename-off")
        if core.desktop_check(None):
            return skip("controls")
        loads = core.helper_loads_load()["loads"]
        if not loads:
            return skip("no-load")
        load_id = max(loads, key=lambda key: loads[key].get("seen_ns") or 0)
        load = loads[load_id]
        if load.get("rename_now") is not None:
            return skip("already-run")
        if load.get("phase") not in ("enqueued", "done"):
            return skip("load-not-ready")  # still settling, or skipped/superseded/expired: no pass for it
        now_s = self.clock()
        age = now_s - (load.get("seen_ns") or 0) / 1e9
        if age > RENAME_NOW_MAX_AGE_S:
            self._mark_rename_now(load_id, {"state": "skipped:too-old"}, only_if_unset=True)
            return skip("too-old")
        if age < RENAME_NOW_AFTER_S:
            return skip("settling")
        active = core.desktop_active_account()
        if not active or active.get("id") != load.get("account"):
            return skip("not-active-account")
        if age < RENAME_NOW_RELINK_WAIT_S and any(
                e.get("state") in ("queued", "claimed") for e in (load.get("reconnects") or {}).values()):
            return skip("relink-running")
        renamer = self.get_renamer()
        window = self.window_platform()
        if not getattr(renamer.platform, "available", False) or not getattr(window, "available", False):
            return skip("no-window-platform")
        if self.login_prompt_open(now_s):
            return skip("login-prompt-open")
        ledger = self.rename_ledger()
        picks = [c for c in self.rename_candidates(active) if self.rename_due(ledger["chats"].get(c["key"]), now_s)]
        restore = self.rename_pending_restore(ledger, renamer, now_s)
        keys = frozenset(c["key"] for c in picks) | ({"restore:" + restore["local"]} if restore else set())
        if not keys:
            self.rename_now_keys = keys
            return skip("nothing-to-rename")
        if keys != self.rename_now_keys:  # Desktop is still turning Remote Control on chat by chat
            self.rename_now_keys, self.rename_now_keys_since_s = keys, now_s
        if now_s - self.rename_now_keys_since_s < RENAME_NOW_QUIET_S:
            return skip("candidates-changing")
        if not self._mark_rename_now(load_id, {"state": "running", "start_ns": int(now_s * 1e9), "total": len(keys)},
                                     only_if_unset=True):
            return skip("already-run")  # remembered BEFORE anything is shown: a crash never repeats the pass
        report = self._run_rename_now(load_id, active, renamer, window, len(keys))
        self.note_skip("rename-now", None)
        return report

    def _wait_front(self, window, hwnd) -> bool:
        for _ in range(max(1, int(RENAME_NOW_FRONT_WAIT_S / 0.25))):
            if window.foreground().get("hwnd") == hwnd:
                return True
            self.sleep(0.25)
        return window.foreground().get("hwnd") == hwnd

    def _stop_reason(self, notice, started_s: float) -> str | None:
        """Why the pass must stop before the next chat, or None."""
        config = self.config()
        if notice.cancelled():
            return "cancelled"
        if self.clock() - started_s > RENAME_NOW_MAX_PASS_S:
            return "time-limit"
        if core.desktop_check(None):
            return "controls"
        if config.get("rename_titles") is False or config.get("rename_now") is False:
            return "config-off"
        if renaming.rename_off():
            return "rename-off"
        return None

    def _run_rename_now(self, load_id: str, active: dict, renamer, window, total: int) -> dict:
        focus = core._focus_module()
        started_s = self.clock()
        stats = {"total": total, "attempted": 0, "renamed": 0, "fails_in_a_row": 0}
        stop = None
        notice = self.notice_factory() if self.notice_factory else rename_notice.Notice(core.helper_dir(),
                                                                                       sleep=self.sleep)
        shown = False
        try:
            shown = bool(notice.show(total))
            if not shown:
                stop = "notice-not-shown"  # never type unannounced; the idle path takes over
                return {"ran": False, "skipped": stop}
            hwnd = focus.desktop_main_window(window)
            if hwnd is None:
                stop = "no-desktop-window"
            else:
                window.allow_any_foreground()
                window.bring_forward(hwnd)
                if not self._wait_front(window, hwnd):
                    stop = "desktop-not-forward"
            tick = renamer.platform.last_input_tick() if stop is None else None
            if stop is None and tick is None:
                stop = "input-unreadable"
            tried: set = set()
            for _ in range(total + 5):  # never loops on a pick that cannot be tried again
                stop = stop or self._stop_reason(notice, started_s)
                if stop is not None:
                    break
                now_s = self.clock()
                ledger = self.rename_ledger()
                pick = self.rename_pending_restore(ledger, renamer, now_s)
                if pick is None:
                    pick = next((c for c in self.rename_candidates(active) if c["key"] not in tried
                                 and self.rename_due(ledger["chats"].get(c["key"]), now_s)), None)
                if pick is None:
                    stop = "finished"
                    break
                gate = renamer.guards(since_tick=tick)
                if not gate["ok"] and gate["reason"] == "desktop-not-in-front" and hwnd is not None:
                    window.bring_forward(hwnd)  # once: something else came forward and the owner has not typed
                    self._wait_front(window, hwnd)
                    gate = renamer.guards(since_tick=tick)
                if not gate["ok"]:
                    stop = str(gate["reason"])
                    break
                notice.update(rename_notice.notice_text(total) + f"\n\nChat {stats['attempted'] + 1} of {total}...")
                result = renamer.rename(pick["local"], pick["title"], since_tick=tick)
                reason = str(result.get("reason"))
                if result.get("attempted"):
                    stats["attempted"] += 1
                    self.rename_record(pick, result, self.clock())
                    self._log_rename(pick, result, source="rename-now")
                    stats["renamed"] += 1 if result.get("ok") else 0
                    stats["fails_in_a_row"] = 0 if result.get("ok") else stats["fails_in_a_row"] + 1
                else:
                    tried.add(pick.get("key") or "restore:" + pick["local"])
                if result.get("input_tick") is not None:
                    tick = result["input_tick"]
                self.write_state(rename_now=dict(stats, load=load_id))  # heartbeat: the tick is blocked meanwhile
                if not result.get("attempted") and (reason in RENAME_NOW_GUARD_REASONS
                                                    or reason.startswith("controls:")):
                    stop = reason
                elif stats["fails_in_a_row"] >= RENAME_NOW_MAX_FAILS:
                    stop = "failed-twice"
            stop = stop or "loop-limit"
        except Exception as error:
            stop = "error:" + type(error).__name__
            self.log.say(f"rename-now {load_id} failed: {error!r}")
        finally:
            if shown:
                done = stop == "finished"
                text = (f"All set: {stats['renamed']} of {stats['total']} chat(s) renamed." if done
                        else "Cancelled. The rest are fixed later, when you are not busy." if stop == "cancelled"
                        else f"Stopped ({stop}). The rest are fixed later, when you are not busy.")
                with contextlib.suppress(Exception):
                    notice.close("done" if done else "cancelled" if stop == "cancelled" else "failed", text)
            self._mark_rename_now(load_id, dict(stats, state="done" if stop == "finished" else "stopped:" + str(stop),
                                                start_ns=int(started_s * 1e9), end_ns=int(self.clock() * 1e9)))
            self.last_rename_scan_s = 0.0  # the idle path may look at what is left right away
            self.log.say(f"rename-now {load_id}: {'finished' if stop == 'finished' else 'stopped: ' + str(stop)}; "
                         f"attempted {stats['attempted']}, renamed {stats['renamed']} of {total}")
        return dict(stats, ran=True, stop=stop)

    # ---- (d) health
    def health(self) -> None:
        notice, clear = core.helper_notice_set, core.helper_notice_clear
        now = self.clock()
        # accounts
        check = core.account_check()
        unknown = check["state"] == "unknown"
        self.account_unknown_since_s = (self.account_unknown_since_s or now) if unknown else None
        for state in ("mismatch", "unknown"):
            if check["state"] == state and (state != "unknown"
                                            or now - self.account_unknown_since_s >= ACCOUNT_UNKNOWN_NOTICE_AFTER_S):
                notice("account-" + state, "account-" + state, check.get("message", ""))
            elif check["state"] != state:
                clear("account-" + state)
        self.offer_login(check)
        # plugin version
        stale = self.plugin_stale()
        if stale:
            notice("plugin-stale", "plugin-stale", stale)
        else:
            clear("plugin-stale")
        # Remote Control flags that differ between the two account folders
        differing = self.remote_flag_differences()
        if differing:
            notice("remote-flag-differs", "remote-flag-differs",
                   f"{len(differing)} chat(s) have Remote Control on for one account and off for the other: "
                   + ", ".join(differing[:5]) + ". Chat sync copies the setting, so they differ only until the "
                   "next sync. Your decision which is right.")
        else:
            clear("remote-flag-differs")
        # the permission rule the restart relink needs, and what the newest load's relink did
        self.permission_notice()
        self.relink_notice()
        self.wake_notice()
        self.hermes_link_notice()

    def hermes_link_notice(self) -> None:
        """Slice 3B.17: the Hermes link pass. It refreshes link-hermes.json, re-checks the Hermes internals when
        Hermes or the plugin's list changed, and runs the self-test ONCE per Hermes version change or newly
        incompatible status. One notice says what is wrong or that Hermes updated; it clears when the link is fine."""
        notice, clear = core.helper_notice_set, core.helper_notice_clear
        try:
            result = core.hermes_link_pass()
        except Exception as error:
            self.log.say(f"hermes link pass failed: {error!r}")
            return
        if result.get("skipped"):
            clear("hermes-link")
            return
        if result.get("selftest_ran"):
            self.log.say(f"hermes link self-test ran ({result.get('trigger')}): "
                         f"{'passed' if result.get('selftest_ok') else 'did not pass'}")
        link = core.hermes_link_load()
        problems = core._link_problems(link)
        if problems:
            notice("hermes-link", "hermes-link", "The Crosstalk link to Hermes is broken: "
                   + problems[0]["text"] + ((" A repair has been offered (crosstalk_p0_cli.py hermes-repair)."
                                             if (link.get("repair") or {}).get("state") == "offered" else "")))
        elif result.get("version_notice"):
            notice("hermes-link", "hermes-link", result["version_notice"]["text"]
                   + ". Run hermes-selftest to confirm Crosstalk still works with it.")
        else:
            clear("hermes-link")

    def offer_login(self, check: dict) -> None:
        """Owner request 2026-10-13: on a mismatch, ask once per (Desktop, CLI) pair whether to log the CLI in to the
        Desktop account. Remembered before the box opens; asked again only after the accounts matched once. Halt skips
        it (messaging off does not: it is for letters). The helper never logs in or reads a credential: the owner finishes the login."""
        state = check.get("state")
        if state != "mismatch":
            if state == "match" and self.state().get("login_prompted"):
                self.write_state(login_prompted=None)
            return  # unknown (as during a switch) keeps the remembered pair
        pair = f"{check.get('desktop')}>{check.get('cli')}"
        if self.config().get("login_prompt") is False or core.desktop_check(None) or \
                self.state().get("login_prompted") == pair:
            return
        self.write_state(login_prompted=pair, login_prompt_spawn_ns=int(self.clock() * 1e9))
        try:
            core.spawn_login_prompt()
        except OSError as error:
            self.log.say(f"login prompt did not start: {error!r}")
        else:
            self.log.say(f"asked whether to log the Claude CLI in to the Desktop account ({pair})")

    def plugin_stale(self) -> str | None:
        try:
            import crosstalk_p0_install as inst
            checkout = core._read_json(inst.plugin_dir() / "plugin" / ".claude-plugin" / "plugin.json", {})
            expected = checkout.get("version")
            installed = core._read_json(core.claude_home() / "plugins" / "installed_plugins.json", {})
            entries = [e for e in (installed.get("plugins", {}).get(inst.PLUGIN_ID) or [])
                       if isinstance(e, dict) and e.get("scope") == "user"]
            if not expected or len(entries) != 1:
                return None if not expected else "The Crosstalk plugin is not installed for this user."
            if entries[0].get("version") != expected:
                return (f"Installed Crosstalk plugin is {entries[0].get('version')} but this checkout builds "
                        f"{expected}. Run the installer's upgrade, then restart Claude Desktop.")
            rendered = inst.plugin_dir() / "plugin" / "hooks" / "register.mjs"
            cached = Path(entries[0].get("installPath", "")) / "hooks" / "register.mjs"
            have, want = inst.sha256_file(cached), inst.sha256_file(rendered)
            if have and want and have != want:
                return "The cached Crosstalk plugin differs from this checkout's build. Run the installer's upgrade."
        except Exception:
            return None  # unknown stays unknown; no notice either way
        return None

    def remote_flag_differences(self) -> list[str]:
        base = core.desktop_sessions_base()
        flags: dict[str, dict[str, tuple]] = {}
        try:
            for account in sorted(p for p in base.iterdir() if p.is_dir() and core.UUID_RE.fullmatch(p.name)):
                for org in sorted(p for p in account.iterdir() if p.is_dir()):
                    for path in sorted(org.glob("local_*.json"))[:5000]:
                        record = core._read_json(path, None)
                        if isinstance(record, dict) and record.get("isArchived") is not True:
                            flags.setdefault(path.stem, {})[account.name] = (
                                record.get("remoteControlUserEnabled") is True, record.get("title"))
        except OSError:
            return []
        return sorted(str(next(iter(per.values()))[1] or chat)[:40] for chat, per in flags.items()
                      if len(per) >= 2 and len({value[0] for value in per.values()}) > 1)

    def permission_notice(self) -> None:
        """The restart relink sends through Desktop's send_message, which needs the installer's allow rule. Read-only
        view of ~/.claude/settings.json; an unreadable file raises nothing."""
        try:
            import crosstalk_p0_install as inst
            status = inst.permission_rules_status()
        except Exception:
            return
        missing = status.get("missing") or []
        if missing:
            core.helper_notice_set("permission-rules-missing", "permission-rules-missing",
                                   "Crosstalk needs permission rule(s) the installer has not added: "
                                   + ", ".join(missing) + ". Until then the restart relink and the Remote Control "
                                   "switch-on after a letter wake do nothing; type in a chat to bring its link back.")
        else:
            core.helper_notice_clear("permission-rules-missing")

    def relink_notice(self) -> None:
        loads = core.helper_loads_load()["loads"]
        newest_id = max(loads, key=lambda key: loads[key].get("seen_ns") or 0, default=None)
        for load_id, load in loads.items():
            key = "reconnect-failed:" + load_id
            bad = [e for e in (load.get("reconnects") or {}).values() if e.get("state") in ("failed", "expired")]
            if load_id == newest_id and bad:
                names = ", ".join(str(e.get("title") or e.get("session"))[:30] for e in bad[:4])
                needs_rule = any("permission" in str(e.get("reason")) for e in bad)
                core.helper_notice_set(key, "reconnect-failed",
                                       f"{len(bad)} chat(s) did not get their Remote Control relink after the last "
                                       f"Desktop start: {names}. "
                                       + ("The restart relink needs the installer's permission rule for "
                                          f"{RELINK_RULE}. " if needs_rule else "")
                                       + "Type in them in Desktop to bring the link back. Not retried.")
            else:
                core.helper_notice_clear(key)
        # A notice whose load left the ledger (it keeps only the newest few) can no longer be checked above: it is
        # superseded by definition, so it must not linger in notices.json.
        for key in list(core.helper_notices_load()["notices"]):
            if key.startswith("reconnect-failed:") and key[len("reconnect-failed:"):] not in loads:
                core.helper_notice_clear(key)

    # ---- one tick
    def tick(self) -> dict:
        now = self.clock()
        summary = {"ts": int(now), "loads_recorded": 0}
        blocked = core.controls_check(None)
        summary["controls"] = blocked  # what stops letters (halt, messaging off)
        summary["desktop_controls"] = core.desktop_check(None)  # what stops the Desktop jobs (halt only)
        with contextlib.suppress(Exception):
            summary["loads_recorded"] = self.follow_log()
        try:
            history = self.restore_switch_history()
            if history:
                summary["switch_history"] = history
        except Exception as error:
            summary["switch_history_error"] = type(error).__name__
            self.log.say(f"restore_switch_history failed: {error!r}")
        try:
            summary["codex_join"] = core.codex_join_scan()
        except Exception as error:
            summary["codex_join_error"] = type(error).__name__
            self.log.say(f"codex_join_scan failed: {error!r}")
        try:
            summary["loads"] = self.process_loads()
        except Exception as error:
            summary["loads_error"] = type(error).__name__
            self.log.say(f"process_loads failed: {error!r}")
        try:
            summary["letter_wake"] = self.wake_for_waiting_letters()
        except Exception as error:
            summary["letter_wake_error"] = type(error).__name__
            self.log.say(f"wake_for_waiting_letters failed: {error!r}")
        try:
            summary["nudge"] = self.nudge_letters()
        except Exception as error:
            summary["nudge_error"] = type(error).__name__
            self.log.say(f"nudge_letters failed: {error!r}")
        try:
            summary["codex_budget_retry"] = self.retry_budget_codex_wakes()
        except Exception as error:
            summary["codex_budget_retry_error"] = type(error).__name__
            self.log.say(f"retry_budget_codex_wakes failed: {error!r}")
        try:
            summary["codex_wake_respawn"] = self.retry_stranded_codex_wakes()
        except Exception as error:
            summary["codex_wake_respawn_error"] = type(error).__name__
            self.log.say(f"retry_stranded_codex_wakes failed: {error!r}")
        try:
            summary["codex_cleanup"] = self.restart_codex_cleanup()
        except Exception as error:
            summary["codex_cleanup_error"] = type(error).__name__
            self.log.say(f"restart_codex_cleanup failed: {error!r}")
        try:
            summary["chat_copy"] = self.chat_copy()
        except Exception as error:
            summary["chat_copy_error"] = type(error).__name__
            self.log.say(f"chat_copy failed: {error!r}")
        passed = False
        try:
            summary["rename_now"] = self.rename_now()
            passed = bool(summary["rename_now"].get("ran"))
        except Exception as error:
            summary["rename_now_error"] = type(error).__name__
            self.log.say(f"rename_now failed: {error!r}")
        try:
            if not passed:  # a pass just renamed everything it could; the idle path looks again next tick
                summary["rename"] = self.rename_chats()
        except Exception as error:
            summary["rename_error"] = type(error).__name__
            self.log.say(f"rename_chats failed: {error!r}")
        if now - self.last_reconcile_s >= RECONCILE_EVERY_S:
            self.last_reconcile_s = now
            try:
                report = core.reconcile(resend=False)  # readback only: sends, wakes and opens nothing
                summary["reconcile"] = {k: report[k] for k in ("checked", "readback_checked", "readback_deferred")}
                summary["reconcile"]["promoted"] = len(report["promoted"])
            except Exception as error:
                summary["reconcile_error"] = type(error).__name__
        if now - self.last_health_s >= HEALTH_EVERY_S:
            self.last_health_s = now
            try:
                self.health()
            except Exception as error:
                summary["health_error"] = type(error).__name__
                self.log.say(f"health failed: {error!r}")
        with contextlib.suppress(Exception):
            self.refresh_in_flight()
        self.write_state(last_tick=summary)
        return summary


# ----------------------------------------------------------------------- CLI
def run_locked(action):
    """Run action() while holding the helper's single-instance lock; None when another helper holds it."""
    try:
        with core.locked("helper-watch", timeout=0.0):
            return action()
    except TimeoutError:
        return None


def out(value: dict, code: int = 0) -> int:
    value = dict(value)
    value.setdefault("tag", TAG)
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def watch(max_seconds: float = 0.0) -> int:
    helper = Helper()
    helper.log.say(f"helper {HELPER_VERSION} started pid {os.getpid()}")
    started = time.monotonic()
    next_tick = started
    while not max_seconds or time.monotonic() - started < max_seconds:
        if time.monotonic() >= next_tick:
            try:
                helper.tick()
            except Exception as error:  # keep running; log and try again next tick
                helper.log.say(f"tick failed: {error!r}")
            next_tick = time.monotonic() + TICK_S
        else:  # the light look between ticks: a Desktop start is seen within a moment, a relink is followed closely
            try:
                helper.fast_step()
            except Exception as error:
                helper.log.say(f"fast step failed: {error!r}")
        time.sleep(max(0.05, min(helper.follow_delay(), next_tick - time.monotonic())))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crosstalk_helper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one tick now")
    mode.add_argument("--watch", action="store_true", help="keep running (started at login)")
    mode.add_argument("--status", action="store_true", help="read-only view of the helper's state")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="tests only: stop watching after this")
    args = parser.parse_args(argv)
    if args.status:
        return out(dict(core.helper_summary(), ok=True, device_folder=str(core.device_dir()),
                        lock_free=run_locked(lambda: True) is True))
    if args.once:
        summary = run_locked(lambda: Helper().tick())
        if summary is None:
            return out({"ok": True, "ran": False, "reason": "another-helper-is-running"})
        return out({"ok": True, "ran": True, "tick": summary})
    core.enable_real_focus()  # the watching helper is the one process that really wakes chats
    done = run_locked(lambda: watch(args.max_seconds))
    if done is None:
        return out({"ok": True, "ran": False, "reason": "another-helper-is-running"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
