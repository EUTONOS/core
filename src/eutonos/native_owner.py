"""Supported creation of an empty native board owner and its registry linkage."""
from __future__ import annotations

from copy import deepcopy
import json
import re
import uuid

from .private_scope import require_shared
from . import boards, tokenslang as ts
from .effects import LocalRoot, digest, plan as effect_plan
from .source import safe_path

PROFILE = 'eutonos.native-owner-register.v1'
REGISTRY = 'docs/REGISTRY.json'


def _path(path):
    path = safe_path(path)
    ts.require(path.startswith('docs/') and path.endswith('.md')
               and path not in {'docs/CURRENT_TASKS.md', 'docs/NEXT_WORK.md', 'docs/DONE_WORK.md', 'docs/WORK_ORDER.md'},
               'invalid', 'A new native board owner must be a new Markdown document under docs/.')
    return path


_DOCUMENT_ID = re.compile(r'eutonos\.doc\.[a-z0-9]+(?:-[a-z0-9]+)*')
_ROLE = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')


def _identity(request):
    """Return (document ID, registry role, intro) from an explicit optional trio or the generated native ones."""
    if not any(name in request for name in ('document_id', 'role', 'intro')):
        return 'native.board.' + request['creation_token'], 'native-board-owner-' + request['creation_token'], None
    ts.require('document_id' in request and 'role' in request, 'invalid',
               'An explicit document_id needs a role and the reverse; intro is optional.')
    document_id, role, intro = request['document_id'], request['role'], request.get('intro')
    ts.require(isinstance(document_id, str) and len(document_id) <= 80 and _DOCUMENT_ID.fullmatch(document_id),
               'invalid', 'document_id must look like eutonos.doc.some-name.')
    ts.require(isinstance(role, str) and len(role) <= 80 and _ROLE.fullmatch(role),
               'invalid', 'role must be a lowercase hyphenated registry role.')
    if intro is not None:
        ts.require(isinstance(intro, str) and intro.strip() and len(intro) <= 2000 and '```' not in intro
                   and all(c.isprintable() or c == '\n' for c in intro) and not intro.lstrip().startswith('#'),
                   'invalid', 'intro is bounded plain prose without code fences or a heading.')
    return document_id, role, intro


def _document(title, token, document_id=None, intro=None):
    ts.require(isinstance(title, str) and title.strip() and len(title) <= 200
               and all(c.isprintable() for c in title), 'invalid', 'Use a bounded single-line owner title.')
    label = 'Document ID: native.board.' + token if document_id is None else 'Document ID: `' + document_id + '`.'
    prose = '' if intro is None else intro.strip('\n') + '\n\n'
    return ('# ' + title + '\n\n' + label + '\n\n' + prose + '```tokenslang-board\n').encode() + boards._json({'profile': boards.BOARD, 'items': []}) + b'\n```\n'


def form(root, path, title, document_id=None, role=None, intro=None):
    require_shared(root)
    local = LocalRoot(root)
    path = _path(path)
    token = str(uuid.uuid4())
    request = {'profile': PROFILE, 'owner_path': path, 'title': title, 'creation_token': token,
               'expected_config_sha256': digest(local.read('eutonos.boards.json')),
               'expected_registry_sha256': digest(local.read(REGISTRY))}
    request.update({name: value for name, value in (('document_id', document_id), ('role', role), ('intro', intro))
                    if value is not None})
    plan(local, request)
    identity, _, prose = _identity(request)
    return {'profile': PROFILE, 'outcome': 'form', 'owner_path': path,
            'owner_sha256': digest(_document(title, token, identity if 'document_id' in request else None, prose)),
            'batch_operation': {'kind': 'owner-register', 'request': request},
            'next': 'Put batch_operation before an add operation in the same managed write batch. Use owner_sha256 for that add.'}


def plan(root, request):
    require_shared(root)
    local = root if isinstance(root, LocalRoot) else LocalRoot(root)
    ts.shape(request, {'profile', 'owner_path', 'title', 'creation_token',
                       'expected_config_sha256', 'expected_registry_sha256'}, {'document_id', 'role', 'intro'})
    ts.require(request['profile'] == PROFILE, 'unsupported', 'Unknown native owner form.')
    token = str(uuid.UUID(request['creation_token']))
    ts.require(token == request['creation_token'], 'invalid', 'Use the exact frozen full UUID creation token.')
    path = _path(request['owner_path'])
    document_id, role, intro = _identity(request)
    ts.require(local.read(path) is None, 'conflict', 'New owner path already exists.')
    cfg_raw, owners = boards._config(local)
    ts.require(cfg_raw is not None, 'missing', 'Native board configuration must already exist.')
    registry_raw = local.read(REGISTRY)
    ts.require(digest(cfg_raw) == request['expected_config_sha256']
               and digest(registry_raw) == request['expected_registry_sha256'], 'stale', 'Owner configuration or registry changed.')
    ts.require(path not in owners, 'conflict', 'Owner path is already registered.')
    registry = json.loads(registry_raw) if registry_raw else {'schema': 'eutonos.documentation-bootstrap.v0', 'documents': []}
    ts.require(isinstance(registry, dict) and isinstance(registry.get('documents'), list), 'invalid', 'Registry documents are unavailable.')
    ts.require(not any(row.get('path') == path or row.get('id') == document_id
                       for row in registry['documents'] if isinstance(row, dict)), 'conflict', 'Registry identity/path already exists.')
    registry = deepcopy(registry)
    registry['documents'].append({'id': document_id, 'path': path, 'role': role})
    _, _, _, existing, _ = boards._load(local, strict=True)
    boards._validate_graph(existing)
    updates = {path: _document(request['title'], token, document_id if 'document_id' in request else None, intro),
               'eutonos.boards.json': boards._json({'profile': boards.CONFIG, 'owners': [*owners, path]}) + b'\n',
               REGISTRY: boards._json(registry) + b'\n'}
    effect = effect_plan(local, updates, inputs=[*owners, 'eutonos.read.json'])
    return {'effect': effect, 'paths': [row['path'] for row in effect['changes']]}
