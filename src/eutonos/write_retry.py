"""Durable retry queue for blocked semantic Docs, board and optional RAM writes.

Queued payloads are operation-specific intents.  They are never stored as stale
replacement bytes: board and closeout organization work is replanned from the
latest source, while RAM reuses its already-frozen, source-qualified operation.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from . import tokenslang as ts
from .effects import LocalRoot, digest
from .host import connect
from .local_lock import local_lock


PROFILE = "eutonos.write-retry.v1"
DATABASE = "write-retry.sqlite"
KINDS = {"batch", "board-update", "closeout-organization", "ram-closeout", "work-lifecycle"}
OPEN_STATES = ("pending", "waiting", "working")
STALE_AFTER = 10 * 60
WAIT_ALERT = 30 * 60
MAX_BACKOFF = 60
MAX_PAYLOAD = 1024 * 1024
TERMINAL_RETENTION = 500
BUSY_MESSAGE = "Scope overlaps protected unfinished work."
WORKER_CLAIM_ENV = "EUTONOS_WRITE_RETRY_WORKER"


def _now() -> float:
    return time.time()


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _db_path(host: str | Path) -> Path:
    host = Path(host).resolve(strict=True)
    ts.require(host.is_dir(), "missing", "Select an existing EUTONOS host directory.")
    return host / DATABASE


def _connect(host: str | Path, *, readonly: bool = False):
    path = _db_path(host)
    if readonly:
        if not path.is_file():
            return None
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    # SQLite's initial WAL mode transition can fail immediately when another
    # constructor is doing the same transition. Serialize only connection/schema
    # initialization, across threads and processes sharing this exact host.
    # Release before caller transactions/dispatch/recovery; retain SQLite timeouts.
    db = None
    try:
        with local_lock(path.parent, timeout=30):
            db = sqlite3.connect(path, timeout=30)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests(
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, actor TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL, error TEXT, result TEXT
                );
                CREATE INDEX IF NOT EXISTS requests_root_state ON requests(root,state,created_at);
                CREATE TABLE IF NOT EXISTS worker(
                    root TEXT PRIMARY KEY, state TEXT NOT NULL, claim TEXT,
                    request_id TEXT, stage TEXT, heartbeat REAL NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_bindings(
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, actor TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)
            columns = {r[1] for r in db.execute('PRAGMA table_info(batch_bindings)')}
            if 'frozen_payload' not in columns:
                db.execute('ALTER TABLE batch_bindings ADD COLUMN frozen_payload TEXT')
    except BaseException:
        # The caller's closing(_connect(...)) has not entered yet if this
        # constructor raises, including failure to release its init lock.
        if db is not None:
            db.close()
        raise
    return db


def is_busy(value) -> bool:
    """Recognize only the coordinator's exact overlap result, not arbitrary conflicts."""
    if isinstance(value, BaseException):
        return getattr(value, "code", None) == "conflict" and getattr(value, "message", str(value)) == BUSY_MESSAGE
    if isinstance(value, dict):
        if value.get("code") == "conflict" and value.get("message") == BUSY_MESSAGE:
            return True
        return any(is_busy(item) for item in value.values() if isinstance(item, (dict, list)))
    if isinstance(value, list):
        return any(is_busy(item) for item in value)
    return False


def _hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_payload(kind: str, payload: dict) -> dict:
    ts.require(kind in KINDS and isinstance(payload, dict), "invalid", "Select a supported semantic retry operation.")
    if kind == "batch":
        ts.shape(payload, {"batch"})
        from .write_batch import normalize
        payload = {"batch": normalize(payload["batch"])}
    elif kind == "work-lifecycle":
        ts.shape(payload, {"request"})
        ts.require(isinstance(payload["request"], dict), "invalid", "Work retry requires a semantic request.")
    elif kind == "board-update":
        ts.shape(payload, {"owner_path", "item_id", "changes"}, {"expected_sha256", "expected_revision", "caller_context", "_attribution"})
        from .source import safe_path
        safe_path(payload["owner_path"]); ts.string(payload["item_id"])
        ts.require(isinstance(payload["changes"], dict) and payload["changes"], "invalid", "Board update changes are required.")
        if "expected_sha256" in payload:
            ts.require(_hex64(payload["expected_sha256"]), "invalid", "Expected board owner SHA-256 is invalid.")
        if "expected_revision" in payload:
            ts.require(_hex64(payload["expected_revision"]), "invalid", "Expected board item revision is invalid.")
    elif kind == "closeout-organization":
        ts.shape(payload, {"values", "context"})
        ts.require(isinstance(payload["values"], dict) and isinstance(payload["context"], dict),
                   "invalid", "Closeout retry needs semantic values and context.")
    else:
        ts.shape(payload, {"prepared", "context"})
        ts.require(isinstance(payload["prepared"], dict) and isinstance(payload["context"], dict),
                   "invalid", "RAM retry needs one frozen prepared operation and context.")
        if payload["prepared"].get("native_identity") is not None:
            ts.require(isinstance(payload["context"].get("native_log"), str)
                       and payload["context"]["native_log"].strip(), "invalid",
                       "Native pre-final RAM retry requires the original host-local native log selector.")
    body = ts.encoded(payload)
    ts.require(len(body) <= MAX_PAYLOAD, "overflow", "Write retry payload exceeds 1 MiB.")
    return ts.loads(body.decode("utf-8"))


