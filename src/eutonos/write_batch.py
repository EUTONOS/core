"""Atomic typed board/work batches; arbitrary prose files are never targets."""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .private_scope import require_shared
from . import boards, tokenslang as ts, work_lifecycle
from .effects import LocalRoot, decode, digest, plan as effect_plan

PROFILE = "eutonos.tokenslang.write-batch.v1"
PLAN = "eutonos.tokenslang.write-batch-plan.v1"
KINDS = {"work", "add", "update", "reorder", "move", "import", "owner-register"}
MAX_OPERATIONS = 16


class OverlayRoot(LocalRoot):
    """A bounded in-memory view used to compose existing planners once."""

    # A batch may pair an update with the move that follows it, so catalogue placement
    # is judged once, on the batch's final state, instead of after each operation.
    defers_catalog_placement = True

    def __init__(self, root):
        super().__init__(root)
        self.updates = {}

    def read(self, name):
        return self.updates[name] if name in self.updates else super().read(name)

    def stage(self, effect):
        for change in effect["changes"]:
            self.updates[change["path"]] = decode(change["after"])


def form(root=".", selection=None):
    """Return repository-owned choices and the hashes/revisions needed to submit."""
    require_shared(root)
    selected_owners, selected_items = None, None
    if selection is not None:
        ts.require(isinstance(selection, dict), "invalid", "Batch form selection must be an object.")
        ts.shape(selection, set(), {"owner_paths", "item_ids"})
        if "owner_paths" in selection:
            selected_owners = set(ts.sequence(selection["owner_paths"]))
            ts.require(all(isinstance(value, str) and value for value in selected_owners),
                       "invalid", "owner_paths must contain owner path strings.")
        if "item_ids" in selection:
            selected_items = set(ts.sequence(selection["item_ids"]))
            ts.require(all(isinstance(value, str) and value for value in selected_items),
                       "invalid", "item_ids must contain stable item IDs.")
    projection = boards.project(root)
    ts.require(projection.get("status") == "available", "unavailable",
               "Configured board owners are unavailable.")
    items = [{key: item.get(key) for key in ("id", "title", "type", "status", "revision", "owner_path")}
             for item in projection.get("items", [])
             if (selected_owners is None or item.get("owner_path") in selected_owners)
             and (selected_items is None or item.get("id") in selected_items)]
    included_paths = ({item["owner_path"] for item in items} if selected_items is not None and selected_owners is None
                      else selected_owners)
    owners = [{**owner, "request_defaults": {"owner_path": owner["path"],
               "expected_sha256": owner.get("sha256")}}
              for owner in projection.get("owners", [])
              if owner.get("status") == "available" and
              (included_paths is None or owner.get("path") in included_paths)]
    if selected_owners is not None:
        missing = selected_owners - {owner["path"] for owner in owners}
        ts.require(not missing, "missing", "Unknown or unavailable board owners: " + ", ".join(sorted(missing)))
    if selected_items is not None:
        missing = selected_items - {item["id"] for item in items}
        ts.require(not missing, "missing", "Unknown items in selected owners: " + ", ".join(sorted(missing)))
    from .work_attribution import profile_hint
    return {'participant_profile':profile_hint(root), "profile": PROFILE, "outcome": "form",
            "question": "Which owner documents and records should this atomic request change?",
            "selection": {"owner_paths": sorted(selected_owners or []), "item_ids": sorted(selected_items or [])},
            "owners": owners, "items": items,
            "operation_id": "<generated when omitted>",
            "requested_by": {"kind": "agent-claim", "value": "<optional model/thread label>"},
            "operations": [
                {"kind": "work", "request": {"operation": "update", "item_id": "<ID>",
                                                "expected_revision": "<items[].revision above>", "changes": {}}},
                {"kind": "update", "request": {"owner_path": "<configured board owner>",
                                                  "expected_sha256": "<owner hash from form/plan>",
                                                  "item_id": "<ID>", "changes": {"status": "completed"}}},
                {"kind": "move", "request": {"from_owner_path": "<configured board owner>",
                                                "from_expected_sha256": "<owner hash from form/plan>",
                                                "to_owner_path": "<other configured board owner>",
                                                "to_expected_sha256": "<owner hash from form/plan>",
                                                "item_id": "<feature or capability ID>", "index": 0,
                                                "type": "<optional feature|capability>",
                                                "category": "<optional category>"}}],
            "rules": ["Choose one or more typed operations.",
                      "The CLI plans one all-or-nothing effect and refreshes derived work views once.",
                      "Architecture, principles, contracts, AGENTS and arbitrary Markdown patches are unsupported."]}


