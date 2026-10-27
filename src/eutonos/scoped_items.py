"""Immutable native-item references with explicit source and privacy boundaries."""
from __future__ import annotations

from copy import deepcopy

from . import boards, device_repositories as device, tokenslang as ts
from .effects import LocalRoot, digest
from .source import GitView
from .work_lifecycle import item_revision

PROFILE = "eutonos.scoped-item-ref.v1"
RESULT = "eutonos.scoped-item-resolution.v1"


def validate(value):
    ts.shape(value, {"profile", *device.IDENTITY, "item_id"})
    ts.require(value["profile"] == PROFILE, "unsupported", "Unknown scoped item reference profile.")
    return {"profile": PROFILE, **device.identity({key: value[key] for key in device.IDENTITY}),
            "item_id": ts.string(value["item_id"])}


def _outcome(outcome, reference, *, coverage="unknown"):
    return {"profile": RESULT, "outcome": outcome, "reference": deepcopy(reference),
            "freshness": "unknown", "coverage": {"status": coverage, "scope": "selected-source"}}


def _shared_item(root, item_id):
    """Read configured native owners once, retaining duplicates and partial coverage."""
    local = LocalRoot(root)
    _, owners, raw_by_path, entries, diagnostics = boards._load(local)
    if owners is None:
        return None, None, "unknown", "unknown"
    matches = [(path, item) for path, board in entries.items() if board is not None
               for item in board["items"] if item["id"] == item_id]
    coverage = "partial" if diagnostics else "complete"
    if len(matches) > 1:
        return None, None, "ambiguous", coverage
    if not matches:
        return None, None, "unknown" if diagnostics else "missing", coverage
    path, item = matches[0]
    return item, {"path": path, "sha256": digest(raw_by_path[path])}, "resolved", coverage


def _selected(origin_private, ident, enrollment, owner_id):
    """Select an enrolled association, never a tuple-only or title fallback."""
    personal = enrollment.get("personal")
    if personal and personal.get("identity") == ident:
        if not origin_private or not owner_id or personal.get("owner_id") != owner_id:
            return None, "denied"
        return personal.get("locator"), "verified"
    matches = [entry for entry in enrollment.get("shared", []) if entry.get("identity") == ident]
    if len(matches) > 1:
        return None, "ambiguous"
    if not matches:
        return None, "missing"
    entry = matches[0]
    status = device.verify(entry)
    return entry.get("selected_locator") if status == "verified" else None, status


def resolve(root, reference, owner_id=None, enrollment=None):
    """Resolve exact current native meaning; observations never mutate the link.

    Same-root shared references need no enrollment. Cross-root references use the
    selected inspected enrollment. An authorized personal caller can read shared
    work, while a shared caller can never traverse into a personal scope.
    """
    reference = validate(reference)
    ident = {key: reference[key] for key in device.IDENTITY}
    from .private_scope import is_private, require_owner, require_shared, require_ordinary_root
    origin_private = is_private(root)
    try:
        root = require_ordinary_root(root)
        origin = device.native_identity(root, owner_id=owner_id)
        if origin != ident:
            # Internal lookup can recognize a private identity without exposing
            # its locator or owners in a public enrollment listing.
            enrollment = enrollment if enrollment is not None else device._load()[0]
            ts.require(isinstance(enrollment, dict) and enrollment.get("profile") == device.PROFILE,
                       "invalid", "Select a valid device enrollment.")
            locator, status = _selected(origin_private, ident, enrollment, owner_id)
            if status != "verified":
                return _outcome(status, reference)
            root = require_ordinary_root(locator)
        target_private = is_private(root)
        if target_private:
            if not origin_private:
                return _outcome("denied", reference)
            require_owner(root, owner_id)
        else:
            require_shared(root)
        if device.native_identity(root, owner_id=owner_id) != ident:
            return _outcome("stale", reference)
        if target_private:
            from .personal import read
            current = read(root, owner_id=owner_id, item_id=reference["item_id"])
            item = current["item"]
            owner = {"path": current["owner_path"], "sha256": current["owner_sha256"]}
            coverage = "complete"
        else:
            item, owner, outcome, coverage = _shared_item(root, reference["item_id"])
            if outcome != "resolved":
                return _outcome(outcome, reference, coverage=coverage)
        commit, observed = None, "unknown"
        try:
            view = GitView(root, "HEAD")
            commit = view.commit
            committed = view._git("show", commit + ":" + owner["path"], allow_missing=True)
            observed = "committed" if digest(committed) == owner["sha256"] else "working-tree"
        except (ts.ReadError, OSError, ValueError):
            pass
        # Recheck the source bytes and identity rather than returning a mixed pin.
        local = LocalRoot(root)
        if digest(local.read(owner["path"])) != owner["sha256"] or device.native_identity(root, owner_id=owner_id) != ident:
            return _outcome("stale", reference, coverage=coverage)
        native = {key: value for key, value in item.items() if key not in {"owner_path", "owner_sha256", "revision"}}
        return {"profile": RESULT, "outcome": "resolved", "reference": deepcopy(reference),
                "item": native, "item_revision": item_revision(native),
                "owner_path": owner["path"], "owner_sha256": owner["sha256"],
                "source_pin": {"identity": ident, "locator": str(root), "commit": commit,
                               "observation": observed, "owner_sha256": owner["sha256"]},
                "freshness": "current-observation", "coverage": {"status": coverage, "scope": "selected-source"}}
    except ts.ReadError as exc:
        outcome = exc.code if exc.code in {"missing", "denied", "ambiguous", "stale"} else "unknown"
        return _outcome(outcome, reference)
    except (OSError, ValueError, TypeError, KeyError):
        return _outcome("unknown", reference)


def check_shared_links(root, refs, previous=(), *, enrollment=None):
    """Admit observed shared targets, preserving issued unavailable references."""
    from .private_scope import require_shared
    require_shared(root)
    ts.require(isinstance(refs, list) and isinstance(previous, (list, tuple)),
               "invalid", "Scoped links must be arrays.")
    previous = {ts.encoded(validate(value)) for value in previous}
    result = []
    for value in refs:
        reference = validate(value)
        resolved = resolve(root, reference, enrollment=enrollment)
        outcome = resolved["outcome"]
        if outcome != "resolved":
            ts.require(outcome != "denied", "denied", "Shared links cannot target a private or denied source.")
            ts.require(ts.encoded(reference) in previous, outcome,
                       "New shared link requires an observed selected shared source.")
        result.append(reference)
    return result
