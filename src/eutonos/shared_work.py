"""Explicit portable source manifest and local opt-in for shared native work."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window
import time
from urllib.parse import urlsplit

from .private_scope import require_shared
from . import tokenslang as ts
from .effects import LocalRoot
from .local_lock import local_lock
from .source import safe_path

PROFILE = 'eutonos.shared-work.config.v1'
PATH = 'eutonos.shared-work.json'
LOCAL = '.eutonos-local/shared-work.json'
IDENTITY = ('repository_id', 'scope_id', 'namespace')


def _hash(value):
    return hashlib.sha256(ts.encoded(value)).hexdigest()


def _read(path):
    if not path.exists(): return None
    if path.is_symlink() or path.stat().st_size > 131072:
        raise ValueError('Shared-work configuration must be a bounded ordinary file.')
    return json.loads(path.read_bytes())


def transport(url):
    if not isinstance(url, str) or not url or len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise ValueError('Shared source URL is invalid.')
    if Path(url).is_absolute(): return url
    if re.fullmatch(r'[\w.-]+@[\w.-]+:[^\s]+', url): return url
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https', 'ssh', 'git', 'file'} or parsed.password
            or parsed.scheme in {'http', 'https', 'git', 'file'} and parsed.username
            or parsed.query or parsed.fragment
            or parsed.scheme != 'file' and not parsed.hostname
            or parsed.scheme == 'file' and not parsed.path.startswith('/')):
        raise ValueError('Use an explicit Git transport without embedded credentials, query or helper commands.')
    return url


def _source(value, peer=False):
    ts.shape(value, {'url', 'ref'}, {'id'} if peer else set())
    url = transport(value['url'])
    ref = value['ref']
    if (not isinstance(ref, str) or not ref.startswith('refs/heads/') or len(ref) > 256
            or re.search(r'[\s~^:?*\[\\\x00-\x1f\x7f]', ref) or '..' in ref or '@{' in ref
            or any(not part or part.startswith('.') or part.endswith(('.', '.lock')) for part in ref.split('/'))):
        raise ValueError('Configure exact refs/heads refs without wildcards or revision syntax.')
    result = {'url': url, 'ref': ref}
    if peer:
        value = {**value, 'id': value.get('id') or 'peer-' + _hash(result)[:16]}
        if value['id'] == 'canonical': raise ValueError('canonical is a reserved source label.')
        if not isinstance(value['id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value['id']):
            raise ValueError('Peer ID must be a bounded stable label.')
        result['id'] = value['id']
    return result


def validate(root, value):
    ts.shape(value, {'profile', *IDENTITY, 'canonical', 'peers', 'attempt_owner', 'fetch_interval_seconds'})
    if value['profile'] != PROFILE: raise ValueError('Unknown shared-work configuration.')
    local = LocalRoot(root)
    native = json.loads(local.read('eutonos.read.json') or b'{}')
    if any(not isinstance(value[k], str) or not value[k] or value[k] != native.get(k) for k in IDENTITY):
        raise ValueError('Shared manifest must match the native repository/scope/namespace identity.')
    peers = value['peers']
    if not isinstance(peers, list) or len(peers) > 8: raise ValueError('Configure at most eight peer refs.')
    peers = [_source(row, True) for row in peers]
    if len({row['id'] for row in peers}) != len(peers): raise ValueError('Peer IDs must be unique.')
    owner = safe_path(value['attempt_owner'])
    from . import boards
    projection = boards.project(local)
    if not any(row['path'] == owner and row['status'] == 'available' for row in projection.get('owners', [])):
        raise ValueError('Select an existing registered native board owner; create one with boards owner form and batch submit.')
    interval = value['fetch_interval_seconds']
    if type(interval) is not int or not 5 <= interval <= 300: raise ValueError('Fetch interval must be 5..300 seconds.')
    return {**deepcopy(value), 'canonical': _source(value['canonical']), 'peers': peers, 'attempt_owner': owner}


def load(root):
    require_shared(root)
    root = Path(root).resolve()
    portable = _read(root / PATH)
    local = _read(root / LOCAL) or {'enabled': False, 'participant': None}
    if not {'enabled', 'participant'} <= set(local) or not set(local) <= {'enabled', 'participant', 'auto_fetch'} or type(local['enabled']) is not bool:
        raise ValueError('Invalid local shared-work opt-in.')
    local.setdefault('auto_fetch', True)
    if type(local['auto_fetch']) is not bool:
        raise ValueError('Auto-fetch must be a local boolean preference.')
    if local['participant'] is not None and (not isinstance(local['participant'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', local['participant'])):
        raise ValueError('Participant must be a bounded local identity.')
    if portable is None:
        native = _read(root / 'eutonos.read.json') or {}
        portable = {'profile': PROFILE, **{k: native.get(k) for k in IDENTITY}, 'canonical': None,
                    'peers': [], 'attempt_owner': None, 'fetch_interval_seconds': 30}
        if local['enabled']: raise ValueError('Local sharing is enabled but its manifest is absent.')
    else:
        # Reads check shape, identity and sources; a temporarily unavailable owner
        # must not erase the source selection or resurrect another Main.
        ts.shape(portable, {'profile', *IDENTITY, 'canonical', 'peers', 'attempt_owner', 'fetch_interval_seconds'})
        if portable['profile'] != PROFILE: raise ValueError('Unknown shared-work configuration.')
        identity = _read(root / 'eutonos.read.json') or {}
        if any(portable[k] != identity.get(k) for k in IDENTITY): raise ValueError('Shared native identity differs.')
        # An adopter's manifest is empty (no canonical source, no attempt owner); sharing cannot be on with it.
        portable['canonical'] = None if portable['canonical'] is None else _source(portable['canonical'])
        if portable['canonical'] is None and local['enabled']:
            raise ValueError('Local sharing is enabled but the manifest names no canonical source.')
        if not isinstance(portable['peers'], list) or len(portable['peers']) > 8: raise ValueError('Too many peer refs.')
        portable['peers'] = [_source(row, True) for row in portable['peers']]
        if len({p['id'] for p in portable['peers']}) != len(portable['peers']): raise ValueError('Duplicate peer IDs.')
        portable['attempt_owner'] = None if portable['attempt_owner'] is None else safe_path(portable['attempt_owner'])
        if type(portable['fetch_interval_seconds']) is not int or not 5 <= portable['fetch_interval_seconds'] <= 300:
            raise ValueError('Invalid fetch interval.')
    value = {**portable, **local}
    return {**value, 'revision': _hash(value)}


def configure(root, value, expected_revision):
    from .current_work import _atomic, invalidate
    root = Path(root).resolve()
    value = deepcopy(value)
    value.pop('revision', None)
    preference_only = set(value) == {'auto_fetch'}
    has_auto_fetch = 'auto_fetch' in value
    auto_fetch = value.pop('auto_fetch', None)
    if has_auto_fetch and type(auto_fetch) is not bool:
        raise ValueError('Auto-fetch must be a local boolean preference.')
    enabled = value.pop('enabled', False)
    participant = value.pop('participant', None)
    if type(enabled) is not bool or enabled and not participant: raise ValueError('Enabled sharing requires a local participant.')
    if participant is not None and (not isinstance(participant, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', participant)):
        raise ValueError('Participant must be a bounded local identity.')
    portable = None if preference_only else validate(root, value)
    with local_lock(root, timeout=5):
        current = load(root)
        if current['revision'] != expected_revision:
            from .settings import StaleSettings
            raise StaleSettings({'shared_work': current, 'revision': current['revision']})
        # The committed manifest cannot enable another clone. Write the local
        # switch last, and fail visibly if the two ordinary saves are interrupted.
        if preference_only:
            enabled, participant = current['enabled'], current['participant']
        else:
            _atomic(root / PATH, ts.encoded(portable) + b'\n')
        _atomic(root / LOCAL, ts.encoded({'enabled': enabled, 'participant': participant,
                                         'auto_fetch': current['auto_fetch'] if auto_fetch is None else auto_fetch}) + b'\n')
    # Fetch preference is not source withdrawal; keep useful cached pins/history.
    if selection(root) != _hash({k: v for k, v in current.items() if k not in {'participant', 'revision', 'auto_fetch'}}):
        invalidate(root)
    return {'shared_work': load(root), 'revision': load(root)['revision'],
            'native_save': 'configuration-saved', 'git_delivery': {'state': 'not-submitted'},
            'qualification': 'Configuration save is not automatic publication or delivery authorization.'}


def selection(root):
    cfg = load(root)
    return _hash({k: v for k, v in cfg.items() if k not in {'participant', 'revision', 'auto_fetch'}})


def refs(cfg):
    if not cfg['enabled']: return []
    namespace = 'refs/eutonos-shared/' + _hash({k: cfg[k] for k in IDENTITY})[:24] + '/'
    return [{**cfg['canonical'], 'id': 'canonical', 'owned_ref': namespace + 'canonical/' + _hash(cfg['canonical'])[:16]},
            *[{**row, 'owned_ref': namespace + 'peer/' + row['id'] + '/' + _hash({k: row[k] for k in ('url', 'ref')})[:16]} for row in cfg['peers']]]


def _git(root, *args, timeout=5):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1')
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, env=env, timeout=timeout, **no_window())


def _fetch_routing(root, destination, *, timeout=5):
    """Reject Git insteadOf rules redirecting an explicitly selected fetch source."""
    result = _git(root, 'config', '--null', '--get-regexp',
                  r'^url[.].*[.]insteadof', timeout=timeout)
    if result.returncode not in (0, 1):
        raise ValueError('Cannot inspect Git fetch URL routing; source coverage is unknown.')
    for entry in result.stdout.decode().split('\0'):
        if not entry:
            continue
        name, prefix = entry.split('\n', 1)
        replacement = name[4:name.rfind('.')]
        if (destination.startswith(prefix)
                and replacement + destination[len(prefix):] != destination):
            raise ValueError('Git URL rewrite changes the configured shared source; inspect insteadOf settings.')

def observe(root, *, fetch=True):
    """One bounded/coalesced network observation, separate from local scan cache."""
    round_started = time.monotonic()
    cfg = load(root)
    if not cfg['enabled']:
        return {'enabled': False, 'auto_fetch': cfg['auto_fetch'], 'revision': cfg['revision'], 'coverage': 'local-only',
                'canonical': None, 'peers': [], 'fetch_state': 'disabled', 'last_observed_at': None,
                'freshness': {'kind': 'unknown', 'age_seconds': None}, 'diagnostics': []}
    common = _git(root, 'rev-parse', '--git-common-dir')
    if common.returncode: raise ValueError('Shared Git directory unavailable.')
    directory = Path(common.stdout.decode().strip())
    if not directory.is_absolute(): directory = Path(root) / directory
    directory = directory.resolve() / 'eutonos-shared-work'
    key = selection(root)
    path = directory / (key + '.json')
    from .current_work import _atomic
    def result(state, fetch_state):
        records = state.get('sources') or [{**r, 'commit': None, 'status': 'unavailable', 'last_successful_fetch': None} for r in refs(cfg)]
        observed = state.get('attempted_at')
        age = max(0, time.time() - observed) if observed else None
        current = cfg['auto_fetch'] and age is not None and age < cfg['fetch_interval_seconds'] and all(r['status'] == 'available' for r in records)
        return {'enabled': True, 'auto_fetch': cfg['auto_fetch'], 'revision': cfg['revision'], 'coverage': 'complete' if current else 'unknown',
                'coverage_scope': 'configured-sources-only',
                'last_observed_at': datetime.fromtimestamp(observed, timezone.utc).isoformat() if observed else None,
                'freshness': {'kind': 'fresh' if current else 'stale' if observed else 'unknown', 'age_seconds': round(age, 3) if age is not None else None},
                'canonical': next((r for r in records if r['id'] == 'canonical'), None),
                'peers': [r for r in records if r['id'] != 'canonical'], 'fetch_state': fetch_state,
                'diagnostics': state.get('diagnostics', [])}
    if not cfg['auto_fetch'] or not fetch:
        value = result(_read(path) or {}, 'auto-fetch-off' if not cfg['auto_fetch'] else 'cached')
        if not cfg['auto_fetch']:
            value['diagnostics'] = [*value['diagnostics'], 'Auto-fetch is off; cached source pins are retained and current remote coverage is unknown.']
        return value
    try:
        with local_lock(directory, timeout=0.25):
            state = _read(path) or {}
            if time.time() - state.get('attempted_at', 0) < cfg['fetch_interval_seconds']:
                return result(state, 'cached')
            old = {row['id']: row for row in state.get('sources', [])}
            started = round_started
            records, diagnostics = [], []
            for source in refs(cfg):
                row = {**source, 'commit': None, 'status': 'unavailable',
                       'last_successful_fetch': old.get(source['id'], {}).get('last_successful_fetch')}
                remaining = 15 - (time.monotonic() - started)
                try:
                    if remaining <= 0: raise TimeoutError('fetch budget exhausted')
                    _fetch_routing(root, source['url'], timeout=min(5, remaining))
                    remaining = 15 - (time.monotonic() - started)
                    if remaining <= 0: raise TimeoutError('fetch budget exhausted')
                    fetched = _git(root, '-c', 'protocol.ext.allow=never', 'fetch', '--no-tags', '--no-recurse-submodules',
                                   '--no-write-fetch-head', source['url'], '+' + source['ref'] + ':' + source['owned_ref'], timeout=min(3, remaining))
                    if fetched.returncode: raise ValueError('configured ref fetch failed')
                    row['status'] = 'available'
                    row['last_successful_fetch'] = datetime.now(timezone.utc).isoformat()
                except (OSError, ValueError, subprocess.TimeoutExpired, TimeoutError):
                    diagnostics.append('Source ' + source['id'] + ' unavailable; coverage is unknown.')
                remaining = 15 - (time.monotonic() - started)
                try:
                    if remaining <= 0: raise TimeoutError('observation budget exhausted')
                    tip = _git(root, 'rev-parse', '--verify', source['owned_ref'] + '^{commit}', timeout=min(1, remaining))
                    if not tip.returncode: row['commit'] = tip.stdout.decode().strip()
                except (OSError, subprocess.TimeoutExpired, TimeoutError):
                    row['commit'] = old.get(source['id'], {}).get('commit')
                    row['status'] = 'unavailable'
                    diagnostics.append('Source ' + source['id'] + ' tip observation delayed; coverage is unknown.')
                records.append(row)
            state = {'attempted_at': time.time(), 'sources': records, 'diagnostics': diagnostics}
            _atomic(path, ts.encoded(state))
            return result(state, 'fresh' if not diagnostics else 'partial')
    except TimeoutError:
        state = _read(path) or {'sources': [{**r, 'commit': None, 'status': 'unavailable', 'last_successful_fetch': None} for r in refs(cfg)],
                                'diagnostics': ['Another source observation is in progress; coverage is unknown.']}
        value = result(state, 'coalesced')
        value['coverage'] = 'unknown'
        value['diagnostics'] = [*value['diagnostics'], 'Source observation is in progress; coverage is delayed and unknown.']
        return value
