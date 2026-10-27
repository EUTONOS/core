"""The ``companion`` component (IPC5 lane B, contract c): the EUTONOS companion's Windows scheduled task.

On Windows it registers the one owned interactive logon task (``\\EUTONOS Desktop Companion``) through
``desktop_companion_runtime.register_task``, running the installed runtime's own interpreter, and removes it through
``delete_task``. The Effect is a ``scheduled-task`` whose identity is the task's command and arguments (what the task
XML holds), so uninstall removes the task only while it still runs what was registered. A task of the same name that is
not EUTONOS's is never touched. When an older EUTONOS companion task of that name is replaced, its full definition is
kept in the Effect's backup (``task-restore``) and uninstall puts it back instead of deleting the name. The
replaced task's own ``--root <project>`` is kept in the new command (the companion finds its project from it; without
one it falls back to the environment or working folder). With no earlier task there is no reliable project to name
(install registers none), so the command carries no ``--root``. On macOS the component is held until IPC8: ``status`` says so, and ``plan`` and
``apply`` write nothing.

The companion's Stop action does not live here: it is ``crosstalk_status.owner_control("halt")``, which runs the active
kit's CLI and falls back to ``degraded_halt`` when the kit is missing or fails ``verify``; ``stop`` below is that call
with its read-back, for the E icon and the dashboard.

Seams: ``task`` (``{"query": () -> (command, arguments) | None, "register": (argv) -> None, "delete": () -> bool,
"export": () -> str | None, "restore": (xml) -> None, "owned": () -> bool}``; the last three are optional: ``export`` and
``restore`` keep and put back a replaced task, and ``owned`` says whether the registered task also carries EUTONOS's own
description, which the command line alone does not show).

Ownership at removal is the command/arguments hash plus a fingerprint of the whole exported task definition
(``task_fingerprint``: triggers, conditions, settings and principal; the registration date, URI, author and security
descriptor are left out because Task Scheduler writes them). The fingerprint is read back after the task is registered
and kept in the Effect's undo. ``task-delete`` and ``task-restore`` act only while the task still matches it; a task the
person edited, or an older record that has no fingerprint, is refused and left alone.
"""
from __future__ import annotations

import codecs
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET

from . import component_support as cs

ID = "companion"
HELD_REASON = "held on macOS until IPC8 (the Mac installer)"


def _real_task() -> dict:
    from . import desktop_companion_runtime as runtime

    def query():
        return runtime.task_spec(runtime._task_query())

    def register(argv):
        runtime.register_task(argv)

    def owned():
        return runtime._task_owned(runtime._task_query())

    return {"query": query, "register": register, "delete": runtime.delete_task, "export": runtime.export_task,
            "restore": runtime.restore_task, "owned": owned}


def _task(ctx: dict) -> dict:
    return cs.seam(ctx, "task") or _real_task()


def _root_argument(arguments: str) -> str | None:
    """The ``--root`` value in a task's argument string (``--root X``, ``--root "X Y"`` or ``--root=X``), or None."""
    words = [word[1:-1] if len(word) > 1 and word[0] == word[-1] == '"' else word
             for word in shlex.split(arguments or "", posix=False)]
    for index, word in enumerate(words):
        if word == "--root" and index + 1 < len(words):
            return words[index + 1] or None
        if word.startswith("--root="):
            return word[len("--root="):] or None
    return None


def launch_command(ctx: dict) -> list[str]:
    """The task's argv: the runtime's windowless interpreter, isolated, running the finite ensure action. When the
    companion task being replaced (or already ours) names a project with ``--root``, the new task names it too."""
    argv = [cs.runtime_pythonw(ctx), "-I", "-m", "eutonos.desktop_companion_runtime"]
    try:
        found = _task(ctx)["query"]()
    except OSError:
        found = None
    if found is not None and "eutonos.desktop_companion_runtime" in found[1]:
        root = _root_argument(found[1])
        if root:
            argv += ["--root", root]
    return argv + ["--ensure-current"]


_VOLATILE = {"Date", "URI", "Author", "SecurityDescriptor"}  # RegistrationInfo children Task Scheduler writes itself


