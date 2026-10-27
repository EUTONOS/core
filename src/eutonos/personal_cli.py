"""Explicit owner-private goals, projects, history and backup operations.

Use bootstrap form/submit for a new private root, then form/prepare/submit and
recover the original operation. These commands never invoke shared closeout.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from . import personal, private_scope, tokenslang as ts


def _load(path):
    ts.require(bool(path), "invalid", "Select --request JSON or - for stdin.")
    value = json.load(sys.stdin) if path == "-" else json.loads(Path(path).read_text(encoding="utf-8-sig"))
    ts.require(isinstance(value, dict), "invalid", "Request must be an object.")
    return value


def _request(path):
    value = _load(path)
    request = value.get("request", value)
    ts.require(isinstance(request, dict), "invalid", "Request must be an object.")
    return request


def _caller(args):
    ts.require(all(isinstance(v, str) and v.strip() for v in (args.actor, args.session)),
               "invalid", "Writes require --actor and actual caller --session.")


def _request_form(operation, args, draft):
    request = {"profile": "eutonos.personal-cli-operation.v1", "operation": operation,
               "operation_id": str(uuid.uuid4()), "owner_id": args.owner_id, **draft}
    ts.require(request["owner_id"] == args.owner_id and request["operation"] == operation,
               "denied", "Request must match selected personal owner and operation.")
    required = ()
    if operation == "bootstrap":
        request.setdefault("identity", {"repository_id": str(uuid.uuid4()),
                                        "scope_id": str(uuid.uuid4()), "namespace": "personal"})
        # Validate the frozen identity without writing files.
        personal.initial_files(args.owner_id, identity=request["identity"])
    elif operation == "configure":
        from .personal_history import read_config
        current = read_config(args.root, args.owner_id)
        request.setdefault("branch", args.branch or current.get("branch", "refs/heads/personal"))
        request.setdefault("expected_revision", args.expected_revision if args.expected_revision is not None else current["revision"])
    elif operation == "export":
        if args.destination:
            request.setdefault("destination", args.destination)
        required = ("destination",)
    elif operation == "restore":
        if args.destination:
            request.setdefault("destination", args.destination)
        if args.archive:
            request.setdefault("archive", args.archive)
        required = ("destination", "archive")
    return {"outcome": "form", "request": request,
            "missing_fields": [key for key in required if not request.get(key)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "host", "read", "form", "prepare", "verify",
                                            "submit", "recover", "history", "export", "restore"))
    parser.add_argument("operation", nargs="?", help="bootstrap/export/restore: form|submit; host: form|enroll|status; history: form|configure.")
    parser.add_argument("--root", required=True, help="Explicit selected private root; bootstrap creates only an empty root.")
    parser.add_argument("--owner-id", help="Explicit personal owner required before any private content is read.")
    parser.add_argument("--kind", choices=("goal", "project", "priorities"))
    parser.add_argument("--item-id")
    parser.add_argument("--request", help="Request or complete form JSON file; - reads stdin.")
    parser.add_argument("--host", help="Dedicated host returned by personal host form; never a shared host.")
    parser.add_argument("--actor")
    parser.add_argument("--session", help="Actual native caller session; never inferred from environment.")
    parser.add_argument("--operation-id", help="Original frozen operation to recover.")
    parser.add_argument("--destination", help="Explicit selected private export/restore destination.")
    parser.add_argument("--archive", help="Selected private backup archive.")
    parser.add_argument("--branch", help="Selected private history ref; defaults to the observed branch.")
    parser.add_argument("--expected-revision", type=int, help="Observed history configuration revision; form defaults to current.")
    args = parser.parse_args(argv)
    try:
        ts.require(isinstance(args.owner_id, str) and bool(args.owner_id.strip()),
                   "denied", "Personal scope requires a valid selected owner.")
        creating = args.command in {"bootstrap", "restore"}
        if not creating:
            private_scope.require_owner(args.root, args.owner_id)
        if args.command in {"bootstrap", "export", "restore", "history"}:
            operation = "configure" if args.command == "history" else args.command
            if args.operation == "form":
                result = _request_form(operation, args, _request(args.request) if args.request else {})
            else:
                expected = "configure" if args.command == "history" else "submit"
                ts.require(args.operation == expected, "invalid", "Select form or the supported write operation.")
                _caller(args)
                request = _request(args.request)
                ts.require(request.get("profile") == "eutonos.personal-cli-operation.v1" and
                           request.get("owner_id") == args.owner_id and request.get("operation") == operation and
                           isinstance(request.get("operation_id"), str) and bool(request["operation_id"].strip()),
                           "denied", "Select the original owner-bound operation form.")
                if args.command == "bootstrap":
                    from . import personal_history
                    result = personal_history.initialize(args.root,
                        personal.initial_files(args.owner_id, identity=request["identity"]),
                        owner_id=args.owner_id, session=args.session)
                    # Host admission is fresh and filesystem-bound, after the initial commit.
                    if result.get("state") == "committed" or result.get("history", {}).get("state") == "committed":
                        from .host import enroll
                        selected = private_scope.private_host(args.root)
                        if selected.exists():
                            from .host import status
                            admission = status(args.root, selected, args.actor)
                        else:
                            selected.parent.mkdir(parents=True, exist_ok=True)
                            admission = enroll(args.root, selected, args.actor, list(private_scope.OWNERS.values()))
                        result = {**result, "host": str(selected), "host_admission": admission}
                    else:
                        result = {**result, "next": "Repeat bootstrap submit with the same frozen request and session after resolving the reported history error."}
                elif args.command == "history":
                    from . import personal_history
                    result = personal_history.configure_local(args.root, owner_id=args.owner_id,
                        branch=request["branch"], expected_revision=request["expected_revision"])
                elif args.command == "export":
                    from . import personal_backup
                    ts.require(bool(args.host), "invalid", "Export requires the selected dedicated --host.")
                    result = personal_backup.export(args.root, request["destination"],
                        owner_id=args.owner_id, host=args.host, actor=args.actor)
                else:
                    from . import personal_backup
                    ts.require(private_scope.validate_private_destination(args.root)["status"] == "allowed",
                               "denied", "Select a supported private restore destination.")
                    ts.require(Path(request["destination"]).resolve() == Path(args.root).resolve(),
                               "denied", "Restore destination must be the explicitly selected root.")
                    result = personal_backup.restore(request["archive"], request["destination"], owner_id=args.owner_id)
        elif args.command == "host":
            selected = private_scope.private_host(args.root)
            if args.host is not None:
                ts.require(Path(args.host).resolve() == selected.resolve(), "denied", "Select the dedicated private host.")
            if args.operation == "form":
                result = {"outcome": "form", "host": str(selected), "owner_id": args.owner_id,
                          "paths": list(private_scope.OWNERS.values()),
                          "request": {"profile": "eutonos.personal-cli-operation.v1", "operation": "host-enroll",
                                      "operation_id": str(uuid.uuid4()), "owner_id": args.owner_id, "host": str(selected)},
                          "next": "personal host enroll --request FORM.json --root ROOT --owner-id OWNER --actor ACTOR --session SESSION"}
            elif args.operation == "enroll":
                _caller(args)
                if args.request:
                    request = _request(args.request)
                    ts.require(request.get("profile") == "eutonos.personal-cli-operation.v1" and
                               request.get("operation") == "host-enroll" and request.get("owner_id") == args.owner_id and
                               Path(request["host"]).resolve() == selected.resolve(),
                               "denied", "Select the original dedicated host form.")
                from .host import enroll
                selected.parent.mkdir(parents=True, exist_ok=True)
                result = enroll(args.root, selected, args.actor, list(private_scope.OWNERS.values()))
            elif args.operation == "status":
                from .host import status
                result = status(args.root, selected, args.actor)
            else:
                raise ts.ReadError("invalid", "Select host form, enroll or status.")
        elif args.command == "read":
            result = personal.read(args.root, owner_id=args.owner_id, kind=args.kind, item_id=args.item_id)
        elif args.command == "form":
            ts.require(bool(args.kind), "invalid", "Select --kind goal, project or priorities.")
            result = personal.form(args.root, args.kind, _request(args.request) if args.request else None,
                                   owner_id=args.owner_id, item_id=args.item_id)
        elif args.command == "prepare":
            result = personal.prepare(args.root, _request(args.request), owner_id=args.owner_id)
        elif args.command == "verify":
            personal.verify(args.root, _load(args.request), owner_id=args.owner_id)
            result = {"outcome": "verified", "current_verified": True}
        else:
            _caller(args)
            ts.require(bool(args.host), "invalid", "Private writes require their dedicated --host.")
            if args.command == "submit":
                result = personal.submit(args.root, _request(args.request), owner_id=args.owner_id,
                                         host=args.host, actor=args.actor, session=args.session)
            else:
                ts.require(bool(args.operation_id), "invalid", "Recovery requires original --operation-id.")
                result = personal.recover(args.root, args.operation_id, owner_id=args.owner_id,
                                          host=args.host, actor=args.actor, session=args.session)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 2 if (result.get("outcome") in {"error", "conflict", "partial", "denied", "unavailable"}
                     or result.get("history", {}).get("state") in {"pending", "uncertain"}) else 0
    except (ts.ReadError, OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # Private operations deliberately omit raw exceptions, paths, titles and input.
        print(json.dumps({"outcome": "error", "code": getattr(exc, "code", "invalid"),
                          "message": "Personal operation could not be completed for the selected owner."}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
