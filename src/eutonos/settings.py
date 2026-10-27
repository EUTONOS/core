"""Validated local settings over existing companion and history owners."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window
import tempfile

from .private_scope import require_shared
from .companion_history import set_tracking, tracking
from .local_lock import local_lock
from . import model_catalog


PROFILE = "eutonos.local-settings.v1"
COMPANION_PROFILE = "eutonos.companion-trial-control.v3"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ("low", "medium", "high")
MODEL_CATALOG = model_catalog.describe_catalog()
MODELS = tuple(MODEL_CATALOG["models"])
MODEL_EFFORTS = {model: entry["efforts"] for model, entry in MODEL_CATALOG["models"].items()}
MODEL_HARNESS = {model: entry["harness"] for model, entry in MODEL_CATALOG["models"].items()}


def _default_models(root=None):
    return [model for model, entry in model_catalog.describe_catalog(root)["models"].items()
            if entry["available"] and entry["basis"] != "saved-pin-unknown"]


# AWR worker profiles (owner, 2026-09-28): "coding" joins the validated set.
# Dashboard redesign (owner, 2026-10-14): "review" joins as its own job, and
# "closeout" is fixed to the main chat (see _normalize_task_rules).
TASKS = ("discovery", "coding", "docs", "git", "review", "closeout")
# Jobs a worker can take; closeout always stays with the main chat.
WORKER_TASKS = tuple(task for task in TASKS if task != "closeout")
CLOSEOUT_ROLE = "orchestrator"

# AWR worker profiles (owner review, 2026-09-28): the per-task default when
# task_rules is unset. Deliberately NOT all "orchestrator": most tasks delegate
# to a default role when routing is enabled.
# Coding, docs and git each own a role so editing one job's model never moves
# another's (they all shared "worker-routine" before 2026-10-14; see
# _split_shared_roles for how an older saved file is migrated).
DEFAULT_TASK_RULES = {"discovery": "discovery", "coding": "worker-coding",
                      "docs": "worker-docs", "git": "worker-git", "review": "reviewer",
                      "closeout": CLOSEOUT_ROLE}
CAPTURE_CADENCES = ("every-turn", "work-sized", "checkpoint", "milestone")
CAPTURE_DETAILS = ("brief", "summary", "detailed")
ROUTING_PREFERENCES = ("cost-savings", "optimus", "high-quality")
MAX_COMPANIONS = 7
# Per-harness worker settings (owner, 2026-10-14): which apps may take worker
# jobs and how many workers each runs at once. Stored in settings.json
# ("harnesses"); once saved, the overall routing cap max_concurrent_companions
# is derived from it (see _effective).
MAX_HARNESS_WORKERS = 7
DEFAULT_HARNESSES = {"claude": {"enabled": True, "max_workers": 3},
                     "codex": {"enabled": True, "max_workers": 4},
                     "hermes": {"enabled": False, "max_workers": 1}}
# Schema-ready harnesses for a role's options. "hermes" is a VALID schema value here
# (so this schema does not need another migration once hermes exists) but nothing in
# this module or in role_router launches hermes: any code that RESOLVES a hermes role
# today must refuse with a clear "not wired yet" error.
ROLE_HARNESSES = ("claude", "codex", "hermes")
ROLE_AUTONOMY = ("off", "ask", "auto-read-only")  # Last value is a legacy alias for ask.
# AWR-CT (2026-09-28, owner-approved): role options are ordered positions 1-4;
# "primary"/"secondary" remain accepted aliases for positions 1/2 so a role saved
# in the old 2-option shape still loads (see role_router.resolve_role).
MAX_ROLE_OPTIONS = 4
MAX_ROLE_PREAMBLE_CHARS = 2000
# AWR worker profiles (owner, 2026-09-28): roles carry a short description, an
# edits default and an optional brief template. All are OPTIONAL on both read
# and write (see _validate_role); only a genuinely unknown key is rejected.
MAX_ROLE_DESCRIPTION_CHARS = 300
MAX_ROLE_BRIEF_TEMPLATE_CHARS = 2000
ROLE_EDITS = ("yes", "ask")
_ROLE_REQUIRED_KEYS = frozenset({"options", "preamble", "autonomy"})
_ROLE_OPTIONAL_KEYS = frozenset({"description", "edits", "brief_template"})
# A role name must be usable as the "role" request field of a cross-app start
# (the same pattern role_router checks), so a savable role can always be sent
# cross-app -- see validate_roles below.
ROLE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Settings keys that older files may still carry but nothing reads any more. A stored file that has
# one still loads: the key is dropped on read and is not written back on the next save.
_LEGACY_SETTINGS_KEYS = frozenset({"mailbox_model_mode"})


class StaleSettings(ValueError):
    def __init__(self, current):
        super().__init__("Settings changed since they were read")
        self.current = current


@lru_cache(maxsize=32)
def _shared_settings_root(resolved_root: str) -> str:
    """AWR7 (2026-09-24), resolving the AWR2 worktree settings-root exception:
    a linked git worktree has its own directory but must share ONE settings/
    companion state with its main checkout -- otherwise the same logical
    repository silently disagrees with itself about whether Smart routing is
    enabled depending only on which worktree happened to be the caller's
    cwd, which is exactly the AWR2 audit finding ("source tools/settings.py
    reads an absent worktree-local control and reports routing off, while
    the installed wrapper reads the parent repository's enabled control").

    Resolves to git's own notion of the shared "common dir" (identical from
    every linked worktree of the same repository) and returns its parent --
    the main checkout root -- as the settings root instead. Falls back to
    ``resolved_root`` unchanged for a non-worktree checkout, a bare/non-git
    directory, or if git itself is unavailable: this is a best-effort
    convenience that never raises, not a hard requirement, so an ordinary
    non-git caller (including most of this module's own tests, which use a
    plain tempdir) is completely unaffected.

    ``git rev-parse --git-common-dir`` returns ``.git`` (relative to cwd) from
    the main worktree itself, and an absolute path from a linked worktree;
    both are handled below. Cached per resolved path for the life of the
    process -- this spawns a real `git` subprocess, so callers should always
    go through `_resolve_shared_root`, not call this directly with an
    unresolved/relative path (that would fragment the cache key)."""
    try:
        result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=resolved_root,
                                 capture_output=True, text=True, timeout=5, **no_window())
    except (OSError, subprocess.TimeoutExpired):
        return resolved_root
    common_dir = result.stdout.strip()
    if result.returncode != 0 or not common_dir:
        return resolved_root
    shared = Path(common_dir)
    if not shared.is_absolute():
        shared = Path(resolved_root) / shared
    return str(shared.resolve().parent)


def _resolve_shared_root(root) -> Path:
    return Path(_shared_settings_root(str(Path(root).resolve())))


def _atomic(path, value, *, keep_harnesses=False):
    if (path.name == "settings.json" and not keep_harnesses and isinstance(value, dict)
            and value.get("harnesses") == DEFAULT_HARNESSES):
        # Untouched defaults are not an explicit choice: leave "harnesses" out of
        # the file so the overall worker cap keeps its stored value (see _effective).
        # The "harnesses" section's own save passes keep_harnesses=True.
        if "harnesses" not in model_catalog._bounded_saved_record(path):
            value = {key: item for key, item in value.items() if key != "harnesses"}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def read_companion(root):
    root = _resolve_shared_root(root)
    path = root / ".eutonos-local" / "companion.json"
    if not path.exists():
        return {"profile": COMPANION_PROFILE, "enabled": False, "routing": "profile",
                "model": None, "effort": None, "allowed_models": _default_models(root),
                "routing_profile": "optimus", "max_concurrent_companions": 1}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid companion control")
    result = {key: value.get(key) for key in ("enabled", "routing", "model", "effort", "allowed_models",
                                               "routing_profile", "max_concurrent_companions")}
    validate_routing(result, allow_unavailable=True)
    if set(value) != {"profile", *result} or value.get("profile") != COMPANION_PROFILE:
        raise ValueError("Invalid companion control")
    return {"profile": value["profile"], **result}


# Owner policy floor (2026-09-29): Luna's minimum allowed effort is XHigh,
# everywhere a Luna model can be pinned -- a role option (_validate_role_option)
# or the global fixed-routing model (validate_routing). This is an ADDITIONAL
# constraint layered on top of MODEL_EFFORTS/supported_efforts, which describe
# only native capability: a lower effort such as Luna "high" is still something
# the model can genuinely do, the owner has just decided not to allow selecting
# it any more. Gated on ``not allow_unavailable``, exactly like the existing
# supported-efforts check right below it: a WRITE (``allow_unavailable=False``,
# the default) rejects a fresh Luna low/medium/high choice outright; a READ of
# settings saved before this rule existed (``allow_unavailable=True``, see
# ``_local``/``read_companion``) retains the owner's stored choice unchanged,
# the same tolerance already extended to plain capability drift. This module
# only rejects an EXPLICIT sub-floor effort on a fresh write; resolving an
# OMITTED (None) Luna effort to xhigh is dispatch behavior owned by Codex's own
# resolver (owner correction, 2026-09-29), so it is deliberately not
# duplicated here.
LUNA_MIN_EFFORT = "xhigh"
_LUNA_FLOOR_EFFORTS = EFFORTS[EFFORTS.index(LUNA_MIN_EFFORT):]


def _is_luna_model(model, root=None):
    """True when ``model`` resolves to the Luna family: the bare family alias
    ("luna") or an exact Luna model ID (e.g. "gpt-6-luna", "gpt-5.6-luna")."""
    if model == "luna":
        return True
    available = model_catalog.describe_catalog(root)["models"]
    if model in available:
        return available[model]["family"] == "luna"
    return model_catalog.is_exact_model_id(model) and model_catalog._family(model) == "luna"


def validate_routing(value, *, allow_unavailable=False, root=None, preserved=None):
    legacy = isinstance(value, dict) and set(value) == {
        "enabled", "routing", "model", "effort", "allowed_models"}
    expected = {"enabled", "routing", "model", "effort", "allowed_models",
                "routing_profile", "max_concurrent_companions"}
    if legacy:
        value = {**value, "routing_profile": "optimus", "max_concurrent_companions": 1}
    if not isinstance(value, dict) or set(value) != expected or type(value.get("enabled")) is not bool:
        raise ValueError("Invalid routing settings")
    if value.get("routing_profile") not in ROUTING_PREFERENCES:
        raise ValueError("Invalid routing preference")
    if (type(value.get("max_concurrent_companions")) is not int
            or not 1 <= value["max_concurrent_companions"] <= MAX_COMPANIONS):
        raise ValueError(f"Concurrent companion limit must be between 1 and {MAX_COMPANIONS}")
    allowed = value.get("allowed_models")
    available = model_catalog.describe_catalog(root)["models"]
    preserved = preserved or {}
    unchanged_allowed = allowed == preserved.get("allowed_models")
    unchanged_fixed = (value.get("routing") == "fixed" and all(
        value.get(key) == preserved.get(key) for key in ("routing", "model", "effort")))
    def selectable(model):
        return model in available and available[model]["available"] and available[model]["basis"] != "saved-pin-unknown"
    def known(model):
        return selectable(model) or (unchanged_allowed and model_catalog.is_exact_model_id(model)
            and model in preserved.get("allowed_models", [])) or (allow_unavailable and (
            model_catalog.is_exact_model_id(model) or model in model_catalog.FAMILIES))
    if (not isinstance(allowed, list) or not allowed or any(type(model) is not str for model in allowed)
            or len(set(allowed)) != len(allowed) or any(not known(model) for model in allowed)):
        raise ValueError("Invalid allowed models")
    if value.get("routing") not in ("profile", "adaptive", "fixed"):
        raise ValueError("Invalid routing mode")
    if value["routing"] in ("profile", "adaptive"):
        if value.get("model") is not None or value.get("effort") not in (None, *EFFORTS):
            raise ValueError("Profile or adaptive routing cannot fix a model and requires a supported global effort")
    elif (value.get("model") not in allowed or not (selectable(value.get("model")) or
          (unchanged_fixed and model_catalog.is_exact_model_id(value.get("model"))) or
          (allow_unavailable and known(value.get("model"))))
          or (not allow_unavailable and not unchanged_fixed and value.get("model") in available and value.get("effort") not in
              (None, *available[value["model"]]["supported_efforts"]))
          or ((allow_unavailable or unchanged_fixed or value.get("model") not in available)
              and value.get("effort") not in (None, *EFFORTS))):
        raise ValueError("Invalid fixed model or effort")
    if (value["routing"] == "fixed" and not allow_unavailable and value.get("effort") is not None
            and _is_luna_model(value.get("model"), root) and value["effort"] not in _LUNA_FLOOR_EFFORTS):
        raise ValueError(f"Luna's minimum effort is {LUNA_MIN_EFFORT} (owner, 2026-09-29); "
                         f"got {value['effort']!r}")


def _validate_role_option(option, *, allow_unavailable=False, root=None):
    if not isinstance(option, dict) or set(option) != {"harness", "model", "effort"}:
        raise ValueError("Invalid role option")
    harness = option.get("harness")
    if harness not in ROLE_HARNESSES:
        raise ValueError("Invalid role option harness")
    model = option.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Invalid role option model")
    effort = option.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ValueError("Invalid role option effort")
    if harness in ("claude", "codex"):
        # hermes has no catalog yet, so it is skipped here (the harness value itself
        # is still validated above); claude/codex models must exist in THAT harness's
        # catalog, never a single shared list.
        available = model_catalog.describe_catalog(root)["models"]
        if model not in available and not (allow_unavailable and (
                (model_catalog.is_exact_model_id(model)
                 and model.startswith("gpt-" if harness == "codex" else "claude-"))
                or model_catalog.FAMILIES.get(model) == harness)):
            raise ValueError(f"Unknown {harness} model: {model}")
        if model in available and available[model]["harness"] != harness:
            raise ValueError(f"Unknown {harness} model: {model}")
        if model in available and not allow_unavailable and (
                not available[model]["available"] or available[model]["basis"] == "saved-pin-unknown"):
            raise ValueError(f"Unavailable {harness} model: {model}")
        # Reads retain the owner's stored choice even after capability drift.
        # Writes and dispatch still check the current supported efforts.
        if effort is not None and effort not in (available[model]["supported_efforts"]
                                                 if model in available and not allow_unavailable else EFFORTS):
            raise ValueError(f"Unsupported effort for {model}: {effort}")
        # Reject only an EXPLICIT sub-floor Luna effort on a fresh save; an omitted
        # (None) effort is left alone here -- resolving it to xhigh at dispatch is
        # Codex's resolver's job (owner correction, 2026-09-29), not this validator's,
        # so it is deliberately not duplicated in this module.
        if (harness == "codex" and not allow_unavailable and effort is not None
                and _is_luna_model(model, root) and effort not in _LUNA_FLOOR_EFFORTS):
            raise ValueError(f"Luna's minimum effort is {LUNA_MIN_EFFORT} (owner, 2026-09-29); "
                             f"got {effort!r}")
    return {"harness": harness, "model": model, "effort": effort}


def worker_model_disabled_reason(harness, model):
    """Worker policy, separate from the owner's available chat model catalog."""
    if harness == "claude" and model_catalog.auto_mode_refused(model):
        return f"{model} cannot run auto mode and is not supported for workers; choose Sonnet, Opus or Haiku 5.5 or later."
    return None


