"""Best-effort native lifecycle metadata, independent of reminder text switches.

This emits no RAM, board, model or transcript effect. Existing hook execution is
required; missing hooks are unavailable coverage, never proof of zero activity.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

PROFILE = 'eutonos.agent-activity.v1'
MAX_BYTES = 4096


def observe(root, payload, *, app, role, codex_identity_safe=True, stop_blocked=False):
    try:
        session = payload.get('session_id')
        child = payload.get('agent_id') or ''
        if not isinstance(session, str) or not session or not isinstance(child, str):
            return
        event = payload.get('hook_event_name')
        if event not in ('SessionStart', 'SubagentStart', 'UserPromptSubmit', 'PreToolUse',
                         'PostToolUse', 'Stop', 'SubagentStop', 'PermissionRequest'):
            return
        key = hashlib.sha256((app + '\0' + session + '\0' + child).encode()).hexdigest()[:24]
        state = 'unknown'
        if event in ('UserPromptSubmit', 'PreToolUse', 'PostToolUse'):
            state = 'working'
        if event == 'PreToolUse':
            tool = payload.get('tool_name') or ''
            if tool in ('request_user_input', 'functions.request_user_input', 'AskUserQuestion'):
                state = 'waiting for owner'
            elif tool in ('wait_threads', 'mcp__codex_app__wait_threads', 'functions.wait', 'WaitAgent'):
                state = 'waiting for reply'
        if event == 'PermissionRequest':
            state = 'waiting for owner'
        if event == 'Stop':
            state = 'working' if stop_blocked else 'idle / turn finished'
        if event == 'SubagentStop':
            state = 'working' if stop_blocked else 'completed'
        if app == 'codex' and not child and not codex_identity_safe:
            state = 'unknown'
        value = {'profile': PROFILE, 'id': key, 'role': role or 'unknown',
                 'harness': app, 'session_id': session, 'child_id': child or None,
                 'state': state, 'updated_at': time.time(), 'event': event,
                 'assignment': os.environ.get('EUTONOS_WORK_ID') or None,
                 'basis': 'native hook observation'}
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True).encode() + b'\n'
        if len(raw) > MAX_BYTES:
            return
        directory = Path(root) / '.eutonos-local' / 'agent-activity'
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=key + '-', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(name, directory / (key + '.json'))
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
    except Exception:
        # Optional metadata must never alter the host hook result.
        pass
