"""Structured native Markdown boards with guarded, exact-byte edit plans."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re

from .private_scope import require_shared
from . import tokenslang as ts
from .effects import LocalRoot, digest, plan as effect_plan
from .source import safe_path


CONFIG = "eutonos.tokenslang.boards.v1"
BOARD = "eutonos.tokenslang.board.v1"
PLAN = "eutonos.tokenslang.board-plan.v1"
ADD = "eutonos.tokenslang.board-add.v1"
UPDATE = "eutonos.tokenslang.board-update.v1"
REORDER = "eutonos.tokenslang.board-reorder.v1"
MOVE = "eutonos.tokenslang.board-move.v1"
TYPES = ("campaign", "stage", "slice", "quick", "task", "feature", "capability", "goal", "project", "bug", "attempt")
STATUSES = ("planned", "active", "blocked", "completed", "deferred", "cancelled", "superseded")
CATEGORY_TYPES = ("feature", "capability")
WORK_FORM_TYPES = ("campaign", "stage", "slice", "quick")
CATEGORY_EXAMPLES = ("Workflow", "Knowledge", "Testing", "Delivery", "Dashboard", "Collaboration")
CATALOG_ROLE = re.compile(r"(current|future)-(feature|capability)-catalogue")
BLOCK = re.compile(rb"^```tokenslang-board[ \t]*\r?\n(?P<body>.*?)^```[ \t]*(?=\r?$)", re.M | re.S)
OPEN = re.compile(rb"^```tokenslang-board[ \t]*(?=\r?$)", re.M)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _refs(value):
    ts.require(isinstance(value, list), "invalid", "Board evidence references must be an array.")
    result = []
    for supplied in value:
        ref = deepcopy(ts.reference(supplied))
        if "path" in ref:
            ref["path"] = safe_path(ref["path"])
        result.append(ref)
    return result


def _item(value):
    ts.shape(value, {"id", "type", "title", "summary", "status"},
             {"parent_id", "evidence_refs", "next_action", "extensions"})
    result = {key: ts.string(value[key]) for key in ("id", "title", "summary")}
    ts.require(value["type"] in TYPES and value["status"] in STATUSES, "invalid", "Board item type or status is invalid.")
    result.update(type=value["type"], status=value["status"])
    if "parent_id" in value:
        result["parent_id"] = ts.string(value["parent_id"])
    if "evidence_refs" in value:
        result["evidence_refs"] = _refs(value["evidence_refs"])
    if "next_action" in value:
        result["next_action"] = ts.string(value["next_action"])
    if "extensions" in value:
        ts.require(isinstance(value["extensions"], dict), "invalid", "Board item extensions must be an object.")
        result["extensions"] = deepcopy(value["extensions"])
        if "completion_checks" in result["extensions"]:
            from . import completion_checks
            ts.require(result["type"] not in {"attempt", "goal", "project"},
                       "invalid", "Completion checks belong to work outcomes, not claims or goal/project catalogs.")
            result["extensions"]["completion_checks"] = completion_checks.validate(
                result["extensions"]["completion_checks"])
    if result['type'] == 'attempt':
        from .shared_attempts import validate
        validate(result)
    elif 'shared_attempt' in result.get('extensions', {}):
        raise ts.ReadError('invalid', 'shared_attempt metadata belongs only to native attempt records.')
    if 'creation_token' in result.get('extensions', {}):
        import uuid
        token = result['extensions']['creation_token']
        try:
            valid_token = str(uuid.UUID(token)) == token
        except (ValueError, TypeError, AttributeError):
            valid_token = False
        ts.require(valid_token, 'invalid', 'Creation token must be a frozen full UUID.')
    if result['type'] in {'goal', 'project'}:
        from .goal_records import validate_goal, validate_project
        (validate_goal if result['type'] == 'goal' else validate_project)(result)
    return result


def execution_recommendation(value):
    """Validate advisory campaign planning metadata without binding to today's model catalog."""
    ts.require(isinstance(value, dict), "invalid", "Execution recommendation must be an object.")
    ts.shape(value, {"model", "effort", "basis"}, {"advisory"})
    result = {key: ts.string(value[key]).strip() for key in ("model", "effort", "basis")}
    ts.require(all(result.values()), "invalid", "Execution recommendation model, effort and basis must be nonblank.")
    if "advisory" in value:
        ts.require(value["advisory"] is True, "invalid", "Execution recommendation is advisory and advisory must be true.")
    result["advisory"] = True
    return result


def _validate_catalog_fields(item):
    """Validate only newly written catalog records; old board data remains readable."""
    if item["type"] not in CATEGORY_TYPES:
        return
    extensions = item.get("extensions", {})
    resolve_category(extensions)
    category = extensions.get("category")
    ts.require(isinstance(category, str) and category.strip() and len(category) <= 120
               and all(character.isprintable() or character.isspace() for character in category),
               "invalid", "Feature and capability items require a nonempty human-readable extensions.category.")
    if "features" in extensions:
        features = extensions["features"]
        ts.require(item["type"] == "capability", "invalid",
                   "extensions.features lists the features a capability relies on; only capabilities use it.")
        ts.require(isinstance(features, list) and all(isinstance(value, str) and value.strip() and value == value.strip()
                                                      for value in features) and len(features) == len(set(features)),
                   "invalid", "extensions.features must be a list of unique exact feature IDs.")
    if "implemented_on" in extensions:
        implemented_on = extensions["implemented_on"]
        ts.require(isinstance(implemented_on, str)
                   and re.fullmatch(r"\d{4}-\d{2}-\d{2}", implemented_on) is not None,
                   "invalid", "extensions.implemented_on must use ISO YYYY-MM-DD when supplied.")
        try:
            date.fromisoformat(implemented_on)
        except ValueError as exc:
            raise ts.ReadError("invalid", "extensions.implemented_on must use ISO YYYY-MM-DD when supplied.") from exc


