"""Bounded timeline cards over already selected companion metadata."""
from copy import deepcopy
from pathlib import PurePosixPath, PureWindowsPath


FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
RECEIPT_PROFILE = "eutonos.companion-capture-receipt.v1"


def _origin(row):
    value = row.get("project_origin")
    if isinstance(value, dict) and value.get("scope") == "current-project" and isinstance(value.get("basis"), str):
        return deepcopy(value)
    return {"scope": "current-project", "basis": "legacy-retained-registration",
            "note": "Accepted retained record from this project's historical registration; native project origin was not recorded."}


def _active_receipts(capture):
    records = capture.get("receipts", []) if isinstance(capture, dict) else []
    items = [item for item in records if isinstance(item, dict)]
    superseded = {item.get("supersedes") for item in items if isinstance(item.get("supersedes"), str)}
    return [item for item in items if item.get("receipt_id") not in superseded]


def _safe_ram_path(value):
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.replace("\\", "/")
    windows, posix = PureWindowsPath(value), PurePosixPath(normalized)
    return not windows.is_absolute() and not windows.drive and not posix.is_absolute() and ".." not in posix.parts


def _usage(value):
    if not isinstance(value, dict):
        return dict.fromkeys(FIELDS)
    result = {field: value.get(field) for field in FIELDS}
    if (any(type(count) is not int or count < 0 for count in result.values())
            or result["cached_input_tokens"] > result["input_tokens"]
            or result["reasoning_output_tokens"] > result["output_tokens"]):
        return dict.fromkeys(FIELDS)
    return result


def _tools(value):
    if not isinstance(value, dict) or any(not isinstance(name, str) or type(count) is not int or count < 0
                                          for name, count in value.items()):
        return None
    return dict(value)


def _ram(row, receipts):
    thread, turn = row.get("thread_id"), row.get("turn_id")
    if not isinstance(thread, str) or not isinstance(turn, str):
        return []
    output = []
    for item in receipts:
        ram = item.get("ram")
        if ((item.get("thread_id"), item.get("turn_id")) != (thread, turn)
                or item.get("profile") != RECEIPT_PROFILE or item.get("stage") not in {"pre-final", "completed"}
                or item.get("disposition") not in {"captured", "no-op"} or not isinstance(ram, dict)):
            continue
        path, revision, summary = ram.get("path"), ram.get("revision"), item.get("summary")
        if (not _safe_ram_path(path) or type(revision) is not int or revision < 1
                or not isinstance(summary, str) or not isinstance(item.get("receipt_id"), str)
                or not isinstance(item.get("stage"), str)):
            continue
        output.append({"path": path, "revision": revision, "summary": summary,
                       "summary_basis": "Receipt-linked capture summary; not a RAM record summary.",
                       "receipt_id": item["receipt_id"], "stage": item["stage"]})
    return sorted(output, key=lambda item: (item["stage"], item["receipt_id"]))


def project(rows, capture=None):
    """Return cards only for rows already retained by the caller's scope."""
    receipts = _active_receipts(capture)
    turns, explicit, native, legacy = [], 0, 0, 0
    for row in rows:
        origin = _origin(row)
        if origin["basis"] == "explicit-source-registration":
            explicit += 1
        elif origin["basis"] == "native-project-session-origin":
            native += 1
        else:
            legacy += 1
        models = row.get("models")
        model = models[0] if isinstance(models, list) and len(models) == 1 and isinstance(models[0], str) else None
        usage, tools = _usage(row.get("usage")), _tools(row.get("tool_counts"))
        card = {key: row.get(key) for key in ("id", "thread_id", "turn_id", "start", "end", "state", "role", "experiment_id")}
        card.update({"model": model, "usage": usage, "tool_counts": tools,
                     "project_origin": origin, "ram": _ram(row, receipts)})
        if isinstance(row.get("request_id"), str) and row["request_id"]:
            card["request_id"] = row["request_id"]
        turns.append(card)
    return {"scope": {"basis": "Project-owned chats, including external-repository tasks and benchmarks, selected by explicit registration, native session origin or retained project history; tool working directories do not determine ownership.",
                       "explicit_registration_turn_count": explicit,
                       "native_project_turn_count": native,
                       "legacy_retained_registration_turn_count": legacy},
            "turns": turns, "diagnostics": []}
