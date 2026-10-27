"""Shared parts of the bundled components (IPC5 lane B, IPC2 contract c).

A component is a manifest (``components/<id>.json``) plus a module whose ``plan``, ``apply``, ``verify`` and
``remove`` take the contract-(c) ``ctx``: ``{home, os, gen_id, runtime_python, kit_path, projects, record, dry_run,
now}``. Two optional keys make every OS and app call injectable:

* ``user_home``: the folder that stands for the user's home (``~/.claude``, ``~/.codex``, ``~/.zprofile``, Startup).
  When absent it is the real home only if the EUTONOS home is the real ``~/.eutonos``; any other EUTONOS home gets
  ``<home>/staging-user``, so a staged or test install can never reach the real machine by omission. The one
  exception is a home whose state records the explicit ``install --for-this-user`` mark (``home.for_this_user``).
* ``seams``: ``{name: callable or object}`` replacing the real call (``claude``, ``shortcut``, ``task``, ``env_path``,
  ``launchctl``, ``hermes_runner`` and so on). Tests fake every one; nothing here runs a real command by itself.

Every write a component makes is a ``Step``: it can read the state it manages, say what the state should be, write
it and describe its own undo. ``plan_steps`` and ``apply_steps`` turn steps into contract-(b) Effects; a component
never writes the install record. Permission-list entries go through ``permissions.apply`` (restricted to the entries
of one component, and to user-wide or per-project scopes) so there is one writer for them.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from . import home as eutonos_home
from . import installer_ports
from . import permissions

PROFILE = "eutonos.component.v1"
KINDS = ("core", "bundled", "addon")
DEFAULTS = ("on", "off", "held")
STATES = ("installed", "held", "skipped", "unlicensed")
VERIFY_STATES = ("ok", "missing", "changed", "held", "unlicensed", "unknown")
EFFECT_KINDS = ("file-create", "file-block", "json-entry", "toml-key", "toml-table", "text-line", "shortcut",
                "scheduled-task", "launchd", "env-path", "env-var", "app-command", "host-enroll")
EFFECT_FIELDS = ("effect_id", "component", "permission_entry", "kind", "target", "selector", "before_sha256",
                 "after_sha256", "shipped_sha256", "backup", "undo")
FILE_UNDO_OPS = ("json-remove", "text-remove", "file-delete", "line-remove")
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ComponentError(ValueError):
    """A component could not plan, apply, verify or remove (the message is one plain sentence)."""


class ComponentApplyError(ComponentError):
    """``apply`` stopped part way; ``effects`` are the Effects already written, so the caller can still record them."""

    def __init__(self, message, effects):
        super().__init__(message)
        self.effects = effects


# --------------------------------------------------------------------------- context
def seam(ctx: dict, name: str, default=None):
    value = (ctx.get("seams") or {}).get(name)
    return default if value is None else value


def ctx_home(ctx: dict) -> Path:
    return Path(ctx["home"]) if ctx.get("home") else eutonos_home.home()


def os_name(ctx: dict) -> str:
    return ctx.get("os") or ("windows" if sys.platform == "win32" else "macos")


def user_home(ctx: dict) -> Path:
    if ctx.get("user_home"):
        return Path(ctx["user_home"])
    base = ctx_home(ctx)
    return Path.home() if eutonos_home.is_default(base) or eutonos_home.for_this_user(base) else base / "staging-user"


def gen_id(ctx: dict) -> str:
    return str(ctx.get("gen_id") or "standalone")


def choices(ctx: dict) -> dict:
    return ((ctx.get("record") or {}).get("consent") or {}).get("choices") or {}


def project_roots(ctx: dict) -> list[Path]:
    return [Path(item["root"]) for item in ctx.get("projects") or [] if isinstance(item, dict) and item.get("root")]


def recorded(ctx: dict, component: str) -> list[dict]:
    """The Effects the record already holds for ``component`` (read-only; used to carry them forward)."""
    return [item for item in (ctx.get("record") or {}).get("effects") or []
            if isinstance(item, dict) and item.get("component") == component]


def runtime_pythonw(ctx: dict) -> str:
    """The windowless interpreter beside the runtime's python (Windows); the console one elsewhere."""
    python = Path(ctx.get("runtime_python") or sys.executable)
    windowless = python.with_name("pythonw.exe")
    return str(windowless) if os_name(ctx) == "windows" and python.name.lower().startswith("python") else str(python)


