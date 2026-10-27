"""Explicit private ZIP exports: Git history plus checked current canonical state.

An export is not evidence of independent media. Host journals, grants, delivery
intents and application caches are never copied or restored.
"""
from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import zipfile

from . import git_delivery as delivery, personal, personal_history as history, tokenslang as ts
from .effects import LocalRoot, digest
from .private_scope import MANIFEST, require_owner, validate_private_destination

PROFILE = 'eutonos.personal-backup.v1'
ENTRIES = {'manifest.json', 'history.bundle', *(f'snapshots/{p}' for p in history.PATHS)}
MAX_ARCHIVE = 256 * 1024 * 1024


def _destination(path, source=None):
    boundary = validate_private_destination(path, source_root=source)
    ts.require(boundary['status'] == 'allowed', 'denied', 'Select a supported private destination.')
    return boundary


def _settled(root, host, actor, owner_id):
    coordinator, authority = history._assert_host(root, host, actor, owner_id)
    root_key = coordinator._root(authority)
    with coordinator._db() as db:
        effects = db.execute("SELECT effects.id FROM effects JOIN attempts ON attempts.id=effects.attempt WHERE attempts.root=? AND effects.state IN ('prepared','rolling-back')", (root_key,)).fetchall()
        held = db.execute('SELECT path FROM reservations WHERE root=?', (root_key,)).fetchall()
    ts.require(not effects and not held, 'conflict', 'Recover unresolved personal native effects before export.')


def _history_scope(root, branch, manifest):
    for commit in delivery.text(root, 'rev-list', branch).splitlines():
        for raw in delivery.git(root, 'ls-tree', '-r', '-z', commit).stdout.split(b'\0'):
            if not raw:
                continue
            header, path = raw.split(b'\t', 1)
            ts.require(header.startswith(b'100644 blob ') and path.decode() in history.PATHS,
                       'invalid', 'Personal bundle history contains noncanonical content.')
        raw = delivery.blob(root, commit, MANIFEST)
        ts.require(raw is not None and len(raw) <= 16384, 'invalid', 'Personal bundle history manifest is missing or oversized.')
        try:
            committed = ts.loads(raw.decode('utf-8'))
        except UnicodeError:
            raise ts.ReadError('invalid', 'Personal bundle history manifest is invalid.') from None
        ts.require(committed == manifest, 'invalid', 'Personal bundle history identity or owner differs.')


def export(root, destination, owner_id, host, actor):
    require_owner(root, owner_id)
    root = Path(root).resolve()
    destination = Path(destination)
    boundary = _destination(destination, root)
    ts.require(not destination.exists() and destination.parent.is_dir(), 'conflict', 'Select a new archive in an existing private directory.')
    manifest = require_owner(root, owner_id)
    _settled(root, host, actor, owner_id)
    personal.read(root, owner_id=owner_id)
    local = LocalRoot(root)
    snapshots = {p: local.read(p) for p in history.PATHS}
    ts.require(all(b is not None for b in snapshots.values()), 'missing', 'Personal snapshot is incomplete.')
    engine = delivery.Engine(root)
    config = engine.config()
    history._validate_configuration(root, config, stored=True)
    branch = config['branch']
    _history_scope(root, branch, manifest)
    oid = delivery.text(root, 'rev-parse', branch)
    with tempfile.TemporaryDirectory(prefix='personal-export-') as folder:
        bundle = Path(folder) / 'history.bundle'
        delivery.git(root, 'bundle', 'create', str(bundle), branch)
        delivery.git(root, 'bundle', 'verify', str(bundle))
        refs = delivery.text(root, 'bundle', 'list-heads', str(bundle)).splitlines()
        ts.require(refs == [oid + ' ' + branch], 'conflict', 'Bundle refs differ from private history.')
        blobs = {'history.bundle': bundle.read_bytes(), **{'snapshots/' + p: b for p,b in snapshots.items()}}
        ts.require(sum(map(len, blobs.values())) <= MAX_ARCHIVE, 'overflow', 'Personal export exceeds the bounded archive size.')
        receipt = {'profile':PROFILE, 'owner_id':owner_id, 'identity':manifest['identity'],
                   'branch':branch, 'commit_oid':oid,
                   'files':{p: {'sha256':digest(b),'size':len(b)} for p,b in blobs.items()},
                   'independence':'unverified'}
        _settled(root, host, actor, owner_id)
        ts.require(delivery.text(root, 'rev-parse', branch) == oid and all(local.read(p) == b for p,b in snapshots.items()), 'stale', 'Personal state changed during export.')
        with destination.open('xb') as stream:
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', ts.encoded(receipt))
                for path, body in blobs.items():
                    archive.writestr(path, body)
    # Read the exact archive again; submission/ZIP close is not verification.
    checked, _ = _read_archive(destination, owner_id)
    ts.require(checked == receipt, 'conflict', 'Archive readback differs.')
    return {'outcome':'completed', 'backup':{'state':'verified','archive':str(destination),
            'commit_oid':oid,'identity':manifest['identity'],'boundary':boundary['boundary'],
            'independence':'unverified'}}


