"""Bounded physical-board census for fixtures and connected native planning.

This is an optimistic precondition, not a lock against unmanaged filesystem
writers. Freeze before staging an overlay. Every tracked Markdown file counts,
including unconfigured and done owners and currently fence-free candidates.
Git reads are local through GitView; no filters, lazy fetch or peer callbacks.
"""
from __future__ import annotations

import re
import unicodedata

from . import boards, tokenslang as ts
from .effects import LocalRoot, MARKER, MARKER_BYTES, digest
from .source import GitView, safe_path

PROFILE = "eutonos.tokenslang.board-census.v1"
NATIVE_PROFILE = "eutonos.tokenslang.board-census.native.v1"
MAX_TRACKED = 4096
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_DESTINATIONS = 16
CONFIG = "eutonos.boards.json"
KEYS = {"profile", "root_identity", "head", "tracked", "destinations",
        "hashes", "entries", "by_id", "bounds"}


def _tracked(view):
    raw = view._git("ls-files", "--stage", "-z", limit=2 * 1024 * 1024)
    ts.require(not raw or raw.endswith(b"\0"), "conflict", "Incomplete Git tracked inventory.")
    rows, seen = [], set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        ts.require(len(rows) < MAX_TRACKED, "overflow", "Board census tracked-path coverage exceeds bounds.")
        try:
            metadata, name = record.split(b"\t", 1)
            mode, blob, stage = metadata.decode("ascii").split()
            name = safe_path(name.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise ts.ReadError("unsupported", "Unsupported Git tracked inventory record.") from error
        ts.require(stage == "0" and mode in {"100644", "100755"},
                   "unsupported", "Unmerged entries, links and submodules cannot establish census coverage.")
        ts.require(bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob)), "invalid", "Invalid tracked object identity.")
        key = unicodedata.normalize("NFC", name).casefold()
        ts.require(key not in seen, "ambiguous", "Case, Unicode or duplicate tracked-path collision.")
        seen.add(key)
        rows.append({"path": name, "mode": mode, "blob": blob})
    return sorted(rows, key=lambda row: row["path"])


def freeze(local, *, destinations=(), authority=None) -> dict:
    """Return the complete physical guard, including JSON-native entries/by_id.

    All selected destination parents must already exist. Missing final files are
    frozen as absent. No virtual overlay content is used as current occupancy.
    """
    local = LocalRoot(local.root, excluded=local.excluded)
    if authority is not None:
        from .board_import import check_context
        check_context(local, authority)
    destinations = list(destinations)
    ts.require(len(destinations) <= MAX_DESTINATIONS, "overflow", "Too many census destinations.")
    destinations = sorted({safe_path(path) for path in destinations})
    total = 0
    bodies = {}

    def read(path):
        nonlocal total
        if path in bodies:
            return bodies[path]
        parts, size = [], 0
        try:
            for part in local.iter_read(path):
                size += len(part)
                total += len(part)
                ts.require(size <= MAX_FILE_BYTES and total <= MAX_TOTAL_BYTES,
                           "overflow", "Board census byte coverage exceeds bounds.")
                parts.append(part)
        except FileNotFoundError:
            # iter_read has not opened a missing final path; changes during an
            # existing read raise conflict instead of becoming absent evidence.
            bodies[path] = None
        else:
            bodies[path] = b"".join(parts)
        return bodies[path]

    marker = read(MARKER)
    ts.require(marker == MARKER_BYTES or (marker is None and authority is not None),
               "denied", "Census imports require a fixture marker or connected native context.")
    profile = PROFILE if marker == MARKER_BYTES else NATIVE_PROFILE
    view = GitView(local.root, "HEAD")
    tracked = _tracked(view)
    raw_config = read(CONFIG)
    owners = []
    if raw_config is not None:
        try:
            config = ts.loads(raw_config.decode("utf-8"))
        except UnicodeError as error:
            raise ts.ReadError("unsupported", "Board census config must be UTF-8.") from error
        ts.shape(config, {"profile", "owners"})
        ts.require(config["profile"] == boards.CONFIG, "unsupported", "Unknown board owner config.")
        owners = [safe_path(path) for path in ts.sequence(config["owners"])]
        ts.require(owners and len(owners) == len(set(owners)), "ambiguous", "Configured owners must be unique and nonempty.")
    candidates = {row["path"] for row in tracked if row["path"].lower().endswith((".md", ".markdown"))}
    candidates.update(owners)
    candidates.update(destinations)
    ts.require(len(candidates) <= MAX_TRACKED + MAX_DESTINATIONS,
               "overflow", "Board census candidate coverage exceeds bounds.")
    entries, by_id = {}, {}
    for path in sorted(candidates):
        raw = read(path)
        ts.require(path not in owners or raw is not None, "missing", "Configured census owner is missing: " + path)
        if raw is None:
            entries[path] = None
            continue
        # _parse rejects a malformed sole fence; this additional count catches a
        # valid fence followed by a second incomplete opening as well.
        opening = re.findall(rb"^```tokenslang-board[^\r\n]*", raw, re.M)
        matches = list(boards.BLOCK.finditer(raw))
        ts.require(len(opening) == len(matches), "invalid", "Malformed board census fence: " + path)
        board, _ = boards._parse(raw, path)
        entries[path] = board
        if board is not None:
            for item in board["items"]:
                ts.require(item["id"] not in by_id, "ambiguous", "Duplicate issued ID in complete census: " + item["id"])
                by_id[item["id"]] = {"path": path, "item": item}
    # Recheck inventory and HEAD after reading bodies rather than certify a
    # snapshot taken across a detected concurrent Git/index change.
    ts.require(_tracked(view) == tracked and GitView(local.root, "HEAD").commit == view.commit,
               "stale", "Tracked inventory or HEAD changed during census.")
    local.path(MARKER)
    return {"profile": profile, "root_identity": list(local.identity), "head": view.commit,
            "tracked": tracked, "destinations": destinations,
            "hashes": {path: digest(body) for path, body in sorted(bodies.items())},
            "entries": entries, "by_id": by_id,
            "bounds": {"tracked": MAX_TRACKED, "file_bytes": MAX_FILE_BYTES,
                       "total_bytes": MAX_TOTAL_BYTES, "destinations": MAX_DESTINATIONS}}


def validate(local, guard, *, authority=None) -> None:
    """Fail closed unless current complete physical occupancy equals the guard."""
    ts.shape(guard, KEYS)
    ts.require(guard["profile"] in (PROFILE, NATIVE_PROFILE) and guard["root_identity"] == list(local.identity),
               "conflict", "Board census profile/root identity mismatch.")
    if guard["profile"] == NATIVE_PROFILE:
        from .board_import import check_context
        check_context(local, authority)
        current = freeze(local, destinations=ts.sequence(guard["destinations"]), authority=authority)
    else:
        current = freeze(local, destinations=ts.sequence(guard["destinations"]))
    ts.require(current == guard, "stale", "Complete board census precondition changed.")