@contextlib.contextmanager
def home_env(ctx: dict):
    """``EUTONOS_HOME`` is the context's home while the kit and the permissions writers run (both read the variable)."""
    before = os.environ.get("EUTONOS_HOME")
    os.environ["EUTONOS_HOME"] = str(ctx_home(ctx))
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("EUTONOS_HOME", None)
        else:
            os.environ["EUTONOS_HOME"] = before


def launcher_path(ctx: dict, name: str) -> Path:
    """A stable launcher under ``H/bin`` (``.exe`` on Windows)."""
    return ctx_home(ctx) / "bin" / (name + (".exe" if os_name(ctx) == "windows" else ""))


# ----------------------------------------------------------------------------- kit
def kit_module(ctx: dict, name: str):
    """Import ``name`` (for example ``crosstalk_p0_install``) from the context's kit folder. The kit cannot import
    ``eutonos``, so it is loaded by path; kit modules from an earlier kit are dropped first so two kits never mix."""
    kit = ctx.get("kit_path")
    if not kit or not (Path(kit) / (name + ".py")).is_file():
        raise ComponentError(f"the {name} kit module was not found in the kit folder")
    for loaded in [key for key in sys.modules if key.startswith("crosstalk_p0_")]:
        module = sys.modules[loaded]
        if os.path.normcase(os.path.dirname(getattr(module, "__file__", "") or "")) != os.path.normcase(str(Path(kit))):
            del sys.modules[loaded]
    if str(kit) not in sys.path:
        sys.path.insert(0, str(kit))
    return importlib.import_module(name)


# --------------------------------------------------------------------------- hashing
def sha256(data) -> str | None:
    if data is None:
        return None
    return hashlib.sha256(data if isinstance(data, bytes) else str(data).encode("utf-8")).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_effect(step: "Step", before, after, backup, undo: dict) -> dict:
    return {"effect_id": step.effect_id, "component": step.component,
            "permission_entry": step.entry, "kind": step.kind, "target": str(step.target),
            "selector": step.selector, "before_sha256": sha256(before), "after_sha256": sha256(after),
            "shipped_sha256": sha256(after), "backup": backup, "undo": undo}


# --------------------------------------------------------------------------- steps
class Step:
    """One thing a component manages. Subclasses say how to read, write and undo it; ``read`` and ``want`` return
    the bytes that identify the state (file contents, or a canonical JSON description), ``None`` meaning absent."""

    kind = "file-create"
    force_apply = False  # a compound step may need work even when its primary identity already matches
    replace = False  # True: a generated file EUTONOS owns outright, rewritten when it differs

    def __init__(self, component: str, name: str, target, selector=None, entry=None):
        self.component, self.name, self.target, self.selector, self.entry = component, name, target, selector, entry

    @property
    def effect_id(self) -> str:
        return f"{self.component}.{self.name}.{sha256(f'{self.target}' + chr(0) + f'{self.selector}')[:8]}"

    def read(self, ctx):
        raise NotImplementedError

    def want(self, ctx):
        raise NotImplementedError

    def write(self, ctx, data) -> None:
        raise NotImplementedError

    def write_effect(self, ctx, data, effect) -> None:
        self.write(ctx, data)

    def undo(self, ctx, before) -> dict:
        raise NotImplementedError

    def backup(self, ctx, before):
        return None

    def may_replace(self, ctx, current) -> bool:
        """May a different existing state be replaced? Files are the user's unless the step ``replace``s them."""
        return self.replace


class FileStep(Step):
    """A whole file. The file is created when absent; one that holds other bytes is the user's and is left alone
    unless the step ``replace``s (a generated file under the EUTONOS home)."""

    def __init__(self, component, name, target, content, *, entry=None, replace=False, kind="file-create"):
        super().__init__(component, name, Path(target), None, entry)
        self.content, self.replace, self.kind = content, replace, kind

    def read(self, ctx):
        return permissions._read(self.target)

    def want(self, ctx):
        return self.content if isinstance(self.content, bytes) else self.content.encode("utf-8")

    def write(self, ctx, data) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        permissions._write_atomic(self.target, data)

    def undo(self, ctx, before) -> dict:
        dirs = [str(item) for item in permissions._missing_dirs(self.target)]
        return {"op": "file-delete", "text": self.want(ctx).decode("utf-8"), "created_dirs": dirs}

    def backup(self, ctx, before):
        with home_env(ctx):
            return permissions._backup(before, self.target, gen_id(ctx), self.effect_id)


