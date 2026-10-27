"""Role reminder hooks for Claude Code and Codex (AWR role hooks, owner 2026-09-28).

ONE small program handles every wired hook event for both apps: it reads a
single JSON object on stdin and writes a single JSON object on stdout, and
this PROGRAM ITSELF never blocks or fails loudly -- any error, timeout risk,
or disabled switch makes it print ``{}`` and exit 0 (see finding 4, Codex
verification, 2026-09-28: every file read is byte-capped or tail-read, every
directory scan is bounded, and its own settings read never waits on a
contended lock -- ``_hooks_config`` uses a short ``local_lock`` timeout and
degrades to "no reminder this time" rather than wait). Finding 5 (coordinator
recheck, 2026-09-28) tightened two bounds that were previously incomplete:
``_hooks_config`` now reads settings through ``settings.read_hooks_bounded``,
a hooks-only path that opens ``settings.json`` directly and caps the read
itself (never ``read_hooks``/``_local``, which read the WHOLE file), and
``_tail_read`` now issues exactly ONE bounded ``read(max_bytes)`` call taken
from a size read off the SAME open handle, rather than an earlier, separately
stat()'d size followed by an unbounded ``read()`` -- so a file growing
DURING the call can no longer make either read unbounded. It makes no model
calls, no network calls, and never reads a big file; the only I/O is a small
settings read and a tiny bounded per-session state file plus a capped
metadata-only log and, at Stop, one byte-capped tail read of the transcript
the payload names (the idle-turn skip below). QUALIFICATION (finding 6, Codex
verification, 2026-09-28): this is a claim about THIS PROGRAM's own I/O. The
``stop_check`` end-turn pass deliberately forces exactly one continuation per
turn (owner, 2026-10-05) unless the turn left nothing to check (owner,
2026-10-23); it never checks closeout proof. It exists to give a running agent
short, role-specific reminders (delegate, stay in scope, verify) that
survive long turns and compaction/resume, without a duplicate "reminder"
setting -- ``settings.py``'s ``hooks`` section (see
``BUILTIN_HOOK_SETS``/``read_hooks``/``update_hooks``) is the single owner of
this text.

REMOVED (owner, 2026-09-29): the earlier continuity checkpoints, Stop-time RAM
capture and reservation-aware patch guard, including their CLI, were deleted
after their hook events had failed since around 2026-09-23 (``turn_intake``
rejection). Workflow closeout (``tools/workflow.py prepare closeout`` / ``run
closeout``) still records RAM independently. ``edit_nudge`` is reminder text,
never a reservation check or an edit block.

ROLE RESOLUTION
  - A subagent (Claude Code Task/Agent-tool child, or a Codex child whose
    event itself names it): detected from the hook payload's ``agent_id``
    field. For Claude Code this is present on EVERY hook event that fires
    inside a subagent (per Claude Code's hooks reference --
    ``PreToolUse``/``PostToolUse``/``Stop``-family events all carry it for
    subagent tool calls, and the dedicated ``SubagentStart`` event always
    does). Its role is looked up from ``agent_type`` in
    ``AGENT_TYPE_ROLE_HINTS`` (a small hardcoded hint, not a setting -- kept
    out of settings.py to avoid a second place to configure role text),
    falling back to ``DEFAULT_CHILD_ROLE`` ("worker-routine", a real catalog
    role name, so its dashboard per-role switch has a meaningful effect on
    real subagents).
  - Otherwise (no ``agent_id``): a normal top-level chat. ``EUTONOS_ROLE`` if
    set (the owner's own manual marker, e.g. for a Director session) wins; otherwise ``"orchestrator"``.
  - CODEX CHILD IDENTITY (finding 1, Codex verification, 2026-09-28;
    redesigned on coordinator recheck, 2026-09-28): Codex documents
    ``SubagentStart``/``SubagentStop`` (https://learn.chatgpt.com/docs/hooks)
    carrying ``agent_id``/``agent_type``, so a Codex child's OWN start/stop
    is identified exactly like Claude's. BUT Codex's ORDINARY events
    (``PreToolUse`` etc.) inside a child are NOT documented as carrying
    ``agent_id`` -- they surface the PARENT's ``session_id`` instead. A live
    Luna child on 2026-09-29 had a distinct rollout session ID, but its
    ordinary tool-event rows used the parent's hashed session key and were
    suppressed as ambiguous. The installed logger did not retain raw hook
    input keys, so that observation does not prove the exact stdin schema.
    The published Codex hook schema lists ``agent_id`` on subagent start/stop
    but not on ``PreToolUse`` or ``UserPromptSubmit``. Its other common fields
    do not provide a documented stable mapping from an ordinary event to one
    child; ``model`` can be shared, and transcript format is explicitly
    unstable. Thus an ordinary Codex event with no ``agent_id`` cannot be
    trusted at face value to be the orchestrator's own action; it might
    actually be a live child's
    action mislabeled with the parent's session_id. This program tracks live
    children as INDEPENDENT MARKER FILES, one per child, under
    ``.eutonos-local/hooks/children/<parent-key>/<agent-key>.marker`` -- not
    a shared map on the parent's own state file (see ``_touch_active_child``/
    ``_release_active_child``/``_active_children_status``). The original
    design used exactly that shared map, updated via a read-modify-write
    (load the parent's state, mutate a dict, save it back); two concurrent
    ``SubagentStart`` calls for the same parent raced on that read-modify-
    write and could silently lose one child's entry to whichever write
    landed last (and a concurrent parent message/session-start save touching
    the same file had the identical problem). One file per child needs no
    shared read-modify-write at all: each child's own existence is created
    and deleted only by that child's own events.

    EXPIRY (finding 2, corrected on coordinator recheck, 2026-09-28): a
    marker's existence, AT ANY AGE, means active/ambiguous -- age is NEVER
    grounds for "no children"; only that child's own confirmed
    ``SubagentStop`` removes its marker and can restore main behavior
    immediately. The original design instead expired a marker after 30
    minutes without a ``SubagentStop``, treating that as "child gone" -- but
    Codex's ordinary child events are exactly the ones that lack
    ``agent_id`` (the whole reason this tracking exists), so there is no
    reliable heartbeat from those events to refresh a marker; letting age
    alone silently clear "active" would let one lost ``SubagentStop`` (a
    crashed child, a Codex version that never fires it) quietly turn
    ambiguous events back into confidently-orchestrator ones -- exactly the
    miscrediting risk this design exists to prevent, and the failure mode is
    permanent silence, not a visible error. Instead, age affects only
    observability: past ``CHILD_STALE_SECONDS`` (4 hours) the logged reason
    changes from ``"ambiguous-child-active"`` to ``"ambiguous-stale"`` so a
    dashboard/test run can see a likely-lost stop, but the suppression
    itself is identical either way. Marker files are never cleaned up on
    age (Codex final recheck, 2026-09-29: any cleanup raced child
    registration); only a marker's own ``SubagentStop``
    (``_release_active_child``) ever deletes it. Empty hashed directories
    are negligible, so they are simply left in place; disk
    hygiene never doubles as a liveness signal. (Since 2026-10-01 the EVIDENCE
    GATE above makes a lost ``SubagentStop`` harmless once Codex has been seen
    sending ``agent_id`` on an ordinary event; age is still never the fix.)
    A marker's mtime is refreshed
    on ANY event that carries
    both ``session_id`` and ``agent_id`` (Claude sends ``agent_id`` on
    ordinary events too, so this keeps a long Claude subagent's marker fresh
    for observability; harmless, since Claude never consults this
    tracking for its own ambiguity decision).

    While ANY marker exists for a Codex session, an ordinary event with no
    ``agent_id`` is treated as AMBIGUOUS in ``_handle_pre_tool_use``: the
    orchestrator-only ``edit_nudge`` and the ``long_turn`` reminder/counter
    are both suppressed for that call rather than risk crediting or nudging
    the wrong actor -- UNLESS the evidence gate below is open. With no active
    child, an ordinary Codex event resolves as the main chat exactly as
    before -- this is the owner's core need and is never made neutral. Claude
    keeps plain ``agent_id`` detection unchanged, since Claude's docs list
    ``agent_id`` as present on ordinary events too; the ambiguity check only
    ever applies when ``app == "codex"``.

    EVIDENCE GATE (live evidence, 2026-10-01): the assumption above (a Codex
    child's ordinary events lack ``agent_id``) is disproved for the installed
    Codex. ``events.jsonl`` rows for worker-routine children's ``long_turn``
    firings, which come only from ``PreToolUse``, all had ``has_agent_id``
    true (six distinct child sessions), and their ``SubagentStop`` rows
    released their markers. Meanwhile the ambiguity rule suppressed 350
    ``long_turn`` rows (``ambiguous-child-active``/``ambiguous-stale``)
    against 1 fired, and ``edit_nudge`` 32 against 2: markers whose child
    never reported ``SubagentStop`` (five, 2026-09-29/30, one parent) kept
    that parent's orchestrator silent -- the permanent-silence failure the
    EXPIRY note warns about. So trust is gated on observed evidence rather
    than on a documentation gap: the first Codex ``PreToolUse`` that carries
    ``agent_id`` writes ``CODEX_AGENT_ID_PROOF_NAME`` (``_note_codex_agent_id``,
    a tiny, bounded, atomic-replace write that never raises). While a valid
    proof exists (``_codex_agent_id_proven``: byte-capped read, a corrupt or
    oversized file counts as NO proof), an ordinary Codex event WITHOUT
    ``agent_id`` is the orchestrator's own: no ambiguous suppression, normal
    counting and reminders, whatever markers exist. Without proof, today's
    conservative suppression is unchanged. Markers remain written and
    released exactly as before but are observability only once proof exists.
  Role names are never validated against a fixed catalog here: any string
  resolves through the active hook set's per-role text, falling back to that
  set's ``"default"`` entry, and ``role_enabled`` similarly defaults an
  unlisted role to enabled -- so an arbitrary/custom role name always
  degrades gracefully instead of raising.

TRIGGERS (``settings.HOOK_TRIGGERS``)
  ``hooks.closeout_check`` defaults on; it adds the owner's recording policy
  (``END_TURN_RECORDING_POLICY``) to a main chat's end-turn pass. Nothing
  checks closeout proof. Global hooks-off remains.
  - ``message``: every ``UserPromptSubmit``. Also resets the long_turn counter
    and the end-turn guard, except for a Codex end-turn continuation prompt
    (recognized by its nonce tag), which keeps both and adds no text.
  - ``session_start``: ``SessionStart`` (any ``source``, including compact and
    resume) and either app's ``SubagentStart``. Also resets the long_turn
    counter and the end-turn guard.
  - ``edit_nudge``: on ``PreToolUse`` for an edit-shaped tool
    (``EDIT_TOOL_NAMES``), main thread only (never for a subagent -- a
    worker's own edits are its job, not a nudge target), rate-limited by
    ``edit_nudge_rate_limit_minutes`` (a setting) so it fires at most once
    per that many minutes per session.
  - ``stop_check`` (default on; owner, 2026-10-05): at ``Stop`` and
    ``SubagentStop`` returns one ``decision: block`` per turn with the role's
    text, the recording policy (main chat only) and ``END_TURN_AUTHORITY``.
    Exactly one: ``stop_hook_active`` never blocks, and a create-once guard
    file per (session, child) makes concurrent Stop + SubagentStop for one
    owner block at most once. Any guard error fails open (no block). Not
    for a Claude ``SubagentStop``: the subagent has already handed back its
    report, so a block cannot continue it (see ``_handle_subagent_stop``).
    IDLE-TURN SKIP (owner, 2026-10-23): Claude Code refuses an empty reply
    after a blocking Stop hook, so a pass on a turn that only answered a
    question made agents post filler. The pass is therefore skipped when the
    turn left nothing to check: since the last real user prompt (Claude) or
    the turn's own ``task_started`` row (Codex) it made no file edit and no
    effect-making tool call (see ``_turn_has_nothing_outstanding``). The
    signal is the payload's ``transcript_path`` (a Codex child's
    ``agent_transcript_path``), read as ONE byte-capped tail
    (``END_TURN_SKIP_TAIL_BYTES``); a missing, unreadable, malformed,
    unrecognized or too-long-to-see-the-start transcript keeps the pass
    (fail toward reminding). Any tool not on a short read-only list counts as
    effect-making. No guard is claimed on a skip; it logs
    ``stop_check`` ``fired=false`` with ``END_TURN_SKIP_REASON``.
  - PENDING ACTIONS (not a configurable trigger; slice-f331681a S4): steps a component or setting switch could
    not finish itself sit in ``<EUTONOS home>/settings/pending-actions.json`` (``eutonos.pending_actions``). A MAIN
    chat (never a subagent) gets ONE extra line, ``_pending_line``, after the reminder text: at ``session_start``
    (any source, so resume and compact too) when something is pending for its app, and on a ``message`` only when
    the pending set changed since this session last saw it (a signature in the per-session state file). It is
    bounded (``PENDING_LINE_MAX_CHARS`` plus the first ``PENDING_LINE_ITEMS`` items, each cut to
    ``PENDING_ITEM_CHARS``), read lock-free and byte-capped, never blocks, and nothing is injected when the list is
    empty; an unreadable file gets one short warning line instead. It follows the global hooks switch only.
  - ``long_turn``: counted on every ``PreToolUse`` (both apps expose this
    event for every tool call, so one program invocation does double duty
    for the edit nudge and this counter -- no separate ``PostToolUse``
    registration is needed). Applies to every role, including subagents,
    keyed per session (see ``_session_key``). Fires and resets the counter
    once ``long_turn_threshold`` (a setting) tool calls have passed since the
    last reset; a ``message`` or ``session_start`` firing also resets it.

OUTPUT SHAPES (confirmed against Claude Code's hooks reference,
https://code.claude.com/docs/en/hooks.md, and this repository's existing
Codex's installed hook interface):
  - ``SessionStart``/``SubagentStart``/``UserPromptSubmit``/``PreToolUse``:
    ``{"hookSpecificOutput": {"hookEventName": <name>, "additionalContext": <text>}}``
    -- for ``PreToolUse`` this is a pure nudge: no ``permissionDecision`` key
    is set, so the tool call proceeds exactly as it would have otherwise.
  - ``Stop``/``SubagentStop``: ``{"decision": "block", "reason": <text>}`` once
    per turn when ``stop_check`` applies and the turn left something to check
    (never a Claude ``SubagentStop``), otherwise ``{}``.
  - Anything unrecognized, disabled, or erroring: ``{}``.

FIRING LOG
  ``.eutonos-local/hooks/events.jsonl`` (metadata only -- never prompt or
  reminder text): one JSON object per line, capped by size (oldest lines
  dropped on rotation). Each row includes ``has_agent_id`` (a boolean, never
  the raw ID). A skipped end-turn pass is a ``stop_check`` row with
  ``fired=false`` and reason ``END_TURN_SKIP_REASON`` ("skipped: nothing
  outstanding"). Either app's ``SubagentStop`` writes a log-only
  ``subagent_stop`` row reporting whether a marker was released; it is not a
  configurable reminder trigger.
  ``read_recent_events`` is the dashboard's read path.

STATE
  ``.eutonos-local/hooks/state/<session-key>.json``: a tiny per-(session,
  subagent) record of the long_turn counter and the edit_nudge rate-limit
  timestamp. ``.eutonos-local/hooks/endturn/<session-key>.guard`` holds the
  current turn's end-turn nonce. Both are pruned opportunistically (bounded, probabilistic) by age and
  count so it never grows unbounded (finding 4: the prune pass itself now
  scans at most ``STATE_PRUNE_SCAN_LIMIT`` directory entries via
  ``os.scandir``, rather than listing/sorting/statting an entire directory,
  so it stays bounded even if that directory somehow grew huge). This file
  no longer holds any child-tracking data (see CHILDREN below -- redesigned
  on coordinator recheck, 2026-09-28, to remove a shared read-modify-write
  race).

CHILDREN (finding 1, redesigned on coordinator recheck, 2026-09-28)
  ``.eutonos-local/hooks/children/<parent-session-key>/<agent-key>.marker``:
  one small, empty marker file per live Codex child, under its PARENT
  session's own subdirectory (see "CODEX CHILD IDENTITY" above) -- created/
  refreshed by ``_touch_active_child``, deleted by ``_release_active_child``
  on that child's own ``SubagentStop``. No shared state to race on. There
  is no age-based cleanup of child markers or their directories (removed on
  Codex final recheck, 2026-09-29: an empty-directory sweep could delete a
  directory between a child's mkdir and its marker touch); see
  ``_active_children_status``.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import random
import sys
import time
from pathlib import Path

from . import home as eutonos_home
from . import pending_actions
from . import settings

ROLE_ENV = "EUTONOS_ROLE"
APPS = ("claude", "codex")

# Both apps' hook payloads use these same field names for an edit-shaped tool
# call (confirmed for Claude: Edit/Write/NotebookEdit; for Codex: apply_patch,
# and observed in this project's Codex hook input).
EDIT_TOOL_NAMES = {"Edit", "Write", "NotebookEdit", "apply_patch"}

SESSION_START_EVENTS = {"SessionStart", "SubagentStart"}
SUBAGENT_STOP_EVENT = "SubagentStop"

# A real settings role name (see settings.DEFAULT_ROLES), not an invented
# label -- so its dashboard per-role on/off switch has a real, visible effect
# on actual subagent traffic. Not a setting itself: kept small and hardcoded
# to avoid a second, harder-to-find place to configure role text.
DEFAULT_CHILD_ROLE = "worker-routine"
AGENT_TYPE_ROLE_HINTS = {"Explore": "discovery"}

# Finding 1 (Codex verification, 2026-09-28; redesigned on coordinator
# recheck, 2026-09-28): each live child gets its OWN small marker file under
# CHILDREN_SUBDIR, keyed by (parent session, agent_id) -- see the module
# docstring's "CODEX CHILD IDENTITY" note. Existence alone (at ANY age) means
# active/ambiguous; only that child's own SubagentStop removes it. Age is
# used for two things ONLY, neither of which is "no longer active":
#   - CHILD_STALE_SECONDS: past this, a suppression is still logged, but with
#     reason "ambiguous-stale" instead of "ambiguous-child-active" -- pure
#     observability, so a dashboard/test run can see a likely-lost
#     SubagentStop; the suppression itself is identical either way.
#   Child markers are never cleaned up on age (see the module docstring).
CHILD_STALE_SECONDS = 4 * 3600

# Evidence gate (live evidence, 2026-10-01; see the module docstring's
# "EVIDENCE GATE"): written once a Codex PreToolUse is seen carrying agent_id,
# under HOOKS_SUBDIR. Read with a tiny byte cap; anything else counts as no proof.
CODEX_AGENT_ID_PROOF_NAME = "codex-ordinary-agent-id.json"
MAX_PROOF_BYTES = 4096

# Idle-turn skip of the end-turn pass (owner, 2026-10-23; see the module
# docstring's ``stop_check`` entry). A module constant, not a setting: turn the
# whole pass off with the ``stop_check`` trigger switch instead.
END_TURN_SKIP_WHEN_IDLE = True
END_TURN_SKIP_REASON = "skipped: nothing outstanding"
END_TURN_SKIP_TAIL_BYTES = 1024 * 1024
MAX_TRANSCRIPT_PATH_CHARS = 2048
# Tools that leave nothing to check. Everything else (Bash, PowerShell, Edit,
# Write, NotebookEdit, apply_patch, Agent/Task, SendMessage, Codex ``exec``, any
# tool this list has never heard of) counts as effect-making. An MCP tool is
# read-only only when its own name starts with one of the read verbs below
# (``mcp__server__list_sessions``); a Crosstalk ``send``/``create`` is not.
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "ToolSearch", "WebFetch", "WebSearch",
                             "view_image", "web_search"})
READ_ONLY_MCP_VERBS = frozenset({"get", "list", "read", "search", "query", "status", "find",
                                 "describe", "show", "view", "fetch", "count", "inspect"})
# Codex rollout ``response_item`` payload types that are not an action. Tool
# outputs (``*_output``) follow their call, which is judged on its own row.
CODEX_BENIGN_ITEM_TYPES = frozenset({"message", "reasoning", "web_search_call"})
# A Claude user row that starts a turn carries one of these content blocks; a
# row holding only ``tool_result`` blocks is a tool's answer, not a prompt.
CLAUDE_PROMPT_BLOCK_TYPES = frozenset({"text", "image", "document"})

HOOKS_SUBDIR = "hooks"
STATE_SUBDIR = "state"
CHILDREN_SUBDIR = "children"
ENDTURN_SUBDIR = "endturn"
CHILDREN_MARKER_SCAN_LIMIT = 200
EVENTS_LOG_NAME = "events.jsonl"
MAX_LOG_BYTES = 256 * 1024
LOG_ROTATE_KEEP_LINES = 500
MAX_STATE_FILES = 500
STATE_PRUNE_AGE_SECONDS = 7 * 24 * 3600
STATE_PRUNE_PROBABILITY = 0.05
MAX_STDIN_BYTES = 262144

# Finding 4 (Codex verification, 2026-09-28): real bounds on every file/
# directory read this program performs, so a corrupted or externally-grown
# state/log file, or a huge state directory, can never make it slow or
# unbounded -- it must stay well inside its documented "never blocks" budget.
MAX_STATE_BYTES = 64 * 1024
MAX_LOG_TAIL_BYTES = 512 * 1024
STATE_PRUNE_SCAN_LIMIT = 2000
# A short, bounded wait for the hook's own settings read (see _hooks_config):
# long enough to ride out a normal, fast settings write, short enough that a
# write that is somehow stuck can never make this program hang.
HOOKS_SETTINGS_LOCK_TIMEOUT_SECONDS = 0.05


# --------------------------------------------------------------- paths/root


def _resolved_root(root) -> Path:
    try:
        return settings._resolve_shared_root(root)
    except Exception:
        try:
            return Path(root).resolve()
        except Exception:
            return Path(".").resolve()


def _hooks_dir(root) -> Path:
    return _resolved_root(root) / ".eutonos-local" / HOOKS_SUBDIR


def _session_key(session_id, agent_id=None) -> str:
    raw = str(session_id or "unknown") + ("\0" + str(agent_id) if agent_id else "")
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _state_path(root, session_id, agent_id=None) -> Path:
    return _hooks_dir(root) / STATE_SUBDIR / (_session_key(session_id, agent_id) + ".json")


# --------------------------------------------------------------------- state


def _load_state(path: Path) -> dict:
    """Finding 4: byte-capped read -- an oversized/corrupted state file (this
    program never writes one that large itself) is treated the same as a
    missing/corrupt one, rather than ever being read in full."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_state(path: Path, value: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            json.dump(value, fh, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def _maybe_prune_state(root) -> None:
    """Bounded, probabilistic cleanup -- never on the hot path for every call.

    Finding 4 (Codex verification, 2026-09-28): the previous implementation
    ``sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)``
    listed and statted EVERY entry in the state directory before doing
    anything -- unbounded if that directory ever grew huge (many long-lived
    sessions). This instead scans at most ``STATE_PRUNE_SCAN_LIMIT`` entries
    via ``os.scandir`` (stopping early rather than listing/sorting the whole
    directory), stats only what it scans, and sorts only that bounded batch
    when trimming down to ``MAX_STATE_FILES``. A directory far larger than
    the scan limit is pruned gradually across successive probabilistic
    passes rather than all at once -- an acceptable trade for staying
    bounded on every single call."""
    if random.random() >= STATE_PRUNE_PROBABILITY:
        return
    for subdir, suffix in ((STATE_SUBDIR, ".json"), (ENDTURN_SUBDIR, ".guard")):
        _prune_dir(_hooks_dir(root) / subdir, suffix)


