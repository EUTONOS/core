"""Effect writers and removers for the installer (IPC5 lane A).

Each writer returns Effects (contract b) and takes ``apply=False`` for a dry run that writes nothing. Each remover
undoes one recorded Effect only while its target still holds the recorded bytes or entry; otherwise the Effect is
kept and reported as "changed by you, kept". The installer-owned undo operations are:

``files-delete`` (a folder of copied files), ``dir-delete`` (a runtime), ``kit-delete``, ``block-remove`` (a managed
instruction block: its undo records the exact earlier block, the markers a wrap added, or the separators an added
block brought, so it comes out in place even after other edits to the file), ``path-remove`` (an entry of the Windows user PATH), ``env-var-remove`` (the user environment
variable ``EUTONOS_HOME`` a real install of a non-default home sets), ``remove-file``, ``text-replace`` (a repointed
Codex block goes back to the owner's earlier block), ``runkey-restore``, ``shortcut-restore``, ``app-command`` and
``none`` (an adopted item: never removed). The permissions list brings its own (``json-remove``, ``text-remove``,
``file-delete``, ``line-remove``).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
import zipfile

from . import completion_policy as cp
from . import crosstalk_instructions as ci
from . import decisions_instructions as di
from . import kits as kits_module
from . import memory_instructions as mi
from . import simon_instructions as si
from . import permissions
from .install_record import sha256_bytes, sha256_file, write_atomic

LEGACY_BEGIN = "# >>> crosstalk-p0 throwaway probe (remove with uninstall.ps1) >>>"
LEGACY_END = "# <<< crosstalk-p0 <<<"
HELPER_SHORTCUT = "Crosstalk helper.lnk"
HELPER_DESCRIPTION = "Watches Claude Desktop restarts for Crosstalk: restart relink, letter wakes, held letters"
COMPANION_RUN_VALUE = "EUTONOS Companion"
PATH_MARK = "# eutonos-managed"
PLUGIN_ID = "crosstalk-p0@crosstalk-p0-local"
MARKETPLACE_NAME = "crosstalk-p0-local"
ENV_HOME = "EUTONOS_HOME"
REMOTE_CONTROL_LNK = "EUTONOS Remote Control.lnk"  # the remote-control component's own login item (never the person's)
PERMISSION_OPS = {"json-remove", "text-remove", "file-delete", "line-remove"}


def effect(effect_id, component, kind, target, *, selector=None, permission_entry=None, before=None, after=None,
           shipped=None, backup=None, undo=None, **extra) -> dict:
    """One Effect with every contract (b) field. `before`, `after` and `shipped` are hashes (or None)."""
    return {"effect_id": effect_id, "component": component, "permission_entry": permission_entry, "kind": kind,
            "target": str(target), "selector": selector, "before_sha256": before, "after_sha256": after,
            "shipped_sha256": shipped, "backup": None if backup is None else str(backup), "undo": undo or {"op": "none"},
            "by": "installer", **extra}


def digest_lines(items: dict[str, str]) -> str:
    return hashlib.sha256("\n".join(f"{name}\0{items[name]}" for name in sorted(items)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the wheel
def _package_member(name: str) -> bool:
    parts = name.split("/")
    return (parts[0] == "eutonos" and not name.endswith("/") and "__pycache__" not in parts
            and not name.endswith((".pyc", ".pyo")))


def inspect_wheel(path) -> dict:
    """What the installer needs to know about a wheel: its hash, version, scripts, kits and package digest."""
    from .installer_ports import wheel_entry_points
    from .install_record import artifact_id
    path = Path(path)
    digest = sha256_file(path)
    if digest is None:
        raise FileNotFoundError(str(path))
    version, kit_names, files = None, set(), {}
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if member.endswith(".dist-info/METADATA") and version is None:
                for line in archive.read(member).decode("utf-8").splitlines():
                    if line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                        break
            parts = member.split("/")
            if len(parts) > 3 and parts[0] == "eutonos" and parts[1] == "kits" and not member.endswith("/"):
                kit_names.add(parts[2])
            if _package_member(member):
                files[member] = hashlib.sha256(archive.read(member)).hexdigest()
    if version is None:
        raise ValueError("the wheel carries no METADATA version")
    return {"file": path.name, "path": str(path), "sha256": digest, "version": version,
            "artifact_id": artifact_id(version, digest), "entry_points": wheel_entry_points(path),
            "kits": sorted(kit_names), "package_digest": digest_lines(files)}


def runtime_package_digest(site_packages: Path) -> str | None:
    """The digest of the ``eutonos`` package files installed under ``site_packages`` (None when absent)."""
    base = Path(site_packages) / "eutonos"
    if not base.is_dir():
        return None
    files = {}
    for item in base.rglob("*"):
        relative = "eutonos/" + item.relative_to(base).as_posix()
        if item.is_file() and _package_member(relative):
            files[relative] = sha256_file(item)
    return digest_lines(files)


def extract_kit(wheel, name: str, destination: Path) -> Path:
    prefix = f"eutonos/kits/{name}/"
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if member.startswith(prefix) and not member.endswith("/"):
                target = Path(destination) / member[len(prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    return Path(destination)


# -------------------------------------------------------------------- runtime
def runtime_effect(layout, ports, info, runtime: Path) -> dict:
    return effect(f"runtime.{info['artifact_id']}", "core", "file-create", runtime, selector=None,
                  after=info["package_digest"], shipped=info["package_digest"],
                  undo={"op": "dir-delete", "path": str(runtime), "artifact_id": info["artifact_id"]})


def kit_effect(record_kit: dict, path: Path) -> dict:
    return effect(f"kit.{record_kit['kit_id']}", "core", "file-create", path, after=record_kit["tree_hash"],
                  shipped=record_kit["tree_hash"],
                  undo={"op": "kit-delete", "path": str(path), "kit_id": record_kit["kit_id"],
                        "tree_hash": record_kit["tree_hash"]})


# ------------------------------------------------------------------- surfaces
def _is_inside(child: Path, parent: Path) -> bool:
    """Whether `child` is `parent` or under it, by whole path parts (so ``runtimes/abc-10`` is not inside ``runtimes/abc-1``)."""
    first, second = os.path.normcase(str(child.resolve())), os.path.normcase(str(parent.resolve()))
    try:
        return os.path.commonpath([first, second]) == second
    except ValueError:  # different drives
        return False


def _delete_file(path: Path, tag: str) -> str | None:
    """Delete a file; a locked one (an exe that is running) is renamed aside. None when done, else the reason."""
    try:
        path.unlink()
        return None
    except FileNotFoundError:
        return None
    except OSError:
        aside = path.with_name(path.name + f".old-{tag}")
        try:
            os.replace(path, aside)
            return None
        except OSError as exc:
            return f"in use ({type(exc).__name__}); remove it after this process exits"


def prune_old(folder: Path) -> None:
    """Remove `name.old-<tag>` files an earlier activation renamed aside (a locked exe)."""
    if folder.is_dir():
        for item in folder.glob("*.old-*"):
            try:
                item.unlink()
            except OSError:
                pass


def install_launchers(layout, ports, runtime: Path, names: list[str], gen_id: str, *, apply=True,
                      keep: set | None = None) -> dict[str, str]:
    """Copy the runtime's entry-point launchers to ``H/bin`` (never links). Returns ``{launcher file: sha256}``.
    A launcher named in `keep` (one you changed) is left as it is; its shipped hash is still reported."""
    scripts = ports.scripts_dir(runtime)
    files = {}
    for name in names:
        file_name = ports.launcher_name(name)
        source = scripts / file_name
        digest = sha256_file(source)
        if digest is None:
            raise FileNotFoundError(f"the runtime has no launcher {file_name}")
        files[file_name] = digest
        if not apply or file_name in (keep or ()):
            continue
        layout.bin.mkdir(parents=True, exist_ok=True)
        target = layout.bin / file_name
        if sha256_file(target) == digest:
            continue
        if target.exists():  # a running exe cannot be overwritten: rename it aside first
            reason = _delete_file(target, gen_id)
            if reason:
                raise OSError(f"cannot replace {target}: {reason}")
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        try:
            shutil.copyfile(source, temporary)
            shutil.copymode(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    if apply:
        prune_old(layout.bin)
    return files


def bin_effect(layout, files: dict[str, str]) -> dict:
    digest = digest_lines(files)
    return effect("surface.bin", "core", "file-create", layout.bin, after=digest, shipped=digest,
                  undo={"op": "files-delete", "dir": str(layout.bin), "files": dict(files)})


def render_register(template: str, runtime: str, cli: str, helper: str) -> str:
    """The Claude plugin hook, rendered. `cli` is ``-`` for a launcher that is already the Crosstalk CLI."""
    def js(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")
    for token, value in (("__CROSSTALK_P0_RUNTIME__", runtime), ("__CROSSTALK_P0_CLI__", cli),
                         ("__CROSSTALK_P0_HELPER__", helper)):
        if token not in template:
            raise ValueError("template token missing: " + token)
        template = template.replace(token, js(value))
    return template


def render_marketplace(layout, ports, kit_dir: Path, *, apply=True, keep: set | None = None,
                       dest: Path | None = None) -> dict[str, str]:
    """The kit's ``claude-plugin`` folder with the hook rendered to run the stable ``H/bin`` launcher, plus QUICKSTART,
    written to `dest` (default ``H/marketplace/crosstalk``, the folder Claude has registered; the installer renders each
    generation's own copy and copies it there on activation). Returns ``{relative path: sha256}`` of what the folder
    should hold; a file named in `keep` (one you changed) is neither rewritten nor removed."""
    source = Path(kit_dir) / "claude-plugin"
    template = (source / "plugin-src" / "register.mjs.in").read_text(encoding="utf-8")
    launcher = str(layout.bin / ports.launcher_name("eutonos-crosstalk"))
    helper = str(layout.base / "crosstalk-probe" / "helper")
    hook = render_register(template, launcher, "-", helper).encode("utf-8")
    quickstart = Path(kit_dir) / "QUICKSTART.md"
    content: dict[str, bytes] = {}
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if item.is_file() and "plugin-src" not in relative.parts and not item.name.startswith("register.mjs.tmp-"):
            content[relative.as_posix()] = item.read_bytes()
    content["plugin/hooks/register.mjs"] = hook
    if quickstart.is_file():
        content["QUICKSTART.md"] = quickstart.read_bytes()
    files = {name: sha256_bytes(data) for name, data in content.items()}
    if not apply:
        return files
    _write_tree(dest or layout.marketplace / "crosstalk", content, keep)
    return files


def _write_tree(final: Path, content: dict[str, bytes], keep: set | None = None) -> None:
    """Make `final` hold exactly `content` ({relative path: bytes}), except that a file named in `keep` is neither
    rewritten nor removed."""
    final.mkdir(parents=True, exist_ok=True)
    keep = keep or set()
    for name, data in content.items():
        target = final / name
        if name not in keep and sha256_file(target) != sha256_bytes(data):
            write_atomic(target, data)
    for item in sorted(final.rglob("*"), reverse=True):  # stale files from an earlier generation
        relative = item.relative_to(final).as_posix()
        if item.is_file() and relative not in content and relative not in keep:
            item.unlink()
        elif item.is_dir() and not any(item.iterdir()):
            item.rmdir()


def repoint_marketplace(layout, gen_id: str, *, keep: set | None = None) -> dict[str, str] | None:
    """Copy generation `gen_id`'s rendered marketplace to ``H/marketplace/crosstalk`` (the folder Claude has
    registered), so the plugin Claude sees is the one that generation shipped. Returns ``{relative path: sha256}`` of
    what the folder should hold, or None when that generation rendered no marketplace (nothing is touched then)."""
    source = layout.generation_marketplace(gen_id)
    if not source.is_dir():
        return None
    content = {item.relative_to(source).as_posix(): item.read_bytes() for item in sorted(source.rglob("*")) if item.is_file()}
    _write_tree(layout.marketplace / "crosstalk", content, keep)
    return {name: sha256_bytes(data) for name, data in content.items()}


def remove_generation_marketplace(layout, gen_id: str) -> bool:
    """Delete a generation's own marketplace copy (its generation was pruned, or EUTONOS was uninstalled)."""
    folder = layout.generation(gen_id) / "marketplace"
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


