"""Bounded, read-only local sources for the EUTONOS desktop Companion.

No dashboard snapshot, metrics backfill, Crosstalk reader with side effects, model,
transcript inventory, or host mutation is invoked. All counts describe retained
records under explicit coverage limits. Old records never acquire invented states.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

FILE_BYTES = 256 * 1024
COMPANION_RESULTS_BYTES = 1024 * 1024
TAIL_BYTES = 512 * 1024
ENTRY_LIMIT = 1000
TOTAL_ENTRY_LIMIT = 4000
FILE_LIMIT = 800
FOLDER_FILE_LIMIT = 64
REGISTRATION_LIMIT = 512
TOTAL_BYTES = 8 * 1024 * 1024
ACTIVE_SECONDS = 120
RECORD_STALE_SECONDS = 24 * 3600


def timestamp(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            # Naive timestamps have no portable time basis.
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def freshness(value, now, stale_after=RECORD_STALE_SECONDS):
    at = timestamp(value)
    if at is None or at > now + 5:
        return 'unknown'
    return 'stale' if now - at > stale_after else 'recent'


def role_group(role):
    # Reviewer is deliberately retained as an additional role.
    if role in ('worker', 'worker-hard', 'worker-routine', 'eutonos'):
        return 'worker'
    return role if role in ('orchestrator', 'director', 'reviewer') else 'unknown'


class Reader:
    """One scan has hard entry/file/byte bounds; cached JSON never masks errors."""
    def __init__(self):
        self.cache = {}
        self.reset()

    def reset(self):
        self.bytes = 0
        self.files = 0
        self.entries_seen = 0
        self.issues = []

    def issue(self, path, code):
        value = {'source': str(path), 'code': code}
        if value not in self.issues:
            self.issues.append(value)

    def json(self, path, optional=False, max_bytes=FILE_BYTES):
        path = Path(path)
        if self.files >= FILE_LIMIT:
            self.issue(path, 'file-limit')
            return None
        self.files += 1
        try:
            stat = path.stat()
            if stat.st_size > max_bytes:
                self.issue(path, 'oversize')
                return None
            key = str(path)
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self.cache.get(key)
            if cached and cached[0] == signature:
                return cached[1]
            if self.bytes + stat.st_size > TOTAL_BYTES:
                self.issue(path, 'byte-limit')
                return None
            with path.open('rb') as stream:
                raw = stream.read(min(max_bytes, TOTAL_BYTES - self.bytes) + 1)
            self.bytes += len(raw)
            if len(raw) > max_bytes or self.bytes > TOTAL_BYTES:
                self.issue(path, 'byte-limit')
                return None
            value = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError('object required')
            if len(self.cache) >= FILE_LIMIT * 2:
                self.cache.clear()
            self.cache[key] = (signature, value)
            return value
        except FileNotFoundError:
            if not optional:
                self.issue(path, 'missing')
        except (OSError, UnicodeError, ValueError, RecursionError):
            self.issue(path, 'unavailable-or-malformed')
        return None

    def entries(self, directory, suffix='.json', optional=False):
        found = []
        try:
            with os.scandir(directory) as entries:
                for number, entry in enumerate(entries):
                    self.entries_seen += 1
                    if number >= ENTRY_LIMIT or self.entries_seen > TOTAL_ENTRY_LIMIT:
                        self.issue(directory, 'entry-limit')
                        break
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(suffix):
                        found.append(Path(entry.path))
        except FileNotFoundError:
            if not optional:
                self.issue(directory, 'missing')
        except OSError:
            self.issue(directory, 'unavailable')
        found = sorted(found, key=lambda p: self.mtime(p), reverse=True)
        if len(found) > FOLDER_FILE_LIMIT:
            self.issue(directory, 'record-limit')
        return found[:FOLDER_FILE_LIMIT]

    @staticmethod
    def mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    def tail(self, path):
        try:
            with Path(path).open('rb') as stream:
                end = stream.seek(0, 2)
                start = max(0, end - TAIL_BYTES)
                stream.seek(start)
                raw = stream.read(min(TAIL_BYTES, max(0, TOTAL_BYTES - self.bytes)))
            self.bytes += len(raw)
            if len(raw) < end - start:
                self.issue(path, 'byte-limit')
            if start:
                raw = raw.partition(b'\n')[2]
            rows = []
            for line in raw.splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                    else:
                        self.issue(path, 'malformed-event')
                except (ValueError, UnicodeError, RecursionError):
                    self.issue(path, 'malformed-event')
            return rows, bool(start)
        except FileNotFoundError:
            self.issue(path, 'missing')
        except OSError:
            self.issue(path, 'unavailable')
        return [], True


class Collector:
    def __init__(self, root, host=None):
        self.root = Path(root).resolve()
        self.host = Path(host).resolve() if host else None
        self.reader = Reader()

    def _resolution_source(self, section, source):
        path = Path(source)
        return path.name in ('capture', 'workflow', 'write-retry.sqlite')

    def collect(self, now=None):
        now = time.time() if now is None else now
        self.reader.reset()
        sections = {}
        for name in ('records', 'crosstalk', 'agents'):
            start = len(self.reader.issues)
            try:
                sections[name] = getattr(self, '_' + name)(now)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, OverflowError):
                self.reader.issue(self.root, name + '-unavailable')
                sections[name] = {'status': 'unknown'}
            sections[name]['diagnostics'] = self.reader.issues[start:]
            if sections[name]['diagnostics']:
                sections[name]['coverage_detail'] = sections[name].get('coverage', 'unknown')
                sections[name]['coverage'] = 'partial'
            section = sections[name]
            section['count_basis'] = 'unknown' if section.get('status') in ('unknown', 'missing') else 'partial' if section['diagnostics'] else 'observed'
            # A capped resolution source cannot prove historical failure unresolved.
            incomplete = any(self._resolution_source(name, issue['source']) and issue['code'] in ('entry-limit', 'record-limit', 'file-limit', 'byte-limit', 'query-budget', 'oversize', 'unavailable', 'unavailable-or-malformed', 'unavailable-or-locked', 'invalid-request', 'invalid-reply') for issue in section['diagnostics'])
            if incomplete and section.get('problems'):
                section['inspection'] = [{**problem, 'detail': problem['detail'] + '; historical resolution unknown because coverage is partial'} for problem in section['problems']]
                section['problems'] = []
        issues = list(self.reader.issues)
        attention = sections['records'].get('problems', []) + sections['crosstalk'].get('problems', [])
        unknown = bool(issues or any(s.get('status') in ('unknown', 'missing') for s in sections.values()))
        return {'profile': 'eutonos.desktop-companion.v1', 'observed_at': now,
                'root': str(self.root), **sections, 'attention': attention,
                'health': 'attention' if attention else 'unknown' if unknown else 'observed',
                'diagnostics': issues, 'io': {'files': self.reader.files, 'bytes': self.reader.bytes}}

    def _crosstalk(self, now):
        """Crosstalk controls, helper liveness and letters in transit, read-only: controls.json,
        the helper heartbeat and a bounded journal tail through ``crosstalk_status`` (metadata
        only, never letter bodies). Any failure here is ``unknown`` for this section alone."""
        r = self.reader
        try:
            from . import crosstalk_status as ct
            base = ct.device_dir()
            if not ct.installed():
                r.issue(base, 'missing')
                return {'status': 'missing', 'installed': False, 'coverage': 'unknown', 'problems': []}
            controls = ct.controls()
            letters = ct.recent_letters(32)
            helper = ct.helper_status(now)
            wake_notice = ct.wake_budget_notice()
            in_transit = ct.in_transit(letters, now)
        except Exception:
            r.issue(self.root, 'crosstalk-unavailable')
            return {'status': 'unknown', 'coverage': 'unknown', 'problems': []}
        # A held letter is common (accepted by the transport, verification pending) and not
        # an attention item; the journal and crosstalk_p0_cli.py status own that detail.
        return {'status': 'observed', 'installed': True, 'coverage': 'bounded', 'messaging': controls['messaging'],
                'halted': controls['halt'], 'denied': len(controls['deny']), 'in_transit': in_transit,
                'in_flight': in_transit, 'helper': helper, 'wake_budget_notice': wake_notice,
                'recent_letters': letters[:8], 'problems': [], 'source': str(base / 'journal.jsonl')}

    def _write_rows(self, dbpath):
        """Bound the read-only query itself and materialized receipt payloads."""
        r = self.reader
        deadline = time.monotonic() + .1
        steps = 0
        def stop():
            nonlocal steps
            steps += 1000
            return steps >= 100_000 or time.monotonic() >= deadline
        rows = []
        db = sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True, timeout=.1)
        try:
            db.set_progress_handler(stop, 1000)
            metadata = db.execute('SELECT substr(id,1,128),substr(kind,1,64),substr(state,1,32),updated_at,substr(error,1,512),length(CAST(result AS BLOB)),length(id)>128 FROM requests WHERE root=? ORDER BY updated_at DESC LIMIT 101',
                                  (str(self.root),)).fetchall()
            if len(metadata) > 100:
                r.issue(dbpath, 'record-limit')
            for rid, kind, state, updated, error, size, long_id in metadata[:100]:
                metadata_size = len(str((rid, kind, state, updated, error)).encode('utf-8'))
                if metadata_size > TOTAL_BYTES - r.bytes:
                    r.issue(dbpath, 'byte-limit')
                    break
                r.bytes += metadata_size
                raw, oversized = None, bool(size and size > FILE_BYTES or long_id)
                if oversized:
                    r.issue(dbpath, 'oversize-result')
                elif size:
                    if size > TOTAL_BYTES - r.bytes:
                        r.issue(dbpath, 'byte-limit')
                        oversized = True
                    else:
                        item = db.execute('SELECT result FROM requests WHERE id=? AND root=? LIMIT 1', (rid, str(self.root))).fetchone()
                        raw = item[0] if item else None
                        if raw is not None:
                            actual = len(str(raw).encode('utf-8'))
                            if actual > min(FILE_BYTES, TOTAL_BYTES - r.bytes):
                                raw, oversized = None, True
                                r.issue(dbpath, 'byte-limit')
                            else:
                                r.bytes += actual
                rows.append((rid, kind, state, updated, raw, error, oversized))
        except sqlite3.OperationalError as error:
            r.issue(dbpath, 'query-budget' if 'interrupted' in str(error) else 'unavailable-or-locked')
        finally:
            db.close()
        return rows
    def _records(self, now):
        r = self.reader
        result = {'status': 'observed', 'docs': None, 'ram': None, 'problems': [], 'coverage': 'bounded'}
        dbpath = self.host / 'write-retry.sqlite' if self.host else None
        if dbpath is None or not dbpath.is_file():
            r.issue(dbpath or self.root, 'write-receipts-missing')
        else:
            try:
                rows = self._write_rows(dbpath)
                for rid, kind, state, updated, raw, error, oversized in rows:
                    if oversized:
                        r.issue(dbpath, 'oversize-result')
                    category = 'ram' if kind == 'ram-closeout' else 'docs'
                    parsed = {}
                    if raw:
                        if len(raw.encode('utf-8')) <= FILE_BYTES:
                            try:
                                parsed = json.loads(raw)
                            except (ValueError, TypeError):
                                r.issue(dbpath, 'malformed-result')
                        else:
                            r.issue(dbpath, 'oversize-result')
                    phase = 'saved' if state == 'complete' else 'queued' if state in ('pending', 'waiting', 'working') else 'failed' if state == 'failed' else 'unknown'
                    if state == 'complete' and isinstance(parsed, dict):
                        proof = parsed.get('result', parsed)
                        if isinstance(proof, dict) and proof.get('current_verified') is True:
                            phase = 'verified'
                    item = {'id': rid, 'state': phase, 'updated_at': updated,
                            'freshness': freshness(updated, now), 'source': str(dbpath), 'kind': kind}
                    if result[category] is None:
                        result[category] = item
                    # Same queue request ID changes state on retry; old errors are not scanned from history.
                    if phase == 'failed':
                        result['problems'].append({'title': category.upper() + ' write failed',
                                                   'detail': rid + ': ' + str(error or 'Inspect queue receipt'), 'source': str(dbpath)})
                    elif phase == 'queued' and freshness(updated, now, 1800) != 'recent':
                        result['problems'].append({'title': category.upper() + ' write needs inspection',
                                                   'detail': rid + ': queued write has no recent progress', 'source': str(dbpath)})
            except sqlite3.Error:
                r.issue(dbpath, 'unavailable-or-locked')
        # The generated view is evidence of a saved Docs file, never an acceptance receipt.
        current = self.root / 'docs' / 'CURRENT_TASKS.md'
        if current.is_file():
            docs = {'state': 'saved', 'updated_at': r.mtime(current), 'source': str(current),
                    'freshness': freshness(r.mtime(current), now), 'kind': 'native work view'}
            if result['docs'] is None or timestamp(docs['updated_at']) > (timestamp(result['docs']['updated_at']) or 0):
                result['docs'] = docs
        else:
            r.issue(current, 'missing')
        captures = []
        for path in r.entries(self.root / '.eutonos-local' / 'capture', optional=True):
            item = r.json(path)
            if item and item.get('profile') == 'eutonos.companion-capture-receipt.v1':
                captures.append((path, item))
        superseded = {item.get('supersedes') for _, item in captures if item.get('supersedes')}
        completed = {(item.get('thread_id'), item.get('turn_id')) for _, item in captures if item.get('counts_completed')}
        for path, item in captures:
            if item.get('receipt_id') in superseded:
                continue
            at = item.get('recorded_at')
            phase = 'verified' if item.get('counts_completed') and item.get('assertion', {}).get('native_proof') == 'verified-readback' else 'saved' if item.get('disposition') == 'captured' else item.get('disposition', 'unknown')
            candidate = {'state': phase, 'updated_at': at, 'freshness': freshness(at, now), 'source': str(path), 'kind': 'RAM turn receipt'}
            if result['ram'] is None or (timestamp(at) or 0) > (timestamp(result['ram']['updated_at']) or 0):
                result['ram'] = candidate
            if phase == 'gap' and (item.get('thread_id'), item.get('turn_id')) not in completed:
                result['problems'].append({'title': 'RAM receipt gap', 'detail': 'Inspect the recorded missing capture', 'source': str(path)})
        for path in r.entries(self.root / '.eutonos-local' / 'workflow', '.result.json', optional=True):
            item = r.json(path)
            if not item or item.get('profile') != 'eutonos.companion-ram-capture.v1':
                continue
            at = r.mtime(path)
            phase = 'verified' if item.get('outcome') in ('completed', 'already-completed') and item.get('current_verified') is True else 'failed' if item.get('outcome') == 'error' else 'saved' if item.get('outcome') in ('completed', 'already-completed') else 'unknown'
            candidate = {'state': phase, 'updated_at': at, 'freshness': freshness(at, now), 'source': str(path), 'kind': 'RAM effect receipt'}
            if result['ram'] is None or at > (timestamp(result['ram']['updated_at']) or 0):
                result['ram'] = candidate
        if result['ram'] and result['ram'].get('state') == 'failed' and not any(problem.get('source') == result['ram']['source'] for problem in result['problems']):
            result['problems'].append({'title': 'RAM effect receipt failed', 'detail': 'Inspect the latest effect receipt before retrying', 'source': result['ram']['source']})
        for category in ('docs', 'ram'):
            if result[category] is None:
                result[category] = {'state': 'missing', 'freshness': 'unknown', 'source': str(self.root / '.eutonos-local' / ('workflow' if category == 'ram' else 'write-receipts'))}
                r.issue(result[category]['source'], category + '-receipt-missing')
        return result

    def _agents(self, now):
        r = self.reader
        directory = self.root / '.eutonos-local' / 'agent-activity'
        from .companion_history import tracking_enabled_bounded
        tracking_enabled = tracking_enabled_bounded(self.root)
        capture = 'enabled' if tracking_enabled is True else 'disabled' if tracking_enabled is False else 'unknown'
        if tracking_enabled is not True:
            r.issue(directory, 'lifecycle-capture-' + capture)
        rows = {}
        observed_keys, explicit_keys = set(), set()
        for path in r.entries(directory, optional=True):
            item = r.json(path)
            if not item or item.get('profile') != 'eutonos.agent-activity.v1' or not isinstance(item.get('id'), str):
                continue
            state = item.get('state') if tracking_enabled is True else 'unknown'
            fresh = freshness(item.get('updated_at'), now, ACTIVE_SECONDS)
            if state in ('working', 'waiting for reply', 'waiting for owner') and fresh != 'recent':
                state = 'stale / unknown'
            if state not in ('working', 'waiting for reply', 'waiting for owner', 'completed', 'idle / turn finished', 'stale / unknown'):
                state = 'unknown'
            observed_keys.add(item['id'])
            rows[item['id']] = {'id': item['id'], 'registration': 'automatically registered from native observation',
                                'session_id': item.get('session_id'), 'child_id': item.get('child_id'), 'role': item.get('role') or 'unknown',
                                'harness': item.get('harness') or 'unknown', 'assignment': item.get('assignment') or 'not recorded',
                                'state': state, 'active': state in ('working', 'waiting for reply', 'waiting for owner'),
                                'updated_at': item.get('updated_at'), 'source': str(path), 'basis': 'native hook observation'}
        cfgpath = self.root / '.eutonos-local' / 'companion-results.json'
        cfg = r.json(cfgpath, optional=True, max_bytes=COMPANION_RESULTS_BYTES) or {}
        sources = cfg.get('sources', [])
        if not isinstance(sources, list):
            sources = []
            r.issue(cfgpath, 'invalid-registrations')
        if len(sources) > REGISTRATION_LIMIT:
            r.issue(cfgpath, 'registration-limit')
        for source in sources[:REGISTRATION_LIMIT]:
            if not isinstance(source, dict):
                r.issue(cfgpath, 'invalid-registration')
                continue
            # Read only one bounded header of explicitly registered log; no log traversal.
            identity = source.get('thread_id') or source.get('session_id')
            harness = source.get('harness') or 'unknown'
            if not identity and isinstance(source.get('path'), str):
                try:
                    if r.bytes + FILE_BYTES > TOTAL_BYTES or r.files >= FILE_LIMIT:
                        raise ValueError('read budget exhausted')
                    r.files += 1
                    with Path(source['path']).open('rb') as stream:
                        raw = stream.readline(FILE_BYTES + 1)
                    r.bytes += len(raw)
                    if len(raw) > FILE_BYTES:
                        raise ValueError('header oversize')
                    header = json.loads(raw)
                    payload = header.get('payload', {})
                    if header.get('type') == 'session_meta':
                        identity = payload.get('id')
                        harness = 'codex'
                except (OSError, ValueError, TypeError):
                    r.issue(cfgpath, 'registration-identity-unavailable')
            if not isinstance(identity, str):
                r.issue(cfgpath, 'registration-identity-unknown')
                continue
            # Activity files expose native session identity; key is harness + session + child.
            from hashlib import sha256
            child = source.get('agent_id') if isinstance(source.get('agent_id'), str) else ''
            key = sha256((harness + '\0' + identity + '\0' + child).encode()).hexdigest()[:24]
            explicit_keys.add(key)
            if key not in rows:
                rows[key] = {'id': key, 'registration': 'explicit tracking registration', 'session_id': identity, 'child_id': child or None, 'role': source.get('role') or 'unknown', 'harness': harness,
                             'assignment': source.get('work_id') or source.get('assignment') or source.get('experiment_id') or 'not recorded',
                             'state': 'unknown', 'active': False, 'updated_at': None, 'source': str(cfgpath), 'basis': 'explicit registration'}
            else:
                rows[key]['registration'] = 'explicit tracking registration + native observation'
                if rows[key]['assignment'] == 'not recorded':
                    rows[key]['assignment'] = source.get('work_id') or source.get('assignment') or source.get('experiment_id') or 'not recorded'
        counts = {}
        for role in ('orchestrator', 'worker', 'director', 'reviewer', 'unknown'):
            selected = [row for row in rows.values() if role_group(row['role']) == role]
            counts[role] = {'registered': len(selected), 'active': sum(row['active'] for row in selected) if tracking_enabled is True and selected and all(row['state'] not in ('unknown', 'stale / unknown') for row in selected) else None}
        if not rows:
            r.issue(directory, 'activity-coverage-unavailable')
        elif any(row['state'] in ('unknown', 'stale / unknown') for row in rows.values()):
            r.issue(directory, 'lifecycle-state-unknown')
        return {'status': 'observed' if rows else 'unknown', 'lifecycle_capture': capture, 'coverage': f'Lifecycle capture {capture}; {len(explicit_keys)} explicit tracking registrations; {len(observed_keys)} automatically registered native identities; no historical activity backfill',
                'registration_counts': {'explicit': len(explicit_keys), 'observed': len(observed_keys), 'overlap': len(explicit_keys & observed_keys), 'union': len(rows)},
                'counts': counts, 'rows': sorted(rows.values(), key=lambda row: (not row['active'], -(timestamp(row['updated_at']) or 0))),
                'source': str(directory)}


def local_configuration(root):
    """Explicit host-local preference; no configuration is created by observation."""
    root = Path(root).resolve()
    reader = Reader()
    cfg = reader.json(root / '.eutonos-local' / 'desktop-companion.json', optional=True) or {}
    url = cfg.get('dashboard_url')
    valid_url = isinstance(url, str) and urlparse(url).scheme in ('http', 'https') and bool(urlparse(url).hostname)
    return {'root': str(root), 'host': cfg.get('host') if isinstance(cfg.get('host'), str) else None, 'dashboard_url': url if valid_url else None,
            'dashboard_launcher': str(root / 'launch eutonos dashboard.bat') if (root / 'launch eutonos dashboard.bat').is_file() else None,
            'diagnostics': reader.issues}