def category_choices(root="."):
    """Saved catalog entries are the registry for reusable custom categories."""
    choices = {v.casefold(): v for v in CATEGORY_EXAMPLES}
    for item in project(root).get("items", []):
        value = item.get("extensions", {}).get("category")
        if item.get("type") in CATEGORY_TYPES and isinstance(value, str) and value.strip() and value.strip().casefold() != "other":
            choices.setdefault(value.strip().casefold(), value.strip())
    return sorted(choices.values(), key=str.casefold)


def resolve_category(extensions):
    value = extensions.get("category")
    if isinstance(value, str) and value.strip().casefold() == "other":
        value = extensions.get("category_other")
        ts.require(isinstance(value, str) and value.strip() and value.strip().casefold() != "other",
                   "invalid", "Other requires a descriptive extensions.category_other.")
        extensions["category"] = value.strip()
    elif isinstance(value, str):
        extensions["category"] = value.strip()
    extensions.pop("category_other", None)


def catalog_form(item_type, draft=None, root="."):
    """Return a no-write catalog draft with every required semantic field visible."""
    ts.require(item_type in CATEGORY_TYPES, "invalid", "Board form type must be feature or capability.")
    draft = {} if draft is None else draft
    ts.require(isinstance(draft, dict), "invalid", "Board form request must be an object.")
    supplied_extensions = draft.get("extensions", {})
    ts.require(isinstance(supplied_extensions, dict), "invalid", "Board form extensions must be an object.")
    item = {
        **deepcopy(draft),
        "id": draft.get("id", "<generated by plan add unless supplied>"),
        "type": item_type,
        "title": draft.get("title", "<required title>"),
        "summary": draft.get("summary", "<required bounded summary>"),
        "status": draft.get("status", "<choose planned|active|blocked|completed|deferred>"),
        "extensions": {**deepcopy(supplied_extensions),
                       "category": supplied_extensions.get("category", "<required category>")},
    }
    if "implemented_on" in supplied_extensions:
        item["extensions"]["implemented_on"] = supplied_extensions["implemented_on"]
    operation_item = deepcopy(item)
    if "id" not in draft:
        operation_item.pop("id", None)
    missing = [name for name, value in (("title", draft.get("title")),
                                         ("summary", draft.get("summary")), ("status", draft.get("status")),
                                         ("extensions.category", supplied_extensions.get("category")))
               if not isinstance(value, str) or not value.strip()]
    if str(supplied_extensions.get("category", "")).strip().casefold() == "other" and not str(supplied_extensions.get("category_other", "")).strip():
        missing.append("extensions.category_other")
    from .shared_work import load as shared_load
    if shared_load(root)['enabled'] and 'id' not in draft:
        import uuid
        token = item['extensions'].setdefault('creation_token', str(uuid.uuid4()))
        item['id'] = item_type + '-' + token
        operation_item = deepcopy(item)
    from .work_attribution import profile_hint
    return {'participant_profile':profile_hint(root) if root is not None else None, "profile": CONFIG, "outcome": "form", "type": item_type, "item": item,
            "missing_fields": missing, "category_examples": list(CATEGORY_EXAMPLES),
            "categories": [*category_choices(root), "Other"],
            "category_other": "For Other, fill extensions.category_other. Saving the item adds that category to future forms.",
            "implemented_on": {"required": False, "format": "YYYY-MM-DD",
                               "meaning": "Supply only when known; it is never fabricated from recorded_on."},
            "catalog_semantics": {"current": "status completed", "future": "status planned or deferred",
                                  "active": "status active or blocked; queued on Current Tasks, not listed as future",
                                  "historical": "status cancelled or superseded; retained by ID, not actionable future work",
                                  "lifecycle": "The ID is kept for life: Future list, then Current Tasks while active, then the matching Current list; completing it needs a move into the Current owner in the same batch."},
            "batch_operation": {"kind": "add", "request": {"owner_path": "<select with batch form>",
                "expected_sha256": "<owners[].sha256 from batch form>", "index": 0, "items": [operation_item]}},
            "next": {"select": "python -m eutonos.boards_cli batch form --root ROOT [--request SELECTION.json]",
                     "submit": "Put batch_operation in one write-batch request, then run python -m eutonos.boards_cli batch submit --root ROOT --request BATCH.json --host HOST --actor ACTOR.",
                     "qualification": "Managed submit obtains exact temporary authority, verifies readback and queues contention; this form itself performs no write."}}


def work_form(item_type, draft=None, root=None):
    """Return a guided no-write draft for the distinct work record kinds."""
    ts.require(item_type in WORK_FORM_TYPES, "invalid", "Work form type must be campaign, stage, slice, or quick.")
    draft = {} if draft is None else draft
    ts.require(isinstance(draft, dict), "invalid", "Board form request must be an object.")
    relationship = {
        "campaign": "A multi-stage outcome. It has no parent.",
        "stage": "One ordered campaign increment. parent_id must name its campaign.",
        "slice": "A substantial standalone chunk of work. It has no campaign parent. Optional extensions.stage_id names the exact stage whose checkpoint must be assessed when this slice completes.",
        "quick": "A small recorded edit. It has no campaign parent and still requires a useful description.",
    }[item_type]
    item = {**deepcopy(draft), "id": draft.get("id", "<generated by plan add unless supplied>"), "type": item_type,
            "title": draft.get("title", "<required title>"),
            "summary": draft.get("summary", "<required description>"),
            "status": draft.get("status", "<choose planned|active|blocked|completed|deferred>")}
    if item_type == "stage":
        item["parent_id"] = draft.get("parent_id", "<required campaign ID>")
    else:
        item.pop("parent_id", None)
    missing = [name for name, value in (("title", draft.get("title")),
                                         ("summary", draft.get("summary")), ("status", draft.get("status")))
               if not isinstance(value, str) or not value.strip()]
    if item_type == "stage" and (not isinstance(draft.get("parent_id"), str) or not draft["parent_id"].strip()):
        missing.append("parent_id")
    supplied_extensions = draft.get("extensions", {})
    ts.require(isinstance(supplied_extensions, dict), "invalid", "Work form extensions must be an object.")
    if "execution_recommendation" in supplied_extensions:
        ts.require(item_type in {"stage", "slice"}, "invalid", "Execution recommendations belong to stages or slices.")
        item["extensions"]["execution_recommendation"] = execution_recommendation(supplied_extensions["execution_recommendation"])
    operation_item = deepcopy(item)
    if "id" not in draft:
        operation_item.pop("id", None)
    if root is not None:
        from .shared_work import load
        if load(root)['enabled'] and 'id' not in draft:
            import uuid
            token = item.setdefault('extensions', {}).setdefault('creation_token', str(uuid.uuid4()))
            item['id'] = item_type + '-' + token
            operation_item = deepcopy(item)
    from .work_attribution import profile_hint
    return {'participant_profile':profile_hint(root) if root is not None else None, "profile": CONFIG, "outcome": "form", "type": item_type, "item": item,
            "missing_fields": missing, "relationship": relationship,
            "execution_recommendation": {"optional": True, "example": {"model": "Sol", "effort": "Medium",
                "basis": "Bounded implementation", "advisory": True},
                "meaning": "Advisory planning only; current routing, availability and owner choice decide execution."} if item_type in {"stage", "slice"} else None,
            "timing": "If the user adds this work during an active run, submit it through the managed batch route before continuing so the board and crash-recovery record are current.",
            "status_groups": {"active": ["active", "blocked"], "not-active": ["planned", "deferred"],
                              "done": ["completed"], "cancelled": ["cancelled", "superseded"]},
            "batch_operation": {"kind": "add", "request": {"owner_path": "<select with batch form>",
                "expected_sha256": "<owners[].sha256 from batch form>", "index": 0, "items": [operation_item]}},
            "next": {"select": "python -m eutonos.boards_cli batch form --root ROOT [--request SELECTION.json]",
                     "submit": "Put batch_operation in one write-batch request, then run python -m eutonos.boards_cli batch submit --root ROOT --request BATCH.json --host HOST --actor ACTOR.",
                     "qualification": "Managed submit obtains exact temporary authority, verifies readback and queues contention; this form itself performs no write."}}


