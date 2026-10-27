"""Read local EUTONOS settings or make an advisory task-routing decision."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

from eutonos import settings


def _companion():
    from eutonos import companion_control
    return companion_control


def route(root, task, primary, approval="pending", waited_seconds=0):
    """Advisory task routing, now resolved through a settings role (AWR worker profiles).

    ``state["task_rules"][task]`` holds a role name (or ``"orchestrator"``, meaning the
    primary keeps the task). This no longer names a model/effort pair directly: the role
    is resolved via ``role_router.resolve_role``, which applies the role's own harness/
    model/effort options and profile placement. Unlike the old "inherit" task rule, a
    role always resolves to one concrete model -- there is no "adaptive: pick any eligible
    model" outcome any more; an unresolvable or ineligible role reports "invalid-settings"
    instead."""
    from . import role_router
    state = settings.status(root)
    role_name = state["task_rules"][task]
    routing = state["routing"]

    def decision(result):
        return {**result, "routing_profile": routing["routing_profile"],
                "max_concurrent_companions": routing["max_concurrent_companions"]}

    if not state["routing"]["enabled"]:
        return decision({"outcome": "continue-primary", "model": primary, "primary_model": primary,
                         "task": task, "basis": "companion-off", "effort": None, "role": role_name})
    if role_name == "orchestrator":
        return decision({"outcome": "continue-primary", "model": primary, "primary_model": primary,
                         "task": task, "basis": "task-rule-orchestrator", "effort": None, "role": role_name})
    try:
        resolved = role_router.resolve_role(root, role_name)
    except role_router.RoleRouterError as exc:
        return decision({"outcome": "invalid-settings", "model": None, "primary_model": primary,
                         "task": task, "basis": "task-role-unresolvable", "role": role_name,
                         "message": exc.message})
    candidate, effort = resolved["model"], resolved["effort"]
    if resolved["harness"] not in ("codex", "claude"):
        return decision({"outcome": "invalid-settings", "model": None, "primary_model": primary,
                         "task": task, "basis": "task-role-harness-unsupported", "role": role_name})
    harness_state = state.get("harnesses", {}).get(resolved["harness"])
    if harness_state is not None and not harness_state["enabled"]:
        return decision({"outcome": "invalid-settings", "model": None, "primary_model": primary,
                         "task": task, "basis": "task-role-harness-disabled", "role": role_name})
    if not _companion().eligible_choice(root, routing["allowed_models"], resolved["model_requested"], candidate):
        return decision({"outcome": "invalid-settings", "model": None, "primary_model": primary,
                         "task": task, "basis": "task-role-model-not-eligible", "role": role_name,
                         "proposed_model": candidate, "effort": effort})
    if effort is not None and effort not in resolved["model_selection"].get(
            "allowed_efforts", resolved["model_selection"]["supported_efforts"]):
        return decision({"outcome": "invalid-settings", "model": None, "primary_model": primary,
                         "task": task, "basis": "task-role-effort-unsupported", "role": role_name,
                         "proposed_model": candidate, "proposed_effort": effort})
    result = _companion().routing_decision(primary, candidate, approval=approval, root=root,
                                           waited_seconds=waited_seconds)
    if result["outcome"] == "selected":
        result.update(task=task, effort=effort, role=role_name,
                      model_requested=resolved["model_requested"], model_selection=resolved["model_selection"])
    else:
        result.update(task=task, effort=None, proposed_effort=effort, role=role_name)
    return decision(result)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if 'participant-profile' in arguments:
        from .work_attribution import cli
        index = arguments.index('participant-profile')
        return cli(arguments[:index] + arguments[index + 1:])
    if 'shared-work' in arguments:
        from .shared_work_cli import main as shared_main
        index = arguments.index('shared-work')
        return shared_main(arguments[:index] + arguments[index + 1:])
    if 'delivery' in arguments:
        from .git_delivery_cli import main as delivery_main
        index = arguments.index('delivery')
        return delivery_main(arguments[:index] + arguments[index + 1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("shared-work", help="Configure explicit shared sources and local participation")
    commands.add_parser("delivery", help="Configure, submit, inspect or recover the single scoped Git publisher")
    report = commands.add_parser(
        "report-claude-models",
        help="Record the Claude model IDs this chat can see (metadata only) for the model catalog")
    report.add_argument("--model", action="append", required=True, dest="models",
                        help="Exact Claude model ID, e.g. claude-sonnet-5-5; repeat for several")
    report.add_argument("--reported-on", default=None, help="YYYY-MM-DD (default: today)")
    routing = commands.add_parser("route")
    routing.add_argument("--task", required=True, choices=settings.TASKS)
    routing.add_argument("--primary", required=True)
    routing.add_argument("--approval", choices=("pending", "approved", "denied"), default="pending")
    routing.add_argument("--waited-seconds", type=float, default=0)
    for child in commands.choices.values():
        child.add_argument("--root", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "report-claude-models":
            from . import model_catalog
            written = model_catalog.write_claude_report(args.models, reported_on=args.reported_on)
            print(json.dumps({"outcome": "reported", **written}, separators=(",", ":")))
            return 0
        result = settings.status(args.root) if args.command == "status" else route(
            args.root, args.task, args.primary, args.approval, args.waited_seconds)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"outcome": "error", "message": str(exc)}, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
