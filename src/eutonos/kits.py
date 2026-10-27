"""Bundled kits: copy a wheel-carried kit into ``<EUTONOS home>/kits/<kit_id>`` and run it (IPC2 contract a).

A kit is a self-contained, standard-library folder that cannot import ``eutonos``; the wheel carries it as data
under ``eutonos/kits/<component>/`` (``tools/build_wheel.py`` stages it from ``[tool.eutonos.kits]``). Installing
copies it once:

* ``kit.json`` is ``{kit_id, component, tree_hash, files{relative path: sha256}}``;
* ``tree_hash = sha256(json.dumps(files, sort_keys=True))`` and ``kit_id = "<component>-<tree_hash[:12]>"``, so the
  same tree always gives the same ``kit_id``;
* the folder is built beside its final name and renamed in one step, so a reader never sees a half-copied kit.

``active_kit`` finds the kit the installed generation points at (``install/state.json``, written by the IPC5
activation) and the three entry points run a kit script with ``runpy``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import sys

from . import home as eutonos_home

KIT_FILE = "kit.json"
PROFILE = "eutonos.kit.v1"
COMPONENT = "crosstalk"
SKIP_PARTS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


class KitError(Exception):
    """A kit could not be found, copied or verified."""


def source_dir(component: str = COMPONENT) -> Path:
    """The kit folder the wheel carries (or a source tree that stages one) for ``component``."""
    return Path(__file__).resolve().parent / "kits" / component


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def tree_files(folder: Path) -> dict[str, str]:
    """``{relative posix path: sha256}`` for every kit file below ``folder`` (``kit.json`` excluded)."""
    files = {}
    for path in sorted(Path(folder).rglob("*")):
        relative = path.relative_to(folder)
        if (not path.is_file() or path.name == KIT_FILE and len(relative.parts) == 1
                or SKIP_PARTS & set(relative.parts) or path.suffix in SKIP_SUFFIXES):
            continue
        files[relative.as_posix()] = _digest(path)
    return files


def tree_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()


def kit_id(component: str, files: dict[str, str]) -> str:
    return f"{component}-{tree_hash(files)[:12]}"


def verify(folder: Path) -> bool:
    """True when ``folder`` holds exactly the files its ``kit.json`` records."""
    try:
        record = json.loads((Path(folder) / KIT_FILE).read_text(encoding="utf-8"))
        files = record["files"]
        return (isinstance(files, dict) and tree_hash(files) == record["tree_hash"]
                and record["kit_id"] == f"{record['component']}-{record['tree_hash'][:12]}"
                and tree_files(Path(folder)) == files)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def materialize(home, component: str = COMPONENT, source: Path | None = None) -> dict:
    """Build ``<home>/kits/<kit_id>/`` from the bundled kit by copy and one directory rename.

    Idempotent: a folder that already verifies is reused. A folder with the same name that does not verify is
    replaced (renamed aside, then removed) so the result is always a verified kit. Returns
    ``{kit_id, component, tree_hash, path, created}``."""
    source = Path(source) if source else source_dir(component)
    if not source.is_dir():
        raise KitError(f"no bundled kit for {component!r} at {source}")
    files = tree_files(source)
    if not files:
        raise KitError(f"the bundled kit for {component!r} is empty")
    digest = tree_hash(files)
    ident = f"{component}-{digest[:12]}"
    kits = Path(home) / "kits"
    final = kits / ident
    result = {"kit_id": ident, "component": component, "tree_hash": digest, "path": str(final), "created": False}
    if final.is_dir() and verify(final):
        return result
    kits.mkdir(parents=True, exist_ok=True)
    staging = kits / f".{ident}.new-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for relative in files:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
        record = {"kit_id": ident, "component": component, "tree_hash": digest, "files": files}
        (staging / KIT_FILE).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                        encoding="utf-8", newline="\n")
        if not verify(staging):
            raise KitError("the copied kit does not match its tree hash")
        if final.is_dir() and verify(final):  # F20: another process finished the same kit while this one copied
            return result
        if final.exists():
            aside = kits / f".{ident}.stale-{os.getpid()}"
            shutil.rmtree(aside, ignore_errors=True)
            os.replace(final, aside)
            shutil.rmtree(aside, ignore_errors=True)
        os.replace(staging, final)
    except OSError:
        if final.is_dir() and verify(final):  # another process finished the same kit first
            return result
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    result["created"] = True
    return result


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def active_kit(home=None, component: str = COMPONENT) -> Path | None:
    """The folder of the active kit for ``component``, or ``None``.

    ``install/state.json`` names the active generation and that generation's ``record.json`` lists its kits. Before
    any generation exists (the state file is absent), the most recently materialized verified kit of the component
    is used. A state file that exists but cannot be read gives ``None``, not that fallback. A kit that fails
    ``verify`` is never returned."""
    base = Path(home) if home else eutonos_home.home()
    state_file = base / "install" / "state.json"
    state = _read_json(state_file)
    if not isinstance(state, dict) and state_file.exists():  # present but unreadable or not an object: fail closed
        return None
    if isinstance(state, dict) and state.get("active"):
        record = _read_json(base / "install" / "generations" / str(state["active"]) / "record.json")
        for item in (record or {}).get("kits") or []:
            if isinstance(item, dict) and item.get("component") == component:
                folder = base / "kits" / str(item.get("kit_id"))
                return folder if folder.is_dir() and verify(folder) else None
        return None
    if isinstance(state, dict):  # state exists but names no active generation
        return None
    candidates = [path for path in (base / "kits").glob(component + "-*") if path.is_dir() and verify(path)] \
        if (base / "kits").is_dir() else []
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.name), default=None)


def _run(script: str, component: str = COMPONENT) -> int:
    kit = active_kit(component=component)
    if kit is None or not (kit / script).is_file():
        message = (f"EUTONOS: no active {component} kit under {eutonos_home.kits()}. "
                   f"Materialize it first (eutonos.kits.materialize).")
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        return 2
    path = kit / script
    sys.dont_write_bytecode = True  # a kit is immutable once copied
    sys.path.insert(0, str(kit))
    sys.argv[0] = str(path)
    runpy.run_path(str(path), run_name="__main__")
    return 0


def crosstalk_main() -> int:
    """``eutonos-crosstalk``: the active kit's CLI."""
    return _run("crosstalk_p0_cli.py")


def crosstalk_helper_main() -> int:
    """``eutonos-crosstalk-helper`` (gui): the active kit's helper."""
    return _run("crosstalk_helper.py")


def crosstalk_mcp_main() -> int:
    """``eutonos-crosstalk-mcp`` (gui, so Codex opens no console window): the active kit's tool server."""
    return _run("codex_tool_server.py")
