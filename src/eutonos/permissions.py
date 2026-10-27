"""The shipped permissions list and its writers (IPC2 contracts b and d).

`permissions.json` is the one list of every approval and setting EUTONOS asks for. This module loads and
validates it, plans the exact additions per file, applies only the missing ones (after a backup) and returns
contract-(b) Effects, and removes only what an Effect recorded while the target still holds the recorded bytes.

Hard limits, enforced here and by `validate`:
- bypass mode is never written;
- `autoMode` and `defaultMode` never go into a user-wide file;
- the only user-wide approvals are entries 4, 6 and 10 (Crosstalk's own tools);
- a rule the user added is never changed or removed;
- a git-tracked target is reported and never edited;
- nothing here writes under an app's own data folder; the Desktop readback reads two keys of session records.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import home as eutonos_home
from .hidden_process import no_window

PROFILE = "eutonos.permissions.v1"
PLAN_PROFILE = "eutonos.permissions.plan.v1"
FIELDS = ("id", "app", "scope", "file", "key", "value", "feature", "component", "why", "required", "default",
          "writer", "readback", "verdict")
APPS = ("claude", "claude-desktop", "claude-org", "codex", "hermes", "os")
SCOPES = ("project-local", "project", "per-launch", "user", "app-setting", "org-admin")
DEFAULTS = ("on", "off", "held")
WRITERS = ("installer", "launch-flag", "guided", "admin-only")
VERDICTS = ("ship", "drop")
# The ``outside_effects`` section: effects outside the repository that are not permissions entries.
OUTSIDE_FIELDS = ("id", "component", "kind", "os", "where", "why", "effects", "readback", "removal")
OUTSIDE_KINDS = ("file-create", "file-block", "json-entry", "toml-key", "toml-table", "text-line", "shortcut",
                 "scheduled-task", "launchd", "env-path", "env-var", "app-command", "host-enroll", "registry-value")
# The only entries that may touch a user-wide Claude or Codex file (the Crosstalk exception, owner 2026-10-22; the
# Simon Codex rule, owner 2026-10-26, written by the Simon part only while it is on).
USER_WIDE_WRITERS = frozenset({"set-remote-control", "crosstalk-reply", "codex-mcp-block", "simon-codex-rule"})
# Entries this module writes. The other installer entries belong to other stages (Hermes CLI, PATH, hook files).
WRITTEN = ("set-remote-control", "crosstalk-reply", "send-message", "native-send-message", "letter-rule", "record-keeping-rule",
           "codex-mcp-block", "codex-sandbox", "codex-record-rule")
EXCLUDED_FILES = {"send-message": "/.claude/settings.local.json", "native-send-message": "/.claude/settings.local.json",
                  "codex-sandbox": "/.codex/config.toml",
                  "codex-record-rule": "/.codex/rules/eutonos.rules"}
MARK = "eutonos permissions"
RULES_BODY = (
    "# Added by EUTONOS (permissions entry codex-record-rule). Allows only the installed record publisher;\n"
    "# every other command keeps Codex's normal sandbox and approval rules.\n"
    "prefix_rule(\n"
    "    pattern = [\"eutonos-settings\", \"delivery\", \"submit\"],\n"
    "    decision = \"allow\",\n"
    "    justification = \"EUTONOS record publisher: commits and pushes only the project's own board and RAM records.\",\n"
    "    match = [[\"eutonos-settings\", \"delivery\", \"submit\", \"--input\", \"request.json\"]],\n"
    "    not_match = [[\"eutonos-settings\", \"delivery\", \"configure\"], [\"git\", \"push\"]],\n"
    ")\n")


class PermissionsError(ValueError):
    pass


class PermissionsApplyError(PermissionsError):
    """`apply` failed part way. `.effects` holds every Effect already written, so a caller can record and undo them."""

    def __init__(self, message: str, effects: list[dict]):
        super().__init__(message)
        self.effects = list(effects)


# ------------------------------------------------------------------ the list
def load(path=None) -> dict:
    source = Path(path) if path else Path(__file__).with_name("permissions.json")
    return json.loads(source.read_text(encoding="utf-8"))


def validate(data: dict) -> list[str]:
    """Problems with a permissions document; an empty list means valid."""
    problems: list[str] = []
    if not isinstance(data, dict) or data.get("profile") != PROFILE:
        return ["profile must be " + PROFILE]
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return ["entries must be a non-empty list"]
    seen = set()
    for index, entry in enumerate(entries, 1):
        name = entry.get("id") if isinstance(entry, dict) else None
        label = f"entry {index} ({name})"
        if not isinstance(entry, dict) or tuple(entry) != FIELDS:
            problems.append(f"{label}: fields must be exactly {', '.join(FIELDS)}")
            continue
        if name in seen or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            problems.append(f"{label}: id must be a unique slug")
        seen.add(name)
        for field, allowed in (("app", APPS), ("scope", SCOPES), ("default", DEFAULTS), ("verdict", VERDICTS)):
            if entry[field] not in allowed:
                problems.append(f"{label}: {field} must be one of {', '.join(allowed)}")
        if not isinstance(entry["required"], bool):
            problems.append(f"{label}: required must be true or false")
        if entry["verdict"] == "ship" and entry["writer"] not in WRITERS:
            problems.append(f"{label}: a shipped entry needs a writer from {', '.join(WRITERS)}")
        if entry["verdict"] == "drop" and entry["writer"] is not None:
            problems.append(f"{label}: a dropped entry has no writer")
        file = entry["file"]
        if file is not None and not (isinstance(file, str) and file.startswith(("{root}/", "{home}/", "{eutonos_home}/"))):
            problems.append(f"{label}: file must be null or start with {{root}}/, {{home}}/ or {{eutonos_home}}/")
        for field in ("key", "feature", "component", "why", "readback"):
            if not isinstance(entry[field], str) or not entry[field].strip() or "\n" in entry[field]:
                problems.append(f"{label}: {field} must be one plain line")
        flat = json.dumps(entry["value"]).lower()
        if "bypass" in flat or entry["key"].lower().endswith("defaultmode") and entry["verdict"] == "ship":
            problems.append(f"{label}: bypass mode and defaultMode are never shipped")
        if entry["verdict"] == "ship" and isinstance(file, str) and file.startswith("{home}/"):
            if name not in USER_WIDE_WRITERS and entry["writer"] == "installer":
                problems.append(f"{label}: only entries {', '.join(sorted(USER_WIDE_WRITERS))} may write a user-wide file")
            if re.search(r"automode|defaultmode", entry["key"], re.IGNORECASE):
                problems.append(f"{label}: autoMode and defaultMode never go in a user-wide file")
        if entry["verdict"] == "ship" and entry["default"] == "held" and entry["required"]:
            problems.append(f"{label}: a held entry is not required")
    steps = data.get("guided_steps")
    if not isinstance(steps, dict):
        problems.append("guided_steps must be an object")
        steps = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("verdict") == "ship" and entry.get("writer") in ("guided", "admin-only"):
            if not isinstance(steps.get(entry.get("id")), str) or not steps[entry["id"]].strip():
                problems.append(f"{entry.get('id')}: a guided entry needs exact steps in guided_steps")
    for name in steps:
        if name not in seen:
            problems.append(f"guided_steps names unknown entry {name}")
    removal = data.get("guided_removal_steps")
    if not isinstance(removal, dict) or set(removal) != set(steps) or any(not isinstance(value, str) or not value.strip() for value in (removal or {}).values()):
        problems.append("guided_removal_steps must give inverse guidance for every guided step")
    for item in data.get("tuning") or []:
        if not isinstance(item, dict) or set(item) != {"id", "app", "file", "key", "recommended", "why"}:
            problems.append("a tuning item must have id, app, file, key, recommended and why")
    for item in data.get("referenced_identifiers") or []:
        if not isinstance(item, dict) or set(item) != {"identifier", "why"}:
            problems.append("a referenced identifier must have identifier and why")
    problems += _outside_problems(data.get("outside_effects"))
    return problems


def _outside_problems(items) -> list[str]:
    """Problems with the ``outside_effects`` section: each outside-the-repository effect that is not a permissions
    entry (a Startup shortcut, the scheduled task, skills, instruction blocks, the plugin registration, a registry
    value) is declared here with what it is, who removes it and how the doctor reads it back."""
    if items is None:
        return []
    if not isinstance(items, list):
        return ["outside_effects must be a list"]
    problems, seen = [], set()
    for index, item in enumerate(items, 1):
        name = item.get("id") if isinstance(item, dict) else None
        label = f"outside effect {index} ({name})"
        if not isinstance(item, dict) or tuple(item) != OUTSIDE_FIELDS:
            problems.append(f"{label}: fields must be exactly {', '.join(OUTSIDE_FIELDS)}")
            continue
        if name in seen or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            problems.append(f"{label}: id must be a unique slug")
        seen.add(name)
        if item["kind"] not in OUTSIDE_KINDS:
            problems.append(f"{label}: kind must be one of {', '.join(OUTSIDE_KINDS)}")
        if not isinstance(item["os"], list) or not item["os"] or not set(item["os"]) <= {"windows", "macos"}:
            problems.append(f"{label}: os must list windows and/or macos")
        for field in ("component", "where", "why", "readback", "removal"):
            if not isinstance(item[field], str) or not item[field].strip() or "\n" in item[field]:
                problems.append(f"{label}: {field} must be one plain line")
        if not isinstance(item["effects"], list) or not all(isinstance(value, str) and value for value in item["effects"]):
            problems.append(f"{label}: effects must be a list of effect id prefixes")
        elif item["readback"] == "effect" and not item["effects"]:
            problems.append(f"{label}: an effect readback needs at least one effect id prefix")
    return problems


def entries(data: dict | None = None, *, verdict: str = "ship") -> list[dict]:
    return [entry for entry in (data or load())["entries"] if entry["verdict"] == verdict]


def entry_by_id(data: dict, entry_id: str) -> dict:
    for entry in data["entries"]:
        if entry["id"] == entry_id:
            return entry
    raise PermissionsError("unknown permission entry: " + entry_id)


def active_choices(data: dict, choices: dict | None) -> dict:
    """Entry id -> "on"/"off" for every entry; refuses choices the contract does not allow."""
    result = {entry["id"]: entry["default"] for entry in data["entries"]}
    for name, value in (choices or {}).items():
        entry = entry_by_id(data, name)
        if value not in ("on", "off"):
            raise PermissionsError(f"choice for {name} must be on or off")
        if entry["verdict"] != "ship" or entry["default"] != "on":
            raise PermissionsError(f"{name} cannot be switched by a choice")
        if value == "off" and (entry["required"] or entry["writer"] != "installer"):
            raise PermissionsError(f"{name} is required and cannot be turned off")
        result[name] = value
    return result


# ---------------------------------------------------------------- locations
def _os_name(os_name: str | None) -> str:
    return os_name or ("windows" if sys.platform == "win32" else "macos")


def locations(root, user_home=None, os_name: str | None = None, *, eutonos_dir=None, home=None) -> dict:
    """Resolved folders. An explicit `user_home` (the person's home, F11) is used as given; with none, CLAUDE_CONFIG_DIR
    and CODEX_HOME apply. `eutonos_dir` is the EUTONOS home (contract c `ctx.home`) that settings, launchers and
    backups use; with none it comes from `eutonos.home`. `root` may be None for machine-wide entries. `home=` is the
    old name of `user_home`."""
    user_home = user_home if user_home is not None else home
    explicit = user_home is not None
    user = Path(user_home) if explicit else Path.home()
    claude = user / ".claude"
    codex = user / ".codex"
    if not explicit:
        claude = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else claude
        codex = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else codex
    return {"root": None if root is None else Path(os.path.abspath(root)), "home": user, "claude": claude,
            "codex": codex, "eutonos_home": Path(eutonos_dir) if eutonos_dir is not None else eutonos_home.home(),
            "os": _os_name(os_name)}


def _fill(template: str, where: dict) -> Path:
    if template.startswith("{home}/.claude/"):
        return where["claude"] / template[len("{home}/.claude/"):]
    if template.startswith("{home}/.codex/"):
        return where["codex"] / template[len("{home}/.codex/"):]
    if template.startswith("{eutonos_home}/settings/"):
        return where["eutonos_home"] / "settings" / template[len("{eutonos_home}/settings/"):]
    for token, key in (("{root}/", "root"), ("{home}/", "home"), ("{eutonos_home}/", "eutonos_home")):
        if template.startswith(token):
            if where[key] is None:
                raise PermissionsError("this entry needs a project root: " + template)
            return where[key] / template[len(token):]
    raise PermissionsError("unresolvable file template: " + template)


def launcher(where: dict, name: str) -> str:
    return str(where["eutonos_home"] / "bin" / (name + (".exe" if where["os"] == "windows" else "")))


# ------------------------------------------------------------ file helpers
def sha256(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


_OVERLAY: dict | None = None  # path -> bytes a dry run "wrote"; set only inside apply(dry_run=True)


def _read(path: Path) -> bytes | None:
    if _OVERLAY is not None and str(path) in _OVERLAY:
        return _OVERLAY[str(path)]
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + f".eutonos-tmp-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _missing_dirs(path: Path) -> list[Path]:
    missing = []
    folder = path.parent
    while not folder.exists() and folder != folder.parent:
        missing.append(folder)
        folder = folder.parent
    return list(reversed(missing))


def _dump_json(value, original: str | None) -> bytes:
    """Serialise keeping the original's indent, final newline and CRLF line ends; a new file gets indent 2 and a newline."""
    indent, newline, crlf = 2, "\n", False
    if original is not None:
        match = re.search(r"\n([ \t]+)\S", original)
        if match:
            indent = "\t" if match.group(1).startswith("\t") else len(match.group(1))
        newline = "\n" if original.endswith("\n") else ""
        crlf = "\r\n" in original
    text = json.dumps(value, indent=indent, ensure_ascii=False) + newline
    return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")


def _git(root: Path, *args: str):
    environment = dict(os.environ, LC_ALL="C", LANGUAGE="C")  # so "not a git repository" is matched in English
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30,
                              env=environment, **no_window())
    except (OSError, subprocess.SubprocessError):
        return None


