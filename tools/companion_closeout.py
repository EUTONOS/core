"""Bounded isolated companion closeout helper.

This helper is intentionally limited to marked benchmark fixture roots. It delegates
RAM and host effects to the installed EUTONOS CLIs and never edits a coordinator DB.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


MARKER = ".companion-closeout-fixture.json"
WORK_ID = "COMPANION-SMOKE"
IDENTITY = {"work_id": WORK_ID, "session_id": "smoke-session", "issuer_id": "fixture-recorder", "memory_id": "closeout"}


def _eutonos_home() -> Path:
    """Parity copy of eutonos.home.home() (IPC5, tests/test_home.py): EUTONOS_HOME (absolute) wins, then the runtime
    this Python lives in (<H>/runtimes/eutonos-*), then ~/.eutonos."""
    given = os.environ.get("EUTONOS_HOME")
    if given:
        if not Path(given).is_absolute():
            raise ValueError(f"EUTONOS_HOME must be an absolute path, not {given!r}.")
        return Path(given)
    try:
        prefix = Path(sys.prefix).resolve()
    except OSError:
        prefix = None
    if prefix is not None and prefix.parent.name == "runtimes" and prefix.name.startswith("eutonos-"):
        return prefix.parent.parent
    return Path.home() / ".eutonos"


def runtime_python() -> Path:
    configured = os.environ.get("EUTONOS_PYTHON")
    candidates = [Path(configured)] if configured else []
    candidates.extend([_eutonos_home() / "runtime" / "Scripts" / "python.exe", Path(sys.executable)])
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError("installed EUTONOS runtime missing")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_fixture(root: Path) -> None:
    marker = root / MARKER
    if not marker.is_file():
        raise SystemExit("refusing unmarked root; run init-fixture first")
    meta = json.loads(marker.read_text(encoding="utf-8"))
    if meta.get("profile") != "eutonos.companion-closeout-fixture.v1":
        raise SystemExit("unsupported fixture marker")
    expected_host = root.parent / (root.name + "-host")
    if Path(meta["host"]).resolve() != expected_host or expected_host.exists():
        raise SystemExit("fixture host differs or already exists; preserve it and inspect recovery")
    args = ["git", "-c", f"safe.directory={root}"]
    if subprocess.check_output(args + ["rev-list", "--count", "HEAD"], cwd=root, text=True).strip() != "1":
        raise SystemExit("closeout requires the initialized fixture baseline")
    for extra in ([], ["--cached"]):
        if subprocess.run(args + ["diff", "--quiet", *extra], cwd=root).returncode:
            raise SystemExit("fixture baseline has tracked changes; no closeout writes made")


def project(root: Path, command: str, *args: str) -> dict:
    runtime = runtime_python()
    modules = {"host": "host_cli", "ram": "ram_cli"}
    cmd = [str(runtime), "-m", "eutonos." + modules[command], *args, "--root", str(root)]
    proc = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or proc.stderr.strip() or f"{command} failed")
    return json.loads(proc.stdout)


def init_fixture(root: Path) -> dict:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit("fixture root must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir()
    (root / "RAM" / WORK_ID / "smoke-session").mkdir(parents=True)
    task = {"profile": "tokenslang.work.v1", "work_id": WORK_ID, "goal": "Run an isolated companion closeout smoke", "status": "selected", "required": [], "optional": []}
    (root / "docs" / "CURRENT_TASK.md").write_text("# Isolated companion closeout\n\n```tokenslang-work\n" + json.dumps(task) + "\n```\n", encoding="utf-8")
    write_json(root / "eutonos.ram.json", {"profile": "eutonos.tokenslang.ram-config.v1", "repository_id": "companion.smoke", "scope_id": "companion.smoke", "namespace": "companion.smoke", "store_root": "RAM", "current_task": "docs/CURRENT_TASK.md", "work_owners": {WORK_ID: "docs/CURRENT_TASK.md"}})
    write_json(root / "eutonos.read.json", {"profile": "eutonos.tokenslang.read-catalog.v1", "repository_id": "companion.smoke", "scope_id": "companion.smoke", "namespace": "companion.smoke", "owners": {"current_task": {"scope_id": "companion.smoke", "namespace": "companion.smoke", "resource_id": "companion.task"}}, "bindings": [], "excluded_roots": [], "source_paths": ["docs/CURRENT_TASK.md"]})
    host = root.parent / (root.name + "-host")
    write_json(root / MARKER, {"profile": "eutonos.companion-closeout-fixture.v1", "work_id": WORK_ID, "identity": IDENTITY, "host": str(host), "baseline_required": True})
    subprocess.run(["git", "-c", f"safe.directory={root}", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "-c", f"safe.directory={root}", "-c", "user.name=EUTONOS fixture", "-c", "user.email=fixture@example.invalid", "add", "docs", "eutonos.ram.json", "eutonos.read.json", MARKER], cwd=root, check=True)
    subprocess.run(["git", "-c", f"safe.directory={root}", "-c", "user.name=EUTONOS fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture baseline"], cwd=root, check=True)
    return {"outcome": "initialized", "root": str(root), "host": str(host), "work_id": WORK_ID, "baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()}


def closeout(root: Path, request_path: Path) -> dict:
    root = root.resolve()
    ensure_fixture(root)
    req = json.loads(request_path.read_text(encoding="utf-8"))
    for key in ("summary", "result", "evidence"):
        if key not in req:
            raise SystemExit(f"request missing {key}")
    if not isinstance(req["summary"], str) or not req["summary"].strip():
        raise SystemExit("summary must be nonempty text")
    if not isinstance(req["evidence"], list) or not req["evidence"] or not all(isinstance(x, str) and x.strip() for x in req["evidence"]):
        raise SystemExit("evidence must be a nonempty list of source descriptions")
    if not isinstance(req.get("identity", {}), dict):
        raise SystemExit("identity must be an object")
    identity = {**IDENTITY, **req.get("identity", {})}
    if identity["work_id"] != WORK_ID or identity["session_id"] != IDENTITY["session_id"]:
        raise SystemExit("identity must use this fixture's work and session")
    actor = req.get("actor", "eup-local-agent")
    host = Path(json.loads((root / MARKER).read_text(encoding="utf-8"))["host"])
    with tempfile.TemporaryDirectory(prefix="companion-closeout-") as temp:
        temp_path = Path(temp)
        task_path = root / "docs" / "CURRENT_TASK.md"
        result_body = req["result"] if isinstance(req["result"], str) else "```json\n" + json.dumps(req["result"], sort_keys=True, indent=2) + "\n```"
        result_text = "# Companion closeout result\n\n" + result_body + "\n\nEvidence:\n\n" + "\n".join("- " + x for x in req["evidence"]) + "\n"
        task_text = task_path.read_text(encoding="utf-8") + "\n## Checkpoint\n\n" + req["summary"] + "\n"
        setup_request = {"profile": "eutonos.tokenslang.setup-recipe.v1", "files": [{"path": "docs/CURRENT_TASK.md", "content": task_text, "before": digest(task_path)}, {"path": "result.md", "content": result_text, "before": None}]}
        setup_file = temp_path / "setup-request.json"; write_json(setup_file, setup_request)
        runtime = runtime_python()
        def run_module(module, *args):
            proc = subprocess.run([str(runtime), "-m", "eutonos." + module, *args], cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
            if proc.returncode: raise RuntimeError(proc.stdout.strip() or proc.stderr.strip() or module + " failed")
            return json.loads(proc.stdout)
        setup_plan = run_module("setup_cli", "plan", "--root", str(root), "--request", str(setup_file))
        setup_plan_file = temp_path / "setup-plan.json"; write_json(setup_plan_file, setup_plan)
        setup_paths = setup_plan["paths"]
        project(root, "host", "enroll", "--host", str(host), "--actor", actor, "--hours", "1", *sum((["--path", p] for p in setup_paths), []))
        attempt = "companion-closeout-smoke"; started = project(root, "host", "start", "--host", str(host), "--actor", actor, "--attempt", attempt); generation = started["generation"]
        project(root, "host", "reserve", "--host", str(host), "--actor", actor, "--attempt", attempt, "--generation", str(generation), *sum((["--path", p] for p in setup_paths), []))
        effect_id = "setup-companion-closeout"
        applied_docs = run_module("setup_cli", "apply", "--root", str(root), "--host", str(host), "--actor", actor, "--attempt", attempt, "--generation", str(generation), "--request", str(setup_plan_file), "--fingerprint", setup_plan["fingerprint"], "--effect-id", effect_id)
        subprocess.run(["git", "-c", f"safe.directory={root}", "-c", "user.name=EUTONOS fixture", "-c", "user.email=fixture@example.invalid", "add", "docs/CURRENT_TASK.md", "result.md"], cwd=root, check=True)
        subprocess.run(["git", "-c", f"safe.directory={root}", "-c", "user.name=EUTONOS fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture closeout evidence"], cwd=root, check=True)
        at = req.get("at", now_iso())
        ram_request = {"profile": "eutonos.tokenslang.ram-write.v1", "operation_id": req.get("operation_id", "companion-closeout"), "operation": "capture", "identity": identity, "expected_revision": None, "at": at, "reason": "Isolated companion closeout", "experience": {"kind": "summary", "summary": req["summary"], "recorded_at": at, "occurred_at": at, "unfinished": False, "actors": [{"id": identity["issuer_id"], "attribution": req.get("attribution", "self-reported")}], "work_path": "docs/CURRENT_TASK.md", "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(), "sources": [{"path": "docs/CURRENT_TASK.md", "evidence": "inspected"}, {"path": "result.md", "evidence": "inspected"}], "children": [], "skill": None}}
        ram_file = temp_path / "ram-request.json"; write_json(ram_file, ram_request)
        plan = run_module("ram_cli", "plan", "--root", str(root), "--actor", actor, "--request", str(ram_file)); plan_file = temp_path / "ram-plan.json"; write_json(plan_file, plan)
        paths = [c["path"] for c in plan["effect"]["changes"]]
        new_paths = [p for p in paths if p not in setup_paths]
        if new_paths:
            project(root, "host", "renew", "--host", str(host), "--actor", actor, "--hours", "1", *sum((["--path", p] for p in new_paths), []))
            project(root, "host", "reserve", "--host", str(host), "--actor", actor, "--attempt", attempt, "--generation", str(generation), *sum((["--path", p] for p in new_paths), []))
        applied_ram = project(root, "ram", "apply", "--host", str(host), "--actor", actor, "--attempt", attempt, "--generation", str(generation), "--fingerprint", plan["fingerprint"], "--request", str(plan_file))
        read_budget = max(32768, (root / plan["record_path"]).stat().st_size * 2 + 8192)
        read_request = temp_path / "ram-read.json"; write_json(read_request, {"profile": "eutonos.tokenslang.ram-read.v1", "operation": "get", "identity": identity, "revision": 1, "max_bytes": read_budget}); readback = project(root, "ram", "read", "--request", str(read_request))
        if readback.get("outcome") == "overflow":
            raise RuntimeError("RAM readback overflow; closeout did not claim success")
        if not readback.get("items"):
            raise RuntimeError("RAM readback returned no record; closeout did not claim success")
        record = readback["items"][0]
        if record["record_revision"] != 1 or record["record_sha256"] != digest(root / plan["record_path"]):
            raise RuntimeError("RAM fresh readback differs from the written record")
        expected = {p: digest(root / p) for p in setup_paths + paths}; close_request = temp_path / "host-closeout.json"; write_json(close_request, {"expected": expected, "checkpoint": {"completed": "Applied guarded docs and RAM closeout and read it back.", "next": "No next action.", "pending": None}})
        closed = project(root, "host", "closeout", "--host", str(host), "--actor", actor, "--attempt", attempt, "--generation", str(generation), "--request", str(close_request))
        return {"outcome": "closed", "attempt": attempt, "record_path": plan["record_path"], "revision": record["record_revision"], "record_sha256": record["record_sha256"], "docs_apply": applied_docs, "ram_apply": applied_ram, "closeout": closed}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-fixture"); init.add_argument("--root", type=Path, required=True)
    close = sub.add_parser("closeout"); close.add_argument("--root", type=Path, required=True); close.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "init-fixture": result = init_fixture(args.root)
    else: result = closeout(args.root, args.request)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
