"""Opt-in scoped Git delivery. Native save, transport and integration stay separate.

No daemon, automatic merge, generated repository scripts, or production writer hooks.
Only participating publishers share the OS lock; unmanaged Git/editor races are
checked before and after commit and block publication rather than rewritten.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from .hidden_process import no_window
import tempfile
import time

from .local_lock import local_lock
from .source import safe_path

CONFIG = 'eutonos.git-delivery.config.v1'
PRIVATE_CONFIG = 'eutonos.personal-history.config.v1'
REQUEST = 'eutonos.git-delivery.request.v1'
CLOSEOUT_REQUEST = 'eutonos.git-delivery.closeout-code.v1'
SCHEMA = 1
MODES = {'manual', 'commit-only', 'commit+push'}


class DeliveryError(ValueError):
    """An actionable limitation; never an instruction to rewrite Git history."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def freeze(body):
    return None if body is None else base64.b64encode(body).decode('ascii')


def thaw(body):
    return None if body is None else base64.b64decode(body, validate=True)


def git(root, *args, input=None, check=True, env=None):
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    environment.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat'})
    if env:
        environment.update(env)
    # Push transfers original objects, so every verification/recovery read must
    # inspect that same object graph regardless of local replacement refs.
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(root), *args], input=input,
                            capture_output=True, timeout=60, env=environment, **no_window())
    if check and result.returncode:
        # Keep diagnostics bounded and never echo command credentials.
        raise DeliveryError('Git operation failed: ' + args[0] + ': ' + result.stderr.decode(errors='replace')[-1500:])
    return result


def text(root, *args):
    return git(root, *args).stdout.decode().strip()


def blob(root, commit, path):
    result = git(root, 'show', commit + ':' + path, check=False)
    return result.stdout if not result.returncode else None


def validate_changes(changes):
    if not isinstance(changes, dict) or not 1 <= len(changes) <= 16:
        raise DeliveryError('Select 1-16 exact paths.')
    result = {}
    total = 0
    for path, change in changes.items():
        path = safe_path(path)
        if path == '.git' or path.startswith(('.git/', '.eutonos-local/')):
            raise DeliveryError('Internal delivery state is not publishable.')
        if not isinstance(change, dict) or set(change) != {'before', 'after'}:
            raise DeliveryError('Changes require frozen before and after bytes.')
        for value in change.values():
            if value is not None and not isinstance(value, bytes):
                raise DeliveryError('Expected bytes or deletion.')
            total += len(value or b'')
        if change['before'] == change['after']:
            raise DeliveryError('Empty change is not a delivery request.')
        result[path] = {k: freeze(v) for k, v in change.items()}
    if total > 2_000_000:
        raise DeliveryError('Frozen scope exceeds 2,000,000 bytes.')
    return result


