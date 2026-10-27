"""Owner-selected private native records, versioned effects and exact recovery."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import uuid

from . import boards, goal_records, tokenslang as ts
from .effects import LocalRoot, decode, digest, plan as effect_plan
from .work_lifecycle import item_revision

WRITE = 'eutonos.personal-write.v1'
PLAN = 'eutonos.personal-plan.v1'
PRIORITIES = 'eutonos.personal-priorities.v1'


def _manifest(root, owner_id):
    from .private_scope import require_owner
    return require_owner(root, owner_id)


def initial_files(owner_id, *, identity=None):
    from .private_scope import MANIFEST, OWNERS, PROFILE
    goal_records._text(owner_id)
    identity = deepcopy(identity) if identity is not None else {
        'repository_id': str(uuid.uuid4()), 'scope_id': str(uuid.uuid4()), 'namespace': 'personal'}
    ts.shape(identity, {'repository_id', 'scope_id', 'namespace'})
    for value in identity.values():
        goal_records._text(value)
    manifest = {'profile': PROFILE, 'identity': identity, 'owner_id': owner_id, 'owners': OWNERS}
    board = b'```tokenslang-board\n' + boards._json({'profile': boards.BOARD, 'items': []}) + b'\n```\n'
    return {MANIFEST: ts.encoded(manifest) + b'\n', 'goals.md': b'# Personal goals\n\n' + board,
            'projects.md': b'# Personal projects\n\n' + board,
            'priorities.json': ts.encoded({'profile': PRIORITIES, 'items': []}) + b'\n'}


def _priorities(value):
    from .scoped_items import validate
    ts.shape(value, {'profile', 'items'})
    ts.require(value['profile'] == PRIORITIES, 'unsupported', 'Unknown personal priorities profile.')
    refs = [validate(v) for v in ts.sequence(value['items'])]
    ts.require(len({ts.encoded(v) for v in refs}) == len(refs), 'ambiguous', 'Personal priorities require unique scoped references.')
    return {'profile': PRIORITIES, 'items': refs}


def read(root, *, owner_id, kind=None, item_id=None):
    manifest = _manifest(root, owner_id)
    ts.require(kind in {None, 'goal', 'project', 'priorities'}, 'invalid', 'Unknown personal record kind.')
    local = LocalRoot(root)
    hashes, items = {}, []
    seen = set()
    for selected in ('goal', 'project'):
        path = manifest['owners'][selected + 's']
        raw = local.read(path)
        ts.require(raw is not None, 'missing', 'A personal native owner is absent.')
        board, _ = boards._parse(raw, path)
        ts.require(board is not None, 'invalid', 'A personal native owner has no board.')
        hashes[path] = digest(raw)
        for item in board['items']:
            ts.require(item['type'] == selected and item['id'] not in seen, 'ambiguous', 'Personal owners require unique matching record types.')
            seen.add(item['id'])
            if kind in {None, selected} and (item_id is None or item['id'] == item_id):
                items.append({**deepcopy(item), 'owner_path': path, 'owner_sha256': hashes[path], 'revision': item_revision(item)})
    raw = local.read(manifest['owners']['priorities'])
    ts.require(raw is not None, 'missing', 'Personal priorities owner is absent.')
    priorities = _priorities(ts.loads(raw.decode('utf-8')))
    hashes[manifest['owners']['priorities']] = digest(raw)
    result = {'profile': 'eutonos.personal-read.v1', 'identity': manifest['identity'], 'owner_id': owner_id,
              'items': items, 'priorities': priorities, 'owner_revisions': hashes,
              'manifest_sha256': digest(local.read('eutonos.personal.json'))}
    if item_id is not None:
        ts.require(len(items) == 1, 'missing', 'Selected personal record is absent.')
        result.update(item=items[0], item_revision=items[0]['revision'], owner_path=items[0]['owner_path'], owner_sha256=items[0]['owner_sha256'])
    return result


def form(root, kind, draft=None, *, owner_id, item_id=None):
    current = read(root, owner_id=owner_id, kind=kind, item_id=item_id)
    draft = deepcopy(draft or {})
    operation_id = str(uuid.uuid4())
    if kind == 'priorities':
        path = 'priorities.json'
        value = draft.get('items', current['priorities']['items'])
        _priorities({'profile': PRIORITIES, 'items': value})
        request = {'profile': WRITE, 'operation_id': operation_id, 'owner_id': owner_id,
                   'operation': 'priorities', 'kind': kind, 'items': value,
                   'expected_revision': current['owner_revisions'][path],
                   'expected_owner_sha256': current['owner_revisions'][path]}
        return {'profile': 'eutonos.personal-form.v1', 'missing_fields': [], 'request': request}
    original = current.get('item')
    source = {k: v for k, v in (original or {}).items() if k not in {'owner_path', 'owner_sha256', 'revision'}}
    guided = goal_records.form(kind, {**source, **draft}, private=True)
    path = kind + 's.md'
    request = {'profile': WRITE, 'operation_id': operation_id, 'owner_id': owner_id,
               'operation': 'update' if original else 'add', 'kind': kind,
               'expected_revision': current.get('item_revision'),
               'expected_owner_sha256': current['owner_revisions'][path]}
    if original:
        request.update(item_id=item_id, changes={k: v for k, v in guided['item'].items() if k != 'id'})
    else:
        request['item'] = guided['item']
    return {**guided, 'request': request}


def prepare(root, request, *, owner_id):
    manifest = _manifest(root, owner_id)
    ts.shape(request, {'profile', 'operation_id', 'owner_id', 'operation', 'kind', 'expected_revision', 'expected_owner_sha256'},
             {'item', 'item_id', 'changes', 'items'})
    ts.require(request['profile'] == WRITE and request['owner_id'] == owner_id, 'denied', 'Select an owner-bound personal request.')
    goal_records._text(request['operation_id'])
    kind, operation = request['kind'], request['operation']
    ts.require((kind == 'priorities' and operation == 'priorities') or
               (kind in {'goal', 'project'} and operation in {'add', 'update'}), 'invalid', 'Unknown personal write operation.')
    local = LocalRoot(root)
    current = read(root, owner_id=owner_id)
    path = manifest['owners']['priorities' if kind == 'priorities' else kind + 's']
    ts.require(current['owner_revisions'][path] == request['expected_owner_sha256'], 'stale', 'Personal owner changed after preparation.')
    records = []
    if kind == 'priorities':
        ts.require(request['expected_revision'] == current['owner_revisions'][path], 'stale', 'Personal priorities revision changed.')
        desired = ts.encoded(_priorities({'profile': PRIORITIES, 'items': request.get('items')})) + b'\n'
    else:
        raw = local.read(path)
        board, matches = boards._parse(raw, path)
        if operation == 'add':
            ts.require(request['expected_revision'] is None, 'invalid', 'A new record has no previous revision.')
            item = boards._item(request.get('item'))
            ts.require('creation_token' in item.get('extensions', {}) and item['id'] == kind + '-' + item['extensions']['creation_token'],
                       'invalid', 'New personal IDs require the frozen full UUID from the form.')
            ts.require(not any(i['id'] == item['id'] for i in current['items']), 'conflict', 'Personal record already exists.')
            board['items'].append(item)
        else:
            found = [i for i in board['items'] if i['id'] == request.get('item_id')]
            ts.require(len(found) == 1, 'missing', 'Selected personal record is absent.')
            before = found[0]
            ts.require(item_revision(before) == request['expected_revision'], 'stale', 'Personal item revision changed.')
            changes = request.get('changes')
            ts.require(isinstance(changes, dict) and changes and 'id' not in changes, 'invalid', 'An update cannot replace stable identity.')
            if 'extensions' in changes and before.get('extensions', {}).get('creation_token') is not None:
                ts.require(changes['extensions'].get('creation_token') == before.get('extensions', {}).get('creation_token'),
                           'invalid', 'An update must preserve the frozen creation identity.')
            item = boards._item({**before, **deepcopy(changes)})
            board['items'][board['items'].index(before)] = item
        ts.require(item['type'] == kind, 'invalid', 'Personal record type cannot change.')
        (goal_records.validate_goal if kind == 'goal' else goal_records.validate_project)(item, writing=True,
            **({'private': True} if kind == 'project' else {}))
        desired = boards._replace(raw, matches[0], board)
        records.append({'id': item['id'], 'owner_path': path, 'revision': item_revision(item)})
    effect = effect_plan(local, {path: desired}, inputs=['eutonos.personal.json', 'goals.md', 'projects.md', 'priorities.json'])
    ts.require(effect['changes'], 'conflict', 'Personal request has no changes.')
    return {'profile': PLAN, 'operation_id': request['operation_id'], 'request': deepcopy(request), 'owner_id': owner_id,
            'effect': effect, 'fingerprint': digest(ts.encoded(effect)), 'paths': [c['path'] for c in effect['changes']],
            'verification': {'manifest_sha256': current['manifest_sha256'], 'records': records, 'owner_hashes': {path: digest(desired)}}}


def verify(root, prepared, *, owner_id):
    ts.require(prepared.get('profile') == PLAN and prepared['owner_id'] == owner_id,
               'denied', 'Typed private verification requires the original owner plan.')
    current = read(root, owner_id=owner_id)
    proof = prepared['verification']
    ts.require(current['manifest_sha256'] == proof['manifest_sha256'], 'stale', 'Private manifest changed.')
    ts.require(prepared['fingerprint'] == digest(ts.encoded(prepared['effect'])), 'conflict', 'Private effect fingerprint differs.')
    for change in prepared['effect']['changes']:
        ts.require(LocalRoot(root).read(change['path']) == decode(change['after']), 'conflict', 'Private native bytes differ from the original write.')
    for record in proof['records']:
        found = [i for i in current['items'] if i['id'] == record['id'] and i['owner_path'] == record['owner_path']]
        ts.require(len(found) == 1 and found[0]['revision'] == record['revision'], 'conflict', 'Private native item readback differs.')


def _host(root, host, owner_id):
    from .private_scope import private_host
    _manifest(root, owner_id)
    ts.require(Path(host).resolve() == private_host(root).resolve(), 'denied', 'Select this personal root dedicated host.')
    return Path(host)


def _artifact(host, operation_id):
    goal_records._text(operation_id)
    return host / 'personal-operations' / (digest(ts.encoded(operation_id)) + '.json')


def submit(root, request, *, owner_id, host, actor, session):
    from . import personal_history
    from .local_lock import local_lock
    from .settings import _atomic
    host = _host(root, host, owner_id)
    goal_records._text(actor); goal_records._text(session)
    artifact = _artifact(host, request['operation_id'])
    with local_lock(host):
        if artifact.exists():
            original = ts.loads(artifact.read_text(encoding='utf-8'))
            ts.require(original['request'] == request and original['actor'] == actor and original['session'] == session,
                       'conflict', 'Private operation ID is already bound to another request or caller.')
            return recover(root, request['operation_id'], owner_id=owner_id, host=host, actor=actor, session=session)
        personal_history.assert_ready(root, operation_id=request['operation_id'])
        prepared = prepare(root, request, owner_id=owner_id)
        original = {'profile': 'eutonos.personal-operation.v1', 'request': deepcopy(request), 'prepared': prepared,
                    'actor': actor, 'session': session, 'owner_id': owner_id}
        _atomic(artifact, original)
        result = personal_history.apply(root, prepared, owner_id=owner_id, host=host, actor=actor, session=session,
                                        verify=lambda: verify(root, prepared, owner_id=owner_id))
        original['result'] = result
        _atomic(artifact, original)
        return result


def recover(root, operation_id, *, owner_id, host, actor, session=None):
    from . import personal_history
    host = _host(root, host, owner_id)
    path = _artifact(host, operation_id)
    ts.require(path.is_file(), 'missing', 'Original private operation is unavailable.')
    original = ts.loads(path.read_text(encoding='utf-8'))
    ts.require(original['actor'] == actor and original['owner_id'] == owner_id and
               (session is None or original['session'] == session), 'denied', 'Private recovery caller differs from original intent.')
    prepared = original['prepared']
    try:
        return personal_history.recover(root, operation_id, owner_id=owner_id, host=host, actor=actor,
                                        verify=lambda: verify(root, prepared, owner_id=owner_id))
    except ts.ReadError as exc:
        if exc.code != 'missing':
            raise
        # The operation artifact may precede the history/native intent. Prove
        # absence before applying the original frozen effect; never replan it.
        from .host import connect
        from .effects import validate_plan
        coordinator, authority = connect(root, host, actor)
        with coordinator._db() as db:
            attempt = 'personal-' + operation_id
            ts.require(db.execute('SELECT 1 FROM attempts WHERE id=?', (attempt,)).fetchone() is None
                       and db.execute('SELECT 1 FROM effects WHERE attempt=?', (attempt,)).fetchone() is None,
                       'conflict', 'Original personal native attempt requires explicit recovery.')
        validate_plan(authority.root, prepared['effect'])
        personal_history.assert_ready(root, operation_id=operation_id)
        return personal_history.apply(root, prepared, owner_id=owner_id, host=host, actor=actor,
                                      session=original['session'], verify=lambda: verify(root, prepared, owner_id=owner_id))