def normalize(value):
    ts.require(isinstance(value, dict), "invalid", "Batch request must be an object.")
    ts.shape(value, {"profile", "operations"}, {"operation_id", "requested_by", "caller_context", "_attribution"})
    ts.require(value["profile"] == PROFILE, "unsupported", "Unknown write batch profile.")
    operations = ts.sequence(value["operations"])
    ts.require(0 < len(operations) <= MAX_OPERATIONS, "invalid", "Choose 1..16 batch operations.")
    normalized = []
    for supplied in operations:
        ts.shape(supplied, {"kind", "request"})
        ts.require(supplied["kind"] in KINDS and isinstance(supplied["request"], dict),
                   "invalid", "Batch operations must be typed board or work requests.")
        normalized.append({"kind": supplied["kind"], "request": deepcopy(supplied["request"])})
    requested_by = value.get("requested_by")
    if requested_by is not None:
        ts.require(isinstance(requested_by, dict), "invalid", "requested_by is claimed provenance metadata.")
        ts.shape(requested_by, {"kind", "value"})
        ts.string(requested_by["kind"]); ts.string(requested_by["value"])
        ts.require(len(requested_by["kind"]) <= 64 and len(requested_by["value"]) <= 256,
                   "invalid", "Keep claimed provenance labels bounded.")
    basis = {"profile": PROFILE, "operations": normalized}
    generated = "batch-" + hashlib.sha256(ts.encoded(basis)).hexdigest()[:24]
    operation_id = value.get("operation_id", generated)
    ts.string(operation_id)
    from . import work_attribution as attribution
    if 'caller_context' in value: attribution.context(value['caller_context'])
    if '_attribution' in value: attribution.validate_snapshot(value['_attribution'])
    return {**basis, "operation_id": operation_id,
            **{k: deepcopy(value[k]) for k in ('caller_context','_attribution') if k in value},
            **({"requested_by": deepcopy(requested_by)} if requested_by is not None else {})}


def _effect_receipt(effect):
    """Project record identity from frozen before/after images, never replanning.

    These fields are prospective on a plan and verified only on a completed
    receipt. Comparing both images preserves identity across campaign moves.
    """
    before, after = {}, {}
    for change in effect["changes"]:
        for field, image in (("backup", before), ("after", after)):
            raw = decode(change[field])
            if raw is None or not boards.OPEN.search(raw):
                continue
            board, _ = boards._parse(raw, change["path"])
            if board is None:
                continue
            for position, item in enumerate(board["items"]):
                ts.require(item["id"] not in image, "ambiguous",
                           "Batch receipt has duplicate board record identity.")
                image[item["id"]] = (change["path"], position, item)
    records = []
    for identity, (path, position, item) in sorted(after.items()):
        if before.get(identity) == (path, position, item):
            continue
        records.append({"id": identity, "revision": work_lifecycle.item_revision(item),
                        "owner_path": path, "order": position, "created": identity not in before})
    order = next((digest(decode(change["after"])) for change in effect["changes"]
                  if change["path"] == work_lifecycle.PATH), None)
    return {"records": records, "work_order_revision": order}


