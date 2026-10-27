"""Keep Remote Control history across a Claude Desktop account switch (slice 3B.16, opt-in, owner request 2026-10-24).

Claude Code stamps each chat's Remote Control pointer rows ({"type":"bridge-session", ...} in the chat's transcript)
with the account the standalone CLI is signed in to (~/.claude.json), not the account Claude Desktop uses. After a
Desktop account switch the restarted chat then trusts the pointer, tries the old remote session (which belongs to the
other account) and mints a new one with its older history held back, so the phone shows only messages after the
switch. When the pointer's owner differs from the CLI login, Claude Code instead takes its "credential store account
changed" branch: it mints a fresh remote session and sends the history since the chat's last compaction (live
2026-10-24, CLI 2.1.308, Dashboard fixes a). Old transcripts also carry durable blocks from earlier CLI versions:
{"type":"history-suppression"} rows (any one taints the whole file) and "noHistoryBackfill":true on pointer rows.

So, right after a switch and before Desktop restarts the chats (about a minute), for each Remote-Control-on chat
whose process is not running, this module:
  - drops history-suppression rows and the noHistoryBackfill flag on pointer rows;
  - when the newest pointer's owner equals the CLI login, sets the owner of every pointer row to an account that is
    not the CLI login: the new Desktop account, or (switching to the CLI login's own account) the previous one, which
    is the account that minted that remote session. When the owner already differs (e.g. the owner signed the CLI in
    to the new account) it leaves owners alone and Claude Code mints fresh by itself.
Every other line stays byte-identical. A backup of the whole transcript is written first, the file is re-read to
check nothing was appended meanwhile, and the new content replaces it atomically. A chat whose process runs is
skipped (its CLI would write the old state back). It reads only the CLI login's accountUuid, never a credential.

This deliberately bypasses Claude Code's cross-account history guard: a chat's history goes to whichever account
Desktop is signed in to. Fine for one person's own accounts; wrong for someone else's. Hence off by default: helper
config.json `"switch_history": ["me@example.com", "me@work.example"]` links the person's own accounts (email or
account ID), and only a switch between two linked accounts is acted on; any other account is left alone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

FLAG = b',"noHistoryBackfill":true'
POINTER_MARK = b'"type":"bridge-session"'
SUPPRESSION_MARK = b'"history-suppression"'
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BACKUPS_KEPT = 2  # per chat


def _row(body: bytes) -> dict | None:
    try:
        row = json.loads(body)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def newest_pointer_owner(lines: list[bytes]) -> str | None:
    """ownerAccountUuid of the newest pointer row that names one (the empty row written when Remote Control is
    turned off names none)."""
    for line in reversed(lines):
        if POINTER_MARK in line:
            row = _row(line.rstrip(b"\r\n"))
            if row and row.get("type") == "bridge-session" and isinstance(row.get("ownerAccountUuid"), str):
                return row["ownerAccountUuid"].lower()
    return None


def choose_stamp(newest_owner: str | None, cli_login: str | None, new: tuple[str, str],
                 previous: tuple[str, str] | None) -> tuple[str, str] | None:
    """(account, org) to stamp on the pointer rows, or None to leave owners alone. The aim is a newest pointer whose
    owner is not the CLI login. Unknown login or owner: None (only the durable blocks are cleared)."""
    if not cli_login or not newest_owner or newest_owner != cli_login:
        return None
    for account in (new, previous):
        if account and account[0].lower() != cli_login:
            return account[0].lower(), account[1].lower()
    return None


def _restamp(line: bytes, stamp: tuple[str, str]) -> bytes:
    row = _row(line.rstrip(b"\r\n"))
    if not row or row.get("type") != "bridge-session" or not isinstance(row.get("ownerAccountUuid"), str):
        return line
    new = line
    for key, value in zip(("ownerAccountUuid", "ownerOrganizationUuid"), stamp):
        old = row.get(key)
        if isinstance(old, str) and old != value:
            new = new.replace(f'"{key}":"{old}"'.encode(), f'"{key}":"{value}"'.encode(), 1)
    expect = dict(row, ownerAccountUuid=stamp[0])
    if isinstance(row.get("ownerOrganizationUuid"), str):
        expect["ownerOrganizationUuid"] = stamp[1]
    return new if _row(new.rstrip(b"\r\n")) == expect else line


def plan(lines: list[bytes], stamp: tuple[str, str] | None) -> tuple[list[bytes], dict]:
    """The cleaned lines and counts {dropped, cleared, restamped}. Lines it does not change stay byte-identical."""
    kept, counts = [], {"dropped": 0, "cleared": 0, "restamped": 0}
    for line in lines:
        body = line.rstrip(b"\r\n")
        if SUPPRESSION_MARK in body:
            row = _row(body)
            if row and row.get("type") == "history-suppression":
                counts["dropped"] += 1
                continue
        if POINTER_MARK in body:
            new = line
            if FLAG in body:
                row = _row(body)
                if row and row.get("type") == "bridge-session" and row.get("noHistoryBackfill") is True:
                    candidate = line.replace(FLAG, b"", 1)
                    if _row(candidate.rstrip(b"\r\n")) == {k: v for k, v in row.items() if k != "noHistoryBackfill"}:
                        new = candidate
                        counts["cleared"] += 1
            if stamp:
                restamped = _restamp(new, stamp)
                counts["restamped"] += restamped != new
                new = restamped
            kept.append(new)
            continue
        kept.append(line)
    return kept, counts


def claude_command_lines() -> list[str]:
    """Command lines of running claude processes (one scan; a PowerShell scan takes about a second, so the caller
    takes it once per switch). A failed scan answers []: the caller also checks Claude Code's own live-session
    registration, which a starting chat writes at once."""
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | ForEach-Object { $_.CommandLine }"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        argv, flags = ["ps", "-axo", "command="], 0
    try:
        output = subprocess.run(argv, capture_output=True, text=True, timeout=30, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in output.splitlines() if "claude" in line.lower()]


def _prune(backup_dir: Path, session_id: str) -> None:
    try:
        old = sorted(backup_dir.glob(f"{session_id}.*.jsonl"))
    except OSError:
        return
    for path in old[:-BACKUPS_KEPT]:
        try:
            path.unlink()
        except OSError:
            pass


def restore_chat(transcript: Path, session_id: str, cli_login: str | None, new: tuple[str, str],
                 previous: tuple[str, str] | None, backup_dir: Path, running) -> dict:
    """Clean one chat's transcript. `running(session_id)` is checked first and again just before the write."""
    if running(session_id):
        return {"outcome": "skipped:running"}
    try:
        raw = transcript.read_bytes()
    except OSError:
        return {"outcome": "skipped:unreadable"}
    lines = raw.splitlines(keepends=True)
    stamp = choose_stamp(newest_pointer_owner(lines), cli_login, new, previous)
    kept, counts = plan(lines, stamp)
    result = dict(counts, stamp=stamp[0] if stamp else None)
    if not any(counts.values()):
        return dict(result, outcome="unchanged")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{session_id}.{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    shutil.copy2(transcript, backup)
    if running(session_id) or transcript.read_bytes() != raw:
        return dict(result, outcome="skipped:changed-meanwhile", backup=str(backup))
    temporary = transcript.with_name(transcript.name + ".switch-history.tmp")
    temporary.write_bytes(b"".join(kept))
    os.replace(temporary, transcript)
    _prune(backup_dir, session_id)
    return dict(result, outcome="cleaned", backup=str(backup))
