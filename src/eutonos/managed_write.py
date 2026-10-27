"""One exact, temporary, journaled transaction for high-level CLI writes."""
from __future__ import annotations

from . import tokenslang as ts
from .effects import decode, digest
from .host import abandon_lease, connect, selected_apply


def _effect_rows(coordinator, attempt):
    with coordinator._db() as db:
        return db.execute(
            "SELECT id,state,fingerprint,plan FROM effects WHERE attempt=? ORDER BY rowid",
            (attempt,),
        ).fetchall()


def _finish_clean(root, host, actor, attempt, coordinator, authority, generation, checkpoint):
    """Release an active attempt whose journal proves that no target write landed."""
    state = coordinator.inspect(attempt)
    expected = {row["path"]: digest(authority.root.read(row["path"])) for row in state["reservations"]}
    if state["state"] == "paused":
        ts.require(not state["reservations"] and not state["unfinished_effects"],
                   "conflict", "Paused managed write still owns recovery state.")
        closed = {"retired_grants": []}
    elif expected:
        closed = coordinator.closeout(authority, attempt, generation, expected, checkpoint)
    else:
        coordinator.pause(authority, attempt, generation)
        closed = {"retired_grants": []}
    retired = abandon_lease(root, host, actor, attempt)
    return sorted(set(closed.get("retired_grants", []) + retired.get("retired_grants", [])))