def _share_base(overlay, board_bases, owner, original):
    """Every operation on one owner names its original SHA-256; the overlay supplies the rolling one."""
    ts.require(isinstance(original, str) and len(original) == 64 and
               all(character in "0123456789abcdef" for character in original),
               "invalid", "Batch board operations require the original owner SHA-256.")
    if owner not in board_bases:
        ts.require(original == digest(overlay.read(owner)), "stale",
                   "Batch board owner changed after the request was prepared: " + owner)
        board_bases[owner] = original
    else:
        ts.require(original == board_bases[owner], "conflict",
                   "Operations for one board owner must share its original SHA-256.")


def plan(root, value, *, authority=None):
    require_shared(root)
    request = normalize(value)
    local = LocalRoot(root)
    if any(op["kind"] == "import" for op in request["operations"]):
        return _plan_import(local, request, **({"authority": authority} if authority is not None else {}))
    from . import work_attribution as attribution
    binding = attribution.snapshot(local.root, request) if attribution.signs('batch', {'batch':request}) else None
    if binding is not None: request['_attribution'] = binding
    overlay = OverlayRoot(local.root)
    observed, results = set(), []
    seen_work = set()
    board_bases = {}
    catalog_before = boards._load(local)[3]
    for index, operation in enumerate(request["operations"]):
        kind, supplied = operation["kind"], deepcopy(operation["request"])
        if binding is not None and kind in {'work','add','update','reorder'}:
            supplied['_attribution'] = binding
        if kind == 'owner-register':
            from .native_owner import plan as owner_plan
            prepared = owner_plan(overlay, supplied)
        elif kind == "work":
            item_id = supplied.get("item_id")
            if item_id is not None:
                ts.require(item_id not in seen_work, "conflict", "One batch cannot decide the same work item twice.")
                seen_work.add(item_id)
            prepared = work_lifecycle.plan(overlay, supplied)
        elif kind == "move":
            for field in ("from", "to"):
                owner = supplied.get(field + "_owner_path")
                ts.string(owner)
                _share_base(overlay, board_bases, owner, supplied.get(field + "_expected_sha256"))
                supplied[field + "_expected_sha256"] = digest(overlay.read(owner))
            supplied["profile"] = boards.MOVE
            prepared = boards.plan_move(overlay, supplied)
        else:
            owner = supplied.get("owner_path")
            ts.string(owner)
            _share_base(overlay, board_bases, owner, supplied.get("expected_sha256"))
            supplied["profile"] = {"add": boards.ADD, "update": boards.UPDATE, "reorder": boards.REORDER}[kind]
            supplied["expected_sha256"] = digest(overlay.read(owner))
            prepared = {"add": boards.plan_add, "update": boards.plan_update,
                        "reorder": boards.plan_reorder}[kind](overlay, supplied)
        observed.update(prepared["effect"]["observed"])
        overlay.stage(prepared["effect"])
        results.append({"index": index, "kind": kind, "outcome": "planned",
                        "paths": [change["path"] for change in prepared["effect"]["changes"]]})
    boards.check_catalog_placement(overlay, catalog_before, boards._load(overlay)[3])
    effect = effect_plan(local, overlay.updates, inputs=observed)
    paths = [change["path"] for change in effect["changes"]]
    ts.require(paths, "conflict", "Batch has no changes to apply.")
    return {"profile": PLAN, "outcome": "planned", "operation_id": request["operation_id"],
            "requested_by": request.get("requested_by"), "operations": results,
            "request": request, "effect": effect, "fingerprint": digest(ts.encoded(effect)), "paths": paths,
            **_effect_receipt(effect)}



