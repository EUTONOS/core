"""Construct and validate EMC fixtures without dispatching a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


PROFILE = "eutonos.model-companion-workflow-suite.v1"
PLAN_PROFILE = "eutonos.model-companion-workflow-plan.v1"
FIXTURE_PROFILE = "eutonos.model-companion-fixture.v1"
SEED_PROFILE = "eutonos.model-companion-seed.v1"
RAM_PROFILE = "eutonos.model-companion-ram-bank.v1"
STATE_PROFILE = "eutonos.model-companion-arm-state.v1"
FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".txt", ".css", ".html", ".js", ".cjs"}


def read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    value = path.read_bytes()
    if path.suffix.casefold() in TEXT_SUFFIXES:
        value = value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sha256_bytes(value)


def source_sha256(value: bytes, path: str | Path) -> str:
    """Hash bytes as stored under the repository's explicit LF attributes."""
    if PurePosixPath(str(path).replace("\\", "/")).suffix.casefold() in TEXT_SUFFIXES:
        value = value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return sha256_bytes(value)


def _run_git(
    root: Path, *args: str, input_bytes: bytes | None = None, check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(root), *args], input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, env=environment,
    )


def git_commit(root: Path, revision: str) -> str:
    if not FULL_REVISION.fullmatch(revision):
        raise ValueError("revision must be a full lowercase 40-character Git object ID")
    result = _run_git(root, "rev-parse", "--verify", revision + "^{commit}", check=False)
    if result.returncode:
        raise ValueError("revision is not an available Git commit")
    commit = result.stdout.decode("ascii").strip()
    if commit != revision:
        raise ValueError("revision must name the exact commit")
    return commit


def git_bytes(root: Path, revision: str, path: str) -> bytes:
    safe = PurePosixPath(path)
    if safe.is_absolute() or ".." in safe.parts or not safe.parts:
        raise ValueError(f"unsafe repository path: {path}")
    result = _run_git(root, "show", f"{revision}:{safe.as_posix()}", check=False)
    if result.returncode:
        raise ValueError(f"revision does not contain {safe.as_posix()}")
    return result.stdout


def tracked_tree_hash(root: Path, revision: str) -> str:
    return sha256_bytes(_run_git(root, "ls-tree", "-r", "--full-tree", revision).stdout)


def _safe_actor_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] == ".git":
        raise ValueError(f"unsafe actor-visible path: {value}")
    return path


def _source_path(root: Path, fixture_directory: Path, value: str) -> tuple[Path, str]:
    path = (fixture_directory / value).resolve()
    try:
        relative = path.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"actor source escapes the frozen repository: {value}") from exc
    return path, relative


def directory_projection(root: Path, fixture_directory: Path, row: dict) -> tuple[str, list[dict]]:
    source, relative = _source_path(root, fixture_directory, row["source"])
    if not source.is_dir():
        raise ValueError(f"actor-visible directory is missing: {relative}")
    target = _safe_actor_path(row["target"])
    tracked = _run_git(root, "ls-files", "-z", "--", relative).stdout.split(b"\0")
    rows = []
    prefix = PurePosixPath(relative)
    for encoded in sorted(value for value in tracked if value):
        repository_path = PurePosixPath(encoded.decode("utf-8", "surrogateescape"))
        child = repository_path.relative_to(prefix)
        path = root / repository_path
        rows.append({"source": repository_path.as_posix(), "target": (target / child).as_posix(),
                     "sha256": source_sha256(path.read_bytes(), repository_path.as_posix())})
    return sha256_bytes(canonical_bytes(rows)), rows


def _git_object_id(kind: str, value: bytes) -> str:
    header = f"{kind} {len(value)}\0".encode("ascii")
    return hashlib.sha1(header + value).hexdigest()


def _actor_tree_id(entries: dict[str, bytes]) -> str:
    root: dict[str, object] = {}
    for path, value in entries.items():
        current = root
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            current = current.setdefault(part, {})  # type: ignore[assignment]
        current[parts[-1]] = value

    def emit(node: dict[str, object]) -> str:
        rows = []
        for name, value in sorted(node.items(), key=lambda pair: (pair[0] + ("/" if isinstance(pair[1], dict) else "")).encode("utf-8")):
            if isinstance(value, dict):
                mode, object_id = "40000", emit(value)
            else:
                mode, object_id = "100644", _git_object_id("blob", value)
            rows.append(mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(object_id))
        return _git_object_id("tree", b"".join(rows))

    return emit(root)


def _actor_commit_message(test_id: str, revision: str, seed_sha256: str, projection_sha256: str) -> bytes:
    return (f"EMC actor seed {test_id}\n\nsource-suite {revision}\nseed-manifest {seed_sha256}\n"
            f"projection {projection_sha256}\n").encode("utf-8")


def _actor_commit_content(tree_id: str, test_id: str, revision: str, seed_sha256: str, projection_sha256: str) -> bytes:
    message = _actor_commit_message(test_id, revision, seed_sha256, projection_sha256).decode("utf-8")
    return (f"tree {tree_id}\n"
            "author EUTONOS EMC <emc-fixture@example.invalid> 946684800 +0000\n"
            "committer EUTONOS EMC <emc-fixture@example.invalid> 946684800 +0000\n\n"
            + message).encode("utf-8")