def _semantic(payload):
    value = json.loads(json.dumps(payload))
    value.pop('_attribution', None)
    for key in ('batch','request'):
        if key in value: value[key].pop('_attribution', None)
    return value


def _frozen_submission(host, root, actor, kind, payload, *, validate_new=None):
    """Existing queue binding retains identity before enqueue, also after row pruning."""
    from . import work_attribution as attribution
    ts.string(actor)
    connect(root, host, actor)  # validate existing root/actor admission before retaining a binding
    payload = _validate_payload(kind, payload)
    semantic = _semantic(payload)
    request_id = _request_id(root, actor, kind, semantic)
    basis = semantic['batch'] if kind == 'batch' else ({k:semantic[k] for k in ('owner_path','item_id','changes','expected_sha256') if k in semantic} if kind == 'board-update' else semantic)
    expected = (str(root), actor, digest(ts.encoded(basis)))
    with closing(_connect(host)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old_request = db.execute('SELECT root,actor,payload FROM requests WHERE id=?', (request_id,)).fetchone()
        prior = db.execute('SELECT root,actor,payload_sha256,frozen_payload FROM batch_bindings WHERE id=?', (request_id,)).fetchone()
        if prior:
            ts.require(tuple(prior[:3]) == expected, 'conflict', 'Logical request is already bound to different content or authority.')
            if prior[3] is not None: return request_id, json.loads(prior[3])
            old = db.execute('SELECT payload FROM requests WHERE id=?', (request_id,)).fetchone()
            # Historic submissions never acquire a later profile/context.
            return request_id, json.loads(old[0]) if old else semantic
        if old_request:
            ts.require(tuple(old_request[:2]) == (str(root), actor), 'conflict', 'Logical request is bound to different authority.')
            retained = json.loads(old_request[2])
            ts.require(_semantic(retained) == semantic, 'conflict', 'Logical request differs from its original queued content.')
            return request_id, retained
        # Reject an editable new draft before retaining its operation identity.
        # This runs inside the same binding transaction, after original lookup.
        if validate_new is not None:
            validate_new()
        value = json.loads(json.dumps(payload))
        request = value['batch'] if kind == 'batch' else value['request'] if kind == 'work-lifecycle' else value
        # Caller cannot replace a binding on retry; first submission may carry
        # a previously prepared frozen snapshot from the same native planner.
        if attribution.signs(kind, value): request['_attribution'] = attribution.snapshot(root, request)
        value = _validate_payload(kind, value)  # bound the frozen payload too, before any insert
        db.execute('INSERT INTO batch_bindings VALUES(?,?,?,?,?,?)',
                   (request_id,*expected,_now(),ts.encoded(value).decode()))
        return request_id, value


def _request_id(root: Path, actor: str, kind: str, payload: dict) -> str:
    payload = _semantic(payload)
    if kind == "batch":
        basis = {"root": str(root), "operation_id": payload["batch"]["operation_id"]}
        return "write-batch-" + hashlib.sha256(ts.encoded(basis)).hexdigest()[:24]
    if kind == "board-update":
        intent = {key: payload[key] for key in ("owner_path", "item_id", "changes", "expected_sha256")
                  if key in payload}
        basis = {"root": str(root), "actor": actor, "kind": kind, "payload": intent}
        return "write-" + hashlib.sha256(ts.encoded(basis)).hexdigest()[:24]
    basis = {"root": str(root), "actor": actor, "kind": kind, "payload": payload}
    return "write-" + hashlib.sha256(ts.encoded(basis)).hexdigest()[:24]


def enqueue(host: str | Path, root: str | Path, actor: str, kind: str, payload: dict,
            *, request_id: str | None = None) -> dict:
    """Persist one bounded semantic retry after validating the selected host/root."""
    root = Path(root).resolve(strict=True)
    ts.string(actor)
    connect(root, host, actor)  # validates external host identity; never enlarges authority
    payload = _validate_payload(kind, payload)
    if kind in {'batch','work-lifecycle','board-update'}:
        _, payload = _frozen_submission(host, root, actor, kind, payload)
    computed_id = _request_id(root, actor, kind, payload)
    if request_id is None:
        request_id = computed_id
    else:
        ts.require(kind == "board-update" and isinstance(request_id, str) and request_id.startswith("write-"),
                   "invalid", "Only a board submission may retain its pre-bound request identity.")
    now = _now()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(_connect(host)) as db, db:
        # Serialize the read-then-insert identity checks. Without an immediate
        # writer lock, two simultaneous closeouts can both observe an absent
        # row and turn an idempotent duplicate into SQLITE_BUSY/UNIQUE noise.
        db.execute("BEGIN IMMEDIATE")
        if kind == "batch":
            payload_sha256 = digest(ts.encoded(_semantic(payload)["batch"]))
            binding = db.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?",
                                 (request_id,)).fetchone()
            if binding is None:
                db.execute("INSERT INTO batch_bindings(id,root,actor,payload_sha256,created_at) VALUES(?,?,?,?,?)",
                           (request_id, str(root), actor, payload_sha256, now))
            else:
                ts.require(tuple(binding) == (str(root), actor, payload_sha256),
                           "conflict", "Logical batch ID is already bound to different content or authority.")
        existed = db.execute("SELECT state,payload,actor FROM requests WHERE id=?", (request_id,)).fetchone()
        if existed is not None and kind == "batch":
            previous = json.loads(existed[1])
            ts.require(previous["batch"] == payload["batch"] and existed[2] == actor,
                       "conflict", "Logical batch ID is already bound to different content or authority.")
        if existed is not None and kind == "board-update":
            previous = json.loads(existed[1])
            ts.require(previous == payload and existed[2] == actor,
                       "conflict", "Board retry identity is already bound to different semantic intent.")
        if existed is None:
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (request_id, str(root), actor, kind, encoded, now, now,
                        "pending", 0, now, None, None))
        elif existed[0] == "failed":
            # An explicit repeat after corrective action is a real retry. Keep
            # the stable request identity and history count, but reactivate it.
            db.execute("UPDATE requests SET state='pending',updated_at=?,next_attempt=?,error=NULL,result=NULL WHERE id=?",
                       (now, now, request_id))
        db.execute("INSERT INTO worker VALUES(?,?,?,?,?,?,?) ON CONFLICT(root) DO NOTHING",
                   (str(root), "ready", None, None, "idle", now, None))
    outcome = "requeued" if existed and existed[0] == "failed" else "already-queued" if existed else "queued"
    state = "pending" if outcome in {"queued", "requeued"} else existed[0]
    return {"profile": PROFILE, "outcome": outcome,
            "request_id": request_id, "kind": kind, "state": state}


