"""Build the current pure-Python EUTONOS wheel offline using only the stdlib.

Supports this project's src/eutonos layout, declared package data, console and GUI scripts
and release.json version provider. Other PEP 621/setuptools declarations refuse
visibly instead of silently producing incomplete distribution metadata.

Core files: a tracked ``src`` or ``pyproject.toml`` file that differs from HEAD refuses the build (a tree that is
not itself a git checkout, like a ``git archive`` export, has no HEAD and is not checked); ``--allow-dirty`` builds
anyway and lists those files in METADATA (``Eutonos-Dirty-Source``).

From HEAD only (``head_only=True``, ``--from-head``): the core files, ``pyproject.toml`` and every kit file are read
from the committed blobs of HEAD, never from the working tree, so edits, untracked files and a dirty tree cannot reach
the wheel (``tools/release.py`` always builds this way). ``plugin_version`` stamps the Claude plugin's version in the
bundled crosstalk kit (``plugin.json`` and ``marketplace.json``) so each release carries a new plugin version.

The wheel is reproducible: the same source gives the same bytes. Entries are sorted, carry one fixed time stamp
(``SOURCE_DATE_EPOCH`` when set, otherwise 1980-01-01) and one fixed permission and creator, never the clock.

Kits: ``[tool.eutonos.kits]`` maps a kit name to a repository folder. The wheel carries each kit as data under
``eutonos/kits/<name>/`` (``eutonos.kits.materialize`` copies it into the EUTONOS home). Only git-tracked files
that are clean (equal to HEAD) are bundled, read from the committed blob so the bytes do not depend on line-ending
settings; a dirty tracked kit file refuses the build. The rendered Claude plugin hook is never bundled.

Kit wheels: a kit may carry a tracked ``wheels.lock.json`` naming the third-party wheels it needs (file name and
sha256). The wheels are committed in the kit's ``wheels/`` folder (like ``src/eutonos/runtime_wheels``), so a normal
build carries them; each is checked against its locked sha256, and a tracked wheel the lock does not name refuses the
build. A locked wheel that is not committed is read from a folder given as ``--kit-wheels NAME=DIR`` (or the
``EUTONOS_<NAME>_WHEELS`` environment variable) and staged under ``eutonos/kits/<name>/wheels/``; the build refuses
when it is missing or differs. The build never downloads anything.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import time
import tomllib
import zipfile


ZIP_EPOCH_MIN = 315532800  # 1980-01-01 00:00:00 UTC: the earliest time a zip entry can hold
ZIP_EPOCH_MAX = 4354819198  # 2107-12-31 23:59:58 UTC: the latest
ZIP_LEVEL = 6


def source_epoch(environ=None) -> int:
    """The one time every built file carries: ``SOURCE_DATE_EPOCH`` (whole seconds, the reproducible-builds convention)
    when set, otherwise 1980-01-01 00:00:00 UTC. The wall clock is never used, so the same source gives the same bytes.
    A value a zip entry cannot hold is raised to 1980 (the earliest) or refused (after 2107)."""
    given = (os.environ if environ is None else environ).get("SOURCE_DATE_EPOCH")
    if given is None or given == "":
        return ZIP_EPOCH_MIN
    if not re.fullmatch(r"[0-9]+", given.strip()):
        raise ValueError("SOURCE_DATE_EPOCH must be a whole number of seconds since 1970.")
    value = int(given)
    if value > ZIP_EPOCH_MAX:
        raise ValueError("SOURCE_DATE_EPOCH is later than a zip entry can hold (2107).")
    return max(value, ZIP_EPOCH_MIN)


def write_archive(path: Path, payload: dict[str, bytes], epoch: int | None = None) -> None:
    """Write ``payload`` ({entry name: bytes}) as a zip whose bytes depend only on the entries: sorted by name, every
    entry with the same time stamp (``epoch``, default ``source_epoch()``), the same creator system and permissions and
    the same compression level. ``zipfile`` would otherwise stamp each entry with the current time and the building
    platform. Refuses an existing file."""
    stamp = time.gmtime(source_epoch() if epoch is None else epoch)[:6]
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=ZIP_LEVEL) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # the same on every platform (zipfile writes 0 on Windows, 3 elsewhere)
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, compresslevel=ZIP_LEVEL)


def _record(data: bytes) -> tuple[str, str]:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return "sha256=" + encoded, str(len(data))


def _line(value, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"Invalid {label} metadata.")
    return value


def _metadata(root: Path, package: Path, texts: tuple[str, str] | None = None) -> tuple[str, dict]:
    """``(version, project)``. `texts` is ``(pyproject.toml, release.json)`` text read from HEAD (``head_only``)."""
    if texts is None:
        project_file = root / "pyproject.toml"
        release_file = package / "release.json"
        for path in (project_file, release_file):
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                raise ValueError("Build metadata must be a regular repository file.")
        texts = (project_file.read_text(encoding="utf-8"), release_file.read_text(encoding="utf-8"))
    config = tomllib.loads(texts[0])
    build_system = config.get("build-system", {})
    if (build_system.get("build-backend") != "setuptools.build_meta"
            or set(build_system) - {"requires", "build-backend"}):
        raise ValueError("Unsupported build backend configuration.")
    project = config["project"]
    supported = {"name", "dynamic", "description", "requires-python", "dependencies", "scripts", "gui-scripts"}
    unknown = set(project) - supported
    if unknown:
        raise ValueError("Unsupported project metadata: " + ", ".join(sorted(unknown)))
    setuptools = config.get("tool", {}).get("setuptools", {})
    if (set(setuptools) != {"packages", "package-data", "dynamic"}
            or setuptools["packages"] != {"find": {"where": ["src"]}}
            or setuptools["dynamic"] != {"version": {"attr": "eutonos.__version__"}}
            or project.get("dynamic") != ["version"]):
        raise ValueError("Unsupported setuptools package or dynamic metadata configuration.")
    if project.get("name") != "eutonos":
        raise ValueError("Only the EUTONOS pure-Python package is supported.")
    patterns = setuptools["package-data"]
    if set(patterns) != {"eutonos"} or not isinstance(patterns["eutonos"], list):
        raise ValueError("Unsupported package-data configuration.")
    project["package-data"] = patterns["eutonos"]
    version = json.loads(texts[1])["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version):
        raise ValueError("Unsupported release version; expected the current three-part numeric identity.")
    return version, project


KIT_NAME = re.compile(r"[a-z][a-z0-9-]*")
# Extra tracked files a kit carries, as {kit name: {repository path: path inside the kit}}.
KIT_EXTRAS = {"crosstalk": {"docs/Crosstalk/QUICKSTART.md": "QUICKSTART.md"},
              "simon": {"docs/Simon/QUICKSTART.md": "QUICKSTART.md"}}
# Files a kit never carries, as {kit name: patterns relative to the kit folder}. A pattern is an exact path, a folder
# ending in "/" or an fnmatch pattern. Crosstalk: the rendered Claude plugin hook is generated per machine at install
# time; the per-machine PowerShell scripts install.ps1 and uninstall.ps1 stay in the repository (their tests use them)
# but the EUTONOS installer, not a script in the kit, installs and removes the kit (IPC4 finding F19). Simon: its tests,
# developer tools, batch launchers, project-only Claude settings (the dev SessionEnd hook) and planning notes stay in
# the repository; the installed part runs helperctl.py through ``eutonos-simon``.
KIT_EXCLUDED = {
    "crosstalk": ("claude-plugin/plugin/hooks/register.mjs", "install.ps1", "uninstall.ps1"),
    "simon": ("tests/", "tools/", "docs/", ".claude/settings.json", ".gitignore", "*.bat"),
}
WHEEL_LOCK = "wheels.lock.json"
WHEEL_LOCK_PROFILE = "eutonos.kit-wheels.v1"
WHEEL_FILE = re.compile(r"[A-Za-z0-9_.+-]+\.whl")
SHA256 = re.compile(r"[0-9a-f]{64}")


def kit_excluded(name: str, inside: str) -> bool:
    """True when ``inside`` (a path relative to kit ``name``) is never bundled."""
    for pattern in KIT_EXCLUDED.get(name, ()):
        if pattern.endswith("/"):
            if inside.startswith(pattern):
                return True
        elif inside == pattern or fnmatch.fnmatchcase(inside, pattern):
            return True
    return "__pycache__" in Path(inside).parts


def wheel_lock(data: bytes) -> dict[str, str]:
    """``{wheel file name: sha256}`` from a kit's ``wheels.lock.json`` bytes; refuses a malformed lock."""
    lock = json.loads(data.decode("utf-8"))
    wheels = lock.get("wheels") if isinstance(lock, dict) else None
    if lock.get("profile") != WHEEL_LOCK_PROFILE or not isinstance(wheels, dict):
        raise ValueError("Invalid " + WHEEL_LOCK + ": expected profile " + WHEEL_LOCK_PROFILE + " and a wheels table.")
    for file, digest in wheels.items():
        if not isinstance(file, str) or not WHEEL_FILE.fullmatch(file) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("Invalid " + WHEEL_LOCK + " entry: " + str(file))
    return wheels