def _not_a_repository(result) -> bool:
    """True only when git itself says the folder is in no repository (not dubious ownership, a bad index, ...)."""
    return result is not None and result.returncode != 0 and "not a git repository" in (result.stderr or "").lower()


def tracked(root: Path, path: Path) -> bool | None:
    """True when the file is git-tracked, False when it is not, None when that cannot be told."""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        return False
    if relative.startswith(".."):
        return False
    result = _git(root, "ls-files", "-z", "--", relative)
    if result is None:
        return None if (root / ".git").exists() else False
    if result.returncode != 0:
        # Only "not a git repository" means untracked. Any other failure (dubious ownership, a corrupt or locked
        # index) cannot confirm it, so the caller must not edit the file.
        return False if _not_a_repository(result) else None
    return bool(result.stdout.strip("\0 \n"))


def _exclude_lookup(root: Path) -> tuple[Path | None, str | None]:
    """(exclude file, None), or (None, "no-repository" | "unknown")."""
    result = _git(root, "rev-parse", "--git-path", "info/exclude")
    if result is None:
        return None, ("unknown" if (root / ".git").exists() else "no-repository")
    if result.returncode != 0:
        return None, "no-repository" if _not_a_repository(result) else "unknown"
    if not result.stdout.strip():
        return None, "unknown"
    path = Path(result.stdout.strip())
    return (path if path.is_absolute() else root / path), None