def _unlink_pruned_file(path, suffix: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        return
    if suffix == ".guard":
        try:
            os.unlink(Path(path).with_suffix(".txt"))
        except OSError:
            pass


def _prune_dir(directory: Path, suffix: str) -> None:
    try:
        if not directory.is_dir():
            return
        now = time.time()
        kept = []
        examined = 0
        with os.scandir(directory) as it:
            for entry in it:
                if examined >= STATE_PRUNE_SCAN_LIMIT:
                    break
                examined += 1
                if not entry.name.endswith(suffix):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                age = now - mtime
                if age > STATE_PRUNE_AGE_SECONDS:
                    _unlink_pruned_file(entry.path, suffix)
                else:
                    kept.append((mtime, entry.path))
        excess = len(kept) - MAX_STATE_FILES
        if excess > 0:
            kept.sort(key=lambda item: item[0])
            for _mtime, path in kept[:excess]:
                _unlink_pruned_file(path, suffix)
    except Exception:
        pass


# ----------------------------------------------------------------- firing log


def _events_log_path(root) -> Path:
    return _hooks_dir(root) / EVENTS_LOG_NAME


def _tail_read(path: Path, max_bytes: int) -> str:
    """Finding 4/5: read at most the last ``max_bytes`` of ``path`` rather
    than the whole file -- this log only ever grows by appending, so the
    newest (most "recent") rows are always at the end, exactly what both
    callers below want.

    BUG FIX (finding 5, coordinator recheck, 2026-09-28): the previous
    version got the file's size from a SEPARATE, earlier ``path.stat()``
    call, then still ended with an unbounded ``fh.read()`` (no argument) --
    so a file that grew between that stat and this read (another process
    still appending, e.g. this same log rotating concurrently) could still
    be read in full, past the intended cap. This version takes the size from
    the SAME open handle via ``seek(0, SEEK_END)``/``tell()`` -- no separate
    stat call to go stale -- and reads with EXACTLY ONE bounded
    ``fh.read(max_bytes)`` call; no ``readline()``/bare ``read()`` ever
    touches the filesystem. Growth during this call cannot make it read more
    than ``max_bytes`` bytes, no matter how large the file becomes in the
    meantime. A byte offset can land mid-line; the leading partial line (if
    any) is trimmed from the ALREADY-READ, already-bounded ``data`` in
    memory -- never by reading anything further from the file."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        offset = size - max_bytes if size > max_bytes else 0
        fh.seek(offset)
        data = fh.read(max_bytes)
    text = data.decode("utf-8", "replace")
    if offset > 0:
        newline = text.find("\n")
        text = text[newline + 1:] if newline != -1 else ""
    return text


def _rotate_log(path: Path) -> None:
    try:
        lines = _tail_read(path, MAX_LOG_TAIL_BYTES).splitlines()
        keep = lines[-LOG_ROTATE_KEEP_LINES:]
        tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            for line in keep:
                fh.write(line + "\n")
        os.replace(tmp, path)
    except Exception:
        pass


def _log_event(root, *, app, session_id, agent_id, role, set_name, trigger, fired, reason) -> None:
    """Metadata only -- NEVER prompt/reminder text. Best-effort; never raises."""
    try:
        path = _events_log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "app": app if isinstance(app, str) else None,
            "session": _session_key(session_id, agent_id),
            "has_agent_id": isinstance(agent_id, str) and bool(agent_id),
            "role": role if isinstance(role, str) else None,
            "set": set_name if isinstance(set_name, str) else None,
            "trigger": trigger,
            "fired": bool(fired),
            "reason": reason if isinstance(reason, str) else None,
        }
        line = json.dumps(row, sort_keys=True, ensure_ascii=False)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_LOG_BYTES:
            _rotate_log(path)
    except Exception:
        pass


def read_recent_events(root, limit=100) -> list:
    """Dashboard read path: metadata-only rows, newest first. Never raises.

    Finding 4 (Codex verification, 2026-09-28): tail-read (see ``_tail_read``)
    rather than a whole-file read -- this is also the dashboard's own log
    reader (``dashboard_cli``'s ``/api/v1/hooks/recent`` calls this same
    function), so a log that somehow grew past ``MAX_LOG_BYTES`` before
    rotation caught it can never make either caller read it in full."""
    try:
        limit = max(1, min(int(limit), LOG_ROTATE_KEEP_LINES))
    except (TypeError, ValueError):
        limit = 100
    try:
        path = _events_log_path(root)
        if not path.is_file():
            return []
        lines = _tail_read(path, MAX_LOG_TAIL_BYTES).splitlines()
        rows = []
        for line in lines[-limit:]:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        rows.reverse()
        return rows[:limit]
    except Exception:
        return []


# -------------------------------------------------------------- role/config


def _resolve_role(payload: dict) -> tuple[str, bool]:
    """Return ``(role, is_subagent)``. Never raises."""
    agent_id = payload.get("agent_id") if isinstance(payload, dict) else None
    if agent_id:
        agent_type = payload.get("agent_type") if isinstance(payload, dict) else None
        role = AGENT_TYPE_ROLE_HINTS.get(agent_type, DEFAULT_CHILD_ROLE) if isinstance(agent_type, str) else DEFAULT_CHILD_ROLE
        return role, True
    env_role = os.environ.get(ROLE_ENV)
    if isinstance(env_role, str) and env_role.strip():
        return env_role.strip(), False
    return "orchestrator", False


def _hooks_config(root):
    """Returns the validated ``hooks`` settings dict, or ``None`` on any error.

    Finding 4 (Codex verification, 2026-09-28): the read takes the same
    shared ``local_lock`` a concurrent settings WRITE (update_roles/
    update_hooks/...) holds for the length of its own read/validate/write
    transaction -- normally brief, but this hook program must never wait on
    it regardless. A short, bounded timeout means a hook event that lands
    mid-write degrades to "no reminder this time" (``None``, handled the same
    as any other settings error by every caller of this function) instead of
    ever blocking the wrapping app on someone else's settings write.

    Finding 5 (coordinator recheck, 2026-09-28): uses
    ``settings.read_hooks_bounded`` (a capped, hooks-only read), NOT
    ``settings.read_hooks`` -- the latter reads the WHOLE settings.json file
    unconditionally via ``_local``, which is correct for every ordinary
    settings consumer but not for this hook program's own hot path (see
    ``read_hooks_bounded``'s docstring)."""
    try:
        return settings.read_hooks_bounded(root, timeout=HOOKS_SETTINGS_LOCK_TIMEOUT_SECONDS)
    except Exception:
        return None


def _active_set_roles(hooks_cfg) -> dict:
    sets = hooks_cfg.get("sets") if isinstance(hooks_cfg, dict) else None
    active_name = hooks_cfg.get("active_set") if isinstance(hooks_cfg, dict) else None
    active = (sets or {}).get(active_name) if isinstance(sets, dict) else None
    if not isinstance(active, dict):
        active = (sets or {}).get(settings.BUILTIN_HOOK_SET_NAME) if isinstance(sets, dict) else None
    roles = active.get("roles") if isinstance(active, dict) else None
    return roles if isinstance(roles, dict) else {}


def _text_for(hooks_cfg, role, trigger) -> tuple[str | None, str]:
    if not isinstance(hooks_cfg, dict):
        return None, "hooks settings unavailable"
    if hooks_cfg.get("enabled") is not True:
        return None, "hooks disabled globally"
    if hooks_cfg.get("role_enabled", {}).get(role, True) is not True:
        return None, "role disabled"
    if hooks_cfg.get("trigger_enabled", {}).get(trigger, True) is not True:
        return None, "trigger disabled"
    roles = _active_set_roles(hooks_cfg)
    texts = roles.get(role) or roles.get("default") or {}
    text = texts.get(trigger) if isinstance(texts, dict) else None
    if not isinstance(text, str) or not text:
        return None, "no text configured for this role/trigger"
    return text, "ok"


def _bounded(text) -> str | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return text[: settings.MAX_HOOK_TEXT_CHARS]


# End-turn pass (owner, 2026-10-05): every main or child turn gets ONE extra
# pass at Stop/SubagentStop carrying the role's ``stop_check`` text. The tag
# carries a per-turn nonce so a Codex continuation, if delivered as a
# synthetic UserPromptSubmit, does not reset the one-pass guard.
END_TURN_TAG = "[eutonos-end-turn:{nonce}]"
END_TURN_HEADER = "One end-turn pass. If nothing below applies, finish normally."
END_TURN_COMPACT_CUE = "Final check\u2026"
END_TURN_KEY_CHARS = 200  # longest first sentence the compact cue's title carries
END_TURN_RECORDING_POLICY = (
    "Record what the next agent would want to know. Routine chat needs nothing; when unsure, "
    "a small RAM entry is fine. A completed tracked stage or slice also gets its board "
    "closeout (a commit or push alone is not completion). Report meaningful choices as **Decided:** lines. "
    "Keep bookkeeping quiet."
)
END_TURN_AUTHORITY = (
    "Retry only within authority: never repeat a denied action, override an owner halt, or "
    "resend an action whose delivery is uncertain before checking its state."
)
# Added to the end-turn pass only while the optional Simon part is switched on in the EUTONOS home (``_simon_line``).
END_TURN_SIMON = (
    "If native computer use failed or was blocked, the Simon desktop helper that ships with EUTONOS is available: "
    "`eutonos-simon helper.bridge_status`, then the simon-planner skill; finish with "
    "`eutonos-simon helper.exit --via-app`."
)


def _simon_line() -> str:
    """``END_TURN_SIMON`` when the Simon part is installed and switched on, else ""; never raises."""
    try:
        from . import component_state
        return END_TURN_SIMON if component_state.component_enabled(eutonos_home.home(), "simon", default=False) else ""
    except Exception:
        return ""


MAX_DELIVERY_NOTICE_CHARS = 240

# Pending actions (slice-f331681a S4): one short line, outside the configured text's own cap.
PENDING_LINE_MAX_CHARS = 420
PENDING_LINE_ITEMS = 2
PENDING_ITEM_CHARS = 90
PENDING_UNREADABLE_LINE = "EUTONOS pending actions: the list could not be read. Run: eutonos-install pending list"


def _pending_line(app, is_subagent, state, *, session_start):
    """The pending-actions line for a main chat, or ``None``. Updates ``state["pending_sig"]`` (the caller saves it).
    Never raises; a reminder must not cost the host anything."""
    try:
        if is_subagent:
            return None
        found = pending_actions.summary(eutonos_home.home(), harness=app, limit=PENDING_LINE_ITEMS,
                                        line_chars=PENDING_ITEM_CHARS)
        signature = found["signature"]
        seen = state.get("pending_sig")
        state["pending_sig"] = signature
        if not signature or (not session_start and signature == (seen if isinstance(seen, str) else "")):
            return None
        if found["error"]:
            return PENDING_UNREADABLE_LINE
        shown = "; ".join(f"{item['id']} {item['action']}" for item in found["items"])
        more = f" (+{found['count'] - len(found['items'])} more)" if found["count"] > len(found["items"]) else ""
        return (f"EUTONOS pending actions ({found['count']}): {shown}{more}. "
                "List: eutonos-install pending list; when done: eutonos-install pending done ID.")[:PENDING_LINE_MAX_CHARS]
    except Exception:
        return None


def _delivery_text(root, text):
    # Bound the configured body first. Appending a separate bounded notice
    # preserves custom text and keeps mode/authority guidance from being cut
    # off at the configured body's limit (like the separate end-turn policy).
    body = _bounded(text)
    if not body:
        return None
    try:
        from .git_delivery_native import guidance
        mode = guidance(root)["mode"]
    except Exception:
        mode = "unavailable"  # Optional reminders must not block entry/launch.
    if mode == "manual":
        notice = ("Manual/absent delivery: commit or push scoped work only under existing authorization; "
                  "a milestone or remote grants none. Assess integration separately.")
    elif mode in ("commit-only", "commit+push"):
        notice = ("Automatic delivery (" + mode + "): within existing authority use scoped submit/inspect/recover. "
                  "The configured publisher owns Git; do not stage, commit or push independently.")
    else:
        notice = ("Delivery configuration unavailable: inspect its owner before selecting a route. "
                  "This reminder authorizes no commit, push or automatic submission.")
    return body + "\n" + notice[:MAX_DELIVERY_NOTICE_CHARS]


def safe_reminder_text(root, role, trigger) -> str:
    """Public, safe helper for ``role_router``'s cross-app (baked-in) prompt use.

    Never raises; returns ``""`` for a blank/unknown role, a settings error, a
    disabled switch, or no configured text -- a broken or disabled hooks setup
    must never block assembling a launch prompt."""
    if not isinstance(role, str) or not role:
        return ""
    hooks_cfg = _hooks_config(root)
    text, _reason = _text_for(hooks_cfg, role, trigger)
    return _delivery_text(root, text) or ""


# ------------------------------------------------------------------ triggers


def _reset_long_turn(state: dict) -> None:
    state["tool_count"] = 0


# --------------------------------------------------- active children (finding 1)
#
# Redesigned on coordinator recheck, 2026-09-28: the original design kept an
# ``active_children`` map on the PARENT's own shared state file, updated by a
# read-modify-write (_load_state -> mutate dict -> _save_state). Two
# concurrent SubagentStart calls for the same parent session race: both read
# the same starting state, each adds its own child, and whichever writes
# LAST wins, silently losing the other's entry (and a concurrent parent
# message/session-start save touching the same file has the identical
# problem). Independent marker files remove the shared read-modify-write
# entirely -- each child's own existence is one file, created/deleted by
# only that child's own events, so two children (or a touch and a release)
# can never step on each other.


def _children_root(root) -> Path:
    return _hooks_dir(root) / CHILDREN_SUBDIR


def _children_dir(root, parent_session_id) -> Path:
    return _children_root(root) / _session_key(parent_session_id)


def _child_marker_name(agent_id) -> str:
    return hashlib.sha256(str(agent_id).encode("utf-8", "replace")).hexdigest()[:24] + ".marker"


def _child_marker_path(root, parent_session_id, agent_id) -> Path:
    return _children_dir(root, parent_session_id) / _child_marker_name(agent_id)


def _touch_active_child(root, parent_session_id, agent_id) -> None:
    """Create (or refresh the mtime of) ONE child's own marker file -- no
    shared state to read or rewrite. Refreshing an already-existing marker's
    mtime is harmless; it is NEVER read as a liveness signal (see
    ``_active_children_status``) -- age never means "no longer active",
    corrected on coordinator recheck, 2026-09-28. Best-effort; never raises."""
    if not parent_session_id or not agent_id:
        return
    try:
        path = _child_marker_path(root, parent_session_id, agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)  # updates mtime even if the marker already existed
    except Exception:
        pass


def _release_active_child(root, parent_session_id, agent_id) -> str:
    """``SubagentStop``: delete exactly this child's marker. This is the ONLY
    thing that ends its active/ambiguous status -- see the module
    docstring's "CODEX CHILD IDENTITY" note. Best-effort; never raises."""
    if not parent_session_id or not agent_id:
        return "no-marker"
    try:
        _child_marker_path(root, parent_session_id, agent_id).unlink()
        return "marker-released"
    except FileNotFoundError:
        return "no-marker"
    except Exception:
        return "marker-release-failed"


def _active_children_status(root, parent_session_id, *, now=None) -> tuple:
    """Return ``(has_active_children, reason)``. Bounded ``os.scandir`` over
    this parent's own marker directory (never the whole ``children`` tree).

    Existence of AT LEAST ONE marker file, AT ANY AGE, means active/ambiguous
    -- corrected on coordinator recheck, 2026-09-28: the original design
    treated a marker older than 30 minutes as expired, meaning "no longer
    active" -- but Codex's ordinary child events are exactly the ones that
    lack ``agent_id`` (see the module docstring), so there is no reliable
    heartbeat to refresh a marker FROM those events; a marker's age says
    nothing about whether its child is still running, only about how long
    ago its last KNOWN event (SubagentStart, or another event that happened
    to carry ``agent_id``) was. Silently treating an old marker as "no
    children" would let a single lost ``SubagentStop`` (a crashed child, a
    Codex version that never fires it) quietly turn ambiguous events back
    into confidently-orchestrator ones -- exactly the miscrediting this
    design exists to prevent. So age is NEVER grounds for "no children"; only
    an explicit ``SubagentStop`` (``_release_active_child``) is. Past
    ``CHILD_STALE_SECONDS`` the return reason changes to ``"ambiguous-stale"``
    (log-only, so a dashboard/test run can see a likely-lost stop), but the
    suppression itself -- ``(True, ...)`` -- is identical either way. If
    multiple markers exist, the freshest one decides the reason: a lost stop
    for a DIFFERENT child never downgrades a fresh, clearly-still-running
    child's own ambiguity to look worse (or better) than it is."""
    if not parent_session_id:
        return False, ""
    now = now if now is not None else time.time()
    try:
        directory = _children_dir(root, parent_session_id)
        if not directory.is_dir():
            return False, ""
        newest_age = None
        examined = 0
        with os.scandir(directory) as it:
            for entry in it:
                if examined >= CHILDREN_MARKER_SCAN_LIMIT:
                    break
                examined += 1
                if not entry.name.endswith(".marker"):
                    continue
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if newest_age is None or age < newest_age:
                    newest_age = age
        if newest_age is None:
            return False, ""
        return True, ("ambiguous-stale" if newest_age > CHILD_STALE_SECONDS else "ambiguous-child-active")
    except Exception:
        return False, ""


def _agent_id_proof_path(root) -> Path:
    return _hooks_dir(root) / CODEX_AGENT_ID_PROOF_NAME


def _codex_agent_id_proven(root) -> bool:
    """True once a Codex ordinary event (PreToolUse) was seen carrying
    ``agent_id`` (see the module docstring's "EVIDENCE GATE"). One byte-capped
    read; a missing, corrupt, oversized or wrong-shaped file is NO proof.
    Never raises."""
    try:
        with _agent_id_proof_path(root).open("rb") as fh:
            raw = fh.read(MAX_PROOF_BYTES + 1)
        if len(raw) > MAX_PROOF_BYTES:
            return False
        value = json.loads(raw.decode("utf-8"))
        return isinstance(value, dict) and isinstance(value.get("first_seen"), str) and bool(value["first_seen"])
    except Exception:
        return False


def _note_codex_agent_id(root) -> None:
    """Record the proof (a tiny file naming only the first-seen time; never an
    ID). Written to a temp file and atomically replaced, so a reader never sees
    a partial file; concurrent writers replace identical content, and an
    invalid existing file is repaired. Best-effort; never raises."""
    if _codex_agent_id_proven(root):
        return
    try:
        path = _agent_id_proof_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            json.dump({"first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "event": "PreToolUse"}, fh, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


# ------------------------------------------------------------ end-turn guard


def _end_turn_guard_path(root, session_id, agent_id=None) -> Path:
    return _hooks_dir(root) / ENDTURN_SUBDIR / (_session_key(session_id, agent_id) + ".guard")


def _end_turn_reminder_path(root, session_id, agent_id=None) -> Path:
    return _end_turn_guard_path(root, session_id, agent_id).with_suffix(".txt")


def _write_end_turn_reminder(root, session_id, agent_id, reminder: str) -> Path | None:
    path = _end_turn_reminder_path(root, session_id, agent_id)
    try:
        path.write_text(reminder, encoding="utf-8")
        return path
    except Exception:
        try:
            path.unlink()
        except Exception:
            pass
        return None


def _end_turn_nonce(root, session_id, agent_id=None) -> str | None:
    """The current turn's spent-guard nonce, or ``None``. Byte-capped read."""
    try:
        with _end_turn_guard_path(root, session_id, agent_id).open("rb") as fh:
            raw = fh.read(MAX_PROOF_BYTES + 1)
        if len(raw) > MAX_PROOF_BYTES:
            return None
        nonce = json.loads(raw.decode("utf-8")).get("nonce")
        return nonce if isinstance(nonce, str) and nonce else None
    except Exception:
        return None


def _claim_end_turn(root, session_id, agent_id, nonce) -> str:
    """Create-once guard so concurrent Stop/SubagentStop for one owner block at
    most once per turn. Returns ``claimed``, ``spent`` or ``guard-unavailable``;
    anything but ``claimed`` fails open (no block)."""
    path = _end_turn_guard_path(root, session_id, agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as fh:
            fh.write(json.dumps({"nonce": nonce}).encode("utf-8"))
    except FileExistsError:
        return "spent"
    except Exception:
        return "guard-unavailable"
    return "claimed" if _end_turn_nonce(root, session_id, agent_id) == nonce else "guard-unavailable"


def _reset_end_turn(root, session_id, agent_id=None) -> None:
    for path in (_end_turn_guard_path(root, session_id, agent_id),
                 _end_turn_reminder_path(root, session_id, agent_id)):
        try:
            path.unlink()
        except Exception:
            pass


def _end_turn_key(text) -> str:
    """The reminder's first sentence for the compact cue's hidden title, so a
    reader who skips the linked file still gets the main point. A sentence too
    long for a title is left out whole rather than cut."""
    first = " ".join(str(text or "").split())
    end = first.find(". ")
    first = first[:end + 1] if end != -1 else first
    if len(first) > END_TURN_KEY_CHARS:
        return ""
    return first.replace('"', "'").replace("\\", "/")


def _compact_end_turn_reason(reminder_path: Path, nonce: str, text="") -> str:
    target = reminder_path.as_posix().replace("<", "%3C").replace(">", "%3E")
    key = _end_turn_key(text)
    lead = key + " " if key else ""
    return (f'[{END_TURN_COMPACT_CUE}](<{target}> '
            f'"{lead}Read this checklist and apply it once. {END_TURN_TAG.format(nonce=nonce)}")')


def _is_end_turn_continuation(root, payload, session_id, agent_id, hooks_cfg, role, is_subagent) -> bool:
    nonce = _end_turn_nonce(root, session_id, agent_id)
    prompt = payload.get("prompt")
    if not nonce or not isinstance(prompt, str):
        return False
    prompt = prompt.strip()
    text, _reason = _text_for(hooks_cfg, role, "stop_check")
    if prompt == _compact_end_turn_reason(_end_turn_reminder_path(root, session_id, agent_id), nonce, text):
        return True
    return bool(text) and prompt == _end_turn_reason(text, hooks_cfg, nonce, is_subagent=is_subagent, root=root)


def _end_turn_reason(text, hooks_cfg, nonce, *, is_subagent, root=None) -> str:
    from .completion_policy import CONTRACT
    parts = [END_TURN_TAG.format(nonce=nonce) + " " + END_TURN_HEADER, _bounded(text)]
    # One shared policy accompanies the selected text, including custom sets.
    # It adds no trigger, proof check, automatic Git action or extra pass.
    parts.append(CONTRACT)
    # Recording is the main chat's job; a child reports to its coordinator.
    if not is_subagent and hooks_cfg.get("closeout_check", True):
        parts.append(END_TURN_RECORDING_POLICY)
    if root is not None:
        parts[1] = _delivery_text(root, parts[1])
    parts.append(_simon_line())
    parts.append(END_TURN_AUTHORITY)
    return "\n".join(part for part in parts if part)


def _handle_session_start(root, payload, hooks_cfg, *, app) -> dict:
    role, is_subagent = _resolve_role(payload)
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id") if is_subagent else None
    event_name = payload.get("hook_event_name")
    state_path = _state_path(root, session_id, agent_id)
    state = _load_state(state_path)
    _reset_long_turn(state)
    pending = _pending_line(app, is_subagent, state, session_start=True)
    _save_state(state_path, state)
    _reset_end_turn(root, session_id, agent_id)
    set_name = hooks_cfg.get("active_set") if isinstance(hooks_cfg, dict) else None
    text, reason = _text_for(hooks_cfg, role, "session_start")
    text = _delivery_text(root, text)
    fired = bool(text)
    _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
               trigger="session_start", fired=fired, reason=reason)
    combined = "\n".join(part for part in (text, pending) if part)
    if not combined:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": combined}}


CROSSTALK_LETTER_PREFIXES = ("Crosstalk message from ", "<cross-session-message")


def _is_crosstalk_letter(prompt) -> bool:
    return isinstance(prompt, str) and prompt.lstrip().startswith(CROSSTALK_LETTER_PREFIXES)


def _handle_user_prompt_submit(root, payload, hooks_cfg, *, app) -> dict:
    role, is_subagent = _resolve_role(payload)
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id") if is_subagent else None
    set_name = hooks_cfg.get("active_set") if isinstance(hooks_cfg, dict) else None
    if _is_end_turn_continuation(root, payload, session_id, agent_id, hooks_cfg, role, is_subagent):
        # If Codex delivers the continuation through UserPromptSubmit, keep
        # the spent guard and do not repeat the message reminder.
        _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                   trigger="message", fired=False, reason="end-turn-continuation")
        return {}
    state_path = _state_path(root, session_id, agent_id)
    state = _load_state(state_path)
    _reset_long_turn(state)
    pending = _pending_line(app, is_subagent, state, session_start=False)
    _save_state(state_path, state)
    _reset_end_turn(root, session_id, agent_id)
    if _is_crosstalk_letter(payload.get("prompt")) and session_id and not is_subagent:
        # A turn that only answers a Crosstalk letter gets no end-turn pass: the
        # pass is a second model turn and made recipients reply twice. Claiming
        # the once-per-turn guard here makes Stop fail open ("spent").
        _claim_end_turn(root, session_id, agent_id, "letter-turn")
    text, reason = _text_for(hooks_cfg, role, "message")
    text = _delivery_text(root, text)
    fired = bool(text)
    _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
               trigger="message", fired=fired, reason=reason)
    combined = "\n".join(part for part in (text, pending) if part)
    if not combined:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": combined}}


def _handle_pre_tool_use(root, payload, hooks_cfg, *, app) -> dict:
    role, is_subagent = _resolve_role(payload)
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id") if is_subagent else None
    tool_name = payload.get("tool_name")
    set_name = hooks_cfg.get("active_set") if isinstance(hooks_cfg, dict) else None

    # Codex child identity (finding 1, Codex verification, 2026-09-28): see the
    # module docstring's "CODEX CHILD IDENTITY" note. Never for Claude, whose
    # agent_id is documented as present on ordinary events too -- only a Codex
    # event with no agent_id, while this session has a marker for a child that
    # has not (yet, or ever) reported SubagentStop, is ambiguous: it might
    # actually be that child's own action mislabeled with the parent's
    # session_id. Suppress edit_nudge/long_turn for this call rather than risk
    # crediting or nudging the wrong actor, and do not advance the counter --
    # with no active child, resolution is unchanged (never made neutral). The
    # marker's age never decides "no children" (corrected on coordinator
    # recheck, 2026-09-28) -- only its reason string reflects staleness.
    # Evidence gate (live evidence, 2026-10-01): a Codex PreToolUse carrying
    # agent_id records the proof; once it exists, an event WITHOUT agent_id is
    # the orchestrator's own and is never ambiguous (markers are then
    # observability only) -- see the module docstring's "EVIDENCE GATE".
    if app == "codex" and is_subagent:
        _note_codex_agent_id(root)
    if app == "codex" and not is_subagent and not _codex_agent_id_proven(root):
        has_active_child, child_reason = _active_children_status(root, session_id)
        if has_active_child:
            _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                       trigger="long_turn", fired=False, reason=child_reason)
            if isinstance(tool_name, str) and tool_name in EDIT_TOOL_NAMES:
                _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                           trigger="edit_nudge", fired=False, reason=child_reason)
            return {}

    state_path = _state_path(root, session_id, agent_id)
    state = _load_state(state_path)

    # --- long_turn: counted for every role, every tool call ---
    threshold = hooks_cfg.get("long_turn_threshold", settings.DEFAULT_LONG_TURN_THRESHOLD) \
        if isinstance(hooks_cfg, dict) else settings.DEFAULT_LONG_TURN_THRESHOLD
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
        threshold = settings.DEFAULT_LONG_TURN_THRESHOLD
    count = state.get("tool_count")
    count = count + 1 if isinstance(count, int) and not isinstance(count, bool) else 1
    long_turn_text = None
    if count >= threshold:
        text, reason = _text_for(hooks_cfg, role, "long_turn")
        text = _delivery_text(root, text)
        _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                   trigger="long_turn", fired=bool(text), reason=reason)
        if text:
            long_turn_text = text
        count = 0
    state["tool_count"] = count

    # --- edit_nudge: main thread only, rate-limited ---
    edit_text = None
    if not is_subagent and isinstance(tool_name, str) and tool_name in EDIT_TOOL_NAMES:
        rate_limit_minutes = hooks_cfg.get("edit_nudge_rate_limit_minutes", settings.DEFAULT_EDIT_NUDGE_RATE_LIMIT_MINUTES) \
            if isinstance(hooks_cfg, dict) else settings.DEFAULT_EDIT_NUDGE_RATE_LIMIT_MINUTES
        if not isinstance(rate_limit_minutes, int) or isinstance(rate_limit_minutes, bool) or rate_limit_minutes < 1:
            rate_limit_minutes = settings.DEFAULT_EDIT_NUDGE_RATE_LIMIT_MINUTES
        last = state.get("edit_nudge_last")
        now = time.time()
        if isinstance(last, (int, float)) and not isinstance(last, bool) and (now - last) < rate_limit_minutes * 60:
            _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                       trigger="edit_nudge", fired=False, reason="rate-limited")
        else:
            text, reason = _text_for(hooks_cfg, role, "edit_nudge")
            text = _delivery_text(root, text)
            _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                       trigger="edit_nudge", fired=bool(text), reason=reason)
            if text:
                edit_text = text
                state["edit_nudge_last"] = now

    _save_state(state_path, state)
    combined = "\n".join(t for t in (edit_text, long_turn_text) if t)
    if not combined:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           # At most two independently bounded bodies/notices.
                                           "additionalContext": combined[:2 * (settings.MAX_HOOK_TEXT_CHARS + 1
                                                                               + MAX_DELIVERY_NOTICE_CHARS) + 1]}}