def resolve_owner(root, item_id, read=None):
    """Resolve one exact board item to its canonical owner while observing every possible owner."""
    require_shared(root)
    local = root if isinstance(root, LocalRoot) else LocalRoot(root)
    read = read or local.read
    ts.string(item_id)
    raw = read("eutonos.boards.json")
    if raw is None:
        return None
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Board owner configuration must be UTF-8.") from exc
    ts.shape(value, {"profile", "owners"})
    ts.require(value["profile"] == CONFIG and isinstance(value["owners"], list),
               "unsupported", "Unknown board owner configuration.")
    owners = [safe_path(path) for path in value["owners"]]
    ts.require(owners and len(owners) == len(set(owners)), "ambiguous",
               "Board owner paths must be nonempty and unique.")
    matches = []
    for path in owners:
        source = read(path)
        ts.require(source is not None, "missing", "Configured board owner is absent: " + path)
        board, _ = _parse(source, path)
        if board is not None and any(item["id"] == item_id for item in board["items"]):
            matches.append(path)
    ts.require(len(matches) <= 1, "ambiguous", "Board item ID has multiple canonical owners: " + item_id)
    return matches[0] if matches else None


def _generated_id(item, owner_path, expected, index):
    supplied = item.get("id")
    if isinstance(supplied, str) and supplied.strip() and supplied.strip().casefold() != "auto":
        return supplied
    item_type = item.get("type")
    ts.require(item_type in TYPES, "invalid", "Board item type is required before generating its ID.")
    token = item.get('extensions', {}).get('creation_token')
    if token is not None:
        import uuid
        ts.require(str(uuid.UUID(token)) == token, 'invalid', 'Creation token must be a full UUID.')
        return ('quick' if item_type == 'task' else item_type) + '-' + token
    seed = {"owner_path": owner_path, "expected_sha256": expected, "index": index,
            "item": {key: value for key, value in item.items() if key != "id"}}
    suffix = hashlib.sha256(ts.encoded(seed)).hexdigest()[:12]
    prefix = {"campaign": "campaign", "stage": "stage", "slice": "slice", "quick": "quick",
              "feature": "feature", "capability": "capability", "goal": "goal", "bug": "bug",
              "task": "quick", "attempt": "attempt", "project": "project"}[item_type]
    return prefix + "-" + suffix


def _validate_stage_association(item, by_id=None):
    """An explicit slice association is a reference, never a parent or inference."""
    extensions = item.get("extensions", {})
    if "stage_id" not in extensions:
        return None
    stage_id = extensions["stage_id"]
    ts.require(item["type"] == "slice", "invalid", "Only slice records use extensions.stage_id.")
    ts.require(isinstance(stage_id, str) and stage_id.strip() and stage_id == stage_id.strip(),
               "invalid", "Slice stage_id must be an exact nonblank stage ID; omit it for standalone scope.")
    if by_id is not None:
        ts.require(stage_id in by_id and by_id[stage_id][1]["type"] == "stage", "missing",
                   "Slice stage_id must name an existing stage: " + stage_id)
        ts.require(("source_import" in extensions) ==
                   ("source_import" in by_id[stage_id][1].get("extensions", {})),
                   "denied", "Imported and ordinary work cannot share stage associations.")
        parent_id = by_id[stage_id][1].get("parent_id")
        ts.require(parent_id in by_id and by_id[parent_id][1]["type"] == "campaign", "invalid",
                   "Associated stage must have an existing campaign parent.")
    return stage_id


def _validate_feature_links(item, by_id):
    """A capability's extensions.features IDs must name existing feature records."""
    features = item.get("extensions", {}).get("features")
    if features is None or by_id is None:
        return
    for feature_id in features:
        ts.require(feature_id in by_id and by_id[feature_id][1]["type"] == "feature", "missing",
                   "Capability extensions.features must name existing features: " + str(feature_id))


