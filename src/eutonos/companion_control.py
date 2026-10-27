"""Local instruction-driven companion trial switch. No agents or services started."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .companion_capture import status as capture_status
from .local_lock import local_lock
from . import model_catalog
from .settings import (COMPANION_PROFILE as PROFILE,
                       EFFORTS, MODELS, MODEL_EFFORTS, _atomic, read_companion, validate_routing)


# Owner-defined escalation order, not a universal quality or cost ranking.
ESCALATION_ORDER = ("luna", "sol", "astra")
# Policy tiers do not change when the catalog is reordered or gains versions.
# Unranked families require explicit selection/approval for a cross-family move.
_FAMILY_TIERS = {"luna": 0, "haiku": 0, "sol": 1, "sonnet": 1, "astra": 2, "opus": 2}


def _family(model):
    if model in _FAMILY_TIERS:
        return model
    # Match the family token, never a substring of an unknown model's name.
    # Historical exact IDs still carry their tier after leaving the catalog.
    match = re.fullmatch(r"gpt-\d+(?:\.\d+)*-([a-z][a-z0-9]*)(?:-[a-z0-9-]+)?", model)
    if match is None:
        match = re.fullmatch(r"claude-([a-z][a-z0-9]*)-\d+(?:-\d+)*", model)
    family = match.group(1) if match else None
    return family if family in _FAMILY_TIERS else None


def eligible_choice(root, allowed, requested, resolved=None):
    """An exact eligibility pin stays exact; a family admits its current target."""
    if requested in allowed or resolved is not None and resolved in allowed:
        return True
    for choice in allowed:
        if choice in _FAMILY_TIERS:
            try:
                entry = model_catalog.resolve_choice(choice, "codex" if choice in ("luna", "sol", "astra") else "claude", root=root)
            except ValueError:
                continue
            if entry["resolved"] == (resolved or requested):
                return True
    return False
CONTEXT_POLICY = ("instruction-driven compact packets: omit raw irrelevant searches, dumps and intermediate reasoning; "
                  "retain relevant failures and constraints with source pointers; shared root instructions and tool "
                  "schemas remain visible and are not runtime-hidden")


def _default_state():
    return {"profile": PROFILE, "enabled": False, "routing": "adaptive", "model": None,
            "effort": None, "allowed_models": list(MODELS)}


def _read_state(path):
    try:
        return read_companion(path.parents[1])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid companion control; no delegation authorized by this file") from exc


def control(root, command="status", model=None, effort=None):
    root = Path(root).resolve()
    path = root / ".eutonos-local" / "companion.json"
    catalog = model_catalog.describe_catalog(root)["models"]
    if (command not in ("status", "on", "off") or model is not None and model not in catalog
            or effort is not None and effort not in EFFORTS):
        raise ValueError("Unsupported companion command/model/effort")
    with local_lock(root, create=command != "status"):
        state = _read_state(path)
        if model is not None and not eligible_choice(root, state["allowed_models"], model):
            raise ValueError("Helper model is outside the configured allowed models")
        if model is not None and effort not in (None, *catalog[model]["efforts"]):
            raise ValueError("Unsupported effort for helper model")
        if command != "status":
            state = {**state, "profile": PROFILE, "enabled": command == "on"}
            if command == "on":
                state.update({"routing": "fixed" if model else "adaptive", "model": model,
                              "effort": effort})
            validate_routing({key: state[key] for key in ("enabled", "routing", "model", "effort", "allowed_models")},
                             root=root, allow_unavailable=True)
            _atomic(path, state)
    return {**state, "enforcement": "repository instructions; no runtime interception",
            "context_policy": CONTEXT_POLICY,
            "primary_model": "unchanged; helper selection never switches the primary",
            "running_agent": "not inspected", "effect": "future delegation only; existing work is not interrupted"}


def routing_decision(primary, candidate, *, user_selected=False, approval="pending", waited_seconds=0, root=None):
    """Advisory per-task decision; caller must obtain real user approval."""
    catalog = model_catalog.describe_catalog(root)["models"]
    if (primary not in catalog and not model_catalog.is_exact_model_id(primary)) or candidate not in catalog:
        raise ValueError("Identify supported primary and helper models before routing")
    if type(user_selected) is not bool or approval not in ("pending", "approved", "denied"):
        raise ValueError("Invalid routing approval state")
    if type(waited_seconds) not in (int, float) or not 0 <= waited_seconds < float("inf"):
        raise ValueError("Routing wait must be finite nonnegative seconds")
    primary_family, candidate_family = _family(primary), _family(candidate)
    higher = (primary != candidate and
              (primary_family not in _FAMILY_TIERS or candidate_family not in _FAMILY_TIERS or
               _FAMILY_TIERS[candidate_family] > _FAMILY_TIERS[primary_family]))
    if user_selected or not higher or approval == "approved":
        return {"outcome": "selected", "model": candidate, "primary_model": primary,
                "basis": "explicit-user-selection" if user_selected else "approved-upgrade" if higher else "same-or-lower-tier"}
    if approval == "denied" or waited_seconds >= 60:
        return {"outcome": "continue-primary", "model": primary, "primary_model": primary,
                "basis": "upgrade-denied" if approval == "denied" else "no-approval-within-60-seconds"}
    return {"outcome": "approval-required", "model": None, "primary_model": primary,
            "proposed_model": candidate, "fallback_model": primary, "wait_seconds_remaining": 60 - waited_seconds}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "on", "off"))
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=EFFORTS)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if (args.model or args.effort) and args.command != "on":
        parser.error("--model and --effort apply to on only")
    try:
        root = Path(args.root).resolve()
        result = control(root, args.command, args.model, args.effort)
        capture = capture_status(root)
        result["capture_assessments"] = {"completed": len(capture["counts_completed"]),
            "pending_receipt_ids": capture["pending"],
            "coverage": "recorded receipts only; unobserved turns are unknown"}
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"outcome": "error", "enabled": False, "message": str(exc)}))
        return 2


__all__ = ["CONTEXT_POLICY", "EFFORTS", "ESCALATION_ORDER", "MODELS", "PROFILE",
           "control", "main", "routing_decision"]