# ------------------------------------------------- idle-turn skip (2026-10-23)
#
# Question answered: did this turn leave anything to check? Only a positive,
# recognizable "no" skips the pass. Every other outcome (no path, unreadable
# file, bad JSON, an unfamiliar row shape, a start too far back to see, a
# compaction in the middle) keeps it.

_TURN_IDLE, _TURN_ACTIVE, _TURN_UNKNOWN = "idle", "active", "unknown"


def _tool_is_effectful(name) -> bool:
    """True unless ``name`` is a known read-only tool. Unknown means effectful."""
    if not isinstance(name, str) or not name:
        return True
    if name in READ_ONLY_TOOLS:
        return False
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3 and parts[-1]:
            return parts[-1].split("_")[0].lower() not in READ_ONLY_MCP_VERBS
    return True


def _parse_transcript_rows(lines):
    """Yield parsed rows newest first; a blank line is skipped and any line that
    is not a JSON object yields ``None`` (the caller treats it as unknown)."""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            yield None
            return
        if not isinstance(row, dict):
            yield None
            return
        yield row


def _claude_turn_state(lines) -> str:
    """Walk a Claude Code transcript tail from the newest row back to the last
    real user prompt. ``active`` as soon as an effect-making tool call is seen;
    ``idle`` on reaching the prompt first. Rows that replay or annotate
    (``api-request*``, attachments, meta rows such as a Stop hook's own
    feedback, a subagent's sidechain rows) are not the main thread's actions."""
    for row in _parse_transcript_rows(lines):
        if row is None:
            return _TURN_UNKNOWN
        if row.get("isSidechain") is True:
            continue
        kind = row.get("type")
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "assistant":
            if isinstance(content, str):
                continue
            if not isinstance(content, list):
                return _TURN_UNKNOWN
            for block in content:
                block_type = block.get("type") if isinstance(block, dict) else None
                if isinstance(block_type, str) and block_type.endswith("tool_use") \
                        and _tool_is_effectful(block.get("name")):
                    return _TURN_ACTIVE
        elif kind == "user":
            if row.get("isMeta") is True:
                continue
            if row.get("isCompactSummary") is True:
                return _TURN_UNKNOWN
            if isinstance(content, str):
                return _TURN_IDLE if content.strip() else _TURN_UNKNOWN
            if not isinstance(content, list) or not content or not all(isinstance(b, dict) for b in content):
                return _TURN_UNKNOWN
            kinds = {b.get("type") for b in content}
            if kinds & CLAUDE_PROMPT_BLOCK_TYPES:
                return _TURN_IDLE
            if kinds != {"tool_result"}:
                return _TURN_UNKNOWN
        elif kind == "system" and row.get("subtype") == "compact_boundary":
            return _TURN_UNKNOWN
    return _TURN_UNKNOWN


