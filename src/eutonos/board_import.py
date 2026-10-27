"""Source-derived import planning for fixtures or connected native context.

Native context is a physical host precondition, not real-root import admission.
Journal undo and abort preserve independently bound intent and frozen images.
"""
from __future__ import annotations
from copy import deepcopy
import re
import sqlite3

from . import boards, tokenslang as ts, work_lifecycle as work
from .effects import LocalRoot, MARKER, MARKER_BYTES, decode, digest
from .source import GitView, safe_path

PROFILE = "eutonos.tokenslang.board-import.v1"
PROVENANCE = "eutonos.tokenslang.source-import.v1"
KEY = "source_import"
MAX_ITEMS = 128
REGISTRY = "docs/REGISTRY.json"
MAX_REGISTRY_BYTES = 1024 * 1024
EFFECT_V1 = "eutonos.tokenslang.board-import-effect.v1"
EFFECT_V2 = "eutonos.tokenslang.board-import-effect.v2"
MAX_BATCH_BYTES = 1024 * 1024


def validate_metadata(metadata):
    """Pure journal validation: never reads source, host or current owner bytes."""
    ts.require(isinstance(metadata, dict), "invalid", "Typed import metadata must be an object.")
    version = metadata.get("profile")
    ts.require(isinstance(version, str) and version in {EFFECT_V1, EFFECT_V2},
               "unsupported", "Unknown typed import metadata.")
    ts.shape(metadata, {"profile", "operation_id", "request_sha256", "modes"}
             | ({"original_batch"} if version == EFFECT_V2 else set()))
    ts.string(metadata["operation_id"])
    _sha(metadata["request_sha256"])
    modes = ts.sequence(metadata["modes"])
    ts.require(0 < len(modes) <= 16 and all(mode in ("register", "append", "reconcile", "undo") for mode in modes),
               "invalid", "Typed import modes exceed the supported bounded scope.")
    if version == EFFECT_V1:
        return None
    original = metadata["original_batch"]
    ts.require(len(ts.encoded(original)) <= MAX_BATCH_BYTES, "overflow", "Retained import batch exceeds 1MiB.")
    from .write_batch import normalize
    normalized = normalize(original)
    ts.require(normalized == original and normalized["operation_id"] == metadata["operation_id"]
               and digest(ts.encoded(normalized)) == metadata["request_sha256"],
               "conflict", "Retained normalized import batch differs from its binding.")
    actual_modes = []
    for op in normalized["operations"]:
        ts.require(op["kind"] == "import", "denied", "Retained import batch cannot contain ordinary operations.")
        request = op["request"]
        ts.require(request.get("profile") == PROFILE, "unsupported", "Unknown retained native import profile.")
        mode = request.get("mode")
        ts.require(mode in ("register", "append", "reconcile", "undo"), "invalid", "Unknown retained import mode.")
        actual_modes.append(mode)
        if mode == "undo":
            ts.shape(request, {"profile", "mode", "host", "actor", "request_id", "effect_id", "fingerprint"})
            for key in ("host", "actor", "request_id", "effect_id"):
                ts.string(request[key])
            _sha(request["fingerprint"])
            continue
        ts.shape(request, {"profile", "mode", "owner_path", "expected_config_sha256", "expected_owner_sha256"},
                 {"source", "items", "document", "expected_registry_sha256"})
        owner = safe_path(request["owner_path"])
        ts.require(owner.startswith("docs/campaigns/") and owner.endswith(".md"), "denied", "Unsafe retained import owner.")
        _sha(request["expected_config_sha256"])
        _sha(request["expected_owner_sha256"], absent=True)
        if "expected_registry_sha256" in request:
            _sha(request["expected_registry_sha256"])
        if "document" in request:
            ts.shape(request["document"], {"id", "role", "path"})
            for value in request["document"].values():
                ts.require(isinstance(value, str) and bool(value.strip()) and len(value) <= 1024,
                           "invalid", "Invalid retained Docs metadata.")
            ts.require(safe_path(request["document"]["path"]) == owner, "conflict", "Retained Docs path differs.")
        items = ts.sequence(request.get("items", []))
        ts.require(len(items) <= MAX_ITEMS and (items or mode == "register"), "invalid", "Invalid retained item selection.")
        if items or "source" in request:
            source = ts.shape(request.get("source"), {"commit", "path", "blob_oid", "sha256"})
            for key in ("commit", "blob_oid"):
                ts.require(isinstance(source[key], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source[key]),
                           "invalid", "Retained source identity must be an exact Git object ID.")
            safe_path(source["path"]); _sha(source["sha256"])
        for item in items:
            ts.shape(item, {"id", "sha256", "disposition"}, {"expected_item_revision", "fresh_id", "fresh_scope"})
            ts.string(item["id"]); _sha(item["sha256"])
            ts.require(item["disposition"] in ("historical", "held", "fresh-held"), "invalid", "Invalid retained disposition.")
            if "expected_item_revision" in item:
                ts.string(item["expected_item_revision"])
            if item["disposition"] == "fresh-held":
                ts.string(item.get("fresh_id"))
                ts.shape(item.get("fresh_scope"), {"title", "summary"})
                for value in item["fresh_scope"].values(): ts.string(value)
    ts.require(actual_modes == modes and ("undo" not in modes or modes == ["undo"]),
               "conflict", "Retained import modes differ or mix an inverse with original work.")
    return normalized


