"""Local project identity and coordinator selection outside repository content.

The host account and admission directory are trusted. This is not protection
against that account, unmanaged editors, synchronization or a hostile filesystem.
Repository payloads cannot create or enlarge this admission. Historical expiry
and grant fields do not control project writes.
"""
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
import json
import math
import sqlite3
import time

from . import tokenslang as ts, grants
from .effects import Coordinator, LocalRoot, digest
from .source import GitView, safe_path

PROFILE = "eutonos.tokenslang.local-host.v2"

def _validate(value, local):
    ts.require(isinstance(value, dict), "invalid", "Host admission must be an object.")
    base = {"profile", "root", "identity", "actor", "expires_at", "baseline_commit", "boundary", "grant_store"}
    ts.shape(value, base)
    ts.require(value["profile"] == PROFILE and value["boundary"] == "cooperative-local-account"
               and value["root"] == str(local.root) and value["identity"] == list(local.identity),
               "wrong-scope", "Host admission belongs to another project/root identity.")
    ts.string(value["actor"])
    expiry = value["expires_at"]
    # null means "no expiry" (IPC3); finite admissions written earlier stay valid as written.
    ts.require(expiry is None or type(expiry) in (int, float) and math.isfinite(expiry), "invalid", "Invalid admission expiry.")
    ts.require(value["grant_store"] == "coordinator.sqlite", "invalid", "Unsupported host grant store.")



def read_snapshot(root, directory, attempt=None, cursor=None):
    """Observe one enrolled project's coordinator without renewal or write locks."""
    local = LocalRoot(root)
    location = Path(directory).absolute()
    ts.require(not location.is_relative_to(local.root) and not local.root.is_relative_to(location),
               "denied", "Host directory must be separate from the project tree.")
    admission, database = location / "admission.json", location / "coordinator.sqlite"
    ts.require(admission.is_file() and database.is_file(), "unavailable", "Host admission or coordinator is missing.")
    try:
        value = json.loads(admission.read_text(encoding="utf-8"))
        _validate(value, local)
        selected_id = None if attempt is None else ts.string(attempt)
        ts.require(cursor is None or type(cursor) is int and cursor > 0,
                   "invalid", "Snapshot cursor must be a positive integer.")
        ts.require(selected_id is None or cursor is None, "invalid", "Select an exact attempt or an inventory cursor, not both.")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            ts.require(db.execute("PRAGMA user_version").fetchone()[0] in (1, 2)
                       and db.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                       "unavailable", "Coordinator is unsupported or damaged.")
            attempts = []
            root_id = ":".join(str(v) for v in local.identity)
            grant_state = grants.describe(db, root_id, value["actor"])
            if selected_id is None:
                if cursor is None:
                    rows = list(db.execute("SELECT rowid AS snapshot_rowid, * FROM attempts WHERE root=? ORDER BY rowid DESC LIMIT 257", (root_id,)))
                else:
                    rows = list(db.execute("SELECT rowid AS snapshot_rowid, * FROM attempts WHERE root=? AND rowid<? ORDER BY rowid DESC LIMIT 257", (root_id, cursor)))
                truncated = len(rows) > 256
                rows = rows[:256]
                next_cursor = rows[-1]["snapshot_rowid"] if truncated else None
            else:
                rows = list(db.execute("SELECT rowid AS snapshot_rowid, * FROM attempts WHERE root=? AND id=?", (root_id, selected_id)))
                truncated = False
                next_cursor = None
            for row in rows:
                item = dict(row)
                item.pop("snapshot_rowid")
                try: item["checkpoint"] = json.loads(item["checkpoint"] or "{}")
                except json.JSONDecodeError: item["checkpoint"] = {"unparsed": True}
                item["reservations"] = [dict(r) for r in db.execute(
                    "SELECT path,baseline FROM reservations WHERE root=? AND attempt=? ORDER BY path", (root_id, item["id"]))]
                item["unfinished_effects"] = [dict(r) for r in db.execute(
                    "SELECT id,state FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back') ORDER BY rowid", (item["id"],))]
                attempts.append(item)
        return {"status": "available", "actor": value["actor"], "expires_at": value["expires_at"],
                "attempts": attempts, "attempts_truncated": truncated,
                "next_cursor": next_cursor,
                "coverage": "selected-project-attempts; process-liveness-unobserved", "grants": grant_state}
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise ts.ReadError("unavailable", "Local coordinator could not be read safely.") from exc


