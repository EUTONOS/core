"""Local runtime persistence and diagnostics, independent of desktop control."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from simon_common import append_log, ensure_runtime_dir, now_iso, now_stamp, read_json, write_json_atomic
from simon_protocol import make_request_id, safe_request_id
from simon_paths import (
    ARTIFACT_MANIFEST_FILE,
    COMMAND_SCREENSHOT_DIR,
    HELPER_BRIDGE_STATUS_FILE,
    HELPER_BRIDGE_HISTORY_FILE,
    HELPER_RUN_JOURNAL_FILE,
    HELPER_CAPTURE_ROUTE_HISTORY_FILE,
    HELPER_CAPTURE_ROUTE_TALLY_FILE,
    COMMAND_LOG_FILE,
    COMMAND_REPORT_DIR,
    PLANNER_REQUEST_DIR,
    FAILURE_DIAGNOSTICS_DIR,
    PLANNER_MEMORY_FILE,
)

_ARTIFACT_LOCK = threading.RLock()


_NEW_PLANNER_REQUESTS_LOCAL = threading.local()


@contextmanager
def artifact_manifest_lock():
    """Serialize manifest read/modify/write across CLI and resident processes."""
    with _ARTIFACT_LOCK:
        ARTIFACT_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ARTIFACT_MANIFEST_FILE.with_suffix(".lock").open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 5.0
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def track_generated_artifacts():
    previous = getattr(_NEW_PLANNER_REQUESTS_LOCAL, "paths", None)
    paths: list[Path] = []
    _NEW_PLANNER_REQUESTS_LOCAL.paths = paths
    try:
        yield paths
    finally:
        _NEW_PLANNER_REQUESTS_LOCAL.paths = previous


def record_generated_artifact(path: Path) -> None:
    paths = getattr(_NEW_PLANNER_REQUESTS_LOCAL, "paths", None)
    if isinstance(paths, list):
        paths.append(path)


def write_bridge_status(state: str, data: dict[str, Any] | None = None) -> None:
    payload = {
        "schema": "helper-bridge-status.v1",
        "state": state,
        "updated_at": now_iso(),
        "updated_epoch": time.time(),
        "data": data or {},
    }
    write_json_atomic(HELPER_BRIDGE_STATUS_FILE, payload)


def append_bridge_history(event: str, request_id: str, command: str, data: dict[str, Any] | None = None) -> None:
    ensure_runtime_dir()
    entry = {
        "event": event,
        "request_id": request_id,
        "command": command,
        "timestamp": now_iso(),
        "epoch": time.time(),
        "data": data or {},
    }
    with HELPER_BRIDGE_HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def append_run_journal(event: str, request_id: str, command: str, role: str, data: dict[str, Any] | None = None) -> None:
    ensure_runtime_dir()
    entry = {
        "schema": "helper-run-journal.v1",
        "event": event,
        "request_id": request_id,
        "command": command,
        "role": role,
        "timestamp": now_iso(),
        "epoch": time.time(),
        "data": data or {},
    }
    with HELPER_RUN_JOURNAL_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def read_bridge_history_tail(limit: int = 10) -> list[dict[str, Any]]:
    try:
        lines = HELPER_BRIDGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    history: list[dict[str, Any]] = []
    for line in lines:
        try:
            history.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return history


def read_lock_file_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    try:
        stat = path.stat()
    except OSError:
        return {"exists": True, "error": "stat_failed"}
    payload = read_json(path)
    return {
        "exists": True,
        "path": str(path),
        "age_seconds": round(time.time() - stat.st_mtime, 3),
        "content": payload,
    }


def record_capture_route(
    method: str,
    target: str,
    status: str,
    started: float,
    attempts: list[dict[str, Any]] | None = None,
) -> None:
    method = method or "unknown"
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
    event = {
        "timestamp": now_iso(),
        "epoch": time.time(),
        "method": method,
        "target": target,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "attempts": attempts or [],
    }
    try:
        ensure_runtime_dir()
        with HELPER_CAPTURE_ROUTE_HISTORY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        tally = read_json(HELPER_CAPTURE_ROUTE_TALLY_FILE) or {}
        totals = tally.get("totals") if isinstance(tally.get("totals"), dict) else {}
        by_method = tally.get("by_method") if isinstance(tally.get("by_method"), dict) else {}
        by_target = tally.get("by_target") if isinstance(tally.get("by_target"), dict) else {}
        totals["captures"] = int(totals.get("captures") or 0) + 1
        totals[status.lower()] = int(totals.get(status.lower()) or 0) + 1
        method_row = by_method.get(method) if isinstance(by_method.get(method), dict) else {}
        method_row["count"] = int(method_row.get("count") or 0) + 1
        method_row[status.lower()] = int(method_row.get(status.lower()) or 0) + 1
        method_row["last_elapsed_ms"] = elapsed_ms
        method_row["last_target"] = target
        by_method[method] = method_row
        target_row = by_target.get(target) if isinstance(by_target.get(target), dict) else {}
        target_row["count"] = int(target_row.get("count") or 0) + 1
        target_row["last_method"] = method
        by_target[target] = target_row
        write_json_atomic(
            HELPER_CAPTURE_ROUTE_TALLY_FILE,
            {
                "schema": "capture-route-tally.v1",
                "updated_at": now_iso(),
                "totals": totals,
                "by_method": by_method,
                "by_target": by_target,
            },
        )
    except OSError:
        pass


def load_planner_working_memory() -> dict[str, Any]:
    data = read_json(PLANNER_MEMORY_FILE)
    return data if isinstance(data, dict) else {}


def save_planner_working_memory(memory: dict[str, Any]) -> None:
    try:
        write_json_atomic(PLANNER_MEMORY_FILE, memory)
    except OSError:
        # Working memory is a speed hint, not an execution authority. If two
        # command probes race on the temp file, keep the command result alive.
        fallback = PLANNER_MEMORY_FILE.with_name(f"{PLANNER_MEMORY_FILE.stem}_{os.getpid()}_{int(time.time() * 1000)}.json")
        try:
            fallback.write_text(json.dumps(memory, indent=2), encoding="utf-8")
        except OSError:
            pass


def planner_request_path(request_id: str) -> Path:
    return PLANNER_REQUEST_DIR / f"{safe_request_id(request_id)}_planner_request.json"


def write_planner_request(request: dict[str, Any]) -> None:
    write_json_atomic(planner_request_path(str(request["request_id"])), request)
    record_generated_artifact(planner_request_path(str(request["request_id"])))
    record_new_planner_request(str(request["request_id"]))


@contextmanager
def track_new_planner_requests():
    """Collect planner request ids written during one dispatched command.

    Working memory takes last_observation_request_id only from these new
    observations, never from request ids echoed back from args or from old
    planner requests that a stale-screen result includes.
    """
    previous = getattr(_NEW_PLANNER_REQUESTS_LOCAL, "ids", None)
    ids: list[str] = []
    _NEW_PLANNER_REQUESTS_LOCAL.ids = ids
    try:
        yield ids
    finally:
        _NEW_PLANNER_REQUESTS_LOCAL.ids = previous


def record_new_planner_request(request_id: str) -> None:
    ids = getattr(_NEW_PLANNER_REQUESTS_LOCAL, "ids", None)
    if isinstance(ids, list) and request_id:
        ids.append(str(request_id))


def load_planner_request(request_id: str) -> dict[str, Any] | None:
    return read_json(planner_request_path(request_id))


def write_guided_action_report(request_id: str, data: dict[str, Any]) -> Path:
    COMMAND_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = COMMAND_REPORT_DIR / f"{safe_request_id(request_id)}_guided_grid_click_report.json"
    report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    record_generated_artifact(report_path)
    return report_path


def write_planner_step_report(request_id: str, data: dict[str, Any]) -> Path:
    COMMAND_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = COMMAND_REPORT_DIR / f"{safe_request_id(request_id)}_planner_step_report.json"
    report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    record_generated_artifact(report_path)
    return report_path


def write_command_log(result: dict[str, Any]) -> None:
    line = f"{now_stamp()} | {result['status']} | {result['command']} | {result['message']}"
    append_log(COMMAND_LOG_FILE, line)


def write_command_report(result: dict[str, Any]) -> Path:
    COMMAND_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = COMMAND_REPORT_DIR / f"{make_request_id()}_{result['command'].replace('.', '_')}.json"
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return report_path


def collect_artifact_paths(value: Any) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_path") or key.endswith("_file"):
                if isinstance(item, str) and item.strip():
                    paths.append(Path(item))
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(collect_artifact_paths(item))
    return paths


def write_command_failure_bundle(result: dict[str, Any]) -> Path:
    FAILURE_DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    request_id = make_request_id()
    bundle_dir = FAILURE_DIAGNOSTICS_DIR / f"{safe_request_id(request_id)}-{result['command'].replace('.', '_')}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    copied_artifacts: list[str] = []
    seen: set[str] = set()
    for path in collect_artifact_paths(result):
        path_text = str(path)
        if path_text in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path_text)
        destination = bundle_dir / path.name
        try:
            shutil.copy2(path, destination)
            copied_artifacts.append(str(destination))
        except OSError:
            continue
    bundle_payload = {
        "timestamp": now_iso(),
        "command": result.get("command"),
        "status": result.get("status"),
        "message": result.get("message"),
        "copied_artifacts": copied_artifacts,
        "result": result,
    }
    bundle_path = bundle_dir / "failure_bundle.json"
    bundle_path.write_text(json.dumps(bundle_payload, indent=2), encoding="utf-8")
    return bundle_path


def register_result_artifacts(result: dict[str, Any], details_path: Path, generated_paths: list[Path] | None = None) -> None:
    """Track only generated evidence under Simon's owned artifact directories."""
    roots = [p.resolve() for p in (COMMAND_REPORT_DIR, COMMAND_SCREENSHOT_DIR, PLANNER_REQUEST_DIR, FAILURE_DIAGNOSTICS_DIR)]
    # References are dependencies, not proof that Simon created a file. Only
    # files emitted by this command or already owned by the manifest may enter.
    # Keep dependency lookup and registration atomic with respect to cleanup.
    with artifact_manifest_lock():
        existing = read_json(ARTIFACT_MANIFEST_FILE) or {"groups": []}
        registered = {str(Path(name).resolve()) for row in existing.get("groups", []) for name in row.get("paths", [])}
        candidates = [path for path in collect_artifact_paths(result) if str(path.resolve()) in registered]
        candidates += list(generated_paths or []) + [Path(details_path)]
        if result.get("failure_bundle"):
            bundle = Path(result["failure_bundle"])
            candidates.append(bundle)
            candidates.extend(Path(p) for p in (read_json(bundle) or {}).get("copied_artifacts", []))
        paths = []
        for path in candidates:
            resolved = path.resolve()
            if any(resolved.is_relative_to(root) for root in roots) and path.is_file():
                paths.append(str(resolved))
        # Planner image uses a generic 'path' field, not a screenshot-path alias.
        image = (result.get("data") or {}).get("planner_image") or {}
        generated = {str(Path(path).resolve()) for path in generated_paths or []}
        for item in [image] + image.get("extra_images", []):
            if item.get("path"):
                path = Path(item["path"]).resolve()
                owned = item is image or str(path) in generated or str(path) in registered
                if owned and any(path.is_relative_to(root) for root in roots) and path.is_file():
                    paths.append(str(path))
        manifest = existing
        manifest.setdefault("schema", "simon-artifacts.v1")
        def request_ids(value):
            if isinstance(value, dict):
                if value.get("request_id") and value.get("planner_request_path"):
                    yield str(value["request_id"])
                for item in value.values():
                    yield from request_ids(item)
            elif isinstance(value, list):
                for item in value:
                    yield from request_ids(item)

        manifest["groups"].append({"created_epoch": time.time(), "paths": sorted(set(paths)),
                                   "request_id": (result.get("data") or {}).get("request_id"),
                                   "observation_request_ids": sorted(set(request_ids(result.get("data", {}))))})
        write_json_atomic(ARTIFACT_MANIFEST_FILE, manifest)


