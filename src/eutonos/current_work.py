"""Shared read-only Current Work; native repository files remain authoritative."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from .hidden_process import no_window
import time
import uuid

from .private_scope import require_shared
from . import repository_state, work_lifecycle, tokenslang as ts
from . import home as eutonos_home
from .document_views import boards_view

PROFILE = "eutonos.current-work.v1"
CACHE_PROFILE = "eutonos.current-work-cache.v2"
CACHE_SECONDS = 2.0
MAX_CACHE_BYTES = 32 * 1024 * 1024
SOURCE_ID = re.compile(r"[0-9a-f]{24}\Z")
NATIVE_KEYS = {"id", "type", "title", "summary", "status", "parent_id",
               "evidence_refs", "next_action", "extensions"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _git_common(root):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    raw = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root),
                          "rev-parse", "--git-common-dir"], env=env, check=True,
                         capture_output=True, timeout=5, **no_window()).stdout.decode("utf-8", "strict").strip()
    if not raw:
        raise ValueError("Git common directory unavailable")
    path = Path(raw)
    return (path if path.is_absolute() else root / path).resolve(strict=True)


def _paths(root):
    require_shared(root)
    common = _git_common(root)
    identity_path = root / "eutonos.read.json"
    info = identity_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 or info.st_size < 1:
        raise ValueError("Native identity file is unavailable or over the read bound")
    with identity_path.open("rb") as stream:
        native = stream.read(512 * 1024 + 1)
    if len(native) > 512 * 1024:
        raise ValueError("Native identity file exceeds the read bound")
    identity = repository_state._identity(native)
    from .shared_work import selection
    key = hashlib.sha256(_encoded([os.path.normcase(str(common)), *identity, selection(root)])).hexdigest()
    home = eutonos_home.home()
    directory = home / "repositories" / key
    return (directory / "current-work.json", directory / "current-work.previous.json",
            directory / "current-work.lock", directory / "current-work.invalidate")


def _load(path):
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        value = json.loads(path.read_bytes().decode("utf-8"))
        if value.get("profile") != CACHE_PROFILE or not isinstance(value.get("payload"), dict):
            return None
        if hashlib.sha256(_encoded(value["payload"])).hexdigest() != value.get("sha256"):
            return None
        if not all(isinstance(value.get(key), int) for key in ("scan_started_ns", "saved_at_ns")):
            return None
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _invalidated(path):
    try:
        return int(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return 0


def _atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    until = time.monotonic() + 2
    while time.monotonic() < until:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 60:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.05)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _save(paths, payload, scan_started_ns):
    current, previous, lock, invalidation = paths
    raw = _encoded(payload)
    if len(raw) > MAX_CACHE_BYTES:
        return False
    with _lock(lock) as acquired:
        if not acquired or scan_started_ns < _invalidated(invalidation):
            return False
        existing = _load(current)
        if existing and existing["scan_started_ns"] > scan_started_ns:
            return False
        envelope = {"profile": CACHE_PROFILE, "scan_started_ns": scan_started_ns,
                    "saved_at_ns": time.time_ns(), "payload": payload,
                    "sha256": hashlib.sha256(raw).hexdigest()}
        if existing and repository_state.shared_cached_sources(
                payload["repository"]["root"], existing["payload"].get("sources", [])):
            _atomic(previous, _encoded(existing))
        _atomic(current, _encoded(envelope))
    return True


def invalidate(root):
    """Best-effort shared invalidation after a successful native write."""
    try:
        _atomic(_paths(Path(root).resolve(strict=True))[3], str(time.time_ns()).encode("ascii"))
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _source_id(kind, ref, commit, path=None):
    return hashlib.sha256(_encoded([kind, ref, commit, path])).hexdigest()[:24]


def _sources(raw, root):
    main = raw["main"]
    refs = [{"ref": main["ref"], "commit": main["commit"],
             "source_kind": main.get("ref_source", "cached-remote-ref"),
             "status": main["status"]}, *raw.get("sources", {}).get("refs", [])]
    result, seen = [], set()
    for row in refs:
        ref, commit, kind = row.get("ref"), row.get("commit"), row.get("source_kind")
        if not isinstance(ref, str) or not isinstance(commit, str) or kind not in {"local-ref", "cached-remote-ref"}:
            continue
        key = (kind, ref, commit)
        if key in seen:
            continue
        seen.add(key)
        result.append({"id": _source_id(kind, ref, commit), "label": ref,
                       "kind": kind, "ref": ref, "commit": commit,
                       "status": row.get("status", "unknown"),
                       "is_main": ref == main["ref"] and commit == main["commit"],
                       "is_checkout": False,
                       "publication": "cached-local-ref" if kind == "cached-remote-ref" else "unknown"})
    for row in raw.get("sources", {}).get("worktrees", []):
        path, commit = row.get("path"), row.get("commit")
        if not isinstance(path, str) or not isinstance(commit, str):
            continue
        ref = "worktree:" + path
        result.append({"id": _source_id("working-tree", ref, commit, path),
                       "label": Path(path).name, "kind": "working-tree", "ref": ref,
                       "commit": commit, "path": path,
                       "status": row.get("status", "unknown"), "is_main": False,
                       "is_checkout": Path(path).resolve() == root,
                       "publication": "unknown",
                       "dirty_native_paths": row.get("dirty_native_paths")})
    return result


def _selected_root(payload, root):
    """Checkout selection is a caller view, never an authority saved in cache."""
    payload = deepcopy(payload)
    payload["repository"]["root"] = str(root)
    for row in payload["sources"]:
        row["is_checkout"] = (row["kind"] == "working-tree"
                              and Path(row["path"]).resolve() == root)
    return payload


def _scan_base(root, refresh=False):
    paths = None
    cached = previous = None
    try:
        paths = _paths(root)
        cached, previous = _load(paths[0]), _load(paths[1])
        # Last-good fallback must obey current privacy, not its former eligibility.
        rejected = []
        if cached and not repository_state.shared_cached_sources(root, cached["payload"].get("sources", [])):
            cached = None
            rejected.append(paths[0])
        if previous and not repository_state.shared_cached_sources(root, previous["payload"].get("sources", [])):
            previous = None
            rejected.append(paths[1])
        for path in rejected:
            path.unlink(missing_ok=True)
        if (not refresh and cached and cached["payload"].get("status") == "available"
                and cached["scan_started_ns"] >= _invalidated(paths[3])):
            age = max(0, (time.time_ns() - cached["saved_at_ns"]) / 1e9)
            if age < CACHE_SECONDS:
                payload = _selected_root(cached["payload"], root)
                payload["freshness"] = {"kind": "cached", "age_seconds": round(age, 3)}
                return payload
    except (OSError, ValueError, subprocess.SubprocessError):
        paths = None
    scan_started_ns = time.time_ns()
    try:
        raw = repository_state.project(root, refresh=True)
        payload = {"status": raw["status"], "repository": raw["repository"],
                   "observed_at": raw["observed_at"],
                   "freshness": raw["observation_freshness"],
                   "coverage": raw["coverage"], "diagnostics": raw["diagnostics"],
                   "sources": _sources(raw, root), "state": raw}
        good = next((entry for entry in (cached, previous)
                     if entry and entry["payload"].get("status") == "available"), None)
        if paths and (raw["status"] == "available" or good is None):
            try:
                _save(paths, payload, scan_started_ns)
            except (OSError, ValueError):
                payload["diagnostics"] = [*payload["diagnostics"], "Shared observation cache unavailable"]
        if raw["status"] == "available":
            return payload
        if good:
            old = _selected_root(good["payload"], root)
            old["status"] = "partial"
            old["freshness"] = {"kind": "stale", "age_seconds": round(max(0, time.time_ns() - good["saved_at_ns"]) / 1e9, 3)}
            old["coverage"] = {**old["coverage"], "latest_scan": raw["coverage"]}
            old["diagnostics"] = [*old["diagnostics"], "Latest source scan partial; retaining last-good observation", *raw["diagnostics"]]
            return old
        return payload
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        good = next((entry for entry in (cached, previous)
                     if entry and entry["payload"].get("status") == "available"), None)
        if good:
            old = _selected_root(good["payload"], root)
            old["status"] = "partial"
            old["freshness"] = {"kind": "stale", "age_seconds": round(max(0, time.time_ns() - good["saved_at_ns"]) / 1e9, 3)}
            old["diagnostics"] = [*old["diagnostics"], "Latest source scan failed: " + str(exc)]
            return old
        return {"status": "unavailable", "repository": {"root": str(root)},
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "freshness": {"kind": "unknown", "age_seconds": None},
                "coverage": {}, "diagnostics": ["Current Work source scan unavailable: " + str(exc)],
                "sources": [], "state": None}


def _base(root, refresh=False):
    require_shared(root)
    from .shared_work import observe
    try:
        shared = observe(root)
    except (OSError, ValueError, TypeError, KeyError, ts.ReadError, subprocess.SubprocessError) as exc:
        return {'status': 'unavailable', 'repository': {'root': str(root)}, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'freshness': {'kind': 'unknown', 'age_seconds': None}, 'coverage': {}, 'diagnostics': ['Shared sources unavailable: ' + str(exc)],
                'sources': [], 'state': None, 'shared_work': {'enabled': True, 'revision': None, 'coverage': 'unknown', 'canonical': None, 'peers': [], 'fetch_state': 'unavailable', 'diagnostics': []}}
    base = _scan_base(root, refresh)
    if shared['enabled'] and base['status'] != 'available':
        shared['coverage'] = 'unknown'
        shared['diagnostics'] = [*shared['diagnostics'], 'Native source coverage is incomplete.']
    base['shared_work'] = shared
    if shared['coverage'] == 'unknown':
        base['status'] = 'partial'
        base['diagnostics'] = [*base['diagnostics'], *shared['diagnostics']]
    return base

def sources(root, *, refresh=False):
    """Source inventory without recursively invoking the knowledge projection."""
    base = _base(Path(root).resolve(strict=True), refresh)
    return {key: deepcopy(base[key]) for key in
            ("status", "repository", "observed_at", "freshness", "coverage", "sources", "diagnostics")}


def _conflict_item(conflict):
    candidates = conflict["candidates"]
    shown = next((row for row in candidates if row.get("record_on_main")), candidates[0])
    result = deepcopy(shown)
    result.update(status="conflict", record_conflict=True,
                  candidate_statuses=sorted({row["status"] for row in candidates}),
                  conflict_candidates=deepcopy(candidates),
                  summary="Incomparable native versions; inspect each candidate before a work decision.",
                  next_action="Inspect and reconcile the exact conflicting native records.")
    return result


def _board(rows, conflicts, work_order, *, basis):
    rows = [row for row in rows if row['type'] != 'attempt']
    conflicts = [row for row in conflicts if not all(c['type'] == 'attempt' for c in row['candidates'])]
    items = [deepcopy(row) for row in rows]
    items.extend(_conflict_item(row) for row in conflicts if row.get("candidates"))
    items.sort(key=lambda item: (item.get("owner_path", ""), item.get("order", 0), item["id"]))
    for item in items:
        if item["type"] == "slice" and item.get("parent_id"):
            item["display_type"] = "stage"
        if item["type"] == "task":
            item["display_type"] = "quick"
        if item["type"] == "campaign":
            children = [child for child in items if child.get("parent_id") == item["id"]
                        and child["type"] in {"stage", "slice"}
                        and "source_import" not in child.get("extensions", {})]
            item["child_counts"] = {"completed": sum(child["status"] == "completed" for child in children),
                                    "total": len(children),
                                    "conflicted": sum(child.get("record_conflict", False) for child in children)}
        native = {key: item[key] for key in NATIVE_KEYS if key in item}
        item["source_revision"] = None if item.get("record_conflict") else work_lifecycle.item_revision(native)
        item["shared_read_only"] = True
        item["work_lane"] = "done" if item["status"] in work_lifecycle.TERMINAL else None
    try:
        selected_order = work_lifecycle.ordered(work_order, items)
    except (TypeError, KeyError, ValueError, ts.ReadError):
        selected_order = work_lifecycle.ordered(None, items)
    selected_order = work_lifecycle.view_order(selected_order, items)
    positions = {identity: (lane, index) for lane in ("current", "next")
                 for index, identity in enumerate(selected_order[lane])}
    for item in items:
        lane, priority = positions.get(item["id"], (None, None))
        if item["work_lane"] is None:
            item["work_lane"] = lane
        item["priority"] = priority
    owner_keys = sorted({(item.get("owner_path"), item.get("owner_sha256")) for item in items
                         if item.get("owner_path") and item.get("owner_sha256")})
    output = {"status": "partial" if conflicts else "available", "items": items,
              "owners": [{"path": path, "sha256": sha, "status": "observed-source"}
                         for path, sha in owner_keys],
              "diagnostics": [], "conflicts": deepcopy(conflicts),
              "work_order": {**selected_order, "configured": work_order is not None,
                             "revision": hashlib.sha256(_encoded(work_order)).hexdigest() if work_order else None,
                             "basis": basis, "derived": True},
              "qualification": "Shared native work is read-only; work status does not establish code delivery or acceptance."}
    return boards_view(output)


def _select(base, view, source):
    if view not in {"current", "main", "checkout", "source"}:
        raise ValueError("Current Work view must be current, main, checkout or source")
    if view == "source":
        if not isinstance(source, str) or not SOURCE_ID.fullmatch(source):
            raise ValueError("Source view requires one returned source ID")
        matches = [row for row in base["sources"] if row["id"] == source]
        if len(matches) != 1:
            raise ValueError("Source ID is unavailable or observation changed")
        return matches[0]
    if source is not None:
        raise ValueError("Source ID is valid only for source view")
    if view == "main":
        return next((row for row in base["sources"] if row["is_main"]), None)
    if view == "checkout":
        return next((row for row in base["sources"] if row["is_checkout"]), None)
    return None


def project(root, *, view="current", source=None, refresh=False, include_knowledge=True):
    """Project shared native work with explicit source and freshness evidence."""
    selected = Path(root).resolve(strict=True)
    base = _base(selected, refresh)
    chosen = _select(base, view, source)
    raw = base["state"]
    if raw is None:
        board = {"status": "unavailable", "items": [], "owners": [], "diagnostics": base["diagnostics"],
                 "conflicts": [], "work_order": {"configured": False, "current": [], "next": [], "revision": None}}
    elif view == "current":
        board = _board(raw["latest"]["items"], raw["latest"]["conflicts"],
                       raw["main"].get("work_order"), basis="observed-main-order-plus-current-records")
    elif view == "main":
        board = _board(raw["main"]["items"], [], raw["main"].get("work_order"), basis="observed-main")
    elif chosen is None or chosen["status"] == "partial":
        board = {"status": "unavailable", "items": [], "owners": [],
                 "diagnostics": ["Selected source was not fully observed"], "conflicts": [],
                 "work_order": {"configured": False, "current": [], "next": [], "revision": None}}
    else:
        try:
            snapshot = repository_state.source_snapshot(selected, chosen)
            expected = tuple(raw["repository"][key] for key in ("repository_id", "scope_id", "namespace"))
            if snapshot["identity"] != expected:
                raise ValueError("Selected source has a different native repository identity")
            main = {item["id"]: item for item in raw["main"]["items"]}
            pin = {"ref": chosen["ref"], "commit": chosen["commit"],
                   "source_kind": chosen["kind"], **({"path": chosen["path"]} if "path" in chosen else {})}
            rows = [repository_state._item_row(item, pin, main.get(item["id"]), "unknown")
                    for item in snapshot["items"].values()]
            board = _board(rows, [], snapshot["work_order"], basis="selected-source")
            board["diagnostics"].extend(snapshot["diagnostics"])
            if snapshot["diagnostics"]:
                board["status"] = "partial"
        except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
            board = {"status": "unavailable", "items": [], "owners": [],
                     "diagnostics": ["Selected source native records unavailable: " + str(exc)],
                     "conflicts": [], "work_order": {"configured": False, "current": [], "next": [], "revision": None}}
    from .shared_attempts import project as attempt_project
    attempt_rows = [] if raw is None else raw['latest']['items'] if view == 'current' else raw['main']['items'] if view == 'main' else locals().get('rows', [])
    attempt_conflicts = raw['latest']['conflicts'] if raw is not None and view == 'current' else []
    result = {"profile": PROFILE,
              'shared_work': deepcopy(base['shared_work']),
              'attempts': attempt_project(attempt_rows, attempt_conflicts, base['shared_work']['coverage'], {k: base['repository'].get(k) for k in ('repository_id', 'scope_id', 'namespace')}),
              "status": base["status"] if board["status"] == "available" else "partial",
              "repository": {**base["repository"], "root": str(selected)},
              "observed_at": base["observed_at"], "freshness": base["freshness"],
              "coverage": deepcopy(base["coverage"]), "sources": deepcopy(base["sources"]),
              "view": view, "selected_source": deepcopy(chosen), "boards": board,
              "diagnostics": [*base["diagnostics"], *board.get("diagnostics", [])],
              "qualification": "Local and cached Git evidence only; native work, code delivery, remote publication and acceptance are separate facts."}
    from .work_attribution import project as attribution_project
    for item in board.get('items', []):
        item['attribution'] = attribution_project(item, result['attempts']['items'])
    if result['attempts']['coverage'] == 'unknown':
        result['diagnostics'].extend(result['attempts']['diagnostics'])
        result['status'] = 'partial'
    try:
        result["storage"] = {"kind": "rebuildable-device-cache", "path": str(_paths(selected)[0]),
                             "max_age_seconds": CACHE_SECONDS}
    except (OSError, ValueError, subprocess.SubprocessError):
        result["storage"] = {"kind": "unavailable", "path": None,
                              "max_age_seconds": CACHE_SECONDS}
    if not include_knowledge:
        return result
    try:
        from . import current_work_knowledge
        knowledge = current_work_knowledge.project(selected, result["sources"],
                                                   view=view, source=source, refresh=refresh)
        result.update({key: knowledge[key] for key in ("ram", "nav", "documents", "measurement") if key in knowledge})
        result["coverage"]["knowledge"] = knowledge.get("coverage", {})
        result["diagnostics"].extend(knowledge.get("diagnostics", []))
        if knowledge.get("status") in {"partial", "unavailable", "error"}:
            result["status"] = "partial"
        elif any(isinstance(result.get(key), dict) and result[key].get("status") in {"partial", "error"}
                 for key in ("ram", "nav", "documents", "measurement")):
            result["status"] = "partial"
    except (ImportError, OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        for key in ("ram", "nav", "documents", "measurement"):
            result[key] = {"status": "unavailable", "reason": "Shared knowledge projection unavailable"}
        result["diagnostics"].append("Shared knowledge projection unavailable: " + str(exc))
        result["status"] = "partial"
    return result


def compact(result):
    """Bounded agent pickup from the same shared Current Work projection."""
    board = result.get("boards", {})
    items = [item for item in board.get("items", [])
             if item.get("status") in {"active", "blocked", "conflict"}
             and (item.get("type") in work_lifecycle.WORK or work_lifecycle.queued(item))]
    def brief(item):
        row = {key: item.get(key) for key in ("id", "type", "title", "status", "owner_path",
                                             "work_lane", "source_integration", "record_conflict",
                                             "source_revision")}
        row["attribution"] = deepcopy(item.get("attribution"))
        if item.get("completion_check_summary"):
            row["completion_check_summary"] = deepcopy(item["completion_check_summary"])
            checks = item.get("extensions", {}).get("completion_checks", [])
            row["completion_checks"] = [
                {key: check.get(key) for key in ("id", "title", "required", "state", "remaining_action")}
                for check in checks[:8]]
            row["completion_checks_omitted"] = max(0, len(checks) - 8)
        row["next_action"] = str(item.get("next_action") or "")[:500]
        row["pins"] = [{key: pin.get(key) for key in ("ref", "commit", "source_kind")}
                       for pin in item.get("provenance", [])[:2]]
        return row
    return {"profile": PROFILE, "status": result.get("status"), "view": result.get("view"),
            'shared_work': result.get('shared_work'), 'attempts': result.get('attempts'),
            "repository": result.get("repository"), "observed_at": result.get("observed_at"),
            "freshness": result.get("freshness"), "coverage": result.get("coverage"),
            "current_row": work_lifecycle.row_summary(board.get("work_order"), board.get("items", [])),
            "current": [brief(item) for item in items[:20]], "current_truncated": len(items) > 20,
            "conflicts": [{"id": conflict["id"], "candidate_count": len(conflict["candidates"])}
                          for conflict in board.get("conflicts", [])[:12]],
            "conflicts_truncated": len(board.get("conflicts", [])) > 12,
            "source_count": len(result.get("sources", [])),
            "storage": result.get("storage"),
            "diagnostics": result.get("diagnostics", [])[:8],
            "qualification": result.get("qualification")}