def _validate_role(role, *, allow_unavailable=False, root=None, preserved_options=None, role_name=None):
    if not isinstance(role, dict):
        raise ValueError("Invalid role")
    present = set(role)
    if (present - (_ROLE_REQUIRED_KEYS | _ROLE_OPTIONAL_KEYS)) or (_ROLE_REQUIRED_KEYS - present):
        raise ValueError("Invalid role")
    options = role.get("options")
    if not isinstance(options, list) or not 1 <= len(options) <= MAX_ROLE_OPTIONS:
        raise ValueError(f"A role must have 1 to {MAX_ROLE_OPTIONS} options")
    preamble = role.get("preamble")
    if preamble is not None and (not isinstance(preamble, str) or len(preamble) > MAX_ROLE_PREAMBLE_CHARS):
        raise ValueError(f"Invalid role preamble (max {MAX_ROLE_PREAMBLE_CHARS} characters)")
    autonomy = role.get("autonomy")
    if autonomy not in ROLE_AUTONOMY:
        raise ValueError("Invalid role autonomy")
    description = role.get("description")
    if description is not None and (not isinstance(description, str)
                                    or len(description) > MAX_ROLE_DESCRIPTION_CHARS):
        raise ValueError(f"Invalid role description (max {MAX_ROLE_DESCRIPTION_CHARS} characters)")
    edits = role.get("edits", "ask")
    if edits not in ROLE_EDITS:
        raise ValueError("Invalid role edits default")
    brief_template = role.get("brief_template")
    if brief_template is not None and (not isinstance(brief_template, str)
                                       or len(brief_template) > MAX_ROLE_BRIEF_TEMPLATE_CHARS):
        raise ValueError(f"Invalid role brief_template (max {MAX_ROLE_BRIEF_TEMPLATE_CHARS} characters)")
    validated_options = []
    for option in options:
        # Reads preserve old pins. Fresh saves must not keep an unusable worker
        # option merely because the retired-pin preservation path allows it.
        if not allow_unavailable and role_name != "orchestrator" and isinstance(option, dict):
            reason = worker_model_disabled_reason(option.get("harness"), option.get("model"))
            if reason:
                raise ValueError(reason)
        preserved = False
        if isinstance(option, dict) and isinstance(option.get("model"), str):
            key = (option.get("harness"), option["model"], option.get("effort"))
            if (all(part is None or isinstance(part, str) for part in key)
                    and model_catalog.is_exact_model_id(option["model"])
                    and preserved_options is not None and preserved_options[key] > 0):
                preserved_options[key] -= 1
                preserved = True
        validated_options.append(_validate_role_option(
            option, allow_unavailable=allow_unavailable or preserved, root=root))
    if not allow_unavailable:
        for option in validated_options:
            if (option["harness"] == "codex" and option["effort"] is not None and
                    _is_luna_model(option["model"], root) and option["effort"] not in _LUNA_FLOOR_EFFORTS):
                raise ValueError(f"Luna's minimum effort is {LUNA_MIN_EFFORT} (owner, 2026-09-29); "
                                 f"got {option['effort']!r}")
    return {"options": validated_options,
            "preamble": preamble, "autonomy": "ask" if autonomy == "auto-read-only" else autonomy,
            "description": description, "edits": edits, "brief_template": brief_template}


