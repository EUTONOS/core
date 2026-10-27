"""Scoped delivery adapters for supported native board, RAM and closeout saves.

Bind before mutation, persist typed proof under native reservations and recover
the original identity without replay. Native and Git results remain independent.
The existing managed writer owns mutations; no caller supplies a success flag.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import sqlite3
from pathlib import Path

from . import managed_write, tokenslang as ts
from .effects import decode, digest
from .git_delivery import DeliveryError, encoded


def prepare(engine, logical_id, *, host, actor, attempt, effect_id, effect,
            session, work_id):
    coordinator, authority = managed_write.connect(engine.root, host, actor)
    if authority.root.root.resolve() != engine.root or authority.actor != actor:
        raise DeliveryError('Native authority differs from delivery source binding.')
    changes = {c['path']: {'before': decode(c['backup']), 'after': decode(c['after'])}
               for c in effect['changes']}
    payload = engine._request(logical_id, changes, kind='personal' if engine.config()['profile'] == 'eutonos.personal-history.config.v1' else 'records',session=session,work_id=work_id,check_eligibility=False)
    key = engine._key(payload)
    native = {'coordinator': str(coordinator.path), 'attempt': attempt, 'actor': actor,
              'root_identity': list(authority.root.identity), 'effect_id': effect_id,
              'fingerprint': digest(ts.encoded(effect)),
              'session_basis': 'native-operation' if session == attempt else 'supplied-session'}
    if list(effect['root_identity']) != native['root_identity']:
        raise DeliveryError('Native plan source root differs.')
    with engine.db() as db:
        bound=db.execute('SELECT id FROM intents WHERE native=?',(encoded(native),)).fetchone()
        if bound and bound[0]!=key: raise DeliveryError('Native effect is already bound to another delivery identity.')
        old=db.execute('SELECT payload,native FROM intents WHERE id=?',(key,)).fetchone()
        if old and (old['payload']!=encoded(payload) or old['native']!=encoded(native)):
            raise DeliveryError('Native handoff identity is already bound to different content.')
        db.execute('INSERT OR IGNORE INTO intents(id,payload,native,state) VALUES(?,?,?,?)',
                   (key,encoded(payload),encoded(native),'awaiting-native-verification'))
    return key


def _original(engine, row, host):
    native=json.loads(row['native']); payload=json.loads(row['payload'])
    coordinator,authority=managed_write.connect(engine.root,host,native['actor'])
    if str(coordinator.path)!=native['coordinator'] or list(authority.root.identity)!=native['root_identity']:
        raise DeliveryError('Native recovery source differs from the durable intent.')
    with coordinator._db() as db:
        effect=db.execute('SELECT * FROM effects WHERE id=?',(native['effect_id'],)).fetchone()
    if not effect or effect['attempt']!=native['attempt'] or effect['state']!='completed' or effect['fingerprint']!=native['fingerprint']:
        raise DeliveryError('Native effect proof is missing or differs.')
    plan=ts.loads(effect['plan'])
    frozen={c['path']:{'before':c['backup'],'after':c['after']} for c in plan['changes']}
    if digest(ts.encoded(plan))!=native['fingerprint'] or frozen!=native.get('changes',payload['changes']):
        raise DeliveryError('Verified native effect does not match immutable publication bytes.')
    state=coordinator.inspect(native['attempt'])
    if state['actor']!=native['actor'] or state['root']!=coordinator._root(authority):
        raise DeliveryError('Native attempt proof belongs to another actor or root.')
    return native,coordinator,authority,state


def _typed_marker(engine,key,host):
    def record(proof):
        with engine.db() as db:
            row=db.execute('SELECT * FROM intents WHERE id=?',(key,)).fetchone()
        native,coordinator,authority,state=_original(engine,row,host)
        payload=json.loads(row['payload'])
        scope=native.get('changes',payload['changes'])
        if proof['attempt']!=native['attempt'] or proof['actor']!=native['actor'] or proof['paths']!=sorted(scope) or proof['generation']!=state['generation']:
            raise DeliveryError('Typed proof differs from original native operation.')
        if any(authority.root.read(path)!=decode(change['after']) for path,change in scope.items()):
            raise DeliveryError('Typed proof bytes differ from original native operation.')
        # Active apply/recovery still holds its fence. Paused recovery can only
        # prove current bytes; successful new writes always persist before release.
        if state['state']=='active':
            if set(r['path'] for r in state['reservations'])!=set(scope):
                raise DeliveryError('Typed proof lacks original native reservations.')
            cleanup_generation=state['generation']+1
        elif state['state']=='paused' and not state['reservations'] and not state['unfinished_effects']:
            cleanup_generation=state['generation']
        else:
            raise DeliveryError('Typed proof has incomplete native protection.')
        with engine.db() as db:
            db.execute("UPDATE intents SET state='native-typed-verified',receipt=? WHERE id=? AND state IN ('awaiting-native-verification','native-typed-verified')",
                       (encoded({**proof,'cleanup_generation':cleanup_generation}),key))
    return record


def confirm(engine,key,*,host,verify,checkpoint):
    """Recover the original typed proof and promote only after native cleanup.

    The frozen proof survives later owner edits. Without a proof, existing typed
    recovery verifies the exact effect; no native mutation is ever replayed here.
    """
    if not callable(verify):
        raise DeliveryError('A real typed driver verifier is required.')
    with engine.db() as db:
        row=db.execute('SELECT * FROM intents WHERE id=?',(key,)).fetchone()
    if not row: raise DeliveryError('Unknown native delivery intent.')
    if row['state'] in {'native-verified','native-drained'}: return json.loads(row['receipt'])
    native=json.loads(row['native'])
    proof=json.loads(row['receipt']) if row['state']=='native-typed-verified' else None
    closed=False
    if proof:
        native,coordinator,authority,state=_original(engine,row,host)
        closed=(state['state']=='paused' and not state['reservations'] and not state['unfinished_effects']
                and state['generation']==proof['cleanup_generation'])
    if not closed:
        receipt=managed_write.recover(engine.root,host,native['actor'],native['attempt'],checkpoint,
                                      verify=verify,effect_id=native['effect_id'],fingerprint=native['fingerprint'],
                                      on_verified=_typed_marker(engine,key,host))
        if not receipt.get('complete') or receipt.get('readback')!='verified':
            raise DeliveryError('Original native operation is not verified; no publication.')
        with engine.db() as db:
            row=db.execute('SELECT * FROM intents WHERE id=?',(key,)).fetchone()
        if row['state'] in {'native-verified','native-drained'}: return json.loads(row['receipt'])
        proof=json.loads(row['receipt'])
        native,coordinator,authority,state=_original(engine,row,host)
        if state['state']!='paused' or state['reservations'] or state['unfinished_effects'] or state['generation']!=proof['cleanup_generation']:
            raise DeliveryError('Original native cleanup is not verified; no publication.')
        retired=receipt.get('retired_grants',[])
    else:
        retired=managed_write.abandon_lease(engine.root,host,native['actor'],native['attempt']).get('retired_grants',[])
    receipt={k:v for k,v in proof.items() if k!='cleanup_generation'}
    receipt.update(outcome='complete',complete=True,generation=state['generation'],retired_grants=retired)
    with engine.db() as db:
        db.execute("UPDATE intents SET state='native-verified',receipt=? WHERE id=? AND state='native-typed-verified'",(encoded(receipt),key))
    return receipt


def managed_apply(engine,logical_id,*,host,actor,attempt,effect_id,effect,session,work_id,
                  checkpoint,verify,fault=None,_import_derivation=None):
    """Proof adapter: preserve the installed-compatible managed writer mechanics."""
    if not callable(verify): raise DeliveryError('A typed verifier is required.')
    key=prepare(engine,logical_id,host=host,actor=actor,attempt=attempt,effect_id=effect_id,
                effect=effect,session=session,work_id=work_id)
    def typed():
        engine._fault(fault,'before_native_verification')
        verify()
    marker=_typed_marker(engine,key,host)
    def verified(proof):
        marker(proof)
        engine._fault(fault,'after_native_typed_verified')
    receipt=managed_write.apply(engine.root,host,actor,attempt,effect_id,effect,digest(ts.encoded(effect)),
                                [c['path'] for c in effect['changes']],checkpoint,verify=typed,on_verified=verified,
                                **({"_import_derivation":_import_derivation} if _import_derivation else {}))
    engine._fault(fault,'after_native_cleanup')
    try:
        confirm(engine,key,host=host,verify=verify,checkpoint=checkpoint)
        engine._fault(fault,'after_native_verified')
        engine.drain_verified()
        delivered=engine.status(key)[0]
    except (DeliveryError, OSError, sqlite3.Error, ts.ReadError) as exc:
        # Native cleanup already returned a real successful receipt. A later
        # journal/handoff failure retains that outcome and its recoverable intent.
        return {'request_id':key,'native':receipt,'delivery':None,
                'delivery_pending':True,'delivery_error':str(exc)}
    return {'request_id':key,'native':receipt,'delivery':delivered}


# Production adapters intentionally do not instantiate Engine for an unconfigured
# or manual save. Reading Settings must not create a journal or activate Git.
def configuration(root, *, owner_id=None):
    from .private_scope import is_private, require_owner
    if is_private(root):
        require_owner(root, owner_id)
        from .personal_history import _standalone
        _standalone(root)
    from .git_delivery import CONFIG, text
    root = Path(root).resolve()
    try:
        common = Path(text(root, 'rev-parse', '--git-common-dir'))
    except DeliveryError:
        # Only a directory with no Git binding is ordinary manual absence.
        probe = root
        while probe != probe.parent:
            if (probe / '.git').exists():
                raise
            probe = probe.parent
        return {'profile': CONFIG, 'mode': 'manual', 'revision': 0}
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    path = common / 'eutonos-delivery' / 'journal.sqlite'
    if not path.exists():
        return {'profile': CONFIG, 'mode': 'manual', 'revision': 0}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version != 1:
            raise DeliveryError('Unsupported delivery journal schema; upgrade or use manual Git.')
        row = db.execute('SELECT payload FROM config WHERE root=?', (str(root),)).fetchone()
    if not row:
        return {'profile': CONFIG, 'mode': 'manual', 'revision': 0}
    value = json.loads(row[0])
    if value.get('profile') == 'eutonos.personal-history.config.v1':
        from .personal_history import _validate_configuration
        _validate_configuration(root, value, stored=True)
        return value
    expected = {'profile','project_id','participant','mode','remote_url','remote_ref','branch',
                'canonical_base','allowed_paths','checks','records_ref','root','revision','activation_cursor'}
    if (not isinstance(value, dict) or set(value) != expected or value.get('profile') != CONFIG
            or value.get('mode') not in {'manual','commit-only','commit+push'}
            or type(value.get('revision')) is not int or value['revision'] < 1
            or not isinstance(value.get('root'),str) or Path(value.get('root', '')).resolve() != root
            or any(not isinstance(value.get(k), str) or not value[k] for k in
                   ('project_id','participant','remote_url','remote_ref','branch','canonical_base','records_ref'))
            or not isinstance(value.get('allowed_paths'), list) or not value['allowed_paths']
            or not all(isinstance(p,str) and p for p in value['allowed_paths'])
            or not isinstance(value.get('checks'), list) or not value['checks']
            or any(not isinstance(a, list) or not a or not all(isinstance(v,str) and v for v in a) for a in value['checks'])):
        raise DeliveryError('Invalid delivery configuration; inspect the delivery owner.')
    return value


def guidance(root):
    try:
        cfg = configuration(root)
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return {'mode': 'unavailable', 'next': 'Inspect Settings delivery configuration: ' + str(exc)}
    if cfg['mode'] == 'manual':
        return {'mode': 'manual', 'next': 'At a coherent checked milestone, create a scoped local commit under existing authority; assess integration and publication separately.'}
    return {'mode': cfg['mode'], 'next': 'Submit the exact scoped code changes to Settings delivery; inspect or recover the bound request. Native board/RAM saves submit automatically. The configured publisher owns commits and pushes; do not stage, commit or push independently.'}


_submitted_at = ContextVar('eutonos_delivery_native_submitted_at', default=None)


@contextmanager
def submitted_at(timestamp):
    """Keep the existing native queue's first submission fence local to dispatch."""
    token = _submitted_at.set(timestamp)
    try:
        yield
    finally:
        _submitted_at.reset(token)