def status(root, directory, actor=None, attempt=None, cursor=None):
    """Return a read-only, generation-aware recovery diagnosis.

    This deliberately observes through ``read_snapshot`` instead of ``connect``:
    a missing/corrupt coordinator is never opened for writing or recreated.
    The recommendation is advice only;
    callers must still invoke the named operation with its normal authority and
    preconditions.
    """
    selected_id = None if attempt is None else ts.string(attempt)
    snapshot = read_snapshot(root, directory, selected_id, cursor)
    owner = snapshot["actor"]
    ts.require(actor is None or actor == owner, "wrong-scope", "Host actor mismatch.")
    raw_attempts = snapshot["attempts"]
    if selected_id is not None:
        ts.require(raw_attempts, "missing", "Attempt is not present in this host journal.")
        ts.require(raw_attempts[0]["actor"] == owner, "wrong-scope", "Attempt belongs to another host actor.")
        attempts = raw_attempts
    else:
        attempts = [item for item in raw_attempts if item["actor"] == owner]

    def described(item):
        return {
            "id": item["id"],
            "state": item["state"],
            "generation": item["generation"],
            "reservations": [entry["path"] for entry in item["reservations"]],
            "unfinished_effects": item["unfinished_effects"],
        }

    described_attempts = [described(item) for item in attempts]
    chosen = described_attempts[0] if selected_id is not None else None
    expiry = snapshot["expires_at"]
    admission_state = ("no-expiry" if expiry is None
                       else "historical-expired" if time.time() >= expiry else "historical-current")
    if chosen is None:
        recommendation = {
            "operation": None,
            "arguments": {},
            "state": "selection-required",
            "allowed_operations": ["status"],
            "reason": "No attempt was selected; choose an exact attempt ID before acting on its journal state.",
        }
    elif chosen["state"] == "paused" and not chosen["reservations"] and not chosen["unfinished_effects"]:
        recommendation = {
            "operation": None,
            "arguments": {},
            "state": "ready",
            "allowed_operations": ["start"],
            "reason": "The paused attempt retains no reservation or unfinished effect; no recovery is needed and new work may start a separate attempt.",
        }
    elif chosen["state"] == "paused":
        recommendation = {
            "operation": "resume",
            "arguments": {"attempt": chosen["id"], "generation": chosen["generation"]},
            "reason": "The selected attempt is paused; resume advances its generation before a new effect.",
        }
    elif chosen["unfinished_effects"]:
        effect = chosen["unfinished_effects"][0]
        recommendation = {
            "operation": "reconcile",
            "arguments": {"attempt": chosen["id"], "generation": chosen["generation"], "effect_id": effect["id"]},
            "reason": "An effect has an unresolved journal state; reconcile it before retrying or releasing work.",
        }
    else:
        recommendation = {
            "operation": None,
            "arguments": {"attempt": chosen["id"], "generation": chosen["generation"]},
            "state": "ready",
            "allowed_operations": ["reserve", "checkpoint", "apply", "closeout", "review-external", "release"],
            "reason": "The selected active attempt has no unfinished effect; its next transition remains subject to normal preconditions.",
        }
    return {
        "outcome": "status",
        "admission": {"actor": owner, "expires_at": snapshot["expires_at"],
                      "state": admission_state},
        "attempts": described_attempts,
        "attempts_truncated": snapshot["attempts_truncated"],
        "next_cursor": snapshot["next_cursor"],
        "recommendation": recommendation,
        "coverage": snapshot["coverage"],
        "grants": snapshot["grants"],
    }


@dataclass(frozen=True)
class ProjectAuthority:
    root: LocalRoot
    actor: str
    paths: frozenset[str]
    expires_at: float | None
    admission: LocalRoot
    receipt: bytes

    def check(self, paths):
        ts.require(self.admission.read("admission.json") == self.receipt,
                   "denied", "Host admission changed; reload it explicitly.")
        self.root.path("eutonos.read.json")
        for name in paths:
            self.root.path(name)

    def claim(self, db, attempt, paths):
        grants.claim(db, Coordinator._root(self), self.actor, attempt, paths)

    def check_attempt(self, db, attempt, paths):
        grants.check_attempt(db, Coordinator._root(self), self.actor, attempt, paths)

    def retire(self, db, attempt):
        return grants.retire(db, Coordinator._root(self), self.actor, attempt)


