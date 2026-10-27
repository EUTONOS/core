"""Render a paste-ready prompt that teaches another harness a field trick.

Source of truth is the "Built-in field tricks (running list)" markdown table
in docs/FUTURE_FEATURES.md (columns: Trick | What it gives | Status | Built-in
version needs). This script parses that table read-only by locating its
heading; it never hardcodes the rows, so it stays correct as the table grows.

Commands:
  list                       print trick names, numbered
  render TRICK --harness H   render a prompt for trick TRICK on harness H
                             (codex or claude); TRICK matches by case-
                             insensitive substring or 1-based row number

Examples:
  python tools/handoff_prompt.py list
  python tools/handoff_prompt.py render bigsearch --harness codex
  python tools/handoff_prompt.py render 3 --harness claude --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

HEADING = "Built-in field tricks (running list)"
FUTURE_FEATURES_REL = os.path.join("docs", "FUTURE_FEATURES.md")

HARNESS_REMEMBER = {
    "claude": "Save this as a feedback/reference memory in your auto-memory.",
    "codex": "Add this to your memories / AGENTS-level notes.",
}


def parse_table(text: str) -> list[dict[str, str]]:
    """Parse the field-tricks markdown table. Returns a list of row dicts
    with keys: trick, gives, status, needs.
    """
    idx = text.find(HEADING)
    if idx == -1:
        raise ValueError(f"heading not found: {HEADING!r}")
    tail = text[idx:]

    lines = tail.splitlines()
    rows = []
    in_table = False
    header_seen = False
    for line in lines:
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("|") and "Trick" in stripped:
                in_table = True
            continue
        if not stripped.startswith("|"):
            break  # table ended
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not header_seen:
            header_seen = True
            continue
        if all(re.fullmatch(r"-+", c) for c in cells):
            continue  # separator row
        if len(cells) < 4:
            continue
        rows.append({
            "trick": cells[0],
            "gives": cells[1],
            "status": cells[2],
            "needs": cells[3],
        })
    return rows


def load_rows(root: str) -> list[dict[str, str]]:
    path = os.path.join(root, FUTURE_FEATURES_REL)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return parse_table(text)


def find_matches(rows: list[dict[str, str]], query: str) -> list[int]:
    """Return 0-based indices of rows matching query by number or substring.

    Substring matching checks the trick name first (e.g. "portability"), then
    backticked paths/commands in the row (so "bigsearch" resolves to the row
    carrying `tools/bigsearch.py`), then the whole row text.
    """
    q = query.strip()
    if q.isdigit():
        n = int(q)
        if 1 <= n <= len(rows):
            return [n - 1]
        return []
    ql = q.lower()
    name_matches = [i for i, r in enumerate(rows) if ql in r["trick"].lower()]
    if name_matches:
        return name_matches
    # A backticked path/command (e.g. `tools/bigsearch.py`) names the tool itself,
    # so it outranks a passing mention in another row's prose.
    code_matches = [i for i, r in enumerate(rows)
                    if any(ql in c.lower() for c in BACKTICK_RE.findall(r["gives"] + " " + r["status"] + " " + r["needs"]))]
    if code_matches:
        return code_matches
    return [
        i for i, r in enumerate(rows)
        if ql in (r["gives"] + " " + r["status"] + " " + r["needs"]).lower()
    ]


BACKTICK_RE = re.compile(r"`([^`]+)`")


def extract_paths_and_commands(cell_text: str, root: str) -> list[str]:
    """Extract backticked snippets from a cell, tagging ones that look like a
    repo-relative tool path that doesn't exist in this checkout.
    """
    notes = []
    for snippet in BACKTICK_RE.findall(cell_text):
        looks_like_path = ("/" in snippet or "\\" in snippet) and " " not in snippet.split()[0] if snippet else False
        first_token = snippet.split()[0] if snippet.split() else snippet
        if ("/" in first_token or first_token.endswith(".py")) and not first_token.startswith("http"):
            candidate = first_token.rstrip(",.;:")
            full = os.path.join(root, candidate)
            if not os.path.exists(full):
                notes.append(f"`{snippet}` (not present in this checkout)")
                continue
        notes.append(f"`{snippet}`")
    return notes


def render_prompt(row: dict[str, str], harness: str, root: str) -> str:
    remember = HARNESS_REMEMBER[harness]
    snippets = extract_paths_and_commands(row["gives"], root) + extract_paths_and_commands(row["needs"], root)
    status_snippets = extract_paths_and_commands(row["status"], root)
    all_snippets = []
    seen = set()
    for s in status_snippets + snippets:
        if s not in seen:
            seen.add(s)
            all_snippets.append(s)

    lines = [
        f"Field trick: {row['trick']}",
        "",
        f"What it gives: {row['gives']}",
        f"Current status: {row['status']}",
    ]
    if all_snippets:
        lines.append("How to use it here: " + "; ".join(all_snippets))
    lines.append(f"Built-in version still needs: {row['needs']}")
    lines.append("")
    lines.append(remember)
    lines.append('Reply with one line confirming what you saved.')
    return "\n".join(lines)


def cmd_list(args) -> int:
    rows = load_rows(args.root)
    for i, r in enumerate(rows, start=1):
        print(f"{i}. {r['trick']}")
    return 0


def cmd_render(args) -> int:
    rows = load_rows(args.root)
    matches = find_matches(rows, args.trick)
    if len(matches) == 0:
        print(f"error: no trick matches {args.trick!r}", file=sys.stderr)
        return 2
    if len(matches) > 1:
        print(f"error: {args.trick!r} is ambiguous, matches:", file=sys.stderr)
        for i in matches:
            print(f"  {i + 1}. {rows[i]['trick']}", file=sys.stderr)
        return 2

    row = rows[matches[0]]
    prompt = render_prompt(row, args.harness, args.repo_path or args.root)

    if args.json:
        print(json.dumps({"trick": row["trick"], "harness": args.harness, "prompt": prompt}, indent=2))
    else:
        print(prompt)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="repo root (default: parent of tools/)")

    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print trick names, numbered")

    p_render = sub.add_parser("render", help="render a handoff prompt for one trick")
    p_render.add_argument("trick", help="trick name (substring, case-insensitive) or 1-based number")
    p_render.add_argument("--harness", required=True, choices=sorted(HARNESS_REMEMBER),
                           help="target harness")
    p_render.add_argument("--repo-path", default=None,
                           help="repo path to check tool existence against (default: --root)")
    p_render.add_argument("--json", action="store_true", help="emit JSON instead of plain text")

    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if args.command == "list":
        return cmd_list(args)
    if args.command == "render":
        return cmd_render(args)
    ap.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