def actor_projection(root: Path, revision: str, item: dict) -> dict:
    fixture_directory = root / item["fixture"]
    fixture = read_manifest(fixture_directory / "fixture.json")
    seed = read_manifest(fixture_directory / fixture["seed"]["path"])
    entries: dict[str, bytes] = {}
    admitted_paths = {"ACTOR_PROMPT.md"}

    def add(target_value: str, value: bytes) -> None:
        target = _safe_actor_path(target_value).as_posix()
        if target in entries and entries[target] != value:
            raise ValueError(f"conflicting actor-visible bytes for {target}")
        entries[target] = value

    prompt_path, prompt_relative = _source_path(root, fixture_directory, fixture["actor_prompt"]["path"])
    prompt = git_bytes(root, revision, prompt_relative)
    if sha256_bytes(prompt) != fixture["actor_prompt"]["sha256"]:
        raise ValueError("frozen actor prompt differs from its fixture hash")
    add("ACTOR_PROMPT.md", prompt)
    for directory in seed.get("actor_directories", []):
        observed, rows = directory_projection(root, fixture_directory, directory)
        if observed != directory.get("sha256"):
            raise ValueError(f"actor directory hash mismatch for {directory['source']}")
        for row in rows:
            value = git_bytes(root, revision, row["source"])
            if source_sha256(value, row["source"]) != row["sha256"]:
                raise ValueError(f"revision differs for actor directory file {row['source']}")
            add(row["target"], value)
    for row in seed["actor_files"]:
        admitted_paths.add(_safe_actor_path(row["target"]).as_posix())
        _, relative = _source_path(root, fixture_directory, row["source"])
        value = git_bytes(root, revision, relative)
        if source_sha256(value, relative) != row["sha256"]:
            raise ValueError(f"revision differs for actor file {relative}")
        add(row["target"], value)
    allowed_diff_paths = [_safe_actor_path(value).as_posix() for value in seed.get("allowed_diff_paths", [])]
    if len(allowed_diff_paths) != len(set(allowed_diff_paths)):
        raise ValueError("allowed diff paths must be unique exact paths")
    missing_admission = sorted(set(allowed_diff_paths) - admitted_paths)
    if missing_admission:
        raise ValueError("every allowed diff path must be an exact host-admitted actor file: "
                         + ", ".join(missing_admission))
    rows = [{"path": path, "sha256": sha256_bytes(value)} for path, value in sorted(entries.items())]
    projection_sha256 = sha256_bytes(canonical_bytes(rows))
    tree_id = _actor_tree_id(entries)
    seed_sha256 = fixture["seed"]["sha256"]
    commit_content = _actor_commit_content(tree_id, item["id"], revision, seed_sha256, projection_sha256)
    return {"entries": entries, "paths": [row["path"] for row in rows],
            "admitted_paths": sorted(admitted_paths),
            "allowed_diff_paths": allowed_diff_paths,
            "projection_sha256": projection_sha256, "seed_sha256": seed_sha256, "tree_id": tree_id,
            "commit_id": _git_object_id("commit", commit_content), "commit_content": commit_content}


def worktree_state_hash(worktree: Path) -> str:
    """Hash tracked changes and every untracked byte for an exact handoff state."""
    payload = bytearray(_run_git(worktree, "diff", "--binary", "HEAD").stdout)
    untracked = _run_git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
    for encoded in sorted(path for path in untracked if path):
        relative = encoded.decode("utf-8", "surrogateescape")
        target = worktree / relative
        payload.extend(b"\0untracked\0" + encoded + b"\0")
        payload.extend(target.read_bytes())
    return sha256_bytes(bytes(payload))


