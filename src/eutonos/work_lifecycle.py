"""Guarded work transitions and derived live/done views.

Board records own meaning; the work order owns priority. Generated Markdown is
never another editable task database. Queued transitions retain item revisions,
so unrelated edits can be rebased but a newer decision is never overwritten.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import posixpath
import re

from . import boards, tokenslang as ts
from .effects import LocalRoot, digest, plan as effect_plan

PROFILE = "eutonos.work-lifecycle.v1"
CONFIG = "eutonos.work-order.v1"
PATH = "eutonos.work.json"
TERMINAL = {"completed", "cancelled", "superseded"}
WORK = {"campaign", "stage", "slice", "quick", "task"}
# A feature or capability keeps its ID for life: planned or deferred it waits on a
# Future list, active or blocked it is queued work (Current Tasks), completed it is
# a Current feature or capability. Only the active middle belongs to the work queue.
CATALOG = frozenset(boards.CATEGORY_TYPES)
CATALOG_ACTIVE = frozenset({"active", "blocked"})
CURRENT = "docs/CURRENT_TASKS.md"
NEXT = "docs/NEXT_WORK.md"
DONE = "docs/DONE_WORK.md"
ORDER = "docs/WORK_ORDER.md"
DONE_CAMPAIGNS = "docs/campaigns/done/README.md"
START = "<!-- eutonos:work-history:start -->"
END = "<!-- eutonos:work-history:end -->"


def item_revision(item):
    return digest(ts.encoded({k: v for k, v in item.items()
        if k not in {"owner_path", "order", "display_type", "child_counts", "work_lane", "priority", "revision", "catalog_section", "attribution"}}))


ADMISSION = ("Saved priority only: agents start work when the owner assigns it. An assigned scope (a campaign, stages or a "
             "row) needs no further per-stage approval. Owner-only actions (owner installs and trials, spending, account or "
             "security changes, restarts, releases) still need the owner.")


def entry_ids(entry):
    """IDs held by one lane entry: a plain ID string or a {"together": [...]} group."""
    if isinstance(entry, str) and entry:
        return [entry]
    if (isinstance(entry, dict) and set(entry) == {"together"} and isinstance(entry["together"], list)
            and len(entry["together"]) >= 2 and all(isinstance(i, str) and i for i in entry["together"])):
        return list(entry["together"])
    raise ts.ReadError("invalid", "Work order lanes must contain stable IDs or together groups of two or more IDs.")


def flat(entries):
    """Every ID in a lane, row by row, group members in their listed order."""
    return [i for entry in entries for i in entry_ids(entry)]


def order_ids(order):
    return flat(order["current"]) + flat(order["next"])


def load_order(local):
    raw = local.read(PATH)
    if raw is None:
        return None
    value = ts.loads(raw.decode("utf-8"))
    ts.shape(value, {"profile", "current", "next"})
    ts.require(value["profile"] == CONFIG, "unsupported", "Unknown work order profile.")
    ids = []
    for lane in ("current", "next"):
        ts.require(isinstance(value[lane], list), "invalid", "Work order lanes must contain stable IDs or together groups.")
        ids.extend(flat(value[lane]))
    ts.require(len(ids) == len(set(ids)), "invalid", "A work ID can appear only once in Current Tasks or Next Work.")
    return value


def queued(item):
    """True for a feature or capability being worked: it is on Current Tasks, not on a Future list."""
    return (item.get("type") in CATALOG and item.get("status") in CATALOG_ACTIVE and not item.get("parent_id")
            and "source_import" not in item.get("extensions", {}))


def _live(items):
    return {i["id"]: i for i in items if queued(i) or (
            i["type"] in WORK and not i.get("parent_id")
            and i["status"] not in TERMINAL and not i.get("extensions", {}).get("archived")
            and "source_import" not in i.get("extensions", {}))}


def _live_stages(items):
    return {i["id"]: i for i in items if i["type"] == "stage" and i.get("parent_id")
            and i["status"] not in TERMINAL and not i.get("extensions", {}).get("archived")
            and "source_import" not in i.get("extensions", {})}


def _without(entries, identity):
    """Drop one ID from a lane; a group left with one item becomes a plain row."""
    result = []
    for entry in entries:
        ids = [i for i in entry_ids(entry) if i != identity]
        if len(ids) >= 2:
            result.append({"together": ids} if isinstance(entry, dict) else ids[0])
        elif ids:
            result.append(ids[0])
    return result


def ordered(value, items):
    """Remove finished references, and include every new top-level work record.

    A lane holds rows: an ID string, or {"together": [IDs]}.  A row may name a
    top-level item or a live campaign stage.  Stage rows are never added here.
    """
    live = _live(items)
    keep = {**_live_stages(items), **live}
    result = {"profile": CONFIG, "current": [], "next": []}
    seen = set()
    for lane in ("current", "next"):
        for entry in (value or {}).get(lane, []):
            ids = [i for i in entry_ids(entry) if i in keep and i not in seen]
            seen.update(ids)
            if len(ids) >= 2:
                result[lane].append({"together": ids})
            elif ids:
                result[lane].append(ids[0])
    for identity, item in live.items():
        if identity not in seen:
            result["current" if item["status"] in {"active", "blocked"} else "next"].append(identity)
    return result


def view_order(rich, items):
    """Consumer shape: `current`/`next` stay flat top-level IDs; `rows` carries stage rows and groups."""
    top = _live(items)
    return {"profile": rich["profile"],
            **{lane: [i for i in flat(rich[lane]) if i in top] for lane in ("current", "next")},
            "rows": {lane: deepcopy(rich[lane]) for lane in ("current", "next")}}


def row_summary(work_order, items):
    """The current row (first row of Current Tasks) as compact item rows, plus the admission rule."""
    rows = ((work_order or {}).get("rows") or {}).get("current") or []
    if not rows:
        return None
    by_id = {i["id"]: i for i in items}
    def brief(identity):
        item = by_id.get(identity) or {}
        parent = by_id.get(item.get("parent_id")) or {}
        return {"id": identity, "type": item.get("type"), "title": item.get("title"), "status": item.get("status"),
                **({"campaign": parent.get("title")} if parent else {})}
    ids = entry_ids(rows[0])
    return {"number": 1, "together": len(ids) > 1, "items": [brief(i) for i in ids], "admission": ADMISSION}


def guard_rows(order, by_id, rank):
    """Refuse stage orders and together groups the campaign sequence cannot honor.

    `by_id` maps ID to item; `rank` maps ID to its position on the boards.
    """
    last = {}
    for identity in order_ids(order):
        item = by_id[identity]
        if item["type"] != "stage" or not item.get("parent_id"):
            continue
        parent = item["parent_id"]
        if parent in last:
            ts.require(rank[identity] > rank[last[parent]], "invalid",
                       f"Stage {identity} is placed after {last[parent]} but comes before it in its campaign; "
                       "stages of one campaign keep the campaign's own order.")
        last[parent] = identity
    for lane in ("current", "next"):
        for entry in order[lane]:
            if isinstance(entry, dict):
                for identity in entry["together"]:
                    ts.require(by_id[identity].get("extensions", {}).get("stage_kind") != "qualification", "invalid",
                               f"Qualification stage {identity} cannot sit in a together group; it runs on its own row.")


def projection(local, items):
    order = load_order(local)
    if order is None:
        return {"configured": False, "revision": None, "current": [], "next": [], "rows": {"current": [], "next": []}}
    ids = {i["id"] for i in items}
    ts.require(all(i in ids for i in order_ids(order)), "missing", "Work order references an absent item; repair the saved order before continuing.")
    shown = view_order(ordered(order, items), items)
    return {**shown, "configured": True, "revision": digest(local.read(PATH))}


def progress(item):
    value = item.get("extensions", {}).get("progress")
    if value is None:
        return None
    ts.shape(value, {"percent", "basis"}, {"updated_on"})
    percent = value["percent"]
    ts.require(percent is None or type(percent) is int and 0 <= percent <= 100,
               "invalid", "Progress percent must be an integer from 0 to 100, or null for unknown.")
    ts.string(value["basis"])
    if "updated_on" in value:
        date.fromisoformat(value["updated_on"])
    return value


def _retire(item, identity, operation, request, why, previous_revision, *, what=None):
    """Apply one cancellation/supersession and append its stable history event."""
    item["status"] = "cancelled" if operation == "cancel" else "superseded"
    extensions = item.setdefault("extensions", {})
    successor = request.get("superseded_by")
    if operation == "supersede":
        extensions["superseded_by"] = successor
    event = {"date": request.get("date", date.today().isoformat()), "status": item["status"],
             "what": what or request.get("what", operation), "why": why,
             "result": request.get("result", why), "checks": request.get("checks", [])}
    date.fromisoformat(event["date"])
    event["id"] = "work-" + hashlib.sha256(ts.encoded(
        {"item": identity, "previous": previous_revision, **event})).hexdigest()[:20]
    extensions.setdefault("work_history", []).append(event)
    item.pop("next_action", None)


def _link(path, source):
    return posixpath.relpath(source, posixpath.dirname(path) or ".")


NEXT_STAGE_DISPOSITIONS = {"unchanged", "adjusted", "review-required", "blocked"}


def stage_assessment(value, item, items):
    """Validate the agent-supplied post-stage plan assessment; no planning is performed here."""
    ts.require(item.get("type") == "stage", "invalid", "Next-stage assessment belongs to a campaign stage.")
    ts.require(isinstance(value, dict), "invalid", "Next-stage assessment must be an object.")
    ts.shape(value, {"disposition", "basis", "affected_stage_ids", "changes"}, {"next_stage_id"})
    disposition = value["disposition"]
    ts.require(isinstance(disposition, str) and disposition in NEXT_STAGE_DISPOSITIONS, "invalid",
               "Next-stage disposition must be unchanged, adjusted, review-required, or blocked.")
    basis = ts.string(value["basis"]).strip()
    ts.require(bool(basis), "invalid", "Next-stage assessment basis must be nonblank.")
    result = {"disposition": disposition, "basis": basis}
    affected = value["affected_stage_ids"]
    changes = value["changes"]
    ts.require(isinstance(affected, list)
               and all(isinstance(identity, str) and identity for identity in affected),
               "invalid", "affected_stage_ids must contain nonblank stage IDs.")
    ts.require(len(affected) == len(set(affected)), "invalid",
               "affected_stage_ids must not contain duplicates.")
    ts.require(isinstance(changes, list) and all(isinstance(change, str) and change.strip() for change in changes),
               "invalid", "Next-stage changes must be an array of concise nonblank descriptions.")
    by_id = {candidate["id"]: candidate for candidate in items if candidate.get("type") in WORK
             and "source_import" not in candidate.get("extensions", {})}
    parent = item.get("parent_id")
    for identity in affected:
        ts.require(identity in by_id and by_id[identity].get("type") == "stage"
                   and by_id[identity].get("parent_id") == parent and identity != item["id"]
                   and by_id[identity].get("status") not in TERMINAL,
                   "invalid", "Affected stages must be other live stages in the same campaign.")
    if disposition == "unchanged":
        ts.require(not affected and not changes, "invalid",
                   "An unchanged assessment cannot claim affected stages or plan changes.")
    else:
        ts.require(bool(changes), "invalid",
                   "A changed, review-required, or blocked assessment must say what changed.")
    result["affected_stage_ids"] = deepcopy(affected)
    result["changes"] = deepcopy(changes)
    if "next_stage_id" in value:
        next_id = value["next_stage_id"]
        ts.require(isinstance(next_id, str) and next_id in by_id
                   and by_id[next_id].get("type") == "stage"
                   and by_id[next_id].get("parent_id") == parent
                   and next_id != item["id"] and by_id[next_id].get("status") not in TERMINAL,
                   "invalid", "next_stage_id must name another live stage in the same campaign.")
        result["next_stage_id"] = next_id
    return result


def _text(value):
    return str(value).replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _kind(item):
    """Name the catalogue kind beside a queued feature or capability; ordinary work rows stay unchanged."""
    return " · " + item["type"] if item["type"] in CATALOG else ""


def _table(path, rows, *, numbered=False):
    output = ["| " + ("Order | " if numbered else "") + "Work | State | Progress |", "| " + ("--- | " if numbered else "") + "--- | --- | --- |"]
    for number, item in enumerate(rows, 1):
        estimate = item.get("extensions", {}).get("progress") or {}
        percent = 100 if item["status"] == "completed" else estimate.get("percent")
        label = ("Complete" if item["status"] == "completed" else
                 "Stopped; not estimated" if item["status"] in {"cancelled", "superseded"} and percent is None else
                 f"Stopped at {percent}%" if item["status"] in {"cancelled", "superseded"} else
                 "Not estimated" if percent is None else f"{percent}% estimated")
        output.append("| " + (f"{number} | " if numbered else "") +
            f"[{_text(item['id'])} · {_text(item['title'])}]({_link(path, item['owner_path'])}){_kind(item)} | {item['status']} | {label} |")
    return "\n".join(output) if rows else "No recorded items."


def _label(item):
    estimate = item.get("extensions", {}).get("progress") or {}
    percent = 100 if item["status"] == "completed" else estimate.get("percent")
    return ("Complete" if item["status"] == "completed" else
            "Stopped; not estimated" if item["status"] in {"cancelled", "superseded"} and percent is None else
            f"Stopped at {percent}%" if item["status"] in {"cancelled", "superseded"} else
            "Not estimated" if percent is None else f"{percent}% estimated")


def _order_table(path, entries, by_id):
    """Numbered rows for the Work order: items, stage rows (with campaign) and together groups."""
    def link(item):
        return f"[{_text(item['id'])} · {_text(item['title'])}]({_link(path, item['owner_path'])}){_kind(item)}"
    def campaign(item):
        parent = by_id.get(item.get("parent_id")) if item["type"] == "stage" else None
        return _text(parent["title"]) if parent else None
    output = ["| Order | Work | State | Progress |", "| --- | --- | --- | --- |"]
    for number, entry in enumerate(entries, 1):
        ids = entry_ids(entry)
        items = [by_id[i] for i in ids]
        names = [campaign(i) for i in items]
        if not isinstance(entry, dict):
            work = link(items[0]) + (f" · stage of {names[0]}" if names[0] else "")
            output.append(f"| {number} | {work} | {items[0]['status']} | {_label(items[0])} |")
            continue
        work = " · ".join(link(i) + (f" ({n}, {i['status']})" if n else f" ({i['status']})") for i, n in zip(items, names))
        sequential = []
        for name in dict.fromkeys(n for n in names if n):
            members = [i["id"] for i, n in zip(items, names) if n == name]
            if len(members) > 1:
                sequential.append(f"{name}: " + ", ".join(members))
        if sequential:
            work += "; stages of one campaign still run one after another (" + "; ".join(sequential) + ")"
        output.append(f"| {number} together | {work} | {'; '.join(i['status'] for i in items)} | {'; '.join(_label(i) for i in items)} |")
    return "\n".join(output) if entries else "No recorded items."


def views(local, entries, order, updates=None):
    """Build only derived files; callers include them in the same guarded effect."""
    updates = {} if updates is None else updates
    items = [{**i, "owner_path": path} for path, board in entries.items() if board for i in board["items"]]
    by_id = {i["id"]: i for i in items}
    rich = ordered(order, items)
    updates[PATH] = boards._json(rich) + b"\n"
    order = view_order(rich, items)
    for path, title, lane in ((CURRENT, "Current Tasks", "current"), (NEXT, "Next Work", "next")):
        body = f"# {title}\n\nGenerated from native work records and the saved priority order. Edit through the work CLI.\n\n"
        body += _table(path, [by_id[i] for i in order[lane]], numbered=True)
        body += f"\n\n[Work order]({_link(path, ORDER)}) · [Done Work]({_link(path, DONE)}) · [Done Campaigns]({_link(path, DONE_CAMPAIGNS)})\n"
        updates[path] = body.encode("utf-8")
    updates[ORDER] = ("# Work order\n\n" + ADMISSION + "\n\n"
        + "## Current Tasks\n\n" + _order_table(ORDER, rich["current"], by_id)
        + "\n\n## Next Work\n\n" + _order_table(ORDER, rich["next"], by_id) + "\n").encode("utf-8")
    done = [i for i in items if i["type"] in WORK and i["status"] in TERMINAL and not i.get("parent_id")
            and "source_import" not in i.get("extensions", {})]
    updates[DONE] = ("# Done Work\n\nCompleted, cancelled and superseded standalone work. Details stay with the stable source record.\n\n"
                    + _table(DONE, [i for i in done if i["type"] != "campaign"]) + "\n").encode("utf-8")
    updates[DONE_CAMPAIGNS] = ("# Done Campaigns\n\nFiled campaigns retain their stages, evidence and stable IDs.\n\n"
                    + _table(DONE_CAMPAIGNS, [i for i in done if i["type"] == "campaign"]) + "\n").encode("utf-8")
    imported = [i for i in items if "source_import" in i.get("extensions", {})]
    for path, disposition, title in ((ORDER, "historical", "Imported history"), (ORDER, "held", "Held source work")):
        selected = [i for i in imported if ((i["status"] in TERMINAL or i["extensions"]["source_import"]["disposition"] == "historical")
                    == (disposition == "historical"))]
        if not selected:
            continue
        body = "\n## " + title + "\n\nSource records only; no current completion, activation or acceptance.\n\n"
        body += "| ID | Source status | Source type | Source parent |\n|---|---|---|---|\n"
        for item in selected:
            source = item["extensions"]["source_import"]["source_item"]
            body += "| " + " | ".join(_text(value) for value in (item["id"], source["status"], source["type"], source.get("parent_id", ""))) + " |\n"
        updates[path] += body.encode("utf-8")
    registry_raw = updates.get("docs/REGISTRY.json", local.read("docs/REGISTRY.json"))
    if registry_raw:
        registry = json.loads(registry_raw)
        if isinstance(registry.get("documents"), list):
            registered = {r["path"] for r in registry["documents"]}
            paths = [CURRENT, NEXT, DONE, ORDER, DONE_CAMPAIGNS]
            if local.read("docs/WORK_LIFECYCLE.md") is not None: paths.append("docs/WORK_LIFECYCLE.md")
            for path in paths:
                suffix = hashlib.sha256(path.encode()).hexdigest()[:12]
                procedure = path == "docs/WORK_LIFECYCLE.md"
                role = "work-lifecycle-procedure" if procedure else "work-view-" + suffix
                if path not in registered:
                    registry["documents"].append({"id": "eutonos.doc.work-" + suffix, "path": path, "role": role})
                elif procedure:
                    # Preserve identity and deliberate roles; repair only the
                    # old generated-view label on this authored procedure.
                    for record in registry["documents"]:
                        if record.get("path") == path and record.get("role") == "work-view-" + suffix:
                            record["role"] = role
            updated = boards._json(registry) + b"\n"
            if updated != registry_raw: updates["docs/REGISTRY.json"] = updated
    changes = []
    for item in items:
        if "source_import" in item.get("extensions", {}):
            continue
        for event in item.get("extensions", {}).get("work_history", []):
            if event.get("status") != "completed":
                continue
            record = {"profile": "eutonos.tokenslang.change.v1", "id": event["id"], "title": item["title"],
                      "summary": event["result"], "status": "completed", "date": event["date"],
                      "evidence_refs": [{"path": item["owner_path"]}],
                      "extensions": {"work_id": item["id"], "why": event["why"], "checks": event["checks"]}}
            changes.append((event["date"], event["id"], "```tokenslang-change\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n```"))
    if changes:
        path = "docs/CHANGELOG.md"
        raw = updates.get(path, local.read(path)) or b"# Changelog\n"
        body = raw.decode("utf-8")
        block = START + "\n\n## Work completions\n\nDerived from closeout results; agents write each result once.\n\n"
        block += "\n\n".join(c[2] for c in sorted(changes, reverse=True)) + "\n\n" + END
        if START in body:
            ts.require(body.count(START) == body.count(END) == 1, "invalid", "Malformed generated work history boundary.")
            body = body[:body.index(START)] + block + body[body.index(END) + len(END):]
        else:
            body = body.rstrip() + "\n\n" + block + "\n"
        updates[path] = body.encode("utf-8")
    return updates


def form(root, item_id=None):
    from .work_attribution import profile_hint
    shown = boards.project(root)
    item = next((i for i in shown["items"] if i["id"] == item_id), None)
    by_id = {candidate["id"]: candidate for candidate in shown["items"]}
    if item_id:
        ts.require(item is not None and item["type"] in boards.TYPES and item["type"] != "attempt", "missing", "Select an existing work or catalog item.")
    return {"profile": PROFILE, "outcome": "form", "item": item,
            "operations": ["initialize", "refresh", "update", "complete", "cancel", "supersede", "delete-stage", "archive", "restore", "reorder", "organize-campaign", "review", "integrate"] if not item or item["type"] in WORK else ["review", "integrate"],
            "request": {"operation": "update", "item_id": item_id, "expected_revision": item_revision(item) if item else None,
                        "changes": {}, "progress": {"percent": None, "basis": "Explain completed and remaining effort, or why unknown."}},
            "closeout": {"item_id": item_id, "expected_revision": item_revision(item) if item else None,
                         "status": "active|blocked|completed", "what": "What changed?", "why": "Why?",
                         "result": "What was the result?", "checks": ["Relevant check results; name anything not run."],
                         "progress": {"percent": None, "basis": "What percent is done, allowing for unequal stage effort?"},
                         **({"completion_checks": deepcopy(item["extensions"]["completion_checks"])}
                            if item and "completion_checks" in item.get("extensions", {}) else {})},
            "execution_recommendation": {"optional": True, "current": item.get("extensions", {}).get("execution_recommendation") if item else None,
                "example": {"model": "Sol", "effort": "Medium", "basis": "Bounded implementation", "advisory": True}} if item and item["type"] in {"stage", "slice"} else None,
            "stage_delete": {"operation": "delete-stage", "item_id": item_id,
                "expected_revision": item_revision(item),
                "expected_parent_revision": item_revision(by_id[item["parent_id"]]),
                "why": "Why should this unfinished stage be removed?"}
                if item and item["type"] == "stage" and item.get("parent_id") in by_id else None,
            "stage_assessment": {"optional": True, "current": item.get("extensions", {}).get("stage_assessment") if item else None,
                "example": {"disposition": "unchanged", "basis": "Stage evidence leaves the next plan unchanged.", "affected_stage_ids": [], "changes": []}} if item and item["type"] == "stage" else None,
            "stage_association": {"stage_id": item.get("extensions", {}).get("stage_id") if item and item["type"] == "slice" else None,
                                  "prompt": "For a slice, assess the exact stage changed. Save extensions.stage_id through board update when applicable; otherwise state in the closeout result that no stage changed. Unmapped legacy slices remain standalone."},
            'participant_profile': profile_hint(root),
            'attribution': attribution_form(item),
            "order": shown.get("work_order"),
            "rules": ["Use null for an unknown estimate. 100% does not mark work completed.",
                      "Completing a stage leaves the campaign open; guided closeout also requires the active parent campaign estimate, or null with a basis.",
                      "Campaign completion requires all stages terminal and explicit acceptance_confirmed: true.",
                      "Campaign completion, cancellation or supersession may explicitly set defer_owner_move: true to retain a mixed owner; the terminal decision still files natively, while archive/organize still require a dedicated owner.",
                      "Completing an associated slice requires its stage and active parent campaign closeout rows; completing a slice does not complete the stage automatically.",
                      "Remove work by cancelling or superseding with a reason; retain IDs, evidence and history.",
                      "Reorder supplies every live top-level ID exactly once, with expected_order_revision. A lane entry is an ID or {\"together\": [two or more IDs]}; a live campaign stage ID may also be a row, and stages of one campaign keep their campaign order. Qualification stages cannot sit in a together group."]}


def attribution_form(item):
    from .work_attribution import content_revision
    return {'caller_context': {'app': None, 'model': None, 'session': None},
            'review': {'operation': 'review', 'item_id': item['id'] if item else None,
                       'expected_revision': item_revision(item) if item else None,
                       'reviewed_revision': content_revision(item) if item else None,
                       'decision': 'KEEP|CHANGE|DEFER', 'evidence': ['Exact check/review evidence.'], 'source_commit': None},
            'integration': {'operation': 'integrate', 'item_id': item['id'] if item else None,
                            'expected_revision': item_revision(item) if item else None,
                            'integration': {'source_commit': '<full commit>', 'target_ref': 'refs/heads/main', 'target_commit': '<full commit>'},
                            'evidence': ['Actual integration evidence.']}}


def folders(root):
    """Explicit directory setup, separate from the no-write effect planner."""
    local = LocalRoot(root)
    paths = ["docs", "docs/campaigns", "docs/campaigns/done"]
    for item in boards.project(root)["items"]:
        if item["type"] == "campaign" and re.fullmatch(r"[A-Za-z0-9_-]+", item["id"]):
            paths.extend(["docs/campaigns/" + item["id"], "docs/campaigns/done/" + item["id"]])
    created = []
    for name in paths:
        target = local.root / name
        local._parents(target.parent)
        if target.exists():
            local._parents(target)
        else:
            target.mkdir()
            local._parents(target)
            created.append(name)
    return {"profile": PROFILE, "outcome": "complete", "created_directories": created}


def _redirect_routes(local, updates, old, new):
    def replace(value):
        if isinstance(value, dict): return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list): return [replace(v) for v in value]
        return new if value == old else value
    for path in ("eutonos.ram.json", "eutonos.read.json", "docs/REGISTRY.json"):
        raw = updates.get(path, local.read(path))
        if raw is not None:
            value = json.loads(raw)
            if path == "docs/REGISTRY.json" and isinstance(value.get("documents"), list):
                destination = [r for r in value["documents"] if r.get("path") == new]
                ts.require(all(r.get("id", "").startswith("eutonos.doc.compat-") for r in destination),
                           "conflict", "Destination already has a different canonical registry owner.")
                value["documents"] = [r for r in value["documents"] if r not in destination]
            changed = replace(value)
            if path == "docs/REGISTRY.json" and isinstance(changed.get("documents"), list) and any(r.get("path") == old for r in value.get("documents", [])):
                suffix = hashlib.sha256(old.encode()).hexdigest()[:12]
                changed["documents"].append({"id": "eutonos.doc.compat-" + suffix, "path": old, "role": "compatibility-route-" + suffix})
            if changed != value: updates[path] = boards._json(changed) + b"\n"


def _redirect_text(identity, source, destination, anchors):
    body = f"<!-- eutonos:campaign-redirect {identity} -->\n# Campaign moved\n\n[Open {identity}]({_link(source, destination)}). Stable campaign and stage IDs are unchanged.\n"
    for anchor in dict.fromkeys(anchors):
        body += f'\n<a id="{anchor}"></a>\n[Open section]({_link(source, destination)}#{anchor}).\n'
    return body


def _move_campaign(local, entries, raw_by_path, owners, item_id, updates):
    old = next(path for path, board in entries.items() if board and any(i["id"] == item_id for i in board["items"]))
    ts.require(not any("source_import" in i.get("extensions", {}) for i in entries[old]["items"]),
               "denied", "Owner containing imported provenance cannot be moved through ordinary filing.")
    selected = next(i for i in entries[old]["items"] if i["id"] == item_id)
    ts.require(selected["type"] == "campaign", "invalid", "Only campaigns have campaign folders.")
    ts.require(all(i["id"] == item_id or i.get("parent_id") == item_id for i in entries[old]["items"]),
               "conflict", "Campaign filing needs a dedicated owner; unrelated board items must remain intact.")
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", item_id)
    ts.require(slug == item_id and slug not in {".", ".."}, "invalid", "Campaign ID is not a safe folder name.")
    new = f"docs/campaigns/{'done/' if selected['status'] in TERMINAL else ''}{slug}/README.md"
    if new == old: return
    destination = local.read(new)
    redirect_body = destination.decode("utf-8").replace("\r\n", "\n") if destination is not None else None
    anchors = re.findall(r'<a id="([^"\n]+)"></a>', redirect_body or "")
    ts.require((destination is None or redirect_body == _redirect_text(item_id, new, old, anchors)) and new not in owners,
               "conflict", "Campaign destination contains unrelated content.")
    # Clear deferral only in the successful move effect, after all move guards.
    if selected.get("extensions", {}).pop("owner_move_deferred", None) is not None:
        original = updates.get(old, raw_by_path[old])
        _, matches = boards._parse(original, old)
        updates[old] = boards._replace(original, matches[0], entries[old])
    raw = updates.pop(old, raw_by_path[old])
    body = raw.decode("utf-8")
    # Relative Markdown links must still reach the same targets after filing.
    def relink(match):
        target = match.group(1)
        if re.match(r"(?:[a-zA-Z][\w+.-]*:|/|#)", target): return match.group(0)
        path, sep, anchor = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(old), path))
        return "](" + _link(new, resolved) + (sep + anchor if sep else "") + ")"
    body = re.sub(r"\]\(([^\s)]+)\)", relink, body)
    updates[new] = body.replace("\r\n", "\n").encode("utf-8")
    legacy_anchors = []
    counts = {}
    for title in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", raw.decode("utf-8")):
        anchor = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = counts.get(anchor, 0); counts[anchor] = count + 1
        legacy_anchors.append(anchor + ("-" + str(count) if count else ""))
    legacy_anchors.extend(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)', raw.decode("utf-8")))
    updates[old] = _redirect_text(item_id, old, new, legacy_anchors).encode("utf-8")
    entries[new] = entries.pop(old)
    owners[owners.index(old)] = new
    _redirect_routes(local, updates, old, new)


def plan(root, request):
    local = root if isinstance(root, LocalRoot) else LocalRoot(root)
    ts.require(isinstance(request, dict), "invalid", "Work request must be an object.")
    ts.shape(request, {"operation"}, {"item_id", "expected_revision", "expected_parent_revision", "changes", "progress", "lane", "reason", "superseded_by",
        "acceptance_confirmed", "defer_owner_move", "stage_assessment", "what", "why", "result", "checks", "completion_checks", "date", "current", "next", "expected_order_revision", "refresh_revision", 'caller_context', '_attribution',
        'reviewed_revision', 'decision', 'evidence', 'source_commit', 'integration'})
    from . import work_attribution as attribution
    binding = attribution.snapshot(local.root, request) if request.get('operation') in {'complete','review','integrate'} or 'completion_checks' in request else None
    config_raw, owners, raw_by_path, entries, diagnostics = boards._load(local, strict=True)
    ts.require(owners is not None, "missing", "Native board configuration is required.")
    ts.require(not any(raw is None for raw in raw_by_path.values()), "missing", "A configured board owner is missing.")
    by_id = boards._validate_graph(entries)
    order = load_order(local)
    if order is not None:
        ts.require(all(i in by_id for i in order_ids(order)), "missing", "Work order references an absent item; reconcile it before writing.")
    operation = request["operation"]
    if "completion_checks" in request:
        ts.require(operation in {"update", "complete", "cancel", "supersede", "archive", "restore", "organize-campaign"},
                   "invalid", "Completion checks require an item content update.")
    if "stage_assessment" in request:
        ts.require(operation == "complete", "invalid", "stage_assessment belongs only to stage completion.")
    if "defer_owner_move" in request:
        ts.require(type(request["defer_owner_move"]) is bool, "invalid", "defer_owner_move must be a boolean.")
        selected = by_id.get(request.get("item_id"))
        ts.require(operation in {"complete", "cancel", "supersede"} and selected is not None and selected[1]["type"] == "campaign",
                   "invalid", "defer_owner_move applies only to terminal campaign decisions.")
    updates = {}
    owner_filing = None
    if operation == "initialize":
        ts.require(order is None, "conflict", "Work lifecycle is already initialized.")
        order = {"profile": CONFIG, "current": [], "next": []}
        _redirect_routes(local, updates, "docs/CURRENT_TASK.md", CURRENT)
        # Legacy work identities and RAM links remain in their original source.
        if "eutonos.ram.json" in updates:
            original = json.loads(local.read("eutonos.ram.json"))
            value = json.loads(updates["eutonos.ram.json"])
            value["work_owners"] = original.get("work_owners", {})
            updates["eutonos.ram.json"] = boards._json(value) + b"\n"
    else:
        ts.require(order is not None or operation in {"review", "integrate"}, "missing", "Initialize the work lifecycle before changing priority or closing work.")
    if operation == "reorder":
        ts.require(request.get("expected_order_revision") == digest(local.read(PATH)), "stale", "Work priority changed; reread before reordering.")
        items = [i for _, i in by_id.values()]
        live, stages = _live(items), _live_stages(items)
        wanted = {"profile": CONFIG, "current": request.get("current"), "next": request.get("next")}
        ts.require(all(isinstance(wanted[l], list) for l in ("current", "next")), "invalid", "Reorder must include every live top-level ID exactly once.")
        ids = order_ids(wanted)
        ts.require(len(ids) == len(set(ids)), "invalid", "Reorder must include every live top-level ID exactly once.")
        ts.require(all(i in live or i in stages for i in ids), "invalid",
                   "Reorder may name only live top-level work and live campaign stages; an ID is absent, finished or not a stage.")
        ts.require({i for i in ids if i in live} == set(live), "invalid", "Reorder must include every live top-level ID exactly once.")
        rank = {item["id"]: n for n, item in enumerate(i for owner_path in owners for i in (entries.get(owner_path) or {"items": []})["items"])}
        guard_rows(wanted, {i["id"]: i for i in items}, rank)
        order = wanted
    elif operation not in {"initialize", "refresh"}:
        ts.require(operation in {"update", "complete", "cancel", "supersede", "delete-stage", "archive", "restore", "organize-campaign", "review", "integrate"}, "invalid", "Unknown lifecycle operation.")
        identity = request.get("item_id")
        ts.require(isinstance(identity, str) and identity in by_id, "missing", "Work item does not exist.")
        owner, item = by_id[identity]
        imported = "source_import" in item.get("extensions", {})
        ts.require(not imported or operation == "cancel", "denied",
                   "Imported history/hold can only be cancelled; provenance remains immutable.")
        if imported:
            ts.require(not request.get("changes") and "progress" not in request and "lane" not in request
                       and "completion_checks" not in request,
                       "denied", "Cancelling imported work cannot rewrite fields, progress or priority.")
        ts.require(item["type"] in WORK or operation in {"review", "integrate"} and item["type"] != "attempt", "invalid", "Lifecycle operations require a work record; catalog sign-offs use review or integrate.")
        ts.require(request.get("expected_revision") == item_revision(item), "stale", "Work item changed; review it before resubmitting this decision.")
        if operation in {'review', 'integrate'}:
            ts.require(not request.get('changes') and not any(k in request for k in ('progress','lane','stage_assessment')), 'invalid', 'Sign-offs cannot change work content.')
            evidence = request.get('evidence')
            ts.require(isinstance(evidence, list) and evidence and all(isinstance(v,str) and v.strip() for v in evidence), 'invalid', 'Explicit sign-offs require evidence.')
            if operation == 'review':
                ts.require(request.get('reviewed_revision') == attribution.content_revision(item), 'stale', 'Reviewed native content revision differs.')
                ts.require(request.get('decision') in {'KEEP','CHANGE','DEFER'}, 'invalid', 'Review decision must be KEEP, CHANGE or DEFER.')
                source = request.get('source_commit')
                if source is not None: attribution.commit(local.root, source)
                attribution.append(item, binding, 'reviewed', reviewed_revision=request['reviewed_revision'],
                                   decision=request['decision'], evidence=evidence, source_commit=source)
            else:
                proof = attribution.integration_proof(local.root, request.get('integration'))
                attribution.append(item, binding, 'integrated', **proof, evidence=evidence)
            raw = raw_by_path[owner]
            _, matches = boards._parse(raw, owner)
            updates[owner] = boards._replace(raw, matches[0], entries[owner])
            if order is not None: views(local, entries, order, updates)
            effect = effect_plan(local, updates, inputs=['eutonos.boards.json', *raw_by_path, PATH])
            return {'profile': PROFILE, 'outcome': 'planned', 'request': deepcopy(request), 'effect': effect,
                    'fingerprint': digest(ts.encoded(effect)), 'paths': [c['path'] for c in effect['changes']]}
        if operation == "delete-stage":
            ts.require(not imported and item["type"] == "stage" and isinstance(item.get("parent_id"), str),
                       "invalid", "Stage deletion requires one ordinary campaign stage.")
            ts.require(item["status"] != "completed", "conflict", "Completed stages cannot be deleted.")
            parent_owner, parent = by_id[item["parent_id"]]
            ts.require(parent["type"] == "campaign", "invalid", "Stage deletion requires a campaign parent.")
            ts.require(request.get("expected_parent_revision") == item_revision(parent), "stale",
                       "Parent campaign changed; review it before deleting the stage.")
            why = request.get("why", request.get("reason"))
            ts.require(isinstance(why, str) and bool(why.strip()), "invalid", "Stage deletion requires a nonblank reason.")
            why = why.strip()
            ts.require(not any(candidate.get("extensions", {}).get("stage_id") == identity
                               for _, candidate in by_id.values()), "conflict",
                       "A slice still references this stage; update that association before deletion.")
            deleted_on = request.get("date", date.today().isoformat())
            date.fromisoformat(deleted_on)
            event = {"date": deleted_on, "status": "stage-deleted", "what": "Deleted stage " + identity,
                     "why": why, "result": request.get("result", why), "checks": request.get("checks", []),
                     "deleted_stage": {"id": identity, "title": item["title"]}}
            event["id"] = "work-" + hashlib.sha256(ts.encoded(
                {"item": parent["id"], "previous": request["expected_parent_revision"], **event})).hexdigest()[:20]
            parent_extensions = parent.setdefault("extensions", {})
            parent_extensions.setdefault("work_history", []).append(event)
            parent_extensions["last_work_request"] = digest(ts.encoded(request))
            entries[owner]["items"].remove(item)
            boards._item(parent)
            for changed_owner in {owner, parent_owner}:
                raw = raw_by_path[changed_owner]
                _, matches = boards._parse(raw, changed_owner)
                updates[changed_owner] = boards._replace(raw, matches[0], entries[changed_owner])
            boards._validate_graph(entries)
            views(local, entries, order, updates)
            effect = effect_plan(local, updates, inputs=["eutonos.boards.json", *raw_by_path, PATH])
            return {"profile": PROFILE, "outcome": "planned", "request": deepcopy(request), "effect": effect,
                    "fingerprint": digest(ts.encoded(effect)), "paths": [c["path"] for c in effect["changes"]]}
        changes = deepcopy(request.get("changes", {}))
        ts.require(isinstance(changes, dict) and set(changes) <= {"title", "summary", "next_action", "status", "execution_recommendation"}, "invalid", "Use work changes for title, summary, next_action, status or execution_recommendation.")
        if "status" in changes:
            ts.require(changes["status"] in {"active", "blocked", "planned", "deferred"}, "invalid", "Use complete, cancel or supersede for terminal transitions.")
        ts.require(item["status"] not in TERMINAL or operation in {"archive", "restore", "organize-campaign", "review", "integrate"}, "conflict", "Restore a finished item before editing it.")
        if "stage_assessment" in request:
            ts.require(operation == "complete" and item["type"] == "stage", "invalid", "stage_assessment belongs only to stage completion.")
        recommendation = changes.pop("execution_recommendation", None)
        if "execution_recommendation" in request.get("changes", {}):
            ts.require(item["type"] in {"stage", "slice"}, "invalid", "Execution recommendations belong to stages or slices.")
            recommendation = boards.execution_recommendation(recommendation)
        item.update(changes)
        extensions = item.setdefault("extensions", {})
        if "completion_checks" in request:
            from . import completion_checks
            extensions["completion_checks"] = completion_checks.reconcile(
                request["completion_checks"], extensions.get("completion_checks"), binding)
        if recommendation is not None:
            extensions["execution_recommendation"] = recommendation
        if "progress" in request:
            extensions["progress"] = deepcopy(request["progress"])
            progress(item)
            extensions["progress"]["updated_on"] = request.get("date", date.today().isoformat())
            date.fromisoformat(extensions["progress"]["updated_on"])
        if operation in {"complete", "cancel", "supersede"}:
            why = request.get("why", request.get("reason"))
            ts.string(why)
            if operation == "complete":
                for field in ("what", "result"): ts.string(request.get(field))
                ts.require(isinstance(request.get("checks"), list) and request["checks"] and all(isinstance(i, str) and i.strip() for i in request["checks"]),
                           "invalid", "Completion requires check results, including explicit unrun checks where applicable.")
                if item["type"] == "campaign":
                    ts.require(request.get("acceptance_confirmed") is True, "invalid", "Campaign completion requires explicit acceptance_confirmed.")
                    ts.require(all(child["status"] in TERMINAL for _, child in by_id.values() if child.get("parent_id") == identity),
                               "conflict", "Campaign still has unfinished stages.")
                if "stage_assessment" in request:
                    extensions["stage_assessment"] = stage_assessment(request["stage_assessment"], item, [candidate for _, candidate in by_id.values()])
                item["status"] = "completed"
                extensions["progress"] = {"percent": 100, "basis": request["result"], "updated_on": request.get("date", date.today().isoformat())}
            else:
                if operation == "supersede":
                    successor = request.get("superseded_by")
                    ts.require(isinstance(successor, str) and successor in by_id and successor != identity and by_id[successor][1]["type"] in WORK
                               and by_id[successor][1]["status"] not in TERMINAL, "invalid", "Select a different live replacement work ID.")
                    ts.require(not (item["type"] == "campaign" and by_id[successor][1].get("parent_id") == identity
                                    and by_id[successor][1]["type"] in {"stage", "slice"}), "invalid",
                               "A campaign replacement cannot be a child retired by the same decision.")
                retirement_date = request.get("date", date.today().isoformat())
                date.fromisoformat(retirement_date)
                retirement_request = {**request, "date": retirement_date}
                previous_revision = request["expected_revision"]
                _retire(item, identity, operation, retirement_request, why, previous_revision)
                if item["type"] == "campaign":
                    for child_identity, (child_owner, child) in by_id.items():
                        if child.get("parent_id") != identity or child["type"] not in {"stage", "slice"} or child["status"] in TERMINAL:
                            continue
                        _retire(child, child_identity, operation, retirement_request, why, item_revision(child),
                                what=f"{operation.capitalize()}d with campaign {identity}")
                        child_extensions = child.setdefault("extensions", {})
                        child_extensions["last_work_request"] = digest(ts.encoded(request))
                        raw = raw_by_path[child_owner]
                        _, matches = boards._parse(raw, child_owner)
                        updates[child_owner] = boards._replace(raw, matches[0], entries[child_owner])
            event = {"date": request.get("date", date.today().isoformat()), "status": item["status"],
                     "what": request.get("what", operation), "why": why, "result": request.get("result", why), "checks": request.get("checks", [])}
            date.fromisoformat(event["date"])
            if operation == "complete" and "stage_assessment" in request:
                event["stage_assessment"] = deepcopy(extensions["stage_assessment"])
            if operation == "complete":
                event["id"] = "work-" + hashlib.sha256(ts.encoded({"item": identity, "previous": request["expected_revision"], **event})).hexdigest()[:20]
                event['attribution'] = attribution.append(item, binding, 'completed', work_event_id=event['id'])
                extensions.setdefault("work_history", []).append(event)
                item.pop("next_action", None)
        if operation == "archive":
            ts.require(item["status"] in TERMINAL, "invalid", "Only terminal work can be archived.")
            extensions["archived"] = True
        if operation == "restore":
            ts.require(item["status"] in TERMINAL, "invalid", "Only terminal work needs restoration.")
            ts.require(not item.get("parent_id") or by_id[item["parent_id"]][1]["status"] not in TERMINAL,
                       "conflict", "Restore the campaign before restoring its stage.")
            item["status"] = "planned"
            extensions.pop("archived", None)
            extensions.pop("superseded_by", None)
            extensions["progress"] = {"percent": None, "basis": "Reopened; a new estimate is needed."}
        extensions["last_work_request"] = digest(ts.encoded(request))
        if operation == "update" and all(k in request for k in ("what", "why", "result", "checks")):
            extensions["checkpoint"] = {k: deepcopy(request[k]) for k in ("what", "why", "result", "checks")}
            extensions["checkpoint"]["date"] = request.get("date", date.today().isoformat())
        if operation == 'complete': event['attribution']['completed_revision'] = attribution.content_revision(item)
        if item["status"] == "completed":
            from . import completion_checks
            completion_checks.require_complete(item)
        boards._item(item)
        if "lane" in request:
            ts.require(request["lane"] in {"current", "next"} and not item.get("parent_id") and item["status"] not in TERMINAL,
                       "invalid", "Only live top-level work has a current/next lane.")
            for lane in ("current", "next"): order[lane] = _without(order[lane], identity)
            order[request["lane"]].append(identity)
        raw = raw_by_path[owner]
        _, matches = boards._parse(raw, owner)
        updates[owner] = boards._replace(raw, matches[0], entries[owner])
        if item["type"] == "campaign" and operation in {"complete", "cancel", "supersede"} and request.get("defer_owner_move", False):
            extensions["owner_move_deferred"] = True
            updates[owner] = boards._replace(raw, matches[0], entries[owner])
            owner_filing = {"state": "deferred", "item_id": identity, "owner_path": owner,
                            "message": "Owner archive/move explicitly deferred until a dedicated owner is available; terminal filing is established only by a verified write receipt."}
        elif item["type"] == "campaign" and operation == "restore" and extensions.get("owner_move_deferred") is True:
            # Reopening must preserve the same mixed owner as deferred completion.
            owner_filing = {"state": "deferred", "item_id": identity, "owner_path": owner,
                            "message": "Reopened in the existing owner; unrelated records remain intact."}
        elif item["type"] == "campaign" and operation in {"complete", "cancel", "supersede", "restore", "archive", "organize-campaign"} and not imported:
            _move_campaign(local, entries, raw_by_path, owners, identity, updates)
    boards._validate_graph(entries)
    if json.loads(config_raw)["owners"] != owners:
        updates["eutonos.boards.json"] = boards._json({"profile": boards.CONFIG, "owners": owners}) + b"\n"
    views(local, entries, order, updates)
    # Observe original owners too, even when filing changed their canonical path.
    effect = effect_plan(local, updates, inputs=["eutonos.boards.json", *raw_by_path, PATH])
    return {"profile": PROFILE, "outcome": "planned", "request": deepcopy(request), "effect": effect,
            "fingerprint": digest(ts.encoded(effect)), "paths": [c["path"] for c in effect["changes"]],
            **({"owner_filing": owner_filing} if owner_filing is not None else {})}


def apply_prepared(root, host, actor, prepared, request_id):
    """Journal, apply, verify every output, release; never acknowledge half a filing."""
    attempt = "work-lifecycle-" + request_id
    # Lifecycle writes are always governed by the CLI's exact semantic plan.
    # Claim only those paths for this attempt, then retire the claim during the
    # verified closeout below.  A caller never needs a broad or retained board
    # grant merely to submit one lifecycle transition.
    from .git_delivery_native import apply
    if prepared['request'].get('operation') == 'integrate':
        from .work_attribution import integration_proof
        integration_proof(root, prepared['request']['integration'])
    result = apply(root, host, actor, attempt, attempt + "-" + prepared["fingerprint"],
                   prepared["effect"], prepared["fingerprint"], prepared["paths"],
                   {"completed": "Work state, order and derived views verified.",
                    "next": "Continue from Current Tasks.", "pending": None},
                   verify=lambda: verify_effect(root, prepared["effect"]),
                   work_id=prepared["request"].get("item_id", attempt))
    return {"profile": PROFILE, **result,
            **({"owner_filing": deepcopy(prepared["owner_filing"])} if "owner_filing" in prepared else {})}


def verify_effect(root, effect):
    """Typed current native board/order readback, beyond matching byte hashes."""
    from .effects import decode
    local = LocalRoot(root)
    ts.require(all(local.read(c['path']) == decode(c['after']) for c in effect['changes']),
               'stale', 'Native work effect readback differs.')
    _, _, _, entries, _ = boards._load(local, strict=True)
    items = [{**item, 'owner_path': path} for path, board in entries.items()
             if board for item in board['items']]
    by_id = {item['id']: (item['owner_path'], item) for item in items}
    from .write_batch import _effect_receipt
    written = {r['id'] for r in _effect_receipt(effect)['records']}
    for item in items:
        if item['id'] in written and 'source_import' not in item.get('extensions', {}):
            boards._validate_written_work_item(item, by_id)
    order = load_order(local)
    if order is not None:
        projection(local, items)
        if 'board_import' in effect and 'undo' in effect['board_import'].get('modes', []):
            return
        generated = views(local, entries, order)
        selected = {c['path'] for c in effect['changes']}
        for path, body in generated.items():
            if path in selected:
                ts.require(local.read(path) == body, 'stale', 'Derived work view differs: ' + path)
