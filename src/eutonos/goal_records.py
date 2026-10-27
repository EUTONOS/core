"""Semantic goal forms over the existing native board and managed batch writer."""
from __future__ import annotations

from copy import deepcopy
import uuid

from . import tokenslang as ts
from .effects import LocalRoot, digest

GOAL = 'eutonos.goal.v1'
PROJECT = 'eutonos.personal-project.v1'


def _text(value):
    ts.string(value)
    ts.require(bool(value.strip()), 'invalid', 'Semantic text must be nonblank.')


def _texts(value):
    for text in ts.sequence(value):
        _text(text)


def _links(value):
    from .scoped_items import validate
    return [validate(ref) for ref in ts.sequence(value)]


def _assessment(value):
    ts.shape(value, {'assessment', 'basis'})
    ts.require(value['assessment'] in {'unassessed', 'supported', 'contradicted'},
               'invalid', 'Unknown outcome assessment.')
    _text(value['basis'])


def validate_goal(item, *, writing=False):
    semantic = item.get('extensions', {}).get('goal')
    if semantic is None:
        ts.require(not writing, 'invalid', 'New goal writes require extensions.goal; legacy goals need explicit conversion.')
        return item
    ts.shape(semantic, {'profile', 'outcome', 'criteria', 'scope', 'exclusions', 'work_links'},
             {'outcome_assessment'})
    ts.require(item.get('type') == 'goal' and semantic['profile'] == GOAL, 'unsupported', 'Unknown goal profile.')
    ts.require('parent_id' not in item, 'invalid', 'Goal links do not use campaign parent_id.')
    _text(semantic['outcome']); _texts(semantic['scope']); _texts(semantic['exclusions'])
    criteria = ts.sequence(semantic['criteria'])
    ts.require(bool(criteria), 'invalid', 'A semantic goal requires success criteria.')
    keys = set()
    for criterion in criteria:
        ts.shape(criterion, {'key', 'statement', 'evidence_refs', 'assessment'}, {'basis'})
        _text(criterion['key']); _text(criterion['statement'])
        ts.require(criterion['key'] not in keys, 'ambiguous', 'Criterion keys must be unique within the goal.')
        keys.add(criterion['key'])
        from .boards import _refs
        _refs(criterion['evidence_refs'])
        ts.require(criterion['assessment'] in {'unassessed', 'supported', 'contradicted'}, 'invalid', 'Unknown criterion assessment.')
        if criterion['assessment'] != 'unassessed':
            _text(criterion.get('basis'))
    _links(semantic['work_links'])
    if 'outcome_assessment' in semantic:
        _assessment(semantic['outcome_assessment'])
    if item.get('status') == 'completed':
        assessment = semantic.get('outcome_assessment', {})
        ts.require(assessment.get('assessment') == 'supported'
                   and all(c['assessment'] == 'supported' and c['evidence_refs'] for c in criteria),
                   'invalid', 'Goal completion requires explicit supported outcome and criteria assessments.')
        _text(assessment.get('basis'))
    return item


def validate_project(item, *, private=False, writing=False):
    semantic = item.get('extensions', {}).get('personal_project')
    if semantic is None:
        ts.require(not writing, 'invalid', 'New personal projects require extensions.personal_project.')
        return item
    ts.require(not writing or private, 'denied', 'Projects can be written only in personal scope.')
    ts.shape(semantic, {'profile', 'purpose', 'scope', 'exclusions', 'goal_links', 'work_links'}, {'completion_basis'})
    ts.require(item.get('type') == 'project' and semantic['profile'] == PROJECT, 'unsupported', 'Unknown personal project profile.')
    ts.require('parent_id' not in item, 'invalid', 'Personal projects have no campaign parent.')
    _text(semantic['purpose']); _texts(semantic['scope']); _texts(semantic['exclusions'])
    _links(semantic['goal_links']); _links(semantic['work_links'])
    if item.get('status') == 'completed':
        _text(semantic.get('completion_basis'))
    return item


def form(kind, draft=None, *, private=False):
    ts.require(kind in {'goal', 'project'} and (kind != 'project' or private), 'denied', 'Select a goal or personal project form.')
    draft = deepcopy(draft or {})
    token = draft.get('extensions', {}).get('creation_token', str(uuid.uuid4()))
    item = {'id': kind + '-' + token, 'type': kind, 'title': '', 'summary': '', 'status': 'planned',
            'evidence_refs': [], **draft}
    extension = item.setdefault('extensions', {})
    extension.setdefault('creation_token', token)
    if kind == 'goal':
        extension.setdefault('goal', {'profile': GOAL, 'outcome': '', 'criteria': [
            {'key': 'criterion-1', 'statement': '', 'evidence_refs': [], 'assessment': 'unassessed'}],
            'scope': [], 'exclusions': [], 'work_links': []})
    else:
        extension.setdefault('personal_project', {'profile': PROJECT, 'purpose': '', 'scope': [],
            'exclusions': [], 'goal_links': [], 'work_links': []})
    missing = []
    for field in ('title', 'summary'):
        if not isinstance(item[field], str) or not item[field].strip():
            missing.append(field)
    try:
        (validate_goal if kind == 'goal' else validate_project)(item, writing=True, **({'private': True} if kind == 'project' else {}))
    except ts.ReadError as exc:
        missing.append(exc.message)
    return {'profile': 'eutonos.goal-form.v1', 'item': item, 'missing_fields': missing}