def _request_row(row) -> dict:
    return {"id": row[0], "actor": row[1], "kind": row[2], "created_at": _iso(row[3]),
            "updated_at": _iso(row[4]), "state": row[5], "attempts": row[6],
            "next_attempt": _iso(row[7]), "error": row[8],
            "result": json.loads(row[9]) if row[9] else None}


def _prune(db, root: str) -> None:
    """Bound completed history; failures remain visible until explicitly recovered."""
    db.execute("""DELETE FROM requests WHERE root=? AND state='complete' AND id NOT IN (
        SELECT id FROM requests WHERE root=? AND state='complete'
        ORDER BY updated_at DESC LIMIT ?)
        """, (root, root, TERMINAL_RETENTION))


def status(host: str | Path, root: str | Path) -> dict:
    root = str(Path(root).resolve(strict=True))
    db = _connect(host, readonly=True)
    if db is None:
        return {"profile": PROFILE, "status": "not-started", "signal": "green",
                "label": "Ready", "counts": {}, "requests": []}
    try:
        rows = db.execute("""SELECT id,actor,kind,created_at,updated_at,state,attempts,next_attempt,error,result
            FROM requests WHERE root=? ORDER BY created_at DESC LIMIT 100""", (root,)).fetchall()
        requests = [_request_row(row) for row in rows]
        count_rows = db.execute("SELECT state,COUNT(*) FROM requests WHERE root=? GROUP BY state",
                                (root,)).fetchall()
        oldest_open = db.execute("""SELECT MIN(created_at) FROM requests
            WHERE root=? AND state IN ('pending','waiting','working')""", (root,)).fetchone()[0]
        worker = db.execute("SELECT state,claim,request_id,stage,heartbeat,error FROM worker WHERE root=?", (root,)).fetchone()
    finally:
        db.close()
    observed_counts = dict(count_rows)
    counts = {name: observed_counts.get(name, 0)
              for name in ("pending", "waiting", "working", "complete", "failed")}
    stale = bool(worker and worker[1] and worker[0] in {"launching", "waiting", "working"}
                 and _now() - worker[4] > STALE_AFTER)
    overdue = bool(oldest_open is not None and _now() - oldest_open > WAIT_ALERT)
    failed = bool(stale or overdue or counts["failed"] or (worker and worker[0] == "failed"))
    busy = bool(sum(counts[name] for name in ("pending", "waiting", "working")))
    signal, label = (("red", "Needs attention") if failed else
                     ("yellow", "Applying queued writes") if busy else ("green", "Ready"))
    return {"profile": PROFILE, "status": "available", "signal": signal, "label": label,
            "counts": counts, "requests": requests,
            "worker": None if worker is None else {"state": "stale" if stale else "overdue" if overdue else worker[0],
                "request_id": worker[2], "stage": worker[3], "heartbeat": _iso(worker[4]), "error": worker[5]}}