class Engine:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if text(self.root, 'rev-parse', '--show-toplevel') != str(self.root).replace('\\', '/') and Path(text(self.root, 'rev-parse', '--show-toplevel')).resolve() != self.root:
            raise DeliveryError('Select the exact worktree root.')
        common = Path(text(self.root, 'rev-parse', '--git-common-dir'))
        self.common = (self.root / common).resolve() if not common.is_absolute() else common.resolve()
        from .private_scope import is_private
        if is_private(self.root):
            from .personal_history import _standalone
            _standalone(self.root)
        self.home = self.common / 'eutonos-delivery'
        self.home.mkdir(exist_ok=True)
        self.dbpath = self.home / 'journal.sqlite'
        with self.db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA):
                raise DeliveryError('Unsupported delivery journal schema; upgrade or use manual Git.')
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise DeliveryError('Unversioned existing delivery journal is unsupported.')
                db.executescript('''
                    CREATE TABLE config(root TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE intents(id TEXT PRIMARY KEY,payload TEXT NOT NULL,native TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);
                    CREATE TABLE jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL,plan TEXT,commit_oid TEXT,error TEXT,checks TEXT,authorized_revision INTEGER NOT NULL);
                    PRAGMA user_version=1;
                ''')
            db.execute('CREATE INDEX IF NOT EXISTS intents_state ON intents(state)')
            db.execute("CREATE INDEX IF NOT EXISTS jobs_closeout_binding ON jobs(state,json_extract(payload,'$.closeout.attempt'),json_extract(payload,'$.closeout.actor'),json_extract(payload,'$.closeout.host'))")
            db.execute("CREATE INDEX IF NOT EXISTS intents_native_operation ON intents(json_extract(native,'$.attempt'),json_extract(native,'$.actor'),json_extract(native,'$.coordinator'))")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.dbpath, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def config(self):
        with self.db() as db:
            row = db.execute('SELECT payload FROM config WHERE root=?', (str(self.root),)).fetchone()
        return json.loads(row[0]) if row else {'profile': CONFIG, 'mode': 'manual', 'revision': 0}

    def _topology(self, root):
        # --no-replace-objects does not disable legacy grafts. Inspect both the
        # effective path and worktree/common directories without changing them.
        paths = [text(root, 'rev-parse', '--git-path', 'info/grafts')]
        paths.extend(str(Path(text(root, 'rev-parse', name)) / 'info/grafts')
                     for name in ('--git-dir', '--git-common-dir'))
        for value in paths:
            path = Path(value)
            if not path.is_absolute(): path = Path(root) / path
            if path.exists() or path.is_symlink():
                raise DeliveryError('Git graft topology is unsupported; inspect info/grafts and use reviewed manual Git.')

    def _routing(self, root, destination):
        # Native Git honors URL rewrites even for an explicit URL/path. Refuse
        # matching rewrites before read or push rather than detect a wrong push.
        result = git(root, 'config', '--null', '--get-regexp',
                     r'^url\..*\.(insteadof|pushinsteadof)$', check=False)
        if result.returncode not in (0, 1):
            raise DeliveryError('Cannot inspect Git URL routing; use manual Git.')
        for entry in result.stdout.decode().split('\0'):
            if not entry: continue
            name, prefix = entry.split('\n', 1)
            replacement = name[4:name.rfind('.')]
            if destination.startswith(prefix) and replacement + destination[len(prefix):] != destination:
                raise DeliveryError('Git URL rewrite changes the configured destination; remove the matching insteadOf/pushInsteadOf rule or explicitly configure its destination.')

    def configure(self, value, *, expected_revision=0):
        from .private_scope import require_shared
        require_shared(self.root)
        keys = {'profile', 'project_id', 'participant', 'mode', 'remote_url', 'remote_ref', 'branch', 'canonical_base', 'allowed_paths', 'checks', 'records_ref'}
        if not isinstance(value, dict) or set(value) != keys or value.get('profile') != CONFIG:
            raise DeliveryError('Unsupported configuration; use the v1 explicit contract.')
        if value['mode'] not in MODES or not all(isinstance(value[k], str) and value[k] for k in ('project_id', 'participant', 'remote_url', 'remote_ref', 'branch', 'canonical_base')):
            raise DeliveryError('Invalid delivery binding.')
        destination=value['remote_url']
        if not (Path(destination).is_absolute() or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://',destination) or re.match(r'^[^/\\:]+@[^/\\:]+:',destination)):
            raise DeliveryError('Relative filesystem remote destinations are unsupported; choose an absolute path or URL.')
        if value['remote_url'].startswith('-') or not value['remote_ref'].startswith('refs/heads/') or not value['branch'].startswith('refs/heads/'):
            raise DeliveryError('Use explicit branch refs and destination URL.')
        for ref in (value['branch'], value['remote_ref'], value['records_ref']):
            git(self.root, 'check-ref-format', ref)
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['canonical_base']):
            raise DeliveryError('Canonical baseline must be a pinned commit OID.')
        git(self.root, 'cat-file', '-e', value['canonical_base'] + '^{commit}')
        if not isinstance(value['allowed_paths'], list) or not value['allowed_paths']:
            raise DeliveryError('Configure explicit allowed paths or directory prefixes.')
        for path in value['allowed_paths']:
            safe_path(path.rstrip('/'))
        if not isinstance(value['checks'], list) or not value['checks'] or any(not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv) for argv in value['checks']):
            raise DeliveryError('Configure existing check adapters as argv lists.')
        self._routing(self.root, destination)
        with self.db() as db:
            row = db.execute('SELECT payload FROM config WHERE root=?', (str(self.root),)).fetchone()
            old = json.loads(row[0]) if row else {'revision': 0}
            if old['revision'] != expected_revision:
                raise DeliveryError('Configuration revision changed.')
            new = {**value, 'root': str(self.root), 'revision': expected_revision + 1,
                   'activation_cursor': time.time_ns()}
            db.execute('INSERT OR REPLACE INTO config VALUES(?,?)', (str(self.root), encoded(new)))
        return new

    def configure_private(self, value, *, expected_revision=0):
        from .personal_history import _configure
        return _configure(self, value, expected_revision=expected_revision)

    @staticmethod
    def authority(cfg):
        return {k: v for k, v in cfg.items() if k not in {'revision', 'mode', 'activation_cursor'}}

    def _request(self, logical_id, changes, *, kind, session, work_id,check_eligibility=True):
        cfg = self.config()
        if cfg['profile'] != PRIVATE_CONFIG:
            from .private_scope import require_shared
            require_shared(self.root)
        else:
            from .private_scope import require_owner
            require_owner(self.root, cfg['participant'])
        if cfg['revision'] == 0:
            raise DeliveryError('Configure this project explicitly; absent configuration is manual.')
        if kind not in ({'personal'} if cfg['profile'] == PRIVATE_CONFIG else {'code', 'records'}) or not all(isinstance(v, str) and v for v in (logical_id, session, work_id)):
            raise DeliveryError('Select kind and stable logical/session/work identities.')
        frozen = validate_changes(changes)
        for path, values in frozen.items():
            if not self._allowed(path, cfg):
                raise DeliveryError('Path outside authority: ' + path)
            if check_eligibility:self._eligible(path,thaw(values['after']))
        key=fingerprint([cfg['project_id'],logical_id])
        with self.db() as db:
            old=db.execute('SELECT payload FROM jobs WHERE id=?',(key,)).fetchone() or db.execute('SELECT payload FROM intents WHERE id=?',(key,)).fetchone()
        if old:
            prior=json.loads(old[0])
            if prior['kind']!=kind or prior['changes']!=frozen or prior['session']!=session or prior['work_id']!=work_id or self.authority(prior['config'])!=self.authority(cfg):
                raise DeliveryError('Logical delivery identity conflicts with original request.')
            return prior
        return {'profile': REQUEST, 'logical_id': logical_id, 'kind': kind, 'config': cfg,
                'actor': cfg['participant'], 'session': session, 'work_id': work_id,
                'source_parent': text(self.root, 'rev-parse', 'HEAD'), 'changes': frozen}

    @staticmethod
    def _allowed(path, cfg):
        return any(path == p or p.endswith('/') and path.startswith(p) for p in cfg['allowed_paths'])

    def _eligible(self,path,body,read=None):
        from . import ram, tokenslang as ts
        reader=read or (lambda p: (self.root/p).read_bytes() if (self.root/p).is_file() else None)
        cfg=reader('eutonos.ram.json')
        try:
            root=safe_path(ram.unpack(cfg)['store_root']) if cfg is not None else 'RAM'
        except (ts.ReadError,ValueError,KeyError,TypeError) as exc:
            raise DeliveryError('Invalid native RAM configuration; repair eutonos.ram.json before publication.') from exc
        # Body failure must not change the independently configured store scope.
        try:
            value=json.loads(body) if body is not None else None
            is_record=isinstance(value,dict) and value.get('profile')==ram.RECORD
        except (ValueError,UnicodeError):
            is_record=False
        if body is None or not (path==root or path.startswith(root+'/') or is_record): return
        if not cfg: raise DeliveryError('Native RAM configuration is required for transport eligibility.')
        # A snapshot reader materializes only frozen values needed by the native
        # loader, including children. It never consults live checkout identities.
        with tempfile.TemporaryDirectory(prefix='ram-eligibility-') as folder:
            class Snapshot:
                def __init__(self): self.root=Path(folder)
                def read(self,name):
                    safe_path(name)
                    value=body if name==path else reader(name)
                    if value is not None:
                        target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(value)
                    return value
            snapshot=Snapshot()
            try:
                store=ram.Store(snapshot)
                record=store.load(path,snapshot.read)
                store.eligible(path,record,snapshot.read)
            except (ts.ReadError,ValueError,OSError) as exc:
                raise DeliveryError('Native RAM is ineligible: '+path+': '+str(exc)) from exc

    def _key(self, payload):
        return fingerprint([payload['config']['project_id'], payload['logical_id']])

    def submit_code(self, logical_id, changes, *, session, work_id):
        payload = self._request(logical_id, changes, kind='code', session=session, work_id=work_id)
        return self._insert_job(self._key(payload), payload)

    def _insert_job(self, key, payload, *, state="queued"):
        with self.db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (key,)).fetchone()
            if row and row[0] != encoded(payload):
                raise DeliveryError('Logical delivery identity is bound to different immutable content.')
            if not row:
                db.execute('INSERT INTO jobs(id,payload,state,authorized_revision) VALUES(?,?,?,?)', (key, encoded(payload), state,payload['config']['revision']))
        return key

    def drain_verified(self):
        with self.db() as db:
            rows = list(db.execute("SELECT id,payload FROM intents WHERE state='native-verified'"))
            for row in rows:
                old = db.execute('SELECT payload FROM jobs WHERE id=?', (row['id'],)).fetchone()
                if old and old[0] != row['payload']:
                    raise DeliveryError('Verified handoff conflicts with delivery journal.')
                db.execute('INSERT OR IGNORE INTO jobs(id,payload,state,authorized_revision) VALUES(?,?,?,?)', (row['id'], row['payload'], 'queued',json.loads(row['payload'])['config']['revision']))
                db.execute("UPDATE intents SET state='native-drained' WHERE id=?", (row['id'],))
        return [r['id'] for r in rows]

    def status(self, key=None):
        with self.db() as db:
            rows = list(db.execute('SELECT * FROM jobs' + (' WHERE id=?' if key else ''), (key,) if key else ()))
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def _save(self, key, **values):
        if set(values) - {'state', 'plan', 'commit_oid', 'error', 'checks'}:
            raise DeliveryError('Invalid journal update.')
        with self.db() as db:
            db.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), key))

    def resume(self, key):
        with local_lock(self.home / 'publisher', timeout=10):
            row = self.status(key)[0]
            if row['state'] == 'waiting-native':
                raise DeliveryError('Run the original closeout; its required native phases are not verified.')
            payload = row['payload']; cfg = self.config()
            if self.authority(cfg) != self.authority(payload['config']):
                raise DeliveryError('Changed authority/destination requires a new reviewed request; cannot redirect.')
            if row['state'] in {'checking', 'staging', 'committing', 'pushing', 'commit-uncertain', 'push-uncertain'}:
                raise DeliveryError('Reconcile the executing or uncertain phase with run before resuming.')
            with self.db() as db:
                db.execute('UPDATE jobs SET authorized_revision=?,state=?,error=NULL WHERE id=?', (cfg['revision'], 'committed' if row['commit_oid'] else 'queued', key))
            return self.status(key)[0]

    def _binding(self, payload, key):
        cfg = self.config()
        if self.authority(cfg) != self.authority(payload['config']):
            raise DeliveryError('Destination or authority changed; pending request was not redirected.')
        if cfg['mode'] == 'manual' or cfg['revision'] != self.status(key)[0]['authorized_revision']:
            raise DeliveryError('Delivery paused; inspect and explicitly resume under current configuration.')
        self._topology(self.root)
        if cfg['profile'] != PRIVATE_CONFIG:
            self._routing(self.root, cfg['remote_url'])
        return cfg

    @staticmethod
    def _fault(fault, stage):
        if fault:
            fault(stage)

    def _worktree(self, payload):
        if payload['kind'] == 'code':
            if text(self.root, 'symbolic-ref', 'HEAD') != payload['config']['branch']:
                raise DeliveryError('Worktree branch binding changed.')
            return self.root
        cfg = payload['config']
        identity = fingerprint([cfg['project_id'], cfg['participant']])[:24]
        branch = 'refs/heads/eutonos-records/' + identity
        target = self.home / 'records' / identity
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            if git(self.root, 'show-ref', '--verify', '--quiet', branch, check=False).returncode == 0:
                raise DeliveryError('Records branch already exists without its owned worktree; inspect recovery.')
            git(self.root, 'worktree', 'add', '-b', branch.removeprefix('refs/heads/'), str(target), cfg['canonical_base'])
        if Path(text(target, 'rev-parse', '--git-common-dir')).resolve() != self.common or text(target, 'symbolic-ref', 'HEAD') != branch:
            raise DeliveryError('Records worktree binding is incompatible.')
        return target

    def _clean_index(self, root):
        for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_LOG', 'index.lock'):
            path = Path(text(root, 'rev-parse', '--git-path', name))
            if not path.is_absolute(): path = root / path
            if path.exists():
                raise DeliveryError('Active Git operation: ' + name)
        if git(root, 'diff', '--cached', '--quiet', check=False).returncode:
            raise DeliveryError('Pre-existing index entries remain untouched.')

    def _delta(self, base, before, after):
        if base == before:
            return after
        if before is None or after is None or base is None or any(b'\0' in b for b in (base, before, after)):
            raise DeliveryError('Overlapping new/deleted/binary path needs reconciliation.')
        with tempfile.TemporaryDirectory(prefix='delta-', dir=self.home) as folder:
            names = [Path(folder)/n for n in ('current', 'before', 'after')]
            for path, body in zip(names, (base, before, after)): path.write_bytes(body)
            result = git(self.root, 'merge-file', '-p', *(str(n) for n in names), check=False)
            if result.returncode:
                raise DeliveryError('Attributable record delta has incompatible overlapping context.')
            return result.stdout

    def _outputs(self, root, parent, payload):
        outputs = {}
        for path, c in payload['changes'].items():
            before, after = thaw(c['before']), thaw(c['after'])
            modes = text(root, 'ls-tree', parent, '--', path)
            if modes and not modes.startswith('100644 '):
                raise DeliveryError('Only ordinary non-executable files are supported initially: ' + path)
            target = root / path
            if target.exists() and not target.is_file(): raise DeliveryError('Selected directories/nonregular paths are unsupported: '+path)
            if target.is_symlink() or any(p.is_symlink() for p in target.parents if p.is_relative_to(root)):
                raise DeliveryError('Symlink/reparse path is unsupported: ' + path)
            if payload['kind'] == 'code':
                actual = target.read_bytes() if target.is_file() else None
                if actual != after or blob(root, parent, path) != before:
                    raise DeliveryError('Selected code scope is stale or contains unrelated edits: ' + path)
                outputs[path] = after
            else:
                base = blob(root, parent, path)
                # Native backups retain working bytes; an LF Git baseline can
                # therefore differ solely by Git conversion. Only exact converted
                # equality admits the raw attributable after snapshot directly.
                attribute_edits = {p:thaw(c['after']) for p,c in payload['changes'].items()
                                   if Path(p).name == '.gitattributes'}
                if base != before and before is not None and self._normalized_outputs(
                        root, parent, {path:before}, attribute_edits=attribute_edits,
                        check_eligibility=False)[path] == base:
                    outputs[path] = after
                else:
                    outputs[path] = self._delta(base, before, after)
        reader=lambda p: outputs[p] if p in outputs else blob(root,parent,p)
        for path,body in outputs.items(): self._eligible(path,body,reader)
        return outputs

    def _normalized_outputs(self, root, parent, outputs, *, attribute_edits=None, check_eligibility=True):
        # Attribute policy belongs to the candidate tree: parent attributes plus
        # selected edits, never unrelated dirty .gitattributes in the caller.
        # The temporary index also prevents deleted attributes falling back to
        # the parent's index. Repository/global/info attributes and Git config
        # remain Git's normal local conversion policy at this one freeze point.
        with tempfile.TemporaryDirectory(prefix='candidate-conversion-', dir=self.home) as folder:
            folder = Path(folder)
            worktree = folder / 'work'; worktree.mkdir()
            env = {'GIT_DIR': text(root, 'rev-parse', '--absolute-git-dir'),
                   'GIT_WORK_TREE': str(worktree), 'GIT_INDEX_FILE': str(folder / 'index')}
            git(worktree, 'read-tree', parent, env=env)
            # Required clean filters may invoke repository-relative helpers.
            # Give them the same pinned full tree used for checks, overlaid with
            # selected raw inputs; never consult unrelated dirty dependencies.
            raw_inputs = {**outputs, **(attribute_edits or {})}
            self._materialize(root, parent, raw_inputs, worktree)
            attributes = {}
            for entry in git(root, 'ls-tree', '-rz', parent).stdout.split(b'\0'):
                if not entry: continue
                header, rawpath = entry.split(b'\t', 1)
                path = safe_path(rawpath.decode())
                if Path(path).name == '.gitattributes':
                    if not header.startswith(b'100644 blob '):
                        raise DeliveryError('Nonregular candidate attributes are unsupported: ' + path)
                    attributes[path] = blob(root, parent, path)
            selected_attributes = outputs if attribute_edits is None else attribute_edits
            attributes.update({p: b for p, b in selected_attributes.items() if Path(p).name == '.gitattributes'})
            for path, body in attributes.items():
                if body is None:
                    git(worktree, 'update-index', '--force-remove', '--', path, env=env)
                else:
                    selected = worktree / path
                    selected.parent.mkdir(parents=True, exist_ok=True); selected.write_bytes(body)
                    oid = git(root, 'hash-object', '-w', '--stdin', input=body).stdout.decode().strip()
                    git(worktree, 'update-index', '--add', '--cacheinfo', '100644', oid, path, env=env)
            normalized = {}
            for path, body in outputs.items():
                if body is None: normalized[path] = None
                else:
                    oid = git(worktree, 'hash-object', '-w', '--path=' + path, '--stdin', input=body, env=env).stdout.decode().strip()
                    normalized[path] = git(root, 'cat-file', 'blob', oid).stdout
            if sum(len(b or b'') for b in normalized.values()) > 2_000_000:
                raise DeliveryError('Git-normalized selected scope exceeds 2,000,000 bytes.')
            reader = lambda p: normalized[p] if p in normalized else blob(root, parent, p)
            if check_eligibility:
                for path, body in normalized.items(): self._eligible(path, body, reader)
            return normalized

    def _stage(self,root,plan,payload):
        if text(root,'rev-parse','HEAD')!=plan['parent']:
            raise DeliveryError('Staging source parent changed.')
        outputs={p:thaw(b) for p,b in plan['outputs'].items()}
        staged=git(root,'diff','--cached','--name-only','-z').stdout.decode().split('\0')[:-1]
        if any(p not in outputs for p in staged):
            raise DeliveryError('Staged tree has unrelated entries; preserve index for review.')
        for path in staged:
            actual=git(root,'show',':'+path,check=False)
            content=actual.stdout if not actual.returncode else None
            if content not in (outputs[path],blob(root,plan['parent'],path)):
                raise DeliveryError('Interrupted staged bytes differ from owned intent.')
        if payload['kind']=='records':
            for path,frozen in plan.get('working_outputs', plan['outputs']).items():
                body=thaw(frozen)
                selected=root/path
                actual=selected.read_bytes() if selected.is_file() else None
                base=blob(root,plan['parent'],path)
                checkout_matches=(actual is not None and base is not None and git(root,'hash-object','--path='+path,'--stdin',input=actual).stdout==git(root,'hash-object','--stdin',input=base).stdout)
                if actual not in (base,body) and not checkout_matches:
                    raise DeliveryError('Owned records files changed during staging.')
                if body is None:
                    if selected.exists():selected.unlink()
                else:
                    selected.parent.mkdir(parents=True,exist_ok=True);selected.write_bytes(body)
        else:self._outputs(root,plan['parent'],payload)
        # Stage the already checked immutable blobs without running conversion
        # again. Raw code snapshots are still validated above and before commit.
        entries = []
        for path, body in outputs.items():
            if body is None: git(root, 'update-index', '--force-remove', '--', path)
            else:
                oid = git(root, 'hash-object', '-w', '--stdin', input=body).stdout.decode().strip()
                entries.append(('100644 ' + oid + '\t' + path).encode() + b'\0')
        if entries: git(root, 'update-index', '-z', '--index-info', input=b''.join(entries))
        if self._index_digest(root)!=plan['snapshot']:
            raise DeliveryError('Staged tree differs from full checked candidate; publication withheld.')
        if not git(root,'diff','--cached','--quiet',check=False).returncode:
            raise DeliveryError('Candidate has no new commit effect; inspect existing record adoption.')
        tree=text(root,'write-tree')
        if self._snapshot_digest(root,tree,{})!=plan['snapshot']:
            raise DeliveryError('Immutable staged tree differs from checked candidate; preserve index.')
        return tree

    def _index_digest(self,root):
        files={}
        for line in git(root,'ls-files','--stage','-z').stdout.split(b'\0'):
            if not line: continue
            header,path=line.split(b'\t',1)
            mode,oid,stage=header.decode().split()
            if stage!='0': raise DeliveryError('Conflicted index is unsupported.')
            files[path.decode()]=mode+' blob '+oid
        return fingerprint(files)

    def _snapshot_digest(self, root, parent, outputs):
        files = {}
        for line in git(root, 'ls-tree', '-rz', parent).stdout.split(b'\0'):
            if line:
                header, path = line.split(b'\t', 1)
                files[path.decode()] = header.decode()
        for path, body in outputs.items():
            if body is None: files.pop(path, None)
            else: files[path] = '100644 blob ' + git(root, 'hash-object', '--stdin', input=body).stdout.decode().strip()
        return fingerprint(files)

    def _materialize(self, root, parent, outputs, folder):
        # Materialize exact Git blobs. archive attributes can omit or
        # substitute files, so archive output cannot establish this proof.
        rows=[]
        for entry in git(root,'ls-tree','-rz',parent).stdout.split(b'\0'):
            if not entry: continue
            header,rawpath=entry.split(b'\t',1);mode,kind,oid=header.decode().split()
            path=safe_path(rawpath.decode())
            if kind!='blob' or mode not in {'100644','100755'}:
                raise DeliveryError('Snapshot links/submodules or path modes are unsupported.')
            rows.append((path,mode,oid))
        if len(rows)>4096: raise DeliveryError('Candidate tree exceeds 4096 files.')
        unique=list(dict.fromkeys(oid for _,_,oid in rows))
        data=git(root,'cat-file','--batch',input=('\n'.join(unique)+'\n').encode()).stdout
        if len(data)>64_000_000: raise DeliveryError('Candidate snapshot exceeds 64,000,000 bytes.')
        objects={};offset=0;blob_sizes={}
        for oid in unique:
            newline=data.index(b'\n',offset);header=data[offset:newline].decode().split()
            if len(header)!=3 or header[0]!=oid or header[1]!='blob': raise DeliveryError('Git candidate object changed or is unavailable.')
            size=int(header[2]);blob_sizes[oid]=size;offset=newline+1;objects[oid]=data[offset:offset+size];offset+=size+1
        # Deduplicated Git objects can appear at many paths. Bound the actual
        # materialized tree, not only the unique blob transfer.
        if sum(blob_sizes[oid] for _, _, oid in rows)>64_000_000:
            raise DeliveryError('Candidate snapshot exceeds 64,000,000 materialized bytes.')
        final_sizes = {path: blob_sizes[oid] for path, _, oid in rows}
        for path, body in outputs.items():
            if body is None: final_sizes.pop(path, None)
            else: final_sizes[path] = len(body)
        if sum(final_sizes.values())>64_000_000:
            raise DeliveryError('Candidate snapshot exceeds 64,000,000 materialized bytes after selected outputs.')
        for path,mode,oid in rows:
            selected=folder/path;selected.parent.mkdir(parents=True,exist_ok=True);selected.write_bytes(objects[oid])
            if os.name!='nt':os.chmod(selected,0o755 if mode=='100755' else 0o644)
        for path, body in outputs.items():
            selected = folder / path
            if body is None:
                if selected.exists(): selected.unlink()
            else:
                selected.parent.mkdir(parents=True, exist_ok=True); selected.write_bytes(body)

    def _checks(self, root, parent, outputs, cfg):
        with tempfile.TemporaryDirectory(prefix='eutonos-delivery-check-') as folder:
            folder = Path(folder)
            self._materialize(root, parent, outputs, folder)
            preserved={str(p.relative_to(folder)):(p.read_bytes(),p.stat().st_mode) for p in folder.rglob('*') if p.is_file()}
            environment={k:v for k,v in os.environ.items() if not k.upper().startswith('GIT_')}
            environment['GIT_CEILING_DIRECTORIES']=str(folder)
            results = []
            for argv in cfg['checks']:
                result = subprocess.run(argv, cwd=folder, capture_output=True, timeout=120,env=environment, **no_window())
                results.append({'argv': argv, 'exit': result.returncode,
                                'stdout': result.stdout.decode(errors='replace')[-1000:],
                                'stderr': result.stderr.decode(errors='replace')[-1000:]})
                if result.returncode:
                    raise DeliveryError('Candidate check failed: ' + encoded(results[-1]))
                current={str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and '.eutonos-local' not in p.parts and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts}
                if current!=set(preserved) or any(not (folder/p).is_file() or (folder/p).is_symlink() or ((folder/p).read_bytes(),(folder/p).stat().st_mode)!=b for p,b in preserved.items()):
                    raise DeliveryError('Check adapter changed candidate source bytes, modes or types.')
            for path, body in outputs.items():
                selected = folder / path
                if (selected.read_bytes() if selected.is_file() else None) != body:
                    raise DeliveryError('Check adapter changed the candidate snapshot.')
            return {'snapshot': self._snapshot_digest(root, parent, outputs), 'configuration': fingerprint(cfg['checks']), 'results': results}

    def _history(self, root, head, payload, *, current_oid=None, publish=False):
        original_cfg=payload['config']
        cfg = self._target(original_cfg,payload)
        self._topology(root)
        # Local saves have no network dependency. Publication always observes
        # the current destination and rechecks all actually outgoing history.
        remote = self._remote_oid(cfg) if publish else None
        if not self._ancestor(root,cfg['canonical_base'],head):
            raise DeliveryError('Candidate is not descended from pinned canonical baseline.')
        if not publish: return None
        floor = remote or cfg['canonical_base']
        if not self._ancestor(root, floor, head):
            raise DeliveryError('Destination advanced or baseline is incompatible; non-fast-forward needs attention.')
        commits = text(root, 'rev-list', '--reverse', floor + '..' + head).splitlines()
        for commit in commits:
            parents = text(root, 'rev-list', '--parents', '-n', '1', commit).split()[1:]
            if len(parents) != 1:
                raise DeliveryError('Merge/root outgoing commits require reviewed integration, not automatic delivery.')
            paths = git(root, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', commit).stdout.split(b'\0')
            for raw in paths:
                if not raw: continue
                path = raw.decode()
                if not self._allowed(path, cfg):
                    raise DeliveryError('Unauthorized outgoing ancestor path: ' + path)
                self._eligible(path,blob(root,commit,path),lambda p:blob(root,commit,p))
            # Prior outgoing commits must be separately bound completed engine jobs.
            if commit != current_oid:
                with self.db() as db:
                    known = db.execute('SELECT payload FROM jobs WHERE commit_oid=?', (commit,)).fetchone()
                if not known or self.authority(json.loads(known[0])['config']) != self.authority(original_cfg):
                    raise DeliveryError('Unbound outgoing ancestor requires explicit review: ' + commit)
        return remote

    @staticmethod
    def _target(cfg,payload):
        return {**cfg,'remote_ref':cfg['records_ref'] if payload['kind']=='records' else cfg['remote_ref']}

    def _remote_oid(self, cfg):
        self._routing(self.root, cfg['remote_url'])
        result = git(self.root, 'ls-remote', '--refs', cfg['remote_url'], cfg['remote_ref'])
        rows = result.stdout.decode().splitlines()
        if len(rows) > 1:
            raise DeliveryError('Remote ref is ambiguous.')
        return rows[0].split()[0] if rows else None

    @staticmethod
    def _ancestor(root, ancestor, descendant):
        return git(root, 'merge-base', '--is-ancestor', ancestor, descendant, check=False).returncode == 0

    def _published(self, root, oid, cfg):
        self._topology(root)
        remote = self._remote_oid(cfg)
        if remote == oid: return remote
        if remote:
            # Fetch the observed destination only into objects/FETCH_HEAD; no merge.
            self._routing(root, cfg['remote_url'])
            git(root, 'fetch', '--no-tags', cfg['remote_url'], cfg['remote_ref'])
            if self._ancestor(root, oid, remote): return remote
        return None

    def run(self, key, *, fault=None):
        with local_lock(self.home / 'publisher', timeout=10):
            rows = self.status(key)
            if not rows: raise DeliveryError('Unknown delivery request.')
            row = rows[0]; payload = row['payload']; cfg = payload['config']
            if payload.get('profile') not in {REQUEST, CLOSEOUT_REQUEST}:
                raise DeliveryError('Unsupported request schema; use manual Git or upgrade.')
            if row['state'] == 'waiting-native':
                return {**row, 'error': 'Run the original closeout; its required native phases are not verified.'}
            if row['state'] == 'published': return row
            if cfg['profile'] == PRIVATE_CONFIG:
                from .personal_history import _run_engine
                return _run_engine(self, key, fault=fault)
            bound_root=Path(payload['config']['root']).resolve()
            if bound_root!=self.root:
                owner=Engine(bound_root)
                if owner.common!=self.common: raise DeliveryError('Request source is outside this shared Git journal.')
                return owner.run(key,fault=fault)
            recovery_read = False
            try:
                if row['state'] not in {'committing','commit-uncertain','pushing','push-uncertain'}:
                    self._binding(payload,key)
                recovery_read = True
                root = self._worktree(payload)
                recovery_read = False
                self._topology(root)
                plan = json.loads(row['plan']) if row['plan'] else None
                oid = row['commit_oid']
                # Recover side effects before the current pause/configuration gate.
                if row['state'] in {'pushing', 'push-uncertain'} and oid:
                    recovery_read = True
                    observed = self._published(root, oid, self._target(cfg,payload))
                    recovery_read = False
                    if observed:
                        self._save(key, state='published', error=None)
                        return self.status(key)[0]
                    self._save(key, state='committed', error=None)
                if row['state'] in {'committing', 'commit-uncertain'} and plan:
                    recovery_read = True
                    head = text(root, 'rev-parse', 'HEAD')
                    if head != plan['parent']:
                        if text(root, 'rev-parse', head+'^') != plan['parent'] or text(root, 'rev-parse', head+'^{tree}') != plan['tree'] or 'Eutonos-Delivery: '+key not in text(root, 'show', '-s', '--format=%B', head):
                            recovery_read = False
                            raise DeliveryError('Interrupted commit has incompatible result; preserve it for review.')
                        oid = head; self._save(key, state='committed', commit_oid=oid, error=None)
                    else:
                        # Our exact staged tree can be resumed; never absorb new entries.
                        if text(root, 'write-tree') != plan['tree']:
                            recovery_read = False
                            raise DeliveryError('Interrupted index changed; inspect without resetting.')
                    recovery_read = False
                cfg = self._binding(payload,key)
                if oid:
                    self._history(root, oid, payload,current_oid=oid, publish=cfg['mode']=='commit+push')
                else:
                    parent = text(root, 'rev-parse', 'HEAD')
                    if plan is None:
                        self._clean_index(root)
                        if payload['kind'] == 'records' and git(root, 'status', '--porcelain').stdout:
                            raise DeliveryError('Owned records worktree is dirty; inspect recovery.')
                        existing = payload['kind'] == 'code' and parent != payload['source_parent']
                        candidate_parent = payload['source_parent'] if existing else parent
                        if existing:
                            # Adopt only the original request's exact single commit.
                            if text(root, 'rev-parse', parent+'^') != candidate_parent:
                                raise DeliveryError('Source branch advanced before delivery.')
                            working = {p: thaw(c['after']) for p,c in payload['changes'].items()}
                        else:
                            working = self._outputs(root, parent, payload)
                        outputs = self._normalized_outputs(root, candidate_parent, working)
                        if existing:
                            actual_paths = set(git(root, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', parent).stdout.decode().split('\0')[:-1])
                            if actual_paths != set(outputs) or any(blob(root,parent,p)!=b for p,b in outputs.items()):
                                raise DeliveryError('Existing commit differs from exact selected scope.')
                        author=re.fullmatch(r'(.*) <([^<>]+)> \d+ [+-]\d{4}',text(root,'var','GIT_AUTHOR_IDENT'))
                        committer=re.fullmatch(r'(.*) <([^<>]+)> \d+ [+-]\d{4}',text(root,'var','GIT_COMMITTER_IDENT'))
                        if not author or not committer: raise DeliveryError('Git author/committer identity is unavailable.')
                        plan={'parent':candidate_parent,'tree':None,
                              'snapshot':self._snapshot_digest(root,candidate_parent,outputs),
                              'outputs':{p:freeze(b) for p,b in outputs.items()},
                              'working_outputs':{p:freeze(b) for p,b in working.items()},
                              'existing_commit':parent if existing else None,
                              'message':'EUTONOS delivery '+payload['work_id']+'\n\nEutonos-Delivery: '+key,
                              'date':str(int(time.time()))+' +0000',
                              'author':list(author.groups()),'committer':list(committer.groups())}
                        # Freeze conversion before checks, including failed/interrupted
                        # checks. Retries reuse these bytes even if local policy changes.
                        self._save(key,state='checking',plan=encoded(plan),checks=None)
                    if not self.status(key)[0]['checks']:
                        self._history(root, plan.get('existing_commit') or plan['parent'], payload,
                                      current_oid=plan.get('existing_commit'), publish=cfg['mode']=='commit+push')
                        outputs = {p:thaw(b) for p,b in plan['outputs'].items()}
                        evidence = self._checks(root, plan['parent'], outputs, cfg)
                        self._fault(fault, 'after_checks')
                        if plan.get('existing_commit'):
                            if text(root,'rev-parse','HEAD') != plan['existing_commit'] or self._snapshot_digest(root,plan['existing_commit'],{}) != evidence['snapshot']:
                                raise DeliveryError('Existing commit tree differs from the checked snapshot.')
                        elif text(root,'rev-parse','HEAD') != plan['parent'] or (payload['kind']=='code' and self._outputs(root,plan['parent'],payload) != {p:thaw(b) for p,b in plan['working_outputs'].items()}):
                            raise DeliveryError('Checked candidate changed before staging.')
                        if evidence['snapshot'] != plan['snapshot']:
                            raise DeliveryError('Checked snapshot differs from frozen candidate.')
                        self._clean_index(root)
                        self._save(key,state='staging',checks=encoded(evidence))
                        self._fault(fault,'before_staging')
                    if plan.get('existing_commit'):
                        oid = plan['existing_commit']
                        if text(root,'rev-parse','HEAD') != oid or self._snapshot_digest(root,oid,{}) != plan['snapshot']:
                            raise DeliveryError('Existing commit changed before adoption.')
                        self._history(root,oid,payload,current_oid=oid,publish=cfg['mode']=='commit+push')
                        self._save(key,state='committed',commit_oid=oid)
                    if not oid:
                        cfg = self._binding(payload,key)
                        if plan['tree'] is None:
                            plan['tree']=self._stage(root,plan,payload)
                            self._save(key,state='committing',plan=encoded(plan))
                        if payload['kind']=='code': self._outputs(root,plan['parent'],payload)
                        if text(root,'rev-parse','HEAD')!=plan['parent'] or text(root,'write-tree')!=plan['tree']:
                            raise DeliveryError('Commit parent/index no longer matches frozen intent.')
                        git(root,'commit','-m',plan['message'],env={'GIT_AUTHOR_DATE':plan['date'],'GIT_COMMITTER_DATE':plan['date'],'GIT_AUTHOR_NAME':plan['author'][0],'GIT_AUTHOR_EMAIL':plan['author'][1],'GIT_COMMITTER_NAME':plan['committer'][0],'GIT_COMMITTER_EMAIL':plan['committer'][1]})
                        self._fault(fault,'after_commit')
                        oid=text(root,'rev-parse','HEAD')
                        if text(root,'rev-parse','HEAD^')!=plan['parent'] or text(root,'rev-parse','HEAD^{tree}')!=plan['tree']:
                            raise DeliveryError('Commit hook changed candidate; publication withheld.')
                        self._save(key,state='committed',commit_oid=oid,error=None)
                self._history(root,oid,payload,current_oid=oid)
                cfg = self._binding(payload,key)
                if cfg['mode']=='commit-only': return self.status(key)[0]
                self._history(root,oid,payload,current_oid=oid,publish=True)
                cfg = self._binding(payload,key)
                if cfg['mode']=='commit-only': return self.status(key)[0]
                self._routing(root, cfg['remote_url'])
                self._save(key,state='pushing')
                self._topology(root)
                git(root,'push',cfg['remote_url'],oid+':'+self._target(cfg,payload)['remote_ref'])
                self._fault(fault,'after_push')
                observed=self._published(root,oid,self._target(cfg,payload))
                if not observed: raise DeliveryError('Push acknowledgment lacks matching remote readback.')
                self._save(key,state='published',error=None)
            except (DeliveryError, OSError, subprocess.TimeoutExpired) as exc:
                now=self.status(key)[0]
                # A repeated failed observation preserves uncertainty; verified
                # topology or result contradictions require owner review.
                state=('push-uncertain' if now['state']=='pushing' or now['state']=='push-uncertain' and recovery_read
                       else 'commit-uncertain' if now['state']=='committing' or now['state']=='commit-uncertain' and recovery_read
                       else 'paused' if 'paused' in str(exc) else 'blocked')
                self._save(key,state=state,error=str(exc))
            return self.status(key)[0]