def read_shared(root, item_id=None):
    from .private_scope import require_shared
    from . import boards
    require_shared(root)
    result = boards.project(root)
    result['items'] = [i for i in result['items'] if i['type'] == 'goal' and (item_id is None or i['id'] == item_id)]
    if item_id is not None:
        ts.require(len(result['items']) == 1, 'missing', 'Selected shared goal is absent or ambiguous.')
        result['item'] = result['items'][0]
    return result


def form_shared(root, owner_path, item_id=None, draft=None):
    from . import boards, work_lifecycle, write_batch
    from .private_scope import require_shared
    require_shared(root)
    local = LocalRoot(root)
    _, owners, raw_by_path, entries, _ = boards._load(local, strict=True)
    ts.require(owner_path in (owners or []), 'denied', 'Select a configured goal owner.')
    original = None
    if item_id is not None:
        matches = [i for i in (entries[owner_path] or {}).get('items', []) if i['id'] == item_id and i['type'] == 'goal']
        ts.require(len(matches) == 1, 'missing', 'Selected goal is absent.')
        original = matches[0]
    guided = form('goal', {**(original or {}), **(draft or {})})
    item = guided['item']
    request = {'owner_path': owner_path, 'expected_sha256': digest(raw_by_path[owner_path])}
    if original is None:
        request.update(index=len((entries[owner_path] or {}).get('items', [])), items=[item])
    else:
        request.update(item_id=item_id, changes={k: v for k, v in item.items() if k != 'id'})
    return {**guided, 'expected_revision': work_lifecycle.item_revision(original) if original else None,
            'request': {'profile': write_batch.PROFILE, 'operation_id': str(uuid.uuid4()),
                        'goal_preconditions': {'owner_path': owner_path, 'item_id': item_id,
                                               'expected_revision': work_lifecycle.item_revision(original) if original else None},
                        'operations': [{'kind': 'update' if original else 'add', 'request': request}]}}


def submit_shared(root, request, *, host, actor, session):
    from . import boards, work_lifecycle
    from .private_scope import require_shared
    from .write_retry import submit_batch
    require_shared(root); _text(session); _text(actor)
    supplied = deepcopy(request)
    preconditions = supplied.pop('goal_preconditions', None)
    ts.require(isinstance(preconditions, dict), 'invalid', 'Use the goal form with observed item revision.')
    ts.shape(preconditions, {'owner_path', 'item_id', 'expected_revision'})
    operations = supplied.get('operations')
    ts.require(isinstance(operations, list) and len(operations) == 1, 'invalid', 'Shared goal submit accepts one semantic goal operation.')
    op = operations[0]
    ts.require(isinstance(op, dict) and op.get('kind') == ('update' if preconditions['item_id'] else 'add'),
               'invalid', 'Shared goal operation must match its observed precondition.')
    selected = op.get('request', {})
    ts.require(selected.get('owner_path') == preconditions['owner_path'], 'invalid', 'Shared goal owner must match its observed precondition.')
    if preconditions['item_id'] is not None:
        ts.require(selected.get('item_id') == preconditions['item_id'] and 'id' not in selected.get('changes', {}),
                   'invalid', 'Shared goal update must preserve the selected identity.')
    else:
        ts.require(preconditions['expected_revision'] is None, 'invalid', 'A new goal has no prior revision.')
        items = selected.get('items')
        ts.require(isinstance(items, list) and len(items) == 1 and 'item' not in selected, 'invalid', 'Add exactly one shared goal.')
        item = boards._item(items[0]); previous = []
        token = item.get('extensions', {}).get('creation_token')
        ts.require(token is not None and item['id'] == 'goal-' + token, 'invalid', 'New shared goals require the frozen form UUID.')

    def validate_new():
        # Only a new write interprets current owner bytes. Exact originals must
        # reach the durable batch receipt/effect recovery before this stale gate.
        if preconditions['item_id'] is not None:
            _, _, _, entries, _ = boards._load(LocalRoot(root), strict=True)
            matches = [i for i in (entries.get(preconditions['owner_path']) or {}).get('items', [])
                       if i['id'] == preconditions['item_id'] and i['type'] == 'goal']
            ts.require(len(matches) == 1 and work_lifecycle.item_revision(matches[0]) == preconditions['expected_revision'],
                       'stale', 'Shared goal revision changed after preparation.')
            item = boards._item({**matches[0], **selected.get('changes', {})})
            previous = matches[0].get('extensions', {}).get('goal', {}).get('work_links', [])
        else:
            item = boards._item(selected['items'][0]); previous = []
        ts.require(item['type'] == 'goal', 'invalid', 'Shared goal submit requires a semantic goal.')
        validate_goal(item, writing=True)
        from .scoped_items import check_shared_links
        check_shared_links(root, item['extensions']['goal']['work_links'], previous=previous)

    supplied['caller_context'] = {**supplied.get('caller_context', {}), 'session': session}
    return submit_batch(host, root, actor, supplied, validate_new=validate_new)
