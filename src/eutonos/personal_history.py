"""Private local history using the existing delivery journal and native proof.

No native replay, records worktree, remote lookup, fetch or push. An isolated
index freezes canonical bytes; only canonical default-index entries are updated.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from . import git_delivery as delivery, git_delivery_native as native, tokenslang as ts
from .private_scope import private_host, require_owner, validate_private_destination

PATHS = ('eutonos.personal.json', 'goals.md', 'projects.md', 'priorities.json')
CONFIG = delivery.PRIVATE_CONFIG


def _standalone(root):
    """Private journals and objects must belong to this standalone repository."""
    root = Path(root)
    gitdir = root / '.git'
    from .private_scope import require_ordinary_root
    require_ordinary_root(gitdir)
    for path in ('objects', 'objects/info', 'refs', 'index', 'eutonos-delivery', 'eutonos-delivery/journal.sqlite'):
        require_ordinary_root(gitdir / path)
    directories = [gitdir / path for path in ('objects', 'refs', 'eutonos-delivery')]
    examined = 0
    while directories:
        directory = directories.pop()
        require_ordinary_root(directory)
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            examined += 1
            ts.require(examined <= 16384, 'unavailable', 'Private Git storage validation exceeded its bounded scan.')
            require_ordinary_root(child)  # Reject aliases before entering directories.
            if child.is_dir():
                directories.append(child)
    ts.require(gitdir.is_dir() and not gitdir.is_symlink() and not (gitdir / 'commondir').exists()
               and not (gitdir / 'objects/info/alternates').exists(),
               'denied', 'Personal history requires standalone private Git storage.')
    for option in ('--absolute-git-dir', '--git-common-dir'):
        selected = Path(delivery.text(root, 'rev-parse', option))
        if not selected.is_absolute():
            selected = root / selected
        ts.require(selected.resolve() == gitdir.resolve(), 'denied', 'Personal Git storage is outside its private root.')
    promisor = delivery.git(root, 'config', '--get-regexp', r'^remote\..*\.promisor$', check=False)
    ts.require(not promisor.stdout, 'denied', 'Partial private object stores are unsupported.')


def _head(root):
    result = delivery.git(root, 'rev-parse', '--verify', 'HEAD', check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def _validate_configuration(root, value, *, stored=False):
    keys = {'profile', 'project_id', 'participant', 'mode', 'remote_url', 'remote_ref',
            'branch', 'canonical_base', 'allowed_paths', 'checks', 'records_ref'}
    if stored:
        keys |= {'root', 'revision', 'activation_cursor'}
    if not isinstance(value, dict) or set(value) != keys or value.get('profile') != CONFIG:
        raise delivery.DeliveryError('Invalid private history configuration.')
    manifest = require_owner(root, value['participant'])
    _standalone(root)
    if value['project_id'] != delivery.fingerprint([manifest['identity'], value['participant']]):
        raise delivery.DeliveryError('Private history identity differs from its manifest.')
    if (value['mode'] != 'commit-only' or any(value[k] is not None for k in ('remote_url', 'remote_ref', 'records_ref'))
            or value['allowed_paths'] != list(PATHS) or value['checks'] != []
            or not isinstance(value['project_id'], str) or not value['project_id']
            or not isinstance(value['branch'], str) or not value['branch'].startswith('refs/heads/')):
        raise delivery.DeliveryError('Private history requires exact canonical paths and NULL transport fields.')
    delivery.git(root, 'check-ref-format', value['branch'])
    if delivery.text(root, 'symbolic-ref', 'HEAD') != value['branch']:
        raise delivery.DeliveryError('Private history branch changed.')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['canonical_base'] or ''):
        raise delivery.DeliveryError('Private history baseline must be a commit.')
    delivery.git(root, 'cat-file', '-e', value['canonical_base'] + '^{commit}')
    if stored and (Path(value['root']).resolve() != Path(root).resolve()
                   or type(value['revision']) is not int or value['revision'] < 1):
        raise delivery.DeliveryError('Private history binding differs from its root.')


def _configure(engine, value, *, expected_revision=0):
    _validate_configuration(engine.root, value)
    with engine.db() as db:
        row = db.execute('SELECT payload FROM config WHERE root=?', (str(engine.root),)).fetchone()
        old = json.loads(row[0]) if row else {'revision': 0}
        if old['revision'] != expected_revision:
            raise delivery.DeliveryError('Configuration revision changed.')
        if row and old.get('profile') != CONFIG:
            raise delivery.DeliveryError('A shared delivery binding cannot become private history.')
        if row and any(old[k] != value[k] for k in ('project_id','participant','branch','canonical_base')):
            raise delivery.DeliveryError('Existing private history binding cannot be redirected.')
        if (db.execute("SELECT 1 FROM jobs WHERE state != 'committed' LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM intents WHERE state != 'native-drained' LIMIT 1").fetchone()):
            raise delivery.DeliveryError('Recover outstanding personal history before changing configuration.')
        new = {**value, 'root': str(engine.root), 'revision': expected_revision + 1,
               'activation_cursor': time.time_ns()}
        db.execute('INSERT OR REPLACE INTO config VALUES(?,?)', (str(engine.root), delivery.encoded(new)))
    return new


def read_config(root, owner_id):
    """A no-write owner-selected configuration view for CLI forms."""
    require_owner(root, owner_id)
    root = Path(root).resolve()
    value = native.configuration(root, owner_id=owner_id)
    if value['revision'] == 0:
        try:
            branch = delivery.text(root, 'symbolic-ref', 'HEAD')
        except delivery.DeliveryError:
            branch = None
        return {'profile': CONFIG, 'mode': 'manual', 'revision': 0, 'branch': branch}
    _validate_configuration(root, value, stored=True)
    return value


def configure_local(root, owner_id, branch='refs/heads/personal', expected_revision=0):
    manifest = require_owner(root, owner_id)
    root = Path(root).resolve()
    engine = delivery.Engine(root)
    previous = engine.config()
    baseline = previous['canonical_base'] if previous.get('profile') == CONFIG and previous['revision'] else _head(root)
    value = {'profile': CONFIG, 'project_id': delivery.fingerprint([manifest['identity'], owner_id]),
             'participant': owner_id, 'mode': 'commit-only', 'remote_url': None, 'remote_ref': None,
             'records_ref': None, 'branch': branch, 'canonical_base': baseline,
             'allowed_paths': list(PATHS), 'checks': []}
    return engine.configure_private(value, expected_revision=expected_revision)


def _assert_host(root, host, actor, owner_id):
    require_owner(root, owner_id)
    _standalone(root)
    if Path(host).resolve() != private_host(root).resolve():
        raise ts.ReadError('denied', 'Select the dedicated personal host.')
    coordinator, authority = native.managed_write.connect(root, host, actor)
    return coordinator, authority


def assert_ready(root, operation_id=None):
    _standalone(root)
    engine = delivery.Engine(root)
    with engine.db() as db:
        pending = list(db.execute("SELECT payload FROM jobs WHERE state != 'committed'"))
        pending += list(db.execute("SELECT payload FROM intents WHERE state != 'native-drained'"))
    if any(json.loads(row[0])['logical_id'] != operation_id for row in pending):
        raise ts.ReadError('conflict', 'Recover outstanding personal history before another write.')


def _entries(root, paths, *, tree=None):
    if tree:
        result = {}
        for raw in delivery.git(root, 'ls-tree', '-z', tree, '--', *paths).stdout.split(b'\0'):
            if raw:
                header, path = raw.split(b'\t', 1)
                mode, kind, oid = header.decode().split()
                if mode != '100644' or kind != 'blob':
                    raise delivery.DeliveryError('Private history supports ordinary canonical files only.')
                result[path.decode()] = ('100644 ' + oid + ' 0\t' + path.decode()).encode()
        return result
    return {raw.split(b'\t', 1)[1].decode(): raw for raw in
            delivery.git(root, 'ls-files', '--stage', '-z', '--', *paths).stdout.split(b'\0') if raw}


def _index_others(root, selected):
    return [raw for raw in delivery.git(root, 'ls-files', '--stage', '-z').stdout.split(b'\0')
            if raw and raw.split(b'\t', 1)[1].decode() not in selected]


def _operation_boundary(engine, payload):
    root = engine.root
    require_owner(root, payload['config']['participant'])
    _standalone(root)
    engine._topology(root)
    if delivery.text(root, 'symbolic-ref', 'HEAD') != payload['config']['branch']:
        raise delivery.DeliveryError('Private history branch changed.')
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG', 'index.lock'):
        path = Path(delivery.text(root, 'rev-parse', '--git-path', name))
        if (path if path.is_absolute() else root / path).exists():
            raise delivery.DeliveryError('Active Git operation; preserve it for recovery.')
    if not payload.get('bootstrap'):
        _validate_configuration(root, engine.config(), stored=True)
        engine._binding(payload, engine._key(payload))


def _stage_candidate(engine, parent, outputs):
    root = engine.root
    with tempfile.TemporaryDirectory(prefix='personal-index-', dir=engine.home) as folder:
        index = str(Path(folder) / 'index')
        env = {'GIT_INDEX_FILE': index}
        delivery.git(root, 'read-tree', parent if parent else '--empty', env=env)
        for path, body in outputs.items():
            if body is None:
                delivery.git(root, 'update-index', '--force-remove', '--', path, env=env)
            else:
                oid = delivery.git(root, 'hash-object', '-w', '--stdin', input=body).stdout.decode().strip()
                delivery.git(root, 'update-index', '--add', '--cacheinfo', '100644', oid, path, env=env)
        return delivery.git(root, 'write-tree', env=env).stdout.decode().strip()


def _reconcile_index(engine, plan, oid):
    paths = list(plan['outputs'])
    previous = {p: delivery.thaw(v) for p, v in plan['index_before'].items()}
    desired = _entries(engine.root, paths, tree=oid)
    current = _entries(engine.root, paths)
    if current == desired:
        return
    if current != previous:
        raise delivery.DeliveryError('Canonical index changed; history retained for review.')
    rows = []
    for path in paths:
        if path in desired:
            rows.append(desired[path] + b'\0')
        else:
            delivery.git(engine.root, 'update-index', '--force-remove', '--', path)
    if rows:
        delivery.git(engine.root, 'update-index', '-z', '--index-info', input=b''.join(rows))


def _run_engine(engine, key, *, fault=None):
    row = engine.status(key)[0]
    payload = row['payload']
    if row['state'] == 'committed':
        return row
    try:
        _operation_boundary(engine, payload)
        plan = json.loads(row['plan']) if row['plan'] else None
        oid = row['commit_oid']
        if plan and row['state'] in {'committing', 'commit-uncertain'}:
            head = _head(engine.root)
            if head != plan['parent']:
                parents = delivery.text(engine.root, 'rev-list', '--parents', '-n', '1', head).split()[1:]
                if (parents != ([plan['parent']] if plan['parent'] else [])
                        or delivery.text(engine.root, 'rev-parse', head + '^{tree}') != plan['tree']
                        or 'Eutonos-Delivery: ' + key not in delivery.text(engine.root, 'show', '-s', '--format=%B', head)):
                    raise delivery.DeliveryError('Interrupted personal commit is incompatible; do not duplicate it.')
                oid = head
        if oid is None:
            parent = _head(engine.root)
            if plan is None:
                if parent != payload['source_parent']:
                    raise delivery.DeliveryError('Personal branch advanced before history.')
                if parent:
                    tracked = set(delivery.git(engine.root, 'ls-tree', '-r', '--name-only', parent).stdout.decode().splitlines())
                    if not tracked <= set(PATHS):
                        raise delivery.DeliveryError('Private history contains noncanonical tracked files.')
                outputs = {p: delivery.thaw(c['after']) for p, c in payload['changes'].items()}
                current = _entries(engine.root, list(outputs))
                expected = _entries(engine.root, list(outputs), tree=parent) if parent else {}
                if current != expected:
                    raise delivery.DeliveryError('Selected canonical index has unrelated edits; preserve them.')
                author = delivery.text(engine.root, 'var', 'GIT_AUTHOR_IDENT')
                committer = delivery.text(engine.root, 'var', 'GIT_COMMITTER_IDENT')
                matches = [re.fullmatch(r'(.*) <([^<>]+)> \d+ [+-]\d{4}', v) for v in (author, committer)]
                if not all(matches):
                    raise delivery.DeliveryError('Git author/committer identity is unavailable.')
                tree = _stage_candidate(engine, parent, outputs)
                if parent and tree == delivery.text(engine.root, 'rev-parse', parent + '^{tree}'):
                    raise delivery.DeliveryError('Personal history has no attributable commit change.')
                plan = {'parent': parent, 'tree': tree, 'outputs': {p: delivery.freeze(b) for p,b in outputs.items()},
                        'index_before': {p: delivery.freeze(b) for p,b in current.items()},
                        'message': 'EUTONOS personal history\n\nEutonos-Delivery: ' + key,
                        'date': str(int(time.time())) + ' +0000',
                        'author': list(matches[0].groups()), 'committer': list(matches[1].groups())}
                engine._save(key, state='checking', plan=delivery.encoded(plan))
                engine._fault(fault, 'after_checks')
            if _head(engine.root) != plan['parent']:
                raise delivery.DeliveryError('Frozen personal commit parent changed.')
            outputs = {p: delivery.thaw(v) for p,v in plan['outputs'].items()}
            before = {p: (engine.root / p).read_bytes() if (engine.root / p).exists() else None for p in PATHS}
            others = _index_others(engine.root, outputs)
            with tempfile.TemporaryDirectory(prefix='personal-commit-', dir=engine.home) as folder:
                env = {'GIT_INDEX_FILE': str(Path(folder) / 'index'),
                       'GIT_AUTHOR_DATE': plan['date'], 'GIT_COMMITTER_DATE': plan['date'],
                       'GIT_AUTHOR_NAME': plan['author'][0], 'GIT_AUTHOR_EMAIL': plan['author'][1],
                       'GIT_COMMITTER_NAME': plan['committer'][0], 'GIT_COMMITTER_EMAIL': plan['committer'][1]}
                delivery.git(engine.root, 'read-tree', plan['tree'], env=env)
                engine._save(key, state='committing')
                delivery.git(engine.root, 'commit', '-m', plan['message'], env=env)
                engine._fault(fault, 'after_commit')
            oid = _head(engine.root)
            parents = delivery.text(engine.root, 'rev-list', '--parents', '-n', '1', oid).split()[1:]
            if parents != ([plan['parent']] if plan['parent'] else []) or delivery.text(engine.root, 'rev-parse', oid + '^{tree}') != plan['tree']:
                raise delivery.DeliveryError('Personal commit hook changed the frozen candidate.')
            if any(((engine.root / p).read_bytes() if (engine.root / p).exists() else None) != b for p,b in before.items()) or _index_others(engine.root, outputs) != others:
                raise delivery.DeliveryError('Working files or unrelated index changed during private commit.')
        _reconcile_index(engine, plan, oid)
        engine._save(key, state='committed', commit_oid=oid, error=None,
                     checks=delivery.encoded({'frozen_tree': plan['tree'], 'native_proof': not payload.get('bootstrap', False)}))
    except (delivery.DeliveryError, OSError, subprocess.TimeoutExpired, sqlite3.Error, ts.ReadError) as exc:
        now = engine.status(key)[0]
        uncertain = now['state'] in {'committing', 'commit-uncertain'}
        engine._save(key, state='commit-uncertain' if uncertain else 'blocked', error=str(exc))
    return engine.status(key)[0]


def _history(row):
    return {'state': 'committed' if row['state'] == 'committed' else 'uncertain' if row['state'] == 'commit-uncertain' else 'pending',
            'commit_oid': row.get('commit_oid'), 'error': row.get('error'), 'request_id': row['id']}


def initialize(root, initial_files, owner_id, session):
    ts.require(validate_private_destination(root)['status'] == 'allowed', 'denied', 'Select a private bootstrap destination.')
    root = Path(root).resolve()
    ts.require(set(initial_files) == set(PATHS) and all(isinstance(b, bytes) for b in initial_files.values()), 'invalid', 'Bootstrap requires canonical personal files.')
    root.mkdir(parents=True, exist_ok=True)
    if (root / '.git').exists():
        _standalone(root)
    ts.require(all(p.name in set(PATHS) | {'.git'} for p in root.iterdir()), 'conflict', 'Bootstrap destination is not empty.')
    for path, body in initial_files.items():
        selected = root / path
        ts.require(not selected.is_symlink() and (not selected.exists() or selected.read_bytes() == body), 'conflict', 'Bootstrap contents differ.')
    for path, body in initial_files.items():
        if not (root / path).exists():
            (root / path).write_bytes(body)
    require_owner(root, owner_id)
    if not (root / '.git').exists():
        delivery.git(root, 'init', '-b', 'personal')
    engine = delivery.Engine(root)
    config = {'profile': CONFIG, 'project_id': delivery.fingerprint([require_owner(root,owner_id)['identity'], owner_id]),
              'participant': owner_id, 'mode': 'commit-only', 'remote_url': None, 'remote_ref': None,
              'records_ref': None, 'branch': 'refs/heads/personal', 'canonical_base': None,
              'allowed_paths': list(PATHS), 'checks': [], 'root': str(root), 'revision': 0}
    payload = {'profile': delivery.REQUEST, 'logical_id': 'bootstrap', 'kind': 'personal', 'bootstrap': True,
               'config': config, 'actor': owner_id, 'session': session, 'work_id': 'bootstrap',
               'source_parent': None, 'changes': {p: {'before':None,'after':delivery.freeze(b)} for p,b in initial_files.items()}}
    key = engine._key(payload)
    engine._insert_job(key, payload)
    row = engine.run(key)
    if row['state'] == 'committed' and engine.config()['revision'] == 0:
        configure_local(root, owner_id)
    return {'outcome': 'completed', 'native': {'complete': True, 'readback':'verified', 'paths':list(PATHS)},
            'history': _history(row), 'backup': {'state':'not-requested'}, 'fresh_admission_required':True}


def apply(root, prepared, owner_id, host, actor, session, verify):
    _assert_host(root, host, actor, owner_id)
    root = Path(root).resolve()
    ts.require(callable(verify), 'invalid', 'A typed personal verifier is required.')
    operation = prepared['operation_id']
    assert_ready(root, operation)
    ts.require(prepared['owner_id'] == owner_id and set(prepared['paths']) <= set(PATHS), 'denied', 'Personal plan scope differs.')
    engine = delivery.Engine(root)
    _validate_configuration(root, engine.config(), stored=True)
    result = native.managed_apply(engine, operation, host=host, actor=actor,
                                  attempt='personal-' + operation, effect_id='personal-effect-' + operation,
                                  effect=prepared['effect'], session=session, work_id=operation,
                                  checkpoint={'operation_id':operation,'scope':'personal'}, verify=verify)
    if result['delivery'] is None:
        history = {'state':'pending', 'commit_oid':None, 'error':result.get('delivery_error'), 'request_id':result['request_id']}
    else:
        history = _history(engine.run(result['request_id']))
    return {'outcome': result['native']['outcome'], 'native':result['native'], 'history':history, 'backup':{'state':'not-requested'}}


def recover(root, operation_id, owner_id, host, actor, verify):
    _assert_host(root, host, actor, owner_id)
    root = Path(root).resolve()
    engine = delivery.Engine(root)
    with engine.db() as db:
        rows = list(db.execute('SELECT id,payload,native FROM intents'))
    selected = [r for r in rows if json.loads(r['payload'])['logical_id'] == operation_id]
    ts.require(len(selected) == 1, 'missing', 'Original personal history intent is unavailable.')
    row = selected[0]
    ts.require(json.loads(row['native'])['actor'] == actor, 'wrong-scope', 'Original personal actor differs.')
    receipt = native.confirm(engine, row['id'], host=host, verify=verify,
                             checkpoint={'operation_id':operation_id,'scope':'personal'})
    engine.drain_verified()
    result = engine.run(row['id'])
    return {'outcome': receipt['outcome'], 'native':receipt, 'history':_history(result), 'backup':{'state':'not-requested'}}