def _sha(value, *, absent=False):
    ts.require((absent and value is None) or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)),
               "invalid", "Expected an exact SHA256 digest.")


def native_binding(local, host, actor, request_id, original):
    """Require independent persistent native intent; never migrate a reader."""
    from . import write_retry
    ts.require(write_retry._request_id(local.root, actor, "batch", {"batch": original}) == request_id,
               "wrong-scope", "Original native request ID differs from retained intent/root.")
    db = write_retry._connect(host, readonly=True)
    ts.require(db is not None, "missing", "Original native batch binding store is absent.")
    try:
        binding = db.execute("SELECT root,actor,payload_sha256 FROM batch_bindings WHERE id=?", (request_id,)).fetchone()
    except sqlite3.Error as exc:
        raise ts.ReadError("unavailable", "Original native batch binding store is unavailable.") from exc
    finally:
        db.close()
    ts.require(binding is not None and tuple(binding) == (str(local.root), actor, digest(ts.encoded(original))),
               "conflict", "Original import lacks its exact durable native batch admission binding.")


def _registry_bytes(physical):
    try:
        with physical._read_stream(REGISTRY) as stream:
            original = stream.read(MAX_REGISTRY_BYTES + 1)
    except FileNotFoundError:
        original = None
    ts.require(original is not None and len(original) <= MAX_REGISTRY_BYTES,
               "overflow", "A bounded existing Docs registry is required.")
    return original


