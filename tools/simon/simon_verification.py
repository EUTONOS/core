"""Pure expected-effect contracts; Windows observation remains in the router."""
from __future__ import annotations
import ntpath
import re
from typing import Any


EFFECT_ALIASES = {"window_open": "new_window", "modal_open": "dialog_open", "modal_close": "dialog_close"}
EFFECT_TYPES = frozenset({"new_window", "dialog_open", "dialog_close", "window_close", "title_change",
                          "foreground_change", "file_exists", "file_absent", "file_created", "file_deleted",
                          "file_content", "file_saved", "file_renamed",
                          "any_change", "visual_stable", "same_window_stable", "content_loaded"})
VISUAL_EFFECTS = frozenset({"any_change", "visual_stable", "same_window_stable", "content_loaded"})
EFFECT_PROPERTIES = {
    "type": {"type": "str", "description": "Supported expected effect; aliases window_open/modal_open/modal_close are accepted."},
    **{key: {"type": "str", "description": "Expected effect selector."} for key in
       ("title", "title_contains", "process_name", "process_contains", "path", "file_path", "desktop_filename",
        "expected_sha256", "old_path", "new_path")},
    **{key: {"type": "int", "description": "Bounded expected effect parameter."} for key in
       ("hwnd", "max_wait_ms", "timeout_ms", "poll_ms", "stable_frames")},
}
MAX_CONTENT_BYTES = 8 * 1024 * 1024


def validate_file_selector(value: dict) -> list[str]:
    errors = []
    paths = [value[key] for key in ("path", "file_path", "desktop_filename") if key in value]
    if not paths or any(not isinstance(path, str) or not path.strip() or "\0" in path for path in paths):
        errors.append("Supply a nonempty path/file_path or bare desktop_filename.")
    if value.get("path") and value.get("file_path") and value["path"] != value["file_path"]:
        errors.append("Conflicting file path aliases.")
    if "desktop_filename" in value:
        name = value["desktop_filename"]
        if any(key in value for key in ("path", "file_path")):
            errors.append("desktop_filename cannot be combined with path/file_path.")
        if not isinstance(name, str) or name in {".", ".."} or any(char in name for char in '\\/:'):
            errors.append("desktop_filename must be a bare filename; use path for directories.")
    return errors


def validate_sha256(value: Any) -> list[str]:
    return [] if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) else ["expected_sha256 must be a 64-character hexadecimal SHA-256 digest."]


def canonical_effect_type(spec: dict) -> str:
    kind = str(spec.get("type") or "").strip().lower()
    return EFFECT_ALIASES.get(kind, kind)