class TextLineStep(Step):
    """One marked line appended to a user file (``~/.zprofile``). Removed by exact line."""

    kind = "text-line"
    replace = True  # the file differs by design: the line is appended to it

    def __init__(self, component, name, target, line, *, entry=None):
        super().__init__(component, name, Path(target), line, entry)
        self.line = line

    def read(self, ctx):
        return permissions._read(self.target)

    def want(self, ctx):
        text = (self.read(ctx) or b"").decode("utf-8", errors="replace")
        if self.line in text.splitlines():
            return text.encode("utf-8")
        lead = "\n" if text and not text.endswith("\n") else ""
        return (text + lead + self.line + "\n").encode("utf-8")

    def write(self, ctx, data) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        permissions._write_atomic(self.target, data)

    def undo(self, ctx, before) -> dict:
        return {"op": "line-remove", "line": self.line,
                "created_dirs": [str(item) for item in permissions._missing_dirs(self.target)]}

    def backup(self, ctx, before):
        with home_env(ctx):
            return permissions._backup(before, self.target, gen_id(ctx), self.effect_id)


class StateStep(Step):
    """State outside a file the component can read back through an injected callable: ``getter(ctx)`` returns the
    observed state as a JSON-able value (or None), ``setter(ctx, value)`` makes it so, ``deleter(ctx, effect)`` undoes
    it. The state's identity is its canonical JSON."""

    replace = True  # existing different state is changed to the wanted one (and backed up)

    def __init__(self, component, name, kind, target, selector, desired, getter, setter, undo_op, undo_args=None,
                 *, entry=None):
        super().__init__(component, name, target, selector, entry)
        self.kind, self.desired, self.getter, self.setter = kind, desired, getter, setter
        self.undo_op, self.undo_args = undo_op, undo_args or {}

    def read(self, ctx):
        value = self.getter(ctx)
        return None if value is None else canonical(value)

    def want(self, ctx):
        value = self.desired(ctx) if callable(self.desired) else self.desired
        return canonical(value)

    def write(self, ctx, data) -> None:
        self.setter(ctx, json.loads(data))

    def undo(self, ctx, before) -> dict:
        return dict(self.undo_args, op=self.undo_op)

    def backup(self, ctx, before):
        with home_env(ctx):
            return permissions._backup(before, Path(str(self.target)), gen_id(ctx), self.effect_id)


UNDO_HANDLERS: dict = {}


def undo_handler(op: str):
    def register(function):
        UNDO_HANDLERS[op] = function
        return function
    return register


# --------------------------------------------------------------------- plan and apply
def _carried(ctx, step: Step, current, wanted):
    """The recorded Effect for this step when the state is still what it recorded (idempotent re-apply)."""
    for item in recorded(ctx, step.component):
        if item.get("effect_id") == step.effect_id and item.get("after_sha256") == sha256(wanted) == sha256(current):
            return item
    return None


def _needed(ctx, step: Step):
    """``(current, wanted, carried_effect)``; ``carried_effect`` is set when the step is already done."""
    current, wanted = step.read(ctx), step.want(ctx)
    carried = _carried(ctx, step, current, wanted)
    if carried is not None:
        return current, wanted, carried
    return current, wanted, None


def _may_replace(ctx, step, current) -> bool:
    earlier = next((item for item in recorded(ctx, step.component) if item.get("effect_id") == step.effect_id), None)
    if earlier and sha256(current) != earlier.get("after_sha256"):
        return False
    return step.may_replace(ctx, current)


def plan_steps(ctx: dict, steps: list) -> list[dict]:
    """The Effects ``apply_steps`` would produce, writing nothing. Present state that EUTONOS did not record is the
    user's and yields no Effect; a different existing value of a user file is kept and also yields none."""
    effects = []
    for step in steps:
        current, wanted, carried = _needed(ctx, step)
        if carried is not None:
            effects.append(carried)
        elif current == wanted and not step.force_apply:
            continue
        elif current is not None and not _may_replace(ctx, step, current):
            continue
        else:
            effects.append(make_effect(step, current, wanted, None, step.undo(ctx, current)))
    return effects