def exclude_file(root: Path) -> Path | None:
    """The repository's `info/exclude`, or None when there is no repository or git could not say."""
    return _exclude_lookup(root)[0]


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_toml(text: str):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


# ------------------------------------------------------------- operations
def _op(entry, kind, target, selector, status, **extra):
    return {"entry": entry["id"], "kind": kind, "target": target, "selector": selector, "status": status, **extra}


def _values(entry) -> list:
    return list(entry["value"]) if isinstance(entry["value"], list) else [entry["value"]]


def _json_list_op(entry, where, target, raw):
    pointer = entry["key"]
    parts = pointer.strip("/").split("/")
    values = _values(entry)
    if raw is None:
        return _op(entry, "json-entry", target, pointer, "add", add=values)
    try:
        text = raw.decode("utf-8")
        document = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return _op(entry, "json-entry", target, pointer, "unreadable", reason="not valid JSON; left alone")
    if text.startswith("﻿") or not isinstance(document, dict):
        return _op(entry, "json-entry", target, pointer, "unreadable", reason="not a plain JSON object; left alone")
    node = document
    for part in parts[:-1]:
        child = node.get(part, {})
        if not isinstance(child, dict):
            return _op(entry, "json-entry", target, pointer, "conflict", reason=f"{part} is not an object; left alone")
        node = child
    current = node.get(parts[-1], [])
    if not isinstance(current, list):
        return _op(entry, "json-entry", target, pointer, "conflict", reason=f"{parts[-1]} is not a list; left alone")
    missing = [value for value in values if value not in current]
    return _op(entry, "json-entry", target, pointer, "add" if missing else "present", add=missing)