def validate_expected_change(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["expected_change must be an object."]
    kind = canonical_effect_type(value)
    if kind not in EFFECT_TYPES:
        return ["expected_change.type must be a supported explicit effect type."]
    selectors = {"title", "title_contains", "process_name", "process_contains"}
    allowed = {"type", "max_wait_ms", "timeout_ms", "poll_ms"}
    if kind in {"new_window", "dialog_open"}:
        allowed |= selectors
    if kind in {"window_close", "dialog_close", "title_change"}:
        allowed.add("hwnd")
    if kind == "title_change":
        allowed |= {"title", "title_contains"}
    if kind.startswith("file_"):
        allowed |= {"old_path", "new_path"} if kind == "file_renamed" else {"path", "file_path", "desktop_filename"}
    if kind in {"file_content", "file_saved"}:
        allowed.add("expected_sha256")
    if kind in VISUAL_EFFECTS - {"any_change"}:
        allowed.add("stable_frames")
    errors = [f"Unsupported expected_change field {key!r} for {kind}." for key in value if key not in allowed]
    for key, item in value.items():
        if key not in EFFECT_PROPERTIES:
            continue
        expected = EFFECT_PROPERTIES[key]["type"]
        if (expected == "str" and (not isinstance(item, str) or not item.strip())) or (expected == "int" and type(item) is not int):
            errors.append(f"expected_change.{key} must be a nonempty string or integer of its declared type.")
    if kind in {"new_window", "dialog_open"} and not any(value.get(key) for key in selectors):
        errors.append("New-window/dialog effects need a title or process selector.")
    if kind in {"window_close", "dialog_close", "title_change"} and (type(value.get("hwnd")) is not int or value["hwnd"] <= 0):
        errors.append("Close/title effects require an explicit positive hwnd.")
    if kind == "title_change" and not (value.get("title") or value.get("title_contains")):
        errors.append("title_change requires the expected title or title_contains.")
    if kind.startswith("file_") and kind != "file_renamed":
        errors.extend(validate_file_selector(value))
    if kind in {"file_content", "file_saved"}:
        errors.extend(validate_sha256(value.get("expected_sha256")))
    if kind == "file_renamed":
        paths = [value.get(key) for key in ("old_path", "new_path")]
        if any(not isinstance(path, str) or not path.strip() or "\0" in path for path in paths):
            errors.append("file_renamed requires nonempty old_path and new_path.")
        elif ntpath.normcase(ntpath.normpath(paths[0])) == ntpath.normcase(ntpath.normpath(paths[1])):
            errors.append("Rename paths must be distinct; case-only renames are not supported.")
    for key, low, high in (("max_wait_ms", 0, 30000), ("timeout_ms", 0, 30000), ("poll_ms", 10, 1000), ("stable_frames", 1, 20)):
        if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
            errors.append(f"expected_change.{key} must be an integer in [{low}, {high}].")
    if "max_wait_ms" in value and "timeout_ms" in value and value["max_wait_ms"] != value["timeout_ms"]:
        errors.append("Conflicting expected-change timeout aliases.")
    return errors


def declared_effect(args: dict) -> tuple[dict, list[str]]:
    """Accept equivalent declarations; reject conflicts instead of silently overriding."""
    values = []
    for container in (args, args.get("step"), args.get("observe_after")):
        if isinstance(container, dict) and "expected_change" in container:
            values.append(container["expected_change"])
    errors = [error for value in values for error in validate_expected_change(value)]
    if errors or not values:
        return {}, errors
    normalized = [{**value, "type": canonical_effect_type(value)} for value in values]
    if any(value != normalized[0] for value in normalized[1:]):
        return {}, ["Conflicting expected_change declarations; supply one consistent effect."]
    return normalized[0], []


def window_matches_effect(window: dict, spec: dict) -> bool:
    title = str(window.get("title") or "")
    process = str(window.get("process_name") or "").lower()
    return (not spec.get("title") or title == spec["title"]) and (
        not spec.get("title_contains") or spec["title_contains"].lower() in title.lower()) and (
        not (spec.get("process_contains") or spec.get("process_name")) or
        str(spec.get("process_contains") or spec.get("process_name")).lower() in process)


def evaluate_effect(spec: dict, before: dict, after: dict) -> tuple[bool, str]:
    """Evaluate typed state/transition evidence, never inferred input delivery."""
    kind = canonical_effect_type(spec)
    if before.get("error") or after.get("error"):
        return False, "effect_observation_unavailable"
    if kind in {"file_content", "file_saved"}:
        digest = str(spec.get("expected_sha256") or "").lower()
        content_matches = after.get("exists") is True and after.get("sha256") == digest
        if kind == "file_content":
            return content_matches, "file_content_matches" if content_matches else "file_content_mismatch"
        transitioned = before.get("exists") is False or (before.get("exists") is True and before.get("sha256") not in {None, digest})
        return content_matches and transitioned, "file_saved_content_transition" if content_matches and transitioned else "file_save_transition_not_observed"
    if kind == "file_renamed":
        old_before, new_before = before.get("old", {}), before.get("new", {})
        old_after, new_after = after.get("old", {}), after.get("new", {})
        if any(item.get("error") for item in (old_before, new_before, old_after, new_after)):
            return False, "effect_observation_unavailable"
        identity = old_before.get("identity")
        matched = (old_before.get("exists") is True and new_before.get("exists") is False
                   and old_after.get("exists") is False and new_after.get("exists") is True
                   and bool(identity) and identity == new_after.get("identity")
                   and bool(old_before.get("sha256")) and old_before["sha256"] == new_after.get("sha256"))
        return bool(matched), "file_renamed_identity_and_content" if matched else "file_rename_transition_not_observed"
    if kind in {"file_exists", "file_absent", "file_created", "file_deleted"}:
        exists = after.get("exists")
        desired = kind in {"file_exists", "file_created"}
        transition = kind not in {"file_created", "file_deleted"} or before.get("exists") is (not desired)
        reason = "expected_file_exists" if kind == "file_exists" else "expected_file_absent" if kind == "file_absent" else "file_transition"
        return exists is desired and transition, reason
    if kind in {"new_window", "dialog_open"}:
        old = {item["hwnd"] for item in before.get("windows", [])}
        found = any(item.get("hwnd") not in old and window_matches_effect(item, spec) for item in after.get("windows", []))
        return found, "new_window_observed" if kind == "new_window" else "new_dialog_observed"
    if kind in {"window_close", "dialog_close"}:
        # Absence is authoritative IsWindow evidence, not failed window enumeration.
        return before.get("alive") is True and after.get("alive") is False, "anchored_window_closed"
    if kind == "title_change":
        same = before.get("alive") is True and after.get("alive") is True and before.get("pid") == after.get("pid")
        return bool(same and before.get("title") != after.get("title") and window_matches_effect(after, spec)), "anchored_title_changed"
    if kind == "foreground_change":
        return bool(before.get("hwnd") and after.get("hwnd") and before["hwnd"] != after["hwnd"]), "foreground_changed"
    return False, "visual_evidence_required"


def effect_verdict(spec: dict, watch: dict | None, *, requested: bool = True) -> dict:
    kind = canonical_effect_type(spec) if spec else None
    matched = bool(watch and watch.get("matched"))
    evidence = "visual" if kind in VISUAL_EFFECTS else "state" if kind in {"file_exists", "file_absent", "file_content"} else "transition"
    return {"type": kind, "verdict": "observed" if matched and evidence == "visual" else "verified" if matched else "unmet" if requested else "unverified",
            "evidence_kind": evidence if requested else "input_acknowledgement", "matched": matched,
            "reason": (watch or {}).get("reason") or "No expected effect was declared."}


def effect_allows_continuation(verdict: dict) -> bool:
    # Visual observation alone never proves an application effect.
    return verdict.get("verdict") == "verified"


def expected_change_from_args(root_args: dict[str, Any]) -> dict[str, Any]:
    observe_after = root_args.get("observe_after") if isinstance(root_args.get("observe_after"), dict) else {}
    value = observe_after.get("expected_change")
    if not isinstance(value, dict):
        value = root_args.get("expected_change")
    if not isinstance(value, dict):
        return {}
    change_type = str(value.get("type") or "").strip().lower()
    if not change_type:
        return {}
    normalized = dict(value)
    normalized["type"] = change_type
    return normalized


def expected_change_result(
    expected_change: dict[str, Any],
    matched: bool,
    attempts: int,
    max_wait_ms: int,
    poll_ms: int,
    reason: str,
    polls: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "enabled": True,
        "expected_change": expected_change,
        "changed": matched,
        "matched": matched,
        "attempts": attempts,
        "max_wait_ms": max_wait_ms,
        "poll_ms": poll_ms,
        "reason": reason,
        "polls": polls[-6:],
    }