def apply_steps(ctx: dict, steps: list, effects: list | None = None, done: list | None = None) -> list[dict]:
    """Make each step's state so, one at a time, and return the Effects. ``effects`` (the plan, after the person's
    choices) limits the run to those Effect ids; ``None`` runs every step. Idempotent: a step already in the wanted
    state writes nothing and returns the Effect the record holds for it. A failure raises ``ComponentApplyError``
    carrying every Effect written so far (``done`` is that running list, shared with the caller)."""
    allowed = None if effects is None else {item["effect_id"] for item in effects}
    result = done if done is not None else []
    for step in steps:
        if allowed is not None and step.effect_id not in allowed:
            continue
        current, wanted, carried = _needed(ctx, step)
        if carried is not None:
            result.append(carried)
            continue
        if (current == wanted and not step.force_apply) or (current is not None and not _may_replace(ctx, step, current)):
            continue
        undo = step.undo(ctx, current)
        effect = make_effect(step, current, wanted, None, undo)
        if ctx.get("dry_run"):
            result.append(effect)
            continue
        try:
            effect["backup"] = step.backup(ctx, current)
            earlier = next((item for item in recorded(ctx, step.component) if item.get("effect_id") == step.effect_id), None)
            if earlier is not None and sha256(current) == earlier.get("after_sha256"):
                effect["before_sha256"] = earlier.get("before_sha256")
                effect["backup"] = earlier.get("backup")
            # Ownership is durable before mutation. A process stop during write/readback
            # can then recover through the same hash-guarded remover.
            sink = ctx.get("effect_sink")
            if sink:
                sink(effect)
            step.write_effect(ctx, wanted, effect)
            if step.read(ctx) != wanted:
                raise ComponentError(f"{step.target} did not read back as written")
        except (OSError, ComponentError, ValueError) as exc:
            # Include the attempted operation: it may have written before failing.
            result.append(effect)
            raise ComponentApplyError(f"{step.component} stopped at {step.name}: {type(exc).__name__}: {exc}",
                                      list(result)) from exc
        if ctx.get("effect_sink"):
            ctx["effect_sink"](effect)
        result.append(effect)
    return result


# ------------------------------------------------------------------- permission entries
def _restricted(data: dict, component, scopes: tuple, only=None) -> tuple[dict, set]:
    """A copy of the list where only the shipped entries of ``component`` (an id or a set of ids) and ``scopes`` stay on."""
    owners = {component} if isinstance(component, str) else set(component)
    copy_ = copy.deepcopy(data)
    keep = set()
    for entry in copy_["entries"]:
        wanted = (entry["component"] in owners and entry["verdict"] == "ship" and entry["scope"] in scopes
                  and (only is None or entry["id"] in only))
        if wanted:
            keep.add(entry["id"])
        else:
            entry["default"] = "off"
    return copy_, keep


def permission_effects(ctx: dict, component: str, *, write: bool, only=None, machine: bool = True,
                       per_project: bool = True, done: list | None = None, entries_from=None) -> list[dict]:
    """Effects for ``component``'s permission entries: user-wide and per-launch entries once, project entries for each
    project in the context. ``write`` False is a dry run (the Effects, nothing written). Entries that are already
    there, tracked by git or the person's own value are skipped by ``permissions.apply`` itself. ``entries_from`` names
    the components whose entries are used (default: ``component`` itself); Effects are tagged ``component``."""
    data = permissions.load()
    asked = choices(ctx)
    result = done if done is not None else []
    passes = []
    if machine:
        passes.append((("user", "per-launch"), ctx_home(ctx)))
    if per_project:
        passes += [(("project", "project-local"), root) for root in project_roots(ctx)]
    for scopes, root in passes:
        restricted, keep = _restricted(data, entries_from or component, scopes, only)
        if not keep:
            continue
        picked = {key: value for key, value in asked.items() if key in keep
                  and next(e for e in restricted["entries"] if e["id"] == key)["default"] == "on"}
        def observer(kind, item):
            if kind == "effect" and ctx.get("effect_sink"):
                ctx["effect_sink"](dict(item, component=component))

        try:
            with home_env(ctx):
                found = permissions.apply(root, user_home(ctx), choices=picked, os_name=os_name(ctx),
                                          gen_id=gen_id(ctx), data=restricted, observer=observer if write else None,
                                          dry_run=not write or bool(ctx.get("dry_run")))
        except permissions.PermissionsApplyError as exc:
            for item in exc.effects:
                item["component"] = component
            raise ComponentApplyError(str(exc), result + list(exc.effects)) from exc
        except permissions.PermissionsError as exc:
            raise ComponentApplyError(str(exc), list(result)) from exc
        for item in found:
            item["component"] = component
        result.extend(found)
    have = {item["effect_id"] for item in result}
    result.extend(item for item in recorded(ctx, component) if item["effect_id"] not in have
                  and (only is None or item.get("permission_entry") in only or item["kind"] == "text-line")
                  and (item.get("permission_entry") or item["kind"] == "text-line") and _present(item))
    return result