def _check_submission(cfg):
    timestamp = _submitted_at.get()
    if (timestamp is not None and cfg['mode'] != 'manual'
            and cfg['activation_cursor'] > int(timestamp * 1_000_000_000)):
        raise DeliveryError('Queued native request predates current delivery activation/binding; no new automatic intent. Use an explicitly reviewed manual publication or a new native request.')


def _engine(root):
    from .git_delivery import Engine
    cfg = configuration(root)
    _check_submission(cfg)
    return Engine(root) if cfg['mode'] != 'manual' else None


def _bound(engine, host, actor, attempt):
    coordinator, _ = managed_write.connect(engine.root, host, actor)
    with engine.db() as db:
        rows = db.execute("SELECT id FROM intents WHERE json_extract(payload,'$.kind')='records' AND json_extract(native,'$.attempt')=? AND json_extract(native,'$.actor')=? AND json_extract(native,'$.coordinator')=?",
                          (attempt, actor, str(coordinator.path))).fetchall()
    if len(rows) > 1:
        raise DeliveryError('Native operation has multiple delivery bindings.')
    return rows[0]['id'] if rows else None


def _publish(engine, key, host, verify, checkpoint):
    confirm(engine, key, host=host, verify=verify, checkpoint=checkpoint)
    engine.drain_verified()
    return {'request_id': key, **engine.run(key)}


