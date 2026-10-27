"""TokenSlang read.v1: explicit shared meanings, no compiler or effect dispatch.

Unknown fields are rejected except in an explicit optional `extensions` object.
Source data is never evaluated as instructions, code, schemas or authorization.
"""

from __future__ import annotations

import json
import re
from . import health
from typing import Any

LANGUAGE = "TokenSlang"
PROFILE = "eutonos.tokenslang.read.v1"
CATALOG = "eutonos.tokenslang.catalog.v1"
WORK = "eutonos.tokenslang.work.v1"
NAV = "eutonos.tokenslang.nav-fixture.v1"
RAM = "eutonos.tokenslang.ram-fixture.v1"
MAX_BYTES = 32768
MAX_SOURCE_BYTES = 131072
MAX_ITEMS = 64
OPERATIONS = {
    "describe": "Describe this read adapter's actual capabilities.",
    "resolve": "Resolve an exact stable reference or explicit locator.",
    "read": "Read UTF-8 source at the selected commit, with exact-source pages when needed.",
    "read.batch": "Read several caller-selected sources at one immutable commit.",
    "orient": "Read the native work owner and its declared context.",
    "nav.read": "Read a NAV fixture record and validate its source routes.",
    "ram.read": "Read a RAM fixture record with time, controls and dependencies.",
}


class ReadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ReadError(code, message)


def shape(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    require(isinstance(value, dict), "invalid", "Expected an object.")
    require(required <= value.keys(), "invalid", "Required fields are missing.")
    require(not value.keys() - required - optional, "unsupported", "Unknown semantic fields.")
    if "extensions" in value:
        require(isinstance(value["extensions"], dict), "invalid", "Extensions must be an object.")
    return value


def string(value: Any) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 1024,
            "invalid", "Expected a nonempty bounded string.")
    return value


def sequence(value: Any, *, advisories=None, label="TokenSlang list") -> list:
    """Validate a list; growth past the compatibility threshold is advisory."""
    require(isinstance(value, list), "invalid", "Expected a list.")
    message = label + " crossed the legacy 64-item glass ceiling; inspect why it is growing."
    if advisories is None:
        health.emit("tokenslang-list-growth", len(value), MAX_ITEMS, message)
    else:
        health.add(advisories, "tokenslang-list-growth", len(value), MAX_ITEMS, message)
    return value


def bounded_sequence(value: Any, max_items: int, *, label="Atomic list") -> list:
    """Use only when a contract proves the collection must fit one atomic operation."""
    require(type(max_items) is int and max_items > 0, "invalid", "Atomic list bound must be positive.")
    require(isinstance(value, list) and len(value) <= max_items,
            "invalid", f"{label} exceeds its {max_items}-item atomic bound.")
    return value


def loads(text: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "ambiguous", "Duplicate JSON key.")
            result[key] = value
        return result

    def constant(_):
        raise ReadError("invalid", "Non-finite JSON numbers are unsupported.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        encoded(value)  # Escaped lone surrogates and numeric overflow are not UTF-8 JSON values.
        return value
    except (ValueError, RecursionError) as exc:
        raise ReadError("invalid", "Invalid or excessively nested JSON.") from exc


def encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ReadError("invalid", "Value is not supported finite UTF-8 JSON.") from exc


def reference(value: Any) -> dict:
    if isinstance(value, dict) and "path" in value:
        shape(value, {"path"}, {"start_line", "end_line"})
    else:
        shape(value, {"scope_id", "namespace", "resource_id"})
    for key, item in value.items():
        if key.endswith("_line"):
            require(type(item) is int and item >= 1, "invalid", "Invalid line range.")
        else:
            string(item)
    require(("start_line" in value) == ("end_line" in value), "invalid", "Supply both range endpoints.")
    if "start_line" in value:
        require(value["start_line"] <= value["end_line"], "invalid", "Reversed line range.")
    return value


def request(value: Any) -> dict:
    shape(value, {"profile", "request_id", "operation", "max_bytes", "purpose"},
          {"target", "items", "expected_commit", "cursor", "extensions"})
    request_size = len(encoded(value))
    health.emit("tokenslang-request-growth", request_size, MAX_SOURCE_BYTES,
                "TokenSlang request crossed the 128 KiB glass ceiling; inspect the caller and prefer chunked fields.",
                unit="bytes")
    require(value["profile"] == PROFILE, "unsupported", "Unsupported TokenSlang profile.")
    string(value["request_id"])
    string(value["purpose"])
    require(isinstance(value["operation"], str) and value["operation"] in OPERATIONS,
            "unsupported", "Unsupported operation.")
    require(type(value["max_bytes"]) is int and value["max_bytes"] >= 1,
            "invalid", "max_bytes must be positive.")
    is_batch = value["operation"] == "read.batch"
    needs_target = value["operation"] not in {"describe", "orient", "read.batch"}
    require(("target" in value) == needs_target, "invalid", "Operation target mismatch.")
    require(("items" in value) == is_batch, "invalid", "Batch item selection mismatch.")
    if needs_target:
        reference(value["target"])
    if is_batch:
        items = bounded_sequence(value["items"], 16, label="Read batch")
        require(bool(items), "invalid", "Read batch must select at least one item.")
        seen = set()
        for item in items:
            shape(item, {"id", "target", "required"})
            string(item["id"]); reference(item["target"])
            require(type(item["required"]) is bool, "invalid", "Batch required flag must be boolean.")
            require(item["id"] not in seen, "conflict", "Batch item IDs must be unique.")
            seen.add(item["id"])
    if "expected_commit" in value:
        string(value["expected_commit"])
    if "cursor" in value:
        require(value["operation"] == "read", "invalid", "Only read accepts a source cursor.")
        cursor = shape(value["cursor"], {"commit", "target_sha256", "text_sha256", "offset"})
        require(isinstance(cursor["commit"], str) and
                bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", cursor["commit"])),
                "invalid", "Invalid source cursor commit.")
        require(isinstance(cursor["text_sha256"], str) and
                bool(re.fullmatch(r"[0-9a-f]{64}", cursor["text_sha256"])),
                "invalid", "Invalid source cursor fingerprint.")
        require(isinstance(cursor["target_sha256"], str) and
                bool(re.fullmatch(r"[0-9a-f]{64}", cursor["target_sha256"])),
                "invalid", "Invalid source cursor target fingerprint.")
        require(type(cursor["offset"]) is int and cursor["offset"] >= 0,
                "invalid", "Invalid source cursor offset.")
    return value


def work(text: str) -> dict:
    """Validate the single native selected-work block used by all readers."""
    blocks = re.findall(r"^```tokenslang-work\r?\n(.*?)^```[ \t]*\r?$",
                        text, re.MULTILINE | re.DOTALL)
    require(len(blocks) == 1, "ambiguous" if blocks else "unsupported",
            "Expected exactly one tokenslang-work block in the native work owner.")
    value = loads(blocks[0])
    shape(value, {"profile", "work_id", "goal", "status", "required", "optional"}, {"extensions"})
    require(value["profile"] == WORK, "unsupported", "Unsupported native work profile.")
    require(value["status"] == "selected", "conflict", "Native work is not selected.")
    string(value["work_id"])
    string(value["goal"])
    for target in sequence(value["required"]):
        reference(target)
    for hint in sequence(value["optional"]):
        shape(hint, {"target", "why"})
        string(hint["why"])
        reference(hint["target"])
    return value
