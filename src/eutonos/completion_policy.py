"""Canonical completion contract and preservation-first instruction distribution.

Instruction files and their existing adoption fingerprints only: no settings,
hook definitions, trust, Git operations, model calls or services. Conflict review
is human/agent judgment, not a merge gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


CONTRACT = (
    "Requests to implement features or finish campaigns, stages, slices or implementation work "
    "include integration and merging into the established target unless explicitly scoped to "
    "exploration, research, testing, prototypes or another narrower deliverable. Implementation "
    "is finished when scoped changes pass required checks, are integrated into that target, and "
    "the target state is verified. Local edits, a commit, passing tests, worker handback or an "
    "open PR are intermediate; unmerged implementation remains unfinished. The coordinator owns "
    "final integration. Resolve the target from repository and campaign rules (a stage target "
    "may be a campaign branch, not main); ask only about material ambiguity. Preserve required "
    "reviews, branch protections, dependency ordering, concurrent work and explicit narrower "
    "instructions. Use the appropriate fast-forward, squash or merge strategy. If blocked, "
    "state the exact remaining step rather than claim completion. Merge does not authorize "
    "deployment, installation, restart or release, bypass permissions, or widen the task. "
    "Follow applicable global and repository instructions; surface conflicts rather than "
    "silently overriding them."
)
END = b"<!-- eutonos:completion:end -->"
START = b"<!-- eutonos:completion:start"
PATTERN = re.compile(re.escape(START) + rb" sha256=([0-9a-f]{64}) -->\r?\n(.*?)" + re.escape(END), re.S)
MAX_BYTES = 256 * 1024
MAX_ENTRIES = 30000
SKIP = {".git", ".eutonos-local", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "portable"}
NAMES = {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md"}
# Exact shipped adoption block at 09c3199, without its trailing newline.
# A digest alone never admits migration: its adoption manifest must bind it too.
LEGACY_ADOPTION_SHA256 = "f719de5887c9358af80fdc8d167b8f5151cc6285b645db3fd841552d73f9e35f"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def markers(name):
    """``(start, end, pattern)`` of the managed block called `name` (``eutonos:NAME:start`` .. ``:end``)."""
    start = ("<!-- eutonos:" + name + ":start").encode()
    end = ("<!-- eutonos:" + name + ":end -->").encode()
    return start, end, re.compile(re.escape(start) + rb" sha256=([0-9a-f]{64}) -->\r?\n(.*?)" + re.escape(end), re.S)


COMPLETION_HEADING = "## Completion contract"
DECISIONS_START, DECISIONS_END, DECISIONS_PATTERN = markers("decisions")
DECISIONS_HEADING = "## Decisions"
# Owner-approved Decisions rule (2026-10-23), user-agnostic: the bold label is part of the text.
DECISIONS = (
    "Decide what an existing rule, test, sibling pattern, current behavior or the user's earlier "
    "direction already answers, and report meaningful choices in a line starting with a bold "
    "**Decided:** label and the reason, saying when it is an inference. Bring the user only new "
    "product direction or visual design, feature removal, risk to user data or compatibility, and "
    "conflicts no rule settles; if current source contradicts the plan that way, stop and report. "
    "**Decided:** is not verification, acceptance, or approval for anything that needs it. Report "
    "in plain English and put questions last, each with a recommendation and reason."
)
SIMILAR = "similar-unmarked-copy"


class SimilarUnmarked(ValueError):
    """The block's heading is in the file outside any marker but its body differs: the person's version stays."""

    def __init__(self, message=SIMILAR):
        super().__init__(message)


def make_block(mark, body):
    start, end, _ = mark
    return start + b" sha256=" + sha(body).encode() + b" -->\n" + body + end


def block_body(text=CONTRACT, heading=COMPLETION_HEADING):
    return (heading + "\n\n" + text + "\n").encode("utf-8")


def block(text=CONTRACT):
    return make_block((START, END, PATTERN), block_body(text))


def decisions_block():
    return make_block((DECISIONS_START, DECISIONS_END, DECISIONS_PATTERN), block_body(DECISIONS, DECISIONS_HEADING))


def find_unmarked(raw, pattern, body):
    """Unmarked copies of a block: ``[(exact, start, end)]`` for each line outside the managed markers (and outside
    code fences) equal to the block's heading line. A copy is exact when the next lines equal the block text, compared
    after normalizing line endings and trailing whitespace, and nothing but blank lines follows it before the next
    heading, marker or the end of the file; a copy with more text in its section is similar, not exact. `start`/`end`
    are the byte span of the exact text without its final line terminator (the heading line for a similar copy)."""
    want = [line.rstrip() for line in body.decode("utf-8").split("\n")]
    while want and not want[-1]:
        want.pop()
    bom = b"\xef\xbb\xbf"
    masked = [(m.start(), m.end()) for m in pattern.finditer(raw)]
    lines, pos = [], 0
    for line in raw.splitlines(keepends=True):
        lines.append((pos, line))
        pos += len(line)

    def text(index):
        line = lines[index][1].rstrip(b" \t\r\n")
        return (line.removeprefix(bom) if index == 0 else line).decode("utf-8", "replace")

    def marked(index):
        return any(a <= lines[index][0] < b for a, b in masked)

    found, fence = [], False
    for i in range(len(lines)):
        if marked(i):
            continue
        current = text(i)
        if current.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence or current != want[0]:
            continue
        j = i + len(want)
        skip = 3 if i == 0 and lines[0][1].startswith(bom) else 0
        begin = lines[i][0] + skip
        exact = j <= len(lines) and [text(k) for k in range(i, j)] == want and not any(marked(k) for k in range(i, j))
        if exact:
            inner, k = False, j
            while k < len(lines):
                line = text(k)
                if line.lstrip().startswith(("```", "~~~")):
                    inner = not inner
                elif not inner and (line.startswith("#") or line.startswith("<!-- eutonos:")):
                    break
                if line.strip():
                    exact = False
                    break
                k += 1
        if exact:
            found.append((True, begin, lines[j - 1][0] + len(lines[j - 1][1].rstrip(b"\r\n"))))
        else:
            found.append((False, begin, lines[i][0] + len(lines[i][1].rstrip(b"\r\n"))))
    return found


def merge_managed(raw, mark, body, *, place="append"):
    """Put the managed block `body` into `raw`: ``(after, mode)``; every surrounding byte is kept. `mode` is
    ``unchanged`` or ``replaced`` (an intact marked block, refreshed in place), ``wrap`` (an exact unmarked copy of
    the text gets the markers around it, nothing moves) or ``added`` (appended, or at the top with
    ``place="top"``). A customized or malformed block raises ValueError; an unmarked copy that differs, or more than
    one copy, raises `SimilarUnmarked` and writes nothing."""
    start, end, pattern = mark
    raw.decode("utf-8-sig")
    matches = list(pattern.finditer(raw))
    if raw.count(start) != len(matches) or raw.count(end) != len(matches) or len(matches) > 1:
        raise ValueError("malformed-or-duplicate-managed-block")
    desired = make_block(mark, body)
    if matches:
        match = matches[0]
        # A checkout may normalize line endings. The shipped digest is LF-normalized.
        if sha(match[2].replace(b"\r\n", b"\n")) != match[1].decode():
            raise ValueError("customized-managed-block")
        if match[0].replace(b"\r\n", b"\n") == desired:
            return raw, "unchanged"
        if b"\r\n" in match[0]:
            desired = desired.replace(b"\n", b"\r\n")
        return raw[:match.start()] + desired + raw[match.end():], "replaced"
    copies = find_unmarked(raw, pattern, body)
    newline = b"\r\n" if b"\r\n" in raw else b"\n"
    if copies:
        if len(copies) != 1 or not copies[0][0]:
            raise SimilarUnmarked()
        _, first, last = copies[0]
        text = raw[first:last]
        nl = b"\r\n" if b"\r\n" in text else b"\n"
        wrapped = text + nl
        marked = start + b" sha256=" + sha(wrapped.replace(b"\r\n", b"\n")).encode() + b" -->" + nl + wrapped + end
        return raw[:first] + marked + raw[last:], "wrap"
    if place == "top":
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        rest = raw[len(bom):]
        top = desired.replace(b"\n", newline) + newline
        return bom + top + (newline + rest if rest else b""), "added"
    return raw + (newline * 2 if raw else b"") + desired.replace(b"\n", newline) + newline, "added"


def merge_block_mode(raw, text=CONTRACT):
    return merge_managed(raw, (START, END, PATTERN), block_body(text))


def merge_block(raw, text=CONTRACT):
    """Replace only an intact shipped block; preserve every surrounding byte."""
    return merge_block_mode(raw, text)[0]


def undo_shape(before, after, mode, pattern=PATTERN):
    """What an uninstall needs to put `before` back around the block that `after` holds, as recorded in an Effect's
    undo: the earlier marked block's exact text (``replaced``), the line ending of a ``wrap``, or the exact
    separators an ``added`` block brought. ``{}`` (no mode) keeps the older whole-block removal."""
    match = pattern.search(after or b"")
    if match is None or mode not in ("replaced", "wrap", "added"):
        return {}
    if mode == "replaced":
        earlier = pattern.search(before or b"")
        return {"mode": mode, "original": earlier[0].decode("utf-8")} if earlier else {}
    if mode == "wrap":
        first = match[0].split(b"\n", 1)[0]
        return {"mode": mode, "wrap_nl": "\r\n" if first.endswith(b"\r") else "\n"}
    head, tail, old = after[:match.start()], after[match.end():], before or b""
    if head.startswith(old):
        sep_before, sep_after = head[len(old):], tail
    elif old.startswith(head) and tail.endswith(old[len(head):]):
        sep_before, sep_after = b"", tail[:len(tail) - len(old[len(head):])]
    else:
        return {"mode": mode}
    return {"mode": mode, "sep_before": sep_before.decode("utf-8"), "sep_after": sep_after.decode("utf-8")}


def restore_block(raw, undo, pattern=PATTERN, start=START, end=END):
    """Undo one install of a block using the shape `undo_shape` recorded: ``(after, state)`` with state ``removed``,
    ``absent`` or ``customized``. The block is found by its markers wherever the file now has it, so edits elsewhere
    do not matter; a block changed inside is left exactly as it is. ``replaced`` puts the earlier block back byte for
    byte, ``wrap`` takes out only the markers, ``added`` takes out the block with the separators it brought. An undo
    with no recorded mode takes the older route (`unmerge_block`)."""
    mode = (undo or {}).get("mode")
    raw.decode("utf-8-sig")
    matches = list(pattern.finditer(raw))
    if raw.count(start) != len(matches) or raw.count(end) != len(matches) or len(matches) > 1:
        raise ValueError("malformed-or-duplicate-managed-block")
    if not matches:
        return raw, "absent"
    match = matches[0]
    if sha(match[2].replace(b"\r\n", b"\n")) != match[1].decode():
        return raw, "customized"
    if mode == "replaced" and isinstance(undo.get("original"), str):
        return raw[:match.start()] + undo["original"].encode("utf-8") + raw[match.end():], "removed"
    if mode == "wrap":
        body = match[2]
        for nl in (undo.get("wrap_nl"), "\r\n", "\n"):
            if nl and body.endswith(nl.encode()):
                body = body[:-len(nl)]
                break
        return raw[:match.start()] + body + raw[match.end():], "removed"
    if mode == "added" and isinstance(undo.get("sep_before"), str) and isinstance(undo.get("sep_after"), str):
        before, after = undo["sep_before"].encode(), undo["sep_after"].encode()
        a, b = match.start() - len(before), match.end() + len(after)
        if a >= 0 and raw[a:match.start()] == before and raw[match.end():b] == after:
            return raw[:a] + raw[b:], "removed"
    return unmerge_block(raw, pattern, start, end)


def _legacy_adoption(root, raw):
    """Remove only the proved shipped embedded copy and refresh its block hash.

    The ordinary installer retains its existing per-file atomicity; this pair is
    not a transaction. Adoption's full source/version upgrade remains separate.
    """
    from . import adoption
    matches, _ = adoption._block(raw)
    if (raw.count(adoption.BLOCK_START.encode()) != len(matches)
            or raw.count(adoption.BLOCK_END.encode()) != len(matches) or len(matches) > 1):
        raise ValueError("malformed-or-duplicate-legacy-adoption-block")
    embedded = [match for match in matches if b"## Completion contract" in match[0]]
    if not matches:
        return raw, None
    match = matches[0]
    if embedded and sha(match[0].replace(b"\r\n", b"\n").rstrip(b"\n")) != LEGACY_ADOPTION_SHA256:
        raise ValueError("customized-or-unknown-legacy-adoption-block")
    path = root / adoption.MANIFEST_PATH
    before = read(path)
    if before is None:
        if embedded:
            raise ValueError("legacy-adoption-manifest-required")
        return raw, None
    try:
        manifest = adoption._validate_manifest(json.loads(before.decode("utf-8")))
    except adoption.ts.ReadError as exc:
        raise ValueError("invalid-legacy-adoption-manifest: " + exc.code) from exc
    entry = manifest["managed"].get("AGENTS.md")
    if "delegation" not in manifest["selected_modules"] or entry != {"mode": "block", "sha256": sha(match[0])}:
        raise ValueError("legacy-adoption-fingerprint-mismatch")
    if not embedded:
        # Keep a snapshot even on a no-op. It also refuses a partially migrated
        # pair or an independently customized adoption block on a later run.
        return raw, {"path": str(path), "before": before, "after": before}
    newline = b"\r\n" if b"\r\n" in match[0] else b"\n"
    suffix = ("## Completion contract\n\n" + CONTRACT + "\n").encode().replace(b"\n", newline)
    replacement = match[0].replace(suffix, b"", 1)
    manifest["managed"]["AGENTS.md"]["sha256"] = sha(replacement)
    after = adoption._json_bytes(manifest)
    return raw[:match.start()] + replacement + raw[match.end():], {"path": str(path), "before": before, "after": after}


def read(path):
    # Do not follow symlinks/junctions in the write route, including parent links.
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("linked-instruction-path")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("instruction-unreadable-or-too-large")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("instruction-too-large")
    raw.decode("utf-8-sig")
    return raw


def precedence_paths(root, *, all_markdown=False):
    """Bounded conservative inventory; nested guidance needs semantic review."""
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        count += len(dirs) + len(files)
        if count > MAX_ENTRIES:
            raise ValueError("precedence-scan-capped")
        base = Path(directory)
        dirs[:] = [name for name in dirs if name not in SKIP and not (base.name == ".claude" and name == "worktrees")]
        for name in dirs:
            target = base / name
            if target.is_symlink() or target.is_junction():
                raise ValueError("precedence-scan-linked-directory: " + str(target))
        for name in files:
            path = base / name
            relative = path.relative_to(root)
            rules = (all_markdown or (".claude" in relative.parts and "rules" in relative.parts)) and path.suffix == ".md"
            if (name in NAMES and relative.as_posix() not in {"AGENTS.md", "CLAUDE.md"}) or rules:
                yield path


def instruction_plan(*, root=None, global_instructions=False, codex_home=None, claude_home=None, reviewed=None):
    reviewed = reviewed or {}
    targets, candidates, conflicts, checks, pending = {}, set(), [], {}, []
    if global_instructions:
        codex = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
        claude = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
        targets[codex / "AGENTS.md"] = CONTRACT
        targets[claude / "CLAUDE.md"] = CONTRACT
        candidates.add(codex / "AGENTS.override.md")
        # Global Claude rules can change the effective policy too.
        rules = claude / "rules"
        if rules.exists():
            try:
                if rules.is_symlink() or rules.is_junction():
                    raise ValueError("linked-global-rules")
                candidates.update(precedence_paths(rules, all_markdown=True))
            except (OSError, ValueError) as exc:
                conflicts.append({"path": str(rules), "code": str(exc)})
    if root is not None:
        root = Path(root).absolute()
        if not root.is_dir():
            conflicts.append({"path": str(root), "code": "repository-missing"})
        targets[root / "AGENTS.md"] = CONTRACT
        # Explicit import also works with older Claude Code and an ancestor CLAUDE.md.
        targets[root / "CLAUDE.md"] = "Follow the repository instructions imported here:\n\n@AGENTS.md"
        try:
            candidates.update(precedence_paths(root))
            for parent in root.parents:
                for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
                    # The home .claude file is a user layer, not ancestor project
                    # guidance. It is handled only when globals are selected.
                    if parent / name not in targets and parent / name != Path.home() / ".claude/CLAUDE.md":
                        candidates.add(parent / name)
        except (OSError, ValueError) as exc:
            conflicts.append({"path": str(root), "code": str(exc)})
    changes = []
    for path, text in targets.items():
        try:
            before = read(path)
            raw = before or b""
            checks[str(path)] = before
            migration = None
            if root is not None and path == root / "AGENTS.md":
                raw, migration = _legacy_adoption(root, raw)
                if migration:
                    checks[migration["path"]] = migration["before"]
            # Existing explicit Claude import remains user-owned.
            if text != CONTRACT and re.search(rb"(?m)^@AGENTS\.md\s*$", raw) and START not in raw:
                after, mode = raw, "unchanged"
            else:
                after, mode = merge_block_mode(raw, text)
            unmanaged = PATTERN.sub(b"", raw).decode("utf-8-sig")
            # Leads for semantic review, never an NLP claim of conflict-free policy.
            if re.search(r"(?im)\b(?:(?:never|do not|don't) merge(?:[.!]|\s+(?:anything|changes|code|unless|without|until)\b)|stop after (?:tests|committing|opening)|complete when tests pass)", unmanaged):
                candidates.add(path)
            changes.append({"path": str(path), "before": before, "after": after, "mode": mode})
            if migration:
                changes.append(migration)
        except SimilarUnmarked:
            # The person's own version stays active; nothing is written to this file.
            pending.append({"path": str(path), "code": SIMILAR, "current_sha256": sha(raw),
                            "shipped_sha256": sha(block(text))})
        except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
            conflicts.append({"path": str(path), "code": str(exc)})
    reviews = []
    for path in sorted(candidates):
        try:
            raw = read(path)
            checks[str(path)] = raw
            if raw and raw.strip():
                item = {"path": str(path), "sha256": sha(raw), "code": "instruction-precedence-review"}
                if reviewed.get(str(path)) != item["sha256"]:
                    conflicts.append(item)
                else:
                    reviews.append(item)
        except (OSError, ValueError, UnicodeError) as exc:
            conflicts.append({"path": str(path), "code": str(exc)})
    return {"changes": changes, "conflicts": conflicts, "pending": pending, "reviewed": reviews, "checks": checks,
            "selection": dict(root=root, global_instructions=global_instructions,
                              codex_home=codex_home, claude_home=claude_home, reviewed=reviewed)}


def decisions_plan(*, root=None, global_instructions=False, codex_home=None, claude_home=None, reviewed=None):
    """The Decisions block for the same global files (and, with `root`, that repository's AGENTS.md only). Same
    preservation rules as the completion block: an exact unmarked copy is wrapped in place, a similar one is listed
    as pending and left alone, a customized marked block is a conflict. `reviewed` is accepted and unused."""
    targets = []
    if global_instructions:
        codex = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
        claude = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
        targets += [codex / "AGENTS.md", claude / "CLAUDE.md"]
    if root is not None:
        targets.append(Path(root).absolute() / "AGENTS.md")
    mark = (DECISIONS_START, DECISIONS_END, DECISIONS_PATTERN)
    changes, conflicts, pending, checks = [], [], [], {}
    for path in targets:
        try:
            before = read(path)
            checks[str(path)] = before
            raw = before or b""
            try:
                after, mode = merge_managed(raw, mark, block_body(DECISIONS, DECISIONS_HEADING))
            except SimilarUnmarked:
                pending.append({"path": str(path), "code": SIMILAR, "current_sha256": sha(raw),
                                "shipped_sha256": sha(decisions_block())})
                continue
            changes.append({"path": str(path), "before": before, "after": after, "mode": mode})
        except (OSError, ValueError, UnicodeError) as exc:
            conflicts.append({"path": str(path), "code": str(exc)})
    return {"changes": changes, "conflicts": conflicts, "pending": pending, "reviewed": [], "checks": checks,
            "selection": dict(root=root, global_instructions=global_instructions, codex_home=codex_home,
                              claude_home=claude_home, reviewed=reviewed)}


def report(plan):
    return {"outcome": "conflict" if plan["conflicts"] else "ready", "policy_sha256": sha(CONTRACT.encode()),
            "files": [{"path": row["path"], "action": "unchanged" if row["before"] == row["after"] else "update",
                       "before_sha256": sha(row["before"]) if row["before"] is not None else None,
                       "after_sha256": sha(row["after"])} for row in plan["changes"]],
            "conflicts": plan["conflicts"], "pending": plan.get("pending", []), "reviewed": plan["reviewed"],
            "qualification": "Instruction bytes and matching adoption fingerprints only; no model compliance, account sync or live hook activation is proven. "
                             "Other imports, custom harness configuration and organizational policy need semantic review."}


def install(plan, planner=None):
    result = report(plan)
    result["written"] = []
    result["applied_but_unverified"] = []
    if plan["conflicts"]:
        return result
    try:
        fresh = (planner or instruction_plan)(**plan["selection"])
        if fresh["conflicts"] or fresh["checks"] != plan["checks"] or fresh["changes"] != plan["changes"]:
            raise ValueError("stale-instruction-plan: instruction content or precedence changed")
        for path, raw in plan["checks"].items():
            if read(Path(path)) != raw:
                raise ValueError("stale-instruction-plan: " + path)
        for row in plan["changes"]:
            if row["before"] == row["after"]:
                continue
            path = Path(row["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            # Recheck at the boundary; atomic replacement never exposes partial text.
            if read(path) != row["before"]:
                raise ValueError("stale-instruction-plan: " + str(path))
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".eutonos-instruction-", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(row["after"])
            try:
                if read(path) != row["before"]:
                    raise ValueError("stale-instruction-plan: " + str(path))
                os.replace(temp, path)
                result["applied_but_unverified"].append(str(path))
            finally:
                temp.unlink(missing_ok=True)
            if read(path) != row["after"]:
                raise ValueError("instruction-readback-failed: " + str(path))
            result["written"].append(str(path))
            result["applied_but_unverified"].remove(str(path))
        result["outcome"] = "installed"
    except (OSError, ValueError, UnicodeError) as exc:
        result.update(outcome="attention-required", error=str(exc))
        if result["written"] or result["applied_but_unverified"]:
            result["next_action"] = ("Inspect applied files and their readback before retry; "
                                     "a partial legacy adoption pair requires reviewed reconciliation.")
    return result


def unmerge_block(raw, pattern=PATTERN, start=START, end=END):
    """The inverse of `merge_block`: ``(after, state)`` with state ``removed``, ``absent`` or ``customized``.

    Only an intact block (its body still hashes to the digest in its marker) is removed, together with the one
    newline the merge added after it and the blank line it added before it (or after it, for a block inserted at the
    top). A customized block is left exactly as it is. Malformed or duplicate markers raise ValueError."""
    raw.decode("utf-8-sig")
    matches = list(pattern.finditer(raw))
    if raw.count(start) != len(matches) or raw.count(end) != len(matches) or len(matches) > 1:
        raise ValueError("malformed-or-duplicate-managed-block")
    if not matches:
        return raw, "absent"
    match = matches[0]
    if sha(match[2].replace(b"\r\n", b"\n")) != match[1].decode():
        return raw, "customized"
    newline = b"\r\n" if b"\r\n" in match[0] else b"\n"
    head, tail = raw[:match.start()], raw[match.end():]
    if tail.startswith(newline):
        tail = tail[len(newline):]
    bom = b"\xef\xbb\xbf" if head.startswith(b"\xef\xbb\xbf") else b""
    if not tail and head.endswith(newline * 2):
        head = head[:-2 * len(newline)]
    elif head == bom and tail.startswith(newline):
        tail = tail[len(newline):]
    return head + tail, "removed"


def remove_plan(*, root=None, global_instructions=False, codex_home=None, claude_home=None):
    """What `remove` would change: the same target files `instruction_plan` selects, each with its block taken out."""
    targets = []
    if global_instructions:
        codex = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
        claude = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
        targets += [codex / "AGENTS.md", claude / "CLAUDE.md"]
    if root is not None:
        targets += [Path(root).absolute() / "AGENTS.md", Path(root).absolute() / "CLAUDE.md"]
    changes, kept = [], []
    for path in targets:
        try:
            before = read(path)
            if before is None:
                continue
            after, state = unmerge_block(before)
        except (OSError, ValueError, UnicodeError) as exc:
            kept.append({"path": str(path), "reason": str(exc)})
            continue
        if state == "customized":
            kept.append({"path": str(path), "reason": "customized-managed-block: changed by you, kept"})
        elif state == "removed":
            changes.append({"path": str(path), "before": before, "after": after})
    return {"changes": changes, "kept": kept,
            "selection": dict(root=root, global_instructions=global_instructions, codex_home=codex_home,
                              claude_home=claude_home)}


def remove(plan):
    """Apply a `remove_plan`: take out each unchanged managed block (atomic replace, source rechecked, readback).
    A file is never deleted here (the caller knows whether it created the file)."""
    result = {"outcome": "removed", "removed": [], "kept": list(plan["kept"])}
    try:
        for row in plan["changes"]:
            path = Path(row["path"])
            if read(path) != row["before"]:
                raise ValueError("stale-instruction-plan: " + str(path))
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".eutonos-instruction-", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(row["after"])
            try:
                if read(path) != row["before"]:
                    raise ValueError("stale-instruction-plan: " + str(path))
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            if read(path) != row["after"]:
                raise ValueError("instruction-readback-failed: " + str(path))
            result["removed"].append(str(path))
    except (OSError, ValueError, UnicodeError) as exc:
        result.update(outcome="attention-required", error=str(exc))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "install"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--global", dest="global_instructions", action="store_true")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--claude-home", type=Path)
    parser.add_argument("--reviewed", action="append", default=[], metavar="PATH=SHA256",
                        help="Acknowledge semantic review of these exact conflicting instruction bytes; never overwrites them.")
    args = parser.parse_args(argv)
    if args.root is None and not args.global_instructions:
        parser.error("Select --global and/or an explicit --root")
    reviewed = {}
    for entry in args.reviewed:
        path, sep, digest = entry.rpartition("=")
        if not sep or not re.fullmatch("[0-9a-f]{64}", digest):
            parser.error("--reviewed needs PATH=SHA256")
        reviewed[str(Path(path).absolute())] = digest
    plan = instruction_plan(root=args.root, global_instructions=args.global_instructions,
                            codex_home=args.codex_home, claude_home=args.claude_home, reviewed=reviewed)
    result = install(plan) if args.command == "install" else report(plan)
    # The Decisions block travels with the contract (same files; the repository's AGENTS.md only).
    dplan = decisions_plan(root=args.root, global_instructions=args.global_instructions,
                           codex_home=args.codex_home, claude_home=args.claude_home)
    decisions = report(dplan)
    decisions["policy_sha256"] = sha(DECISIONS.encode())
    if args.command == "install" and result["outcome"] == "installed":
        decisions = install(dplan, decisions_plan)
        decisions["policy_sha256"] = sha(DECISIONS.encode())
    result["decisions"] = decisions
    print(json.dumps(result, ensure_ascii=True, indent=2))
    good = {"ready", "installed"}
    return 0 if result["outcome"] in good and decisions["outcome"] in good else 2


if __name__ == "__main__":
    raise SystemExit(main())
