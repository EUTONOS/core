"""Bounded search for large files: print only compact snippets around matches.

Built for agent use, where every byte of tool output costs context. Handles
multi-megabyte JSONL logs, huge single-line files and binaries without dumping
whole lines. Output is capped by --budget; a footer always reports how many
matches were found versus shown, so a truncated result is never mistaken for
a complete one.

Examples:
  python tools/bigsearch.py "autoCompactWindow" path/to/claude.exe
  python tools/bigsearch.py -F "task_complete" rollout.jsonl --since 77307223 \
      --field payload.type --field payload.last_agent_message
  python tools/bigsearch.py "def \\w+_queue" src --glob "*.py" --count
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import mmap
import os
import re
import sys

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def iter_files(paths: list[str], globs: list[str]):
    for root in paths:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not globs or any(fnmatch.fnmatch(name, g) for g in globs):
                    yield os.path.join(dirpath, name)


def clean(raw: bytes, width: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    # Collapse whitespace and unreadable runs so binary context stays short.
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f�]+", "·", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def dig(obj, dotted: str):
    for part in dotted.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pattern", help="regex (or literal with -F)")
    ap.add_argument("paths", nargs="+", help="files or directories")
    ap.add_argument("-F", "--fixed", action="store_true", help="literal pattern")
    ap.add_argument("-i", "--ignore-case", action="store_true")
    ap.add_argument("-C", "--context", type=int, default=60, help="bytes of context each side (default 60)")
    ap.add_argument("--width", type=int, default=240, help="max chars per snippet (default 240)")
    ap.add_argument("--max", type=int, default=20, help="max snippets shown in total (default 20)")
    ap.add_argument("--per-file", type=int, default=5, help="max snippets per file (default 5)")
    ap.add_argument("--budget", type=int, default=4000, help="max output chars before stopping (default 4000)")
    ap.add_argument("--since", type=int, default=0, help="skip bytes before this offset (e.g. a recorded log size)")
    ap.add_argument("--glob", action="append", default=[], help="filename filter for directories (repeatable)")
    ap.add_argument("--count", action="store_true", help="only print match counts per file")
    ap.add_argument("--unique", action="store_true", help="drop repeated snippets")
    ap.add_argument("--field", action="append", default=[],
                    help="JSONL mode: print these dotted fields of each matching line instead of raw context")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    pat = re.escape(args.pattern) if args.fixed else args.pattern
    rx = re.compile(pat.encode("utf-8"), re.IGNORECASE if args.ignore_case else 0)

    shown = found = files_hit = files_scanned = bytes_scanned = 0
    used = 0
    seen: set[str] = set()
    stopped = False

    def emit(line: str) -> bool:
        nonlocal used
        if used + len(line) + 1 > args.budget:
            return False
        print(line)
        used += len(line) + 1
        return True

    for path in iter_files(args.paths, args.glob):
        try:
            size = os.path.getsize(path)
            if size <= args.since:
                continue
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            files_scanned += 1
            bytes_scanned += size - args.since
            data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) if size else b""
            n_here = shown_here = 0
            line_no, line_pos = 1, 0
            for m in rx.finditer(data, args.since):
                n_here += 1
                if args.count or stopped or shown >= args.max or shown_here >= args.per_file:
                    continue
                start, end = m.start(), m.end()
                line_no += data[line_pos:start].count(b"\n")
                line_pos = start
                if args.field:
                    ls = data.rfind(b"\n", 0, start) + 1
                    le = data.find(b"\n", end)
                    le = len(data) if le == -1 else le
                    try:
                        rec = json.loads(data[ls:le])
                        parts = [f"{f}={json.dumps(dig(rec, f), ensure_ascii=False)}" for f in args.field]
                        snippet = clean(" ".join(parts).encode("utf-8"), args.width)
                    except ValueError:
                        snippet = "(not JSON) " + clean(data[max(ls, start - args.context):end + args.context], args.width)
                else:
                    snippet = clean(data[max(0, start - args.context):end + args.context], args.width)
                if args.unique:
                    if snippet in seen:
                        continue
                    seen.add(snippet)
                if not emit(f"{path}:{line_no}@{start}: {snippet}"):
                    stopped = True
                    continue
                shown += 1
                shown_here += 1
            if isinstance(data, mmap.mmap):
                data.close()
            if n_here:
                files_hit += 1
                found += n_here
                if args.count:
                    emit(f"{path}: {n_here}")

    note = " (output budget reached)" if stopped else ""
    print(f"-- {found} matches in {files_hit}/{files_scanned} files "
          f"({bytes_scanned:,} bytes scanned); {shown} shown{note}")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