def recover(root, host, actor, attempt, checkpoint, verify=None, *, effect_id=None, fingerprint=None, on_verified=None):
    """Recover one deterministic managed attempt before interpreting current source.

    A fully applied journal is verified and closed.  A journal proven entirely
    unapplied is released so its semantic request can be planned again.  Mixed
    or divergent bytes retain their reservations and exact lease for explicit
    recovery instead of being guessed through.
    """
    ts.require(on_verified is None or (callable(on_verified) and callable(verify)),
               "invalid", "Verification callback requires a typed verifier.")
    coordinator, authority = connect(root, host, actor)
    try:
        state = coordinator.inspect(attempt)
    except ts.ReadError as exc:
        if exc.code == "missing":
            return {"outcome": "absent", "complete": False}
        raise
    ts.require(state["actor"] == authority.actor and state["root"] == coordinator._root(authority),
               "wrong-scope", "Managed write attempt belongs to another actor or root.")
    rows = _effect_rows(coordinator, attempt)
    ts.require(len(rows) <= 1, "conflict", "Managed write attempt contains more than one effect.")
    row = rows[0] if rows else None
    retry_advice = ""
    if row is not None and ((effect_id is not None and row["id"] != effect_id)
                            or (fingerprint is not None and row["fingerprint"] != fingerprint)):
        if "board_import" in ts.loads(row["plan"]):
            retry_advice = (" Recover the original attempt first; after verified cleanup and released reservations, "
                            "use a fresh operation_id if the census changed.")
    if row is not None and effect_id is not None:
        ts.require(row["id"] == effect_id, "conflict", "Managed write ID is bound to another effect." + retry_advice)
    if row is not None and fingerprint is not None:
        ts.require(row["fingerprint"] == fingerprint, "conflict",
                   "Managed write fingerprint is bound to another effect." + retry_advice)

    generation = state["generation"]
    if state["state"] == "paused" and (state["reservations"] or state["unfinished_effects"]):
        generation = coordinator.resume(authority, attempt, generation)
        state = coordinator.inspect(attempt)
    elif state["state"] == "paused":
        if row is None or row["state"] in {"not-applied", "rolled-back"}:
            retired = _finish_clean(root, host, actor, attempt, coordinator, authority, generation,
                                    {"completed": "No managed write was applied.",
                                     "next": "Replan the semantic request.", "pending": None})
            return {"outcome": "retry", "complete": False, "attempt": attempt,
                    "retired_grants": retired}
        ts.require(row["state"] == "completed", "conflict",
                   "Paused managed write has an unsupported journal state.")

    if row is None:
        retired = _finish_clean(root, host, actor, attempt, coordinator, authority, generation,
                                {"completed": "No managed write was applied.",
                                 "next": "Replan the semantic request.", "pending": None})
        return {"outcome": "retry", "complete": False, "attempt": attempt,
                "retired_grants": retired}

    if row["state"] == "not-applied":
        effect = ts.loads(row["plan"])
        ts.require(digest(ts.encoded(effect)) == row["fingerprint"], "conflict",
                   "Unapplied managed write journal fingerprint differs.")
        ts.require(all(digest(decode(change["backup"])) == change["before"] for change in effect["changes"]),
                   "conflict", "Unapplied managed write backup differs from its baseline.")
        ts.require(all(digest(authority.root.read(change["path"])) == change["before"] for change in effect["changes"]),
                   "stale", "Unapplied managed write bytes changed before cleanup.")
        retired = _finish_clean(root, host, actor, attempt, coordinator, authority, generation,
                                {"completed": "Verified restored or unapplied managed write baseline.",
                                 "next": "Replan the semantic request.", "pending": None})
        return {"outcome": "retry", "complete": False, "attempt": attempt,
                "retired_grants": retired}

    if row["state"] in {"prepared", "rolling-back"}:
        reconciled = coordinator.reconcile(authority, attempt, generation, row["id"])
        abort_advice = (" Use host rollback to abort the interrupted import, then retry managed recovery."
                        if reconciled["outcome"] in {"partial", "rollback-incomplete"}
                        and "board_import" in ts.loads(row["plan"]) else "")
        ts.require(reconciled["outcome"] in {"completed", "not-applied", "rolled-back"},
                   "conflict",
                   "Interrupted managed write is partial or divergent; exact protection was retained." + abort_advice)
        if reconciled["outcome"] in {"not-applied", "rolled-back"}:
            retired = _finish_clean(root, host, actor, attempt, coordinator, authority, generation,
                                    {"completed": "Interrupted managed write did not land.",
                                     "next": "Replan the semantic request.", "pending": None})
            return {"outcome": "retry", "complete": False, "attempt": attempt,
                    "retired_grants": retired}
    else:
        ts.require(row["state"] == "completed", "conflict",
                   "Managed write journal is not recoverable automatically.")

    effect = ts.loads(row["plan"])
    expected = {change["path"]: digest(decode(change["after"])) for change in effect["changes"]}
    ts.require(all(digest(authority.root.read(path)) == value for path, value in expected.items()),
               "stale", "Recovered managed write bytes changed before verification.")
    if verify is not None:
        verify()
    current = coordinator.inspect(attempt)
    if on_verified is not None:
        on_verified({"outcome": "typed-verified", "complete": False, "writes": 0,
                     "readback": "verified", "reason": "recovered", "paths": sorted(expected),
                     "actor": authority.actor, "attempt": attempt, "generation": current["generation"]})
    retired = []
    if current["state"] == "active":
        closed = coordinator.closeout(authority, attempt, generation, expected, checkpoint)
        retired.extend(closed.get("retired_grants", []))
    else:
        ts.require(current["state"] == "paused" and not current["reservations"] and
                   not current["unfinished_effects"], "conflict",
                   "Recovered managed write cleanup is incomplete.")
    abandoned = abandon_lease(root, host, actor, attempt)
    retired.extend(abandoned.get("retired_grants", []))
    final = coordinator.inspect(attempt)
    ts.require(final["state"] == "paused" and not final["reservations"] and not final["unfinished_effects"],
               "conflict", "Recovered managed write host cleanup is incomplete.")
    return {"outcome": "complete", "complete": True, "writes": 0,
            "readback": "verified", "reason": "recovered", "paths": sorted(expected),
            "actor": authority.actor, "attempt": attempt, "generation": final["generation"],
            "retired_grants": sorted(set(retired))}


