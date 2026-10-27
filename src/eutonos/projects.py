"""Per-project hosts and the `H/projects.json` registry (IPC2 contract a).

`add` binds one project root to a host under `H/hosts/`. A single existing host whose
admission names this exact root is adopted as it is (the EUTONOS repository
adopts `hosts/eutonos`); several matching hosts are refused as `ambiguous` and
`add(host=...)` picks one; with none, a new host is enrolled with no expiry. A
host is never reset, renamed or deleted, and never created inside the project
tree. `hosts_report` only lists; it changes nothing.

Every read-modify-write of the registry runs under the projects lock (a `local_lock`
beside the registry, IPC5 F8). `remove` unregisters a root; `retire_host` archives a
host with a small marker file and never touches its admission, journal or name.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import home, tokenslang as ts
from .effects import LocalRoot
from .host import enroll, read_snapshot
from .local_lock import local_lock

PROFILE = "eutonos.projects.v1"
RETIRED = "eutonos.host-retired.v1"
RETIRED_FILE = "retired.json"
DEFAULT_ACTOR = "eutonos-agent"
_REGISTRY_LIMIT = 2 * 1024 * 1024
LOCK_SECONDS = 30


def _abs(root):
    return os.path.abspath(root)


def _real(path):
    """Resolved and case-folded, so a junction, `subst` drive or 8.3 short name compares equal to the real path."""
    return os.path.normcase(os.path.realpath(path))


def _same(left, right):
    return _real(left) == _real(right)


def project_key(root):
    """slug(basename)[:24] + "-" + sha256(normcase(abs root))[:12]."""
    absolute = _abs(root)
    slug = re.sub(r"[^a-z0-9]+", "-", os.path.basename(absolute.rstrip("\\/")).lower()).strip("-")[:24].strip("-") or "project"
    return slug + "-" + hashlib.sha256(os.path.normcase(absolute).encode("utf-8")).hexdigest()[:12]


def lock_root():
    """The folder `local_lock` is taken on for the registry; repository operations hold this, not the machine lock."""
    return home.home() / "projects.lock"


@contextmanager
def locked(timeout=None):
    """Hold the projects lock (reentrant in one thread). A busy lock is a refusal, not a hang."""
    try:
        with local_lock(lock_root(), timeout=LOCK_SECONDS if timeout is None else timeout):
            yield
    except TimeoutError as exc:
        raise ts.ReadError("unavailable", "Project registry is busy; try again.") from exc


def load():
    """The registry, or an empty one when absent. A damaged file is an error, never overwritten."""
    path = home.projects_file()
    if not path.is_file():
        return {"profile": PROFILE, "projects": []}
    try:
        if path.stat().st_size > _REGISTRY_LIMIT:
            raise ValueError("registry exceeds its size bound")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ts.ReadError("invalid", "Project registry is unreadable; fix or remove it by hand.") from exc
    ts.require(isinstance(value, dict) and value.get("profile") == PROFILE and isinstance(value.get("projects"), list)
               and all(isinstance(item, dict) for item in value["projects"])
               and isinstance(value.get("retired_hosts", []), list),
               "invalid", "Project registry has an unsupported shape.")
    return value


def _save(value):
    path = home.projects_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temporary, path)


def lookup(root):
    for item in load()["projects"]:
        if isinstance(item.get("root"), str) and _same(item["root"], root):
            return item
    return None


def resolve_host(root):
    """The registered host directory for this root, or None. Never raises."""
    try:
        item = lookup(root)
        host = item.get("host") if item else None
        return host if isinstance(host, str) and Path(host).is_dir() else None
    except (OSError, ValueError, ts.ReadError):
        return None


_ADMISSION_LIMIT = 65536


def _names_root(directory, local):
    """Cheap pre-filter: does `admission.json` name this root and identity? Opens no database."""
    path = Path(directory) / "admission.json"
    try:
        if path.stat().st_size > _ADMISSION_LIMIT:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(value, dict) and isinstance(value.get("root"), str) and _same(value["root"], local.root)
            and value.get("identity") == list(local.identity))


def _adoptable(local):
    """Every host under `H/hosts/` that belongs to this root, as `[(directory, snapshot)]`, sorted by name."""
    parent = home.hosts()
    if not parent.is_dir():
        return []
    found = []
    for directory in sorted(path for path in parent.iterdir() if path.is_dir()):
        if not _names_root(directory, local):
            continue
        try:
            found.append((directory, read_snapshot(local.root, directory)))
        except (OSError, ValueError, ts.ReadError):
            continue
    return found


def _host_directory(value):
    """A `--host` value: absolute, else under the EUTONOS home (`hosts/NAME` or a bare `NAME`), else relative to cwd."""
    path = Path(value)
    if not path.is_absolute():
        for base in (home.hosts(), home.home()):
            if (base / path).is_dir():
                return (base / path).absolute()
    return path.absolute()


def add(root, actor=None, host=None):
    """Register a project root; idempotent. Returns the registry entry and how its host was obtained.

    With `host`, that directory is adopted if its admission is this exact root (otherwise the read refuses). With no
    `host`, one matching host is adopted, several give outcome `ambiguous` and nothing is written. The whole
    operation holds the projects lock, so two concurrent adds neither lose an entry nor enroll one root twice."""
    local = LocalRoot(root)
    parent = home.hosts().absolute()  # before the lock folder exists: nothing is created inside the project tree
    ts.require(not parent.is_relative_to(local.root) and not local.root.is_relative_to(parent),
               "denied", "Host directory must be separate from the project tree.")
    with locked():
        return _add(root, actor, host)


def _add(root, actor, host):
    local = LocalRoot(root)
    chosen = None if host is None else _host_directory(host)
    existing = lookup(local.root)
    if existing is not None and isinstance(existing.get("host"), str) and Path(existing["host"]).is_dir():
        ts.require(chosen is None or _same(chosen, existing["host"]), "conflict",
                   "This project is already bound to another host; nothing was changed.")
        return {"outcome": "already-added", "project": existing, "adopted_host": None}
    if chosen is not None:
        ts.require(chosen.is_dir(), "missing", "The host directory does not exist.")
        found = [(chosen, read_snapshot(local.root, chosen))]  # refuses a host that is not this root's
    else:
        found = _adoptable(local)
    if len(found) > 1:
        return {"outcome": "ambiguous", "project_key": project_key(local.root),
                "candidates": [{"host": str(directory.absolute()), "actor": snapshot["actor"],
                                "expires_at": snapshot["expires_at"]}
                               for directory, snapshot in found],
                "next": "Several hosts are enrolled for this root. Choose one with add-project --host DIR.",
                "changed": False}
    if found:
        directory, snapshot = found[0]
        host, adopted, owner = str(directory.absolute()), True, snapshot["actor"]
        if host_state(directory) == "retired":  # a detached project coming back: its archived host is adopted again
            _mark_host(directory, "reinstated", local.root)
    else:
        owner = actor or DEFAULT_ACTOR
        parent = home.hosts().absolute()
        ts.require(not parent.is_relative_to(local.root) and not local.root.is_relative_to(parent),
                   "denied", "Host directory must be separate from the project tree.")
        host = str(parent / project_key(local.root))
        home.hosts().mkdir(parents=True, exist_ok=True)
        enroll(local.root, host, owner, [], hours=None)
        adopted = False
    entry = {"root": str(local.root), "identity": list(local.identity), "host": host, "actor": owner,
             "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    registry = load()
    registry["projects"] = [item for item in registry["projects"]
                            if not (isinstance(item.get("root"), str) and _same(item["root"], local.root))] + [entry]
    registry["projects"].sort(key=lambda item: os.path.normcase(str(item.get("root"))))
    _save(registry)
    result = {"outcome": "added", "project": entry, "project_key": project_key(local.root), "adopted_host": adopted}
    if adopted and actor and actor != owner:
        result["requested_actor"] = actor
    return result


def remove(root):
    """Unregister a project root under the projects lock. The host directory is never touched (see `retire_host`)."""
    with locked():
        registry = load()
        gone = [item for item in registry["projects"] if isinstance(item.get("root"), str) and _same(item["root"], root)]
        if not gone:
            return {"outcome": "absent", "project": None, "changed": False}
        registry["projects"] = [item for item in registry["projects"] if item not in gone]
        _save(registry)
        return {"outcome": "removed", "project": gone[0], "changed": True}


def effects_for(root):
    """The permission Effects recorded for a project (what `permissions.apply` returned for it), or []."""
    item = lookup(root)
    effects = item.get("effects") if item else None
    return list(effects) if isinstance(effects, list) else []


def set_effects(root, effects):
    """Replace the project's recorded permission Effects, so a later removal can reverse exactly them."""
    ts.require(isinstance(effects, list) and all(isinstance(effect, dict) for effect in effects), "invalid",
               "Effects must be a list of Effect objects.")
    with locked():
        registry = load()
        for item in registry["projects"]:
            if isinstance(item.get("root"), str) and _same(item["root"], root):
                if effects:
                    item["effects"] = effects
                else:
                    item.pop("effects", None)
                _save(registry)
                return item
    raise ts.ReadError("missing", "The project is not registered; nothing was recorded.")


