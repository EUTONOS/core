"""Local personal-scope routing boundary; no account or filesystem ACL system."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
from .hidden_process import no_window

from . import tokenslang as ts
from . import home as eutonos_home

MANIFEST = "eutonos.personal.json"
PROFILE = "eutonos.personal.v1"
OWNERS = {"goals": "goals.md", "projects": "projects.md", "priorities": "priorities.json"}
DENIED = "Private scope is unavailable to shared operations."


def _path(root):
    return Path(root if isinstance(root, (str, os.PathLike)) else root.root)


def is_private(root):
    """Presence alone denies shared reads, including malformed/linked markers."""
    try:
        (_path(root) / MANIFEST).lstat()
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True


def require_shared(root):
    if is_private(root):
        raise ts.ReadError("denied", DENIED)
    # Removing a tracked marker from the overlay does not make its HEAD public.
    path = _path(root)
    if (path / ".git").exists():
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        try:
            with subprocess.Popen(["git", "-c", "safe.directory=" + str(path), "-C", str(path),
                                   "ls-tree", "--name-only", "HEAD", "--", MANIFEST],
                                  env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, **no_window()) as process:
                try:
                    raw, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
        except (OSError, subprocess.SubprocessError):
            raise ts.ReadError("unavailable", "Shared scope eligibility is unavailable.") from None
        if process.returncode != 0:
            raise ts.ReadError("unavailable", "Shared scope eligibility is unavailable.")
        if raw.strip():
            raise ts.ReadError("denied", DENIED)


def _ordinary_path(path):
    """Inspect the original ancestor chain before resolution erases aliases."""
    original = Path(os.path.abspath(os.fspath(path)))
    for parent in (original, *original.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError()
    return original.resolve()


def require_ordinary_root(root):
    """Return the original-path-checked root, without granting native authority."""
    try:
        return _ordinary_path(_path(root))
    except (OSError, ValueError, TypeError, AttributeError):
        raise ts.ReadError("denied", "Selected root is unavailable or uses an unsupported alias.") from None


def require_shared_view(view):
    """Check both the current root and the pinned tree without reading owners."""
    require_shared(view.root)
    raw = view._git("ls-tree", "--name-only", view.commit, "--", MANIFEST)
    if raw.strip():
        raise ts.ReadError("denied", DENIED)


def require_owner(root, owner_id):
    """Validate the selected existing owner before any personal content read."""
    message = "Personal scope requires a valid selected owner."
    try:
        if (not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 256
                or any(ord(c) < 32 for c in owner_id)):
            raise ValueError()
        path = _ordinary_path(_path(root)) / MANIFEST
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size > 16384):
            raise ValueError()
        value = ts.loads(path.read_text(encoding="utf-8"))
        ts.shape(value, {"profile", "identity", "owner_id", "owners"})
        ts.shape(value["identity"], {"repository_id", "scope_id", "namespace"})
        if value["profile"] != PROFILE or value["owner_id"] != owner_id or value["owners"] != OWNERS:
            raise ValueError()
        for part in value["identity"].values():
            if not isinstance(part, str) or not part.strip() or len(part) > 256 or any(ord(c) < 32 for c in part):
                raise ValueError()
        if path.stat().st_mtime_ns != info.st_mtime_ns:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, ts.ReadError):
        raise ts.ReadError("denied", message) from None


def private_host(root, home=None):
    """Dedicated owner/identity storage; never reuse a shared repository host."""
    try:
        marker = _ordinary_path(_path(root)) / MANIFEST
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError()
        value = ts.loads(marker.read_text(encoding="utf-8"))
        manifest = require_owner(root, value.get("owner_id"))
        selected = _path(root).resolve()
        base = _ordinary_path(home if home is not None else eutonos_home.home())
        if base == selected or selected in base.parents:
            raise ValueError()
        key = hashlib.sha256(ts.encoded([manifest["identity"], manifest["owner_id"], os.path.normcase(str(selected))])).hexdigest()
        host = base / "private-hosts" / key
        if validate_private_destination(host, source_root=root)["status"] != "allowed":
            raise ValueError()
        return host
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, ts.ReadError):
        raise ts.ReadError("denied", "Private host selection is unavailable.") from None


def validate_private_destination(path, source_root=None):
    """Bound an explicitly selected local destination, excluding shared stores.

    This checks application routing only; independent-media/access evidence is
    supplied by the backup operation and is not inferred from a local pathname.
    """
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or raw.startswith(("\\", "//")) or "://" in raw:
            raise ValueError()
        target = _ordinary_path(raw)
        if source_root is not None:
            source = _path(source_root).resolve()
            if target == source or source in target.parents:
                raise ValueError()
        for parent in (target, *target.parents):
            if parent.exists():
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError()
            if parent.name.casefold() in {".git", "eutonos-delivery"}:
                raise ValueError()
            shared = any((parent / name).exists() for name in
                         ("eutonos.shared-work.json", "eutonos.delivery.json", "eutonos.read.json", "eutonos.boards.json"))
            if shared or (parent / ".git").exists() and not is_private(parent):
                raise ValueError()
        return {"status": "allowed", "boundary": "selected-local-private-destination", "independence": "unverified"}
    except (OSError, ValueError, TypeError):
        return {"status": "denied", "reason": "Private destination is unavailable or outside the supported boundary."}


is_private_root = is_private
require_shared_root = require_shared