def _document_registration(local, request, owner):
    """Merge explicit Docs identity into the overlay, bound to physical bytes."""
    original = _registry_bytes(LocalRoot(local.root, excluded=local.excluded))
    raw = getattr(local, "updates", {}).get(REGISTRY, original)
    ts.require(isinstance(raw, bytes) and len(raw) <= MAX_REGISTRY_BYTES,
               "overflow", "Staged Docs registry exceeds 1MiB.")
    registry = ts.loads(raw)
    ts.require(isinstance(registry, dict) and registry.get("schema") == "eutonos.documentation-bootstrap.v0"
               and isinstance(registry.get("documents"), list), "invalid", "Invalid Docs registry schema.")
    seen = {key: set() for key in ("id", "role", "path")}
    existing = None
    for row in registry["documents"]:
        ts.require(isinstance(row, dict), "invalid", "Invalid Docs registry row.")
        for key in seen:
            value = row.get(key)
            ts.require(isinstance(value, str) and bool(value.strip()) and len(value) <= 1024,
                       "invalid", "Docs identity fields must be bounded nonempty strings.")
            ts.require(value not in seen[key], "ambiguous", "Duplicate Docs registry " + key + ".")
            seen[key].add(value)
        safe_path(row["path"])
        if row["path"] == owner:
            existing = row
    supplied = request.get("document")
    if "document" in request:
        ts.shape(supplied, {"id", "role", "path"})
        for value in supplied.values():
            ts.require(isinstance(value, str) and bool(value.strip()) and len(value) <= 1024,
                       "invalid", "Select bounded nonempty Docs identity fields.")
        ts.require(safe_path(supplied["path"]) == owner, "conflict", "Docs metadata must name the exact import owner.")
    if supplied is not None or "expected_registry_sha256" in request or existing is None:
        expected = request.get("expected_registry_sha256")
        ts.require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected)
                   and expected == digest(original), "stale", "Select the exact physical Docs registry SHA256.")
    if existing is not None:
        ts.require(supplied is None or all(existing[key] == supplied[key] for key in seen),
                   "conflict", "Preserve the existing Docs identity and role.")
        return {}
    ts.require(supplied is not None, "missing", "New Docs owners require explicit document metadata.")
    ts.require(all(supplied[key] not in seen[key] for key in seen), "ambiguous", "Docs identity or role already exists.")
    registry["documents"].append(deepcopy(supplied))
    encoded = boards._json(registry) + b"\n"
    ts.require(len(encoded) <= MAX_REGISTRY_BYTES, "overflow", "Registered Docs payload exceeds 1MiB.")
    return {REGISTRY: encoded}


def fixture(local):
    ts.require(local.read(MARKER) == MARKER_BYTES, "denied", "Native import is admitted only in marked disposable fixtures.")


def check_context(local, authority):
    """Check connected native context without consulting marker/source/census."""
    from .host import ProjectAuthority, _validate
    ts.require(isinstance(authority, ProjectAuthority), "wrong-scope",
               "Native import requires connected project authority.")
    ts.require(str(authority.root.root) == str(local.root) and authority.root.identity == local.identity,
               "wrong-scope", "Native import authority belongs to another root.")
    authority.check(())
    admission = ts.loads(authority.receipt.decode("utf-8"))
    _validate(admission, local)
    ts.require(admission["actor"] == authority.actor, "wrong-scope", "Native import authority actor differs.")


def eligibility(local, *, authority=None):
    """Select planning eligibility from physical marker and trusted context."""
    physical = LocalRoot(local.root, excluded=local.excluded)
    if authority is not None:
        check_context(physical, authority)
    marker = physical.read(MARKER)
    ts.require(marker == MARKER_BYTES or (marker is None and authority is not None),
               "denied", "Import requires a disposable fixture marker or connected native context.")
    return marker == MARKER_BYTES


def form(root=".", *, authority=None):
    local = LocalRoot(root)
    marked = eligibility(local, authority=authority)
    return {"profile": PROFILE, "qualification": ("Fixture-only; no real native import admission." if marked else
            "Connected native import context; select an explicitly admitted source and disposition."),
            "batch_kind": "import", "modes": ["register", "append", "reconcile", "undo"],
            "request": {"profile": PROFILE, "mode": "register", "owner_path": "docs/campaigns/SELECTED.md",
                        "expected_config_sha256": digest(local.read("eutonos.boards.json")),
                        "expected_owner_sha256": None,
                        "expected_registry_sha256": digest(_registry_bytes(local)),
                        "document": {"id": "<selected stable Docs ID>", "role": "<selected unique role>",
                                     "path": "docs/campaigns/SELECTED.md"},
                        "source": {"commit": "<exact local commit>", "path": "<source owner>",
                                   "blob_oid": "<Git object ID>", "sha256": "<source blob SHA256>"},
                        "items": [{"id": "<original issued ID>", "sha256": "<encoded source item SHA256>",
                                   "disposition": "held"}]},
            "undo_fields": ["host", "actor", "request_id", "effect_id", "fingerprint"]}