def _block(entry_id: str, body: str, newline: str) -> str:
    return newline.join([f"# >>> {MARK} {entry_id} >>>", *body.rstrip("\r\n").splitlines(), f"# <<< {MARK} {entry_id} <<<"]) + newline


def _mcp_block(entry, where, newline) -> str:
    value = entry["value"]
    lines = [f"[{entry['key']}]", f"command = {_toml_string(launcher(where, 'eutonos-crosstalk-mcp'))}",
             f"startup_timeout_sec = {value['startup_timeout_sec']}", f"tool_timeout_sec = {value['tool_timeout_sec']}",
             f"default_tools_approval_mode = {_toml_string(value['default_tools_approval_mode'])}"]
    if where["os"] == "windows":
        lines.append("env_vars = [" + ", ".join(_toml_string(item) for item in value["env_vars"]) + "]")
    return _block(entry["id"], "\n".join(lines), newline)


def codex_block_text(user_home=None, os_name: str | None = None, *, eutonos_dir=None, newline: str = "\n",
                     data: dict | None = None) -> str:
    """The managed ``mcp_servers.crosstalk_p0`` block (entry 10) as `plan` would write it, for the installer's
    migration of the legacy block."""
    data = data or load()
    where = locations(None, user_home, os_name, eutonos_dir=eutonos_dir)
    return _mcp_block(entry_by_id(data, "codex-mcp-block"), where, newline)


def entry_kind(entry_id: str) -> str:
    """The Effect kind an entry is recorded with."""
    if entry_id not in _KINDS:
        raise PermissionsError("no writer for " + entry_id)
    return _KINDS[entry_id]


def _toml_text_op(entry, where, target, raw):
    text = "" if raw is None else raw.decode("utf-8", errors="replace")
    parsed = {} if raw is None or not text.strip() else _parse_toml(text)
    if parsed is None or (raw is not None and text.startswith("﻿")):
        return _op(entry, "toml-table", target, entry["key"], "unreadable", reason="not valid TOML; left alone")
    current = (parsed.get("mcp_servers") or {}).get("crosstalk_p0")
    if current is not None:
        if isinstance(current, dict) and current.get("default_tools_approval_mode") == entry["value"]["default_tools_approval_mode"]:
            return _op(entry, "toml-table", target, entry["key"], "present")
        return _op(entry, "toml-table", target, entry["key"], "kept",
                   reason="a crosstalk_p0 block you or an earlier install wrote is already there; left unchanged")
    newline = _newline(text)
    block = _mcp_block(entry, where, newline)
    prefix = text + (newline if text and not text.endswith("\n") else "")
    if prefix.strip():
        prefix += newline
    updated = prefix + block
    check = _parse_toml(updated)
    server = ((check or {}).get("mcp_servers") or {}).get("crosstalk_p0")
    if not isinstance(server, dict) or server.get("default_tools_approval_mode") != "approve":
        return _op(entry, "toml-table", target, entry["key"], "unreadable", reason="the block would not parse back")
    return _op(entry, "toml-table", target, entry["key"], "add", add=block, text=block, updated=updated.encode("utf-8"),
               add_summary=[f"{entry['key']}.default_tools_approval_mode = approve"])