def wheelhouse_dir(name: str, given: dict | None = None, environ=None) -> Path | None:
    """The folder holding kit ``name``'s locked wheels: ``given[name]``, else ``EUTONOS_<NAME>_WHEELS``, else None."""
    if given and given.get(name):
        return Path(given[name])
    value = (os.environ if environ is None else environ).get("EUTONOS_" + name.upper().replace("-", "_") + "_WHEELS", "")
    return Path(value) if value.strip() else None


def stage_wheels(name: str, lock: dict[str, str], folder: Path | None) -> dict[str, bytes]:
    """``{"wheels/<file>": bytes}`` for every locked wheel, each checked against its sha256. Nothing is downloaded."""
    if not lock:
        return {}
    if folder is None or not folder.is_dir():
        raise ValueError(f"Kit {name} locks {len(lock)} wheel(s); give the folder holding them with --kit-wheels "
                         f"{name}=DIR (nothing is downloaded).")
    staged = {}
    for file, digest in sorted(lock.items()):
        path = folder / file
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Kit {name} wheel is missing: {file}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Kit {name} wheel does not match its locked sha256: {file}")
        staged["wheels/" + file] = data
    return staged


def check_kit_wheels(name: str, lock: dict[str, str], files: dict[str, bytes], folder: Path | None) -> dict[str, bytes]:
    """The kit's ``wheels/`` entries after the lock check: committed wheels are checked against their sha256, a
    committed wheel the lock does not name refuses, and locked wheels that are not committed come from `folder`."""
    committed = {inside[len("wheels/"):] for inside in files if inside.startswith("wheels/")}
    unlocked = sorted(committed - set(lock))
    if unlocked:
        raise ValueError(f"Kit {name} has committed wheel(s) its {WHEEL_LOCK} does not name: " + ", ".join(unlocked))
    for file in sorted(committed):
        if hashlib.sha256(files["wheels/" + file]).hexdigest() != lock[file]:
            raise ValueError(f"Kit {name} wheel does not match its locked sha256: {file}")
    rest = {file: digest for file, digest in lock.items() if file not in committed}
    return stage_wheels(name, rest, folder) if rest else {}


