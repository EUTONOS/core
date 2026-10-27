"""Bounded, metadata-only model choices for EUTONOS worker profiles.

Family choices follow the newest visible stable model in that family. Exact
model IDs are pins and never silently change identity.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window
from functools import lru_cache
from collections import OrderedDict
from copy import deepcopy
import threading

from . import home as eutonos_home


EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ("low", "medium", "high")
LUNA_ALLOWED_EFFORTS = EFFORTS[EFFORTS.index("xhigh"):]
MAX_CACHE_BYTES = 2_000_000
MAX_REPORT_BYTES = 65_536
FALLBACK_DATE = "2026-10-14"
FAMILIES = {"sol": "codex", "luna": "codex", "astra": "codex",
            "sonnet": "claude", "opus": "claude"}
_CODEX_ID = re.compile(r"gpt-(\d+(?:\.\d+)*)(?:-([a-z][a-z0-9-]*))?$")
_CLAUDE_ID = re.compile(r"claude-([a-z][a-z0-9]*)-(\d+(?:-\d+)*)$")
_FALLBACK_CODEX = {
    "gpt-6.1-sol": EFFORTS,
    "gpt-6-astra": EFFORTS,
    "gpt-6-sol": EFFORTS,
    "gpt-6-luna": EFFORTS[:-1],
    "gpt-5.6-sol": EFFORTS,
    "gpt-5.6-terra": EFFORTS,
    "gpt-5.6-luna": EFFORTS[:-1],
    "gpt-5.5": EFFORTS[:-1],
}
_CLAUDE = {"claude-sonnet-5-5": CLAUDE_EFFORTS,
           "claude-sonnet-5": CLAUDE_EFFORTS,
           "claude-opus-5-5": CLAUDE_EFFORTS,
           "claude-haiku-5-5": CLAUDE_EFFORTS,
           "claude-haiku-4-5-20251001": CLAUDE_EFFORTS}
_LAST_GOOD = OrderedDict()
_LAST_GOOD_LOCK = threading.Lock()
SAVED_SETTINGS_BYTES = 262_144


def is_exact_model_id(value) -> bool:
    """Recognize a concrete native-style ID without claiming availability."""
    return isinstance(value, str) and len(value) <= 128 and bool(
        _CODEX_ID.fullmatch(value) or _CLAUDE_ID.fullmatch(value))


def _cache_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"


def watch_home() -> Path:
    """Per-user watch evidence, shared across projects without Git discovery."""
    return eutonos_home.home() / "model-watch"


_AUTO_MODE_ID = re.compile(r"claude-([a-z][a-z0-9]*)-(\d+(?:-\d+)*)$")


def auto_mode_supported(model_id) -> bool:
    """Whether a Claude model can run auto mode (owner, 2026-10-22: allow Haiku 5.5).

    The permission-modes documentation lists Haiku 5.5 and later, and the Sonnet, Opus and Fable
    models, as supported; Haiku 4.5 and older and the claude-3 models are not. A Claude ID this
    does not recognise is False (the conservative answer). Unversioned family names (sonnet,
    opus, fable, haiku) follow the family's latest version, as the catalog does; the latest Haiku
    is claude-haiku-5-5, so bare ``haiku`` is True. A trailing ``[1m]`` style
    suffix and a trailing eight-digit date are ignored. A parity copy lives in
    ``tools/crosstalk/crosstalk_p0_core.py`` (the kit must not import eutonos); keep them equal.
    """
    if not isinstance(model_id, str):
        return False
    name = re.sub(r"\[[^\]]*\]$", "", model_id.strip().lower())
    if name in ("sonnet", "opus", "fable", "haiku"):
        return True
    match = _AUTO_MODE_ID.fullmatch(name)
    if not match:
        return False
    parts = match.group(2).split("-")
    if re.fullmatch(r"\d{8}", parts[-1]):
        parts.pop()
    if not parts:
        return False
    major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    family = match.group(1)
    if family in ("sonnet", "opus"):
        return major >= 4
    if family == "fable":
        return True
    if family == "haiku":
        return major > 5 or (major == 5 and minor >= 5)
    return False


def auto_mode_refused(model_id) -> bool:
    """True for a Claude model name that cannot run auto mode; other names are not this policy's concern."""
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    name = model_id.strip().lower()
    return (name.startswith("claude-") or "haiku" in name) and not auto_mode_supported(model_id)


