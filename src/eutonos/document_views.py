"""Read structured change history and shared optional display metadata."""
from datetime import date
import json
import re
from .effects import LocalRoot
from .source import safe_path
from . import tokenslang as ts

CHANGE = "eutonos.tokenslang.change.v1"
CONFIG = "eutonos.tokenslang.changes.v1"


def metadata(item):
    values = item.get("extensions", {})
    values = {**values, **{k: item[k] for k in ("area", "module", "date", "recorded_at") if k in item}}
    result = {}
    for field in ("area", "module"):
        if field in values:
            if not isinstance(values[field], str):
                raise ValueError(field + " must be text")
            result[field] = values[field]
    for field in ("recorded_on", "completed_on", "date"):
        if values.get(field) is not None:
            raw = values[field]
            if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                raise ValueError(field + " must be YYYY-MM-DD")
            date.fromisoformat(raw)
            result[field] = raw
    if values.get("recorded_at") is not None:
        raw = values["recorded_at"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw):
            raise ValueError("recorded_at must be UTC YYYY-MM-DDTHH:MM:SSZ")
        result["recorded_at"] = raw
        result["display_time"] = raw
    result["display_date"] = result.get("completed_on") or result.get("recorded_on") or result.get("date")
    result["date_kind"] = "Completed" if result.get("completed_on") else "Recorded" if result.get("recorded_on") else "Recorded" if result.get("date") else "Undated"
    return result


def boards_view(value):
    from . import completion_checks
    value = {**value, "items": [dict(i) for i in value.get("items", [])], "diagnostics": list(value.get("diagnostics", []))}
    for item in value["items"]:
        checks = item.get("extensions", {}).get("completion_checks")
        if checks is not None:
            try:
                counted = completion_checks.summary(checks)
                if counted:
                    item["completion_check_summary"] = counted
            except (TypeError, ValueError, ts.ReadError) as exc:
                value["diagnostics"].append("Completion checks unavailable for " + item["id"] + ": " + str(exc))
                value["status"] = "partial"
        try:
            item["display"] = metadata(item)
        except (TypeError, ValueError) as exc:
            item["display"] = {"display_date": None, "date_kind": "Undated"}
            value["diagnostics"].append("Invalid optional metadata for " + item["id"] + ": " + str(exc))
            value["status"] = "partial"
    return value


def _blocks(text):
    fence = None
    body = []
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None:
            if match:
                fence = (match[1][0], len(match[1]), match[2].strip() == "tokenslang-change")
                body = []
        elif match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
            if fence[2]:
                yield "\n".join(body)
            fence = None
        elif fence[2]:
            body.append(line)
    if fence and fence[2]:
        raise ValueError("Unclosed TokenSlang change block")


def changes(root):
    local = LocalRoot(root)
    result = {"status": "available", "items": [], "owners": [], "diagnostics": []}
    try:
        raw = local.read("eutonos.changes.json")
        configured = raw is not None
        if raw is None:
            owners = ["docs/CHANGELOG.md"]
        else:
            config = json.loads(raw.decode("utf-8"))
            if not isinstance(config, dict) or config.get("profile") != CONFIG or not isinstance(config.get("owners"), list):
                raise ValueError("Invalid changes owner configuration")
            owners = config["owners"]
        if len(owners) != len(set(owners)):
            raise ValueError("Duplicate changes owner")
        owners = [safe_path(p) for p in owners]
    except (OSError, ValueError, TypeError, ts.ReadError) as exc:
        return {**result, "status": "error", "diagnostics": [str(exc)]}
    result["owners"] = owners
    seen = set()
    for owner in owners:
        try:
            raw = local.read(owner)
            if raw is None:
                result["diagnostics"].append("Change owner absent: " + owner)
                continue
            for body in _blocks(raw.decode("utf-8")):
                item = json.loads(body)
                if not isinstance(item, dict) or item.get("profile") != CHANGE or any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("id", "title", "summary", "status")):
                    raise ValueError("Invalid change record")
                if item["status"] not in {"completed", "continuing", "blocked"}:
                    raise ValueError("Invalid change disposition")
                if item["id"] in seen:
                    raise ValueError("Duplicate change ID: " + item["id"])
                seen.add(item["id"])
                result["items"].append({**item, "type": "change", "owner_path": owner, "display": metadata(item)})
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
            result["diagnostics"].append(owner + ": " + str(exc))
    result["items"].sort(key=lambda i: i["display"].get("display_date") or "", reverse=True)
    if result["diagnostics"]:
        result["status"] = "partial"
    if not result["items"] and not configured and raw is None and len(owners) == 1 and owners[0] == "docs/CHANGELOG.md":
        result["status"] = "not-configured"
    return result