def _codex_turn_state(lines, turn_id) -> str:
    """Walk a Codex rollout tail from the newest row back to this turn's
    ``task_started``. Codex's own code mode runs every command through ``exec``,
    so a turn with any tool call other than a known read-only one is active.
    The rollout format is not a stable interface: a row this does not
    recognize as benign counts as an action."""
    for row in _parse_transcript_rows(lines):
        if row is None:
            return _TURN_UNKNOWN
        kind = row.get("type")
        payload = row.get("payload")
        if kind == "compacted":
            return _TURN_UNKNOWN
        if kind == "event_msg":
            if isinstance(payload, dict) and payload.get("type") == "task_started":
                if isinstance(turn_id, str) and turn_id and payload.get("turn_id") != turn_id:
                    return _TURN_UNKNOWN
                return _TURN_IDLE
            continue
        if kind != "response_item":
            continue
        item_type = payload.get("type") if isinstance(payload, dict) else None
        if not isinstance(item_type, str):
            return _TURN_UNKNOWN
        if item_type in CODEX_BENIGN_ITEM_TYPES or item_type.endswith("_output"):
            continue
        if item_type in ("function_call", "custom_tool_call") and not _tool_is_effectful(payload.get("name")):
            continue
        return _TURN_ACTIVE
    return _TURN_UNKNOWN


