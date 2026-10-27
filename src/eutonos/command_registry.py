"""Shared, inspectable command definitions for the guided EUTONOS workflow."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from .orientation import REQUEST_SCHEMA as ORIENTATION_REQUEST_SCHEMA, PACKET_SCHEMA as ORIENTATION_PACKET_SCHEMA, EFFECTS as ORIENTATION_EFFECTS


PROFILE = "eutonos.tokenslang.workflow.v1"

EXPERT_ROUTES = {"tests": "eutonos.user_tests_cli", "skills": "eutonos.skills_cli", "version": "eutonos.release", "ram-relevance": "eutonos.ram_relevance_cli", "boards": "eutonos.boards_cli", "eutonos": "eutonos.cli", "docs": "eutonos.docs_cli", "ram": "eutonos.ram_cli", "nav": "eutonos.nav_cli",
                 "host": "eutonos.host_cli", "settings": "eutonos.settings_cli",
                 "setup": "eutonos.setup_cli", "dashboard": "eutonos.dashboard_cli",
                 "communication": "eutonos.communication_cli",
                 "measurement": "eutonos.measurement_cli", "companion": "eutonos.companion_control",
                 "capture": "eutonos.companion_capture", "ram-capture": "eutonos.companion_ram_capture",
                 "assignments": "eutonos.assignments_cli", "delivery": "eutonos.delivery_summary_cli",
                 "write-retry": "eutonos.write_retry_cli",
                 "attribution": "eutonos.companion_attribution_cli", "precheck": "eutonos.work_precheck",
                 "closeout-debt": "eutonos.closeout_debt", "state": "eutonos.repository_state_cli",
                 "goals": "eutonos.goals_cli", "personal": "eutonos.personal_cli", "device": "eutonos.device_repositories_cli",
                 "goal-context": "eutonos.goal_context"}

CAPTURE_FIELDS = (
    {"name": "children", "type": "array", "required": False,
     "description": "Explicit native RAM child references {identity: {work_id, session_id, issuer_id, memory_id}, path, revision}. Each must be current and eligible in this repository. Nonempty children make this closeout a milestone; child prose is never inferred."},
    {"name": "work_size", "type": "string", "required": False, "enum": ["quick", "slice", "campaign"],
     "description": "Quick edit, substantial standalone slice, or campaign exchange. Defaults to slice; no file-count threshold."},
    {"name": "event", "type": "string", "required": False, "enum": ["turn", "checkpoint", "milestone"],
     "description": "The actual completion boundary, default turn. Capture cadence decides whether to defer."},
    {"name": "consolidated_pending_ids", "type": "array[string]", "required": False,
     "description": "Exact pending IDs summarized by this form. Their risks, decisions, checks and changed paths are still retained."},
    {"name": "selected_pending_ids", "type": "array[string]", "required": False,
     "description": "Optional exact pending IDs for this capture; omitted selects all, empty selects none. Other notes remain pending."},
)

CATALOG_FIELDS = (
    {"name": "catalog_changes", "type": "array[string]", "required": True,
     "prompt": "Did this work add, change or retire features or capabilities? List them, or use [] for None.",
     "description": "Explicit product assessment, required even when RAM capture is off."},
    {"name": "catalog_ids", "type": "array[string]", "required": False,
     "description": "Saved catalog IDs for the changes; required when catalog_changes is nonempty."},
)

WORK_FIELDS = (
    {"name": "work_closeout", "type": "array", "required": False,
     "prompt": "For each changed work item: what did you do, why, result, checks, status and estimated percent complete? Cancelled or superseded rows also require cancellation_reason.",
     "description": "Required when work lifecycle is configured. Object answers include item_id, expected_revision, status, what, why, result, checks and progress {percent: 0..100 or null, basis}. Cancelled and superseded rows require cancellation_reason; superseded also requires superseded_by. Optional stage_assessment records completed-stage plan reasoning; execution_recommendation is advisory. Use boards work form --item-id ID. Campaign completion also requires acceptance_confirmed; optional defer_owner_move: true retains a mixed owner and explicitly defers its archive/move."},
    {"name": "work_closeout_reason", "type": "string", "required": False,
     "description": "Required with an empty work_closeout array: explain why no tracked work changed."},
    {"name": "work_changes", "type": "array[string]", "required": True,
     "prompt": "Did the user add work while this run was active? List additions already saved through the board CLI, or use [] for none.",
     "description": "Immediate-work assessment. Added scope belongs on the board when requested, before more implementation."},
    {"name": "work_ids", "type": "array[string]", "required": False,
     "description": "Saved campaign, stage, slice, or Quick work IDs; required when work_changes is nonempty."},
)

CLOSEOUT_FIELDS = (
    {"name": "request", "type": "string", "required": True, "prompt": "What did the user ask for?",
     "description": "The bounded user request in plain language."},
    {"name": "actions", "type": "array[string]", "required": True, "prompt": "What did the agent do?",
     "description": "Actions actually taken."},
    {"name": "result", "type": "string", "required": True, "prompt": "What was the outcome?",
     "description": "Observed result without inferred acceptance."},
    {"name": "why", "type": "string", "required": True, "prompt": "Why were these files changed?",
     "description": "Reason the selected files changed."},
    {"name": "checks", "type": "array[string]", "required": False, "description": "Checks actually run."},
    {"name": "unresolved", "type": "array[string]", "required": False, "description": "Remaining uncertainty or work."},
    {"name": "changed_paths", "type": "array[string]", "required": False,
     "description": "Optional exact scope; omitted paths are detected from Git and are not assumed reviewed."},
    {"name": "decisions", "type": "array[string]", "required": False, "description": "Optional user or agent decisions."},
    *CAPTURE_FIELDS,
)

LEGACY_FIELDS = (
    {"name": "what", "type": "string", "required": True, "description": "Legacy description of what changed."},
    {"name": "why", "type": "string", "required": True, "description": "Why this result is useful or required."},
    {"name": "checks", "type": "array[string]", "required": True, "description": "Checks actually run."},
    {"name": "unresolved", "type": "array[string]", "required": True, "description": "Remaining uncertainty or work."},
    {"name": "changed_paths", "type": "array[string]", "required": False},
    {"name": "decisions", "type": "array[string]", "required": False},
    *CAPTURE_FIELDS,
)


def _object_schema(properties=None, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties or {},
            "required": list(required)}


def _field_schema(fields):
    properties = {}
    required = []
    for field in fields:
        properties[field["name"]] = ({"type": "array", "items": {"type": "string", "pattern": r"\S"}}
                                     if field["type"] == "array[string]" else {"type": field["type"], "pattern": r"\S"})
        if field["name"] == "children":
            text = {"type": "string", "pattern": r"\S"}
            identity = _object_schema({name: text for name in ("work_id", "session_id", "issuer_id", "memory_id")},
                                      ("work_id", "session_id", "issuer_id", "memory_id"))
            properties[field["name"]] = {"type": "array", "items": _object_schema({
                "identity": identity, "path": text, "revision": {"type": "integer", "minimum": 1}},
                ("identity", "path", "revision"))}
        elif field["type"] == "array":
            text = {"type": "string", "pattern": r"\S"}
            assessment = _object_schema({"disposition": {"type": "string", "enum": ["unchanged", "adjusted", "review-required", "blocked"]},
                "basis": text, "affected_stage_ids": {"type": "array", "items": text, "uniqueItems": True},
                "changes": {"type": "array", "items": text}, "next_stage_id": text},
                ("disposition", "basis", "affected_stage_ids", "changes"))
            recommendation = _object_schema({"model": text, "effort": text, "basis": text, "advisory": {"const": True}}, ("model", "effort", "basis"))
            row = _object_schema({
                "item_id": text, "expected_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "status": {"type": "string", "enum": ["active", "blocked", "planned", "deferred", "completed", "cancelled", "superseded"]},
                "what": text, "why": text, "result": text,
                "cancellation_reason": text, "superseded_by": text,
                "checks": {"type": "array", "minItems": 1, "items": text},
                "completion_checks": {"type": "array", "maxItems": 40, "items": {"type": "object"}},
                "progress": _object_schema({"percent": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                                             "basis": text}, ("percent", "basis")),
                "acceptance_confirmed": {"type": "boolean"}, "defer_owner_move": {"type": "boolean"}, "next_action": text,
                "stage_assessment": assessment, "execution_recommendation": recommendation,
                "lane": {"type": "string", "enum": ["current", "next"]},
            }, ("item_id", "expected_revision", "status", "what", "why", "result", "checks", "progress"))
            properties[field["name"]] = {"type": "array", "items": row}
        properties[field["name"]]["description"] = field.get("description", "")
        if "enum" in field:
            properties[field["name"]]["enum"] = list(field["enum"])
        if field["required"]:
            required.append(field["name"])
    return _object_schema(properties, required)


def _closeout_schema():
    schema = _field_schema((*CLOSEOUT_FIELDS, *CATALOG_FIELDS, *WORK_FIELDS))
    memory_required = [f["name"] for f in CLOSEOUT_FIELDS if f["required"]]
    schema["required"] = ["catalog_changes", "work_changes"]
    schema["anyOf"] = [
        {"required": ["work_closeout"], "properties": {"work_closeout": {"minItems": 1}}},
        {"required": memory_required},
    ]
    schema["allOf"] = [{
        "if": {"required": ["work_closeout"], "properties": {"work_closeout": {"maxItems": 0}}},
        "then": {"required": ["work_closeout_reason"]},
    }]
    return schema


COMMANDS = (
    {"id": "find-context", "label": "Find context", "summary": "Read bounded source, NAV, or RAM context.",
     "module": "eutonos.cli", "example": {"route": "eutonos", "argv": ["describe"]},
     "task_intent": "Orient to bounded project context and resolve source-backed references.",
     "input_schema_scope": "native-route-description; the native CLI owns and validates its executable arguments",
     "input_schema": _object_schema({"route": {"type": "string"}, "argv": {"type": "array", "items": {"type": "string"}}}),
     "side_effects": [], "prerequisites": ["An installed EUTONOS core and access to the selected source view."],
     "usage": ["python -m eutonos.workflow_cli expert eutonos describe --root ROOT"],
     "steps": ["Describe the available read interface.", "Select the smallest bounded read for the task."],
     "sources": ["modules/agent-interface/CONTRACT.md#adaptive-context-not-compulsory-packets"]},
    {"id": "save-memory", "label": "Save memory", "summary": "Use the native guarded RAM interface.",
     "module": "eutonos.ram_cli", "example": {"route": "ram", "argv": ["plan", "--request", "request.json"]},
     "task_intent": "Plan or apply one source-qualified RAM record through native guarded effects.",
     "input_schema_scope": "native-route-description; the native CLI owns and validates its executable arguments",
     "input_schema": _object_schema({"route": {"const": "ram"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["A RAM apply can write an admitted native record and work-owner link; plan and read are read-only."],
     "prerequisites": ["Configured RAM ownership; writes also require an existing host, admission, and exact reviewed paths."],
     "usage": ["python -m eutonos.workflow_cli expert ram describe --root ROOT"],
     "steps": ["Inspect native RAM choices.", "Plan the exact record.", "Apply only with existing write authority and verify readback."],
     "sources": ["modules/ram/CONTRACT.md", "docs/DEPLOYMENT_STARTUP.md#eutonos-proc-ram"]},
    {"id": "closeout", "label": "Close out", "summary": "Capture a semantic closeout and return verified RAM and host readback.",
     "module": "eutonos.companion_ram_capture", "example": {"request": "Implement the bounded change.",
     "actions": ["Updated the selected module."], "result": "Targeted tests passed.",
     "why": "The files implement the requested behavior.", "checks": ["targeted tests passed"], "unresolved": [],
     "changed_paths": ["src/example.py"], "catalog_changes": [], "work_changes": []}, "guided": {"prepare": True, "run": True},
     "task_intent": "File selected work and optionally capture memory; phase results are independent.",
     "organization_example": {"organization": {"profile": "eutonos.tokenslang.closeout-organization.v1",
         "title": "Dashboard styling", "summary": "Changed the launch button styling; checked the result.",
         "disposition": "completed", "record_change": True, "area": "application", "module": "dashboard"}},
     "organization_questions": ["What changed and what was the result?", "Is work completed, continuing or blocked?",
                                "Did a durable fact change? Update only its owner.",
                                "Is this a coherent checked milestone that should be committed under existing authority?"],
     "organization_note": "An organization object selects filing independently of RAM; exact archive headings/path are optional. Review the prepared plan and pass that exact JSON to run. Legacy memory forms remain supported and report organization not assessed.",
     "input_schema_scope": "catalog and work assessment with lifecycle answers or legacy memory answers; prepare enforces repository configuration",
     "input_schema": _closeout_schema(),
     "side_effects": ["Organization mode can archive selected Current Task sections and append a structured change record using a guarded plan.", "Run can write one guarded RAM record and its native work-owner RAM link.",
                      "Multiple structured work decisions are planned and applied as one atomic typed batch; no successful subset is reported.",
                      "Managed board, organization and RAM writes obtain an exact short-lived lease and retire it after clean success or failure.",
                      "Architecture, principles, contracts, AGENTS and other nuanced prose remain outside mechanical batch authoring.",
                      "A write blocked by another exact owner is retained as a bounded semantic retry; Docs are replanned from current bytes and RAM reuses its frozen source-qualified operation.",
                      "Run writes a local frozen operation artifact and host journal metadata for guarded recovery.",
                      "When an active native log is explicitly supplied, run can write one native pre-final capture receipt.",
                      "The CLI can record compact local diagnostic metadata when tracking is enabled."],
     "prerequisites": ["Existing context values for host, actor, work_id, and session_id.",
                       "An active native log is required only when native_log is supplied for a Codex pre-final receipt.",
                       "Generic closeout does not require a native log. Input defaults to an empty object; stdin is read only with --input -."],
     "usage": ["python -m eutonos.workflow_cli prepare closeout --root ROOT --context CONTEXT.json --input SUMMARY.json",
               "python -m eutonos.workflow_cli run closeout --root ROOT --context CONTEXT.json --input SUMMARY.json"],
     "steps": ["Prepare: submit the semantic facts and existing context; preparation reports every missing field and candidate path.",
               "Fill: in a configured lifecycle, answer work_closeout rows with status, result, checks and estimated progress. These answers are reused for RAM; do not duplicate the narrative. Otherwise fill request, actions, result, why, checks and unresolved.",
               "Run: submit the reviewed summary, or replay the exact prepared result, through one guarded closeout. Multiple work rows use one atomic filing.",
               "For a Codex pre-final receipt, supply --native-log with the explicit active native log; the command verifies identity before effects and readback after them."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#eutonos-proc-guided-workflow",
                 "modules/tokenslang/CONTRACT.md#guided-workflow-adapter"]},
    {"id": "pending", "label": "Pick up deferred notes", "summary": "Preview local small-edit notes or expand one exact entry.",
     "module": "eutonos.capture_policy", "example": {"context": "CONTEXT.json", "limit": 10},
     "task_intent": "Read pending closeout context without creating RAM or reserving source files.",
     "input_schema_scope": "workflow pending command arguments",
     "input_schema": _object_schema({"context": {"type": "string"}, "entry": {"type": "string"},
                                     "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1}}, ("context",)),
     "side_effects": [], "prerequisites": ["The prior worker actor, work ID and session ID in its context file."],
     "usage": ["python -m eutonos.workflow_cli pending --root ROOT --context CONTEXT.json",
               "python -m eutonos.workflow_cli pending --root ROOT --context CONTEXT.json --entry ID"],
     "steps": ["Preview pending notes.", "Expand only relevant entries; a later eligible closeout captures included notes."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#work-sized-memory-capture"]},
    {"id": "nav", "label": "Maintain NAV", "summary": "Check NAV impact, plan exact outputs, then run a guarded update in one checkout.",
     "module": "eutonos.nav_cli", "example": {"route": "nav", "argv": ["status"]},
     "task_intent": "Update NAV for one selected root and its current branch/worktree. Other worktrees and branches are not updated; shared Current Work observation does not merge NAV maps.",
     "input_schema_scope": "native-route-description; the native CLI owns and validates its executable arguments",
     "input_schema": _object_schema({"route": {"const": "nav"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["status, maintenance, history-plan, report-template and validate-report are read-only (validate-report is mechanical only and consumes no retry); maintenance only identifies affected routes.",
                      "start, next and submit use the guarded host and may write NAV run/map outputs; host closeout releases only verified selected reservations."],
     "prerequisites": ["A configured NAV module; writes require the selected procedure and guarded host authority.",
                       "Replace ROOT, HOST, ACTOR, ATTEMPT, N, RUN and file/path placeholders with inspected values; use one unique RUN throughout. Stop on unavailable I/O, stale source, conflicts or unfinished effects and inspect the cause before continuing."],
     "usage": ["python -m eutonos.workflow_cli expert nav status --root ROOT"],
     "steps": ["Read-only: python -m eutonos.workflow_cli expert nav status --root ROOT. Inspect source/map freshness, the current run and gaps.",
               "Read-only: python -m eutonos.workflow_cli expert nav maintenance --root ROOT. Inspect changedPaths, affectedRecords and unmappedPaths; a no-op needs no maintenance cycle.",
               "Read-only: python -m eutonos.workflow_cli expert nav history-plan --run-id RUN --root ROOT. Keep its exact paths, including archive objects; replan on conflict.",
               "Read-only: python -m eutonos.workflow_cli expert host status --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT. Select an active attempt and current generation N through the documented host lifecycle; status does not prove process liveness.",
               "Guarded lifecycle: for a new attempt, run python -m eutonos.workflow_cli expert host start --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT. For an existing paused attempt, run python -m eutonos.workflow_cli expert host resume --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT --generation N using its observed generation. Use the generation returned by start or resume as N for reserve and all later writes. An already active attempt keeps its observed generation and needs neither command.",
               "Guarded write: python -m eutonos.workflow_cli expert host reserve --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT --generation N --path EXACT_OUTPUT. Repeat --path in this command for every history-plan path, then start the same RUN.",
               "Guarded write: python -m eutonos.workflow_cli expert nav start --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT --generation N --mode maintenance --run-id RUN.",
               "Guarded packet: python -m eutonos.workflow_cli expert nav next --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT --generation N. Inspect its issued source/context, then obtain the read-only skeleton with python -m eutonos.workflow_cli expert nav report-template --root ROOT. Save and fill REPORT.json as UTF-8 using exact issued IDs and current source evidence.",
               "Optional read-only pre-check: python -m eutonos.workflow_cli expert nav validate-report --root ROOT --request REPORT.json (mechanical validation only; writes nothing, consumes no retry). Guarded write: python -m eutonos.workflow_cli expert nav submit --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT --generation N --request REPORT.json. Repeat next, inspection, template and submit until the queue closes; stale reports require fresh inspection, and queue completion does not establish semantic acceptance.",
               "Closeout: reconcile any interrupted effect through the documented recovery path, then run python -m eutonos.workflow_cli expert host closeout --root ROOT --host HOST --actor ACTOR --attempt ATTEMPT --generation N --request CLOSEOUT.json with exact current hashes and a checkpoint. An authorized external edit first requires review-external of those bytes; its receipt retains the attribution gap and later byte changes invalidate it.",
               "Readback: repeat host status and nav status, then verify the selected reader questions against source. Report remaining reservations, NAV gaps and semantic acceptance separately; integration into another branch is a separate step."],
     "sources": ["modules/nav/CONTRACT.md", "docs/DEPLOYMENT_STARTUP.md#eutonos-proc-nav-maintain"]},
    {"id": "find-skill", "label": "Find a reusable skill", "summary": "Search durable procedures by task or tool, independently of RAM.",
     "module": "eutonos.skills_cli", "example": {"route": "skills", "argv": ["profile"]},
     "task_intent": "Find an established procedure before repeating task or tool discovery.",
     "input_schema_scope": "native skills CLI forms",
     "input_schema": _object_schema({"route": {"const": "skills"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Search/list/read are read-only. Explicit record-use writes deduplicated local usage metadata; no tool executes."],
     "prerequisites": ["A durable eutonos.skills.json catalog and readable procedure sources; RAM and ATLAS are not required."],
     "usage": ["python -m eutonos.workflow_cli expert skills search closeout --root ROOT"],
     "steps": ["Search task terms or filter by tool.", "Read the exact skill version and verify applicability.", "Record actual use only after the procedure was attempted."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#durable-skill-search"]},
    {"id": "find-memory", "label": "Browse or find memory", "summary": "List recent RAM summaries, filter/sort them, or search small relevant previews.",
     "module": "eutonos.ram_cli", "example": {"route": "ram", "argv": ["describe"]},
     "task_intent": "Browse recent memory, search relevant previews, expand exact revisions, or create an explicitly requested disposable report.",
     "input_schema_scope": "native RAM and RAM relevance CLI forms",
     "input_schema": _object_schema({"route": {"enum": ["ram", "ram-relevance"]}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Recent/browse/show and relevance search/get are read-only. RAM report writes only a disposable Markdown projection; feedback writes local metadata without changing native RAM."],
     "prerequisites": ["Configured native RAM and applicable visibility controls; optional lookup follows retrieval settings."],
     "usage": ["python -m eutonos.workflow_cli expert ram recent --limit 10 --root ROOT",
               "python -m eutonos.workflow_cli expert ram-relevance find TERMS --root ROOT"],
     "steps": ["Start with recent summaries or a filtered browse.", "Use relevance search when task terms matter.", "Expand an exact revision, then verify source before relying on it."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#relevant-ram-lookup"]},
    {"id": "boards", "label": "Read or update boards", "summary": "Record, adjust, prioritize and close native work through guarded CLI forms.",
     "module": "eutonos.boards_cli", "example": {"route": "boards", "argv": ["profile"]},
     "task_intent": "Maintain ordered native boards without duplicating document owners.",
     "input_schema_scope": "native boards CLI semantic forms",
     "input_schema": _object_schema({"route": {"const": "boards"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Read and plan are read-only; apply uses existing guarded effects and source fingerprints."],
     "prerequisites": ["Explicit board owner paths; native writes need the existing host authority."],
     "usage": ["python -m eutonos.workflow_cli expert boards profile --root ROOT",
               "python -m eutonos.boards_cli work form --item-id ID --root ROOT",
               "python -m eutonos.boards_cli work submit --request REQUEST.json --root ROOT --host HOST --actor ACTOR"],
     "steps": ["Read the selected board and work form.", "Supply intent with the observed item revision; use a saved order revision to reorder.",
               "Use update, complete, cancel, supersede, archive or restore. Closeout asks for results, checks and an effort estimate.",
               "Submit through the existing host; verify the completion receipt. Current, Next, Done and changelog filing derive automatically."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#structured-boards", "docs/WORK_LIFECYCLE.md"]},
    {"id": "delegate", "label": "Assign and collect", "summary": "Prepare one complete worker assignment and collect its compact result.",
     "module": "eutonos.assignments_cli", "example": {"route": "assignments", "argv": ["profile"]},
     "task_intent": "Track the exact request, selected worker and result without automatic model dispatch.",
     "input_schema_scope": "native assignments CLI semantic forms",
     "input_schema": _object_schema({"route": {"const": "assignments"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Create, attach and collect write local assignment metadata with revision checks; no model is launched."],
     "prerequisites": ["One complete bounded brief and effective routing preferences; the primary retains dispatch and external-action authority."],
     "usage": ["python -m eutonos.workflow_cli expert assignments profile --root ROOT"],
     "steps": ["Create a complete assignment.", "Dispatch through available host tools and attach the returned worker identity.",
               "Collect one result with evidence and closeout; use the returned revision for updates."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#assign-and-collect"]},
    {"id": "adopt", "label": "Adopt or upgrade", "summary": "Map existing owners and plan selected portable modules without overwriting local work.",
     "module": "eutonos.setup_cli", "example": {"route": "setup", "argv": ["discover"]},
     "task_intent": "Inspect existing setup, select an explicit adoption profile and review a preservation-first upgrade plan.",
     "input_schema_scope": "native-route-description; setup owns profile and effect validation",
     "input_schema": _object_schema({"route": {"const": "setup"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Discovery, profile and planning are read-only; selected apply uses the existing guarded host."],
     "prerequisites": ["An existing Git repository and explicit native owner mapping; apply requires selected write authority."],
     "usage": ["python -m eutonos.workflow_cli expert setup discover --root ROOT",
               "python -m eutonos.workflow_cli expert setup profile --root ROOT",
               "python -m eutonos.workflow_cli expert setup plan-profile --root ROOT --request PROFILE.json"],
     "steps": ["Discover existing candidates.", "Select native owners and modules in the profile.",
               "Review the plan, apply with existing authority and verify only selected modules."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#portable-profiles-and-managed-upgrades"]},
    {"id": "settings", "label": "Settings", "summary": "Read or change supported local settings with revision checks.",
     "module": "eutonos.settings", "example": {"route": "settings", "argv": ["status"]},
     "task_intent": "Inspect or conditionally update repository-local workflow preferences.",
     "input_schema_scope": "native-route-description; the native CLI owns and validates its executable arguments",
     "input_schema": _object_schema({"route": {"const": "settings"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Update operations can change repository-local settings; status and route are read-only."],
     "prerequisites": ["Status can read defaults without a settings file; updates require the returned current revision."],
     "usage": ["python -m eutonos.workflow_cli expert settings status --root ROOT"],
     "steps": ["Read current status and revision.", "Apply only a selected supported update using that revision."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#eutonos-proc-guided-workflow"]},
    {"id": "goals", "label": "Shared goals", "summary": "Read outcome goals or submit versioned native forms in an explicit shared repository.",
     "module": "eutonos.goals_cli", "example": {"route": "goals", "argv": ["read"]},
     "task_intent": "Exercise shared goal forms and guarded writes without changing pickup or shared work order.",
     "input_schema_scope": "native-route-description; native CLI validates executable arguments",
     "input_schema": _object_schema({"route": {"const": "goals"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Submit writes the selected native owner; read and form are read-only."],
     "prerequisites": ["Explicit shared root and configured native goal owner; submit requires host, actor and actual session."],
     "usage": ["python -m eutonos.workflow_cli expert goals read --root ROOT",
               "python -m eutonos.workflow_cli expert goals form --root ROOT --owner-path OWNER.md"],
     "steps": ["Read shared goals.", "Fill a no-write form with outcome, criteria, scope and evidence.", "Submit its frozen request and inspect native readback."],
     "sources": ["docs/campaigns/GOAL_ALIGNMENT_PERSONAL_PRIORITIES.md#frozen-minimal-contracts"]},
    {"id": "personal", "label": "Personal records", "summary": "Explicitly select private goals, projects, priorities, local history and backup.",
     "module": "eutonos.personal_cli", "example": {"route": "personal", "argv": ["read", "--owner-id", "OWNER"]},
     "task_intent": "Exercise isolated personal records through owner-bound forms and original-operation recovery.",
     "input_schema_scope": "native-route-description; native CLI validates executable arguments",
     "input_schema": _object_schema({"route": {"const": "personal"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Selected bootstrap, host enrollment, native submit, history, export and restore write private state; form/read/prepare/verify are read-only."],
     "prerequisites": ["Explicit private root and owner; writes require actor and actual session; native writes/export require the dedicated external private host."],
     "usage": ["python -m eutonos.workflow_cli expert personal bootstrap form --root NEW_ROOT --owner-id OWNER",
               "python -m eutonos.workflow_cli expert personal form --kind project --root ROOT --owner-id OWNER"],
     "steps": ["Freeze a create-only bootstrap form and establish private local history and host.", "Prepare and submit the selected versioned form.", "Inspect native and history results separately; recover the original operation.", "Select a private export destination and restore into a new empty root with fresh host admission."],
     "sources": ["docs/campaigns/GOAL_ALIGNMENT_PERSONAL_PRIORITIES.md#frozen-minimal-contracts"]},
    {"id": "device", "label": "Device repositories", "summary": "Read or explicitly enroll, relocate and withdraw local repository selections.",
     "module": "eutonos.device_repositories_cli", "example": {"route": "device", "argv": ["read"]},
     "task_intent": "Maintain explicit device enrollment independently of shared boards and dashboard roots.",
     "input_schema_scope": "native-route-description; native CLI validates executable arguments",
     "input_schema": _object_schema({"route": {"const": "device"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["Enrollment writes only the selected device-local file; read/form never enroll a root."],
     "prerequisites": ["Explicit selected locators with verifiable native identity; personal selection requires the matching owner."],
     "usage": ["python -m eutonos.workflow_cli expert device read --root ROOT",
               "python -m eutonos.workflow_cli expert device form enroll --request DRAFT.json --root ROOT"],
     "steps": ["Read the selected device file and observed revision.", "Prepare an enrollment/relocation/withdrawal form.", "Apply its exact request with the expected revision and inspect selections."],
     "sources": ["docs/campaigns/GOAL_ALIGNMENT_PERSONAL_PRIORITIES.md#frozen-minimal-contracts"]},
    {"id": "goal-context", "label": "Selected goal context", "summary": "Select one shared goal or explicitly selected private goal/project; read bounded context and make a bounded revision-bound edit.",
     "module": "eutonos.goal_context", "example": {"route": "goal-context", "argv": ["select"]},
     "task_intent": "Give an agent the outcome, criteria, scope, exclusions, links, pins and unknowns of one selected goal without full RAM or device scans.",
     "input_schema_scope": "native-route-description; native CLI validates executable arguments",
     "input_schema": _object_schema({"route": {"const": "goal-context"}, "argv": {"type": "array", "items": {"type": "string"}}}, ("route", "argv")),
     "side_effects": ["select, context and edit form are read-only; edit submit reuses the existing goals or personal guarded write."],
     "prerequisites": ["Shared scope needs an explicit shared root; personal scope needs --scope personal, the private root and owner; edit submit needs host, actor and actual session."],
     "usage": ["python -m eutonos.workflow_cli expert goal-context select --root ROOT",
               "python -m eutonos.workflow_cli expert goal-context context --item-id ID --root ROOT [--detail full]",
               "python -m eutonos.workflow_cli expert goal-context context --scope personal --owner-id OWNER --item-id ID --root PERSONAL_ROOT",
               "python -m eutonos.workflow_cli pickup --root ROOT --goal ID [--personal-root PERSONAL_ROOT --owner-id OWNER]"],
     "steps": ["List candidates and choose an ID.", "Read brief context, then full detail if needed.", "Optionally freeze a bounded edit form and submit it through the existing write.", "Apply advice labels (do now, defer, minimal integration preparation, deliberate goal revision) as guidance only."],
     "sources": ["docs/campaigns/GOAL_ALIGNMENT_PERSONAL_PRIORITIES.md#goal-scope-and-personal-state-contract"]},
    {"id": "recover", "label": "Recover work", "summary": "Inspect one existing host attempt before reconciliation.",
     "module": "eutonos.host_cli", "example": {"route": "host", "argv": ["status", "--attempt", "attempt-id"]},
     "task_intent": "Read exact host attempt state before choosing supported recovery.",
     "input_schema_scope": "native-route-description; the native CLI owns and validates its executable arguments",
     "input_schema": _object_schema({"attempt": {"type": "string"}}, ("attempt",)),
     "side_effects": [], "prerequisites": ["The existing host path and attempt identifier."],
     "usage": ["python -m eutonos.workflow_cli expert host status --attempt ATTEMPT --root ROOT --host HOST --actor ACTOR"],
     "steps": ["Read the selected attempt.", "Reconcile only through a separately authorized native operation."],
     "sources": ["docs/OPERATIONS.md"]},
    {"id": "repository-state", "label": "Repository state", "summary": "Compare cached Main with latest observed native work across branches and checkouts.",
     "module": "eutonos.repository_state_cli", "example": {"root": "ROOT", "summary": True},
     "task_intent": "Orient to evidence-backed completed and unintegrated work before repository-wide claims.",
     "input_schema_scope": "native-route-description",
     "input_schema": _object_schema({"root": {"type": "string"}, "summary": {"type": "boolean"}}),
     "side_effects": [], "prerequisites": ["Git repository and readable native owners; incomplete coverage remains unknown."],
     "usage": ["python -m eutonos.workflow_cli expert state --root ROOT --summary"],
     "steps": ["Read Main and latest work with source pins.", "Inspect conflicts and coverage before selecting canonical source."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#repository-wide-state-from-any-checkout"]},
    {"id": "orient", "label": "Orient to work", "summary": "Compose bounded cold-entry or source-pinned resume context with explicit gaps.",
     "module": "eutonos.orientation", "example": {"profile": "eutonos.orientation-request.v1", "mode": "cold", "budget_bytes": 32768},
     "task_intent": "Read native work/source pins and canonical reference rules without claiming identity or authority.",
     "input_schema_scope": "orientation request v1", "input_schema": ORIENTATION_REQUEST_SCHEMA,
     "output_schema": ORIENTATION_PACKET_SCHEMA, "side_effects": ORIENTATION_EFFECTS,
     "prerequisites": ["Permitted ordinary project root; unknown caller is supported. Source importability does not prove installed availability.",
                       "Readable native instructions/onboarding remain the fallback; packet/receipt is not compulsory or proof of reading."],
     "usage": ["eutonos-workflow orient --root ROOT --request REQUEST.json",
               "python tools/workflow.py orient --root ROOT --request REQUEST.json",
               "python tools/eutonos_project.py workflow orient --root ROOT --request REQUEST.json"],
     "steps": ["Inspect coverage, conflicts, gaps and unresolved required selections.",
               "Repeat the same request with returned continuation as cursor; resume with receipt rechecks changed pins.",
               "Use native owners and supported write commands' own context validation for subsequent work."],
     "sources": ["NAV/WORKFLOWS.md", "docs/DEPLOYMENT_STARTUP.md", "docs/CAMPAIGN_CONTEXT.md"]},
    {"id": "pickup", "label": "Pick up work", "summary": "Read the selected task and direct command routes from bounded local owners.",
     "module": "eutonos.workflow", "example": {}, "task_intent": "Read the selected native task and registered references.",
     "input_schema_scope": "direct read-only workflow input", "input_schema": _object_schema({"root": {"type": "string"}}), "side_effects": [],
     "prerequisites": ["A configured project root and current task owner."],
     "usage": ["python -m eutonos.workflow_cli pickup --root ROOT"],
     "steps": ["Read the selected task owner.", "Open only the references needed for the bounded task."],
     "sources": ["NAV/WORKFLOWS.md", "docs/DEPLOYMENT_STARTUP.md#eutonos-proc-guided-workflow"]},
    {"id": "diagnose", "label": "Diagnose workflow", "summary": "Explain repeated workflow failures from supplied outcome metadata.",
     "module": "eutonos.workflow_diagnostics", "example": {"results": [{"outcome": "error", "code": "stale"}]},
     "task_intent": "Classify supplied or locally recorded workflow outcomes and suggest a supported next action.",
     "input_schema_scope": "direct read-only workflow input",
     "input_schema": _object_schema({"results": {"type": "array", "items": {"type": "object"}}}, ("results",)),
     "side_effects": [], "prerequisites": ["Outcome metadata supplied as JSON, or local diagnostic events."],
     "usage": ["python -m eutonos.workflow_cli diagnose --root ROOT", "python -m eutonos.workflow_cli diagnose --root ROOT --input RESULTS.json"],
     "steps": ["Read bounded outcomes.", "Inspect the reported phase and supported next action before retrying effects."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#eutonos-proc-guided-workflow"]},
    {"id": "other", "label": "Other / all commands", "summary": "Search this catalog or call an allowlisted native CLI directly.",
     "module": "eutonos.workflow_cli", "example": {"query": "memory", "expert_routes": sorted(EXPERT_ROUTES)},
     "task_intent": "Discover commands or explicitly forward arguments to an existing allowlisted EUTONOS CLI.",
     "input_schema_scope": "native-route-description; the selected native CLI owns and validates its executable arguments",
     "input_schema": _object_schema({"query": {"type": "string"}, "route": {"enum": sorted(EXPERT_ROUTES)},
                                     "argv": {"type": "array", "items": {"type": "string"}}}),
     "side_effects": ["Effects depend on the selected native route and remain subject to that operation's safeguards."],
     "prerequisites": ["The selected route must be supported; availability does not establish configuration or authority."],
     "usage": ["python -m eutonos.workflow_cli catalog QUERY", "python -m eutonos.workflow_cli expert ROUTE --help --root ROOT"],
     "steps": ["Search the compact catalog.", "Describe a candidate command.", "Use expert routing only for an existing native operation."],
     "sources": ["docs/DEPLOYMENT_STARTUP.md#eutonos-proc-guided-workflow"]},
)

GUIDED_COMMANDS = tuple(item["id"] for item in COMMANDS if item.get("guided", {}).get("prepare") and item.get("guided", {}).get("run"))

_GLOSSARY = {
    "ram": {"term": "RAM", "meaning": "Source-qualified experience memory. It does not own current work, approval, or completion.", "sources": ["modules/ram/CONTRACT.md"]},
    "nav": {"term": "NAV", "meaning": "A bounded route from human intent to first source. It does not prove complete impact or correctness.", "sources": ["modules/nav/CONTRACT.md"]},
    "tokenslang": {"term": "TokenSlang", "meaning": "The canonical EUTONOS structured semantic profile family; the general language and VM remain unimplemented.", "sources": ["modules/tokenslang/CONTRACT.md"]},
    "receipt": {"term": "receipt", "meaning": "A read-back record of an observed operation or capture stage; it does not itself prove semantic correctness or acceptance.", "sources": ["docs/OPERATIONS.md"]},
    "source pin": {"term": "source pin", "meaning": "An exact source revision or digest that qualifies which bytes an observation or operation used.", "sources": ["docs/WORK_MODEL.md"]},
    "work": {"term": "work", "meaning": "The canonical selected work record that owns its goal, state, requirements, and evidence.", "sources": ["docs/WORK_MODEL.md"]},
    "campaign": {"term": "campaign", "meaning": "A group of dependent outcomes across sessions, with its own scope, slices and acceptance contract. Selection belongs to pickup and the shared Current Work view.", "sources": ["docs/WORK_MODEL.md"]},
    "slice": {"term": "slice", "meaning": "One coherent design or implementation outcome with its own scope, inputs, output and checks. A slice may stand alone or belong to a campaign.", "sources": ["docs/WORK_MODEL.md"]},
    "feature": {"term": "feature", "meaning": "A named part of EUTONOS you can point at: a command, a page, an installable component. A noun: what is in the box. Current and future features live in their catalogue boards.", "sources": ["docs/WORK_MODEL.md"]},
    "capability": {"term": "capability", "meaning": "Something a person can now get done, usually using one or more features. A verb phrase: the benefit. Each capability lists the feature IDs it relies on in extensions.features.", "sources": ["docs/WORK_MODEL.md"]},
    "canonical owner paths": {"term": "canonical owner paths", "meaning": "Repository paths designated to own particular facts; derived views and summaries do not replace them.", "sources": ["docs/REGISTRY.json", "docs/WORK_MODEL.md"]},
}


def _available(module):
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def fingerprint():
    value = {"profile": PROFILE, "commands": COMMANDS, "closeout_fields": CLOSEOUT_FIELDS,
             "catalog_fields": CATALOG_FIELDS, "work_fields": WORK_FIELDS,
             "legacy_fields": LEGACY_FIELDS, "expert_routes": EXPERT_ROUTES, "glossary": _GLOSSARY,
             "support": {item["id"]: _available(item["module"]) for item in COMMANDS}}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact(definition):
    item = {key: deepcopy(definition[key]) for key in ("id", "label", "summary", "example")}
    item["supported"] = _available(definition["module"])
    if definition["id"] == "closeout":
        item["fields"] = deepcopy((*CLOSEOUT_FIELDS, *CATALOG_FIELDS, *WORK_FIELDS))
        item["legacy_fields"] = deepcopy(LEGACY_FIELDS)
    return item


def catalog(query=None):
    needle = (query or "").strip().casefold()
    available = [_compact(definition) for definition in COMMANDS]
    commands = [item for item in available if not needle or needle in " ".join(str(value) for value in item.values()).casefold()]
    return {"profile": PROFILE, "outcome": "catalog", "query": query,
            "capability_fingerprint": fingerprint(), "commands": commands}


def describe(command_id):
    command = next((item for item in COMMANDS if item["id"] == command_id), None)
    if command is None:
        return {"profile": PROFILE, "outcome": "error", "code": "unknown-command", "command_id": command_id,
                "next": "Use help or catalog to list supported command IDs."}
    value = deepcopy(command)
    value.pop("module", None)
    value.update(supported=_available(command["module"]), configured=None, authorized=None)
    if command["id"] == "closeout":
        value["fields"] = deepcopy((*CLOSEOUT_FIELDS, *CATALOG_FIELDS, *WORK_FIELDS))
        value["legacy_fields"] = deepcopy(LEGACY_FIELDS)
    return {"profile": PROFILE, "outcome": "description", "capability_fingerprint": fingerprint(), "command": value,
            "status_meaning": "supported means the implementation is importable; configured and authorized require project and operation-specific evidence."}


def help_topics(query=None):
    needle = (query or "").strip().casefold()
    topics = ([{"kind": "command", "id": item["id"], "label": item["label"], "summary": item["summary"]} for item in COMMANDS] +
              [{"kind": "term", "id": key, "label": value["term"], "summary": value["meaning"]} for key, value in _GLOSSARY.items()])
    selected = [item for item in topics if not needle or needle in " ".join(str(value) for value in item.values()).casefold()]
    return {"profile": PROFILE, "outcome": "help", "query": query, "capability_fingerprint": fingerprint(),
            "usage": {"describe": "describe COMMAND_ID", "guide": "guide COMMAND_ID",
                      "explain": "explain TERM", "direct": "expert ROUTE --help --root ROOT"},
            "topics": deepcopy(selected)}


def explain(term):
    key = " ".join(str(term).strip().casefold().replace("-", " ").split())
    value = _GLOSSARY.get(key)
    if value is None:
        return {"profile": PROFILE, "outcome": "error", "code": "unknown-term", "term": term,
                "next": "Use help to list glossary terms or catalog to list commands."}
    return {"profile": PROFILE, "outcome": "explanation", "term": deepcopy(value), "capability_fingerprint": fingerprint()}


def capabilities():
    values = [{"id": item["id"], "supported": _available(item["module"]), "configured": None, "authorized": None}
              for item in COMMANDS]
    return {"profile": PROFILE, "outcome": "capabilities", "capability_fingerprint": fingerprint(),
            "meaning": {"supported": "Implementation is importable in this Python environment.",
                        "configured": "Project prerequisites are satisfied; null means this registry did not inspect a project.",
                        "authorized": "The concrete operation is authorized; null means authority was not supplied or inferred."},
            "capabilities": values}


__all__ = ["COMMANDS", "CLOSEOUT_FIELDS", "EXPERT_ROUTES", "GUIDED_COMMANDS", "LEGACY_FIELDS", "PROFILE", "WORK_FIELDS",
           "capabilities", "catalog", "describe", "explain", "fingerprint", "help_topics"]
