"""Native, read-only user test records for ETT1.

Templates are portable suggestions. Project setups snapshot their chosen
template version; independent run records pin canonical setup content. Nothing
in these records is executable authority.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import tokenslang as ts

TEMPLATE_PROFILE = "eutonos.user-test.template.v1"
SETUP_PROFILE = "eutonos.user-test.setup.v1"
RUN_PROFILE = "eutonos.user-test.run.v1"
EVIDENCE_PROFILE = "eutonos.user-test.evidence.v1"
OUTCOMES = frozenset({"not-run", "passed", "failed", "blocked", "inconclusive"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WINDOWS_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL"}
                             | {f"COM{index}" for index in range(1, 10)}
                             | {f"LPT{index}" for index in range(1, 10)})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|working-tree)\Z")
MAX_RECORD_BYTES = 262144
MAX_SETUPS = 1000
MAX_RUNS_PER_SETUP = 1000

_DEFAULTS = (
    {"profile": TEMPLATE_PROFILE, "id": "behavior-regression", "version": 1,
     "title": "Behavior or regression", "purpose": "Check a specified behavior against its expected result.",
     "guidance": ["Name the behavior and the source revision being tested.",
                  "Choose representative inputs, including the prior failure when relevant.",
                  "State the observable expected result for each step."]},
    {"profile": TEMPLATE_PROFILE, "id": "user-workflow", "version": 1,
     "title": "User workflow", "purpose": "Check whether a person can complete a real task.",
     "guidance": ["Name the user's goal and starting conditions.",
                  "Record the sequence of user-visible actions and expected feedback.",
                  "Include completion criteria and any human observation limits."]},
    {"profile": TEMPLATE_PROFILE, "id": "comparison-experiment", "version": 1,
     "title": "Comparison or experiment", "purpose": "Compare selected variants against the same declared criteria.",
     "guidance": ["Name the variants and conditions before running them.",
                  "Use the same inputs and evaluation criteria for each variant.",
                  "Record measured or observed differences and limits."]},
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _shape(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    _require(isinstance(value, dict) and required <= value.keys()
             and not value.keys() - required - optional,
             "Missing or unsupported record fields")
    return value


def _text(value: object, label: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 8192,
             f"{label} must be nonempty bounded text")
    return value


def _id(value: object, label: str) -> str:
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None
             and not value.endswith(".") and value.split(".", 1)[0].upper() not in _WINDOWS_DEVICES,
             f"{label} must be a safe stable ID")
    return value


def _hash(value: object, label: str) -> str:
    _require(isinstance(value, str) and _HASH.fullmatch(value) is not None,
             f"{label} must be a lowercase SHA-256 digest")
    return value


def _path(value: object, label: str) -> str:
    _require(isinstance(value, str) and len(value) <= 1024 and "\\" not in value
             and ":" not in value and "\x00" not in value,
             f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    _require(value == path.as_posix() and not path.is_absolute()
             and all(part not in ("", ".", "..") for part in value.split("/")),
             f"{label} must stay inside the project")
    return value


def _strings(value: object, label: str) -> list[str]:
    _require(isinstance(value, list), f"{label} must be a list")
    for item in value:
        _text(item, label)
    return value


def _unique(items: list[dict], label: str) -> None:
    ids = [item["id"] for item in items]
    _require(len(ids) == len(set(ids)), f"Duplicate {label} ID")


def record_sha256(value: dict) -> str:
    """Hash canonical finite UTF-8 JSON, independent of file indentation."""
    return hashlib.sha256(ts.encoded(value)).hexdigest()


def _bounded_json(value: dict) -> None:
    _require(len(ts.encoded(value)) <= MAX_RECORD_BYTES, "Record exceeds 256 KiB")


def default_templates() -> list[dict]:
    """Return detached copies so callers cannot mutate packaged defaults."""
    return copy.deepcopy(_DEFAULTS)


def validate_setup(value: object) -> dict:
    row = _shape(value, {"profile", "id", "title", "purpose", "work_id", "readiness",
                         "template", "sources", "prerequisites", "inputs", "steps",
                         "criteria", "limits"})
    _require(row["profile"] == SETUP_PROFILE, "Unsupported setup profile")
    _id(row["id"], "Setup ID")
    _text(row["title"], "Setup title")
    _text(row["purpose"], "Setup purpose")
    if row["work_id"] is not None:
        _id(row["work_id"], "Work ID")
    _require(row["readiness"] in ("draft", "ready"), "Unsupported setup readiness")
    if row["template"] is not None:
        ref = _shape(row["template"], {"id", "version"})
        _id(ref["id"], "Template ID")
        _require(type(ref["version"]) is int and ref["version"] >= 1,
                 "Invalid template version")
    _require(isinstance(row["sources"], list), "Sources must be a list")
    for source in row["sources"]:
        source = _shape(source, {"path", "revision", "sha256"})
        _path(source["path"], "Source path")
        _require(isinstance(source["revision"], str)
                 and _REVISION.fullmatch(source["revision"]) is not None,
                 "Source revision must be a full commit or working-tree")
        _hash(source["sha256"], "Source digest")
    _require(len({source["path"] for source in row["sources"]}) == len(row["sources"]),
             "Duplicate selected source")
    for field in ("prerequisites", "inputs", "limits"):
        _strings(row[field], field)
    _require(isinstance(row["steps"], list), "Steps must be a list")
    for step in row["steps"]:
        step = _shape(step, {"id", "action", "expected"})
        _id(step["id"], "Step ID")
        _text(step["action"], "Step action")
        _text(step["expected"], "Expected result")
    _unique(row["steps"], "step")
    _require(isinstance(row["criteria"], list), "Criteria must be a list")
    for criterion in row["criteria"]:
        criterion = _shape(criterion, {"id", "description"})
        _id(criterion["id"], "Criterion ID")
        _text(criterion["description"], "Criterion description")
    _unique(row["criteria"], "criterion")
    if row["readiness"] == "ready":
        _require(bool(row["sources"]) and bool(row["steps"]) and bool(row["criteria"]),
                 "Ready setup requires source, steps and criteria")
    _bounded_json(row)
    return row


def instantiate_setup(template_id: str | None, *, setup_id: str, title: str,
                      purpose: str, work_id: str | None = None, readiness: str = "draft",
                      sources: list[dict] | None = None, prerequisites: list[str] | None = None,
                      inputs: list[str] | None = None, steps: list[dict] | None = None,
                      criteria: list[dict] | None = None, limits: list[str] | None = None) -> dict:
    """Build a project setup from explicit selections; None selects custom."""
    templates = {item["id"]: item for item in _DEFAULTS}
    _require(template_id is None or (isinstance(template_id, str) and template_id in templates),
             "Unknown starter template")
    template = None if template_id is None else {"id": template_id,
                                                  "version": templates[template_id]["version"]}
    row = {"profile": SETUP_PROFILE, "id": setup_id, "title": title, "purpose": purpose,
           "work_id": work_id, "readiness": readiness, "template": template,
           "sources": sources if sources is not None else [],
           "prerequisites": prerequisites if prerequisites is not None else [],
           "inputs": inputs if inputs is not None else [],
           "steps": steps if steps is not None else [],
           "criteria": criteria if criteria is not None else [],
           "limits": limits if limits is not None else []}
    validate_setup(row)
    return copy.deepcopy(row)


def validate_evidence(value: object) -> dict:
    row = _shape(value, {"profile", "items"})
    _require(row["profile"] == EVIDENCE_PROFILE, "Unsupported evidence profile")
    _require(isinstance(row["items"], list), "Evidence items must be a list")
    for item in row["items"]:
        item = _shape(item, set(), {"path", "sha256", "note"})
        _require(("path" in item) == ("sha256" in item),
                 "Evidence path and digest must be paired")
        _require("path" in item or "note" in item, "Evidence item must contain a path or note")
        if "path" in item:
            _path(item["path"], "Evidence path")
            _hash(item["sha256"], "Evidence digest")
        if "note" in item:
            _text(item["note"], "Evidence note")
    _bounded_json(row)
    return row


def validate_run(value: object) -> dict:
    """Validate a run against its immutable embedded setup snapshot."""
    row = _shape(value, {"profile", "id", "setup", "setup_id", "setup_sha256", "recorded_at",
                         "outcome", "summary", "observations", "evidence", "limits"})
    setup = validate_setup(row["setup"])
    _require(row["profile"] == RUN_PROFILE, "Unsupported run profile")
    _id(row["id"], "Run ID")
    _require(row["setup_id"] == setup["id"], "Run setup ID differs")
    _require(row["setup_sha256"] == record_sha256(setup), "Run setup digest differs")
    _require(isinstance(row["recorded_at"], str), "Run time is required")
    try:
        stamp = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Run time must be ISO 8601") from exc
    _require(stamp.tzinfo is not None, "Run time requires a time zone")
    _require(isinstance(row["outcome"], str) and row["outcome"] in OUTCOMES,
             "Unsupported run outcome")
    _require(setup["readiness"] == "ready" or row["outcome"] in ("not-run", "blocked"),
             "Only not-run or blocked may reference a draft setup")
    _text(row["summary"], "Run summary")
    _strings(row["limits"], "Run limits")
    _require(isinstance(row["observations"], list), "Observations must be a list")
    criteria = {criterion["id"] for criterion in setup["criteria"]}
    seen = set()
    observed = {}
    for item in row["observations"]:
        item = _shape(item, {"criterion_id", "outcome", "note"})
        _id(item["criterion_id"], "Criterion ID")
        _require(item["criterion_id"] in criteria and item["criterion_id"] not in seen,
                 "Unknown or duplicate criterion observation")
        _require(isinstance(item["outcome"], str)
                 and item["outcome"] in OUTCOMES - {"not-run"},
                 "Invalid criterion outcome")
        _text(item["note"], "Criterion observation")
        seen.add(item["criterion_id"])
        observed[item["criterion_id"]] = item["outcome"]
    evidence = validate_evidence(row["evidence"])
    if row["outcome"] == "passed":
        _require(seen == criteria and all(value == "passed" for value in observed.values())
                 and bool(evidence["items"]),
                 "Passed run requires every criterion to pass with explicit evidence")
    elif row["outcome"] == "failed":
        _require("failed" in observed.values() and bool(evidence["items"]),
                 "Failed run requires a failed observation and evidence")
    elif row["outcome"] == "not-run":
        _require(not seen and not evidence["items"], "Not-run cannot claim observations or evidence")
    _bounded_json(row)
    return row


def setup_path(setup_id: str) -> str:
    return f".eutonos/tests/setups/{_id(setup_id, 'Setup ID')}.json"


def run_path(setup_id: str, run_id: str) -> str:
    return f".eutonos/tests/runs/{_id(setup_id, 'Setup ID')}/{_id(run_id, 'Run ID')}.json"


def project_tests(root: str | Path) -> dict:
    """Read native records for a dashboard or agent; invalid files stay visible as diagnostics."""
    root = Path(root).resolve()
    base = root / ".eutonos" / "tests"
    rows, diagnostics = [], []
    def safe_directory(path):
        return (path.is_dir() and not path.is_symlink()
                and not getattr(path, "is_junction", lambda: False)()
                and path.resolve().is_relative_to(root))

    def read_record(path):
        _require(not path.is_symlink() and path.resolve().is_relative_to(root),
                 "Record escapes project")
        _require(path.stat().st_size <= MAX_RECORD_BYTES, "Record exceeds 256 KiB")
        return ts.loads(path.read_text(encoding="utf-8"))

    if not (root / ".eutonos").exists():
        return {"tests": rows, "diagnostics": diagnostics}
    if not safe_directory(root / ".eutonos"):
        return {"tests": rows, "diagnostics": [{"path": ".eutonos/tests", "error": "Unsafe test directory"}]}
    if not base.exists():
        return {"tests": rows, "diagnostics": diagnostics}
    if not safe_directory(base):
        return {"tests": rows, "diagnostics": [{"path": ".eutonos/tests", "error": "Unsafe test directory"}]}
    setup_dir = base / "setups"
    if not setup_dir.exists():
        return {"tests": rows, "diagnostics": diagnostics}
    if not safe_directory(setup_dir):
        return {"tests": rows, "diagnostics": [{"path": ".eutonos/tests/setups", "error": "Unsafe setup directory"}]}
    setup_files = list(itertools.islice(setup_dir.glob("*.json"), MAX_SETUPS + 1))
    if len(setup_files) > MAX_SETUPS:
        diagnostics.append({"path": ".eutonos/tests/setups", "error": "Setup scan limit reached"})
    for path in sorted(setup_files[:MAX_SETUPS]):
        relative = path.relative_to(root).as_posix()
        try:
            setup = validate_setup(read_record(path))
            _require(relative == setup_path(setup["id"]), "Setup path does not match ID")
            runs = []
            history_complete = True
            runs_root = base / "runs"
            run_dir = runs_root / setup["id"]
            if (runs_root.exists() or runs_root.is_symlink()) and not safe_directory(runs_root):
                history_complete = False
                diagnostics.append({"path": ".eutonos/tests/runs", "error": "Unsafe run directory"})
            elif (run_dir.exists() or run_dir.is_symlink()) and not safe_directory(run_dir):
                history_complete = False
                diagnostics.append({"path": run_dir.relative_to(root).as_posix(),
                                    "error": "Unsafe run directory"})
            elif safe_directory(run_dir):
                run_files = list(itertools.islice(run_dir.glob("*.json"), MAX_RUNS_PER_SETUP + 1))
                if len(run_files) > MAX_RUNS_PER_SETUP:
                    history_complete = False
                    diagnostics.append({"path": run_dir.relative_to(root).as_posix(),
                                        "error": "Run scan limit reached"})
                for run_file in sorted(run_files[:MAX_RUNS_PER_SETUP]):
                    run_relative = run_file.relative_to(root).as_posix()
                    try:
                        run = validate_run(read_record(run_file))
                        _require(run_relative == run_path(setup["id"], run["id"]),
                                 "Run path does not match ID")
                        _require(run["setup_id"] == setup["id"], "Run belongs to another setup")
                        status = "current" if run["setup_sha256"] == record_sha256(setup) else "stale"
                        runs.append({"record": run, "path": run_relative, "status": status})
                    except (OSError, UnicodeError, ValueError, ts.ReadError) as exc:
                        history_complete = False
                        diagnostics.append({"path": run_relative, "error": str(exc)})
            runs.sort(key=lambda item: (datetime.fromisoformat(
                item["record"]["recorded_at"].replace("Z", "+00:00")).timestamp(),
                item["record"]["id"]))
            current = [item for item in runs if item["status"] == "current"]
            rows.append({"setup": setup, "path": relative, "runs": runs,
                         "run_count": len(current), "run_count_partial": not history_complete,
                         "history_complete": history_complete,
                         "latest_run": current[-1]["record"] if history_complete and current else None,
                         "stale_runs": [item for item in runs if item["status"] == "stale"]})
        except (OSError, UnicodeError, ValueError, ts.ReadError) as exc:
            diagnostics.append({"path": relative, "error": str(exc)})
    return {"tests": rows, "diagnostics": diagnostics}
