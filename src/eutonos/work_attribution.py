"""Native work provenance, local participant identity, and exact sign-off evidence.

Recorded identity is caller provenance, never authentication or approval.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from . import tokenslang as ts
from .effects import digest

PROFILE = 'eutonos.participant-profile.v1'
ATTRIBUTION = 'eutonos.work-attribution.v1'
LOCAL = '.eutonos-local/participant-profile.json'


def profile_root(root):
    from .settings import _resolve_shared_root
    return _resolve_shared_root(root)


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128 or any(ord(c) < 32 for c in value):
        raise ValueError('Both given and family names are required (1..128 characters each).')
    return value.strip()


def human(value):
    if value is None: return None
    ts.shape(value, {'id', 'given_name', 'family_name', 'full_name'})
    ts.require(str(uuid.UUID(value['id'])) == value['id'], 'invalid', 'Participant ID must be a canonical UUID.')
    given, family = _name(value['given_name']), _name(value['family_name'])
    ts.require(value['full_name'] == given + ' ' + family, 'invalid', 'Full name must retain the given and family names.')
    return deepcopy(value)


def profile_status(root):
    path = profile_root(root) / LOCAL
    raw = path.read_bytes() if path.is_file() else None
    participant = None
    if raw is not None:
        saved = json.loads(raw)
        ts.shape(saved, {'profile', 'participant'})
        ts.require(saved['profile'] == PROFILE, 'unsupported', 'Unknown participant profile.')
        participant = human(saved['participant'])
    return {'profile': PROFILE, 'configured': participant is not None, 'participant': participant,
            'revision': digest(raw), 'scope': 'local-clone', 'path': str(path),
            'setup': None if participant else {'required':False,'instruction':'Ask the owner once for both given and family names; never guess. Save through Settings Participant profile or settings participant-profile set.'}}


def profile_hint(root):
    try: return profile_status(root)
    except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        return {'profile':PROFILE,'configured':False,'participant':None,'revision':None,'scope':'local-clone',
                'status':'unavailable','error':str(exc),'setup':{'required':False,'instruction':'Inspect the existing local profile before recording signatures; do not guess or replace identity.'}}


def configure_profile(root, value, expected_revision):
    ts.shape(value, {'given_name', 'family_name'}, {'participant_id'})
    given, family = _name(value['given_name']), _name(value['family_name'])
    explicit = value.get('participant_id')
    if explicit is not None and str(uuid.UUID(explicit)) != explicit:
        raise ValueError('Use a canonical participant UUID for deliberate identity reuse.')
    from .local_lock import local_lock
    from .settings import _atomic, StaleSettings
    selected = profile_root(root)
    with local_lock(selected):
        current = profile_status(root)
        if current['revision'] != expected_revision:
            raise StaleSettings({'participant_profile': current, 'revision': current['revision']})
        retained = (current['participant'] or {}).get('id')
        if retained and explicit is not None and explicit != retained:
            raise ValueError('A profile rename cannot replace its stable participant ID.')
        participant = {'id': retained or explicit or str(uuid.uuid4()), 'given_name': given,
                       'family_name': family, 'full_name': given + ' ' + family}
        _atomic(selected / LOCAL, {'profile': PROFILE, 'participant': participant})
    return profile_status(root)


def context(value=None):
    value = value or {}
    ts.shape(value, set(), {'app', 'model', 'session'})
    result = {}
    for key in ('app', 'model', 'session'):
        v = value.get(key)
        ts.require(v is None or isinstance(v, str) and bool(v.strip()) and len(v) <= 256,
                   'invalid', 'Caller context must be a bounded actual app/model/session or null.')
        result[key] = v
    return result


def freeze(root, caller=None):
    return {'event_id': str(uuid.uuid4()), 'recorded_at': datetime.now(timezone.utc).isoformat(),
            'human': profile_status(root)['participant'], 'context': context(caller)}


def validate_snapshot(value):
    ts.shape(value, {'event_id', 'recorded_at', 'human', 'context'})
    ts.require(str(uuid.UUID(value['event_id'])) == value['event_id'], 'invalid', 'Attribution requires a frozen UUID.')
    ts.require(datetime.fromisoformat(value['recorded_at']).tzinfo is not None, 'invalid', 'Attribution timestamp requires timezone.')
    human(value['human']); context(value['context'])
    return deepcopy(value)


def unknown_snapshot(identity, recorded_at):
    return {'event_id': str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
            'recorded_at': recorded_at, 'human': None, 'context': context()}


def signs(kind, payload):
    if kind == 'batch':
        return any(op['kind'] == 'add' or
                   op['kind'] == 'work' and signs('work-lifecycle', {'request': op['request']}) or
                   op['kind'] == 'update' and signs('board-update', op['request'])
                   for op in payload['batch']['operations'])
    if kind == 'work-lifecycle':
        request = payload['request']
        return request.get('operation') in {'complete','review','integrate'} or 'completion_checks' in request
    if kind == 'board-update':
        changes = payload.get('changes') or {}
        return changes.get('status') == 'completed' or (
            isinstance(changes.get('extensions'), dict) and
            'completion_checks' in changes['extensions'])
    return False


def snapshot(root, request):
    if '_attribution' in request: return validate_snapshot(request['_attribution'])
    caller = deepcopy(request.get('caller_context') or {})
    claimed = request.get('requested_by') or {}
    if claimed.get('kind') == 'actual-session' and not caller.get('session'): caller['session'] = claimed['value']
    return freeze(root, caller)


def event(binding, item_id, kind, **evidence):
    result = deepcopy(binding)
    result['event_id'] = str(uuid.uuid5(uuid.UUID(binding['event_id']), item_id + ':' + kind))
    return {**result, 'kind': kind, **deepcopy(evidence)}


def create(item, binding):
    extensions = item.setdefault('extensions', {})
    ts.require('attribution' not in extensions, 'denied', 'Creator provenance is assigned by the native writer; import original provenance through import.')
    extensions['attribution'] = {'profile': ATTRIBUTION, 'creator': event(binding, item['id'], 'created'), 'events': []}


def append(item, binding, kind, **evidence):
    extensions = item.setdefault('extensions', {})
    value = extensions.setdefault('attribution', {'profile': ATTRIBUTION, 'creator': None, 'events': []})
    ts.require(isinstance(value,dict) and value.get('profile') == ATTRIBUTION and isinstance(value.get('events'),list), 'invalid', 'Native attribution history is unavailable; reconcile its exact source before signing.')
    entry = event(binding, item['id'], kind, **evidence)
    ts.require(all(isinstance(e,dict) and isinstance(e.get('event_id'),str) for e in value['events']), 'invalid', 'Existing native contributor history is unavailable; reconcile before appending.')
    ts.require(not any(e['event_id'] == entry['event_id'] for e in value['events']), 'conflict', 'Attribution event already exists.')
    value['events'].append(entry)
    return entry


def preserve(before, changes):
    if 'extensions' in changes:
        old = before.get('extensions', {})
        for key in ('attribution', 'work_history'):
            ts.require(changes['extensions'].get(key) == old.get(key), 'denied', 'Native creator and contributor history must be preserved.')


def native_item(item):
    # Projection fields never become native content or invalidate a signature.
    return {k: deepcopy(item[k]) for k in ('id','type','title','summary','status','parent_id','evidence_refs','next_action','extensions') if k in item}


def content_revision(item):
    value = native_item(item)
    extensions = value.get('extensions', {})
    extensions.pop('attribution', None)
    history = extensions.get('work_history', [])
    if isinstance(history,list):
        for entry in history:
            if isinstance(entry,dict): entry.pop('attribution', None)
    if not extensions: value.pop('extensions', None)
    return digest(ts.encoded(value))


def commit(root, oid):
    from .git_delivery import text
    ts.require(isinstance(oid, str) and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', oid), 'invalid', 'Supply an exact full Git commit ID.')
    ts.require(text(root, 'cat-file', '-t', oid) == 'commit', 'invalid', 'Git evidence must name a commit object.')
    return oid


def integration_proof(root, value):
    ts.shape(value, {'source_commit', 'target_ref', 'target_commit'})
    from .git_delivery import git, text
    # Match publisher topology: raw objects and no legacy graft graph.
    for name in ('--git-dir', '--git-common-dir'):
        path = Path(text(root, 'rev-parse', name)); path = path if path.is_absolute() else Path(root) / path
        ts.require(not (path / 'info/grafts').exists() and not (path / 'info/grafts').is_symlink(), 'unsupported', 'Integration proof does not support Git grafts.')
    graft = Path(text(root, 'rev-parse', '--git-path', 'info/grafts'))
    graft = graft if graft.is_absolute() else Path(root) / graft
    ts.require(not graft.exists() and not graft.is_symlink(), 'unsupported', 'Integration proof does not support Git grafts.')
    source, target = commit(root, value['source_commit']), commit(root, value['target_commit'])
    ref = value['target_ref']
    ts.require(isinstance(ref, str) and ref.startswith('refs/') and git(root, 'check-ref-format', ref, check=False).returncode == 0,
               'invalid', 'Integration target requires an exact refs/... reference.')
    ts.require(text(root, 'rev-parse', '--verify', ref + '^{commit}') == target, 'stale', 'Target ref no longer names the supplied target commit.')
    ts.require(git(root, 'merge-base', '--is-ancestor', source, target, check=False).returncode == 0,
               'unverified', 'Source is not a raw Git ancestor of target; squash mapping remains unverified.')
    return {**deepcopy(value), 'verification': 'raw-git-ancestry', 'verified': True}


def label(entry, software=False):
    name = ((entry or {}).get('human') or {}).get('full_name') or 'Unknown'
    ctx = (entry or {}).get('context') or {}
    suffix = ctx.get('model') or ctx.get('app')
    return name + (' · ' + suffix if software and suffix else '')


def project(item, attempts=()):
    extensions = item.get('extensions', {})
    imported = extensions.get('source_import') or {}
    original = imported.get('source_item') if isinstance(imported,dict) else None
    value = extensions.get('attribution', {})
    if not value and isinstance(original,dict): value = original.get('extensions', {}).get('attribution', {})
    diagnostics = []
    try:
        if value:
            ts.shape(value, {'profile','creator','events'})
            ts.require(value['profile'] == ATTRIBUTION and isinstance(value['events'], list), 'invalid', 'Unavailable attribution schema.')
            for e in ([value['creator']] if value['creator'] else []) + value['events']:
                ts.require(isinstance(e,dict) and e.get('kind') in {'created','completed','reviewed','integrated'}, 'invalid', 'Unavailable attribution event.')
                validate_snapshot({k:e[k] for k in ('event_id','recorded_at','human','context')})
        creator = deepcopy(value.get('creator'))
        events = deepcopy(value.get('events', []))
    except (ValueError, TypeError, KeyError, AttributeError, ts.ReadError):
        creator, events = None, []
        diagnostics.append('Recorded attribution is unavailable; identity remains unknown.')
    completions = [e for e in events if e.get('kind') == 'completed']
    workers = [deepcopy(a) for a in attempts if a.get('state') == 'active' and a.get('target', {}).get('work_id') == item['id']]
    revision = content_revision(item)
    reviews = [{**e, 'current': e.get('reviewed_revision') == revision if not original else None,
                'binding_scope':'source-original' if original else 'native-content',
                'source_status': 'recorded' if e.get('source_commit') else 'unknown'} for e in events if e.get('kind') == 'reviewed']
    integrations = [{**e, 'verification_status':'recorded', 'target_status':'not-rechecked'} for e in events if e.get('kind') == 'integrated']
    contributor_map = {}
    for e in completions:
        person, ctx = e.get('human') or {}, e.get('context') or {}
        key = ('human',person['id']) if person.get('id') else ('unknown',ctx.get('app'),ctx.get('model'),ctx.get('session') or e['event_id'])
        history = contributor_map.get(key, {}).get('events', [])
        contributor_map[key] = {**e, 'events': [*history,deepcopy(e)]}
    contributors = list(contributor_map.values())
    projected = {e['event_id']:e for e in [*reviews,*integrations]}
    events = [projected.get(e['event_id'],e) for e in events]
    completion = completions[-1] if completions else None
    return {'creator': creator, 'workers': workers, 'completion': completion, 'contributors': contributors,
            'reviews': reviews, 'integrations': integrations, 'events':events, 'content_revision': revision, 'diagnostics':diagnostics,
            'provenance_scope':'source-original' if original else 'native',
            'labels': {'creator': 'Created by ' + label(creator),
                       'workers': [label(a.get('attribution'), True) for a in workers],
                       'completion': 'Completed by ' + label(completion, True) if item['status'] == 'completed' else None}}


def cli(argv=None):
    import argparse
    p = argparse.ArgumentParser(description='Local human profile, shared by apps and linked worktrees.')
    p.add_argument('--root', default='.')
    p.add_argument('operation', choices=('status','set'))
    p.add_argument('--given-name'); p.add_argument('--family-name'); p.add_argument('--participant-id')
    p.add_argument('--expected-revision')
    args = p.parse_args(argv)
    try:
        value = {'given_name': args.given_name, 'family_name': args.family_name}
        if args.participant_id: value['participant_id'] = args.participant_id
        result = profile_status(args.root) if args.operation == 'status' else configure_profile(args.root, value, args.expected_revision)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (ValueError, OSError, ts.ReadError) as exc:
        print(json.dumps({'outcome': 'error', 'message': str(exc)})); return 2
