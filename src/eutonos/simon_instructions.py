"""Managed Simon block: tells Claude Code, Codex and Hermes to use the Simon that ships with EUTONOS when native
computer use fails or is blocked.

Written only while the Simon part is installed and switched on (the installer gates it: ``COMPONENT_BLOCKS`` with
``needs_on``), into the global ``~/.claude/CLAUDE.md`` and ``~/.codex/AGENTS.md`` and, when the Hermes part is on,
into Hermes' existing ``SOUL.md`` (never created). The same preservation rules as completion_policy, whose machinery it
reuses (hash-marked block, every surrounding byte kept, exact unmarked copy wrapped in place, similar copy left active
and listed, customized block refused, atomic replace with readback, CRLF and BOM kept). Instruction files only.

The text is short and user-agnostic (no names or personal paths); the per-harness lines are in ``HARNESSES``.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import completion_policy as cp

NAME = "simon"
TITLE = "## EUTONOS Simon (desktop control)"
START, END, PATTERN = cp.markers(NAME)
MARK = (START, END, PATTERN)
SIZE_CAP = 1800
HARNESSES = {
    "claude": {"skill": "the `simon-planner` skill (`~/.claude/skills/simon-planner/`)", "sandbox": ""},
    "codex": {"skill": "the `simon-planner` skill (`~/.codex/skills/simon-planner/`)",
              "sandbox": " In Codex, run `eutonos-simon` outside the sandbox (ask for escalation): Simon refuses the "
                         "sandbox desktop because it cannot see the real one."},
    "hermes": {"skill": "the `simon-planner` skill (Hermes skills, category `eutonos`)", "sandbox": ""},
}
TEXT = """\
EUTONOS put this block here because its optional Simon part is switched on. Simon is the Windows computer-use helper that ships with EUTONOS.

- When native computer use fails, is blocked or is not available and the task needs the Windows desktop, use Simon. When the user says "use Simon", this is the Simon meant.
- Run it as `eutonos-simon <command>` from any folder (it is on the PATH): start with `eutonos-simon helper.bridge_status`; `eutonos-simon --where` shows where Simon and its Quickstart are installed.{sandbox}
- Before desktop work read {skill} and the QUICKSTART.md beside it. Prefer files, APIs or app commands where they fit; act one verified step at a time and stop when capture or focus is uncertain.
- Finish with `eutonos-simon helper.exit --via-app` and check that the Helper stopped."""


def render(harness: str) -> str:
    if harness not in HARNESSES:
        raise ValueError("unknown-harness: " + str(harness))
    return TEXT.format(**HARNESSES[harness])


def body(harness: str) -> bytes:
    return cp.block_body(render(harness), TITLE)


def block(harness: str, base=None) -> bytes:
    return cp.make_block(MARK, body(harness))


def policy_sha256(base=None) -> dict:
    return {harness: cp.sha(body(harness)) for harness in HARNESSES}


def _note_missing(path) -> str:
    return (f"Hermes has no SOUL.md at {path}, so the Simon instruction block was not added there. EUTONOS never "
            "creates that file; once it exists, run an update.")


def instruction_plan(*, base=None, codex_home=None, claude_home=None, hermes_soul=None):
    """The Simon block for ``~/.codex/AGENTS.md`` and ``~/.claude/CLAUDE.md`` (created when absent, like the other
    blocks) and, when `hermes_soul` is given, for that file only if it already exists. Rows carry their own
    ``shipped`` bytes (the text differs per harness)."""
    codex = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
    claude = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").absolute()
    targets = [("codex", codex / "AGENTS.md"), ("claude", claude / "CLAUDE.md")]
    if hermes_soul is not None:
        targets.append(("hermes", Path(hermes_soul).absolute()))
    shipped = {harness: block(harness) for harness, _path in targets}
    changes, conflicts, pending, checks, notes = [], [], [], {}, []
    for harness, path in targets:
        row = {"path": str(path), "shipped_sha256": cp.sha(shipped[harness])}
        try:
            before = cp.read(path)
            checks[str(path)] = before
            if before is None and harness == "hermes":
                notes.append(_note_missing(path))
                continue
            try:
                after, mode = cp.merge_managed(before or b"", MARK, body(harness), place="append")
            except cp.SimilarUnmarked:
                pending.append({**row, "code": cp.SIMILAR, "current_sha256": cp.sha(before or b"")})
                continue
            changes.append({"path": str(path), "before": before, "after": after, "mode": mode,
                            "shipped": shipped[harness]})
        except (OSError, ValueError, UnicodeError) as exc:
            conflicts.append({**row, "code": str(exc)})
    return {"changes": changes, "conflicts": conflicts, "pending": pending, "reviewed": [], "checks": checks,
            "notes": notes,
            "selection": dict(base=base, codex_home=codex_home, claude_home=claude_home, hermes_soul=hermes_soul)}


def report(plan):
    result = cp.report(plan)
    result["policy_sha256"] = policy_sha256()
    return result


def install(plan):
    result = cp.install(plan, instruction_plan)
    result["policy_sha256"] = policy_sha256()
    return result