def _version(model: str) -> tuple[int, ...]:
    match = _CODEX_ID.fullmatch(model)
    if match:
        return tuple(map(int, match.group(1).split(".")))
    match = _CLAUDE_ID.fullmatch(model)
    if match:
        return tuple(map(int, match.group(2).split("-")))
    return ()


def _family(model: str) -> str | None:
    match = _CODEX_ID.fullmatch(model)
    if match:
        suffix = match.group(2)
        if suffix:
            family = suffix.split("-")[0]
            return family if family in ("sol", "luna", "astra") else None
        return None
    match = _CLAUDE_ID.fullmatch(model)
    return match.group(1) if match and match.group(1) in FAMILIES else None


def read_codex_capabilities(path=None) -> dict:
    """Read bounded native metadata, without fallback or cache identity/prompt fields.

    ``status`` distinguishes a valid empty catalog from unknown availability.
    Invalid individual records are skipped and counted for watch diagnostics.
    """
    try:
        path = Path(path) if path is not None else _cache_path()
        with path.open("rb") as handle:
            raw = handle.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError("oversized cache")
        payload = json.loads(raw)
        models = payload["models"]
        if not isinstance(models, list) or len(models) > 256:
            raise ValueError("invalid model list")
        result = {}
        invalid = 0
        for item in models:
            if not isinstance(item, dict):
                invalid += 1
                continue
            if item.get("visibility") != "list":
                continue
            if item.get("picker_enabled", True) is False or item.get("is_available", True) is False:
                continue
            if item.get("availability") in ("unavailable", "disabled"):
                continue
            slug = item.get("slug")
            if not is_exact_model_id(slug) or not _CODEX_ID.fullmatch(slug):
                invalid += 1
                continue
            levels = item.get("supported_reasoning_levels")
            if not isinstance(levels, list):
                invalid += 1
                continue
            efforts = tuple(e for e in EFFORTS if any(
                isinstance(level, dict) and level.get("effort") == e for level in levels))
            if efforts:
                result[slug] = {"supported_efforts": list(efforts), "family": _family(slug),
                                "effort_basis": "native-supported-efforts"}
            else:
                invalid += 1
        return {"status": "ok", "basis": "codex-native-cache", "models": result,
                "invalid_items": invalid}
    except FileNotFoundError:
        return {"status": "unknown", "basis": "codex-cache-missing", "models": {}}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RuntimeError, RecursionError):
        return {"status": "unavailable", "basis": "codex-cache-unreadable", "models": {}}


@lru_cache(maxsize=32)
def _shared_root(root: str) -> Path:
    """Use the main checkout for worktree-local reports, as settings does."""
    path = Path(root).resolve()
    try:
        result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=path,
                                capture_output=True, text=True, timeout=2, **no_window())
        if result.returncode == 0 and result.stdout.strip():
            common = Path(result.stdout.strip())
            return ((path / common) if not common.is_absolute() else common).resolve().parent
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        pass
    return path


def shared_root(root) -> Path:
    """Canonical repository root for shared capability reports and watch state."""
    return _shared_root(str(root))


