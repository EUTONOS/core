"""The update staging folder: ``<home>/updates/staged/<artifact_id>/`` holds a release that was downloaded and
verified in the background (``auto`` update mode), ready for the person's "Install now".

A staged release is exactly the release-folder layout ``release_source.FolderSource`` reads: the wheel, the manifest
``eutonos-release.json`` and its signature ``eutonos-release.json.sig``. That is deliberate. ``eutonos-install update
--staged`` installs through the ordinary ``update`` path with the staging folder as the source, so the signature (over
the manifest, against the pinned keys) and the wheel's sha256 are checked again at install time, and a staged folder
someone tampered with after staging is refused exactly as a bad download would be.

Rules:

* Fail closed. Only a release the source marked ``verified`` is staged (never ``unsigned-allowed``). A failed or partial
  download leaves nothing that looks complete: the files are written into a scratch folder beside ``staged``, checked
  again with ``FolderSource`` (signature, manifest, sha256), and only then renamed into place.
* Only the newest staged release is kept; ``prune`` removes the rest (and any scratch folder left by a crash).
* Nothing here installs anything or touches the network by itself: ``stage`` calls ``source.fetch_wheel`` once, and the
  caller chose the source.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time

from . import home as eutonos_home
from . import release_signing as sig
from . import release_source as rs
from . import tokenslang as ts

SCRATCH_PREFIX = "scratch-"
SCRATCH_STALE_SECONDS = 3600


def updates_root(home=None) -> Path:
    return (Path(home) if home is not None else eutonos_home.home()) / "updates"


def staged_root(home=None) -> Path:
    return updates_root(home) / "staged"


def staged_source(home=None, *, keys=None) -> rs.FolderSource:
    """The staging folder as an update source. Signature checks use the pinned keys (or ``keys``, for tests) and there
    is never an unsigned override."""
    return rs.FolderSource(staged_root(home), allow_unsigned=False, keys=keys)


def _remove(target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():  # a Windows file may be held for a moment
        time.sleep(0.2)
        shutil.rmtree(target, ignore_errors=True)


def releases(home=None, *, keys=None) -> list[dict]:
    """The verified releases in the staging folder (usually zero or one)."""
    return staged_source(home, keys=keys).releases()


def staged_ready(home, artifact_id: str, *, keys=None) -> bool:
    """Is a complete, verified copy of ``artifact_id`` staged? A cheap check (signature and manifest are read, the wheel's
    size is not recomputed; the install checks the sha256 again)."""
    for item in releases(home, keys=keys):
        manifest = item["manifest"]
        wheel = staged_root(home) / item["id"] / manifest["wheel"]
        if manifest["artifact_id"] == artifact_id and item.get("trust") == "verified" and wheel.is_file():
            return True
    return False


def prune(home=None, keep: str | None = None) -> list[str]:
    """Remove every staged release except the one named ``keep`` (an artifact id; none keeps nothing) and any stale
    scratch folder. Returns what was removed. Never raises for a missing folder or a file Windows holds."""
    removed = []
    base = staged_root(home)
    try:
        for item in sorted(base.iterdir()) if base.is_dir() else ():
            if item.is_dir() and item.name != keep:
                _remove(item)
                removed.append(item.name)
    except OSError:
        pass
    root = updates_root(home)
    try:
        for item in sorted(root.iterdir()) if root.is_dir() else ():
            if item.is_dir() and item.name.startswith(SCRATCH_PREFIX):
                if time.time() - item.stat().st_mtime > SCRATCH_STALE_SECONDS or keep is None:
                    _remove(item)
    except OSError:
        pass
    return removed


def stage(source, release: dict, home=None) -> dict:
    """Download (through ``source.fetch_wheel``), verify and stage ``release``. Outcomes: ``staged`` (``artifact_id``,
    ``path``, ``already`` when a verified copy was there), ``refused`` (not verified, or the copy did not check out) and
    ``unavailable`` (the download failed). Older staged releases are removed once the new one is in place."""
    manifest = release["manifest"]
    artifact = manifest["artifact_id"]
    if release.get("trust") != "verified":
        return {"outcome": "refused", "reason": "not-verified",
                "detail": "Only a release whose signature was verified is staged."}
    keys = getattr(source, "keys", None)
    if staged_ready(home, artifact, keys=keys):
        prune(home, keep=artifact)
        return {"outcome": "staged", "artifact_id": artifact, "path": str(staged_root(home) / artifact), "already": True}
    data, signature = release.get("manifest_bytes"), release.get("signature_bytes")
    if not isinstance(data, (bytes, bytearray)) or not isinstance(signature, (bytes, bytearray)):
        return {"outcome": "refused", "reason": "no-signed-copy",
                "detail": "The release did not keep its signed manifest, so it cannot be staged."}
    root = updates_root(home)
    root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=root))
    try:
        folder = scratch / artifact
        folder.mkdir()
        try:
            wheel = Path(source.fetch_wheel(release, folder))
        except ts.ReadError as exc:
            return {"outcome": "unavailable" if exc.code in ("unavailable", "missing") else "refused", "reason": exc.code,
                    "detail": str(exc)}
        if wheel.parent != folder or wheel.name != manifest["wheel"]:
            return {"outcome": "refused", "reason": "wrong-file", "detail": "The download is not the file the manifest names."}
        (folder / rs.MANIFEST_NAME).write_bytes(bytes(data))
        (folder / sig.SIGNATURE_NAME).write_bytes(bytes(signature))
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest != manifest["sha256"]:
            return {"outcome": "refused", "reason": "sha256-mismatch",
                    "detail": "The downloaded wheel does not match the release manifest."}
        again = rs.FolderSource(scratch, allow_unsigned=False, keys=keys).releases()  # the same check install will make
        if len(again) != 1 or again[0].get("trust") != "verified" or again[0]["manifest"] != manifest:
            return {"outcome": "refused", "reason": "not-verified", "detail": "The staged copy did not verify."}
        target = staged_root(home) / artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _remove(target)
        os.replace(folder, target)
    except OSError as exc:
        return {"outcome": "unavailable", "reason": "write-failed", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        _remove(scratch)
    prune(home, keep=artifact)
    return {"outcome": "staged", "artifact_id": artifact, "path": str(target)}