def _set_worker(db, root, state, claim=None, request_id=None, stage="idle", error=None):
    db.execute("""INSERT INTO worker VALUES(?,?,?,?,?,?,?) ON CONFLICT(root) DO UPDATE SET
        state=excluded.state,claim=excluded.claim,request_id=excluded.request_id,
        stage=excluded.stage,heartbeat=excluded.heartbeat,error=excluded.error""",
               (root, state, claim, request_id, stage, _now(), error))


def _process_claim() -> str | None:
    value = os.environ.get(WORKER_CLAIM_ENV)
    return value if isinstance(value, str) and value else None


def _heartbeat_claim(host: str | Path, root: Path, claim: str, request_id: str,
                     stop: threading.Event) -> None:
    """Keep a live dispatch from being mistaken for an abandoned worker."""
    interval = max(0.05, min(30.0, STALE_AFTER / 3))
    while not stop.wait(interval):
        try:
            with closing(_connect(host)) as db, db:
                updated = db.execute("""UPDATE worker SET heartbeat=?
                    WHERE root=? AND state='working' AND claim=? AND request_id=?""",
                    (_now(), str(root), claim, request_id)).rowcount
            if not updated:
                return
        except (OSError, sqlite3.Error):
            # A transient heartbeat failure must not replace the semantic result.
            # Claim fencing below still prevents a displaced worker from finishing.
            continue


