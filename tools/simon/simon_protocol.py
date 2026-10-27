"""CLI parsing, result envelopes, emission, and request identity (C1.0)."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import threading
import time
from typing import Any

from simon_common import HELPER_SHORT_NAME, now_iso
from simon_schema import ARGUMENTS, CLI_CONTROLS, COMMANDS


_REQUEST_ID_LOCK = threading.Lock()
_LAST_REQUEST_ID_MILLIS = -1

# CLI-only controls are never forwarded into command payloads. An explicit
# timeout is forwarded because helper.start also uses it as its launch wait.
CLI_CONTROL_KEYS = frozenset({"command", "via_app", "json", "json_file", "pretty"})
DEFAULT_CLI_BRIDGE_TIMEOUT_SECONDS = 10.0


def build_cli_parser() -> argparse.ArgumentParser:
    # argument_default=SUPPRESS keeps flags the caller did not pass out of the
    # namespace entirely, so commands apply their own defaults instead of
    # inheriting CLI-wide defaults such as target_window/"Simon Target".
    parser = argparse.ArgumentParser(
        description="Universal command surface for the Simon helper POC.",
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument("command", help="Command name, such as status.current or recovery.capture_grid.")
    for name, spec in {**ARGUMENTS, **CLI_CONTROLS}.items():
        options: dict[str, Any] = {"help": spec["description"]}
        kind = spec["type"]
        if kind == "bool":
            options["action"] = argparse.BooleanOptionalAction
        elif kind in {"int", "number", "object", "array"}:
            options["type"] = {"int": int, "number": float, "object": json.loads, "array": json.loads}[kind]
        if "choices" in spec:
            options["choices"] = spec["choices"]
        parser.add_argument("--" + name.replace("_", "-"), **options)
    return parser


def build_cli_payload(namespace: argparse.Namespace, extra: dict[str, Any]) -> dict[str, Any]:
    """Return only explicitly passed CLI flags plus --json keys (JSON wins)."""
    payload = {key: value for key, value in vars(namespace).items() if key not in CLI_CONTROL_KEYS}
    payload.update(extra)
    return payload


def command_result(
    status: str,
    command: str,
    message: str,
    data: dict[str, Any] | None = None,
    next_allowed_actions: list[str] | None = None,
) -> dict[str, Any]:
    payload = data or {}
    if status in {"ERROR", "FAILED", "UNCERTAIN", "NEEDS_PLANNER"} and "planner_recovery_hint" not in payload:
        payload["planner_recovery_hint"] = (
            "Before retrying the same path, inspect the active window for related controls, "
            "dropdowns, alternate buttons, labels, and navigation options that may solve the blockage."
        )
    result = {
        "status": status,
        "command": command,
        "message": message,
        "timestamp": now_iso(),
        "data": payload,
        "next_allowed_actions": (
            next_allowed_actions if next_allowed_actions is not None else default_next_actions(status)
        ),
    }
    result["outcome"] = result_outcome(result)
    return result


def result_outcome(result: dict[str, Any]) -> str:
    status, command = result.get("status"), str(result.get("command") or "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if status in {"ERROR", "FAILED", "UNCERTAIN", "UNAVAILABLE"}:
        return "blocked"
    if status == "NEEDS_PLANNER" and data.get("failure_type") not in {None, "", "NONE"}:
        return "blocked"
    verification = data.get("verification")
    if isinstance(verification, dict) and "verdict" in verification and verification.get("verdict") != "verified":
        return "blocked" if verification.get("verdict") == "unmet" else "needs_decision"
    if status == "NEEDS_PLANNER":
        if data.get("failure_type") not in {None, "", "NONE"}:
            return "blocked"
        if command in {"recovery.capture_grid", "recovery.guided_grid_crop", "planner.fast_observe", "simon.force_grid_request"}:
            return "observation_ready"
        if command == "planner.step" and data.get("action_result") and result_outcome(data["action_result"]) == "blocked":
            return "blocked"
        if command == "planner.step" and (data.get("action_result") or {}).get("status") == "SUCCESS":
            return "observation_ready"
        return "needs_decision"
    if status == "SUCCESS":
        if command == "input.action_sequence":
            steps = [row.get("result", {}) for row in data.get("results", [])]
            if any(result_outcome(step) == "blocked" for step in steps):
                return "blocked"
            if any(step.get("status") == "NEEDS_PLANNER" or result_outcome(step) == "needs_decision" for step in steps):
                return "needs_decision"
        return "observation_ready" if COMMANDS.get(command, {}).get("authority") == "read_only" else "action_done"
    return "needs_decision"


CONTEXT_KEYS = {"active_context", "action_menu", "planner_coaching", "working_memory"}
WINDOW_KEYS = {"target_window", "foreground_window", "active_window", "current_dialog", "foreground_before", "foreground_after"}
IMAGE_KEYS = {"raw_screenshot_path", "grid_screenshot_path", "candidate_overlay_screenshot_path", "raw_crop_screenshot_path", "grid_crop_screenshot_path", "context_screenshot_path", "context_grid_screenshot_path"}


def result_view(result: dict[str, Any], detail: str = "minimal") -> dict[str, Any]:
    """Project a full operator report without altering its verification evidence."""
    clean = {k: v for k, v in result.items() if k != "image"}
    if detail == "full":
        return copy.deepcopy(clean)
    data = clean.get("data") if isinstance(clean.get("data"), dict) else {}

    def prune(value, key=""):
        if isinstance(value, dict):
            if key == "planner_request":
                return {**{k: value[k] for k in ("request_id", "planner_request_path", "capture_timestamp", "failure_type", "expected_result") if k in value}, "context_ref": "#/data/context"}
            if key == "working_memory" and detail == "minimal":
                return {k: value[k] for k in ("last_observation_request_id", "last_action", "last_failed_action") if k in value}
            return {k: prune(v, k) for k, v in value.items()
                    if k not in CONTEXT_KEYS | WINDOW_KEYS | IMAGE_KEYS | {"capture_route", "synthetic_windows", "candidate_overlay", "command_schemas"}
                    and not ("screenshot" in k and k.endswith("_path"))}
        if isinstance(value, list):
            if key == "results" and detail == "minimal":
                return [{"index": row.get("index"), "action": row.get("action"),
                         "result": {k: row.get("result", {}).get(k) for k in ("status", "outcome", "message")},
                         "failure_type": row.get("result", {}).get("data", {}).get("failure_type"),
                         **({"verification": {k: row["verification"][k] for k in ("type", "verdict", "reason", "evidence_kind", "matched") if k in row["verification"]}}
                            if isinstance(row.get("verification"), dict) else {})}
                        for row in value]
            return [prune(v) for v in value]
        return value

    public = prune(clean)
    public_data = public.setdefault("data", {})
    context = data.get("active_context") or {}
    menu = data.get("action_menu") or {}
    window = context.get("active_window") or data.get("target_window") or data.get("foreground_window")
    packet = {"window": {k: window[k] for k in ("hwnd", "title", "bounds", "process_name", "class_name") if k in window}} if isinstance(window, dict) else {}
    packet["dialog_open"] = bool(context.get("dialog_open"))
    candidates = menu.get("ranked_candidates") or context.get("child_candidates") or []
    packet["candidates"] = [
        {"id": c.get("candidate_id"), "kind": c.get("kind"), "label": c.get("label") or c.get("title"),
         "center": c.get("click_point") or c.get("center"),
         "actions": [] if c.get("enabled") is False or c.get("visible") is False else c.get("available_actions") or c.get("actions") or [],
         **{key: False for key in ("enabled", "visible") if c.get(key) is False},
         "provenance": c.get("provenance") or "win32",
         **({"bounds": c.get("bounds"), "score": c.get("score")} if detail == "standard" else {})}
        for c in candidates[:(18 if COMMANDS.get(clean.get("command"), {}).get("authority") == "read_only" else 8)]
    ]
    candidate_ids = {c["id"] for c in packet["candidates"]}
    if len(candidates) > len(packet["candidates"]):
        packet["candidate_count"] = len(candidates)
    for key in ("likely_click_targets", "likely_text_fields", "likely_primary_buttons"):
        if key in menu:
            packet[key] = [c.get("candidate_id") for c in menu[key] if c.get("candidate_id") in candidate_ids]
    recommended = menu.get("recommended_candidate")
    if isinstance(recommended, dict) and recommended.get("candidate_id") in candidate_ids:
        packet["recommended_candidate"] = recommended.get("candidate_id")
    if "keyboard_entry_safe" in menu:
        packet["keyboard_entry_safe"] = menu["keyboard_entry_safe"]
    if clean.get("command") != "verify.file_state":
        public_data["context"] = packet
    if "working_memory" in data:
        public_data["working_memory"] = prune(data["working_memory"], "working_memory")
    if data.get("planner_coaching"):
        public_data["hints"] = data["planner_coaching"][:2] if detail == "minimal" else data["planner_coaching"]
    return public


def default_next_actions(status: str) -> list[str]:
    if status in {"SUCCESS", "UNAVAILABLE"}:
        return ["status.current", "observe.window", "observe.screenshot"]
    return ["status.current", "recovery.capture_grid", "observe.screenshot", "helper.commands"]


def make_request_id() -> str:
    global _LAST_REQUEST_ID_MILLIS
    with _REQUEST_ID_LOCK:
        current_millis = time.time_ns() // 1_000_000
        allocated_millis = max(current_millis, _LAST_REQUEST_ID_MILLIS + 1)
        _LAST_REQUEST_ID_MILLIS = allocated_millis
    seconds, millis = divmod(allocated_millis, 1000)
    stamp = time.strftime("REQ-%Y%m%d-%H%M%S", time.localtime(seconds))
    return f"{stamp}-{millis:03d}"


def safe_request_id(request_id: str) -> str:
    raw = str(request_id).strip()
    sanitized = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in raw)
    if raw and sanitized == raw and len(raw) <= 96:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    base = sanitized[:80].strip("_-") or "request"
    return f"{base}-{digest}"


def emit(result: dict[str, Any], pretty: bool = False) -> int:
    # Compact single-line JSON by default keeps planner-facing output small;
    # --pretty restores the indented human-readable form.
    if pretty:
        print(json.dumps(strip_internal(result), indent=2))
    else:
        print(json.dumps(strip_internal(result), separators=(",", ":")))
    return 0 if result.get("status") in {"SUCCESS", "NEEDS_PLANNER", "UNAVAILABLE"} else 1


def strip_internal(result: dict[str, Any]) -> dict[str, Any]:
    result.pop("image", None)
    return result