def marketplace_effect(layout, files: dict[str, str]) -> dict:
    digest = digest_lines(files)
    final = layout.marketplace / "crosstalk"
    return effect("surface.marketplace", "core", "file-create", final, after=digest, shipped=digest,
                  undo={"op": "files-delete", "dir": str(final), "files": dict(files)})


# ----------------------------------------------------------------- user PATH
def _same_entry(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left.strip().strip('"'))) == os.path.normcase(os.path.normpath(right))


def path_effect(layout, ports, user_home: Path, gen_id: str, *, apply=True, adopt=False, sink=None):
    """Entry 22: ``H/bin`` on the user PATH. Returns ``(Effect | None, status)``; status is ``added``, ``present``
    (an adopted Effect when `adopt`), or ``planned``. `sink(effect)` hears the Effect just before the write."""
    bin_dir = str(layout.bin)
    if ports.os_name == "windows":
        state = ports.user_path_state()
        current = None if state is None else state[0]
        kind = None if state is None else state[1]
        text = current or ""
        if any(_same_entry(item, bin_dir) for item in text.split(";")):
            if not adopt:
                return None, "present"
            return effect("env-path.bin", "core", "env-path", r"reg:HKCU\Environment\Path", selector="Path",
                          permission_entry="user-path", before=None if current is None else sha256_bytes(text.encode("utf-8")),
                          after=sha256_bytes(text.encode("utf-8")),
                          shipped=sha256_bytes(bin_dir.encode("utf-8")), adopted=True), "present"
        # The original text is kept exactly (empty entries, order, a trailing ";"); only bin_dir is appended, with a
        # separator only when the original neither is empty nor already ends in one. What was added is recorded so
        # the removal can take out just that, and `created` / `kind` so an absent value is deleted again and the
        # registry type is never converted.
        separated = bool(text) and not text.endswith(";")
        updated = text + (";" if separated else "") + bin_dir
        built = effect("env-path.bin", "core", "env-path", r"reg:HKCU\Environment\Path", selector="Path",
                       permission_entry="user-path", before=None if current is None else sha256_bytes(text.encode("utf-8")),
                       after=sha256_bytes(updated.encode("utf-8")), shipped=sha256_bytes(bin_dir.encode("utf-8")),
                       undo={"op": "path-remove", "entry": bin_dir, "separator": separated, "created": current is None,
                             "kind": kind})
        if apply:
            if sink:
                sink(built)
            ports.user_path_set(updated, kind)
        return built, "added" if apply else "planned"
    profile = Path(user_home) / ".zprofile"
    line = f'export PATH="{bin_dir}:$PATH" {PATH_MARK}'
    before = profile.read_bytes() if profile.exists() else None
    text = (before or b"").decode("utf-8", errors="replace")
    if line in text.splitlines():
        if not adopt:
            return None, "present"
        digest = sha256_bytes(before)
        return effect("env-path.bin", "core", "env-path", profile, selector=PATH_MARK, permission_entry="user-path",
                      before=digest, after=digest, shipped=sha256_bytes((line + "\n").encode("utf-8")),
                      adopted=True), "present"
    lead = "\n" if text and not text.endswith("\n") else ""
    after = (text + lead + line + "\n").encode("utf-8")
    backup = layout.backups(gen_id) / "env-path.bin.zprofile" if before is not None else None
    built = effect("env-path.bin", "core", "env-path", profile, selector=PATH_MARK, permission_entry="user-path",
                   before=sha256_bytes(before), after=sha256_bytes(after),
                   shipped=sha256_bytes((line + "\n").encode("utf-8")), backup=backup,
                   undo={"op": "line-remove", "line": line, "created_dirs": []})
    if apply:
        if sink:
            sink(built)
        if backup is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before)
        write_atomic(profile, after)
    return built, "added" if apply else "planned"


