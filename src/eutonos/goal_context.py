"""Selected-goal read, bounded context and bounded edit for agents.

An agent selects one shared goal or one owner-private goal/project by explicit
ID. Nothing is stored: there is no selection file, board, cache or writer here.
Reads reuse the native resolvers; edits reuse the existing validated,
revision-bound goal and personal writes. Private scope is entered only through
``--scope personal`` with an explicit root and owner, and its errors never carry
titles, content, paths or request text.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from . import tokenslang as ts

PROFILE = "eutonos.goal-context.v1"
BRIEF_TEXT = 240
BRIEF_LINKS = 10
FULL_LINKS = 25
MAX_LIST = 50

# Advice vocabulary (GAP campaign "Goal, scope and personal-state contract").
# These are labels for a human or agent to apply to a tangent; this module never
# classifies a tangent, ranks work, widens authority or dispatches anything.
ADVICE = {
    "profile": "eutonos.goal-advice-vocabulary.v1",
    "authority": "Advice only: it grants no permission, changes no rank or goal and dispatches no work.",
    "options": [
        {"advice": "do-now", "use": "The tangent serves the selected outcome or is cheap and unblocks it."},
        {"advice": "defer", "requires": ["rationale", "reconsideration_trigger"],
         "use": "Keep the tangent out of this goal's scope with a concrete trigger to revisit it."},
        {"advice": "minimal-integration-preparation", "requires": ["future_consumer", "smallest_seam"],
         "use": "Add only the seam a named future consumer needs; postponement-cost claims need affected "
                "consumers, retrofit evidence, assumptions and uncertainty."},
        {"advice": "deliberate-goal-revision", "requires": ["owner_decision", "changed_scope_or_version", "affected_work"],
         "use": "The outcome itself changes; make it through the validated goal edit, never silently."},
    ],
    "against": "Compare the tangent with this goal's outcome, success criteria, scope and exclusions.",
}


def _scope(root, scope, owner_id):
    from .private_scope import require_owner, require_ordinary_root, require_shared
    ts.require(scope in {"shared", "personal"}, "invalid", "Select --scope shared or personal.")
    if scope == "personal":
        ts.require(isinstance(owner_id, str) and owner_id.strip(), "denied", "Personal scope requires a valid selected owner.")
        require_owner(require_ordinary_root(root), owner_id)
        return True
    ts.require(owner_id is None, "invalid", "Shared scope takes no personal owner.")
    require_shared(root)
    return False


def _cut(text, full):
    if full or not isinstance(text, str) or len(text) <= BRIEF_TEXT:
        return text
    return text[:BRIEF_TEXT] + "..."


def _compact(item, full):
    return {key: _cut(item[key], full) for key in ("id", "type", "title", "summary", "status") if key in item}


def select(root, scope, owner_id=None, *, offset=0, limit=20, include_closed=False):
    """List selectable goals (and, in personal scope, projects); IDs are the selection."""
    private = _scope(root, scope, owner_id)
    ts.require(isinstance(offset, int) and isinstance(limit, int) and offset >= 0 and 0 < limit <= MAX_LIST,
               "invalid", "Offset must be nonnegative and limit 1-%d." % MAX_LIST)
    if private:
        from . import personal
        items = personal.read(root, owner_id=owner_id)["items"]
    else:
        from . import goal_records
        items = goal_records.read_shared(root)["items"]
    closed = {"completed", "archived", "cancelled", "superseded", "removed"}
    items = [i for i in items if include_closed or i.get("status") not in closed]
    page = items[offset:offset + limit]
    return {"profile": PROFILE, "outcome": "selection", "scope": scope, "total": len(items),
            "candidates": [{**_compact(i, False), "owner_path": i.get("owner_path")} for i in page],
            "next_offset": offset + limit if offset + limit < len(items) else None,
            "next": "Pass --item-id ID to context, or --goal ID to pickup.",
            "qualification": "Listing is a selection aid, not ranking, permission or dispatch."}


def _reference(root, item_id, owner_id):
    from . import device_repositories as device, scoped_items
    ident = device.native_identity(root, owner_id=owner_id)
    return scoped_items.validate({"profile": scoped_items.PROFILE, **ident, "item_id": item_id})


def _read_selected(root, scope, item_id, owner_id):
    """Return (item, owner pin, source pin or None) for one explicit goal/project."""
    from . import scoped_items
    ts.require(isinstance(item_id, str) and item_id.strip(), "invalid", "Select an explicit --item-id.")
    private = _scope(root, scope, owner_id)
    try:
        reference = _reference(root, item_id, owner_id)
    except ts.ReadError:
        if private:
            raise
        # No native identity catalog: the direct-root read still works, with an unknown pin.
        item, owner, outcome, _ = scoped_items._shared_item(root, item_id)
        ts.require(outcome == "resolved", outcome if outcome in {"missing", "ambiguous"} else "unavailable",
                   "Selected shared goal is absent, ambiguous or unavailable.")
        from .work_lifecycle import item_revision
        return item, {"path": owner["path"], "sha256": owner["sha256"], "item_revision": item_revision(item)}, None, None
    resolved = scoped_items.resolve(root, reference, owner_id=owner_id)
    code = {"denied": "denied", "missing": "missing", "ambiguous": "ambiguous", "stale": "stale"}.get(resolved["outcome"], "unavailable")
    ts.require(resolved["outcome"] == "resolved", code, "Selected goal is %s." % resolved["outcome"])
    return (resolved["item"], {"path": resolved["owner_path"], "sha256": resolved["owner_sha256"],
                               "item_revision": resolved["item_revision"]}, resolved["source_pin"], reference)


def _link_status(root, link, owner_id):
    from . import scoped_items
    try:
        result = scoped_items.resolve(root, link, owner_id=owner_id)
    except (ts.ReadError, OSError, ValueError, TypeError, KeyError):
        return {"reference": link, "outcome": "unknown"}
    shown = {"reference": link, "outcome": result["outcome"]}
    if result["outcome"] == "resolved":
        item = result["item"]
        shown.update(item={key: item[key] for key in ("id", "type", "title", "status") if key in item},
                     item_revision=result["item_revision"], source_pin=result["source_pin"])
    return shown


def context(root, scope, item_id, owner_id=None, *, detail="brief"):
    """Bounded goal/project context: outcome, criteria, scope, links, pins, unknowns."""
    ts.require(detail in {"brief", "full"}, "invalid", "Select --detail brief or full.")
    full = detail == "full"
    private = _scope(root, scope, owner_id)
    item, owner, source_pin, reference = _read_selected(root, scope, item_id, owner_id)
    extensions = item.get("extensions", {})
    semantic = extensions.get("goal") or extensions.get("personal_project")
    unknowns = ["RAM history, device enrollment and other repositories were not scanned; absence there is unknown.",
                "Completed linked work does not by itself prove the outcome met."]
    packet = {"profile": PROFILE, "outcome": "context", "scope": scope, "detail": detail,
              "selected": _compact(item, full), "kind": item.get("type"),
              "pins": {"item_revision": owner["item_revision"], "owner_path": owner["path"],
                       "owner_sha256": owner["sha256"], "source_pin": source_pin},
              "unknowns": unknowns, "advice": deepcopy(ADVICE)}
    if source_pin is None:
        unknowns.append("Native identity catalog is absent: no source commit pin or cross-repository link resolution.")
    elif source_pin.get("observation") != "committed":
        unknowns.append("The selected owner has uncommitted working-tree changes beyond the pinned commit.")
    if semantic is None:
        unknowns.append("Legacy record without a semantic outcome profile; only title and summary are known.")
        packet["semantic"] = None
        return packet
    if item.get("type") == "goal":
        packet["outcome_statement"] = _cut(semantic["outcome"], full)
        criteria = semantic["criteria"]
        packet["criteria"] = [{"key": c["key"], "statement": _cut(c["statement"], full), "assessment": c["assessment"],
                               "evidence_refs": c["evidence_refs"] if full else len(c["evidence_refs"]),
                               **({"basis": _cut(c["basis"], full)} if full and "basis" in c else {})}
                              for c in criteria]
        open_criteria = [c["key"] for c in criteria if c["assessment"] != "supported"]
        if open_criteria:
            unknowns.append("Criteria not yet supported: " + ", ".join(open_criteria) + ".")
        assessment = semantic.get("outcome_assessment")
        packet["outcome_assessment"] = assessment["assessment"] if assessment else "unassessed"
        if not assessment:
            unknowns.append("No explicit outcome assessment has been recorded.")
        links = {"work_links": semantic["work_links"]}
    else:
        packet["purpose"] = _cut(semantic["purpose"], full)
        links = {"goal_links": semantic["goal_links"], "work_links": semantic["work_links"]}
        if "completion_basis" in semantic:
            packet["completion_basis"] = _cut(semantic["completion_basis"], full)
    packet["scope_items"] = [_cut(t, full) for t in semantic["scope"]]
    packet["exclusions"] = [_cut(t, full) for t in semantic["exclusions"]]
    shown, budget = {}, FULL_LINKS if full else BRIEF_LINKS
    for name, refs in links.items():
        if full:
            take = refs[:max(0, budget)]
            shown[name] = [_link_status(root, ref, owner_id) for ref in take]
            budget -= len(take)
            if len(take) < len(refs):
                unknowns.append("%d %s beyond the %d-link bound were not resolved." % (len(refs) - len(take), name, FULL_LINKS))
        else:
            shown[name] = {"count": len(refs), "references": [{"item_id": r["item_id"], "repository_id": r["repository_id"]}
                                                                 for r in refs[:BRIEF_LINKS]]}
            if refs:
                unknowns.append("%s are listed unresolved in brief detail; use --detail full." % name)
    packet["links"] = shown
    if not full:
        packet["expand"] = "Repeat with --detail full for complete text, evidence references and resolved link status."
    return packet


_GOAL_FIELDS = {"outcome", "criteria", "scope", "exclusions", "work_links", "outcome_assessment"}
_PROJECT_FIELDS = {"purpose", "scope", "exclusions", "goal_links", "work_links", "completion_basis"}


def edit_form(root, scope, item_id, draft, owner_id=None):
    """Return an executable frozen form for a bounded edit of one selected record.

    Only title, summary, status and the semantic fields are editable here. The
    revision-bound form is then submitted through the existing guarded write.
    """
    ts.require(isinstance(draft, dict) and draft, "invalid", "Supply the bounded changes to make.")
    private = _scope(root, scope, owner_id)
    item, owner, _, _ = _read_selected(root, scope, item_id, owner_id)
    kind = item["type"]
    name = "goal" if kind == "goal" else "personal_project"
    fields = _GOAL_FIELDS if kind == "goal" else _PROJECT_FIELDS
    semantic_changes = draft.get("semantic", {})
    ts.require(set(draft) <= {"title", "summary", "status", "semantic"} and isinstance(semantic_changes, dict)
               and set(semantic_changes) <= fields, "invalid",
               "Edit only title, summary, status and these semantic fields: " + ", ".join(sorted(fields)))
    base = item.get("extensions", {}).get(name, {})
    changes = {key: draft[key] for key in ("title", "summary", "status") if key in draft}
    extensions = deepcopy(item.get("extensions", {}))
    extensions[name] = {**base, **deepcopy(semantic_changes)}
    changes["extensions"] = extensions
    if private:
        from . import personal
        form = personal.form(root, kind, changes, owner_id=owner_id, item_id=item_id)
    else:
        from . import goal_records
        form = goal_records.form_shared(root, owner["path"], item_id, changes)
    return {**form, "profile_of_edit": PROFILE, "outcome": "form", "scope": scope,
            "bounded_edit": {"item_id": item_id, "expected_revision": owner["item_revision"],
                             "changed_fields": sorted({*[k for k in draft if k != "semantic"], *semantic_changes})},
            "next": "Submit the complete form with `goal-context edit submit` (or the existing goals/personal submit); "
                    "a stale revision requires a fresh read and a deliberate new edit."}


def edit_submit(root, scope, request, owner_id=None, *, host, actor, session):
    """Delegate to the existing validated, revision-bound write for the scope."""
    private = _scope(root, scope, owner_id)
    request = request.get("request", request)
    ts.require(isinstance(request, dict), "invalid", "Request must be an object.")
    if private:
        from . import personal
        return personal.submit(root, request, owner_id=owner_id, host=host, actor=actor, session=session)
    from . import goal_records
    return goal_records.submit_shared(root, request, host=host, actor=actor, session=session)


def _load(path):
    ts.require(bool(path), "invalid", "Select --request JSON or - for stdin.")
    value = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text(encoding="utf-8-sig"))
    ts.require(isinstance(value, dict), "invalid", "Request must be an object.")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("select", "context", "edit"))
    parser.add_argument("operation", nargs="?", help="edit: form or submit.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--scope", choices=("shared", "personal"), default="shared")
    parser.add_argument("--owner-id", help="Required for --scope personal; never inferred.")
    parser.add_argument("--item-id")
    parser.add_argument("--detail", choices=("brief", "full"), default="brief")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--request", help="Bounded draft (edit form) or frozen form (edit submit); - for stdin.")
    parser.add_argument("--host")
    parser.add_argument("--actor")
    parser.add_argument("--session", help="Actual native caller session.")
    args = parser.parse_args(argv)
    try:
        # Scope and owner checks precede any request-file or stdin read.
        _scope(args.root, args.scope, args.owner_id)
        if args.command == "select":
            result = select(args.root, args.scope, args.owner_id, offset=args.offset, limit=args.limit,
                            include_closed=args.include_closed)
        elif args.command == "context":
            result = context(args.root, args.scope, args.item_id, args.owner_id, detail=args.detail)
        elif args.operation == "form":
            result = edit_form(args.root, args.scope, args.item_id, _load(args.request), args.owner_id)
        elif args.operation == "submit":
            ts.require(all(isinstance(v, str) and v.strip() for v in (args.host, args.actor, args.session)),
                       "invalid", "Submit requires --host, --actor and the actual --session.")
            result = edit_submit(args.root, args.scope, _load(args.request), args.owner_id,
                                 host=args.host, actor=args.actor, session=args.session)
        else:
            raise ts.ReadError("invalid", "Select edit form or edit submit.")
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2 if result.get("outcome") in {"error", "conflict", "partial", "denied", "unavailable"} else 0
    except (ts.ReadError, OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        private = args.scope == "personal"
        # Personal errors omit raw exceptions, paths, titles and input.
        print(json.dumps({"profile": PROFILE, "outcome": "error", "code": getattr(exc, "code", "invalid"),
                          "message": ("Personal operation could not be completed for the selected owner." if private
                                      else getattr(exc, "message", str(exc)))}, ensure_ascii=True, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
