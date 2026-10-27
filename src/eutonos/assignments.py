"""Deterministic local delegation assignments; no worker dispatch or model calls."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from .hidden_process import no_window
import tempfile

from .private_scope import require_shared
from . import companion_control, settings, settings_cli, model_catalog, tokenslang as ts
from .local_lock import local_lock
from .source import safe_path


PROFILE = "eutonos.local-assignment.v1"
CREATE = "eutonos.assignment.create.v1"
ATTACH = "eutonos.assignment.attach.v1"
COLLECT = "eutonos.assignment.collect.v1"
RESULTS = ("completed", "needs-attention")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value):
    return hashlib.sha256(ts.encoded(value)).hexdigest()


def _git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                          env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", **no_window()).stdout.strip()


def _origin(root):
    resolved = Path(root).resolve()
    top = Path(_git(resolved, "rev-parse", "--show-toplevel")).resolve()
    ts.require(top == resolved, "wrong-scope", "Assignment root must be the owning Git project root.")
    return {"root": str(resolved), "root_id": hashlib.sha256(os.path.normcase(str(resolved)).encode()).hexdigest(),
            "head": _git(resolved, "rev-parse", "HEAD"),
            "basis": "local assignment store root; task target paths do not establish project attribution"}


def _references(value):
    result = []
    for source in ts.sequence(value):
        ref = deepcopy(ts.reference(source))
        if "path" in ref:
            ref["path"] = safe_path(ref["path"])
        result.append(ref)
    return result


def _strings(value, *, allow_empty=True):
    items = ts.sequence(value)
    ts.require(allow_empty or bool(items), "invalid", "At least one value is required.")
    return [ts.string(item) for item in items]


def _missing(value, required, profile, operation):
    if not isinstance(value, dict):
        value = {}
    missing = sorted(required - set(value))
    if missing:
        return {"profile": profile, "outcome": "incomplete", "operation": operation,
                "missing_fields": missing, "next": "Supply every reported semantic field and retry."}
    return None


def _validate_create(value, root=None, *, check_launch=True):
    required = {"profile", "idempotency_key", "request_id", "primary_thread_id", "primary_model",
                "worker_model", "worker_effort", "category", "logical_scope", "brief", "source_refs"}
    optional = {"primary_turn_id", "user_selected", "approval"}
    incomplete = _missing(value, required, CREATE, "create")
    if incomplete:
        return incomplete
    ts.shape(value, required, optional)
    ts.require(value["profile"] == CREATE, "unsupported", "Unknown assignment create profile.")
    for key in ("idempotency_key", "request_id", "primary_thread_id"):
        ts.string(value[key])
    logical_scope = ts.string(value["logical_scope"]).strip()
    ts.require(bool(logical_scope), "invalid", "Logical scope cannot be blank.")
    # Validate identity syntax here; new-launch capability checks belong after
    # the idempotency lookup. A replay of an existing job never selects again.
    ts.require(all(model_catalog.is_exact_model_id(value[key]) or value[key] in model_catalog.FAMILIES
                   for key in ("primary_model", "worker_model")),
               "invalid", "Assignment models must use supported model IDs.")
    ts.require(value["worker_effort"] in settings.EFFORTS,
               "invalid", "Worker effort is malformed.")
    if check_launch:
        _validate_launch(value, root)
    ts.require(value["category"] in (*settings.TASKS, "other"), "invalid", "Assignment category is unsupported.")
    if "primary_turn_id" in value:
        ts.string(value["primary_turn_id"])
    ts.require(type(value.get("user_selected", False)) is bool
               and value.get("approval", "pending") in {"pending", "approved", "denied"},
               "invalid", "Assignment routing selection or approval is invalid.")
    ts.shape(value["brief"], {"goal", "scope", "checks", "closeout"})
    brief = {"goal": ts.string(value["brief"]["goal"]), "scope": ts.string(value["brief"]["scope"]),
             "checks": _strings(value["brief"]["checks"], allow_empty=False),
             "closeout": ts.string(value["brief"]["closeout"])}
    return {"profile": CREATE, **{key: value[key] for key in required - {"profile", "brief", "source_refs",
                                                                       "logical_scope"}},
            "logical_scope": logical_scope,
            "brief": brief, "source_refs": _references(value["source_refs"]),
            "primary_turn_id": value.get("primary_turn_id"), "user_selected": value.get("user_selected", False),
            "approval": value.get("approval", "pending")}


def _validate_launch(value, root):
    catalog = model_catalog.describe_catalog(root)["models"]
    ts.require(value["worker_model"] in catalog,
               "invalid", "Requested worker model is unavailable.")
    entry = catalog[value["worker_model"]]
    ts.require(value["worker_effort"] in entry["supported_efforts"],
               "invalid", "Worker effort is unsupported by the requested model.")
    ts.require(value["worker_effort"] in entry["efforts"],
               "invalid", "Worker effort is below the current model-selection policy.")


def _routing(root, value):
    choice = value["worker_model"]
    try:
        entry = model_catalog.describe_catalog(root)["models"][choice]
        selection = model_catalog.resolve_choice(choice, entry["harness"], root=root,
                                                  effort=value["worker_effort"])
    except (KeyError, ValueError) as exc:
        return {"outcome": "invalid-settings", "basis": "requested-model-unavailable",
                "message": str(exc), "record_created": False}
    result = _routing_exact(root, {**value, "worker_model": selection["resolved"]})
    return {**result, "model_requested": choice, "model_selection": selection}


def _routing_exact(root, value):
    state = settings.status(root)
    if not state["routing"]["enabled"]:
        return {"outcome": "continue-primary", "basis": "companion-off", "record_created": False}
    model, effort = value["worker_model"], value["worker_effort"]
    if not companion_control.eligible_choice(root, state["routing"]["allowed_models"], model):
        return {"outcome": "invalid-settings", "basis": "requested-model-not-allowed", "record_created": False}
    if value["user_selected"]:
        result = companion_control.routing_decision(value["primary_model"], model, user_selected=True, root=root,
                                                    approval=value["approval"])
        return {**result, "effort": effort, "record_created": result["outcome"] == "selected"}
    if value["category"] == "other":
        configured_model, configured_effort = state["routing"]["model"], state["routing"]["effort"]
        if state["routing"]["routing"] == "fixed" and not companion_control.eligible_choice(root, [configured_model], model):
            return {"outcome": "invalid-settings", "basis": "request-differs-from-global-fixed-route",
                    "record_created": False}
        if configured_effort is not None and configured_effort != effort:
            return {"outcome": "invalid-settings", "basis": "requested-effort-differs",
                    "record_created": False}
        result = companion_control.routing_decision(value["primary_model"], model, approval=value["approval"], root=root)
        return {**result, "task": "other", "effort": effort,
                "record_created": result["outcome"] == "selected"}
    decision = settings_cli.route(root, value["category"], value["primary_model"], value["approval"])
    if decision["outcome"] == "adaptive":
        if model not in decision["eligible_models"]:
            return {"outcome": "invalid-settings", "basis": "requested-model-not-eligible", "record_created": False}
        configured_effort = decision.get("effort")
        if configured_effort is not None and configured_effort != effort:
            return {"outcome": "invalid-settings", "basis": "requested-effort-differs", "record_created": False}
        result = companion_control.routing_decision(value["primary_model"], model, approval=value["approval"], root=root)
        return {**result, "effort": effort, "record_created": result["outcome"] == "selected"}
    if decision["outcome"] != "selected":
        return {**decision, "record_created": False}
    if decision["model"] != model or decision.get("effort") not in {None, effort}:
        return {"outcome": "invalid-settings", "basis": "request-differs-from-effective-route",
                "record_created": False, "effective": decision}
    return {**decision, "effort": effort, "record_created": True}


def _directory(root):
    require_shared(root)
    return Path(root).resolve() / ".eutonos-local" / "assignments"


def _path(root, assignment_id):
    ts.require(isinstance(assignment_id, str) and assignment_id.startswith("assignment-")
               and len(assignment_id) == 35 and all(char in "0123456789abcdef" for char in assignment_id[11:]),
               "invalid", "Assignment ID is invalid.")
    return _directory(root) / (assignment_id + ".json")


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _packet(record):
    attachment = record.get("attachment")
    routes = [{"path": ".eutonos-local/assignments/" + record["assignment_id"] + ".json"},
              *deepcopy(record["source_refs"])]
    if record.get("usage_reference") is not None:
        routes.append({"usage_reference": deepcopy(record["usage_reference"])})
    packet = {"profile": "eutonos.assignment.packet.v1", "assignment_id": record["assignment_id"],
            "request_id": record["request_id"], "revision": record["revision"], "state": record["state"],
            "primary": deepcopy(record["primary"]), "requested_worker": deepcopy(record["requested_worker"]),
            "worker": deepcopy(attachment), "task": deepcopy(record["task"]), "brief": deepcopy(record["brief"]),
            "evidence_routes": routes,
            "limits": ["No dispatch, model call, scheduler, host lease, correctness claim, or inferred usage attribution"]}
    if "model_selection" in record:
        packet["model_selection"] = deepcopy(record["model_selection"])
    return packet


def _set_packet_hash(record):
    record["packet_sha256"] = _digest(_packet(record))


def _validate_usage(value):
    if value is None:
        return None
    ts.shape(value, {"record_id", "turn_id"})
    return {"record_id": ts.string(value["record_id"]), "turn_id": ts.string(value["turn_id"])}


def _validate_record(value):
    ts.shape(value, {"profile", "assignment_id", "revision", "state", "created_at", "updated_at",
                     "idempotency_key", "create_sha256", "request_id", "primary", "requested_worker",
                     "task", "brief", "source_refs", "project_origin", "routing", "attachment", "result",
                     "usage_reference", "packet_sha256"}, {"model_selection"})
    ts.require(value["profile"] == PROFILE and type(value["revision"]) is int and value["revision"] in {1, 2, 3}
               and value["state"] in {"prepared", "dispatched", *RESULTS}, "invalid", "Assignment record is invalid.")
    _path(".", value["assignment_id"])
    for key in ("created_at", "updated_at", "idempotency_key", "request_id"):
        ts.string(value[key])
    for key in ("create_sha256", "packet_sha256"):
        ts.require(isinstance(value[key], str) and len(value[key]) == 64
                   and all(char in "0123456789abcdef" for char in value[key]), "invalid", "Assignment hash is invalid.")
    ts.shape(value["primary"], {"thread_id", "turn_id", "model"})
    ts.string(value["primary"]["thread_id"])
    ts.require(value["primary"]["turn_id"] is None or isinstance(value["primary"]["turn_id"], str)
               and value["primary"]["turn_id"], "invalid", "Primary turn ID is invalid.")
    ts.require(model_catalog.is_exact_model_id(value["primary"]["model"])
               or value["primary"]["model"] in ("luna", "sol", "astra", "sonnet", "opus"),
               "invalid", "Primary model is invalid.")
    ts.shape(value["requested_worker"], {"model", "effort", "identity_basis"})
    ts.require((model_catalog.is_exact_model_id(value["requested_worker"]["model"])
                or value["requested_worker"]["model"] in ("luna", "sol", "astra", "sonnet", "opus"))
               and value["requested_worker"]["effort"] in settings.EFFORTS,
               "invalid", "Requested worker is invalid.")
    ts.string(value["requested_worker"]["identity_basis"])
    if "model_selection" in value:
        selection = value["model_selection"]
        ts.shape(selection, {"requested", "resolved", "selection_kind", "family", "supported_efforts", "basis"},
                 {"allowed_efforts", "default_effort"})
        ts.require(selection["requested"] == value["requested_worker"]["model"]
                   and model_catalog.is_exact_model_id(selection["resolved"]),
                   "invalid", "Assignment model selection identity is invalid.")
        family_choice = selection["requested"] in model_catalog.FAMILIES
        ts.require(selection["selection_kind"] == ("family" if family_choice else "pin")
                   and (selection["family"] == selection["requested"]
                        and companion_control._family(selection["resolved"]) == selection["requested"]
                        if family_choice else selection["resolved"] == selection["requested"]
                        and selection["family"] in (*model_catalog.FAMILIES, None)),
                   "invalid", "Assignment model selection intent is invalid.")
        efforts = ts.sequence(selection["supported_efforts"])
        ts.require(bool(efforts) and len(efforts) <= len(settings.EFFORTS)
                   and all(effort in settings.EFFORTS for effort in efforts)
                   and len(set(efforts)) == len(efforts)
                   and value["requested_worker"]["effort"] in efforts,
                   "invalid", "Assignment model selection efforts are invalid.")
        allowed = selection.get("allowed_efforts")
        if allowed is not None:
            allowed = ts.sequence(allowed)
            expected_allowed = (tuple(effort for effort in model_catalog.LUNA_ALLOWED_EFFORTS
                                      if effort in efforts)
                                if selection["family"] == "luna" else tuple(efforts))
            ts.require(bool(allowed) and len(allowed) <= len(settings.EFFORTS)
                       and all(effort in efforts for effort in allowed)
                       and len(set(allowed)) == len(allowed)
                       and tuple(allowed) == expected_allowed
                       and value["requested_worker"]["effort"] in allowed,
                       "invalid", "Assignment model selection allowed efforts are invalid.")
        if "default_effort" in selection:
            default_effort = selection["default_effort"]
            expected_default = ("xhigh" if allowed is not None and "xhigh" in allowed
                                else "max" if allowed is not None and "max" in allowed else None)
            ts.require(selection["family"] == "luna" and default_effort == expected_default
                       and default_effort == value["requested_worker"]["effort"],
                       "invalid", "Assignment model selection default effort is invalid.")
        ts.string(selection["basis"])
    ts.shape(value["task"], {"category", "logical_scope"})
    ts.require(value["task"]["category"] in (*settings.TASKS, "other"), "invalid", "Task category is invalid.")
    ts.string(value["task"]["logical_scope"])
    ts.shape(value["brief"], {"goal", "scope", "checks", "closeout"})
    for key in ("goal", "scope", "closeout"):
        ts.string(value["brief"][key])
    _strings(value["brief"]["checks"], allow_empty=False); _references(value["source_refs"])
    ts.shape(value["project_origin"], {"root", "root_id", "head", "basis"})
    ts.string(value["project_origin"]["root"]); ts.string(value["project_origin"]["basis"])
    for key in ("root_id", "head"):
        ts.require(isinstance(value["project_origin"][key], str) and value["project_origin"][key]
                   and all(char in "0123456789abcdef" for char in value["project_origin"][key]),
                   "invalid", "Project provenance identity is invalid.")
    ts.require(isinstance(value["routing"], dict), "invalid", "Assignment routing evidence is invalid.")
    if value["attachment"] is not None:
        ts.shape(value["attachment"], {"thread_id", "turn_id", "identity_basis"})
        ts.string(value["attachment"]["thread_id"]); ts.string(value["attachment"]["identity_basis"])
        ts.require(value["attachment"]["turn_id"] is None or isinstance(value["attachment"]["turn_id"], str)
                   and value["attachment"]["turn_id"], "invalid", "Worker turn ID is invalid.")
    if value["result"] is not None:
        ts.require(value["result"] == _normalize_result(value["result"], stored=True),
                   "invalid", "Stored assignment result is not canonical.")
    _validate_usage(value["usage_reference"])
    ts.require((value["state"] == "prepared" and value["revision"] == 1
                and value["attachment"] is None and value["result"] is None)
               or (value["state"] == "dispatched" and value["revision"] == 2
                   and value["attachment"] is not None and value["result"] is None)
               or (value["state"] in RESULTS and value["revision"] == 3
                   and value["attachment"] is not None and value["result"] is not None),
               "invalid", "Assignment lifecycle fields disagree.")
    ts.require(value["packet_sha256"] == _digest(_packet(value)), "stale", "Assignment packet fingerprint differs.")
    return value


def _read(path, root=None):
    try:
        value = _validate_record(ts.loads(path.read_text(encoding="utf-8")))
        if root is not None:
            ts.require(Path(value["project_origin"]["root"]).resolve() == Path(root).resolve(), "wrong-scope",
                       "Assignment record belongs to another project root.")
        return value
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Assignment record must be UTF-8.") from exc


def create(root, request):
    value = _validate_create(request, root=root, check_launch=False)
    if value.get("outcome") == "incomplete":
        return value
    origin = _origin(root)
    create_sha = _digest(value)
    assignment_id = "assignment-" + hashlib.sha256(
        ts.encoded({"root_id": origin["root_id"], "idempotency_key": value["idempotency_key"]})).hexdigest()[:24]
    path = _path(root, assignment_id)
    with local_lock(root, create=False):
        if path.exists():
            current = _read(path, root)
            if current["create_sha256"] != create_sha:
                return {"profile": CREATE, "outcome": "conflict", "code": "idempotency-conflict",
                        "assignment_id": assignment_id,
                        "message": "This idempotency key already binds another create payload."}
            return {"profile": CREATE, "outcome": "idempotent", "assignment": deepcopy(current)}
    _validate_launch(value, root)
    routing = _routing(root, value)
    if not routing.get("record_created"):
        return {"profile": CREATE, **routing,
                "message": "Routing remains advisory; no assignment record or dispatch was created."}
    with local_lock(root):
        if path.exists():
            current = _read(path, root)
            if current["create_sha256"] != create_sha:
                return {"profile": CREATE, "outcome": "conflict", "code": "idempotency-conflict",
                        "assignment_id": assignment_id,
                        "message": "This idempotency key already binds another create payload."}
            return {"profile": CREATE, "outcome": "idempotent", "assignment": deepcopy(current)}
        stamp = _now()
        record = {"profile": PROFILE, "assignment_id": assignment_id, "revision": 1, "state": "prepared",
                  "created_at": stamp, "updated_at": stamp, "idempotency_key": value["idempotency_key"],
                  "create_sha256": create_sha, "request_id": value["request_id"],
                  "primary": {"thread_id": value["primary_thread_id"], "turn_id": value["primary_turn_id"],
                              "model": value["primary_model"]},
                  "requested_worker": {"model": value["worker_model"], "effort": value["worker_effort"],
                                       "identity_basis": "requested; not observed"},
                  "task": {"category": value["category"], "logical_scope": value["logical_scope"]},
                  "brief": value["brief"], "source_refs": value["source_refs"], "project_origin": origin,
                  "routing": routing, "attachment": None, "result": None, "usage_reference": None,
                  "model_selection": {**routing["model_selection"],
                                      "supported_efforts": list(routing["model_selection"]["supported_efforts"])},
                  "packet_sha256": ""}
        _set_packet_hash(record); _atomic(path, record)
    return {"profile": CREATE, "outcome": "created", "assignment": deepcopy(record)}


def _validate_attach(value):
    required = {"profile", "assignment_id", "expected_revision", "worker_thread_id"}
    incomplete = _missing(value, required, ATTACH, "attach")
    if incomplete:
        return incomplete
    ts.shape(value, required, {"worker_turn_id", "usage_reference"})
    ts.require(value["profile"] == ATTACH and type(value["expected_revision"]) is int
               and value["expected_revision"] >= 1, "invalid", "Invalid assignment attachment.")
    _path(".", value["assignment_id"]); ts.string(value["worker_thread_id"])
    if "worker_turn_id" in value:
        ts.string(value["worker_turn_id"])
    return {"assignment_id": value["assignment_id"], "expected_revision": value["expected_revision"],
            "attachment": {"thread_id": value["worker_thread_id"], "turn_id": value.get("worker_turn_id"),
                           "identity_basis": "caller-supplied; not independently verified"},
            "usage_reference": _validate_usage(value.get("usage_reference"))}


def attach(root, request):
    value = _validate_attach(request)
    if value.get("outcome") == "incomplete":
        return value
    path = _path(root, value["assignment_id"])
    with local_lock(root):
        ts.require(path.is_file(), "missing", "Assignment record is absent.")
        record = _read(path, root)
        ts.require(record["primary"]["thread_id"] != value["attachment"]["thread_id"], "conflict",
                   "Primary thread cannot be attached as its own delegated worker.")
        exact = record["attachment"] == value["attachment"] and record["usage_reference"] == value["usage_reference"]
        if record["state"] in {"dispatched", *RESULTS} and exact:
            return {"profile": ATTACH, "outcome": "idempotent", "assignment": deepcopy(record)}
        ts.require(record["revision"] == value["expected_revision"], "stale", "Assignment revision changed.")
        ts.require(record["state"] == "prepared", "conflict", "Only a prepared assignment can attach a worker.")
        record.update(revision=record["revision"] + 1, state="dispatched", updated_at=_now(),
                      attachment=value["attachment"], usage_reference=value["usage_reference"])
        _set_packet_hash(record); _atomic(path, record)
    return {"profile": ATTACH, "outcome": "attached", "assignment": deepcopy(record)}


def _normalize_result(result, stored=False):
    required = {"summary", "findings", "changed_paths", "checks", "unresolved", "closeout", "model"}
    ts.shape(result, required | ({"model_identity_basis"} if stored else set()))
    findings = []
    for finding in ts.sequence(result["findings"]):
        ts.shape(finding, {"summary", "source_refs"})
        findings.append({"summary": ts.string(finding["summary"]),
                         "source_refs": _references(finding["source_refs"])})
    ts.shape(result["closeout"], {"outcome", "evidence_refs"})
    closeout = {"outcome": ts.string(result["closeout"]["outcome"]),
                "evidence_refs": _references(result["closeout"]["evidence_refs"])}
    normalized = {"summary": ts.string(result["summary"]), "findings": findings,
                  "changed_paths": [safe_path(path) for path in _strings(result["changed_paths"])],
                  "checks": _strings(result["checks"]), "unresolved": _strings(result["unresolved"]),
                  "closeout": closeout, "model": ts.string(result["model"]),
                  "model_identity_basis": "worker self-reported; not independently verified"}
    if stored:
        ts.require(result["model_identity_basis"] == normalized["model_identity_basis"],
                   "invalid", "Stored model identity qualification is invalid.")
    return normalized


def _validate_collect(value):
    required = {"profile", "assignment_id", "expected_revision", "outcome", "result"}
    incomplete = _missing(value, required, COLLECT, "collect")
    if incomplete:
        return incomplete
    ts.shape(value, required, {"usage_reference"})
    ts.require(value["profile"] == COLLECT and value["outcome"] in RESULTS
               and type(value["expected_revision"]) is int and value["expected_revision"] >= 1,
               "invalid", "Invalid assignment collection.")
    _path(".", value["assignment_id"])
    normalized = _normalize_result(value["result"])
    return {"assignment_id": value["assignment_id"], "expected_revision": value["expected_revision"],
            "outcome": value["outcome"], "result": normalized,
            "usage_reference": _validate_usage(value.get("usage_reference"))}


def collect(root, request):
    value = _validate_collect(request)
    if value.get("outcome") == "incomplete":
        return value
    path = _path(root, value["assignment_id"])
    with local_lock(root):
        ts.require(path.is_file(), "missing", "Assignment record is absent.")
        record = _read(path, root)
        usage = value["usage_reference"] if value["usage_reference"] is not None else record["usage_reference"]
        exact = record["state"] == value["outcome"] and record["result"] == value["result"] \
            and record["usage_reference"] == usage
        if exact:
            return {"profile": COLLECT, "outcome": "idempotent", "assignment": deepcopy(record)}
        ts.require(record["revision"] == value["expected_revision"], "stale", "Assignment revision changed.")
        ts.require(record["state"] == "dispatched", "conflict", "Only a dispatched assignment can be collected.")
        record.update(revision=record["revision"] + 1, state=value["outcome"], updated_at=_now(),
                      result=value["result"], usage_reference=usage)
        _set_packet_hash(record); _atomic(path, record)
    return {"profile": COLLECT, "outcome": "collected", "assignment": deepcopy(record)}


def get(root, assignment_id):
    path = _path(root, assignment_id)
    with local_lock(root, create=False):
        ts.require(path.is_file(), "missing", "Assignment record is absent.")
        return deepcopy(_read(path, root))


def records(root):
    directory = _directory(root)
    with local_lock(root, create=False):
        return [deepcopy(_read(path, root)) for path in sorted(directory.glob("assignment-*.json"))] if directory.is_dir() else []


def list_records(root, offset=0, limit=20):
    ts.require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
               "invalid", "Assignment page must use nonnegative offset and limit 1..100.")
    all_records = records(root)
    page = all_records[offset:offset + limit]
    return {"profile": PROFILE, "outcome": "listed", "offset": offset, "limit": limit,
            "total": len(all_records), "complete": offset + len(page) >= len(all_records),
            "next_offset": None if offset + len(page) >= len(all_records) else offset + len(page),
            "assignments": [{"assignment_id": item["assignment_id"], "request_id": item["request_id"],
                             "revision": item["revision"], "state": item["state"], "updated_at": item["updated_at"],
                             "primary_model": item["primary"]["model"],
                             "requested_worker_model": item["requested_worker"]["model"],
                             "category": item["task"]["category"]} for item in page]}


def packet(root, assignment_id):
    return _packet(get(root, assignment_id))


def profile_description():
    create_fields = ["idempotency_key", "request_id", "primary_thread_id", "primary_model", "worker_model",
                     "worker_effort", "category", "logical_scope", "brief", "source_refs"]
    collect_fields = ["assignment_id", "expected_revision", "outcome", "result"]
    create_example = {"profile": CREATE, "idempotency_key": "request-42-create", "request_id": "request-42",
                      "primary_thread_id": "primary-thread", "primary_model": "gpt-6-astra",
                      "worker_model": "gpt-6-luna", "worker_effort": "xhigh", "category": "other",
                      "logical_scope": "bounded implementation", "brief": {"goal": "Implement the selected slice.",
                      "scope": "Only the assigned paths.", "checks": ["Run focused tests."],
                      "closeout": "Return changes, evidence, and unresolved risks."}, "source_refs": []}
    collect_example = {"profile": COLLECT, "assignment_id": "assignment-" + "0" * 24,
                       "expected_revision": 2, "outcome": "completed", "result": {
                       "summary": "Selected work completed.", "findings": [], "changed_paths": [],
                       "checks": ["Focused tests passed."], "unresolved": [],
                       "closeout": {"outcome": "reported", "evidence_refs": []}, "model": "gpt-6-luna"}}
    return {"profile": PROFILE, "outcome": "profile", "operations": {
        "create": {"profile": CREATE, "required": create_fields,
                   "optional": ["primary_turn_id", "user_selected", "approval"], "example": create_example},
        "attach": {"profile": ATTACH,
                   "required": ["assignment_id", "expected_revision", "worker_thread_id"],
                   "optional": ["worker_turn_id", "usage_reference"]},
        "collect": {"profile": COLLECT, "required": collect_fields, "optional": ["usage_reference"],
                    "example": collect_example}},
        "lifecycle": ["prepared (not dispatched)", "dispatched", "completed or needs-attention"],
        "limits": ["Local records only", "No automatic dispatch, model call, scheduler, host lease, or usage inference"]}


__all__ = ["ATTACH", "COLLECT", "CREATE", "PROFILE", "attach", "collect", "create", "get",
           "list_records", "packet", "profile_description", "records"]
