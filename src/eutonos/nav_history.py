"""Immutable NAV history objects with a constant-size manifest and paged reads."""
from . import tokenslang as ts
from .effects import digest

INDEX = "eutonos.tokenslang.nav-history.v2"
NODE = "eutonos.tokenslang.nav-history-node.v1"


def load(root, ref, prefix):
    ts.shape(ref, {"path", "sha256"})
    name = ref["path"]
    import re
    ts.require(isinstance(name, str) and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}\.json", name),
               "invalid", "Invalid NAV archive reference.")
    raw = root.read(name)
    ts.require(raw is not None and digest(raw) == ref["sha256"], "stale", "NAV archive missing or hash mismatch: " + name)
    return ts.loads(raw.decode("utf-8"))


def prepare(root, history_path, old, run_id):
    """No writes. Return archive outputs and their exact input fingerprints."""
    raw = root.read(history_path)
    prefix = history_path.removesuffix(".json") + "_"
    observed = {history_path: digest(raw)}
    updates = {}
    def put(value, name=None):
        body = ts.encoded(value) + b"\n"
        path = name or prefix + digest(body) + ".json"
        before = root.read(path)
        ts.require(before is None or before == body, "conflict", "Immutable NAV archive differs: " + path)
        observed[path] = digest(before)
        if before is None:
            updates[path] = body
        return {"path": path, "sha256": digest(body)}
    history = ts.loads(raw.decode("utf-8")) if raw else {"profile": INDEX, "head": None, "legacy": None, "count": 0}
    ts.shape(history, {"profile", "head", "legacy", "count"})
    ts.require(history["profile"] == INDEX, "unsupported", "Unknown NAV history index.")
    if history["legacy"]:
        legacy = load(root, history["legacy"], prefix) if history["legacy"]["path"] not in updates else ts.loads(updates[history["legacy"]["path"]].decode())
        ts.require(all(c["runID"] != run_id for c in legacy["cycles"]), "conflict", "Archived run ID cannot be reused.")
        observed[history["legacy"]["path"]] = digest(root.read(history["legacy"]["path"]))
    # Deterministic node names preserve uniqueness without scanning all prior runs.
    proposed_name = prefix + digest(run_id.encode()) + ".json"
    ts.require(root.read(proposed_name) is None, "conflict", "Archived run ID cannot be reused.")
    observed[proposed_name] = None
    if history["head"]:
        load(root, history["head"], prefix)
        observed[history["head"]["path"]] = history["head"]["sha256"]
    if old:
        cycle_ref = put(old)
        node = {"profile": NODE, "runID": old["runID"], "cycle": cycle_ref, "previous": history["head"]}
        node_ref = put(node, prefix + digest(old["runID"].encode()) + ".json")
        history = {**history, "head": node_ref, "count": history["count"] + 1}
    updates[history_path] = ts.encoded(history) + b"\n"
    return updates, observed


def page(root, history_path, limit=10, cursor=None):
    ts.require(type(limit) is int and 1 <= limit <= 64, "invalid", "History page size must be 1..64; use continuation for more.")
    raw = root.read(history_path)
    if raw is None:
        return {"outcome": "ok", "items": [], "continuation": None}
    value = ts.loads(raw.decode("utf-8"))
    prefix = history_path.removesuffix(".json") + "_"
    stamp = digest(raw)
    ts.require(value["profile"] == INDEX, "unsupported", "Unknown history profile.")
    legacy = load(root, value["legacy"], prefix) if value["legacy"] else {"cycles": []}
    head = value["head"]
    offset = 0
    if cursor:
        ts.shape(cursor, {"snapshot", "node", "legacy_offset"})
        ts.require(cursor["snapshot"] == stamp, "stale", "History changed; begin a new listing.")
        head, offset = cursor["node"], cursor["legacy_offset"]
        ts.require(type(offset) is int and 0 <= offset <= len(legacy["cycles"]), "invalid", "Invalid legacy cursor.")
    items = []
    def item(cycle, ref):
        return {"runID": cycle["runID"], "status": cycle["status"], "mode": cycle["mode"],
                "sourceCommit": cycle["sourceCommit"], "receipts": len(cycle["receipts"]), "archive": ref}
    while head and len(items) < limit:
        node = load(root, head, prefix)
        ts.require(node.get("profile") == NODE, "invalid", "Invalid history node.")
        cycle = load(root, node["cycle"], prefix)
        entry = item(cycle, node["cycle"])
        if len(ts.encoded(items + [entry])) > 28000:
            break
        items.append(entry)
        head = node["previous"]
    cycles = list(reversed(legacy["cycles"]))
    while not head and offset < len(cycles) and len(items) < limit:
        entry = item(cycles[offset], {"path": value["legacy"]["path"], "legacy_runID": cycles[offset]["runID"]})
        if len(ts.encoded(items + [entry])) > 28000:
            break
        items.append(entry)
        offset += 1
    ts.require(digest(root.read(history_path)) == stamp, "stale", "History changed during listing.")
    return {"outcome": "ok", "items": items, "continuation": {"snapshot": stamp, "node": head, "legacy_offset": offset} if head or offset < len(cycles) else None}