def remove_path_entry(ports, undo: dict):
    """The one undo of the user PATH edit: take out only the entry EUTONOS added. The rest of the value stays byte
    for byte: empty entries, order and a trailing ";" included, and so does the registry type. A value EUTONOS created
    (there was none before) is deleted once nothing else is left in it; entries added after the install stay. An older
    record without these fields is read as "a separator was added, the value existed". Returns ``(state, reason)``."""
    entry = undo["entry"]
    state = ports.user_path_state()
    if state is None:
        return "gone", None
    current, kind = state
    parts = current.split(";")
    found = [index for index, part in enumerate(parts) if _same_entry(part, entry)]
    if not found:
        return "gone", None
    index = found[-1]  # the one appended last is the one EUTONOS added
    if undo.get("separator", True) or index < len(parts) - 1:
        del parts[index]  # its own separator goes with it
    else:
        parts[index] = ""  # the original ended in ";": leave it as it was
    rest = ";".join(parts)
    if undo.get("created") and not rest:
        ports.user_path_delete()
    else:
        ports.user_path_set(rest, kind)  # the type now in the registry, never converted
    return "removed", None


# ------------------------------------------------------------ EUTONOS_HOME
def env_home_effect(layout, ports, gen_id: str, *, apply=True, sink=None):
    """``EUTONOS_HOME`` as a user environment variable, so code outside the runtime (the Crosstalk kit, the project
    tools, the companion) finds a home that is not ``~/.eutonos``. Returns ``(Effect | None, status)``; status is
    ``added``, ``present`` (already this folder: nothing to record), ``planned``, ``conflict`` (the person set it to
    another folder: it is never changed) or ``not-windows``. Only the Windows user environment is written here."""
    value = str(layout.base)
    if ports.os_name != "windows":
        return None, "not-windows"
    current = ports.user_env_get(ENV_HOME)
    if current is not None and _same_entry(current, value):
        return None, "present"
    if current is not None:
        return None, "conflict"
    built = effect("env-var.eutonos-home", "core", "env-var", rf"reg:HKCU\Environment\{ENV_HOME}", selector=ENV_HOME,
                   permission_entry="user-env-home", before=None, after=sha256_bytes(value.encode("utf-8")),
                   shipped=sha256_bytes(value.encode("utf-8")),
                   undo={"op": "env-var-remove", "name": ENV_HOME, "value": value})
    if apply:
        if sink:
            sink(built)
        ports.user_env_set(ENV_HOME, value)
    return built, "added" if apply else "planned"