def _plan_import(local, request, *, authority=None):
    from . import board_import, board_census
    operations = request["operations"]
    ts.require(all(op["kind"] == "import" for op in operations), "denied",
               "Native imports cannot share a batch with ordinary work changes.")
    undo = any(op["request"].get("mode") == "undo" for op in operations)
    ts.require(not undo or len(operations) == 1, "invalid", "Undo is one inverse-only batch.")
    board_import.eligibility(local, authority=authority)
    context = {"authority": authority} if authority is not None else {}
    destinations = sorted({path for op in operations for path in board_import.destinations(local, op["request"], **context)})
    census = board_census.freeze(local, destinations=destinations, **context)
    overlay = OverlayRoot(local.root)
    results = []
    for index, op in enumerate(operations):
        supplied = deepcopy(op["request"])
        if not undo:
            owner = supplied["owner_path"]
            ts.require(supplied["expected_config_sha256"] == digest(local.read("eutonos.boards.json"))
                       and supplied["expected_owner_sha256"] == digest(local.read(owner)),
                       "stale", "Each import must name the original physical owner/configuration hashes.")
            supplied["expected_config_sha256"] = digest(overlay.read("eutonos.boards.json"))
            supplied["expected_owner_sha256"] = digest(overlay.read(owner))
        prepared = board_import.prepare(overlay, supplied, operation_id=request["operation_id"], census=census, **context)
        overlay.updates.update(prepared["updates"])
        results.append({"index": index, "kind": "import", "mode": prepared["mode"], "outcome": "planned",
                        "paths": sorted(prepared["updates"])})
    if not undo:
        _, _, _, entries, _ = boards._load(overlay, strict=True)
        order = work_lifecycle.load_order(overlay)
        ts.require(order is not None, "missing", "Initialize native work order in the fixture before import.")
        work_lifecycle.views(overlay, entries, order, overlay.updates)
    effect = effect_plan(local, overlay.updates)
    effect["board_census"] = census
    effect["board_import"] = {"profile": board_import.EFFECT_V2,
        "operation_id": request["operation_id"], "request_sha256": digest(ts.encoded(request)),
        "modes": [op["request"]["mode"] for op in operations], "original_batch": deepcopy(request)}
    board_import.validate_metadata(effect["board_import"])
    paths = [change["path"] for change in effect["changes"]]
    ts.require(paths, "conflict", "Import has no changes to apply.")
    return {"profile": PLAN, "outcome": "planned", "operation_id": request["operation_id"],
            "requested_by": request.get("requested_by"), "operations": results,
            "request": request, "effect": effect, "fingerprint": digest(ts.encoded(effect)),
            "paths": paths, **_effect_receipt(effect)}

def apply_prepared(root, host, actor, prepared, request_id):
    prepared = ts.loads(ts.encoded(prepared).decode())
    ts.require(prepared.get("profile") == PLAN, "invalid", "Select a prepared write batch.")
    attempt = "write-batch-" + request_id.removeprefix("write-batch-")
    _check_import_request(prepared["effect"], prepared["request"])
    import_derivation = None
    if "board_import" in prepared["effect"]:
        from . import board_import
        from .effects import validate_plan
        request = normalize(prepared["request"])
        ts.require(request == prepared["request"], "conflict", "Typed application requires the normalized original request.")
        recovered = recover_submitted(root, host, actor, request, request_id)
        if recovered is not None:
            return recovered
        # Preserve current source/census rejection before requiring native intent.
        from .host import connect
        _, authority = connect(root, host, actor)
        from . import board_census
        context = ({"authority": authority} if isinstance(prepared["effect"].get("board_census"), dict)
                   and prepared["effect"]["board_census"].get("profile") == board_census.NATIVE_PROFILE else {})
        validate_plan(LocalRoot(root), prepared["effect"], **context)
        board_import.native_binding(LocalRoot(root), host, actor, attempt, prepared["request"])
        derived = plan(root, request, authority=authority)
        ts.require(prepared["effect"] == derived["effect"] and prepared["fingerprint"] == derived["fingerprint"]
                   and prepared["paths"] == derived["paths"], "denied",
                   "Supplied typed import effect differs from its source-derived plan.")
        verified_fingerprint = derived["fingerprint"]
        def import_derivation(value):
            ts.require(digest(ts.encoded(value)) == verified_fingerprint, "denied",
                       "Typed import changed after trusted source derivation.")
    # Derive metadata from the guarded effect, not caller-supplied plan labels.
    metadata = _effect_receipt(prepared["effect"])
    from .work_attribution import integration_proof
    for operation in prepared['request']['operations']:
        if operation['kind'] == 'work' and operation['request'].get('operation') == 'integrate':
            integration_proof(root, operation['request']['integration'])
    from .git_delivery_native import apply
    result = apply(root, host, actor, attempt, attempt + "-" + prepared["fingerprint"],
                   prepared["effect"], prepared["fingerprint"], prepared["paths"],
                   {"completed": "Atomic typed write batch verified.",
                    "next": "Continue from Current Tasks.", "pending": None},
                   verify=lambda: work_lifecycle.verify_effect(root, prepared["effect"]),
                   work_id=prepared["operation_id"],
                   session=prepared.get('requested_by', {}).get('value') if (prepared.get('requested_by') or {}).get('kind') == 'actual-session' else None,
                   **({"_import_derivation": import_derivation} if import_derivation is not None else {}))
    return {"profile": PROFILE, "operation_id": prepared["operation_id"],
            "requested_by": prepared.get("requested_by"),
            "observed_by": {"actor": actor, "attempt": attempt},
            "operations": [{**operation, "outcome": "complete"} for operation in prepared["operations"]],
            **metadata, **result}


