"""Model-choice resolution and per-session provenance for worker `new`/`resume`
launches (AWR slice-16e8b55993c3, "Model families, exact pins and after-job
worker upgrades", owner-approved 2026-09-29).

WHY: a worker caller may name either a model **family** ("sol", "luna",
"astra", "sonnet", "opus" -- meaning "the latest stable model in that family,
resolved when a NEW worker starts") or an **exact model ID** (e.g.
"gpt-6-luna", "claude-opus-5-5" -- which stays exactly that, forever, for the
life of the session). A running session must never switch models underneath
itself. This module is the single place that:

  1. resolves a family/pin choice at `new` (never at `resume` -- a resume
     always continues on whatever the session already recorded);
  2. records what was requested, what was resolved, and (when the launched
     app's own output reveals it) what model actually ran, one small JSON
     file per session under `.eutonos-local/workers/<session_id>.json` (an
     older record under `.eutonos-local/mailbox/workers/` is still read when
     the new file is absent, and is never deleted);
  3. enforces model consistency across a `resume` (refusing a change, and
     refusing a resume with nothing recorded, rather than silently defaulting
     to anything); and
  4. retires a family-selected worker that is asked to pick up a NEW job (a
     different `conversation_id`/`caller_work_id`) once a newer model in that
     family exists, so stale workers are not reused for fresh, unrelated work
     -- while a resume that continues the SAME job never retires, and an
     exact pin never retires at all. Retirement is sticky (Sol review,
     2026-09-29): once an entry is retired, EVERY later different-job resume
     attempt is refused, not just the first -- see `plan_resume`.

CONCURRENCY (Sol review, 2026-09-29): a read-modify-write against a session's
entry (retiring it in `plan_resume`, or updating job/observed state in
`finalize`) is serialized with the existing cross-process `eutonos.local_lock`
helper (the same OS-released lock the other `.eutonos-local` writers
use) rather than a bespoke lockfile, so two concurrent
processes can never race a retirement update. It fails CLOSED: a
`TimeoutError` from the lock is translated into `WorkerModelError
("registry-busy", ...)` -- this module never proceeds unlocked. A load also
byte-caps and validates the record's shape before trusting it
(`MAX_ENTRY_BYTES`, `_valid_entry`) rather than trusting any file found at
that path.

LAUNCH FENCING (Sol review, 2026-09-29): a different `job_key` alone does not
prove the prior job's launch actually finished -- nothing else holds a
per-session launch reservation, so without this, a same-pin (never-retiring)
worker's different-job resume could overlap an in-flight, unfinalized launch
on the very same session. Every entry therefore also tracks `busy` (set by
`record_new` and by a successful `plan_resume`, cleared by `finalize`, which
always runs at the end of a launch whether it succeeded or failed). ANY
resume attempt -- same job or different -- while `busy` is true is refused
outright (`session-busy`), never queued or raced; the caller is told to retry
once the prior launch finishes or start a fresh worker. This is checked
before, and independently of, the after-job turnover/retirement logic above:
an outdated-family different-job request that is refused as `worker-retired`
never claims the prior job completed, and the job binding (`job_key`) is only
ever advanced by `finalize`, i.e. at an actual completed boundary.

CATALOG CONTRACT (`eutonos.model_catalog`, a separate Codex-owned module):
`resolve_choice(choice, harness, *, root=None, effort=None)` returns a dict
with `requested`, `resolved`, `selection_kind` ('family'|'pin'), `family`,
`supported_efforts` and `basis`, and raises `ValueError` with a clear message
for an unavailable or unsupported choice. This module imports it lazily
(inside a function body, never at module load) so it degrades cleanly if that
module is ever absent on some branch/host: a bare family name is refused
outright (never guessed), while anything that is not a known bare family name
passes through untouched as an exact pin (`selection_kind="pin"`,
`basis="catalog-unavailable"`).

OBSERVED MODEL: the recorded truth beats the resolved one whenever the
launched app's own output reveals it.
  - Claude: the `-p --output-format json` result's top-level `modelUsage`
    dict is keyed by exact model id. When it carries exactly one key, that
    key is the observed model (see `observed_model_from_claude_result`).
    Ambiguous (zero or multiple keys, e.g. a session that used more than one
    model) is left unobserved rather than guessed.
  - Codex: no equivalent field was found in the `exec --json` event stream
    (`thread.started`/`turn.started`/`turn.completed`/... carry no model
    field) -- this module has no Codex observed-model source at all; only
    `resolved` is ever recorded for Codex sessions.

THREE RESUME PATHS (coordinator note, 2026-09-29):
  - A `resume` request for the SAME job (see `job_key`) as the one
    recorded always continues on the recorded exact model; it is never
    refused for staleness, even when a newer model in the same family now
    exists or the native catalog no longer lists that exact model (owner,
    2026-09-30). Catalog absence is not proof an existing session cannot
    launch: try its stored pin and let the native launch report inability.
    Effort policy, provenance validation and launch fencing still apply.
  - A `resume` request for a DIFFERENT job is where the after-job
    turnover rule applies: a family-selected (never a pinned) worker is
    retired (``worker-retired``) once a newer model exists in that family.
  - An interactive resume the owner starts in the native app is a third path outside both
    rules: it never calls into this module and is never retired or refused by the turnover rule.

JOB COMPLETION (read-only, `python -m eutonos.worker_models status`): once a
session's registry entry sees a `resume`/`finalize` call under a job
different from the one it was last recorded under, that transition is
recorded (`job_completed_at`, `prior_job_key`) regardless of whether the
after-job turnover rule actually retired the worker (a pin, or a family with
no newer model, still "completes" its prior job and keeps going on the new
one). This is informational only -- see `finalize`. A request that carries no
identity at all (neither `conversation_id` nor `caller_work_id` -- typical of
a plain resume that only names `session_id`) never clears the existing job
binding; `finalize` only ever updates it when the incoming request actually
supplies one (Sol review, 2026-09-29 -- clearing it on an identity-less
request would make a later, genuinely different job look like "job unknown"
and silently bypass after-job turnover).
"""
from __future__ import annotations

