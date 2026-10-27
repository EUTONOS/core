"""Read-only dashboard projections over selected EUTONOS repositories."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.resources import files
import json
from pathlib import Path
import re
import subprocess
from .hidden_process import no_window

from .private_scope import is_private, require_shared
from . import tokenslang as ts
from . import home as eutonos_home
from .effects import LocalRoot, digest
from . import nav
from .release import status as release_status
from .boards import project as boards_project
from .skills import project as skills_project
from .document_views import changes as changes_project, boards_view
from .nav_runner import Session
from .ram import Store as RamStore
from .measurement import Store as MeasurementStore
from .metric_index import project as companion_project
from .delivery_summary import project as delivery_project
from .write_retry import status as write_retry_status
from .companion_attribution import project as attribution_project, receipts as attribution_receipts
from .assignments import records as assignment_records
from . import repo_views
from . import user_tests

PROFILE = "eutonos.tokenslang.dashboard-projection.v1"
MAX_RECORDS = 512
SURFACE_GALLERY = Path(".eutonos-local/dashboard-polish")
SURFACE_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _decode(raw, label):
    ts.require(raw is not None, "missing", f"{label} is absent.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ts.ReadError("invalid", f"{label} is not valid UTF-8 JSON.") from exc


def _read_json(local, path, label=None):
    return _decode(local.read(path), label or path)


def _git(root, *args):
    result = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                            capture_output=True, text=True, timeout=10, **no_window())
    return result.stdout.strip() if result.returncode == 0 else None


def _git_raw(root, *args):
    result = subprocess.run(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
                            capture_output=True, text=True, timeout=10, **no_window())
    return result.stdout if result.returncode == 0 else None


def _repository_state(root):
    worktree_text = _git(root, "worktree", "list", "--porcelain")
    branch_text = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    recent_text = _git(root, "rev-list", "--count", "--since=7 days ago", "--branches")
    current_branch = _git(root, "branch", "--show-current")
    status_text = _git_raw(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = []
    if status_text is not None:
        fields, index = status_text.split("\0"), 0
        while index < len(fields):
            entry = fields[index]
            index += 1
            if len(entry) < 3:
                continue
            code = entry[:2]
            entries.append(code)
            if any(marker in {"R", "C"} for marker in code) and index < len(fields):
                index += 1
    conflicts = sum(code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"} for code in entries)
    untracked = sum(code == "??" for code in entries)
    staged = sum(code[0] not in {" ", "?"} and code not in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
                 for code in entries)
    modified = sum(code[1] not in {" ", "?"} and code not in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
                   for code in entries)
    try:
        recent_commits = int(recent_text) if recent_text is not None else None
    except ValueError:
        recent_commits = None
    return {
        "status": "available" if all(value is not None for value in
                                     (worktree_text, branch_text, recent_text, status_text)) else "partial",
        "worktrees": None if worktree_text is None else sum(
            line.startswith("worktree ") for line in worktree_text.splitlines()),
        "branches": None if branch_text is None else len(branch_text.splitlines()) if branch_text else 0,
        "current_branch": current_branch or ("Detached HEAD" if _git(root, "rev-parse", "HEAD") else None),
        "recent_commits": recent_commits,
        "recent_days": 7,
        "uncommitted": None if status_text is None else len(entries),
        "changes": {"modified": modified, "staged": staged, "untracked": untracked, "conflicts": conflicts},
        "qualification": "Local Git metadata observed at dashboard refresh; branch count is local and recent commits span local refs.",
    }


def _first_paragraph(text, heading=None):
    if heading:
        match = re.search(r"(?m)^##\s+" + re.escape(heading) + r"\s*$", text)
        text = text[match.end():] if match else ""
    for part in re.split(r"\r?\n\s*\r?\n", text):
        clean = re.sub(r"\s+", " ", part.strip())
        if clean and not clean.startswith(("#", "```")):
            return clean
    return ""


def _work_path(catalog):
    ref = catalog.get("owners", {}).get("current_task", {})
    selectors = {key: ref[key] for key in ("resource_id", "path") if ref.get(key)}
    if not selectors or any(ref.get(key, catalog.get(key)) != catalog.get(key) for key in ("scope_id", "namespace")):
        return None
    candidates = [binding for binding in catalog.get("bindings", [])
                  if all(binding.get(key) == value for key, value in selectors.items())]
    if len(candidates) != 1:
        return None
    binding = candidates[0]
    path = binding.get("path")
    same_path = [item for item in catalog.get("bindings", []) if item.get("path") == path]
    if binding.get("kind") != "docs" or not all(item.get("visibility") == "public" and item.get("state") == "active"
                                               and item.get("kind") in {"docs", "source"} for item in same_path):
        return None
    if any(path == root or path.startswith(root + "/") for root in catalog.get("excluded_roots", [])):
        return None
    return path


def _work(local, path="docs/CURRENT_TASK.md"):
    if path is None:
        return {"available": False, "reason": "Current Task owner is not uniquely visible in the catalog."}
    try:
        raw = local.read(path)
    except (OSError, ts.ReadError):
        return {"available": False, "reason": "Configured Current Task source is unavailable."}
    if raw is None:
        return {"available": False, "reason": "Current Task is absent."}
    text = raw.decode("utf-8", errors="replace")
    headings = re.findall(r"(?m)^##\s+(.+?)\s*$", text)
    block = re.search(r"```tokenslang-work\s*\r?\n(.*?)\r?\n```", text, re.S)
    selected = None
    if block:
        try:
            selected = json.loads(block.group(1))
        except json.JSONDecodeError:
            pass
    priority = next((h for h in headings if h.lower().startswith("top priority")), headings[0] if headings else None)
    return {"available": True, "path": path, "heading": priority, "summary": _first_paragraph(text, priority),
            "work_id": selected.get("work_id") if selected else None,
            "goal": selected.get("goal") if selected else None,
            "status": selected.get("status") if selected else None,
            "recorded_on": selected.get("recorded_on") if selected else None,
            "required": selected.get("required", []) if selected else []}


def _architecture(local):
    path = "docs/ARCHITECTURE.md"
    raw = local.read(path)
    if raw is None:
        return {"status": "unavailable", "path": path, "reason": "Canonical architecture document is absent."}
    text = raw.decode("utf-8", errors="replace")
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append({"title": match.group(1).strip(), "detail": text[match.end():end].strip()})
    return {"status": "available", "path": path, "sha256": digest(raw), "title": "EUTONOS architecture",
            "summary": _first_paragraph(text), "sections": sections,
            "qualification": "Registered canonical orientation; section names are projected without inferring component status."}


def _ram(local, root):
    config = _read_json(local, "eutonos.ram.json", "RAM configuration")
    from .ram_relevance import open_store, _eligible
    store = open_store(root)
    base = root / config["store_root"]
    records, diagnostics = [], []
    if not base.exists():
        return {"status": "available", "records": [], "totals": {"records": 0, "words": 0, "bytes": 0}}
    paths = sorted(p for p in base.rglob("*.json") if p.is_file())
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            value = store.load(rel)
            _eligible(store, rel, value, store.root.read)
            if value["controls"]["state"] == "archived":
                continue
            if len(records) >= MAX_RECORDS:
                diagnostics.append("RAM library view is partial; use paged relevance lookup for more eligible records.")
                break
            latest = value["versions"][-1] if value["versions"] else None
            if latest is None:
                continue
            records.append({"identity": value["identity"], "path": rel, "revision": value["revision"],
                            "state": value["controls"]["state"], "visibility": value["controls"]["visibility"],
                            "kind": latest["kind"], "summary": latest["summary"], "unfinished": latest["unfinished"],
                            "recorded_at": latest["recorded_at"], "occurred_at": latest["occurred_at"],
                            "actors": latest["actors"], "sources": latest["sources"],
                            "words": len(re.findall(r"\S+", latest["summary"])), "bytes": path.stat().st_size,
                            "versions": [{"revision": v["revision"], "summary": v["summary"], "recorded_at": v["recorded_at"]}
                                         for v in value["versions"]]})
        except (OSError, ts.ReadError, KeyError, TypeError) as exc:
            if getattr(exc, "code", None) not in {"denied", "stale", "missing"}:
                diagnostics.append(f"{rel}: {getattr(exc, 'message', str(exc))}")
    records.sort(key=lambda item: item["recorded_at"], reverse=True)
    return {"status": "available", "records": records, "diagnostics": diagnostics,
            "totals": {"records": len(records), "words": sum(r["words"] for r in records),
                       "bytes": sum(r["bytes"] for r in records)}}


def _receipt_findings(receipt):
    report = receipt.get("report") or {}
    proposed = report.get("proposedRecords") or []
    raw_dispositions = receipt.get("dispositions") or report.get("leadDispositions") or []
    if isinstance(raw_dispositions, dict):
        disposition_values = list(raw_dispositions.values())
    else:
        disposition_values = [d.get("disposition") for d in raw_dispositions if isinstance(d, dict)]
    return {"summary": receipt.get("summary") or report.get("summary") or "No report summary retained.",
            "inspected_paths": receipt.get("inspectedPaths") or report.get("inspectedPaths") or [],
            "changed_records": receipt.get("proposedRecordIDs") or [p.get("recordID") for p in proposed if p.get("recordID")],
            "dispositions": {name: disposition_values.count(name) for name in sorted(set(filter(None, disposition_values)))},
            "omission_search": receipt.get("omissionSearch") or report.get("omissionSearch")}


def _cycle(value, current=False):
    receipts = value.get("receipts") or []
    return {"run_id": value.get("runID"), "mode": value.get("mode"), "status": value.get("status"),
            "source_commit": value.get("sourceCommit"), "source_fingerprint": value.get("sourceFingerprint"),
            "current": current, "pass_count": len(receipts),
            "sweep_count": len(set(value.get("closedSweeps") or [r.get("sweepID") for r in receipts if r.get("sweepID")])),
            "closed_sweeps": value.get("closedSweeps") or [],
            "unresolved_paths": value.get("debtPaths") or [], "unresolved_records": value.get("debtRecords") or [],
            "passes": [{"index": i + 1, "sweep_id": r.get("sweepID"), "packet_id": r.get("packetID"),
                        **_receipt_findings(r)} for i, r in enumerate(receipts)]}


def _archived_cycle(local, session, item):
    ref = item.get("archive") or {}
    if "legacy_runID" not in ref:
        return _read_json(local, ref["path"], "NAV archived cycle")
    history = _read_json(local, session.history_path, "NAV history")
    from . import nav_history
    legacy = nav_history.load(local, history["legacy"], session.history_path.removesuffix(".json") + "_")
    return next(c for c in legacy["cycles"] if c.get("runID") == ref["legacy_runID"])


def _nav(local):
    cfg = nav.config(local.read("eutonos.nav.json"))
    bodies = {name: local.read(cfg["map_root"] + "/" + name) for name in nav.MAPS}
    records, _ = nav.parse_maps(bodies)
    session = Session(local)
    raw = local.read(session.state_path)
    current = _decode(raw, "NAV current run") if raw else None
    cycles = [_cycle(current, True)] if current else []
    cursor = None
    seen_cursors = set()
    while True:
        cursor_key = json.dumps(cursor, sort_keys=True, separators=(",", ":")) if cursor is not None else "<start>"
        ts.require(cursor_key not in seen_cursors, "invalid", "NAV history continuation repeated; refusing a cyclic read.")
        seen_cursors.add(cursor_key)
        page = session.history(64, cursor)
        for item in page["items"]:
            try:
                cycles.append(_cycle(_archived_cycle(local, session, item)))
            except (ts.ReadError, KeyError, StopIteration) as exc:
                cycles.append({"run_id": item.get("runID"), "status": item.get("status"), "mode": item.get("mode"),
                               "source_commit": item.get("sourceCommit"), "pass_count": item.get("receipts", 0),
                               "passes": [], "detail_unavailable": getattr(exc, "message", str(exc))})
        cursor = page["continuation"]
        if cursor is None:
            break
    by_kind, review = {}, {}
    routes = []
    for record in records.values():
        kind, state = record["fields"]["Kind"], record["fields"]["Review"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        review[state] = review.get(state, 0) + 1
        routes.append({"id": record["id"], "title": record["title"], "kind": kind,
                       "summary": record["fields"]["Summary"], "review": state,
                       "evidence": record["evidence"], "related": record["related"]})
    return {"status": "available", "records": len(records), "by_kind": by_kind, "review": review,
            "routes": routes, "runs": cycles, "run_count": len(cycles),
            "pass_count": sum(c.get("pass_count", 0) for c in cycles), "history_complete": cursor is None}


def _measurements(local, root):
    # Measurement owns selected-store discovery, record qualification and
    # current-control readback. The dashboard only projects that checked result.
    projected = MeasurementStore(local).project(MAX_RECORDS)
    result = projected["summary"]
    coverage = projected["coverage"]
    usage = result.get("exposed_usage") or []
    return {"status": "available" if coverage["collection_mode"] == "metadata" else "off",
            "summary": result, "coverage": coverage, "diagnostics": projected["diagnostics"],
            "token_evidence": {"provider_reported": usage, "payload_counted": [],
                "estimated": [], "unknown": not bool(usage),
                "rule": "Unknown is not zero; payload counts do not establish provider billing."}}


def _host(root):
    parent = eutonos_home.hosts()
    if not parent.is_dir():
        return {"status": "unavailable", "reason": "No local EUTONOS host directory is installed."}
    from .host import read_snapshot
    failures = []
    for directory in sorted(path for path in parent.iterdir() if path.is_dir()):
        try:
            return read_snapshot(root, directory)
        except (OSError, ts.ReadError, ValueError) as exc:
            failures.append(getattr(exc, "message", str(exc)))
    return {"status": "unavailable", "reason": "No enrolled local coordinator matched this repository.",
            "checked_hosts": len(failures)}


def _write_retry(root):
    parent = eutonos_home.hosts()
    if not parent.is_dir():
        return {"status": "not-started", "signal": "green", "label": "Ready", "requests": [], "counts": {}}
    from .host import read_snapshot
    for directory in sorted(path for path in parent.iterdir() if path.is_dir()):
        try:
            read_snapshot(root, directory)
            return write_retry_status(directory, root)
        except (OSError, ts.ReadError, ValueError):
            continue
    return {"status": "not-started", "signal": "green", "label": "Ready", "requests": [], "counts": {}}


def _surface_gallery(root):
    directory = root / SURFACE_GALLERY
    configured = any((root / candidate).exists() for candidate in ("eutonos.visual-atlas.json", "VISUAL_ATLAS"))
    images = []
    if directory.is_dir():
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.casefold() not in SURFACE_IMAGE_TYPES:
                continue
            observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            surface = path.stem.removesuffix("-mobile").replace("-", " ")
            images.append({"id": path.stem, "name": path.name, "title": surface.title(),
                           "observed_at": observed, "variant": "mobile" if path.stem.endswith("-mobile") else "desktop",
                           "style_path": "docs/style/surfaces/DASHBOARD.md"})
    if images or configured:
        return {"status": "available", "sources": [directory.as_posix()] if images else [], "items": images,
                "records": len(images), "message": None,
                "qualification": "Dated local screenshots demonstrate appearance only; native sources remain authoritative."}
    return {"status": "not-installed", "sources": [], "items": [], "records": 0,
            "message": "No retained dashboard surface screenshots are available in this repository."}


def surface_image(root, name):
    require_shared(root)
    root = Path(root).resolve(strict=True)
    ts.require(isinstance(name, str) and name == Path(name).name and name not in {"", ".", ".."},
               "invalid", "Select one retained surface image name.")
    path = (root / SURFACE_GALLERY / name).resolve(strict=True)
    directory = (root / SURFACE_GALLERY).resolve(strict=True)
    ts.require(path.parent == directory and path.suffix.casefold() in SURFACE_IMAGE_TYPES,
               "invalid", "Selected surface image is outside the retained gallery.")
    return path.read_bytes(), SURFACE_IMAGE_TYPES[path.suffix.casefold()]


def _optional_modules(root):
    result = {}
    for name, candidates in {"atlas": ("eutonos.atlas.json", "ATLAS")}.items():
        found = [candidate for candidate in candidates if (root / candidate).exists()]
        if found:
            result[name] = {"status": "available", "sources": found, "runs": [], "message": None}
        elif name == "atlas":
            result[name] = {"status": "not-configured", "sources": [], "runs": [],
                            "message": "No ATLAS engine, index, or run adapter is configured in this repository. The release inventory separately reports the read CLI."}
    result["visual_atlas"] = _surface_gallery(root)
    return result


def current_view(root, *, view="current", source=None, refresh=False, include_knowledge=True):
    """One shared read model for the lightweight feed and full dashboard."""
    from . import current_work
    value = current_work.project(root, view=view, source=source, refresh=refresh, include_knowledge=include_knowledge)
    modules = {"current_work": {key: value.get(key) for key in
        ("profile", "status", "repository", "view", "source", "observed_at",
         "freshness", "coverage", "sources", "diagnostics", "storage", "shared_work", "attempts")}}
    keys = ["boards", *(["ram", "nav", "documents"] if include_knowledge else [])]
    usable = any(row.get("status") in {"observed", "available", "equivalent-ref", "already-on-main"}
                 for row in value.get("sources", []))
    local_fallback = view == 'current' and not usable and not value.get('shared_work', {}).get('enabled')
    local = LocalRoot(root) if local_fallback else None
    fallbacks = {"boards": lambda: boards_view(boards_project(root)), "ram": lambda: _ram(local, Path(root)),
                 "nav": lambda: _nav(local), "documents": lambda: {"status": "unavailable", "items": []}}
    for name in keys:
        if local_fallback:
            try:
                module = fallbacks[name]()
                module["source_scope"] = "selected-checkout-fallback"
            except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
                module = {"status": "unavailable", "reason": str(exc)}
        else:
            module = value.get(name, {"status": "unavailable", "reason": "Source not observed"})
        modules[name] = boards_view(module) if name == "boards" else module
    if include_knowledge:
        modules["current_work"]["knowledge"] = {"observed_at": value.get("observed_at"),
            "coverage": value.get("coverage", {}).get("knowledge", {}),
            "diagnostics": value.get("diagnostics", [])}
    if isinstance(value.get("measurement"), dict):
        modules["current_work"]["measurement_scope"] = value["measurement"]
    return {"modules": modules, "shared_modules": keys, "data_scope": {
        "view": "checkout-fallback" if local_fallback else view, "source": source,
        "shared": [] if local_fallback else keys,
        "runtime": "Usage, sessions, settings and host state remain selected-device observations; branch copies are not added together.",
        "remote": "Other developers' published work appears after Git fetch; unpushed work is local."}}



SUMMARY_DEFERRED = ("ram", "delivery", "write_retry", "companion",
                    "attribution", "repository_state", "repo_metrics", "repo_health", "documents")
LAZY_MODULES = ("delivery", "write_retry", "companion", "attribution",
                "repository_state", "repo_metrics", "repo_health")
_COMPANION_HEAVY = ("runs", "capture", "benchmark_suites", "experiments")


def _deferred(**extra):
    return {"status": "deferred", "deferred": True, **extra}


def _companion_summary(value):
    """Companion module without its multi-megabyte run, capture and timeline bodies."""
    result = {key: item for key, item in value.items() if key not in _COMPANION_HEAVY}
    lifetime = value.get("lifetime_metrics")
    if isinstance(lifetime, dict):
        result["lifetime_metrics"] = {key: item for key, item in lifetime.items() if key not in {"timeline", "model_roles"}}
    result["deferred"] = True
    result["deferred_fields"] = [key for key in _COMPANION_HEAVY if key in value] + ["lifetime_metrics.timeline", "lifetime_metrics.model_roles"]
    return result


def lazy_module(root, name, *, refresh_state=False):
    """Compute one section that the summary snapshot defers. Returns {module name: module}."""
    require_shared(root)
    ts.require(name in LAZY_MODULES, "invalid", "Unknown deferred dashboard section.")
    root = Path(root).resolve(strict=True)
    local = LocalRoot(root)

    def guard(reader):
        try:
            return reader()
        except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
            return {"status": "error", "reason": getattr(exc, "message", str(exc))}

    if name == "delivery":
        return {"delivery": guard(lambda: delivery_project(root))}
    if name == "write_retry":
        return {"write_retry": guard(lambda: _write_retry(root))}
    if name == "companion":
        return {"companion": guard(lambda: companion_project(root))}
    if name == "attribution":
        def build():
            companion = companion_project(root)
            return attribution_project(assignment_records(root), companion.get("runs", []),
                                       receipts=attribution_receipts(root), comparisons=companion.get("comparisons", []))
        return {"attribution": guard(build)}
    if name == "repository_state":
        def build_state():
            from . import repository_state
            return repository_state.project(root, refresh=refresh_state)
        return {"repository_state": guard(build_state)}
    if name == "repo_metrics":
        return {"repo_metrics": guard(lambda: repo_views.metrics(root))}
    modules = {"repo_metrics": guard(lambda: repo_views.metrics(root)), "write_retry": guard(lambda: _write_retry(root)),
               "boards": guard(lambda: boards_view(boards_project(root))), "nav": guard(lambda: _nav(local))}
    partial = {"modules": modules, "repository": guard(lambda: _repository_state(root)),
               "release": guard(lambda: release_status(root))}
    health = guard(lambda: repo_views.health(root, partial))
    return {"repo_health": health, "repo_metrics": modules["repo_metrics"], "write_retry": modules["write_retry"]}


def project(root, *, refresh_state=False, work_view="current", work_source=None, detail="full"):
    """detail="summary" defers heavy sections to lazy_module() and the Current Work knowledge read."""
    summary = detail == "summary"
    require_shared(root)
    root = Path(root).resolve(strict=True)
    local = LocalRoot(root)
    catalog = _read_json(local, "eutonos.read.json", "Hub catalog")
    # Summary reads overlap their independent process, SQLite and file waits.
    pool = ThreadPoolExecutor(max_workers=3) if summary else None
    if pool:
        view_future = pool.submit(current_view, root, view=work_view, source=work_source, refresh=refresh_state,
                                  include_knowledge=False)
        host_future, release_future = pool.submit(_host, root), pool.submit(release_status, root)
        pool.shutdown(wait=False)
    commit, status = _git(root, "rev-parse", "HEAD"), _git(root, "status", "--porcelain=v1", "--untracked-files=no")
    result = {"profile": PROFILE, "generated_at": _now(), "repository_id": catalog["repository_id"],
              "scope_id": catalog["scope_id"], "namespace": catalog["namespace"], "name": root.name,
              "root": str(root), "source": {"kind": "local-worktree", "commit": commit,
              "dirty": None if status is None else bool(status), "local_overlay": "observed"},
              "repository": _repository_state(root), "release": release_future.result() if pool else release_status(root),
              "work": _work(local, _work_path(catalog)), "modules": {}, "host": host_future.result() if pool else _host(root), "diagnostics": []}
    readers = (("skills", lambda: skills_project(root)), ("changes", lambda: changes_project(root)), ("boards", lambda: boards_view(boards_project(root))), ("architecture", lambda: _architecture(local)), ("delivery", lambda: delivery_project(root)), ("write_retry", lambda: _write_retry(root)), ("ram", lambda: _ram(local, root)), ("nav", lambda: _nav(local)),
               ("measurement", lambda: _measurements(local, root)),
               ("companion", lambda: companion_project(root)),
               ("user_tests", lambda: {"status": "available", **user_tests.project_tests(root)}))
    for name, reader in readers:
        if summary and name in SUMMARY_DEFERRED and name != "companion":
            result["modules"][name] = _deferred(**({"totals": {}, "records": []} if name == "ram" else {}))
            continue
        try:
            value = reader()
            result["modules"][name] = _companion_summary(value) if summary and name == "companion" else value
        except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
            result["modules"][name] = {"status": "error", "reason": getattr(exc, "message", str(exc))}
    if summary:
        for name in ("repository_state", "attribution"):
            result["modules"][name] = _deferred()
    else:
        try:
            from . import repository_state
            result["modules"]["repository_state"] = repository_state.project(root, refresh=refresh_state)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["modules"]["repository_state"] = {"status": "unavailable", "reason": str(exc)}
        try:
            companion = result["modules"].get("companion", {})
            result["modules"]["attribution"] = attribution_project(
                assignment_records(root), companion.get("runs", []),
                receipts=attribution_receipts(root), comparisons=companion.get("comparisons", []))
        except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
            result["modules"]["attribution"] = {"status": "error", "reason": getattr(exc, "message", str(exc))}
    try:
        shared = view_future.result() if pool else current_view(
            root, view=work_view, source=work_source, refresh=refresh_state)
        # Runtime observations retain their native scope. Route maps can share
        # records while the selected device's execution history remains local.
        if "nav" in shared["modules"]:
            local_nav = result["modules"].get("nav", {})
            for key in ("runs", "run_count", "pass_count", "history_complete"):
                shared["modules"]["nav"][key] = local_nav.get(key)
        measurement = shared["modules"].pop("measurement", None)
        if measurement is not None:
            shared["modules"]["current_work"]["measurement_scope"] = measurement
        result["modules"].update(shared["modules"])
        result["data_scope"] = shared["data_scope"]
        if (shared["modules"].get("boards", {}).get("status") in {"available", "partial"}
                and shared["data_scope"]["view"] != "checkout-fallback"):
            result["checkout_work"] = result["work"]
            result["work"] = {"path": None, "heading": "Current Work", "status": "observed",
                              "summary": "Combined recorded work; integration is shown separately."}
        elif shared["data_scope"]["view"] == "checkout-fallback":
            result["data_scope"]["boards"] = "selected-checkout-fallback"
            result["data_scope"]["reason"] = "Combined board unavailable; selected local records retained."
        else:
            result["work"] = {"status": "unavailable", "summary": "Selected work source could not be observed."}
    except (OSError, ValueError, TypeError, KeyError, ts.ReadError) as exc:
        result["modules"]["current_work"] = {"status": "unavailable", "diagnostics": [str(exc)]}
        result["data_scope"] = {"view": "checkout-fallback" if work_view == "current" else work_view, "reason": str(exc)}
        if work_view != "current":
            for name in ("boards", "ram", "nav", "documents"):
                result["modules"][name] = {"status": "unavailable", "reason": str(exc), "items": [], "records": []}
    if summary:
        result["modules"].setdefault("documents", _deferred(items=[]))
    result["modules"].update(_optional_modules(root))
    if summary:
        result["modules"]["repo_metrics"] = _deferred()
        result["modules"]["repo_health"] = _deferred()
    else:
        try:
            result["modules"]["repo_metrics"] = repo_views.metrics(root)
        except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
            result["modules"]["repo_metrics"] = {"status": "error", "reason": getattr(exc, "message", str(exc))}
        try:
            result["modules"]["repo_health"] = repo_views.health(root, result)
        except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
            result["modules"]["repo_health"] = {"status": "error", "reason": getattr(exc, "message", str(exc))}
    for name in ("docs", "hub", "tokenslang", "communication", "coordination"):
        result["modules"][name] = {"status": "available"}
    return result


def _private_root(root):
    try:
        require_shared(root)
        return False
    except ts.ReadError:
        return True


def snapshot(roots, *, refresh_state=False, work_view="current", work_source=None, work_project=None, detail="full"):
    projects, diagnostics, shared_repositories = [], [], []
    from . import shared_work
    for index, selected in enumerate(roots):
        if _private_root(selected):
            projects.append({"status": "denied", "unavailable_project": True, "modules": {}})
            diagnostics.append({"status": "denied", "reason": "Private scope is unavailable to shared operations."})
            continue
        if detail == "summary" and work_project is not None and index != work_project:
            # Only the selected project is read; the page asks again when another is chosen.
            projects.append({"name": Path(selected).name, "root": str(selected), "deferred_project": True, "modules": {}})
            continue
        try: projects.append(project(selected, refresh_state=refresh_state,
                                     work_view=work_view if work_project in (None, index) else "current",
                                     work_source=work_source if work_project in (None, index) else None,
                                     detail=detail))
        except (OSError, ts.ReadError, KeyError, TypeError, ValueError) as exc:
            diagnostics.append({"root": str(selected), "reason": getattr(exc, "message", str(exc))})
            projects.append({"name": Path(selected).name, "root": str(selected), "unavailable_project": True, "modules": {}})
    # Read after selected project construction, which may have completed a due fetch.
    for index, selected in enumerate(roots):
        # Reuse the selected-root roster. Status reads never fetch other roots.
        if _private_root(selected):
            shared_repositories.append({'project': index, 'status': 'denied'})
            continue
        row = {'project': index, 'name': Path(selected).name, 'root': str(selected)}
        try:
            row.update(status='available', configuration=shared_work.load(selected),
                       observation=shared_work.observe(selected, fetch=False))
        except (OSError, ValueError, TypeError, KeyError, ts.ReadError, subprocess.SubprocessError) as exc:
            row.update(status='unavailable', reason=str(exc))
        shared_repositories.append(row)
    return {"profile": PROFILE, "generated_at": _now(), "coverage": "selected-roots-only",
            "projects": projects, "shared_repositories": shared_repositories, "diagnostics": diagnostics}


OFFERS_PROFILE = "eutonos.offers.v1"
OFFER_SECTIONS = (("feature", "current"), ("feature", "future"), ("capability", "current"), ("capability", "future"))
OFFER_STATUSES = {"completed", "planned", "deferred", "in progress"}


def offers():
    """EUTONOS's own four lists, read from the file shipped inside the package (never from an open project).

    The file is generated by ``tools/build_offers.py``; a missing or malformed copy is refused in plain words
    rather than shown half-read."""
    try:
        data = json.loads(files("eutonos").joinpath("offers/offers.json").read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise ts.ReadError("unavailable", "The list of what EUTONOS offers is missing or damaged in this installation.") from exc
    items = data.get("items") if isinstance(data, dict) else None
    ts.require(isinstance(data, dict) and data.get("profile") == OFFERS_PROFILE and isinstance(items, list),
               "invalid", "The list of what EUTONOS offers is not in a form this version understands.")
    seen = set()
    for item in items:
        ts.require(isinstance(item, dict) and all(isinstance(item.get(key), str) and item[key]
                                                  for key in ("id", "type", "title", "summary", "category", "status", "horizon"))
                   and (item["type"], item["horizon"]) in OFFER_SECTIONS and item["status"] in OFFER_STATUSES
                   and item["id"] not in seen
                   and (item["type"] == "capability") == ("features" in item)
                   and all(isinstance(name, str) for name in item.get("features", [])),
                   "invalid", "The list of what EUTONOS offers has a record this version cannot read.")
        seen.add(item["id"])
    return data


def static(name):
    ts.require(name in {"index.html", "app.css", "brand.css", "mobile.css", "app.js", "appearance.js", "companion.js", "user_tests.js", "companion.css", "repo_views.js", "repo_views.css", "eutonos-epsilon-32.png", "eutonos-epsilon-64.png"},
               "denied", "Unknown dashboard asset.")
    asset_root = "dashboard_assets/" if name.endswith(".png") else "dashboard_static/"
    return files("eutonos").joinpath(asset_root + name).read_bytes()
