"""The ``role-hooks`` component (IPC5 lane B, contract c): reminder hooks for Claude and Codex chats. HELD.

Held on gate slice ``slice-fe8c09fb-1163-4e9e-bac4-876914ddd824`` (the role-hooks field proof): ``status`` says held,
and ``plan`` and ``apply`` write nothing unless the gate is opened by ``seams["gate_open"]`` (a test, or the loader
once the gate slice has closed). When open it renders, per machine and per project:

* permissions entry 18: ``{root}/.claude/settings.local.json`` with the hooks, each running
  ``H/bin/eutonos-hook --app claude`` (no shell: ``command`` plus ``args``);
* permissions entry 19: ``{root}/.codex/hooks.json`` with ``command`` (macOS and Linux) and ``commandWindows``.

Each is one ``json-entry`` Effect on the key ``/hooks``: the file is created when absent, and an existing file only
gets that one key added (entry 5 of Crosstalk already puts rules in the same ``settings.local.json``). Whatever is in
the file besides ``hooks`` is never touched. A file that already has a ``hooks`` key that is not recognisably
EUTONOS's (it does not run ``eutonos-hook``), and a file the project tracks in git, is the person's or the team's and
yields no Effect and a plain note in ``verify``. A file this component creates also gets a ``.git/info/exclude`` line
when the project is a git repository. Removal takes the one key out while it still holds what was written, and
deletes the file only when it created it and nothing else is in it. Entry 17 (Codex hook trust, ``codex-hook-trust``)
is a person step: Codex asks to trust each hook by hash, and nothing here writes it.

The program the hooks run is ``eutonos.role_hooks`` (entry point ``eutonos-hook``); its launcher under ``H/bin`` is
placed by the installer (lane A), exactly as for the other launchers.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import component_support as cs
from . import permissions

ID = "role-hooks"
GATE = "slice-fe8c09fb-1163-4e9e-bac4-876914ddd824"
LAUNCHER = "eutonos-hook"
EVENTS = ("PreToolUse", "SessionStart", "SubagentStart", "SubagentStop", "Stop", "UserPromptSubmit")
CONTEXT_EVENTS = ("SessionStart", "SubagentStart")  # the events whose reminder text is capped (as in the repo's own file)
HELD_REASON = f"held until the roles cleanup is done (gate {GATE})"
TRUST_NOTE = ("Codex will ask you to trust each hook once; that is your step (permissions entry 17), "
              "and the hook files here are not changed by it.")


def gate_open(ctx: dict) -> bool:
    return bool(cs.seam(ctx, "gate_open"))


def status(ctx: dict) -> dict:
    if gate_open(ctx):
        return {"state": "installed", "reason": ""}
    return {"state": "held", "reason": HELD_REASON}


def hook_exe(ctx: dict) -> str:
    return str(cs.launcher_path(ctx, LAUNCHER))


def claude_hooks(ctx: dict) -> dict:
    """Entry 18's ``hooks`` value, rendered for this machine. Exec form (``command`` plus ``args``): no shell."""
    hook = {"type": "command", "command": hook_exe(ctx), "args": ["--app", "claude"], "timeout": 5}
    return {event: [{"hooks": [dict(hook)]}] for event in sorted(EVENTS)}


def render_claude(ctx: dict) -> bytes:
    """Entry 18's file as it is when created, rendered for this machine."""
    return (json.dumps({"hooks": claude_hooks(ctx)}, indent=2) + "\n").encode("utf-8")


CODEX_DESCRIPTION = "EUTONOS role reminder hooks (short, role-specific text only; no shell execution or model calls)."


def codex_hooks(ctx: dict) -> dict:
    """Entry 19's ``hooks`` value, rendered for this machine: ``command`` for macOS and Linux, ``commandWindows`` for
    Windows."""
    exe = hook_exe(ctx)
    hooks = {}
    for event in sorted(EVENTS):
        item = {"type": "command", "command": f'"{exe}" --app codex', "commandWindows": f'"{exe}" --app codex',
                "timeout": 5}
        if event in CONTEXT_EVENTS:
            item["additionalContextLimit"] = 1000
        hooks[event] = [{"hooks": [item]}]
    return hooks


def render_codex(ctx: dict) -> bytes:
    """Entry 19's file as it is when created, rendered for this machine."""
    return (json.dumps({"description": CODEX_DESCRIPTION, "hooks": codex_hooks(ctx)}, indent=2) + "\n").encode("utf-8")


def _ours(value) -> bool:
    """Is this ``hooks`` value EUTONOS's? Every command in it runs ``eutonos-hook`` (or the module it starts)."""
    commands = []

    def walk(item):
        if isinstance(item, dict):
            commands.extend(str(item[key]) for key in ("command", "commandWindows") if key in item)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return bool(commands) and all(LAUNCHER in text or "eutonos.role_hooks" in text for text in commands)