def validate_roles(value, *, allow_unavailable=False, root=None, preserved=None):
    """Validate the ``roles`` settings section.  Unknown keys anywhere are rejected.

    A role NAME must match ``ROLE_NAME_RE`` -- the same pattern role_router
    enforces for a start's "role" field -- so a role that
    validates here can always be passed on as a chat title and prompt label (coordinator
    review, 2026-09-28); a non-conforming name is rejected outright."""
    if not isinstance(value, dict):
        raise ValueError("Invalid roles settings")
    # A role may be renamed or reordered without selecting another retired pin.
    # Count saved options across names so copying a retired option into an extra
    # role still counts as a new selection and remains refused.
    preserved_options = Counter()
    for role in (preserved or {}).values():
        if not isinstance(role, dict) or not isinstance(role.get("options"), list):
            continue
        for option in role["options"]:
            if isinstance(option, dict) and isinstance(option.get("harness"), str) and isinstance(option.get("model"), str) and (option.get("effort") is None or isinstance(option.get("effort"), str)):
                preserved_options[(option["harness"], option["model"], option.get("effort"))] += 1
    result = {}
    for name, role in value.items():
        if not isinstance(name, str) or not ROLE_NAME_RE.fullmatch(name):
            raise ValueError("Invalid role name")
        result[name] = _validate_role(role, allow_unavailable=allow_unavailable, root=root,
                                      preserved_options=preserved_options, role_name=name)
    return result


# ---------------------------------------------------------------- default role set
#
# AWR worker profiles (owner, 2026-09-28): a starter catalog of named roles, used
# whenever ``roles`` is unset/empty (a fresh project) and to backfill any of these
# names an EXISTING user's saved roles section is missing (see _merge_default_roles).
# A user's own saved role of the same name is NEVER overwritten. "orchestrator" is
# the chat itself: it is a normal schema-valid entry so it appears in settings like
# any other role, but role_router.start refuses to launch it by name (see that
# module). Every model/effort pair here is checked against MODEL_CATALOG by
# _validate_role via the same helper used for any other saved role.
def _default_option(harness, model, effort=None):
    return {"harness": harness, "model": model, "effort": effort}


def _default_role(options, *, autonomy="ask", edits="ask", description=None,
                  preamble=None, brief_template=None):
    return {"options": options, "preamble": preamble, "autonomy": autonomy, "description": description,
            "edits": edits, "brief_template": brief_template}


# Small brief checklist skeleton (owner, 2026-09-28): a starter shape for the
# three worker roles' brief_template default, used only when a project has
# never saved its own template for that role (see _merge_default_roles --
# this never overwrites a user's saved value). Deliberately short; a caller
# fills each line in, it does not prescribe wording.
_WORKER_BRIEF_TEMPLATE = ("Goal / why:\nCurrent state:\nExact targets:\n"
                         "Must not break (including data saved by the previous version):\n"
                         "Where to look (RAM/NAV/docs):\nChecks:\nReport shape:\n"
                         "- Write reports and scratch files inside your own worktree. If the sandbox or permission "
                         "system denies a step, skip that step, report it, and continue independent authorized work. "
                         "Never route around the denial.\n"
                         "- Corrections from the coordinator may arrive mid-task. If one conflicts "
                         "with this brief, ask back before acting either way.")

# Discovery briefing shape (owner, 2026-09-29): the owner wants discovery workers
# briefed broadly -- the question and why it is asked, room to plan their own
# search, no time pressure -- and held to on-main/on-branch proof, not fenced in
# with a step list. This is guidance prose, not a fill-in skeleton like
# _WORKER_BRIEF_TEMPLATE above. Used only when a project has never saved its own
# discovery brief_template (see _merge_default_roles -- never overwrites a
# user's saved value).
_DISCOVERY_BRIEF_TEMPLATE = (
    "How to do discovery:\n"
    "- Start from the question and why it is asked. Anything you are told is already known is a lead: "
    "check it, and look beyond it.\n"
    "- Plan your own search. Take the time the question needs; there is no deadline. Save very large "
    "outputs to a file and work from a summary rather than skipping them.\n"
    "- Proof: a claim that something is, or is not, on main or a branch needs a check at that exact "
    "commit (for example `git cat-file -e <sha>:<path>` or `git grep <symbol> <sha>`); cite what you ran. "
    "If you could not check it, write UNKNOWN. A related feature is not the same feature.\n"
    "- Report each finding with where it lives and its evidence, what you checked and what you did not "
    "reach, and where you disagree with what you were told.\n"
    "- Anything you do not mention will be treated as unknown, not as absent. Name each item you saw but "
    "did not examine. A general 'not everything was checked' is not enough.\n"
    "- Write your report inside your own worktree. If the sandbox or permission system denies a step, "
    "skip that step, report it, and continue independent authorized work. Never route around the denial.\n"
    "- Corrections from the coordinator may arrive mid-task. If one conflicts with this brief, ask back "
    "before acting either way."
)

DEFAULT_ROLES = {
    "orchestrator": _default_role(
        [_default_option("claude", "sonnet", "medium")],
        autonomy="off", edits="yes",
        description="The chat itself: plans, delegates and verifies. Never launched directly."),
    "director": _default_role(
        [_default_option("claude", "opus", "high")],
        autonomy="ask", edits="yes",
        description="Hands-on director: review, second opinions, high-level campaign overviews "
                    "and revisions, and really important or big implementations."),
    "worker-critical": _default_role(
        [_default_option("claude", "opus", "high")],
        autonomy="ask", edits="yes", brief_template=_WORKER_BRIEF_TEMPLATE,
        description="Correctness-critical bounded work needing higher intelligence and judgment."),
    "worker-hard": _default_role(
        [_default_option("codex", "sol", "high"), _default_option("claude", "sonnet", "high")],
        autonomy="ask", edits="yes", brief_template=_WORKER_BRIEF_TEMPLATE,
        description="Substantial implementation, difficult debugging, and cross-owner review."),
    "worker-routine": _default_role(
        [_default_option("codex", "luna", "xhigh"), _default_option("claude", "sonnet", "medium")],
        autonomy="ask", edits="yes", brief_template=_WORKER_BRIEF_TEMPLATE,
        description="Routine bounded work: well-scoped, mostly mechanical tasks."),
    "worker-coding": _default_role(
        [_default_option("codex", "luna", "xhigh"), _default_option("claude", "sonnet", "medium")],
        autonomy="ask", edits="yes", brief_template=_WORKER_BRIEF_TEMPLATE,
        description="Bounded implementation work (the Coding job)."),
    "worker-docs": _default_role(
        [_default_option("codex", "luna", "xhigh"), _default_option("claude", "sonnet", "medium")],
        autonomy="ask", edits="yes", brief_template=_WORKER_BRIEF_TEMPLATE,
        description="Documentation work (the Documentation job)."),
    "worker-git": _default_role(
        [_default_option("codex", "luna", "xhigh"), _default_option("claude", "sonnet", "medium")],
        autonomy="ask", edits="yes", brief_template=_WORKER_BRIEF_TEMPLATE,
        description="Routine Git work (the Git job)."),
    "discovery": _default_role(
        [_default_option("codex", "luna", "xhigh"),
         _default_option("claude", "sonnet", "medium")],
        autonomy="ask", edits="ask", brief_template=_DISCOVERY_BRIEF_TEMPLATE,
        description="Unfamiliar mapping and widening discovery across the codebase."),
    "reviewer": _default_role(
        [_default_option("codex", "sol", "high"), _default_option("claude", "sonnet", "high")],
        autonomy="ask", edits="ask",
        description="Scoped before-and-after code review."),
}


