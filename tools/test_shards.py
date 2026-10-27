"""Run the unittest suite split across parallel processes and summarize.

The full suite takes over an hour in one process; split across shards it takes
10-20 minutes. Shards default to a third of the CPU count (at most 8). Test files are
balanced greedily by recorded cost (size until a run has recorded them); a
module far above a fair share is split by TestCase class or slow test. Each
shard writes its full output to a log, per-class and slow-test wall seconds are
recorded to .eutonos-local/stress/test_costs.json after each run, and only a
compact summary is printed: per-shard counts, the total, and every FAIL/ERROR line.

PYTHONPATH is set to this checkout's own src and tests, so a worktree run
never picks up another checkout's installed package.

Examples:
  python tools/test_shards.py                 # default shards, this checkout
  python tools/test_shards.py 4 --root ../wt  # 4 shards of another worktree
  python tools/test_shards.py --only "test_settings*"
  python tools/test_shards.py --only test_settings,test_assignments
  python tools/test_shards.py --profile       # also write per-test seconds

Agents run tests this way rather than setting PYTHONPATH on a command line.
The dashboard JS tests need Node and skip without it; when node is not on
PATH, the runner adds a known local copy (Codex ships one) and says so.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def node_dir(env: dict) -> str | None:
    """A directory holding node.exe/node when PATH lacks one, else None."""
    if shutil.which("node", path=env.get("PATH")):
        return None
    home = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    for candidate in (home / "OpenAI" / "Codex" / "bin",):
        if (candidate / "node.exe").is_file() or (candidate / "node").is_file():
            return str(candidate)
    return None


COSTS_NAME = "test_costs.json"
SPLIT_MIN_SHARE = 0.05  # a class or test is peeled into its own unit when it costs this share of a shard


def default_shards() -> int:
    """A third of the CPUs, between 1 and 8: the suite is git/IO heavy, so more shards than this slow every test
    down (measured: 12-16 shards on 24 cores were no faster than 8) and time-sensitive tests flake."""
    return max(1, min((os.cpu_count() or 1) // 3, 8))


def load_costs(path: Path | None) -> dict[str, dict[str, float]]:
    """Recorded seconds: {"classes": {module.Class: s}, "tests": {module.Class.test: s}}; empty when absent or unreadable."""
    out: dict[str, dict[str, float]] = {"classes": {}, "tests": {}}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for field, source in (("classes", data["units"]), ("tests", data.get("slow_tests", {}))):
            out[field] = {str(k): float(v) for k, v in source.items() if isinstance(v, (int, float))}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"classes": {}, "tests": {}}
    return out


def module_cost(classes: dict[str, float], module: str) -> float | None:
    prefix = module + "."
    found = [v for k, v in classes.items() if k.startswith(prefix)]
    return sum(found) if found else None


def top_level_classes(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def peel_candidates(module: str, source: str, costs: dict) -> list[tuple[float, str, str]]:
    """(seconds, unit name, skip token) for classes and slow tests that may run in their own shard.

    Slow tests are peeled individually; a class is peeled whole only when none of
    its tests is recorded as slow (so no test is covered by two units).
    """
    classes = top_level_classes(source)
    prefix = module + "."
    found: list[tuple[float, str, str]] = []
    heavy_in: set[str] = set()
    for key, seconds in costs.get("tests", {}).items():
        if not key.startswith(prefix):
            continue
        cls, _, method = key[len(prefix):].partition(".")
        if cls in classes and method and "." not in method and re.search(rf"def {re.escape(method)}\(", source):
            found.append((seconds, f"{module}.{cls}.{method}", f"{cls}.{method}"))
            heavy_in.add(cls)
    for key, seconds in costs.get("classes", {}).items():
        cls = key[len(prefix):] if key.startswith(prefix) else ""
        if cls in classes and cls not in heavy_in:
            found.append((seconds, f"{module}.{cls}", cls))
    return sorted(found, key=lambda item: (-item[0], item[1]))


def plan(files: list[Path], shards: int, costs: dict | None = None) -> list[list[str]]:
    """Longest unit first onto the cheapest shard; empty shards are dropped.

    A file's cost is its recorded seconds, else its size scaled by the median
    seconds-per-byte of recorded files (so new files rank sensibly). A module
    much costlier than a fair share is split: its big classes and slow tests
    become ``module.Class`` / ``module.Class.test`` units and the rest stays one
    ``module^Class^Class.test`` unit, so nothing (including tests never
    recorded) is dropped or run twice.
    """
    costs = costs or {}
    classes = costs.get("classes", {})
    count = max(1, shards)
    known, sizes = {}, {}
    for path in files:
        sizes[path] = max(1, path.stat().st_size)
        recorded = module_cost(classes, "tests." + path.stem)
        if recorded is not None:
            known[path] = recorded
    rates = sorted(known[p] / sizes[p] for p in known)
    rate = rates[len(rates) // 2] if rates else 1.0 / 1024  # about a second per KB when nothing is recorded
    weight = {p: known.get(p, sizes[p] * rate) for p in files}
    target = sum(weight.values()) / count
    units: list[tuple[float, str]] = []
    for path in files:
        module = "tests." + path.stem
        total = weight[path]
        peeled: list[tuple[float, str, str]] = []
        if path in known and count > 1 and total > 0.75 * target:
            source = path.read_text(encoding="utf-8", errors="replace")
            remainder = total
            for seconds, name, skip in peel_candidates(module, source, costs):
                if remainder <= 0.5 * target:
                    break
                if seconds < SPLIT_MIN_SHARE * target:
                    continue
                peeled.append((seconds, name, skip))
                remainder -= seconds
        if peeled:
            units.extend((seconds, name) for seconds, name, _ in peeled)
            units.append((max(total - sum(item[0] for item in peeled), 0.0),
                          module + "".join("^" + skip for _, _, skip in peeled)))
        else:
            units.append((total, module))
    buckets: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for seconds, name in sorted(units, key=lambda item: (-item[0], item[1])):
        index = loads.index(min(loads))
        buckets[index].append(name)
        loads[index] += seconds
    return [bucket for bucket in buckets if bucket]


def record_costs(path: Path, shard_files: list[Path]) -> None:
    """Merge per-shard timings into the cost file, replacing the modules they cover.

    A module split across shards reports a part from each, so the parts add up.
    """
    classes: dict[str, float] = {}
    tests: dict[str, float] = {}
    for item in shard_files:
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
            for key, value in data["classes"].items():
                classes[key] = classes.get(key, 0.0) + float(value)
            for key, value in data.get("tests", {}).items():
                tests[key] = tests.get(key, 0.0) + float(value)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    if not classes:
        return
    old = load_costs(path)
    touched = {key.rsplit(".", 1)[0] for key in classes}
    kept_classes = {k: v for k, v in old["classes"].items() if k.rsplit(".", 1)[0] not in touched}
    kept_tests = {k: v for k, v in old["tests"].items() if k.rsplit(".", 2)[0] not in touched}
    kept_classes.update({k: round(v, 3) for k, v in classes.items()})
    kept_tests.update({k: round(v, 3) for k, v in tests.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": 2, "units": dict(sorted(kept_classes.items())),
                                "slow_tests": dict(sorted(kept_tests.items()))}, indent=1), encoding="utf-8")
    os.replace(temp, path)


def select(tests_dir: Path, only: str) -> list[Path]:
    """Files matching a comma-separated list of globs or module names."""
    found: set[Path] = set()
    for pattern in filter(None, (part.strip() for part in only.split(","))):
        pattern = pattern.removeprefix("tests.")
        if not any(ch in pattern for ch in "*?[") and not pattern.endswith(".py"):
            pattern += ".py"
        # A bare glob like "test_dashboard*" also matches .cjs fixtures; only .py files are tests.
        found.update(path for path in tests_dir.glob(pattern) if path.suffix == ".py")
    return sorted(found)


def summarize(text: str) -> dict:
    """Test count, final status line and FAIL/ERROR headings from one log."""
    ran = re.findall(r"^Ran (\d+) tests? in ([\d.]+)s", text, re.M)
    status = re.findall(r"^(OK.*|FAILED.*)$", text, re.M)
    return {"tests": int(ran[-1][0]) if ran else 0, "seconds": float(ran[-1][1]) if ran else None,
            "status": status[-1] if status else None,
            "problems": re.findall(r"^(?:FAIL|ERROR): .*$", text, re.M)}


def stop_children(processes, timeout: float = 2.0) -> None:
    """Best-effort bounded cleanup of only this runner's Popen children.

    Signal all children before waiting, so the grace period is shared rather
    than multiplied by the shard count. Cleanup diagnostics must not replace
    the exception that made the run stop.
    """
    pending = []
    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
            pending.append(process)
        except BaseException as error:
            print(f"child cleanup: terminate failed: {error!r}", file=sys.stderr)
            pending.append(process)
    deadline = time.monotonic() + timeout
    survivors = []
    for process in pending:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException as error:
            if not isinstance(error, subprocess.TimeoutExpired):
                print(f"child cleanup: wait failed: {error!r}", file=sys.stderr)
            survivors.append(process)
    for process in survivors:
        try:
            if process.poll() is None:
                process.kill()
        except BaseException as error:
            print(f"child cleanup: kill failed: {error!r}", file=sys.stderr)
    deadline = time.monotonic() + timeout
    for process in survivors:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except BaseException as error:
            print(f"child cleanup: reap failed: {error!r}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("shards", nargs="?", type=int, default=None,
                        help="shard count (default: CPU count, at most 16)")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="checkout to test (default: this one)")
    parser.add_argument("--only", default="test_*.py", help="comma-separated test file globs or module names inside tests/")
    parser.add_argument("--profile", action="store_true", help="also write per-test seconds to <logs>/shardN_tests.json")
    parser.add_argument("--logs", help="log directory (default: <root>/.eutonos-local/stress/shards)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    files = select(root / "tests", args.only)
    if not files:
        print(f"no test files match {args.only} under {root / 'tests'}")
        return 2
    logs = Path(args.logs) if args.logs else root / ".eutonos-local" / "stress" / "shards"
    logs.mkdir(parents=True, exist_ok=True)
    costs_path = root / ".eutonos-local" / "stress" / COSTS_NAME
    shard_count = args.shards if args.shards is not None else default_shards()
    with tempfile.TemporaryDirectory(prefix="eutonos-test-user-") as user_home:
        # CROSSTALK_P0_FOCUS=off: no test may draw the real focus overlay or move windows on the owner's screen.
        env = {**os.environ, "EUTONOS_HOME": user_home, "EUTONOS_DOCS_WATCH_DISABLED": "1",
               "CROSSTALK_P0_FOCUS": "off",
               "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")])}
        extra = node_dir(env)
        if extra:
            env["PATH"] = extra + os.pathsep + env.get("PATH", "")
            print(f"node: using {extra}")
        elif not shutil.which("node", path=env.get("PATH")):
            print("node: not found; dashboard JS tests will be skipped")
        start = time.monotonic()
        running = []
        opened_logs = []
        cost_files: list[Path] = []
        try:
            for index, modules in enumerate(plan(files, shard_count, load_costs(costs_path))):
                log_path = logs / f"shard{index}.txt"
                cost_files.append(logs / f"shard{index}_costs.json")
                log = open(log_path, "w", encoding="utf-8")
                opened_logs.append(log)
                process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve().with_name("timed_unittest.py")),
                                            "--costs-out", str(cost_files[-1]),
                                            *(["--tests-out", str(logs / f"shard{index}_tests.json")] if args.profile else []), *modules], cwd=root, env=env,
                                           stdout=log, stderr=subprocess.STDOUT)
                running.append((index, log, log_path, process))
            total, problems, failed = 0, [], False
            for index, log, log_path, process in running:
                code = process.wait()
                log.close()
                result = summarize(log_path.read_text(encoding="utf-8", errors="replace"))
                total += result["tests"]
                problems += result["problems"]
                failed = failed or code != 0
                print(f"shard{index}: exit={code} tests={result['tests']} {result['status'] or '?'}")
            try:
                record_costs(costs_path, cost_files)
            except OSError as error:
                print(f"cost record: not written: {error!r}", file=sys.stderr)
            print(f"total tests={total} wall={int(time.monotonic() - start)}s shards={len(running)} logs={logs}")
            for line in problems:
                print(line)
            return 1 if failed or problems else 0
        except BaseException:
            stop_children(process for _, _, _, process in running)
            raise
        finally:
            for log in opened_logs:
                try:
                    log.close()
                except BaseException as error:
                    print(f"log cleanup: close failed: {error!r}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