def read_claude_capabilities(root=None, *, path=None) -> dict:
    """Read the canonical 64 KiB/64-model report without availability guesses.

    Omitted effort metadata carries the same dated assumption used by dispatch.
    No source strings, dates or other payload fields enter model comparison.
    """
    try:
        if path is not None:
            path = Path(path)
        else:
            path = watch_home() / "claude-models.json"
        with path.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("oversized report")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("profile") != "eutonos.claude-model-report.v1":
            raise ValueError("wrong report profile")
        models = payload.get("models")
        if not isinstance(models, list) or len(models) > 64:
            raise ValueError("invalid report model list")
        date = payload.get("reported_on")
        if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
            date = "undated"
        result = {}
        invalid = 0
        for item in models:
            if not isinstance(item, dict):
                invalid += 1
                continue
            model = item.get("id")
            if not is_exact_model_id(model) or not _CLAUDE_ID.fullmatch(model):
                invalid += 1
                continue
            stated = item.get("supported_efforts")
            if stated is None:
                efforts = CLAUDE_EFFORTS
                effort_basis = f"dated-effort-assumption-{FALLBACK_DATE}"
            elif (isinstance(stated, list) and stated and len(stated) <= len(EFFORTS)
                  and all(isinstance(e, str) and e in EFFORTS for e in stated)
                  and len(set(stated)) == len(stated)):
                efforts = tuple(e for e in EFFORTS if e in stated)
                effort_basis = "reported-efforts"
            else:
                invalid += 1
                continue
            result[model] = {"supported_efforts": list(efforts), "family": _family(model),
                             "effort_basis": effort_basis,
                             "basis": f"claude-local-report-{date};{effort_basis}"}
        return {"status": "ok", "basis": f"claude-local-report-{date}",
                "models": result, "invalid_items": invalid}
    except FileNotFoundError:
        return {"status": "unknown", "basis": "no-claude-report", "models": {}}
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError, RecursionError):
        return {"status": "unavailable", "basis": "claude-report-unreadable", "models": {}}


CLAUDE_REPORT_PROFILE = "eutonos.claude-model-report.v1"


def write_claude_report(models, *, reported_on=None, path=None) -> dict:
    """Write the bounded, metadata-only Claude model report a Claude chat can see.

    Only exact Claude model IDs enter the file (no source text, no prompts);
    ``read_claude_capabilities`` is the reader. Returns the written payload.
    """
    import datetime
    import tempfile
    ids = []
    for model in models:
        if not isinstance(model, str) or not is_exact_model_id(model) or not _CLAUDE_ID.fullmatch(model):
            raise ValueError(f"Not an exact Claude model ID: {model!r}")
        if model not in ids:
            ids.append(model)
    if not 1 <= len(ids) <= 64:
        raise ValueError("Report 1 to 64 distinct Claude model IDs")
    date = reported_on or datetime.date.today().isoformat()
    if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
        raise ValueError("reported_on must be YYYY-MM-DD")
    payload = {"profile": CLAUDE_REPORT_PROFILE, "reported_on": date,
               "models": [{"id": model} for model in ids]}
    target = Path(path) if path is not None else watch_home() / "claude-models.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="claude-models-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return payload


def _reported_claude(root) -> dict[str, tuple[tuple[str, ...], str]]:
    source = read_claude_capabilities(root)
    return {model: (tuple(entry["supported_efforts"]), entry["basis"])
            for model, entry in source["models"].items()}


def _watch_snapshot(root):
    """Import the pure reader lazily; settings imports the catalog at startup."""
    try:
        from .model_watch import catalog_snapshot
        value = catalog_snapshot(root)
        if not isinstance(value, dict):
            value = {"error": "watch snapshot unknown"}
    except (ImportError, OSError, ValueError, TypeError, RuntimeError, RecursionError):
        value = {"error": "watch snapshot unknown"}
    try:
        key = os.path.normcase(str(watch_home().resolve()))
    except (OSError, RuntimeError):
        return value
    sources = value.get("sources", {})
    public = {}
    if isinstance(sources, dict):
        for harness in ("codex", "claude"):
            source = sources.get(harness)
            if isinstance(source, dict) and source.get("initialized") is True:
                public[harness] = {"initialized": True, "status": source.get("status", "unknown"),
                    "basis": _public_basis(source.get("basis"), harness),
                    "last_scan_at": _timestamp(source.get("last_scan_at")),
                    "last_good_at": _timestamp(source.get("last_good_at")),
                    "models": _retained_models(source, harness)}
    with _LAST_GOOD_LOCK:
        if public and not value.get("error"):
            _LAST_GOOD[key] = deepcopy(public)
            _LAST_GOOD.move_to_end(key)
            while len(_LAST_GOOD) > 8:
                _LAST_GOOD.popitem(last=False)
        elif key in _LAST_GOOD:
            public = deepcopy(_LAST_GOOD[key])
            for source in public.values():
                source["status"] = "unknown"
            value = {**value, "sources": public, "error": "watch snapshot unknown; last good evidence retained"}
    return value


