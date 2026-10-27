"""Bounded official harness documentation watch; review candidates only.

Fetched pages are untrusted data. This module never edits a policy or catalog,
and a normalized-text hash is only a change signal, not semantic judgment.
"""
from __future__ import annotations

from copy import deepcopy
from difflib import unified_diff
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import model_catalog
from .local_lock import local_lock

PROFILE = "eutonos.harness-docs-watch.v1"
INTERVAL = 86_400
MAX_PAGE_BYTES = 8_388_608
MAX_NORMALIZED_TEXT = 1_048_576
MAX_TEXT = 12_000
MAX_STATE_BYTES = 262_144
MAX_PENDING = 64
MAX_DIFF = 3_000
PICKUP_ITEMS = 12
TIMEOUT = 3
PAGE_DEADLINE = 7
SCAN_DEADLINE = 90
URLS = (
    "https://learn.chatgpt.com/docs/models",
    "https://learn.chatgpt.com/docs/changelog",
    "https://learn.chatgpt.com/docs/agent-configuration/subagents",
    "https://learn.chatgpt.com/docs/agent-configuration/agents-md",
    "https://learn.chatgpt.com/docs/hooks",
    "https://code.claude.com/docs/en/model-config",
    "https://code.claude.com/docs/en/sub-agents",
    "https://code.claude.com/docs/en/hooks",
    "https://code.claude.com/docs/en/changelog",
)
_URL_SET = frozenset(URLS)
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


class DocsWatchError(ValueError):
    """Watch state or scan cannot be used without risking prior evidence."""


def state_path(root=None):
    return model_catalog.watch_home() / "harness-docs-watch" / "baseline.json"


def _at(now):
    value = time.time() if now is None else now
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise DocsWatchError("watch time must be a finite nonnegative UTC epoch")
    return value