def _toml_keys_op(entry, where, target, raw):
    text = "" if raw is None else raw.decode("utf-8", errors="replace")
    parsed = {} if raw is None or not text.strip() else _parse_toml(text)
    if parsed is None or (raw is not None and text.startswith("﻿")):
        return _op(entry, "toml-key", target, entry["key"], "unreadable", reason="not valid TOML; left alone")
    missing, kept = {}, []
    for key, value in entry["value"].items():
        if key not in parsed:
            missing[key] = value
        elif parsed[key] != value:
            kept.append(f"{key} is {parsed[key]!r} (you set it); left unchanged")
    if not missing:
        return _op(entry, "toml-key", target, entry["key"], "kept" if kept else "present", reason="; ".join(kept))
    newline = _newline(text)
    block = _block(entry["id"], "\n".join(f"{key} = {_toml_string(value)}" for key, value in missing.items()), newline)
    updated = block + text
    check = _parse_toml(updated)
    if check is None or any(check.get(key) != value for key, value in missing.items()):
        return _op(entry, "toml-key", target, entry["key"], "unreadable", reason="the keys would not parse back")
    return _op(entry, "toml-key", target, entry["key"], "add", add=missing, text=block, updated=updated.encode("utf-8"),
               reason="; ".join(kept))


def _rules_op(entry, where, target, raw):
    if raw is None:
        return _op(entry, "file-create", target, "prefix_rule", "add", add=RULES_BODY, text=RULES_BODY,
                   updated=RULES_BODY.encode("utf-8"))
    if raw == RULES_BODY.encode("utf-8"):
        return _op(entry, "file-create", target, "prefix_rule", "present")
    return _op(entry, "file-create", target, "prefix_rule", "kept",
               reason="a rules file you wrote is already there; left unchanged")


_KINDS = {"set-remote-control": "json-entry", "crosstalk-reply": "json-entry", "send-message": "json-entry",
          "native-send-message": "json-entry",
          "letter-rule": "json-entry", "record-keeping-rule": "json-entry", "codex-mcp-block": "toml-table",
          "codex-sandbox": "toml-key", "codex-record-rule": "file-create"}


def _entry_op(entry, where):
    """The operation for one entry on the file as it is now. A read error is an `unreadable` status, never an
    exception. `read_sha256` is the hash of the bytes this operation was computed from (None: file absent)."""
    name = entry["id"]
    if name not in _KINDS:
        raise PermissionsError("no writer for " + name)
    target = _fill(entry["file"], where)
    try:
        raw = _read(target)
    except OSError as exc:
        return _op(entry, _KINDS[name], target, entry["key"], "unreadable",
                   reason=f"could not read it ({type(exc).__name__}); left alone")
    if name in ("set-remote-control", "crosstalk-reply", "send-message", "native-send-message", "letter-rule", "record-keeping-rule"):
        operation = _json_list_op(entry, where, target, raw)
    elif name == "codex-mcp-block":
        operation = _toml_text_op(entry, where, target, raw)
    elif name == "codex-sandbox":
        operation = _toml_keys_op(entry, where, target, raw)
    else:
        operation = _rules_op(entry, where, target, raw)
    operation["read_sha256"] = sha256(raw)
    if operation["status"] in ("add", "present") and entry["scope"] in ("project", "project-local"):
        state = tracked(where["root"], operation["target"])
        if state is not False:
            operation.update(status="tracked", reason=("this file is tracked by git, so it is not edited; add the "
                                                      "entry by hand" if state else "git could not confirm this file "
                                                      "is untracked, so it is not edited"))
    return operation


def _active_written(data: dict, choices: dict | None, only=None) -> list[dict]:
    chosen = active_choices(data, choices)
    return [entry_by_id(data, name) for name in WRITTEN if chosen[name] == "on" and (only is None or name in only)]


def machine_entry_ids(data: dict | None = None) -> list[str]:
    """Written entries that need no project root: their file is under `{home}/` or `{eutonos_home}/` (4, 6, 7, 8, 10)."""
    data = data or load()
    return [name for name in WRITTEN
            if entry_by_id(data, name)["file"].startswith(("{home}/", "{eutonos_home}/"))]


def _exclude_ops(where, operations) -> tuple[list[dict], list[str]]:
    """text-line operations for the project files this run adds, unless a file is tracked."""
    adding = [op for op in operations if op["status"] == "add" and op["entry"] in EXCLUDED_FILES]
    if not adding:
        return [], []
    path, why = _exclude_lookup(where["root"])
    if path is None:
        return [], ["no git repository here, so no .git/info/exclude lines were added" if why == "no-repository"
                    else "git could not be asked about .git/info/exclude, so no exclude lines were added"]
    try:
        present = set((_read(path) or b"").decode("utf-8", errors="replace").splitlines())
    except OSError:
        return [], ["could not read .git/info/exclude, so no exclude lines were added"]
    entry = {"id": "git-exclude"}
    lines = dict.fromkeys(EXCLUDED_FILES[op["entry"]] for op in adding)  # two entries can share one file
    return [_op(entry, "text-line", path, line, "add", add=line) for line in lines if line not in present], []