def _present(effect: dict) -> bool:
    """Is a recorded file effect still in its file (its entry, text or line)? Used to carry it through a re-apply."""
    try:
        raw = permissions._read(Path(effect["target"]))
    except OSError:
        return False
    if raw is None:
        return False
    text = raw.decode("utf-8", errors="replace")
    undo = effect.get("undo") or {}
    op = undo.get("op")
    if op == "json-remove":
        try:
            node = json.loads(text)
            for part in undo["pointer"].strip("/").split("/"):
                node = node[part]
            return all(value in node for value in undo["values"])
        except (ValueError, KeyError, TypeError):
            return False
    if op in ("text-remove", "file-delete"):
        return bool(undo.get("text")) and undo["text"] in text
    if op == "line-remove":
        return undo.get("line") in text.splitlines()
    return False


def permission_missing(ctx: dict, component: str, only=None) -> list[str]:
    """Entry ids of ``component`` that a fresh apply would still add (a dry run that ignores the record)."""
    fresh = dict(ctx, record=None, dry_run=True)
    return sorted({item["permission_entry"] for item in permission_effects(fresh, component, write=False, only=only)
                   if item.get("permission_entry")})


# --------------------------------------------------------------------------- remove
def removal_incomplete(reason) -> bool:
    """A deliberate preservation is settled; a failed/unknown undo still needs a retry."""
    if not reason or reason == "already gone":
        return False
    return not any(word in str(reason).lower() for word in
                   ("changed by you", "adopted:", "not the eutonos plugin", "another vendor"))


def remove_effects(ctx: dict, effects: list) -> dict:
    """Undo Effects in reverse order. File effects use the exact-bytes rules of ``permissions.remove`` (removed only
    while the target still holds the recorded bytes or entry); other kinds use their registered undo handler.
    Returns ``{removed[effect_id], kept[{effect_id, reason}]}``."""
    removed, kept, failed = [], [], []
    for effect in reversed(list(effects)):
        op = (effect.get("undo") or {}).get("op")
        try:
            if op in UNDO_HANDLERS:
                reason = UNDO_HANDLERS[op](ctx, effect)
            else:
                with home_env(ctx):
                    outcome = permissions.remove([effect])
                reason = outcome["kept"][0]["reason"] if outcome["kept"] else None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = f"could not be undone ({type(exc).__name__}); kept"
        if reason is None or reason == "already gone":
            removed.append(effect["effect_id"])
        else:
            row = {"effect_id": effect["effect_id"], "reason": reason}
            kept.append(row)
            if removal_incomplete(reason):
                failed.append(row)
    return {"removed": removed, "kept": kept, "failed": failed}


# -------------------------------------------------------------------------- verify
def verify_result(component: str, checks: list, *, state: str | None = None) -> dict:
    """``{id, state, checks}``; ``state`` is derived from the checks when not given. A check is
    ``{name, ok, detail}`` plus ``missing`` or ``changed`` for the way it failed."""
    if state is None:
        failed = [item for item in checks if not item.get("ok")]
        if not failed:
            state = "ok"
        elif any(item.get("changed") for item in failed):
            state = "changed"
        else:
            state = "missing"
    return {"id": component, "state": state, "checks": checks}


def step_checks(ctx: dict, component: str, steps: list) -> list[dict]:
    """One check per step against the record: present and unchanged, absent, or changed since it was written."""
    saved = {item.get("effect_id"): item for item in recorded(ctx, component)}
    checks = []
    for step in steps:
        current, wanted = step.read(ctx), step.want(ctx)
        item = saved.get(step.effect_id)
        if current is None:
            checks.append({"name": step.name, "ok": False, "detail": "absent"})
        elif current == wanted:
            checks.append({"name": step.name, "ok": True, "detail": "present"})
        else:
            changed = item is not None and sha256(current) != item.get("after_sha256")
            checks.append({"name": step.name, "ok": False, "changed": True,
                           "detail": "changed by you" if changed else "differs from what EUTONOS would write"})
    return checks


# --------------------------------------------------------------- manifests and loading
def components_dir() -> Path:
    return Path(__file__).resolve().parent / "components"


def manifest_ids() -> list[str]:
    return sorted(path.stem for path in components_dir().glob("*.json"))


TOOLS = ("git",)  # command-line tools a part may need on PATH; a missing one makes that part unavailable, not the install