def apply(root, host, actor, attempt, effect_id, effect, fingerprint, paths, checkpoint,
          verify, *, session=None, work_id=None, _import_derivation=None):
    """Optional transport around a typed native save; never replace its result."""
    engine = key = None
    error = None
    code_controls = []
    try:
        code_controls = code_before(root,host,actor,attempt,effect_id,effect)
        engine = _engine(root)
        if engine is not None:
            key = prepare(engine, 'native:' + attempt, host=host, actor=actor, attempt=attempt,
                          effect_id=effect_id, effect=effect, session=session or attempt,
                          work_id=work_id or attempt)
    except ts.ReadError:
        raise
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        error = str(exc)
    def marker(proof):
        nonlocal error
        for control in code_controls:
            verified(control,host,proof)
        try:
            if key:
                _typed_marker(engine, key, host)(proof)
        except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
            error = str(exc)
    result = managed_write.apply(root, host, actor, attempt, effect_id, effect, fingerprint,
                                 paths, checkpoint, verify=verify,
                                 **({'on_verified': marker} if key or code_controls else {}),
                                 **({'_import_derivation': _import_derivation} if _import_derivation else {}))
    transport = {'state': 'manual', 'automatic': False}
    if key and error is None:
        try:
            transport = _publish(engine, key, host, verify, checkpoint)
        except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
            error = str(exc)
    if error:
        transport = {'state': 'unavailable', 'error': error, 'request_id': key,
                     'next': 'Inspect/recover delivery; the native save remains successful.'}
    transport['status_basis'] = 'Snapshot at native save/recovery; inspect delivery for current transport state.'
    return {**result, 'git_delivery': transport}


