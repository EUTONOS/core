"""Duplicate-work precheck: pull together the evidence a chat needs before it
starts on a board item, plus a light advisory "someone is on this" soft claim.

Two chats (a Claude Code chat and a Codex chat) pick up parts of slices with no
fixed ownership. The risk this guards against is duplicate work: the owner
forgets a stage was already done, or half-done, and asks the other chat to
redo it. Most of the evidence already exists -- board item status/progress,
RAM closeout records, git commits, uncommitted edits in other worktrees. This
module is one cheap read that pulls it together, plus an advisory claim file.

Nothing here blocks or authorizes anything. ``verdict``/``reasons`` are a
summary an agent reads before deciding what to do; a claim is advisory only
and is never enforced. See docs/WORK_LIFECYCLE.md#duplicate-work-precheck.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window
import tempfile

from .private_scope import require_shared
from . import boards, ram, tokenslang as ts
from .effects import LocalRoot
from .local_lock import local_lock
from .work_lifecycle import TERMINAL

PROFILE = "eutonos.tokenslang.work-precheck.v1"
CLAIMS_DIR = ".eutonos-local/claims"
ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
HARNESSES = ("claude", "codex")
DEFAULT_SINCE_DAYS = 14
DEFAULT_TTL_MINUTES = 120
MAX_TTL_MINUTES = 60 * 24 * 14
GIT_TIMEOUT = 5
LOCK_TIMEOUT = 5


def validate_item_id(item_id):
    ts.require(isinstance(item_id, str) and bool(ITEM_ID_RE.fullmatch(item_id)),
               "invalid", "Item ID must be a bounded portable slug (letters, digits, '.', '_', '-'; no path separators).")
    return item_id


# ---------------------------------------------------------------- git helpers

def _git_env():
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return env


def _git(args, cwd, timeout=GIT_TIMEOUT):
    """Bounded, tolerant git call. Returns decoded stdout, or None on any failure."""
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + str(Path(cwd).resolve()), *args], cwd=str(cwd), env=_git_env(),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=timeout, **no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8", "replace")
    except UnicodeError:
        return None


_SEP = "\x1f"
_REC = "\x02"
_FMT = "%H" + _SEP + "%ad" + _SEP + "%s"

# A commit that only touches generated record/view paths (board order, RAM
# notes, generated Current Tasks/Next Work/Work Order/Done Work views, or a
# campaign board owner's fenced JSON block) is bookkeeping, not evidence that
# someone is doing the item's actual work. See docs/WORK_LIFECYCLE.md#duplicate-work-precheck.
_RECORD_JSON_RE = re.compile(r"^eutonos\.[^/]+\.json$")
_RECORD_GENERATED_VIEWS = frozenset({"docs/CURRENT_TASKS.md", "docs/NEXT_WORK.md",
                                      "docs/WORK_ORDER.md", "docs/DONE_WORK.md"})
_RECORD_CAMPAIGNS_RE = re.compile(r"^docs/campaigns/.*\.md$")


def _is_record_path(path):
    return (path.startswith("RAM/") or bool(_RECORD_JSON_RE.match(path))
            or path in _RECORD_GENERATED_VIEWS or bool(_RECORD_CAMPAIGNS_RE.match(path)))


def _parse_commits(text):
    commits = []
    for chunk in (text or "").split(_REC):
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        parts = lines[0].split(_SEP)
        if len(parts) != 3:
            continue
        files = [line for line in lines[1:] if line.strip()]
        commits.append({"sha": parts[0], "date": parts[1], "subject": parts[2],
                         "records_only": bool(files) and all(_is_record_path(f) for f in files)})
    return commits


def _id_pattern(item_id):
    # Whole-ID match (ERE): a short ID such as "RCI" must not match inside a
    # word like "exercised".
    # ITEM_ID_RE admits only [A-Za-z0-9._-]; "." is the one ERE metacharacter.
    escaped = validate_item_id(item_id).replace(".", "[.]")
    return "(^|[^[:alnum:]_-])" + escaped + "([^[:alnum:]_-]|$)"


def _commits(repo_root, item_id, paths, since_days):
    ts.require(type(since_days) is int and since_days >= 0, "invalid", "--since must be a non-negative day count.")
    since = "--since=" + str(since_days) + " days ago"
    result = {"since_days": since_days, "available": True, "matching_message": [], "matching_paths": []}
    out = _git(["log", since, "-n", "50", "--name-only", "--format=" + _REC + _FMT,
                "--date=iso-strict", "-i", "-E", "--grep=" + _id_pattern(item_id)], repo_root)
    if out is None:
        result["available"] = False
        return result
    result["matching_message"] = _parse_commits(out)
    if paths:
        out2 = _git(["log", since, "-n", "50", "--name-only", "--format=" + _REC + _FMT,
                     "--date=iso-strict", "--", *paths], repo_root)
        if out2 is None:
            result["available"] = False
        else:
            result["matching_paths"] = _parse_commits(out2)
    return result


def _status_entries(output):
    """Decode NUL-delimited porcelain v1 without quoted-path ambiguity."""
    chunks = output.split('\0')
    entries = []
    index = 0
    while index < len(chunks):
        record = chunks[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != ' ':
            raise ValueError('Invalid Git porcelain status record.')
        code, path = record[:2], record[3:]
        paths = [path]
        if 'R' in code or 'C' in code:
            if index >= len(chunks) or not chunks[index]:
                raise ValueError('Incomplete Git rename/copy status record.')
            paths.append(chunks[index])
            index += 1
        entries.append({'code': code, 'paths': paths, 'display': record})
    return entries

def _worktrees(repo_root, item_id, paths):
    """Every worktree's dirty-file count, plus which ones are actually relevant
    to this item: a worktree whose branch name contains the item ID, or (when
    --paths is given) one with a dirty file inside those paths. An unrelated
    dirty worktree stays in ``worktrees`` for context but is never counted
    toward the verdict, and is summarized as one ``other_dirty_count``."""
    out = _git(["worktree", "list", "--porcelain"], repo_root)
    if out is None:
        return {"available": False, "worktrees": [], "relevant": [], "other_dirty_count": 0}
    entries, current = [], {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(current); current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
    if current:
        entries.append(current)
    normalized_paths = [str(pp).replace("\\", "/") for pp in paths] if paths else []
    rows, relevant, other_dirty = [], [], 0
    for entry in entries:
        wt_path = entry.get("path")
        if not wt_path or entry.get("bare"):
            continue
        try:
            require_shared(wt_path)
        except ts.ReadError:
            continue
        p = Path(wt_path)
        branch_name = (entry.get("branch") or "").replace("refs/heads/", "") or None
        matches_branch = bool(branch_name and item_id in branch_name)
        row = {"path": wt_path, "branch": branch_name, "detached": bool(entry.get("detached")),
               "matches_item_branch": matches_branch}
        if not p.exists():
            row["available"] = False
            rows.append(row)
            continue
        status_out = _git(['status', '--porcelain=v1', '-z'], p)
        row['available'] = status_out is not None
        if status_out is None:
            rows.append(row)
            continue
        try:
            entries = _status_entries(status_out)
        except ValueError:
            row['available'] = False
            rows.append(row)
            continue
        row['changed_count'] = len(entries)
        path_count = 0
        if normalized_paths:
            filtered = [entry for entry in entries
                        if any(path.startswith(prefix) for path in entry['paths']
                               for prefix in normalized_paths)]
            path_count = len(filtered)
            row['paths_changed_count'] = path_count
            row['changes'] = [entry['display'] for entry in filtered[:50]]
        rows.append(row)
        if row["changed_count"] == 0:
            continue
        if matches_branch:
            relevant.append({"path": wt_path, "branch": branch_name, "changed_count": row["changed_count"]})
        elif normalized_paths and path_count:
            relevant.append({"path": wt_path, "branch": branch_name, "changed_count": path_count})
        else:
            other_dirty += 1
    return {"available": all(row.get('available', True) for row in rows), "worktrees": rows, "relevant": relevant, "other_dirty_count": other_dirty}


# --------------------------------------------------------------- RAM helpers

def _ram_records(root, item_id, limit=5):
    try:
        local = LocalRoot(root)
        store = ram.Store(local)
        result = store.read({"profile": ram.READ, "operation": "browse", "summary_only": True,
                              "work_id": item_id, "sort": "recorded_at", "order": "desc", "limit": limit})
    except ts.ReadError as exc:
        if not (Path(root) / 'eutonos.ram.json').exists():
            return {'available': False, 'reason': 'not-configured', 'records': []}
        return {"available": False, "reason": getattr(exc, "code", "invalid"), "records": []}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return {"available": False, "reason": "error", "records": []}
    records = [{"path": item["path"], "session_id": item["identity"]["session_id"],
                "recorded_at": item.get("recorded_at"), "occurred_at": item.get("occurred_at"),
                "kind": item.get("kind"), "preview": item.get("summary")}
               for item in result.get("items", [])]
    return {"available": True, "records": records}


# ------------------------------------------------------------- board helpers

def _board_item(root, item_id):
    projection = boards.project(root)
    item = next((i for i in projection.get("items", []) if i["id"] == item_id), None)
    if item is None:
        return None, projection.get("diagnostics", [])
    return _native_item(item), projection.get("diagnostics", [])


def _native_item(item):
    """Expose verdict fields consistently without losing selected source pins."""
    if item is None: return None
    extensions = item.get('extensions') or {}
    return {**item, 'progress': extensions.get('progress', item.get('progress')),
            'checkpoint': extensions.get('checkpoint', item.get('checkpoint'))}



# --------------------------------------------------------------- claim files

def _claim_path(root, item_id):
    require_shared(root)
    return Path(root).resolve() / CLAIMS_DIR / (item_id + ".json")


def _now():
    return datetime.now(timezone.utc)


def _parse_instant(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_claim(root, item_id):
    """Read a claim file if present and well-formed. Never raises; a bad file reads as absent."""
    path = _claim_path(root, item_id)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or not {"item", "chat", "t", "expires"} <= set(value):
        return None
    return value


def _atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="claim-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_claim(root, item_id, chat, harness=None, ttl_minutes=DEFAULT_TTL_MINUTES):
    """Write or refresh an advisory soft claim. Never blocks or authorizes anything."""
    validate_item_id(item_id)
    ts.require(isinstance(chat, str) and bool(chat.strip()), "invalid", "A claim needs a non-empty chat name.")
    ts.require(harness is None or harness in HARNESSES, "invalid", "Unsupported harness; use claude or codex.")
    ts.require(type(ttl_minutes) is int and 1 <= ttl_minutes <= MAX_TTL_MINUTES, "invalid", "TTL must be a bounded number of minutes.")
    now = _now()
    value = {"item": item_id, "chat": chat, "harness": harness, "t": now.isoformat(),
              "expires": (now + timedelta(minutes=ttl_minutes)).isoformat()}
    with local_lock(root, timeout=LOCK_TIMEOUT):
        _atomic_write(_claim_path(root, item_id), value)
    return value


def release_claim(root, item_id, chat):
    """Remove a claim this chat owns. A claim owned by someone else is left alone."""
    validate_item_id(item_id)
    ts.require(isinstance(chat, str) and bool(chat.strip()), "invalid", "A release needs a non-empty chat name.")
    with local_lock(root, timeout=LOCK_TIMEOUT):
        existing = read_claim(root, item_id)
        if existing is None:
            return {"outcome": "absent"}
        if existing.get("chat") != chat:
            return {"outcome": "not-owner", "chat": existing.get("chat")}
        path = _claim_path(root, item_id)
        try:
            path.unlink()
        except OSError:
            pass
        return {"outcome": "released"}


def _claim_status(root, item_id):
    """Expired claims are reported, never auto-deleted by a reader."""
    claim = read_claim(root, item_id)
    if claim is None:
        return {"active": [], "expired": []}
    expires = _parse_instant(claim.get("expires"))
    if expires is not None and expires < _now():
        return {"active": [], "expired": [claim]}
    return {"active": [claim], "expired": []}


# ------------------------------------------------------------------ verdict

def _after(date_str, checkpoint_date):
    if not checkpoint_date or not date_str:
        return True
    try:
        return date_str[:10] > checkpoint_date
    except TypeError:
        return True


def _verdict(item, ram_info, commits, live, claims, chat):
    reasons = []
    if item is not None and item["status"] in TERMINAL:
        reasons.append("Board item status is '" + item["status"] + "'.")
    for claim in claims["active"]:
        if chat is None or claim.get("chat") != chat:
            reasons.append("Live claim by '" + str(claim.get("chat")) + "' (" + str(claim.get("harness") or "unknown harness")
                            + "), expires " + str(claim.get("expires")) + ".")
    if reasons:
        return "likely-duplicate", reasons

    checkpoint_date = None
    if item:
        checkpoint = item.get("checkpoint")
        if checkpoint and checkpoint.get("date"):
            checkpoint_date = checkpoint["date"]
        elif item.get("progress") and item["progress"].get("updated_on"):
            checkpoint_date = item["progress"]["updated_on"]

    reasons = []
    ram_new = [r for r in ram_info.get("records", []) if _after(r.get("recorded_at"), checkpoint_date)]
    if ram_new:
        reasons.append(str(len(ram_new)) + " RAM record(s) recorded"
                        + (" since checkpoint " + checkpoint_date + "." if checkpoint_date else "."))
    by_sha = {c["sha"]: c for c in commits.get("matching_message", []) + commits.get("matching_paths", [])}
    commits_new = [c for c in by_sha.values() if _after(c.get("date"), checkpoint_date) and not c.get("records_only")]
    if commits_new:
        reasons.append(str(len(commits_new)) + " commit(s) mention this item"
                        + (" since checkpoint " + checkpoint_date + "." if checkpoint_date else "."))
    relevant = live.get("relevant", [])
    if relevant:
        total = sum(w.get("changed_count", 0) for w in relevant)
        reasons.append(str(total) + " uncommitted change(s) across " + str(len(relevant))
                        + " worktree(s) linked to this item.")
    branch_matches = [w for w in live.get("worktrees", []) if w.get("matches_item_branch")]
    if branch_matches:
        reasons.append("A worktree branch name contains this item ID: "
                        + ", ".join(w.get("branch") or w["path"] for w in branch_matches) + ".")
    progress = item.get("progress") if item else None
    if progress and isinstance(progress.get("percent"), int) and 0 < progress["percent"] < 100:
        reasons.append("Recorded progress is " + str(progress["percent"]) + "% (" + str(progress.get("basis")) + ").")
    if reasons:
        return "partial-work", reasons
    if (not commits.get('available', True) or not live.get('available', True)
            or any(row.get('available') is False for row in live.get('worktrees', []))
            or (not ram_info.get('available', True) and ram_info.get('reason') != 'not-configured')):
        return 'unknown', ['One or more evidence readers failed; no absence-of-work conclusion is supported.']
    # Older or records-only RAM/commits may still be listed above; "clear" only means none is new since the
    # checkpoint (HAO5 pilot 2026-10-21: the old wording read as "nothing exists" beside listed records).
    return "clear", ["No RAM records or work commits since the last checkpoint, and no linked live edits or "
                     "active claims; older or records-only matches may still be listed."]


# ------------------------------------------------------------------ precheck

def precheck(root, item_id, *, since_days=DEFAULT_SINCE_DAYS, paths=None, chat=None):
    validate_item_id(item_id)
    try:
        require_shared(root)
    except ts.ReadError as exc:
        if exc.code != "unavailable":
            raise
        # No public/private HEAD proof: report unknown without opening owners.
        message = "Shared scope eligibility is unavailable; no absence-of-work conclusion is supported."
        return {"profile": PROFILE, "outcome": "ok", "item_id": item_id, "item": None,
                "shared_work": None, "attempts": None, "board_diagnostics": [message],
                "ram": {"available": False, "records": [], "reason": "Scope eligibility unavailable"},
                "commits": {"available": False, "matching_message": [], "matching_paths": [], "since_days": since_days},
                "live_edits": {"available": False, "worktrees": [], "relevant": [], "other_dirty_count": 0},
                "claims": [], "verdict": "unknown", "reasons": [message]}
    repo_root = Path(root).resolve()
    paths = list(paths) if paths else []
    from . import current_work
    shared_view = current_work.project(root, include_knowledge=False)
    observed = [row for row in shared_view.get('boards', {}).get('items', []) if row['id'] == item_id]
    if shared_view.get('shared_work', {}).get('enabled'):
        item = observed[0] if len(observed) == 1 else None
        diagnostics = shared_view.get('diagnostics', [])
    else:
        if len(observed) == 1 and shared_view['status'] == 'available':
            item = observed[0]
            diagnostics = shared_view.get('diagnostics', [])
        else:
            item, diagnostics = _board_item(root, item_id)
            diagnostics = [*diagnostics, 'Using checkout-local board fallback; shared source coverage is unavailable.']
    item = _native_item(item)
    ram_info = _ram_records(root, item_id)
    commits = _commits(repo_root, item_id, paths, since_days)
    live = _worktrees(repo_root, item_id, paths)
    claims = _claim_status(root, item_id)
    verdict, reasons = _verdict(item, ram_info, commits, live, claims, chat)
    if shared_view.get('shared_work', {}).get('enabled'):
        target_scope = {item_id}
        parent = (item or {}).get('parent_id')
        observed_by_id = {row['id']: row for row in shared_view.get('boards', {}).get('items', [])}
        while parent and parent not in target_scope:
            target_scope.add(parent); parent = observed_by_id.get(parent, {}).get('parent_id')
        overlapping = [row for row in shared_view.get('attempts', {}).get('items', []) if row['state'] == 'active'
                       and row['session'] != chat and (row['target']['work_id'] in target_scope or item_id in row['target']['ancestors'])]
        if overlapping:
            verdict = 'overlap'
            reasons += ['Observed attempt ' + row['id'] + ' by ' + row['participant'] + (' (intentional parallel work).' if row['intentional_parallel'] else '.') for row in overlapping]
        elif verdict == 'clear' and (item is None or item.get('record_conflict') or shared_view['status'] != 'available'
              or shared_view['shared_work']['coverage'] != 'complete' or not all(v.get('available', True) for v in (ram_info, commits, live))):
            verdict = 'unknown'; reasons = ['Evidence coverage is incomplete; no absence-of-work conclusion is supported.']
    return {"profile": PROFILE, "outcome": "ok", "item_id": item_id, "item": item,
            'shared_work': shared_view.get('shared_work'), 'attempts': shared_view.get('attempts'),
            "board_diagnostics": diagnostics, "ram": ram_info, "commits": commits,
            "live_edits": live, "claims": claims, "verdict": verdict, "reasons": reasons}


def format_text(result):
    lines = []
    item = result.get("item")
    header = "Item: " + result["item_id"]
    header += " -- " + item["title"] + " [" + item["status"] + "]" if item else " (not found on board)"
    lines.append(header)
    if item:
        progress = item.get("progress")
        if progress:
            lines.append("Progress: " + str(progress.get("percent")) + "% -- " + str(progress.get("basis")))
        if item.get("next_action"):
            lines.append("Next action: " + item["next_action"])
        checkpoint = item.get("checkpoint")
        if checkpoint:
            lines.append("Last checkpoint (" + str(checkpoint.get("date")) + "): " + str(checkpoint.get("what"))
                          + " -> " + str(checkpoint.get("result")))
    ram_info = result["ram"]
    lines.append("RAM: " + str(len(ram_info.get("records", []))) + " record(s)"
                  + ("" if ram_info.get("available", True) else " (unavailable: " + str(ram_info.get("reason")) + ")"))
    for record in ram_info.get("records", [])[:5]:
        lines.append("  - " + str(record.get("recorded_at")) + " [" + str(record.get("session_id")) + "] " + str(record.get("preview")))
    commits = result["commits"]
    total_commits = len(commits.get("matching_message", [])) + len(commits.get("matching_paths", []))
    lines.append("Commits (last " + str(commits.get("since_days")) + "d): " + str(total_commits) + " matching"
                  + ("" if commits.get("available", True) else " (git unavailable)"))
    for commit in commits.get("matching_message", [])[:5]:
        line = "  - " + commit["sha"][:10] + " " + commit["date"] + " " + commit["subject"]
        if commit.get("records_only"):
            line += " (records only)"
        lines.append(line)
    live = result["live_edits"]
    lines.append("Live worktrees:" if live.get("available", True) else "Live worktrees: (git unavailable)")
    shown = [w for w in live.get("worktrees", []) if w.get("matches_item_branch") or w.get("paths_changed_count")]
    for worktree in shown:
        row = "  - " + worktree["path"] + " branch=" + str(worktree.get("branch")) + " changed=" + str(worktree.get("changed_count"))
        if worktree.get("matches_item_branch"):
            row += " [item-branch]"
        if worktree.get("available") is False:
            row += " (missing)"
        lines.append(row)
    other_dirty = live.get("other_dirty_count", 0)
    if other_dirty:
        lines.append("Other worktrees: " + str(other_dirty) + " dirty, not linked to this item.")
    claims = result["claims"]
    for claim in claims["active"]:
        lines.append("Claim: " + str(claim["chat"]) + " (" + str(claim.get("harness")) + ") until " + str(claim["expires"]))
    for claim in claims["expired"]:
        lines.append("Expired claim: " + str(claim["chat"]) + " (expired " + str(claim["expires"]) + ")")
    lines.append("Verdict: " + result["verdict"])
    for reason in result.get("reasons", []):
        lines.append("  - " + reason)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--since", type=int, default=DEFAULT_SINCE_DAYS, dest="since_days",
                        help="Commit lookback window in days (default 14).")
    parser.add_argument("--paths", nargs="*", default=None, help="Repository-relative paths to check for matching commits/live edits.")
    parser.add_argument("--chat", help="This chat's name; used to identify a claim as your own, and required for --claim/--release.")
    parser.add_argument("--harness", choices=HARNESSES)
    parser.add_argument("--model", help="Actual caller model, when available; never inferred from Git.")
    parser.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES, dest="ttl_minutes")
    parser.add_argument("--claim", action="store_true", help="Write or refresh a soft claim for --chat on this item.")
    parser.add_argument('--host')
    parser.add_argument('--actor')
    parser.add_argument('--attempt-id')
    parser.add_argument('--intent')
    parser.add_argument('--intentional-parallel', action='store_true')
    parser.add_argument('--finish', action='store_true', help='Finish only this session owned attempt; canonical work remains unchanged.')
    parser.add_argument("--release", action="store_true", help="Remove --chat's soft claim on this item.")
    parser.add_argument("--text", action="store_true", help="Print short human text instead of JSON.")
    args = parser.parse_args(argv)
    try:
        validate_item_id(args.item_id)
        ts.require(sum((args.claim, args.release, args.finish)) <= 1, 'invalid', 'Choose one attempt action.')
        from .shared_work import load as shared_load
        if shared_load(args.root)['enabled'] and (args.claim or args.release or args.finish):
            from . import shared_attempts
            ts.require(bool(args.chat), 'invalid', 'Shared attempt actions require actual --chat session.')
            if args.claim:
                action = shared_attempts.start(args.root, args.item_id, args.chat, args.host, args.actor,
                    attempt_id=args.attempt_id, intent=args.intent, intentional_parallel=args.intentional_parallel, caller_context={'app':args.harness,'model':args.model})
            else:
                action = shared_attempts.finish(args.root, args.item_id, args.chat, args.host, args.actor,
                    attempt_id=args.attempt_id, state='finished' if args.finish else 'released', checkpoint=args.intent)
            result = {'profile': PROFILE, 'outcome': 'ok', 'item_id': args.item_id, 'claim_action': action}
        elif args.finish:
            raise ValueError('--finish requires enabled shared work; use canonical work lifecycle for solo completion.')
        elif args.release:
            ts.require(bool(args.chat), "invalid", "--release requires --chat.")
            result = {"profile": PROFILE, "outcome": "ok", "item_id": args.item_id,
                      "claim_action": release_claim(args.root, args.item_id, args.chat)}
        elif args.claim:
            ts.require(bool(args.chat), "invalid", "--claim requires --chat.")
            claim = write_claim(args.root, args.item_id, args.chat, args.harness, args.ttl_minutes)
            result = {"profile": PROFILE, "outcome": "ok", "item_id": args.item_id,
                      "claim_action": {"outcome": "claimed", "claim": claim}}
        else:
            result = precheck(args.root, args.item_id, since_days=args.since_days, paths=args.paths, chat=args.chat)
        if args.text and "verdict" in result:
            print(format_text(result))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        print(json.dumps({"profile": PROFILE, "outcome": "error",
                          "code": getattr(exc, "code", "invalid"), "message": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