import argparse
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Family tokens the worker model field may carry in place of an exact model
# id (owner, 2026-09-29; confirmed against the eutonos.model_catalog contract:
# "Family keys are sol, luna, astra, sonnet and opus"). A choice is treated as
# a bare family name only when it matches one of these, case-insensitively;
# anything else is an exact pin.
FAMILY_NAMES = frozenset({"sol", "luna", "astra", "sonnet", "opus"})

REGISTRY_PROFILE = "eutonos.worker-model.v1"


class WorkerModelError(Exception):
    """A refused model choice or resume, with a short stable ``code`` (a caller's
    own error type can wrap it by copying ``code``, ``message`` and ``extra``)."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def job_key(req: dict) -> str | None:
    """A single display/informational string naming whatever job identity
    ``req`` carries (preferring ``conversation_id``, then ``caller_work_id``),
    or ``None`` when it carries neither. This is NOT how "is this a different
    job" is decided any more (see `_different_job`) -- it is only used to
    record *some* identity for `status`/`provenance_view` display, and to gate
    "does this request carry any identity at all" (e.g. `finalize` never wipes
    the recorded binding on a request that carries neither field)."""
    cid = req.get("conversation_id")
    if isinstance(cid, str) and cid:
        return "conversation:" + cid
    wid = req.get("caller_work_id")
    if isinstance(wid, str) and wid:
        return "work:" + wid
    return None


def _different_job(entry: dict, req: dict) -> bool:
    """Whether ``req`` is for a DIFFERENT job than the one ``entry`` was last
    recorded under (Sol/Codex review, 2026-09-29: the original rule compared a
    single combined key that preferred ``conversation_id`` over
    ``caller_work_id``, which misclassifies two real cases -- work item W's
    conversation moving from A to B after an owner-visible upgrade is still
    the SAME job, since W matches; and a bare conversation continuing under a
    newly-added work id is also the SAME job).

    Rule: when BOTH sides carry a ``caller_work_id``, that decides (equal ids
    -> same job). Otherwise, when BOTH sides carry a ``conversation_id``, that
    decides. When nothing is comparable on either axis (one or both sides
    missing every identity field, or missing the one axis that IS present on
    the other side), it is NOT a different job -- there is no evidence either
    way, same as `job_key` returning ``None``."""
    recorded_work = entry.get("caller_work_id")
    current_work = req.get("caller_work_id")
    if isinstance(recorded_work, str) and recorded_work and isinstance(current_work, str) and current_work:
        return recorded_work != current_work
    recorded_conv = entry.get("conversation_id")
    current_conv = req.get("conversation_id")
    if isinstance(recorded_conv, str) and recorded_conv and isinstance(current_conv, str) and current_conv:
        return recorded_conv != current_conv
    return False


# ---------------------------------------------------------------- resolution

def resolve_choice(choice: str, harness: str, *, root=None, effort: str | None = None) -> dict:
    """Resolve one model choice (family or exact) for a NEW launch.

    Returns ``{"requested", "resolved", "selection_kind", "family",
    "supported_efforts", "basis"}``. Raises ``WorkerModelError`` (code
    ``model-unresolved``) when the choice cannot be resolved -- including the
    catalog-unavailable fallback's refusal of a bare family name, which never
    guesses at an exact model."""
    if not isinstance(choice, str) or not choice:
        raise WorkerModelError("model-unresolved", "a model choice is required")
    try:
        from eutonos import model_catalog  # lazy: keeps this module importable if that one is ever absent
    except ImportError:
        model_catalog = None
    if model_catalog is not None:
        try:
            result = model_catalog.resolve_choice(choice, harness, root=root, effort=effort)
        except ValueError as exc:
            raise WorkerModelError("model-unresolved", str(exc) or f"{choice!r} is not available") from exc
        if not isinstance(result, dict) or not result.get("resolved"):
            raise WorkerModelError("model-unresolved", "model_catalog returned an unexpected shape")
        plan = {
            "requested": result.get("requested", choice),
            "resolved": result["resolved"],
            "selection_kind": result.get("selection_kind"),
            "family": result.get("family"),
            "supported_efforts": result.get("supported_efforts"),
            "basis": result.get("basis") or "model_catalog",
        }
        if result.get("default_effort") is not None:
            plan["effort"] = result["default_effort"]
        if result.get("allowed_efforts") is not None:
            plan["allowed_efforts"] = result["allowed_efforts"]
        return plan
    # Fallback: the catalog module is unavailable on this branch/host. Never guess a
    # family's exact model; an exact-looking pin still passes straight through.
    if choice.lower() in FAMILY_NAMES:
        raise WorkerModelError("model-unresolved",
                               f"model family {choice!r} cannot be resolved: the model catalog is unavailable")
    return {"requested": choice, "resolved": choice, "selection_kind": "pin", "family": None,
            "supported_efforts": None, "basis": "catalog-unavailable"}


def plan_new(req: dict, *, harness: str, root=None, default_model: str | None = None) -> dict:
    """The model plan for a `new` launch. When no model was requested at all,
    ``default_model`` (when given) is used with ``selection_kind="default"``
    -- never treated as a resolved family/pin choice. With no requested model
    and no default, the plan carries no model at all (``selection_kind=
    "unspecified"``; the launched app's own configured default applies, and a
    later resume of that exact session will refuse -- see `plan_resume` --
    since nothing was ever pinned)."""
    requested = req.get("model")
    effort = req.get("effort")
    if not requested:
        if default_model:
            return {"requested": None, "resolved": default_model, "selection_kind": "default",
                    "family": None, "supported_efforts": None, "basis": f"{harness}-default"}
        return {"requested": None, "resolved": None, "selection_kind": "unspecified",
                "family": None, "supported_efforts": None, "basis": "no-request"}
    return resolve_choice(requested, harness, root=root, effort=effort)


# A new `--mode new` request that picks up a retired worker's handoff must
# itself carry this job's continuity from the durable work record (source
# revision, checks, unresolved items, next action) -- worker_models.py has no
# contract with boards/RAM and does not reach into them (less new surface);
# the handoff instead names exactly what the caller is required to attach.
HANDOFF_CONTINUITY_REQUIRED = ("source_revision", "checks", "unresolved", "next_action")


def _build_retirement_handoff(session_id, entry, newer_model) -> dict:
    return {
        "prior_session_id": session_id,
        "conversation_id": entry.get("conversation_id"),
        "caller_work_id": entry.get("caller_work_id"),
        "last_request_id": entry.get("last_request_id"),
        "resolved_model": entry.get("resolved"),
        "observed_model": entry.get("observed"),
        "family": entry.get("family"),
        "newer_model": newer_model,
        "continuity_required": list(HANDOFF_CONTINUITY_REQUIRED),
    }


def _resume_catalog_entry(recorded_model: str, *, root=None) -> dict | None:
    try:
        from eutonos import model_catalog
    except ImportError:
        return None
    entry = model_catalog.describe_catalog(root)["models"].get(recorded_model)
    return entry if entry is not None and entry.get("available", True) else None


def _resume_luna_plan(entry: dict, recorded_model: str, req: dict, *, harness: str,
                      root=None, different_job: bool, catalog_entry=None) -> dict:
    """Validate Luna effort without treating a same-job catalog absence as
    model removal. Current capability evidence wins when the pin is listed;
    otherwise use the recorded capabilities, with the owner floor still in
    force. Missing capability evidence permits a policy-valid attempt on the
    stored pin; only the native launch can establish that it cannot launch.
    Different jobs keep ordinary current-catalog validation.
    """
    if different_job:
        return resolve_choice(recorded_model, harness, root=root, effort=req.get("effort"))
    if catalog_entry is not None:
        return resolve_choice(recorded_model, harness, root=root, effort=req.get("effort"))
    # Catalog absence must not bypass the harness check that resolution used
    # to provide for these Luna entries. Native exact IDs name their harness.
    if not re.fullmatch(r"gpt-\d+(?:\.\d+)*(?:-[a-z][a-z0-9-]*)?", recorded_model):
        raise WorkerModelError("model-unresolved",
                               f"recorded model {recorded_model!r} is not a Codex exact model ID")

    # The registry reader has already checked the type of supported_efforts.
    # Do not rewrite model identity, or silently raise an explicit lower effort.
    supported = entry.get("supported_efforts")
    allowed = tuple(e for e in ("xhigh", "max", "ultra")
                    if supported is None or e in supported)
    effort = req.get("effort")
    if effort is not None:
        if not isinstance(effort, str) or effort not in ("xhigh", "max", "ultra"):
            raise WorkerModelError("model-unresolved",
                                   "Luna effort must be at or above the owner policy minimum XHigh")
        if effort not in allowed:
            raise WorkerModelError("model-unresolved",
                                   f"Unsupported recorded effort for {recorded_model}: {effort}")
        return {}
    if not allowed:
        raise WorkerModelError("model-unresolved",
                               f"Luna choice {recorded_model} has no recorded supported effort at or above XHigh")
    return {"effort": allowed[0]}


def plan_resume(req: dict, *, harness: str, root=None) -> dict:
    """The model plan for a `resume` launch: the recorded exact model, never a
    fresh resolution -- a running session must not switch models. Raises
    ``WorkerModelError``:

    - ``resume-model-unknown``: nothing was ever recorded for this session
      (session unknown, recorded with no resolved/observed model at all, or
      the stored record failed `_valid_entry`'s shape/type check -- a
      malformed or corrupt entry is never partially trusted). Never falls
      back to any default.
    - ``resume-harness-mismatch``: the session id was recorded under a
      different harness (Sol review, 2026-09-29: "the same UUID under an
      incompatible harness is refused") -- e.g. a codex session id replayed
      as a claude resume. Checked before anything else that reads the
      recorded model, since a cross-harness record must never be used at all.
    - ``worker-retired``: this is a different job (see `job_key`) than the one
      the session was recorded under, the recorded choice was a `family`
      selection (a pin never retires), the catalog is available, and it now
      resolves to a newer model than what is recorded -- OR the entry was
      already retired by an earlier different-job attempt (retirement is
      sticky: every later different-job attempt is refused too, never just
      the first one; a same-job resume, or the owner-invoked manual `open`
      path which never calls this function at all, is never affected). The
      registry entry is marked retired as a side effect of the FIRST such
      refusal; nothing running is ever interrupted (the caller is refused
      before any launch). The error's ``extra["handoff"]`` names the newer
      model, enough of the prior session for the caller to send a fresh
      `--mode new`, and the continuity that new request is required to carry
      (see ``HANDOFF_CONTINUITY_REQUIRED``).
    - ``resume-model-mismatch``: the request named a model that conflicts
      with the recorded one (an exact id that differs, or a family that does
      not match the recorded family).
    - ``session-busy``: this session has a launched but not yet finalized job
      (see the module docstring's LAUNCH FENCING). Refused unconditionally --
      same job or different, even a retry with the SAME request id, since a
      matching request id is not proof the earlier launch ended. Nothing
      clears it automatically (a terminal reply written by the retired file
      mailbox is no longer read): the caller is pointed at the owner-invoked
      manual `python -m eutonos.worker_models release --session <id>`. This
      is checked before, and independently of, the turnover/retirement logic
      below.

    The read-modify-write against the registry entry (loading it, possibly
    marking it retired or busy, saving it back) is serialized with
    `_locked_registry` so two concurrent resume attempts can never race each
    other's update.
    """
    session_id = req.get("session_id")
    with _locked_registry(root):
        entry = load_entry(root, session_id) if session_id else None
        if not isinstance(entry, dict):
            raise WorkerModelError("resume-model-unknown",
                                   "no recorded model for this session; refusing rather than guessing "
                                   "(a resume never defaults to Sonnet or anything else)")
        if entry.get("harness") != harness:
            raise WorkerModelError(
                "resume-harness-mismatch",
                f"session {session_id!r} was recorded under harness {entry.get('harness')!r}, "
                f"not {harness!r}; refusing rather than resuming a cross-harness session id")
        recorded_model = entry.get("observed") or entry.get("resolved")
        if not recorded_model:
            raise WorkerModelError("resume-model-unknown",
                                   "the recorded entry for this session has no resolved or observed model")
        if entry.get("busy"):
            raise WorkerModelError(
                "session-busy",
                "this session has a launched but not yet finalized job; refusing rather than risking "
                "two overlapping launches on the same session -- retry once it finishes, or if the "
                "launch died midway, use `python -m eutonos.worker_models release --session "
                f"{session_id}` to clear it manually",
                in_flight_request_id=entry.get("in_flight_request_id"))
        different_job = _different_job(entry, req)

        if entry.get("retired"):
            if different_job:
                # Sticky: an earlier different-job attempt already retired this worker.
                # Every later different-job attempt is refused too, not just the first --
                # never re-derived from scratch, but refreshed opportunistically so the
                # handoff's "newer_model" stays current when the catalog is reachable.
                newer = entry.get("retired_newer_model")
                try:
                    fresh = resolve_choice(entry.get("family"), harness, root=root, effort=req.get("effort"))
                    if fresh.get("resolved"):
                        newer = fresh["resolved"]
                except WorkerModelError:
                    pass  # a catalog hiccup on a later attempt must never un-refuse a retired worker
                raise WorkerModelError(
                    "worker-retired",
                    f"this worker was already retired ({entry.get('retired_reason')}) after an earlier "
                    "different-job resume attempt; send --mode new with this handoff to start fresh",
                    handoff=_build_retirement_handoff(session_id, entry, newer))
            # Same job (or job unknown) resuming a retired entry is unaffected --
            # fall through to the ordinary consistency check below.
        elif different_job and entry.get("selection_kind") == "family":
            newer = None
            try:
                fresh = resolve_choice(entry.get("family"), harness, root=root, effort=req.get("effort"))
                newer = fresh.get("resolved")
            except WorkerModelError:
                newer = None  # catalog unavailable, or the family no longer resolves -- never retire on a guess
            if newer and newer != recorded_model:
                handoff = _build_retirement_handoff(session_id, entry, newer)
                entry["retired"] = True
                entry["retired_at"] = _now_iso()
                entry["retired_reason"] = "after-job-turnover"
                entry["retired_newer_model"] = newer
                save_entry(root, session_id, entry)
                raise WorkerModelError(
                    "worker-retired",
                    f"this worker was launched on {recorded_model!r} ({entry.get('family')} family); "
                    f"{newer!r} is now the latest in that family and this resume is for a different job -- "
                    "send --mode new with this handoff to start fresh",
                    handoff=handoff)

        requested = req.get("model")
        if requested:
            if requested.lower() in FAMILY_NAMES:
                if entry.get("family") != requested.lower():
                    raise WorkerModelError("resume-model-mismatch",
                                           f"recorded family is {entry.get('family')!r}, not {requested!r}")
            elif requested != recorded_model:
                raise WorkerModelError("resume-model-mismatch",
                                       f"session was launched on {recorded_model!r}; a running session must "
                                       f"never switch models (requested {requested!r})")
        # Validate before reserving, but after turnover and the busy fence.
        # A same-job follow-up tries its exact pin even if native listing drops
        # it; a different job still validates current capability evidence.
        if different_job and entry.get("selection_kind") == "pin":
            from eutonos import model_catalog
            candidate = model_catalog.describe_catalog(root)["models"].get(recorded_model)
            if candidate is not None and candidate.get("available") is False:
                raise WorkerModelError(
                    "model-unresolved",
                    f"recorded pin {recorded_model!r} is unavailable for a new job; "
                    "start a fresh worker on an available exact pin or current family model")
        luna_plan = None
        catalog_entry = _resume_catalog_entry(recorded_model, root=root) if not different_job else None
        if harness == "codex" and (entry.get("family") == "luna" or
                re.fullmatch(r"gpt-\d+(?:\.\d+)*-luna(?:-[a-z0-9-]+)?", recorded_model)):
            luna_plan = _resume_luna_plan(entry, recorded_model, req, harness=harness,
                                          root=root, different_job=different_job,
                                          catalog_entry=catalog_entry)
        # Approved: fence any other resume attempt out until this launch's own
        # `finalize` clears `busy` again (LAUNCH FENCING, module docstring).
        entry["busy"] = True
        entry["in_flight_request_id"] = req.get("id")
        save_entry(root, session_id, entry)
        plan = {"resolved": recorded_model, "requested": requested,
                "selection_kind": entry.get("selection_kind"), "family": entry.get("family")}
        if not different_job and catalog_entry is None:
            # Advisory only: the launcher can add a fresh-job suggestion if
            # this stored pin actually fails to launch. Never a refusal here.
            plan["same_job_model_unlisted"] = True
        if luna_plan and luna_plan.get("effort") is not None:
            plan["effort"] = luna_plan["effort"]
        return plan


def plan_launch(req: dict, *, harness: str, root=None, default_model: str | None = None) -> dict:
    """The single entry point for a `new` or `resume` launch: the
    model plan for a `new` or `resume` launch, including the refusal of a Claude
    model that cannot run auto mode (Haiku 4.5 and older; Haiku 5.5 and later is
    allowed, owner 2026-10-22), which applies to both modes and is never remapped
    silently."""
    requested = req.get("model")
    from eutonos import model_catalog  # lazy, as elsewhere in this module
    if harness == "claude" and isinstance(requested, str) and model_catalog.auto_mode_refused(requested):
        raise WorkerModelError("model-unavailable",
                               f"{requested} cannot run auto mode (Haiku 4.5 and older; owner, 2026-10-22)")
    if req.get("mode") == "resume":
        return plan_resume(req, harness=harness, root=root)
    return plan_new(req, harness=harness, root=root, default_model=default_model)


# ------------------------------------------------------------------ registry

# A provenance record is a small, fixed-shape document; capping the read
# guards against ever trusting an unbounded or foreign file at this path
# (Sol review, 2026-09-29: "read byte-capped, validated records").
MAX_ENTRY_BYTES = 200_000

LOCK_TIMEOUT_SECONDS = 5.0


def registry_dir(root) -> Path:
    return Path(root) / ".eutonos-local" / "workers"


def legacy_registry_dir(root) -> Path:
    """Where this registry lived while the folder carried the retired file
    mailbox's name. Read as a fallback only, never written and never deleted."""
    return Path(root) / ".eutonos-local" / "mailbox" / "workers"


def _entry_path(root, session_id: str) -> Path:
    return registry_dir(root) / f"{session_id}.json"


def _legacy_entry_path(root, session_id: str) -> Path:
    return legacy_registry_dir(root) / f"{session_id}.json"


# Every field that can end up in a launched app's argv (`-m <model>`) or in
# the `model_provenance` reply must be, at most, a plain string -- never a
# list/dict/number a corrupt or foreign file could smuggle into a command
# line (Sol review, 2026-09-29: "validate the types of the recorded
# exact-model/choice fields ... before using them in argv or provenance").
_OPTIONAL_STR_FIELDS = (
    "requested", "resolved", "observed", "observed_source", "selection_kind",
    "family", "basis", "job_key", "conversation_id", "caller_work_id",
    "created_request_id", "last_request_id", "in_flight_request_id",
    "retired_reason", "retired_newer_model", "retired_at", "created_at",
    "updated_at", "job_completed_at", "prior_job_key",
)
_BOOL_FIELDS = ("retired", "busy")


def _valid_entry(data, session_id: str) -> bool:
    """Shape/identity/type check before trusting a loaded record: the right
    profile tag, a session id matching the file it was loaded from (a
    mismatch means a stray/foreign file, never something to act on), a
    non-empty string ``harness``, and every optional field present is either
    absent, ``None`` or the expected plain type. A record failing this check
    is treated exactly like "nothing recorded" by every caller (`load_entry`
    returns ``None``) -- it never partially trusts a malformed/corrupt entry."""
    if not (isinstance(data, dict)
            and data.get("profile") == REGISTRY_PROFILE
            and data.get("session_id") == session_id
            and isinstance(data.get("harness"), str) and data.get("harness")):
        return False
    for field in _OPTIONAL_STR_FIELDS:
        if field in data and data[field] is not None and not isinstance(data[field], str):
            return False
    for field in _BOOL_FIELDS:
        if field in data and not isinstance(data[field], bool):
            return False
    supported = data.get("supported_efforts")
    if supported is not None and not (isinstance(supported, list)
                                      and all(isinstance(e, str) for e in supported)):
        return False
    return True


def load_entry(root, session_id: str | None) -> dict | None:
    if not root or not session_id:
        return None
    try:
        try:
            handle = _entry_path(root, session_id).open("rb")
        except FileNotFoundError:
            # No record in the current folder: fall back to the older location. A later
            # save writes the current folder, which then wins; the older file stays put.
            handle = _legacy_entry_path(root, session_id).open("rb")
        with handle:
            raw = handle.read(MAX_ENTRY_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_ENTRY_BYTES:
        return None  # oversized -- never trust an unbounded read
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return data if _valid_entry(data, session_id) else None


@contextmanager
def _locked_registry(root):
    """Serialize a registry read-modify-write using the existing cross-process
    `eutonos.local_lock` helper -- the same OS-released lock the other
    `.eutonos-local` writers use (Sol review, 2026-09-29: reuse it
    rather than a bespoke per-session lockfile). FAILS CLOSED: a lock timeout
    (`TimeoutError`) is translated into `WorkerModelError("registry-busy", ...)`
    -- this module never proceeds without the lock. A falsy ``root``
    (legacy/unit-level bypass callers that never governed this launch to begin
    with) is a no-op."""
    if not root:
        yield
        return
    from eutonos.local_lock import local_lock  # lazy: avoids a load-order assumption on that module
    try:
        with local_lock(root, timeout=LOCK_TIMEOUT_SECONDS):
            yield
    except TimeoutError as exc:
        raise WorkerModelError("registry-busy",
                               "the worker-model registry lock is busy; refusing rather than risking "
                               "a concurrent update") from exc


def save_entry(root, session_id: str, entry: dict) -> None:
    # The generic atomic write lives in the neutral ``local_state`` module.
    from eutonos.local_state import atomic_write
    path = _entry_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(entry, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def record_new(root, *, session_id: str, harness: str, req: dict, plan: dict) -> dict:
    """Create the registry entry for a session just launched by `new`. Never
    raises for I/O trouble (best-effort recording:
    a write failure here must never
    break a launch that already ran)."""
    now = _now_iso()
    entry = {
        "profile": REGISTRY_PROFILE, "harness": harness, "session_id": session_id,
        "requested": plan.get("requested"), "resolved": plan.get("resolved"),
        "selection_kind": plan.get("selection_kind"), "family": plan.get("family"),
        "supported_efforts": plan.get("supported_efforts"), "basis": plan.get("basis"),
        "observed": None, "observed_source": None,
        "conversation_id": req.get("conversation_id"), "caller_work_id": req.get("caller_work_id"),
        "job_key": job_key(req),
        "job_completed_at": None, "prior_job_key": None,
        "created_request_id": req.get("id"), "last_request_id": req.get("id"),
        "created_at": now, "updated_at": now,
        "retired": False, "retired_at": None, "retired_reason": None, "retired_newer_model": None,
        # LAUNCH FENCING (module docstring): this launch is now in flight;
        # `finalize` clears this once it completes, success or failure.
        "busy": True, "in_flight_request_id": req.get("id"),
    }
    try:
        save_entry(root, session_id, entry)
    except OSError:
        pass
    return entry


def finalize(root, *, session_id: str | None, req: dict, observed: str | None = None,
             observed_source: str | None = None) -> dict | None:
    """Update the registry entry after a launch completes (both `new` and
    `resume`): the latest request/job seen, and the observed model when the
    launched app's own output revealed one. When the request's job differs
    from the one last recorded, that transition is noted (`job_completed_at`,
    `prior_job_key` -- read-only via `python -m eutonos.worker_models status`;
    see the module docstring's "JOB COMPLETION" section) regardless of
    whether the after-job turnover rule actually retired anything. Returns the
    updated entry, or ``None`` when there is nothing recorded for this session
    (e.g. `new` crashed before a session id was ever pinned/harvested). The
    read-modify-write is serialized with `_locked_registry`, same as
    `plan_resume`. Always clears `busy` (LAUNCH FENCING, module docstring) --
    this is the "actual completed boundary" a launch reaches whether it
    succeeded or failed, so a fenced-out resume can proceed again."""
    if not session_id:
        return None
    with _locked_registry(root):
        entry = load_entry(root, session_id)
        if entry is None:
            return None
        entry["busy"] = False
        entry["in_flight_request_id"] = None
        new_job = job_key(req)
        old_job = entry.get("job_key")
        # A request that carries neither a conversation_id nor a caller_work_id
        # (e.g. a plain resume that only names session_id) must leave the
        # existing job binding untouched -- overwriting it with None here would
        # erase the recorded job_key/conversation_id/caller_work_id, and a LATER
        # request for a genuinely different job would then wrongly look like
        # "job unknown" and bypass after-job turnover entirely (Sol review,
        # 2026-09-29). Only update the binding when the incoming request
        # actually supplies an identity.
        if new_job is not None:
            different_job = _different_job(entry, req)
            if old_job is not None and different_job:
                entry["job_completed_at"] = _now_iso()
                entry["prior_job_key"] = old_job
            for field in ("conversation_id", "caller_work_id"):
                value = req.get(field)
                # A continuation may omit one known identity axis. Preserve it
                # until comparable evidence actually establishes a new job.
                if different_job or isinstance(value, str) and value:
                    entry[field] = value
            entry["job_key"] = job_key(entry)
        entry["last_request_id"] = req.get("id")
        entry["updated_at"] = _now_iso()
        if observed:
            entry["observed"] = observed
            entry["observed_source"] = observed_source
        try:
            save_entry(root, session_id, entry)
        except OSError:
            pass
        return entry


def release_unlaunched(root, *, session_id: str | None, request_id) -> dict | None:
    """Clear a launch's own busy reservation after it turns out the child
    process never actually started at all (Codex review, 2026-09-29): a
    launcher reserves `busy` before it starts the process, and a halt or
    cancel just before the process starts can end the launch BEFORE it ever
    reaches its own `finalize` call. Left alone, that leaves the registry
    permanently `busy` for a launch that never happened. Clears `busy`/
    `in_flight_request_id` ONLY when they still name exactly THIS
    ``request_id`` -- never a different (possibly still-running, or already
    otherwise resolved) request's reservation. Returns the updated entry, or
    ``None`` when there was nothing to clear (unknown session, not busy, or
    the reservation belongs to a different request)."""
    if not session_id or not request_id:
        return None
    with _locked_registry(root):
        entry = load_entry(root, session_id)
        if entry is None:
            return None
        if not entry.get("busy") or entry.get("in_flight_request_id") != request_id:
            return None
        entry["busy"] = False
        entry["in_flight_request_id"] = None
        entry["updated_at"] = _now_iso()
        try:
            save_entry(root, session_id, entry)
        except OSError:
            pass
        return entry


def release(root, session_id: str) -> dict:
    """Owner-invoked manual recovery only (`python -m eutonos.worker_models
    release --session <id>`), never automatic: unconditionally clear a stuck
    `busy`/`in_flight_request_id` reservation, e.g. after the launching
    process or the whole machine died mid-launch and `release_unlaunched`
    (the launch never started) never ran. Returns ``{"session_id",
    "found": False}`` for an unknown session, or ``{"session_id", "found":
    True, "cleared": bool, "prior_in_flight_request_id"}`` naming what (if
    anything) was actually cleared."""
    with _locked_registry(root):
        entry = load_entry(root, session_id)
        if entry is None:
            return {"session_id": session_id, "found": False}
        was_busy = bool(entry.get("busy"))
        prior_in_flight = entry.get("in_flight_request_id")
        entry["busy"] = False
        entry["in_flight_request_id"] = None
        entry["updated_at"] = _now_iso()
        try:
            save_entry(root, session_id, entry)
        except OSError:
            pass
        return {"session_id": session_id, "found": True, "cleared": was_busy,
                "prior_in_flight_request_id": prior_in_flight}


def provenance_view(entry: dict) -> dict:
    """The additive ``model_provenance`` reply field built from a registry
    entry -- everything except the internal profile tag (the reply already
    carries its own ``session_id``)."""
    return {k: v for k, v in entry.items() if k != "profile"}


def observed_model_from_claude_result(data) -> str | None:
    """The exact model actually used, per Claude's own `-p --output-format
    json` result object's ``modelUsage`` (a dict keyed by exact model id).
    Only unambiguous when it carries exactly one key; a session that used
    more than one model (or a result with no such field at all) is left
    unobserved rather than guessed."""
    if not isinstance(data, dict):
        return None
    usage = data.get("modelUsage")
    if isinstance(usage, dict) and len(usage) == 1:
        (key,) = usage.keys()
        if isinstance(key, str) and key:
            return key
    return None


# ------------------------------------------------------------------ read-only status CLI

def _status_view(entry: dict) -> dict:
    return {
        "harness": entry.get("harness"), "requested": entry.get("requested"),
        "resolved": entry.get("resolved"), "observed": entry.get("observed"),
        "selection_kind": entry.get("selection_kind"), "family": entry.get("family"),
        "retired": bool(entry.get("retired")), "retired_reason": entry.get("retired_reason"),
        "busy": bool(entry.get("busy")), "in_flight_request_id": entry.get("in_flight_request_id"),
        "job_key": entry.get("job_key"), "job_completed": entry.get("job_completed_at") is not None,
        "job_completed_at": entry.get("job_completed_at"), "prior_job_key": entry.get("prior_job_key"),
        "created_request_id": entry.get("created_request_id"), "last_request_id": entry.get("last_request_id"),
        "created_at": entry.get("created_at"), "updated_at": entry.get("updated_at"),
    }


def status(root, session_id: str | None = None) -> dict:
    """Read-only: a session's recorded model provenance, or (with no
    ``session_id``) every recorded session under this root's registry."""
    if session_id:
        entry = load_entry(root, session_id)
        if entry is None:
            return {"session_id": session_id, "found": False}
        return {"session_id": session_id, "found": True, **_status_view(entry)}
    rows = []
    stems = set()
    for directory in (registry_dir(root), legacy_registry_dir(root)):
        if directory.is_dir():
            stems.update(path.stem for path in directory.glob("*.json"))
    for stem in sorted(stems):
        data = load_entry(root, stem)  # same byte-cap/validation as any other read; the current folder wins
        if data is not None:
            rows.append({"session_id": data.get("session_id"), **_status_view(data)})
    return {"sessions": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eutonos.worker_models",
                                     description="Worker-model provenance registry: read-only status, "
                                                 "plus owner-invoked manual busy-reservation recovery.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="print one session's recorded model provenance, "
                                      "or every recorded session with no --session")
    p.add_argument("--root", default=".")
    p.add_argument("--session", dest="session_id", default=None)
    p2 = sub.add_parser("release", help="owner-invoked only: unconditionally clear a stuck "
                                        "busy/in_flight_request_id reservation (see `release`'s docstring)")
    p2.add_argument("--root", default=".")
    p2.add_argument("--session", dest="session_id", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        result = status(args.root, args.session_id)
    elif args.command == "release":
        result = release(args.root, args.session_id)
    else:  # pragma: no cover - argparse already restricts `command` to known subparsers
        raise SystemExit(f"unknown command: {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