def _claim(host: str | Path, root: Path) -> dict:
    process_claim = _process_claim()
    with closing(_connect(host)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        worker = db.execute("SELECT state,claim,heartbeat FROM worker WHERE root=?", (str(root),)).fetchone()
        fresh = bool(worker and _now() - worker[2] <= STALE_AFTER)
        live = bool(worker and worker[0] in {"launching", "waiting", "working"} and worker[1])
        if live and fresh and worker[1] != process_claim:
            return {"outcome": "busy"}
        if live and not fresh:
            if worker[0] == "working":
                db.execute("UPDATE requests SET state='waiting',updated_at=?,next_attempt=? WHERE root=? AND state='working'",
                           (_now(), _now(), str(root)))
            _set_worker(db, str(root), "ready")
        row = db.execute("""SELECT id,actor,kind,payload,created_at FROM requests
            WHERE root=? AND state IN ('pending','waiting') AND next_attempt<=?
            ORDER BY next_attempt,created_at LIMIT 1""", (str(root), _now())).fetchone()
        if row is None:
            delayed = db.execute("""SELECT MIN(next_attempt) FROM requests
                WHERE root=? AND state IN ('pending','waiting')""", (str(root),)).fetchone()[0]
            if delayed is not None:
                _set_worker(db, str(root), "waiting", claim=process_claim, stage="backoff")
                return {"outcome": "delayed", "wait_seconds": max(0.05, min(MAX_BACKOFF, delayed - _now()))}
            _set_worker(db, str(root), "ready")
            return {"outcome": "idle"}
        active_claim = process_claim or ("sync-" + uuid.uuid4().hex)
        db.execute("UPDATE requests SET state='working',updated_at=?,attempts=attempts+1,error=NULL WHERE id=?",
                   (_now(), row[0]))
        _set_worker(db, str(root), "working", active_claim, row[0], "apply")
        return {"outcome": "claimed", "request_id": row[0], "actor": row[1], "kind": row[2],
                "payload": json.loads(row[3]), "submitted_at": row[4], "claim": active_claim}


def _board_update(host, root: Path, actor: str, request_id: str, payload: dict) -> dict:
    from . import boards
    owner = payload["owner_path"]
    attempt = "write-retry-board-" + request_id.removeprefix("write-")
    def verify():
        # Native completion may append a signature to requested extensions.
        # Read back the original effect, including that signature, rather than
        # comparing the unsigned semantic request to the signed result.
        from . import work_lifecycle as work
        from .managed_write import _effect_rows
        coordinator, _ = connect(root, host, actor)
        rows = _effect_rows(coordinator, attempt)
        ts.require(len(rows) == 1, 'conflict', 'Board recovery needs one original effect.')
        work.verify_effect(root, ts.loads(rows[0]['plan']))
    from .git_delivery_native import apply, recover
    prior = recover(root, host, actor, attempt,
                    {"completed": "Recovered queued board update verified.",
                     "next": "No automatic follow-up.", "pending": None},
                    verify=verify, effect_id=attempt)
    if prior["complete"]:
        return prior
    projection = boards.project(root)
    matches = [item for item in projection.get("items", [])
               if item.get("id") == payload["item_id"] and item.get("owner_path") == owner]
    ts.require(len(matches) == 1, "missing", "Queued board item is absent or ambiguous.")
    owner_rows = [row for row in projection.get("owners", []) if row.get("path") == owner]
    ts.require(len(owner_rows) == 1 and owner_rows[0].get("status") == "available",
               "missing", "Queued board owner is absent or unavailable.")
    if "expected_revision" not in payload:
        expected_owner = payload.get("expected_sha256")
        if expected_owner is not None:
            ts.require(owner_rows[0].get("sha256") == expected_owner, "stale",
                       "Selected board owner changed before submission; reconcile the update against current source.")
        payload["expected_sha256"] = owner_rows[0]["sha256"]
        payload["expected_revision"] = matches[0]["revision"]
    else:
        ts.require(matches[0].get("revision") == payload["expected_revision"], "stale",
                   "Queued board item changed after submission; reconcile the newer same-item decision before retrying.")
    if all(matches[0].get(key) == value for key, value in payload["changes"].items()):
        return {"outcome": "complete", "writes": 0, "reason": "already-applied"}
    local = LocalRoot(root)
    body = local.read(owner)
    plan = boards.plan_update(root, {"profile": boards.UPDATE, "owner_path": owner,
        "expected_sha256": digest(body), "item_id": payload["item_id"], "changes": payload["changes"],
        **{k:payload[k] for k in ('caller_context','_attribution') if k in payload}})
    return apply(root, host, actor, attempt, attempt, plan["effect"], plan["fingerprint"], plan["paths"],
                 {"completed": "Queued board update verified.", "next": "No automatic follow-up.", "pending": None},
                 verify=verify)


def submit_board_update(host: str | Path, root: str | Path, actor: str, payload: dict) -> dict:
    """Apply one board update now, or durably queue it when its owner is busy."""
    root = Path(root).resolve(strict=True)
    payload = _validate_payload("board-update", payload)
    request_id, payload = _frozen_submission(host, root, actor, "board-update", payload)
    try:
        result = _board_update(host, root, actor, request_id, payload)
        return {"profile": PROFILE, **result, "request_id": request_id, "kind": "board-update"}
    except ts.ReadError as exc:
        if not is_busy(exc):
            raise
        queued = enqueue(host, root, actor, "board-update", payload, request_id=request_id)
        queued["worker"] = start_worker(host, root)
        return {**queued, "reason": "write-owner-busy"}


def _dispatch(host, root: Path, actor: str, request_id: str, kind: str, payload: dict) -> dict:
    if kind in {'batch','work-lifecycle','board-update'}:
        # Historic queue rows are intentionally unknown, not rebound to today's profile.
        from . import work_attribution as attribution
        if attribution.signs(kind, payload):
            payload = json.loads(json.dumps(payload))
            request = payload['batch'] if kind == 'batch' else payload['request'] if kind == 'work-lifecycle' else payload
            if '_attribution' not in request:
                with closing(_connect(host, readonly=True)) as db:
                    created = db.execute('SELECT created_at FROM requests WHERE id=?', (request_id,)).fetchone()[0]
                request['_attribution'] = attribution.unknown_snapshot(request_id, _iso(created))
    if kind == "batch":
        from . import write_batch
        recovered = write_batch.recover_submitted(root, host, actor, payload["batch"], request_id)
        if recovered is not None:
            return recovered
        if any(op["kind"] == "import" for op in payload["batch"]["operations"]):
            _, authority = connect(root, host, actor)
            prepared = write_batch.plan(root, payload["batch"], authority=authority)
        else:
            prepared = write_batch.plan(root, payload["batch"])
        return write_batch.apply_prepared(root, host, actor, prepared, request_id)
    if kind == "work-lifecycle":
        return _work_update(host, root, actor, request_id, payload["request"])
    if kind == "board-update":
        return _board_update(host, root, actor, request_id, payload)
    if kind == "closeout-organization":
        from . import closeout_route
        context = {**payload["context"], "root": str(root), "host": str(Path(host).resolve()),
                   "actor": actor, "_write_retry_worker": True}
        # Catalog/work assessments already passed before this required Docs
        # operation was queued. Retry only that frozen semantic phase.
        result = closeout_route._run(payload["values"], context)
    else:
        from . import workflow
        from .companion_ram_capture import capture
        context = {**payload["context"], "root": str(root), "host": str(Path(host).resolve()),
                   "actor": actor, "_ram_retry_worker": True}
        result = workflow.execute_closeout(payload["prepared"], context,
            capture_fn=lambda *args: capture(*args, retry_waiting=True))
    if result.get("outcome") in {"completed", "already-completed", "skipped"}:
        return {"outcome": "complete", "result": result}
    if is_busy(result):
        return {"outcome": "waiting", "reason": BUSY_MESSAGE, "result": result}
    return {"outcome": "failed", "error": result.get("message", "Queued semantic write did not complete."),
            "result": result}


def _work_update(host, root, actor, request_id, request):
    from . import work_lifecycle as work
    from .effects import decode
    coordinator, authority = connect(root, host, actor)
    attempt = "work-lifecycle-" + request_id
    try:
        state = coordinator.inspect(attempt)
    except ts.ReadError as exc:
        if exc.code != "missing": raise
        state = None
    # Recover the original typed save/intent before interpreting changed owners.
    if state:
        from .managed_write import _effect_rows
        from .git_delivery_native import recover
        rows = _effect_rows(coordinator, attempt)
        def verify():
            ts.require(len(rows) == 1, 'conflict', 'Work recovery needs one original effect.')
            work.verify_effect(root, ts.loads(rows[0]['plan']))
        prior = recover(root, host, actor, attempt,
                        {'completed': 'Recovered filing verified.', 'next': 'Continue from Current Tasks.', 'pending': None},
                        verify=verify)
        if prior['complete']:
            return prior
    prepared = work.plan(root, request)
    return work.apply_prepared(root, host, actor, prepared, request_id)


def submit_work(host, root, actor, request):
    """Submit once durably; the queue owns recovery even if the caller exits."""
    from . import work_lifecycle as work
    root = Path(root).resolve(strict=True)
    if request.get("operation") == "refresh":
        request = {**request, "refresh_revision": work.plan(root, request)["fingerprint"]}
    payload = {'request': request}
    request_id, payload = _frozen_submission(host, root, actor, 'work-lifecycle', payload)
    request = payload['request']
    # An exact repeat can retrieve its durable receipt despite later edits.
    basis = {"root": str(root), "actor": actor, "kind": "work-lifecycle", "payload": payload}
    request_id = "write-" + hashlib.sha256(ts.encoded({**basis, "payload": _semantic(payload)})).hexdigest()[:24]
    db = _connect(host, readonly=True)
    prior = None
    if db is not None:
        with closing(db): prior = db.execute("SELECT state FROM requests WHERE id=?", (request_id,)).fetchone()
    if prior is None:
        coordinator, _ = connect(root, host, actor)
        from .managed_write import _effect_rows
        if _effect_rows(coordinator, 'work-lifecycle-' + request_id):
            recovered = _work_update(host, root, actor, request_id, request)
            if recovered.get('complete'):
                return {'profile':PROFILE,'outcome':'complete','state':'complete','request_id':request_id,'kind':'work-lifecycle','result':recovered}
        work.plan(root, request)  # reject invalid/stale semantics before retaining
    queued = enqueue(host, root, actor, "work-lifecycle", payload)
    result = run_once(host, root)
    current = next((r for r in status(host, root)["requests"] if r["id"] == request_id), None)
    if current and current["state"] == "complete":
        return {**queued, "outcome": "complete", "state": "complete", "result": current["result"]}
    if current and current["state"] == "failed":
        return {**queued, "outcome": "error", "state": "failed", "message": current["error"], "result": current["result"]}
    return {**queued, "outcome": "queued", "worker": start_worker(host, root),
            "next": "Submission is durable. No manual retry is needed while yellow; inspect receipts for verified completion or red attention."}


def submit_batch(host, root, actor, batch, *, validate_new=None):
    """Submit/recover a batch; optional current-source checks run only for new work."""
    from . import write_batch
    root = Path(root).resolve(strict=True)
    normalized = write_batch.normalize(batch)
    payload = {'batch': normalized}
    validated = False
    def validate():
        nonlocal validated
        if validate_new is not None and not validated:
            validate_new()
            validated = True
    request_id, payload = _frozen_submission(host, root, actor, 'batch', payload, validate_new=validate)
    normalized = payload['batch']
    # A batch submission is a write operation, so open the queue writable here.
    # Besides reading prior identity, this runs additive schema migration for
    # hosts created before batch_bindings existed. A read-only first access to
    # such a host would otherwise query a table that has not been created yet.
    db = _connect(host)
    prior = binding = None
    if db is not None:
        with closing(db):
            prior = db.execute("SELECT state FROM requests WHERE id=?", (request_id,)).fetchone()
            binding = db.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?",
                                 (request_id,)).fetchone()
    if binding is not None:
        expected = (str(root), actor, digest(ts.encoded(_semantic(payload)["batch"])))
        ts.require(tuple(binding) == expected, "conflict",
                   "Logical batch ID is already bound to different content or authority.")
        if prior is None:
            try:
                recovered = write_batch.recover_submitted(root, host, actor, normalized, request_id)
            except ts.ReadError as exc:
                return {"profile": PROFILE, "outcome": "error", "request_id": request_id,
                        "kind": "batch", "state": "failed", "message": exc.message}
            if recovered is not None:
                return {"profile": PROFILE, "outcome": "complete", "request_id": request_id,
                        "kind": "batch", "state": "complete", "result": recovered}
    if prior is None:
        # A retained binding alone is not an accepted semantic operation: an
        # earlier planner can have failed before queue/effect admission.
        validate()
        if any(op["kind"] == "import" for op in normalized["operations"]):
            _, authority = connect(root, host, actor)
            write_batch.plan(root, normalized, authority=authority)
        else:
            write_batch.plan(root, normalized)
    queued = enqueue(host, root, actor, "batch", payload)
    run_once(host, root)
    current = next((row for row in status(host, root)["requests"] if row["id"] == request_id), None)
    if current and current["state"] == "complete":
        return {**queued, "outcome": "complete", "state": "complete", "result": current["result"]}
    if current and current["state"] == "failed":
        return {**queued, "outcome": "error", "state": "failed", "message": current["error"], "result": current["result"]}
    return {**queued, "outcome": "queued", "worker": start_worker(host, root),
            "next": "Atomic batch is durable. No manual retry is needed while yellow."}