def cleanup_runtime(*, dry_run: bool = True, max_age_days: float = 7,
                    max_groups: int = 100, max_bytes: int = 256 * 1024 * 1024) -> dict[str, Any]:
    """Retention never scans/deletes arbitrary runtime or external material."""
    roots = [p.resolve() for p in (COMMAND_REPORT_DIR, COMMAND_SCREENSHOT_DIR, PLANNER_REQUEST_DIR, FAILURE_DIAGNOSTICS_DIR)]
    now = time.time()
    protected_id = load_planner_working_memory().get("last_observation_request_id")
    with artifact_manifest_lock():
        manifest = read_json(ARTIFACT_MANIFEST_FILE) or {"schema": "simon-artifacts.v1", "groups": []}
        groups = manifest.get("groups", [])
        valid_paths = {}
        for group in groups:
            for name in group.get("paths", []):
                path = Path(name)
                resolved = path.resolve()
                if any(resolved.is_relative_to(root) for root in roots) and path.is_file() and not path.is_symlink():
                    valid_paths[name] = path.stat().st_size
        survivors = list(groups)
        total = sum(valid_paths.values())
        removed = []
        for group in sorted(groups, key=lambda g: g.get("created_epoch", 0)):
            age = now - group.get("created_epoch", now)
            if age < 300 or (protected_id and (group.get("request_id") == protected_id
                                              or protected_id in group.get("observation_request_ids", []))):
                continue
            if age > max_age_days * 86400 or len(survivors) > max_groups or total > max_bytes:
                survivors.remove(group)
                removed.append(group)
                remaining = {name for row in survivors for name in row.get("paths", [])}
                total = sum(size for name, size in valid_paths.items() if name in remaining)
        keep = {name for row in survivors for name in row.get("paths", [])}
        delete = sorted({name for row in removed for name in row.get("paths", []) if name in valid_paths and name not in keep})
        deleted, errors = [], []
        if not dry_run:
            for name in delete:
                try:
                    Path(name).unlink()
                    deleted.append(name)
                except OSError as exc:
                    errors.append({"path": name, "error": str(exc)})
            # Keep failed deletions registered so a later cleanup can retry.
            if errors:
                survivors.extend({**row, "paths": [p for p in row["paths"] if p not in deleted]} for row in removed)
            manifest["groups"] = survivors
            write_json_atomic(ARTIFACT_MANIFEST_FILE, manifest)
        return {"dry_run": dry_run, "eligible_files": len(delete), "deleted_files": len(deleted),
                "eligible_bytes": sum(valid_paths[p] for p in delete), "retained_groups": len(survivors),
                "retained_bytes": total, "errors": errors}
