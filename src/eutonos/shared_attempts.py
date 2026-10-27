"""Portable advisory attempts in native boards, with frozen managed-write retry."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import uuid

from .private_scope import require_shared
from . import tokenslang as ts
from .effects import digest
from .local_lock import local_lock

PROFILE = 'eutonos.shared-attempt.v1'
STATES = {'active': 'active', 'released': 'cancelled', 'finished': 'completed'}


def validate(item):
    value = item.get('extensions', {}).get('shared_attempt')
    ts.require(isinstance(value, dict) and item['type'] == 'attempt' and 'parent_id' not in item,
               'invalid', 'Attempts require typed shared_attempt metadata and no parent_id.')
    ts.shape(value, {'profile', 'attempt_id', 'project', 'participant', 'session', 'branch', 'target',
                     'canonical_commit', 'state', 'intent', 'checkpoint', 'intentional_parallel'}, {'attribution'})
    if 'attribution' in value:
        from .work_attribution import validate_snapshot
        validate_snapshot(value['attribution'])
    ts.require(value['profile'] == PROFILE, 'unsupported', 'Unknown shared attempt profile.')
    token = str(uuid.UUID(value['attempt_id']))
    ts.require(token == value['attempt_id'] and item['id'] == 'attempt-' + token,
               'invalid', 'Attempt identity must retain its full frozen UUID.')
    ts.shape(value['project'], {'repository_id', 'scope_id', 'namespace'})
    for name in ('participant', 'session', 'branch', 'intent', 'checkpoint'):
        ts.require(isinstance(value[name], str) and value[name].strip() and len(value[name]) <= 2000,
                   'invalid', 'Attempt ' + name + ' must be a bounded nonblank value.')
    ts.require(all(isinstance(v, str) and v for v in value['project'].values()), 'invalid', 'Missing native attempt identity.')
    target = value['target']
    ts.shape(target, {'work_id', 'source_ref', 'source_commit', 'revision', 'ancestors'})
    ts.require(isinstance(target['work_id'], str) and target['work_id'] and isinstance(target['source_ref'], str)
               and target['source_ref'], 'invalid', 'Attempt target must name exact observed work/source.')
    ts.require(isinstance(target['ancestors'], list) and len(target['ancestors']) <= 32
               and all(isinstance(v, str) and v for v in target['ancestors'])
               and len(set(target['ancestors'])) == len(target['ancestors']), 'invalid', 'Attempt ancestors must be bounded exact IDs.')
    for name, oid in [('target revision', target['revision']), ('source commit', target['source_commit']), ('canonical commit', value['canonical_commit'])]:
        ts.require(isinstance(oid, str) and re.fullmatch('[0-9a-f]{64}' if name == 'target revision' else '[0-9a-f]{40,64}', oid),
                   'invalid', 'Invalid ' + name + ' pin.')
    ts.require(value['state'] in STATES and item['status'] == STATES[value['state']]
               and type(value['intentional_parallel']) is bool, 'invalid', 'Attempt state/status or parallel intent differs.')
    return value


def immutable(before, after):
    old, new = validate(before), validate(after)
    ts.require(all(old[key] == new[key] for key in old if key not in {'state', 'checkpoint'}),
               'denied', 'An attempt cannot change identity, participant, session or selected work after creation.')
    ts.require(old['state'] == 'active' or old == new, 'denied', 'A released or finished attempt cannot be reopened; start a new UUID attempt.')


def overlaps(left, right):
    a, b = left['target'], right['target']
    return left['project'] == right['project'] and (a['work_id'] == b['work_id'] or a['work_id'] in b['ancestors'] or b['work_id'] in a['ancestors'])


def project(rows, conflicts, coverage, identity=None):
    items, diagnostics = [], []
    selected = [(row, False) for row in rows if row['type'] == 'attempt']
    for conflict in conflicts:
        selected.extend((row, True) for row in conflict['candidates'] if row['type'] == 'attempt')
    for row, conflict in selected:
        try:
            value = deepcopy(validate(row))
        except (ValueError, TypeError, KeyError, ts.ReadError):
            retained = row.get('extensions', {}).get('shared_attempt', {})
            if identity is not None and retained.get('project') != identity:
                diagnostics.append('Attempt ' + row['id'] + ' has foreign/unavailable project identity and is excluded.')
                continue
            diagnostics.append('Attempt ' + row['id'] + ' has unavailable or removed observed metadata.')
            items.append({'id': row['id'], 'attempt_id': retained.get('attempt_id'), 'participant': retained.get('participant'),
                          'session': retained.get('session'), 'state': 'unknown', 'target': deepcopy(retained.get('target', {})),
                          'intent': retained.get('intent'), 'checkpoint': 'Observed record is removed or unavailable; no completion inferred.',
                          'intentional_parallel': False, 'record_conflict': conflict, 'provenance': deepcopy(row.get('provenance', [])),
                          'source_integration': row.get('source_integration'), 'publication': row.get('publication')})
            continue
        if identity is not None and value['project'] != identity:
            diagnostics.append('Attempt ' + row['id'] + ' has foreign project identity and is excluded.')
            continue
        if conflict:
            value['observed_state'] = value['state']
            value['state'] = 'conflict'
            diagnostics.append('Attempt ' + row['id'] + ' has incomparable observed variants.')
        items.append({'id': row['id'], **value, 'record_conflict': conflict,
                      **{key: deepcopy(row.get(key)) for key in ('provenance', 'source_integration', 'publication')}})
    items.sort(key=lambda row: (str(row.get('participant')), str(row.get('session')), str(row.get('attempt_id'))))
    truncated = len(items) > 200
    items = items[:200]
    if truncated or diagnostics: coverage = 'unknown'
    active = [row for row in items if row['state'] == 'active']
    relations = [{'left': a['id'], 'right': b['id'], 'intentional_parallel': a['intentional_parallel'] or b['intentional_parallel']}
                 for index, a in enumerate(active) for b in active[index + 1:] if a['id'] != b['id'] and overlaps(a, b)]
    return {'items': items[:200], 'truncated': truncated, 'coverage': coverage,
            'overlaps': relations[:200], 'overlaps_truncated': len(relations) > 200, 'diagnostics': diagnostics[:20]}


def _submit(root, host, actor, batch, attempt_id):
    from . import write_retry
    ts.require(bool(host and actor), 'invalid', 'Shared attempts require --host and --actor for the native managed writer.')
    result = write_retry.submit_batch(host, root, actor, batch)
    # A complete queue receipt may outlive its original transport snapshot.
    # Recover that exact bound effect to inspect/resume publication without rewriting.
    transport = (result.get('result') or {}).get('git_delivery', {})
    if result.get('outcome') == 'complete' and transport.get('request_id') and transport.get('state') != 'published':
        from . import write_batch
        from .git_delivery import DeliveryError
        try:
            recovered = write_batch.recover_submitted(root, host, actor, batch, result['request_id'])
            if recovered is not None: result = {**result, 'result': recovered}
        except (OSError, ValueError, TypeError, KeyError, ts.ReadError, DeliveryError) as exc:
            # A later transport failure cannot turn verified native success into
            # a failed save or cause replay against a subsequently changed owner.
            result = {**result, 'result': {**result['result'], 'git_delivery': {
                'state': 'unavailable', 'request_id': transport['request_id'], 'error': str(exc)}}}
    return {'attempt_id': attempt_id, 'native_save': result.get('outcome', 'unknown'),
            'git_delivery': (result.get('result') or {}).get('git_delivery', {'state': 'unknown'}),
            'write_result': result, 'qualification': 'Native attempt save, Git publication and canonical work completion are separate facts.'}


def start(root, work_id, session, host, actor, *, attempt_id=None, intent=None, intentional_parallel=False, caller_context=None):
    require_shared(root)
    from . import boards, current_work, shared_work, write_batch
    root = Path(root).resolve()
    cfg = shared_work.load(root)
    ts.require(isinstance(session, str) and bool(session.strip()) and len(session) <= 256, 'invalid', 'Supply the bounded actual session identity.')
    ts.require(cfg['enabled'] and bool(cfg['participant']) and bool(session), 'invalid', 'Shared start requires local opt-in, participant and actual --chat session.')
    if attempt_id is not None:
        ts.require(str(uuid.UUID(attempt_id)) == attempt_id, 'invalid', 'Use a full attempt UUID.')
    key = digest(ts.encoded([cfg['repository_id'], cfg['scope_id'], cfg['namespace'], cfg['participant'], session, work_id, attempt_id]))
    path = root / '.eutonos-local/shared-attempt-starts' / (key + '.json')
    with local_lock(root, timeout=5):
        if path.exists():
            frozen = json.loads(path.read_bytes())
            ts.require(frozen['intent'] == intent and frozen['intentional_parallel'] == intentional_parallel,
                       'conflict', 'Logical attempt start is already bound; reuse its intent or choose an explicit new attempt UUID.')
        else:
            observed = current_work.project(root, refresh=True, include_knowledge=False)
            matches = [row for row in observed['boards']['items'] if row['id'] == work_id]
            ts.require(len(matches) == 1 and not matches[0].get('record_conflict'), 'ambiguous', 'Select one observed nonconflicting work record.')
            target = matches[0]
            ts.require(target['type'] in {'campaign', 'stage', 'slice', 'quick', 'task'} and target['status'] not in {'completed', 'cancelled', 'superseded'},
                       'invalid', 'Attempts start on unfinished canonical work.')
            canonical = observed.get('shared_work', {}).get('canonical') or {}
            ts.require(bool(canonical.get('commit')), 'unavailable', 'Canonical ref has never been observed; no fork fallback is permitted.')
            pins = target.get('provenance', [])
            pin = next((p for p in pins if p.get('source_kind') == 'cached-remote-ref'), next(iter(pins), None))
            ts.require(pin is not None and bool(target.get('source_revision')), 'unavailable', 'Target source/revision is unavailable.')
            ancestors, parent = [], target.get('parent_id')
            by_id = {row['id']: row for row in observed['boards']['items']}
            while parent:
                ts.require(parent in by_id and parent not in ancestors and len(ancestors) < 32, 'unavailable', 'Target ancestor scope is unavailable.')
                ancestors.append(parent); parent = by_id[parent].get('parent_id')
            from .shared_work import _git
            branch = _git(root, 'symbolic-ref', '--short', 'HEAD')
            ts.require(branch.returncode == 0, 'invalid', 'Shared attempts require a named work branch.')
            from .work_attribution import freeze
            attribution = freeze(root, {**(caller_context or {}), 'session': session})
            token = attempt_id or str(uuid.uuid4())
            value = {'profile': PROFILE, 'attempt_id': token, 'project': {k: cfg[k] for k in shared_work.IDENTITY},
                     'participant': cfg['participant'], 'session': session, 'branch': branch.stdout.decode().strip(), 'attribution': attribution,
                     'target': {'work_id': work_id, 'source_ref': pin['ref'], 'source_commit': pin['commit'],
                                'revision': target['source_revision'], 'ancestors': ancestors},
                     'canonical_commit': canonical['commit'], 'state': 'active', 'intent': intent or 'Work on ' + work_id,
                     'checkpoint': 'Attempt started; no completion asserted.', 'intentional_parallel': intentional_parallel}
            item = {'id': 'attempt-' + token, 'type': 'attempt', 'title': 'Attempt on ' + work_id,
                    'summary': value['intent'], 'status': 'active', 'extensions': {'shared_attempt': value}}
            validate(item)
            owner = next((row for row in boards.project(root)['owners'] if row['path'] == cfg['attempt_owner'] and row['status'] == 'available'), None)
            ts.require(owner is not None, 'unavailable', 'Configured attempt owner is unavailable; create/register it through the managed owner form.')
            batch = {'profile': write_batch.PROFILE, 'operation_id': 'shared-start-' + token,
                     'requested_by': {'kind': 'actual-session', 'value': session}, '_attribution': attribution, 'operations': [{'kind': 'add', 'request': {
                         'owner_path': owner['path'], 'expected_sha256': owner['sha256'], 'index': 0, 'items': [item]}}]}
            overlapping = [row for row in observed.get('attempts', {}).get('items', []) if row['state'] == 'active'
                           and row['session'] != session and overlaps(value, row)]
            advisory = {'coverage': observed['attempts']['coverage'], 'overlaps': overlapping,
                        'recommendation': 'Acknowledge intentional parallel work when overlapping; incomplete coverage is unknown.'}
            frozen = {'attempt_id': token, 'intent': intent, 'intentional_parallel': intentional_parallel, 'batch': batch, 'advisory': advisory}
            current_work._atomic(path, ts.encoded(frozen))
    return {**_submit(root, host, actor, frozen['batch'], frozen['attempt_id']), 'advisory': frozen['advisory']}


def finish(root, work_id, session, host, actor, *, attempt_id=None, state='released', checkpoint=None):
    require_shared(root)
    from . import boards, current_work, shared_work, write_batch
    cfg = shared_work.load(root)
    ts.require(cfg['enabled'] and bool(cfg['participant']) and bool(session), 'invalid', 'Attempt changes require current local opt-in and actual session.')
    matches = [row for row in boards.project(root)['items'] if row['type'] == 'attempt'
               and row['extensions']['shared_attempt']['target']['work_id'] == work_id
               and row['extensions']['shared_attempt']['project'] == {k: cfg[k] for k in shared_work.IDENTITY}
               and row['extensions']['shared_attempt']['participant'] == cfg['participant']
               and row['extensions']['shared_attempt']['session'] == session
               and (attempt_id is None or row['extensions']['shared_attempt']['attempt_id'] == attempt_id)]
    active = [row for row in matches if row['extensions']['shared_attempt']['state'] == 'active']
    if not active and len(matches) == 1 and matches[0]['extensions']['shared_attempt']['state'] == state:
        token = matches[0]['extensions']['shared_attempt']['attempt_id']
        original = Path(root) / '.eutonos-local/shared-attempt-starts' / (state + '-' + token + '.json')
        ts.require(original.exists(), 'unavailable', 'Original terminal request is unavailable; inspect native writer/delivery history without replay.')
        return _submit(root, host, actor, json.loads(original.read_bytes()), token)
    ts.require(len(active) == 1, 'ambiguous', 'Select exactly one owned active attempt with --attempt-id when needed.')
    item = active[0]; value = deepcopy(item['extensions']['shared_attempt'])
    value.update(state=state, checkpoint=checkpoint or 'Attempt ' + state + '; canonical work status is unchanged.')
    owner = next(row for row in boards.project(root)['owners'] if row['path'] == item['owner_path'])
    batch = {'profile': write_batch.PROFILE, 'operation_id': 'shared-' + state + '-' + value['attempt_id'],
             'requested_by': {'kind': 'actual-session', 'value': session}, 'operations': [{'kind': 'update', 'request': {
                 'owner_path': owner['path'], 'expected_sha256': owner['sha256'], 'item_id': item['id'],
                 'changes': {'status': STATES[state], 'extensions': {**item['extensions'], 'shared_attempt': value}}}}]}
    # Freeze the terminal request before submission as well. A retry cannot
    # substitute newer owner hashes or a different checkpoint under the same ID.
    path = Path(root) / '.eutonos-local/shared-attempt-starts' / (state + '-' + value['attempt_id'] + '.json')
    with local_lock(root, timeout=5):
        if path.exists(): batch = json.loads(path.read_bytes())
        else: current_work._atomic(path, ts.encoded(batch))
    return _submit(root, host, actor, batch, value['attempt_id'])
