#!/usr/bin/env python3
"""Read-only checks for EUTONOS's current Docs workspace, not a runtime validator.

Supports ordinary inline Markdown links, ATX headings and fenced code blocks.
Supports explicit standalone HTML ID anchors; semantic correctness needs human review.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_BYTES = 2_000_000
LINK = re.compile(r"\[[^\]\n]+\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def local_file(root: Path, name: str) -> Path:
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or ".." in rel.parts or "\\" in name:
        raise ValueError(f"unsafe registered path: {name!r}")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink not supported: {name}")
    if not current.is_file():
        raise ValueError(f"missing file: {name}")
    if current.stat().st_size > MAX_BYTES:
        raise ValueError(f"file exceeds checker bound: {name}")
    return current


def visible_lines(text: str, name: str) -> list[str]:
    visible: list[str] = []
    active: tuple[str, int] | None = None
    for line in text.splitlines():
        match = FENCE.match(line)
        if match:
            marker, suffix = match.groups()
            if active is None:
                active = (marker[0], len(marker))
            elif marker[0] == active[0] and len(marker) >= active[1] and not suffix.strip():
                active = None
            continue
        if active is None:
            visible.append(line)
    if active is not None:
        raise ValueError(f"unclosed fence: {name}")
    return visible


def heading_ids(lines: list[str]) -> set[str]:
    counts: dict[str, int] = {}
    ids: set[str] = set()
    for line in lines:
        match = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+\s*)?$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match[1].strip().lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        ids.add(slug + (f"-{count}" if count else ""))
        counts[slug] = count + 1
    for line in lines:
        match = re.fullmatch(r'<a id="([a-zA-Z][a-zA-Z0-9_.:-]*)"></a>', line.strip())
        if match:
            if match[1] in ids:
                raise ValueError(f"duplicate anchor: {match[1]}")
            ids.add(match[1])
    return ids


def check(root: Path) -> dict[str, object]:
    errors: list[str] = []
    texts: dict[str, str] = {}
    visible: dict[str, list[str]] = {}
    anchors: dict[str, set[str]] = {}
    registry = json.loads(local_file(root, "docs/REGISTRY.json").read_text(encoding="utf-8"))
    if registry.get("schema") != "eutonos.documentation-bootstrap.v0":
        raise ValueError("unsupported bootstrap registry schema")
    records = registry.get("documents")
    if not isinstance(records, list):
        raise ValueError("documents must be a list")
    seen: dict[str, set[str]] = {k: set() for k in ("id", "role", "path")}
    for record in records:
        if not isinstance(record, dict):
            errors.append("document entry is not an object")
            continue
        for key in seen:
            value = record.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f"missing/invalid {key} in document entry")
            elif value in seen[key]:
                errors.append(f"duplicate {key}: {value}")
            else:
                seen[key].add(value)
        name = record.get("path")
        if not isinstance(name, str):
            continue
        try:
            p = local_file(root, name)
            if p.suffix != ".md":
                raise ValueError(f"document must be Markdown: {name}")
            text = p.read_text(encoding="utf-8")
            texts[name] = text
            visible[name] = visible_lines(text, name)
            anchors[name] = heading_ids(visible[name])
            raw = p.read_bytes()
            if b"\r" in raw or not raw.endswith(b"\n"):
                errors.append(f"LF/final-newline error: {name}")
            if any(line != line.rstrip() for line in text.splitlines()):
                errors.append(f"trailing whitespace: {name}")
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(str(exc))
    for record in records:
        if isinstance(record, dict) and "superseded_by" in record:
            if record["superseded_by"] not in seen["id"]:
                errors.append(f"unknown successor: {record['superseded_by']}")
    inventory = {"README.md", "AGENTS.md"}
    for folder in ("docs", "modules"):
        inventory.update(p.relative_to(root).as_posix() for p in (root / folder).rglob("*.md"))
    for name in sorted(inventory - seen["path"]):
        errors.append(f"unregistered Markdown: {name}")
    link_count = 0
    for name, lines in visible.items():
        for match in LINK.finditer("\n".join(lines)):
            url = urlsplit(match[1].strip("<>"))
            if url.scheme or url.netloc:
                continue  # External URLs are not contacted or validated.
            link_count += 1
            raw_path = unquote(url.path)
            try:
                target = (root / name).parent / raw_path if raw_path else root / name
                relative = target.resolve().relative_to(root).as_posix()
                local_file(root, relative)
                # Reject a symlinked lexical route even when its resolved target is inside root.
                cursor = root
                for part in target.relative_to(root).parts:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        raise ValueError("symlinked link route")
                fragment = unquote(url.fragment)
                if fragment and relative in anchors and fragment not in anchors[relative]:
                    raise ValueError(f"unknown heading #{fragment}")
                if fragment and relative not in anchors:
                    raise ValueError("fragment target is not registered Markdown")
            except (OSError, ValueError) as exc:
                errors.append(f"bad link in {name}: {match[1]} ({exc})")
    return {"ok": not errors, "registered_documents": len(records),
            "markdown_files": len(inventory), "relative_links_checked": link_count,
            "errors": errors}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        report = check(root)
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        report = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
