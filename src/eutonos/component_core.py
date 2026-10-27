"""The ``core`` component (IPC5 lane B, contract c): the always-installed machine parts.

* permissions entry 22 (``user-path``): ``H/bin`` on the user PATH. The installer is its one writer
  (``installer_effects.path_effect``, an ``env-path`` Effect); this part only reads it back;
* the core's own permission-list entries: ``record-keeping-rule`` (``H/settings/claude-launch.json``), and per project
  ``codex-sandbox`` and ``codex-record-rule`` with their ``.git/info/exclude`` lines;
* the files a repository that adopts EUTONOS receives (``adopter_files``): its own lessons owner with the ask-why
  rule (owner decision 12, 2026-09-29), and an empty ``eutonos.shared-work.json``. The adoption step (lane D) writes
  them; the component ships the text so the wheel, not a person's private folder, is the source.

``ctx`` and the injectable ``seams`` are described in ``component_support``.
"""
from __future__ import annotations


from . import component_support as cs

ID = "core"
ENTRY = "user-path"
SHARED_WORK_PROFILE = "eutonos.shared-work.config.v1"
SHARED_WORK_IDENTITY = ("repository_id", "scope_id", "namespace")

ASK_WHY_RULE = (
    "When a worker or model gets something wrong (a false claim, a missed item, an early stop), ask it why before "
    "moving on: resume its own session read-only, quote the wrong claim and the proof, and ask what in the brief, "
    "commands or evidence led it there and what would have prevented it. Add the lesson to this file and to the "
    "closeout result so the project record keeps it, and fold repeated lessons into brief templates. Lessons exist "
    "so this repository and its agents improve their own workflow.")

LESSONS_TEMPLATE = (
    "# Lessons\n\n"
    "This file is this repository's own lessons owner. It belongs to you: EUTONOS never overwrites it once it exists.\n\n"
    "## The ask-why rule\n\n" + ASK_WHY_RULE + "\n\n"
    "## Lessons\n\n"
    "Add one entry per lesson, newest first: what went wrong, the evidence, and the prevention rule.\n")


def empty_shared_work(identity: dict | None = None) -> dict:
    """The shared-work manifest an adopter gets: no canonical source, no peers (contract f, defaults). ``identity`` is
    the repository's ``repository_id``/``scope_id``/``namespace`` from ``eutonos.read.json`` when known."""
    identity = identity or {}
    return {"profile": SHARED_WORK_PROFILE, **{key: identity.get(key) for key in SHARED_WORK_IDENTITY},
            "canonical": None, "peers": [], "attempt_owner": None, "fetch_interval_seconds": 30}


def adopter_files(identity: dict | None = None) -> dict[str, bytes]:
    """``{path relative to the repository: bytes}`` for the files an adopting repository gets from ``core``."""
    from . import tokenslang as ts
    return {"docs/LESSONS.md": LESSONS_TEMPLATE.encode("utf-8"),
            "eutonos.shared-work.json": ts.encoded(empty_shared_work(identity)) + b"\n"}


# --------------------------------------------------------------------------- PATH
# The user PATH has one writer: ``installer_effects.path_effect`` (Windows registry value, or the marked ``.zprofile``
# line on a Mac) and one undo, ``installer_effects.remove_path_entry``. The core part only reads it back here and keeps
# the undo name an older record may carry, so that record is still removed through the same code.
def _ports(ctx: dict):
    return ctx.get("ports")


def path_check(ctx: dict) -> dict:
    """Readback of the PATH entry: is ``H/bin`` on the user PATH (Windows registry) or in ``~/.zprofile`` (macOS)?"""
    from . import installer_effects as fx
    entry = str(cs.ctx_home(ctx) / "bin")
    ports = _ports(ctx)
    if cs.os_name(ctx) == "windows":
        if ports is None:
            return {"name": "user-path", "ok": True, "detail": "not read here (no ports)"}
        text = ports.user_path_get() or ""
        found = any(fx._same_entry(item, entry) for item in text.split(";"))
    else:
        profile = cs.user_home(ctx) / ".zprofile"
        line = f'export PATH="{entry}:$PATH" {fx.PATH_MARK}'
        found = profile.is_file() and line in profile.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"name": "user-path", "ok": found, "detail": "present" if found else "absent"}


@cs.undo_handler("env-path-remove")
def _undo_env_path(ctx: dict, effect: dict) -> str | None:
    """An older install recorded its own PATH Effect with this undo; it is undone by the installer's one remover."""
    from . import installer_effects as fx
    ports = _ports(ctx)
    if ports is None:
        from .installer_ports import Ports
        ports = Ports()
    state, reason = fx.remove_path_entry(ports, effect["undo"])
    return "already gone" if state == "gone" else reason


# ------------------------------------------------------------- the component functions
def _steps(ctx: dict) -> list:
    return []


def status(ctx: dict) -> dict:
    return {"state": "installed", "reason": ""}


def plan(ctx: dict) -> list[dict]:
    """The Effects ``apply`` would produce; writes nothing."""
    effects = cs.plan_steps(ctx, _steps(ctx))
    effects += cs.permission_effects(dict(ctx, dry_run=True), ID, write=False)
    return effects


def apply(ctx: dict, effects: list | None = None) -> list[dict]:
    """Write the core's permission entries (not the PATH entry: the installer writes that); idempotent, and each Effect
    is returned as written."""
    done: list = []
    try:
        cs.apply_steps(ctx, _steps(ctx), effects, done)
        keep = None if effects is None else {item.get("permission_entry") for item in effects}
        only = None if keep is None else keep - {None, ENTRY}
        if only is None or only:
            cs.permission_effects(ctx, ID, write=True, only=only, done=done)
    except cs.ComponentApplyError as exc:
        raise cs.ComponentApplyError(str(exc), exc.effects or done) from exc
    return done


def verify(ctx: dict) -> dict:
    checks = cs.step_checks(ctx, ID, _steps(ctx)) + [path_check(ctx)]
    missing = cs.permission_missing(ctx, ID)
    checks.append({"name": "permission entries", "ok": not missing,
                   "detail": "all present" if not missing else "missing: " + ", ".join(missing)})
    checks.append({"name": "adopter files", "ok": "docs/LESSONS.md" in adopter_files(),
                   "detail": "lessons file and empty shared-work manifest ship with core"})
    return cs.verify_result(ID, checks)


def remove(ctx: dict, effects: list) -> dict:
    """Undo the Effects in reverse order, each only while it still holds what was written."""
    return cs.remove_effects(ctx, effects)