def begin(root, host, actor, attempt, paths):
    """Return one active attempt; file reservations provide write coordination."""
    coordinator = authority = None
    try:
        coordinator, authority = connect(root, host, actor)
        authority.check(paths)
        try:
            state = coordinator.inspect(attempt)
        except ts.ReadError as exc:
            if exc.code != "missing":
                raise
            state = None
        if state is not None:
            ts.require(state["actor"] == authority.actor and
                       state["root"] == coordinator._root(authority),
                       "wrong-scope", "Managed write attempt belongs to another actor or root.")
        generation = (coordinator.start(authority, attempt) if state is None else
                      coordinator.resume(authority, attempt, state["generation"])
                      if state["state"] == "paused" else state["generation"])
        return coordinator, authority, generation
    except BaseException:
        # Preserve any reservations or interrupted effects if start failed.
        try:
            abandon_lease(root, host, actor, attempt)
        except (OSError, ValueError, ts.ReadError):
            pass
        raise


def abort(root, host, actor, attempt, coordinator=None, authority=None):
    """Pause a clean attempt without discarding unresolved file protection."""
    if coordinator is not None:
        try:
            state = coordinator.inspect(attempt)
            if state["state"] == "active" and not state["reservations"] and not state["unfinished_effects"]:
                coordinator.pause(authority, attempt, state["generation"])
        except ts.ReadError:
            pass
    try:
        return abandon_lease(root, host, actor, attempt)
    except (OSError, ValueError, ts.ReadError) as exc:
        return {"outcome": "unavailable", "message": getattr(exc, "message", str(exc)),
                "retired_grants": []}


def apply(root, host, actor, attempt, effect_id, effect, fingerprint, paths, checkpoint, verify=None,
          *, _import_derivation=None, on_verified=None):
    """Reserve all paths, apply once, verify every output and release reservations."""
    # Trusted callback persists a proof while reservations still protect the
    # verified bytes. It is not a completed receipt: cleanup can still fail.
    ts.require(on_verified is None or (callable(on_verified) and callable(verify)),
               "invalid", "Verification callback requires a typed verifier.")
    selected = sorted(set(paths))
    ts.require(selected and selected == sorted(change["path"] for change in effect["changes"]),
               "conflict", "Managed write paths differ from its effect.")
    coordinator = authority = None
    try:
        prior = recover(root, host, actor, attempt, checkpoint, verify=verify,
                        effect_id=effect_id, fingerprint=fingerprint, on_verified=on_verified)
        if prior["complete"]:
            return prior
        ts.require("board_import" not in effect or callable(_import_derivation), "denied",
                   "Import-tagged effects require trusted typed source derivation.")
        coordinator, authority, generation = begin(root, host, actor, attempt, selected)
        coordinator.reserve(authority, attempt, generation, selected)
        result = selected_apply(coordinator, authority, attempt, generation, effect_id, effect, fingerprint,
                                **({"_import_derivation": _import_derivation} if _import_derivation is not None else {}))
        expected = {change["path"]: digest(decode(change["after"])) for change in effect["changes"]}
        ts.require(all(digest(authority.root.read(path)) == value for path, value in expected.items()),
                   "stale", "Managed write readback differs from its exact effect.")
        if verify is not None:
            verify()
        if on_verified is not None:
            on_verified({"outcome": "typed-verified", "complete": False,
                         "writes": result.get("writes", 0), "readback": "verified", "paths": selected,
                         "actor": authority.actor, "attempt": attempt, "generation": generation})
        closed = coordinator.closeout(authority, attempt, generation, expected, checkpoint)
        state = coordinator.inspect(attempt)
        ts.require(state["state"] == "paused" and not state["reservations"] and not state["unfinished_effects"],
                   "conflict", "Managed write host cleanup is incomplete.")
        return {"outcome": "complete", "complete": True,
                "writes": result.get("writes", 0), "readback": "verified",
                "paths": selected, "actor": authority.actor, "attempt": attempt,
                "generation": state["generation"], "retired_grants": closed.get("retired_grants", [])}
    except BaseException:
        abort(root, host, actor, attempt, coordinator, authority)
        raise
