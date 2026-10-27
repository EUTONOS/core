"""Compatibility for retired local write grants.

Old grant tables and admission inventories remain journal history. They do not
authorize or restrict project writes. The coordinator's exact reservations,
generations, and stale-content checks remain the write protection.
"""

from . import tokenslang as ts
from .source import safe_path


def paths(values):
    ts.require(isinstance(values, (list, tuple, set, frozenset)), "invalid", "Supply exact paths.")
    return sorted({safe_path(value) for value in values})


def initialize(db):
    """Retained for old callers; new journals need no grant schema."""


def seed(db, root, actor, selected, expiry, legacy_sha=None):
    """Retired: do not import old admission paths as write grants."""


def add(db, root, actor, selected, expiry, attempt=None, shared=False):
    """Retired: do not issue write grants."""


def active(db, root, actor):
    return set()


def observe(database, root, actor):
    return set()


def claim(db, root, actor, attempt, selected):
    """Retired: reservations in the coordinator own concurrent scope."""


def check_attempt(db, root, actor, attempt, selected):
    """Retired: the coordinator checks the attempt's exact reservations."""


def retire(db, root, actor, attempt):
    """Never erase historical grants; they no longer affect recovery."""
    return []


def describe(db, root, actor):
    """Report legacy rows as history without treating them as authority."""
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='write_grants'").fetchone()
    retained = (db.execute("SELECT COUNT(*) FROM write_grants WHERE root=? AND actor=?", (root, actor)).fetchone()[0]
                if table else 0)
    return {"enforced": False, "active_paths": 0, "retained": retained,
            "temporary": 0, "shared": 0, "pending": 0, "expired": 0,
            "warnings": []}