def _validate_written_work_item(item, by_id=None):
    _validate_feature_links(item, by_id)
    if item["type"] in {"goal", "project"}:
        from .goal_records import validate_goal, validate_project
        (validate_goal if item["type"] == "goal" else validate_project)(item, writing=True)
    _validate_stage_association(item, by_id)
    if item["type"] in WORK_FORM_TYPES:
        from .work_lifecycle import progress
        progress(item)
    if "execution_recommendation" in item.get("extensions", {}):
        ts.require(item["type"] in {"stage", "slice"}, "invalid", "Execution recommendations belong to stages or slices.")
        item["extensions"]["execution_recommendation"] = execution_recommendation(item["extensions"]["execution_recommendation"])
    if item["type"] in {"campaign", "slice", "quick"}:
        ts.require("parent_id" not in item, "invalid", item["type"].capitalize() + " records cannot have a parent_id.")
    if item["type"] == "stage":
        parent = item.get("parent_id")
        ts.require(isinstance(parent, str) and parent, "invalid", "Stage records require a campaign parent_id.")
        if by_id is not None:
            ts.require(parent in by_id and by_id[parent][1]["type"] == "campaign", "invalid",
                       "Stage parent_id must name a campaign.")


def _board(value):
    ts.shape(value, {"profile", "items"}, {"extensions"})
    ts.require(value["profile"] == BOARD and isinstance(value["items"], list), "unsupported", "Unknown board document.")
    result = {"profile": BOARD, "items": [_item(item) for item in value["items"]]}
    if "extensions" in value:
        ts.require(isinstance(value["extensions"], dict), "invalid", "Board extensions must be an object.")
        result["extensions"] = deepcopy(value["extensions"])
    return result


def _config(local):
    require_shared(local)
    raw = local.read("eutonos.boards.json")
    if raw is None:
        return None, None
    try:
        value = ts.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Board owner configuration must be UTF-8.") from exc
    ts.shape(value, {"profile", "owners"})
    ts.require(value["profile"] == CONFIG and isinstance(value["owners"], list), "unsupported", "Unknown board owner configuration.")
    owners = [safe_path(path) for path in value["owners"]]
    ts.require(owners and len(owners) == len(set(owners)), "ambiguous", "Board owner paths must be nonempty and unique.")
    return raw, owners


def _parse(raw, path):
    matches = list(BLOCK.finditer(raw))
    if not matches:
        ts.require(not OPEN.search(raw), "invalid", "Unclosed or malformed TokenSlang board block in " + path)
        return None, matches
    ts.require(len(matches) == 1, "ambiguous", "Multiple TokenSlang board blocks in " + path)
    try:
        value = ts.loads(matches[0].group("body").decode("utf-8"))
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Board block must be UTF-8: " + path) from exc
    return _board(value), matches


def _validate_graph(entries):
    by_id = {}
    for path, board in entries.items():
        if board is None:
            continue
        for item in board["items"]:
            ts.require(item["id"] not in by_id, "ambiguous", "Duplicate board item ID: " + item["id"])
            by_id[item["id"]] = (path, item)
    for _, item in by_id.values():
        _validate_stage_association(item, by_id)
        parent = item.get("parent_id")
        ts.require(parent is None or parent in by_id, "missing", "Board parent ID is absent: " + str(parent))
        if parent in by_id:
            ts.require(("source_import" in item.get("extensions", {})) ==
                       ("source_import" in by_id[parent][1].get("extensions", {})),
                       "denied", "Imported and ordinary work cannot share parent-child links.")
        ts.require(parent != item["id"], "conflict", "Board item cannot parent itself: " + item["id"])
    for item_id in by_id:
        seen, current = set(), item_id
        while current is not None:
            ts.require(current not in seen, "conflict", "Board parent cycle includes: " + current)
            seen.add(current)
            current = by_id[current][1].get("parent_id")
    return by_id


def _project_graph(entries, diagnostics):
    """Keep independent valid records visible while quarantining broken components."""
    occurrences = {}
    for path, board in entries.items():
        if board is None:
            continue
        for item in board["items"]:
            occurrences.setdefault(item["id"], []).append((path, item))
    duplicate_ids = {item_id for item_id, rows in occurrences.items() if len(rows) != 1}
    for item_id in sorted(duplicate_ids):
        diagnostics.append("Duplicate board item ID was quarantined: " + item_id)
    by_id = {item_id: rows[0] for item_id, rows in occurrences.items() if item_id not in duplicate_ids}
    changed = True
    while changed:
        changed = False
        for item_id, (_, item) in list(by_id.items()):
            parent = item.get("parent_id")
            if parent is not None and parent not in by_id:
                diagnostics.append("Board item with absent parent was quarantined: " + item_id)
                del by_id[item_id]
                changed = True
    cyclic = set()
    for item_id in by_id:
        seen, current = [], item_id
        while current is not None and current in by_id:
            if current in seen:
                cyclic.update(seen[seen.index(current):])
                break
            seen.append(current)
            current = by_id[current][1].get("parent_id")
    for item_id in sorted(cyclic):
        diagnostics.append("Board parent cycle item was quarantined: " + item_id)
        by_id.pop(item_id, None)
    for item_id, (_, item) in by_id.items():
        try:
            _validate_stage_association(item, by_id)
        except ts.ReadError as exc:
            diagnostics.append("Invalid stage association for " + item_id + ": " + str(exc))
    return by_id


def _load(local, strict=False):
    require_shared(local)
    config_raw, owners = _config(local)
    if owners is None:
        return config_raw, None, {}, {}, []
    raw_by_path, boards, diagnostics = {}, {}, []
    for path in owners:
        raw = None
        try:
            raw = local.read(path)
            raw_by_path[path] = raw
            if raw is None:
                boards[path] = None
                diagnostics.append("Configured board owner is absent: " + path)
                continue
            board, matches = _parse(raw, path)
            boards[path] = board
            if not matches:
                diagnostics.append("No TokenSlang board block; prose was not interpreted: " + path)
        except (OSError, TypeError, ValueError, ts.ReadError) as exc:
            raw_by_path[path] = raw
            boards[path] = None
            diagnostics.append("Board owner unavailable: " + path + " (" + getattr(exc, "code", "invalid") + ")")
            if strict:
                raise
    if strict:
        _validate_graph(boards)
    return config_raw, owners, raw_by_path, boards, diagnostics