def _merge_default_roles(roles, seeded=()):
    """Add any DEFAULT_ROLES name missing from ``roles``, but only when it has
    never been seeded into this project before -- deleting a default role must
    stick (coordinator review, 2026-09-28). ``seeded`` is the settings-level
    ``role_defaults_seeded`` sibling list (see ``_local``/``update_roles``): the
    DEFAULT_ROLES names already written into this project at least once. An
    empty ``seeded`` (a brand-new project) means "nothing seeded yet", so every
    default still applies. Never overwrites an existing entry."""
    if not isinstance(roles, dict):
        return roles
    merged = dict(roles)
    for name, default in DEFAULT_ROLES.items():
        if name not in merged and name not in seeded:
            merged[name] = deepcopy(default)
    return merged


def default_brief_template(role_name):
    """The DEFAULT_ROLES built-in ``brief_template`` for ``role_name``, or ``None``
    when that name has no built-in default (including a name outside
    DEFAULT_ROLES entirely). Read-only; never touches a saved roles file.

    Owner decision (2026-09-29): a saved role whose ``brief_template`` is empty
    or missing must still pick up a new built-in default template introduced
    after that role was saved. This project's seven roles were all saved
    earlier with empty templates (before ``_WORKER_BRIEF_TEMPLATE``/
    ``_DISCOVERY_BRIEF_TEMPLATE`` existed), so without this fallback the new
    defaults would never reach a live launch for any of them. See
    ``effective_brief_template``, the single place this is applied."""
    default = DEFAULT_ROLES.get(role_name)
    return default["brief_template"] if default else None


def effective_brief_template(role_name, entry):
    """The ``brief_template`` a role actually resolves to at launch/display time:
    ``entry``'s own saved value when it is a non-empty string, else this role's
    built-in default (``default_brief_template``), else ``None``.

    This NEVER writes back into the saved roles file -- it is applied only at
    read/resolve time (``role_router.resolve_role``), so a saved empty template
    stays exactly as saved on disk, and a project that later ships a different
    built-in default for this role name picks it up automatically. A
    non-empty saved template always wins unchanged."""
    saved = entry.get("brief_template") if isinstance(entry, dict) else None
    return saved if saved else default_brief_template(role_name)