def task_fingerprint(xml) -> str:
    """A stable hash of a task's exported definition: every element, attribute and text value, in order, with
    whitespace between elements ignored and the volatile ``RegistrationInfo`` fields (registration date, URI, author,
    security descriptor) left out. Raises ``ValueError`` when the text is not a task definition."""
    if isinstance(xml, bytes):
        try:
            xml = xml.decode("utf-16") if xml[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE) else xml.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("the task definition is not readable text") from exc
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", str(xml or "")).strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("the task definition is not valid XML") from exc

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def walk(node):
        children = [walk(child) for child in node
                    if not (local(node.tag) == "RegistrationInfo" and local(child.tag) in _VOLATILE)]
        return [local(node.tag), sorted((local(k), v) for k, v in node.attrib.items()),
                (node.text or "").strip(), children]

    if local(root.tag) != "Task":
        raise ValueError("the XML is not a scheduled task definition")
    return hashlib.sha256(json.dumps(walk(root), separators=(",", ":")).encode("utf-8")).hexdigest()


def _definition_refusal(ctx: dict, effect: dict) -> str | None:
    """Why the task must not be deleted or restored: its full definition no longer matches what EUTONOS recorded, or
    there is nothing to compare with. None when it still matches."""
    recorded = (effect.get("undo") or {}).get("task_fingerprint")
    if not recorded:
        return "no full task fingerprint was recorded (an older install), so the task was left alone"
    export = _task(ctx).get("export")
    if export is None:
        return "the task's full definition cannot be read, so the task was left alone"
    try:
        now = task_fingerprint(export())
    except (OSError, ValueError):
        return "the task's full definition could not be read, so the task was left alone"
    if now != recorded:
        return "changed by you (triggers, conditions, settings or principal), kept"
    return None


def _spec(argv: list) -> dict:
    return {"command": argv[0], "arguments": subprocess.list2cmdline(argv[1:])}


class TaskStep(cs.StateStep):
    """A task already registered under the companion's name is replaced only when it is EUTONOS's own: it runs the
    companion's ensure action (``eutonos.desktop_companion_runtime ... --ensure-current``) and carries EUTONOS's task
    description (the ``owned`` seam, the same test ``register_task`` and ``delete_task`` apply, which refuse any other
    task of that name). Any other task of that name is the person's and is left alone; a description that cannot be
    read counts as not ours, so nothing is replaced on a guess."""

    def may_replace(self, ctx, current) -> bool:
        spec = json.loads(current)
        if not ("eutonos.desktop_companion_runtime" in spec["arguments"] and "--ensure-current" in spec["arguments"]):
            return False
        owned = _task(ctx).get("owned")
        if owned is None:
            return True
        try:
            return bool(owned())
        except OSError:
            return False

    def write_effect(self, ctx, data, effect) -> None:
        """Register the task, then record the fingerprint of what Task Scheduler now holds (not of what was sent: it
        adds its own defaults). Without one the removal refuses, so a task never goes on a guess."""
        super().write_effect(ctx, data, effect)
        export = _task(ctx).get("export")
        effect["undo"].pop("task_fingerprint", None)
        if export is not None:
            try:
                effect["undo"]["task_fingerprint"] = task_fingerprint(export())
            except (OSError, ValueError):
                pass

    def _earlier(self, ctx):
        """The Effect an earlier generation recorded for this task, when what is there now is EUTONOS's own write."""
        return next((item for item in cs.recorded(ctx, ID) if item.get("effect_id") == self.effect_id), None)

    def undo(self, ctx, before) -> dict:
        earlier = self._earlier(ctx)
        if earlier is not None and earlier.get("undo"):  # an update: what the person had first is still the one to put back
            return dict(earlier["undo"])
        if before is None:
            return super().undo(ctx, before)
        return {"op": "task-restore", "task": self.undo_args["task"]}

    def backup(self, ctx, before):
        """The replaced task's definition (its full XML when the seam can export it), kept for ``task-restore``."""
        earlier = self._earlier(ctx)
        if earlier is not None and earlier.get("undo"):
            return earlier.get("backup")
        if before is None:
            return None
        export = _task(ctx).get("export")
        saved = {"spec": json.loads(before), "xml": export() if export else None}
        with cs.home_env(ctx):
            return cs.permissions._backup(json.dumps(saved, sort_keys=True).encode("utf-8"), Path(str(self.target)),
                                          cs.gen_id(ctx), self.effect_id)