def project(root):
    require_shared(root)
    local = root if isinstance(root, LocalRoot) else LocalRoot(root)
    try:
        _, owners, raw_by_path, boards, diagnostics = _load(local)
    except (OSError, TypeError, ValueError, ts.ReadError) as exc:
        return {"status": "partial", "owners": [], "items": [],
                "diagnostics": ["Board configuration unavailable: " + getattr(exc, "code", "invalid")]}
    if owners is None:
        return {"status": "not-configured", "owners": [], "items": [], "diagnostics": []}
    by_id = _project_graph(boards, diagnostics)
    projected = []
    if by_id:
        for path in owners:
            board = boards.get(path)
            if board is None:
                continue
            for order, item in enumerate(board["items"]):
                if by_id.get(item["id"]) != (path, item):
                    continue
                shown = {**deepcopy(item), "owner_path": path, "order": order}
                if item["type"] in CATEGORY_TYPES:
                    shown["catalog_section"] = ("current" if item["status"] == "completed" else
                                                "active" if item["status"] in {"active", "blocked"} else
                                                "future" if item["status"] in {"planned", "deferred"}
                                                else "historical")
                if item["type"] == "slice" and item.get("parent_id"):
                    shown["display_type"] = "stage"
                if item["type"] == "task":
                    shown["display_type"] = "quick"
                if item["type"] == "campaign":
                    children = [child for _, child in by_id.values()
                                if child.get("parent_id") == item["id"] and child["type"] in {"stage", "slice"}
                                and "source_import" not in child.get("extensions", {})]
                    shown["child_counts"] = {"completed": sum(child["status"] == "completed" for child in children),
                                             "total": len(children)}
                projected.append(shown)
    owner_rows = [{"path": path, "sha256": digest(raw_by_path.get(path)),
                   "status": "available" if boards.get(path) is not None else
                             "unavailable" if raw_by_path.get(path) is None else
                             "invalid" if OPEN.search(raw_by_path[path]) else "legacy"}
                  for path in owners]
    from . import work_lifecycle
    try:
        work_order = work_lifecycle.projection(local, projected)
    except (ValueError, OSError, ts.ReadError) as exc:
        diagnostics.append("Work order unavailable: " + str(exc))
        work_order = {"configured": False, "revision": None, "current": [], "next": []}
    positions = {identity: (lane, n) for lane in ("current", "next")
                 for n, identity in enumerate(work_order[lane])}
    from .work_attribution import project as attribution_project
    for item in projected:
        item['attribution'] = attribution_project(item)
        item["revision"] = work_lifecycle.item_revision(item)
        lane, priority = positions.get(item["id"], (None, None))
        imported = item.get("extensions", {}).get("source_import")
        import_lane = ("imported-history" if imported.get("disposition") == "historical" else "held") if imported else None
        item.update(work_lane=("imported-history" if import_lane == "imported-history" else
                               "done" if item["status"] in work_lifecycle.TERMINAL else import_lane or lane),
                    priority=None if imported else priority)
    inspection = {"imported_history": [i for i in projected if i["work_lane"] == "imported-history"],
                  "held_work": [i for i in projected if i["work_lane"] == "held"]}
    return {"status": "partial" if diagnostics else "available", "owners": owner_rows,
            "items": projected, "diagnostics": diagnostics, "work_order": work_order,
            **(inspection if any(inspection.values()) else {})}


read = project


def _expected(value):
    expected = value["expected_sha256"]
    ts.require(isinstance(expected, str) and len(expected) == 64
               and all(char in "0123456789abcdef" for char in expected), "invalid", "Expected source SHA-256 is invalid.")
    return expected


def _replace(raw, match, board):
    block = b"```tokenslang-board\n" + _json(board) + b"\n```"
    return raw[:match.start()] + block + raw[match.end():]