def plan(root, user_home=None, *, choices: dict | None = None, os_name: str | None = None, data: dict | None = None,
         eutonos_dir=None, only=None, home=None) -> dict:
    """The exact additions per file. Writes nothing. `only` limits it to those entry ids."""
    data = data or load()
    where = locations(root, user_home if user_home is not None else home, os_name, eutonos_dir=eutonos_dir)
    operations = [_entry_op(entry, where) for entry in _active_written(data, choices, only)]
    extra, notes = _exclude_ops(where, operations)
    chosen = active_choices(data, choices)
    files: dict[str, dict] = {}
    for operation in operations + extra:
        item = files.setdefault(str(operation["target"]), {
            "file": str(operation["target"]), "exists": operation["target"].exists(), "additions": [],
            "satisfied": [], "kept": [], "blocked": []})
        if operation["status"] == "add" and operation.get("reason"):
            item["kept"].append({"entry": operation["entry"], "reason": operation["reason"]})
        if operation["status"] == "add":
            item["additions"].append({"entry": operation["entry"],
                                      "kind": operation["kind"], "selector": operation["selector"],
                                      "add": operation["add"]})
        elif operation["status"] == "present":
            item["satisfied"].append(operation["entry"])
        elif operation["status"] == "kept":
            item["kept"].append({"entry": operation["entry"], "reason": operation.get("reason", "")})
        else:
            item["blocked"].append({"entry": operation["entry"], "status": operation["status"],
                                    "reason": operation.get("reason", "")})
    steps = data["guided_steps"]
    return {
        "profile": PLAN_PROFILE, "root": None if where["root"] is None else str(where["root"]),
        "home": str(where["home"]), "os": where["os"],
        "files": list(files.values()), "notes": notes,
        "guided": [{"entry": entry["id"], "steps": steps[entry["id"]]} for entry in entries(data)
                   if entry["writer"] in ("guided", "admin-only") and entry["default"] != "held"],
        "held": [entry["id"] for entry in entries(data) if entry["default"] == "held"],
        "elsewhere": [{"entry": entry["id"], "writer": entry["writer"]} for entry in entries(data)
                      if entry["writer"] in ("launch-flag", "installer") and entry["id"] not in WRITTEN
                      and entry["default"] != "held"],
        "off": [name for name in WRITTEN if chosen[name] == "off"]}


# ------------------------------------------------------------------- apply
def _effect(operation, component, before, after, shipped, backup, undo):
    selector = operation["selector"]
    token = hashlib.sha256((str(operation["target"]) + "\0" + str(selector)).encode("utf-8")).hexdigest()[:8]
    return {"effect_id": f"{operation['entry']}.{token}", "component": component,
            "permission_entry": None if operation["kind"] == "text-line" else operation["entry"],
            "kind": operation["kind"], "target": str(operation["target"]), "selector": selector,
            "before_sha256": sha256(before), "after_sha256": sha256(after), "shipped_sha256": shipped,
            "backup": backup, "undo": undo}


def _backup(before: bytes | None, target: Path, gen_id: str, effect_id: str, base: Path | None = None) -> str | None:
    if before is None:
        return None
    folder = (base if base is not None else eutonos_home.home()) / "install" / "backups" / gen_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (effect_id + (target.suffix or ".bak"))
    path.write_bytes(before)
    return str(path)


def apply(root, user_home=None, *, choices: dict | None = None, os_name: str | None = None, gen_id: str = "standalone",
          data: dict | None = None, eutonos_dir=None, only=None, observer=None, home=None,
          dry_run: bool = False) -> list[dict]:
    """Add only the missing entries, after a backup of each existing target; return the Effects.
    Targets that are unreadable, tracked by git or already hold the user's own value are left alone.
    A read error is an `unreadable` status, not an exception, and each write first checks that the target still holds
    the bytes its operation was computed from. If a write fails, `PermissionsApplyError` carries the Effects already
    written (`.effects`), so nothing is left unrecorded. `only` limits it to those entry ids; `eutonos_dir` is the
    EUTONOS home. `observer(kind, info)` hears ``"intent"`` (target, before hash) just before each write and
    ``"effect"`` (the Effect) just after it, for the installer's journal.
    `dry_run` computes the same Effects (later entries see earlier ones) and writes nothing: no file, backup or folder."""
    global _OVERLAY
    if dry_run:
        _OVERLAY = {}
        try:
            return apply(root, user_home, choices=choices, os_name=os_name, gen_id=gen_id, data=data,
                         eutonos_dir=eutonos_dir, only=only, observer=None, home=home)
        finally:
            _OVERLAY = None
    data = data or load()
    problems = validate(data)
    if problems:
        raise PermissionsError("permissions list is invalid: " + "; ".join(problems))
    where = locations(root, user_home if user_home is not None else home, os_name, eutonos_dir=eutonos_dir)
    chosen = _active_written(data, choices, only)
    effects: list[dict] = []
    added: list[dict] = []
    try:
        for entry in chosen:
            operation = _entry_op(entry, where)  # computed on the files as they are now, after earlier entries
            if operation["status"] != "add":
                continue
            effects.append(_write_operation(entry, operation, where, gen_id, observer))
            added.append(operation)
        extra, _ = _exclude_ops(where, added)
        for operation in extra:
            effects.append(_write_operation({"component": "core", "id": "git-exclude"}, operation, where, gen_id, observer))
    except PermissionsApplyError:
        raise
    except (OSError, PermissionsError, ValueError) as exc:
        raise PermissionsApplyError(f"apply stopped after {len(effects)} write(s): {type(exc).__name__}: {exc}",
                                    effects) from exc
    return effects


