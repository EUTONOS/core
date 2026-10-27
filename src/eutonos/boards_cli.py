"""Read native boards or prepare/apply exact guarded board edits."""
import argparse
import json
from pathlib import Path
import sys

from . import boards
from .private_scope import require_shared
from . import tokenslang as ts
from .host import connect, selected_apply


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("profile")
    commands.add_parser("read")
    commands.add_parser("categories")
    form = commands.add_parser("form")
    form.add_argument("--type", choices=(*boards.WORK_FORM_TYPES, *boards.CATEGORY_TYPES), required=True)
    form.add_argument("--request")
    owner = commands.add_parser('owner', help='Create and register a native board owner through an atomic batch.')
    owner.add_argument('operation', choices=('form',))
    owner.add_argument('--path', required=True)
    owner.add_argument('--title', required=True)
    owner.add_argument('--document-id', help='Optional explicit registry document ID, e.g. eutonos.doc.some-name (needs --role).')
    owner.add_argument('--role', help='Registry role for an explicit --document-id.')
    owner.add_argument('--intro', help='Optional plain-prose header paragraph(s) placed above the board fence.')
    planned = commands.add_parser("plan")
    planned.add_argument("operation", choices=("add", "update", "reorder"))
    planned.add_argument("--request", required=True)
    applied = commands.add_parser("apply")
    applied.add_argument("--request", required=True)
    applied.add_argument("--host", required=True)
    applied.add_argument("--actor", required=True)
    applied.add_argument("--attempt", required=True)
    applied.add_argument("--generation", required=True, type=int)
    applied.add_argument("--effect-id", required=True)
    applied.add_argument("--fingerprint", required=True)
    submitted = commands.add_parser("submit-update")
    submitted.add_argument("--request", required=True,
                           help="Semantic board update request with its observed owner SHA-256; retries preserve same-item intent while reconciling unrelated owner changes.")
    submitted.add_argument("--host", required=True)
    submitted.add_argument("--actor", required=True)
    batch = commands.add_parser("batch", help="One atomic request containing multiple typed board/work changes.")
    batch.add_argument("operation", choices=("form", "plan", "submit"))
    batch.add_argument("--request")
    batch.add_argument("--host")
    batch.add_argument("--actor")
    imported = commands.add_parser("import", help="Source-derived import form; submit through batch.")
    imported.add_argument("operation", choices=("form",))
    imported.add_argument("--host")
    imported.add_argument("--actor")
    work = commands.add_parser("work", help="Lifecycle, closeout filing and cross-board priority.")
    work.add_argument("operation", choices=("form", "folders", "plan", "submit"))
    work.add_argument("--item-id")
    work.add_argument("--request")
    work.add_argument("--host")
    work.add_argument("--actor")
    for child in commands.choices.values():
        child.add_argument("--root", default=argparse.SUPPRESS)
    for child in (planned, submitted, batch, work):
        child.add_argument('--app', help='Actual caller app when available.')
        child.add_argument('--model', help='Actual caller model when available.')
        child.add_argument('--chat', help='Actual caller session when available.')
    args = parser.parse_args(argv)
    def caller(value):
        supplied = {k:getattr(args,v,None) for k,v in (('app','app'),('model','model'),('session','chat'))}
        if any(v is not None for v in supplied.values()):
            value = {**value,'caller_context': {**value.get('caller_context',{}), **{k:v for k,v in supplied.items() if v is not None}}}
        return value
    try:
        if args.command != "profile":
            require_shared(args.root)
        if args.command == 'owner':
            from .native_owner import form as owner_form
            result = owner_form(args.root, args.path, args.title, args.document_id, args.role, args.intro)
        elif args.command == "import":
            from . import board_import
            ts.require(bool(args.host) == bool(args.actor), "invalid", "Import form requires --host and --actor together.")
            if args.host:
                from .host import connect
                _, authority = connect(args.root, args.host, args.actor)
                result = board_import.form(args.root, authority=authority)
            else:
                result = board_import.form(args.root)
        elif args.command == "batch":
            from . import write_batch
            if args.operation == "form":
                result = write_batch.form(args.root, _load(args.request) if args.request else None)
            elif args.operation == "plan":
                ts.require(bool(args.request), "invalid", "Batch plan requires --request.")
                request = caller(_load(args.request))
                normalized = write_batch.normalize(request)
                imports = any(op["kind"] == "import" for op in normalized["operations"])
                if imports:
                    ts.require(bool(args.host) == bool(args.actor), "invalid", "Batch plan requires --host and --actor together.")
                if args.host and imports:
                    from .host import connect
                    _, authority = connect(args.root, args.host, args.actor)
                    result = write_batch.plan(args.root, request, authority=authority)
                else:
                    result = write_batch.plan(args.root, request)
            else:
                from . import write_retry
                ts.require(bool(args.request and args.host and args.actor), "invalid",
                           "Batch submit requires --request, --host and --actor.")
                result = write_retry.submit_batch(args.host, args.root, args.actor, caller(_load(args.request)))
        elif args.command == "profile":
            result = boards.profile_description()
        elif args.command == "read":
            result = boards.project(args.root)
        elif args.command == "work":
            from . import work_lifecycle
            if args.operation == "form":
                result = work_lifecycle.form(args.root, args.item_id)
            elif args.operation == "folders":
                result = work_lifecycle.folders(args.root)
            elif args.operation == "plan":
                result = work_lifecycle.plan(args.root, caller(_load(args.request)))
            else:
                from . import write_retry
                ts.require(bool(args.host and args.actor and args.request), "invalid", "Work submit requires --request, --host and --actor.")
                result = write_retry.submit_work(args.host, args.root, args.actor, caller(_load(args.request)))
        elif args.command == "form":
            draft = _load(args.request) if args.request else None
            result = (boards.catalog_form(args.type, draft, args.root) if args.type in boards.CATEGORY_TYPES
                      else boards.work_form(args.type, draft, args.root))
        elif args.command == "categories":
            result = {"categories": [*boards.category_choices(args.root), "Other"],
                      "custom_field": "extensions.category_other",
                      "note": "Custom categories become reusable when a catalog item is saved."}
        elif args.command == "plan":
            operation = {"add": boards.plan_add, "update": boards.plan_update,
                         "reorder": boards.plan_reorder}[args.operation]
            result = operation(args.root, caller(_load(args.request)))
        elif args.command == "apply":
            value = _load(args.request)
            ts.shape(value, {"profile", "effect", "fingerprint"},
                     {"outcome", "operation", "owner_path", "source_sha256", "result_sha256",
                      "item_ids", "written_ids", "paths", "qualification"})
            ts.require(value["profile"] == boards.PLAN and value["fingerprint"] == args.fingerprint,
                       "conflict", "Selected board plan or fingerprint differs.")
            coordinator, authority = connect(args.root, args.host, args.actor)
            result = selected_apply(coordinator, authority, args.attempt, args.generation,
                                    args.effect_id, value["effect"], args.fingerprint)
        else:
            from . import write_retry
            value = caller(_load(args.request))
            ts.shape(value, {"profile", "owner_path", "expected_sha256", "item_id", "changes"}, {"caller_context", "_attribution"})
            ts.require(value["profile"] == boards.UPDATE, "unsupported", "Submit-update requires a board update request.")
            result = write_retry.submit_board_update(args.host, args.root, args.actor,
                {"owner_path": value["owner_path"], "expected_sha256": value["expected_sha256"],
                 "item_id": value["item_id"], "changes": value["changes"],
                 **{k:value[k] for k in ("caller_context","_attribution") if k in value}})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("outcome") not in {"error", "conflict", "partial"} else 2
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, ts.ReadError) as exc:
        result = {"profile": boards.CONFIG, "outcome": "error",
                  "code": getattr(exc, "code", "invalid"), "message": str(exc)}
        if "extensions.category" in str(exc):
            result["missing_fields"] = ["extensions.category"]
            result["category_examples"] = list(boards.CATEGORY_EXAMPLES)
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
