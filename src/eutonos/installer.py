"""The one installer: install, update, rollback, uninstall, resume and status (IPC5 lane A).

``eutonos-install install --wheel W`` builds a new generation beside any earlier one, in this fixed order:
runtime, kits, surfaces (the ``H/bin`` launchers and the Claude marketplace), effects (components, the permissions
list, the instruction blocks, the PATH entry, the owner profile), plugin and activation. Each phase writes a receipt
under ``H/install/receipts/<gen_id>/`` and the journal ``H/install/journal/<txn_id>.jsonl`` keeps one line per intent,
effect and phase, so an interrupted run can be inspected and resumed. Only this module writes ``state.json`` and the
record. The person's only step is the consent screen (``consent.py``); everything else is an agent step.

The OS and app calls all go through ``installer_ports``; a test passes ``FakePorts`` and temporary folders and never
reaches the real machine.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import functools
import importlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from . import component_hermes
from . import component_state
from . import component_support
from . import consent as consent_module
from . import home as eutonos_home
from . import install_record as ir
from . import installer_effects as fx
from . import installer_ports as iports
from . import kits as kits_module
from . import pending_actions
from . import permissions
from . import prereq as prereq_module
from . import release_source as rs
from . import self_repair as sr
from . import setup_checkpoint as sc
from . import tokenslang as ts
from . import update_check as uc

CHECKLIST_REVISION = "ipc5-1"
COMPONENT_PROFILE = "eutonos.component.v1"
PHASES = ir.PHASES
PRIVATE_PREFIXES = ("runtime.", "kit.")
KEEP_GENERATIONS = 2
PROGRESS = None  # a callable ``(phase, number, total)`` told when each phase starts (the Setup wizard's live steps)
PHASE_TEXT = {"runtime": "Setting up the EUTONOS program", "kits": "Copying the bundled parts",
              "surfaces": "Adding the commands and the Claude plugin files",
              "effects": "Applying your choices to Claude, Codex and this computer",
              "plugin": "Registering the Claude plugin", "activate": "Finishing and checking"}

INSTALL_STEPS = (
    ("prereq", "mechanical", "agent"), ("consent", "person", "owner"), ("runtime", "mechanical", "agent"),
    ("kits", "mechanical", "agent"), ("surfaces", "mechanical", "agent"), ("components", "mechanical", "agent"),
    ("permissions", "mechanical", "agent"), ("instructions", "mechanical", "agent"), ("path", "mechanical", "agent"),
    ("owner-profile", "mechanical", "agent"), ("plugin", "mechanical", "agent"), ("activate", "mechanical", "agent"),
)
INSTALL_OPEN = (
    ("restart-apps", "person", "owner", "Restart Claude Desktop, Claude Code and Codex so they read the new settings."),
    ("remote-control-prompt", "person", "owner", "Answer the Remote Control prompt (y/n) the first time Claude starts it."),
    ("mac-grants", "person", "owner", "On a Mac, allow the Terminal and Claude automation prompts when macOS asks."),
    ("orchestration", "agent", "agent", "Open the orchestrating chat and run the Setup status checks (doctor)."),
    ("cross-app-check", "agent", "agent", "Send one Crosstalk status letter across apps and read it back."),
)


# ---------------------------------------------------------------- small helpers
def resolve_home(home=None) -> Path:
    base = Path(home) if home is not None else eutonos_home.home()
    ts.require(base.is_absolute(), "invalid", f"The EUTONOS home must be an absolute path: {base}")
    return base


def default_user_home(base, for_this_user: bool | None = None) -> Path | None:
    """The folder standing for the user's home. The real home only when `base` is the real ``~/.eutonos`` or the
    install was explicitly marked real (``install --for-this-user``, kept in the install state; `for_this_user`
    None reads that mark); any other EUTONOS home (a test or a staged install) gets ``<home>/staging-user``, so it
    can never reach the real apps by omission. None means "the real home" (``permissions.locations`` resolves it)."""
    if for_this_user is None:
        for_this_user = eutonos_home.for_this_user(base)
    return None if for_this_user or eutonos_home.is_default(base) else Path(base) / "staging-user"


def _version_tuple(text) -> tuple:
    parts = []
    for item in str(text).replace("-", ".").replace("+", ".").split("."):
        if not item.isdigit():
            break
        parts.append(int(item))
    return tuple(parts)


def version_at_least(version, floor) -> bool:
    """True when `version` is at or above `floor` (a missing floor accepts everything)."""
    if not floor:
        return True
    return _version_tuple(version) >= _version_tuple(floor)


def registered_projects(layout) -> list:
    """The projects lane D registered (``H/projects.json``). A damaged file counts as registered: refuse, never guess.

    Lock order (fixed across EUTONOS): the projects lock comes BEFORE the machine lifecycle lock, never the reverse
    (lane D's remove-project holds the projects lock and takes the machine lock inside it). This function only reads
    the file, without any lock, so it is safe to call while holding the machine lock."""
    try:
        value = json.loads(layout.projects_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [{"root": None, "reason": "H/projects.json is unreadable"}]
    items = value.get("projects") if isinstance(value, dict) else None
    return list(items) if isinstance(items, list) else []


# -------------------------------------------------------------------- components
def _check_manifest(manifest) -> dict:
    ts.require(isinstance(manifest, dict) and manifest.get("profile") == COMPONENT_PROFILE, "invalid",
               "A component manifest is not an eutonos.component.v1 document.")
    ts.require(isinstance(manifest.get("id"), str) and manifest["id"], "invalid", "A component manifest has no id.")
    ts.require(manifest.get("kind", "bundled") in ("core", "bundled", "addon"), "invalid",
               f"Component {manifest['id']}: unknown kind.")
    ts.require(manifest.get("default", "on") in ("on", "off", "held"), "invalid",
               f"Component {manifest['id']}: unknown default.")
    return manifest


def load_components(extra=None, *, group="eutonos.components") -> list[dict]:
    """Manifests from ``eutonos/components/*.json`` in the package, from the ``eutonos.components`` entry points
    (add-ons), and any `extra` manifests a caller passes. Ids must be unique."""
    found = []
    folder = Path(__file__).resolve().parent / "components"
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            found.append(json.loads(path.read_text(encoding="utf-8")))
    try:
        from importlib import metadata
        for point in metadata.entry_points(group=group):
            loaded = point.load()
            loaded = loaded() if callable(loaded) else loaded
            if isinstance(loaded, (str, Path)):
                loaded = json.loads(Path(loaded).read_text(encoding="utf-8"))
            found.extend(loaded if isinstance(loaded, list) else [loaded])
    except Exception:  # an add-on that fails to load must not stop the core install
        pass
    found.extend(extra or [])
    ids = [_check_manifest(item)["id"] for item in found]
    ts.require(len(ids) == len(set(ids)), "invalid", "Two components share an id.")
    return found


def _entry_function(manifest, name, resolver=None):
    spec = (manifest.get("entry") or {}).get(name)
    if not spec:
        return None
    if resolver is not None:
        return resolver(spec)
    module, _, function = str(spec).partition(":")
    return getattr(importlib.import_module(module), function)


STATUS_STATES = ("installed", "held", "skipped", "unlicensed")


def resolve_components(manifests, *, os_name, version, has_app, choices=None, check_licence=None,
                       status_of=None, enabled_state=None, has_tool=None) -> list[dict]:
    """Decide each component's state: ``installed``, ``held``, ``skipped`` or ``unlicensed``, in dependency order.
    `has_app(name)` says whether an app is present; `choices` is ``{id: "on"|"off"}``; `check_licence(manifest)``
    returns ``{state, detail}`` for a manifest that needs one. `status_of(manifest)` is the component's own optional
    ``status(ctx)`` answer ``{state, reason}``; it is asked last and may say held, skipped or unlicensed.

    Every available part is ``installed``; "off" (a choice, or the manifest default) only leaves it disabled, which
    the row says as ``enabled: False`` (``enabled`` is False on every row that is not installed). `enabled_state`
    (``{id: bool}``, from ``settings/components.json``) overrides the choice for a part the person has switched since.

    `has_tool(name)` says whether a command-line tool (Git) is on PATH (None: assume present). A part whose
    ``requires.tools`` names a missing tool is ``skipped`` with the reason ``needs Git``; the core part cannot be
    skipped, so it stays ``installed`` and its row carries ``missing_tools`` (what in it is unavailable)."""
    choices = choices or {}
    enabled_state = enabled_state or {}
    pending = {item["id"]: item for item in manifests}
    rows: dict[str, dict] = {}
    ordered: list[dict] = []

    def settle(manifest, state, reason=None, enabled=True):
        row = {"id": manifest["id"], "version": manifest.get("version"), "state": state, "reason": reason,
               "enabled": bool(enabled) and state == "installed", "manifest": manifest,
               "missing_tools": [name for name in (manifest.get("requires") or {}).get("tools") or []
                                 if has_tool is not None and not has_tool(name)]}
        rows[manifest["id"]] = row
        ordered.append(row)
        pending.pop(manifest["id"], None)

    progress = True
    while pending and progress:
        progress = False
        for manifest in list(pending.values()):
            needs = list((manifest.get("requires") or {}).get("components") or [])
            if any(name not in rows and name in pending for name in needs):
                continue
            progress = True
            requires = manifest.get("requires") or {}
            wanted = manifest.get("default", "on")
            if manifest["id"] in choices:
                wanted = choices[manifest["id"]] if choices[manifest["id"]] in ("on", "off") else wanted
            missing = [name for name in needs if name not in rows or rows[name]["state"] != "installed"]
            if requires.get("os") and os_name not in requires["os"]:
                settle(manifest, "skipped", f"not for {os_name}")
            elif requires.get("eutonos") and not version_at_least(version, str(requires["eutonos"]).lstrip(">=~ ")):
                settle(manifest, "skipped", f"needs EUTONOS {requires['eutonos']}")
            elif requires.get("apps") and not any(has_app(name) for name in requires["apps"]):
                settle(manifest, "skipped", "none of " + ", ".join(requires["apps"]) + " found")
            elif missing:
                settle(manifest, "skipped", "needs " + ", ".join(missing))
            elif manifest.get("kind") != "core" and has_tool is not None and any(
                    not has_tool(name) for name in requires.get("tools") or []):
                settle(manifest, "skipped", "needs " + ", ".join(
                    name.capitalize() for name in requires["tools"] if not has_tool(name)))
            elif wanted == "held":
                settle(manifest, "held", "held for later")
            else:
                licence = manifest.get("licence") or {}
                verdict = None
                if licence.get("required"):
                    verdict = check_licence(manifest) if check_licence else {"state": "unknown", "detail": "no check"}
                    if verdict.get("state") != "valid":
                        settle(manifest, "unlicensed", f"{verdict.get('state')}: {verdict.get('detail')}")
                        continue
                own = status_of(manifest) if status_of else None
                if own and own.get("state") in STATUS_STATES and own["state"] != "installed":
                    settle(manifest, own["state"], own.get("reason") or None)
                    continue
                if manifest.get("kind") == "core":
                    settle(manifest, "installed")  # the core is always on
                elif manifest["id"] in enabled_state:
                    on = bool(enabled_state[manifest["id"]])
                    settle(manifest, "installed", None if on else "turned off", on)
                elif wanted == "off":
                    settle(manifest, "installed", "off by default" if manifest["id"] not in choices else "turned off",
                           False)
                else:
                    settle(manifest, "installed")
    for manifest in pending.values():  # a dependency cycle or an unknown dependency
        settle(manifest, "skipped", "unresolvable dependencies")
    return ordered


# ------------------------------------------------------------------------- run
class Run:
    """One lifecycle operation: the layout, the ports, the state and the record being built, and the journal."""

    def __init__(self, base, *, kind, ports, user_home=None, os_name=None, now=None, manifests=None, resolver=None,
                 for_this_user=None):
        self.layout = ir.Layout(base)
        self.base = Path(base)
        self.kind = kind
        self.for_this_user = bool(eutonos_home.for_this_user(base) if for_this_user is None else for_this_user)
        self.os_name = os_name or iports.default_os_name()
        self.ports = ports or iports.Ports(self.os_name)
        self.where = permissions.locations(None, user_home or default_user_home(self.base, self.for_this_user), self.os_name,
                                           eutonos_dir=self.base)
        self.user_home = self.where["home"]
        self.now = now
        self.manifests = manifests
        self.resolver = resolver
        self.state = None
        self.record = None
        self.txn = None
        self.params: dict = {}
        self.kit_override: dict = {}  # kit name -> folder; a dry run plans from a scratch copy of the wheel's kits
        self.info: dict | None = None
        self.notes: list[str] = []
        self.substeps: dict = {}
        self.rows: list[dict] | None = None
        self.doctor: dict | None = None  # set by the new runtime's handover, else `finish` runs it
        self.doctor_fn = None

    # -- journal, record and state
    def journal(self, event: dict) -> None:
        ir.journal_append(self.layout, self.txn, event, self.now)

    def save(self) -> None:
        ir.write_record(self.layout, self.state, self.record)
        ir.write_state(self.layout, self.state)

    def sink(self, item: dict, by: str | None = None) -> dict:
        """Journal an Effect first, then put it in the record (write-ahead: an interrupted run keeps it)."""
        built = dict(item)
        if by:
            built["by"] = by
        self.journal({"event": "effect", "effect": built})
        ir.merge_effects(self.record, [built])
        self.save()
        return built

    def withdraw(self, effect_ids) -> None:
        """Take Effects back out of the record after their change was undone in the same run, and journal that so a
        resume does not put them back."""
        ids = set(effect_ids)
        self.journal({"event": "effect-withdrawn", "effect_ids": sorted(ids)})
        self.record["effects"] = [item for item in self.record["effects"] if item["effect_id"] not in ids]
        self.save()

    def set_phase(self, phase: str) -> None:
        if self.state.get("pending"):
            self.state["pending"]["phase"] = phase
        self.journal({"event": "phase", "phase": phase, "state": "start"})
        self.save()

    # -- components
    def components(self) -> list[dict]:
        if self.manifests is None:
            self.manifests = load_components()
        return self.manifests

    def component(self, component_id):
        for manifest in self.components():
            if manifest["id"] == component_id:
                return manifest
        return None

    def component_ctx(self, manifest=None, *, dry_run=False) -> dict:
        kit_path = None
        wanted = None
        if manifest and manifest.get("kit"):
            wanted = str(manifest["kit"]).split("/", 1)[-1]
        for item in (self.record or {}).get("kits", []):
            if wanted and item["component"] == wanted:
                kit_path = self.kit_override.get(wanted) or self.layout.kits / item["kit_id"]
        runtime_python = None
        if self.info:
            runtime_python = self.ports.runtime_python(self.layout.runtimes / self.info["artifact_id"])
        return {"home": self.base, "os": self.os_name, "gen_id": (self.record or {}).get("gen_id"),
                "runtime_python": runtime_python, "kit_path": kit_path, "projects": registered_projects(self.layout),
                "record": copy.deepcopy(self.record), "dry_run": dry_run, "now": ir.now_iso(self.now),
                "user_home": self.user_home, "seams": self.ports.seams(), "ports": self.ports, "kits": kits_module,
                "component_choices": {**self.owner_components(), **(self.params.get("component_choices") or {})},
                "choices": (self.record or {}).get("consent", {}) and (self.record["consent"].get("choices") or {})}

    def has_app(self, name) -> bool:
        return (bool(self.ports.which(name)) or (self.user_home / f".{name}").is_dir()
                or name == "hermes" and component_hermes.found(self.component_ctx()))

    def has_tool(self, name) -> bool:
        return bool(self.ports.which(name))

    def component_status(self, manifest):
        """The component's optional ``status(ctx)`` (a function named ``status`` beside its ``plan``), or None."""
        spec = (manifest.get("entry") or {}).get("plan")
        if not spec:
            return None
        try:
            function = _entry_function({"entry": {"status": str(spec).partition(":")[0] + ":status"}}, "status",
                                       self.resolver)
        except Exception:  # no such function: the component simply has no status hook
            return None
        if function is None:
            return None
        try:
            return function(self.component_ctx(manifest))
        except Exception as exc:  # a broken status answer is "skipped", never a crash of the install
            return {"state": "skipped", "reason": f"status failed: {type(exc).__name__}: {exc}"}

    def licence_check(self, manifest) -> dict:
        spec = (manifest.get("licence") or {}).get("check")
        function = _entry_function({"entry": {"check": spec}}, "check", self.resolver)
        if function is None:
            return {"state": "unknown", "detail": "no licence check is shipped"}
        try:
            return function(self.component_ctx(manifest))
        except Exception as exc:  # a broken licence hook is "unknown", never a crash
            return {"state": "unknown", "detail": f"{type(exc).__name__}: {exc}"}

    def resolve(self) -> list[dict]:
        if self.rows is None:
            profile = self.owner_components()
            choices = {**profile, **(self.params.get("component_choices") or {})}
            self.rows = resolve_components(self.components(), os_name=self.os_name, version=self.info["version"],
                                           has_app=self.has_app, choices=choices, check_licence=self.licence_check,
                                           status_of=self.component_status, enabled_state=self.enabled_state(),
                                           has_tool=self.has_tool)
        return self.rows

    def enabled_state(self) -> dict:
        """``{id: bool}`` from ``settings/components.json``: what the person has switched on or off. A first install
        and a dry run ignore any stale file (their choices win); an unreadable file raises, never reads as "all on"."""
        if self.params.get("fresh_components") or self.kind == "install":
            return {}
        return {name: item["enabled"] for name, item in component_state.read(self.base).items()
                if item["source"] == "toggle"}

    def owner_components(self) -> dict:
        source = self.params.get("owner_profile") or str(self.layout.profiles / "owner.json")
        try:
            document = json.loads(Path(source).read_text(encoding="utf-8"))
            value = document.get("components")
            return dict(value) if isinstance(value, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}


# ---------------------------------------------------------------------- reversal
def reverse_effects(run: Run, effects: list[dict], *, tag="uninstall") -> dict:
    """Undo `effects` newest first. Component effects go to their component's ``remove`` once, at the component's
    newest effect. Returns ``{removed[], kept[{effect_id, reason}]}``; nothing here deletes data."""
    remover = fx.Remover(run.layout, run.ports, run.user_home, run.os_name, tag)
    removed, kept, failed, handled = [], [], [], set()
    # Keep runtimes and kits available when an outside effect still needs its remover.
    ordered = [item for item in reversed(effects) if not item["effect_id"].startswith(PRIVATE_PREFIXES)]
    ordered += [item for item in reversed(effects) if item["effect_id"].startswith(PRIVATE_PREFIXES)]
    for item in ordered:
        if failed and item["effect_id"].startswith(PRIVATE_PREFIXES):
            row = {"effect_id": item["effect_id"], "reason": "retained until incomplete removals can be retried"}
            kept.append(row)
            failed.append(row)
            continue
        manifest = run.component(item.get("component")) if item.get("by") == "component" else None
        function = _entry_function(manifest, "remove", run.resolver) if manifest else None
        if manifest and function:
            if manifest["id"] in handled:
                continue
            handled.add(manifest["id"])
            group = [entry for entry in effects if entry.get("by") == "component" and entry["component"] == manifest["id"]]
            try:
                result = function(run.component_ctx(manifest), group)
                removed += list(result.get("removed") or [])
                part_kept = list(result.get("kept") or [])
                kept += part_kept
                failed += [row for row in part_kept if component_support.removal_incomplete(row.get("reason"))]
            except Exception as exc:
                rows = [{"effect_id": entry["effect_id"], "reason": f"component remove failed: {exc}"} for entry in group]
                kept += rows
                failed += rows
            continue
        try:
            state, reason = remover.one(item)
        except Exception as exc:
            state, reason = "kept", f"{type(exc).__name__}: {exc}"
        if state == "kept":
            row = {"effect_id": item["effect_id"], "reason": reason}
            kept.append(row)
            if component_support.removal_incomplete(reason):
                failed.append(row)
        else:
            removed.append(item["effect_id"])
    return {"removed": removed, "kept": kept, "failed": failed}


# ------------------------------------------------------------------------ phases
def phase_runtime(run: Run) -> dict:
    layout, ports, info = run.layout, run.ports, run.info
    runtime = layout.runtimes / info["artifact_id"]
    ownership = fx.runtime_effect(layout, ports, info, runtime)
    ownership["undo"]["building"] = True
    run.sink(ownership, by="installer")
    reused = False
    if runtime.is_dir() and ports.runtime_python(runtime).exists() \
            and fx.runtime_package_digest(ports.site_packages(runtime)) == info["package_digest"]:
        reused = True
    else:
        if runtime.exists():  # a half-built runtime of this very artifact: this run owns it
            run.journal({"event": "intent", "what": "remove-partial-runtime", "target": str(runtime)})
            shutil.rmtree(runtime)
        run.journal({"event": "intent", "what": "build-runtime", "target": str(runtime)})
        layout.runtimes.mkdir(parents=True, exist_ok=True)
        ports.build_runtime(runtime, Path(run.params["wheel"]))
        ts.require(fx.runtime_package_digest(ports.site_packages(runtime)) == info["package_digest"], "runtime-mismatch",
                   "The runtime's package bytes differ from the wheel; it was not accepted.")
    run.sink(fx.runtime_effect(layout, ports, info, runtime), by="installer")
    return {"runtime": str(runtime), "reused": reused, "package_digest": info["package_digest"]}


def phase_kits(run: Run) -> dict:
    layout, info = run.layout, run.info
    made = []
    with tempfile.TemporaryDirectory(prefix="eutonos-kits-") as scratch:
        for name in info["kits"]:
            source = fx.extract_kit(run.params["wheel"], name, Path(scratch) / name)
            files = kits_module.tree_files(source)
            entry = {"component": name, "kit_id": kits_module.kit_id(name, files), "tree_hash": kits_module.tree_hash(files)}
            ownership = fx.kit_effect(entry, layout.kits / entry["kit_id"])
            ownership["undo"]["building"] = True
            run.sink(ownership, by="installer")
            run.journal({"event": "intent", "what": "materialize-kit", "target": str(layout.kits), "kit": name})
            result = kits_module.materialize(run.base, name, source)
            entry = {"component": name, "kit_id": result["kit_id"], "tree_hash": result["tree_hash"]}
            ts.require(kits_module.verify(Path(result["path"])), "kit-mismatch", f"Kit {name} did not verify after copy.")
            made.append(entry)
            run.sink(fx.kit_effect(entry, Path(result["path"])), by="installer")
    run.record["kits"] = made
    run.save()
    return {"kits": made}


def _recorded_files(record, effect_id) -> dict:
    for item in (record or {}).get("effects", []):
        if item["effect_id"] == effect_id:
            return dict((item.get("undo") or {}).get("files") or {})
    return {}


def _changed_by_you(folder: Path, old: dict, shipped: dict) -> dict:
    """``{name: current sha256}`` of recorded files whose bytes are neither the recorded ones nor the new shipped ones."""
    found = {}
    for name in old:
        current = ir.sha256_file(Path(folder) / name)
        if current is not None and current != old[name] and current != shipped.get(name):
            found[name] = current
    return found


def _three_way_row(block: str, folder: Path, name: str, old: dict, shipped: dict, current: str) -> dict:
    return {"block": block, "target": str(Path(folder) / name), "reason": "changed-by-you",
            "old_shipped_sha256": old.get(name), "new_shipped_sha256": shipped.get(name), "current_sha256": current}


def sync_surfaces(run: Run, *, runtime: Path, kit_dir, names: list[str], gen_id: str, older=None, repoint=True) -> dict:
    """Bring ``H/bin`` and the Claude marketplace to `runtime` and `kit_dir`. The marketplace is rendered into the
    generation's own folder (``install/generations/<gen_id>/marketplace``); with `repoint` it is then copied to
    ``H/marketplace/crosstalk``, the folder Claude has registered. An install or update waits and repoints when the
    plugin phase starts; a rollback or an abort repoints at once. `older` is the record being replaced:
    a recorded file you changed since (it matches neither that record nor the new bytes) is kept and listed for the
    agent's three-way step (contract b), and a launcher the new generation no longer ships is removed only while its
    bytes still match."""
    layout, ports = run.layout, run.ports
    old_bin, old_market = _recorded_files(older, "surface.bin"), _recorded_files(older, "surface.marketplace")
    three_way = []
    shipped = fx.install_launchers(layout, ports, runtime, names, gen_id, apply=False)
    changed_bin = _changed_by_you(layout.bin, old_bin, shipped)
    three_way += [_three_way_row("bin", layout.bin, name, old_bin, shipped, cur) for name, cur in sorted(changed_bin.items())]
    files = fx.install_launchers(layout, ports, runtime, names, gen_id, keep=set(changed_bin))
    stale = []
    for name in sorted(set(old_bin) - set(files)):
        if name not in changed_bin and ir.sha256_file(layout.bin / name) == old_bin[name]:
            if fx._delete_file(layout.bin / name, gen_id) is None:
                stale.append(name)
    market = None
    if kit_dir is not None:
        final = layout.marketplace / "crosstalk"
        planned = fx.render_marketplace(layout, ports, kit_dir, apply=False)
        changed_market = _changed_by_you(final, old_market, planned)
        three_way += [_three_way_row("marketplace", final, name, old_market, planned, cur)
                      for name, cur in sorted(changed_market.items())]
        own = fx.render_marketplace(layout, ports, kit_dir, dest=layout.generation_marketplace(gen_id))
        market = fx.repoint_marketplace(layout, gen_id, keep=set(changed_market)) if repoint else own
    return {"bin": files, "marketplace": market, "three_way": three_way, "stale_removed": stale}


def add_three_way(run: Run, rows: list[dict]) -> None:
    """Add rows to the record's three-way list, once per (block, target)."""
    have = {(item.get("block"), item.get("target")) for item in run.record.get("three_way") or []}
    run.record["three_way"] = list(run.record.get("three_way") or []) + \
        [row for row in rows if (row["block"], row["target"]) not in have]


def _older_record(run: Run):
    """The record an update replaces (the active generation); None for an install."""
    if run.kind != "update" or not run.state.get("active"):
        return None
    return ir.read_record(run.layout, run.state["active"])


def phase_surfaces(run: Run) -> dict:
    layout, info = run.layout, run.info
    runtime = layout.runtimes / info["artifact_id"]
    crosstalk = next((item for item in run.record["kits"] if item["component"] == "crosstalk"), None)
    run.journal({"event": "intent", "what": "launchers", "target": str(layout.bin)})
    if crosstalk:
        run.journal({"event": "intent", "what": "marketplace", "target": str(layout.marketplace / "crosstalk")})
    done = sync_surfaces(run, runtime=runtime, kit_dir=layout.kits / crosstalk["kit_id"] if crosstalk else None,
                         names=info["entry_points"], gen_id=run.record["gen_id"], older=_older_record(run), repoint=False)
    run.sink(fx.bin_effect(layout, done["bin"]), by="installer")
    result = {"bin": sorted(done["bin"]), "stale_removed": done["stale_removed"]}
    if done["marketplace"] is not None:
        run.sink(fx.marketplace_effect(layout, done["marketplace"]), by="installer")
        result["marketplace"] = sorted(done["marketplace"])
    if done["three_way"]:
        add_three_way(run, done["three_way"])
        run.save()
        result["three_way"] = len(done["three_way"])
    return result


def _permission_choices(run: Run) -> dict:
    data = permissions.load()
    chosen = ((run.record.get("consent") or {}).get("choices")) or {}
    return {name: value for name, value in chosen.items()
            if value in ("on", "off") and permissions.entry_by_id(data, name)["writer"] == "installer"}


def hermes_soul_path(run: Run, rows: list[dict]):
    """Where the memory block goes for Hermes: ``<hermes home>/SOUL.md`` when the resolved ``hermes`` component row is
    installed (so a person who declined Hermes, or has none, gets nothing written there), else None. The path is only
    where to look: the memory block is never put into a SOUL.md that does not exist."""
    for row in rows:
        if row["id"] == "hermes" and row["state"] == "installed" and row.get("enabled", True):
            home = component_hermes.hermes_home(run.component_ctx(row["manifest"]))
            return None if home is None else Path(home) / "SOUL.md"
    return None


# ------------------------------------------------------- parts that are switched on or off
# The instruction blocks that belong to a part rather than to the core: they follow it on and off.
# The apps a person has to restart before a part being switched on or off takes effect (they read plugins, hooks
# and tool servers at start-up). Switching it leaves each app a pending action (``pending_actions``). Kept here, not in
# the manifest, because the manifest keys are fixed by contract c.
COMPONENT_RESTARTS = {"crosstalk": ("claude", "codex"), "role-hooks": ("claude", "codex")}
# ``needs_on``: the block is written only while its part is installed and switched on (an optional part that is off
# by default, or absent from the build, leaves no block behind); without it the block is left out only while the part
# is installed and switched off.
COMPONENT_BLOCKS = {"crosstalk": {"blocks": ("crosstalk",), "prefix": "instructions.crosstalk."},
                    "hermes": {"blocks": ("memory",),  # switching Hermes off also takes the Simon block out of SOUL.md
                               "prefix": ("instructions.memory.hermes-soul", "instructions.simon.hermes-soul")},
                    "simon": {"blocks": ("simon",), "prefix": "instructions.simon.", "needs_on": True}}


def _optional_function(manifest, name, resolver=None):
    """A function named `name` beside the component's ``plan`` (like ``status``), or None when it has none."""
    spec = (manifest.get("entry") or {}).get("plan")
    if not spec:
        return None
    try:
        return _entry_function({"entry": {name: str(spec).partition(":")[0] + ":" + name}}, name, resolver)
    except Exception:
        return None


def _inside_home(base, effect) -> bool:
    """Is the Effect's target a path inside the EUTONOS home? (``app:...`` and ``reg:...`` targets are outside.)"""
    target = str(effect.get("target") or "")
    if not target or not os.path.isabs(target):
        return False
    root = os.path.normcase(os.path.abspath(base))
    path = os.path.normcase(os.path.abspath(target))
    return path == root or path.startswith(root + os.sep)


def _blocks_off(rows) -> set:
    """Instruction blocks to leave out because their part is installed but switched off, plus each ``needs_on``
    block whose part is not installed and switched on."""
    by_id = {row["id"]: row for row in rows}
    off = set()
    for component_id, spec in COMPONENT_BLOCKS.items():
        row = by_id.get(component_id)
        installed = row is not None and row["state"] == "installed"
        on = installed and row.get("enabled", True)
        if (spec.get("needs_on") and not on) or (installed and not on):
            off.update(spec["blocks"])
    return off


def _blocks_on(rows) -> tuple:
    """The ``needs_on`` blocks to write: those whose part is installed and switched on."""
    off = _blocks_off(rows)
    return tuple(name for spec in COMPONENT_BLOCKS.values() if spec.get("needs_on")
                 for name in spec["blocks"] if name not in off)


def _write_enabled_state(run: Run, rows) -> None:
    """Write ``settings/components.json`` from the resolved rows. A first install replaces any stale file; an update
    keeps each part's recorded detail while its state is unchanged."""
    existing = {} if run.kind == "install" else component_state.read(run.base)
    entries = {}
    for row in rows:
        if row["state"] != "installed":
            continue
        prior = existing.get(row["id"])
        same = prior is not None and prior["enabled"] == bool(row["enabled"])
        entries[row["id"]] = {"enabled": bool(row["enabled"]), "changed_at": prior["changed_at"] if same else None,
                              "source": prior["source"] if same else "install", "detail": prior["detail"] if same else {}}
    component_state.write_all(run.base, entries, now=run.now)


def apply_component(run: Run, manifest, *, ctx_extra=None) -> list[dict]:
    """Plan and apply one part's Effects through its own entry functions and record each one (write-ahead). The one
    path an install, an update and ``component enable`` all use."""
    ctx = dict(run.component_ctx(manifest), effect_sink=lambda item: run.sink(item, by="component"), **(ctx_extra or {}))
    plan = _entry_function(manifest, "plan", run.resolver)
    apply_ = _entry_function(manifest, "apply", run.resolver)
    ts.require(plan and apply_, "invalid", f"Component {manifest['id']} has no plan or apply entry.")
    run.journal({"event": "intent", "what": "component", "component": manifest["id"]})
    planned = list(plan(ctx) or [])
    try:
        applied = list(apply_(ctx, planned) or [])
    except Exception as exc:
        for item in getattr(exc, "effects", None) or []:
            run.sink(dict(item, component=item.get("component") or manifest["id"]), by="component")
        raise
    for item in applied:
        run.sink(dict(item, component=item.get("component") or manifest["id"]), by="component")
    return applied


def apply_component_blocks(run: Run, component_id: str) -> list[dict]:
    """Put back the instruction block that belongs to a part that is being switched on."""
    spec = COMPONENT_BLOCKS.get(component_id)
    if not spec:
        return []
    previous = {item["effect_id"]: item for item in run.record["effects"] if item["effect_id"].startswith("instructions.")}
    soul = None
    if component_id == "hermes":
        soul = hermes_soul_path(run, [{"id": "hermes", "state": "installed", "enabled": True,
                                       "manifest": run.component("hermes")}])
    elif component_id == "simon":  # Hermes' SOUL.md gets the Simon block only while the Hermes part is on too
        hermes = _component_rows(run.record).get("hermes")
        if hermes is not None and run.component("hermes") is not None:
            try:
                on = _is_enabled(hermes, component_state.read(run.base))
            except component_state.ComponentStateError:
                on = False
            soul = hermes_soul_path(run, [dict(hermes, enabled=on, manifest=run.component("hermes"))])
    run.journal({"event": "intent", "what": "instruction-blocks", "component": component_id})
    effects, three_way, notes = fx.instruction_effects(
        run.layout, run.where["home"], run.record["gen_id"], previous=previous, codex_home=run.where["codex"],
        claude_home=run.where["claude"], hermes_soul=soul, only_blocks=spec["blocks"],
        sink=lambda e: run.journal({"event": "effect", "effect": e}))
    for item in effects:
        run.sink(item, by="installer")
    add_three_way(run, three_way)
    run.notes += notes
    return effects


def disable_component(run: Run, manifest, detail=None) -> dict:
    """Undo a part's Effects that live outside the EUTONOS home (Startup items, scheduled tasks, permission entries,
    the Hermes folder, its instruction block) through the same undo code uninstall uses, and take the undone ones out
    of the record. A part's own ``disable(ctx, effects)`` may decide differently (Crosstalk keeps its Claude plugin
    registered and switches it off). Returns ``{removed, kept, detail, notes}``; what could not be undone stays recorded."""
    component_id = manifest["id"]
    effects = [item for item in run.record["effects"] if item.get("by") == "component" and item.get("component") == component_id]
    ctx = dict(run.component_ctx(manifest), toggle_detail=dict(detail or {}))
    hook = _optional_function(manifest, "disable", run.resolver)
    run.journal({"event": "intent", "what": "disable-component", "component": component_id})
    if hook is not None:
        answer = hook(ctx, effects)
    else:
        outside = [item for item in effects if not _inside_home(run.base, item)]
        remove = _entry_function(manifest, "remove", run.resolver)
        answer = remove(ctx, outside) if (remove and outside) else {"removed": [], "kept": []}
    removed, kept = list(answer.get("removed") or []), list(answer.get("kept") or [])
    spec = COMPONENT_BLOCKS.get(component_id)
    if spec:
        remover = fx.Remover(run.layout, run.ports, run.user_home, run.os_name, "disable")
        for item in reversed([e for e in run.record["effects"] if e["effect_id"].startswith(spec["prefix"])]):
            try:
                state, reason = remover.one(item)
            except Exception as exc:
                state, reason = "kept", f"{type(exc).__name__}: {exc}"
            if state == "kept":
                kept.append({"effect_id": item["effect_id"], "reason": reason})
            else:
                removed.append(item["effect_id"])
    if removed:
        run.withdraw(removed)
    return {"removed": removed, "kept": kept, "detail": dict(answer.get("detail") or {}),
            "notes": list(answer.get("notes") or [])}


def enable_component(run: Run, manifest, detail=None) -> dict:
    """Apply a part's Effects again through the code install uses (after its own ``enable(ctx)`` has undone any
    plugin or messaging switch it made on the way off) and put back its instruction block."""
    ctx_extra = {"toggle_detail": dict(detail or {})}
    hook = _optional_function(manifest, "enable", run.resolver)
    if hook is not None:
        hook(dict(run.component_ctx(manifest), **ctx_extra))
    applied = apply_component(run, manifest, ctx_extra=ctx_extra)
    blocks = apply_component_blocks(run, manifest["id"])
    answer = {"applied": [item["effect_id"] for item in applied], "blocks": [item["effect_id"] for item in blocks]}
    if manifest["id"] == "crosstalk":  # a part that was off at install never had its plugin registered
        answer["plugin"] = register_plugin(run, update=False, honour_off=False)
    return answer


def phase_effects(run: Run) -> dict:
    layout, ports, params, where = run.layout, run.ports, run.params, run.where
    gen_id = run.record["gen_id"]
    sub: dict = {}
    previous = {item["effect_id"]: item for item in run.record["effects"] if item["effect_id"].startswith("instructions.")}
    # 1. adopt: repoint what an earlier hand install left, once
    if params.get("adopt"):
        legacy = run.state.get("legacy") or {}
        if not legacy.get("migrated"):
            run.journal({"event": "intent", "what": "migrate-legacy"})
            for item in fx.migrate_windows(layout, ports, where["home"], gen_id, sink=lambda e: run.sink(e, by="installer")):
                run.sink(item, by="installer")
            legacy["migrated"] = True
            run.state["legacy"] = legacy
            run.save()
            sub["migration"] = "done"
        else:
            sub["migration"] = "already done"
        for item in fx.adopted_permission_effects(layout, where["home"], run.os_name, _permission_choices(run)):
            run.sink(item, by="installer")
    # 2. components
    rows = run.resolve()
    record_rows = []
    sub["components"] = []
    for row in rows:
        record_rows.append({"id": row["id"], "version": row["version"], "state": row["state"], "reason": row["reason"],
                            "enabled": bool(row.get("enabled"))})
        if row["state"] != "installed":
            sub["components"].append({"id": row["id"], "state": row["state"], "reason": row["reason"]})
            continue
        if not row["enabled"]:  # installed (its kit is in the home) but switched off: nothing outside the home is applied
            sub["components"].append({"id": row["id"], "state": "installed", "enabled": False, "effects": 0})
            continue
        applied = apply_component(run, row["manifest"])
        sub["components"].append({"id": row["id"], "state": "installed", "effects": len(applied)})
    run.record["components"] = record_rows
    run.save()
    _write_enabled_state(run, rows)
    hermes_soul = hermes_soul_path(run, rows)
    # 3. the permissions list (machine entries only; a part that is switched off writes none of its entries)
    data = permissions.load()
    switched_off = {row["id"] for row in rows if row["state"] == "installed" and not row.get("enabled")}
    entries = [name for name in permissions.machine_entry_ids(data)
               if permissions.entry_by_id(data, name)["component"] not in switched_off]

    def observer(kind, info):
        if kind == "intent":
            run.journal({"event": "intent", "what": "permission", **info})
        else:
            run.sink(info, by="permissions")

    try:
        written = permissions.apply(None, where["home"], choices=_permission_choices(run), os_name=run.os_name, gen_id=gen_id, data=data,
                                    eutonos_dir=run.base, only=entries, observer=observer)
    except permissions.PermissionsApplyError as exc:
        for item in exc.effects:
            run.sink(item, by="permissions")
        raise
    sub["permissions"] = {"written": [item["effect_id"] for item in written]}
    # 4. instruction blocks
    run.journal({"event": "intent", "what": "instruction-blocks"})
    effects, three_way, notes = fx.instruction_effects(
        layout, where["home"], gen_id, previous=previous, adopt=bool(params.get("adopt")),
        codex_home=where["codex"], claude_home=where["claude"], hermes_soul=hermes_soul,
        skip_blocks=_blocks_off(rows), with_blocks=_blocks_on(rows),
        sink=lambda e: run.journal({"event": "effect", "effect": e}))
    for item in effects:
        run.sink(item, by="installer")
    add_three_way(run, three_way)
    run.notes += notes
    sub["instructions"] = {"blocks": [item["effect_id"] for item in effects], "three_way": len(three_way)}
    # 5. PATH
    run.journal({"event": "intent", "what": "path", "target": str(layout.bin)})
    item, status = fx.path_effect(layout, ports, where["home"], gen_id, adopt=bool(params.get("adopt")),
                                  sink=lambda e: run.journal({"event": "effect", "effect": e}))
    if item is not None:
        run.sink(item, by="installer")
    sub["path"] = status
    # 5b. EUTONOS_HOME: a real install of a home that is not ~/.eutonos lets code outside the runtime find it
    if run.for_this_user and not eutonos_home.is_default(run.base):
        run.journal({"event": "intent", "what": "env-home", "target": str(layout.base)})
        item, status = fx.env_home_effect(layout, ports, gen_id, sink=lambda e: run.journal({"event": "effect", "effect": e}))
        if item is not None:
            run.sink(item, by="installer")
        if status == "conflict":
            run.notes.append("The user environment variable EUTONOS_HOME already names another folder, so it was "
                             "left alone. Tools outside EUTONOS find this home only through that variable.")
        sub["env-home"] = status
    # 6. owner profile
    if params.get("owner_profile"):
        run.journal({"event": "intent", "what": "owner-profile"})
        item = fx.profile_effect(layout, gen_id, Path(params["owner_profile"]),
                                 sink=lambda e: run.journal({"event": "effect", "effect": e}))
        if item is not None:
            run.sink(item, by="installer")
        sub["owner-profile"] = "copied" if item is not None else "unchanged"
    else:
        sub["owner-profile"] = "not supplied"
    run.record["projects"] = run.record.get("projects") or []
    run.save()
    run.substeps = sub
    return sub


def _journalled_effects(events) -> list[dict]:
    """The Effects a journal wrote ahead, minus those a later ``effect-withdrawn`` event took back."""
    found: dict = {}
    for event in events:
        if event.get("event") == "effect":
            found[event["effect"]["effect_id"]] = event["effect"]
        elif event.get("event") == "effect-withdrawn":
            for effect_id in event.get("effect_ids") or []:
                found.pop(effect_id, None)
    return list(found.values())


def _recover_effects(record, events) -> None:
    # Withdrawal is also write-ahead. A stop before the record save must not put
    # a successfully reversed external action back on the retry list.
    for event in events:
        if event.get("event") == "effect-withdrawn":
            ids = set(event.get("effect_ids") or [])
            record["effects"] = [item for item in record["effects"] if item["effect_id"] not in ids]
    ir.merge_effects(record, _journalled_effects(events))


def _known_marketplace(run: Run):
    registry_path = run.where["claude"] / "plugins" / "known_marketplaces.json"
    try:
        return (json.loads(registry_path.read_text(encoding="utf-8")) or {}).get(fx.MARKETPLACE_NAME)
    except (OSError, ValueError, AttributeError):
        return None


def _source_path(known) -> str:
    """Where a registered marketplace points: Claude writes ``{"source": {"source": "directory", "path": ...}}``; a
    component or an older Claude may write the path as a plain string. Anything else is "unknown" (empty)."""
    source = (known or {}).get("source") if isinstance(known, dict) else None
    if isinstance(source, str):
        return source
    return str(source.get("path", "")) if isinstance(source, dict) else ""


def _plugin_update(run: Run) -> dict:
    """``claude plugin update`` when our marketplace is registered here; it never registers anything."""
    claude = run.ports.which("claude")
    market = run.layout.marketplace / "crosstalk"
    known = _known_marketplace(run)
    if not claude or known is None:
        return {"plugin": "skipped", "reason": "claude or the registered marketplace was not found"}
    source = _source_path(known)
    if source and not fx._same_entry(source, str(market)):
        return {"plugin": "skipped", "reason": "a marketplace of that name is registered elsewhere"}
    code, out, err = run.ports.run([claude, "plugin", "update", fx.PLUGIN_ID])
    return {"plugin": "updated" if code == 0 else "skipped", "reason": None if code == 0 else (err or out)[-160:]}


PLUGIN_SOURCE = "setup:crosstalk-claude-plugin"


def _pending_plugin(run: Run, add: bool) -> None:
    """A Claude folder without the ``claude`` command cannot register the Crosstalk plugin: leave one step saying so
    (`add`), or clear it once the command is found. The install does not fail either way; a store that cannot be
    written only adds a note."""
    try:
        pending_actions.dismiss_source(run.base, PLUGIN_SOURCE, note="superseded by a later install", now=run.now)
        if add:
            pending_actions.add(
                run.base, source=PLUGIN_SOURCE, harness="any", now=run.now,
                action="The Crosstalk Claude plugin is not registered: the claude command was not found. Tell the person to "
                       "install Claude Code's command line, then run: claude plugin marketplace add "
                       f"{run.layout.marketplace / 'crosstalk'}  and  claude plugin install {fx.PLUGIN_ID}. Mark this done after.",
                why="EUTONOS found a Claude folder but not the claude command, and the plugin is registered through it.")
    except (pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        run.notes.append(f"The follow-up step for the Claude plugin could not be saved ({exc}). Tell the person the plugin "
                         "needs the claude command to be registered.")


def phase_plugin(run: Run) -> dict:
    return register_plugin(run)


def register_plugin(run: Run, *, update: bool = True, honour_off: bool = True) -> dict:
    """The one writer of the Crosstalk Claude plugin registration (``claude plugin marketplace add`` and ``claude
    plugin install``), each recorded as an ``app-command`` Effect with its undo. The install's plugin phase calls it,
    and so does ``component enable`` when the Crosstalk part is switched on later (``honour_off=False``, ``update=False``).
    The Crosstalk part itself only reads the registration back and switches the plugin off and on."""
    ports, layout, where = run.ports, run.layout, run.where
    market = layout.marketplace / "crosstalk"
    claude = ports.which("claude")
    if honour_off and any(row["id"] == "crosstalk" and row["state"] == "installed" and not row["enabled"]
                          for row in run.resolve()):
        return {"plugin": "skipped", "reason": "Crosstalk is turned off, so its Claude plugin was not registered"}
    if not (market / ".claude-plugin" / "marketplace.json").is_file():
        return {"plugin": "skipped", "reason": "no marketplace was rendered"}
    if not claude:
        _pending_plugin(run, add=True)
        return {"plugin": "skipped", "reason": "claude was not found on PATH"}
    _pending_plugin(run, add=False)
    known = _known_marketplace(run)
    plugin_id = fx.PLUGIN_ID
    if known is None:
        steps = (("plugin.marketplace", [claude, "plugin", "marketplace", "add", str(market)],
                  [claude, "plugin", "marketplace", "remove", fx.MARKETPLACE_NAME]),
                 ("plugin.install", [claude, "plugin", "install", plugin_id], [claude, "plugin", "uninstall", plugin_id]))
        done = []
        for effect_id, argv, undo in steps:
            run.journal({"event": "intent", "what": "app-command", "argv": argv})
            code, out, err = ports.run(argv)
            if code != 0:
                return {"plugin": "skipped", "reason": f"{' '.join(argv[1:4])} failed: {(err or out)[-160:]}", "done": done}
            run.sink(fx.effect(effect_id, "crosstalk", "app-command", " ".join(argv[1:]), after=None,
                               shipped=fx.sha256_bytes(" ".join(argv).encode("utf-8")),
                               undo={"op": "app-command", "argv": undo}), by="installer")
            done.append(effect_id)
        return {"plugin": "registered", "done": done}
    source = _source_path(known)
    if source and not fx._same_entry(source, str(market)):
        if run.params.get("adopt"):
            return _repoint_marketplace(run, claude, market, source)
        run.notes.append(f"The Claude marketplace {fx.MARKETPLACE_NAME} points at {source}, not {market}; "
                         "install with --adopt to repoint it before relying on the plugin.")
        return {"plugin": "skipped", "reason": "a marketplace of that name is registered elsewhere"}
    if not update:
        return {"plugin": "present"}
    code, out, err = ports.run([claude, "plugin", "update", plugin_id])
    return {"plugin": "updated" if code == 0 else "skipped", "reason": None if code == 0 else (err or out)[-160:]}


def _repoint_marketplace(run: Run, claude: str, market: Path, old_source: str) -> dict:
    """Adopt mode: the marketplace name is registered at an earlier hand install. Remove it, record that as a
    ``migrate.marketplace`` Effect whose undo adds the old source back and reinstalls the plugin, then register ours.
    If anything after the remove fails, put the old marketplace and plugin back at once, so Claude keeps its plugin."""
    ports, plugin_id, name = run.ports, fx.PLUGIN_ID, fx.MARKETPLACE_NAME
    remove = [claude, "plugin", "marketplace", "remove", name]
    run.journal({"event": "intent", "what": "app-command", "argv": remove})
    code, out, err = ports.run(remove)
    if code != 0:
        return {"plugin": "skipped", "reason": f"could not remove the old marketplace registration {name} "
                                               f"({old_source}); nothing was changed: {(err or out)[-160:]}"}
    restore = [[claude, "plugin", "marketplace", "add", old_source], [claude, "plugin", "install", plugin_id]]
    recorded = [run.sink(fx.effect("migrate.marketplace", "crosstalk", "app-command", " ".join(remove[1:]), after=None,
                                   shipped=fx.sha256_bytes(" ".join(remove).encode("utf-8")),
                                   undo={"op": "app-command", "argvs": restore}, adopted=True, repointed=True), by="installer")]
    steps = (("plugin.marketplace", [claude, "plugin", "marketplace", "add", str(market)],
              [claude, "plugin", "marketplace", "remove", name]),
             ("plugin.install", [claude, "plugin", "install", plugin_id], [claude, "plugin", "uninstall", plugin_id]))
    failure = None
    for effect_id, argv, undo in steps:
        run.journal({"event": "intent", "what": "app-command", "argv": argv})
        code, out, err = ports.run(argv)
        if code != 0:
            failure = f"{' '.join(argv[1:4])} failed: {(err or out)[-160:]}"
            break
        recorded.append(run.sink(fx.effect(effect_id, "crosstalk", "app-command", " ".join(argv[1:]), after=None,
                                           shipped=fx.sha256_bytes(" ".join(argv).encode("utf-8")),
                                           undo={"op": "app-command", "argv": undo}), by="installer"))
    if failure is None:
        run.notes.append(f"The Claude marketplace {name} was repointed from {old_source} to {market}.")
        return {"plugin": "registered", "done": [item["effect_id"] for item in recorded], "repointed_from": old_source}
    remover = fx.Remover(run.layout, ports, run.user_home, run.os_name, "install")
    undone, stuck = [], []
    for item in reversed(recorded):  # newest first, ending with the old marketplace and plugin
        try:
            state, reason = remover.one(item)
        except Exception as exc:
            state, reason = "kept", f"{type(exc).__name__}: {exc}"
        if state == "kept":
            stuck.append(f"{item['effect_id']}: {reason}")
        else:
            undone.append(item["effect_id"])
    if undone:
        run.withdraw(undone)
    if stuck:
        run.notes.append(f"Restoring the earlier Claude marketplace {name} did not finish ({'; '.join(stuck)}); "
                         "uninstall retries it from the record.")
        return {"plugin": "skipped", "reason": f"{failure}; restoring the earlier marketplace did not finish: {'; '.join(stuck)}"}
    return {"plugin": "skipped", "reason": f"{failure}; the earlier marketplace and plugin were put back"}


def _private_ids(record) -> set:
    return {item["effect_id"] for item in record.get("effects", []) if item["effect_id"].startswith(PRIVATE_PREFIXES)}


def prune_old(run: Run, keep: set) -> list[str]:
    """Remove the runtimes and kits of generations older than the two retained ones."""
    kept_private: set = set()
    for gen in keep:
        try:
            kept_private |= _private_ids(ir.read_record(run.layout, gen))
        except ts.ReadError:
            pass
    pruned = []
    remover = fx.Remover(run.layout, run.ports, run.user_home, run.os_name, "prune")
    for gen in list(run.state["generations"]):
        if gen in keep:
            continue
        try:
            old = ir.read_record(run.layout, gen)
        except ts.ReadError:
            continue
        if old["state"] == "prepared":
            continue
        fx.remove_generation_marketplace(run.layout, gen)
        for item in old.get("effects", []):
            if item["effect_id"].startswith(PRIVATE_PREFIXES) and item["effect_id"] not in kept_private:
                state, _ = remover.one(item)
                if state in ("removed", "gone"):
                    pruned.append(item["effect_id"])
    return pruned


def phase_activate(run: Run) -> dict:
    layout, ports, record = run.layout, run.ports, run.record
    runtime_effect = next(item for item in record["effects"] if item["effect_id"] == f"runtime.{run.info['artifact_id']}")
    ts.require(fx.runtime_package_digest(ports.site_packages(layout.runtimes / run.info["artifact_id"]))
               == runtime_effect["after_sha256"], "runtime-mismatch", "The runtime changed before activation.")
    for item in record["kits"]:
        ts.require(kits_module.verify(layout.kits / item["kit_id"]), "kit-mismatch", f"Kit {item['kit_id']} changed.")
    _repoint_own_marketplace(run)  # the registered marketplace folder holds this generation's plugin
    previous = run.state.get("active")
    if previous and previous != record["gen_id"]:
        before = ir.read_record(layout, previous)
        before["state"] = "retired"
        ir.write_record(layout, run.state, before)
    record["state"] = "active"
    # Activation runs in the chosen engine after handover; the caller that made
    # the pending record can be an older engine without this recovery contract.
    record["recovery_contract"] = 1
    record["previous_gen"] = previous if previous != record["gen_id"] else record.get("previous_gen")
    if run.params.get("channel"):
        run.state["channel"] = run.params["channel"]
    run.state["active"] = record["gen_id"]
    run.state["pending"] = None
    keep = {record["gen_id"]} | ({record["previous_gen"]} if record.get("previous_gen") else set())
    pruned = prune_old(run, keep)
    if pruned:
        record["pruned"] = list(record.get("pruned") or []) + pruned
    run.save()
    return {"active": record["gen_id"], "previous": record.get("previous_gen"), "pruned": pruned}


def _repoint_own_marketplace(run: Run) -> None:
    """Copy this generation's rendered marketplace to the folder Claude has registered (idempotent). A file you changed
    (a three-way row for the marketplace) is left alone."""
    layout = run.layout
    final = layout.marketplace / "crosstalk"
    keep = set()
    for row in run.record.get("three_way") or []:
        if row.get("block") == "marketplace":
            with contextlib.suppress(ValueError):
                keep.add(Path(row["target"]).relative_to(final).as_posix())
    fx.repoint_marketplace(layout, run.record["gen_id"], keep=keep)


def migrate_components(run: Run) -> list[dict]:
    """On an update, call each installed and enabled component's optional ``migrate(ctx, from_version, to_version)``
    (a function named ``migrate`` beside its ``plan``, like ``status``, ``enable`` and ``disable``). The versions are
    EUTONOS's: the one the update starts from and the one it installs. A component that has no ``migrate`` is skipped.
    The hook runs after the component's effects are applied, with the new generation's kit and runtime in place, and
    must be safe to run twice (a resumed update runs it again until the phase completes). A hook that raises fails the
    phase, so the update stops and the earlier generation stays active. Returns what ran."""
    start, end = run.params.get("from_version"), (run.record or {}).get("eutonos_version")
    if run.kind != "update" or not start or not end:
        return []
    done = run.record.setdefault("migrations", [])
    ran = []
    for row in run.resolve():
        if row["state"] != "installed" or not row.get("enabled"):
            continue
        hook = _optional_function(row["manifest"], "migrate", run.resolver)
        if hook is None or any(item["component"] == row["id"] and item["from"] == start and item["to"] == end for item in done):
            continue
        run.journal({"event": "intent", "what": "migrate-component", "component": row["id"], "from": start, "to": end})
        answer = hook(dict(run.component_ctx(row["manifest"])), start, end)
        notes = list((answer or {}).get("notes") or []) if isinstance(answer, dict) else []
        run.notes += [str(item) for item in notes]
        done.append({"component": row["id"], "from": start, "to": end})
        ran.append({"component": row["id"], "from": start, "to": end})
        run.save()
    return ran


def phase_effects_migrated(run: Run) -> dict:
    """The effects phase, then the components' ``migrate`` hooks (update only)."""
    payload = phase_effects(run)
    ran = migrate_components(run)
    return {**(payload or {}), "migrated": [item["component"] for item in ran]} if ran else payload


def phase_plugin_repointed(run: Run) -> dict:
    """The plugin phase, after the registered marketplace folder is pointed at this generation's own copy."""
    _repoint_own_marketplace(run)
    return phase_plugin(run)


PHASE_FUNCTIONS = {"runtime": phase_runtime, "kits": phase_kits, "surfaces": phase_surfaces,
                   "effects": phase_effects_migrated,
                   "plugin": phase_plugin_repointed, "activate": phase_activate}


# --------------------------------------------------------------------- execution
def execute(run: Run, *, resuming=False, stop_after=None) -> dict:
    """Run the phases that have no receipt yet. A first install that fails is reversed; anything else stays pending."""
    done = ir.done_phases(run.layout, run.record["gen_id"])
    for phase in PHASES:
        if phase in done:
            continue
        run.set_phase(phase)
        if PROGRESS is not None:
            PROGRESS(phase, PHASES.index(phase) + 1, len(PHASES))
        try:
            payload = PHASE_FUNCTIONS[phase](run)
            ir.write_receipt(run.layout, run.record["gen_id"], phase, {"txn_id": run.txn, **(payload or {})}, run.now)
            run.journal({"event": "phase", "phase": phase, "state": "done"})
        except Exception as exc:
            return _failed(run, phase, exc, resuming)
        if stop_after == phase:
            return {"outcome": "stopped", "phase": phase}
    return finish(run)


def _failed(run: Run, phase: str, exc: Exception, resuming: bool) -> dict:
    error = f"{type(exc).__name__}: {exc}"
    run.journal({"event": "failed", "phase": phase, "error": error})
    if run.kind == "install" and not resuming:
        return abort_install(run, phase, error)
    if run.kind == "update" and not resuming:
        return abort_update(run, phase, error)
    run.state["pending"]["error"] = {"phase": phase, "error": error}
    run.save()
    return {"outcome": "failed", "phase": phase, "error": error, "gen_id": run.record["gen_id"],
            "next_action": "Fix the cause, then run: eutonos-install resume"}


def abort_install(run: Run, phase: str, error: str) -> dict:
    """First-install failure: reverse this run's own Effects newest first and mark the record rolled-back."""
    run.journal({"event": "cleanup-start", "action": "uninstall"})
    result = reverse_effects(run, list(run.record["effects"]), tag="abort")
    if result["failed"]:
        run.withdraw(result["removed"])
        run.state["pending"]["recovery_action"] = "uninstall"
        run.record["recovery_action"] = "uninstall"
        run.state["pending"]["error"] = {"phase": phase, "error": error, "failed": result["failed"]}
        run.save()
        return {"outcome": "failed", "phase": phase, "error": error, **result,
                "gen_id": run.record["gen_id"], "next_action": "Cleanup is incomplete. Fix the reported failure, then run eutonos-install uninstall."}
    component_state.remove_file(run.base)
    run.record["state"] = "rolled-back"
    run.record["abort"] = {"phase": phase, "error": error, **result}
    run.state["pending"] = None
    run.save()
    ir.write_atomic(run.layout.receipts(run.record["gen_id"]) / "95-abort.json",
                    ir.dump({"phase": phase, "error": error, **result, "completed_at": ir.now_iso(run.now)}))
    return {"outcome": "rolled-back", "phase": phase, "error": error, "gen_id": run.record["gen_id"],
            "removed": result["removed"], "kept": result["kept"],
            "next_action": "Nothing is installed. Fix the cause and run the install again."}


def _install_steps(run: Run, receipts: dict) -> list[dict]:
    steps = []
    consent = run.record.get("consent") or {}
    for step_id, kind, runner in INSTALL_STEPS:
        evidence, state, reason = [], "done", None
        if step_id == "prereq":
            evidence = ["prereq check ready"]
            if run.kind == "update":
                state, reason = "skipped", "an update does not repeat the prerequisite check"
        elif step_id == "consent":
            evidence = [f"{consent.get('text_id')} sha256 {str(consent.get('text_sha256'))[:12]} "
                        f"accepted {consent.get('accepted_at')}"]
            if run.kind == "update":
                state, reason = "skipped", "the first install's consent still stands"
        elif step_id in ("runtime", "kits", "surfaces", "plugin", "activate"):
            evidence = [receipts.get(step_id, "receipt written")]
            if step_id == "plugin" and receipts.get("plugin-note"):
                state, reason = "skipped", receipts["plugin-note"]
        else:
            sub = run.substeps.get(step_id if step_id != "owner-profile" else "owner-profile")
            evidence = [json.dumps(sub, sort_keys=True, default=str)[:300] if sub is not None else "effects receipt"]
            if step_id == "owner-profile" and sub == "not supplied":
                state, reason = "skipped", "no owner profile was supplied"
        steps.append({"id": step_id, "kind": kind, "runner": runner, "state": state, "evidence": evidence,
                      "reason": reason})
    for step_id, kind, runner, action in INSTALL_OPEN:
        if step_id == "mac-grants" and run.os_name != "macos":
            steps.append({"id": step_id, "kind": kind, "runner": runner, "state": "skipped",
                          "reason": "this is not a Mac", "evidence": []})
        else:
            steps.append({"id": step_id, "kind": kind, "runner": runner, "state": "pending", "evidence": [],
                          "next_action": action})
    if run.record.get("three_way"):
        steps.append({"id": "three-way", "kind": "agent", "runner": "agent", "state": "pending", "evidence": [],
                      "next_action": "For each row in the record's three_way list, merge the old shipped text, the new "
                                     "shipped text and the current text, then record the result here."})
    return steps


def run_doctor(function=None, *, home=None, user_home=None) -> dict:
    """Run doctor when it is present (a lazy import); otherwise say it is not built yet. Doctor only reads, and it is
    pointed at this install's own home (and user home) rather than the process default."""
    if function is None:
        try:
            function = importlib.import_module(".doctor", __package__).main
        except ImportError as exc:
            if getattr(exc, "name", None) not in ("eutonos.doctor", __package__ + ".doctor"):
                return {"ran": False, "reason": f"doctor could not be imported: {exc}"}
            return {"ran": False, "reason": "doctor not built yet"}
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = function((["--home", str(home)] if home else []) + (["--user-home", str(user_home)] if user_home else []))
    except SystemExit as stop:
        code = stop.code
    except Exception as exc:
        return {"ran": True, "exit": None, "error": f"{type(exc).__name__}: {exc}", "output": buffer.getvalue()[-2000:]}
    return {"ran": True, "exit": code, "output": buffer.getvalue()[-2000:]}


def finish(run: Run) -> dict:
    layout = run.layout
    receipts = {}
    for phase in PHASES:
        path = layout.receipts(run.record["gen_id"]) / f"{PHASES.index(phase) + 1:02d}-{phase}.json"
        receipts[phase] = str(path)
        if phase == "plugin":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("plugin") == "skipped":
                    receipts["plugin-note"] = str(data.get("reason"))
            except (OSError, ValueError):
                pass
    if not run.substeps:  # a resumed run: read what the effects phase recorded
        try:
            run.substeps = json.loads((layout.receipts(run.record["gen_id"]) / "04-effects.json")
                                      .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run.substeps = {}
    if run.kind == "update" and run.doctor is None:
        run.doctor = run_doctor(run.doctor_fn, home=run.base, user_home=run.user_home)
    steps = _install_steps(run, receipts)
    checkpoint = sc.start("install", steps, checklist_revision=CHECKLIST_REVISION, gen_id=run.record["gen_id"],
                          home=run.base, now=run.now)
    open_steps = [item["id"] for item in checkpoint["steps"] if item["state"] not in ("done", "skipped")]
    local = run.record.get("local_patch")
    result = {"outcome": "installed" if run.kind == "install" else "patched" if isinstance(local, dict) else "updated",
              "gen_id": run.record["gen_id"],
              "artifact_id": run.record["wheel"]["artifact_id"], "version": run.record["eutonos_version"],
              "effects": len(run.record["effects"]), "components": run.record["components"],
              "three_way": run.record.get("three_way") or [], "notes": run.notes, "open_steps": open_steps,
              "doctor": run.doctor,
              "checkpoint": str(sc.checkpoint_path(run.base)),
              "next_action": "Do the open steps in the checkpoint: " + ", ".join(open_steps) if open_steps else None}
    if isinstance(local, dict):
        result["local_patch"] = local
        result["status_line"] = sr.patch_line(local)
    if isinstance(run.record.get("replaced_local_patch"), dict):
        result["replaced_local_patch"] = run.record["replaced_local_patch"]
    return result


# ----------------------------------------------------------------------- install
def _prepare_wheel(layout, wheel) -> Path:
    """Keep a copy of the wheel under ``H/install/wheels`` so a resume or rollback never depends on the download."""
    source = Path(wheel)
    ts.require(source.is_file(), "missing", f"The wheel does not exist: {source}")
    folder = layout.install / "wheels"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if not target.exists() or ir.sha256_file(target) != ir.sha256_file(source):
        shutil.copyfile(source, target)
    return target


def plan_install(run: Run, info: dict, *, adopt, owner_profile, consent_note, wheel=None, component_choices=None) -> dict:
    """Every write an install would make. Writes nothing and takes no lock. With a `wheel`, the components plan from a
    scratch copy of its kits (their ``plan`` may import a kit module), deleted before this returns. `component_choices`
    (``--component ID=on|off``) selects the components exactly as a real install would."""
    with tempfile.TemporaryDirectory(prefix="eutonos-plan-") as scratch:
        if wheel is not None:
            run.kit_override = {name: fx.extract_kit(wheel, name, Path(scratch) / name) for name in info["kits"]}
        try:
            return _plan_install(run, info, adopt=adopt, owner_profile=owner_profile, consent_note=consent_note,
                                 component_choices=component_choices)
        finally:
            run.kit_override = {}


def setup_parts(manifests, choices=None) -> list[dict]:
    """The parts the Setup wizard offers: core (required) and each bundled part, with its on or off state. Held parts
    (role hooks) and add-ons (the test stand-in) are not offered."""
    choices = choices or {}
    rows = []
    for manifest in manifests:
        if manifest.get("kind") not in ("core", "bundled") or manifest.get("default") == "held":
            continue
        state = "required" if manifest.get("kind") == "core" else (
            choices[manifest["id"]] if choices.get(manifest["id"]) in ("on", "off") else manifest.get("default", "on"))
        rows.append({"id": manifest["id"], "name": manifest.get("name") or manifest["id"], "state": state})
    return rows


def setup_base(run: Run, choices, adopt) -> dict:
    """The part of the Setup consent change list this engine decides itself (see ``consent.from_file``)."""
    return {"location": str(run.base), "parts": setup_parts(run.components(), choices),
            "for_this_user": run.for_this_user, "default_home": eutonos_home.is_default(run.base), "adopt": bool(adopt)}


def _plan_install(run: Run, info: dict, *, adopt, owner_profile, consent_note, component_choices=None) -> dict:
    layout, ports, where = run.layout, run.ports, run.where
    rows = []

    def add(phase, action, target=None, **extra):
        rows.append({"phase": phase, "action": action, "target": None if target is None else str(target), **extra})

    run.info = info
    run.record = {"gen_id": "dry-run", "kits": [{"component": name, "kit_id": f"{name}-planned"} for name in info["kits"]],
                  "consent": {"choices": {}}, "effects": []}
    run.params = {"owner_profile": owner_profile, "user_home": run.params.get("user_home"),
                  "component_choices": component_choices or {}}
    add("consent", consent_note)
    add("runtime", "create a venv and pip install the wheel", layout.runtimes / info["artifact_id"])
    for name in info["kits"]:
        add("kits", "copy the bundled kit", layout.kits, kit=name)
    for name in info["entry_points"]:
        add("surfaces", "copy launcher", layout.bin / ports.launcher_name(name))
    if "crosstalk" in info["kits"]:
        add("surfaces", "render the Claude marketplace and QUICKSTART", layout.marketplace / "crosstalk")
    if adopt:
        for key, value in fx.inspect_legacy(layout, ports, where["home"], run.os_name).items():
            if value:
                add("effects", "adopt and repoint the earlier install item", None, item=key, found=str(value))
    resolved = run.resolve()
    for row in resolved:
        if row["state"] != "installed":
            add("effects", f"component {row['id']} {row['state']}", None, reason=row["reason"])
            continue
        if not row["enabled"]:
            add("effects", f"component {row['id']} installed and left switched off", None, reason=row["reason"])
            continue
        manifest = row["manifest"]
        plan = _entry_function(manifest, "plan", run.resolver)
        for item in (plan(run.component_ctx(manifest, dry_run=True)) if plan else []):
            add("effects", f"component {row['id']}: {item.get('kind')}", item.get("target"))
    data = permissions.load()
    switched_off = {row["id"] for row in resolved if row["state"] == "installed" and not row["enabled"]}
    plan = permissions.plan(None, where["home"], os_name=run.os_name, data=data, eutonos_dir=layout.base,
                            only=[name for name in permissions.machine_entry_ids(data)
                                  if permissions.entry_by_id(data, name)["component"] not in switched_off])
    for item in plan["files"]:
        for addition in item["additions"]:
            add("effects", f"permission entry {addition['entry']}", item["file"])
    hermes_soul = hermes_soul_path(run, resolved)
    blocks_off = _blocks_off(resolved)
    for name, module, _marker in fx.BLOCKS:
        if name in blocks_off:
            continue
        inspected, _wanted = fx.block_plan(name, module, layout, where["codex"], where["claude"], hermes_soul)
        for change in inspected["changes"]:
            if change["before"] != change["after"]:
                what = {"wrap": "put markers around your existing copy of the", "replaced": "refresh the"}.get(change.get("mode"), "add the")
                add("effects", f"{what} {name} instruction block", change["path"])
        for conflict in inspected["conflicts"]:
            add("effects", f"{name} instruction block needs your review", conflict["path"], reason=conflict.get("code"))
        for row in inspected.get("pending", []):
            add("effects", f"{name} instruction block: your similar copy stays active, listed for review", row["path"],
                reason=row.get("code"))
        for note in inspected.get("notes", []):
            add("effects", note, None, block=name)
    item, status = fx.path_effect(layout, ports, where["home"], "dry-run", apply=False, adopt=adopt)
    if status == "planned":
        add("effects", "put the bin folder on the user PATH", item["target"])
    if run.for_this_user and not eutonos_home.is_default(run.base):
        item, status = fx.env_home_effect(layout, ports, "dry-run", apply=False)
        if status == "planned":
            add("effects", "set the user environment variable EUTONOS_HOME to this home", item["target"])
        elif status == "conflict":
            add("effects", "leave EUTONOS_HOME alone (it names another folder)", None)
    if owner_profile:
        add("effects", "copy the owner profile", layout.profiles / "owner.json")
    if adopt and ports.which("claude") and "crosstalk" in info["kits"]:
        old_source = _source_path(_known_marketplace(run))
        if old_source and not fx._same_entry(old_source, str(layout.marketplace / "crosstalk")):
            add("plugin", f"repoint the Claude marketplace {fx.MARKETPLACE_NAME} from its earlier install "
                          "(claude plugin marketplace remove; uninstall adds it back and reinstalls the plugin)",
                old_source, item="migrate.marketplace")
    if "crosstalk" not in {row["id"] for row in resolved if row["state"] == "installed" and not row["enabled"]}:
        add("plugin", "register the Claude plugin (claude plugin marketplace add, install)", layout.marketplace / "crosstalk")
    add("activate", "write the record and state", layout.state)
    return {"outcome": "dry-run", "artifact_id": info["artifact_id"], "version": info["version"], "writes": rows,
            "changes_nothing": True}


def _default_prereq():
    return prereq_module.install_prereq_check()


def install(wheel, *, home=None, user_home=None, os_name=None, ports=None, consent=None, adopt=False, dry_run=False,
            owner_profile=None, channel=None, component_choices=None, components=None, resolver=None, now=None,
            prereq=None, min_compatible=None, for_this_user=False, consent_file=None) -> dict:
    """Install EUTONOS from a wheel into the EUTONOS home. `consent` is a consent block (or a callable returning
    one); with none, the typed ``I agree`` screen runs and needs a person at a console. `consent_file` is the other
    route: what the Setup wizard showed and the person accepted, checked against this machine's own rendering
    (``consent.from_file``). `for_this_user` marks a home that is not ``~/.eutonos`` as the real install for this
    user: it then reaches the person's real apps and sets ``EUTONOS_HOME`` (Windows); without it such a home is a
    staging home."""
    base = resolve_home(home)
    for_this_user = bool(for_this_user)
    if for_this_user and not eutonos_home.is_default(base) and (os_name or iports.default_os_name()) != "windows":
        return {"outcome": "refused", "reason": "for-this-user-windows-only",
                "next_action": "Install into the default per-user home, or use a staging home without this flag."}
    run = Run(base, kind="install", ports=ports, user_home=user_home, os_name=os_name, now=now, manifests=components,
              resolver=resolver, for_this_user=for_this_user)
    run.params = {"user_home": str(user_home) if user_home else None, "for_this_user": for_this_user}
    ready = (prereq or _default_prereq)()
    if ready["outcome"] != "ready":
        return {"outcome": "prereq-missing", "missing": ready["missing"], "guidance": ready.get("guidance"),
                "next_action": "Install the missing tools and run the install again. Nothing was changed."}
    info = fx.inspect_wheel(wheel)
    ts.require(channel is None or channel in ir.CHANNELS, "invalid", f"Unknown channel: {channel}")
    if owner_profile:
        fx.profile_effect(run.layout, "check", Path(owner_profile), apply=False)  # validates the file's shape
    if dry_run:
        note = ("the typed I agree screen will be shown" if consent is None and consent_file is None
                else "consent was supplied by the caller")
        return plan_install(run, info, adopt=adopt, owner_profile=owner_profile, consent_note=note, wheel=wheel,
                            component_choices=component_choices)
    try:
        # Lock order: projects lock first, then this machine lock. Install never touches projects.json.
        with sc.machine_lock(base, wait=0):
            return _install_locked(run, info, wheel, consent, adopt, owner_profile, channel, component_choices,
                                   min_compatible, consent_file)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc), "next_action": "Wait for the other operation to finish."}


def _install_locked(run, info, wheel, consent, adopt, owner_profile, channel, component_choices, min_compatible,
                    consent_file=None):
    layout = run.layout
    state = ir.read_state(layout)
    if state.get("uninstall"):
        return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
    if state["pending"]:
        return {"outcome": "refused", "reason": "pending", "gen_id": state["pending"]["gen_id"],
                "next_action": "An earlier run did not finish. Run: eutonos-install resume (or eutonos-install status)"}
    if state["active"]:
        return {"outcome": "refused", "reason": "already-installed", "gen_id": state["active"],
                "next_action": "EUTONOS is installed. Use: eutonos-install update"}
    try:
        if consent is None and consent_file is not None:
            block = consent_module.from_file(consent_file, None, run.base, os_name=run.os_name,
                                             setup=setup_base(run, component_choices, adopt),
                                             user_home=run.params.get("user_home") or default_user_home(
                                                 run.base, run.for_this_user))
        else:
            block = consent() if callable(consent) else consent if consent is not None else \
                consent_module.accept(None, None, surface="terminal", os_name=run.os_name)
    except consent_module.ConsentRefused as refusal:
        return {"outcome": "consent-refused", "reason": refusal.reason, "detail": refusal.detail,
                "next_action": "Run the install again from an interactive console and type I agree."}
    wheel_copy = _prepare_wheel(layout, wheel)
    gen_id, txn = ir.new_gen_id(), ir.new_txn_id()
    run.state, run.txn, run.info = state, txn, info
    run.params = {"wheel": str(wheel_copy), "adopt": bool(adopt), "owner_profile": str(owner_profile) if owner_profile else None,
                  "user_home": run.params.get("user_home"), "os_name": run.os_name, "channel": channel,
                  "component_choices": component_choices or {}, "min_compatible": min_compatible,
                  "for_this_user": run.for_this_user}
    run.record = ir.new_record(gen_id=gen_id, previous_gen=None, os_name=run.os_name, version=info["version"],
                               wheel={"file": info["file"], "sha256": info["sha256"], "artifact_id": info["artifact_id"]},
                               kits=[], consent=block, now=run.now, min_compatible=min_compatible,
                               for_this_user=run.for_this_user)
    state["for_this_user"] = run.for_this_user
    run.record["component_choices"] = dict(component_choices or {})
    run.record["app_versions"] = sr.read_app_versions(run.ports, run.now)
    state["channel"] = channel or state["channel"]
    state["pending"] = {"txn_id": txn, "gen_id": gen_id, "kind": "install", "phase": None, "params": run.params}
    if adopt and not (state.get("legacy") or {}).get("migrated"):  # the migration runs once per machine
        state["legacy"] = {"inspected": fx.inspect_legacy(layout, run.ports, run.where["home"], run.os_name),
                           "migrated": False}
    run.save()
    run.journal({"event": "begin", "kind": "install", "gen_id": gen_id, "artifact_id": info["artifact_id"]})
    return execute(run)


# ---------------------------------------------------------------------- resume
def _restore_run(base, *, user_home=None, os_name=None, ports=None, now=None, components=None, resolver=None):
    """Rebuild a Run from ``state.pending`` and the journal (the library half of resume and continue)."""
    layout = ir.Layout(base)
    state = ir.read_state(layout)
    pending = state.get("pending")
    ts.require(pending, "missing", "No install, update or rollback is pending.")
    params = pending["params"]
    run = Run(base, kind=pending["kind"], ports=ports, user_home=user_home or params.get("user_home"),
              os_name=os_name or params.get("os_name"), now=now, manifests=components, resolver=resolver)
    run.state, run.txn, run.params = state, pending["txn_id"], params
    run.record = ir.read_record(layout, pending["gen_id"])
    run.info = fx.inspect_wheel(params["wheel"])
    events = ir.journal_read(layout, run.txn)
    _recover_effects(run.record, events)  # written ahead, not yet in the record
    if run.record.get("recovery_action") == "uninstall" or any(event.get("event") == "cleanup-start" for event in events):
        run.state["pending"]["recovery_action"] = "uninstall"
    return run


def _target_state(item) -> str:
    target = str(item.get("target") or "")
    if not target or target.startswith("reg:"):
        return "not-a-file"
    path = Path(target)
    if not path.is_file():
        return "missing" if not path.exists() else "directory"
    current = ir.sha256_file(path)
    if current == item.get("after_sha256"):
        return "after"
    return "before" if current == item.get("before_sha256") else "other"


def recovery_report(layout, state) -> dict:
    """What an interrupted run did and what is left: receipts, the journal and the actual targets."""
    pending = state["pending"]
    gen_id = pending["gen_id"]
    done = ir.done_phases(layout, gen_id)
    events = ir.journal_read(layout, pending["txn_id"])
    recorded = {event["effect"]["effect_id"] for event in events if event.get("event") == "effect"}
    unrecorded = []
    for event in events:
        if event.get("event") == "intent" and event.get("effect_id") and event["effect_id"] not in recorded:
            target = Path(event["target"])
            current = permissions.sha256(permissions._read(target)) if target.exists() else None
            if current != event.get("before_sha256"):
                unrecorded.append({"effect_id": event["effect_id"], "target": event["target"],
                                   "now_matches": "after" if current == event.get("after_sha256") else "other",
                                   "action": "unrecorded, inspect"})
    try:
        record = ir.read_record(layout, gen_id)
        targets = [{"effect_id": item["effect_id"], "target": item["target"], "state": _target_state(item)}
                   for item in record["effects"]]
    except ts.ReadError:
        targets = []
    needs_cleanup = pending.get("recovery_action") == "uninstall" or any(event.get("event") == "cleanup-start" for event in events)
    nxt = next((phase for phase in PHASES if phase not in done), None)
    return {"gen_id": gen_id, "kind": pending["kind"], "txn_id": pending["txn_id"], "completed_phases": done,
            "next_phase": nxt, "journal_events": len(events), "unrecorded": unrecorded, "targets": targets,
            "error": pending.get("error"),
            "next_action": "Cleanup is incomplete. Run: eutonos-install uninstall" if needs_cleanup
                           else f"Run: eutonos-install resume (it continues at {nxt})" if nxt else "Run: eutonos-install resume"}


def resume(*, home=None, user_home=None, os_name=None, ports=None, now=None, components=None, resolver=None) -> dict:
    """Continue an interrupted install or update at the next phase without a receipt."""
    base = resolve_home(home)
    try:
        with sc.machine_lock(base, wait=0):
            state = ir.read_state(ir.Layout(base))
            if state.get("uninstall") or (state.get("pending") or {}).get("recovery_action") == "uninstall":
                return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
            run = _restore_run(base, user_home=user_home, os_name=os_name, ports=ports, now=now, components=components,
                               resolver=resolver)
            if run.state["pending"].get("recovery_action") == "uninstall":
                return _refuse("uninstall-incomplete", "Cleanup already started. Finish removal: eutonos-install uninstall")
            report = recovery_report(run.layout, run.state)
            run.state["pending"].pop("error", None)
            run.journal({"event": "resume", "next_phase": report["next_phase"], "unrecorded": len(report["unrecorded"])})
            if run.kind == "update" and run.params.get("handover", True):
                done = ir.done_phases(run.layout, run.record["gen_id"])
                if "kits" not in done:
                    result = execute(run, resuming=True, stop_after="kits")
                    if result.get("outcome") != "stopped":
                        return result
                result = _handover(run)
            else:
                result = execute(run, resuming=True)
            result["recovery"] = {key: report[key] for key in ("completed_phases", "next_phase", "unrecorded")}
            return result
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc)}
    except ts.ReadError as exc:
        return {"outcome": "refused", "reason": exc.code, "detail": str(exc)}


# ------------------------------------------------------------------ update, rollback
def _launcher_names(run: Run, record) -> list[str]:
    """The entry-point names a generation's ``surface.bin`` Effect recorded."""
    files = _recorded_files(record, "surface.bin")
    suffix = ".exe" if run.os_name == "windows" else ""
    return [name[:-len(suffix)] if suffix and name.endswith(suffix) else name for name in sorted(files)]


def _resync(run: Run, target: dict, older=None) -> dict:
    """Point ``H/bin`` and the marketplace back at `target`'s own runtime and kit."""
    crosstalk = next((item for item in target.get("kits", []) if item["component"] == "crosstalk"), None)
    return sync_surfaces(run, runtime=run.layout.runtimes / target["wheel"]["artifact_id"],
                         kit_dir=run.layout.kits / crosstalk["kit_id"] if crosstalk else None,
                         names=_launcher_names(run, target), gen_id=target["gen_id"], older=older)


def abort_update(run: Run, phase: str, error: str) -> dict:
    """A failed update: the active generation stays active. Undo only what this run newly added, put ``H/bin`` and
    the marketplace back on the active generation, and mark the failed record rolled-back. Blocks refreshed in place
    (the instruction blocks) already hold valid managed text and stay."""
    layout = run.layout
    active_id = run.state["active"]
    older = ir.read_record(layout, active_id)
    known = {item["effect_id"] for item in older["effects"]}
    run.journal({"event": "cleanup-start", "action": "uninstall"})
    result = reverse_effects(run, [item for item in run.record["effects"] if item["effect_id"] not in known], tag="abort")
    resynced: dict = {}
    try:
        done = _resync(run, older)
        resynced = {"bin": sorted(done["bin"]), "marketplace": sorted(done["marketplace"] or {})}
        resynced["plugin"] = _plugin_update(run)
    except Exception as exc:  # the previous runtime may be damaged: say so, never hide it
        resynced = {"error": f"{type(exc).__name__}: {exc}"}
    if result["failed"] or resynced.get("error"):
        run.withdraw(result["removed"])
        run.state["pending"]["recovery_action"] = "uninstall"
        run.record["recovery_action"] = "uninstall"
        run.state["pending"]["error"] = {"phase": phase, "error": error, "failed": result["failed"], "resynced": resynced}
        run.save()
        return {"outcome": "failed", "phase": phase, "error": error, **result, "resynced": resynced,
                "gen_id": run.record["gen_id"], "next_action": "Cleanup is incomplete. Inspect status, fix the failure, then run eutonos-install uninstall."}
    ir.merge_effects(older, [copy.deepcopy(item) for item in run.record["effects"]
                            if item["effect_id"] in known and not item["effect_id"].startswith(("surface.",) + PRIVATE_PREFIXES)])
    ir.write_record(layout, run.state, older)
    run.record["state"] = "rolled-back"
    run.record["abort"] = {"phase": phase, "error": error, **result, "resynced": resynced}
    run.state["pending"] = None
    run.save()
    ir.write_atomic(layout.receipts(run.record["gen_id"]) / "95-abort.json",
                    ir.dump({"phase": phase, "error": error, **result, "resynced": resynced,
                             "completed_at": ir.now_iso(run.now)}))
    return {"outcome": "rolled-back", "phase": phase, "error": error, "gen_id": run.record["gen_id"], "active": active_id,
            "removed": result["removed"], "kept": result["kept"], "resynced": resynced,
            "next_action": "The earlier generation is still active. Fix the cause and run the update again."}


def _last_json(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _handover(run: Run) -> dict:
    """Let the new runtime finish the update (its own components and instruction text do the effects). The new
    process does not take the machine lock this one holds; it proves it was started by us with a one-time token."""
    layout, ports = run.layout, run.ports
    python = ports.runtime_python(layout.runtimes / run.info["artifact_id"])
    token = secrets.token_hex(12)
    run.state["pending"]["handover"] = {"token": token}
    run.save()
    run.journal({"event": "intent", "what": "handover", "python": str(python)})
    code, out, err = ports.handover(python, run.record["gen_id"], run.base, token)
    state = ir.read_state(layout)
    record = ir.read_record(layout, run.record["gen_id"])
    run.state, run.record = state, record
    if state.get("pending") and state["pending"].get("gen_id") == record["gen_id"]:
        if state["pending"].get("recovery_action") == "uninstall":
            return _last_json(out) or {"outcome": "failed", "gen_id": record["gen_id"],
                                      "next_action": "Cleanup is incomplete. Run eutonos-install uninstall."}
        _recover_effects(run.record, ir.journal_read(layout, run.txn))  # the new runtime wrote ahead
        return abort_update(run, "handover", f"the new runtime did not finish (exit {code}): {(err or out).strip()[-200:]}")
    child = _last_json(out)
    if record["state"] == "rolled-back":
        return child or {"outcome": "rolled-back", "gen_id": record["gen_id"], "error": (record.get("abort") or {}).get("error")}
    run.doctor = child.get("doctor")
    return finish(run)


def update(*, source=None, home=None, user_home=None, os_name=None, ports=None, channel=None, dry_run=False,
           handover=True, components=None, resolver=None, now=None, doctor=None, allow_unsigned=False,
           interpreter=None) -> dict:
    """Update to the newest release that names this machine's channel. The release's signature (over its manifest) and
    the wheel's sha256 are checked before anything is written; a release that needs a newer starting version
    (``min_compatible``) or a different bundled Python (``interpreter``) is refused with "run Setup again". A new
    generation is built beside the active one; with `handover` the new runtime finishes the work. A failed update
    leaves the earlier generation active. `allow_unsigned` is the developer override for a release folder."""
    base = resolve_home(home)
    ts.require(channel is None or channel in ir.CHANNELS, "invalid", f"Unknown channel: {channel}")
    run = Run(base, kind="update", ports=ports, user_home=user_home, os_name=os_name, now=now, manifests=components,
              resolver=resolver)
    run.doctor_fn = doctor
    run.interpreter_tag = interpreter
    run.params = {"user_home": str(user_home) if user_home else None}
    try:
        # Lock order: projects lock first, then this machine lock. Update never touches projects.json.
        with sc.machine_lock(base, wait=0):
            return _update_locked(run, _open_source(base, source, allow_unsigned), channel, dry_run, handover)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc), "next_action": "Wait for the other operation to finish."}
    except ts.ReadError as exc:
        if exc.code == "sha256-mismatch":  # the download was refused before anything under the home was written
            return {"outcome": "refused", "reason": "sha256-mismatch", "detail": str(exc),
                    "next_action": "The download does not match the release manifest. Nothing was changed."}
        return {"outcome": "unavailable", "reason": exc.code, "detail": str(exc),
                "next_action": "Nothing was changed. Check the network connection and the releases repository, then try again."}


def _open_source(base, source, allow_unsigned=False):
    """The update source: a given source object or folder, else the public releases repository (the saved one)."""
    if source is not None and source != "github":
        return rs.open_source(source, allow_unsigned=allow_unsigned)
    try:
        return rs.open_source(None, repo=uc.configured_repo(base), allow_unsigned=allow_unsigned)
    except uc.UpdateCheckError as exc:
        raise ts.ReadError("invalid", f"{exc}. Repair or delete it, then try again.") from exc


def compatibility(active: dict, manifest: dict, interpreter=None) -> dict | None:
    """Why an update cannot apply `manifest` over the `active` generation, or None when it can. An update reuses the
    installed Python and the data the installed version wrote, so it refuses a release that (a) needs a newer starting
    version than the installed one (``min_compatible``: the installed version cannot migrate from there) or (b) was
    built for a different bundled Python (``interpreter``: only Setup can replace that)."""
    installed, floor = active["eutonos_version"], manifest.get("min_compatible")
    if floor and rs.version_tuple(installed) < rs.version_tuple(floor):
        return {"reason": "min-compatible", "installed": installed, "min_compatible": floor,
                "detail": f"EUTONOS {manifest['version']} can only be applied over {floor} or newer, and this install is {installed}.",
                "next_action": "Run Setup again (the newest Setup from the releases page); an update cannot migrate this far."}
    wanted = manifest.get("interpreter")
    have = interpreter or rs.current_interpreter()
    if wanted and not rs.interpreter_matches(wanted, have):
        return {"reason": "interpreter-changed", "installed": have, "needs": wanted,
                "detail": f"EUTONOS {manifest['version']} needs the bundled Python {wanted}, and this install runs {have}.",
                "next_action": "Run Setup again: an update cannot replace the bundled Python."}
    return None


def _untrusted(source, release) -> dict | None:
    """A refusal when the release was not verified (a custom source that skipped the signature check counts as unsigned)."""
    if release is not None and release.get("trust") in ("verified", "unsigned-allowed"):
        return None
    shown = (getattr(source, "rejected", None) or [])[:5]
    return {"outcome": "refused", "reason": (shown[0]["reason"] if shown else "unsigned"), "rejected": shown,
            "detail": shown[0]["detail"] if shown else "The release was not signature-checked.",
            "next_action": "Nothing was changed. Only a release signed with the EUTONOS release key can be installed."}


def check_update(*, source=None, home=None, channel=None, now=None, allow_unsigned=False, interpreter=None,
                 capture=None) -> dict:
    """Read-only: is a newer release available? Asks the release source for its manifests and downloads no wheel and
    writes nothing under the install (the update notice, when switched on, is the only thing recorded, in settings).
    Outcomes: ``update-available`` (with ``installed`` and ``available``), ``up-to-date``, ``no-release``, ``refused``
    (not installed, or an incomplete earlier run) and ``unavailable`` (the source could not be reached). ``capture``
    (a dict) is for the companion's background check: it gets the opened ``source`` and the newest ``release`` so a
    verified release can be staged without asking the network again."""
    base = resolve_home(home)
    layout = ir.Layout(base)
    try:
        state = ir.read_state(layout)
        if state.get("uninstall"):
            return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
        if not state["active"]:
            return {"outcome": "refused", "reason": "not-installed", "next_action": "Install first: eutonos-install install"}
        active = ir.read_record(layout, state["active"])
        wanted = channel or state["channel"]
        ts.require(wanted in ir.CHANNELS, "invalid", f"Unknown channel: {wanted}")
        opened = _open_source(base, source, allow_unsigned)
        release = rs.newest(opened.releases(), wanted)
    except ts.ReadError as exc:
        return {"outcome": "unavailable", "reason": exc.code, "detail": str(exc),
                "next_action": "Nothing was changed. Check the network connection and the releases repository, then try again."}
    if capture is not None:
        capture.update(source=opened, release=release)
    installed = {"version": active["eutonos_version"], "artifact_id": active["wheel"]["artifact_id"]}
    if release is None and getattr(opened, "rejected", None):
        return {"outcome": "refused", **_untrusted(opened, None), "channel": wanted, "installed": installed}
    if release is None:
        result = {"outcome": "no-release", "channel": wanted, "installed": installed,
                  "next_action": f"No release names the {wanted} channel."}
    elif _untrusted(opened, release):
        return {"outcome": "refused", **_untrusted(opened, release), "channel": wanted, "installed": installed}
    else:
        manifest = release["manifest"]
        available = {"version": manifest["version"], "artifact_id": manifest["artifact_id"], "release": release["id"],
                     "min_compatible": manifest["min_compatible"], "published": release.get("published")}
        older = rs.version_tuple(manifest["version"]) < rs.version_tuple(active["eutonos_version"])
        if manifest["artifact_id"] == installed["artifact_id"] or (older and wanted == state["channel"]):
            result = {"outcome": "up-to-date", "channel": wanted, "installed": installed, "available": available}
        else:
            blocked = compatibility(active, manifest, interpreter)
            if blocked:
                available["blocked"] = blocked
            result = {"outcome": "update-available", "channel": wanted, "installed": installed, "available": available,
                      "next_action": blocked["next_action"] if blocked else "Update with: eutonos-install update"}
    result["notice"] = uc.record_check(result, home=base, now=now)["notice"]
    return result


def _update_locked(run: Run, source, channel, dry_run, handover, local_patch=None) -> dict:
    layout = run.layout
    state = ir.read_state(layout)
    if state.get("uninstall"):
        return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
    if state["pending"]:
        return {"outcome": "refused", "reason": "pending", "gen_id": state["pending"]["gen_id"],
                "next_action": "An earlier run did not finish. Run: eutonos-install resume (or eutonos-install status)"}
    if not state["active"]:
        return {"outcome": "refused", "reason": "not-installed", "next_action": "Install first: eutonos-install install"}
    active = ir.read_record(layout, state["active"])
    wanted = channel or state["channel"]
    release = rs.newest(source.releases(), wanted)
    if release is None and getattr(source, "rejected", None):
        return {**_untrusted(source, None), "channel": wanted}
    if release is None:
        return {"outcome": "no-release", "channel": wanted, "next_action": f"No release names the {wanted} channel."}
    if _untrusted(source, release):
        return {**_untrusted(source, release), "channel": wanted}
    manifest = release["manifest"]
    if manifest["artifact_id"] == active["wheel"]["artifact_id"]:
        same = {"outcome": "up-to-date", "channel": wanted, "version": manifest["version"],
                "artifact_id": manifest["artifact_id"]}
        if local_patch:
            same["note"] = "The checkout builds the same wheel as the active generation; nothing was changed."
        return same
    older = rs.version_tuple(manifest["version"]) < rs.version_tuple(active["eutonos_version"])
    if older and wanted == state["channel"]:
        return {"outcome": "refused", "reason": "older-than-installed", "version": manifest["version"],
                "installed": active["eutonos_version"], "next_action": "Use rollback to go back to an earlier generation."}
    blocked = None if local_patch else compatibility(active, manifest, getattr(run, "interpreter_tag", None))
    if blocked:
        return {"outcome": "refused", **blocked, "version": manifest["version"], "artifact_id": manifest["artifact_id"]}
    replaced = None
    if not local_patch and isinstance(active.get("local_patch"), dict):  # an official build replaces a local patch
        shown = sr.patch_summary(active["local_patch"])
        replaced = {"reason": shown["reason"] if shown else None, "source_commit": active["local_patch"].get("source_commit"),
                    **sr.fix_verdict(active["local_patch"], manifest.get("source_commit"))}
    if dry_run:
        plan = {"outcome": "dry-run", "changes_nothing": True, "channel": wanted, "release": release["id"],
                "would_install": {"version": manifest["version"], "artifact_id": manifest["artifact_id"],
                                  "min_compatible": manifest["min_compatible"]},
                "installed": {"version": active["eutonos_version"], "artifact_id": active["wheel"]["artifact_id"]}}
        if local_patch:
            plan["local_patch"] = local_patch
        if replaced:
            plan["replaces_local_patch"] = replaced
        return plan
    with tempfile.TemporaryDirectory(prefix="eutonos-update-") as scratch:
        wheel = source.fetch_wheel(release, Path(scratch))
        digest = ir.sha256_file(wheel)
        if digest != manifest["sha256"]:  # nothing under the home has been written yet
            return {"outcome": "refused", "reason": "sha256-mismatch", "expected": manifest["sha256"], "found": digest,
                    "next_action": "The download does not match the release manifest. Nothing was changed."}
        info = fx.inspect_wheel(wheel)
        if info["artifact_id"] != manifest["artifact_id"] or info["version"] != manifest["version"]:
            return {"outcome": "refused", "reason": "artifact-mismatch", "manifest": manifest["artifact_id"],
                    "wheel": info["artifact_id"], "next_action": "The wheel is not the one the manifest names. Nothing was changed."}
        wheel_copy = _prepare_wheel(layout, wheel)
    gen_id, txn = ir.new_gen_id(), ir.new_txn_id()
    run.state, run.txn, run.info = state, txn, info
    choices = dict(active.get("component_choices") or {})
    run.params = {"wheel": str(wheel_copy), "adopt": False, "owner_profile": None, "user_home": run.params.get("user_home"),
                  "os_name": run.os_name, "channel": wanted, "component_choices": choices,
                  "min_compatible": manifest["min_compatible"], "release": release["id"], "handover": bool(handover),
                  "from_version": active["eutonos_version"]}
    run.record = ir.new_record(gen_id=gen_id, previous_gen=state["active"], os_name=run.os_name, version=info["version"],
                               wheel={"file": info["file"], "sha256": info["sha256"], "artifact_id": info["artifact_id"]},
                               kits=[], consent=active.get("consent"), now=run.now, min_compatible=manifest["min_compatible"],
                               for_this_user=run.for_this_user)
    run.record["effects"] = [copy.deepcopy(item) for item in active["effects"]
                             if not item["effect_id"].startswith(PRIVATE_PREFIXES)]
    run.record["projects"] = list(active.get("projects") or [])
    run.record["component_choices"] = choices
    run.record["app_versions"] = sr.read_app_versions(run.ports, run.now)
    if local_patch:
        run.record["local_patch"] = local_patch
    if replaced:
        run.record["replaced_local_patch"] = replaced
    state["pending"] = {"txn_id": txn, "gen_id": gen_id, "kind": "update", "phase": None, "params": run.params}
    run.save()
    run.journal({"event": "begin", "kind": "update", "gen_id": gen_id, "artifact_id": info["artifact_id"],
                 "from": state["active"]})
    if not handover:
        return execute(run)
    stopped = execute(run, stop_after="kits")
    if stopped.get("outcome") != "stopped":
        return stopped
    return _handover(run)


def patch(*, checkout, reason, allow_dirty=False, home=None, user_home=None, os_name=None, ports=None, dry_run=False,
          handover=True, components=None, resolver=None, now=None, doctor=None, build=None, git=None) -> dict:
    """Install a local patch: build a wheel from `checkout` and install it through the normal update path as a new
    generation whose record carries ``local_patch`` (reason, checkout, source commit, base release, dirty flag, a digest
    and a short list of the changed package files). It is an ordinary generation: ``rollback``, ``update`` and
    ``uninstall`` work on it unchanged. A checkout with uncommitted build inputs is refused unless `allow_dirty`
    (then ``dirty`` is recorded true). `build(root, output_dir) -> wheel` and `git(root, *args)` are test seams.
    ``dry_run`` builds the wheel in a scratch folder and writes nothing under the EUTONOS home."""
    base = resolve_home(home)
    run = Run(base, kind="update", ports=ports, user_home=user_home, os_name=os_name, now=now, manifests=components,
              resolver=resolver)
    run.doctor_fn = doctor
    run.params = {"user_home": str(user_home) if user_home else None}
    try:
        text = sr.clean_reason(reason)
        with sc.machine_lock(base, wait=0):
            return _patch_locked(run, checkout, text, allow_dirty, dry_run, handover, build, git)
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc), "next_action": "Wait for the other operation to finish."}
    except ts.ReadError as exc:
        return {"outcome": "refused", "reason": exc.code, "detail": str(exc),
                "next_action": "Nothing was changed. Fix the cause named in detail and run the patch again."}


def _patch_locked(run: Run, checkout, reason, allow_dirty, dry_run, handover, build, git) -> dict:
    layout = run.layout
    state = ir.read_state(layout)
    if state.get("uninstall"):
        return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
    if state["pending"]:
        return {"outcome": "refused", "reason": "pending", "gen_id": state["pending"]["gen_id"],
                "next_action": "An earlier run did not finish. Run: eutonos-install resume (or eutonos-install status)"}
    if not state["active"]:
        return {"outcome": "refused", "reason": "not-installed", "next_action": "Install first: eutonos-install install"}
    active = ir.read_record(layout, state["active"])
    found = sr.inspect_checkout(checkout, allow_dirty=allow_dirty, git=git)
    with tempfile.TemporaryDirectory(prefix="eutonos-patch-") as scratch:
        scratch = Path(scratch)
        wheel = Path((build or functools.partial(sr.build_wheel_from_checkout, allow_dirty=found["dirty"]))(found["root"], scratch / "wheel"))
        info = fx.inspect_wheel(wheel)
        changed, digest = sr.changed_files(wheel, run.ports.site_packages(layout.runtimes / active["wheel"]["artifact_id"]))
        meta = sr.local_patch_record(reason, found, active, changed, digest)
        folder = scratch / "releases" / "local-patch"
        folder.mkdir(parents=True)
        shutil.copyfile(wheel, folder / wheel.name)
        manifest = {"profile": rs.PROFILE, "version": info["version"], "artifact_id": info["artifact_id"],
                    "wheel": wheel.name, "sha256": info["sha256"], "channels": [state["channel"]],
                    "min_compatible": active.get("min_compatible"), "source_commit": found["commit"]}
        (folder / rs.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        # built here from the owner's own checkout: no release key signed it, so it is the one unsigned local source
        return _update_locked(run, rs.FolderSource(scratch / "releases", allow_unsigned=True), None, dry_run, handover,
                              local_patch=meta)


def continue_handover(*, gen_id: str, token: str | None = None, home=None, user_home=None, os_name=None, ports=None,
                      now=None, components=None, resolver=None, doctor=None) -> dict:
    """The new runtime's half of an update (``eutonos-install continue``). With the caller's one-time token it runs the
    remaining phases without the machine lock the caller holds; without it this is an ordinary resume."""
    base = resolve_home(home)
    layout = ir.Layout(base)
    pending = (ir.read_state(layout).get("pending") or {})
    ts.require(pending.get("gen_id") == gen_id, "missing", f"Generation {gen_id} is not the pending one.")
    if not token or token != (pending.get("handover") or {}).get("token"):
        return resume(home=home, user_home=user_home, os_name=os_name, ports=ports, now=now, components=components,
                      resolver=resolver)
    run = _restore_run(base, user_home=user_home, os_name=os_name, ports=ports, now=now, components=components,
                       resolver=resolver)
    result = execute(run, stop_after="activate")
    if result.get("outcome") != "stopped":
        return result
    return {"outcome": "handover-done", "gen_id": gen_id,
            "doctor": run_doctor(doctor, home=base, user_home=run.user_home)}


def rollback(*, home=None, user_home=None, os_name=None, ports=None, dry_run=False, now=None, components=None,
             resolver=None, official=False) -> dict:
    """Re-activate the previous generation. It refuses when that generation is older than the active record's
    ``min_compatible`` (F7), or damaged. Effects only the newer generation made are reversed; blocks the newer
    generation refreshed in place stay and are listed. The steps are idempotent, and ``state.active`` flips last.
    With `official` it only goes back when the active generation is a local patch and the generation before it is an
    official build; otherwise it refuses and says what to do (the older official runtimes are not kept)."""
    base = resolve_home(home)
    run = Run(base, kind="rollback", ports=ports, user_home=user_home, os_name=os_name, now=now, manifests=components,
              resolver=resolver)
    layout = run.layout
    try:
        with sc.machine_lock(base, wait=0):
            state = ir.read_state(layout)
            if state.get("uninstall"):
                return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
            if state["pending"]:
                return {"outcome": "refused", "reason": "pending", "gen_id": state["pending"]["gen_id"],
                        "next_action": "An earlier run did not finish. Run: eutonos-install resume"}
            if not state["active"]:
                return {"outcome": "refused", "reason": "not-installed", "next_action": "There is nothing to roll back."}
            newer = ir.read_record(layout, state["active"])
            previous_id = newer.get("previous_gen")
            if not previous_id:
                return {"outcome": "refused", "reason": "no-previous-generation",
                        "next_action": "Only one generation exists, so there is nothing to go back to."}
            try:
                previous = ir.read_record(layout, previous_id)
            except ts.ReadError as exc:
                return {"outcome": "refused", "reason": "previous-generation-missing", "detail": str(exc),
                        "next_action": "The previous generation's record is gone; install the earlier wheel instead."}
            if official and not isinstance(newer.get("local_patch"), dict):
                return {"outcome": "refused", "reason": "not-a-local-patch", "gen_id": state["active"],
                        "next_action": "The active generation is already an official build; there is no patch to leave."}
            if official and isinstance(previous.get("local_patch"), dict):
                return {"outcome": "refused", "reason": "previous-is-also-a-local-patch", "previous": previous_id,
                        "next_action": "The generation before this one is a local patch too, and older official runtimes "
                                       "are not kept. Run: eutonos-install update (installs the newest official release)."}
            floor = newer.get("min_compatible")
            if not version_at_least(previous["eutonos_version"], floor):
                return {"outcome": "refused", "reason": "below-min-compatible", "previous": previous["eutonos_version"],
                        "min_compatible": floor,
                        "next_action": f"Version {previous['eutonos_version']} is older than the {floor} this data needs."}
            damage = _previous_damage(run, previous)
            if damage:
                return {"outcome": "refused", "reason": "previous-generation-damaged", "detail": damage,
                        "next_action": "The earlier runtime or kit changed on disk; reinstall that release instead."}
            if newer.get("recovery_contract") and not previous.get("recovery_contract"):
                return _refuse("previous-engine-lacks-recovery", "This older engine cannot safely reverse the current Effects. "
                               "Keep the current engine for removal, or install a release with reversible recovery support.")
            prior_ids = {item["effect_id"] for item in previous["effects"]}
            public = [item for item in newer["effects"] if not item["effect_id"].startswith(PRIVATE_PREFIXES)]
            only_newer = [item for item in public if item["effect_id"] not in prior_ids]
            by_id = {item["effect_id"]: item for item in previous["effects"]}
            refreshed = [item["effect_id"] for item in public if item["effect_id"] in by_id
                         and not item["effect_id"].startswith("surface.")
                         and (item["after_sha256"], item["shipped_sha256"])
                         != (by_id[item["effect_id"]]["after_sha256"], by_id[item["effect_id"]]["shipped_sha256"])]
            if dry_run:
                return {"outcome": "dry-run", "changes_nothing": True, "to": previous_id,
                        "version": previous["eutonos_version"],
                        "would_reverse": [item["effect_id"] for item in reversed(only_newer)],
                        "would_keep_newer": refreshed}
            run.state, run.record, run.txn = state, previous, ir.new_txn_id()
            run.info = {"artifact_id": previous["wheel"]["artifact_id"], "version": previous["eutonos_version"]}
            run.journal({"event": "begin", "kind": "rollback", "from": newer["gen_id"], "to": previous_id})
            done = _resync(run, previous, older=newer)
            plugin = _plugin_update(run)
            result = reverse_effects(run, only_newer, tag="rollback")
            kept_newer = [{"effect_id": name, "reason": "refreshed by the newer generation; left in place"}
                          for name in refreshed]
            newer["state"] = "rolled-back"
            newer["rollback"] = {"at": ir.now_iso(now), "to": previous_id, **result, "kept_newer": kept_newer}
            # These effects deliberately stay newer; the active record must own their current
            # hashes and original undo so uninstall can still reverse them.
            ir.merge_effects(previous, [copy.deepcopy(item) for item in public if item["effect_id"] in refreshed])
            failures = {item["effect_id"] for item in result["failed"]}
            ir.merge_effects(previous, [copy.deepcopy(item) for item in only_newer if item["effect_id"] in failures])
            previous["state"] = "active"
            ir.write_record(layout, state, previous)
            ir.write_record(layout, state, newer)
            state["active"] = previous_id
            if failures:
                remaining = {item["effect_id"]: item for item in previous["effects"]}
                for retained_id in state["generations"]:
                    retained = ir.read_record(layout, retained_id)
                    remaining.update({item["effect_id"]: item for item in retained["effects"]
                                      if item["effect_id"].startswith(PRIVATE_PREFIXES)})
                state["uninstall"] = {"effects": list(remaining.values()), "gen_ids": [previous_id, newer["gen_id"]]}
                ir.write_state(layout, state)
                return {"outcome": "failed", "phase": "rollback", "gen_id": previous_id, **result,
                        "next_action": "Some newer effects could not be removed. Fix the reported failure, then run eutonos-install uninstall."}
            ir.write_state(layout, state)
            try:  # the earlier generation may remember a part's Effects differently from how the person left it
                reconciled = reconcile_components(run)
            except Exception as exc:
                reconciled = {"error": f"{type(exc).__name__}: {exc}"}
            ir.write_atomic(layout.receipts(newer["gen_id"]) / "91-rollback.json",
                            ir.dump({"phase": "rollback", "gen_id": newer["gen_id"], "to": previous_id, **result,
                                     "kept_newer": kept_newer, "three_way": done["three_way"],
                                     "completed_at": ir.now_iso(now)}))
            steps = [{"id": "resync", "kind": "mechanical", "runner": "agent", "state": "done",
                      "evidence": [f"bin {len(done['bin'])} launchers, plugin {plugin['plugin']}"]},
                     {"id": "reverse-newer-effects", "kind": "mechanical", "runner": "agent", "state": "done",
                      "evidence": [f"removed {len(result['removed'])}, kept {len(result['kept'])}"]},
                     {"id": "restart-apps", "kind": "person", "runner": "owner", "state": "pending",
                      "next_action": "Restart Claude Desktop, Claude Code and Codex so they use the earlier version."}]
            if done["three_way"] or kept_newer:
                steps.append({"id": "three-way", "kind": "agent", "runner": "agent", "state": "pending", "evidence": [],
                              "next_action": "Review the kept-newer blocks and any changed files in the rollback receipt."})
            sc.start("install", steps, checklist_revision=CHECKLIST_REVISION, gen_id=previous_id, home=base, now=now)
            answer = {"outcome": "rolled-back-to", "gen_id": previous_id, "version": previous["eutonos_version"],
                      "from": newer["gen_id"], "removed": result["removed"], "kept": result["kept"],
                      "kept_newer": kept_newer, "three_way": done["three_way"], "plugin": plugin,
                      "components": reconciled,
                      "next_action": "Restart the apps. Run an update later to move forward again."}
            if isinstance(newer.get("local_patch"), dict):
                answer["left_local_patch"] = sr.patch_summary(newer["local_patch"])
            return answer
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc), "next_action": "Wait for the other operation to finish."}


def _previous_damage(run: Run, previous: dict) -> str | None:
    """Why `previous` cannot be re-activated (its runtime or a kit is not the recorded bytes), else None."""
    runtime = run.layout.runtimes / previous["wheel"]["artifact_id"]
    effect_id = f"runtime.{previous['wheel']['artifact_id']}"
    recorded = next((item for item in previous["effects"] if item["effect_id"] == effect_id), None)
    if recorded is None or not runtime.is_dir():
        return f"the runtime {runtime.name} is missing"
    if fx.runtime_package_digest(run.ports.site_packages(runtime)) != recorded["after_sha256"]:
        return f"the runtime {runtime.name} does not match its record"
    for item in previous.get("kits", []):
        if not kits_module.verify(run.layout.kits / item["kit_id"]):
            return f"the kit {item['kit_id']} is missing or changed"
    return None


# --------------------------------------------------------------------- uninstall
def _person_reversals() -> list[dict]:
    data = permissions.load()
    steps = data.get("guided_removal_steps") or {}
    found = [{"id": name, "what": steps[name]} for name in steps]
    found.append({"id": "restart-apps", "what": "Restart Claude Desktop, Claude Code and Codex so they stop using EUTONOS."})
    return found


def uninstall(*, home=None, user_home=None, os_name=None, ports=None, keep_projects=False, dry_run=False, now=None,
              components=None, resolver=None) -> dict:
    """Reverse the active record newest first. It keeps what you changed, backups, hosts, RAM, project data and the
    record, and it refuses while projects are registered unless `keep_projects`. It never deletes the home."""
    base = resolve_home(home)
    run = Run(base, kind="uninstall", ports=ports, user_home=user_home, os_name=os_name, now=now, manifests=components,
              resolver=resolver)
    layout = run.layout
    try:
        # Lock order: projects lock first, then this machine lock, never the reverse. Only a lock-free read of
        # projects.json happens inside it (registered_projects), so no projects lock is needed here.
        with sc.machine_lock(base, wait=0):
            state = ir.read_state(layout)
            gen_id = state["active"] or (state["pending"] or {}).get("gen_id")
            if not gen_id:
                return {"outcome": "nothing-to-uninstall", "next_action": "EUTONOS is not installed here."}
            projects = registered_projects(layout)
            if projects and not keep_projects:
                return {"outcome": "refused", "reason": "projects-registered",
                        "projects": [item.get("root") if isinstance(item, dict) else item for item in projects],
                        "next_action": "Detach each project first (eutonos remove-project ROOT), or pass --keep-projects "
                                       "to uninstall and keep them registered."}
            record = ir.read_record(layout, gen_id)
            records = {gen_id: record}
            pending = state.get("pending") or {}
            if pending.get("gen_id") and pending["gen_id"] not in records:
                records[pending["gen_id"]] = ir.read_record(layout, pending["gen_id"])
            if pending.get("txn_id"):
                _recover_effects(records[pending["gen_id"]], ir.journal_read(layout, pending["txn_id"]))
            retained_private = {}
            for retained_id in state["generations"]:
                # Active/pending records can legitimately be ahead of state after
                # record-write succeeds and state-write stops; their Effects are
                # recovered above from the pending write-ahead journal. Historical
                # records have no such recovery path, so retain the strict hash gate.
                if retained_id not in records and not ir.record_intact(layout, state, retained_id):
                    return _refuse("record-damaged", f"Inspect generation {retained_id}; its record cannot safely authorize cleanup.")
                retained = ir.read_record(layout, retained_id)
                for item in retained["effects"]:
                    if item["effect_id"].startswith(PRIVATE_PREFIXES):
                        retained_private[item["effect_id"]] = item
            retry = state.get("uninstall")
            if retry:
                effects = list(retry["effects"])
            else:
                combined = dict(retained_private)
                for current in list(records.values()):
                    for item in current["effects"]:
                        combined[item["effect_id"]] = item
                effects = list(combined.values())
            if dry_run:
                return {"outcome": "dry-run", "gen_id": gen_id, "changes_nothing": True,
                        "would_reverse": [{"effect_id": item["effect_id"], "target": item["target"],
                                           "undo": (item.get("undo") or {}).get("op")} for item in reversed(effects)],
                        "person_reversals": _person_reversals()}
            run.state, run.record, run.txn = state, record, ir.new_txn_id()
            run.info = {"artifact_id": record["wheel"]["artifact_id"], "version": record["eutonos_version"]}
            run.journal({"event": "begin", "kind": "uninstall", "gen_id": gen_id})
            result = reverse_effects(run, effects)
            if result["failed"]:
                remaining = {row["effect_id"] for row in result["failed"]}
                state["uninstall"] = {"effects": [item for item in effects if item["effect_id"] in remaining],
                                      "gen_ids": list(records)}
                record["uninstall"] = {"at": ir.now_iso(now), **result, "complete": False}
                run.save()
                ir.write_atomic(layout.receipts(gen_id) / "90-uninstall.json", ir.dump(record["uninstall"]))
                next_action = ("Fix the incomplete removals, then run eutonos-install uninstall again. "
                               "Completed removals will not be replayed; Setup support files must stay.")
                if any("running runtime" in row.get("reason", "") for row in result["failed"]):
                    next_action = ("This launcher uses the runtime being removed. On Windows use Settings > Installed apps > EUTONOS "
                                   "or the bundled Python/bootstrap uninstall command; elsewhere use a supported external interpreter. "
                                   "Do not retry through the same runtime launcher. Setup support files must stay.")
                return {"outcome": "failed", "phase": "uninstall", "gen_id": gen_id, **result, "next_action": next_action}
            state.pop("uninstall", None)
            for other_id in set(records) | set(state["generations"]):
                fx.remove_generation_marketplace(layout, other_id)
            for other_id, other in records.items():
                if other_id != gen_id:
                    other["state"] = "rolled-back"
                    other["uninstall"] = {"at": ir.now_iso(now), **result}
                    ir.write_record(layout, state, other)
            component_state.remove_file(base)  # the parts are gone, so is the note of which ones were switched on
            if state.get("legacy") and any(str(item).startswith("migrate.") for item in result["removed"]):
                state["legacy"]["migrated"] = False  # the earlier hand install is back, so a later --adopt repoints it again
            was_pending = state["active"] != gen_id
            record["state"] = "rolled-back" if was_pending else "retired"
            record["uninstall"] = {"at": ir.now_iso(now), **result}
            if projects:
                record["kept_projects"] = [item.get("root") if isinstance(item, dict) else item for item in projects]
            state["active"] = None
            state["pending"] = None
            run.save()
            ir.write_atomic(layout.receipts(gen_id) / "90-uninstall.json",
                            ir.dump({"phase": "uninstall", "gen_id": gen_id, "completed_at": ir.now_iso(now), **result}))
            person = _person_reversals()
            steps = [{"id": "registered-projects", "kind": "mechanical", "runner": "agent", "state": "done",
                      "evidence": [f"{len(projects)} registered; keep_projects={bool(keep_projects)}"]},
                     {"id": "halt-crosstalk", "kind": "agent", "runner": "agent", "state": "pending",
                      "next_action": "Run eutonos-crosstalk halt if it is still on the PATH, then confirm it."},
                     {"id": "running-chats", "kind": "agent", "runner": "agent", "state": "pending",
                      "next_action": "Tell the owner which EUTONOS chats are still open."},
                     {"id": "stop-companion", "kind": "agent", "runner": "agent", "state": "pending",
                      "next_action": "Stop the EUTONOS companion and dashboard (port 8765) if they are still running."},
                     {"id": "reverse-effects", "kind": "mechanical", "runner": "agent", "state": "done",
                      "evidence": [f"removed {len(result['removed'])}, kept {len(result['kept'])}"]}]
            steps += [{"id": f"undo-{item['id']}", "kind": "person", "runner": "owner", "state": "pending",
                       "next_action": item["what"]} for item in person]
            sc.start("uninstall", steps, checklist_revision=CHECKLIST_REVISION, gen_id=gen_id, home=base, now=now)
            return {"outcome": "uninstalled", "gen_id": gen_id, "removed": result["removed"], "kept": result["kept"],
                    "kept_projects": record.get("kept_projects", []), "person_reversals": person,
                    "kept_note": "Backups, hosts, RAM, project data and the record stay.",
                    "checkpoint": str(sc.checkpoint_path(base))}
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc), "next_action": "Wait for the other operation to finish."}


# ------------------------------------------------------------- component enable / disable
def _component_rows(record) -> dict:
    return {row["id"]: row for row in (record or {}).get("components", []) if isinstance(row, dict) and "id" in row}


def _is_enabled(row, saved) -> bool:
    """The part's state: the state file when it names the part, else the install record's row."""
    entry = saved.get(row["id"])
    return entry["enabled"] if entry is not None else bool(row.get("enabled", True))


def component_status(*, home=None, components=None, resolver=None) -> dict:
    """Every part with its state and whether it is switched on. Read-only; takes no lock."""
    base = resolve_home(home)
    layout = ir.Layout(base)
    state = ir.read_state(layout)
    if not state["active"]:
        return {"outcome": "not-installed", "next_action": "EUTONOS is not installed here."}
    try:
        saved = component_state.read(base)
    except component_state.ComponentStateError as exc:
        return {"outcome": "refused", "reason": "state-unreadable", "detail": str(exc),
                "next_action": f"Repair or delete {component_state.path(base)}, then ask again."}
    record = ir.read_record(layout, state["active"])
    rows = _component_rows(record)
    manifests = components if components is not None else load_components()
    listed = []
    for manifest in manifests:
        row = rows.get(manifest["id"])
        if row is None:
            listed.append({"id": manifest["id"], "name": manifest.get("name") or manifest["id"], "state": "not-installed",
                           "enabled": False, "reason": "not part of this installation", "toggleable": False})
            continue
        installed = row.get("state") == "installed"
        listed.append({"id": manifest["id"], "name": manifest.get("name") or manifest["id"], "state": row.get("state"),
                       "enabled": installed and _is_enabled(row, saved), "reason": row.get("reason"),
                       "toggleable": installed and manifest.get("kind") != "core",
                       "detail": (saved.get(manifest["id"]) or {}).get("detail") or {}})
    return {"outcome": "ok", "home": str(base), "gen_id": state["active"], "components": listed}


def _refuse(reason: str, message: str, **extra) -> dict:
    return {"outcome": "refused", "reason": reason, "detail": message, "next_action": message, **extra}


def _record_follow_ups(base, manifest, enable: bool, kept, now) -> dict:
    """Put the steps a switch could not finish into ``settings/pending-actions.json`` for the next agent: an app
    restart the part needs (``COMPONENT_RESTARTS``) and, on a disable, changes that were kept. Entries an earlier
    switch of this part left are dismissed first (they described the other state). The switch itself has already
    succeeded, so a store that cannot be written only adds a note."""
    component_id, label = manifest["id"], manifest.get("name") or manifest["id"]
    source, ids, notes = f"component:{component_id}", [], []
    try:
        pending_actions.dismiss_source(base, source, note="superseded by a later switch", now=now)
        for app in COMPONENT_RESTARTS.get(component_id, ()):
            if app in pending_actions.HARNESS_NAMES:
                name = pending_actions.HARNESS_NAMES[app]
                ids.append(pending_actions.add(
                    base, source=source, harness=app, now=now,
                    action=f"Ask the person to restart {name} so it picks up {label} being switched "
                           f"{'on' if enable else 'off'}, then mark this done.",
                    why=f"{name} reads its plugins, hooks and tool servers when it starts.")["id"])
        if kept and not enable:
            shown = ", ".join(item["effect_id"] for item in kept[:3]) + (", ..." if len(kept) > 3 else "")
            reasons = "; ".join(f"{item['effect_id']}: {item.get('reason')}" for item in kept[:2])
            ids.append(pending_actions.add(
                base, source=source, harness="any", now=now,
                action=f"Finish switching {label} off: {len(kept)} change(s) could not be undone ({shown}). Look at "
                       "them, undo what is safe by hand or leave them, then mark this done.",
                why=reasons)["id"])
    except (pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        notes.append(f"The follow-up steps for {label} could not be saved ({exc}). Tell the person what is left: "
                     "restart the apps that use it" + ("; some changes were kept" if kept and not enable else "") + ".")
    return {"pending": ids, "notes": notes}


def set_component(component_id: str, enable: bool, *, home=None, user_home=None, os_name=None, ports=None, now=None,
                  components=None, resolver=None) -> dict:
    """Switch one part on or off after install, mechanically and through the same code install uses.

    Disable undoes the part's Effects outside the EUTONOS home (Startup items, scheduled tasks, permission entries, its
    instruction block, ...); enable applies them again. The part's kit and files inside the home stay. Both record
    the change in the install record (undone Effects leave it, new ones join it), so uninstall reverses exactly what is
    present, and in ``settings/components.json``. Asking for the state a part already has changes nothing. Core, held,
    unavailable and unknown parts are refused."""
    base = resolve_home(home)
    run = Run(base, kind="toggle", ports=ports, user_home=user_home, os_name=os_name, now=now, manifests=components,
              resolver=resolver)
    layout = run.layout
    verb = "enable" if enable else "disable"
    try:
        with sc.machine_lock(base, wait=0):
            state = ir.read_state(layout)
            if state.get("uninstall"):
                return _refuse("uninstall-incomplete", "Finish the incomplete removal: eutonos-install uninstall")
            if state["pending"]:
                return _refuse("pending", "An earlier install or update did not finish. Run: eutonos-install resume",
                               gen_id=state["pending"]["gen_id"])
            if not state["active"]:
                return _refuse("not-installed", "EUTONOS is not installed here. Install first: eutonos-install install")
            manifest = run.component(component_id)
            if manifest is None:
                names = ", ".join(sorted(item["id"] for item in run.components()))
                return _refuse("unknown-component", f"There is no part called {component_id}. The parts are: {names}.")
            record = ir.read_record(layout, state["active"])
            rows = _component_rows(record)
            row = rows.get(component_id)
            if manifest.get("kind") == "core":
                return _refuse("core-always-on", f"{component_id} is the EUTONOS core and is always on.")
            if row is None or row.get("state") != "installed":
                kind = "not-installed" if row is None else row.get("state")
                why = (row or {}).get("reason") or "it is not part of this installation"
                return _refuse("held" if kind == "held" else "not-installed",
                               f"{component_id} is not installed here ({kind}: {why}), so it cannot be switched.")
            try:
                saved = component_state.read(base)
            except component_state.ComponentStateError as exc:
                return _refuse("state-unreadable", f"{exc}. Repair or delete that file, then try again.")
            current = _is_enabled(row, saved)
            if current == enable:
                return {"outcome": "unchanged", "component": component_id, "enabled": current,
                        "detail": f"{component_id} is already {'on' if enable else 'off'}."}
            if enable:
                lacking = [name for name in ((manifest.get("requires") or {}).get("components") or [])
                           if name in rows and rows[name].get("state") == "installed" and not _is_enabled(rows[name], saved)]
                if lacking:
                    return _refuse("needs-disabled-part", f"{component_id} needs {', '.join(lacking)}, which is switched "
                                   f"off. Switch {lacking[0]} on first.", needs=lacking)
            else:
                users = [name for name, other in rows.items() if name != component_id and other.get("state") == "installed"
                         and _is_enabled(other, saved)
                         and component_id in (((run.component(name) or {}).get("requires") or {}).get("components") or [])]
                if users:
                    return _refuse("needed-by", f"{', '.join(users)} needs {component_id}. Switch {users[0]} off first.",
                                   needed_by=users)
            run.state, run.record, run.txn = state, record, ir.new_txn_id()
            run.info = {"artifact_id": record["wheel"]["artifact_id"], "version": record["eutonos_version"]}
            run.journal({"event": "begin", "kind": "component-" + verb, "component": component_id, "gen_id": record["gen_id"]})
            detail = (saved.get(component_id) or {}).get("detail") or {}
            try:
                if enable:
                    done = enable_component(run, manifest, detail)
                    new_detail, kept = {}, []
                else:
                    done = disable_component(run, manifest, detail)
                    new_detail, kept = done["detail"], done["kept"]
            except Exception as exc:
                run.journal({"event": "failed", "phase": verb, "error": f"{type(exc).__name__}: {exc}"})
                return {"outcome": "failed", "component": component_id, "error": f"{type(exc).__name__}: {exc}",
                        "next_action": f"Fix the cause, then run: eutonos-install component {verb} {component_id} "
                                       "(it picks up where this stopped)."}
            row["enabled"] = bool(enable)
            row["reason"] = None if enable else "turned off"
            record["components"] = list(rows.values())
            run.save()
            component_state.set_enabled(base, component_id, enable, detail=new_detail, now=now)
            run.journal({"event": "component", "component": component_id, "enabled": bool(enable)})
            follow = _record_follow_ups(base, manifest, enable, kept, now)
            answer = {"outcome": "enabled" if enable else "disabled", "component": component_id, "enabled": bool(enable),
                      "effects": len(record["effects"]), "notes": list(done.get("notes") or []) + run.notes + follow["notes"]}
            if follow["pending"]:
                answer["pending"] = follow["pending"]
            if enable:
                answer.update(applied=done["applied"], blocks=done["blocks"])
            else:
                answer.update(removed=done["removed"], kept=kept)
                if new_detail:
                    answer["how"] = new_detail
                if kept:
                    answer["next_action"] = ("Some changes could not be undone and stay recorded (they were changed "
                                             "by you, or an app refused); uninstall tries them again.")
            return answer
    except sc.LifecycleBusy as exc:
        return {"outcome": "busy", "reason": str(exc), "next_action": "Wait for the other operation to finish."}
    except component_state.ComponentStateError as exc:
        return {"outcome": "refused", "reason": "state-unreadable", "detail": str(exc), "next_action": str(exc)}


def reconcile_components(run: Run) -> dict:
    """Make the machine match what the person chose, for the record in ``run.record`` (used after a rollback, whose
    earlier generation may remember a part's Effects differently from how the person left it). A part the person
    switched with ``component enable`` or ``disable`` keeps that state; any other part takes the state its record
    holds, and the file is brought into line with it. Parts the file does not name are left alone."""
    saved = component_state.read(run.base)
    result = {"enabled": [], "disabled": [], "kept": []}
    if not saved:
        return result
    rows = _component_rows(run.record)
    for component_id, row in rows.items():
        manifest = run.component(component_id)
        entry = saved.get(component_id)
        if manifest is None or entry is None or row.get("state") != "installed" or manifest.get("kind") == "core":
            continue
        if entry["source"] != "toggle":
            if entry["enabled"] != bool(row.get("enabled", True)):
                component_state.set_enabled(run.base, component_id, bool(row.get("enabled", True)), source="install",
                                            now=run.now)
            continue
        if entry["enabled"]:
            enable_component(run, manifest, entry.get("detail"))
            result["enabled"].append(component_id)
            follow = _record_follow_ups(run.base, manifest, True, [], run.now)
        else:
            done = disable_component(run, manifest, entry.get("detail"))
            result["disabled"].append(component_id)
            result["kept"] += done["kept"]
            component_state.set_enabled(run.base, component_id, False, detail=done["detail"] or entry.get("detail"),
                                        now=run.now)
            follow = _record_follow_ups(run.base, manifest, False, done["kept"], run.now)
        # The same steps a switch leaves (restart the apps that use the part, changes that were kept), so a rollback
        # that re-applies a part does not leave the next agent without them.
        if follow["pending"]:
            result.setdefault("pending", []).extend(follow["pending"])
        if follow["notes"]:
            result.setdefault("notes", []).extend(follow["notes"])
        row["enabled"] = bool(entry["enabled"])
        row["reason"] = None if entry["enabled"] else "turned off"
    run.record["components"] = list(rows.values())
    run.save()
    return result


# ------------------------------------------------------------------------ status
def status(*, home=None) -> dict:
    base = resolve_home(home)
    layout = ir.Layout(base)
    state = ir.read_state(layout)
    result = {"home": str(base), "active": state["active"], "pending": None, "channel": state["channel"],
              "generations": sorted(state["generations"]), "legacy": state.get("legacy"),
              "uninstall_incomplete": bool(state.get("uninstall")),
              "pending_actions": pending_actions.pickup_hint(base), "pending_delivery": pending_actions.delivery_status()}
    if state["active"]:
        record = ir.read_record(layout, state["active"])
        result["active_record"] = {"gen_id": record["gen_id"], "version": record["eutonos_version"],
                                   "artifact_id": record["wheel"]["artifact_id"], "effects": len(record["effects"]),
                                   "intact": ir.record_intact(layout, state, state["active"]),
                                   "three_way": len(record.get("three_way") or [])}
        line = sr.patch_line(record.get("local_patch"))
        if line:
            result["active_record"]["local_patch"] = sr.patch_summary(record["local_patch"])
            result["local_patch_line"] = line
    if state["pending"]:
        result["pending"] = recovery_report(layout, state)
    try:
        checkpoint = sc.read(base)
    except sc.CheckpointError as exc:
        checkpoint = {"error": str(exc)}
    if checkpoint:
        result["checkpoint"] = {"checklist": checkpoint.get("checklist"), "counts": sc.summary(checkpoint),
                                "next_open": (sc.next_open_step(checkpoint) or {}).get("id")}
    return result


# --------------------------------------------------------------------------- CLI
def _emit(value: dict) -> int:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))
    bad = {"refused", "rolled-back", "failed", "busy", "consent-refused", "prereq-missing", "unavailable"}
    return 2 if value.get("outcome") in ("consent-refused", "prereq-missing") else 1 if value.get("outcome") in bad else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eutonos-install", description=__doc__.split("\n\n")[0])
    parser.add_argument("--home", help="the EUTONOS home (default: the per-user EUTONOS home, or EUTONOS_HOME for tests)")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("install", help="install from a wheel")
    one.add_argument("--wheel", required=True)
    one.add_argument("--adopt", action="store_true", help="adopt an earlier hand install")
    one.add_argument("--dry-run", action="store_true")
    one.add_argument("--owner-profile")
    one.add_argument("--channel", choices=ir.CHANNELS)
    one.add_argument("--for-this-user", action="store_true",
                     help="a home other than the per-user default is the real install for this user (not a staging home)")
    one.add_argument("--consent-file", help="consent the Setup wizard took (checked against this computer's own text)")
    one.add_argument("--component", action="append", default=[], metavar="ID=on|off",
                     help="turn an optional part on or off")
    one.add_argument("--progress", action="store_true", help="print one EUTONOS-STEP line as each phase starts")
    gone = sub.add_parser("uninstall", help="reverse the installation")
    gone.add_argument("--keep-projects", action="store_true")
    gone.add_argument("--dry-run", action="store_true")
    sub.add_parser("resume", help="continue an interrupted install or update")
    new = sub.add_parser("update", help="update to the newest release of this machine's channel")
    new.add_argument("--source", help="a release folder (default: the public GitHub releases repository)")
    new.add_argument("--channel", choices=ir.CHANNELS, help="switch to this channel first")
    new.add_argument("--dry-run", action="store_true", help="show the release found; change nothing")
    new.add_argument("--allow-unsigned", action="store_true",
                     help="developer override for a --source folder: accept a release that has no signature")
    new.add_argument("--check", action="store_true",
                     help="only say whether a newer release is available (downloads no wheel, changes nothing)")
    new.add_argument("--staged", action="store_true",
                     help="install the release the companion already downloaded and verified (checked again here)")
    back = sub.add_parser("rollback", help="re-activate the previous generation")
    back.add_argument("--dry-run", action="store_true")
    back.add_argument("--official", action="store_true",
                      help="only leave a local patch for the official generation before it")
    fix = sub.add_parser("patch", help="install a local patch built from a checkout (a new generation, marked local)")
    fix.add_argument("--checkout", required=True, help="the EUTONOS checkout to build the patch from")
    fix.add_argument("--reason", required=True, help="what is broken and what the patch fixes")
    fix.add_argument("--allow-dirty", action="store_true", help="build from uncommitted changes (recorded as dirty)")
    fix.add_argument("--dry-run", action="store_true", help="build in a scratch folder and show the plan; change nothing")
    check = sub.add_parser("self-check", help="read-only: what changed in the apps and what EUTONOS piece likely broke")
    check.add_argument("--user-home", help="Claude and Codex configuration home (tests)")
    check.add_argument("--json", action="store_true", help="print the full JSON report (default is a short text)")
    cont = sub.add_parser("continue", help="continue a pending generation inside the new runtime (internal)")
    cont.add_argument("--gen-id", required=True)
    cont.add_argument("--token")
    sub.add_parser("status", help="show the active generation, a pending run and the checkpoint")
    part = sub.add_parser("component", help="show the parts and switch one on or off after install")
    part.add_argument("action", choices=("status", "enable", "disable"))
    part.add_argument("component", nargs="?", metavar="ID", help="the part (for enable and disable)")
    part.add_argument("--json", action="store_true", help="status: print the full JSON (default is a short text)")
    knob = sub.add_parser("setting", help="show or change Crosstalk messaging and switches, RAM and linked Claude accounts")
    knob.add_argument("action", choices=("status", "set", "suggest"))
    knob.add_argument("name", nargs="?", metavar="NAME",
                      help="crosstalk-messaging, crosstalk-switch KEY VALUE, crosstalk-everything off, ram, ram-capture, "
                           "ram-retrieval, linked-accounts, update-mode (manual, notify or auto) or update-notice (on or "
                           "off) (for set)")
    knob.add_argument("values", nargs="*", metavar="VALUE",
                      help="on or off; for linked-accounts, your own Claude emails or account IDs (or off for none)")
    knob.add_argument("--root", help="the project checkout the RAM settings belong to (default: the current folder)")
    knob.add_argument("--json", action="store_true", help="status: print the full JSON (default is a short text)")
    wait = sub.add_parser("pending", help="steps a switch could not finish itself, left for the next agent")
    wait.add_argument("action", choices=("list", "done", "dismiss"))
    wait.add_argument("id", nargs="?", metavar="ID", help="the pending action (for done and dismiss)")
    wait.add_argument("--harness", choices=pending_actions.HARNESSES[:-1],
                      help="list: only the steps for this app (and the ones for any app)")
    wait.add_argument("--all", action="store_true", help="list: include done and dismissed steps")
    wait.add_argument("--by", help="done: who carried it out (your chat or agent name)")
    wait.add_argument("--note", help="dismiss: why it is not needed")
    wait.add_argument("--json", action="store_true", help="list: print the full JSON (default is a short text)")
    point = sub.add_parser("checkpoint", help="show or record the setup checkpoint")
    point.add_argument("action", choices=("show", "record"))
    point.add_argument("--step")
    point.add_argument("--state", choices=sc.STATES)
    point.add_argument("--evidence", action="append", default=[])
    point.add_argument("--reason")
    point.add_argument("--next-action")
    return parser


def _install_cli(args, home) -> int:
    choices = {}
    for item in args.component:
        name, _, value = item.partition("=")
        if not name or value not in ("on", "off"):
            print(json.dumps({"outcome": "error", "error": f"--component needs ID=on or ID=off, not {item!r}"}))
            return 2
        choices[name] = value
    global PROGRESS
    if args.progress:
        def say(phase, number, total):
            print(f"EUTONOS-STEP|{number}|{total}|{PHASE_TEXT.get(phase, phase)}", flush=True)
        PROGRESS = say
    try:
        return _emit(install(args.wheel, home=home, adopt=args.adopt, dry_run=args.dry_run,
                             owner_profile=args.owner_profile, channel=args.channel, component_choices=choices or None,
                             for_this_user=args.for_this_user, consent_file=args.consent_file))
    finally:
        PROGRESS = None


def format_components(found: dict) -> str:
    lines = []
    for item in found["components"]:
        mark = "on" if item["enabled"] else "off" if item["state"] == "installed" else item["state"]
        extra = f"  ({item['reason']})" if item.get("reason") and mark != "on" else ""
        lines.append(f"{item['id']:<16} {mark:<14} {item['name']}{extra}")
    return "\n".join(lines) + "\n"


def _component_cli(args, home) -> int:
    if args.action == "status":
        found = component_status(home=home)
        if args.json or found["outcome"] != "ok":
            return _emit(found)
        sys.stdout.write(format_components(found))
        return 0
    if not args.component:
        print(json.dumps({"outcome": "error", "error": f"component {args.action} needs a part ID"}))
        return 2
    return _emit(set_component(args.component, args.action == "enable", home=home))


def format_settings(found: dict) -> str:
    lines = []
    for name, item in found["settings"].items():
        if item["outcome"] != "ok":
            lines.append(f"{name:<20} {item['outcome']:<8} {item.get('detail') or item.get('reason')}")
        elif name == "linked-accounts":
            lines.append(f"{name:<20} {'on' if item['enabled'] else 'off':<8} " + (", ".join(item["accounts"]) or "no accounts linked"))
        elif name == "crosstalk-switches":
            rows = [row for row in item["switches"] if row["kind"] == "bool" and row["available"]]
            on = [row["key"] for row in rows if row["value"]]
            lines.append(f"{name:<20} {len(on)} of {len(rows)} on" + (": " + ", ".join(on) if on else ""))
        elif name == "update-mode":
            lines.append(f"{name:<20} {item['mode']:<8} asks {item['repo']}"
                         + (f"; last checked {item['last_checked']}" if item.get("last_checked") else "; never checked"))
        elif name == "ram":
            lines.append(f"{name:<20} {'on' if item['enabled'] else 'off':<8} capture {'on' if item['capture'] else 'off'}, "
                         f"retrieval {item['retrieval']} (this project)")
        else:
            lines.append(f"{name:<20} {'on' if item['enabled'] else 'off':<8}" + ("  (halted)" if item.get("halt") else ""))
    return "\n".join(lines) + "\n"


def _record_setting_follow_ups(base, name: str, answer: dict, now=None) -> dict:
    """What ``setting set`` could not finish itself, for the next agent (``settings/pending-actions.json``), as
    ``_record_follow_ups`` does for a part. The source is ``setting:NAME``; the steps an earlier call left are
    dismissed first when this call reports a state (they described the earlier one), and a step already pending is
    never added twice. Crosstalk messaging switched on does not make letters flow while the owner's stop is set or
    the Crosstalk part is off; a failed change is left for a retry. A refused or busy call touched nothing and leaves
    every entry as it was. The change itself has already happened, so a store that cannot be written only adds a note."""
    outcome = answer.get("outcome")
    source, ids, notes = f"setting:{name}", [], []
    if outcome not in ("enabled", "disabled", "changed", "unchanged", "failed"):
        return {"pending": ids, "notes": notes}
    steps = []
    if outcome == "failed":
        steps.append(("any", f"Apply the {name} setting again once the cause is fixed: {answer.get('error') or 'it failed'}. "
                             "Then mark this done.", "The setting change did not finish."))
    elif name == "crosstalk-messaging" and answer.get("enabled"):
        if answer.get("halt"):
            steps.append(("any", "Tell the person that Crosstalk is halted (their own stop), so letters do not flow although "
                                 "messaging is on; only they clear the halt. Then mark this done.",
                          "The owner's stop overrides the messaging switch."))
        try:
            part_on = component_state.component_enabled(base, "crosstalk")
        except component_state.ComponentStateError:
            part_on = True  # unknown is not a reason to ask for a switch
        if not part_on:
            steps.append(("any", "Switch the Crosstalk part on with: eutonos-install component enable crosstalk. "
                                 "Then mark this done.", "Messaging is on, but the Crosstalk part is switched off, so "
                                 "letters still do not flow."))
    try:
        if outcome != "failed":
            pending_actions.dismiss_source(base, source, note="superseded by a later change", now=now)
        for harness, action, why in steps:
            ids.append(pending_actions.add(base, source=source, harness=harness, action=action, why=why, now=now)["id"])
    except (pending_actions.PendingActionsError, sc.LifecycleBusy, OSError) as exc:
        notes.append(f"The follow-up steps for {name} could not be saved ({exc}). Tell the person what is left.")
    return {"pending": ids, "notes": notes}


def _update_cli(args, home) -> int:
    """``update`` and ``update --staged`` (the staging folder is an ordinary release folder, so the signature and the
    sha256 are checked again here). A finished update clears the offer, the pending action and the staged copy."""
    from . import update_stage
    source = args.source
    if args.staged:
        if args.source or args.allow_unsigned:
            return _emit({"outcome": "refused", "reason": "invalid",
                          "detail": "--staged takes no --source and no --allow-unsigned.",
                          "next_action": "Run: eutonos-install update --staged"})
        base = resolve_home(home)
        if not update_stage.releases(base):
            return _emit({"outcome": "refused", "reason": "nothing-staged",
                          "detail": "No verified release is staged on this computer.",
                          "next_action": "Run: eutonos-install update (it downloads the newest release)."})
        source = str(update_stage.staged_root(base))
    answer = update(source=source, home=home, channel=args.channel, dry_run=args.dry_run, allow_unsigned=args.allow_unsigned)
    if answer.get("outcome") == "updated":
        answer["offer"] = uc.after_update(home=resolve_home(home))
    return _emit(answer)


def _setting_cli(args, home) -> int:
    from . import feature_settings as fs
    base = resolve_home(home)
    if args.action == "status":
        found = fs.status(home=base, root=args.root)
        found["settings"][uc.MODE_SETTING] = uc.status(base)
        if args.json:
            return _emit(found)
        sys.stdout.write(format_settings(found))
        return 0
    if args.action == "suggest":
        return _emit(fs.suggest_linked_accounts(home=base))
    if not args.name:
        print(json.dumps({"outcome": "error", "error": "setting set needs a setting name"}))
        return 2
    if args.name == uc.NOTICE_SETTING:
        return _emit(uc.set_notice(args.values[0] if len(args.values) == 1 else None, home=base))
    if args.name == uc.MODE_SETTING:
        return _emit(uc.set_mode(args.values[0] if len(args.values) == 1 else None, home=base))
    answer = fs.set_setting(args.name, args.values, home=base, root=args.root)
    follow = _record_setting_follow_ups(base, args.name, answer)
    if follow["pending"]:
        answer["pending"] = follow["pending"]
    if follow["notes"]:
        answer["notes"] = list(answer.get("notes") or []) + follow["notes"]
    return _emit(answer)


def format_pending(entries: list) -> str:
    if not entries:
        return "Nothing is waiting for an agent.\n"
    lines = []
    for item in entries:
        mark = "" if item["state"] == "pending" else f" ({item['state']})"
        lines.append(f"{item['id']}  [{item['harness']}]  {item['action']}{mark}")
        if item.get("why"):
            lines.append(f"    why: {item['why']}  (from {item['source']})")
    return "\n".join(lines) + "\n"


def _pending_cli(args, home) -> int:
    base = resolve_home(home)
    try:
        if args.action == "list":
            entries = pending_actions.list_actions(base, harness=args.harness, state="all" if args.all else "pending")
            if args.json:
                return _emit({"outcome": "ok", "count": len(entries), "actions": entries,
                              "delivery": pending_actions.delivery_status()})
            sys.stdout.write(format_pending(entries))
            if entries:
                sys.stdout.write("Delivery: " + pending_actions.delivery_status()["reason"] + "\n")
            return 0
        if not args.id:
            print(json.dumps({"outcome": "error", "error": f"pending {args.action} needs an action ID"}))
            return 2
        if args.action == "done":
            entry = pending_actions.complete(base, args.id, by=args.by)
        else:
            entry = pending_actions.dismiss(base, args.id, note=args.note)
        return _emit({"outcome": "done" if args.action == "done" else "dismissed", "action": entry})
    except pending_actions.UnknownAction as exc:
        return _emit({"outcome": "refused", "reason": "unknown-action", "detail": str(exc),
                      "next_action": "Run: eutonos-install pending list --all"})
    except (pending_actions.PendingActionsError, sc.LifecycleBusy) as exc:
        return _emit({"outcome": "refused", "reason": "state-unreadable", "detail": str(exc),
                      "next_action": f"Repair or delete {pending_actions.path(base)}, then ask again."})


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    home = args.home
    try:
        if args.command == "install":
            return _install_cli(args, home)
        if args.command == "uninstall":
            return _emit(uninstall(home=home, keep_projects=args.keep_projects, dry_run=args.dry_run))
        if args.command == "update":
            if args.check:
                return _emit(check_update(source=args.source, home=home, channel=args.channel,
                                          allow_unsigned=args.allow_unsigned))
            return _update_cli(args, home)
        if args.command == "rollback":
            return _emit(rollback(home=home, dry_run=args.dry_run, official=args.official))
        if args.command == "patch":
            return _emit(patch(checkout=args.checkout, reason=args.reason, allow_dirty=args.allow_dirty, home=home,
                               dry_run=args.dry_run))
        if args.command == "self-check":
            found = sr.self_check(home=resolve_home(home), user_home=args.user_home)
            if args.json:
                return _emit(found)
            sys.stdout.write(sr.format_check(found))
            return 0
        if args.command == "continue":
            return _emit(continue_handover(gen_id=args.gen_id, token=args.token, home=home))
        if args.command == "resume":
            return _emit(resume(home=home))
        if args.command == "status":
            return _emit(status(home=home))
        if args.command == "component":
            return _component_cli(args, home)
        if args.command == "setting":
            return _setting_cli(args, home)
        if args.command == "pending":
            return _pending_cli(args, home)
        if args.command == "checkpoint":
            base = resolve_home(home)
            if args.action == "show":
                return _emit(sc.read(base) or {"outcome": "none"})
            if not args.step or not args.state:
                print("checkpoint record needs --step and --state", file=sys.stderr)
                return 2
            return _emit(sc.record_step(args.step, args.state, args.evidence, args.reason, args.next_action, home=base))
    except (ts.ReadError, sc.CheckpointError, ValueError, OSError) as exc:
        print(json.dumps({"outcome": "error", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