def _write_operation(entry, operation, where, gen_id, observer=None):
    target = operation["target"]
    before = _read(target)
    if "read_sha256" in operation and sha256(before) != operation["read_sha256"]:
        raise PermissionsError(f"{target} changed while it was being planned; it was not written")
    kind = operation["kind"]
    dirs = _missing_dirs(target)
    if kind == "json-entry":
        parts = operation["selector"].strip("/").split("/")
        text = None if before is None else before.decode("utf-8")
        document = {} if text is None else json.loads(text)
        node = document
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(parts[-1], []).extend(operation["add"])
        after = _dump_json(document, text)
        shipped = sha256(json.dumps(operation["add"], sort_keys=True).encode("utf-8"))
        undo = {"op": "json-remove", "pointer": operation["selector"], "values": operation["add"]}
    elif kind in ("toml-table", "toml-key", "file-create"):
        after = operation["updated"]
        shipped = sha256(operation["text"].encode("utf-8"))
        undo = {"op": "file-delete" if kind == "file-create" else "text-remove", "text": operation["text"]}
    else:  # text-line
        text = "" if before is None else before.decode("utf-8", errors="replace")
        lead = "\n" if text and not text.endswith("\n") else ""
        after = (text + lead + operation["add"] + "\n").encode("utf-8")
        shipped = sha256((operation["add"] + "\n").encode("utf-8"))
        undo = {"op": "line-remove", "line": operation["add"]}
    undo["created_dirs"] = [str(item) for item in dirs]
    effect = _effect(operation, entry.get("component", "core"), before, after, shipped, None, undo)
    if _OVERLAY is not None:  # dry run: remember the result for later entries, write nothing
        _OVERLAY[str(target)] = after
        return effect
    effect["backup"] = _backup(before, target, gen_id, effect["effect_id"], where["eutonos_home"])
    if observer is not None:
        observer("intent", {"entry": operation["entry"], "effect_id": effect["effect_id"], "target": str(target),
                            "before_sha256": effect["before_sha256"], "after_sha256": effect["after_sha256"]})
        observer("effect", effect)  # durable undo before the atomic mutation, including a process kill
    try:
        for item in dirs:
            item.mkdir(exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(target, after)
    except OSError:
        for item in reversed(dirs):  # the target is unchanged (the replace did not happen); drop what this call made
            try:
                item.rmdir()
            except OSError:
                pass
        raise
    if observer is not None:
        observer("effect", effect)
    return effect


# ------------------------------------------------------------------ remove
def _drop_dirs(effect) -> None:
    for name in reversed((effect.get("undo") or {}).get("created_dirs") or []):
        try:
            Path(name).rmdir()
        except OSError:
            pass


def _remove_one(effect) -> str | None:
    """None when removed, else the reason it was kept."""
    target = Path(effect["target"])
    current = _read(target)
    undo = effect.get("undo") or {}
    if current is None:
        return "already gone"
    if sha256(current) == effect["after_sha256"]:  # untouched since: put the exact bytes back
        if effect.get("before_sha256") is None:
            target.unlink()
            _drop_dirs(effect)
            return None
        backup = Path(effect["backup"]) if effect.get("backup") else None
        original = _read(backup) if backup else None
        if original is not None and sha256(original) == effect["before_sha256"]:
            _write_atomic(target, original)
            return None
        return "backup is missing or changed; kept"
    operation = undo.get("op")
    text = current.decode("utf-8", errors="replace")
    if operation == "json-remove":
        try:
            document = json.loads(text)
            node = document
            parts = undo["pointer"].strip("/").split("/")
            for part in parts[:-1]:
                node = node[part]
            listed = node[parts[-1]]
        except (ValueError, KeyError, TypeError):
            return "changed by you, kept"
        gone = [value for value in undo["values"] if value in listed]
        if not gone:
            return "already gone"
        for value in gone:
            listed.remove(value)
        _write_atomic(target, _dump_json(document, text))
        return None
    if operation in ("text-remove", "file-delete") and undo.get("text") in text:
        if operation == "file-delete":
            if text != undo["text"]:
                return "changed by you, kept"
            target.unlink()
            _drop_dirs(effect)
            return None
        _write_atomic(target, text.replace(undo["text"], "", 1).encode("utf-8"))
        return None
    if operation == "line-remove":
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.rstrip("\r\n") == undo["line"]:
                del lines[index]
                _write_atomic(target, "".join(lines).encode("utf-8"))
                return None
    return "changed by you, kept"


def remove(effects: list[dict]) -> dict:
    """Undo recorded effects in reverse order. Each is removed only while its target still holds the recorded
    bytes or entry; otherwise it is kept and reported. Backups stay."""
    removed, kept = [], []
    for effect in reversed(effects):
        reason = _remove_one(effect)
        if reason is None:
            removed.append(effect["effect_id"])
        elif reason == "already gone":
            removed.append(effect["effect_id"])
        else:
            kept.append({"effect_id": effect["effect_id"], "reason": reason})
    return {"removed": removed, "kept": kept}


# --------------------------------------------------------------- readbacks
def _same_path(left, right) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def desktop_session_readback(root, *, home=None, local_app_data=None, os_name: str | None = None,
                             limit: int = 500, byte_limit: int = 16_000_000) -> dict:
    """Entry 2: newest matching Desktop session, bounded by file count and total bytes."""
    name = _os_name(os_name)
    if name == "windows":
        base = local_app_data or os.environ.get("LOCALAPPDATA")
        base = Path(base) / "Claude-Data" if base else None
    else:
        base = Path(home or Path.home()) / "Library" / "Application Support" / "Claude-Data"
    if base is None or not base.is_dir():
        return {"state": "unavailable", "permission_mode": None, "checked": 0}
    records = []
    try:
        for folder in base.glob("*/claude-code-sessions"):
            for path in folder.rglob("*.json"):
                try:
                    records.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    except OSError as exc:
        return {"state": "unknown", "permission_mode": None, "checked": 0,
                "reason": f"session listing failed ({type(exc).__name__})", "bytes_read": 0}
    records.sort(key=lambda item: item[0], reverse=True)
    checked = bytes_read = 0
    capped = len(records) > limit
    for _, path in records[:limit]:
        checked += 1
        try:
            size = path.stat().st_size
            if size > 4_000_000 or bytes_read + size > byte_limit:
                return {"state": "unknown", "permission_mode": None, "checked": checked - 1,
                        "bytes_read": bytes_read, "reason": "session readback reached its byte limit"}
            with path.open("rb") as stream:
                raw = stream.read(min(4_000_001, byte_limit - bytes_read + 1))
            if len(raw) > 4_000_000 or bytes_read + len(raw) > byte_limit:
                return {"state": "unknown", "permission_mode": None, "checked": checked - 1,
                        "bytes_read": bytes_read, "reason": "session readback reached its byte limit"}
            bytes_read += len(raw)
            record = json.loads(raw.decode("utf-8"),
                                object_pairs_hook=lambda pairs: {key: value for key, value in pairs
                                                                if key in ("cwd", "permissionMode")})
        except (OSError, ValueError):
            continue
        cwd = record.get("cwd") if isinstance(record, dict) else None
        mode = record.get("permissionMode") if isinstance(record, dict) else None
        if isinstance(cwd, str) and _same_path(cwd, os.path.abspath(root)):
            return {"state": "auto" if mode == "auto" else "not-auto", "permission_mode": mode if isinstance(mode, str) else None,
                    "checked": checked, "bytes_read": bytes_read}
    if capped:
        return {"state": "unknown", "permission_mode": None, "checked": checked, "bytes_read": bytes_read,
                "reason": "session listing reached its file limit"}
    return {"state": "no-session", "permission_mode": None, "checked": checked, "bytes_read": bytes_read}


def codex_trust_readback(root, home=None) -> dict:
    """Entry 14: is the project trusted in the Codex user config. Reads only the `projects` table."""
    where = locations(root, home)
    raw = _read(where["codex"] / "config.toml")
    if raw is None:
        return {"state": "absent"}
    parsed = _parse_toml(raw.decode("utf-8", errors="replace"))
    if parsed is None:
        return {"state": "unreadable"}
    for path, table in (parsed.get("projects") or {}).items():
        if isinstance(table, dict) and _same_path(path, where["root"]):
            return {"state": "trusted" if table.get("trust_level") == "trusted" else "untrusted",
                    "trust_level": table.get("trust_level")}
    return {"state": "absent"}


def tuning(home=None, *, data: dict | None = None, recommended: dict | None = None) -> list[dict]:
    """Current values beside recommended ones. Recommendations only: nothing here changes a value."""
    data = data or load()
    where = locations(Path.cwd(), home)
    documents: dict[str, object] = {}
    result = []
    for item in data.get("tuning") or []:
        target = _fill(item["file"], where)
        if str(target) not in documents:
            raw = _read(target)
            text = None if raw is None else raw.decode("utf-8", errors="replace")
            try:
                documents[str(target)] = (json.loads(text) if target.suffix == ".json" else _parse_toml(text)) if text else None
            except ValueError:
                documents[str(target)] = None
        document = documents[str(target)]
        key = item["key"].strip("/") if item["key"].startswith("/") else item["key"]
        current = document.get(key) if isinstance(document, dict) else None
        wanted = (recommended or {}).get(item["id"], item["recommended"])
        result.append({"id": item["id"], "app": item["app"], "setting": key, "recommended": wanted, "current": current,
                       "state": "not set" if current is None else ("matches" if current == wanted else "differs"),
                       "why": item["why"]})
    return result


def readbacks(root, home=None, **kwargs) -> dict:
    return {"desktop_auto": desktop_session_readback(root, home=home, **{k: v for k, v in kwargs.items()
                                                                          if k in ("local_app_data", "os_name", "limit", "byte_limit")}),
            "codex_trust": codex_trust_readback(root, home), "tuning": tuning(home)}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Show or validate the shipped permissions list; plans write nothing.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    shown = sub.add_parser("plan")
    shown.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if args.command == "validate":
        problems = validate(load())
        print(json.dumps({"valid": not problems, "problems": problems}, indent=2))
        return 1 if problems else 0
    print(json.dumps(plan(args.root), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