def _roles_revision(roles):
    canonical = json.dumps(roles, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def read_roles(root):
    """Read the ``roles`` settings section with its own CAS revision (mirrors
    ``read_companion``: read-only, defaults when absent, never raises StaleSettings)."""
    root = _resolve_shared_root(root)
    with local_lock(root, create=False):
        roles = _local(root)["roles"]
        diagnostics = []
        for name, role in roles.items():
            if name == "orchestrator":
                continue
            for index, option in enumerate(role["options"], 1):
                reason = worker_model_disabled_reason(option["harness"], option["model"])
                if reason:
                    diagnostics.append({"role": name, "option": index, "model": option["model"],
                                        "code": "worker-model-disabled", "reason": reason})
        result = {"roles": deepcopy(roles), "revision": _roles_revision(roles)}
        if diagnostics:
            result["diagnostics"] = diagnostics
        return result


def _stored_roles(root):
    record = model_catalog._bounded_saved_record(root / ".eutonos-local" / "settings.json")
    return record.get("roles") if record.get("profile") == PROFILE else {}


def update_roles(root, roles, *, expected_revision):
    """Replace the ``roles`` settings section (mirrors ``update``'s CAS/locking/atomic-write
    pattern, scoped to just this section's own revision instead of the whole-settings one).

    AWR worker profiles: the merged (defaults-backfilled) shape is what every
    reader's revision is computed from -- ``_local``'s read path merges
    ``DEFAULT_ROLES`` into whatever is stored, using the project's current
    ``role_defaults_seeded`` list (see ``_merge_default_roles``) -- so a write
    here must merge (against that SAME, pre-write seeded list, so a first-ever
    write still backfills every not-yet-offered default) and persist that same
    shape too. Storing the caller's unmerged dict would desync this call's
    returned revision from the very next ``read_roles``/``update_roles`` CAS
    check, which always reads back through the merging ``_local`` path.

    Every current DEFAULT_ROLES name is then recorded as seeded (coordinator
    review, 2026-09-28): this is what makes a deliberate deletion of a default
    role stick from here on -- the next read/write's merge will no longer treat
    that name as eligible to be re-added -- while a future new entry added to
    DEFAULT_ROLES by a later code release (not yet in this project's seeded
    list) still appears once, the same as it would for a project that has never
    written roles at all."""
    if not isinstance(expected_revision, str):
        raise ValueError("Invalid expected_revision")
    root = _resolve_shared_root(root)
    validate_roles(roles, root=root, preserved=_stored_roles(root))
    with local_lock(root):
        local = _local(root)
        current_revision = _roles_revision(local["roles"])
        if expected_revision != current_revision:
            raise StaleSettings({"roles": deepcopy(local["roles"]), "revision": current_revision})
        validated_input = validate_roles(roles, root=root, preserved=_stored_roles(root))
        validated = _merge_default_roles(validated_input, seeded=local.get("role_defaults_seeded", []))
        local["roles"] = validated
        local["role_defaults_seeded"] = list(DEFAULT_ROLES)
        _atomic(root / ".eutonos-local" / "settings.json", local)
        return {"roles": deepcopy(validated), "revision": _roles_revision(validated)}


# ---------------------------------------------------------------- role hooks
#
# AWR role hooks (owner, 2026-09-28): short, role-specific reminder text injected
# into live sessions (Claude Code and Codex project hooks -- see eutonos.role_hooks)
# plus, for a cross-app chat created through Crosstalk (whose hooks do not know the role), baked
# directly into that chat's own prompt (see role_router._build_prompt). This
# section is the SINGLE owner of that reminder text.
#
# A "trigger" is one of HOOK_TRIGGERS. A "set" maps a role name (or the literal
# "default", used for any role the set does not name explicitly -- including the
# generic "worker" fallback role_hooks resolves for an undetected/child session)
# to per-trigger text. "eutonos-default" is a read-only built-in set: a write that
# submits it back unchanged round-trips fine; a write that submits it modified is
# rejected outright; an omitted "eutonos-default" is silently reinserted, so it can
# never actually be edited or deleted, only duplicated into a custom set and edited
# there (the dashboard's "duplicate" action does exactly that).
HOOK_TRIGGERS = ("message", "session_start", "edit_nudge", "stop_check", "long_turn")
MAX_HOOK_TEXT_CHARS = 800
BUILTIN_HOOK_SET_NAME = "eutonos-default"
HOOK_SET_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
MIN_EDIT_NUDGE_RATE_LIMIT_MINUTES = 1
MAX_EDIT_NUDGE_RATE_LIMIT_MINUTES = 1440
DEFAULT_EDIT_NUDGE_RATE_LIMIT_MINUTES = 10
MIN_LONG_TURN_THRESHOLD = 1
MAX_LONG_TURN_THRESHOLD = 1000
DEFAULT_LONG_TURN_THRESHOLD = 20


def _hook_role_texts(**by_trigger):
    return {trigger: by_trigger.get(trigger) for trigger in HOOK_TRIGGERS}


# Suggested defaults (owner brief, 2026-09-28), tuned to stay short. Haiku 5.5 or later is
# allowed (owner, 2026-10-22); these still mention only Luna/Sol/Sonnet, as Luna stays the preferred low-end route.
BUILTIN_HOOK_SETS = {
    BUILTIN_HOOK_SET_NAME: {"roles": {
        "orchestrator": _hook_role_texts(
            message="Follow AGENTS/campaign scope and owner-selected native models; saved routes are advisory. "
                   "Delegate bounded work: Luna routine, Sol hard, Sonnet for this chat's context. "
                   "role_router start: same-app gives native spawn instructions; cross-app creates the chat "
                   "through Crosstalk (create-claude or create-codex) when it is installed, on and not halted. "
                   "Keep direction, integration and verification; brief why, state, targets, constraints "
                   "and sources. Discover large or unknown scope first; small changes need a one-line brief. "
                   "Use native records, official tests and closeout. Git follows existing "
                   "authority and delivery configuration. This reminder grants no authority.",
            session_start="New session or resume: re-check scope, AGENTS.md/campaign rules and current board "
                         "state before acting. Before starting a board item, run "
                         "`eutonos-workflow expert precheck ITEM_ID --root .` (in a EUTONOS source checkout: "
                         "`python tools/workflow.py expert precheck ITEM_ID --root .`) to check for duplicate or "
                         "in-progress work. This reminder grants no authority.",
            edit_nudge="You're editing directly. Is this small, or should a worker take it?",
            stop_check="Before finishing, check whether you delivered what the user asked for. If blocked, "
                      "reconsider available tools or another agent, try a useful next step and verify it. "
                      "Account for workers and background tasks you started. Explain plainly what changed, "
                      "what you checked, unfinished work, user decisions and the next step.",
            long_turn="Long turn: are you doing work a worker should own (reading many files, editing)? "
                     "Delegate if so.",
        ),
        "director": _hook_role_texts(
            message="Review with fresh eyes: second opinions, campaign direction, big or critical "
                   "implementations. Report findings with evidence; don't fan out work unless asked. No new "
                   "authority.",
            session_start="New session or resume: review with fresh eyes; report findings with evidence. No "
                         "new authority.",
            edit_nudge="You're editing directly as Director. Is this small, or should a worker take it?",
            stop_check="Before finishing: did you answer the review question, back each finding with "
                      "evidence and separate verified facts from leads?",
            long_turn="Long turn: still reviewing, or should this move to a worker? Stay directed.",
        ),
        "discovery": _hook_role_texts(
            message="Do not delegate further. This is discovery: follow the question where it leads, cite "
                   "exact paths and evidence, and edit nothing outside any scratch folder your brief names. "
                   "If the sandbox or permission system denies an action, don't work around it: skip that "
                   "action, say so, and continue with the rest. A tool error or warning is not a denial; "
                   "handle it.",
            session_start="Resumed or started: re-read your brief before continuing; stay in scope.",
            stop_check="Before stopping: did you cite files/evidence for every claim, and does your report "
                      "match what you actually found?",
            long_turn="Long turn is fine: discovery takes as long as it needs. Save very large outputs to a "
                     "file and work from a summary instead of skipping them; verify citations before claiming "
                     "completion.",
        ),
        "reviewer": _hook_role_texts(
            message="Stay inside your brief; do not delegate further. This is review: find problems and cite "
                   "them; do not fix unless asked. If the sandbox or permission system denies an action, "
                   "don't work around it: skip that action, say so, and continue with the rest. A tool error "
                   "or warning is not a denial; handle it.",
            session_start="Resumed or started: re-read your brief before continuing; stay in scope.",
            stop_check="Before stopping: did you review only what your brief asked, without fixing unless "
                      "told to?",
            long_turn="Long turn: still reviewing only? Verify before claiming completion.",
        ),
        "default": _hook_role_texts(
            message="Stay inside your brief; do not delegate further. Edit only the files your brief names. "
                   "Run tests only with this repository's official test command (in EUTONOS: python tools/test_shards.py). If the sandbox or permission system "
                   "denies an action, don't work around it: skip that action, say so, and continue with the "
                   "rest. A tool error or warning is not a denial; handle it. Before a non-trivial change, "
                   "read the code you'll touch and its callers. If your brief lacks something you need, say "
                   "so instead of guessing.",
            session_start="Resumed or started: re-read your brief before continuing; stay in scope. Before a "
                         "non-trivial change, read the code you'll touch and its callers. If your brief lacks "
                         "something you need, say so instead of guessing.",
            stop_check="Before stopping: did you run the checks your brief required, and does your report "
                      "match what you actually did?",
            long_turn="Long turn: still inside your brief? Verify before claiming completion.",
        ),
        # worker-routine/worker-hard/worker-critical (owner brief, 2026-09-28):
        # explicit per-role entries (previously these three fell through to
        # "default" implicitly -- see role_hooks._text_for) so each is its own
        # editable row in the dashboard's hook-set grid, all sharing the same
        # worker guidance as "default" plus the read-before-you-edit addendum.
        "worker-routine": _hook_role_texts(
            message="Stay inside your brief; do not delegate further. Edit only the files your brief names. "
                   "Run tests only with this repository's official test command (in EUTONOS: python tools/test_shards.py). If the sandbox or permission system "
                   "denies an action, don't work around it: skip that action, say so, and continue with the "
                   "rest. A tool error or warning is not a denial; handle it. Before a non-trivial change, "
                   "read the code you'll touch and its callers. If your brief lacks something you need, say "
                   "so instead of guessing.",
            session_start="Resumed or started: re-read your brief before continuing; stay in scope. Before a "
                         "non-trivial change, read the code you'll touch and its callers. If your brief lacks "
                         "something you need, say so instead of guessing.",
            stop_check="Before stopping: did you run the checks your brief required, and does your report "
                      "match what you actually did?",
            long_turn="Long turn: still inside your brief? Verify before claiming completion.",
        ),
        "worker-hard": _hook_role_texts(
            message="Stay inside your brief; do not delegate further. Edit only the files your brief names. "
                   "Run tests only with this repository's official test command (in EUTONOS: python tools/test_shards.py). If the sandbox or permission system "
                   "denies an action, don't work around it: skip that action, say so, and continue with the "
                   "rest. A tool error or warning is not a denial; handle it. Before a non-trivial change, "
                   "read the code you'll touch and its callers. If your brief lacks something you need, say "
                   "so instead of guessing.",
            session_start="Resumed or started: re-read your brief before continuing; stay in scope. Before a "
                         "non-trivial change, read the code you'll touch and its callers. If your brief lacks "
                         "something you need, say so instead of guessing.",
            stop_check="Before stopping: did you run the checks your brief required, and does your report "
                      "match what you actually did?",
            long_turn="Long turn: still inside your brief? Verify before claiming completion.",
        ),
        "worker-critical": _hook_role_texts(
            message="Stay inside your brief; do not delegate further. Edit only the files your brief names. "
                   "Run tests only with this repository's official test command (in EUTONOS: python tools/test_shards.py). If the sandbox or permission system "
                   "denies an action, don't work around it: skip that action, say so, and continue with the "
                   "rest. A tool error or warning is not a denial; handle it. Before a non-trivial change, "
                   "read the code you'll touch and its callers. If your brief lacks something you need, say "
                   "so instead of guessing.",
            session_start="Resumed or started: re-read your brief before continuing; stay in scope. Before a "
                         "non-trivial change, read the code you'll touch and its callers. If your brief lacks "
                         "something you need, say so instead of guessing.",
            stop_check="Before stopping: did you run the checks your brief required, and does your report "
                      "match what you actually did?",
            long_turn="Long turn: still inside your brief? Verify before claiming completion.",
        ),
    }},
}


def _validate_hook_role_texts(value):
    if not isinstance(value, dict) or set(value) - set(HOOK_TRIGGERS):
        raise ValueError("Invalid hook role texts")
    result = {}
    for trigger in HOOK_TRIGGERS:
        text = value.get(trigger)
        if text is not None and (not isinstance(text, str) or not text or len(text) > MAX_HOOK_TEXT_CHARS):
            raise ValueError(f"Invalid hook text for {trigger} (1-{MAX_HOOK_TEXT_CHARS} characters, or null)")
        result[trigger] = text
    return result


def _hook_role_name_valid(name):
    return isinstance(name, str) and (name == "default" or ROLE_NAME_RE.fullmatch(name))


def _validate_hook_set(value):
    if not isinstance(value, dict) or set(value) != {"roles"}:
        raise ValueError("Invalid hook set")
    roles = value["roles"]
    if not isinstance(roles, dict):
        raise ValueError("Invalid hook set roles")
    result = {}
    for name, texts in roles.items():
        if not _hook_role_name_valid(name):
            raise ValueError("Invalid hook set role name")
        result[name] = _validate_hook_role_texts(texts)
    return {"roles": result}


def validate_hooks(value):
    """Validate the ``hooks`` settings section. Unknown keys anywhere are rejected.

    ``sets`` always contains ``BUILTIN_HOOK_SET_NAME`` on return: if the caller's
    value omits it, it is reinserted unchanged (read-only, so there is nothing to
    merge); if the caller's value names it but with different content, this raises
    -- the built-in set can never actually be edited or deleted, only duplicated
    into a custom set (a normal, mutable ``sets`` entry) and edited there."""
    required = {"enabled", "active_set", "role_enabled", "trigger_enabled",
                "edit_nudge_rate_limit_minutes", "long_turn_threshold", "sets"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"closeout_check"}:
        raise ValueError("Invalid hooks settings")
    if type(value.get("enabled")) is not bool:
        raise ValueError("Invalid hooks enabled flag")
    if type(value.get("closeout_check", True)) is not bool:
        raise ValueError("Invalid hooks closeout_check flag")
    role_enabled = value.get("role_enabled")
    if (not isinstance(role_enabled, dict)
            or any(not _hook_role_name_valid(name) or type(enabled) is not bool
                  for name, enabled in role_enabled.items())):
        raise ValueError("Invalid hooks role_enabled")
    trigger_enabled = value.get("trigger_enabled")
    if (not isinstance(trigger_enabled, dict) or set(trigger_enabled) != set(HOOK_TRIGGERS)
            or any(type(enabled) is not bool for enabled in trigger_enabled.values())):
        raise ValueError("Invalid hooks trigger_enabled")
    rate_limit = value.get("edit_nudge_rate_limit_minutes")
    if (type(rate_limit) is not int or isinstance(rate_limit, bool)
            or not MIN_EDIT_NUDGE_RATE_LIMIT_MINUTES <= rate_limit <= MAX_EDIT_NUDGE_RATE_LIMIT_MINUTES):
        raise ValueError(f"edit_nudge_rate_limit_minutes must be between {MIN_EDIT_NUDGE_RATE_LIMIT_MINUTES} "
                         f"and {MAX_EDIT_NUDGE_RATE_LIMIT_MINUTES}")
    threshold = value.get("long_turn_threshold")
    if (type(threshold) is not int or isinstance(threshold, bool)
            or not MIN_LONG_TURN_THRESHOLD <= threshold <= MAX_LONG_TURN_THRESHOLD):
        raise ValueError(f"long_turn_threshold must be between {MIN_LONG_TURN_THRESHOLD} "
                         f"and {MAX_LONG_TURN_THRESHOLD}")
    sets = value.get("sets")
    if not isinstance(sets, dict):
        raise ValueError("Invalid hooks sets")
    result_sets = {}
    for name, definition in sets.items():
        if not isinstance(name, str) or not HOOK_SET_NAME_RE.fullmatch(name):
            raise ValueError("Invalid hook set name")
        validated_definition = _validate_hook_set(definition)
        if name == BUILTIN_HOOK_SET_NAME and validated_definition != BUILTIN_HOOK_SETS[BUILTIN_HOOK_SET_NAME]:
            raise ValueError("the built-in hook set is read-only")
        result_sets[name] = validated_definition
    result_sets.setdefault(BUILTIN_HOOK_SET_NAME, deepcopy(BUILTIN_HOOK_SETS[BUILTIN_HOOK_SET_NAME]))
    active_set = value.get("active_set")
    if not isinstance(active_set, str) or active_set not in result_sets:
        raise ValueError("Invalid active hook set")
    return {"enabled": value["enabled"], "closeout_check": value.get("closeout_check", True),
            "active_set": active_set, "role_enabled": dict(role_enabled),
            "trigger_enabled": {trigger: trigger_enabled[trigger] for trigger in HOOK_TRIGGERS},
            "edit_nudge_rate_limit_minutes": rate_limit, "long_turn_threshold": threshold, "sets": result_sets}


def _refresh_builtin_hook_set(hooks):
    """Read side only (2026-09-29): every saved settings.json carries a copy of the
    read-only built-in set, because ``validate_hooks`` always reinserts it. That copy
    is stale data, not an edit, so a read swaps in the current built-in. Without
    this, changing any built-in text made every older settings.json fail to read
    (settings status, the dashboard, role launches) and silently turned reminders off.
    Writes still go through ``validate_hooks`` unchanged and refuse a real edit."""
    if isinstance(hooks, dict) and isinstance(hooks.get("sets"), dict) and BUILTIN_HOOK_SET_NAME in hooks["sets"]:
        sets = {**hooks["sets"], BUILTIN_HOOK_SET_NAME: deepcopy(BUILTIN_HOOK_SETS[BUILTIN_HOOK_SET_NAME])}
        return {**hooks, "sets": sets}
    return hooks


# Every trigger defaults on. "stop_check" is the end-turn pass (owner,
# 2026-10-05): Stop (and a Codex SubagentStop) blocks exactly once per turn with the role's
# text so the agent reconsiders delivery, retries within authority and applies
# the recording policy. The owner accepts the extra continuation's cost.
_DEFAULT_TRIGGER_ENABLED = {trigger: True for trigger in HOOK_TRIGGERS}

DEFAULT_HOOKS = {"enabled": True, "closeout_check": True, "active_set": BUILTIN_HOOK_SET_NAME, "role_enabled": {},
                 "trigger_enabled": dict(_DEFAULT_TRIGGER_ENABLED),
                 "edit_nudge_rate_limit_minutes": DEFAULT_EDIT_NUDGE_RATE_LIMIT_MINUTES,
                 "long_turn_threshold": DEFAULT_LONG_TURN_THRESHOLD,
                 "sets": {BUILTIN_HOOK_SET_NAME: deepcopy(BUILTIN_HOOK_SETS[BUILTIN_HOOK_SET_NAME])}}

def _hooks_revision(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def read_hooks(root, *, timeout=None):
    """Read the ``hooks`` settings section with its own CAS revision (mirrors
    ``read_roles``: read-only, defaults when absent, never raises StaleSettings).

    ``timeout`` (finding 4, Codex verification, 2026-09-28): passed straight
    through to ``local_lock`` -- ``None`` (the default) blocks exactly as
    before for every existing caller; ``role_hooks.py``'s hook program is the
    one caller that passes a short bound so a settings write it lands mid-way
    through can never make it block, and raises ``TimeoutError`` instead."""
    root = _resolve_shared_root(root)
    with local_lock(root, create=False, timeout=timeout):
        hooks = _local(root)["hooks"]
        return {"hooks": deepcopy(hooks), "revision": _hooks_revision(hooks)}


# Finding 5 (coordinator recheck, 2026-09-28): role_hooks.py's own hot path
# must never do an unbounded read of settings.json -- ``read_hooks``/``_local``
# read the WHOLE file unconditionally, which is fine for every ordinary
# settings consumer but not for a hook program that must never be made to
# read something an external write (or a bug) grew arbitrarily large.
HOOKS_BOUNDED_READ_MAX_BYTES = 256 * 1024


def read_hooks_bounded(root, *, timeout=None, max_bytes=HOOKS_BOUNDED_READ_MAX_BYTES):
    """A capped, hooks-only read path for ``role_hooks.py``'s hook program
    (finding 5, coordinator recheck, 2026-09-28) -- NOT a replacement for
    ``read_hooks``, which every ordinary settings consumer (the dashboard,
    the settings CLI) keeps using unchanged.

    ``read_hooks``/``_local`` call ``path.read_text(...)`` on the WHOLE
    ``settings.json`` unconditionally, then validate every section
    (task_rules, ram, roles, hooks). That is correct and
    necessary for a real settings read/write, but role_hooks.py's hook
    program fires on nearly every tool call in a live session and must never
    be made to read an unbounded amount of data, whatever the cause (a bug
    elsewhere, a corrupted file, disk corruption). This instead opens the
    file directly and reads at most ``max_bytes + 1`` bytes: anything over
    the cap is treated exactly like any other read error this function's
    caller already handles -- returns ``None`` (never falls through to
    reading the rest of an oversized file). It then validates only ``hooks``
    -- never touching roles/task_rules/ram, which are
    irrelevant to a hook firing.

    Returns the validated ``hooks`` dict, or ``None`` on any error (oversized
    file, missing/corrupt JSON, a lock timeout, or a validation failure) --
    every caller already treats ``None`` as "no reminder this time"."""
    try:
        root = _resolve_shared_root(root)
        path = root / ".eutonos-local" / "settings.json"
        with local_lock(root, create=False, timeout=timeout):
            if not path.exists():
                return deepcopy(DEFAULT_HOOKS)
            with path.open("rb") as fh:
                raw = fh.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict) or value.get("profile") != PROFILE:
                return None
            hooks = value.get("hooks")
            if not isinstance(hooks, dict):
                hooks = deepcopy(DEFAULT_HOOKS)
            return validate_hooks(_refresh_builtin_hook_set(hooks))
    except Exception:
        return None


def update_hooks(root, value, *, expected_revision):
    """Replace the ``hooks`` settings section (mirrors ``update_roles``'s CAS/
    locking/atomic-write pattern, scoped to just this section's own revision)."""
    if not isinstance(expected_revision, str):
        raise ValueError("Invalid expected_revision")
    validated_input = validate_hooks(value)
    root = _resolve_shared_root(root)
    with local_lock(root):
        local = _local(root)
        current_revision = _hooks_revision(local["hooks"])
        if expected_revision != current_revision:
            raise StaleSettings({"hooks": deepcopy(local["hooks"]), "revision": current_revision})
        local["hooks"] = validated_input
        _atomic(root / ".eutonos-local" / "settings.json", local)
        return {"hooks": deepcopy(validated_input), "revision": _hooks_revision(validated_input)}


def _local(root):
    root = _resolve_shared_root(root)
    path = root / ".eutonos-local" / "settings.json"
    default = {"profile": PROFILE, "task_rules": dict(DEFAULT_TASK_RULES),
               "ram": {"retrieval": "on-demand", "capture": True,
                       "cadence": "every-turn", "detail": "summary"},
               "roles": _merge_default_roles({}), "role_defaults_seeded": [],
               "hooks": deepcopy(DEFAULT_HOOKS), "harnesses": deepcopy(DEFAULT_HARNESSES)}
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("profile") != PROFILE:
        raise ValueError("Invalid local settings")
    # A key retired from the settings is ignored, not an error, so an older file still loads.
    value = {key: item for key, item in value.items() if key not in _LEGACY_SETTINGS_KEYS}
    if set(value) - set(default):
        raise ValueError("Invalid local settings")
    # A section added after this file was written starts at its default.
    value = {**deepcopy(default), **value}
    # Files saved before "review" existed lack it; closeout is always the main chat.
    rules = _normalize_task_rules(value.get("task_rules"), strict_closeout=False)
    seeded_raw = value.get("role_defaults_seeded")
    seeded = [name for name in seeded_raw if isinstance(name, str)] if isinstance(seeded_raw, list) else []
    saved_roles = validate_roles(value.get("roles"), allow_unavailable=True)
    if "task_rules" in value and isinstance(value.get("task_rules"), dict):
        rules, saved_roles = _split_shared_roles(rules, saved_roles, seeded)
    return {**value, "task_rules": rules, "ram": _normalize_ram(value.get("ram")),
            "role_defaults_seeded": seeded,
            "harnesses": validate_harnesses(value.get("harnesses")),
            "roles": _merge_default_roles(saved_roles, seeded=seeded),
            "hooks": validate_hooks(_refresh_builtin_hook_set(value.get("hooks")))}


def validate_harnesses(value):
    """Per-harness worker settings: ``{harness: {enabled: bool, max_workers: 1-7}}``.

    Every ROLE_HARNESSES entry is required on write; a missing harness on read
    takes its default so older files and partial writers still load."""
    if not isinstance(value, dict) or set(value) - set(ROLE_HARNESSES):
        raise ValueError("Invalid harness settings")
    result = {}
    for name in ROLE_HARNESSES:
        entry = value.get(name, DEFAULT_HARNESSES[name])
        if (not isinstance(entry, dict) or set(entry) != {"enabled", "max_workers"}
                or type(entry["enabled"]) is not bool or type(entry["max_workers"]) is not int
                or not 1 <= entry["max_workers"] <= MAX_HARNESS_WORKERS):
            raise ValueError(f"Invalid {name} harness settings "
                             f"(worker limit must be 1 to {MAX_HARNESS_WORKERS})")
        result[name] = {"enabled": entry["enabled"], "max_workers": entry["max_workers"]}
    return result


def harness_worker_cap(harnesses):
    """Overall concurrent-worker cap implied by the harness limits (1 to 7)."""
    total = sum(entry["max_workers"] for entry in harnesses.values() if entry["enabled"])
    return max(1, min(MAX_COMPANIONS, total))


def _normalize_task_rules(value, *, strict_closeout=True):
    """Validate shape and return the full rule map.

    ``review`` is filled from its default when absent (older saved files).
    Closeout is fixed to the main chat: a write naming anything else is
    rejected (``strict_closeout``); a read of an older file silently forces it."""
    if not isinstance(value, dict) or not set(value) <= set(TASKS) or not set(TASKS) - {"review"} <= set(value):
        raise ValueError("Invalid task rules")
    result = {task: value.get(task, DEFAULT_TASK_RULES[task]) for task in TASKS}
    for rule in result.values():
        if not isinstance(rule, str) or not rule:
            raise ValueError("Invalid task rules")
    if result["closeout"] != CLOSEOUT_ROLE:
        if strict_closeout:
            raise ValueError("Closeout is fixed to the main chat (orchestrator)")
        result["closeout"] = CLOSEOUT_ROLE
    return result


def _split_shared_roles(rules, roles, seeded):
    """Migration (2026-10-14): jobs that shared one role (coding, docs and git all
    mapped to ``worker-routine``) each get their own copy, ``worker-<job>``, so
    the per-job table can edit one without moving the others. The copy is taken
    from the owner's saved role, never from a built-in default, so customised
    roles carry over unchanged. Only names the project has not been seeded with
    are created, and a role is left shared when no free name exists."""
    original = dict(rules)
    rules, roles = dict(rules), dict(roles)
    for task in WORKER_TASKS:
        shared = original[task]
        if shared == CLOSEOUT_ROLE or shared not in roles:
            continue
        if not any(other != task and original[other] == shared for other in WORKER_TASKS):
            continue
        own = f"worker-{task}"
        if own in roles or own in seeded:
            continue
        roles[own] = deepcopy(roles[shared])
        rules[task] = own
    return rules, roles


def _validate_task_rules(value):
    """A task_rules value maps every task to a role name, or the
    ``"orchestrator"`` sentinel (unassigned; the primary/orchestrator keeps
    the task). This checks shape only (every task present, every value a
    non-empty string) and is used on both the read path (``_local``)
    and the write path (``update``), where a value that merely looks like a
    role name but does not actually exist yet is still allowed through here.

    The cross-check against the current ``roles`` section -- rejecting a
    value that names a role that does not exist in this project (and is not
    ``"orchestrator"``) -- happens only in ``update``'s own ``task_rules``
    branch (coordinator review, 2026-09-28), since only there is a live
    ``roles`` section available under the same lock to check against. A
    stale mapping that predates this check (e.g. one written before this
    validation existed, or one that goes stale because its named role was
    deleted afterwards) is still caught at resolution time instead
    (``role_router.start``/``resolve_role`` raise ``unknown-role``), the same
    way a role option's own model/effort is validated independently of any
    other section."""
    _normalize_task_rules(value)


def _normalize_ram(value, current=None):
    if not isinstance(value, dict) or not set(value) <= {"retrieval", "capture", "cadence", "detail"}:
        raise ValueError("Invalid RAM settings")
    baseline = current or {"retrieval": "on-demand", "capture": True,
                           "cadence": "every-turn", "detail": "summary"}
    result = {**baseline, **value}
    if (set(result) != {"retrieval", "capture", "cadence", "detail"}
            or result.get("retrieval") not in ("on-demand", "off")
            or type(result.get("capture")) is not bool
            or result.get("cadence") not in CAPTURE_CADENCES
            or result.get("detail") not in CAPTURE_DETAILS):
        raise ValueError("Invalid RAM settings")
    return result


def _validate_ram(value):
    _normalize_ram(value)


def _effective(root, project):
    local = _local(root)
    companion = read_companion(root)
    routing = {key: companion[key] for key in ("enabled", "routing", "model", "effort",
                                                 "allowed_models", "routing_profile",
                                                 "max_concurrent_companions")}
    if "harnesses" in model_catalog._bounded_saved_record(root / ".eutonos-local" / "settings.json"):
        # Explicit per-harness limits derive the overall cap (an unsaved file keeps the stored one).
        routing["max_concurrent_companions"] = harness_worker_cap(local["harnesses"])
    sections = {"routing": routing, "harnesses": deepcopy(local["harnesses"]),
                "task_rules": deepcopy(local["task_rules"]),
                "ram": deepcopy(local["ram"]), "tracking": {"enabled": tracking(root)["enabled"]}}
    canonical = json.dumps(sections, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    catalog = model_catalog.describe_catalog(root)
    return {"project": project, "revision": hashlib.sha256(canonical).hexdigest(), **sections,
            "models": list(catalog["models"]), "efforts": list(EFFORTS),
            "model_catalog": catalog}


def status(root, project=0):
    require_shared(root)
    if type(project) is not int or project < 0:
        raise ValueError("project must be a nonnegative integer")
    selected_root = Path(root).resolve()
    root = _resolve_shared_root(root)
    with local_lock(root, create=False):
        result = _effective(root, project)
    result.update(delivery_status(selected_root))
    from .shared_work import load as shared_load
    try:
        shared = shared_load(selected_root)
        result.update(shared_work=shared, shared_work_revision=shared['revision'])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(shared_work={'enabled': False, 'status': 'unavailable', 'error': str(exc)}, shared_work_revision=None)
    from .work_attribution import profile_hint
    profile = profile_hint(selected_root)
    result.update(participant_profile=profile, participant_profile_revision=profile['revision'])
    return result


def delivery_status(root):
    require_shared(root)
    from .git_delivery_native import configuration
    try:
        value = configuration(root)
        return {'delivery': value, 'delivery_revision': value['revision']}
    except (OSError, ValueError, TypeError, KeyError, __import__('sqlite3').Error) as exc:
        return {'delivery': {'mode': 'unavailable', 'error': str(exc)}, 'delivery_revision': None}


def update_delivery(root, value, expected_revision):
    require_shared(root)
    from .git_delivery import Engine, DeliveryError
    engine = Engine(root)
    cfg = engine.config()
    if type(expected_revision) is not int or expected_revision != cfg['revision']:
        raise StaleSettings({'delivery': cfg, 'revision': cfg['revision']})
    result = engine.configure(value, expected_revision=expected_revision)
    return {'delivery': result, 'revision': result['revision']}


def update(root, project, revision, section, value):
    if section == 'participant_profile':
        from .work_attribution import configure_profile
        result = configure_profile(root, value, revision)
        return {'participant_profile': result, 'revision': result['revision']}
    if section in {'shared_work', 'shared_auto_fetch'}:
        from .shared_work import configure
        if section == 'shared_auto_fetch' and (not isinstance(value, dict) or set(value) != {'auto_fetch'}):
            raise ValueError('Invalid auto-fetch preference.')
        return configure(root, value, revision)
    if section == 'delivery':
        if type(project) is not int or project < 0:
            raise ValueError('Invalid selected project')
        return update_delivery(root, value, revision)
    if type(project) is not int or project < 0 or not isinstance(revision, str):
        raise ValueError("Invalid project or revision")
    if section not in ("routing", "harnesses", "task_rules", "ram", "tracking") or not isinstance(value, dict):
        raise ValueError("Invalid settings section")
    if section == "routing":
        root = _resolve_shared_root(root)
        previous = read_companion(root) if (root / ".eutonos-local" / "companion.json").exists() else None
        # Invalid input must not create even a lock directory. Revalidate below
        # under the CAS lock because this read-only preflight can race a save.
        validate_routing(value, root=root, preserved=previous)
    elif section == "tracking":
        if set(value) != {"enabled"} or type(value.get("enabled")) is not bool:
            raise ValueError("Invalid tracking settings")
    elif section == "task_rules":
        _validate_task_rules(value)
    elif section == "harnesses":
        value = validate_harnesses(value)
    elif section == "ram":
        _normalize_ram(value)
    root = _resolve_shared_root(root)
    with local_lock(root):
        current = _effective(root, project)
        if revision != current["revision"]:
            raise StaleSettings(current)
        if section == "routing":
            if set(value) == {"enabled", "routing", "model", "effort", "allowed_models"}:
                value = {**value, "routing_profile": "optimus", "max_concurrent_companions": 1}
            previous = current["routing"] if (root / ".eutonos-local" / "companion.json").exists() else None
            validate_routing(value, root=root, preserved=previous)
            _atomic(root / ".eutonos-local" / "companion.json", {"profile": COMPANION_PROFILE, **value})
        elif section == "tracking":
            set_tracking(root, value["enabled"])
        elif section == "task_rules":
            # Cross-check against the LIVE roles section (coordinator review,
            # 2026-09-28): _validate_task_rules above only checks shape, since it
            # also runs on the read path where no roles section is at
            # hand. Only here, under the same lock as the write, can a value
            # naming a role that does not actually exist in this project (and
            # is not the "orchestrator" sentinel) be rejected up front.
            local = _local(root)
            unknown = sorted({name for name in value.values()
                              if name != "orchestrator" and name not in local["roles"]})
            if unknown:
                raise ValueError(f"task_rules names unknown role(s): {', '.join(unknown)}")
            local["task_rules"] = _normalize_task_rules(value)
            _atomic(root / ".eutonos-local" / "settings.json", local)
        elif section == "harnesses":
            local = _local(root)
            local["harnesses"] = value
            _atomic(root / ".eutonos-local" / "settings.json", local, keep_harnesses=True)
        else:
            local = _local(root)
            local[section] = (_normalize_ram(value, local["ram"]) if section == "ram" else deepcopy(value))
            _atomic(root / ".eutonos-local" / "settings.json", local)
        return _effective(root, project)


__all__ = ["CAPTURE_CADENCES", "CAPTURE_DETAILS", "COMPANION_PROFILE", "DEFAULT_ROLES",
           "DEFAULT_TASK_RULES", "DEFAULT_HARNESSES", "MAX_HARNESS_WORKERS", "WORKER_TASKS", "EFFORTS",
           "validate_harnesses", "harness_worker_cap", "MODEL_CATALOG", "MODELS", "MODEL_EFFORTS",
           "MODEL_HARNESS", "MAX_ROLE_OPTIONS", "MAX_ROLE_PREAMBLE_CHARS",
           "MAX_ROLE_DESCRIPTION_CHARS", "MAX_ROLE_BRIEF_TEMPLATE_CHARS", "ROLE_EDITS",
           "ROLE_AUTONOMY", "ROLE_HARNESSES", "ROLE_NAME_RE", "StaleSettings", "TASKS", "read_companion",
           "read_roles", "status", "update", "update_roles",
           "validate_roles", "validate_routing",
           "HOOK_TRIGGERS", "MAX_HOOK_TEXT_CHARS", "BUILTIN_HOOK_SET_NAME", "HOOK_SET_NAME_RE",
           "MIN_EDIT_NUDGE_RATE_LIMIT_MINUTES", "MAX_EDIT_NUDGE_RATE_LIMIT_MINUTES",
           "DEFAULT_EDIT_NUDGE_RATE_LIMIT_MINUTES", "MIN_LONG_TURN_THRESHOLD", "MAX_LONG_TURN_THRESHOLD",
           "DEFAULT_LONG_TURN_THRESHOLD", "BUILTIN_HOOK_SETS", "DEFAULT_HOOKS",
           "validate_hooks", "read_hooks", "read_hooks_bounded", "HOOKS_BOUNDED_READ_MAX_BYTES", "update_hooks"]