def parts_needing(manifests, tool: str) -> list[dict]:
    """``[{id, name}]`` for each part whose manifest declares ``requires.tools`` with `tool` (ISU stage 6)."""
    return [{"id": item["id"], "name": item.get("name") or item["id"]} for item in manifests
            if tool in ((item.get("requires") or {}).get("tools") or [])]


def load_manifest(component: str) -> dict:
    return json.loads((components_dir() / (component + ".json")).read_text(encoding="utf-8"))


def validate_manifest(value) -> list[str]:
    """Problems with a manifest against contract (c); an empty list means valid."""
    if not isinstance(value, dict):
        return ["a manifest must be an object"]
    problems = []
    fields = ("profile", "id", "name", "version", "kind", "requires", "default", "gate", "kit", "entry",
              "permissions", "consent_lines", "licence")
    if tuple(value) != fields:
        return ["fields must be exactly " + ", ".join(fields)]
    if value["profile"] != PROFILE:
        problems.append("profile must be " + PROFILE)
    for field in ("id", "name", "version"):
        if not isinstance(value[field], str) or not value[field].strip():
            problems.append(f"{field} must be a non-empty string")
    if value["kind"] not in KINDS:
        problems.append("kind must be one of " + ", ".join(KINDS))
    if value["default"] not in DEFAULTS:
        problems.append("default must be one of " + ", ".join(DEFAULTS))
    requires = value["requires"]
    if not isinstance(requires, dict) or tuple(requires) != ("eutonos", "components", "os", "apps", "tools"):
        problems.append("requires must have eutonos, components, os, apps and tools")
    else:
        if not isinstance(requires["eutonos"], str) or not requires["eutonos"].startswith(">="):
            problems.append('requires.eutonos must look like ">=0.5"')
        if not isinstance(requires["components"], list) or not all(isinstance(i, str) for i in requires["components"]):
            problems.append("requires.components must be a list of ids")
        if not isinstance(requires["os"], list) or not requires["os"] or not set(requires["os"]) <= {"windows", "macos"}:
            problems.append("requires.os must list windows and/or macos")
        if not isinstance(requires["apps"], list) or not set(requires["apps"]) <= {"claude", "codex", "hermes"}:
            problems.append("requires.apps must list claude, codex and/or hermes")
        if not isinstance(requires["tools"], list) or not set(requires["tools"]) <= set(TOOLS):
            problems.append("requires.tools must list " + " and/or ".join(TOOLS))
    gate = value["gate"]
    if value["default"] == "held" and not gate:
        problems.append("a held component needs a gate")
    if gate is not None and (not isinstance(gate, dict) or set(gate) != {"slice", "reason"}
                             or not all(isinstance(gate[k], str) and gate[k] for k in gate)):
        problems.append("gate must be null or {slice, reason}")
    if value["kit"] is not None and not (isinstance(value["kit"], str) and value["kit"].startswith("kits/")):
        problems.append('kit must be null or "kits/<name>"')
    entry = value["entry"]
    if not isinstance(entry, dict) or tuple(entry) != ("plan", "apply", "verify", "remove"):
        problems.append("entry must name plan, apply, verify and remove")
    else:
        for name, target in entry.items():
            if not isinstance(target, str) or target.count(":") != 1 or not all(target.split(":")):
                problems.append(f"entry.{name} must be module:function")
    for field in ("permissions", "consent_lines"):
        if not isinstance(value[field], list) or not all(isinstance(i, str) for i in value[field]):
            problems.append(f"{field} must be a list of ids")
    licence = value["licence"]
    if not isinstance(licence, dict) or tuple(licence) != ("required", "check") or not isinstance(licence["required"], bool):
        problems.append("licence must be {required, check}")
    elif licence["required"] and not (isinstance(licence["check"], str) and licence["check"].count(":") == 1):
        problems.append("a licence-required component needs licence.check as module:function")
    elif not licence["required"] and licence["check"] is not None:
        problems.append("licence.check must be null when no licence is required")
    return problems


def resolve(target: str):
    """``module:function`` to the function."""
    module, _, function = target.partition(":")
    return getattr(importlib.import_module(module), function)


def licence_state(manifest: dict, ctx: dict) -> dict:
    """``{state valid|missing|expired|unknown, detail}``; a component with no licence requirement is always valid."""
    licence = manifest["licence"]
    if not licence["required"]:
        return {"state": "valid", "detail": "no licence needed"}
    try:
        value = resolve(licence["check"])(ctx)
    except Exception as exc:  # a broken check is unknown, never valid
        return {"state": "unknown", "detail": f"the licence check failed ({type(exc).__name__})"}
    if not isinstance(value, dict) or value.get("state") not in ("valid", "missing", "expired", "unknown"):
        return {"state": "unknown", "detail": "the licence check returned no state"}
    return value


