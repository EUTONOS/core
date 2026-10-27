"""Managed Decisions block for the global Claude and Codex instruction files.

The same preservation rules as completion_policy, whose machinery it reuses (hash-marked block, every surrounding
byte kept, exact unmarked copy wrapped in place, similar copy left active and listed, customized block refused,
atomic replace with readback). Instruction files only: no settings, services or Git.
"""
from __future__ import annotations

from . import completion_policy as cp

TITLE = cp.DECISIONS_HEADING
TEXT = cp.DECISIONS
START, END, PATTERN = cp.DECISIONS_START, cp.DECISIONS_END, cp.DECISIONS_PATTERN


def block():
    return cp.decisions_block()


def policy_sha256():
    return cp.sha(TEXT.encode("utf-8"))


def instruction_plan(*, root=None, global_instructions=True, codex_home=None, claude_home=None, reviewed=None):
    return cp.decisions_plan(root=root, global_instructions=global_instructions, codex_home=codex_home,
                             claude_home=claude_home, reviewed=reviewed)


def report(plan):
    result = cp.report(plan)
    result["policy_sha256"] = policy_sha256()
    return result


def install(plan):
    result = cp.install(plan, cp.decisions_plan)
    result["policy_sha256"] = policy_sha256()
    return result


def unmerge(raw):
    """The older whole-block removal (``(after, state)``); `cp.restore_block` is the lossless one."""
    return cp.unmerge_block(raw, PATTERN, START, END)