def recover(root, host, actor, attempt, checkpoint, verify, *, effect_id=None, fingerprint=None):
    """Recover only pre-mutation intent, including a proof after later edits."""
    from .git_delivery import Engine
    engine = key = None
    error = None
    try:
        cfg = configuration(root)
        # Existing paused intents remain inspectable/recoverable in manual mode.
        if cfg.get('revision'):
            engine = Engine(root)
            key = _bound(engine, host, actor, attempt)
        if key:
            with engine.db() as db:
                row = db.execute('SELECT state,receipt,native FROM intents WHERE id=?', (key,)).fetchone()
            original = json.loads(row['native'])
            if ((effect_id is not None and effect_id != original['effect_id'])
                    or (fingerprint is not None and fingerprint != original['fingerprint'])):
                ts.require(False, 'conflict', 'Native recovery identity differs from its original effect.')
            if row['state'] in {'native-typed-verified', 'native-verified', 'native-drained'}:
                receipt = confirm(engine, key, host=host, verify=verify, checkpoint=checkpoint)
                try:
                    transport = _publish(engine, key, host, verify, checkpoint)
                except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
                    transport = {'state': 'unavailable', 'request_id': key, 'error': str(exc)}
                return {**receipt, 'writes': 0, 'reason': 'recovered', 'git_delivery': transport}
    except ts.ReadError:
        raise
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        error = str(exc)
    coordinator, _ = managed_write.connect(root,host,actor)
    rows = managed_write._effect_rows(coordinator,attempt)
    try:
        code_controls = code_before(root,host,actor,attempt,rows[0]['id'],ts.loads(rows[0]['plan'])) if len(rows)==1 else []
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        code_controls = []
        error = str(exc)
    def marker(proof):
        nonlocal error
        for control in code_controls:
            verified(control,host,proof)
        try:
            if key:
                _typed_marker(engine, key, host)(proof)
        except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
            error = str(exc)
    result = managed_write.recover(root, host, actor, attempt, checkpoint, verify=verify,
                                   effect_id=effect_id, fingerprint=fingerprint, on_verified=marker if key or code_controls else None)
    if result.get('complete'):
        transport = {'state': 'not-submitted', 'reason': 'No pre-mutation automatic delivery intent.'}
        if key:
            try:
                transport = _publish(engine, key, host, verify, checkpoint)
            except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
                error = str(exc)
        if error:
            transport = {'state': 'unavailable', 'request_id': key, 'error': error}
        result = {**result, 'git_delivery': transport}
    return result


