"""Cheap, bounded repository health and metrics projections for the dashboard.

No composite health percentage; missing coverage is reported as Unknown, never
folded into a healthy or unhealthy score. Dirty worktrees and old campaigns are
not automatically unhealthy. Co-change evidence suggests investigation, not a
declared dependency. All scans are bounded (a single log window, capped file
lists) and nothing here runs tests or calls a model."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
import subprocess
from .hidden_process import no_window

from . import tokenslang as ts

HEALTH_PROFILE = "eutonos.repo-health.v1"
METRICS_PROFILE = "eutonos.repo-metrics.v1"

_METRICS_CACHE: dict = {}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _git(root, *args):
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                                capture_output=True, text=True, timeout=10, **no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_raw(root, *args):
    try:
        result = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                                capture_output=True, text=True, timeout=20, **no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _indicator(id_, area, label, state, rule, *, threshold=None, evidence=(), scope="repository", next_action=""):
    return {"id": id_, "area": area, "label": label, "state": state, "rule": rule, "threshold": threshold,
            "evidence": list(evidence), "scope": scope, "observed_at": _now(), "next_action": next_action}


# --------------------------------------------------------------------------- health


def _verification(indicators):
    indicators.append(_indicator(
        "verification.no_store", "verification", "Test result verification", "unknown",
        "No recorded test-result store exists yet; the ETT campaign owns that evidence.",
        evidence=["No test-result store is wired into the dashboard."],
        next_action="Wire a named test-group result store (ETT) before this indicator can report pass/fail state."))


def _work_records(indicators, boards_module):
    status = boards_module.get("status")
    if status == "available":
        indicators.append(_indicator(
            "work_records.boards", "work_records", "Work record integrity", "healthy",
            "Board configuration and owners parsed without diagnostics.",
            evidence=[f"{len(boards_module.get('items', []))} board items projected."]))
    elif status == "partial":
        diagnostics = boards_module.get("diagnostics", [])
        indicators.append(_indicator(
            "work_records.boards", "work_records", "Work record integrity", "warning",
            "Board diagnostics were raised while projecting work records.",
            evidence=list(diagnostics[:20]),
            next_action="Resolve the listed board diagnostics."))
    elif status == "not-configured":
        indicators.append(_indicator(
            "work_records.boards", "work_records", "Work record integrity", "healthy",
            "No board configuration is registered in this repository.",
            evidence=["Boards module reports not-configured."]))
    elif status == "error":
        indicators.append(_indicator(
            "work_records.boards", "work_records", "Work record integrity", "alert",
            "Boards module raised an error while projecting work records.",
            evidence=[boards_module.get("reason", "Unknown error.")],
            next_action="Investigate the boards module error before trusting work records."))
    else:
        indicators.append(_indicator(
            "work_records.boards", "work_records", "Work record integrity", "unknown",
            "Boards module status was not recognized.",
            evidence=[f"status={status!r}"]))


def _knowledge(indicators, root, local, nav_module):
    if nav_module.get("status") != "available":
        indicators.append(_indicator(
            "knowledge.nav_freshness", "knowledge", "NAV source freshness", "unknown",
            "NAV module is unavailable; freshness cannot be computed.",
            evidence=[f"nav status={nav_module.get('status')!r}"]))
    else:
        try:
            from .nav_runner import Session
            session = Session(local)
            current = session.status()
            if current.get("outcome") == "no-baseline":
                # Maps exist but no baseline run: freshness is unknown, never "healthy".
                indicators.append(_indicator(
                    "knowledge.nav_freshness", "knowledge", "NAV source freshness", "unknown",
                    "NAV maps exist but no baseline run is recorded, so freshness cannot be computed.",
                    evidence=[f"records={current.get('records')}"], next_action=current.get("next", "")))
            else:
                state = "healthy" if current["sourceCurrent"] is True else "warning"
                indicators.append(_indicator(
                    "knowledge.nav_freshness", "knowledge", "NAV source freshness", state,
                    "NAV's recorded source fingerprint is compared to the current source inventory.",
                    evidence=[f"sourceCurrent={current['sourceCurrent']}", f"mapsCurrent={current['mapsCurrent']}"],
                    next_action="" if state == "healthy" else "Run nav next to refresh evidence against changed sources."))
        except ts.ReadError as exc:
            indicators.append(_indicator(
                "knowledge.nav_freshness", "knowledge", "NAV source freshness", "unknown",
                "No NAV run is recorded to compare against the current source fingerprint.",
                evidence=[getattr(exc, "message", str(exc))]))
        except (OSError, KeyError, TypeError, ValueError):
            indicators.append(_indicator(
                "knowledge.nav_freshness", "knowledge", "NAV source freshness", "unknown",
                "NAV freshness could not be computed.", evidence=[]))

    if nav_module.get("status") != "available":
        indicators.append(_indicator(
            "knowledge.nav_evidence", "knowledge", "NAV evidence paths", "unknown",
            "NAV module is unavailable; evidence paths cannot be checked.", evidence=[]))
        return
    missing = []
    seen = set()
    for route in nav_module.get("routes", []):
        for path in route.get("evidence", []):
            if path in seen:
                continue
            seen.add(path)
            if not (root / path).exists():
                missing.append(path)
            if len(missing) >= 20:
                break
        if len(missing) >= 20:
            break
    if missing:
        indicators.append(_indicator(
            "knowledge.nav_evidence", "knowledge", "NAV evidence paths", "warning",
            "One or more NAV evidence paths no longer exist in the repository.",
            evidence=missing, next_action="Update or remove the NAV records pointing at missing evidence."))
    else:
        indicators.append(_indicator(
            "knowledge.nav_evidence", "knowledge", "NAV evidence paths", "healthy",
            "All checked NAV evidence paths exist in the repository.", evidence=[]))


def _delivery(indicators, repository, repo_metrics_module):
    if repository.get("status") != "available":
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "unknown",
            "Local repository state could not be fully read.", evidence=[]))
        return
    conflicts = repository.get("changes", {}).get("conflicts", 0) or 0
    if conflicts:
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "alert",
            "Unresolved merge conflict markers are present in the working tree.",
            evidence=[f"conflicts={conflicts}"],
            next_action="Resolve the conflicted paths before continuing delivery."))
        return
    divergence = (repo_metrics_module or {}).get("divergence") if isinstance(repo_metrics_module, dict) else None
    if repo_metrics_module is None or repo_metrics_module.get("status") != "available" or divergence is None:
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "unknown",
            "Branch divergence against upstream could not be computed.",
            evidence=[f"uncommitted={repository.get('uncommitted')}"],
            next_action=""))
        return
    if not divergence.get("upstream"):
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "unknown",
            "No upstream is configured for the current branch.",
            evidence=[f"branch={divergence.get('branch')}"]))
        return
    ahead, behind = divergence.get("ahead") or 0, divergence.get("behind") or 0
    evidence = [f"branch={divergence.get('branch')}", f"upstream={divergence.get('upstream')}",
                f"ahead={ahead}", f"behind={behind}", f"uncommitted={repository.get('uncommitted')}"]
    if ahead and behind:
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "warning",
            "Local branch has diverged from its upstream (ahead and behind).",
            evidence=evidence, next_action="Reconcile (pull/rebase) before pushing further commits."))
    elif ahead:
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "warning",
            "Local branch has commits not yet pushed to its upstream.",
            evidence=evidence, next_action="Push the ahead commits when ready."))
    else:
        indicators.append(_indicator(
            "delivery.push_state", "delivery", "Delivery / push state", "healthy",
            "Local branch matches or trails its upstream; a dirty worktree alone is not unhealthy.",
            evidence=evidence))


def _recovery(indicators, write_retry_module):
    status = write_retry_module.get("status")
    if status == "error":
        indicators.append(_indicator(
            "recovery.write_retry", "recovery", "Unfinished host effects", "alert",
            "The write-retry/recovery module reported an error.",
            evidence=[write_retry_module.get("reason", "Unknown error.")],
            next_action="Investigate the recovery module error."))
        return
    if status not in ("available",):
        indicators.append(_indicator(
            "recovery.write_retry", "recovery", "Unfinished host effects", "healthy",
            "No host coordinator effects are pending recovery.", evidence=[f"status={status!r}"]))
        return
    counts = write_retry_module.get("counts", {})
    worker = write_retry_module.get("worker") or {}
    pending_total = sum(counts.get(name, 0) for name in ("pending", "waiting", "working"))
    failed = counts.get("failed", 0) or 0
    if failed or worker.get("state") in ("stale", "failed"):
        indicators.append(_indicator(
            "recovery.write_retry", "recovery", "Unfinished host effects", "alert",
            "Recovery requests have failed or the worker is stale.",
            evidence=[f"counts={counts}", f"worker={worker}"],
            next_action="Recover or re-run the failed requests."))
    elif pending_total:
        indicators.append(_indicator(
            "recovery.write_retry", "recovery", "Unfinished host effects", "warning",
            "Host effects are queued or in progress.",
            evidence=[f"counts={counts}"], next_action="Allow the recovery worker to drain the queue."))
    else:
        indicators.append(_indicator(
            "recovery.write_retry", "recovery", "Unfinished host effects", "healthy",
            "No pending, waiting, working or failed recovery requests.", evidence=[f"counts={counts}"]))


def _dependencies(indicators):
    indicators.append(_indicator(
        "dependencies.no_audit", "dependencies", "Dependency alerts", "unknown",
        "No dependency audit evidence has been consumed for this repository.",
        evidence=["An audit has not been recorded."],
        next_action="Record dependency audit evidence before this indicator can report alerts."))


def _runtime(indicators, release_module):
    status = release_module.get("status")
    if status != "available":
        indicators.append(_indicator(
            "runtime.alignment", "runtime", "Runtime compatibility", "unknown",
            "No project release manifest is configured to compare against the installed runtime.",
            evidence=[f"release status={status!r}"]))
        return
    declared, installed = release_module.get("version"), release_module.get("runtime_version")
    evidence = [f"declared_version={declared}", f"runtime_version={installed}"]
    if declared is not None and installed is not None and declared == installed:
        indicators.append(_indicator(
            "runtime.alignment", "runtime", "Runtime compatibility", "healthy",
            "Declared project version matches the installed runtime version.", evidence=evidence))
    else:
        indicators.append(_indicator(
            "runtime.alignment", "runtime", "Runtime compatibility", "warning",
            "Declared project version does not match the installed runtime version.",
            evidence=evidence, next_action="Reinstall or update the runtime to match the declared release."))


def health(root, project_result):
    """Cheap advisory health indicators derived from an already-built project() result."""
    root = Path(root)
    local_root = None
    try:
        from .effects import LocalRoot
        local_root = LocalRoot(root)
    except (OSError, ts.ReadError, ValueError, TypeError):
        local_root = None
    modules = project_result.get("modules", {})
    indicators = []
    _verification(indicators)
    _work_records(indicators, modules.get("boards", {}))
    if local_root is not None:
        _knowledge(indicators, root, local_root, modules.get("nav", {}))
    else:
        indicators.append(_indicator("knowledge.nav_freshness", "knowledge", "NAV source freshness", "unknown",
                                      "Local root could not be opened.", evidence=[]))
        indicators.append(_indicator("knowledge.nav_evidence", "knowledge", "NAV evidence paths", "unknown",
                                      "Local root could not be opened.", evidence=[]))
    _delivery(indicators, project_result.get("repository", {}), modules.get("repo_metrics"))
    _recovery(indicators, modules.get("write_retry", {}))
    _dependencies(indicators)
    _runtime(indicators, project_result.get("release", {}))
    counts = {"healthy": 0, "warning": 0, "alert": 0, "unknown": 0, "disabled": 0}
    for indicator in indicators:
        counts[indicator["state"]] = counts.get(indicator["state"], 0) + 1
    return {"status": "available", "profile": HEALTH_PROFILE, "observed_at": _now(), "indicators": indicators,
            "counts": counts,
            "coverage_note": "Indicators reflect only the declared checks above; missing coverage is Unknown, "
                             "not folded into a health percentage. A dirty worktree and old campaigns are not "
                             "automatically unhealthy."}


# --------------------------------------------------------------------------- metrics


def _parse_log(raw):
    commits = []
    current = None
    for line in raw.splitlines():
        if line.startswith("COMMIT\t"):
            _, commit_hash, epoch = line.split("\t", 2)
            current = {"hash": commit_hash, "epoch": int(epoch), "files": []}
            commits.append(current)
        elif line.strip() and current is not None:
            current["files"].append(line.strip())
    return commits


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _divergence(root):
    branch = _git(root, "branch", "--show-current") or None
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream:
        return {"branch": branch, "upstream": None, "ahead": None, "behind": None,
                "qualification": "From local refs; not fetched."}
    counts = _git(root, "rev-list", "--left-right", "--count", "@{u}...HEAD")
    behind, ahead = (None, None)
    if counts:
        parts = counts.split()
        if len(parts) == 2:
            try:
                behind, ahead = int(parts[0]), int(parts[1])
            except ValueError:
                behind = ahead = None
    return {"branch": branch, "upstream": upstream, "ahead": ahead, "behind": behind,
            "qualification": "From local refs; not fetched."}


def metrics(root, limit=500):
    root = Path(root).resolve(strict=True)
    head = _git(root, "rev-parse", "HEAD")
    cache_key = (str(root), head, limit)
    if head is not None and cache_key in _METRICS_CACHE:
        return _METRICS_CACHE[cache_key]

    version = _git(root, "--version")
    if version is None:
        result = {"status": "unavailable", "profile": METRICS_PROFILE, "observed_at": _now(),
                  "reason": "git is not available."}
        return result
    raw = _git_raw(root, "log", f"-n{limit}", "--no-merges", "--name-only", "--format=COMMIT\t%H\t%ct")
    if raw is None:
        result = {"status": "unavailable", "profile": METRICS_PROFILE, "observed_at": _now(),
                  "reason": "Not a git repository, or it has no commits."}
        return result

    commits = _parse_log(raw)
    commits_scanned = len(commits)
    hotspot_counts, hotspot_last = {}, {}
    dir_counts = {}
    pair_counts = {}
    skipped = 0
    for commit in commits:
        files = commit["files"]
        unique_files = sorted(set(files))
        for path in unique_files:
            hotspot_counts[path] = hotspot_counts.get(path, 0) + 1
            if path not in hotspot_last:
                hotspot_last[path] = commit["epoch"]
        dirs = set()
        for path in unique_files:
            parts = Path(path).parts[:2]
            if parts:
                dirs.add("/".join(parts))
        for directory in dirs:
            dir_counts[directory] = dir_counts.get(directory, 0) + 1
        if len(unique_files) > 40:
            skipped += 1
            continue
        for a, b in combinations(unique_files, 2):
            key = (a, b) if a < b else (b, a)
            pair_counts[key] = pair_counts.get(key, 0) + 1

    hotspots = [{"path": path, "commits": count, "last_at": _iso(hotspot_last[path])}
                for path, count in hotspot_counts.items() if (root / path).exists()]
    hotspots.sort(key=lambda item: item["commits"], reverse=True)
    hotspots = hotspots[:25]

    directories = [{"path": path, "commits": count} for path, count in dir_counts.items()]
    directories.sort(key=lambda item: item["commits"], reverse=True)
    directories = directories[:15]

    co_change = [{"a": pair[0], "b": pair[1], "count": count} for pair, count in pair_counts.items() if count >= 3]
    co_change.sort(key=lambda item: item["count"], reverse=True)
    co_change = co_change[:20]

    tracked = _git_raw(root, "ls-files")
    tracked_files = len(tracked.splitlines()) if tracked is not None else None

    first_commit = commits[-1]["hash"] if commits else None
    last_commit = commits[0]["hash"] if commits else None
    first_at = _iso(commits[-1]["epoch"]) if commits else None
    last_at = _iso(commits[0]["epoch"]) if commits else None

    result = {
        "status": "available", "profile": METRICS_PROFILE, "observed_at": _now(),
        "window": {"commits_scanned": commits_scanned, "limit": limit,
                   "first_commit": first_commit, "last_commit": last_commit,
                   "first_at": first_at, "last_at": last_at, "truncated": commits_scanned >= limit},
        "hotspots": hotspots, "directories": directories,
        "co_change": co_change, "co_change_skipped_commits": skipped,
        "divergence": _divergence(root),
        "size": {"tracked_files": tracked_files},
        "coverage_note": (f"Hotspots and co-change reflect only the most recent {commits_scanned} scanned "
                          f"commits (limit {limit}); older history is not scanned. Files changed together "
                          "suggest investigation, not a declared dependency. Ahead/behind are read from local "
                          "refs and are not fetched from the remote."),
    }
    if head is not None:
        _METRICS_CACHE[cache_key] = result
    return result
