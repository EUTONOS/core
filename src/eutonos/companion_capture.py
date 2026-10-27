"""Local companion capture receipts; semantic content remains in native RAM."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tempfile

from eutonos import ram, tokenslang as ts  # noqa: E402
from eutonos.effects import LocalRoot  # noqa: E402

PROFILE = "eutonos.companion-capture-receipt.v1"
STAGES = {"pre-final", "completed"}
DISPOSITIONS = {"captured", "no-op", "gap", "withdrawn"}
COVERAGE = {"summary-only", "full-exchange"}
COMPLETION_BASIS = {"assessment", "observed-final"}
ASSERTION_FIELDS = {"native_proof", "identity_basis", "evidence"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _native_lifecycle(log):
    """Read lifecycle metadata from one explicit log, without returning messages."""
    thread, turns, completed_order, active = None, {}, [], None
    with Path(log).open("rb") as stream:
        end = Path(log).stat().st_size
        while stream.tell() < end:
            raw = stream.readline(end - stream.tell())
            if not raw.endswith(b"\n"):
                break  # The host may still be appending its final event.
            event = json.loads(raw)
            value = event.get("payload") or {}
            if not isinstance(value, dict):
                continue
            kind = event.get("type")
            if kind == "session_meta":
                if thread and thread != value.get("id"):
                    raise ValueError("Native thread identity changed")
                thread = value.get("id")
            elif kind == "event_msg" and value.get("type") == "task_started":
                active = value.get("turn_id")
                if not active or active in turns:
                    raise ValueError("Missing or repeated native turn identity")
                turns[active] = {"state": "active", "final_observed": False}
            elif kind == "response_item" and active and value.get("role") == "assistant" and value.get("phase") == "final_answer":
                turns[active]["final_observed"] = True
            elif kind == "event_msg" and value.get("type") == "task_complete":
                completed = value.get("turn_id")
                if completed not in turns:
                    raise ValueError("Completion has no observed native start")
                turns[completed]["state"] = "completed"
                completed_order.append(completed)
                if completed == active:
                    active = None
    return thread, turns, completed_order, active


def native_identity(log, turn_id=None, stage="pre-final"):
    """Resolve lifecycle identity from one explicit log, without returning messages."""
    thread, turns, _, active = _native_lifecycle(log)
    if stage not in STAGES or not thread:
        raise ValueError("Native thread/stage unavailable")
    if stage == "completed" and not turn_id:
        raise ValueError("Completed capture requires an explicit turn ID")
    selected = turn_id or active
    value = turns.get(selected)
    if not value or (stage == "pre-final" and (selected != active or value["state"] != "active")):
        raise ValueError("Pre-final identity must be the current active native turn")
    if stage == "completed" and (value["state"] != "completed" or not value["final_observed"]):
        raise ValueError("Completed capture requires an observed native final and completion")
    return {"thread_id": thread, "turn_id": selected, "stage": stage, **value}


def latest_completed_identity(log):
    """Resolve the last completed native turn with an observed final."""
    thread, turns, completed, _ = _native_lifecycle(log)
    if not thread or not completed:
        raise ValueError("Native log has no completed turn")
    selected = completed[-1]
    value = turns[selected]
    if value["state"] != "completed" or not value["final_observed"]:
        raise ValueError("Latest completed turn has no observed native final")
    return {"thread_id": thread, "turn_id": selected, "stage": "completed", **value}


def capture_template(log, stage, turn_id=None):
    identity = native_identity(log, turn_id, stage)
    return {"thread_id": identity["thread_id"], "turn_id": identity["turn_id"], "stage": stage,
            "disposition": "gap", "coverage": "summary-only", "completion_basis": "assessment",
            "summary": "", "ram": None,
            "assertion": {"native_proof": "not-asserted", "identity_basis": "Observed explicit native session lifecycle; semantic capture still requires assessment"}}


def root_dir(root):
    return Path(root).resolve() / ".eutonos-local" / "capture"


def semantic(value):
    keys = ("thread_id", "turn_id", "stage", "disposition", "coverage", "completion_basis",
            "summary", "ram", "assertion", "supersedes")
    return {key: value.get(key) for key in keys}


def _safe_ram_path(value):
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(normalized)
    return not windows.is_absolute() and not windows.drive and not posix.is_absolute() and ".." not in posix.parts


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("Receipt must be an object")
    required = {"thread_id", "turn_id", "stage", "disposition", "coverage", "completion_basis",
                "summary", "ram", "assertion"}
    missing = required - value.keys()
    if missing:
        raise ValueError("Missing receipt fields: " + ", ".join(sorted(missing)))
    for key in ("thread_id", "turn_id", "summary"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("Receipt field must be a nonempty string: " + key)
    if (value["stage"] not in STAGES or value["disposition"] not in DISPOSITIONS or
            value["coverage"] not in COVERAGE or value["completion_basis"] not in COMPLETION_BASIS):
        raise ValueError("Unsupported receipt enum")
    if value["stage"] == "pre-final" and value.get("counts_completed", False):
        raise ValueError("Pre-final receipt cannot count as completed")
    if value["coverage"] == "summary-only" and value["completion_basis"] != "assessment":
        raise ValueError("Summary-only coverage requires assessment basis")
    if value["coverage"] == "full-exchange" and value["completion_basis"] != "observed-final":
        raise ValueError("Full-exchange coverage requires observed-final basis")
    assertion = value["assertion"]
    if (not isinstance(assertion, dict) or set(assertion) - ASSERTION_FIELDS or
            assertion.get("native_proof") not in {"verified-readback", "not-asserted"}):
        raise ValueError("Native proof must be explicit")
    if value["ram"] is not None:
        ram = value["ram"]
        if (not isinstance(ram, dict) or not _safe_ram_path(ram.get("path")) or
                type(ram.get("revision")) is not int or ram["revision"] < 1):
            raise ValueError("RAM reference must contain safe relative path and positive revision")
    if value["disposition"] == "captured" and value["ram"] is None:
        raise ValueError("Captured receipt requires a RAM reference")
    if value.get("supersedes") is not None and not isinstance(value["supersedes"], str):
        raise ValueError("supersedes must be a receipt ID or null")


def make_receipt(value, recorded_at=None):
    validate(value)
    if "recorded_at" in value:
        raise ValueError("recorded_at is generated when the receipt is persisted")
    body = semantic(value)
    content_id = hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
    return {"profile": PROFILE, "receipt_id": "capture-" + content_id[:24], "content_id": content_id,
            "recorded_at": recorded_at or timestamp(), **body,
            "counts_completed": value["stage"] == "completed" and value["disposition"] in {"captured", "no-op"}}


def verified_ram_reference(readback, read_request):
    """Derive an exact current RAM reference from a bounded native read result."""
    if (not isinstance(read_request, dict) or read_request.get("profile") != "eutonos.tokenslang.ram-read.v1" or
            read_request.get("operation") != "get" or not isinstance(read_request.get("identity"), dict) or
            not read_request["identity"] or type(read_request.get("revision")) is not int or
            read_request["revision"] < 1):
        raise ValueError("RAM request must identify one exact revision and owner")
    if (not isinstance(readback, dict) or readback.get("profile") != "eutonos.tokenslang.ram-read.v1" or
            readback.get("outcome") != "ok" or readback.get("complete") is not True):
        raise ValueError("RAM readback must be one complete successful native read")
    items = readback.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("RAM readback must contain exactly one item")
    item = items[0]
    identity = item.get("identity") if isinstance(item, dict) else None
    if identity != read_request["identity"]:
        raise ValueError("RAM readback identity does not match the expected owner")
    revision = item.get("record_revision")
    experience = item.get("experience")
    if (item.get("historical") is not False or type(revision) is not int or revision < 1 or
            revision != read_request["revision"] or not isinstance(experience, dict) or
            experience.get("revision") != revision or
            not _safe_ram_path(item.get("path"))):
        raise ValueError("RAM readback is not the exact latest revision")
    return {"path": item["path"], "revision": revision}


def completed_request(log, assessment, ram_request, ram_readback):
    """Bind a supplied assessment to the latest completed turn and exact RAM owner."""
    if not isinstance(assessment, dict):
        raise ValueError("Assessment must be an object")
    allowed = {"disposition", "coverage", "completion_basis", "summary"}
    if set(assessment) - allowed:
        raise ValueError("Assessment contains unsupported fields")
    identity = latest_completed_identity(log)
    if assessment.get("coverage") != "summary-only" or assessment.get("completion_basis") != "assessment":
        raise ValueError("Automated completed capture is limited to a summary-only assessment")
    ram = verified_ram_reference(ram_readback, ram_request)
    value = {"thread_id": identity["thread_id"], "turn_id": identity["turn_id"], "stage": "completed",
             "disposition": assessment.get("disposition"), "coverage": assessment.get("coverage"),
             "completion_basis": assessment.get("completion_basis"), "summary": assessment.get("summary"),
             "ram": ram, "assertion": {"native_proof": "verified-readback",
             "identity_basis": "Latest completed turn from explicit native lifecycle; exact current RAM owner and revision read back"}}
    validate(value)
    return value


def native_ram_readback(root, ram_request):
    """Read one exact RAM record for completed receipt composition.

    This is deliberately read-only.  The request still names one identity and
    revision, and ``verified_ram_reference`` applies the same current-record,
    non-historical checks used for a supplied native readback.
    """
    if (not isinstance(ram_request, dict) or ram_request.get("profile") != ram.READ or
            ram_request.get("operation") != "get"):
        raise ValueError("RAM request must be an exact native get")
    return ram.Store(LocalRoot(root)).read(ram_request)


def read_receipts(root):
    directory = root_dir(root)
    if not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob("capture-*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(item, dict) and item.get("profile") == PROFILE
                    and item.get("receipt_id") == path.stem):
                result.append((path, item))
        except (OSError, json.JSONDecodeError):
            continue
    return result


def _active_records(records):
    superseded = {item.get("supersedes") for _, item in records if item.get("supersedes")}
    return [(path, item) for path, item in records if item.get("receipt_id") not in superseded]


def write_receipt(root, value, supersedes=None, recorded_at=None):
    receipt = make_receipt({**value, "supersedes": supersedes}, recorded_at)
    records = read_receipts(root)
    for path, prior in records:
        if prior.get("receipt_id") == receipt["receipt_id"]:
            if prior.get("content_id") == receipt["content_id"]:
                return {"outcome": "idempotent", "receipt": prior, "path": str(path)}
            raise ValueError("Receipt identity already contains different content")
    active = _active_records(records)
    matching = [(path, prior) for path, prior in active
                if all(prior.get(key) == receipt.get(key) for key in ("thread_id", "turn_id", "stage"))]
    if supersedes is not None:
        if not any(prior.get("receipt_id") == supersedes and
                   prior.get("thread_id") == receipt.get("thread_id") and
                   prior.get("turn_id") == receipt.get("turn_id") for _, prior in active):
            raise ValueError("supersedes must identify the active receipt for this turn")
    elif any(prior.get("content_id") != receipt["content_id"] for _, prior in matching):
        raise ValueError("Conflicting receipt; provide explicit supersedes")
    directory = root_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (receipt["receipt_id"] + ".json")
    temp_path = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".capture-write-", suffix=".tmp", dir=directory)
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, path)
    except FileExistsError:
        for prior_path, prior in read_receipts(root):
            if prior.get("receipt_id") == receipt["receipt_id"] and prior.get("content_id") == receipt["content_id"]:
                return {"outcome": "idempotent", "receipt": prior, "path": str(prior_path)}
        raise ValueError("Receipt path already exists")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return {"outcome": "recorded", "receipt": receipt, "path": str(path)}


def write_and_read_receipt(root, value, supersedes=None, recorded_at=None):
    """Persist atomically and return the canonical receipt plus status evidence."""
    result = write_receipt(root, value, supersedes, recorded_at)
    receipt_id = result["receipt"]["receipt_id"]
    matches = [(path, item) for path, item in read_receipts(root) if item.get("receipt_id") == receipt_id]
    if len(matches) != 1 or matches[0][1] != result["receipt"]:
        raise ValueError("Persisted receipt readback differs from the recorded receipt")
    state = status(root)
    return {**result, "readback": matches[0][1],
            "counted_completed": receipt_id in state["counts_completed"],
            "counted_verified": receipt_id in state["counts_verified"]}


def status(root):
    items = [{"path": str(path), **item} for path, item in read_receipts(root)]
    superseded = {item.get("supersedes") for item in items if item.get("supersedes")}
    active = [item for item in items if item.get("receipt_id") not in superseded]
    completed_turns = {(item.get("thread_id"), item.get("turn_id")) for item in active if item.get("counts_completed")}
    pending = [item["receipt_id"] for item in active
               if ((item.get("disposition") == "gap" or item.get("stage") == "pre-final") and
                   (item.get("thread_id"), item.get("turn_id")) not in completed_turns)]
    return {"profile": PROFILE, "outcome": "ok", "receipts": items, "pending": pending,
            "counts_completed": [item["receipt_id"] for item in active if item.get("counts_completed")],
            "counts_verified": [item["receipt_id"] for item in active
                                if item.get("counts_completed") and
                                item.get("assertion", {}).get("native_proof") == "verified-readback"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "record", "record-completed"):
        item = sub.add_parser(command)
        item.add_argument("--root", default=".")
    sub.choices["record"].add_argument("--input", required=True, help="Exact JSON receipt request")
    sub.choices["record"].add_argument("--supersedes")
    sub.choices["record"].add_argument("--log", help="Verify request identity against one explicit native log before recording")
    completed = sub.choices["record-completed"]
    completed.add_argument("--log", required=True, help="Explicit native log containing the completed exchange")
    completed.add_argument("--assessment", required=True, help="Summary-only completed-exchange assessment JSON")
    completed.add_argument("--ram-request", required=True, help="Exact native RAM get request JSON")
    completed.add_argument("--ram-readback", help="Exact native RAM read output JSON (omit to fetch it read-only)")
    completed.add_argument("--supersedes", help="Only for an explicit conflicting completed receipt correction")
    template = sub.add_parser("template", help="Derive the native turn identity; unfilled output is not a receipt")
    template.add_argument("--log", required=True)
    template.add_argument("--stage", required=True, choices=sorted(STAGES))
    template.add_argument("--turn", help="Required for completed exchanges; pre-final defaults to current active turn")
    template.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            value = capture_template(args.log, args.stage, args.turn)
            with Path(args.output).open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            result = {"outcome": "template", "thread_id": value["thread_id"], "turn_id": value["turn_id"], "stage": args.stage,
                      "note": "Fill the assessment; no capture receipt or RAM write has occurred"}
        elif args.command == "status":
            result = status(args.root)
        elif args.command == "record-completed":
            assessment = json.loads(Path(args.assessment).read_text(encoding="utf-8"))
            ram_request = json.loads(Path(args.ram_request).read_text(encoding="utf-8"))
            readback = (json.loads(Path(args.ram_readback).read_text(encoding="utf-8"))
                        if args.ram_readback else native_ram_readback(args.root, ram_request))
            value = completed_request(args.log, assessment, ram_request, readback)
            result = write_and_read_receipt(args.root, value, args.supersedes)
        else:
            value = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if args.log:
                identity = native_identity(args.log, value.get("turn_id"), value.get("stage"))
                if identity["thread_id"] != value.get("thread_id"):
                    raise ValueError("Receipt thread does not match selected native log")
            result = write_and_read_receipt(args.root, value, args.supersedes)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ts.ReadError) as exc:
        print(json.dumps({"outcome": "error", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