def find_remote_control_launchers(ports, os_name: str) -> list[dict]:
    """Startup-folder shortcuts that look like a Remote Control launcher the person already has: the file name or
    the shortcut's target or arguments mention remote control. The component's own shortcut is not listed. Read
    only; the person's launcher is never touched. Windows only."""
    if os_name != "windows":
        return []
    try:
        folder = Path(ports.startup_folder())
        names = sorted(item for item in folder.glob("*.lnk") if item.name.lower() != REMOTE_CONTROL_LNK.lower())[:40]
    except (OSError, RuntimeError):
        return []
    found = []
    for path in names:
        try:
            info = ports.shortcut_read(path) or {}
        except (OSError, RuntimeError):
            info = {}
        words = " ".join([path.stem, str(info.get("target", "")), str(info.get("args") or info.get("arguments") or "")])
        flat = words.lower().replace("-", " ").replace("_", " ")
        if "remote control" in flat or "remotecontrol" in flat:
            found.append({"name": path.name, "path": str(path), "target": info.get("target"),
                          "arguments": info.get("args") or info.get("arguments")})
    return found


# ------------------------------------------------------------ owner profile
def profile_effect(layout, gen_id: str, source: Path, *, apply=True, sink=None):
    """Copy the owner profile (contract f) to ``H/profiles/owner.json``: a recorded file-create."""
    data = Path(source).read_bytes()
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, dict) or document.get("profile") != "eutonos.owner-profile.v1":
        raise ValueError("not an eutonos.owner-profile.v1 file")
    target = layout.profiles / "owner.json"
    before = target.read_bytes() if target.exists() else None
    if before == data:
        return None
    backup = layout.backups(gen_id) / "profile.owner.json" if before is not None else None
    built = effect("profile.owner", "core", "file-create", target, before=sha256_bytes(before),
                   after=sha256_bytes(data), shipped=sha256_bytes(data), backup=backup,
                   undo={"op": "remove-file", "path": str(target)})
    if apply:
        if sink:
            sink(built)
        if backup is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before)
        write_atomic(target, data)
    return built


