"""Explicit device-local repository choices; no scanning, fetching or task copies."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
import uuid

from . import tokenslang as ts
from . import home as eutonos_home
from .effects import digest
from .local_lock import local_lock
from .source import GitView

PROFILE = "eutonos.device-repositories.v1"
REQUEST = "eutonos.device-repositories.update.v1"
PATH = "device-repositories.json"
IDENTITY = ("repository_id", "scope_id", "namespace")
MAX_BYTES = 2 * 1024 * 1024
HISTORY_LIMIT = 4096


def identity(value):
    ts.shape(value, set(IDENTITY))
    return {key: ts.string(value[key]) for key in IDENTITY}


def _home(home=None):
    from .private_scope import validate_private_destination
    selected = Path(home) if home else eutonos_home.home()
    ts.require(validate_private_destination(selected)["status"] == "allowed", "denied",
               "Device enrollment requires owner-local storage outside shared repositories.")
    return selected.resolve()


def _locator(value):
    from .private_scope import require_ordinary_root
    ts.string(value)
    ts.require(Path(value).is_absolute() and not any(ord(c) < 32 for c in value),
               "invalid", "Select an absolute repository locator.")
    return str(require_ordinary_root(value))


def _read_file(path):
    if not path.exists():
        return None
    ts.require(not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_BYTES,
               "invalid", "Device enrollment must be a bounded ordinary file.")
    raw = path.read_bytes()
    ts.require(len(raw) <= MAX_BYTES, "invalid", "Device enrollment exceeds the read bound.")
    return raw


def _load(home=None):
    raw = _read_file(_home(home) / PATH)
    if raw is None:
        return {"profile": PROFILE, "cache_generation": 0, "shared": [], "personal": None}, None
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("invalid", "Device enrollment must be UTF-8.") from exc
    ts.shape(value, {"profile", "cache_generation", "shared", "personal"})
    ts.require(value["profile"] == PROFILE, "unsupported", "Unknown device enrollment profile.")
    ts.require(type(value["cache_generation"]) is int and value["cache_generation"] >= 0,
               "invalid", "Invalid device cache generation.")
    ts.require(isinstance(value["shared"], list), "invalid", "Shared enrollment must be an array.")
    seen = set()
    for entry in value["shared"]:
        ts.shape(entry, {"identity", "locators", "selected_locator", "enabled", "association"})
        identity(entry["identity"])
        ts.require(isinstance(entry["locators"], list) and bool(entry["locators"]), "invalid", "Enrollment needs explicit locators.")
        locators = [_locator(path) for path in entry["locators"]]
        ts.require(locators == entry["locators"] and len(set(locators)) == len(locators)
                   and not seen.intersection(locators), "ambiguous", "Enrollment locators must be unique.")
        seen.update(locators)
        ts.require(entry["selected_locator"] in locators and type(entry["enabled"]) is bool,
                   "invalid", "Select one enrolled source explicitly.")
        ts.require(isinstance(entry["association"], dict) and set(entry["association"]) == set(locators),
                   "invalid", "Each source needs inspected association provenance.")
        for proof in entry["association"].values():
            ts.shape(proof, {"kind", "common_git_dir", "commit", "identity_sha256"})
            ts.require(proof["kind"] in {"native-root", "git-common", "commit-lineage"}, "invalid", "Invalid association kind.")
            _locator(proof["common_git_dir"])
            ts.require(isinstance(proof["commit"], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", proof["commit"]),
                       "invalid", "Invalid association source pin.")
            ts.require(proof["identity_sha256"] == digest(ts.encoded(entry["identity"])),
                       "invalid", "Association identity differs from enrollment.")
    if value["personal"] is not None:
        ts.shape(value["personal"], {"identity", "locator", "owner_id"})
        identity(value["personal"]["identity"])
        _locator(value["personal"]["locator"])
        ts.string(value["personal"]["owner_id"])
    return value, digest(raw)


def read(*, home=None, owner_id=None):
    value, revision = _load(home)
    shown = deepcopy(value)
    personal = shown["personal"]
    if personal and (not owner_id or owner_id != personal["owner_id"]):
        shown["personal"] = {"outcome": "denied"}
    return {**shown, "outcome": "available", "revision": revision}


def native_identity(root, *, owner_id=None):
    """Inspect only the explicit native identity, with private denial before owners."""
    from .private_scope import is_private, require_owner, require_shared, require_ordinary_root
    root = require_ordinary_root(root)
    if is_private(root):
        return identity(require_owner(root, owner_id)["identity"])
    require_shared(root)
    raw = _read_file(root / "eutonos.read.json")
    ts.require(raw is not None, "missing", "Native repository identity is unavailable.")
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("invalid", "Native identity is invalid.") from exc
    ts.require(isinstance(value, dict) and value.get("profile") == ts.CATALOG,
               "invalid", "Native identity catalog is invalid.")
    return identity({key: value.get(key) for key in IDENTITY})


def inspect(root):
    """Read bounded local Git evidence; never run network or repair operations."""
    root = Path(_locator(str(root)))
    ident = native_identity(root)
    view = GitView(root, "HEAD")
    raw = view._git("rev-parse", "--git-common-dir").decode("utf-8").strip()
    common = Path(raw)
    common = (common if common.is_absolute() else root / common).resolve()
    return ident, {"kind": "native-root", "common_git_dir": str(common),
                   "commit": view.commit, "identity_sha256": digest(ts.encoded(ident))}, view


def _commit_identity(view, commit):
    try:
        raw = view._git("show", commit + ":eutonos.read.json", limit=MAX_BYTES)
        value = ts.loads(raw.decode("utf-8"))
        return identity({key: value.get(key) for key in IDENTITY})
    except (ts.ReadError, ValueError, UnicodeError, OSError):
        return None


def _association(locator, anchor, ident, observation, view):
    other_ident, other, other_view = inspect(anchor)
    ts.require(ident == other_ident, "conflict", "Repository identities differ.")
    if os.path.normcase(observation["common_git_dir"]) == os.path.normcase(other["common_git_dir"]):
        return {**observation, "kind": "git-common"}
    left = view._git("rev-list", "--max-count=" + str(HISTORY_LIMIT), "HEAD").decode("ascii").splitlines()
    right = set(other_view._git("rev-list", "--max-count=" + str(HISTORY_LIMIT), "HEAD").decode("ascii").splitlines())
    for commit in left:
        if commit in right and _commit_identity(view, commit) == ident and _commit_identity(other_view, commit) == ident:
            return {**observation, "kind": "commit-lineage", "commit": commit}
    raise ts.ReadError("ambiguous", "No inspected common native lineage proves this source association.")


def verify(entry):
    """Verify the selected locator against its stored proof; offline is unknown."""
    locator = entry["selected_locator"]
    if not entry["enabled"]:
        return "denied"
    try:
        ident, observed, view = inspect(locator)
        if ident != entry["identity"]:
            return "stale"
        proof = entry["association"][locator]
        if proof["kind"] == "git-common" and os.path.normcase(proof["common_git_dir"]) != os.path.normcase(observed["common_git_dir"]):
            return "stale"
        if view._git("cat-file", "-t", proof["commit"], allow_missing=True).strip() != b"commit":
            return "unknown"
        if view._git("merge-base", proof["commit"], "HEAD", allow_missing=True).decode("ascii").strip() != proof["commit"]:
            return "stale"
        return "verified"
    except ts.ReadError as exc:
        return "denied" if exc.code == "denied" else "unknown"
    except (KeyError, OSError, ValueError):
        return "unknown"


def form(operation, draft=None, *, home=None, owner_id=None):
    ts.require(operation in {"enroll", "relocate", "withdraw"}, "unsupported", "Unsupported enrollment operation.")
    ts.require(draft is None or isinstance(draft, dict), "invalid", "Enrollment draft must be an object.")
    if draft is not None:
        ts.shape(draft, set(), {"profile", "operation", "kind", "locator", "owner_id", "new_locator", "associate_with"})
    request = {"kind": "shared", **deepcopy(draft or {}), "profile": REQUEST, "operation": operation}
    ts.require(request["kind"] in {"shared", "personal"}, "invalid", "Invalid enrollment kind.")
    if request["kind"] == "personal" and owner_id:
        ts.require(request.get("owner_id") in {None, owner_id}, "denied", "Personal selection is unavailable.")
        request.setdefault("owner_id", owner_id)
    if request["kind"] == "personal" and request.get("locator"):
        selected, _ = _load(home)
        if operation == "withdraw":
            personal = selected["personal"]
            ts.require(personal is not None and request.get("owner_id") == personal["owner_id"]
                       and _locator(request["locator"]) == personal["locator"],
                       "denied", "Personal selection is unavailable.")
        else:
            from .private_scope import require_owner
            target = request.get("new_locator") if operation == "relocate" else request["locator"]
            if target:
                require_owner(target, request.get("owner_id"))
    elif request["kind"] == "shared":
        from .private_scope import require_shared, require_ordinary_root
        for key in ("locator", "new_locator", "associate_with"):
            if request.get(key):
                require_shared(require_ordinary_root(request[key]))
    required = ["locator"] + (["new_locator"] if operation == "relocate" else []) + (["owner_id"] if request["kind"] == "personal" else [])
    current = read(home=home, owner_id=request.get("owner_id", owner_id))
    return {"profile": PROFILE, "outcome": "form", "request": request,
            "expected_revision": current["revision"], "missing_fields": [key for key in required if not request.get(key)],
            "current": current}


def _mutate(value, request):
    operation = request["operation"]
    kind = request.get("kind", "shared")
    locator = _locator(request["locator"])
    if kind == "personal":
        from .private_scope import require_owner
        owner = request.get("owner_id")
        selected = value["personal"]
        if operation != "enroll":
            ts.require(selected is not None and owner and selected["owner_id"] == owner,
                       "denied", "Personal selection is unavailable.")
            ts.require(selected["locator"] == locator, "denied", "Personal selection is unavailable.")
        if operation == "withdraw":
            value["personal"] = None
        else:
            target = _locator(request["new_locator"]) if operation == "relocate" else locator
            manifest = require_owner(Path(target), owner)
            ident = identity(manifest["identity"])
            if selected:
                ts.require(selected["owner_id"] == owner and selected["identity"] == ident,
                           "denied", "Personal selection is unavailable.")
            value["personal"] = {"identity": ident, "locator": target, "owner_id": owner}
        return
    entries = value["shared"]
    existing = next((entry for entry in entries if locator in entry["locators"]), None)
    if operation == "withdraw":
        ts.require(existing is not None, "missing", "Enrolled source is absent.")
        existing["locators"].remove(locator)
        existing["association"].pop(locator)
        if not existing["locators"]:
            entries.remove(existing)
        elif existing["selected_locator"] == locator:
            # A remaining source is not implicitly selected after withdrawal.
            existing["selected_locator"] = existing["locators"][0]
            existing["enabled"] = False
        return
    target = _locator(request["new_locator"]) if operation == "relocate" else locator
    ident, proof, view = inspect(target)
    if operation == "relocate":
        ts.require(existing is not None, "missing", "Enrolled source is absent.")
        ts.require(ident == existing["identity"], "conflict", "Relocated source identity differs.")
        ts.require(not any(target in entry["locators"] for entry in entries), "conflict", "New locator is already enrolled.")
        old = existing["association"][locator]
        view._git("merge-base", "--is-ancestor", old["commit"], "HEAD")
        ts.require(_commit_identity(view, old["commit"]) == ident, "ambiguous", "Relocation lineage is unverified.")
        proof = {**proof, "kind": "commit-lineage", "commit": old["commit"]}
        index = existing["locators"].index(locator)
        existing["locators"][index] = target
        existing["association"].pop(locator)
        existing["association"][target] = proof
        if existing["selected_locator"] == locator:
            existing["selected_locator"] = target
        return
    if existing:
        ts.require(ident == existing["identity"], "conflict", "Enrolled source identity changed.")
        ts.require(verify({**existing, "selected_locator": locator, "enabled": True}) == "verified",
                   "stale", "Enrolled association needs reconciliation before reselection.")
        existing["selected_locator"] = locator
        existing["enabled"] = True
        return
    anchor = request.get("associate_with")
    if anchor is not None:
        anchor = _locator(anchor)
        existing = next((entry for entry in entries if anchor in entry["locators"]), None)
        ts.require(existing is not None, "missing", "Association source is not enrolled.")
        ts.require(existing["identity"] == ident, "stale", "Enrolled association identity changed.")
        proof = _association(locator, anchor, ident, proof, view)
        existing["locators"].append(locator)
        existing["association"][locator] = proof
        existing["selected_locator"] = locator
        existing["enabled"] = True
    else:
        entries.append({"identity": ident, "locators": [locator], "selected_locator": locator,
                        "enabled": True, "association": {locator: proof}})


def update(request, *, expected_revision, home=None):
    ts.shape(request, {"profile", "operation", "locator"}, {"kind", "owner_id", "new_locator", "associate_with"})
    ts.require(request["profile"] == REQUEST and request["operation"] in {"enroll", "relocate", "withdraw"},
               "unsupported", "Unknown device enrollment request.")
    ts.require(request.get("kind", "shared") in {"shared", "personal"}, "invalid", "Invalid enrollment kind.")
    ts.require(("new_locator" in request) == (request["operation"] == "relocate"), "invalid", "Relocation requires a new locator.")
    ts.require("associate_with" not in request or request["operation"] == "enroll" and request.get("kind", "shared") == "shared",
               "invalid", "Association applies only to shared enrollment.")
    directory = _home(home)
    try:
        with local_lock(directory, timeout=5):
            value, revision = _load(home)
            ts.require(revision == expected_revision, "conflict", "Device enrollment changed; reread before updating.")
            _mutate(value, request)
            value["cache_generation"] += 1
            raw = ts.encoded(value) + b"\n"
            ts.require(len(raw) <= MAX_BYTES, "invalid", "Device enrollment exceeds the write bound.")
            temporary = directory / (PATH + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, directory / PATH)
            finally:
                temporary.unlink(missing_ok=True)
            result = read(home=home, owner_id=request.get("owner_id"))
            ts.require(result["revision"] == digest(raw), "conflict", "Enrollment readback changed.")
            return {**result, "outcome": "updated", "current_verified": True}
    except ts.ReadError:
        raise
    except (OSError, ValueError, TimeoutError) as exc:
        raise ts.ReadError("unavailable", "Device enrollment operation is unavailable.") from exc
