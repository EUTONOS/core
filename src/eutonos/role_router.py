"""Resolve a settings ``roles`` entry to a concrete launch and, for cross-app calls,
create the worker chat through Crosstalk (``tools/crosstalk/crosstalk_p0_cli.py``
``create-codex`` / ``create-claude``; guide ``docs/Crosstalk/README.md``).

CROSS-APP START THROUGH CROSSTALK
  A cross-app start runs the Crosstalk CLI with the
  installed runtime interpreter (``-I -B``) to create a native Desktop chat for the
  target harness (a Codex role from Claude Code uses ``create-codex``, a Claude role
  from Codex uses ``create-claude``), with the assembled prompt passed as a UTF-8
  file. Crosstalk must be installed, messaging on and not halted: otherwise the
  launch is refused visibly with ``crosstalk-not-installed``, ``crosstalk-halted``
  or ``crosstalk-messaging-off`` (``crosstalk_status``), with no other route. Crosstalk takes an exact model and an explicit effort, so the
  resolved model id (never the family name) and the role's effort are passed; a role
  with no effort is refused with ``effort-required``. Creation is never retried: a
  failure after the chat may exist names it (``thread_id``) in the error details.

WORKER PROFILES (AWR worker profiles, owner, 2026-09-28)
  ``start`` is the one entry point either app uses to launch a role, named directly
  OR looked up from a task via the settings ``task_rules`` mapping (a task maps to one
  role name, or to ``"orchestrator"``/unassigned, meaning the primary keeps it -- see
  ``settings.TASKS``/``settings._validate_task_rules``). It resolves the option with
  the same ``resolve_role``/profile-placement logic ``open_role`` already uses, then:
    - same harness as the caller: nothing is launched here. It returns the harness,
      model, effort, the role's ``edits`` default, and the assembled prompt (preamble
      + brief_template + brief + reminder) for the CALLER to spawn with its own native
      subagent/session tool (Claude Code's Agent tool, Codex's own subagent spawn).
    - a different harness: created through Crosstalk exactly like ``open_role``
      (autonomy still gates it exactly as today). A resolved Claude option that cannot run
      auto mode (Haiku 4.5 and older) is refused on both paths, visibly rather than silently
      selecting another model; Haiku 5.5 and later is allowed.
  ``"orchestrator"`` is the chat itself -- it plans, delegates and verifies, and is
  never launched -- so ``start`` refuses it by name before resolving anything, with a
  clear message; it otherwise remains a normal, schema-valid entry in ``roles`` (so it
  still appears in settings like any other role).

WHAT THIS DOES NOT DO
  This module itself does not edit project work. New Crosstalk-created chats are ordinary
  working chats and may run commands and edit files under their native permissions.
  Role autonomy is off or ask. The historical auto-read-only settings value is
  accepted as an alias for ask; it cannot silently launch a write-capable session.
  Explicit named calls remain separate from automatic role dispatch.

HERMES
  ``"hermes"`` is a valid schema value for a role option's ``harness`` (see
  ``settings.ROLE_HARNESSES``), so the schema will not need another migration once
  hermes exists.  But hermes is not wired up: resolving a role whose chosen option
  names hermes, or opening one, raises ``RoleRouterError("unsupported", ...)``.  No
  code path here ever launches hermes.

SAME-APP CALLS
  If the resolved harness equals the caller's own harness (``from_harness``),
  Crosstalk is not involved at all -- same-app fan-out is each app's own job (its own
  native subagent/session spawn).  ``open_role`` reports this and returns without
  creating anything.

EXPLICIT CALLS ALWAYS WORK
  ``send_explicit`` is a thin, direct Crosstalk chat creation (mode ``new`` only).  It
  takes no role and is never gated by any role's autonomy setting: an explicit, named
  call (harness + model + effort chosen by the caller) is always available, regardless
  of what any role's autonomy is set to.  Autonomy only ever gates AUTOMATIC use
  through ``open_role``.  Messaging an EXISTING chat (the old ``resume``/``queue``/
  ``deliver`` modes) is Crosstalk's ``crosstalk_send`` tool, not this module.

CLI
  ``python -m eutonos.role_router {resolve,open,start,send-explicit} --root R ...``
  JSON output on stdout; a non-zero exit code marks an error (see ``RoleRouterError``).
  ``start`` is the one entry point either app uses to launch a role -- by ``--role``
  NAME or by ``--task`` KIND (see "WORKER PROFILES" above); ``resolve``/``open`` remain
  as they were.

PROFILE PLACEMENT (AWR7, 2026-09-24)
  For a role with two options, an explicit ``choice`` (``"primary"`` or
  ``"secondary"``) always wins -- this is unchanged, and remains the caller's
  escape hatch regardless of profile or autonomy.  Only when ``choice`` is
  omitted does the companion routing profile (``settings.read_companion``'s
  ``routing_profile``: ``"cost-savings"``, ``"optimus"`` default, or
  ``"high-quality"``) pick the default: cost-savings defaults to secondary
  when one is configured (falling back to the role's only option otherwise),
  high-quality defaults to primary, and optimus keeps today's plain default
  (primary) unchanged.  This reduces Claude orchestration/messaging cost the
  same way the profile already reduces companion-delegation cost, without
  touching autonomy or the always-available explicit-choice override.

CALLER PROVENANCE AND VISIBILITY
  ``start``/``open_role`` accept optional ``caller_session`` and
  ``caller_work_id`` values. They use the caller-reference pattern
  (``CALLER_REF_RE``) and are echoed in the result as
  ``caller_session_id``/``caller_work_id``; Crosstalk's create commands have no field
  for them, so they are not sent anywhere. They identify provenance only and never
  grant permission or create worker RAM. When provenance is supplied, cross-app
  results label the route ``visibility="visible-chat"`` (Crosstalk creates a native
  Desktop chat). Same-app results label their native-spawn descriptor as
  ``visibility="owner-native-handoff"``. Those labels describe the route; this module
  does not observe or promise UI/sidebar visibility. Omitting provenance omits the
  labels.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import crosstalk_status
from . import home as eutonos_home
from . import role_hooks
from . import settings
from . import model_catalog

PREVIEW_CHARS = 500

# Optional caller provenance (AWR-Y): opaque attribution labels, never used for routing.
CALLER_REF_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# Chats Crosstalk can create: the two Desktop apps. Hermes is not wired (see HERMES above).
CREATE_HARNESSES = ("claude", "codex")
# Mirrors crosstalk_p0_core.EFFORTS, so a bad effort is refused here with a clear
# message instead of as an argparse usage error from the CLI.
CROSSTALK_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# R4 (AWR-FR): from_harness is caller-declared and can be honestly wrong (an
# app started with the wrong flag) as well as spoofed. Cross-check it against
# a harness-specific environment marker the real host process sets, and
# refuse an outright mismatch. Only Claude Code's marker is confirmed today
# (observed directly in this session's own environment: CLAUDECODE=1). A
# Codex CLI equivalent is not yet confirmed against a live session -- nothing
# in this repository defines or relies on one -- so it is intentionally left
# unwired rather than guessed at. See AWR_FR_OPUS_REVIEW_2026-09-24.md R4.
HARNESS_ENV_MARKERS = {"claude": "CLAUDECODE"}


class RoleRouterError(Exception):
    """A rejected or failed operation with a short stable ``code``."""

    def __init__(self, code: str, message: str = "", details: dict | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code
        self.details = details


# ---------------------------------------------------------------- resolve


_CHOICE_ALIAS = {"primary": 1, "secondary": 2}


def _profile_default_choice(root, num_options: int) -> tuple[int, str | None]:
    """AWR7 profile placement: the default position when the caller passes none.

    Only ever called when ``choice`` was omitted -- an explicit choice from the
    caller never reaches this. Read-only (``read_companion``); any failure to
    read settings (e.g. a corrupt local file) falls back to position 1, the same
    default this function is replacing, rather than raising out of a read-only
    resolve. AWR-CT (2026-09-28): generalized from a binary primary/secondary
    pick to a position among up to ``MAX_ROLE_OPTIONS`` (4) options; a role with
    fewer than 2 options is unaffected (always position 1).

    Returns ``(position, reason)``. ``reason`` is a short, plain-text explanation
    of which routing profile picked this default (AWR-CT ``auto_reason``); it is
    ``None`` for a single-option role, where no auto choice was actually made."""
    if num_options < 2:
        return 1, None
    try:
        profile = settings.read_companion(root)["routing_profile"]
    except (ValueError, OSError):
        return 1, "routing profile unavailable; defaulted to primary"
    if profile == "cost-savings":
        return 2, "routing profile 'cost-savings' selected secondary"
    return 1, f"routing profile {profile!r} selected primary"


def resolve_role(root, role, *, choice=None) -> dict:
    """Read-only: pick one of a role's 1-4 options and return it plus preamble/autonomy.

    ``choice`` is ``"primary"``, ``"secondary"``, an ordered position 1-4 (as an
    ``int`` or a digit string), or ``None``. An explicit choice always wins.
    ``"primary"``/``"secondary"`` remain accepted aliases for positions 1/2, so a
    role saved in the old 2-option shape keeps resolving the same way. ``None``
    defaults per the companion routing profile -- see "PROFILE PLACEMENT" in this
    module's docstring -- rather than unconditionally position 1. Raises
    ``RoleRouterError`` for an unknown role, an unrecognized choice, or a choice
    naming a position the role does not have (a bare ``"secondary"`` on a
    one-option role keeps the historical ``no-secondary`` code; any other
    out-of-range position raises ``no-such-position``).

    Also returns AWR-CT's ``chosen_by`` (``"request"`` when the caller passed an
    explicit ``choice`` on a role with 2+ options, ``"auto"`` when the profile
    picked it, or ``None`` for a single-option role -- there was no choice to make)
    and ``auto_reason`` (a short string when ``chosen_by == "auto"``, else
    ``None``). These are metadata only: the picked option is unaffected.
    """
    if not isinstance(role, str) or not role:
        raise RoleRouterError("unknown-role", "role must be a non-empty string")
    roles = settings.read_roles(root)["roles"]
    if role not in roles:
        raise RoleRouterError("unknown-role", f"no such role: {role}")
    entry = roles[role]
    options = entry["options"]
    auto_reason = None
    if choice is None:
        position, auto_reason = _profile_default_choice(root, len(options))
    elif choice in _CHOICE_ALIAS:
        position = _CHOICE_ALIAS[choice]
    elif isinstance(choice, bool):
        raise RoleRouterError("bad-choice", "choice must be primary, secondary, or a position 1-4")
    elif isinstance(choice, int) and 1 <= choice <= settings.MAX_ROLE_OPTIONS:
        position = choice
    elif isinstance(choice, str) and choice.isdigit() and 1 <= int(choice) <= settings.MAX_ROLE_OPTIONS:
        position = int(choice)
    else:
        raise RoleRouterError("bad-choice", "choice must be primary, secondary, or a position 1-4")
    if position > len(options):
        if choice == "secondary":
            raise RoleRouterError("no-secondary", f"role {role!r} has only one option")
        raise RoleRouterError("no-such-position", f"role {role!r} has no option at position {position}")
    if len(options) < 2:
        chosen_by, auto_reason = None, None
    elif choice is not None:
        chosen_by, auto_reason = "request", None
    else:
        chosen_by = "auto"
    option = options[position - 1]
    selection = None
    model = option["model"]
    if option["harness"] in ("codex", "claude"):
        try:
            selection = model_catalog.resolve_choice(model, option["harness"], root=root,
                                                      effort=option["effort"])
        except ValueError as exc:
            raise RoleRouterError("model-unavailable", str(exc)) from exc
        model = selection["resolved"]
    label = "primary" if position == 1 else "secondary" if position == 2 else str(position)
    return {"role": role, "choice": label, "position": position, "harness": option["harness"],
            "model": model, "model_requested": option["model"], "model_selection": selection,
            "effort": option["effort"] or (selection or {}).get("default_effort"),
            "preamble": entry["preamble"],
            # Owner decision (2026-09-29): an empty/missing saved brief_template
            # resolves to this role's built-in default here -- the single place
            # every launch path (same-app and cross-app) and any dashboard
            # "effective value" read should agree on -- without ever writing the
            # default back into the saved roles file (settings.effective_brief_template).
            "brief_template": settings.effective_brief_template(role, entry),
            "autonomy": entry["autonomy"], "chosen_by": chosen_by, "auto_reason": auto_reason}


# ---------------------------------------------------------------- prompt assembly


def _load_preamble(root, preamble_rel: str | None) -> str:
    if not preamble_rel:
        return ""
    root_real = Path(root).resolve()
    candidate = Path(root) / preamble_rel
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        raise RoleRouterError("preamble-escape", "preamble cannot be resolved") from None
    try:
        resolved.relative_to(root_real)
    except ValueError:
        raise RoleRouterError("preamble-escape", "preamble must resolve under the repo root") from None
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError:
        raise RoleRouterError("preamble-missing", f"preamble file not found: {preamble_rel}") from None


def _brief_body(brief_path, brief_text) -> str:
    if brief_text is not None:
        return brief_text
    return f"See the brief at repo-relative path: {brief_path}"


def _build_prompt(root, preamble_rel, brief_path, brief_text, *, role=None, brief_template=None, preview=False) -> str:
    """Cross-app launch prompt: preamble + reminder + brief_template + brief, in the
    same order as the same-app ``_assemble_role_prompt``.

    ``brief_template`` (2026-09-29): the role's saved template. Before this, only a
    same-app spawn carried it, so a Codex discovery worker never saw the discovery
    template (question first, no deadline, name the gaps).

    ``role`` (hooks rework, owner 2026-09-28): a cross-app chat is created by
    Crosstalk as an ordinary Desktop chat, so its own hooks fire but do not know
    which settings role it was made for. Its one role-specific reminder is
    therefore baked into this one-shot prompt, right after the preamble, using the
    active hook set's "message" text for this role (falling back to "default").
    ``role_hooks.safe_reminder_text`` never raises and returns "" on any settings
    error, a disabled switch, or an unknown/blank role -- a broken or disabled
    hooks setup never blocks a launch."""
    preamble = _load_preamble(root, preamble_rel)
    body = _brief_body(brief_path, brief_text)
    reminder = role_hooks.safe_reminder_text(root, role, "message") if role else ""
    if preview:
        # A prefix of the assembled prompt can hide the actual request behind
        # reminders. Keep bounded excerpts of both task inputs and the reminder
        # tail (which includes delivery authority/mode) within the existing cap.
        excerpts = [("Template: " + _preview(brief_template, 80)) if brief_template else "",
                    "Brief: " + _preview(body, 140),
                    ("Reminder (tail): " + reminder[-240:]) if reminder else ""]
        return _preview("\n\n".join(part for part in excerpts if part))
    sections = [section for section in (preamble, reminder, brief_template or "", body) if section]
    return "\n\n".join(sections)


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _launch_root(root) -> str:
    """R5 (AWR-FR): resolve the same shared worktree root that ``settings.read_roles``
    already uses (``settings._resolve_shared_root``) before launching. A role call
    made from a linked git worktree reads the main checkout's roles (settings
    resolves the shared root), so the chat Crosstalk creates opens in that same
    project folder rather than in the short-lived worktree. Mirrors AWR7 commit
    08afcbf, which resolved the same split for settings itself."""
    return str(settings._resolve_shared_root(root))


def _caller_metadata(caller_session, caller_work_id) -> dict:
    """Validate optional provenance against the caller-reference pattern."""
    metadata = {}
    for argument, key, value, code in (
            ("caller_session", "caller_session_id", caller_session, "bad-caller-session"),
            ("caller_work_id", "caller_work_id", caller_work_id, "bad-caller-work-id")):
        if value is None:
            continue
        if not isinstance(value, str) or not CALLER_REF_RE.fullmatch(value):
            raise RoleRouterError(code, f"{argument} must match the caller-reference pattern")
        metadata[key] = value
    return metadata


# ---------------------------------------------------------------- open


def open_role(root, host_directory, role, *, from_harness, brief_path=None, brief_text=None,
             choice=None, confirm=False, timeout_seconds=None, explicit=False,
             caller_session=None, caller_work_id=None) -> dict:
    """Resolve ``role`` and, for a cross-app call, either create the chat through
    Crosstalk or report what would be created.

    ``host_directory`` is accepted for parity with this repo's host-addressing
    convention (pickup's external ``--host`` directory) and is echoed back for
    traceability; it is NOT used to locate anything. The created chat's folder is
    ``root``, resolved through the same shared-worktree rule as settings (see
    ``_launch_root``).

    Exactly one of ``brief_path``/``brief_text`` is required.  Same-app resolution
    never touches Crosstalk (each app's own native subagent/session spawn is used
    instead).  A hermes resolution always raises ``RoleRouterError("unsupported", ...)``.
    Cross-app: ``autonomy="off"`` raises UNLESS ``explicit`` (see below), in which case
    it is treated exactly like ``"ask"``; ``"ask"``/``"auto-read-only"`` without
    ``confirm`` returns a bounded preview and creates nothing; a confirmed ask (or an
    explicit off-autonomy call with ``confirm=True``) runs ``create_chat`` (Crosstalk
    ``create-codex`` / ``create-claude``). Crosstalk absent, halted or off refuses
    visibly.
    Legacy auto-read-only is treated as ask, not as a read-only launch guarantee.

    ``explicit`` (AWR worker profiles, coordinator review, 2026-09-28): set by
    ``start`` only when its OWN caller named a role directly (as opposed to reaching
    this role through a ``task`` lookup in ``task_rules``). A direct, named call is an
    explicit act by the caller, so an autonomy of ``"off"`` -- which exists to block
    AUTOMATIC/task-driven dispatch -- must not block it outright; it still requires
    ``confirm=True`` to actually send, exactly like ``"ask"`` does, so this is not a
    bypass of confirmation, only of the outright refusal. A ``task``-resolved call
    (``explicit=False``, the default) keeps raising ``autonomy-off`` exactly as before
    this parameter existed -- this default preserves ``open_role``'s own behavior for
    every existing direct caller of this function, including the CLI's ``open``
    subcommand, which has no task/role distinction of its own and always passes
    ``explicit=False`` (a role opened directly via the CLI's low-level ``open``
    subcommand is a lower-level operation than ``start``'s own explicit-role path;
    see ``start`` for the caller-facing explicit/automatic distinction).

    R3/R4 (AWR-FR): ``confirm`` must be an actual ``bool`` -- a truthy non-bool (e.g. a
    string forwarded from untrusted input) is refused rather than silently treated as an
    explicit owner confirmation. ``from_harness`` is cross-checked against this process's
    own environment where a confirmed marker exists (see ``HARNESS_ENV_MARKERS``); a
    mismatch is refused rather than trusted. Neither check can authenticate the human
    behind an explicit, correctly-declared CLI call -- that remains a documented DEFER
    (AWR_FR_OPUS_REVIEW_2026-09-24.md R3); the router only enforces what it can actually
    observe.

    ``caller_session`` and ``caller_work_id`` are optional opaque provenance
    references validated by the caller-reference pattern (``CALLER_REF_RE``).
    They are echoed in the result, never into the prompt, and do not change
    confirmation or autonomy checks. A result that includes them labels
    cross-app dispatch ``visible-chat`` (Crosstalk creates a native Desktop chat)
    or same-app dispatch ``owner-native-handoff``; both are route labels and do
    not establish UI/sidebar visibility.
    """
    if (brief_path is None) == (brief_text is None):
        raise RoleRouterError("bad-brief", "exactly one of brief_path or brief_text is required")
    if not isinstance(confirm, bool):
        raise RoleRouterError("bad-confirm", "confirm must be a boolean")
    if not isinstance(from_harness, str) or from_harness not in settings.ROLE_HARNESSES:
        raise RoleRouterError("bad-harness", "from_harness must be claude, codex or hermes")
    caller_metadata = _caller_metadata(caller_session, caller_work_id)
    for harness_name, marker in HARNESS_ENV_MARKERS.items():
        if from_harness != harness_name and os.environ.get(marker):
            raise RoleRouterError("harness-mismatch",
                f"this process's environment marks it as {harness_name!r} ({marker} is set), "
                f"but from_harness={from_harness!r} was supplied")
    resolved = resolve_role(root, role, choice=choice)
    harness, model, effort = resolved["harness"], resolved["model"], resolved["effort"]
    _refuse_haiku(harness, model, from_harness)
    base = {"role": role, "choice": resolved["choice"], "harness": harness, "model": model,
            "model_requested": resolved.get("model_requested", model), "model_selection": resolved.get("model_selection"),
            "effort": effort, "host_directory": str(host_directory) if host_directory is not None else None}
    if caller_metadata:
        base.update(caller_metadata)
        base["visibility"] = "owner-native-handoff" if harness == from_harness else "visible-chat"
    if harness == from_harness:
        return {**base, "same_app": True,
                "note": "use this app's own native subagent/session spawn; Crosstalk refuses same-app calls"}
    if harness == "hermes":
        raise RoleRouterError("unsupported", "hermes is not wired yet")
    autonomy = resolved["autonomy"]
    if autonomy == "off" and not explicit:
        raise RoleRouterError("autonomy-off", f"role {role!r} autonomy is off; automatic cross-app calls are disabled")
    needs_confirmation = (autonomy in ("ask", "auto-read-only") or (autonomy == "off" and explicit)) and not confirm
    prompt = _build_prompt(root, resolved["preamble"], brief_path, brief_text, role=role,
                           brief_template=resolved.get("brief_template"), preview=needs_confirmation)
    if needs_confirmation:
        return {**base, "needs_confirmation": True, "prompt_preview": prompt}
    # Confirmed ordinary working session; no read-only claim is made. The chat is
    # created in the project folder, resolved through the same shared-worktree rule
    # as the roles settings (R5). Crosstalk takes the exact resolved model id.
    created = create_chat(root, harness=harness, model=model, effort=effort, prompt=prompt,
                          title=_chat_title(role), timeout_seconds=timeout_seconds)
    return {**base, "autonomy": autonomy, "route": "crosstalk", "created": created, "key": created.get("key")}


# ---------------------------------------------------------------- start (worker profiles)


HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _refuse_haiku(harness: str, model: str, from_harness: str) -> None:
    """Refuse a Claude model that cannot run auto mode (Haiku 4.5 and older; Haiku 5.5 and later is allowed,
    owner 2026-10-22). The error codes keep their original names."""
    if harness == "claude" and model_catalog.auto_mode_refused(model):
        code = "haiku-refused" if harness == from_harness else "haiku-cross-app-refused"
        raise RoleRouterError(code, f"Claude model {model} cannot run auto mode, so it cannot be launched "
                                    "through role routing")


def _assemble_role_prompt(root, entry, brief_path, brief_text, *, from_harness=None, role=None,
                          brief_template=None) -> str:
    """preamble (a repo-relative file, see ``_load_preamble``) + brief_template +
    the brief itself, each section separated by a blank line, omitting any
    section the role does not set.

    ``brief_template`` (2026-09-29): the caller passes ``resolve_role``'s own
    resolved value (already defaulted per ``settings.effective_brief_template``)
    rather than this function reading ``entry.get("brief_template")`` itself --
    the point is to have exactly one place decide the effective template, so a
    same-app launch never falls out of step with the cross-app one built by
    ``_build_prompt`` (which already took this same resolved value).

    No reminder section here for most callers (hooks rework, owner
    2026-09-28): a same-app spawn keeps this app's own project hooks firing
    live for the whole session (see ``role_hooks``), so its reminder text is
    injected turn-by-turn instead of baked once into this static prompt --
    unlike the cross-app prompt built by ``_build_prompt``, whose hooks never
    fire at all.

    CODEX-ONLY FALLBACK (finding 1, Codex verification, 2026-09-28): a live
    Codex child's ORDINARY hook events are not documented as carrying
    ``agent_id`` (see ``role_hooks``'s module docstring, "CODEX CHILD
    IDENTITY"), so this same-app path's own live turn-by-turn reminder
    injection for that child was first seen live on 2026-10-01 (see the
    role_hooks "EVIDENCE GATE" note) -- unlike Claude, where
    ``agent_id`` is documented as present on ordinary events too, making its
    live injection reliable. Because it is still undocumented, a
    short one-shot reminder is baked into this prompt as a fallback, but ONLY
    for ``from_harness == "codex"`` -- Claude's own same-app spawn is
    unaffected and keeps relying solely on its live hooks, exactly as before
    this parameter existed."""
    reminder = role_hooks.safe_reminder_text(root, role, "message") if (from_harness == "codex" and role) else ""
    sections = [_load_preamble(root, entry.get("preamble")), reminder, brief_template or "",
                _brief_body(brief_path, brief_text)]
    return "\n\n".join(section for section in sections if section)


def start(root, host_directory, role=None, *, task=None, from_harness, brief_path=None, brief_text=None,
         choice=None, confirm=False, timeout_seconds=None, caller_session=None, caller_work_id=None) -> dict:
    """Resolve a role -- named directly, or looked up from a task via the settings
    ``task_rules`` mapping -- and either report the same-app native subagent to
    spawn, or create it cross-app through Crosstalk (see ``open_role``, whose
    autonomy/hermes/harness-mismatch handling this reuses unchanged for the
    cross-app branch: an automatic start by task still honors autonomy, exactly
    like a direct role-name call does today).

    Exactly one of ``role``/``task`` is required (a ``task`` is looked up in
    ``settings.status(root)["task_rules"]``; an unknown task raises
    ``unknown-task``). The ``"orchestrator"`` role -- the chat itself -- is always
    refused, by name, before anything else is resolved: it is never launched.

    A resolved Claude option that cannot run auto mode (Haiku 4.5 and older) is refused before
    either launch path.

    EXPLICIT VS AUTOMATIC AUTONOMY (coordinator review, 2026-09-28): calling
    ``start`` with an explicit ``role`` NAME is itself an explicit act by the
    caller, so for the cross-app branch an autonomy of ``"off"`` does not block
    it outright -- it is treated like ``"ask"`` (a preview unless ``confirm=True``;
    see ``open_role``'s own ``explicit`` parameter, set here only in this branch).
    Calling ``start`` with ``--task``/``task=`` is AUTOMATIC dispatch (the caller
    picked a task, not a specific role), so it keeps honoring ``"off"`` exactly as
    before -- it still raises ``autonomy-off`` for a cross-app call, with no preview."""
    if (role is None) == (task is None):
        raise RoleRouterError("bad-start", "exactly one of role or task is required")
    if not isinstance(from_harness, str) or from_harness not in settings.ROLE_HARNESSES:
        raise RoleRouterError("bad-harness", "from_harness must be claude, codex or hermes")
    caller_metadata = _caller_metadata(caller_session, caller_work_id)
    explicit = role is not None
    if role is None:
        rules = settings.status(root)["task_rules"]
        if not isinstance(task, str) or task not in rules:
            raise RoleRouterError("unknown-task", f"no such task: {task}")
        role = rules[task]
    if role == "orchestrator":
        raise RoleRouterError("orchestrator-not-launchable",
                              "'orchestrator' is the chat itself: it plans, delegates and verifies, "
                              "and is never launched")
    resolved = resolve_role(root, role, choice=choice)
    harness, model, effort = resolved["harness"], resolved["model"], resolved["effort"]
    _refuse_haiku(harness, model, from_harness)
    if harness == from_harness:
        if (brief_path is None) == (brief_text is None):
            raise RoleRouterError("bad-brief", "exactly one of brief_path or brief_text is required")
        roles = settings.read_roles(root)["roles"]
        entry = roles.get(role)
        if entry is None:
            raise RoleRouterError("unknown-role", f"no such role: {role}")
        prompt = _assemble_role_prompt(root, entry, brief_path, brief_text, from_harness=from_harness, role=role,
                                       brief_template=resolved.get("brief_template"))
        result = {"role": role, "task": task, "choice": resolved["choice"], "same_app": True,
                  "model_requested": resolved.get("model_requested", model), "model_selection": resolved.get("model_selection"),
                  "harness": harness, "model": model, "effort": effort, "edits": entry["edits"], "prompt": prompt,
                  "note": "same-app call: spawn this with this app's own native subagent/session tool; "
                          "Crosstalk refuses same-app calls"}
        if caller_metadata:
            result.update(caller_metadata)
            result["visibility"] = "owner-native-handoff"
        return result
    return {"task": task, **open_role(root, host_directory, role, from_harness=from_harness,
                                      brief_path=brief_path, brief_text=brief_text, choice=choice,
                                      confirm=confirm, timeout_seconds=timeout_seconds, explicit=explicit,
                                      caller_session=caller_session, caller_work_id=caller_work_id)}


# ---------------------------------------------------------------- Crosstalk chat creation


def _chat_title(role) -> str:
    return f"EUTONOS {role}"[:80] if role else "EUTONOS worker"


def _crosstalk_python() -> str:
    """The interpreter that runs the Crosstalk CLI: ``EUTONOS_PYTHON`` when it names a
    file, else the installed EUTONOS runtime (``<EUTONOS home>/runtime``, default ``~/.eutonos``), else this
    process's own interpreter. The CLI is standard library only."""
    override = os.environ.get("EUTONOS_PYTHON")
    if override and Path(override).is_file():
        return override
    runtime = eutonos_home.home() / "runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(runtime) if runtime.is_file() else sys.executable


def _run_crosstalk_cli(argv, timeout_seconds):
    """Run one Crosstalk CLI command and return ``(returncode, stdout)``. The single
    place that starts a process, so tests replace it. Raises ``subprocess.TimeoutExpired``
    and ``OSError`` unchanged for the caller to translate."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=timeout_seconds, creationflags=flags)
    return done.returncode, done.stdout


def _last_json_line(text: str):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


def create_chat(root, *, harness, model, effort, prompt, title=None, cwd=None, timeout_seconds=None) -> dict:
    """Create one native chat through Crosstalk and return its JSON result.

    ``harness`` is the TARGET app: ``codex`` runs ``create-codex``, ``claude`` runs
    ``create-claude`` (default permission mode ``auto``, shown in Desktop). Refuses,
    visibly and before anything is created, when Crosstalk is not installed, halted
    or off (``crosstalk_status``), when the target is not claude/codex, or when
    model/effort are missing or unsupported. Never retries: one command, one chat. A command that fails after a chat may
    exist raises ``RoleRouterError`` whose ``details`` carry Crosstalk's own result
    (including ``thread_id`` for Codex) so nobody makes a duplicate."""
    if harness not in CREATE_HARNESSES:
        raise RoleRouterError("unsupported", f"Crosstalk creates claude or codex chats, not {harness!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RoleRouterError("bad-brief", "the prompt for a new chat must be non-empty text")
    if not isinstance(model, str) or not model:
        raise RoleRouterError("model-required", "Crosstalk creates a chat with an exact model id")
    if not effort:
        raise RoleRouterError("effort-required", "Crosstalk needs an explicit effort; set one on the role option")
    if effort not in CROSSTALK_EFFORTS:
        raise RoleRouterError("effort-unsupported",
                              f"Crosstalk supports efforts {', '.join(CROSSTALK_EFFORTS)}; got {effort!r}")
    folder = str(Path(cwd).resolve()) if cwd else _launch_root(root)
    state = crosstalk_status.status(_launch_root(root), root)
    if not state["available"]:
        raise RoleRouterError(state["reason"], crosstalk_status.reason_text(state["reason"]),
                              details={"crosstalk": state})
    argv = [_crosstalk_python(), "-I", "-B", state["cli"], "create-" + harness,
            "--folder", folder, "--model", model, "--effort", effort]
    if title:
        argv += ["--title", title]
    handle, prompt_path = tempfile.mkstemp(prefix="eutonos-crosstalk-prompt-", suffix=".txt")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(prompt)
        argv += ["--prompt-file", prompt_path]
        try:
            code, stdout = _run_crosstalk_cli(argv, timeout_seconds)
        except subprocess.TimeoutExpired:
            raise RoleRouterError("crosstalk-create-timeout",
                                  "the Crosstalk create command timed out; a chat may already exist, so check "
                                  "crosstalk_list before trying again") from None
        except OSError as exc:
            raise RoleRouterError("crosstalk-cli-unavailable",
                                  f"could not start the Crosstalk CLI ({type(exc).__name__})") from None
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
    payload = _last_json_line(stdout)
    if payload is None:
        raise RoleRouterError("crosstalk-no-result",
                              f"the Crosstalk create command (exit {code}) printed no JSON result; a chat may "
                              "already exist, so check crosstalk_list before trying again")
    if code != 0 or payload.get("ok") is not True:
        reason = str(payload.get("reason") or "unknown")
        detail = str(payload.get("detail") or "")[:300]
        thread = payload.get("thread_id")
        note = f" A Codex thread exists (thread_id {thread}); do not create a second." if thread else ""
        raise RoleRouterError("crosstalk-create-failed",
                              f"Crosstalk could not create the chat: {reason}" + (f" ({detail})" if detail else "")
                              + "." + note, details={"crosstalk": payload})
    return payload


# ---------------------------------------------------------------- explicit escape hatch


def send_explicit(root, *, to, sender, prompt=None, prompt_file=None, mode="new", model=None, effort=None,
                  cwd=None, title=None, timeout_seconds=None) -> dict:
    """The escape hatch: create one chat through Crosstalk with a harness, model and
    effort the caller names (``create_chat``).

    Autonomy settings never block an explicit, named call.  There is no role here;
    the caller states harness (``to``), ``model`` and ``effort`` itself. The model may
    be a catalog family or exact id; Crosstalk is given the exact resolved id. Only
    ``mode="new"`` exists: messaging an existing chat is Crosstalk's ``crosstalk_send``,
    and same-app creation is each app's own native spawn.
    """
    if mode != "new":
        raise RoleRouterError("mode-unsupported",
                              "only mode 'new' creates a chat; message an existing chat with crosstalk_send")
    if to not in CREATE_HARNESSES:
        raise RoleRouterError("unsupported", f"Crosstalk creates claude or codex chats, not {to!r}")
    if to == sender:
        raise RoleRouterError("same-app", "use this app's own native subagent/session spawn for a same-app chat")
    if (prompt is None) == (prompt_file is None):
        raise RoleRouterError("bad-brief", "exactly one of prompt or prompt_file is required")
    if prompt is None:
        try:
            prompt = Path(prompt_file).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise RoleRouterError("bad-brief", f"prompt file unreadable ({type(exc).__name__})") from None
    if not model:
        raise RoleRouterError("model-required", "name a model for the new chat")
    try:
        selection = model_catalog.resolve_choice(model, to, root=root, effort=effort)
    except ValueError as exc:
        raise RoleRouterError("model-unavailable", str(exc)) from exc
    resolved_model = selection["resolved"]
    _refuse_haiku(to, resolved_model, sender)
    created = create_chat(root, harness=to, model=resolved_model, effort=effort or selection.get("default_effort"),
                          prompt=prompt, title=title, cwd=cwd, timeout_seconds=timeout_seconds)
    return {"route": "crosstalk", "harness": to, "model": resolved_model, "model_requested": model,
            "created": created, "key": created.get("key")}


# ---------------------------------------------------------------- CLI


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eutonos.role_router",
                                     description="Resolve and (for cross-app calls) create a chat for a settings role "
                                                 "through Crosstalk.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".")
        return p

    choice_values = ("primary", "secondary", "1", "2", "3", "4")

    p = add("resolve")
    p.add_argument("--role", required=True)
    p.add_argument("--choice", choices=choice_values)

    p = add("open")
    p.add_argument("--host-directory")
    p.add_argument("--role", required=True)
    p.add_argument("--from-harness", required=True, choices=settings.ROLE_HARNESSES)
    p.add_argument("--choice", choices=choice_values)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--timeout", type=int, help="optional launch run-time cap in seconds (default: no limit; never killed unless set)")
    p.add_argument("--caller-session", help="opaque calling chat reference for provenance; does not grant permission")
    p.add_argument("--caller-work", "--caller-work-id", dest="caller_work_id",
                    help="opaque board work reference for provenance")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--brief-path")
    grp.add_argument("--brief-text")

    p = add("start")
    p.add_argument("--host-directory")
    role_grp = p.add_mutually_exclusive_group(required=True)
    role_grp.add_argument("--role")
    role_grp.add_argument("--task", choices=settings.TASKS)
    p.add_argument("--from-harness", required=True, choices=settings.ROLE_HARNESSES)
    p.add_argument("--choice", choices=choice_values)
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--timeout", type=int, help="optional launch run-time cap in seconds (default: no limit; never killed unless set)")
    p.add_argument("--caller-session", help="opaque calling chat reference for provenance; does not grant permission")
    p.add_argument("--caller-work", "--caller-work-id", dest="caller_work_id",
                    help="opaque board work reference for provenance")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--brief-path")
    grp.add_argument("--brief-text")

    p = add("send-explicit")
    p.add_argument("--to", required=True, choices=CREATE_HARNESSES)
    p.add_argument("--from", dest="sender", required=True, choices=settings.ROLE_HARNESSES + ("owner",))
    p.add_argument("--mode", default="new", choices=("new",))
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--prompt")
    grp.add_argument("--prompt-file")
    p.add_argument("--model", required=True)
    p.add_argument("--effort", required=True)
    p.add_argument("--cwd", help="absolute project folder for the new chat (default: the repository root)")
    p.add_argument("--title")
    p.add_argument("--timeout", type=int)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            _emit(resolve_role(args.root, args.role, choice=args.choice))
        elif args.command == "open":
            _emit(open_role(args.root, args.host_directory, args.role, from_harness=args.from_harness,
                            brief_path=args.brief_path, brief_text=args.brief_text, choice=args.choice,
                            confirm=args.confirm, timeout_seconds=args.timeout,
                            caller_session=args.caller_session, caller_work_id=args.caller_work_id))
        elif args.command == "start":
            _emit(start(args.root, args.host_directory, args.role, task=args.task, from_harness=args.from_harness,
                       brief_path=args.brief_path, brief_text=args.brief_text, choice=args.choice,
                       confirm=args.confirm, timeout_seconds=args.timeout,
                       caller_session=args.caller_session, caller_work_id=args.caller_work_id))
        elif args.command == "send-explicit":
            _emit(send_explicit(args.root, to=args.to, sender=args.sender, prompt=args.prompt,
                                prompt_file=args.prompt_file, mode=args.mode, model=args.model,
                                effort=args.effort, cwd=args.cwd, title=args.title,
                                timeout_seconds=args.timeout))
    except (RoleRouterError, ValueError) as exc:
        code = getattr(exc, "code", "error")
        message = getattr(exc, "message", str(exc))
        failure = {"ok": False, "error": code, "message": message}
        if getattr(exc, "details", None):
            failure["details"] = exc.details
        _emit(failure)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