def _end_turn_transcript(payload, is_subagent):
    """The transcript file the Stop payload names, or ``None``. A Codex child's
    own rollout is ``agent_transcript_path``; ``transcript_path`` there would be
    its parent's. Must be a plain ``.jsonl`` file."""
    value = payload.get("agent_transcript_path" if is_subagent else "transcript_path")
    if (not isinstance(value, str) or not value or len(value) > MAX_TRANSCRIPT_PATH_CHARS
            or "\0" in value or not value.lower().endswith(".jsonl")):
        return None
    path = Path(value)
    return path if path.is_file() else None


def _turn_has_nothing_outstanding(payload, *, app, is_subagent) -> bool:
    """True only when the transcript positively shows a turn with no file edit
    and no effect-making tool call. One byte-capped tail read; never raises."""
    try:
        path = _end_turn_transcript(payload, is_subagent)
        if path is None:
            return False
        lines = _tail_read(path, END_TURN_SKIP_TAIL_BYTES).split("\n")  # not splitlines: U+2028 in JSON text
        state = _codex_turn_state(lines, payload.get("turn_id")) if app == "codex" else _claude_turn_state(lines)
        return state == _TURN_IDLE
    except Exception:
        return False


def _handle_stop(root, payload, hooks_cfg, *, app) -> dict:
    """One end-turn pass per turn (owner, 2026-10-05). Blocking is the only way
    either app shows Stop text to the model, and it costs one continuation;
    ``stop_hook_active`` plus the create-once guard keep it to exactly one.
    There is no closeout proof check: the model picks the recording category.
    A turn that left nothing to check gets no pass (owner, 2026-10-23)."""
    role, is_subagent = _resolve_role(payload)
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id") if is_subagent else None
    set_name = hooks_cfg.get("active_set") if isinstance(hooks_cfg, dict) else None

    def log(fired, reason):
        _log_event(root, app=app, session_id=session_id, agent_id=agent_id, role=role, set_name=set_name,
                   trigger="stop_check", fired=fired, reason=reason)

    if payload.get("stop_hook_active"):
        log(False, "already-continued")
        return {}
    if not isinstance(session_id, str) or not session_id:
        log(False, "identity-unavailable")
        return {}
    text, reason = _text_for(hooks_cfg, role, "stop_check")
    if not text:
        log(False, reason)
        return {}
    if END_TURN_SKIP_WHEN_IDLE and _turn_has_nothing_outstanding(payload, app=app, is_subagent=is_subagent):
        log(False, END_TURN_SKIP_REASON)
        return {}
    nonce = os.urandom(6).hex()
    claim = _claim_end_turn(root, session_id, agent_id, nonce)
    if claim != "claimed":
        log(False, claim)
        return {}
    log(True, "ok")
    full_reason = _end_turn_reason(text, hooks_cfg, nonce, is_subagent=is_subagent, root=root)
    if app == "codex":
        reminder_path = _write_end_turn_reminder(root, session_id, agent_id, full_reason)
        if reminder_path is not None:
            return {"decision": "block", "reason": _compact_end_turn_reason(reminder_path, nonce, text)}
    return {"decision": "block", "reason": full_reason}


