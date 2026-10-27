"""Create and record project user tests; never execute their steps."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import tokenslang as ts
from . import user_tests as tests
from .local_lock import local_lock


class UserTestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _need(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise UserTestError(code, message)


def _safe(root: Path, path: Path, *, create_parent: bool = False) -> Path:
    """Reject symlinks/junctions at every project-owned path component."""
    root = root.resolve()
    absolute = path if path.is_absolute() else root / path
    try:
        parts = absolute.relative_to(root).parts
    except ValueError as exc:
        raise UserTestError("invalid", "Path must stay inside the selected project") from exc
    _need(bool(parts) and all(part not in ("", ".", "..") for part in parts),
          "invalid", "Invalid project path")
    current = root
    for part in parts[:-1]:
        current = current / part
        if create_parent and not current.exists():
            current.mkdir()
        _need(current.is_dir() and not current.is_symlink()
              and not getattr(current, "is_junction", lambda: False)()
              and current.resolve().is_relative_to(root),
              "invalid", "Unsafe project directory")
    target = current / parts[-1]
    _need(not target.is_symlink() and not getattr(target, "is_junction", lambda: False)()
          and target.resolve().is_relative_to(root), "invalid", "Unsafe project file")
    return target


def _load(root: Path, selected: str) -> object:
    if selected == "-":
        body = sys.stdin.buffer.read(tests.MAX_RECORD_BYTES + 1)
    else:
        path = _safe(root, Path(selected))
        _need(path.is_file(), "unavailable", "Input file is missing")
        with path.open("rb") as stream:
            body = stream.read(tests.MAX_RECORD_BYTES + 1)
    _need(len(body) <= tests.MAX_RECORD_BYTES, "invalid", "Input exceeds 256 KiB")
    try:
        return ts.loads(body.decode("utf-8"))
    except UnicodeError as exc:
        raise UserTestError("invalid", "Input must be UTF-8 JSON") from exc


def _request(root: Path, selected: str | None, allowed: set[str]) -> dict:
    value = _load(root, selected) if selected else {}
    _need(isinstance(value, dict) and not value.keys() - allowed,
          "invalid", "Request has unsupported fields")
    return value


def _setup(root: Path, setup_id: str) -> dict:
    path = _safe(root, Path(tests.setup_path(setup_id)))
    _need(path.is_file(), "unavailable", "Setup is missing")
    return tests.validate_setup(_load(root, path.relative_to(root).as_posix()))


def _bytes(value: dict) -> bytes:
    readable = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
    return readable if len(readable) <= tests.MAX_RECORD_BYTES else ts.encoded(value)


def _write(root: Path, relative: str, value: dict, *, replace: bool) -> None:
    path = _safe(root, Path(relative), create_parent=True)
    if not replace:
        _need(not path.exists(), "conflict", "Native record already exists")
    descriptor, temporary = tempfile.mkstemp(prefix=".user-test-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise UserTestError("conflict", "Native record already exists") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _lock(root: Path):
    """Check the local lock location before using the shared project lock."""
    _safe(root, Path(".eutonos-local/.locks/metadata.lock"), create_parent=True)
    return local_lock(root)


def setup_form(root: Path, template_id: str, request: str | None = None,
               *, setup_id: str | None = None, title: str | None = None,
               purpose: str | None = None) -> dict:
    allowed = {"setup_id", "title", "purpose", "work_id", "readiness", "sources",
               "prerequisites", "inputs", "steps", "criteria", "limits"}
    values = _request(root, request, allowed)
    values.update({key: value for key, value in (("setup_id", setup_id), ("title", title),
                                                  ("purpose", purpose)) if value is not None})
    _need(all(key in values for key in ("setup_id", "title", "purpose")),
          "invalid", "Setup form requires setup_id, title and purpose")
    return tests.instantiate_setup(None if template_id == "custom" else template_id, **values)


def run_form(root: Path, setup_id: str, run_id: str, request: str | None = None) -> dict:
    values = _request(root, request, {"recorded_at", "outcome", "summary", "observations",
                                      "evidence", "limits"})
    setup = _setup(root, setup_id)
    row = {"profile": tests.RUN_PROFILE, "id": run_id, "setup": setup,
           "setup_id": setup_id, "setup_sha256": tests.record_sha256(setup),
           "recorded_at": values.get("recorded_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
           "outcome": values.get("outcome", "not-run"),
           "summary": values.get("summary", "Not run."),
           "observations": values.get("observations", []),
           "evidence": values.get("evidence", {"profile": tests.EVIDENCE_PROFILE, "items": []}),
           "limits": values.get("limits", [])}
    return tests.validate_run(row)


def validate_input(root: Path, selected: str, kind: str | None = None) -> dict:
    value = _load(root, selected)
    _need(isinstance(value, dict), "invalid", "Record must be an object")
    if kind is None:
        profile = value.get("profile")
        _need(isinstance(profile, str), "invalid", "Unknown setup or run profile")
        kind = {tests.SETUP_PROFILE: "setup", tests.RUN_PROFILE: "run"}.get(profile)
    _need(kind in ("setup", "run"), "invalid", "Unknown setup or run profile")
    validated = tests.validate_setup(value) if kind == "setup" else tests.validate_run(value)
    return {"outcome": "valid", "kind": kind, "id": validated["id"],
            "sha256": tests.record_sha256(validated)}


def save_setup(root: Path, selected: str, expected_sha256: str | None = None) -> dict:
    value = tests.validate_setup(_load(root, selected))
    if expected_sha256 is not None:
        _need(re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
              "invalid", "Expected digest must be lowercase SHA-256")
    relative = tests.setup_path(value["id"])
    with _lock(root):
        path = _safe(root, Path(relative), create_parent=True)
        if expected_sha256 is None:
            _need(not path.exists(), "conflict", "Setup already exists; supply expected digest to update")
        else:
            _need(path.is_file(), "conflict", "Setup is missing; expected digest cannot update")
            current = tests.validate_setup(_load(root, relative))
            _need(tests.record_sha256(current) == expected_sha256,
                  "conflict", "Setup changed since the expected digest")
        _write(root, relative, value, replace=expected_sha256 is not None)
    return {"outcome": "saved", "kind": "setup", "path": relative,
            "sha256": tests.record_sha256(value)}


def append_run(root: Path, selected: str) -> dict:
    value = tests.validate_run(_load(root, selected))
    relative = tests.run_path(value["setup_id"], value["id"])
    with _lock(root):
        current = _setup(root, value["setup_id"])
        _need(tests.record_sha256(current) == value["setup_sha256"],
              "conflict", "Run setup snapshot is stale")
        _write(root, relative, value, replace=False)
    return {"outcome": "saved", "kind": "run", "path": relative,
            "sha256": tests.record_sha256(value)}


def _form_output(root: Path, selected: str, kind: str, value: dict) -> dict:
    candidate = Path(selected)
    try:
        relative = (candidate if candidate.is_absolute() else root / candidate).relative_to(root).as_posix()
    except ValueError as exc:
        raise UserTestError("invalid", "Form output must stay inside the selected project") from exc
    _need(not relative.casefold().startswith(".eutonos/tests/"), "invalid",
          "Form output cannot be a native test record")
    _safe(root, Path(relative), create_parent=True)
    _write(root, relative, value, replace=False)
    return {"outcome": "formed", "kind": kind, "path": relative,
            "sha256": tests.record_sha256(value)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("templates")
    setup = commands.add_parser("setup-form")
    setup.add_argument("--template", required=True,
                       choices=[item["id"] for item in tests.default_templates()] + ["custom"])
    setup.add_argument("--setup-id")
    setup.add_argument("--title")
    setup.add_argument("--purpose")
    setup.add_argument("--request")
    setup.add_argument("--output", help="Create a UTF-8 JSON form inside the project")
    run = commands.add_parser("run-form")
    run.add_argument("--setup-id", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--request")
    run.add_argument("--output", help="Create a UTF-8 JSON form inside the project")
    validate = commands.add_parser("validate")
    validate.add_argument("--input", required=True)
    validate.add_argument("--kind", choices=("setup", "run"))
    saved = commands.add_parser("save-setup")
    saved.add_argument("--input", required=True)
    saved.add_argument("--expected-sha256")
    appended = commands.add_parser("append-run")
    appended.add_argument("--input", required=True)
    for child in commands.choices.values():
        child.add_argument("--root", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        root = Path(args.root).resolve(strict=True)
        _need(root.is_dir(), "invalid", "Selected project root must be a directory")
        if args.command == "templates":
            result = {"templates": tests.default_templates()}
        elif args.command == "setup-form":
            result = setup_form(root, args.template, args.request,
                                setup_id=args.setup_id, title=args.title, purpose=args.purpose)
            if args.output:
                result = _form_output(root, args.output, "setup", result)
        elif args.command == "run-form":
            result = run_form(root, args.setup_id, args.run_id, args.request)
            if args.output:
                result = _form_output(root, args.output, "run", result)
        elif args.command == "validate":
            result = validate_input(root, args.input, args.kind)
        elif args.command == "save-setup":
            result = save_setup(root, args.input, args.expected_sha256)
        else:
            result = append_run(root, args.input)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return 0
    except UserTestError as exc:
        error = {"outcome": "error", "code": exc.code, "message": str(exc)}
    except (ValueError, ts.ReadError) as exc:
        error = {"outcome": "error", "code": "invalid", "message": str(exc)}
    except OSError as exc:
        error = {"outcome": "error", "code": "unavailable", "message": str(exc)}
    print(json.dumps(error, ensure_ascii=True, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