# ------------------------------------------------------------ instruction blocks
BLOCKS = (
    ("completion", cp, "eutonos:completion"),
    ("crosstalk", ci, "eutonos:crosstalk"),
    ("decisions", di, "eutonos:decisions"),
    ("memory", mi, "eutonos:memory"),
    ("simon", si, "eutonos:simon"),  # only while the Simon part is on (installer.COMPONENT_BLOCKS, needs_on)
)
# Uninstall finds a block's markers through this map, so a new block is registered by adding it to BLOCKS above.
MODULES = {name: module for name, module, _marker in BLOCKS}
# Blocks written only when the caller names them (`with_blocks` or `only_blocks`): an optional part's block must never
# appear because a caller forgot to leave it out.
OPT_IN_BLOCKS = frozenset({"simon"})


def _file_label(path: Path) -> str:
    return {"AGENTS.md": "codex-agents", "SOUL.md": "hermes-soul"}.get(path.name, "claude-claude")


def block_plan(name, module, layout, codex_home, claude_home, hermes_soul=None):
    """``(plan, wanted)``: one block's plan for the global files and the exact block bytes it would write. The
    install, the dry run and the readback all use this one selection. `hermes_soul` (the path of Hermes' SOUL.md,
    given only when the hermes component is installed) matters to the memory and Simon blocks alone. A block whose text differs
    per file puts its own ``shipped`` bytes on each row; `wanted` is then the Claude file's."""
    if module is cp:
        return (cp.instruction_plan(global_instructions=True, codex_home=codex_home, claude_home=claude_home),
                cp.block())
    if module is ci:
        return (ci.instruction_plan(root=layout.base, codex_home=codex_home, claude_home=claude_home, installed=True),
                ci.block(layout.base, True))
    if module is mi:
        return (mi.instruction_plan(base=layout.base, codex_home=codex_home, claude_home=claude_home,
                                    hermes_soul=hermes_soul),
                mi.block("claude", layout.base))
    if module is si:
        return (si.instruction_plan(base=layout.base, codex_home=codex_home, claude_home=claude_home,
                                    hermes_soul=hermes_soul),
                si.block("claude"))
    return (di.instruction_plan(global_instructions=True, codex_home=codex_home, claude_home=claude_home),
            di.block())


def _shipped_sha256(row, wanted) -> str:
    """The digest of the block this row's file is meant to hold: the row's own bytes (or digest), else `wanted`."""
    if row.get("shipped") is not None:
        return sha256_bytes(row["shipped"])
    return row.get("shipped_sha256") or sha256_bytes(wanted)


def _three_way(name, wanted, previous, row, reason):
    old = previous.get(f"instructions.{name}.{_file_label(Path(row['path']))}")
    return {"block": name, "target": row["path"], "reason": reason,
            "old_shipped_sha256": (old or {}).get("shipped_sha256"),
            "new_shipped_sha256": _shipped_sha256(row, wanted), "current_sha256": sha256_file(row["path"])}


def instruction_effects(layout, user_home: Path, gen_id: str, *, apply=True, previous: dict | None = None,
                        adopt=False, codex_home=None, claude_home=None, hermes_soul=None, sink=None,
                        skip_blocks=(), only_blocks=None, with_blocks=()):
    """The managed instruction blocks (completion, crosstalk, decisions, memory, simon) in the global Claude and Codex files
    and, for the memory block, Hermes' SOUL.md (`hermes_soul`, given only when the hermes component is installed;
    never created), as recorded file-block Effects.

    Returns ``(effects, three_way, notes)``; the notes are plain sentences for the person (a SOUL.md that is missing
    or nearly full). Per file and block: no copy adds the block; an intact marked block is
    refreshed in place (with `previous`, the earlier generation's effects by id, the first install's before-hash,
    backup and undo are kept); an exact unmarked copy of the text is wrapped in the markers in place (recorded with
    ``adoption: wrap``; uninstall takes out only the markers); a similar unmarked copy, or a customized marked block,
    writes nothing to that file and is listed for the semantic three-way step (``similar-unmarked-copy``, with the
    current and shipped hashes). Each Effect's undo records what was there before, so uninstall can put it back in
    place. `sink(effect)` hears each Effect before its block is written (write-ahead, so an interrupted run can
    still be recorded).

    `skip_blocks` names blocks to leave out (the block of a part that is switched off); `only_blocks`, when given, names
    the only blocks to look at (switching one part back on touches just its own block). A block in ``OPT_IN_BLOCKS`` is
    looked at only when `with_blocks` or `only_blocks` names it."""
    previous = previous or {}
    codex_home = codex_home or Path(user_home) / ".codex"
    claude_home = claude_home or Path(user_home) / ".claude"
    effects, three_way, notes = [], [], []
    for name, module, marker in BLOCKS:
        if name in skip_blocks or (only_blocks is not None and name not in only_blocks):
            continue
        if name in OPT_IN_BLOCKS and name not in with_blocks and name not in (only_blocks or ()):
            continue
        plan, wanted = block_plan(name, module, layout, codex_home, claude_home, hermes_soul)
        notes.extend(plan.get("notes", []))
        for row in plan.get("pending", []):
            three_way.append(_three_way(name, wanted, previous, row, row.get("code")))
        if plan["conflicts"]:
            for conflict in plan["conflicts"]:
                three_way.append(_three_way(name, wanted, previous, conflict, conflict.get("code")))
            continue
        for row in plan["changes"]:
            path = Path(row["path"])
            effect_id = f"instructions.{name}.{_file_label(path)}"
            if row["before"] == row["after"]:
                if adopt and effect_id not in previous:
                    effects.append(effect(effect_id, "core", "file-block", path, selector=marker,
                                          before=sha256_bytes(row["before"]), after=sha256_bytes(row["after"]),
                                          shipped=_shipped_sha256(row, wanted), adopted=True))
                continue
            earlier = previous.get(effect_id)
            backup = None
            mode = row.get("mode")
            if earlier is not None and mode == "replaced":
                built = dict(earlier, after_sha256=sha256_bytes(row["after"]), shipped_sha256=_shipped_sha256(row, wanted))
            else:
                if apply and row["before"] is not None:
                    layout.backups(gen_id).mkdir(parents=True, exist_ok=True)
                    backup = layout.backups(gen_id) / f"{effect_id}.md"
                    backup.write_bytes(row["before"])
                undo = {"op": "block-remove", "module": name, "created": row["before"] is None,
                        **cp.undo_shape(row["before"], row["after"], mode, module.PATTERN)}
                built = effect(effect_id, "core", "file-block", path, selector=marker,
                               before=sha256_bytes(row["before"]), after=sha256_bytes(row["after"]),
                               shipped=_shipped_sha256(row, wanted), backup=backup, undo=undo,
                               **({"adoption": "wrap"} if mode == "wrap" else {}))
            effects.append(built)
            if apply and sink:
                sink(built)
        if apply and any(row["before"] != row["after"] for row in plan["changes"]):
            result = module.install(plan)
            if result["outcome"] != "installed":
                raise OSError(f"instruction block {name} was not written: {result.get('error') or result['outcome']}")
    return effects, three_way, notes