def _handle_subagent_stop(root, payload, hooks_cfg, *, app) -> dict:
    """``SubagentStop`` (finding 1, Codex verification, 2026-09-28): release
    the ending child's own marker file (see the module docstring's "CODEX
    CHILD IDENTITY" note) -- this is the ONLY thing that ends its
    active/ambiguous status; age never does. Fires no reminder text of its
    own -- ``SubagentStart`` already fired this child's session_start
    reminder -- this exists only to keep the parent's ambiguity tracking
    accurate and to restore main behavior immediately once the last child has
    confirmed stopping. A metadata-only row records the result. For a Codex
    child the end-turn pass may continue it once first; its marker then stays
    active until the child's next stop. A Claude subagent ends its run with a
    handback call, so a block arrives after its report is delivered and never
    continues it (live, Claude Code 2.1.286, 2026-10-05): no pass there."""
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    if app != "claude":
        out = _handle_stop(root, payload, hooks_cfg, app=app)
        if out:
            return out
    reason = _release_active_child(root, session_id, agent_id)
    role, is_subagent = _resolve_role(payload)
    set_name = hooks_cfg.get("active_set") if isinstance(hooks_cfg, dict) else None
    _log_event(root, app=app, session_id=session_id, agent_id=agent_id if is_subagent else None,
               role=role if is_subagent else None, set_name=set_name,
               trigger="subagent_stop", fired=False, reason=reason)
    return {}