def before(root, host, actor, attempt, effect_id, effect, *, session, work_id):
    """RAM driver hook before mutation; returns only process-local control data."""
    try:
        engine = _engine(root)
        if engine is None:
            return {'engine': None, 'key': None, 'error': None}
        key = prepare(engine, 'native:' + attempt, host=host, actor=actor, attempt=attempt,
                      effect_id=effect_id, effect=effect, session=session, work_id=work_id)
        return {'engine': engine, 'key': key, 'error': None}
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        return {'engine': None, 'key': None, 'error': str(exc)}


def verified(control, host, proof):
    if control['key']:
        try:
            _typed_marker(control['engine'], control['key'], host)(proof)
        except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
            control['error'] = str(exc)


def finish(control, host, verify, checkpoint):
    if control['error']:
        return {'state': 'unavailable', 'error': control['error'], 'request_id': control['key']}
    if not control['key']:
        return {'state': 'manual', 'automatic': False}
    try:
        return _publish(control['engine'], control['key'], host, verify, checkpoint)
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        return {'state': 'unavailable', 'error': str(exc), 'request_id': control['key']}


def terminal(root, host, actor, attempt, verify, checkpoint):
    """Resume transport only for the original RAM intent; never mint on enable."""
    try:
        cfg = configuration(root)
        if not cfg.get('revision'):
            return {'state': 'manual', 'automatic': False}
        from .git_delivery import Engine
        engine = Engine(root)
        key = _bound(engine, host, actor, attempt)
        if not key:
            return {'state': 'not-submitted', 'reason': 'No pre-mutation automatic delivery intent.'}
        return _publish(engine, key, host, verify, checkpoint)
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        return {'state': 'unavailable', 'error': str(exc)}


def prepare_code(root, selection, *, session, work_id, closeout=None):
    """Freeze an explicitly selected product scope and the engine check binding."""
    from .git_delivery import Engine, blob, text
    if not isinstance(selection, dict) or set(selection) != {'logical_id', 'paths'}:
        raise DeliveryError('Code delivery selection requires logical_id and explicit paths.')
    paths = selection['paths']
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise DeliveryError('Select unique explicit code paths; observed changed_paths are not authority.')
    engine = Engine(root)
    parent = text(engine.root, 'rev-parse', 'HEAD')
    from .source import safe_path
    changes = {}
    for path in paths:
        safe_path(path)
        target = engine.root / path
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise DeliveryError('Selected code path must be an ordinary file or explicit deletion: ' + path)
        for parent_path in target.parents:
            if parent_path == engine.root:
                break
            if parent_path.is_symlink():
                raise DeliveryError('Selected code path crosses a symlink: ' + path)
        before = blob(engine.root, parent, path)
        after = target.read_bytes() if target.is_file() else None
        changes[path] = {'before': before, 'after': after}
    payload = engine._request(selection['logical_id'], changes, kind='code', session=session, work_id=work_id)
    if closeout is not None:
        from .git_delivery import CLOSEOUT_REQUEST
        binding = {'host': str(Path(closeout['host']).resolve()), 'actor': closeout['actor'], 'attempt': closeout['attempt']}
        if 'closeout' in payload and payload['closeout'] != binding:
            raise DeliveryError('Code closeout identity differs from its original binding.')
        payload = {**payload, 'profile': CLOSEOUT_REQUEST, 'closeout': binding}
    engine._insert_job(engine._key(payload), payload, state='waiting-native' if closeout else 'queued')
    return payload