def _bounded_saved_record(path):
    """Public-choice recovery reads only; never import the settings owner here."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(SAVED_SETTINGS_BYTES + 1)
        if len(raw) > SAVED_SETTINGS_BYTES:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _saved_pins(root):
    if root is None:
        return {}
    folder = shared_root(root) / ".eutonos-local"
    result = {}

    def retain(model, effort=None, harness=None):
        if not is_exact_model_id(model):
            return
        owner = "codex" if _CODEX_ID.fullmatch(model) else "claude"
        if harness not in (None, owner) or effort not in (None, *EFFORTS):
            return
        efforts = result.setdefault(model, set())
        if effort is not None:
            efforts.add(effort)

    local = _bounded_saved_record(folder / "settings.json")
    # "mailbox_model_mode" is a retired key that older settings files may still carry; accept it so
    # their saved role pins are still read.
    if (local.get("profile") == "eutonos.local-settings.v1" and not set(local) - {
            "profile", "task_rules", "ram", "roles", "role_defaults_seeded", "mailbox_model_mode", "hooks",
            "harnesses"}):
        roles = local.get("roles")
        if isinstance(roles, dict) and len(roles) <= 128:
            for role in roles.values():
                if (not isinstance(role, dict) or not {"options", "preamble", "autonomy"} <= set(role) or
                        set(role) - {"options", "preamble", "autonomy", "edits", "description", "brief_template"}):
                    continue
                options = role.get("options") if isinstance(role, dict) else None
                if isinstance(options, list) and len(options) <= 16:
                    for option in options:
                        if isinstance(option, dict) and set(option) == {"harness", "model", "effort"}:
                            retain(option.get("model"), option.get("effort"), option.get("harness"))
    companion = _bounded_saved_record(folder / "companion.json")
    if (companion.get("profile") == "eutonos.companion-trial-control.v3"
            and not set(companion) - {"profile", "enabled", "routing", "model", "effort", "allowed_models", "routing_profile", "max_concurrent_companions"}
            and companion.get("routing") == "fixed"):
        retain(companion.get("model"), companion.get("effort"))
    return {model: tuple(e for e in EFFORTS if e in efforts) for model, efforts in sorted(result.items())[:512]}


def _allowed_efforts(model: str, supported_efforts) -> tuple[str, ...]:
    """Apply owner selection policy without changing native capability evidence."""
    if _family(model) == "luna":
        return tuple(effort for effort in LUNA_ALLOWED_EFFORTS if effort in supported_efforts)
    return tuple(supported_efforts)



def _timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**12 else None


def _public_basis(value, harness):
    """Drop legacy free-form Claude report source text from retained evidence."""
    if not isinstance(value, str) or len(value) > 256:
        return "retained-catalog"
    if harness == "claude" and value.startswith("claude-local-report-"):
        match = re.match(r"claude-local-report-(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|undated)(?=$|[;:])", value)
        if not match:
            return "retained-claude-report"
        # Retain the established effort-evidence label, never report source text.
        tail = value.rsplit(";", 1)[-1] if ";" in value else None
        if tail in ("reported-efforts", f"dated-effort-assumption-{FALLBACK_DATE}"):
            return f"{match.group(0)};{tail}"
        return match.group(0)
    return value


def _retained_models(source, harness):
    """Whitelist bounded public fields rather than forwarding saved payloads."""
    models = source.get("models", {})
    if not isinstance(models, dict) or len(models) > 512:
        return {}
    result = {}
    pattern = _CODEX_ID if harness == "codex" else _CLAUDE_ID
    for model, entry in models.items():
        if not is_exact_model_id(model) or not pattern.fullmatch(model) or not isinstance(entry, dict):
            continue
        efforts = entry.get("supported_efforts")
        if (not isinstance(efforts, (list, tuple)) or not efforts or len(efforts) > len(EFFORTS)
                or any(not isinstance(e, str) or e not in EFFORTS for e in efforts)
                or len(set(efforts)) != len(efforts) or type(entry.get("available")) is not bool):
            continue
        effort_basis = entry.get("effort_basis")
        result[model] = {"supported_efforts": tuple(e for e in EFFORTS if e in efforts),
                         "available": entry["available"], "family": _family(model),
                         "effort_basis": (effort_basis if isinstance(effort_basis, str) and
                                          len(effort_basis) <= 128 else "retained-supported-efforts"),
                         "last_seen": _timestamp(entry.get("last_seen")),
                         "missing_since": _timestamp(entry.get("missing_since"))}
    return result


def describe_catalog(root=None) -> dict:
    """One read-only availability/effort projection for picker and dispatch.

    Native additions are immediate. A retained watch snapshot governs absence:
    pending removals stay selectable and confirmed retirement keeps an exact pin
    visible but unavailable. This reader never scans, writes or launches models.
    """
    observed = {"codex": read_codex_capabilities(), "claude": read_claude_capabilities(root)}
    snapshot = _watch_snapshot(root)
    saved = snapshot.get("sources", {})
    if not isinstance(saved, dict):
        saved = {}
    pins, sources = {}, {}
    for harness, native in observed.items():
        retained_source = saved.get(harness, {})
        if not isinstance(retained_source, dict):
            retained_source = {}
        retained = _retained_models(retained_source, harness)
        initialized = retained_source.get("initialized") is True
        healthy = native["status"] == "ok" and not native.get("invalid_items")
        last_scan = _timestamp(retained_source.get("last_scan_at"))
        last_good = _timestamp(retained_source.get("last_good_at"))
        source_freshness = "stale" if snapshot.get("error") and initialized else "current" if healthy else "stale" if initialized else "unknown"
        sources[harness] = {"status": native["status"] if not native.get("invalid_items") else "unavailable",
                            "basis": native["basis"], "freshness": source_freshness,
                            "initialized": initialized, "last_scan_at": last_scan, "last_good_at": last_good}
        candidates = {}
        # Dated assumptions remain labelled when there is no retained authority.
        # Explicit retirement evidence below always overrides those assumptions.
        fallback = _CLAUDE if harness == "claude" else _FALLBACK_CODEX if not healthy and not initialized and not retained else {}
        fallback_basis = f"dated-claude-catalog-{FALLBACK_DATE}" if harness == "claude" else f"dated-fallback-{FALLBACK_DATE}"
        for model, efforts in fallback.items():
            candidates[model] = {"supported_efforts": efforts, "family": _family(model),
                                 "available": True, "basis": fallback_basis, "freshness": "unknown",
                                 "effort_basis": f"dated-effort-assumption-{FALLBACK_DATE}"}
        for model, entry in retained.items():
            candidates[model] = {**entry, "basis": retained_source.get("basis", "retained-catalog") if initialized else fallback_basis,
                                 "freshness": "retired" if not entry["available"] else
                                              "unknown" if not initialized else "retained" if healthy else "stale"}
        for model, entry in native["models"].items():
            # Valid individual public records can add models even when an
            # unrelated malformed record makes the source unsafe for removals.
            candidates[model] = {**entry, "available": True,
                                 "basis": entry.get("basis", native["basis"]),
                                 "freshness": "current", "missing_since": None,
                                 "last_seen": retained.get(model, {}).get("last_seen")}
        for model, entry in candidates.items():
            efforts = tuple(entry["supported_efforts"])
            basis = _public_basis(entry.get("basis"), harness)
            pins[model] = {"harness": harness, "efforts": _allowed_efforts(model, efforts), "supported_efforts": efforts,
                           "label": model, "selection_kind": "pin", "resolved": model,
                           "available": entry["available"], "basis": basis, "family": _family(model),
                           "effort_basis": entry["effort_basis"], "freshness": entry["freshness"],
                           "missing_since": entry.get("missing_since"), "last_seen": entry.get("last_seen"),
                           "last_scan_at": last_scan, "last_good_at": last_good}
    if snapshot.get("error"):
        for model, efforts in _saved_pins(root).items():
            if model in pins:
                continue  # especially never override known retirement
            harness = "codex" if _CODEX_ID.fullmatch(model) else "claude"
            pins[model] = {"harness": harness, "efforts": _allowed_efforts(model, efforts),
                "supported_efforts": efforts, "label": model, "selection_kind": "pin", "resolved": model,
                "available": True, "basis": "saved-pin-unknown", "family": _family(model),
                "effort_basis": "saved-selection-unverified", "freshness": "unknown",
                "missing_since": None, "last_seen": None, "last_scan_at": None, "last_good_at": None}
    # Settings captures MODELS at import and includes that ordered list in its
    # default/CAS payload. Native and sorted JSON storage order must not change
    # the public list for an otherwise identical set of model capabilities.
    ordered = []
    for harness, known in (("codex", _FALLBACK_CODEX), ("claude", _CLAUDE)):
        ordered.extend(model for model in known if model in pins)
        ordered.extend(sorted(model for model, entry in pins.items()
                              if entry["harness"] == harness and model not in known))
    pins = {model: pins[model] for model in ordered}
    choices = {}
    for family, harness in FAMILIES.items():
        matches = [model for model, entry in pins.items()
                   if entry["harness"] == harness and entry["family"] == family
                   and entry["available"]
                   and entry["basis"] != "saved-pin-unknown"
                   and "preview" not in model and "experimental" not in model]
        if matches:
            latest = max(matches, key=lambda model: (_version(model), model))
            choices[family] = {**pins[latest], "label": family.title(),
                               "selection_kind": "family", "resolved": latest}
    return {"profile": "eutonos.model-capability-catalog.v2",
            "recorded_on": FALLBACK_DATE, "models": {**choices, **pins},
            "sources": sources,
            "pending_count": snapshot.get("pending_count", 0) if type(snapshot.get("pending_count", 0)) is int else 0,
            "watch_state": "unknown" if snapshot.get("error") else "read",
            "costs": {"state": "unknown", "basis": "no current price evidence recorded"}}


def resolve_choice(choice: str, harness: str, *, root=None, effort=None) -> dict:
    if not isinstance(choice, str) or not choice:
        raise ValueError("Model choice must be a non-empty string")
    models = describe_catalog(root)["models"]
    entry = models.get(choice)
    if entry is None or not entry["available"]:
        raise ValueError(f"Model choice unavailable: {choice}")
    if entry["harness"] != harness:
        raise ValueError(f"Model choice {choice} is for {entry['harness']}, not {harness}")
    if effort is not None:
        if effort not in entry["supported_efforts"]:
            raise ValueError(f"Unsupported effort for {choice}: {effort}")
        if effort not in entry["efforts"]:
            raise ValueError(
                f"Effort {effort!r} is below the owner policy minimum for Luna; "
                f"allowed efforts: {', '.join(entry['efforts']) or 'none'}")
    default_effort = None
    if effort is None and entry["family"] == "luna":
        if entry["efforts"]:
            default_effort = entry["efforts"][0]
        else:
            raise ValueError(
                f"Luna choice {choice} has no natively supported effort at or above XHigh")
    return {"requested": choice, "resolved": entry["resolved"],
            "selection_kind": entry["selection_kind"], "family": entry["family"],
            "supported_efforts": entry["supported_efforts"], "basis": entry["basis"],
            **({"allowed_efforts": entry["efforts"]} if entry["family"] == "luna" else {}),
            **({"default_effort": default_effort} if default_effort is not None else {})}


__all__ = ["FAMILIES", "LUNA_ALLOWED_EFFORTS", "describe_catalog", "is_exact_model_id", "resolve_choice",
           "read_codex_capabilities", "read_claude_capabilities", "write_claude_report", "shared_root", "watch_home"]