def validate(manifest: dict) -> dict:
    errors: list[str] = []
    if manifest.get("profile") != PROFILE:
        errors.append("unsupported profile")
    if manifest.get("dispatch_authorized") is not False:
        errors.append("dispatch_authorized must be false")
    models = manifest.get("models")
    if not isinstance(models, list) or not models:
        errors.append("models must be a non-empty list")
        models = []
    model_by_short = {}
    for model in models:
        if not isinstance(model, dict):
            errors.append("each model must be an object")
            continue
        short = model.get("short")
        if not isinstance(short, str) or short in model_by_short:
            errors.append("model short names must be unique strings")
            continue
        if type(model.get("rank")) is not int or model["rank"] < 1:
            errors.append(f"model {short} needs a positive integer rank")
        model_by_short[short] = model
    shapes = manifest.get("execution_shapes")
    if not isinstance(shapes, list) or not shapes:
        errors.append("execution_shapes must be a non-empty list")
        shapes = []
    shape_ids: set[str] = set()
    for shape in shapes:
        if not isinstance(shape, dict):
            errors.append("each execution shape must be an object")
            continue
        shape_id = shape.get("id")
        primary = model_by_short.get(shape.get("primary"))
        support_name = shape.get("support")
        support = model_by_short.get(support_name) if support_name is not None else None
        if not isinstance(shape_id, str) or shape_id in shape_ids:
            errors.append("execution shape IDs must be unique strings")
        else:
            shape_ids.add(shape_id)
        if primary is None:
            errors.append(f"shape {shape_id} has an unknown primary")
        if support_name is not None and support is None:
            errors.append(f"shape {shape_id} has an unknown support model")
        if primary and support and support["rank"] >= primary["rank"]:
            errors.append(f"shape {shape_id} does not use a lower-ranked support model")
    ram_conditions = manifest.get("ram_conditions")
    if ram_conditions != ["off", "on"]:
        errors.append("ram_conditions must be exactly ['off', 'on']")
        ram_conditions = ram_conditions if isinstance(ram_conditions, list) else []
    if manifest.get("capture_during_answer") is not False:
        errors.append("capture_during_answer must be false")
    execution_policy = manifest.get("execution_policy")
    if not isinstance(execution_policy, dict):
        errors.append("execution_policy must be an object")
    else:
        if execution_policy.get("arm_concurrency") != 1:
            errors.append("arm_concurrency must be one isolated arm")
        if "counterbalanced" not in str(execution_policy.get("ram_pairing", "")):
            errors.append("RAM pairs must use the frozen counterbalanced schedule")
        if "never silently replace" not in str(execution_policy.get("rerun", "")):
            errors.append("rerun policy must retain failed and interrupted evidence")
    review_policy = manifest.get("review_policy")
    if not isinstance(review_policy, dict):
        errors.append("review_policy must be an object")
    else:
        if review_policy.get("reviewer_model") != "gpt-6-astra" or review_policy.get("reviewer_effort") != "medium":
            errors.append("semantic review must use the frozen Astra medium policy")
        if review_policy.get("reviewer_assignments") != 90:
            errors.append("semantic review must project one reviewer assignment per arm")
        if review_policy.get("dispatch_authorized") is not False:
            errors.append("review dispatch must remain unauthorized before EMC5")
    cost_policy = manifest.get("cost_policy")
    if not isinstance(cost_policy, dict) or "null" not in str(cost_policy.get("missing_values", "")):
        errors.append("cost policy must preserve missing values as unknown nulls")
    elif cost_policy.get("stages") != ["fixture_construction", "answer_production", "semantic_review", "closeout"]:
        errors.append("cost policy must keep construction, answer, review and closeout stages separate")
    tests = manifest.get("tests")
    if not isinstance(tests, list) or len(tests) != 5:
        errors.append("the selected suite must contain five tests")
        tests = tests if isinstance(tests, list) else []
    test_ids = [test.get("id") for test in tests if isinstance(test, dict)]
    if len(test_ids) != len(set(test_ids)) or not all(isinstance(value, str) for value in test_ids):
        errors.append("test IDs must be unique strings")
    if sum(test.get("length") == "short" for test in tests if isinstance(test, dict)) != 3:
        errors.append("the selected suite must contain three short tests")
    if sum(test.get("length") == "long" for test in tests if isinstance(test, dict)) != 2:
        errors.append("the selected suite must contain two long tests")
    phases_by_test = {test.get("id"): test.get("answer_phases") for test in tests if isinstance(test, dict)}
    if phases_by_test.get("EMC-T5") != 2 or any(
        phases_by_test.get(test_id) != 1 for test_id in ("EMC-T1", "EMC-T2", "EMC-T3", "EMC-T4")
    ):
        errors.append("EMC-T5 must have two answer phases and every other test exactly one")
    arm_count = len(shapes) * len(ram_conditions) * len(tests)
    worker_assignments = sum(
        test.get("answer_phases", 0) * len(ram_conditions) * sum(
            1 + (shape.get("support") is not None) for shape in shapes if isinstance(shape, dict)
        )
        for test in tests if isinstance(test, dict) and type(test.get("answer_phases")) is int
    )
    if len(shapes) != 9:
        errors.append("the selected hierarchy must contain nine execution shapes")
    if arm_count != 90:
        errors.append("the selected suite must project 90 arms")
    if worker_assignments != 180:
        errors.append("the selected suite must project 180 answer-production worker assignments")
    return {"outcome": "ok" if not errors else "invalid", "profile": PROFILE,
            "suite_id": manifest.get("suite_id"), "test_count": len(tests), "shape_count": len(shapes),
            "ram_condition_count": len(ram_conditions), "arm_count": arm_count,
            "worker_assignments": worker_assignments, "errors": errors, "dispatch_authorized": False}