def _document(path: Path):
    """``(parsed object, original text)``; ``({}, None)`` when absent; raises ``ValueError`` when unusable."""
    raw = permissions._read(path)
    if raw is None:
        return {}, None
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value, raw.decode("utf-8")


class HooksKeyStep(cs.StateStep):
    """The one ``hooks`` key of a JSON settings file (kind ``json-entry``, selector ``/hooks``). The file is created
    when absent (with ``extra`` fields); an existing file only gains the key; a ``hooks`` value that is not EUTONOS's
    is left alone. The backup is the whole file as it was."""

    def __init__(self, ctx: dict, name: str, entry: str, target: Path, hooks: dict, extra: dict):
        self.path, self.extra = target, extra

        def getter(context):
            try:
                value = _document(target)[0].get("hooks")
            except (OSError, ValueError):
                return {"unreadable": True}
            return None if value is None else value

        def setter(context, value):
            document, original = _document(target)
            if original is None:
                document = dict(extra)
            document["hooks"] = value
            target.parent.mkdir(parents=True, exist_ok=True)
            permissions._write_atomic(target, permissions._dump_json(document, original))

        super().__init__(ID, name, "json-entry", str(target), "/hooks", hooks, getter, setter, "hooks-key-remove", {},
                         entry=entry)

    def may_replace(self, ctx, current) -> bool:
        value = json.loads(current)
        return _ours(value) and not (isinstance(value, dict) and value.get("unreadable"))

    def undo(self, ctx, before) -> dict:
        return {"op": "hooks-key-remove", "path": str(self.path), "created_file": not self.path.exists(),
                "extra": self.extra, "created_dirs": [str(item) for item in permissions._missing_dirs(self.path)]}

    def backup(self, ctx, before):
        with cs.home_env(ctx):
            return permissions._backup(permissions._read(self.path), self.path, cs.gen_id(ctx), self.effect_id)


@cs.undo_handler("hooks-key-remove")
def _undo_hooks(ctx: dict, effect: dict) -> str | None:
    path = Path(effect["target"])
    try:
        document, original = _document(path)
    except (OSError, ValueError):
        return "unreadable; kept"
    if original is None:
        return "already gone"
    if "hooks" not in document:
        return "already gone"
    if cs.sha256(cs.canonical(document["hooks"])) != effect["after_sha256"]:
        return "changed by you, kept"
    del document["hooks"]
    undo = effect["undo"]
    if undo.get("created_file") and document == undo.get("extra"):
        path.unlink()
        permissions._drop_dirs(effect)
    elif document:
        permissions._write_atomic(path, permissions._dump_json(document, original))
    else:
        permissions._write_atomic(path, permissions._dump_json({}, original))
    return None


def _steps(ctx: dict) -> list:
    steps: list = []
    if not gate_open(ctx):
        return steps
    held = {item["effect_id"]: item for item in cs.recorded(ctx, ID)}
    for root in cs.project_roots(ctx):
        for entry, relative, hooks, extra in (
                ("claude-hooks", Path(".claude") / "settings.local.json", claude_hooks(ctx), {}),
                ("codex-hooks", Path(".codex") / "hooks.json", codex_hooks(ctx), {"description": CODEX_DESCRIPTION})):
            target = root / relative
            if permissions.tracked(root, target) is not False:
                continue  # the project's own file (or git cannot say): never edited
            step = HooksKeyStep(ctx, f"{entry}-{root.name}", entry, target, hooks, extra)
            steps.append(step)
            made = not target.exists() or (held.get(step.effect_id) or {}).get("undo", {}).get("created_file")
            exclude = permissions.exclude_file(root)
            if exclude is not None and made:
                steps.append(cs.TextLineStep(ID, f"exclude-{entry}-{root.name}", exclude, "/" + relative.as_posix(),
                                             entry=entry))
    return steps


def plan(ctx: dict) -> list[dict]:
    return cs.plan_steps(ctx, _steps(ctx))


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    return cs.apply_steps(ctx, _steps(ctx), effects)


def verify(ctx: dict) -> dict:
    if not gate_open(ctx):
        return cs.verify_result(ID, [{"name": "gate", "ok": False, "detail": HELD_REASON}], state="held")
    steps = _steps(ctx)
    held = {item["effect_id"] for item in cs.recorded(ctx, ID)}
    checks = []
    for step in steps:
        if isinstance(step, HooksKeyStep) and step.effect_id not in held and step.read(ctx) not in (None, step.want(ctx)):
            checks.append({"name": step.name, "ok": True, "detail": "an existing hooks entry that is not EUTONOS's "
                           "is kept as it is"})
        else:
            checks += cs.step_checks(ctx, ID, [step])
    checks.append({"name": "trust", "ok": True, "detail": TRUST_NOTE})
    return cs.verify_result(ID, checks)


def remove(ctx: dict, effects: list) -> dict:
    return cs.remove_effects(ctx, effects)
