"""Disposable cross-platform index for dashboard companion metrics."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .private_scope import require_shared
from . import health

PROFILE = "eutonos.metric-index.v1"
NAME = "metric-index.json"


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(root):
    return Path(root).resolve() / ".eutonos-local" / NAME


def _configuration(root):
    config = root / ".eutonos-local" / "companion-results.json"
    explicit, native = [], None
    if config.is_file():
        try:
            value = json.loads(config.read_text(encoding="utf-8"))
            explicit = [Path(item["path"]) for item in value.get("sources", [])
                        if isinstance(item, dict) and isinstance(item.get("path"), str)]
            candidate = value.get("native_sessions_root")
            native = Path(candidate) if isinstance(candidate, str) and candidate else None
        except (OSError, ValueError, TypeError):
            pass
    return explicit, native


def _watch_files(root):
    explicit, native = _configuration(root)
    selected = list(explicit)
    if native is not None:
        try:
            from .companion_native_sessions import sources
            selected.extend(Path(item["path"]) for item in sources(native, root).get("sources", [])
                            if isinstance(item, dict) and isinstance(item.get("path"), str))
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return sorted({str(path.resolve()) for path in selected if path.is_file()})


def _local_sources(root):
    """Return only local records that contribute to the metric projection."""
    local = root / ".eutonos-local"
    candidates = [
        local / "companion-results.json",
        local / "companion-history.json",
        local / "companion.json",
        local / "settings.json",
        local / "benchmark-suites.json",
    ]
    for directory in (local / "assignments", local / "capture"):
        if directory.is_dir():
            candidates.extend(Path(base) / name for base, _, names in os.walk(directory) for name in names)
    return candidates


def _signature(root, watch=()):
    digest = hashlib.sha256()
    files = {}
    index_path = _path(root).resolve()
    explicit, native = _configuration(root)
    selected_files = [*_local_sources(root), *explicit, *(Path(path) for path in watch)]
    for selected in selected_files:
        try:
            resolved = selected.resolve()
            if not resolved.is_file() or resolved == index_path or resolved.name.startswith("metric-index-"):
                continue
            stat = resolved.stat()
            files[os.path.normcase(str(resolved))] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    # Directory mtimes detect newly created/removed native logs without statting
    # every unrelated session file on every dashboard refresh.
    if native is not None and native.is_dir():
        for base, directories, _ in os.walk(native):
            for directory in [Path(base), *(Path(base) / name for name in directories)]:
                try:
                    stat = directory.stat()
                    key = "directory:" + os.path.normcase(str(directory.resolve()))
                    files[key] = (0, stat.st_mtime_ns)
                except OSError:
                    continue
    total_bytes = 0
    for name, (size, modified) in sorted(files.items()):
        digest.update(name.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0" + str(size).encode("ascii") + b"\0" + str(modified).encode("ascii") + b"\n")
        total_bytes += size
    return {"sha256": digest.hexdigest(), "files": len(files), "bytes": total_bytes}


def _load(root):
    path = _path(root)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (value.get("profile") != PROFILE or not isinstance(value.get("source"), dict)
            or not isinstance(value.get("project"), dict)):
        raise ValueError("Invalid metric index")
    return value


def _write(root, value):
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="metric-index-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def update(root, *, rebuild=False):
    """Refresh the derived index; source records remain authoritative."""
    require_shared(root)
    root = Path(root).resolve()
    current = None if rebuild else _load(root)
    watch = [] if current is None else current.get("watch", [])
    before = _signature(root, watch)
    if current is not None and current["source"] == before:
        return {"profile": PROFILE, "outcome": "current", "generated_at": current["generated_at"],
                "source": before, "runs": len(current["project"].get("runs", [])),
                "advisories": current.get("advisories", [])}
    watch = _watch_files(root)
    before = _signature(root, watch)
    from .companion_results import project as build_project
    advisories = []
    health.add(advisories, "metric-source-file-growth", before["files"], 10000,
               "Metric source inventory crossed 10,000 files; indexing continued.", unit="files")
    health.add(advisories, "metric-source-byte-growth", before["bytes"], 1024 * 1024 * 1024,
               "Metric source inventory crossed 1 GiB; indexing continued.", unit="bytes")
    projected = build_project(root)
    after = _signature(root, watch)
    # Building the projection intentionally advances companion-history.json.
    # The post-build signature therefore becomes the saved source checkpoint.
    value = {"profile": PROFILE, "generated_at": _now(), "source": after, "watch": watch,
             "project": projected, "advisories": advisories}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    health.add(advisories, "metric-index-growth", len(encoded), 64 * 1024 * 1024,
               "Derived metric index crossed 64 MiB; indexing continued.", unit="bytes")
    value["advisories"] = advisories
    _write(root, value)
    return {"profile": PROFILE, "outcome": "rebuilt" if rebuild else "updated",
            "generated_at": value["generated_at"], "source": after,
            "runs": len(projected.get("runs", [])), "advisories": advisories}


def status(root):
    require_shared(root)
    root = Path(root).resolve()
    current = _load(root)
    source = _signature(root, current.get("watch", []) if current else [])
    return {"profile": PROFILE, "outcome": "missing" if current is None else "status",
            "current": current is not None and current["source"] == source,
            "generated_at": current.get("generated_at") if current else None,
            "source": source, "runs": len(current["project"].get("runs", [])) if current else 0,
            "advisories": current.get("advisories", []) if current else []}


def project(root, *, refresh=False):
    require_shared(root)
    root = Path(root).resolve()
    current = _load(root)
    if refresh:
        update(root)
        current = _load(root)
    if current is None:
        from .companion_results import project as build_project
        return build_project(root)
    return current["project"]