def validate_materialized(root: Path, manifest: dict, revision: str | None = None) -> dict:
    errors: list[str] = []
    rows = []
    ram_reference = manifest.get("ram_bank", {})
    ram_path = root / ram_reference.get("path", "")
    try:
        bank = read_manifest(ram_path)
        if bank.get("profile") != RAM_PROFILE:
            errors.append("RAM bank uses an unsupported profile")
        if sha256_path(ram_path) != ram_reference.get("sha256"):
            errors.append("RAM bank hash differs from the suite manifest")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        bank = {}
        errors.append("frozen RAM bank is unreadable")
    bound = revision is not None
    if bound:
        try:
            git_commit(root, revision)
            suite_path = root / "experiments" / "EMC_WORKFLOW_SUITE.json"
            if sha256_bytes(git_bytes(root, revision, "experiments/EMC_WORKFLOW_SUITE.json")) != sha256_path(suite_path):
                errors.append("revision does not contain the current workflow-suite bytes")
        except ValueError as exc:
            errors.append(str(exc))
            bound = False
    for item in manifest.get("tests", []):
        fixture_path = root / item["fixture"] / "fixture.json"
        try:
            fixture = read_manifest(fixture_path)
            if fixture.get("profile") != FIXTURE_PROFILE or fixture.get("id") != item["id"]:
                raise ValueError("fixture identity/profile mismatch")
            if fixture.get("seed_status") != "materialized":
                raise ValueError("fixture seed is not materialized")
            file_rows = []
            for reference in (fixture["actor_prompt"], fixture["seed"], fixture["grader"]):
                path = root / item["fixture"] / reference["path"]
                observed = sha256_path(path)
                if observed != reference["sha256"]:
                    raise ValueError(f"hash mismatch for {path.relative_to(root).as_posix()}")
                file_rows.append({"path": path.relative_to(root).as_posix(), "sha256": observed})
            seed = read_manifest(root / item["fixture"] / fixture["seed"]["path"])
            if seed.get("profile") != SEED_PROFILE or seed.get("test_id") != item["id"]:
                raise ValueError("seed identity/profile mismatch")
            for directory in seed.get("actor_directories", []):
                observed, directory_rows = directory_projection(root, fixture_path.parent, directory)
                if observed != directory.get("sha256"):
                    raise ValueError(f"actor directory hash mismatch for {directory.get('source')}")
                file_rows.extend({"path": row["source"], "sha256": row["sha256"]} for row in directory_rows)
            for visible in seed.get("actor_files", []):
                path = (root / item["fixture"] / visible["source"]).resolve()
                observed = sha256_path(path)
                if observed != visible["sha256"]:
                    raise ValueError(f"actor seed hash mismatch for {path.relative_to(root).as_posix()}")
                file_rows.append({"path": path.relative_to(root).as_posix(), "sha256": observed})
            for overlay in seed.get("initial_dirty_overlays", []):
                path = (root / item["fixture"] / overlay["source"]).resolve()
                observed = sha256_path(path)
                if observed != overlay["sha256"]:
                    raise ValueError(f"initial overlay hash mismatch for {path.relative_to(root).as_posix()}")
                file_rows.append({"path": path.relative_to(root).as_posix(), "sha256": observed})
            for check in seed.get("smoke", []):
                for overlay in check.get("overlays", []):
                    path = (root / item["fixture"] / overlay["source"]).resolve()
                    observed = sha256_path(path)
                    if observed != overlay["sha256"]:
                        raise ValueError(f"smoke overlay hash mismatch for {path.relative_to(root).as_posix()}")
                    file_rows.append({"path": path.relative_to(root).as_posix(), "sha256": observed})
            known_ids = {row.get("id") for row in bank.get("records", [])}
            if not set(fixture.get("ram_record_ids", [])) <= known_ids:
                raise ValueError("fixture RAM identities are absent from the frozen bank")
            if bound:
                frozen = [{"path": fixture_path.relative_to(root).as_posix(), "sha256": sha256_path(fixture_path)},
                          *file_rows, {"path": ram_path.relative_to(root).as_posix(), "sha256": sha256_path(ram_path)}]
                for file_row in frozen:
                    if source_sha256(git_bytes(root, revision, file_row["path"]), file_row["path"]) != file_row["sha256"]:
                        raise ValueError(f"revision does not contain the frozen bytes for {file_row['path']}")
            rows.append({"id": item["id"], "fixture_sha256": sha256_path(fixture_path), "files": file_rows})
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors.append(f"{item.get('id', 'unknown')}: {exc}")
    return {"outcome": "ok" if not errors else "invalid", "dispatch_authorized": False,
            "revision_bound": bound and not errors, "revision": revision, "fixtures": rows, "errors": errors}