def _observe_lifecycle_activity(root, payload, *, app, stop_blocked=False):
    # Lifecycle observation respects tracking, independently of reminder switches.
    # It emits a tiny metadata file only; no removed Stop RAM/checkpoint hooks.
    try:
        from .desktop_agent_activity import observe
        from .companion_history import tracking_enabled_bounded
        role, child = _resolve_role(payload)
        safe = True
        if app == "codex" and not child and not _codex_agent_id_proven(root):
            safe = not _active_children_status(root, payload.get("session_id"))[0]
        activity_root = _resolved_root(root)
        if tracking_enabled_bounded(activity_root) is True:
            observe(activity_root, payload, app=app, role=role,
                    codex_identity_safe=safe, stop_blocked=stop_blocked)
    except Exception:
        pass


def _dispatch(root, payload, *, app) -> dict:
    if not isinstance(payload, dict):
        return {}
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str):
        return {}
    hooks_cfg = _hooks_config(root)
    if event_name not in {"Stop", SUBAGENT_STOP_EVENT}:
        _observe_lifecycle_activity(root, payload, app=app)
    if not isinstance(hooks_cfg, dict) or hooks_cfg.get("enabled") is not True:
        if event_name in {"Stop", SUBAGENT_STOP_EVENT}:
            _observe_lifecycle_activity(root, payload, app=app)
        return {}
    _maybe_prune_state(root)
    # Finding 2 (corrected, coordinator recheck, 2026-09-28): refresh a
    # child's own marker on ANY event that carries both session_id and
    # agent_id -- Claude sends agent_id on ordinary events too, so this
    # keeps a long-lived Claude subagent's marker fresh for the storage
    # sweep above; it is harmless bookkeeping only (see
    # _active_children_status -- age is never a liveness signal). Skipped
    # for SubagentStop itself, whose handler below deletes this same marker
    # a moment later -- touching it first would only be undone immediately.
    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    if event_name != SUBAGENT_STOP_EVENT and session_id and agent_id:
        _touch_active_child(root, session_id, agent_id)
    if event_name in SESSION_START_EVENTS:
        return _handle_session_start(root, payload, hooks_cfg, app=app)
    if event_name == "UserPromptSubmit":
        return _handle_user_prompt_submit(root, payload, hooks_cfg, app=app)
    if event_name == "PreToolUse":
        return _handle_pre_tool_use(root, payload, hooks_cfg, app=app)
    if event_name == "Stop":
        result = _handle_stop(root, payload, hooks_cfg, app=app)
        _observe_lifecycle_activity(root, payload, app=app, stop_blocked=result.get("decision") == "block")
        return result
    if event_name == SUBAGENT_STOP_EVENT:
        result = _handle_subagent_stop(root, payload, hooks_cfg, app=app)
        _observe_lifecycle_activity(root, payload, app=app, stop_blocked=result.get("decision") == "block")
        return result
    return {}


# ----------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eutonos.role_hooks",
                                     description="EUTONOS role reminder hook (Claude Code and Codex)")
    parser.add_argument("--app", required=True, choices=APPS)
    parser.add_argument("--root", default=".",
                        help="fallback repo root; the hook payload's own cwd is preferred when present")
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        if len(raw) > MAX_STDIN_BYTES:
            sys.stdout.write("{}\n")
            return 0
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            sys.stdout.write("{}\n")
            return 0
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        root = cwd if isinstance(cwd, str) and cwd else args.root
        result = _dispatch(root, payload, app=args.app)
        sys.stdout.write(json.dumps(result) + "\n")
    except Exception:
        try:
            sys.stdout.write("{}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