# --------------------------------------------------------- windows migration
def _legacy_span(text: str):
    start = text.find(LEGACY_BEGIN)
    end = text.find(LEGACY_END)
    if start < 0 or end < start:
        return None
    end += len(LEGACY_END)
    if text[end:end + 2] == "\r\n":
        end += 2
    elif text[end:end + 1] == "\n":
        end += 1
    return start, end


def inspect_legacy(layout, ports, user_home: Path, os_name: str) -> dict:
    """Read-only look at what an earlier hand install left (adopt mode runs this first)."""
    report = {"codex_block": False, "startup_shortcut": None, "run_key": None, "runtime": None, "helper_src": None}
    for key, name in (("runtime", "runtime"), ("helper_src", "crosstalk-helper-src")):
        if (layout.base / name).is_dir():
            report[key] = str(layout.base / name)
    config = Path(user_home) / ".codex" / "config.toml"
    try:
        report["codex_block"] = _legacy_span(config.read_text(encoding="utf-8")) is not None
    except OSError:
        pass
    if os_name == "windows":
        try:
            shortcut = ports.startup_folder() / HELPER_SHORTCUT
            report["startup_shortcut"] = ports.shortcut_read(shortcut)
            report["run_key"] = ports.runkey_get(COMPANION_RUN_VALUE)
        except (OSError, RuntimeError, ImportError):
            pass
    return report


_RUN_PATTERN = re.compile(r'^\s*"?[^"]*?pythonw?(?:\.exe)?"?\s+(?:-[IB]\s+)*(?:-m\s+eutonos\.desktop_companion_runtime|'
                          r'"?[^"\s]*desktop_companion\.py"?)(?P<rest>.*)$', re.I | re.S)