def _prepare(root, request, operation):
    require_shared(root)
    local = root if isinstance(root, LocalRoot) else LocalRoot(root)
    common = {"profile", "owner_path", "expected_sha256"}
    required = common | ({"index"} if operation == "add" else
                         {"item_id", "changes"} if operation == "update" else {"item_ids"})
    ts.shape(request, required, ({"item", "items"} if operation == "add" else set()) | {'caller_context', '_attribution'})
    from . import work_attribution as attribution
    request_changes = request.get("changes") or {}
    ts.require(isinstance(request_changes, dict), "invalid", "Board update changes must be an object.")
    request_extensions = request_changes.get("extensions")
    ts.require(request_extensions is None or isinstance(request_extensions, dict), "invalid",
               "Board update extensions must be an object.")
    binding = attribution.snapshot(local.root, request) if operation == 'add' or operation == 'update' and (
        request_changes.get('status') == 'completed' or
        isinstance(request_extensions, dict) and 'completion_checks' in request_extensions) else None
    expected_profile = {"add": ADD, "update": UPDATE, "reorder": REORDER}[operation]
    ts.require(request["profile"] == expected_profile, "unsupported", "Unknown board edit profile.")
    owner_path, expected = safe_path(request["owner_path"]), _expected(request)
    config_raw, owners, raw_by_path, boards, _ = _load(local, strict=True)
    ts.require(owners is not None and owner_path in owners, "denied", "Board owner path is not selected by configuration.")
    raw = raw_by_path[owner_path]
    ts.require(raw is not None, "missing", "Selected board owner is absent.")
    ts.require(digest(raw) == expected, "stale", "Selected board owner differs from expected bytes.")
    board, matches = _parse(raw, owner_path)
    prior_goals = {item['id']: deepcopy(item.get('extensions', {}).get('goal', {}).get('work_links', []))
                   for item in (board or {}).get('items', []) if item['type'] == 'goal'}
    if operation == "add":
        ts.require(("item" in request) != ("items" in request), "invalid", "Supply one item or one ordered items array.")
        if board is None:
            board = {"profile": BOARD, "items": []}
        ts.require(type(request["index"]) is int and 0 <= request["index"] <= len(board["items"]),
                   "invalid", "Board insertion index is outside the current order.")
        supplied = [request["item"]] if "item" in request else request["items"]
        ts.require(isinstance(supplied, list) and supplied, "invalid", "Board add items must be a nonempty array.")
        normalized = []
        for offset, item in enumerate(supplied):
            ts.require(isinstance(item, dict), "invalid", "Board add items must be objects.")
            ts.require("source_import" not in item.get("extensions", {}), "denied",
                       "Import provenance is reserved for the fixture import route.")
            from .shared_work import load as shared_config
            if shared_config(local.root)['enabled'] and (not item.get('id') or str(item['id']).casefold() == 'auto'):
                ts.require(bool(item.get('extensions', {}).get('creation_token')), 'invalid', 'Shared creation requires a frozen full UUID token from the native form.')
            normalized.append({**deepcopy(item), "id": _generated_id(item, owner_path, expected,
                                                                       request["index"] + offset)})
        for item in normalized:
            attribution.create(item, binding)
            if "completion_checks" in item.get("extensions", {}):
                from . import completion_checks
                item["extensions"]["completion_checks"] = completion_checks.reconcile(
                    item["extensions"]["completion_checks"], actor=binding)
            if item['status'] == 'completed':
                attribution.append(item, binding, 'completed', completed_revision=attribution.content_revision(item))
        supplied_items = [_item(item) for item in normalized]
        for item in supplied_items:
            if item['type'] in {'goal', 'project'}:
                token = item.get('extensions', {}).get('creation_token')
                ts.require(token is not None and item['id'] == item['type'] + '-' + token,
                           'invalid', 'New goals and projects require the frozen full UUID from their form.')
            _validate_catalog_fields(item)
            _validate_written_work_item(item)
            if item["status"] == "completed":
                from . import completion_checks
                completion_checks.require_complete(item)
        board["items"][request["index"]:request["index"]] = supplied_items
        written_ids = {item["id"] for item in supplied_items}
    else:
        ts.require(board is not None, "missing", "Selected owner has no TokenSlang board block.")
        if operation == "update":
            ts.string(request["item_id"])
            matches_by_id = [index for index, item in enumerate(board["items"]) if item["id"] == request["item_id"]]
            ts.require(len(matches_by_id) == 1, "missing", "Selected board item is absent.")
            changes = request["changes"]
            allowed = {"type", "title", "summary", "status", "parent_id", "evidence_refs", "next_action", "extensions"}
            ts.require(isinstance(changes, dict) and changes and set(changes) <= allowed,
                       "invalid", "Board update fields are invalid.")
            if "extensions" in changes:
                ts.require(isinstance(changes["extensions"], dict), "invalid",
                           "Board update extensions must be an object.")
            changed = deepcopy(board["items"][matches_by_id[0]])
            attribution.preserve(changed, changes)
            previous_status = changed['status']
            ts.require("source_import" not in changed.get("extensions", {})
                       and "source_import" not in changes.get("extensions", {}), "denied",
                       "Imported history and hold are immutable through ordinary board updates.")
            from . import work_lifecycle
            if changed["type"] in work_lifecycle.WORK and work_lifecycle.load_order(local) is not None:
                ts.require(changes.get("status") not in work_lifecycle.TERMINAL,
                           "invalid", "Use boards work submit to complete, cancel or supersede work and file its result.")
                ts.require(changed["status"] not in work_lifecycle.TERMINAL,
                           "invalid", "Restore finished work through the lifecycle CLI before editing it.")
                if "extensions" in changes:
                    protected = {"work_history", "checkpoint", "last_work_request", "archived", "superseded_by"}
                    ts.require(all(changes["extensions"].get(k) == changed.get("extensions", {}).get(k) for k in protected),
                               "invalid", "Lifecycle history must be preserved; use the work CLI for filing.")
            for key, value in changes.items():
                if key in {"parent_id", "next_action"} and value is None:
                    changed.pop(key, None)
                else:
                    changed[key] = deepcopy(value)
            prior_checks = board["items"][matches_by_id[0]].get("extensions", {}).get("completion_checks")
            if "extensions" in changes and ("completion_checks" in changes["extensions"] or prior_checks is not None):
                from . import completion_checks
                ts.require("completion_checks" in changed["extensions"], "invalid",
                           "Preserve existing completion checks when replacing extensions.")
                changed["extensions"]["completion_checks"] = completion_checks.reconcile(
                    changed["extensions"]["completion_checks"], prior_checks, binding)
            if changed['status'] == 'completed' and previous_status != 'completed':
                attribution.append(changed, binding, 'completed', completed_revision=attribution.content_revision(changed))
            changed = _item(changed)
            if board['items'][matches_by_id[0]]['type'] == 'attempt':
                from .shared_attempts import immutable
                immutable(board['items'][matches_by_id[0]], changed)
            _validate_catalog_fields(changed)
            _validate_written_work_item(changed)
            if changed["status"] == "completed":
                from . import completion_checks
                completion_checks.require_complete(changed)
            board["items"][matches_by_id[0]] = changed
            written_ids = {changed["id"]}
        else:
            order = request["item_ids"]
            ts.require(isinstance(order, list) and all(isinstance(item_id, str) for item_id in order)
                       and len(order) == len(set(order))
                       and set(order) == {item["id"] for item in board["items"]}
                       and len(order) == len(board["items"]), "invalid", "Reorder must list every item ID exactly once.")
            by_id = {item["id"]: item for item in board["items"]}
            board["items"] = [by_id[item_id] for item_id in order]
            written_ids = set()
    prospective = {**boards, owner_path: board}
    prospective_by_id = _validate_graph(prospective)
    if not getattr(local, "defers_catalog_placement", False):
        check_catalog_placement(local, boards, prospective)
    for item_id in written_ids:
        _validate_written_work_item(prospective_by_id[item_id][1], prospective_by_id)
        item = prospective_by_id[item_id][1]
        if item['type'] == 'goal':
            from .scoped_items import check_shared_links
            check_shared_links(local.root, item.get('extensions', {}).get('goal', {}).get('work_links', []),
                               previous=prior_goals.get(item_id, []))
    if matches:
        desired = _replace(raw, matches[0], board)
    else:
        separator = b"\n" if raw.endswith((b"\n", b"\r")) else b"\n\n"
        desired = raw + separator + b"```tokenslang-board\n" + _json(board) + b"\n```\n"
    inputs = ["eutonos.boards.json", *[path for path in owners if path != owner_path]]
    updates = {owner_path: desired}
    from . import work_lifecycle
    order = work_lifecycle.load_order(local)
    if order is not None:
        work_lifecycle.views(local, prospective, order, updates)
        inputs.append(work_lifecycle.PATH)
    effect = effect_plan(local, updates, inputs=inputs)
    return {"profile": PLAN, "outcome": "planned", "operation": operation, "owner_path": owner_path,
            "source_sha256": expected, "result_sha256": digest(desired), "item_ids": [item["id"] for item in board["items"]],
            "written_ids": sorted(written_ids),
            "effect": effect, "fingerprint": digest(ts.encoded(effect)), "paths": [c["path"] for c in effect["changes"]],
            "qualification": "Guarded byte plan only; application does not infer approval, dispatch, or prose status."}