def deliver_code(root, frozen, *, session=None, work_id=None, selection=None):
    """Run configured candidate checks; narrative check lists are not proof."""
    from .git_delivery import Engine, REQUEST, CLOSEOUT_REQUEST, thaw
    try:
        expected_keys = {'profile','logical_id','kind','config','actor','session','work_id','source_parent','changes'}
        if not isinstance(frozen, dict) or set(frozen) not in (expected_keys, expected_keys | {'closeout'}) or frozen.get('profile') not in {REQUEST,CLOSEOUT_REQUEST} or frozen.get('kind') != 'code':
            raise DeliveryError('Select a frozen checked-code delivery request.')
        engine = Engine(root)
        if (session is not None and frozen['session'] != session) or (work_id is not None and frozen['work_id'] != work_id):
            raise DeliveryError('Checked-code session/work differs from original closeout context.')
        if selection is not None and (frozen['logical_id'] != selection.get('logical_id')
                or set(frozen['changes']) != set(selection.get('paths', []))):
            raise DeliveryError('Checked-code scope differs from explicit closeout selection.')
        if Path(frozen['config'].get('root', '')).resolve() != engine.root:
            raise DeliveryError('Checked-code source root differs.')
        if frozen['actor'] != frozen['config'].get('participant'):
            raise DeliveryError('Checked-code actor differs from original participant binding.')
        from .git_delivery import blob, text
        text(engine.root, 'rev-parse', frozen['source_parent'] + '^{commit}')
        if any(blob(engine.root, frozen['source_parent'], path) != thaw(values['before'])
               for path, values in frozen['changes'].items()):
            raise DeliveryError('Checked-code before bytes differ from pinned source parent.')
        # Validates current authority and immutable logical identity. Retain the
        # original revision so disable/re-enable cannot silently revive a job.
        current = engine._request(frozen['logical_id'],
            {p: {k: thaw(v) for k, v in c.items()} for p, c in frozen['changes'].items()},
            kind='code', session=frozen['session'], work_id=frozen['work_id'])
        if engine.authority(current['config']) != engine.authority(frozen['config']) or any(current[k] != frozen[k] for k in ('profile','logical_id','kind','actor','session','work_id','changes')):
            raise DeliveryError('Checked-code authority changed; prepare a new reviewed request.')
        key = engine._insert_job(engine._key(frozen), frozen)
        return {'request_id': key, **engine.run(key)}
    except (DeliveryError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        return {'state': 'unavailable', 'error': str(exc), 'next': 'Inspect/recover checked code delivery; native record results remain independent.'}


def inspect(root, key=None):
    """Read existing jobs and pre-enqueue intents without creating a journal."""
    from .private_scope import require_shared
    require_shared(root)
    from .git_delivery import text
    cfg = configuration(root)
    root = Path(root).resolve()
    if not cfg.get('revision'):
        return {'configuration': cfg, 'requests': [], 'native_intents': []}
    common = Path(text(root, 'rev-parse', '--git-common-dir'))
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    with closing(sqlite3.connect((common / 'eutonos-delivery/journal.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        jobs = db.execute('SELECT * FROM jobs' + (' WHERE id=?' if key else ''), (key,) if key else ()).fetchall()
        intents = db.execute('SELECT * FROM intents' + (' WHERE id=?' if key else ''), (key,) if key else ()).fetchall()
    def selected(rows):
        result = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item['payload'])
            if Path(item['payload']['config']['root']).resolve() != root:
                continue
            if 'native' in item:
                item['native'] = json.loads(item['native'])
                item['receipt'] = json.loads(item['receipt']) if item['receipt'] else None
                item['next'] = 'Recover the original supported native operation or use delivery recover with its exact host; never replay a successful semantic save.'
            result.append(item)
        return result
    return {'configuration': cfg, 'requests': selected(jobs), 'native_intents': selected(intents)}


def recover_request(engine, key, host):
    """CLI typed recovery from original frozen native images, no success flag."""
    from .private_scope import require_shared
    require_shared(engine.root)
    from .effects import LocalRoot
    from . import boards, ram, work_lifecycle
    with engine.db() as db:
        row = db.execute('SELECT * FROM intents WHERE id=?', (key,)).fetchone()
    if not row:
        raise DeliveryError('Unknown native intent; inspect original request identity.')
    payload = json.loads(row['payload'])
    if payload.get('closeout'):
        raise DeliveryError('Run the original closeout to verify all required native phases; delivery recovery cannot release its waiting code request.')
    def verify():
        changes = [{'path': path, 'after': value['after'], 'backup': value['before']}
                   for path, value in payload['changes'].items()]
        local = LocalRoot(engine.root)
        if any(local.read(c['path']) != decode(c['after']) for c in changes):
            raise DeliveryError('Native frozen bytes are not current; retain protection and recover original operation.')
        record_paths = []
        for c in changes:
            body = decode(c['after'])
            if body is None:
                continue
            try:
                record = ram.unpack(body)
            except (ValueError, ts.ReadError, UnicodeError):
                continue
            if record.get('profile') == ram.RECORD:
                record_paths.append(c['path'])
        if record_paths:
            store = ram.Store(local)
            for path in record_paths:
                record = store.load(path, local.read)
                readback = store.read({'profile': ram.READ, 'operation': 'get', 'identity': record['identity'],
                    'revision': record['revision'], 'max_bytes': 32768, 'limit': 1})
                ts.require(readback.get('complete') is True and len(readback.get('items', [])) == 1
                           and readback['items'][0].get('historical') is False,
                           'stale', 'Original RAM typed readback is incomplete.')
        elif local.read('eutonos.boards.json') is not None:
            work_lifecycle.verify_effect(engine.root, {'changes': changes})
        else:
            raise DeliveryError('Original typed recovery driver is unsupported; use the native operation artifact.')
    return _publish(engine, key, host, verify,
                    {'completed': 'Original native save typed-verified.', 'next': 'Inspect delivery separately.', 'pending': None})


def code_before(root, host, actor, attempt, effect_id, effect):
    """Bind waiting code closeout to its actual native effect before mutation."""
    from .git_delivery import Engine
    try:
        cfg = configuration(root)
    except (DeliveryError,OSError,sqlite3.Error,ValueError):
        return []
    if not cfg.get('revision'):
        return []
    _check_submission(cfg)
    engine = Engine(root)
    with engine.db() as db:
        rows = db.execute("SELECT id,payload FROM jobs WHERE state='waiting-native' AND json_extract(payload,'$.closeout.attempt')=? AND json_extract(payload,'$.closeout.actor')=? AND json_extract(payload,'$.closeout.host')=?",
                          (attempt, actor, str(Path(host).resolve()))).fetchall()
    controls = []
    for row in rows:
        payload = json.loads(row['payload'])
        if Path(payload['config']['root']).resolve() != engine.root:
            continue
        coordinator, authority = managed_write.connect(root, host, actor)
        native = {'coordinator':str(coordinator.path), 'attempt':attempt, 'actor':actor,
                  'root_identity':list(authority.root.identity), 'effect_id':effect_id,
                  'fingerprint':digest(ts.encoded(effect)), 'session_basis':'supplied-session',
                  'changes':{c['path']:{'before':c['backup'],'after':c['after']} for c in effect['changes']}}
        with engine.db() as db:
            old = db.execute('SELECT payload,native FROM intents WHERE id=?',(row['id'],)).fetchone()
            if old and (old['payload'] != row['payload'] or old['native'] != encoded(native)):
                raise DeliveryError('Checked code is bound to another native closeout effect.')
            db.execute('INSERT OR IGNORE INTO intents(id,payload,native,state) VALUES(?,?,?,?)',
                       (row['id'],row['payload'],encoded(native),'awaiting-native-verification'))
        controls.append({'engine':engine,'key':row['id'],'error':None})
    return controls


def _release_code(root, frozen, host, *, session, work_id, actor, selection=None):
    """Trusted aggregate closeout calls this only after its required phases pass."""
    from .git_delivery import Engine
    if (frozen.get('session') != session or frozen.get('work_id') != work_id
            or frozen.get('closeout', {}).get('actor') != actor
            or (selection is not None and (frozen.get('logical_id') != selection.get('logical_id')
                or set(frozen.get('changes', {})) != set(selection.get('paths', []))))):
        raise DeliveryError('Checked-code identity/scope differs from original closeout context; native save remains independent.')
    engine = Engine(root)
    key = engine._key(frozen)
    with engine.db() as db:
        job = db.execute('SELECT payload,state FROM jobs WHERE id=?',(key,)).fetchone()
    if not job or job['payload'] != encoded(frozen):
        raise DeliveryError('Checked closeout code payload differs from its durable preparation.')
    if job['state'] != 'waiting-native':
        return
    confirm(engine,key,host=host,verify=lambda: (_ for _ in ()).throw(DeliveryError('Original typed closeout proof is missing.')),
            checkpoint={'completed':'Required closeout phases verified.','next':'Inspect scoped code delivery.','pending':None})
    engine.drain_verified()
    with engine.db() as db:
        db.execute("UPDATE jobs SET state='queued' WHERE id=? AND state='waiting-native'",(key,))