def _git(root: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if done.returncode != 0:
        raise ValueError("Kit staging needs a git checkout: git " + " ".join(args[:2]) + " failed.")
    return done.stdout


NUL, TAB, NL = chr(0), chr(9), chr(10)


def head_files(root: Path, *prefixes: str) -> dict[str, bytes]:
    """``{path: committed bytes}`` of every file HEAD tracks under the given paths (a file or a folder), read from the
    blobs themselves (one ``git cat-file --batch`` process), so neither the working tree nor line-ending settings
    reach the bytes. A symlink or submodule entry refuses."""
    listing = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", *prefixes).decode("utf-8")
    entries = []
    for row in filter(None, listing.split(NUL)):
        meta, _, path = row.partition(TAB)
        mode, kind, sha = meta.split()
        if mode in ("120000", "160000") or kind != "blob":
            raise ValueError("A symlink or submodule is not allowed in the committed tree: " + path)
        entries.append((path, sha))
    if not entries:
        return {}
    request = "".join(sha + NL for _, sha in entries).encode("ascii")
    done = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], input=request, capture_output=True)
    if done.returncode != 0:
        raise ValueError("Staging from HEAD needs a git checkout: git cat-file failed.")
    out, data, offset = {}, done.stdout, 0
    for path, sha in entries:
        end = data.index(NL.encode("ascii"), offset)
        header = data[offset:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError("Unexpected git object for " + path)
        size = int(header[2])
        out[path] = data[end + 1:end + 1 + size]
        offset = end + 1 + size + 1
    return out


def _kits(root: Path, wheelhouses: dict | None = None, head_only: bool = False,
          pyproject: str | None = None) -> dict[str, dict[str, bytes]]:
    """``{kit name: {path inside the kit: committed bytes}}`` for every ``[tool.eutonos.kits]`` entry, plus any locked
    wheels (see the module docstring). With `head_only` the list of files comes from HEAD (not the index), the working
    tree is never read and a dirty tracked kit file does not matter; `pyproject` is then HEAD's ``pyproject.toml``."""
    config = tomllib.loads(pyproject if pyproject is not None else (root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = config.get("tool", {}).get("eutonos", {})
    if set(declared) - {"kits"}:
        raise ValueError("Unsupported [tool.eutonos] configuration.")
    kits = declared.get("kits", {})
    if not isinstance(kits, dict):
        raise ValueError("Invalid [tool.eutonos.kits] table.")
    staged = {}
    for name, folder in sorted(kits.items()):
        if not KIT_NAME.fullmatch(name) or not isinstance(folder, str) or not folder:
            raise ValueError("Invalid kit declaration: " + name)
        relative = Path(folder)
        if relative.is_absolute() or ".." in relative.parts or (not head_only and not (root / relative).is_dir()):
            raise ValueError("A kit must be a repository folder: " + name)
        if not head_only and ((root / relative).is_symlink() or not (root / relative).resolve().is_relative_to(root)):
            raise ValueError("A kit must not be a symlink or leave the repository: " + name)
        prefix = relative.as_posix().rstrip("/")
        lister = ["ls-tree", "-r", "--name-only", "-z", "HEAD"] if head_only else ["ls-files", "-z"]
        sources = {path[len(prefix) + 1:]: path for path in
                   _git(root, *lister, "--", prefix).decode("utf-8").split("\0")
                   if path.startswith(prefix + "/")}
        for source, inside in KIT_EXTRAS.get(name, {}).items():
            if _git(root, *lister, "--", source).decode("utf-8").strip("\0") != source:
                raise ValueError("Kit file is not tracked by git: " + source)
            sources[inside] = source
        if not sources:
            raise ValueError("The kit has no git-tracked files: " + name)
        tracked = sorted(set(sources.values()))
        if not head_only:
            dirty = [line[3:] for line in
                     _git(root, "status", "--porcelain", "--untracked-files=no", "--", *tracked).decode("utf-8").splitlines()]
            if dirty:
                raise ValueError("Dirty kit files; commit or restore them first: " + ", ".join(sorted(dirty)))
        blobs = head_files(root, *tracked) if head_only else {}
        files = {}
        for inside, source in sorted(sources.items()):
            if kit_excluded(name, inside):
                continue
            files[inside] = blobs[source] if head_only else _git(root, "cat-file", "blob", "HEAD:" + source)
        if WHEEL_LOCK in files:
            files.update(check_kit_wheels(name, wheel_lock(files[WHEEL_LOCK]), files, wheelhouse_dir(name, wheelhouses)))
        staged[name] = files
    return staged


def _dirty_core(root: Path) -> list[str]:
    """Tracked core files (``src``, ``pyproject.toml``) that differ from HEAD. A tree that is not itself a git checkout
    (a ``git archive`` export) has no HEAD to differ from, so it has none."""
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        return []
    return sorted(line[3:] for line in _git(root, "status", "--porcelain", "--untracked-files=no", "--", "src",
                                            "pyproject.toml").decode("utf-8").splitlines())


PLUGIN_FILES = {"crosstalk": {"claude-plugin/plugin/.claude-plugin/plugin.json": "the plugin manifest",
                              "claude-plugin/.claude-plugin/marketplace.json": "the marketplace listing"}}
_VERSION_FIELD = re.compile(rb'("version"\s*:\s*")[^"]*(")')


def stamp_plugin_version(kits: dict[str, dict[str, bytes]], version: str) -> None:
    """Set the Claude plugin version in the staged kit bytes (``plugin.json`` and the marketplace listing), so a new
    release always carries a new plugin version. Only the staged bytes change, never a file in the repository."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version):
        raise ValueError("The plugin version must be three numeric parts.")
    for kit, names in PLUGIN_FILES.items():
        for inside, label in names.items():
            data = kits.get(kit, {}).get(inside)
            if data is None:
                raise ValueError(f"Cannot stamp the plugin version: the {kit} kit has no {label} ({inside}).")
            stamped, count = _VERSION_FIELD.subn(lambda m: m.group(1) + version.encode("ascii") + m.group(2), data)
            if count != 1:
                raise ValueError(f"Cannot stamp the plugin version: {inside} must name exactly one version.")
            kits[kit][inside] = stamped


def build(root: Path, output: Path, wheelhouses: dict | None = None, allow_dirty: bool = False,
          head_only: bool = False, plugin_version: str | None = None) -> Path:
    """`allow_dirty` builds core files that differ from HEAD anyway; the wheel's METADATA then lists them.
    `head_only` builds only what HEAD commits (the working tree is never read; `allow_dirty` has no meaning then).
    `plugin_version` stamps the Claude plugin version in the crosstalk kit."""
    root = root.resolve(strict=True)
    source = root / "src"
    package = source / "eutonos"
    texts = None
    if head_only:
        core = head_files(root, "src/eutonos", "pyproject.toml")
        if "pyproject.toml" not in core or not any(name.startswith("src/eutonos/") for name in core):
            raise ValueError("HEAD has no src/eutonos package or pyproject.toml to build from.")
        folders = _git(root, "ls-tree", "-d", "--name-only", "HEAD", "src/").decode("utf-8").split()
        if any(name != "src/eutonos" for name in folders):
            raise ValueError("Unsupported additional source package.")
        texts = (core["pyproject.toml"].decode("utf-8"), core["src/eutonos/release.json"].decode("utf-8"))
        dirty = []
    else:
        if source.is_symlink() or package.is_symlink() or not package.is_dir():
            raise ValueError("Source package must be a regular repository directory.")
        if any(path.is_dir() and path.name != "eutonos" for path in source.iterdir()):
            raise ValueError("Unsupported additional source package.")
        dirty = _dirty_core(root)
        if dirty and not allow_dirty:
            raise ValueError("Dirty core files; commit or restore them first: " + ", ".join(dirty))
    version, project = _metadata(root, package, texts)
    groups = {"console_scripts": project.get("scripts", {}),
              "gui_scripts": project.get("gui-scripts", {})}
    dist = f"eutonos-{version}.dist-info"
    payload = {}
    if head_only:
        for name, data in sorted(core.items()):
            if not name.startswith("src/eutonos/"):
                continue
            relative = PurePosixPath(name[len("src/eutonos/"):])
            if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
                continue
            if relative.suffix != ".py" and not any(relative.match(pattern) for pattern in project["package-data"]):
                raise ValueError("Undeclared package data: " + relative.as_posix())
            payload["eutonos/" + relative.as_posix()] = data
    else:
        for path in sorted(package.rglob("*")):
            relative = path.relative_to(package)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Symlink or outside-repository package payload: " + relative.as_posix())
            if (not path.is_file() or "__pycache__" in relative.parts
                    or path.suffix in {".pyc", ".pyo"}):
                continue
            if path.suffix != ".py" and not any(relative.match(pattern) for pattern in project["package-data"]):
                raise ValueError("Undeclared package data: " + relative.as_posix())
            payload["eutonos/" + relative.as_posix()] = path.read_bytes()
    staged = _kits(root, wheelhouses, head_only, texts[0] if texts else None)
    if plugin_version is not None:
        stamp_plugin_version(staged, plugin_version)
    for name, files in staged.items():
        for inside, data in files.items():
            payload["eutonos/kits/" + name + "/" + inside] = data
    metadata = ["Metadata-Version: 2.3", "Name: eutonos", "Version: " + version]
    for key, header in (("description", "Summary"), ("requires-python", "Requires-Python")):
        if key in project:
            metadata.append(header + ": " + _line(project[key], key))
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("Invalid dependencies metadata.")
    if dirty:
        metadata.append("Eutonos-Dirty-Source: " + _line(", ".join(dirty), "dirty source"))
    metadata.extend("Requires-Dist: " + _line(value, "dependency") for value in dependencies)
    for group, scripts in groups.items():
        if not isinstance(scripts, dict):
            raise ValueError("Invalid " + group + " metadata.")
        for name, target in scripts.items():
            _line(name, "script name")
            _line(target, "script target")
            if not re.fullmatch(r"[\w.-]+", name) or not re.fullmatch(r"[\w.]+:[\w.]+", target):
                raise ValueError("Unsupported " + group + " entry.")
    payload[f"{dist}/METADATA"] = ("\n".join(metadata) + "\n\n").encode("utf-8")
    payload[f"{dist}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: eutonos-stdlib-wheel\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    payload[f"{dist}/entry_points.txt"] = "".join(
        "[" + group + "]\n" + "".join(f"{name} = {target}\n" for name, target in sorted(scripts.items())) + "\n"
        for group, scripts in groups.items() if scripts).encode("utf-8")
    payload[f"{dist}/top_level.txt"] = b"eutonos\n"
    rows = []
    for name, data in sorted(payload.items()):
        digest, size = _record(data); rows.append((name, digest, size))
    record_name = f"{dist}/RECORD"
    rows.append((record_name, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    payload[record_name] = stream.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / f"eutonos-{version}-py3-none-any.whl"
    if wheel.exists():
        raise FileExistsError(wheel)
    write_archive(wheel, payload)
    return wheel


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kit-wheels", action="append", default=[], metavar="NAME=DIR",
                        help="the folder holding a kit's locked wheels (repeatable; nothing is downloaded)")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="build core files that differ from HEAD (METADATA records them)")
    parser.add_argument("--from-head", action="store_true",
                        help="build only what HEAD commits; the working tree is never read")
    parser.add_argument("--plugin-version", help="stamp the Claude plugin version in the crosstalk kit (x.y.z)")
    args = parser.parse_args(argv)
    wheelhouses = {}
    for value in args.kit_wheels:
        name, sep, folder = value.partition("=")
        if not sep or not KIT_NAME.fullmatch(name) or not folder:
            parser.error("--kit-wheels takes NAME=DIR")
        wheelhouses[name] = Path(folder)
    print(build(args.root, args.output, wheelhouses, args.allow_dirty, args.from_head, args.plugin_version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