def _marker(directory):
    path = Path(directory) / RETIRED_FILE
    try:
        if path.stat().st_size > _ADMISSION_LIMIT:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"profile": RETIRED, "history": []}
    except (OSError, ValueError):
        return None
    ok = (isinstance(value, dict) and value.get("profile") == RETIRED and isinstance(value.get("history"), list)
          and all(isinstance(entry, dict) and entry.get("event") in ("retired", "reinstated") for entry in value["history"]))
    return value if ok else None


def host_state(directory):
    """`active`, or `retired` when the marker's last event is a retirement. An unreadable marker counts as retired."""
    value = _marker(directory)
    if value is None:
        return "retired"
    return "retired" if value["history"] and value["history"][-1]["event"] == "retired" else "active"


def _mark_host(directory, event, root, reason=None):
    value = _marker(directory)
    ts.require(value is not None, "invalid", "The host's retired marker is unreadable; fix or remove it by hand.")
    value["history"].append({"event": event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "root": None if root is None else str(root), "reason": reason})
    path = Path(directory) / RETIRED_FILE
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    temporary.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temporary, path)
    return value


def retire_host(host, *, root=None, reason=None):
    """Archive a host: write `retired.json` into it and record it in the registry. Nothing is deleted or renamed, and
    `admission.json` and the journal are not touched, so `read_snapshot` still validates. Idempotent."""
    directory = _host_directory(host)
    ts.require(directory.is_dir() and (directory / "admission.json").is_file(), "missing",
               "The host directory or its admission is missing; nothing was retired.")
    with locked():
        if host_state(directory) == "retired":
            return {"outcome": "already-retired", "host": str(directory), "changed": False}
        _mark_host(directory, "retired", root, reason)
        registry = load()
        registry.setdefault("retired_hosts", [])
        registry["retired_hosts"] = [item for item in registry["retired_hosts"]
                                     if not (isinstance(item, dict) and item.get("host") == str(directory))]
        registry["retired_hosts"].append({"host": str(directory), "root": None if root is None else str(root),
                                          "retired_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        _save(registry)
        return {"outcome": "retired", "host": str(directory), "changed": True}


def hosts_report(now=None):
    """Report-only: expired hosts and `runtime.backup-*` / upgrade folders under H. Deletes nothing."""
    now = time.time() if now is None else now
    base = home.home()
    expired, folders, unreadable = [], [], 0
    if home.hosts().is_dir():
        for directory in sorted(path for path in home.hosts().iterdir() if path.is_dir()):
            try:
                value = json.loads((directory / "admission.json").read_text(encoding="utf-8"))
                expiry = value["expires_at"]
                root = value.get("root")
            except (OSError, ValueError, KeyError, TypeError):
                unreadable += 1
                continue
            if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry <= now:
                expired.append({"host": str(directory), "root": root, "expires_at": expiry,
                                "root_exists": isinstance(root, str) and Path(root).is_dir()})
    if base.is_dir():
        for path in sorted(item for item in base.iterdir() if item.is_dir()):
            name = path.name.lower()
            if name.startswith("runtime.backup-") or "upgrade" in name:
                folders.append({"path": str(path), "kind": "runtime-backup" if name.startswith("runtime.backup-") else "upgrade"})
    return {"outcome": "report", "home": str(base), "expired_hosts": expired, "leftover_folders": folders,
            "unreadable_hosts": unreadable, "changed": False,
            "note": "Report only. Nothing was changed; an expired host still holds its journal and is never reset."}