def migrate_windows(layout, ports, user_home: Path, gen_id: str, *, apply=True, sink=None) -> list[dict]:
    """F12: adopt and repoint the legacy Codex block, the Startup shortcut and the Run key as recorded Effects.
    Each keeps the owner's earlier state in its undo so uninstall puts it back. ``H/runtime`` is never touched."""
    effects = []
    config = Path(user_home) / ".codex" / "config.toml"
    try:
        raw = config.read_bytes()
    except OSError:
        raw = None
    if raw is not None:
        text = raw.decode("utf-8", errors="replace")
        span = _legacy_span(text)
        if span:
            newline = "\r\n" if "\r\n" in text else "\n"
            new_block = permissions.codex_block_text(user_home, ports.os_name, eutonos_dir=layout.base, newline=newline)
            updated = text[:span[0]] + new_block + text[span[1]:]
            parsed = tomllib.loads(updated)
            if not isinstance((parsed.get("mcp_servers") or {}).get("crosstalk_p0"), dict):
                raise ValueError("the repointed Codex block would not parse back")
            backup = layout.backups(gen_id) / "migrate.codex-config.toml"
            built = effect("migrate.codex-block", "crosstalk", "toml-table", config,
                           selector="mcp_servers.crosstalk_p0", permission_entry="codex-mcp-block",
                           before=sha256_bytes(raw), after=sha256_bytes(updated.encode("utf-8")),
                           shipped=sha256_bytes(new_block.encode("utf-8")), backup=backup,
                           undo={"op": "text-replace", "old": text[span[0]:span[1]], "new": new_block},
                           adopted=True, repointed=True)
            if apply:
                if sink:
                    sink(built)
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(raw)
                write_atomic(config, updated.encode("utf-8"))
            effects.append(built)
    if ports.os_name != "windows":
        return effects
    launcher_helper = layout.bin / ports.launcher_name("eutonos-crosstalk-helper")
    try:
        shortcut = ports.startup_folder() / HELPER_SHORTCUT
    except (OSError, RuntimeError):
        shortcut = None
    info = ports.shortcut_read(shortcut) if shortcut is not None else None
    if info and not _same_entry(str(info.get("target", "")), str(launcher_helper)):
        before = shortcut.read_bytes()
        backup = layout.backups(gen_id) / "migrate.helper-shortcut.lnk"
        built = effect("migrate.startup-shortcut", "crosstalk", "shortcut", shortcut, before=sha256_bytes(before),
                       after=None, shipped=sha256_bytes(json.dumps([str(launcher_helper), "--watch"]).encode("utf-8")),
                       backup=backup, undo={"op": "shortcut-restore"}, adopted=True, repointed=True)
        if apply:
            if sink:
                sink(built)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before)
            ports.shortcut_write(shortcut, str(launcher_helper), "--watch", str(layout.bin), HELPER_DESCRIPTION)
            built["after_sha256"] = sha256_file(shortcut)
        effects.append(built)
    value = ports.runkey_get(COMPANION_RUN_VALUE)
    if value:
        launcher_companion = layout.bin / ports.launcher_name("eutonos-desktop-companion")
        match = _RUN_PATTERN.match(value)
        if match and str(layout.bin).lower() not in value.lower():
            new_value = f'"{launcher_companion}"{match.group("rest")}'
            built = effect("migrate.run-key", "companion", "shortcut",
                           rf"reg:HKCU\Software\Microsoft\Windows\CurrentVersion\Run\{COMPANION_RUN_VALUE}",
                           before=sha256_bytes(value.encode("utf-8")), after=sha256_bytes(new_value.encode("utf-8")),
                           shipped=sha256_bytes(str(launcher_companion).encode("utf-8")),
                           undo={"op": "runkey-restore", "name": COMPANION_RUN_VALUE, "old": value, "new": new_value},
                           adopted=True, repointed=True)
            if apply:
                if sink:
                    sink(built)
                ports.runkey_set(COMPANION_RUN_VALUE, new_value)
            effects.append(built)
    return effects


def adopted_permission_effects(layout, user_home: Path, os_name: str, choices: dict | None, data=None) -> list[dict]:
    """Adopt mode: an Effect (never removed) for each machine-wide permission entry that is already in place."""
    data = data or permissions.load()
    names = permissions.machine_entry_ids(data)
    plan = permissions.plan(None, user_home, choices=choices, os_name=os_name, data=data, eutonos_dir=layout.base,
                            only=names)
    found = []
    for item in plan["files"]:
        for name in item["satisfied"]:
            entry = permissions.entry_by_id(data, name)
            digest = sha256_file(item["file"])
            found.append(effect(f"adopted.{name}", entry["component"], permissions.entry_kind(name), item["file"],
                                selector=entry["key"], permission_entry=name, before=digest, after=digest,
                                shipped=None, adopted=True))
    return found