def _journal(local, request, *, authority=None):
    eligibility(local, authority=authority)
    supplied_authority = authority
    from .host import connect
    coordinator, connected = connect(local.root, request["host"], request["actor"])
    if authority is not None:
        check_context(local, authority)
        ts.require(str(authority.admission.root) == str(connected.admission.root)
                   and authority.actor == connected.actor and authority.receipt == connected.receipt,
                   "wrong-scope", "Undo host or actor differs from its connected import context.")
    authority = connected
    attempt = "write-batch-" + ts.string(request["request_id"]).removeprefix("write-batch-")
    with coordinator._db() as db:
        state = db.execute("SELECT root,actor FROM attempts WHERE id=?", (attempt,)).fetchone()
        rows = db.execute("SELECT id,state,fingerprint,plan FROM effects WHERE attempt=?", (attempt,)).fetchall()
        ts.require(state is not None and state["root"] == coordinator._root(authority) and state["actor"] == authority.actor,
                   "wrong-scope", "Undo requires the exact original root and actor.")
        ts.require(len(rows) == 1 and rows[0]["state"] == "completed"
                   and rows[0]["id"] == request["effect_id"] and rows[0]["fingerprint"] == request["fingerprint"],
                   "conflict", "Undo requires one verified completed import journal effect.")
        effect = ts.loads(rows[0]["plan"])
        ts.require(digest(ts.encoded(effect)) == request["fingerprint"] and "board_census" in effect,
                   "conflict", "Undo journal fingerprint or import census is absent.")
        ts.require(not db.execute("SELECT 1 FROM reservations WHERE root=?", (coordinator._root(authority),)).fetchone(),
                   "conflict", "Undo requires no active reservations in the fixture root.")
    from .effects import PROFILE as EFFECT_PROFILE
    ts.require(effect.get("profile") == EFFECT_PROFILE and effect.get("root_identity") == list(local.identity)
               and effect["board_census"].get("root_identity") == list(local.identity),
               "wrong-scope", "Original import root identity differs from this fixture root.")
    from . import board_census
    census = ts.shape(effect["board_census"], board_census.KEYS)
    ts.require(census["profile"] in (board_census.PROFILE, board_census.NATIVE_PROFILE),
               "conflict", "Original import census profile differs.")
    if census["profile"] == board_census.NATIVE_PROFILE:
        check_context(local, supplied_authority)
    else:
        fixture(LocalRoot(local.root, excluded=local.excluded))
    binding = effect.get("board_import", {})
    original = validate_metadata(binding)
    ts.require("undo" not in binding["modes"],
               "denied", "Undo requires an original typed import effect, not a guarded ordinary edit.")
    from . import write_retry, write_batch
    db = write_retry._connect(request["host"], readonly=True)
    ts.require(db is not None, "missing", "Original native batch receipt is absent.")
    try:
        row = db.execute("SELECT root,actor,kind,payload,state FROM requests WHERE id=?", (attempt,)).fetchone()
    finally:
        db.close()
    if row is not None:
        ts.require(row[0] == str(local.root) and row[1] == authority.actor and row[2] == "batch"
                   and row[4] == "complete" and len(row[3].encode("utf-8")) <= MAX_BATCH_BYTES,
                   "conflict", "Surviving original native receipt differs or is incomplete.")
        receipt_batch = write_batch.normalize(ts.loads(row[3])["batch"])
        ts.require(original is None or receipt_batch == original, "conflict", "Retained batch and native receipt disagree.")
        original = receipt_batch
    ts.require(original is not None, "missing", "Legacy import undo requires its retained completed native receipt.")
    ts.require(all(op["kind"] == "import" for op in original["operations"])
               and original["operation_id"] == binding["operation_id"]
               and digest(ts.encoded(original)) == binding["request_sha256"]
               and [op["request"]["mode"] for op in original["operations"]] == binding["modes"],
               "conflict", "Original typed import payload differs from its frozen effect binding.")
    native_binding(local, request["host"], authority.actor, attempt, original)
    for op in original["operations"]:
        owner = safe_path(op["request"]["owner_path"])
        ts.require(op["request"].get("profile") == PROFILE and owner.startswith("docs/campaigns/") and owner.endswith(".md"),
                   "denied", "Original import owner scope is unsafe.")
    ts.require(len(effect["changes"]) <= 16 and isinstance(effect["observed"], dict)
               and len(effect["observed"]) <= 64, "overflow", "Original import journal exceeds effect bounds.")
    allowed = {op["request"]["owner_path"] for op in original["operations"]}
    allowed.update({"eutonos.boards.json", work.PATH, work.CURRENT, work.NEXT, work.DONE,
                    work.ORDER, work.DONE_CAMPAIGNS, "docs/CHANGELOG.md", "docs/REGISTRY.json"})
    ts.require(all(change["path"] in allowed for change in effect["changes"]), "denied",
               "Original import journal contains a path outside its typed owner/view scope.")
    seen = set()
    for change in effect["changes"]:
        ts.shape(change, {"path", "before", "backup", "after"})
        name = safe_path(change["path"])
        ts.require(name not in seen and name in effect["observed"]
                   and effect["observed"][name] == change["before"]
                   and digest(decode(change["backup"])) == change["before"],
                   "invalid", "Original journal backup/preconditions differ.")
        seen.add(name); decode(change["after"])
    return effect