def decide(manifest: dict, ctx: dict, *, status=None) -> dict:
    """The record row ``{id, version, state, reason}`` a loader writes for a component, from its manifest:

    * the OS is not supported -> ``skipped``; the manifest default is ``off`` and the person did not switch it on
      -> ``skipped``; the manifest is ``held`` -> ``held`` with the gate's reason;
    * a licence is required and not valid -> ``unlicensed``;
    * else the component's own ``status(ctx)`` (``{state, reason}``) decides ``installed``, ``held`` or ``skipped``
      (Hermes absent, companion on a Mac), and with none the component is ``installed``.
    A loader that wants the same answer calls this; it writes nothing."""
    row = {"id": manifest["id"], "version": manifest["version"]}
    if os_name(ctx) not in manifest["requires"]["os"]:
        return dict(row, state="skipped", reason=f"not supported on {os_name(ctx)}")
    wanted = (choices(ctx).get(manifest["id"]) or (ctx.get("component_choices") or {}).get(manifest["id"]))
    if manifest["default"] == "off" and wanted != "on":
        return dict(row, state="skipped", reason="off by default")
    if manifest["default"] == "held":
        return dict(row, state="held", reason=(manifest["gate"] or {}).get("reason", "held"))
    if manifest["licence"]["required"]:
        licence = licence_state(manifest, ctx)
        if licence["state"] != "valid":
            return dict(row, state="unlicensed", reason=f"licence {licence['state']}: {licence['detail']}")
    if status is not None:
        own = status(ctx)
        if own and own.get("state") in STATES:
            return dict(row, state=own["state"], reason=own.get("reason") or "")
    return dict(row, state="installed", reason="")


# ----------------------------------------------------------------- real-machine seams
def run_app(argv: list, timeout: int = 120) -> tuple[int, str]:
    """The one place an app command runs for real. Tests replace it through ``seams["claude"]`` and the like."""
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=HIDDEN)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def shortcut_script(action: str) -> str:
    if action == "read":
        return ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:EU_LNK);"
                "[Console]::Out.Write(($s.TargetPath+'|'+$s.Arguments+'|'+$s.WorkingDirectory))")
    return ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:EU_LNK);"
            "$s.TargetPath=$env:EU_TARGET;$s.Arguments=$env:EU_ARGS;$s.WorkingDirectory=$env:EU_CWD;"
            "$s.WindowStyle=7;$s.Description=$env:EU_DESC;$s.Save()")


def real_shortcut():
    """Startup-folder shortcuts through PowerShell COM (Windows only). ``read(path)`` returns
    ``{target, arguments, cwd}`` or None; ``create(spec)`` writes the .lnk; ``delete(path)`` removes that one file."""
    def powershell(script, env):
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                              env=dict(os.environ, **env), capture_output=True, text=True, timeout=60,
                              creationflags=HIDDEN)
        if done.returncode != 0:
            raise OSError("shortcut command failed: " + (done.stderr or "")[-160:])
        return done.stdout

    def read(path):
        if not Path(path).exists():
            return None
        target, _, rest = powershell(shortcut_script("read"), {"EU_LNK": str(path)}).partition("|")
        arguments, _, cwd = rest.partition("|")
        return {"target": target, "arguments": arguments, "cwd": cwd}

    def create(spec):
        Path(spec["path"]).parent.mkdir(parents=True, exist_ok=True)
        powershell(shortcut_script("create"), {"EU_LNK": spec["path"], "EU_TARGET": spec["target"],
                                               "EU_ARGS": spec["arguments"], "EU_CWD": spec["cwd"],
                                               "EU_DESC": spec["description"]})

    def delete(path):
        Path(path).unlink(missing_ok=True)

    return {"read": read, "create": create, "delete": delete}


def shortcut_seam(ctx: dict) -> dict:
    return seam(ctx, "shortcut") or real_shortcut()


def startup_folder(ctx: dict) -> Path:
    """The Windows Startup folder (``seams["startup_folder"]`` overrides). For the real user it is the one the installer
    ports use (under %APPDATA%); a context with its own ``user_home`` (a test or a staging user) keeps it under that home."""
    override = seam(ctx, "startup_folder")
    if override:
        return Path(override)
    home = user_home(ctx)
    if not ctx.get("user_home") and home == Path.home():
        return installer_ports.startup_folder()
    return home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


