"""Repository lifecycle (IPC5 lane D): detach from this machine, full de-adoption, and the checks the add and remove
pages paste into a slice.

Detach is the default. It reverses this machine's project permission Effects and exclude lines, withdraws the root from
`device-repositories.json`, unregisters it from `projects.json` and retires the host last. Repository files are never
touched. `--de-adopt --plan PLAN.json` also reverses `eutonos.adoption.json` for the paths the plan marks `remove`, while
their hashes still match. Nothing is permanently deleted: removed repository content is backed up and the host is
archived, not deleted. Every step is read from live state, so a run can be repeated after an interruption.

This module imports only installed EUTONOS code, so it keeps working after the repository's own wrapper is gone.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from . import adoption, completion_checks, device_repositories, home, permissions, projects, tokenslang as ts

CHECKLIST = "remove-repository"
CHECKLIST_REVISION = "ipc5-d.1"
PROJECT_ENTRIES = ("send-message", "native-send-message", "codex-sandbox", "codex-record-rule")  # written by this module's permission helper
PERSON_STEPS = {
    "desktop-folder-auto": "In Claude Desktop, open this folder and choose Auto in the mode selector under the message box "
                           "(once per project and account). EUTONOS then reads your newest session record to confirm it.",
    "codex-trust": "In Codex, open this folder and choose Trust when asked, or add [projects.\"<project folder>\"] with "
                   "trust_level = \"trusted\" to your Codex config.toml.",
}
PERSON_REVERSALS = {
    "desktop-folder-auto": "In Claude Desktop, set this folder's chats back from Auto if you no longer want it. "
                           "EUTONOS never changes that setting for you.",
    "codex-trust": "In Codex, remove this folder's trust line ([projects.\"<project folder>\"] with trust_level) from your "
                   "Codex config.toml if you want Codex to stop trusting it. EUTONOS never edits that file for you.",
}
STEPS = (
    ("preservation-plan", "agent", "agent"),
    ("reverse-project-entries", "mechanical", "agent"),
    ("person-reversals", "person", "owner"),
    ("reverse-adoption", "mechanical", "agent"),
    ("withdraw-device-repository", "mechanical", "agent"),
    ("unregister-project", "mechanical", "agent"),
    ("retire-host", "mechanical", "agent"),
)
NL = bytes([10])
_FAILURES = (ts.ReadError, OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ project permissions (the add side)
def apply_project_permissions(root, *, user_home=None, choices=None, os_name=None, gen_id="add-repository", data=None):
    """Apply only the project-scoped permission entries (send-message, native-send-message, codex-sandbox, codex-record-rule, plus the
    exclude lines) and record their Effects on the registered project, so a later detach reverses exactly them.
    User-wide and per-launch entries are the machine install's, and are not touched here."""
    entry = projects.lookup(root)
    ts.require(entry is not None, "missing", "Register the project (add-project) before applying its permissions.")
    data = deepcopy(data or permissions.load())
    for item in data["entries"]:
        if item["id"] in permissions.WRITTEN and item["id"] not in PROJECT_ENTRIES:
            item["default"] = "off"
    recorded = projects.effects_for(root)
    try:
        effects = permissions.apply(root, user_home, choices=choices, os_name=os_name, gen_id=gen_id, data=data)
    except permissions.PermissionsApplyError as exc:  # keep what was written so it can be undone
        projects.set_effects(root, recorded + exc.effects)
        raise
    projects.set_effects(root, recorded + effects)
    return effects


