"""Setup's bootstrap: run the EUTONOS engine from the wheel Setup carries, with the Python Setup carries.

    python -I -B bootstrap.py WHEEL (wizard|engine) ARGS...
    python -I -B bootstrap.py --installed-engine HOME WHEEL engine uninstall --home HOME ...

``wizard`` runs ``eutonos.setup_wizard`` (read-only inspection, consent text) and ``engine`` runs
``eutonos.installer`` (``eutonos-install``). The wheel is unpacked into a temporary folder of this run's own (``eutonos-setup-<hash>-<random>``; its component
manifests and permissions list are read by file path, so it cannot run zipped) and put first on ``sys.path``;
nothing is read from ``PYTHONPATH`` or the current folder (the caller runs Python with ``-I``).

With ``--installed-engine`` the uninstall uses a temporary copy of the engine that is installed in HOME (so the
uninstall matches the version that was installed, and the runtime folder it is about to delete is not in use). It
falls back to the wheel when there is none. Nothing is downloaded and nothing is written outside the temporary folder.
Each run removes the one temporary folder it created when it ends (success, failure or refusal), and never any other.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile


def _unpack(wheel: Path) -> Path:
    """Unpack the wheel into a fresh folder of this run's own, which the caller removes. A refused wheel leaves none."""
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()[:16]
    target = Path(tempfile.mkdtemp(prefix=f"eutonos-setup-{digest}-"))
    try:
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.namelist():
                destination = (target / member).resolve()
                if target.resolve() not in destination.parents and destination != target.resolve():
                    raise SystemExit(f"The wheel holds a path outside itself: {member}")
            archive.extractall(target)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _installed_engine(home: Path) -> Path | None:
    """A temporary copy of the engine package inside the active runtime of HOME, or None."""
    try:
        state = json.loads((home / "install" / "state.json").read_text(encoding="utf-8"))
        # A pending update can own newer effects that the active engine cannot remove.
        generation = (state.get("pending") or {}).get("gen_id") or state.get("active")
        record = json.loads((home / "install" / "generations" / generation / "record.json").read_text(encoding="utf-8"))
        artifact = record["wheel"]["artifact_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    package = home / "runtimes" / artifact / "Lib" / "site-packages" / "eutonos"
    if not (package / "__init__.py").is_file():
        return None
    target = Path(tempfile.mkdtemp(prefix="eutonos-uninstall-"))
    shutil.copytree(package, target / "eutonos", ignore=shutil.ignore_patterns("__pycache__"))
    return target


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    installed = None
    if argv[:1] == ["--installed-engine"] and len(argv) >= 3:
        installed, argv = Path(argv[1]), argv[2:]
    if len(argv) < 2 or argv[1] not in ("wizard", "engine"):
        print("usage: bootstrap.py [--installed-engine HOME] WHEEL (wizard|engine) ARGS...", file=sys.stderr)
        return 2
    wheel, kind, rest = Path(argv[0]), argv[1], argv[2:]
    source = _installed_engine(installed) if installed is not None and "uninstall" in rest else None
    if source is None and not (wheel.is_file() and zipfile.is_zipfile(wheel)):
        print(f"The EUTONOS wheel is missing or damaged: {wheel}", file=sys.stderr)
        return 2
    try:
        source = source or _unpack(wheel)
        sys.path.insert(0, str(source))
        if kind == "wizard":
            from eutonos import setup_wizard as module
        else:
            from eutonos import installer as module
        return int(module.main(rest) or 0)
    finally:  # only the folder this run made (unpacked wheel or installed-engine copy), never anything else
        if source is not None:
            shutil.rmtree(source, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