def validate_abort_journal(coordinator, authority, db, row, *, _parent=False):
    """Validate frozen interrupted-import provenance without source/census reads.

    The already-open coordinator transaction supplies the journal and attempt.
    Native binding is independently read from that authority's actual host.
    """
    from .host import ProjectAuthority
    from . import board_census, write_retry
    from .effects import PROFILE as EFFECT_PROFILE
    ts.require(isinstance(authority, ProjectAuthority)
               and coordinator.path == authority.admission.path("coordinator.sqlite"),
               "wrong-scope", "Import abort requires its connected native host authority.")
    local = authority.root
    check_context(local, authority)
    state = db.execute("SELECT root,actor FROM attempts WHERE id=?", (row["attempt"],)).fetchone()
    ts.require(state is not None and state["root"] == coordinator._root(authority)
               and state["actor"] == authority.actor, "wrong-scope", "Import abort journal root or actor differs.")
    ts.require(db.execute("SELECT count(*) FROM effects WHERE attempt=?", (row["attempt"],)).fetchone()[0] == 1,
               "conflict", "Import abort requires one frozen journal effect.")
    effect = ts.loads(row["plan"])
    ts.shape(effect, {"profile", "root_identity", "observed", "changes", "board_census", "board_import"})
    ts.require(digest(ts.encoded(effect)) == row["fingerprint"], "conflict", "Import abort journal fingerprint differs.")
    ts.require(effect["profile"] == EFFECT_PROFILE and effect["root_identity"] == list(local.identity),
               "wrong-scope", "Import abort effect root identity differs.")
    census = ts.shape(effect["board_census"], board_census.KEYS)
    ts.require(census["profile"] in (board_census.PROFILE, board_census.NATIVE_PROFILE)
               and census["root_identity"] == list(local.identity),
               "wrong-scope", "Import abort frozen census root identity differs.")
    if census["profile"] == board_census.PROFILE:
        fixture(local)
    original = validate_metadata(effect["board_import"])
    host = authority.admission.root
    queue = write_retry._connect(host, readonly=True)
    ts.require(queue is not None, "missing", "Import abort native intent store is absent.")
    try:
        receipt = queue.execute("SELECT root,actor,kind,payload FROM requests WHERE id=?", (row["attempt"],)).fetchone()
    finally:
        queue.close()
    if receipt is not None:
        ts.require(receipt[0] == str(local.root) and receipt[1] == authority.actor and receipt[2] == "batch"
                   and len(receipt[3].encode("utf-8")) <= MAX_BATCH_BYTES,
                   "conflict", "Import abort surviving native intent differs.")
        from .write_batch import normalize
        retained = normalize(ts.loads(receipt[3])["batch"])
        ts.require(original is None or retained == original, "conflict", "Import abort retained intent differs.")
        original = retained
    ts.require(original is not None, "missing", "Legacy import abort requires its retained native intent.")
    # Legacy metadata needs the same full original-intent validation as v2.
    validate_metadata({**effect["board_import"], "profile": EFFECT_V2, "original_batch": original})
    native_binding(local, host, authority.actor, row["attempt"], original)
    modes = effect["board_import"]["modes"]
    ts.require(not _parent or "undo" not in modes, "denied", "Import abort inverse parent must be an original import.")
    parent = None
    if modes == ["undo"]:
        request = original["operations"][0]["request"]
        ts.require(LocalRoot(request["host"]).root == host and request["actor"] == authority.actor,
                   "wrong-scope", "Import abort inverse host or actor differs.")
        parent = db.execute("SELECT * FROM effects WHERE id=?", (request["effect_id"],)).fetchone()
        ts.require(parent is not None and parent["state"] == "completed"
                   and parent["attempt"] == "write-batch-" + request["request_id"].removeprefix("write-batch-")
                   and parent["fingerprint"] == request["fingerprint"],
                   "conflict", "Import abort inverse parent journal differs.")
        parent_effect = validate_abort_journal(coordinator, authority, db, parent, _parent=True)
        allowed = {change["path"] for change in parent_effect["changes"]}
    else:
        allowed = {op["request"]["owner_path"] for op in original["operations"]}
        allowed.update({"eutonos.boards.json", work.PATH, work.CURRENT, work.NEXT, work.DONE,
                        work.ORDER, work.DONE_CAMPAIGNS, "docs/CHANGELOG.md", REGISTRY})
    changes = ts.sequence(effect["changes"])
    ts.require(0 < len(changes) <= 16 and isinstance(effect["observed"], dict)
               and len(effect["observed"]) <= 64, "overflow", "Import abort journal exceeds effect bounds.")
    for name, expected in effect["observed"].items():
        ts.require(safe_path(name) in allowed, "denied", "Import abort observed scope differs.")
        _sha(expected, absent=True)
    seen = set()
    for change in changes:
        ts.shape(change, {"path", "before", "backup", "after"})
        name = safe_path(change["path"])
        ts.require(name in allowed and name not in seen and name in effect["observed"]
                   and change["before"] == effect["observed"][name]
                   and digest(decode(change["backup"])) == change["before"],
                   "denied", "Import abort frozen backup or scope differs.")
        if not _parent:
            reservation = db.execute("SELECT baseline FROM reservations WHERE root=? AND path=? AND attempt=?",
                                     (coordinator._root(authority), name, row["attempt"])).fetchone()
            ts.require(reservation is not None and reservation["baseline"] == change["before"],
                       "conflict", "Import abort backup differs from its independently reserved baseline.")
        seen.add(name)
        decode(change["after"])
    if parent is not None:
        expected = {change["path"]: {"path": change["path"], "before": digest(decode(change["after"])),
                    "backup": change["after"], "after": change["backup"]} for change in parent_effect["changes"]}
        ts.require({change["path"]: change for change in changes} == expected,
                   "conflict", "Import abort inverse images differ from its original journal.")
    return effect


