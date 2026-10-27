"""Bounded, read-only view of native work across one shared Git repository.

Refs are local observations, including cached remote-tracking refs.  This module
never fetches, writes Git, or treats a native completion as product acceptance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from .hidden_process import no_window
import time

from .private_scope import is_private, require_shared
from . import boards, tokenslang as ts, work_lifecycle
from .source import safe_path

PROFILE = "eutonos.repository-state.v1"
_CACHE = {}
_CACHE_SECONDS = 45
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}\Z")
_IDENTITY = ("repository_id", "scope_id", "namespace")
_TERMINAL = frozenset({"completed", "cancelled", "superseded"})


class _Limit(ValueError):
    pass


class _Reader:
    def __init__(self, root, max_bytes, deadline):
        self.root = root
        self.max_bytes = max_bytes
        self.used = 0
        self.deadline = deadline
        self.commands = 0
        self.blobs = {}
        self.trees = {}
        self.commit_trees = {}
        self.snapshots = {}
        self.batch = None
        self.graph = None
        self.topology = None
        self.ancestor_cache = {}
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        self.env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                        GIT_NO_LAZY_FETCH="1", GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")

    def git(self, where, *args, missing=False, cap=1024 * 1024):
        if self.commands >= 1024 or time.monotonic() >= self.deadline:
            raise _Limit("Git command/time budget exhausted")
        self.commands += 1
        allowed = min(cap, self.max_bytes - self.used)
        if allowed <= 0:
            raise _Limit("Total read-byte budget exhausted")
        command = ["git", "-c", "safe.directory=" + str(where),
                   "-c", "core.fsmonitor=false", "-c", "core.quotepath=false",
                   "-C", str(where), *args]
        remaining = max(0.1, min(5.0, self.deadline - time.monotonic()))
        try:
            with subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window()) as process:
                # communicate(timeout) has no byte cap, so stream one bounded chunk.
                import threading
                chunks = []
                thread = threading.Thread(target=lambda: chunks.append(process.stdout.read(allowed + 1)), daemon=True)
                thread.start()
                thread.join(remaining)
                if thread.is_alive() or (chunks and len(chunks[0]) > allowed):
                    process.kill()
                    process.wait()
                    thread.join(1)
                    raise _Limit("Git output/time bound exceeded")
                process.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise _Limit("Git timeout") from exc
        except OSError as exc:
            raise ValueError("Git read unavailable") from exc
        data = chunks[0] if chunks else b""
        self.used += len(data)
        if process.returncode != 0:
            if missing:
                return None
            raise ValueError("Git read failed: " + args[0])
        return data

    def close(self):
        if self.batch is not None:
            try:
                if self.batch.poll() is None:
                    self.batch.kill()
                self.batch.wait(timeout=1)
            finally:
                if self.batch.stdin:
                    self.batch.stdin.close()
                if self.batch.stdout:
                    self.batch.stdout.close()
            self.batch = None

    def __del__(self):
        self.close()

    def object(self, oid, expected):
        if not _SHA.fullmatch(oid) or time.monotonic() >= self.deadline:
            raise _Limit("Invalid object ID or total observation time exhausted")
        if oid in self.blobs:
            kind, body = self.blobs[oid]
            if kind != expected:
                raise ValueError("Unexpected Git object type")
            return body
        if self.batch is None:
            command = ["git", "-c", "safe.directory=" + str(self.root),
                       "-c", "core.fsmonitor=false", "-C", str(self.root), "cat-file", "--batch"]
            self.batch = subprocess.Popen(command, env=self.env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window())
            self.commands += 1
        import threading
        batch = self.batch
        packet, errors = [], []
        def read_packet():
            try:
                header = batch.stdout.readline(160).strip().split()
                if len(header) != 3 or header[0] != oid.encode("ascii") or header[1] != expected.encode("ascii"):
                    raise ValueError("Git batch object unavailable or wrong type")
                size = int(header[2])
                if size > 512 * 1024 or size > self.max_bytes - self.used:
                    raise _Limit("Git object/total byte bound exceeded")
                packet.append((batch.stdout.read(size), batch.stdout.read(1), size))
            except Exception as exc:
                errors.append(exc)
        try:
            self.batch.stdin.write((oid + "\n").encode("ascii"))
            self.batch.stdin.flush()
        except OSError as exc:
            raise ValueError("Git batch request failed") from exc
        thread = threading.Thread(target=read_packet, daemon=True)
        thread.start()
        thread.join(max(0.01, min(2.0, self.deadline - time.monotonic())))
        if thread.is_alive():
            self.close()
            thread.join(1)
            raise _Limit("Git batch object timed out")
        if errors:
            raise errors[0]
        body, separator, size = packet[0]
        if len(body) != size or separator != b"\n":
            raise ValueError("Incomplete Git batch object")
        self.used += len(body)
        self.blobs[oid] = (expected, body)
        return body

    def oid(self, commit, path, max_files):
        safe_path(path)
        if commit not in self.commit_trees:
            raw = self.object(commit, "commit")
            head = raw.split(b"\n", 1)[0]
            if not head.startswith(b"tree "):
                raise ValueError("Commit tree unavailable")
            self.commit_trees[commit] = head[5:].decode("ascii")
        tree_oid = self.commit_trees[commit]
        for index, part in enumerate(path.split("/")):
            if tree_oid not in self.trees:
                raw = self.object(tree_oid, "tree")
                entries = {}
                offset = 0
                oid_bytes = len(tree_oid) // 2
                while offset < len(raw):
                    nul = raw.find(b"\0", offset)
                    if nul < 0 or nul + 1 + oid_bytes > len(raw):
                        raise ValueError("Malformed Git tree")
                    mode_name = raw[offset:nul]
                    mode, name = mode_name.split(b" ", 1)
                    entries[name.decode("utf-8", "strict")] = (mode, raw[nul + 1:nul + 1 + oid_bytes].hex())
                    if len(entries) > max_files:
                        raise _Limit("Git tree entry bound exceeded")
                    offset = nul + 1 + oid_bytes
                self.trees[tree_oid] = entries
            entry = self.trees[tree_oid].get(part)
            if entry is None:
                return None
            mode, child = entry
            if index == len(path.split("/")) - 1:
                return child if mode in {b"100644", b"100755"} else None
            if mode != b"40000":
                return None
            tree_oid = child
        return None

    def blob(self, oid):
        return self.object(oid, "blob")

    def stream_blob(self, oid, consume):
        """Stream one pinned blob through the batch pipe without retaining its body."""
        if not _SHA.fullmatch(oid) or time.monotonic() >= self.deadline:
            raise _Limit("Invalid object ID or total observation time exhausted")
        if self.batch is None:
            if self.commands >= 1024:
                raise _Limit("Git command budget exhausted")
            command = ["git", "-c", "safe.directory=" + str(self.root),
                       "-c", "core.fsmonitor=false", "-C", str(self.root), "cat-file", "--batch"]
            self.batch = subprocess.Popen(command, env=self.env, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **no_window())
            self.commands += 1
        batch = self.batch
        errors = []

        def stream():
            try:
                header = batch.stdout.readline(160).strip().split()
                if len(header) != 3 or header[0] != oid.encode("ascii") or header[1] != b"blob":
                    raise ValueError("Git batch blob unavailable or wrong type")
                remaining = int(header[2])
                if remaining < 0:
                    raise ValueError("Invalid Git blob size")
                while remaining:
                    if time.monotonic() >= self.deadline:
                        raise _Limit("Document observation time bound exceeded")
                    chunk = batch.stdout.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("Incomplete Git batch blob")
                    consume(chunk)
                    remaining -= len(chunk)
                if batch.stdout.read(1) != b"\n":
                    raise ValueError("Incomplete Git batch blob separator")
            except Exception as exc:
                errors.append(exc)

        try:
            batch.stdin.write((oid + "\n").encode("ascii"))
            batch.stdin.flush()
        except OSError as exc:
            self.close()
            raise ValueError("Git batch request failed") from exc
        import threading
        thread = threading.Thread(target=stream, daemon=True)
        thread.start()
        thread.join(max(0.01, self.deadline - time.monotonic()))
        if thread.is_alive():
            self.close()
            thread.join(1)
            raise _Limit("Document Git batch timed out")
        if errors:
            self.close()
            raise errors[0]

    def load_graph(self, extra_commits=()):
        raw = self.git(self.root, "rev-list", "--parents", "--topo-order", "--all",
                       *extra_commits,
                       cap=2 * 1024 * 1024)
        graph, topology = {}, []
        for line in raw.decode("ascii", "strict").splitlines():
            parts = line.split()
            if not parts or any(not _SHA.fullmatch(part) for part in parts):
                raise ValueError("Malformed Git parent graph")
            graph[parts[0]] = parts[1:]
            topology.append(parts[0])
            if len(graph) > 40000:
                raise _Limit("Git parent graph count bound exceeded")
        self.graph, self.topology = graph, topology

    def ancestors(self, commit):
        if commit in self.ancestor_cache:
            return self.ancestor_cache[commit]
        if self.graph is None or commit not in self.graph:
            raise ValueError("Commit missing from bounded Git parent graph")
        found, stack = set(), [commit]
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(self.graph.get(current, ()))
            if len(found) > 40000:
                raise _Limit("Git ancestry count bound exceeded")
        self.ancestor_cache[commit] = found
        return found

    def is_ancestor(self, older, newer):
        return older in self.ancestors(newer)

    def merge_base(self, left, right):
        common = self.ancestors(left) & self.ancestors(right)
        if not common:
            raise ValueError("No common Git ancestor with main")
        maxima = []
        for commit in self.topology:
            if commit in common and not any(self.is_ancestor(commit, kept) for kept in maxima):
                maxima.append(commit)
                if len(maxima) > 1:
                    raise ValueError("Multiple incomparable Git merge bases")
        return maxima[0]

    def file(self, root, path):
        safe_path(path)
        target = root
        for part in path.split("/"):
            target = target / part
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Source path is a link/reparse point")
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 or info.st_size > self.max_bytes - self.used:
            raise _Limit("Source file size/type bound exceeded")
        data = target.read_bytes()
        self.used += len(data)
        if self.used > self.max_bytes or target.stat().st_mtime_ns != info.st_mtime_ns:
            raise _Limit("Source changed or total read-byte bound exceeded")
        return data


class SourceReader:
    """One bounded, commit-pinned batch reader for verified source descriptors.

    Callers select descriptors from ``project(...)["sources"]``. This reader
    performs no ref resolution or network access after construction.
    """

    def __init__(self, root, *, max_bytes=64 * 1024 * 1024, time_budget=35,
                 max_files=40000):
        require_shared(root)
        self.root = Path(root).resolve(strict=True)
        if any(type(value) is not int or value < 1 for value in (max_bytes, time_budget, max_files)):
            raise ValueError("Source bounds must be positive integers")
        self.reader = _Reader(self.root, max_bytes, time.monotonic() + time_budget)
        self.max_files = max_files
        self._checked_worktrees = set()
        top = self.reader.git(self.root, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
        if Path(top).resolve() != self.root:
            self.reader.close()
            raise ValueError("Source root must be the exact Git checkout")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.reader.close()

    def _pin(self, source):
        commit = source.get("commit") if isinstance(source, dict) else None
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise ValueError("Source commit pin is invalid")
        _require_public_commit(self.reader, commit, self.max_files)
        kind = source.get("kind", source.get("source_kind"))
        if kind != "working-tree":
            if kind not in {"local-ref", "cached-remote-ref"}:
                raise ValueError("Source kind is invalid")
            return commit, None
        path = source.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("Working-tree source path is missing")
        require_shared(path)
        overlay = Path(path).resolve(strict=True)
        for parent in (overlay, *overlay.parents):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Working-tree source has a linked/reparse parent")
        pin = (str(overlay), commit)
        if pin not in self._checked_worktrees:
            top = self.reader.git(overlay, "rev-parse", "--show-toplevel", cap=1024).decode("utf-8", "strict").strip()
            if Path(top).resolve() != overlay:
                raise ValueError("Working-tree source root changed; refresh source inventory")
            head = self.reader.git(overlay, "rev-parse", "HEAD", cap=128).decode("ascii", "strict").strip()
            if head != commit:
                raise ValueError("Working-tree source HEAD changed; refresh source inventory")
            self._checked_worktrees.add(pin)
        return commit, overlay

    def read(self, source, path):
        safe_path(path)
        commit, overlay = self._pin(source)
        if overlay is not None:
            try:
                return self.reader.file(overlay, path)
            except FileNotFoundError:
                return None
        oid = self.reader.oid(commit, path, self.max_files)
        return self.reader.blob(oid) if oid else None

    def document(self, source, path, *, offset=None, max_bytes=131072):
        """Hash and validate a UTF-8 document, retaining only one requested page.

        ``offset=None`` returns metadata only. Page offsets count UTF-8 bytes and
        must start at a character boundary. A page can exceed max_bytes by at
        most three bytes to include one complete Unicode character.
        """
        safe_path(path)
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError("Document offset must be a nonnegative integer")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 131072:
            raise ValueError("Document page size must be 1..131072 bytes")
        commit, overlay = self._pin(source)
        sha = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        total = 0
        captured = bytearray()

        def consume(chunk):
            nonlocal total
            decoder.decode(chunk)
            sha.update(chunk)
            if offset is not None:
                start = max(offset, total)
                end = min(offset + max_bytes + 3, total + len(chunk))
                if end > start:
                    captured.extend(chunk[start - total:end - total])
            total += len(chunk)

        if overlay is None:
            oid = self.reader.oid(commit, path, self.max_files)
            if oid is None:
                return None
            self.reader.stream_blob(oid, consume)
        else:
            target = overlay
            try:
                for part in path.split("/"):
                    target = target / part
                    info = target.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError("Source path is a link/reparse point")
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Document source must be a regular file")
            with target.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("Document source changed during open")
                while True:
                    if time.monotonic() >= self.reader.deadline:
                        raise _Limit("Document observation time bound exceeded")
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    consume(chunk)
                after = os.fstat(stream.fileno())
            latest = target.stat()
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if (identity(info) != identity(opened) or identity(info) != identity(after)
                    or identity(info) != identity(latest) or info.st_ctime_ns != latest.st_ctime_ns
                    or total != info.st_size):
                raise _Limit("Document source changed during observation")
        decoder.decode(b"", final=True)
        result = {"sha256": sha.hexdigest(), "total_bytes": total}
        if offset is None:
            return result
        if offset > total:
            raise IndexError("Document offset exceeds source length")
        try:
            # final=False allows the bounded capture to end inside a character.
            content = codecs.getincrementaldecoder("utf-8")("strict").decode(bytes(captured), final=False)
        except UnicodeDecodeError as exc:
            raise IndexError("Document offset splits a UTF-8 character") from exc
        size = 0
        boundary = 0
        for char in content:
            width = len(char.encode("utf-8"))
            if size + width > max_bytes and boundary:
                break
            size += width
            boundary += 1
            if size >= max_bytes:
                break
        content = content[:boundary]
        next_offset = offset + size
        return {**result, "content": content, "offset": offset,
                "next_offset": None if next_offset == total else next_offset,
                "eof": next_offset == total}

    def list(self, source, prefix):
        """List regular files below one portable prefix, with a hard count cap."""
        safe_path(prefix)
        commit, overlay = self._pin(source)
        if overlay is None:
            raw = self.reader.git(self.root, "ls-tree", "-r", "-z", "--name-only",
                                  commit, "--", prefix, cap=2 * 1024 * 1024)
            paths = [part.decode("utf-8", "strict") for part in raw.split(b"\0") if part]
            if len(paths) > self.max_files:
                raise _Limit("Source file count bound exceeded")
            return [safe_path(path) for path in paths if path == prefix or path.startswith(prefix + "/")]
        base = overlay / Path(prefix)
        if not base.exists():
            return []
        target = overlay
        for part in prefix.split("/"):
            target = target / part
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Source listing path is linked/reparse")
        if not base.is_dir():
            return []
        paths = []
        for directory, dirs, files in os.walk(base, followlinks=False):
            if time.monotonic() >= self.reader.deadline:
                raise _Limit("Source listing time bound exceeded")
            here = Path(directory)
            dirs[:] = [name for name in dirs if not (here / name).is_symlink()
                       and not getattr((here / name).lstat(), "st_file_attributes", 0) & 0x400]
            for name in files:
                target = here / name
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    continue
                paths.append(safe_path(target.relative_to(overlay).as_posix()))
                if len(paths) > self.max_files:
                    raise _Limit("Source file count bound exceeded")
        return sorted(paths)


def source_snapshot(root, source):
    """Read full native board items and work order for one enumerated source."""
    with SourceReader(root) as batch:
        commit, overlay = batch._pin(source)
        return deepcopy(_snapshot(batch.reader, commit, batch.max_files, overlay))


def _identity(raw):
    value = json.loads(raw.decode("utf-8"))
    result = tuple(value.get(key) for key in _IDENTITY)
    if not all(isinstance(part, str) and part for part in result):
        raise ValueError("Native repository identity is missing")
    return result


def _private_commit(reader, commit, max_files):
    # Populate the pinned top-level tree; presence includes any marker type.
    reader.oid(commit, "eutonos.personal.json", max_files)
    return "eutonos.personal.json" in reader.trees[reader.commit_trees[commit]]


def _require_public_commit(reader, commit, max_files):
    if _private_commit(reader, commit, max_files):
        raise ts.ReadError("denied", "Private scope is unavailable to shared operations.")


def shared_cached_sources(root, sources):
    """Recheck privacy on cache reuse; marker creation must revoke old views."""
    try:
        with SourceReader(root) as reader:
            for source in sources:
                commit = source.get("commit") if isinstance(source, dict) else None
                if not isinstance(commit, str) or not _SHA.fullmatch(commit):
                    return False
                _require_public_commit(reader.reader, commit, reader.max_files)
                if source.get("kind", source.get("source_kind")) == "working-tree":
                    # HEAD changes retain honest cache age; privacy changes revoke it.
                    require_shared(source["path"])
        return True
    except (OSError, ValueError, TypeError, KeyError, ts.ReadError):
        return False


def _snapshot(reader, commit, max_files, overlay=None):
    if overlay is not None:
        require_shared(overlay)
    _require_public_commit(reader, commit, max_files)
    cache_key = (commit, str(overlay) if overlay else "")
    if cache_key in reader.snapshots:
        return reader.snapshots[cache_key]
    def read(path):
        if overlay is not None:
            try:
                return reader.file(overlay, path)
            except FileNotFoundError:
                return None
        oid = reader.oid(commit, path, max_files)
        return reader.blob(oid) if oid else None

    native = read("eutonos.read.json")
    if native is None:
        raise ValueError("Native identity file absent")
    identity = _identity(native)
    config = read("eutonos.boards.json")
    if config is None:
        raise ValueError("Native board configuration absent")
    value = json.loads(config.decode("utf-8"))
    if value.get("profile") != boards.CONFIG or not isinstance(value.get("owners"), list):
        raise ValueError("Native board configuration invalid")
    paths = [safe_path(p) for p in value["owners"]]
    if not paths or len(paths) != len(set(paths)) or len(paths) > 128:
        raise _Limit("Native owner path bound/uniqueness failed")
    entries, diagnostics, hashes = {}, [], {}
    for path in paths:
        try:
            raw = read(path)
            if raw is None:
                diagnostics.append("Native owner missing: " + path)
                continue
            hashes[path] = hashlib.sha256(raw).hexdigest()
            board, matches = boards._parse(raw, path)
            if board is None or not matches:
                diagnostics.append("Native owner has no board: " + path)
            else:
                entries[path] = board
        except (ValueError, OSError, UnicodeError, _Limit, ts.ReadError) as exc:
            diagnostics.append("Native owner unavailable: " + path + " (" + str(exc) + ")")
    by_id = boards._project_graph(entries, diagnostics)
    order_by_id = {item["id"]: index for path in paths if path in entries
                   for index, item in enumerate(entries[path]["items"])}
    items = {}
    for item_id, (path, item) in by_id.items():
        body = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        items[item_id] = {**item, "owner_path": path, "order": order_by_id[item_id],
                          "owner_sha256": hashes[path],
                          "record_sha256": hashlib.sha256(body).hexdigest()}
    work_order = None
    try:
        order_raw = read(work_lifecycle.PATH)
        if order_raw is not None:
            work_order = json.loads(order_raw.decode("utf-8"))
            if (not isinstance(work_order, dict) or work_order.get("profile") != work_lifecycle.CONFIG or
                not isinstance(work_order.get("current"), list) or
                not isinstance(work_order.get("next"), list)):
                raise ValueError("Native work order invalid")
    except (ValueError, OSError, UnicodeError, _Limit, ts.ReadError) as exc:
        diagnostics.append("Native work order unavailable: " + str(exc))
        work_order = None
    result = {"identity": identity, "items": items, "owners": paths,
              "work_order": work_order, "diagnostics": diagnostics, "complete": not diagnostics}
    reader.snapshots[cache_key] = result
    return result


def _ref_rows(reader, max_refs):
    from .shared_work import load, refs
    cfg = load(reader.root)
    shared = refs(cfg)
    allowed = {row['owned_ref'] for row in shared}
    head = reader.git(reader.root, "symbolic-ref", "refs/remotes/origin/HEAD", missing=True)
    preferred = head.decode("utf-8", "replace").strip() if head else None
    if shared: preferred = shared[0]['owned_ref']
    raw = reader.git(reader.root, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(committerdate:unix)%00",
                     'refs/heads', *(['refs/eutonos-shared'] if shared else ['refs/remotes']), cap=128 * 1024)
    # An uncommitted marker also withdraws the worktree's named branch.
    private_refs = set()
    worktrees = reader.git(reader.root, "worktree", "list", "--porcelain", cap=128 * 1024)
    selected_path = None
    for line in worktrees.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            selected_path = line[9:]
        elif line.startswith("branch ") and selected_path and is_private(selected_path):
            private_refs.add(line[7:])
    rows = []
    for line in raw.splitlines():
        parts = line.split(b"\0")
        if len(parts) < 3:
            continue
        ref, oid = parts[0].decode("utf-8", "replace"), parts[1].decode("ascii", "replace")
        if (ref.startswith('refs/heads/') or (ref in allowed if shared else ref.startswith('refs/remotes/'))) and _SHA.fullmatch(oid):
            if ref in private_refs or _private_commit(reader, oid, 40000):
                continue
            rows.append({"ref": ref, "commit": oid,
                         "commit_time": int(parts[2]) if parts[2].isdigit() else None,
                         "source_kind": "cached-remote-ref" if ref.startswith(("refs/remotes/", "refs/eutonos-shared/")) else "local-ref"})
    # A tight caller bound must retain a usable Main before exploratory refs.
    rows.sort(key=lambda row: (0 if row["ref"] == preferred else
                               1 if row["ref"] == "refs/remotes/origin/main" else
                               2 if row["ref"].startswith("refs/remotes/") and row["ref"].endswith("/main") else
                               3 if row["ref"] == "refs/heads/main" else 4,
                               -(row["commit_time"] or 0), row["ref"]))
    truncated = len(rows) > max_refs
    return rows[:max_refs], truncated, preferred


def _main_ref(rows, preferred):
    by_ref = {row["ref"]: row for row in rows}
    if preferred and preferred.startswith('refs/eutonos-shared/'):
        if preferred not in by_ref: raise ValueError('Configured canonical source has never been observed; no local Main fallback.')
        return by_ref[preferred]
    for ref in (preferred, "refs/remotes/origin/main", "refs/heads/main"):
        if ref in by_ref:
            return by_ref[ref]
    remotes = [row for row in rows if row["ref"].startswith("refs/remotes/") and row["ref"].endswith("/main")]
    if remotes:
        return remotes[0]
    raise ValueError("No configured remote default or local main ref")


def _worktrees(reader, max_worktrees):
    raw = reader.git(reader.root, "worktree", "list", "--porcelain", cap=128 * 1024)
    entries, current = [], {}
    for line in raw.decode("utf-8", "replace").splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
                current = {}
        elif line.startswith("worktree "):
            current["path"] = line[9:]
        elif line.startswith("branch "):
            current["branch"] = line[7:]
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
    entries = [entry for entry in entries if not is_private(entry.get("path", ""))
               and not (isinstance(entry.get("head"), str) and _SHA.fullmatch(entry["head"])
                        and _private_commit(reader, entry["head"], 40000))]
    return entries[:max_worktrees], len(entries) > max_worktrees


def _changes(reader, base, tip, max_files):
    raw = reader.git(reader.root, "diff", "--name-only", "-z", "--no-renames",
                     base, tip, cap=512 * 1024)
    paths = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    if len(paths) > max_files:
        raise _Limit("Changed-file count bound exceeded")
    return paths


def _item_row(item, source, main_item=None, source_integration="unknown"):
    imported = item.get("extensions", {}).get("source_import")
    import_pin = ({key: imported.get(key) for key in ("disposition", "source_id", "operation_id")}
                  | {"source": imported.get("source")}) if isinstance(imported, dict) else None
    return {**deepcopy(item), "source_import": import_pin, "provenance": [source],
            "record_on_main": bool(main_item and main_item["record_sha256"] == item["record_sha256"]),
            "source_integration": ("unknown-retained-import" if imported and
                                   source_integration == "unknown" else source_integration),
            "publication": "cached-local-ref" if source["source_kind"] == "cached-remote-ref" else "unknown"}


def _historical_progress(main_item, candidate):
    """Recognize a bounded, explicitly earlier native campaign checkpoint.

    Git timestamp alone never chooses a winner. A completed Main campaign with
    recorded 100% progress and completed work history supersedes an earlier,
    lower-progress active checkpoint. All other divergent edits stay conflicts.
    """
    if not main_item or main_item.get("type") != "campaign" or main_item.get("status") != "completed":
        return False
    if candidate.get("type") != "campaign" or candidate.get("status") not in {"planned", "active", "blocked"}:
        return False
    for field in ("id", "type", "title", "summary", "parent_id", "evidence_refs"):
        if main_item.get(field) != candidate.get(field):
            return False
    current = main_item.get("extensions", {})
    older = candidate.get("extensions", {})
    progress_fields = {"progress", "checkpoint", "work_history", "last_work_request", "owner_move_deferred"}
    if ({key: value for key, value in current.items() if key not in progress_fields} !=
        {key: value for key, value in older.items() if key not in progress_fields}):
        return False
    current_progress, older_progress = current.get("progress"), older.get("progress")
    if not isinstance(current_progress, dict) or not isinstance(older_progress, dict):
        return False
    newer_date, older_date = current_progress.get("updated_on"), older_progress.get("updated_on")
    if (current_progress.get("percent") != 100 or
        type(older_progress.get("percent")) is not int or older_progress["percent"] >= 100 or
        not isinstance(newer_date, str) or not isinstance(older_date, str) or
        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", newer_date) or
        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", older_date) or
        older_date >= newer_date):
        return False
    history = current.get("work_history")
    return isinstance(history, list) and any(isinstance(row, dict) and row.get("status") == "completed"
                                             for row in history)


def _project(root, *, max_refs=512, max_worktrees=128, max_files=40000,
             max_bytes=64 * 1024 * 1024, time_budget=35):
    """Read all bounded local refs/worktrees for the exact selected checkout.

    The result is advisory.  A cached remote ref is never a live remote readback.
    """
    require_shared(root)
    selected = Path(root).resolve(strict=True)
    if any(type(v) is not int or v < 1 for v in (max_refs, max_worktrees, max_files, max_bytes, time_budget)):
        raise ValueError("Bounds must be positive integers")
    reader = _Reader(selected, max_bytes, time.monotonic() + time_budget)
    try:
        return _project_with_reader(selected, reader, max_refs=max_refs,
            max_worktrees=max_worktrees, max_files=max_files)
    finally:
        reader.close()


def _project_with_reader(selected, reader, *, max_refs, max_worktrees, max_files):
    top = reader.git(selected, "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
    if Path(top).resolve() != selected:
        raise ValueError("Repository root must be the exact selected Git checkout")
    observed_at = datetime.now(timezone.utc).isoformat()
    diagnostics = []
    coverage = {"refs_truncated": False, "worktrees_truncated": False,
                "sources_partial": 0, "owners_partial": 0, "changed_paths_truncated": False,
                "dirty_source_paths": "unknown",
                "ref_aliases_deduped": 0,
                "commands": None, "bytes": None}
    rows, coverage["refs_truncated"], preferred = _ref_rows(reader, max_refs)
    if coverage["refs_truncated"]:
        diagnostics.append("Ref count exceeds configured bound")
    main_ref = _main_ref(rows, preferred)
    main_commit = main_ref["commit"]
    worktrees, worktrees_truncated = _worktrees(reader, max_worktrees)
    worktree_heads = [entry["head"] for entry in worktrees
                      if isinstance(entry.get("head"), str) and _SHA.fullmatch(entry["head"])]
    reader.load_graph(worktree_heads)
    try:
        main_snapshot = _snapshot(reader, main_commit, max_files)
    except (ValueError, OSError, UnicodeError, ts.ReadError) as exc:
        diagnostics.append("Main native owners unavailable: " + str(exc))
        coverage.update(commands=reader.commands, bytes=reader.used,
                        sources_partial=len(rows), refs_observed=len(rows),
                        worktrees_observed=0, worktrees_truncated=worktrees_truncated)
        return {"profile": PROFILE, "status": "partial", "observed_at": observed_at,
                "observation_freshness": {"kind": "live", "age_seconds": 0},
                "repository": {"root": str(selected), **dict.fromkeys(_IDENTITY)},
                "main": {"ref": main_ref["ref"], "commit": main_commit, "work_order": None,
                         "ref_source": main_ref["source_kind"], "freshness": "cached-local-ref",
                         "status": "partial", "items": []},
                "latest": {"items": [], "completed_unintegrated": [], "conflicts": [],
                           "historical_candidates": [], "unclassified_changes": []},
                "sources": {"refs": [], "worktrees": []}, "coverage": coverage,
                "diagnostics": diagnostics,
                "qualification": "Main native records are unavailable; no current work conclusion is supported."}
    from .shared_work import load as shared_load
    shared_cfg = shared_load(selected)
    if shared_cfg['enabled'] and main_snapshot['identity'] != tuple(shared_cfg[k] for k in _IDENTITY):
        raise ValueError('Configured canonical source has foreign native identity; no fork fallback.')
    identity = main_snapshot["identity"]
    diagnostics.extend(main_snapshot["diagnostics"])
    coverage["owners_partial"] += len(main_snapshot["diagnostics"])
    def integration(item):
        imported = item.get("extensions", {}).get("source_import")
        if isinstance(imported, dict):
            source = imported.get("source")
            pin = source.get("commit") if isinstance(source, dict) else None
            if isinstance(pin, str) and _SHA.fullmatch(pin):
                try:
                    if reader.is_ancestor(pin, main_commit):
                        return "source-commit-on-main"
                    if imported.get("disposition") in {"held", "retained", "historical"}:
                        return "retained-unintegrated"
                except (_Limit, ValueError):
                    pass
            return "unknown-retained-import"
        delivery = item.get("extensions", {}).get("delivery")
        if not isinstance(delivery, dict):
            return "unknown"
        disposition = delivery.get("disposition")
        pin = delivery.get("source_commit")
        if disposition in {"retained", "held", "deferred", "not-integrated"}:
            return "retained-unintegrated"
        if disposition != "integrated" or not isinstance(pin, str) or not _SHA.fullmatch(pin):
            return "unknown"
        try:
            on_main = reader.is_ancestor(pin, main_commit)
            return "source-commit-on-main" if on_main else "unknown"
        except (_Limit, ValueError):
            return "unknown"

    main_source = {**main_ref, "source_kind": main_ref["source_kind"], "commit_on_main": True}
    main_items = {item_id: _item_row(item, main_source, item, integration(item))
                  for item_id, item in main_snapshot["items"].items()}
    native_items = {row["record_sha256"]: main_snapshot["items"][item_id]
                    for item_id, row in main_items.items()}
    candidates = {item_id: [row] for item_id, row in main_items.items()}
    source_rows = []
    unclassified = []
    considered_commits = set()

    def consider(commit, source, overlay=None, dirty_paths=None):
        try:
            native_oid = reader.oid(commit, "eutonos.read.json", max_files)
            if native_oid is None or _identity(reader.blob(native_oid)) != identity:
                raise ValueError("Foreign repository/scope/namespace identity")
            board_inputs = set(main_snapshot["owners"]) | {"eutonos.boards.json", "eutonos.read.json"}
            native_changed = bool(board_inputs.intersection(dirty_paths or []))
            if not native_changed:
                for path in board_inputs:
                    if reader.oid(commit, path, max_files) != reader.oid(main_commit, path, max_files):
                        native_changed = True
                        break
            if not native_changed:
                source_rows.append({**source, "status": "observed", "native_coverage": "same-owner-blobs-as-main"})
                return
            base = reader.merge_base(main_commit, commit)
            base_snapshot = _snapshot(reader, base, max_files)
            snapshot = _snapshot(reader, commit, max_files, overlay)
            if snapshot["identity"] != identity or base_snapshot["identity"] != identity:
                raise ValueError("Foreign repository/scope/namespace identity")
            source_rows.append({**source, "status": "observed", "merge_base": base,
                                "native_coverage": "partial" if snapshot["diagnostics"] else "available"})
            diagnostics.extend(snapshot["diagnostics"])
            coverage["owners_partial"] += len(snapshot["diagnostics"])
            for item_id in sorted(set(base_snapshot["items"]) | set(snapshot["items"])):
                before = base_snapshot["items"].get(item_id)
                after = snapshot["items"].get(item_id)
                if before and after and before["record_sha256"] == after["record_sha256"]:
                    continue
                if after is None:
                    # Absence is only a claim when all owners were read.
                    if snapshot["complete"] and before:
                        after = {**before, "status": "removed",
                                 "record_sha256": hashlib.sha256(("removed:" + before["record_sha256"]).encode()).hexdigest()}
                    else:
                        continue
                current_main = main_snapshot["items"].get(item_id)
                source_import = (current_main or {}).get("extensions", {}).get("source_import")
                if isinstance(source_import, dict) and source_import.get("source_id") == item_id:
                    original = source_import.get("source_item")
                    observed = {key: value for key, value in after.items()
                                if key not in {"owner_path", "owner_sha256", "record_sha256"}}
                    if isinstance(original, dict) and original == observed:
                        main_items[item_id]["provenance"].append({**source, "source_kind": "retained-source-history"})
                        continue
                row = _item_row(after, source, main_snapshot["items"].get(item_id), integration(after))
                native_items[row["record_sha256"]] = after
                if row["record_on_main"]:
                    continue
                if (current_main is None and before is None or
                    current_main is not None and before is not None and
                    current_main["record_sha256"] == before["record_sha256"]):
                    candidates[item_id] = [existing for existing in candidates.get(item_id, [])
                                           if existing["provenance"][0]["ref"] != main_ref["ref"]]
                candidates.setdefault(item_id, []).append(row)
        except (_Limit, ValueError, OSError, UnicodeError, ts.ReadError) as exc:
            coverage["sources_partial"] += 1
            source_rows.append({**source, "status": "partial", "reason": str(exc)})
            diagnostics.append(source["ref"] + ": " + str(exc))

    ref_deadline = reader.deadline - 8
    for ref in rows:
        if time.monotonic() >= ref_deadline:
            coverage["sources_partial"] += 1
            diagnostics.append("Ref scan paused to preserve worktree observation budget")
            break
        if ref["ref"] == main_ref["ref"]:
            continue
        if ref["commit"] in considered_commits:
            coverage["ref_aliases_deduped"] += 1
            source_rows.append({**ref, "status": "equivalent-ref", "equivalent_commit": ref["commit"]})
            continue
        considered_commits.add(ref["commit"])
        if reader.is_ancestor(ref["commit"], main_commit):
            source_rows.append({**ref, "status": "already-on-main"})
        else:
            consider(ref["commit"], ref)

    coverage["worktrees_truncated"] = worktrees_truncated
    if coverage["worktrees_truncated"]:
        diagnostics.append("Worktree count exceeds configured bound")
    worktree_rows = []
    for entry in worktrees:
        if entry.get("bare") or not entry.get("path"):
            continue
        path = Path(entry["path"])
        wt = {"path": str(path), "branch": entry.get("branch"), "detached": entry.get("detached", False)}
        try:
            if not path.is_dir() or path.is_symlink():
                raise ValueError("Worktree path unavailable or linked")
            for parent in (path, *path.parents):
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Worktree path has a linked/reparse parent")
            head = entry.get("head")
            if not isinstance(head, str) or not _SHA.fullmatch(head):
                raise ValueError("Worktree HEAD unavailable")
            changed = []
            # Native owners are the only dirty paths needed for work-state
            # reconciliation. Other dirty source paths remain unknown here.
            for native_path in ["eutonos.read.json", "eutonos.boards.json", *main_snapshot["owners"]]:
                try:
                    raw = reader.file(path, native_path)
                except FileNotFoundError:
                    raw = None
                committed_oid = reader.oid(head, native_path, max_files)
                committed = reader.blob(committed_oid) if committed_oid else None
                # Git checkout EOL conversion is not a native work edit. Keep
                # raw fingerprints on rows, but do not invent dirty overlays
                # merely because a Windows checkout has CRLF owner text.
                if (raw.replace(b"\r\n", b"\n") if raw is not None else None) != (committed.replace(b"\r\n", b"\n") if committed is not None else None):
                    changed.append(native_path)
            wt.update(commit=head, dirty_native_paths=len(changed),
                      dirty_source_coverage="unknown", status="observed")
            if changed:
                source = {"ref": "worktree:" + str(path), "commit": head,
                          "source_kind": "working-tree", "path": str(path),
                          "changed_paths": changed[:100], "commit_on_main": False}
                consider(head, source, overlay=path, dirty_paths=changed)
        except (_Limit, ValueError, OSError) as exc:
            wt.update(status="partial", reason=str(exc))
            coverage["sources_partial"] += 1
            diagnostics.append("Worktree " + str(path) + ": " + str(exc))
        worktree_rows.append(wt)

    # One bounded Git history read exposes source changes without launching a
    # diff process for every branch. A path is intentionally unclassified when
    # no exact native item-to-path evidence exists.
    try:
        from .shared_work import load as shared_load
        history_refs = [row['ref'] for row in rows]
        raw = reader.git(selected, "log", *history_refs, "--not", main_commit,
                         "--max-count=5001", "--no-renames", "--format=commit:%H",
                         "--name-only", cap=2 * 1024 * 1024)
        selected_paths = set(main_snapshot["owners"]) | {"eutonos.boards.json", "eutonos.work.json",
            "docs/CURRENT_TASKS.md", "docs/NEXT_WORK.md", "docs/WORK_ORDER.md", "docs/DONE_WORK.md"}
        seen_paths = set()
        source_commit = None
        observed_commits = 0
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.startswith("commit:") and _SHA.fullmatch(line[7:]):
                observed_commits += 1
                if observed_commits > 5000:
                    coverage["changed_paths_truncated"] = True
                    diagnostics.append("Unclassified commit history exceeds configured count bound")
                    break
                source_commit = None if _private_commit(reader, line[7:], max_files) else line[7:]
            elif source_commit and line and line not in selected_paths and line not in seen_paths:
                seen_paths.add(line)
                unclassified.append({"path": line, "provenance": {
                    "ref": None, "commit": source_commit, "source_kind": "commit-outside-main"}})
                if len(unclassified) >= max_files:
                    coverage["changed_paths_truncated"] = True
                    break
    except (_Limit, ValueError, UnicodeError) as exc:
        coverage["changed_paths_truncated"] = True
        diagnostics.append("Unclassified committed paths unavailable: " + str(exc))

    latest, conflicts, historical_candidates = [], [], []
    for item_id in sorted(candidates):
        versions = candidates[item_id]
        unique = {}
        for row in versions:
            key = row["record_sha256"]
            if key in unique:
                unique[key]["provenance"].extend(row["provenance"])
            else:
                unique[key] = row
        choices = list(unique.values())
        if len(choices) > 1 and item_id in main_snapshot["items"]:
            main_native = main_snapshot["items"][item_id]
            kept = []
            for candidate in choices:
                if not candidate["record_on_main"] and _historical_progress(
                        main_native, native_items[candidate["record_sha256"]]):
                    historical_candidates.append({"id": item_id, "candidate": candidate,
                        "classification": "inferred-historical-checkpoint",
                        "reason": "Earlier lower-progress native campaign checkpoint appears superseded by completed Main work history; review exact sources before discarding"})
                else:
                    kept.append(candidate)
            choices = kept
        if len(choices) > 1:
            survivors = []
            for candidate in choices:
                superseded = False
                for other in choices:
                    if candidate is other:
                        continue
                    left, right = candidate["provenance"][0], other["provenance"][0]
                    if left["commit"] == right["commit"]:
                        if left["source_kind"] != "working-tree" and right["source_kind"] == "working-tree":
                            superseded = True
                            break
                        continue
                    try:
                        if reader.is_ancestor(left["commit"], right["commit"]):
                            superseded = True
                            break
                    except (_Limit, ValueError):
                        diagnostics.append("Ancestry comparison incomplete for " + item_id)
                        break
                if superseded:
                    continue
                survivors.append(candidate)
            choices = survivors
        if len(choices) == 1:
            latest.append(choices[0])
        else:
            conflicts.append({"id": item_id, "candidates": choices,
                              "reason": "Incomparable native record versions"})
    completed = [row for row in latest if row["type"] != "attempt" and row["status"] == "completed" and
                 (not row["record_on_main"] or row["source_import"]) and
                 row["source_integration"] != "source-commit-on-main"]
    coverage.update(commands=reader.commands, bytes=reader.used,
                    refs_observed=len(rows), worktrees_observed=len(worktree_rows))
    result = {"profile": PROFILE, "status": "partial" if diagnostics else "available",
            "observed_at": observed_at,
            "observation_freshness": {"kind": "live", "age_seconds": 0},
            "repository": {"root": str(selected), **dict(zip(_IDENTITY, identity))},
            "main": {"ref": main_ref["ref"], "commit": main_commit,
                     "ref_source": main_ref["source_kind"], "freshness": "cached-local-ref",
                     "work_order": main_snapshot["work_order"],
                     "status": "partial" if main_snapshot["diagnostics"] else "available",
                     "items": [main_items[key] for key in sorted(main_items)]},
            "latest": {"items": latest, "completed_unintegrated": completed,
                       "conflicts": conflicts, "historical_candidates": historical_candidates,
                       "unclassified_changes": unclassified},
            "sources": {"refs": source_rows, "worktrees": worktree_rows},
            "coverage": coverage, "diagnostics": diagnostics,
            "qualification": "Local ref and native-record observation only; source correctness, live remote publication, installation and acceptance are not established."}
    return result


def project(root, *, max_refs=512, max_worktrees=128, max_files=40000,
            max_bytes=64 * 1024 * 1024, time_budget=35, refresh=False):
    """Read repository state, reusing a clearly aged snapshot for brief repeat views.

    Callers that need a new observation after an edit pass ``refresh=True``.
    """
    require_shared(root)
    from .shared_work import selection
    key = (str(Path(root).resolve(strict=True)), selection(root), max_refs, max_worktrees,
           max_files, max_bytes, time_budget)
    now = time.monotonic()
    cached_sources = []
    if key in _CACHE:
        cached_sources = [*_CACHE[key][1]["sources"]["refs"],
                          *[{**row, "kind": "working-tree"} for row in _CACHE[key][1]["sources"]["worktrees"]
                            if row.get("commit")]]
    if (not refresh and key in _CACHE and now - _CACHE[key][0] < _CACHE_SECONDS
            and shared_cached_sources(root, cached_sources)):
        result = deepcopy(_CACHE[key][1])
        result["observation_freshness"] = {"kind": "cached", "age_seconds": round(now - _CACHE[key][0], 1)}
        return result
    result = _project(root, max_refs=max_refs, max_worktrees=max_worktrees,
                      max_files=max_files, max_bytes=max_bytes, time_budget=time_budget)
    _CACHE.clear()
    _CACHE[key] = (time.monotonic(), deepcopy(result))
    return result


def compact(result):
    """Small pickup summary; keep conflicts and partial coverage visible."""
    latest = result["latest"]
    def brief(item):
        return {"id": item["id"], "type": item["type"], "title": item["title"],
                "status": item["status"], "owner_path": item["owner_path"],
                "source_integration": item["source_integration"],
                "provenance": [{"ref": pin["ref"], "commit": pin["commit"],
                                "source_kind": pin["source_kind"]}
                               for pin in item["provenance"][:1]]}
    main_campaigns = [brief(item) for item in result["main"]["items"]
                      if item["type"] == "campaign" and item["status"] in {"active", "blocked", "completed"}]
    main_campaigns.sort(key=lambda item: (item["status"] not in {"active", "blocked"}, item["id"]))
    main_current = [brief(item) for item in result["main"]["items"]
                    if item["type"] != "attempt" and item["status"] in {"active", "blocked"}]
    latest_current = [brief(item) for item in latest["items"]
                      if item["status"] in {"active", "blocked"} and
                      (item["type"] in work_lifecycle.WORK or work_lifecycle.queued(item)) and
                      (not item["record_on_main"] or item["type"] == "campaign")]
    return {"profile": PROFILE, "status": result["status"],
            "observed_at": result["observed_at"],
            "observation_freshness": result["observation_freshness"],
            "main": {"ref": result["main"]["ref"], "commit": result["main"]["commit"],
                     "freshness": result["main"]["freshness"],
                     "item_count": sum(item["type"] != "attempt" for item in result["main"]["items"]),
                     "campaigns": main_campaigns[:8], "campaigns_truncated": len(main_campaigns) > 8,
                     "other_campaign_count": sum(item["type"] == "campaign" and item["status"] not in {"active", "blocked", "completed"} for item in result["main"]["items"]),
                     "current": main_current[:12], "current_truncated": len(main_current) > 12},
            "latest": {"item_count": sum(item["type"] != "attempt" for item in latest["items"]),
                       "current": latest_current[:12], "current_truncated": len(latest_current) > 12,
                       "completed_unintegrated_count": len(latest["completed_unintegrated"]),
                       "completed_unintegrated": [brief(i) for i in latest["completed_unintegrated"][:6]],
                       "completed_unintegrated_truncated": len(latest["completed_unintegrated"]) > 6,
                       "conflict_count": len(latest["conflicts"]),
                       "conflicts": [{"id": row["id"], "candidate_count": len(row["candidates"]),
                                      "candidates": [{key: brief(i)[key] for key in ("status", "owner_path", "provenance")} for i in row["candidates"][:2]]}
                                     for row in latest["conflicts"][:6]],
                       "conflicts_truncated": len(latest["conflicts"]) > 6,
                       "historical_candidate_count": len(latest["historical_candidates"]),
                       "unclassified_change_count": len(latest["unclassified_changes"])},
            "coverage": result["coverage"], "diagnostics": [str(d)[:500] for d in result["diagnostics"][:8]],
            "next_action": "Use the full state reader for omitted/conflicting records, then inspect their exact source owners; no integration is authorized by this summary."}


read = project