def enroll(root, directory, actor, paths, *, hours=8, attempt=None, shared=False):
    """Bind an external coordinator to one local root and actor.

    ``hours=None`` writes an admission with ``expires_at: null`` (no expiry).
    """
    ts.require(type(shared) is bool and not (shared and attempt is not None), "invalid", "Choose task grants or shared grants.")
    if attempt is not None: ts.string(attempt)
    local = LocalRoot(root)
    view = GitView(local.root, "HEAD")
    location = Path(directory).absolute()
    ts.require(not location.is_relative_to(local.root) and not local.root.is_relative_to(location),
               "denied", "Host directory must be separate from the project tree.")
    ts.require(not location.exists(), "conflict", "Host directory already exists; do not reset its journal.")
    LocalRoot(location.parent).path(location.name)
    ts.string(actor)
    ts.require(hours is None or type(hours) in (int, float) and math.isfinite(hours) and 0 < hours <= 720,
               "invalid", "Admission lifetime must be positive and at most 30 days.")
    selected = grants.paths(paths)
    for name in selected:
        local.path(name)
    expiry = None if hours is None else time.time() + hours * 3600
    value = {"profile": PROFILE, "root": str(local.root), "identity": list(local.identity),
             "actor": actor, "grant_store": "coordinator.sqlite", "expires_at": expiry,
             "baseline_commit": view.commit, "boundary": "cooperative-local-account"}
    location.mkdir()
    Coordinator.initialize(location / "coordinator.sqlite")
    with (location / "admission.json").open("xb") as stream:
        stream.write(ts.encoded(value) + b"\n")
    return {"outcome": "enrolled", "host": str(location), "baseline_commit": view.commit,
            "paths": [], "expires_at": expiry, "grants": {"enforced": False, "retained": 0}}


def connect(root, directory, actor=None, *, _renewal=False):
    local, admission = LocalRoot(root), LocalRoot(directory)
    ts.require(not admission.root.is_relative_to(local.root) and not local.root.is_relative_to(admission.root),
               "denied", "Host directory must be separate from the project tree.")
    raw = admission.read("admission.json")
    ts.require(raw is not None, "unavailable", "Host admission is missing.")
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("invalid", "Host admission is not UTF-8.") from exc
    _validate(value, local)
    owner = value["actor"]
    ts.require(actor is None or actor == owner, "wrong-scope", "Host actor mismatch.")
    coordinator = Coordinator(admission.path("coordinator.sqlite"))
    authority = ProjectAuthority(local, owner, frozenset(), value["expires_at"], admission, raw)
    authority.check(())
    return coordinator, authority


def selected_apply(coordinator, authority, attempt, generation, effect_id, value, fingerprint,
                   *, _import_derivation=None):
    ts.require(digest(ts.encoded(value)) == fingerprint, "denied", "Selected effect fingerprint differs.")
    return coordinator.apply(authority, attempt, generation, effect_id, value,
                             **({"_import_derivation": _import_derivation} if _import_derivation is not None else {}))


def renew(root, directory, actor, paths, hours=8, drop_paths=(), *, attempt=None, shared=False):
    """Retired command; validate old inputs but grant no authority."""
    _, authority = connect(root, directory, actor)
    ts.require(type(hours) in (int, float) and math.isfinite(hours) and 0 < hours <= 720, "invalid", "Choose up to 30 days.")
    if attempt is not None: ts.string(attempt)
    for name in grants.paths(paths) + grants.paths(drop_paths):
        authority.root.path(name)
    return {"outcome": "retired", "reason": "Write grants and renewal are no longer required.",
            "paths": [], "journal": "preserved"}


def abandon_lease(root, directory, actor, attempt):
    """Retired command; leave journal and historical grants untouched."""
    ts.string(attempt)
    coordinator, authority = connect(root, directory, actor)
    with coordinator._db() as db:
        reservations = db.execute(
            "SELECT COUNT(*) FROM reservations WHERE root=? AND attempt=?",
            (coordinator._root(authority), attempt)).fetchone()[0]
        unfinished = db.execute(
            "SELECT COUNT(*) FROM effects WHERE attempt=? AND state IN ('prepared','rolling-back')",
            (attempt,)).fetchone()[0]
        if reservations or unfinished:
            return {"outcome": "retained", "reason": "recovery-state", "reservations": reservations,
                    "unfinished_effects": unfinished, "retired_grants": []}
    return {"outcome": "retired", "retired_grants": [], "grants": {"enforced": False}}