# --------------------------------------------------------------------- remove
class Remover:
    """Undo recorded Effects for an uninstall or rollback. Nothing here deletes data: a hosts folder, RAM, project
    data and backups are never listed as Effects."""

    def __init__(self, layout, ports, user_home: Path, os_name: str, tag: str = "uninstall"):
        self.layout, self.ports, self.user_home, self.os_name, self.tag = layout, ports, Path(user_home), os_name, tag

    def one(self, item: dict) -> tuple[str, str | None]:
        """``(state, reason)``; state is ``removed``, ``gone`` or ``kept``."""
        op = (item.get("undo") or {}).get("op", "none")
        if item.get("adopted") and op == "none":
            return "kept", "adopted: it was there before EUTONOS, so it stays"
        handler = getattr(self, "_" + op.replace("-", "_"), None)
        if op in PERMISSION_OPS:
            result = permissions.remove([item])
            if result["removed"]:
                return "removed", None
            return "kept", result["kept"][0]["reason"]
        if handler is None:
            return self._component_undo(item, op)
        return handler(item)

    def _component_undo(self, item, op):
        """An undo operation a component registered (``component_support.undo_handler``), run without the component
        itself (it may be held or skipped now). Without such a handler the Effect is left in place."""
        try:
            from . import component_support as support
        except ImportError:
            return "kept", f"unknown undo operation {op!r}; left in place"
        if op not in support.UNDO_HANDLERS:
            return "kept", f"unknown undo operation {op!r}; left in place"
        ctx = {"home": self.layout.base, "os": self.os_name, "user_home": self.user_home, "seams": self.ports.seams(),
               "gen_id": None, "record": None, "projects": [], "dry_run": False}
        done = support.remove_effects(ctx, [item])
        if done["removed"]:
            return "removed", None
        return "kept", (done["kept"][0]["reason"] if done["kept"] else f"{op} kept it")

    # -- folders of copied files
    def _files_delete(self, item):
        undo = item["undo"]
        base = Path(undo["dir"])
        kept = []
        for relative, digest in undo["files"].items():
            path = base / relative
            current = sha256_file(path)
            if current is None:
                continue
            if current != digest:
                kept.append(f"{relative} changed by you, kept")
                continue
            reason = _delete_file(path, self.tag)
            if reason:
                kept.append(f"{relative}: {reason}")
        if base.is_dir():
            for folder in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
            prune_old(base)
            try:
                base.rmdir()
            except OSError:
                pass
        return ("kept", "; ".join(kept)) if kept else ("removed", None)

    def _dir_delete(self, item):
        undo = item["undo"]
        path = Path(undo["path"])
        if not path.is_dir():
            return "gone", None
        if _is_inside(Path(sys.prefix), path):
            return "kept", "this is the running runtime; delete the folder after this process exits"
        digest = runtime_package_digest(self.ports.site_packages(path))
        if digest != item["after_sha256"]:
            if undo.get("building"):
                return "kept", "runtime build interrupted before verification; inspect partial bytes before retrying"
            return "kept", "changed by you (or not the installed bytes), kept"
        try:
            shutil.rmtree(path)
        except OSError as exc:
            return "kept", f"could not delete it ({type(exc).__name__}); close what uses it and delete the folder"
        return "removed", None

    def _kit_delete(self, item):
        undo = item["undo"]
        path = Path(undo["path"])
        if not path.is_dir():
            return "gone", None
        if not kits_module.verify(path):
            if undo.get("building"):
                return "kept", "kit copy interrupted before verification; inspect partial bytes before retrying"
            return "kept", "changed by you, kept"
        try:
            shutil.rmtree(path)
        except OSError as exc:
            return "kept", f"could not delete it ({type(exc).__name__})"
        return "removed", None

    def _remove_file(self, item):
        path = Path(item["undo"]["path"])
        current = sha256_file(path)
        if current is None:
            return "gone", None
        if current != item["after_sha256"]:
            return "kept", "changed by you, kept"
        if item.get("before_sha256") is None:
            path.unlink()
        else:
            backup = Path(item["backup"]) if item.get("backup") else None
            if backup is None or sha256_file(backup) != item["before_sha256"]:
                return "kept", "backup is missing or changed; kept"
            write_atomic(path, backup.read_bytes())
        return "removed", None

    def _block_remove(self, item):
        """Take the managed block out of the file where it now is, putting back what was there before it (the
        undo's recorded shape; an older undo without one removes just the block). Edits elsewhere in the file stay;
        a block changed inside is kept."""
        target = Path(item["target"])
        try:
            current = cp.read(target)
        except (OSError, ValueError) as exc:
            return "kept", str(exc)
        if current is None:
            return "gone", None
        undo = item["undo"]
        if sha256_bytes(current) == item["after_sha256"]:
            if item.get("before_sha256") is None:
                target.unlink()
                return "removed", None
            backup = Path(item["backup"]) if item.get("backup") else None
            if backup is not None and sha256_file(backup) == item["before_sha256"]:
                write_atomic(target, backup.read_bytes())
                return "removed", None
        module = MODULES.get(undo.get("module"), cp)
        try:
            after, state = cp.restore_block(current, undo, module.PATTERN, module.START, module.END)
        except ValueError as exc:
            return "kept", str(exc)
        if state == "absent":
            return "gone", None
        if state == "customized":
            return "kept", "changed by you, kept"
        if not after and undo.get("created"):
            target.unlink()
        else:
            write_atomic(target, after)
        return "removed", None

    def _path_remove(self, item):
        """Take out only the entry EUTONOS added to the user PATH (see ``remove_path_entry``)."""
        return remove_path_entry(self.ports, item["undo"])

    def _env_var_remove(self, item):
        undo = item["undo"]
        current = self.ports.user_env_get(undo["name"])
        if current is None:
            return "gone", None
        if not _same_entry(current, undo["value"]):
            return "kept", "changed by you, kept"
        self.ports.user_env_delete(undo["name"])
        return "removed", None

    def _text_replace(self, item):
        target = Path(item["target"])
        try:
            text = target.read_bytes().decode("utf-8")
        except OSError:
            return "gone", None
        undo = item["undo"]
        if undo["new"] not in text:
            return "kept", "changed by you, kept"
        write_atomic(target, text.replace(undo["new"], undo["old"], 1).encode("utf-8"))
        return "removed", None

    def _runkey_restore(self, item):
        undo = item["undo"]
        current = self.ports.runkey_get(undo["name"])
        if current is None:
            return "gone", None
        if current != undo["new"]:
            return "kept", "changed by you, kept"
        self.ports.runkey_set(undo["name"], undo["old"])
        return "removed", None

    def _shortcut_restore(self, item):
        target = Path(item["target"])
        current = sha256_file(target)
        if current is None:
            return "gone", None
        if current != item["after_sha256"]:
            return "kept", "changed by you, kept"
        backup = Path(item["backup"]) if item.get("backup") else None
        if backup is None or sha256_file(backup) != item["before_sha256"]:
            return "kept", "backup is missing or changed; kept"
        write_atomic(target, backup.read_bytes())
        return "removed", None

    def _app_command(self, item):
        """Run the undo's ``argv``, or, when it carries ``argvs``, each of those commands in order, stopping at the first
        that fails (a later step must not run on a failed earlier one)."""
        undo = item["undo"]
        for argv in (undo["argvs"] if undo.get("argvs") else [undo["argv"]]):
            code, out, err = self.ports.run(argv)
            if code != 0:
                return "kept", (err or out)[-200:] or f"exit {code}"
        return "removed", None

    def _none(self, item):
        return "kept", "no undo recorded; left in place"