def destinations(local, request, *, authority=None):
    if request.get("mode") == "undo":
        return [change["path"] for change in _journal(local, request, authority=authority)["changes"]]
    return [request["owner_path"], REGISTRY]


def prepare(local, request, *, operation_id, census, authority=None):
    eligibility(local, authority=authority)
    ts.require(request.get("profile") == PROFILE, "unsupported", "Unknown native import profile.")
    mode = request.get("mode")
    if mode == "undo":
        ts.shape(request, {"profile", "mode", "host", "actor", "request_id", "effect_id", "fingerprint"})
        ts.require(operation_id != request["request_id"] and operation_id != request["request_id"].removeprefix("write-batch-"),
                   "conflict", "Undo needs a fresh logical batch operation ID.")
        effect = _journal(local, request, authority=authority)
        ts.require(operation_id != effect["board_import"]["operation_id"], "conflict", "Undo requires a new logical operation ID.")
        updates = {}
        for change in effect["changes"]:
            ts.require(digest(local.read(change["path"])) == digest(decode(change["after"])),
                       "stale", "Undo target changed after the original import: " + change["path"])
            updates[change["path"]] = decode(change["backup"])
        return {"updates": updates, "mode": mode, "undo_of": request["effect_id"]}
    ts.shape(request, {"profile", "mode", "owner_path", "expected_config_sha256", "expected_owner_sha256"},
             {"source", "items", "document", "expected_registry_sha256"})
    ts.require(mode in {"register", "append", "reconcile"}, "invalid", "Unknown native import mode.")
    owner = safe_path(request["owner_path"])
    ts.require(owner.startswith("docs/campaigns/") and owner.endswith(".md"), "denied", "Select a campaign Markdown owner.")
    config_raw, owners, raws, entries, _ = boards._load(local, strict=True)
    ts.require(owners is not None and digest(config_raw) == request["expected_config_sha256"],
               "stale", "Board configuration differs from the explicit import precondition.")
    raw = local.read(owner)
    ts.require(digest(raw) == request["expected_owner_sha256"], "stale", "Import owner differs from its explicit precondition.")
    ts.require(mode == "register" or owner in owners, "denied", "Register this owner before append or reconciliation.")
    board, matches = boards._parse(raw, owner) if raw is not None else (None, [])
    board = deepcopy(board) if board else {"profile": boards.BOARD, "items": []}
    selections = request.get("items", [])
    ts.require(mode != "register" or not board["items"] or selections, "denied",
               "Existing native rows require explicit source-verified reconciliation when registering.")
    ts.require(isinstance(selections, list) and len(selections) <= MAX_ITEMS and (selections or mode == "register"),
               "invalid", "Select 1..128 source records, or registration only.")
    source_items, metadata = {}, None
    if selections:
        source = request.get("source")
        ts.shape(source, {"commit", "path", "blob_oid", "sha256"})
        ts.require(isinstance(source["commit"], str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source["commit"]),
                   "invalid", "Import source must be an exact local commit, not a ref.")
        view = GitView(local.root, source["commit"])
        source_path = safe_path(source["path"])
        listing = view._git("ls-tree", "-z", view.commit, "--", source_path).split(b"\0")
        listing = [row for row in listing if row]
        ts.require(len(listing) == 1, "missing", "Exact source blob is absent.")
        oid = listing[0].split(b"\t", 1)[0].split()[-1].decode("ascii")
        ts.require(oid == source["blob_oid"] and int(view._git("cat-file", "-s", oid)) <= 1024 * 1024,
                   "overflow", "Source blob identity/size exceeds bounded import selection.")
        body, metadata = view.read(source_path)
        ts.require(len(body) <= 1024 * 1024, "overflow", "Import source owner exceeds 1MiB.")
        ts.require(metadata["git_blob"] == source["blob_oid"] and metadata["sha256"] == source["sha256"]
                   and view.commit == source["commit"], "stale", "Exact source commit/blob identity differs.")
        parsed, _ = boards._parse(body, source["path"])
        ts.require(parsed is not None, "missing", "Source owner has no native board fence.")
        source_items = {item["id"]: item for item in parsed["items"]}
        ts.require(len(source_items) == len(parsed["items"]), "ambiguous", "Source has duplicated IDs.")
    selected, written = set(), []
    current = boards._validate_graph(entries)
    if owner not in owners:
        for existing_item in board["items"]:
            current[existing_item["id"]] = (owner, existing_item)
    for choice in selections:
        ts.shape(choice, {"id", "sha256", "disposition"}, {"expected_item_revision", "fresh_id", "fresh_scope"})
        ts.require(choice["id"] in source_items and choice["id"] not in selected, "missing", "Select each exact source ID once.")
        selected.add(choice["id"])
        original = source_items[choice["id"]]
        ts.require(digest(ts.encoded(original)) == choice["sha256"], "stale", "Source item digest differs.")
        disposition = choice["disposition"]
        terminal = original["status"] in work.TERMINAL
        ts.require(disposition in {"historical", "held", "fresh-held"}
                   and (disposition == "historical") == terminal, "invalid", "Source terminals are historical; other source work stays held.")
        item = deepcopy(original)
        prior = None
        if disposition == "fresh-held":
            ts.string(choice.get("fresh_id")); ts.shape(choice.get("fresh_scope"), {"title", "summary"})
            ts.require(choice["fresh_id"] != original["id"], "conflict", "Standalone follow-up needs a fresh ID.")
            item.update(id=choice["fresh_id"], type="slice", **deepcopy(choice["fresh_scope"]))
            item.pop("parent_id", None)
        identity = item["id"]
        existing = current.get(identity)
        occupancy = census["by_id"].get(identity)
        if mode == "reconcile" or (mode == "register" and existing is not None):
            ts.require(existing is not None and existing[0] == owner and occupancy is not None
                       and occupancy["path"] == owner, "conflict", "Reconcile the one exact existing owner identity.")
            prior = deepcopy(existing[1])
            ts.require(KEY not in prior.get("extensions", {}) and work.item_revision(prior) == choice.get("expected_item_revision"),
                       "stale", "Reconciliation requires exact unimported local item revision.")
        else:
            ts.require(existing is None and occupancy is None, "ambiguous", "Original or follow-up ID already occupies a native owner.")
        item["status"] = original["status"] if terminal else "deferred"
        item["extensions"] = {KEY: {"profile": PROVENANCE, "disposition": disposition,
            "source_id": original["id"], "source_item": deepcopy(original), "source_item_sha256": choice["sha256"],
            "source": deepcopy(request["source"]), "operation_id": operation_id,
            "local_before": prior}}
        boards._item(item)
        if prior is None:
            board["items"].append(item)
        else:
            board["items"] = [item if old["id"] == identity else old for old in board["items"]]
        current[identity] = (owner, item)
        written.append(identity)
    if mode == "register":
        ts.require(all(KEY in i.get("extensions", {}) for i in board["items"]), "denied",
                   "Registration must explicitly reconcile every existing native row; no stale live rows are admitted.")
    if owner not in owners:
        owners.append(owner)
    entries[owner] = board
    boards._validate_graph(entries)
    if matches:
        desired = boards._replace(raw, matches[0], board)
    else:
        prose = raw if raw is not None else b"# Imported native campaign owner\n\nSource history only; no execution or acceptance is granted.\n"
        desired = prose + (b"\n" if prose.endswith(b"\n") else b"\n\n") + b"```tokenslang-board\n" + boards._json(board) + b"\n```\n"
    updates = {owner: desired, **_document_registration(local, request, owner)}
    if owners != ts.loads(config_raw)["owners"]:
        updates["eutonos.boards.json"] = boards._json({"profile": boards.CONFIG, "owners": owners}) + b"\n"
    return {"updates": updates, "mode": mode, "written_ids": written}