def catalog_owners(local):
    """Catalogue owners by path: {path: (horizon, type)}, declared by docs/REGISTRY.json roles.

    A role such as current-feature-catalogue or future-capability-catalogue names the
    horizon (current or future) and the record type the owner lists. An absent or
    unreadable registry declares nothing, so no placement rule applies.
    """
    try:
        raw = local.read("docs/REGISTRY.json")
        documents = json.loads(raw.decode("utf-8")).get("documents") if raw is not None else None
    except (OSError, UnicodeError, ValueError, AttributeError, ts.ReadError):
        return {}
    owners = {}
    for row in documents if isinstance(documents, list) else []:
        role = row.get("role") if isinstance(row, dict) else None
        match = CATALOG_ROLE.fullmatch(role) if isinstance(role, str) else None
        if match and isinstance(row.get("path"), str):
            owners[row["path"]] = (match.group(1), match.group(2))
    return owners


def check_catalog_placement(local, before, after):
    """Refuse a write that leaves a touched, completed feature or capability in a Future owner.

    `before` and `after` map owner path to its parsed board. Only records this write
    added, changed or moved are judged, so older misplaced records never block unrelated
    work. When no Current owner is declared for the record's type there is nowhere to
    move it, and the write is not refused.
    """
    def rows(entries):
        return {item["id"]: (path, item) for path, board in entries.items() if board for item in board["items"]}

    old, new = rows(before), rows(after)
    touched = {item_id: (path, item) for item_id, (path, item) in new.items()
               if item["type"] in CATEGORY_TYPES and item["status"] == "completed" and old.get(item_id) != (path, item)}
    if not touched:  # most writes never complete a catalogue record; they never read the registry
        return
    owners = catalog_owners(local)
    futures = {path for path, (horizon, _) in owners.items() if horizon == "future"}
    problems = []
    for item_id, (path, item) in touched.items():
        if path not in futures:
            continue
        target = next((owner for owner, (horizon, kind) in sorted(owners.items())
                       if horizon == "current" and kind == item["type"]), None)
        if target is not None:
            problems.append(f"{item_id} ({item['type']}) is completed but sits in the Future owner {path}; "
                            f"in the same batch move it to {target}")
    ts.require(not problems, "invalid",
               "A completed feature or capability must end up in its Current owner. " + "; ".join(problems)
               + ". Use one write batch: an update that sets status completed plus a move operation "
               "(from/to owner paths and each owner's original sha256, item_id, index).")


def plan_move(root, request):
    """Carry one feature or capability record between two configured owners, keeping its ID.

    The record is copied unchanged except for the optional new type (feature or
    capability) and optional extensions.category. Both owners are guarded by their
    expected SHA-256; nothing here regenerates or invents an ID.
    """
    require_shared(root)
    local = root if isinstance(root, LocalRoot) else LocalRoot(root)
    ts.shape(request, {"profile", "from_owner_path", "from_expected_sha256", "to_owner_path",
                       "to_expected_sha256", "item_id", "index"}, {"type", "category"})
    ts.require(request["profile"] == MOVE, "unsupported", "Unknown board move profile.")
    source_path, target_path = safe_path(request["from_owner_path"]), safe_path(request["to_owner_path"])
    ts.require(source_path != target_path, "invalid", "A move needs two different owners; use reorder inside one owner.")
    expected = {}
    for name, path in (("from", source_path), ("to", target_path)):
        value = request[name + "_expected_sha256"]
        ts.require(isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value),
                   "invalid", "Expected source SHA-256 is invalid.")
        expected[path] = value
    item_id = ts.string(request["item_id"])
    config_raw, owners, raw_by_path, boards_by_path, _ = _load(local, strict=True)
    ts.require(owners is not None and source_path in owners and target_path in owners, "denied",
               "Both board owner paths must be selected by configuration.")
    for path in (source_path, target_path):
        ts.require(raw_by_path[path] is not None, "missing", "Selected board owner is absent: " + path)
        ts.require(digest(raw_by_path[path]) == expected[path], "stale",
                   "Selected board owner differs from expected bytes: " + path)
    source_board, source_matches = _parse(raw_by_path[source_path], source_path)
    target_board, target_matches = _parse(raw_by_path[target_path], target_path)
    ts.require(source_board is not None, "missing", "Source owner has no TokenSlang board block.")
    if target_board is None:
        target_board = {"profile": BOARD, "items": []}
    ts.require(not any(item["id"] == item_id for item in target_board["items"]), "conflict",
               "Target owner already holds this item ID.")
    positions = [n for n, item in enumerate(source_board["items"]) if item["id"] == item_id]
    ts.require(len(positions) == 1, "missing", "Selected board item is absent from the source owner.")
    moved = deepcopy(source_board["items"][positions[0]])
    ts.require(moved["type"] in CATEGORY_TYPES, "invalid", "Only feature and capability records can move.")
    ts.require("source_import" not in moved.get("extensions", {}), "denied",
               "Imported history is immutable through ordinary board moves.")
    if "type" in request:
        ts.require(request["type"] in CATEGORY_TYPES, "invalid", "A move may only retype to feature or capability.")
        moved["type"] = request["type"]
    if "category" in request:
        ts.require(isinstance(request["category"], str), "invalid", "Move category must be a string.")
        moved.setdefault("extensions", {})["category"] = request["category"]
    ts.require(type(request["index"]) is int and 0 <= request["index"] <= len(target_board["items"]),
               "invalid", "Board insertion index is outside the target order.")
    moved = _item(moved)
    _validate_catalog_fields(moved)
    _validate_written_work_item(moved)
    del source_board["items"][positions[0]]
    target_board["items"].insert(request["index"], moved)
    prospective = {**boards_by_path, source_path: source_board, target_path: target_board}
    prospective_by_id = _validate_graph(prospective)
    _validate_written_work_item(prospective_by_id[item_id][1], prospective_by_id)
    updates = {}
    for path, board, raw, matches in ((source_path, source_board, raw_by_path[source_path], source_matches),
                                      (target_path, target_board, raw_by_path[target_path], target_matches)):
        if matches:
            updates[path] = _replace(raw, matches[0], board)
        else:
            separator = b"\n" if raw.endswith((b"\n", b"\r")) else b"\n\n"
            updates[path] = raw + separator + b"```tokenslang-board\n" + _json(board) + b"\n```\n"
    inputs = ["eutonos.boards.json", *[path for path in owners if path not in updates]]
    from . import work_lifecycle
    order = work_lifecycle.load_order(local)
    if order is not None:
        work_lifecycle.views(local, prospective, order, updates)
        inputs.append(work_lifecycle.PATH)
    effect = effect_plan(local, updates, inputs=inputs)
    return {"profile": PLAN, "outcome": "planned", "operation": "move", "owner_path": target_path,
            "source_sha256": expected[source_path],
            "result_sha256": digest(updates[target_path]),
            "item_ids": [item["id"] for item in target_board["items"]], "written_ids": [item_id],
            "effect": effect, "fingerprint": digest(ts.encoded(effect)), "paths": [c["path"] for c in effect["changes"]],
            "qualification": "Guarded byte plan only; the item keeps its ID and fields apart from the requested type and category."}


