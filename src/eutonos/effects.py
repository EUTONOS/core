"""EHF2W cooperative, fixture-only effects; no effects are added to Hub reads.

One explicit host-local SQLite owner serializes participating writers. This is
not a sandbox against unmanaged editors, hostile directory swaps or DB tampering.
"""

from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import unicodedata
import uuid

from . import tokenslang as ts
from .source import safe_path

PROFILE = "eutonos.tokenslang.effect-plan.v1"
MARKER = ".eutonos-synthetic"
MARKER_BYTES = b"EUTONOS disposable effect fixture v1\n"
MAX_FILES = 16


def digest(body: bytes | None) -> str | None:
    return None if body is None else hashlib.sha256(body).hexdigest()


class LocalRoot:
    """Bounded exact local paths, existing ordinary parents and regular files only."""

    def __init__(self, root: str | Path, excluded: tuple[str, ...] = ()):
        self.root = Path(os.path.abspath(root))
        self.excluded = tuple(safe_path(p) for p in excluded)
        self._parents(self.root)
        info = self.root.stat()
        self.identity = (info.st_dev, info.st_ino)

    @staticmethod
    def _ordinary(path: Path):
        info = path.lstat()
        ts.require(not stat.S_ISLNK(info.st_mode)
                   and not getattr(info, "st_file_attributes", 0) & 0x400,
                   "unsafe-path", "Symlinks and reparse points are unsupported.")
        return info

    def _parents(self, path: Path):
        for parent in reversed((path, *path.parents)):
            info = self._ordinary(parent)
            ts.require(stat.S_ISDIR(info.st_mode), "unsafe-path", "Parent is not an ordinary directory.")

    def path(self, name: str) -> Path:
        name = safe_path(name)
        ts.require(not any(name == p or name.startswith(p + "/") for p in self.excluded),
                   "denied", "Excluded source root.")
        self._parents(self.root)
        info = self.root.stat()
        ts.require((info.st_dev, info.st_ino) == self.identity, "conflict", "Selected root changed.")
        current = self.root
        for index, part in enumerate(name.split("/")):
            key = unicodedata.normalize("NFC", part).casefold()
            # Directory size is not an eligibility condition. Scan names without
            # retaining unrelated entries; still reject every identity collision.
            matches = []
            for entry in current.iterdir():
                if unicodedata.normalize("NFC", entry.name).casefold() == key:
                    matches.append(entry)
                    if len(matches) > 1:
                        break
            ts.require(len(matches) <= 1 and (not matches or matches[0].name == part),
                       "ambiguous", "Exact path has a case or Unicode collision.")
            current = current / part
            if index < len(name.split("/")) - 1:
                ts.require(current.exists(), "unsupported", "Create parent directories separately; implicit tree creation is unsupported.")
                ts.require(stat.S_ISDIR(self._ordinary(current).st_mode), "unsafe-path", "Unsupported parent.")
                ts.require(not (current / ".git").exists(), "denied", "Nested repository root.")
                ts.require(not any((current / folder / marker).exists()
                           for folder in (".project", ".tova")
                           for marker in ("TOVA_INSTALLATION.json", "EXECUTION_STATE.json")),
                           "denied", "Nested native project root.")
            elif matches:
                info = self._ordinary(current)
                ts.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                           "unsupported", "Only ordinary singly-linked files are supported.")
        return current

    @contextmanager
    def _read_stream(self, name: str):
        path = self.path(name)
        before = path.stat()
        try:
            opened_stream = path.open("rb")
        except FileNotFoundError as exc:
            raise ts.ReadError("conflict", "Source disappeared while opening.") from exc
        with opened_stream as stream:
            opened = os.fstat(stream.fileno())
            ts.require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino),
                       "conflict", "Source changed while opening.")
            yield stream
        try:
            self.path(name)
            after = path.stat()
        except FileNotFoundError as exc:
            raise ts.ReadError("conflict", "Source disappeared during observation.") from exc
        ts.require(all(getattr(after, field) == getattr(before, field) for field in
                       ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")),
                   "conflict", "Source changed during observation.")

    def iter_read(self, name: str, chunk_size: int = 65536):
        """Observe a regular file in chunks, retaining identity/change checks.

        Missing paths raise FileNotFoundError. Fully consume this iterator before
        using an observed digest; its final check detects changes during reading.
        Chunk size controls buffering, not the size of an eligible source file.
        """
        ts.require(type(chunk_size) is int and chunk_size > 0, "invalid", "Positive read chunk size required.")
        with self._read_stream(name) as stream:
            while chunk := stream.read(chunk_size):
                yield chunk

    def read(self, name: str) -> bytes | None:
        try:
            with self._read_stream(name) as stream:
                return stream.read()
        except FileNotFoundError:
            # Preserve tracked-deletion handling. A disappearance after an open
            # must not be mistaken for an empty, successfully observed file.
            return None

    def replace(self, name: str, before: str | None, body: bytes | None):
        ts.require(digest(self.read(name)) == before, "stale", "Source differs from expected bytes.")
        path = self.path(name)
        if body is None:
            path.unlink()
            return
        temporary = path.with_name(".eutonos-write-" + uuid.uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            ts.require(digest(self.read(name)) == before, "stale", "Source changed before replacement.")
            os.replace(temporary, self.path(name))
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True)
class FixtureAuthority:
    """Host-supplied admission, never deserialized from a TokenSlang request.

    Marker plus explicit scope prevents accidental use on ordinary repositories;
    neither proves user permission. The caller must own a disposable fixture.
    """
    root: LocalRoot
    actor: str
    paths: frozenset[str]
    expires_at: float

    def check(self, paths):
        ts.string(self.actor)
        ts.require(time.time() < self.expires_at, "denied", "Host admission expired.")
        ts.require(self.root.read(MARKER) == MARKER_BYTES, "denied", "Only explicitly marked disposable fixtures support effects.")
        ts.require(set(paths) <= self.paths and MARKER not in paths, "denied", "Effect exceeds host-admitted paths.")
        for name in paths:
            self.root.path(name)


def plan(root: LocalRoot, updates: dict[str, bytes | None], inputs=(), *, board_census=None,
         board_import=None, nav_source_fingerprint=None) -> dict:
    ts.require(len(updates) <= MAX_FILES and len(set(inputs) | updates.keys()) <= 64,
               "overflow", "Effect scope exceeds bounds.")
    originals = {name: root.read(name) for name in sorted(set(inputs) | updates.keys())}
    observed = {name: digest(body) for name, body in originals.items()}
    changes = []
    for name, body in sorted(updates.items()):
        ts.require(body is None or isinstance(body, bytes),
                   "invalid", "Expected bytes or deletion.")
        if digest(body) != observed[name]:
            changes.append({"path": name, "before": observed[name],
                            "backup": None if originals[name] is None else base64.b64encode(originals[name]).decode("ascii"),
                            "after": None if body is None else base64.b64encode(body).decode("ascii")})
    value = {"profile": PROFILE, "root_identity": list(root.identity), "observed": observed, "changes": changes}
    if board_census is not None:
        value["board_census"] = ts.loads(ts.encoded(board_census).decode())
    if board_import is not None:
        value["board_import"] = ts.loads(ts.encoded(board_import).decode())
    if nav_source_fingerprint is not None:
        ts.require(isinstance(nav_source_fingerprint, str)
                   and re.fullmatch(r"[0-9a-f]{64}", nav_source_fingerprint),
                   "invalid", "Expected a NAV source fingerprint.")
        value["nav_source_fingerprint"] = nav_source_fingerprint
    return value


def validate_plan(root: LocalRoot, value: dict, *, authority=None):
    ts.shape(value, {"profile", "root_identity", "observed", "changes"},
             {"board_census", "board_import", "nav_source_fingerprint"})
    if "board_import" in value:
        ts.require("board_census" in value, "invalid", "Typed import metadata requires a census guard.")
        from .board_import import validate_metadata
        validate_metadata(value["board_import"])
    ts.require(value["profile"] == PROFILE and value["root_identity"] == list(root.identity),
               "conflict", "Effect profile or local root identity mismatch.")
    ts.require(isinstance(value["observed"], dict) and len(value["observed"]) <= 64,
               "invalid", "Expected bounded source preconditions.")
    changes = ts.sequence(value["changes"])
    ts.require(len(changes) <= MAX_FILES, "overflow", "Too many changed files.")
    seen = set()
    for change in changes:
        ts.shape(change, {"path", "before", "backup", "after"})
        name = safe_path(change["path"])
        ts.require(name not in seen and name in value["observed"]
                   and change["before"] == value["observed"][name], "invalid", "Conflicting change/precondition.")
        seen.add(name)
        ts.require(digest(decode(change["backup"])) == change["before"], "invalid", "Backup differs from precondition.")
        decode(change["after"])
    for name, expected in value["observed"].items():
        ts.require(digest(root.read(name)) == expected, "stale", "Plan source precondition changed: " + name)
    if "nav_source_fingerprint" in value:
        expected = value["nav_source_fingerprint"]
        ts.require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
                   "invalid", "Expected a NAV source fingerprint.")
        from . import nav
        config_path = "eutonos.nav.json"
        current = nav.inventory(root, nav.config(root.read(config_path)), config_path)
        ts.require(current["fingerprint"] == expected, "stale", "NAV source changed after planning.")
    if "board_census" in value:
        from . import board_census
        context = ({"authority": authority} if "board_import" in value and authority is not None
                   and isinstance(value.get("board_census"), dict)
                   and value["board_census"].get("profile") == board_census.NATIVE_PROFILE else {})
        board_census.validate(root, value["board_census"], **context)


