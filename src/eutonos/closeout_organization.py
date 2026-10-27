"""Prepare guarded closeout filing changes independently of optional RAM."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from . import tokenslang as ts
from .effects import LocalRoot, digest, plan as effect_plan
from .source import safe_path


PROFILE = "eutonos.tokenslang.closeout-organization.v1"
PLAN = "eutonos.tokenslang.closeout-organization-plan.v1"
DISPOSITIONS = {"completed", "continuing", "blocked"}
DEFAULT_SOURCE = "docs/CURRENT_TASK.md"
CHANGELOG = "docs/CHANGELOG.md"
_HEADING = re.compile(br"^##[ \t]+(.+?)[ \t]*(?:#+[ \t]*)?(?:\r?\n)?$")
_FENCE = re.compile(br"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _request(value: dict) -> dict:
    required = {"profile", "title", "summary", "disposition"}
    optional = {"source_path", "archive_path", "headings", "archive_active", "record_change",
                "date", "generated_at", "module", "area"}
    ts.shape(value, required, optional)
    ts.require(value["profile"] == PROFILE, "unsupported", "Unknown closeout organization profile.")
    ts.string(value["title"]); ts.string(value["summary"])
    ts.require(value["disposition"] in DISPOSITIONS, "invalid", "Select completed, continuing, or blocked.")
    headings = value.get("headings", [])
    ts.require(isinstance(headings, list)
               and all(isinstance(item, str) and 0 < len(item) <= 256 for item in headings),
               "invalid", "Headings must be exact H2 title strings.")
    ts.require(len(headings) == len(set(headings)), "invalid", "Selected headings must be unique.")
    for flag in ("archive_active", "record_change"):
        ts.require(flag not in value or type(value[flag]) is bool, "invalid", flag + " must be true or false.")
    for tag in ("module", "area"):
        if tag in value:
            ts.string(value[tag])
    date = value.get("date")
    ts.require(date is None or isinstance(date, str) and _DATE.fullmatch(date), "invalid", "Date must be YYYY-MM-DD.")
    stamp = value.get("generated_at")
    ts.require(stamp is None or isinstance(stamp, str) and _STAMP.fullmatch(stamp),
               "invalid", "generated_at must be UTC YYYY-MM-DDTHH:MM:SSZ.")
    return value


def _output_path(value: str) -> str:
    value = safe_path(value)
    parts = Path(value).parts
    ts.require(not any(part.startswith(".") or "local" in part.casefold() or "private" in part.casefold() for part in parts),
               "denied", "Closeout outputs cannot use hidden, local, or private paths.")
    ts.require(len(parts) >= 2 and parts[0] == "docs" and Path(value).suffix.casefold() == ".md", "denied",
               "Closeout output must be a Markdown file under docs.")
    return value


def _sections(body: bytes) -> list[dict]:
    lines = body.splitlines(keepends=True)
    offsets, total = [], 0
    for line in lines:
        offsets.append(total); total += len(line)
    found, fence = [], None
    for index, line in enumerate(lines):
        marker = _FENCE.match(line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = (run[:1], len(run))
            elif run[:1] == fence[0] and len(run) >= fence[1] and not marker.group(2).strip():
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING.match(line)
        if match:
            try:
                title = match.group(1).decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise ts.ReadError("invalid", "H2 headings must be UTF-8.") from exc
            found.append({"title": title, "start": offsets[index]})
    for index, item in enumerate(found):
        item["end"] = found[index + 1]["start"] if index + 1 < len(found) else len(body)
        item["bytes"] = body[item["start"]:item["end"]]
    return found


def _change_record(request: dict, record_id: str, date: str) -> dict:
    value = {"profile": "eutonos.tokenslang.change.v1", "id": record_id,
             "title": request["title"], "summary": request["summary"],
             "status": request["disposition"], "date": date}
    for key in ("module", "area"):
        if key in request:
            value[key] = request[key]
    return value


def _change_blocks(body: bytes) -> list[dict]:
    pattern = re.compile(br"(?ms)^```tokenslang-change[ \t]*\r?\n(.*?)\r?\n```[ \t]*(?:\r?\n|$)")
    values = []
    for match in pattern.finditer(body):
        try:
            value = json.loads(match.group(1).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ts.ReadError("invalid", "Malformed TokenSlang change record.") from exc
        ts.require(isinstance(value, dict), "invalid", "Change record must be an object.")
        values.append(value)
    return values


def prepare(root: str | Path, request: dict) -> dict:
    """Return an exact effect plan; this function never applies repository writes."""
    request = _request(request)
    local = LocalRoot(root)
    source_path = safe_path(request.get("source_path", DEFAULT_SOURCE))
    ts.require(source_path.casefold().endswith(".md"), "invalid", "Closeout source must be Markdown.")
    source = local.read(source_path)
    ts.require(source is not None, "missing", "Selected closeout source is absent.")
    headings = request.get("headings", [])
    archive_path = request.get("archive_path")
    ts.require(not headings or archive_path is not None, "invalid", "Archiving requires an explicit archive path.")
    if archive_path is not None:
        archive_path = _output_path(archive_path)
        ts.require(archive_path != source_path, "invalid", "Archive and source paths must differ.")
        ts.require(Path(archive_path).parent == Path(source_path).parent, "denied",
                   "Archive must share the source directory so relative links retain meaning.")

    indexed = _sections(source)
    by_title = {}
    for item in indexed:
        by_title.setdefault(item["title"], []).append(item)
    selected = []
    for heading in headings:
        matches = by_title.get(heading, [])
        ts.require(matches, "missing", "Selected H2 heading is absent: " + heading)
        ts.require(len(matches) == 1, "conflict", "Selected H2 heading is duplicated: " + heading)
        section = matches[0]
        ts.require(request.get("archive_active", False) or not heading.casefold().startswith("active"),
                   "denied", "Active-looking sections require archive_active=true: " + heading)
        lowered = section["bytes"].lower()
        ts.require(b"```tokenslang-work" not in lowered and b"```tokenslang-ram-links" not in lowered, "denied",
                   "Sections containing TokenSlang work or RAM routing require separate retention: " + heading)
        selected.append(section)
    slugs = [_heading_slug(item["title"]) for item in selected]
    ts.require(len(slugs) == len(set(slugs)), "conflict", "Selected headings produce duplicate archive anchors.")

    payload_basis = {key: request[key] for key in sorted(request) if key != "generated_at"}
    payload_basis["source_sha256"] = digest(source)
    record_id = "ECO-" + hashlib.sha256(
        json.dumps(payload_basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    generated_at = request.get("generated_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date = request.get("date") or generated_at[:10]

    updates, inputs = {}, [source_path]
    if selected:
        spans = sorted((item["start"], item["end"], item["title"]) for item in selected)
        cursor, retained = 0, bytearray()
        archive_name = Path(archive_path).name
        for start, end, heading in spans:
            retained.extend(source[cursor:start])
            slug = _heading_slug(heading)
            retained.extend(f'<a id="{slug}"></a>\n\nArchived: [{heading}]({archive_name}#{slug})\n\n'.encode())
            cursor = end
        retained.extend(source[cursor:])
        updates[source_path] = bytes(retained)
        archive = local.read(archive_path)
        archive_before = archive or b"# Closeout history\n\nHistorical source sections preserved verbatim.\n"
        marker = f"<!-- eutonos-closeout:{record_id} -->".encode()
        ts.require(marker not in archive_before, "conflict", "Archive already contains this closeout ID.")
        separator = b"" if archive_before.endswith(b"\n\n") else (b"\n" if archive_before.endswith(b"\n") else b"\n\n")
        intro = (marker + b"\nRecorded: " + generated_at.encode() + b"\nDisposition: "
                 + request["disposition"].encode() + b"\nSource: " + source_path.encode() + b"\n\n")
        archived = b"".join(item["bytes"] for item in selected)
        updates[archive_path] = archive_before + separator + intro + archived

    if request.get("record_change", False):
        changelog_path = _output_path(CHANGELOG)
        current = local.read(changelog_path)
        before = current or b"# Changes\n"
        record = _change_record(request, record_id, date)
        matches = [item for item in _change_blocks(before) if item.get("id") == record_id]
        ts.require(not matches or matches == [record], "conflict", "Changelog ID collides with different content.")
        if not matches:
            separator = b"\n" if before.endswith(b"\n") else b"\n\n"
            block = b"```tokenslang-change\n" + json.dumps(record, indent=2, sort_keys=True).encode() + b"\n```\n"
            updates[changelog_path] = before + separator + block
        else:
            inputs.append(changelog_path)

    effect = effect_plan(local, updates, inputs=inputs)
    files = {change["path"]: {"before": change["before"],
             "after": digest(base64.b64decode(change["after"]))}
             for change in effect["changes"]}
    return {"profile": PLAN, "outcome": "planned" if files else "no-op", "id": record_id,
            "generated_at": generated_at, "date": date, "title": request["title"],
            "summary": request["summary"], "disposition": request["disposition"],
            "source_path": source_path, "archive_path": archive_path,
            "headings": headings, "files": files, "paths": sorted(files),
            "allowed_paths": sorted(files),
            "effect": effect, "fingerprint": digest(ts.encoded(effect)),
            "qualification": "Organization plan only; historical filing does not prove work completion, RAM capture, publication, or deployment."}


def readback(root: str | Path, prepared: dict) -> dict:
    """Classify current bytes against one prepared result without applying changes."""
    ts.shape(prepared, {"profile", "id", "files"}, set(prepared) - {"profile", "id", "files"})
    ts.require(prepared["profile"] == PLAN and isinstance(prepared["files"], dict),
               "invalid", "Select a closeout organization plan.")
    local = LocalRoot(root)
    states = {}
    for path, expected in prepared["files"].items():
        path = safe_path(path)
        actual = digest(local.read(path))
        states[path] = "applied" if actual == expected.get("after") else "before" if actual == expected.get("before") else "diverged"
    values = set(states.values())
    outcome = "applied" if values <= {"applied"} else "pending" if values <= {"before"} else "diverged"
    return {"profile": PLAN, "id": prepared["id"], "outcome": outcome,
            "files": states, "writes": 0,
            "qualification": "Read-only byte comparison; organization and optional RAM remain separate."}


def _heading_slug(value: str) -> str:
    value = value.casefold().strip()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", value).strip("-") or "section"