def install_consent(home_dir=None):
    """The accepted install consent, read-only: `H/install/state.json` names the active generation and that
    generation's `record.json` holds the consent. Returns {"accepted_at", "choices", "gen_id"}, or None when there is
    no active record or no accepted consent (a missing, damaged or unaccepted record all count as none)."""
    base = Path(home_dir) if home_dir else home.home()
    try:
        state = json.loads((base / "install" / "state.json").read_text(encoding="utf-8"))
        gen = state["active"]
        ts.require(isinstance(gen, str) and gen and "/" not in gen and "\\" not in gen and gen != "..", "invalid", "bad generation")
        record = json.loads((base / "install" / "generations" / gen / "record.json").read_text(encoding="utf-8"))
        consent = record["consent"]
        if record.get("state") != "active" or not isinstance(consent.get("accepted_at"), str) or not consent["accepted_at"]:
            return None
        choices = consent.get("choices") or {}
        ts.require(isinstance(choices, dict), "invalid", "bad choices")
        return {"accepted_at": consent["accepted_at"], "gen_id": gen,
                "choices": {name: value for name, value in choices.items() if value in ("on", "off")}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ts.ReadError):
        return None


def _project_choices(choices):
    """The recorded choices that concern project entries and can be applied (a required entry cannot be turned off)."""
    data = permissions.load()
    result = {}
    for name in PROJECT_ENTRIES:
        if name in choices:
            entry = permissions.entry_by_id(data, name)
            if choices[name] == "on" or not entry["required"]:
                result[name] = choices[name]
    return result


def add_project(root, actor=None, host=None, *, apply_permissions=True, user_home=None, gen_id="add-repository"):
    """`projects.add`, then the project permission entries when an install consent is recorded. The Effects are
    recorded on the project, so a later detach reverses exactly them. Nothing is applied without an accepted consent,
    or with `apply_permissions=False` (`--no-permissions`); the result says so and what to do next. Entries 2 and 14
    stay person steps, listed with their text."""
    result = projects.add(root, actor, host)
    if result["outcome"] not in ("added", "already-added"):
        return result
    result["person_steps"] = [{"entry": name, "do": text} for name, text in PERSON_STEPS.items()]
    if not apply_permissions:
        result["permissions"] = {"applied": False, "message": "permissions not applied: --no-permissions",
                                 "next": "Run add-project again without --no-permissions to apply the project entries."}
        return result
    consent = install_consent()
    if consent is None:
        result["permissions"] = {"applied": False, "message": "permissions not applied: no install consent",
                                 "next": "Install EUTONOS and accept the consent screen, then run add-project again."}
        return result
    try:
        effects = apply_project_permissions(root, user_home=user_home, choices=_project_choices(consent["choices"]),
                                            gen_id=gen_id)
    except permissions.PermissionsError as exc:
        kept = len(getattr(exc, "effects", []) or [])
        result["permissions"] = {"applied": False, "message": "permissions partly applied: " + str(exc), "recorded": kept,
                                 "next": "Fix the cause and run add-project again; applied entries are recorded."}
        return result
    result["permissions"] = {"applied": True, "effects": len(effects), "consent_accepted_at": consent["accepted_at"]}
    return result


# ------------------------------------------------------------------ checkpoint (lane A's, with a local fallback)
def _checkpoint_module():
    """Lane A's `setup_checkpoint`, or None when it is not installed (lazy, so this module imports without it)."""
    try:
        return importlib.import_module("eutonos.setup_checkpoint")
    except ImportError:
        return None


def _open_steps(de_adopt):
    steps = []
    for name, kind, runner in STEPS:
        step = {"id": name, "kind": kind, "runner": runner, "state": "pending", "evidence": [], "reason": None,
                "next_action": None}
        if name == "preservation-plan" and not de_adopt:
            step.update(state="skipped", reason="detach level: no repository files are removed, so no plan is needed")
        elif name == "reverse-adoption" and not de_adopt:
            step.update(state="skipped", reason="detach level: repository files are untouched")
        steps.append(step)
    return steps