def decode(value):
    if value is None:
        return None
    try:
        body = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ts.ReadError("invalid", "Invalid effect byte encoding.") from exc
    return body


class Coordinator:
    """One explicitly initialized local DB outside every participating checkout.

    No auto-create on open or recovery. DB loss/corruption is an unavailable owner,
    never permission to start with an empty reservation table.
    """

    @classmethod
    def initialize(cls, path: str | Path):
        path = Path(path).absolute()
        LocalRoot(path.parent).path(path.name)
        with path.open("xb"):
            pass
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                PRAGMA user_version=1;
                CREATE TABLE attempts(id TEXT PRIMARY KEY, root TEXT, actor TEXT,
                    generation INTEGER, state TEXT, checkpoint TEXT);
                CREATE TABLE reservations(root TEXT, path TEXT, attempt TEXT,
                    baseline TEXT, PRIMARY KEY(root,path));
                CREATE TABLE effects(id TEXT PRIMARY KEY, attempt TEXT, fingerprint TEXT,
                    plan TEXT, state TEXT);
            """)
        return cls(path)

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()
        LocalRoot(self.path.parent).path(self.path.name)
        ts.require(self.path.is_file(), "unavailable", "Coordinator is missing; do not recreate it implicitly.")
        with self._db() as db:
            ts.require(db.execute("PRAGMA user_version").fetchone()[0] in (1, 2)
                       and db.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                       "unavailable", "Coordinator is unsupported or damaged.")

    @contextmanager
    def _db(self):
        try:
            # mode=rw must not recreate a store lost between opening and use.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.Error as exc:
            # CLI callers serialize ReadError, losing its chained exception.
            # Keep SQLite's diagnostic identity, not SQL/values from raw errors.
            code = getattr(exc, "sqlite_errorcode", None)
            name = getattr(exc, "sqlite_errorname", "SQLITE_UNKNOWN")
            detail = f" ({name}, code {code})" if code is not None else ""
            hint = ""
            if isinstance(code, int) and code & 0xff == sqlite3.SQLITE_READONLY:
                hint = (" The host coordinator requires write access; check sandbox "
                        "and filesystem permissions and use an authorized host-write "
                        "context. Do not recreate or reset the database.")
            raise ts.ReadError(
                "unavailable",
                f"Local coordinator failed{detail}; existing protection is unknown.{hint}",
            ) from exc

    @staticmethod
    def _root(authority):
        return ":".join(str(v) for v in authority.root.identity)

    def _attempt(self, db, authority, attempt, generation, active=True):
        authority.check(())
        ts.require(not self.path.is_relative_to(authority.root.root), "denied", "Coordinator must stay outside the checkout.")
        row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
        ts.require(row is not None and row["root"] == self._root(authority)
                   and row["actor"] == authority.actor and row["generation"] == generation,
                   "conflict", "Attempt ownership or generation changed.")
        ts.require(not active or row["state"] == "active", "denied", "Attempt is not active.")
        return row

    def start(self, authority, attempt: str):
        authority.check(())
        ts.string(attempt)
        ts.require(not self.path.is_relative_to(authority.root.root), "denied", "Coordinator must stay outside checkout.")
        with self._db() as db:
            ts.require(db.execute("SELECT 1 FROM attempts WHERE id=?", (attempt,)).fetchone() is None,
                       "conflict", "Attempt already exists.")
            db.execute("INSERT INTO attempts VALUES(?,?,?,1,'active','{}')",
                       (attempt, self._root(authority), authority.actor))
        return 1

    def reserve(self, authority, attempt, generation, paths):
        paths = tuple(paths)
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            authority.check(paths)
            if hasattr(authority, "claim"):
                authority.claim(db, attempt, paths)
            for name in paths:
                # Exact files plus ancestor overlap protect future file/directory conflicts.
                rows = db.execute("SELECT * FROM reservations WHERE root=?", (self._root(authority),)).fetchall()
                ts.require(not any(r["attempt"] != attempt and (r["path"] == name
                           or r["path"].startswith(name + "/") or name.startswith(r["path"] + "/")) for r in rows),
                           "conflict", "Scope overlaps protected unfinished work.")
                db.execute("INSERT OR IGNORE INTO reservations VALUES(?,?,?,?)",
                           (self._root(authority), name, attempt, digest(authority.root.read(name))))

    def checkpoint(self, authority, attempt, generation, value):
        body = ts.encoded(value)
        ts.require(len(body) <= 8192, "overflow", "Checkpoint exceeds 8 KiB.")
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            db.execute("UPDATE attempts SET checkpoint=? WHERE id=?", (body.decode(), attempt))

    def pause(self, authority, attempt, generation):
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            db.execute("UPDATE attempts SET state='paused',generation=generation+1 WHERE id=?", (attempt,))
        return generation + 1

    def resume(self, authority, attempt, generation):
        with self._db() as db:
            row = self._attempt(db, authority, attempt, generation, active=False)
            ts.require(row["state"] == "paused", "conflict", "Only a paused attempt can resume.")
            db.execute("UPDATE attempts SET state='active',generation=generation+1 WHERE id=?", (attempt,))
        return generation + 1

    def inspect(self, attempt):
        with self._db() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            ts.require(row is not None, "missing", "Attempt is absent.")
            return {**dict(row), "reservations": [dict(r) for r in db.execute(
                "SELECT * FROM reservations WHERE attempt=?", (attempt,))],
                "unfinished_effects": [dict(r) for r in db.execute(
                    "SELECT id,state FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back')", (attempt,))]}

    def _held(self, db, authority, attempt, value):
        paths = [c["path"] for c in value["changes"]]
        authority.check(paths)
        held = {r[0] for r in db.execute("SELECT path FROM reservations WHERE root=? AND attempt=?",
                                      (self._root(authority), attempt))}
        ts.require(set(paths) <= held, "denied", "Reserve exact effect paths first.")
        if hasattr(authority, "check_attempt"):
            authority.check_attempt(db, attempt, paths)

    def apply(self, authority, attempt, generation, effect_id, value, *, precondition=None,
              _import_derivation=None):
        ts.string(effect_id)
        # Detach caller-owned mutable data before checking or recording it.
        value = ts.loads(ts.encoded(value).decode())
        fingerprint = digest(ts.encoded(value))
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            old = db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
            if old:
                ts.require(old["attempt"] == attempt and old["fingerprint"] == fingerprint,
                           "conflict", "Logical effect ID is bound to different content.")
                if old["state"] == "completed":
                    return {"effect_id": effect_id, "outcome": "already-completed", "writes": 0}
                ts.require(old["state"] == "not-applied", "unknown", "Inspect and reconcile the unfinished effect before retry.")
            ts.require(db.execute("SELECT 1 FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back')", (attempt,)).fetchone() is None,
                       "unknown", "Reconcile the attempt's interrupted effect first.")
            if precondition is not None:
                precondition()  # Trusted host check, never loaded from a request or source file.
            if "board_import" in value:
                ts.require(callable(_import_derivation), "denied", "Import tags require trusted typed source derivation.")
                _import_derivation(value)
            from . import board_census
            context = ({"authority": authority} if "board_import" in value
                       and isinstance(value.get("board_census"), dict)
                       and value["board_census"].get("profile") == board_census.NATIVE_PROFILE else {})
            if context:
                from .board_import import check_context
                check_context(authority.root, authority)
                ts.require(self.path == authority.admission.path("coordinator.sqlite"),
                           "wrong-scope", "Native import requires its connected coordinator host.")
            validate_plan(authority.root, value, **context)
            self._held(db, authority, attempt, value)
            if not value["changes"]:
                return {"effect_id": effect_id, "outcome": "no-op", "writes": 0}
            if old:
                db.execute("UPDATE effects SET state='prepared' WHERE id=?", (effect_id,))
            else:
                db.execute("INSERT INTO effects VALUES(?,?,?,?,'prepared')",
                           (effect_id, attempt, fingerprint, ts.encoded(value).decode()))
        # A durable prepared journal precedes the first target mutation. Process death
        # rolls this transaction back, leaving an unknown outcome for explicit inspection.
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            state = db.execute("SELECT state FROM effects WHERE id=?", (effect_id,)).fetchone()[0]
            ts.require(state == "prepared", "conflict", "Effect state changed before execution.")
            self._held(db, authority, attempt, value)
            if precondition is not None:
                precondition()
            if "board_import" in value:
                _import_derivation(value)
            validate_plan(authority.root, value, **context)
            for change in value["changes"]:
                authority.check([change["path"]])
                authority.root.replace(change["path"], change["before"], decode(change["after"]))
            db.execute("UPDATE effects SET state='completed' WHERE id=?", (effect_id,))
        return {"effect_id": effect_id, "outcome": "completed", "writes": len(value["changes"])}

    def reconcile(self, authority, attempt, generation, effect_id):
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            row = db.execute("SELECT * FROM effects WHERE id=? AND attempt=?", (effect_id, attempt)).fetchone()
            ts.require(row is not None, "missing", "Effect is absent.")
            value = ts.loads(row["plan"])
            self._held(db, authority, attempt, value)
            states = []
            for change in value["changes"]:
                actual = digest(authority.root.read(change["path"]))
                states.append("after" if actual == digest(decode(change["after"])) else
                              "before" if actual == change["before"] else "diverged")
            if row["state"] == "rolling-back":
                outcome = "rollback-incomplete"
                if all(s == "before" for s in states):
                    outcome = "rolled-back"
                    if "board_import" in value:
                        from .board_import import validate_abort_journal
                        validate_abort_journal(self, authority, db, row)
                        outcome = "not-applied"
                    db.execute("UPDATE effects SET state=? WHERE id=?", (outcome, effect_id))
            elif row["state"] == "prepared" and all(s == "after" for s in states):
                db.execute("UPDATE effects SET state='completed' WHERE id=?", (effect_id,))
                outcome = "completed"
            elif row["state"] == "prepared" and all(s == "before" for s in states):
                # Inspection established no target effect. Permit this exact retry ID.
                db.execute("UPDATE effects SET state='not-applied' WHERE id=?", (effect_id,))
                outcome = "not-applied"
            else:
                outcome = "diverged" if "diverged" in states else "partial" if "before" in states else "completed"
            return {"effect_id": effect_id, "outcome": outcome, "paths": states, "writes": 0}

    def rollback(self, authority, attempt, generation, effect_id):
        """Inspect first, then undo only bytes still attributable to this effect.

        Diverged files are preserved and retain reservations. A process interruption
        leaves the journal prepared, so another rollback can inspect and continue.
        Completed native imports require typed undo. Interrupted imports may abort
        only when every frozen path is attributable; recovery retains their fence.
        """
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            row = db.execute("SELECT * FROM effects WHERE id=? AND attempt=?", (effect_id, attempt)).fetchone()
            ts.require(row is not None and row["state"] in {"prepared", "completed", "rolling-back", "rolled-back"},
                       "conflict", "Effect is not available for rollback.")
            value = ts.loads(row["plan"])
            imported = "board_import" in value
            if imported:
                ts.require(row["state"] in {"prepared", "rolling-back"}, "denied",
                           "Completed native imports require typed undo; only interrupted imports may abort.")
                from .board_import import validate_abort_journal
                value = validate_abort_journal(self, authority, db, row)
            self._held(db, authority, attempt, value)
            if imported:
                self._import_abort_preflight(authority, value)
            if row["state"] == "rolled-back":
                return {"outcome": "already-rolled-back", "writes": 0}
            db.execute("UPDATE effects SET state='rolling-back' WHERE id=?", (effect_id,))
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            if imported:
                row = db.execute("SELECT * FROM effects WHERE id=? AND attempt=?", (effect_id, attempt)).fetchone()
                ts.require(row is not None and row["plan"] == ts.encoded(value).decode()
                           and row["fingerprint"] == digest(ts.encoded(value)),
                           "conflict", "Import abort journal changed before execution.")
                validate_abort_journal(self, authority, db, row)
            self._held(db, authority, attempt, value)
            ts.require(db.execute("SELECT state FROM effects WHERE id=?", (effect_id,)).fetchone()[0] == "rolling-back",
                       "conflict", "Rollback state changed before execution.")
            if imported:
                self._import_abort_preflight(authority, value)
            preserved, writes = [], 0
            for change in reversed(value["changes"]):
                authority.check([change["path"]])
                actual = digest(authority.root.read(change["path"]))
                if actual == change["before"]:
                    continue
                if actual != digest(decode(change["after"])):
                    ts.require(not imported, "stale", "Import abort path diverged during execution.")
                    preserved.append(change["path"])
                    continue
                authority.root.replace(change["path"], actual, decode(change["backup"]))
                writes += 1
            if imported:
                ts.require(all(digest(authority.root.read(c["path"])) == c["before"] for c in value["changes"]),
                           "stale", "Import abort baseline changed before verification.")
                return {"outcome": "abort-awaiting-reconcile", "writes": writes, "preserved": []}
            db.execute("UPDATE effects SET state='rolled-back' WHERE id=?", (effect_id,))
            return {"outcome": "rolled-back-with-preserved-edits" if preserved else "rolled-back",
                    "preserved": preserved, "writes": writes}

    @staticmethod
    def _import_abort_preflight(authority, value):
        for change in value["changes"]:
            authority.check([change["path"]])
            ts.require(digest(authority.root.read(change["path"])) in
                       {change["before"], digest(decode(change["after"]))},
                       "stale", "Import abort requires every path at its frozen before or after image.")

    def release(self, authority, attempt, generation, expected: dict):
        authority.check(expected)
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            ts.require(db.execute("SELECT 1 FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back')", (attempt,)).fetchone() is None,
                       "unknown", "Unfinished effects retain protection.")
            for name, fingerprint in expected.items():
                row = db.execute("SELECT * FROM reservations WHERE root=? AND path=? AND attempt=?",
                                 (self._root(authority), name, attempt)).fetchone()
                ts.require(row is not None, "conflict", "Reservation is not owned by this attempt.")
                ts.require(digest(authority.root.read(name)) == fingerprint, "stale", "Release source changed.")
                # Only original bytes or the last completed attributable effect can clear a path.
                attributable = row["baseline"]
                for effect in db.execute("SELECT plan FROM effects WHERE attempt=? AND state IN ('completed','reviewed-external') ORDER BY rowid", (attempt,)):
                    entry = ts.loads(effect[0])
                    for reviewed in entry.get("evidence", []):
                        if reviewed["path"] == name:
                            attributable = reviewed["reviewed"]
                    for change in entry["changes"]:
                        if change["path"] == name:
                            attributable = digest(decode(change["after"]))
                ts.require(fingerprint == attributable, "conflict", "Unattributed dirty work remains protected.")
                db.execute("DELETE FROM reservations WHERE root=? AND path=? AND attempt=?",
                           (self._root(authority), name, attempt))

            if hasattr(authority, "retire"):
                authority.retire(db, attempt)

    def closeout(self, authority, attempt, generation, expected, checkpoint):
        """Release reviewed attributable paths and pause in one journal transaction.

        Unselected reservations remain protected and are explicitly returned.
        """
        body = ts.encoded(checkpoint)
        ts.require(len(body) <= 8192 and isinstance(expected, dict), "invalid", "Closeout needs fingerprints and a checkpoint <=8 KiB.")
        authority.check(expected)
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            ts.require(db.execute("SELECT 1 FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back')", (attempt,)).fetchone() is None,
                       "unknown", "Reconcile unfinished effects before closeout.")
            for name, fingerprint in expected.items():
                row = db.execute("SELECT * FROM reservations WHERE root=? AND path=? AND attempt=?",
                                 (self._root(authority), name, attempt)).fetchone()
                ts.require(row is not None, "conflict", "Reservation is not owned: " + name)
                ts.require(digest(authority.root.read(name)) == fingerprint, "stale", "Release source changed: " + name)
                attributable = row["baseline"]
                for effect in db.execute("SELECT plan FROM effects WHERE attempt=? AND state IN ('completed','reviewed-external') ORDER BY rowid", (attempt,)):
                    entry = ts.loads(effect[0])
                    for reviewed in entry.get("evidence", []):
                        if reviewed["path"] == name:
                            attributable = reviewed["reviewed"]
                    for change in entry["changes"]:
                        if change["path"] == name:
                            attributable = digest(decode(change["after"]))
                ts.require(fingerprint == attributable, "conflict", "Unattributed dirty work remains protected: " + name)
                db.execute("DELETE FROM reservations WHERE root=? AND path=? AND attempt=?", (self._root(authority), name, attempt))
            remaining = [r[0] for r in db.execute("SELECT path FROM reservations WHERE attempt=? ORDER BY path", (attempt,))]
            retired = authority.retire(db, attempt) if hasattr(authority, "retire") else []
            db.execute("UPDATE attempts SET checkpoint=?,state='paused',generation=generation+1 WHERE id=?", (body.decode(), attempt))
        return {"outcome": "paused-with-reservations" if remaining else "closed", "generation": generation + 1,
                "remainingReservations": remaining, "released": sorted(expected), "retired_grants": retired}

    def review_external(self, authority, attempt, generation, expected, reason):
        """Explicitly acknowledge exact externally authored bytes without attributing their creation.

        Receipt is durable in the existing effect journal; repository bytes are not changed.
        A new baseline permits later release, while the receipt retains the old baseline.
        """
        ts.string(reason)
        ts.require(isinstance(expected, dict) and 0 < len(expected) <= 16, "invalid", "Review selects 1..16 exact paths.")
        authority.check(expected)
        with self._db() as db:
            self._attempt(db, authority, attempt, generation)
            ts.require(db.execute("SELECT 1 FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back')", (attempt,)).fetchone() is None,
                       "unknown", "Reconcile unfinished effects before external review.")
            evidence = []
            for name, fingerprint in expected.items():
                row = db.execute("SELECT * FROM reservations WHERE root=? AND path=? AND attempt=?",
                                 (self._root(authority), name, attempt)).fetchone()
                ts.require(row is not None, "conflict", "Review requires an owned reservation: " + name)
                ts.require(digest(authority.root.read(name)) == fingerprint, "stale", "Reviewed external bytes changed: " + name)
                evidence.append({"path": name, "priorBaseline": row["baseline"], "reviewed": fingerprint})
            receipt = {"changes": [], "kind": "external-review", "actor": authority.actor,
                       "reason": reason, "evidence": evidence, "at": time.time(),
                       "qualification": "Reviewed external bytes; creator and original write remain unattributed."}
            raw = ts.encoded(receipt).decode()
            receipt_id = "external-review-" + uuid.uuid4().hex
            db.execute("INSERT INTO effects VALUES(?,?,?,?,?)", (receipt_id, attempt, digest(raw.encode()), raw, "reviewed-external"))
            for item in evidence:
                db.execute("UPDATE reservations SET baseline=? WHERE root=? AND path=? AND attempt=?",
                           (item["reviewed"], self._root(authority), item["path"], attempt))
        return {"outcome": "reviewed-external", "receiptID": receipt_id, "receipt": receipt, "writes": 0}