def run_once(host: str | Path, root: str | Path) -> dict:
    root = Path(root).resolve(strict=True)
    process_claim = _process_claim()
    claimed = _claim(host, root)
    if claimed["outcome"] != "claimed":
        return {"profile": PROFILE, **claimed}
    request_id = claimed["request_id"]
    active_claim = claimed["claim"]
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_claim,
        args=(host, root, active_claim, request_id, heartbeat_stop), daemon=True)
    heartbeat.start()
    try:
        from .git_delivery_native import submitted_at
        with submitted_at(claimed["submitted_at"]):
            result = _dispatch(host, root, claimed["actor"], request_id, claimed["kind"], claimed["payload"])
    except BaseException as exc:
        result = ({"outcome": "waiting", "reason": BUSY_MESSAGE} if is_busy(exc) else
                  {"outcome": "failed", "error": getattr(exc, "message", str(exc)) or type(exc).__name__})
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
    with closing(_connect(host)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        worker = db.execute("SELECT claim,request_id FROM worker WHERE root=?", (str(root),)).fetchone()
        if worker is None or tuple(worker) != (active_claim, request_id):
            return {"profile": PROFILE, "request_id": request_id, "outcome": "superseded",
                    "result": result,
                    "next": "A newer worker owns this queue. Its durable state was preserved."}
        if result["outcome"] == "waiting":
            attempts = db.execute("SELECT attempts FROM requests WHERE id=?", (request_id,)).fetchone()[0]
            backoff = min(MAX_BACKOFF, max(2, 2 ** min(attempts, 6)))
            db.execute("UPDATE requests SET state='waiting',updated_at=?,next_attempt=?,error=? WHERE id=?",
                       (_now(), _now() + backoff, result.get("reason"), request_id))
            _set_worker(db, str(root), "waiting", claim=process_claim, request_id=request_id, stage="waiting")
        elif result["outcome"] == "complete":
            db.execute("UPDATE requests SET state='complete',updated_at=?,result=?,error=NULL WHERE id=?",
                       (_now(), json.dumps(result.get("result", result), sort_keys=True), request_id))
            if process_claim:
                _set_worker(db, str(root), "waiting", claim=process_claim, stage="continuing")
            else:
                _set_worker(db, str(root), "ready")
        else:
            error = result.get("error", "Queued write failed.")
            db.execute("UPDATE requests SET state='failed',updated_at=?,error=?,result=? WHERE id=?",
                       (_now(), error, json.dumps(result.get("result"), sort_keys=True) if result.get("result") else None,
                        request_id))
            _set_worker(db, str(root), "failed", request_id=request_id, stage="failed", error=error)
        _prune(db, str(root))
    if result["outcome"] == "complete":
        try:
            from .current_work import invalidate
            invalidate(root)
        except (OSError, ValueError, TypeError, ts.ReadError):
            # The projection is disposable. A cache failure must never turn
            # an already committed native write into a retry of that write.
            pass
    return {"profile": PROFILE, "request_id": request_id, **result}


def start_worker(host: str | Path, root: str | Path) -> dict:
    root = Path(root).resolve(strict=True)
    worker_claim = uuid.uuid4().hex
    with closing(_connect(host)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT state,claim,heartbeat FROM worker WHERE root=?", (str(root),)).fetchone()
        age = _now() - row[2] if row else None
        if row and row[0] in {"launching", "waiting", "working"} and row[1] and age <= STALE_AFTER:
            return {"outcome": "already-running", "state": row[0]}
        if row and row[0] == "working" and age is not None and age > STALE_AFTER:
            db.execute("UPDATE requests SET state='waiting',updated_at=?,next_attempt=? WHERE root=? AND state='working'",
                       (_now(), _now(), str(root)))
        _set_worker(db, str(root), "launching", claim=worker_claim, stage="starting")
    command = [sys.executable, "-m", "eutonos.write_retry_cli", "worker",
               "--host", str(Path(host).resolve()), "--root", str(root)]
    environment = dict(os.environ)
    environment[WORKER_CLAIM_ENV] = worker_claim
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True, "env": environment}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
        return {"outcome": "started", "pid": process.pid}
    except BaseException as exc:
        with closing(_connect(host)) as db, db:
            _set_worker(db, str(root), "failed", stage="start-failed", error=str(exc))
        raise


__all__ = ["BUSY_MESSAGE", "DATABASE", "PROFILE", "enqueue", "is_busy", "run_once", "start_worker",
           "status", "submit_batch", "submit_board_update", "submit_work"]