def project_plan(manifest: dict, revision: str, root: Path) -> dict:
    result = validate(manifest)
    if result["errors"]:
        raise ValueError("; ".join(result["errors"]))
    materialized = validate_materialized(root, manifest, revision)
    if materialized["errors"]:
        raise ValueError("; ".join(materialized["errors"]))
    models = {model["short"]: model["id"] for model in manifest["models"]}
    fixture_by_id = {row["id"]: row for row in materialized["fixtures"]}
    tree_hash = tracked_tree_hash(root, revision)
    projections = {test["id"]: actor_projection(root, revision, test) for test in manifest["tests"]}
    arms = []
    for test_index, test in enumerate(manifest["tests"]):
        for shape_index, shape in enumerate(manifest["execution_shapes"]):
            ram_order = list(manifest["ram_conditions"])
            if (test_index * len(manifest["execution_shapes"]) + shape_index) % 2:
                ram_order.reverse()
            for ram in ram_order:
                arm_id = f"{test['id'].lower()}-{shape['id']}-ram-{ram}"
                projection = projections[test["id"]]
                phases = []
                for phase_number in range(1, test["answer_phases"] + 1):
                    phases.append({"id": f"phase-{phase_number}", "primary_model": models[shape["primary"]],
                                   "support_model": models.get(shape["support"]),
                                   "fresh_identities_required": phase_number > 1,
                                   "conversation_history": False if phase_number > 1 else None})
                arms.append({"id": arm_id, "task_id": test["id"], "fixture": test["fixture"],
                             "fixture_sha256": fixture_by_id[test["id"]]["fixture_sha256"],
                             "revision": revision, "tracked_tree_sha256": tree_hash,
                             "actor_projection_sha256": projection["projection_sha256"],
                             "actor_seed_sha256": projection["seed_sha256"],
                             "actor_tree_id": projection["tree_id"], "actor_commit_id": projection["commit_id"],
                             "actor_paths": projection["paths"],
                             "allowed_diff_paths": projection["allowed_diff_paths"],
                             "host_admitted_paths": projection["admitted_paths"],
                              "primary_model": models[shape["primary"]], "support_model": models.get(shape["support"]),
                              "answer_phases": phases,
                              "answer_worker_assignments": test["answer_phases"] * (1 + (shape["support"] is not None)),
                              "ram_condition": ram, "ram_bank_sha256": manifest["ram_bank"]["sha256"],
                              "capture": False, "worktree_name": arm_id, "host_namespace": arm_id,
                              "sequence": len(arms) + 1, "status": "planned"})
    benchmark_results = []
    for arm in arms:
        helper = arm["support_model"] or "solo"
        benchmark_results.append({
            "id": arm["id"], "label": f"{arm['task_id']} | {arm['primary_model']} + {helper} | RAM {arm['ram_condition']}",
            "primary_model": arm["primary_model"], "support_model": arm["support_model"], "status": "planned",
            "context_condition": "isolated-actor-repository", "ram_condition": arm["ram_condition"],
            "answer_run_ids": [], "review_run_ids": [], "closeout_run_ids": [], "closeout_applicable": False,
            "answer_text": "", "quality": None,
            "packet": {"sequence": arm["sequence"], "task_id": arm["task_id"],
                       "actor_commit_id": arm["actor_commit_id"], "answer_phases": arm["answer_phases"]},
            "memory_metrics": None,
        })
    benchmark_suite = {
        "id": f"{manifest['suite_id']}-{revision[:12]}", "label": "EMC model, companion and RAM workflow evaluation",
        "task_id": manifest["suite_id"], "status": "planned-unstarted", "test_type": "model-companion-ram",
        "description": "Frozen five-test, nine-shape, RAM-off/on evaluation",
        "started_at": None, "completed_at": None,
        "limits": {"arm_count": len(arms), "answer_worker_assignments": result["worker_assignments"],
                   "reviewer_assignments": manifest["review_policy"]["reviewer_assignments"],
                   "effort": manifest["effort"], "arm_concurrency": manifest["execution_policy"]["arm_concurrency"]},
        "results": benchmark_results,
    }
    return {"profile": PLAN_PROFILE, "suite_id": manifest["suite_id"],
            "manifest_sha256": sha256_bytes(canonical_bytes(manifest)), "revision": revision,
            "suite_file_sha256": sha256_path(root / "experiments" / "EMC_WORKFLOW_SUITE.json"),
             "tracked_tree_sha256": tree_hash, "effort": manifest["effort"], "arm_count": len(arms),
             "worker_assignments": result["worker_assignments"],
             "reviewer_assignments": manifest["review_policy"]["reviewer_assignments"],
             "execution_policy": manifest["execution_policy"], "review_policy": manifest["review_policy"],
             "cost_policy": manifest["cost_policy"], "benchmark_suite": benchmark_suite,
             "dispatch_authorized": False, "arms": arms}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def prepare_arm(root: Path, plan: dict, arm_id: str, run_root: Path) -> dict:
    if plan.get("dispatch_authorized") is not False:
        raise ValueError("only a non-dispatching EMC plan is accepted")
    matches = [arm for arm in plan.get("arms", []) if arm.get("id") == arm_id]
    if len(matches) != 1:
        raise ValueError("select one exact planned arm")
    arm = matches[0]
    try:
        run_root.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("run root must be outside the source checkout")
    revision = git_commit(root, arm["revision"])
    if tracked_tree_hash(root, revision) != arm["tracked_tree_sha256"]:
        raise ValueError("tracked tree differs from the frozen plan")
    worktree = (run_root / "worktrees" / arm_id).resolve()
    host = (run_root / "hosts" / arm_id).resolve()
    state_path = (run_root / "states" / f"{arm_id}.json").resolve()
    if state_path.exists() or worktree.exists() or host.exists():
        raise ValueError("arm namespace already exists; use recover and preserve its evidence")
    state = {"profile": STATE_PROFILE, "arm": arm, "status": "preparing", "worktree": str(worktree),
             "host": str(host), "capture_allowed": False,
             "ram": {"condition": arm["ram_condition"], "search_calls": 0, "get_calls": 0,
                     "eligible": [], "expanded": []},
             "recovery": {"command": f"recover --run-root {run_root} --arm {arm_id}"}}
    _write_json(state_path, state)
    try:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        worktree.mkdir()
        fixture_root = root / arm["fixture"]
        fixture = read_manifest(fixture_root / "fixture.json")
        seed = read_manifest(fixture_root / fixture["seed"]["path"])
        projection = actor_projection(root, revision, {"id": arm["task_id"], "fixture": arm["fixture"]})
        if (projection["projection_sha256"] != arm["actor_projection_sha256"] or
                projection["seed_sha256"] != arm["actor_seed_sha256"] or
                projection["tree_id"] != arm["actor_tree_id"] or
                projection["commit_id"] != arm["actor_commit_id"] or
                projection["allowed_diff_paths"] != arm.get("allowed_diff_paths") or
                projection["admitted_paths"] != arm.get("host_admitted_paths")):
            raise ValueError("actor projection differs from the frozen plan")
        for actor_name, value in projection["entries"].items():
            actor_path = worktree / actor_name
            actor_path.parent.mkdir(parents=True, exist_ok=True)
            actor_path.write_bytes(value)
        _run_git(worktree, "init", "-q", "--object-format=sha1", ".")
        _run_git(worktree, "add", "--all")
        observed_tree = _run_git(worktree, "write-tree").stdout.decode("ascii").strip()
        if observed_tree != projection["tree_id"]:
            raise ValueError("generated actor tree differs from the frozen plan")
        git_environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        git_environment.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "EUTONOS EMC", "GIT_AUTHOR_EMAIL": "emc-fixture@example.invalid",
            "GIT_COMMITTER_NAME": "EUTONOS EMC", "GIT_COMMITTER_EMAIL": "emc-fixture@example.invalid",
            "GIT_AUTHOR_DATE": "946684800 +0000", "GIT_COMMITTER_DATE": "946684800 +0000",
        })
        actor_commit = _run_git(
            worktree, "commit-tree", observed_tree,
            input_bytes=_actor_commit_message(arm["task_id"], revision, projection["seed_sha256"],
                                              projection["projection_sha256"]),
            environment=git_environment,
        ).stdout.decode("ascii").strip()
        if actor_commit != projection["commit_id"]:
            raise ValueError("generated actor commit differs from the frozen plan")
        _run_git(worktree, "checkout", "-q", "--detach", actor_commit, environment=git_environment)
        admitted_paths = projection["admitted_paths"]
        if not set(projection["allowed_diff_paths"]) <= set(admitted_paths):
            raise ValueError("allowed diff paths are not covered by exact host admission")
        host.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root / "src")
        command = [sys.executable, "-m", "eutonos.host_cli", "enroll", "--root", str(worktree),
                   "--host", str(host), "--actor", "emc-" + arm_id, "--hours", "24"]
        for admitted in admitted_paths:
            command.extend(["--path", admitted])
        completed = subprocess.run(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode:
            raise ValueError("isolated EUTONOS host enrollment failed: " + completed.stdout.decode("utf-8", "replace"))
        enrollment = json.loads(completed.stdout)
        controller = run_root / "controller"
        controller.mkdir(parents=True, exist_ok=True)
        bank_target = controller / "ram-bank.json"
        bank_target.write_bytes(git_bytes(root, revision, "experiments/EMC_RAM_BANK.json"))
        if sha256_path(bank_target) != arm["ram_bank_sha256"]:
            raise ValueError("copied RAM bank differs from the frozen plan")
        dirty_baselines = {}
        for overlay in seed.get("initial_dirty_overlays", []):
            source_path, source_relative = _source_path(root, fixture_root, overlay["source"])
            value = git_bytes(root, revision, source_relative)
            if source_sha256(value, source_relative) != overlay["sha256"]:
                raise ValueError("initial dirty overlay differs from its seed hash")
            target = worktree / _safe_actor_path(overlay["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
            dirty_baselines[target.relative_to(worktree).as_posix()] = sha256_path(target)
        state.update({"actor_repository_created": True, "status": "prepared",
                      "source_revision": revision, "worktree_head": git_commit(worktree, actor_commit),
                      "actor_tree_id": observed_tree, "actor_projection_sha256": projection["projection_sha256"],
                      "worktree_identity": sha256_bytes(str(worktree).encode()),
                      "host_namespace_identity": sha256_bytes(str(host).encode()),
                      "host_setup": enrollment, "initial_dirty_hashes": dirty_baselines,
                      "actor_prompt": str(worktree / "ACTOR_PROMPT.md")})
        _write_json(state_path, state)
        return state
    except Exception as exc:
        state.update({"status": "preparation-failed-preserved", "error": str(exc)})
        _write_json(state_path, state)
        raise


def recover_arm(root: Path, run_root: Path, arm_id: str) -> dict:
    state = read_manifest(run_root / "states" / f"{arm_id}.json")
    if state.get("profile") != STATE_PROFILE:
        raise ValueError("unsupported arm state")
    worktree, host = Path(state["worktree"]), Path(state["host"])
    observed = {"state": state, "worktree_exists": worktree.exists(), "host_exists": host.exists(),
                "dispatch_authorized": False}
    if worktree.exists():
        observed["worktree_head"] = _run_git(worktree, "rev-parse", "HEAD", check=False).stdout.decode().strip() or None
        observed["worktree_status"] = _run_git(worktree, "status", "--porcelain=v1", "--untracked-files=all", check=False).stdout.decode("utf-8", "replace").splitlines()
    observed["cleanup"] = "safe only when worktree status is empty and the host namespace has no retained evidence"
    return observed


def _require_inside(path: Path, parent: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes the selected run root") from exc
    return resolved


def cleanup_arm(root: Path, run_root: Path, arm_id: str) -> dict:
    observed = recover_arm(root, run_root, arm_id)
    state = observed["state"]
    if observed.get("worktree_status"):
        return {"outcome": "preserved", "reason": "worktree contains evidence", "recovery": observed}
    if observed["host_exists"]:
        return {"outcome": "preserved", "reason": "host namespace exists and must be reviewed explicitly", "recovery": observed}
    worktree = _require_inside(Path(state["worktree"]), run_root / "worktrees", "actor repository")
    if worktree.exists():
        shutil.rmtree(worktree)
    state["status"] = "cleaned"
    _write_json(run_root / "states" / f"{arm_id}.json", state)
    return {"outcome": "cleaned", "arm": arm_id, "state_preserved": True}


def archive_arm(root: Path, run_root: Path, arm_id: str) -> dict:
    """Move worktree and host evidence to a recoverable archive; never delete either."""
    observed = recover_arm(root, run_root, arm_id)
    state = observed["state"]
    archive = run_root / "archive" / arm_id
    archived_worktree = archive / "worktree"
    archived_host = archive / "host"
    if archive.exists():
        raise ValueError("arm archive already exists; inspect it instead of overwriting evidence")
    archive.mkdir(parents=True)
    worktree = _require_inside(Path(state["worktree"]), run_root / "worktrees", "actor repository")
    host = _require_inside(Path(state["host"]), run_root / "hosts", "host namespace")
    if worktree.exists():
        worktree.replace(archived_worktree)
    if host.exists():
        host.replace(archived_host)
    state.update({"status": "archived", "worktree": str(archived_worktree), "host": str(archived_host),
                  "archived_status": observed.get("worktree_status", [])})
    _write_json(run_root / "states" / f"{arm_id}.json", state)
    return {"outcome": "archived", "arm": arm_id, "archive": str(archive),
            "dirty_evidence_preserved": bool(observed.get("worktree_status")), "dispatch_authorized": False}


def ram_access(run_root: Path, arm_id: str, action: str, *, query: str | None = None, record_id: str | None = None) -> dict:
    state_path = run_root / "states" / f"{arm_id}.json"
    state = read_manifest(state_path)
    ram = state["ram"]
    if ram["condition"] == "off":
        raise ValueError("RAM retrieval is disabled for this arm")
    bank_path = run_root / "controller" / "ram-bank.json"
    bank = read_manifest(bank_path)
    if sha256_path(bank_path) != state["arm"]["ram_bank_sha256"]:
        raise ValueError("RAM bank differs from the frozen plan")
    if action == "search":
        if ram["search_calls"] >= 1:
            raise ValueError("RAM-on permits one bounded search pass")
        terms = {part.casefold() for part in re.findall(r"[\w.-]+", query or "") if len(part) > 2}
        rows = []
        for record in bank["records"]:
            haystack = " ".join([record["id"], record["title"], record["preview"], *record.get("terms", [])]).casefold()
            score = sum(term in haystack for term in terms)
            if score:
                rows.append({"id": record["id"], "class": record["class"], "preview": record["preview"], "score": score})
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        ram["search_calls"] += 1
        visible = rows[:5]
        ram["eligible"] = [row["id"] for row in visible]
        result = {"outcome": "ok", "results": visible, "total": len(rows)}
    elif action == "get":
        if not ram.get("search_calls") or record_id not in ram.get("eligible", []):
            raise ValueError("select one exact RAM identity from this arm's search preview")
        matches = [record for record in bank["records"] if record["id"] == record_id]
        if len(matches) != 1:
            raise ValueError("select one exact RAM identity from the search preview")
        ram["get_calls"] += 1
        ram["expanded"].append(record_id)
        result = {"outcome": "ok", "record": matches[0]}
    else:
        raise ValueError("unsupported RAM action")
    _write_json(state_path, state)
    return result


def checkpoint_t5(root: Path, run_root: Path, arm_id: str, actor_id: str, checks_path: Path) -> dict:
    state_path = run_root / "states" / f"{arm_id}.json"
    state = read_manifest(state_path)
    if state.get("arm", {}).get("task_id") != "EMC-T5" or state.get("status") != "prepared":
        raise ValueError("T5 checkpoint requires one prepared EMC-T5 arm")
    if not actor_id.strip():
        raise ValueError("phase-one actor identity is required")
    checks = read_manifest(checks_path)
    required = {"javascript-syntax", "client-characterization", "dashboard-focused"}
    passed = {row.get("id") for row in checks.get("checks", []) if row.get("outcome") == "passed"}
    if not required <= passed:
        raise ValueError("phase-one checkpoint checks are incomplete")
    worktree = Path(state["worktree"])
    status = _run_git(worktree, "status", "--porcelain=v1", "--untracked-files=all").stdout.decode().splitlines()
    diff = _run_git(worktree, "diff", "--binary", "HEAD").stdout
    state_hash = worktree_state_hash(worktree)
    packet = {
        "profile": "eutonos.model-companion-handoff-packet.v1", "test_id": "EMC-T5",
        "arm_id": arm_id, "phase_one_actor": actor_id, "conversation_history": False,
        "revision": state["arm"]["revision"], "worktree_status": status,
        "git_diff_sha256": sha256_bytes(diff), "worktree_state_sha256": state_hash,
        "checks": checks["checks"],
        "ram_condition": state["ram"]["condition"], "eligible_ram_expanded": list(state["ram"]["expanded"]),
        "continuation_inputs": ["actor prompt", "Git HEAD and worktree diff", "checkpoint checks", "eligible RAM identities"],
        "dispatch_authorized": False,
    }
    packet_path = run_root / "controller" / "handoffs" / f"{arm_id}.json"
    _write_json(packet_path, packet)
    state.update({"status": "handoff-ready", "phase_one_actor": actor_id,
                  "phase_one_diff_sha256": packet["git_diff_sha256"],
                  "phase_one_state_sha256": state_hash, "handoff_packet": str(packet_path)})
    _write_json(state_path, state)
    return {"outcome": "handoff-ready", "packet": str(packet_path), "packet_sha256": sha256_path(packet_path),
            "conversation_history": False, "dispatch_authorized": False}


def resume_t5(root: Path, run_root: Path, arm_id: str, actor_id: str) -> dict:
    state_path = run_root / "states" / f"{arm_id}.json"
    state = read_manifest(state_path)
    if state.get("status") != "handoff-ready" or state.get("arm", {}).get("task_id") != "EMC-T5":
        raise ValueError("T5 continuation requires a completed phase-one checkpoint")
    if not actor_id.strip() or actor_id == state.get("phase_one_actor"):
        raise ValueError("phase two requires a fresh primary identity")
    worktree = Path(state["worktree"])
    current_diff = sha256_bytes(_run_git(worktree, "diff", "--binary", "HEAD").stdout)
    current_state = worktree_state_hash(worktree)
    if current_diff != state["phase_one_diff_sha256"] or current_state != state["phase_one_state_sha256"]:
        raise ValueError("worktree changed after the frozen handoff checkpoint")
    packet = read_manifest(Path(state["handoff_packet"]))
    if packet.get("conversation_history") is not False:
        raise ValueError("handoff packet exposes conversation history")
    state.update({"status": "phase-two-ready", "phase_two_actor": actor_id})
    _write_json(state_path, state)
    return {"outcome": "phase-two-ready", "actor": actor_id, "packet": state["handoff_packet"],
            "conversation_history": False, "capture_allowed": False, "dispatch_authorized": False}


def _resolve_command_part(part: str) -> str:
    """Resolve a portable command token to a runnable path.

    `{python}` always means this interpreter. A bare `node` is resolved through
    PATHEXT (`shutil.which`) so a Windows shim (`node.cmd`/`node.bat`, common with
    nvm-windows/Volta installs) still launches: `subprocess.run` on Windows does not
    search `PATHEXT` extensions for an unresolved name the way `cmd.exe` does, so an
    un-resolved shim otherwise fails with `FileNotFoundError: [WinError 2]` even
    though the same name works from an interactive shell.
    """
    if part == "{python}":
        return sys.executable
    if part == "node":
        return shutil.which("node") or part
    return part


def _smoke_command(cwd: Path, command: list[str], expected: int) -> dict:
    selected = [_resolve_command_part(part) for part in command]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(selected, cwd=cwd, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return {"command": command, "expected": expected, "returncode": completed.returncode,
            "passed": completed.returncode == expected, "output_sha256": sha256_bytes(completed.stdout.encode())}


def smoke(root: Path, manifest: dict) -> dict:
    materialized = validate_materialized(root, manifest)
    rows = []
    for item in manifest["tests"]:
        fixture_root = root / item["fixture"]
        fixture = read_manifest(fixture_root / "fixture.json")
        seed = read_manifest(fixture_root / fixture["seed"]["path"])
        checks = seed.get("smoke", [])
        temporary = tempfile.TemporaryDirectory(prefix=item["id"].lower() + "-") if any(check.get("overlays") for check in checks) else None
        selected_root = fixture_root
        if temporary:
            selected_root = Path(temporary.name) / "fixture"
            shutil.copytree(fixture_root, selected_root)
        results = []
        for check in checks:
            for overlay in check.get("overlays", []):
                source = selected_root / overlay["source"]
                if sha256_path(source) != overlay["sha256"]:
                    raise ValueError(f"smoke overlay hash mismatch for {item['id']}")
                target = selected_root / overlay["target"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            results.append(_smoke_command(selected_root / check.get("cwd", "."), check["command"], check["expected_exit"]))
        if temporary:
            temporary.cleanup()
        rows.append({"id": item["id"], "checks": results,
                     "outcome": "ok" if all(row["passed"] for row in results) else "failed"})
    errors = list(materialized["errors"])
    errors.extend(f"{row['id']} smoke failed" for row in rows if row["outcome"] != "ok")
    return {"outcome": "ok" if not errors else "invalid", "dispatch_authorized": False,
            "answer_production_calls": 0, "reviewer_calls": 0, "fixtures": rows, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "preflight", "smoke", "plan", "prepare-arm", "recover", "cleanup", "archive-arm", "ram-search", "ram-get", "checkpoint-t5", "resume-t5"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--manifest", default="experiments/EMC_WORKFLOW_SUITE.json")
    parser.add_argument("--revision")
    parser.add_argument("--plan")
    parser.add_argument("--run-root")
    parser.add_argument("--arm")
    parser.add_argument("--query")
    parser.add_argument("--record-id")
    parser.add_argument("--actor-id")
    parser.add_argument("--check-results")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    manifest = read_manifest(manifest_path)
    try:
        if args.command == "validate":
            payload = validate(manifest)
        elif args.command == "preflight":
            payload = validate_materialized(root, manifest, args.revision)
        elif args.command == "smoke":
            payload = smoke(root, manifest)
        elif args.command == "plan":
            if not args.revision:
                parser.error("plan requires --revision")
            payload = project_plan(manifest, args.revision, root)
        else:
            if not args.run_root or not args.arm:
                parser.error(f"{args.command} requires --run-root and --arm")
            run_root = Path(args.run_root).resolve()
            if args.command == "prepare-arm":
                if not args.plan:
                    parser.error("prepare-arm requires --plan")
                payload = prepare_arm(root, read_manifest(Path(args.plan)), args.arm, run_root)
            elif args.command == "recover":
                payload = recover_arm(root, run_root, args.arm)
            elif args.command == "cleanup":
                payload = cleanup_arm(root, run_root, args.arm)
            elif args.command == "archive-arm":
                payload = archive_arm(root, run_root, args.arm)
            elif args.command == "ram-search":
                payload = ram_access(run_root, args.arm, "search", query=args.query)
            elif args.command == "ram-get":
                payload = ram_access(run_root, args.arm, "get", record_id=args.record_id)
            elif args.command == "checkpoint-t5":
                if not args.actor_id or not args.check_results:
                    parser.error("checkpoint-t5 requires --actor-id and --check-results")
                payload = checkpoint_t5(root, run_root, args.arm, args.actor_id, Path(args.check_results))
            else:
                if not args.actor_id:
                    parser.error("resume-t5 requires --actor-id")
                payload = resume_t5(root, run_root, args.arm, args.actor_id)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        payload = {"outcome": "invalid", "dispatch_authorized": False, "errors": [str(error)]}
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        _write_json(Path(args.output), payload)
    else:
        print(text, end="")
    return 0 if payload.get("outcome") not in {"invalid", "failed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