def _safe_url(url):
    if url not in _URL_SET:
        raise DocsWatchError("documentation URL is outside the official allowlist")
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _fetch(url, etag=None, last_modified=None):
    _safe_url(url)
    deadline = time.monotonic() + PAGE_DEADLINE
    headers = {"User-Agent": "EUTONOS-docs-watch/1", "Accept": "text/html"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_Redirect())
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            _safe_url(response.geturl())
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                raise DocsWatchError("source did not return HTML")
            parts = []
            size = 0
            while True:
                if time.monotonic() >= deadline:
                    raise DocsWatchError("source page exceeded time cap")
                # HTTPResponse.read1 returns available bytes without waiting
                # to fill a large buffer. The socket timeout bounds one read;
                # the monotonic check bounds a slow trickle across reads.
                chunk = response.read1(min(65_536, MAX_PAGE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    raise DocsWatchError("source page exceeds byte cap")
                parts.append(chunk)
            body = b"".join(parts)
            return {"status": response.status, "body": body, "url": response.geturl(),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified")}
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            _safe_url(exc.geturl())
            return {"status": 304, "url": exc.geturl()}
        raise


class _Article(HTMLParser):
    _skip = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form"}
    _blocks = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code", "tr", "br", "section"}
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.parts = []
        self.found = False
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self._void:
            self.handle_startendtag(tag, attrs)
            return
        attrs = dict(attrs)
        article = tag in ("main", "article") or attrs.get("role") == "main"
        self.stack.append((tag, article, tag in self._skip))
        if article:
            self.depth += 1
            self.found = True
        if tag in self._skip:
            self.skip += 1
        if tag in self._blocks and self.depth and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        # Browsers accept implicit closures (notably paragraphs and list
        # items). Close through the matching ancestor while still requiring
        # every main/article element to finish by end of document.
        position = next((i for i in range(len(self.stack) - 1, -1, -1)
                         if self.stack[i][0] == tag), None)
        if position is None:
            return
        for _, article, skipped in self.stack[position:]:
            if article:
                self.depth -= 1
            if skipped:
                self.skip -= 1
        del self.stack[position:]
        if tag in self._blocks and self.depth and not self.skip:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self.depth and not self.skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.parts.append(data)


def _normalize(body):
    if not isinstance(body, bytes) or len(body) > MAX_PAGE_BYTES:
        raise DocsWatchError("invalid or oversized page body")
    try:
        source = body.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DocsWatchError("page encoding invalid") from exc
    parser = _Article()
    try:
        parser.feed(source)
        parser.close()
    except (ValueError, RecursionError) as exc:
        raise DocsWatchError("page article invalid") from exc
    if not parser.found or parser.depth != 0:
        raise DocsWatchError("page has no complete main article")
    text = "\n".join(re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in text.splitlines() if line)
    if len(text) < 40 or len(text) > MAX_NORMALIZED_TEXT:
        raise DocsWatchError("article is empty, suspicious, or exceeds text cap")
    return text


def _fresh():
    return {"profile": PROFILE, "sequence": 0, "last_scan_at": None, "pages": {}, "pending": {}}


def _validate(state):
    if not isinstance(state, dict) or set(state) != {"profile", "sequence", "last_scan_at", "pages", "pending"} or state["profile"] != PROFILE:
        raise ValueError("profile")
    if type(state["sequence"]) is not int or not 0 <= state["sequence"] <= 10**12:
        raise ValueError("sequence")
    if state["last_scan_at"] is not None:
        _at(state["last_scan_at"])
    pages, notices = state["pages"], state["pending"]
    if not isinstance(pages, dict) or not set(pages) <= _URL_SET or not isinstance(notices, dict) or len(notices) > MAX_PENDING:
        raise ValueError("page or notice count")
    for url, page in pages.items():
        if not isinstance(page, dict) or set(page) != {"status", "hash", "text", "etag", "last_modified", "last_scan_at", "last_good_at"}:
            raise ValueError("page shape")
        if page["status"] not in ("ok", "unavailable") or not isinstance(page["text"], str) or len(page["text"]) > MAX_TEXT:
            raise ValueError("page content")
        if page["hash"] is not None and (not isinstance(page["hash"], str) or len(page["hash"]) != 64):
            raise ValueError("page hash")
        for key in ("etag", "last_modified"):
            if page[key] is not None and (not isinstance(page[key], str) or len(page[key]) > 256):
                raise ValueError("page validator")
        for key in ("last_scan_at", "last_good_at"):
            if page[key] is not None:
                _at(page[key])
    for token, item in notices.items():
        if not isinstance(item, dict) or set(item) != {"id", "source", "kind", "key", "before", "after", "first_seen", "updated_at", "url", "date", "diff"}:
            raise ValueError("notice shape")
        if token != f"{item['kind']}:{item['url']}" or item["url"] not in _URL_SET or item["kind"] not in ("changed", "source_failure", "source_recovery"):
            raise ValueError("notice key")
        if item["source"] != "harness_docs" or item["key"] != item["url"] or not isinstance(item["id"], str) or not re.fullmatch(r"harness-docs-watch-\d+", item["id"]):
            raise ValueError("notice identity")
        for key in ("before", "after"):
            if item[key] is not None and (not isinstance(item[key], str) or len(item[key]) > 128):
                raise ValueError("notice value")
        if not isinstance(item["diff"], str) or len(item["diff"]) > MAX_DIFF:
            raise ValueError("notice diff")
        _at(item["first_seen"])
        _at(item["updated_at"])
    return state


def _read(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DocsWatchError("documentation baseline unreadable; preserved") from exc
    try:
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("oversized")
        return _validate(json.loads(raw))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise DocsWatchError("documentation baseline corrupt or oversized; preserved") from exc


def _write(path, state):
    try:
        _validate(state)
        raw = (json.dumps(state, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, KeyError) as exc:
        raise DocsWatchError("documentation state invalid; prior baseline preserved") from exc
    if len(raw) > MAX_STATE_BYTES:
        raise DocsWatchError("documentation state exceeds cap; prior baseline preserved")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".docs-watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _notice(state, url, kind, before, after, at, diff=""):
    token = f"{kind}:{url}"
    prior = state["pending"].get(token)
    if prior and prior["after"] == after and prior["diff"] == diff:
        return False
    if len(state["pending"]) >= MAX_PENDING and prior is None:
        raise DocsWatchError("documentation pending notices at capacity; acknowledge before scanning")
    state["sequence"] += 1
    state["pending"][token] = {"id": f"harness-docs-watch-{state['sequence']}", "source": "harness_docs",
                               "kind": kind, "key": url, "url": url, "date": time.strftime("%Y-%m-%d", time.gmtime(at)),
                               "before": before, "after": after, "diff": diff,
                               "first_seen": prior["first_seen"] if prior else at, "updated_at": at}
    return True


def _diff(before, after):
    lines = unified_diff(before.splitlines(), after.splitlines(), fromfile="previous", tofile="current", lineterm="")
    diff = "\n".join(lines)[:MAX_DIFF]
    return diff or "Article changed outside the retained excerpt; review the official URL."


def _failure_reason(exc):
    """Expose a bounded category without echoing URLs, accounts, or server text."""
    if isinstance(exc, DocsWatchError):
        return str(exc)[:120]
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP status {exc.code}"
    if isinstance(exc, TimeoutError):
        return "network timeout"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return "network or source I/O unavailable"
    return "source format or fetch unavailable"


def _observation(fetcher, url, page):
    result = fetcher(url, page["etag"] if page else None, page["last_modified"] if page else None)
    if not isinstance(result, dict) or result.get("status") not in (200, 304):
        raise DocsWatchError("source returned an invalid response")
    _safe_url(result.get("url", url))
    if result["status"] == 304:
        if not page or not page["hash"]:
            raise DocsWatchError("source returned 304 without prior article")
        return None
    return _normalize(result.get("body")), result.get("etag"), result.get("last_modified")


def scan(root=None, *, now=None, fetcher=None, _due=False):
    """Force a bounded scan. An injected fetcher receives URL, ETag, Last-Modified."""
    at = _at(now)
    scan_deadline = time.monotonic() + SCAN_DEADLINE
    path = state_path(root)
    fetcher = fetcher or _fetch
    try:
        with local_lock(path.parent, timeout=0.1 if _due else 2):
            prior = _read(path)
            if _due and prior and prior["last_scan_at"] is not None and at < prior["last_scan_at"] + INTERVAL:
                return {"status": "not-due", "scanned": False, "changed": False,
                        "pending_count": len(prior["pending"]), "next_due_at": prior["last_scan_at"] + INTERVAL}
            updated = deepcopy(prior) if prior else _fresh()
            changed = False
            results = {}
            errors = {}
            for url in URLS:
                page = updated["pages"].get(url)
                try:
                    if time.monotonic() >= scan_deadline:
                        raise DocsWatchError("documentation scan exceeded time cap")
                    observed = _observation(fetcher, url, page)
                    if observed is None:
                        if page["status"] == "unavailable":
                            changed |= _notice(updated, url, "source_recovery", "unavailable", "ok", at)
                        page.update(status="ok", last_scan_at=at, last_good_at=at)
                        results[url] = "unchanged"
                        continue
                    article, etag, last_modified = observed
                    digest = sha256(article.encode("utf-8")).hexdigest()
                    if page:
                        if page["status"] == "unavailable":
                            changed |= _notice(updated, url, "source_recovery", "unavailable", "ok", at)
                        if page["hash"] and page["hash"] != digest:
                            changed |= _notice(updated, url, "changed", page["hash"], digest, at,
                                               _diff(page["text"], article[:MAX_TEXT]))
                    updated["pages"][url] = {"status": "ok", "hash": digest, "text": article[:MAX_TEXT],
                                             "etag": etag if isinstance(etag, str) and len(etag) <= 256 else None,
                                             "last_modified": last_modified if isinstance(last_modified, str) and len(last_modified) <= 256 else None,
                                             "last_scan_at": at, "last_good_at": at}
                    results[url] = "baseline" if page is None or page["hash"] is None else "changed" if page["hash"] != digest else "unchanged"
                except Exception as exc:
                    if isinstance(exc, DocsWatchError) and "pending notices at capacity" in str(exc):
                        raise
                    # Invalid documents and network failures never replace a
                    # last good article, hash, or validator.
                    if page is None:
                        page = {"status": "unavailable", "hash": None, "text": "", "etag": None,
                                "last_modified": None, "last_scan_at": at, "last_good_at": None}
                        updated["pages"][url] = page
                        changed |= _notice(updated, url, "source_failure", "unknown", "unavailable", at)
                    elif page["status"] == "ok":
                        page["status"] = "unavailable"
                        changed |= _notice(updated, url, "source_failure", "ok", "unavailable", at)
                    page["last_scan_at"] = at
                    results[url] = "unavailable"
                    errors[url] = _failure_reason(exc)
            updated["last_scan_at"] = at
            _write(path, updated)
    except TimeoutError as exc:
        if _due:
            return {"status": "deferred", "reason": "scan-in-progress", "scanned": False, "changed": False}
        raise DocsWatchError("documentation metadata lock busy; baseline unchanged") from exc
    except OSError as exc:
        raise DocsWatchError("documentation storage unavailable; baseline unchanged") from exc
    return {"status": "attention-required" if errors else "ok", "scanned": True, "changed": changed,
            "pending_count": len(updated["pending"]),
            "sources": results, "errors": errors, "path": str(path), "next_due_at": at + INTERVAL}


def scan_if_due(root=None, *, now=None, fetcher=None):
    """Schedule a lazy scan without blocking pickup/dashboard on the network."""
    if fetcher is None and os.environ.get("EUTONOS_DOCS_WATCH_DISABLED") == "1":
        return {"status": "disabled", "scanned": False, "changed": False}
    try:
        at = _at(now)
        path = state_path(root)
        state = _read(path)
        if state and state["last_scan_at"] is not None and at < state["last_scan_at"] + INTERVAL:
            return {"status": "not-due", "scanned": False, "changed": False,
                    "pending_count": len(state["pending"]), "next_due_at": state["last_scan_at"] + INTERVAL}
        key = str(path)
        with _ACTIVE_LOCK:
            if key in _ACTIVE:
                return {"status": "deferred", "reason": "scan-in-progress", "scanned": False, "changed": False}
            _ACTIVE.add(key)

        def run():
            try:
                scan(root, now=at, fetcher=fetcher, _due=True)
            except Exception:
                pass  # Explicit scan and pickup_hint expose durable errors.
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(key)

        threading.Thread(target=run, name="eutonos-harness-docs-watch", daemon=True).start()
        return {"status": "scheduled", "scanned": False, "changed": False,
                "pending_count": len(state["pending"]) if state else 0}
    except Exception:
        return {"status": "attention-required", "scanned": False, "changed": False,
                "error": "documentation watch state unavailable; prior evidence preserved"}


def pending(root=None):
    try:
        path = state_path(root)
        state = _read(path)
        items = sorted((state or {"pending": {}})["pending"].values(), key=lambda x: (x["first_seen"], x["id"]))
        return {"status": "ok", "count": len(items), "items": items, "path": str(path)}
    except Exception:
        return {"status": "attention-required", "count": 0, "items": [],
                "error": "documentation watch state unavailable; prior evidence preserved"}


def ack(root=None, ids=None):
    if not isinstance(ids, list) or len(ids) > MAX_PENDING or any(not isinstance(x, str) or not re.fullmatch(r"harness-docs-watch-\d+", x) for x in ids):
        raise DocsWatchError("ack requires explicit bounded notice IDs")
    path = state_path(root)
    try:
        with local_lock(path.parent, timeout=2):
            state = _read(path)
            if state is None:
                return {"status": "ok", "acknowledged": [], "count": 0, "path": str(path)}
            wanted = set(ids)
            acknowledged = sorted(x["id"] for x in state["pending"].values() if x["id"] in wanted)
            if acknowledged:
                state["pending"] = {k: x for k, x in state["pending"].items() if x["id"] not in wanted}
                _write(path, state)
    except TimeoutError as exc:
        raise DocsWatchError("documentation metadata lock busy; acknowledgement unchanged") from exc
    return {"status": "ok", "acknowledged": acknowledged, "count": len(state["pending"]), "path": str(path)}


def pickup_hint(root=None):
    try:
        state = _read(state_path(root))
        if state is None:
            return None
        items = sorted(state["pending"].values(), key=lambda x: (x["first_seen"], x["id"]))
        return {"status": "ok", "pending_count": len(items), "items": items[:PICKUP_ITEMS],
                "pending_truncated": len(items) > PICKUP_ITEMS,
                "sources": {url: {k: page[k] for k in ("status", "last_scan_at", "last_good_at")}
                            for url, page in state["pages"].items()}}
    except Exception:
        return {"status": "attention-required", "pending_count": 0, "items": [],
                "pending_truncated": False, "sources": {},
                "error": "documentation watch state unavailable; prior evidence preserved"}