def plan_add(root, request):
    return _prepare(root, request, "add")


def plan_update(root, request):
    return _prepare(root, request, "update")


def plan_reorder(root, request):
    return _prepare(root, request, "reorder")


def profile_description():
    sha = "<owners[].sha256 from boards read>"
    item = {"id": "SLICE-1", "type": "slice", "title": "Selected slice",
            "summary": "Substantial standalone work.", "status": "planned"}
    return {"profile": CONFIG, "outcome": "profile", "config": {"profile": CONFIG, "owners": ["docs/WORK_ITEMS.md"]},
            "board": {"profile": BOARD, "items": [item]},
            "plans": {
                "move": {"profile": MOVE, "from_owner_path": "docs/FUTURE_FEATURES.md", "from_expected_sha256": sha,
                         "to_owner_path": "docs/CURRENT_FEATURES.md", "to_expected_sha256": sha, "item_id": "feature-1",
                         "index": 0, "type": "feature", "category": "Delivery"},
                "add": {"profile": ADD, "owner_path": "docs/WORK_ITEMS.md", "expected_sha256": sha,
                        "index": 0, "items": [item]},
                "update": {"profile": UPDATE, "owner_path": "docs/WORK_ITEMS.md", "expected_sha256": sha,
                        "item_id": "SLICE-1", "changes": {"status": "active", "next_action": "Run focused checks."}},
                "reorder": {"profile": REORDER, "owner_path": "docs/WORK_ITEMS.md", "expected_sha256": sha,
                            "item_ids": [item["id"]]}},
            "work_records": {"campaign": "multi-stage outcome", "stage": "campaign increment",
                             "slice": "substantial standalone work", "quick": "small recorded edit",
                             "description_required": True,
                             "execution_recommendation": "Optional advisory model, effort and basis on stages and slices; never dispatch authority.",
                             "mid_slice_additions": "Use the type form, batch form and managed batch submit immediately before continuing work."},
            "catalog": {"category": {"required_for": list(CATEGORY_TYPES), "examples": list(CATEGORY_EXAMPLES),
                                     "rule": "extensions.category is required for add/update writer plans; useful custom categories are allowed."},
                        "implemented_on": "Optional extensions.implemented_on must be YYYY-MM-DD when known; recorded_on is never fabricated.",
                        "sections": {"current": "completed", "future": "planned or deferred",
                                     "active": "active or blocked; shown on Current Tasks",
                                     "historical": "cancelled or superseded"},
                        "placement": "A completed feature or capability belongs in the Current owner for its type; a write that leaves it in a Future owner is refused. Owners declare themselves through registry roles current-feature-catalogue, future-feature-catalogue, current-capability-catalogue and future-capability-catalogue."},
            "lifecycle": {"form": "python -m eutonos.boards_cli work form --item-id ID --root ROOT",
                          "submit": "python -m eutonos.boards_cli work submit --request REQUEST.json --root ROOT --host HOST --actor ACTOR",
                          "rule": "Use lifecycle operations to edit, complete, cancel, supersede, archive, restore or prioritize work. Current Tasks, Next Work and Done are derived views; closeout asks for effort estimates."},
            "commands": {"read": "python -m eutonos.boards_cli read --root ROOT",
                         "form": "python -m eutonos.boards_cli form --type feature --root ROOT [--request DRAFT.json]",
                         "batch": "python -m eutonos.boards_cli batch form --root ROOT; then batch submit --request REQUEST.json --host HOST --actor ACTOR",
                         "plan": "python -m eutonos.boards_cli plan add --root ROOT --request REQUEST.json",
                         "apply": "python -m eutonos.boards_cli apply --root ROOT --request PLAN.json --host HOST --actor ACTOR --attempt ATTEMPT --generation N --effect-id ID --fingerprint PLAN_FINGERPRINT"},
            "types": list(TYPES), "statuses": list(STATUSES)}


__all__ = ["ADD", "BOARD", "CATALOG_ROLE", "CATEGORY_EXAMPLES", "CATEGORY_TYPES", "CONFIG", "MOVE", "PLAN", "REORDER", "STATUSES", "TYPES", "UPDATE",
           "WORK_FORM_TYPES", "catalog_form", "catalog_owners", "check_catalog_placement", "work_form", "plan_add", "plan_move", "plan_reorder", "plan_update", "profile_description", "project", "read"]