def _read_archive(archive, owner_id):
    try:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            ts.require(len(entries) == len(ENTRIES) and {i.filename for i in entries} == ENTRIES,
                       'invalid', 'Backup entries are duplicated or outside canonical paths.')
            ts.require(sum(i.file_size for i in entries) <= MAX_ARCHIVE and
                       all(not i.is_dir() and (i.external_attr >> 16 & 0o170000) != stat.S_IFLNK for i in entries),
                       'invalid', 'Backup contains oversized or linked entries.')
            content = {i.filename:z.read(i) for i in entries}
        manifest = ts.loads(content.pop('manifest.json').decode('utf-8'))
        ts.shape(manifest, {'profile','owner_id','identity','branch','commit_oid','files','independence'})
        ts.shape(manifest['identity'], {'repository_id','scope_id','namespace'})
        ts.require(manifest['profile'] == PROFILE and manifest['owner_id'] == owner_id and manifest['independence'] == 'unverified',
                   'denied', 'Backup identity does not match the selected personal owner.')
        ts.require(set(manifest['files']) == set(content), 'invalid', 'Backup checksum inventory differs.')
        for path, body in content.items():
            ts.shape(manifest['files'][path], {'sha256','size'})
            ts.require(manifest['files'][path] == {'sha256':digest(body),'size':len(body)}, 'invalid', 'Backup bytes differ from checksums.')
        return manifest, content
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ts.ReadError):
            raise
        raise ts.ReadError('invalid', 'Personal backup archive is invalid.') from None


def restore(archive, destination, owner_id):
    destination = Path(destination)
    boundary = _destination(destination)
    ts.require(destination.parent.is_dir() and (not destination.exists() or destination.is_dir() and not any(destination.iterdir())),
               'conflict', 'Restore requires a new empty private destination.')
    manifest, content = _read_archive(archive, owner_id)
    ts.require(isinstance(manifest['branch'],str) and manifest['branch'].startswith('refs/heads/')
               and isinstance(manifest['commit_oid'],str), 'invalid', 'Invalid personal history ref.')
    delivery.git(destination.parent, 'check-ref-format', manifest['branch'])
    with tempfile.TemporaryDirectory(prefix='personal-restore-check-', dir=destination.parent) as folder:
        candidate = Path(folder) / 'scope'
        candidate.mkdir()
        delivery.git(candidate, 'init', '-b', manifest['branch'].removeprefix('refs/heads/'))
        bundle = Path(folder) / 'history.bundle'
        bundle.write_bytes(content['history.bundle'])
        delivery.git(candidate, 'bundle', 'verify', str(bundle))
        refs = delivery.text(candidate, 'bundle', 'list-heads', str(bundle)).splitlines()
        ts.require(refs == [manifest['commit_oid'] + ' ' + manifest['branch']], 'invalid', 'Backup history refs differ.')
        delivery.git(candidate, 'bundle', 'unbundle', str(bundle))
        delivery.git(candidate, 'update-ref', manifest['branch'], manifest['commit_oid'])
        delivery.git(candidate, 'read-tree', manifest['commit_oid'])
        for path in history.PATHS:
            (candidate / path).write_bytes(content['snapshots/' + path])
        owner = require_owner(candidate, owner_id)
        ts.require(owner['identity'] == manifest['identity'], 'invalid', 'Backup snapshot identity differs.')
        _history_scope(candidate, manifest['branch'], owner)
        personal.read(candidate, owner_id=owner_id)
        # Validation completes before the chosen destination acquires any bytes.
        if destination.exists():
            destination.rmdir()
        candidate.rename(destination)
    history.configure_local(destination, owner_id, branch=manifest['branch'])
    ts.require(all((destination / p).read_bytes() == content['snapshots/' + p] for p in history.PATHS), 'conflict', 'Restore snapshot readback differs.')
    return {'outcome':'completed', 'backup':{'state':'verified','identity':manifest['identity'],
            'commit_oid':manifest['commit_oid'],'boundary':boundary['boundary'],'independence':'unverified'},
            'fresh_admission_required':True,'reenrollment_required':True}
