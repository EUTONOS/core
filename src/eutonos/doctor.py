"""Read-only install and setup report for EUTONOS."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

from . import completion_policy, consent, doctor_outside, home as eutonos_home, permissions, prereq, self_repair
from .hidden_process import no_window

PROFILE = "eutonos.doctor.v1"
BASELINE_PROFILE = "eutonos.doctor-baseline.v1"
MAX_JSON_BYTES = 2_000_000
MAX_EFFECT_BYTES = 16_000_000
MAX_INVENTORY_BYTES = 16_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SECRET_NAMES = {".credentials.json", "auth.json", "pipe.key", "auth.lock", "cookies", "tokens"}
_SECRET_DIRS = {".sandbox-secrets", ".credentials", "credentials", "tokens"}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _secret_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return (name in _SECRET_NAMES or name == ".env" or name.startswith(".env.") or name.endswith(".key")
            or bool(parts & _SECRET_DIRS) or "hosts" in parts)


def _read_bytes(path, limit: int, *, avoid_secrets: bool = True) -> tuple[str, bytes | None, str | None]:
    try:
        path = Path(path)
    except (TypeError, ValueError):
        return "unknown", None, "invalid path"
    if avoid_secrets and _secret_path(path):
        return "unknown", None, "protected secret path was not opened"
    try:
        if not path.exists():
            return "missing", None, None
        if path.is_symlink() or not path.is_file():
            return "unknown", None, "not an ordinary file"
        size = path.stat().st_size
        if size > limit:
            return "unknown", None, f"file exceeds the {limit}-byte read limit"
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return "unknown", None, f"file exceeds the {limit}-byte read limit"
        return "ok", raw, None
    except (OSError, ValueError) as exc:
        return "unknown", None, f"read failed ({type(exc).__name__})"


def _read_json(path, limit: int = MAX_JSON_BYTES) -> tuple[str, dict | None, str | None, bytes | None]:
    state, raw, reason = _read_bytes(path, limit)
    if state != "ok":
        return state, None, reason, raw
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return "unknown", None, "malformed JSON", raw
    if not isinstance(value, dict):
        return "unknown", None, "expected a JSON object", raw
    return "ok", value, None, raw


def _normal_path(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(value)))
    except (OSError, ValueError):
        try:
            return os.path.normcase(os.path.normpath(os.path.abspath(value)))
        except (OSError, ValueError):
            return None


def _load_active_record(home: Path) -> dict:
    state_path = home / "install" / "state.json"
    state_status, state, reason, _ = _read_json(state_path)
    if state_status == "missing":
        return {"state": "missing", "reason": "install state is absent", "record": None, "effects": []}
    if state_status != "ok":
        return {"state": "unknown", "reason": reason, "record": None, "effects": []}
    if state.get("profile") != "eutonos.install.state.v1":
        return {"state": "unknown", "reason": "unsupported install state profile", "record": None, "effects": []}
    gen_id = state.get("active")
    if gen_id is None:
        return {"state": "missing", "reason": "no active generation is selected", "record": None, "effects": []}
    if not isinstance(gen_id, str) or not re.fullmatch(r"[0-9a-f]{16}", gen_id):
        return {"state": "unknown", "reason": "active generation id is malformed", "record": None, "effects": []}
    expected = (state.get("generations") or {}).get(gen_id) if isinstance(state.get("generations"), dict) else None
    record_path = home / "install" / "generations" / gen_id / "record.json"
    record_status, record, record_reason, raw = _read_json(record_path, MAX_EFFECT_BYTES)
    if record_status != "ok":
        return {"state": "unknown", "reason": record_reason or "active record is absent", "record": None, "effects": []}
    if expected != _digest(raw):
        return {"state": "unknown", "reason": "active record hash does not match install state", "record": None, "effects": []}
    if (record.get("profile") != "eutonos.install.record.v1" or record.get("gen_id") != gen_id
            or record.get("state") != "active" or not isinstance(record.get("effects"), list)):
        return {"state": "unknown", "reason": "active record is malformed or inconsistent", "record": None, "effects": []}
    effects = [effect for effect in record["effects"] if isinstance(effect, dict)]
    if len(effects) != len(record["effects"]):
        return {"state": "unknown", "reason": "active record contains a malformed Effect", "record": record, "effects": []}
    return {"state": "ok", "reason": None, "record": record, "effects": effects}


def _block_readback(raw: bytes, effect: dict) -> tuple[str, str | None, str | None]:
    """``(state, reason, current_block_sha256)`` for one managed instruction block (a ``file-block`` Effect), read by
    its own markers. Several blocks share one file and each was recorded with the whole file as it stood right after
    that block went in, so the whole-file digest only ever matches the block written last; this looks at the block
    itself and leaves the rest of the file (the person's own text, the other blocks) out of it.

    ``ok``: the block is there once, intact (its body still matches the digest in its start marker) and equals the
    text the record says EUTONOS shipped (line endings ignored), or is an exact copy of the person's that EUTONOS
    wrapped in markers (the record says ``adoption: wrap``, and its text stays the person's own).
    ``missing``: the file has no such block. ``changed by you``: the block was edited inside, is not the shipped
    text, or appears more than once or half-marked."""
    selector = effect.get("selector")
    name = selector.split(":", 1)[1] if isinstance(selector, str) and selector.startswith("eutonos:") else ""
    if not name:
        return "unknown", "the Effect does not name its block", None
    start, end, pattern = completion_policy.markers(name)
    matches = list(pattern.finditer(raw))
    if raw.count(start) != len(matches) or raw.count(end) != len(matches) or len(matches) > 1:
        return "changed by you", "the managed block is half-marked or appears more than once", None
    if not matches:
        return "missing", "the managed block is not in the target", None
    found = matches[0]
    if completion_policy.sha(found[2].replace(b"\r\n", b"\n")) != found[1].decode():
        return "changed by you", "the managed block was edited inside", None
    actual = completion_policy.sha(found[0].replace(b"\r\n", b"\n"))
    expected = effect.get("shipped_sha256")
    if expected is None or actual == expected or effect.get("adoption") == "wrap":
        return "ok", None, actual
    return "changed by you", "the managed block is not the text EUTONOS recorded", actual


def _registry_effect(effect, ports=None) -> dict:
    """Read only the declared HKCU environment values; never interpret an arbitrary registry target."""
    from . import installer_effects as fx, installer_ports
    ports = ports or installer_ports.Ports("windows")
    undo = effect.get("undo") or {}
    try:
        if effect.get("kind") == "env-path" and effect.get("target") == r"reg:HKCU\Environment\Path" and undo.get("entry"):
            value = ports.user_path_get()
            present = any(fx._same_entry(part, undo["entry"]) for part in (value or "").split(";") if part.strip())
        elif effect.get("kind") == "env-var" and undo.get("name") == "EUTONOS_HOME" and effect.get("target") == r"reg:HKCU\Environment\EUTONOS_HOME":
            value = ports.user_env_get("EUTONOS_HOME")
            present = value is not None and fx._same_entry(value, undo.get("value") or "")
        else:
            return {"state": "unknown", "reason": "registry target has no declared readback"}
        return {"state": "ok" if present else "missing" if value is None else "changed by you",
                "reason": None if present else "the recorded environment entry is absent"}
    except (OSError, ImportError, ValueError) as exc:
        return {"state": "unknown", "reason": f"environment readback failed ({type(exc).__name__})"}


def _effect_report(effects: list[dict], *, ports=None, components=None) -> list[dict]:
    owners = {item.get("id"): item for item in components or [] if isinstance(item, dict)}
    result = []
    for effect in effects:
        target = effect.get("target")
        item = {"effect_id": effect.get("effect_id"), "component": effect.get("component"),
                "permission_entry": effect.get("permission_entry"), "target": target}
        if not isinstance(target, str) or target.startswith("app:"):
            owner = owners.get(effect.get("component"))
            if isinstance(target, str) and owner and owner.get("state") == "ok":
                # A scheduled task or app command has no bytes to hash; its part's own verify reads it back.
                item.update(state="ok", reason=f"read back by the {effect.get('component')} part's verify")
            else:
                item.update(state="unknown", reason="app command or invalid target has no byte readback"
                            + (f" (the {effect.get('component')} part reports {owner.get('state')})" if owner else ""))
            result.append(item)
            continue
        if target.startswith("reg:"):
            item.update(_registry_effect(effect, ports))
            result.append(item)
            continue
        path = Path(target)
        if not path.is_absolute() or _secret_path(path):
            item.update(state="unknown", reason="target is not an eligible ordinary path")
            result.append(item)
            continue
        state, raw, reason = _read_bytes(path, MAX_EFFECT_BYTES)
        if state == "missing":
            item.update(state="missing", reason="recorded target is absent")
        elif state != "ok":
            item.update(state="unknown", reason=reason)
        elif effect.get("kind") == "shortcut" and (effect.get("undo") or {}).get("raw_after")                 and not (effect.get("undo") or {}).get("raw_pending"):
            # after_sha256 is the hash of the canonical {target, arguments, cwd}; the .lnk's own bytes are in undo.raw_after.
            actual, expected = _digest(raw), effect["undo"]["raw_after"]
            item.update(state="ok" if actual == expected else "changed by you", current_sha256=actual,
                        expected_sha256=expected,
                        reason=None if actual == expected else "the Startup shortcut differs from what EUTONOS wrote")
        elif effect.get("kind") == "file-block":
            block_state, block_reason, block_sha = _block_readback(raw, effect)
            item.update(state=block_state, reason=block_reason, current_sha256=block_sha,
                        expected_sha256=effect.get("shipped_sha256"))
        else:
            actual = _digest(raw)
            expected = effect.get("after_sha256")
            item.update(state="ok" if actual == expected else "changed by you",
                        current_sha256=actual, expected_sha256=expected,
                        reason=None if actual == expected else "target bytes differ from the active Effect")
        result.append(item)
    return result


def _permission_states(plan: dict) -> dict[str, tuple[str, str | None]]:
    states: dict[str, tuple[str, str | None]] = {}
    for file in plan.get("files", []):
        for entry in file.get("additions", []):
            states[entry["entry"]] = ("missing", "the declared setting is not present")
        for entry in file.get("satisfied", []):
            states[entry] = ("ok", None)
        for entry in file.get("kept", []):
            states[entry["entry"]] = ("changed by you", entry.get("reason") or "a different setting was preserved")
        for entry in file.get("blocked", []):
            states[entry["entry"]] = ("unknown", entry.get("reason") or entry.get("status") or "readback unavailable")
    return states


def _permissions_report(root: Path, user_home: Path, effects: list[dict], record: dict | None, outside: dict | None = None) -> dict:
    try:
        data = permissions.load()
        receipt = record.get("consent") if isinstance(record, dict) and isinstance(record.get("consent"), dict) else {}
        choices = receipt.get("choices") if isinstance(receipt.get("choices"), dict) else None
        planned = permissions.plan(root, user_home, choices=choices, data=data)
        chosen = permissions.active_choices(data, choices)
    except Exception as exc:
        return {"state": "unknown", "reason": f"permissions plan unavailable ({type(exc).__name__})", "items": []}
    readbacks = {}
    try:
        desktop_kwargs = {"os_name": "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else None}
        if os.name == "nt":
            desktop_kwargs["local_app_data"] = user_home / "AppData" / "Local"
        readbacks["desktop_auto"] = permissions.desktop_session_readback(root, home=user_home, **desktop_kwargs)
    except Exception as exc:
        readbacks["desktop_auto"] = {"state": "unknown", "reason": type(exc).__name__}
    try:
        readbacks["codex_trust"] = permissions.codex_trust_readback(root, user_home)
    except Exception as exc:
        readbacks["codex_trust"] = {"state": "unknown", "reason": type(exc).__name__}
    states = _permission_states(planned)
    effect_by_permission = {}
    for effect in effects:
        if isinstance(effect.get("permission_entry"), str):
            effect_by_permission[effect["permission_entry"]] = effect
    items = []
    for entry in permissions.entries(data):
        entry_id = entry["id"]
        selected = chosen.get(entry_id, entry.get("default")) == "on"
        reason = None
        if entry.get("default") == "held":
            state, reason = "held", "held by the permissions contract"
        elif not selected:
            state, reason = "skipped", "not selected by the recorded consent choices"
        elif entry.get("default") == "off" or entry.get("writer") is None:
            state, reason = "skipped", "not selected by the permissions contract"
        elif entry_id == "desktop-folder-auto":
            observed = readbacks["desktop_auto"]
            state = {"auto": "ok", "not-auto": "missing", "no-session": "unknown", "unavailable": "unknown"}.get(observed.get("state"), "unknown")
            reason = observed.get("reason") or ("Desktop session is not in Auto mode" if state == "missing" else None)
        elif entry_id == "codex-trust":
            observed = readbacks["codex_trust"]
            state = {"trusted": "ok", "untrusted": "missing", "absent": "missing", "unreadable": "unknown"}.get(observed.get("state"), "unknown")
            reason = observed.get("reason") or ("project is not trusted in Codex" if state == "missing" else None)
        elif entry_id in states:
            state, reason = states[entry_id]
        elif entry_id in (outside or {}):
            state, reason = outside[entry_id]["state"], outside[entry_id]["reason"]
        elif entry_id in ("claude-launch-auto", "codex-hook-trust", "claude-hooks", "codex-hooks"):
            state, reason = ("held", "held pending the corresponding chat or hook workflow") if entry_id != "claude-launch-auto" else ("unknown", "direct Crosstalk create has no enforced auto-mode check")
        else:
            state, reason = "unknown", "no readback is available for this declared item"
        row = {"id": entry_id, "app": entry.get("app"), "scope": entry.get("scope"),
               "feature": entry.get("feature"), "why": entry.get("why"), "required": bool(entry.get("required")),
               "selected": selected, "state": state, "reason": reason}
        if entry_id in (outside or {}):
            row["observed"] = outside[entry_id]  # read even when the entry is off: Setup or a runtime choice may have written it
        effect = effect_by_permission.get(entry_id)
        if effect:
            row["recorded_effect_id"] = effect.get("effect_id")
            effect_state = next((item["state"] for item in _effect_report([effect])), "unknown")
            row["effect_state"] = effect_state
            row["ownership"] = "recorded"
        else:
            row["ownership"] = "not-recorded"
        items.append(row)
    return {"state": "ok", "profile": planned.get("profile"), "items": items, "readbacks": readbacks,
            "notes": planned.get("notes", []), "guided": planned.get("guided", [])}


def _components_report(package_dir: Path, record: dict | None, home: Path) -> list[dict]:
    installed = record.get("components", []) if isinstance(record, dict) else []
    installed_by_id = {item.get("id"): item for item in installed if isinstance(item, dict) and isinstance(item.get("id"), str)}
    manifests = {}
    try:
        paths = sorted((package_dir / "components").glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        state, value, reason, _ = _read_json(path, 256_000)
        if state == "ok" and value.get("profile") == "eutonos.component.v1" and isinstance(value.get("id"), str):
            manifests[value["id"]] = value
        else:
            manifests[path.stem] = {"id": path.stem, "_invalid": reason or "unsupported component manifest"}
    ids = sorted(set(manifests) | set(installed_by_id))
    result = []
    for component_id in ids:
        manifest = manifests.get(component_id)
        installed_item = installed_by_id.get(component_id, {})
        if manifest is None:
            result.append({"id": component_id, "state": "unknown", "reason": "component manifest is not installed",
                           "required": installed_item.get("state") == "installed", "selected": installed_item.get("state") == "installed"})
            continue
        if manifest.get("_invalid"):
            result.append({"id": component_id, "state": "unknown", "reason": manifest["_invalid"], "required": False, "selected": False})
            continue
        if component_id in installed_by_id:
            selected = installed_item.get("state") in ("installed", "unlicensed") and installed_item.get("enabled") is not False
        else:
            selected = manifest.get("default") == "on"
        requires = manifest.get("requires") if isinstance(manifest.get("requires"), dict) else {}
        required = manifest.get("kind") == "core" or bool(requires.get("required"))
        default = manifest.get("default")
        if default == "held" or installed_item.get("state") == "held":
            gate = manifest.get("gate") if isinstance(manifest.get("gate"), dict) else {}
            state, reason = "held", installed_item.get("reason") or gate.get("reason") or "held by component manifest"
        elif installed_item.get("state") == "installed" and installed_item.get("enabled") is False:
            state, reason = "skipped", "turned off (installed, not switched on)"
        elif (default == "off" and installed_item.get("state") != "installed") \
                or installed_item.get("state") in ("skipped", "unlicensed"):
            state = "skipped" if installed_item.get("state") != "unlicensed" else "unlicensed"
            reason = installed_item.get("reason") or "not selected for this installation"
        else:
            entry = manifest.get("entry") if isinstance(manifest.get("entry"), dict) else {}
            reference = entry.get("verify")
            if not isinstance(reference, str) or ":" not in reference:
                state, reason = "unknown", "component has no verify function"
            else:
                try:
                    ctx = {"home": str(home), "os": "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else sys.platform,
                           "gen_id": record.get("gen_id") if record else None, "runtime_python": None, "kit_path": None,
                           "projects": record.get("projects", []) if record else [], "record": record, "dry_run": True,
                           "now": datetime.now(timezone.utc).isoformat()}
                    license_spec = manifest.get("licence") if isinstance(manifest.get("licence"), dict) else {}
                    if license_spec.get("required"):
                        license_ref = license_spec.get("check")
                        if not isinstance(license_ref, str) or ":" not in license_ref:
                            state, reason = "unlicensed", "required licence check is unavailable"
                            result.append({"id": component_id, "name": manifest.get("name"), "state": state,
                                           "reason": reason, "required": required, "selected": selected})
                            continue
                        license_module, license_function = license_ref.split(":", 1)
                        license_result = getattr(importlib.import_module(license_module), license_function)(ctx)
                        if not isinstance(license_result, dict) or license_result.get("state") != "valid":
                            state = "unlicensed" if isinstance(license_result, dict) and license_result.get("state") != "unknown" else "unknown"
                            reason = license_result.get("detail") if isinstance(license_result, dict) else "licence check returned an invalid result"
                            result.append({"id": component_id, "name": manifest.get("name"), "state": state,
                                           "reason": reason, "required": required, "selected": selected})
                            continue
                    module_name, function_name = reference.split(":", 1)
                    verifier = getattr(importlib.import_module(module_name), function_name)
                    checked = verifier(ctx)
                    observed = checked.get("state") if isinstance(checked, dict) else None
                    state = {"ok": "ok", "missing": "missing", "changed": "changed by you", "held": "held",
                             "unlicensed": "unlicensed", "unknown": "unknown"}.get(observed, "unknown")
                    reason = checked.get("reason") if isinstance(checked, dict) else "verify returned an invalid result"
                except Exception as exc:
                    state, reason = "unknown", f"verify unavailable ({type(exc).__name__})"
        result.append({"id": component_id, "name": manifest.get("name"), "state": state, "reason": reason,
                       "required": required, "selected": selected})
    return result


_CHECKLISTS = ("install", "uninstall", "add-repository", "remove-repository", "repair")
_PAGE_REVISION = re.compile(r"^Checklist revision:[ \t]*(\S+)[ \t]*$", re.M)


def _checklist_page(checklist, recorded) -> dict | None:
    """Where the shipped checklist page for this checkpoint is, and whether its revision is the one the checkpoint
    recorded. ``None`` for a checklist this package has no page for."""
    if checklist not in _CHECKLISTS:
        return None
    path = Path(__file__).parent / "checklists" / f"{checklist}.md"
    try:
        found = _PAGE_REVISION.search(path.read_text(encoding="utf-8"))
    except OSError:
        return {"path": str(path), "revision": None, "recorded_revision": recorded, "matches": None}
    revision = found.group(1) if found else None
    return {"path": str(path), "revision": revision, "recorded_revision": recorded,
            "matches": None if revision is None or recorded is None else revision == recorded}


def _checkpoint_state(home: Path, permissions_items: list[dict], effects: list[dict], components: list[dict], allowed_roots=()) -> dict:
    path = home / "install" / "checkpoint.json"
    read_state, value, reason, _ = _read_json(path)
    if read_state == "missing":
        return {"state": "missing", "reason": "setup checkpoint is absent", "steps": [], "next_action": None}
    if read_state != "ok":
        return {"state": "unknown", "reason": reason, "steps": [], "next_action": None}
    if value.get("profile") != "eutonos.setup-checkpoint.v1" or not isinstance(value.get("steps"), list):
        return {"state": "unknown", "reason": "malformed setup checkpoint", "steps": [], "next_action": None}
    known = {"permission": {item["id"]: item["state"] for item in permissions_items},
             "effect": {item.get("effect_id"): item.get("state") for item in effects},
             "component": {item["id"]: item["state"] for item in components}}
    steps, next_action = [], None
    for step in value["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("id"), str) or step.get("state") not in {"pending", "in_progress", "done", "blocked", "skipped"}:
            return {"state": "unknown", "reason": "malformed setup checkpoint step", "steps": [], "next_action": None}
        shown = dict(step)
        original_state = step["state"]
        if original_state == "in_progress":
            shown["recorded_state"] = "in_progress"
            shown["state"] = "pending"
        evidence = step.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            return {"state": "unknown", "reason": "malformed checkpoint evidence", "steps": [], "next_action": None}
        if original_state == "done":
            mismatches, comparable = [], 0
            for item in evidence:
                match = re.fullmatch(r"(permission|effect|component):([^=]+)=([a-z0-9 -]+)", item)
                if match:
                    group, item_id, expected = match.groups()
                    actual = known[group].get(item_id)
                    if actual is not None:
                        comparable += 1
                        if actual != expected:
                            mismatches.append(f"{group} {item_id} is now {actual}")
                file_match = re.fullmatch(r"sha256:(.+)#([0-9a-f]{64})", item)
                if file_match:
                    evidence_path, expected = file_match.groups()
                    try:
                        candidate = Path(evidence_path).resolve()
                        if not any(candidate.is_relative_to(Path(base).resolve()) for base in allowed_roots):
                            continue
                    except (OSError, ValueError):
                        continue
                    file_state, raw, file_reason = _read_bytes(evidence_path, MAX_EFFECT_BYTES)
                    if file_state == "ok":
                        comparable += 1
                        if _digest(raw) != expected:
                            mismatches.append(f"evidence file changed: {evidence_path}")
                    elif file_state == "missing":
                        comparable += 1
                        mismatches.append(f"evidence file is missing: {evidence_path}")
            if not evidence and step.get("kind") in ("person", "agent"):
                mismatches.append("completed person/agent step has no evidence")
            if mismatches:
                shown["state"] = "stale"
                shown["stale_reasons"] = mismatches
            shown["evidence_readback"] = "matched" if comparable and not mismatches else "stale" if mismatches else "unknown"
        steps.append(shown)
        if next_action is None and shown.get("state") in ("blocked", "pending", "stale"):
            next_action = step.get("next_action") or step.get("reason")
    return {"state": "ok", "profile": value.get("profile"), "checklist": value.get("checklist"),
            "checklist_revision": value.get("checklist_revision"), "gen_id": value.get("gen_id"), "root": value.get("root"),
            "page": _checklist_page(value.get("checklist"), value.get("checklist_revision")),
            "steps": steps, "next_action": next_action}


def _read_run_items(user_home: Path, os_name: str) -> tuple[list[dict], str | None]:
    found = []
    if os_name == "windows":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                index = 0
                while True:
                    try:
                        name, command, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if any(term in (str(name) + " " + str(command)).lower() for term in ("eutonos", "crosstalk")):
                        found.append({"source": "Run", "name": str(name), "command": str(command)})
        except FileNotFoundError:
            pass
        except OSError as exc:
            return found, f"Run-key read failed ({type(exc).__name__})"
        startup = user_home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if startup.is_dir():
            try:
                for path in startup.iterdir():
                    if any(term in path.name.lower() for term in ("eutonos", "crosstalk")):
                        found.append({"source": "Startup", "name": path.name, "path": str(path)})
            except OSError as exc:
                return found, f"Startup read failed ({type(exc).__name__})"
    elif os_name == "macos":
        launch = user_home / "Library" / "LaunchAgents"
        if launch.is_dir():
            try:
                for path in launch.iterdir():
                    if any(term in path.name.lower() for term in ("eutonos", "crosstalk")):
                        found.append({"source": "launchd", "name": path.name, "path": str(path)})
            except OSError as exc:
                return found, f"LaunchAgents read failed ({type(exc).__name__})"
    return found, None


def _os_name() -> str:
    return "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "other"


def _unknown(kind: str, target: str, reason: str) -> dict:
    return {"kind": kind, "target": target, "state": "unknown", "reason": reason}


def _allow_rules(value) -> list[str]:
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "allow" and isinstance(child, list):
                result.extend(item for item in child if isinstance(item, str))
            else:
                result.extend(_allow_rules(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_allow_rules(child))
    return result


def _toml(path: Path) -> tuple[str, dict | None, str | None]:
    state, raw, reason = _read_bytes(path, MAX_JSON_BYTES)
    if state != "ok":
        return state, None, reason
    try:
        return "ok", tomllib.loads(raw.decode("utf-8")), None
    except (UnicodeError, tomllib.TOMLDecodeError):
        return "unknown", None, "malformed TOML"


KNOWN_ALLOW_ENTRIES = frozenset({"set-remote-control", "crosstalk-reply", "send-message", "native-send-message"})


def _known_allow_rules() -> set:
    """The allow rules the shipped permission entries declare (so an installed one is never reported undeclared)."""
    known_rules = set()
    try:
        shipped = permissions.load()
        for entry in permissions.entries(shipped):
            if entry.get("id") in KNOWN_ALLOW_ENTRIES:
                values = entry.get("value")
                known_rules.update(values if isinstance(values, list) else [values] if isinstance(values, str) else [])
    except Exception:
        pass
    return known_rules


def _undeclared_report(home: Path, user_home: Path, root: Path, effects: list[dict]) -> list[dict]:
    found = []
    known_rules = _known_allow_rules()
    json_paths = [user_home / ".claude" / "settings.json", root / ".claude" / "settings.json",
                  root / ".claude" / "settings.local.json"]
    for path in json_paths:
        state, value, reason, _ = _read_json(path)
        if state == "ok":
            for rule in _allow_rules(value):
                if (rule.startswith("mcp__crosstalk-p0__") or rule.startswith("mcp__ccd_")) and rule not in known_rules:
                    found.append(_unknown("undeclared-allow-rule", str(path), f"unlisted allow rule: {rule}"))
        elif state == "unknown":
            found.append(_unknown("allow-rule-readback", str(path), reason or "unreadable"))
    codex_paths = [user_home / ".codex" / "config.toml", root / ".codex" / "config.toml"]
    recorded_targets = {_normal_path(effect.get("target")) for effect in effects if effect.get("permission_entry") == "codex-mcp-block"}
    for path in codex_paths:
        state, value, reason = _toml(path)
        if state == "ok" and isinstance(value.get("mcp_servers"), dict) and "crosstalk_p0" in value["mcp_servers"]:
            if _normal_path(str(path)) not in recorded_targets:
                found.append(_unknown("undeclared-codex-mcp", str(path), "[mcp_servers.crosstalk_p0] has no matching active Effect"))
        elif state == "unknown":
            found.append(_unknown("codex-mcp-readback", str(path), reason or "unreadable"))
    try:
        hosts = home / "hosts"
        if hosts.is_dir():
            for directory in hosts.iterdir():
                if directory.is_dir() and not (directory / "admission.json").is_file():
                    found.append(_unknown("admission-less-host", str(directory), "host directory has no admission.json"))
    except OSError as exc:
        found.append(_unknown("host-scan", str(home / "hosts"), f"directory read failed ({type(exc).__name__})"))
    for name in ("runtime", "crosstalk-helper-src"):
        path = home / name
        if path.exists():
            found.append(_unknown("legacy-runtime", str(path), "legacy path is report-only"))
    try:
        for path in home.iterdir():
            if path.is_dir() and path.name.lower().startswith("runtime.backup-"):
                found.append(_unknown("legacy-runtime-backup", str(path), "legacy path is report-only"))
    except OSError:
        pass
    projects_path = home / "projects.json"
    projects_state, projects_value, projects_reason, _ = _read_json(projects_path)
    devices_path = home / "device-repositories.json"
    devices_state, devices_value, devices_reason, _ = _read_json(devices_path)
    if projects_state == "unknown":
        found.append(_unknown("repository-registry", str(projects_path), projects_reason or "unreadable"))
    if devices_state == "unknown":
        found.append(_unknown("device-repository-registry", str(devices_path), devices_reason or "unreadable"))
    registries_valid = projects_state in ("ok", "missing") and devices_state in ("ok", "missing")
    roots, locators = set(), set()
    if projects_state == "ok":
        project_rows = projects_value.get("projects")
        if projects_value.get("profile") != "eutonos.projects.v1" or not isinstance(project_rows, list):
            registries_valid = False
            found.append(_unknown("repository-registry", str(projects_path), "unsupported projects registry shape"))
        else:
            for index, item in enumerate(project_rows):
                normalized = _normal_path(item.get("root")) if isinstance(item, dict) else None
                if normalized is None:
                    registries_valid = False
                    found.append(_unknown("repository-registry", f"{projects_path}#{index}", "project root is missing or malformed"))
                else:
                    roots.add(normalized)
    if devices_state == "ok":
        shared_rows = devices_value.get("shared")
        if devices_value.get("profile") != "eutonos.device-repositories.v1" or not isinstance(shared_rows, list):
            registries_valid = False
            found.append(_unknown("device-repository-registry", str(devices_path), "unsupported device registry shape"))
        else:
            for index, item in enumerate(shared_rows):
                paths = item.get("locators") if isinstance(item, dict) else None
                if not isinstance(paths, list):
                    registries_valid = False
                    found.append(_unknown("device-repository-registry", f"{devices_path}#shared[{index}]", "locator list is missing or malformed"))
                    continue
                for locator in paths:
                    normalized = _normal_path(locator)
                    if normalized is None:
                        registries_valid = False
                        found.append(_unknown("device-repository-registry", f"{devices_path}#shared[{index}]", "locator is malformed"))
                    else:
                        locators.add(normalized)
        personal = devices_value.get("personal")
        if personal is not None:
            normalized = _normal_path(personal.get("locator")) if isinstance(personal, dict) else None
            if normalized is None:
                registries_valid = False
                found.append(_unknown("device-repository-registry", f"{devices_path}#personal", "personal locator is malformed"))
            else:
                locators.add(normalized)
    if registries_valid:
        for item in sorted(roots - locators):
            found.append(_unknown("repository-registry-mismatch", item, "present in projects.json but not device-repositories.json"))
        for item in sorted(locators - roots):
            found.append(_unknown("repository-registry-mismatch", item, "present in device-repositories.json but not projects.json"))
    os_name = _os_name()
    run_items, run_error = _read_run_items(user_home, os_name)
    if run_error:
        found.append(_unknown("startup-readback", str(user_home), run_error))
    for item in run_items:
        found.append(_unknown("startup-or-run-item", item.get("path") or item.get("name", "unknown"),
                              "named EUTONOS or Crosstalk; report only"))
    return found


def _git_readback(root: Path) -> dict:
    values, errors = {}, []
    for key in ("safe.directory", "core.autocrlf"):
        try:
            process = subprocess.run(["git", "-C", str(root), "config", "--get-all", key],
                                     capture_output=True, text=True, timeout=4, check=False, **no_window())
        except (OSError, subprocess.TimeoutExpired) as exc:
            values[key] = None
            errors.append(f"{key}: {type(exc).__name__}")
            continue
        if process.returncode == 0:
            values[key] = [line for line in process.stdout.splitlines() if line]
        elif process.returncode == 1:
            values[key] = []
        else:
            values[key] = None
            errors.append(f"{key}: git exited {process.returncode}")
    return {"state": "unknown" if errors else "ok", "values": values, "reason": "; ".join(errors) or None}


def _git_present() -> bool:
    return bool(shutil.which("git"))


def _git_parts_readback(components) -> dict:
    """Which parts need Git (their manifest's ``requires.tools``) and whether Git is here. Without Git the core keeps
    working without its project memory features and each other part that needs it is skipped; nothing here is a
    blocker, because Git is not required to install (ISU stage 6)."""
    try:
        from . import component_support
        from .installer import load_components
        needing = component_support.parts_needing(load_components(), "git")
    except Exception as exc:
        return {"state": "unknown", "reason": f"component manifests unreadable ({type(exc).__name__})", "parts": []}
    present = _git_present()
    states = {item.get("id"): item for item in components or []}
    parts = [{"id": item["id"], "name": item["name"], "state": (states.get(item["id"]) or {}).get("state", "unknown"),
              "available": present or item["id"] == "core",
              "limited": (not present) and item["id"] == "core"} for item in needing]
    line = prereq.without_git_line([] if present else parts)
    return {"state": "ok" if present else "limited", "git": "found" if present else "not found", "parts": parts,
            "reason": line or None,
            "next_action": None if present else "Install Git (" + (prereq.GUIDANCE["git"][prereq._os_key(sys.platform)]) + "), then run Setup again to add the parts it unlocks."}


def _account_readback() -> dict:
    try:
        module = importlib.import_module("tools.crosstalk.crosstalk_p0_core")
        result = module.account_check()
        if not isinstance(result, dict):
            raise TypeError("account check returned an invalid result")
        return {"state": result.get("state", "unknown"), "message": result.get("message")}
    except Exception as exc:
        return {"state": "unknown", "reason": f"account readback unavailable ({type(exc).__name__})"}


def _codex_hook_readback(root: Path, user_home: Path) -> dict:
    """Report exact file-hash trust entries when present; unknown schemas are not guessed."""
    state, config, reason = _toml(user_home / ".codex" / "config.toml")
    if state != "ok":
        return {"state": "unknown", "reason": reason or "Codex config is absent"}
    hook_state = (config.get("hooks") or {}).get("state") if isinstance(config.get("hooks"), dict) else None
    if not isinstance(hook_state, dict):
        return {"state": "unknown", "reason": "Codex hooks.state is absent or has an unknown shape"}
    path = root / ".codex" / "hooks.json"
    file_state, raw, file_reason = _read_bytes(path, MAX_JSON_BYTES)
    if file_state != "ok":
        return {"state": "unknown", "reason": file_reason or "project hooks file is absent"}
    expected = []
    for key, value in hook_state.items():
        if isinstance(key, str) and "hooks.json" in key.lower() and isinstance(value, str) and _SHA256.fullmatch(value.lower()):
            expected.append(value.lower())
        elif isinstance(value, dict) and isinstance(value.get("hooks_sha256"), str) and _SHA256.fullmatch(value["hooks_sha256"].lower()):
            expected.append(value["hooks_sha256"].lower())
    if not expected:
        return {"state": "unknown", "reason": "Codex trust entries do not expose a comparable hooks.json hash"}
    current = _digest(raw)
    stale = current not in expected
    return {"state": "stale" if stale else "ok", "current_sha256": current,
            "trusted_sha256": expected, "reason": "trusted hook hash differs from current hooks.json" if stale else None}


def _baseline_proposal(root: Path) -> dict:
    path = root / "docs" / "campaigns" / "ipc" / "IPC1_INVENTORY.jsonl"
    state, raw, reason = _read_bytes(path, MAX_INVENTORY_BYTES)
    if state != "ok":
        return {"state": "unknown", "reason": reason or "IPC1 inventory is absent", "items": []}
    classes = {"product": "adopted", "unknown": "kept", "personal": "personal"}
    items = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError("malformed inventory row")
            items.append({"id": row["id"], "classification": classes.get(row.get("class"), "kept")})
    except (UnicodeError, ValueError):
        return {"state": "unknown", "reason": "IPC1 inventory is malformed", "items": []}
    tally = {name: sum(item["classification"] == name for item in items) for name in ("adopted", "kept", "personal")}
    return {"state": "available", "inventory_sha256": _digest(raw), "counts": tally, "items": items}


def _baseline_report(home: Path, proposed: dict) -> dict:
    path = home / "install" / "doctor-baseline.json"
    state, value, reason, raw = _read_json(path)
    if state == "missing":
        return {"state": "not-saved", "path": str(path), "proposed": {k: v for k, v in proposed.items() if k != "items"}}
    if (state != "ok" or value.get("profile") != BASELINE_PROFILE or not isinstance(value.get("items"), list)
            or not isinstance(value.get("counts"), dict) or not isinstance(value.get("inventory_sha256"), str)):
        return {"state": "unknown", "path": str(path), "reason": reason or "unsupported baseline"}
    return {"state": "saved", "path": str(path), "sha256": _digest(raw),
            "counts": value.get("counts"), "inventory_sha256": value.get("inventory_sha256")}


def _report(home=None, user_home=None, root=None, ports=None) -> dict:
    """Read install state, setup evidence, declared settings and drift without writing."""
    selected_home = Path(home) if home is not None else eutonos_home.home()
    selected_user_home = Path(user_home) if user_home is not None else Path.home()
    selected_root = Path(root) if root is not None else Path.cwd()
    record_info = _load_active_record(selected_home)
    record = record_info["record"]
    ports = ports if ports is not None else doctor_outside.default_ports(_os_name())
    components = _components_report(Path(__file__).parent, record, selected_home)
    effects = _effect_report(record_info["effects"], ports=ports, components=components)
    outside = doctor_outside.readbacks(selected_home, selected_user_home, record, record_info["effects"], ports)
    try:
        declared_outside = doctor_outside.declared_report(
            permissions.load().get("outside_effects"), os_name=_os_name(), components=components, effect_rows=effects, outside=outside)
    except Exception as exc:
        declared_outside = [{"id": "outside-effects", "state": "unknown", "reason": f"declared outside effects unavailable ({type(exc).__name__})"}]
    permissions_info = _permissions_report(selected_root, selected_user_home, record_info["effects"], record, outside)
    checkpoint = _checkpoint_state(selected_home, permissions_info.get("items", []), effects, components,
                                   (selected_home, selected_user_home, selected_root))
    undeclared = _undeclared_report(selected_home, selected_user_home, selected_root, record_info["effects"])
    try:
        text = consent.render(selected_root, selected_user_home)
        receipt = record.get("consent") if isinstance(record, dict) else None
        consent_state = "missing" if not isinstance(receipt, dict) else "ok" if (
            receipt.get("text_id") == consent.TEXT_ID
            and (receipt.get("base_sha256") or receipt.get("text_sha256")) == consent.text_sha256(text)) else "changed by you"
        consent_info = {"state": consent_state, "text_id": consent.TEXT_ID,
                        "text_sha256": consent.text_sha256(text), "text": text, "receipt": receipt}
    except Exception as exc:
        consent_info = {"state": "unknown", "reason": f"consent text unavailable ({type(exc).__name__})", "receipt": None}
    readbacks = {"outside": outside, "git": _git_readback(selected_root),
                 "git_parts": _git_parts_readback(components), "claude_account": _account_readback(),
                 "codex_hook_hashes": _codex_hook_readback(selected_root, selected_user_home),
                 "working_chat_modes": {"state": "unknown", "inbound": "unknown",
                                        "reason": "current Crosstalk chat mode is not exposed as a read-only readback"},
                 "crosstalk_hermes_link": _hermes_link_readback()}
    proposed = _baseline_proposal(selected_root)
    required = [item for item in permissions_info.get("items", []) if item.get("required") and item.get("selected")]
    required += [item for item in components if item.get("required") and item.get("selected")]
    blockers = [{"id": item.get("id"), "state": item.get("state"), "reason": item.get("reason")}
                for item in required if item.get("state") not in ("ok", "held", "skipped")]
    if record_info["state"] != "ok":
        blockers.insert(0, {"id": "active-install", "state": record_info["state"], "reason": record_info.get("reason")})
    if consent_info.get("state") != "ok":
        blockers.insert(1 if blockers else 0, {"id": "consent", "state": consent_info.get("state"),
                                               "reason": consent_info.get("reason") or "current consent receipt is absent or stale"})
    optional_states = permissions_info.get("items", []) + components
    readiness = {"ready": not blockers, "state": "ready" if not blockers else "not-ready", "blockers": blockers,
                 "held": [{"id": item.get("id"), "reason": item.get("reason")} for item in optional_states if item.get("state") == "held"],
                 "skipped": [{"id": item.get("id"), "reason": item.get("reason")} for item in optional_states if item.get("state") == "skipped"],
                 "limited": [{"id": "git", "reason": readbacks["git_parts"].get("reason"),
                              "next_action": readbacks["git_parts"].get("next_action")}]
                 if readbacks["git_parts"].get("state") == "limited" else []}
    next_action = checkpoint.get("next_action")
    if not next_action and blockers:
        first = blockers[0]
        next_action = first.get("reason") or f"Resolve required capability {first.get('id')} ({first.get('state')})."
    return {"profile": PROFILE, "root": str(selected_root), "home": str(selected_home),
            "active_record": {"state": record_info["state"], "reason": record_info.get("reason"),
                              "gen_id": record.get("gen_id") if record else None,
                              "state_name": record.get("state") if record else None,
                              "local_patch": self_repair.patch_summary(record.get("local_patch")) if record else None,
                              "local_patch_line": self_repair.patch_line(record.get("local_patch")) if record else None},
            "declared": {"permissions": permissions_info, "components": components, "effects": effects,
                         "outside_effects": declared_outside},
            "checkpoint": checkpoint, "undeclared": undeclared, "readbacks": readbacks,
            # Managed files for the agent's semantic three-way step (a customized or similar copy was left active).
            "three_way": [row for row in (record.get("three_way") or []) if isinstance(row, dict)] if isinstance(record, dict) else [],
            "consent": consent_info, "readiness": readiness, "next_action": next_action,
            "baseline": _baseline_report(selected_home, proposed),
            "seams": [],
            "qualification": "Read-only local report; unknown coverage stays unknown. No install or setup action was performed."}


def report(home=None, user_home=None, root=None, ports=None) -> dict:
    """Return a report even when a local subsystem is malformed or unavailable."""
    try:
        return _report(home, user_home, root, ports)
    except Exception as exc:
        reason = f"report subsystem failed ({type(exc).__name__})"
        return {"profile": PROFILE, "active_record": {"state": "unknown", "reason": reason},
                "declared": {"permissions": {"state": "unknown", "items": []}, "components": [], "effects": []},
                "checkpoint": {"state": "unknown", "reason": reason, "steps": [], "next_action": None},
                "undeclared": [], "readbacks": {}, "consent": {"state": "unknown", "reason": reason},
                "readiness": {"ready": False, "state": "unknown", "blockers": [{"id": "doctor", "state": "unknown", "reason": reason}],
                              "held": [], "skipped": [], "limited": []}, "next_action": "Resolve the doctor readback error and rerun.",
                "seams": [], "qualification": "Partial report; a subsystem failed and no state was changed."}


def _save_baseline(home: Path, proposed: dict) -> dict:
    if proposed.get("state") != "available":
        raise ValueError(proposed.get("reason") or "IPC1 inventory is unavailable")
    path = home / "install" / "doctor-baseline.json"
    value = {"profile": BASELINE_PROFILE, "created_at": datetime.now(timezone.utc).isoformat(),
             "inventory_sha256": proposed["inventory_sha256"], "counts": proposed["counts"], "items": proposed["items"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"state": "saved", "path": str(path), "counts": value["counts"], "inventory_sha256": value["inventory_sha256"]}


def _hermes_link_readback() -> dict:
    """Slice 3B.17: is the Crosstalk link to Hermes healthy, and did Hermes update since it was last checked?
    Read-only, from Crosstalk's link record; never raises."""
    try:
        from . import crosstalk_link
        return crosstalk_link.doctor_row()
    except Exception as exc:
        return {"present": False, "attention": False, "detail": f"unavailable ({type(exc).__name__})"}


def _short_text(value: dict) -> str:
    readiness = value.get("readiness", {})
    declared = value.get("declared", {})
    count = len(declared.get("permissions", {}).get("items", [])) + len(declared.get("components", [])) + len(declared.get("effects", []))
    page = (value.get("checkpoint") or {}).get("page")
    page_line = (f"Checklist page: {page['path']} (revision {page.get('revision')}"
                 f"{'' if page.get('matches') is not False else ', the checkpoint recorded ' + str(page.get('recorded_revision'))})\n"
                 if page else "")
    patch_line = (value.get("active_record") or {}).get("local_patch_line")
    patch_text = f"{patch_line}\n" if patch_line else ""
    three_way = value.get("three_way") or []
    patch_text += (f"Three-way review pending: {len(three_way)} managed file(s) left as you have them "
                   f"({', '.join(sorted({str(row.get('reason')) for row in three_way}))})\n" if three_way else "")
    link = (value.get("readbacks") or {}).get("crosstalk_hermes_link") or {}
    if link.get("attention"):
        patch_text += f"Hermes link: {link.get('detail')}" + chr(10)
    return (f"EUTONOS doctor: {readiness.get('state', 'unknown')}\n"
            f"Active install: {value.get('active_record', {}).get('state', 'unknown')}\n"
            f"{patch_text}"
            f"Declared: {count} items; "
            f"undeclared: {len(value.get('undeclared', []))}\n"
            f"{page_line}"
            f"Next: {value.get('next_action') or 'No next action recorded.'}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only EUTONOS install and setup report.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--home", help="EUTONOS home (installer staging or tests)")
    parser.add_argument("--user-home", help="Claude and Codex configuration home")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--save-baseline", action="store_true", help="write the IPC1 classification baseline")
    args = parser.parse_args(argv)
    selected_home = Path(args.home) if args.home else eutonos_home.home()
    value = report(selected_home, args.user_home, args.root)
    if args.save_baseline:
        try:
            value["baseline"] = _save_baseline(selected_home, _baseline_proposal(Path(args.root)))
        except (OSError, ValueError) as exc:
            value["baseline"] = {"state": "unknown", "reason": f"baseline write failed ({type(exc).__name__})"}
            sys.stdout.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n" if args.json else _short_text(value))
            return 2
    sys.stdout.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n" if args.json else _short_text(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