def task_step(ctx: dict):
    argv = launch_command(ctx)

    def getter(context):
        found = _task(context)["query"]()
        return None if found is None else {"command": found[0], "arguments": found[1]}

    def setter(context, _value):
        _task(context)["register"](argv)

    return TaskStep(ID, "task", "scheduled-task", "app:schtasks", r"\EUTONOS Desktop Companion", _spec(argv),
                        getter, setter, "task-delete", {"task": r"\EUTONOS Desktop Companion"})


@cs.undo_handler("task-delete")
def _undo_task(ctx: dict, effect: dict) -> str | None:
    found = _task(ctx)["query"]()
    if found is None:
        return "already gone"
    if cs.sha256(cs.canonical({"command": found[0], "arguments": found[1]})) != effect["after_sha256"]:
        return "changed by you, kept"
    refusal = _definition_refusal(ctx, effect)
    if refusal:
        return refusal
    return None if _task(ctx)["delete"]() else "already gone"


@cs.undo_handler("task-restore")
def _undo_task_restore(ctx: dict, effect: dict) -> str | None:
    """Put the task EUTONOS replaced back, while the task is still the one EUTONOS registered."""
    seam = _task(ctx)
    found = seam["query"]()
    if found is None:
        return "already gone"
    if cs.sha256(cs.canonical({"command": found[0], "arguments": found[1]})) != effect["after_sha256"]:
        return "changed by you, kept"
    refusal = _definition_refusal(ctx, effect)
    if refusal:
        return refusal
    try:
        saved = json.loads(Path(effect["backup"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "the earlier task's backup is missing, kept"
    if saved.get("xml") and seam.get("restore"):
        seam["restore"](saved["xml"])
    else:  # no full definition: put back what it ran
        spec = saved["spec"]
        seam["register"]([spec["command"], *shlex.split(spec["arguments"], posix=False)])
    back = seam["query"]()
    if back is None or cs.sha256(cs.canonical({"command": back[0], "arguments": back[1]})) != cs.sha256(
            cs.canonical(saved["spec"])):
        return "the earlier task did not read back, kept"
    return None


def status(ctx: dict) -> dict:
    if cs.os_name(ctx) != "windows":
        return {"state": "held", "reason": HELD_REASON}
    return {"state": "installed", "reason": ""}


def _steps(ctx: dict) -> list:
    return [task_step(ctx)] if cs.os_name(ctx) == "windows" else []


def plan(ctx: dict) -> list[dict]:
    return cs.plan_steps(ctx, _steps(ctx))


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    """Register the task. Idempotent; nothing is written on macOS."""
    return cs.apply_steps(ctx, _steps(ctx), effects)


def verify(ctx: dict) -> dict:
    if cs.os_name(ctx) != "windows":
        return cs.verify_result(ID, [{"name": "task", "ok": False, "detail": HELD_REASON}], state="held")
    checks = cs.step_checks(ctx, ID, _steps(ctx))
    ports = ctx.get("ports")
    if ports is not None:  # the declared Appearance value (permissions entry companion-appearance): read only
        stored = ports.appearance_get()
        valid = stored in (None, "system", "light", "dark")
        checks.append({"name": "appearance", "ok": valid, "changed": not valid,
                       "detail": "not set (the companion follows the system)" if stored is None
                       else f"set to {stored}" if valid else f"holds {stored!r}, which is not a companion setting"})
    return cs.verify_result(ID, checks)


def remove(ctx: dict, effects: list) -> dict:
    """Undo the task, then delete the Appearance value the companion wrote under its own HKCU key (permissions entry
    ``companion-appearance``). The value is never recorded as an Effect because it only exists once the person picks an
    Appearance, so removal (uninstall, or the part switched off) deletes it by name here."""
    result = cs.remove_effects(ctx, effects)
    ports = ctx.get("ports")
    if ports is not None and cs.os_name(ctx) == "windows":
        ports.appearance_delete()
    return result


def stop(*roots) -> dict:
    """The companion's Stop: halt Crosstalk and read ``controls.halt`` back. ``degraded`` is true when the kit CLI
    could not do it and ``crosstalk_status.degraded_halt`` wrote the control directly."""
    from . import crosstalk_status
    return crosstalk_status.owner_control("halt", *roots)
