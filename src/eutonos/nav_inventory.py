"""Streaming, scope-first construction of the live NAV source inventory."""

from __future__ import annotations

from collections.abc import Mapping
import codecs
import hashlib

from . import tokenslang as ts
from .effects import digest
from .source import GitView, safe_path


def _under(path, roots):
    return any(path == root or path.startswith(root + "/") for root in roots)


def _decode_path(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "Non-UTF-8 Git path in selected scope.") from exc


def _observe(local, path, retain_text=False):
    checksum = hashlib.sha256()
    size = 0
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = [] if retain_text else None
    try:
        for chunk in local.iter_read(path):
            checksum.update(chunk)
            size += len(chunk)
            decoded = decoder.decode(chunk)
            if text is not None:
                text.append(decoded)
        decoded = decoder.decode(b"", final=True)
        if text is not None:
            text.append(decoded)
    except UnicodeError as exc:
        raise ts.ReadError("unsupported", "NAV requires UTF-8 source.") from exc
    return checksum.hexdigest(), size, None if text is None else "".join(text)


class LazyContents(Mapping):
    """Selected UTF-8 sources, verified against their inventory observation."""

    def __init__(self, local, hashes, sizes):
        self._local = local
        self._hashes = hashes
        self._sizes = sizes

    def __iter__(self):
        return iter(self._hashes)

    def __len__(self):
        return len(self._hashes)

    def __getitem__(self, path):
        if path not in self._hashes:
            raise KeyError(path)
        try:
            checksum, size, text = _observe(self._local, path, retain_text=True)
        except FileNotFoundError as exc:
            raise ts.ReadError("stale", "Selected NAV source disappeared after inventory.") from exc
        ts.require(checksum == self._hashes[path] and size == self._sizes[path],
                   "stale", "Selected NAV source changed after inventory.")
        return text


def _literal(path):
    return ":(top,literal)" + path


def inventory(local, cfg, config_path):
    """Observe only configured live roots and context through streaming Git output."""
    view = GitView(local.root, "HEAD")
    scopes = list(dict.fromkeys((*cfg["source_roots"], *cfg["context_paths"])))
    pathspecs = [_literal(path) for path in scopes]
    has_unmerged = False
    for _ in view.iter_nul("ls-files", "--unmerged", "-z", "--", *pathspecs):
        has_unmerged = True
    ts.require(not has_unmerged, "conflict", "Resolve unmerged source before NAV construction.")

    selected = set(cfg["context_paths"])
    for raw in view.iter_nul("ls-files", "--cached", "--others", "--exclude-standard",
                             "-z", "--", *pathspecs):
        path = safe_path(_decode_path(raw))
        if (_under(path, cfg["source_roots"])
                and not _under(path, cfg["excluded_roots"] + [cfg["map_root"]])
                and path != config_path):
            selected.add(path)

    hashes, sizes = {}, {}
    for path in sorted(selected):
        try:
            checksum, size, _ = _observe(local, path)
        except FileNotFoundError:
            ts.require(path not in cfg["context_paths"], "missing",
                       "Required governing context is absent.")
            continue  # A tracked deletion remains visible through baseline comparison.
        hashes[path] = checksum
        sizes[path] = size
    return {"fingerprint": digest(ts.encoded(hashes)), "paths": hashes,
            "sizes": sizes, "contents": LazyContents(local, hashes, sizes),
            "commit": view.commit, "view": "live-tracked-and-nonignored",
            "excluded_roots": cfg["excluded_roots"]}