class _Checkpoint:
    """One place to record progress, backed by lane A's checkpoint or by a local JSON file beside projects.json."""

    def __init__(self, module, root, key, de_adopt):
        self.module, self.root, self.key = module, str(root), key
        self.used, self.note, self.resumed = "local-json", None, False
        self.path = home.projects_file().with_name("remove-repository-checkpoint-" + key + ".json")
        steps = _open_steps(de_adopt)
        if module is not None:
            self._open_module(steps)
        if self.used == "local-json":
            self._open_local(steps)

    # lane A's checkpoint is one file for the whole machine; never replace another unfinished checklist
    def _open_module(self, steps):
        try:
            existing = self.module.read()
        except Exception as exc:  # a damaged checkpoint is not ours to replace
            self.note = "the machine setup checkpoint could not be read (" + type(exc).__name__ + "); used a local file"
            return
        names = {step["id"] for step in steps}
        if existing is not None:
            open_steps = [step for step in existing["steps"] if step["state"] not in ("done", "skipped")]
            same = existing.get("checklist") == CHECKLIST and _same_root(existing.get("root"), self.root)
            if same and names <= {step["id"] for step in existing["steps"]}:
                self.used, self.resumed = "setup_checkpoint", True
                return
            if open_steps and not same:
                self.note = ("the machine setup checkpoint holds an unfinished " + str(existing.get("checklist"))
                             + " checklist, so it was left alone; used a local file")
                return
        self.module.start(CHECKLIST, steps, checklist_revision=CHECKLIST_REVISION, root=self.root)
        self.used = "setup_checkpoint"

    def _open_local(self, steps):
        value = self._read_local()
        if value is not None and value.get("checklist") == CHECKLIST and _same_root(value.get("root"), self.root):
            self.resumed = True
            return
        self._write_local({"profile": "eutonos.setup-checkpoint.v1", "checklist": CHECKLIST,
                           "checklist_revision": CHECKLIST_REVISION, "gen_id": None, "root": self.root,
                           "updated_at": _now(), "steps": steps})

    def _read_local(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ts.ReadError("invalid", "The local removal checkpoint is unreadable: " + str(self.path)) from exc
        ts.require(isinstance(value, dict) and isinstance(value.get("steps"), list), "invalid",
                   "The local removal checkpoint has an unsupported shape.")
        return value

    def _write_local(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp-" + str(os.getpid()))
        try:
            temporary.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def record(self, step_id, state, evidence=None, reason=None, next_action=None):
        evidence = [item for item in (evidence or []) if isinstance(item, str) and item]
        if state in ("done",) and not evidence and step_id in ("preservation-plan", "person-reversals"):
            evidence = ["recorded " + _now()]  # a person or agent step is done only with evidence
        if self.used == "setup_checkpoint":
            return self.module.record_step(step_id, state, evidence, reason=reason, next_action=next_action)
        value = self._read_local()
        for step in value["steps"]:
            if step["id"] == step_id:
                step["state"] = state
                step["evidence"] = step["evidence"] + [item for item in evidence if item not in step["evidence"]]
                step["reason"] = reason if reason is not None else step.get("reason")
                step["next_action"] = None if state == "done" else (next_action if next_action is not None else step.get("next_action"))
                break
        else:
            raise ts.ReadError("invalid", "Unknown checkpoint step: " + step_id)
        value["updated_at"] = _now()
        self._write_local(value)
        return value

    def describe(self):
        path = str(self.module.checkpoint_path()) if self.used == "setup_checkpoint" and hasattr(self.module, "checkpoint_path") \
            else str(self.path)
        result = {"used": self.used, "path": path, "resumed": self.resumed}
        if self.note:
            result["note"] = self.note
        return result


def _same_root(left, right):
    return isinstance(left, str) and isinstance(right, str) and projects._same(left, right)


# ------------------------------------------------------------------ read-only planning
def _simulate(effect):
    """What `permissions.remove` would do for one Effect, decided on a throwaway copy of its target."""
    target = Path(effect["target"])
    if not target.is_file():
        return {"effect_id": effect["effect_id"], "result": "already-gone"}
    with tempfile.TemporaryDirectory(prefix="eutonos-dryrun-") as scratch:
        copy = Path(scratch) / "target"
        shutil.copyfile(target, copy)
        trial = dict(effect, target=str(copy), undo=dict(effect.get("undo") or {}, created_dirs=[]))
        outcome = permissions.remove([trial])
    if outcome["kept"]:
        return {"effect_id": effect["effect_id"], "result": "would-keep", "reason": outcome["kept"][0]["reason"]}
    return {"effect_id": effect["effect_id"], "result": "would-remove"}


def _already_reversed(effect):
    """True when an earlier, interrupted run already took this Effect out (its target or its line is gone)."""
    target = Path(effect["target"])
    if not target.is_file():
        return True
    undo = effect.get("undo") or {}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if undo.get("op") == "line-remove":
        return undo.get("line") not in text.splitlines()
    if undo.get("op") == "text-remove":
        return undo.get("text") not in text
    return False


def _identity(root):
    """The registered entry for this root (or None) and its host directory (or None)."""
    entry = projects.lookup(root)
    host = None
    if entry is not None and isinstance(entry.get("host"), str):
        host = Path(entry["host"])
    else:
        guess = home.hosts() / projects.project_key(root)
        if (guess / "admission.json").is_file():
            host = guess
    return entry, host


def _enrolled_locator(root):
    """The stored device-repository locator for this root, or None."""
    for entry in device_repositories.read()["shared"]:
        for locator in entry["locators"]:
            if projects._same(locator, root):
                return locator
    return None


def _local(root, de_adopt):
    if not de_adopt:
        return None
    from .effects import LocalRoot
    return LocalRoot(root)


def _read_plan(path):
    ts.require(path, "invalid", "Full de-adoption needs a preservation plan: --plan PLAN.json "
                                 "(`preservation_template` in adoption.py prints the default).")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ts.ReadError("missing", "The preservation plan could not be read: " + str(path)) from exc
    ts.require(len(raw) <= 1024 * 1024, "invalid", "The preservation plan is too large.")
    return ts.loads(raw.decode("utf-8"))


def plan_remove_project(root, *, de_adopt=False, plan=None, host=None):
    """Everything a removal would change, and nothing changed. Refuses a de-adoption whose plan does not match the
    manifest (so a bad plan is caught before any write)."""
    root = os.path.abspath(root)
    entry, found = _identity(root)
    host = Path(host) if host else found
    report = {"outcome": "plan", "level": "de-adopt" if de_adopt else "detach", "root": root,
              "registered": entry is not None, "host": None if host is None else str(host),
              "host_state": None if host is None or not host.is_dir() else projects.host_state(host)}
    effects = projects.effects_for(root) if entry else []
    report["permission_effects"] = [_simulate(effect) for effect in reversed(effects)]
    report["person_reversals"] = [{"entry": name, "do": text} for name, text in PERSON_REVERSALS.items()]
    try:
        report["device_repositories"] = "would-withdraw" if _enrolled_locator(root) else "not-enrolled"
    except ts.ReadError:
        report["device_repositories"] = "unavailable"
    report["adoption"] = None
    if de_adopt:
        local = _local(root, True)
        manifest_present = local.read(adoption.MANIFEST_PATH) is not None
        if manifest_present:
            report["adoption"] = adoption.plan_removal(local, plan if isinstance(plan, dict) else _read_plan(plan))
        else:
            ts.require(plan is not None, "invalid", "Full de-adoption needs a preservation plan: --plan PLAN.json.")
            report["adoption"] = {"outcome": "no-manifest", "note": "no adoption manifest: nothing to reverse"}
    report["registry"] = "would-remove" if entry else "absent"
    report["host_action"] = ("would-retire" if report["host_state"] == "active" else
                             "already-retired" if report["host_state"] else "no-host")
    report["person_notes"] = ["Repository files are untouched." if not de_adopt else
                              "Removed repository content is copied to the backup folder first."]
    return report


# ------------------------------------------------------------------ the run
def remove_project(root, *, de_adopt=False, plan=None, dry_run=False, host=None, checkpoint=None):
    """Detach `root` from this machine, or fully de-adopt it under `plan`. `plan` is a preservation plan (dict) or a
    path to one. `dry_run` changes nothing. A step that fails is recorded as blocked and the run stops there; run it
    again to resume. `checkpoint` is only for tests: a setup_checkpoint-like module, or False to force the local file."""
    root = os.path.abspath(root)
    if de_adopt:
        ts.require(plan is not None, "invalid", "Full de-adoption needs a preservation plan: --plan PLAN.json.")
        plan = plan if isinstance(plan, dict) else _read_plan(plan)
    report = plan_remove_project(root, de_adopt=de_adopt, plan=plan, host=host)  # refuses a bad plan before any write
    if dry_run:
        return {**report, "outcome": "dry-run", "changed": False}
    key = projects.project_key(root)
    module = _checkpoint_module() if checkpoint is None else (checkpoint or None)
    done, result = [], {"outcome": "removed", "level": report["level"], "root": root, "steps": {}}
    with projects.locked():  # lock order is fixed: the projects lock first, then the machine lock (inside the checkpoint)
        ledger = _Checkpoint(module, root, key, de_adopt)
        result["checkpoint"] = ledger.describe()
        result["rechecked"] = ["registry entry"] + (["adoption manifest hashes"] if de_adopt else [])
        entry, found = _identity(root)
        directory = Path(host) if host else found

        def step(name, action):
            """Run one step; on failure record it blocked and stop the run."""
            ledger.record(name, "in_progress")
            try:
                state, evidence, detail = action()
            except _FAILURES as exc:
                reason = getattr(exc, "message", None) or type(exc).__name__ + ": " + str(exc)
                ledger.record(name, "blocked", reason=reason, next_action="Fix the cause, then run remove-project again.")
                result.update(outcome="blocked", blocked_at=name, reason=reason,
                              next="Fix the cause, then run the same remove-project command again; finished steps are not repeated.")
                return False
            waiting = "; ".join(f"{key}: {text}" for key, text in PERSON_REVERSALS.items()) if state == "pending" else None
            ledger.record(name, state, evidence, reason=detail if state == "skipped" else None, next_action=waiting)
            result["steps"][name] = {"state": state, "detail": detail if state != "skipped" else None, "evidence": evidence}
            return True

        def preservation():
            if not de_adopt:
                return "skipped", [], "detach level"
            return "done", ["plan checked against the adoption manifest; " + str(plan.get("profile"))], None

        def entries():
            effects = [effect for effect in (projects.effects_for(root) if entry else []) if not _already_reversed(effect)]
            outcome = permissions.remove(effects) if effects else {"removed": [], "kept": []}
            result["kept_permission_effects"] = outcome["kept"]
            if entry:  # what was reversed is no longer recorded, so a resumed run never repeats it
                held = {item["effect_id"] for item in outcome["kept"]}
                projects.set_effects(root, [effect for effect in effects if effect["effect_id"] in held])
            note = f"reversed {len(outcome['removed'])} effect(s); kept {len(outcome['kept'])} you changed"
            return "done", [note], outcome

        def person():
            result["person_reversals"] = [{"entry": name, "do": text} for name, text in PERSON_REVERSALS.items()]
            return "pending", [], PERSON_REVERSALS

        def reverse_adoption():
            if not de_adopt:
                return "skipped", [], "detach level"
            local = _local(root, True)
            if local.read(adoption.MANIFEST_PATH) is None:
                return "done", ["no adoption manifest left; nothing to reverse"], {"outcome": "no-manifest"}
            backup = home.install() / "backups" / ("remove-" + key)
            outcome = adoption.apply_removal(local, plan, backup_dir=backup)
            result["adoption"] = outcome
            result["backup_dir"] = str(backup)
            changed = [item for item in outcome.get("kept", []) if item["reason"] != "kept by the preservation plan"]
            if changed:
                result["kept_repository_files"] = changed
            evidence = [f"{outcome['outcome']}: removed {len(outcome.get('removed', []))}, kept {len(outcome.get('kept', []))}"]
            return "done", evidence, outcome

        def withdraw():
            locator = _enrolled_locator(root)
            if locator is None:
                return "done", ["not enrolled in device-repositories"], "not-enrolled"
            current = device_repositories.read()
            updated = device_repositories.update({"profile": device_repositories.REQUEST, "operation": "withdraw",
                                                  "locator": locator}, expected_revision=current["revision"])
            return "done", ["withdrawn; revision " + str(updated["revision"])[:16]], "withdrawn"

        def unregister():
            outcome = projects.remove(root)
            return "done", ["registry: " + outcome["outcome"]], outcome["outcome"]

        def retire():
            if directory is None or not directory.is_dir():
                return "done", ["no host directory found"], "no-host"
            outcome = projects.retire_host(directory, root=root, reason="remove-project " + report["level"])
            result["host"] = str(directory)
            return "done", ["host " + outcome["outcome"] + ": " + str(directory)], outcome["outcome"]

        for name, action in (("preservation-plan", preservation), ("reverse-project-entries", entries),
                             ("person-reversals", person), ("reverse-adoption", reverse_adoption),
                             ("withdraw-device-repository", withdraw), ("unregister-project", unregister),
                             ("retire-host", retire)):
            if not step(name, action):
                break
    if result["outcome"] == "removed":
        partial = result.get("kept_repository_files") or result.get("kept_permission_effects")
        if partial:
            result["outcome"] = "removed-with-kept"
    result["changed"] = True
    return result


# ------------------------------------------------------------------ completion checks for the add and remove pages
def _check(identity, title, remaining, required=True):
    return {"id": identity, "title": title, "required": required, "state": "pending", "evidence": [],
            "remaining_action": remaining, "related_work_ids": []}


def repository_checks(kind="add"):
    """The `completion_checks` list a slice for the add or remove page starts with. Every check is `pending`. The page
    pastes it into the repository's slice; each check becomes `verified` only with a real readback reference."""
    ts.require(kind in ("add", "remove"), "invalid", "kind is `add` or `remove`.")
    if kind == "add":
        checks = [
            _check("add.orient", "Exact root, branch, HEAD and dirty state recorded; repository classified; owners listed.",
                   "Record the orientation in the slice."),
            _check("add.registered", "add-project registered the root and created or adopted its one host.",
                   "Read back the projects.json entry and the host's admission."),
            _check("add.adoption-applied", "Adoption profile planned read-only, applied, and recorded in eutonos.adoption.json.",
                   "Read back eutonos.adoption.json and verify-profile."),
            _check("add.project-entries", "Project permission entries and exclude lines applied; Effects recorded on the project.",
                   "Read back the project's recorded Effects against the files."),
            _check("add.person-desktop-auto", "Desktop Auto mode chosen for this folder (person step).",
                   "Ask the person; confirm from the newest session record.", required=False),
            _check("add.person-codex-trust", "Codex trust given for this root (person step).",
                   "Ask the person; confirm with the Codex trust readback.", required=False),
            _check("add.docs", "Docs module filed and verified.", "Run the Docs discovery pass, file it, verify."),
            _check("add.nav", "NAV module filed and verified.", "Build the source map and verify freshness.", required=False),
            _check("add.ram", "RAM module filed and read back from a fresh process.", "File one record and read it back.",
                   required=False),
            _check("add.module-boundaries", "The person was asked whether to continue at each module boundary.",
                   "Record each offer in the Setup status block."),
            _check("add.repository-checks", "The repository's own checks pass.", "Run the repository's verification commands."),
            _check("add.finalize", "eutonos-setup finalize planned, applied and read back.", "Plan and apply eutonos-setup finalize."),
            _check("add.acceptance", "Evidence recorded in eutonos.acceptance.json.", "Record the acceptance evidence."),
            _check("add.setup-status", "The Setup status block shows the real state of Docs, NAV and RAM.",
                   "Update the Setup status block; the slice points to it and does not copy it."),
        ]
    else:
        checks = [
            _check("remove.work-parked", "Active work finished or parked and closed out.", "Close out or park the active work."),
            _check("remove.writers-stopped", "Chats working in this repository have stopped writing.",
                   "Halt Crosstalk, then confirm each chat has stopped (halt does not stop a running agent)."),
            _check("remove.preservation-plan", "Preservation plan agreed with the owner (full de-adoption only).",
                   "Agree the plan; the default keeps native Docs, RAM, NAV and history.", required=False),
            _check("remove.checkpoint-saved", "Checkpoint saved before the wrapper is removed.",
                   "Confirm the checkpoint file lists the steps."),
            _check("remove.entries-reversed", "Project permission Effects and exclude lines reversed; changed ones reported.",
                   "Read back the files named in the Effects."),
            _check("remove.person-reversals", "The person was told the Desktop Auto and Codex trust settings are theirs to undo.",
                   "List them for the person; they remain the person's to change."),
            _check("remove.adoption-reversed", "Adoption manifest reversed for remove paths; changed files reported and kept.",
                   "Read back the removal report and the backup folder.", required=False),
            _check("remove.device-withdrawn", "Root withdrawn from device-repositories.json.",
                   "Read back device-repositories.json."),
            _check("remove.unregistered", "Root removed from projects.json under the registry lock.",
                   "Read back projects.json."),
            _check("remove.host-retired", "Host archived with its retired marker, not deleted.",
                   "Read back the host's retired.json and that read_snapshot still validates."),
            _check("remove.repository-files", "Repository files are untouched (detach) or only plan-listed paths changed (de-adopt).",
                   "Compare the repository tree with its recorded state."),
        ]
    return completion_checks.validate(checks)


# ------------------------------------------------------------------ command line
def main(argv=None):
    parser = argparse.ArgumentParser(prog="eutonos remove-project",
                                     description="Detach a repository from this machine, or fully de-adopt it under a plan.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--de-adopt", action="store_true", help="also reverse eutonos.adoption.json (needs --plan)")
    parser.add_argument("--plan", help="preservation plan JSON: every adoption-manifest path, keep or remove")
    parser.add_argument("--dry-run", action="store_true", help="print every planned change and change nothing")
    parser.add_argument("--host", help="the project's host directory, when it is no longer in the registry")
    parser.add_argument("--print-checks", choices=("add", "remove"),
                        help="print the completion_checks the add or remove page pastes into a slice, and change nothing")
    parser.add_argument("--print-preservation-template", action="store_true",
                        help="print the default preservation plan for --root (needs eutonos.adoption.json), and change nothing")
    args = parser.parse_args(argv)
    try:
        if args.print_checks:
            sys.stdout.buffer.write(ts.encoded({"completion_checks": repository_checks(args.print_checks)}) + NL)
            return 0
        if args.print_preservation_template:
            from .effects import LocalRoot
            sys.stdout.buffer.write(ts.encoded(adoption.preservation_template(LocalRoot(os.path.abspath(args.root)))) + NL)
            return 0
        ts.require(args.de_adopt or not args.plan, "invalid", "--plan only applies with --de-adopt.")
        result = remove_project(args.root, de_adopt=args.de_adopt, plan=args.plan, dry_run=args.dry_run, host=args.host)
    except (ts.ReadError, OSError, UnicodeError, KeyError, TypeError) as exc:
        sys.stdout.buffer.write(ts.encoded({"outcome": "error", "code": getattr(exc, "code", "invalid"),
                                          "message": getattr(exc, "message", "Removal input or local I/O unavailable.")}) + b"\n")
        return 2
    sys.stdout.buffer.write(ts.encoded(result) + b"\n")
    return 2 if result["outcome"] in ("blocked", "error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
