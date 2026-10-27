"""Managed memory block: a short pointer for the global Claude and Codex instruction files and, when the Hermes
component is installed, Hermes' ``SOUL.md``.

The same preservation rules as completion_policy, whose machinery it reuses (hash-marked block, every surrounding
byte kept, exact unmarked copy wrapped in place, similar copy left active and listed, customized block refused,
atomic replace with readback, CRLF and BOM kept). Instruction files only: no settings, services or Git.

The text lives in ``templates/memory_block.md`` and is rendered per harness from a closed set of tokens
(`TOKENS`); an unknown or leftover token raises `TemplateError`, so a typo cannot ship. The heading is `TITLE`.
Hermes is gated by the installer (it passes ``hermes_soul`` only when the hermes component row is installed) and
``SOUL.md`` is never created: a missing file is reported in the plan's ``notes`` and left missing.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from . import completion_policy as cp

NAME = "memory"
TITLE = "## EUTONOS (installed tooling)"
START, END, PATTERN = cp.markers(NAME)
MARK = (START, END, PATTERN)
TEMPLATE_FILE = "memory_block.md"
# Rendered block (markers included): the size we aim for and the cap a test enforces. Codex reads global text first
# under a combined limit, so every global byte can push a repository's own rules out.
SIZE_TARGET = 3000
SIZE_CAP = 3500
# Hermes keeps about this many characters of SOUL.md; an appended block lands in the tail, so say so well before it.
SOUL_CAP_CHARS = 20000
SOUL_NOTE_CHARS = 16000
HARNESSES = {
    "claude": {"harness": "Claude Code", "file": "~/.claude/CLAUDE.md",
               "crosstalk_tools": "the crosstalk-p0 tools (status, list, send, create)"},
    "codex": {"harness": "Codex", "file": "~/.codex/AGENTS.md",
              "crosstalk_tools": "the crosstalk_* tools (status, list, send, create)"},
    "hermes": {"harness": "Hermes", "file": "Hermes's SOUL.md",
               "crosstalk_tools": "the crosstalk_* tools (status, list, send, create)"},
}
TOKENS = ("harness", "file", "crosstalk_tools", "eutonos")

_TOKEN = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")
_AUTHORING = re.compile(r"\A\s*(?:<!--.*?-->\s*)+", re.S)


class TemplateError(ValueError):
    """The block template is missing, empty, or names a token this module does not know."""


def template() -> str:
    """The template text, LF line endings (a checkout may have converted them)."""
    path = Path(__file__).parent / "templates" / TEMPLATE_FILE
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TemplateError(f"memory-template-unreadable: {path}") from exc
    return text.replace("\r\n", "\n")


def render(harness: str, base) -> str:
    """The body text (no heading, LF, no trailing newline) for `harness`; `base` is the EUTONOS home. Leading HTML
    comments in the template are authoring notes and are dropped."""
    if harness not in HARNESSES:
        raise TemplateError("unknown-harness: " + str(harness))
    values = dict(HARNESSES[harness], eutonos=str(base).replace("\\", "/").rstrip("/"))
    text = _AUTHORING.sub("", template(), count=1).strip()
    if not text:
        raise TemplateError("memory-template-empty")
    if "{{" in _TOKEN.sub("", text) or "}}" in _TOKEN.sub("", text):
        raise TemplateError("leftover-template-token")

    def fill(match):
        if match[1] not in TOKENS:
            raise TemplateError("unknown-template-token: " + match[1])
        return values[match[1]]

    return _TOKEN.sub(fill, text)


def body(harness: str, base) -> bytes:
    return cp.block_body(render(harness, base), TITLE)


def block(harness: str, base) -> bytes:
    return cp.make_block(MARK, body(harness, base))


def policy_sha256(base) -> dict:
    """The digest of each harness' rendered body (the bodies differ per harness)."""
    return {harness: cp.sha(body(harness, base)) for harness in HARNESSES}


def _note_missing(path) -> str:
    return (f"Hermes has no SOUL.md at {path}, so the memory instruction block was not added there. EUTONOS never "
            "creates that file; once it exists, run an update.")


def _note_size(path, size) -> str:
    return (f"Hermes' SOUL.md at {path} is {size} characters with the memory block, and Hermes keeps about "
            f"{SOUL_CAP_CHARS} of them (it drops the middle), so the block may be cut. Shorten the file.")


def instruction_plan(*, base, codex_home=None, claude_home=None, hermes_soul=None):
    """The memory block for ``~/.codex/AGENTS.md`` and ``~/.claude/CLAUDE.md`` (created when absent, like the other
    blocks) and, when `hermes_soul` is given, for that file only if it already exists. Rows carry their own
    ``shipped`` bytes (the bodies differ per harness). ``notes`` lists what the person should know (a missing or
    nearly full SOUL.md); it is not part of the selection."""
    codex = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
    claude = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
    targets = [("codex", codex / "AGENTS.md"), ("claude", claude / "CLAUDE.md")]
    if hermes_soul is not None:
        targets.append(("hermes", Path(hermes_soul).absolute()))
    # Render first: a broken template stops the install visibly instead of turning into a per-file conflict.
    shipped = {harness: block(harness, base) for harness, _path in targets}
    changes, conflicts, pending, checks, notes = [], [], [], {}, []
    for harness, path in targets:
        row = {"path": str(path), "shipped_sha256": cp.sha(shipped[harness])}
        try:
            before = cp.read(path)
            checks[str(path)] = before
            if before is None and harness == "hermes":
                notes.append(_note_missing(path))
                continue
            raw = before or b""
            try:
                after, mode = cp.merge_managed(raw, MARK, body(harness, base), place="append")
            except cp.SimilarUnmarked:
                pending.append({**row, "code": cp.SIMILAR, "current_sha256": cp.sha(raw)})
                continue
            changes.append({"path": str(path), "before": before, "after": after, "mode": mode,
                            "shipped": shipped[harness]})
            if harness == "hermes":
                size = len(after.decode("utf-8-sig"))
                if size > SOUL_NOTE_CHARS:
                    notes.append(_note_size(path, size))
        except (OSError, ValueError, UnicodeError) as exc:
            conflicts.append({**row, "code": str(exc)})
    return {"changes": changes, "conflicts": conflicts, "pending": pending, "reviewed": [], "checks": checks,
            "notes": notes,
            "selection": dict(base=base, codex_home=codex_home, claude_home=claude_home, hermes_soul=hermes_soul)}


def report(plan):
    result = cp.report(plan)
    result["policy_sha256"] = policy_sha256(plan["selection"]["base"])
    return result


def install(plan):
    result = cp.install(plan, instruction_plan)
    result["policy_sha256"] = policy_sha256(plan["selection"]["base"])
    return result