def _check_import_request(effect, request):
    normalized = normalize(request)
    imports = [op["kind"] == "import" for op in normalized["operations"]]
    ts.require(("board_import" in effect) == any(imports), "denied",
               "Import request operations and effect tags must agree.")
    ts.require(not any(imports) or all(imports), "denied",
               "Native imports cannot share a batch with ordinary work changes.")
    if "board_import" not in effect:
        return
    from .board_import import validate_metadata
    retained = validate_metadata(effect["board_import"])
    ts.require(effect["board_import"]["request_sha256"] == digest(ts.encoded(request))
               and (retained is None or retained == request), "conflict",
               "Import application/recovery requires the exact frozen native batch request.")


def recover_submitted(root, host, actor, batch, request_id):
    """Recover a deterministic batch attempt before current-source replanning."""
    request = normalize(batch)
    attempt = "write-batch-" + request_id.removeprefix("write-batch-")
    from .host import connect
    from .managed_write import _effect_rows
    coordinator, _ = connect(root, host, actor)
    frozen = _effect_rows(coordinator, attempt)
    for row in frozen:
        effect = ts.loads(row["plan"])
        _check_import_request(effect, request)
        if "board_import" in effect:
            from . import board_import
            board_import.native_binding(LocalRoot(root), host, actor, attempt, request)
    from .git_delivery_native import recover
    def verify():
        ts.require(len(frozen) == 1, "conflict", "Typed batch recovery needs one effect.")
        work_lifecycle.verify_effect(root, ts.loads(frozen[0]["plan"]))
    result = recover(root, host, actor, attempt,
                     {"completed": "Recovered atomic typed write batch verified.",
                      "next": "Continue from Current Tasks.", "pending": None}, verify=verify)
    if not result["complete"]:
        return None
    from .host import connect
    from .managed_write import _effect_rows
    coordinator, _ = connect(root, host, actor)
    rows = _effect_rows(coordinator, attempt)
    ts.require(len(rows) == 1 and rows[0]["state"] == "completed", "conflict",
               "Completed batch is missing its verified journal effect.")
    effect = ts.loads(rows[0]["plan"])
    ts.require(digest(ts.encoded(effect)) == rows[0]["fingerprint"], "conflict",
               "Batch receipt journal fingerprint differs.")
    _check_import_request(effect, request)
    metadata = _effect_receipt(effect)
    operations = [{"index": index, "kind": operation["kind"],
                   "outcome": "recovered", "paths": []}
                  for index, operation in enumerate(request["operations"])]
    return {"profile": PROFILE, "operation_id": request["operation_id"],
            "requested_by": request.get("requested_by"),
            "observed_by": {"actor": actor, "attempt": attempt},
            "operations": operations, **metadata, **result}