class ShortcutStep(StateStep):
    """A Startup shortcut. One that exists and is not recognisably EUTONOS's (``ours(read_spec)`` false) is the
    person's own and is left alone."""

    ours = staticmethod(lambda spec: True)

    def may_replace(self, ctx, current) -> bool:
        earlier = next((item for item in recorded(ctx, self.component) if item.get("effect_id") == self.effect_id), None)
        if earlier:
            undo = earlier.get("undo") or {}
            raw = sha256(permissions._read(Path(self.target)))
            if sha256(current) != earlier.get("after_sha256") or undo.get("raw_pending"):
                return False
            if "raw_after" in undo and raw != undo["raw_after"]:
                return False
        return current is None or bool(self.ours(json.loads(current)))

    def undo(self, ctx, before) -> dict:
        earlier = next((item for item in recorded(ctx, self.component) if item.get("effect_id") == self.effect_id), None)
        if earlier:
            return dict(earlier["undo"], raw_pending=True)
        result = dict(super().undo(ctx, before), raw_before=sha256(permissions._read(Path(self.target))), raw_pending=True)
        if before is not None:
            raw = permissions._read(Path(self.target))
            result.update(before=json.loads(before), backup_sha256=sha256(raw))
        return result

    def write_effect(self, ctx, data, effect) -> None:
        effect["undo"]["raw_pending"] = True
        try:
            self.write(ctx, data)
        finally:
            effect["undo"]["raw_after"] = sha256(permissions._read(Path(self.target)))
            effect["undo"]["raw_pending"] = False
            if ctx.get("effect_sink"):
                ctx["effect_sink"](effect)

    def backup(self, ctx, before):
        earlier = next((item for item in recorded(ctx, self.component) if item.get("effect_id") == self.effect_id), None)
        if earlier:
            return earlier.get("backup")
        raw = permissions._read(Path(self.target))
        if before is not None and raw is None:
            raise OSError("the earlier shortcut bytes could not be backed up")
        with home_env(ctx):
            return permissions._backup(raw, Path(self.target), gen_id(ctx), self.effect_id)


def shortcut_step(ctx: dict, component: str, name: str, lnk_name: str, spec: dict, *, entry=None,
                  ours=None) -> StateStep:
    """A Startup-folder shortcut as a ``shortcut`` Effect. ``spec`` is ``{target, arguments, cwd, description}``; the
    identity of the state is target, arguments and working directory (what ``read`` returns)."""
    path = startup_folder(ctx) / lnk_name
    wanted = {"target": spec["target"], "arguments": spec["arguments"], "cwd": spec["cwd"]}

    def getter(context):
        return shortcut_seam(context)["read"](str(path))

    def setter(context, value):
        shortcut_seam(context)["create"](dict(spec, path=str(path)))

    step = ShortcutStep(component, name, "shortcut", str(path), None, wanted, getter, setter, "shortcut-delete",
                        {"path": str(path)}, entry=entry)
    if ours is not None:
        step.ours = staticmethod(ours)
    return step


@undo_handler("shortcut-delete")
def _undo_shortcut(ctx: dict, effect: dict) -> str | None:
    path = effect["undo"]["path"]
    current = shortcut_seam(ctx)["read"](path)
    if current is None:
        return "already gone"
    undo = effect["undo"]
    raw_now = sha256(permissions._read(Path(path)))
    if undo.get("before") == current and raw_now == undo.get("backup_sha256"):
        return None  # a previous undo restored bytes before a later step failed
    if undo.get("raw_pending") and raw_now != undo.get("raw_before"):
        return "shortcut write was interrupted before byte readback; inspect before retrying"
    if "raw_after" in undo and raw_now != undo["raw_after"]:
        return "changed by you, kept"
    if sha256(canonical(current)) != effect["after_sha256"]:
        return "changed by you, kept"
    if undo.get("before") is not None:
        backup = Path(effect["backup"]) if effect.get("backup") else None
        raw = permissions._read(backup) if backup else None
        if raw is None or sha256(raw) != undo.get("backup_sha256"):
            return "the earlier shortcut's backup is missing or changed; kept"
        restore = shortcut_seam(ctx).get("restore")
        if restore:
            restore(path, raw, undo["before"])
        else:
            permissions._write_atomic(Path(path), raw)
    else:
        shortcut_seam(ctx)["delete"](path)
    return None
