"""Offline qualification for the bounded official documentation watcher."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from eutonos import harness_docs_watch as watch


def html(text, *, chrome="Navigation links and account settings"):
    return (f"<html><header>{chrome}</header><nav>{chrome}</nav>"
            f"<main><article><h1>Harness documentation</h1><p>{text}</p></article></main>"
            f"<footer>{chrome}</footer></html>").encode()


class HarnessDocsWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)
        self.pages = {url: html("A model option and an agent configuration behavior apply to this release.")
                      for url in watch.URLS}
        self.calls = []

    def fetch(self, url, etag, last_modified):
        self.calls.append((url, etag, last_modified))
        return {"status": 200, "url": url, "body": self.pages[url], "etag": '"page-v1"',
                "last_modified": "Wed, 30 Sep 2026 12:00:00 GMT"}

    def test_quiet_baseline_chrome_normalization_and_coalesced_candidate(self):
        first = watch.scan(now=1000, fetcher=self.fetch)
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["changed"])
        self.assertEqual(watch.pending()["count"], 0)
        self.assertEqual(len(self.calls), len(watch.URLS))
        self.assertTrue(all(e == None and m == None for _, e, m in self.calls))
        self.pages[watch.URLS[0]] = html("A model option and an agent configuration behavior apply to this release.", chrome="A newly styled navigation menu")
        self.assertFalse(watch.scan(now=2000, fetcher=self.fetch)["changed"])
        self.pages[watch.URLS[0]] = html("A new model option and a changed agent configuration behavior apply to this release.")
        result = watch.scan(now=3000, fetcher=self.fetch)
        self.assertTrue(result["changed"])
        items = watch.pending()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "changed")
        self.assertEqual(items[0]["url"], watch.URLS[0])
        self.assertIn("new model", items[0]["diff"])
        old_id = items[0]["id"]
        self.assertFalse(watch.scan(now=4000, fetcher=self.fetch)["changed"])
        self.assertEqual(watch.pending()["items"][0]["id"], old_id)
        self.pages[watch.URLS[0]] = html("A third model option and another agent configuration behavior apply to this release.")
        watch.scan(now=5000, fetcher=self.fetch)
        self.assertNotEqual(watch.pending()["items"][0]["id"], old_id)
        self.assertEqual(watch.ack(ids=[old_id])["acknowledged"], [])
        fresh_id = watch.pending()["items"][0]["id"]
        self.assertEqual(watch.ack(ids=[fresh_id])["acknowledged"], [fresh_id])
        self.assertEqual(watch.pending()["count"], 0)
        self.assertTrue(all(e == '"page-v1"' and m for _, e, m in self.calls[len(watch.URLS):]))

    def test_invalid_format_and_failure_preserve_good_article_without_logspam(self):
        watch.scan(now=1000, fetcher=self.fetch)
        path = watch.state_path()
        prior = json.loads(path.read_text())
        target = watch.URLS[1]
        self.pages[target] = b"<html><nav>A false release note</nav></html>"
        failed = watch.scan(now=2000, fetcher=self.fetch)
        self.assertTrue(failed["changed"])
        self.assertEqual(failed["status"], "attention-required")
        self.assertEqual(failed["errors"][target], "page has no complete main article")
        state = json.loads(path.read_text())
        self.assertEqual(state["pages"][target]["hash"], prior["pages"][target]["hash"])
        self.assertEqual(state["pages"][target]["text"], prior["pages"][target]["text"])
        self.assertEqual(state["pages"][target]["status"], "unavailable")
        notice_id = watch.pending()["items"][0]["id"]
        self.assertFalse(watch.scan(now=3000, fetcher=self.fetch)["changed"])
        self.assertEqual(watch.pending()["items"][0]["id"], notice_id)
        self.pages[target] = html("A model option and an agent configuration behavior apply to this release.")
        self.assertTrue(watch.scan(now=4000, fetcher=self.fetch)["changed"])
        self.assertIn("source_recovery", [x["kind"] for x in watch.pending()["items"]])
        self.assertEqual(watch.pickup_hint()["sources"][target]["status"], "ok")

    def test_redirect_and_oversize_do_not_replace_prior_baseline(self):
        watch.scan(now=1000, fetcher=self.fetch)
        target = watch.URLS[2]
        before = json.loads(watch.state_path().read_text())["pages"][target]["hash"]
        def bad_fetch(url, etag, last_modified):
            if url == target:
                return {"status": 200, "url": "https://example.com/redirect", "body": html("A malicious change that should never be trusted by the watch module.")}
            return self.fetch(url, etag, last_modified)
        watch.scan(now=2000, fetcher=bad_fetch)
        self.assertEqual(json.loads(watch.state_path().read_text())["pages"][target]["hash"], before)
        self.pages[target] = b"x" * (watch.MAX_PAGE_BYTES + 1)
        watch.scan(now=3000, fetcher=self.fetch)
        self.assertEqual(json.loads(watch.state_path().read_text())["pages"][target]["hash"], before)
        self.assertEqual(len(watch.URLS), 9)
        self.assertTrue(all(url.startswith(("https://learn.chatgpt.com/docs/", "https://code.claude.com/docs/en/")) for url in watch.URLS))

    def test_304_dedup_due_and_corrupt_state(self):
        watch.scan(now=1000, fetcher=self.fetch)
        unchanged = lambda url, etag, last_modified: {"status": 304, "url": url}
        self.assertFalse(watch.scan(now=2000, fetcher=unchanged)["changed"])
        self.assertEqual(watch.scan(now=3000, fetcher=unchanged, _due=True)["status"], "not-due")
        path = watch.state_path()
        path.write_bytes(b"{bad")
        self.assertEqual(watch.pending()["status"], "attention-required")
        with self.assertRaisesRegex(watch.DocsWatchError, "corrupt"):
            watch.scan(now=4000, fetcher=self.fetch)
        self.assertEqual(path.read_bytes(), b"{bad")

    def test_304_without_baseline_and_change_past_excerpt(self):
        target = watch.URLS[3]
        prior = "A" * (watch.MAX_TEXT + 200)
        self.pages[target] = html(prior)
        watch.scan(now=1000, fetcher=self.fetch)
        state = json.loads(watch.state_path().read_text())
        self.assertEqual(len(state["pages"][target]["text"]), watch.MAX_TEXT)
        self.pages[target] = html(prior[:-1] + "B")
        watch.scan(now=2000, fetcher=self.fetch)
        item = next(x for x in watch.pending()["items"] if x["url"] == target)
        self.assertIn("outside the retained excerpt", item["diff"])
        watch.state_path().unlink()
        watch.scan(now=3000, fetcher=lambda url, etag, last_modified: {"status": 304, "url": url})
        self.assertEqual(watch.pending()["count"], len(watch.URLS))
        self.assertTrue(all(p["status"] == "unavailable" for p in json.loads(watch.state_path().read_text())["pages"].values()))

    def test_write_failure_keeps_prior_bytes_and_redirect_handler_rejects_offsite(self):
        watch.scan(now=1000, fetcher=self.fetch)
        path = watch.state_path()
        before = path.read_bytes()
        self.pages[watch.URLS[0]] = html("A second supported configuration behavior changes this official article today.")
        with mock.patch.object(watch.os, "replace", side_effect=OSError("injected write failure")):
            with self.assertRaises(watch.DocsWatchError):
                watch.scan(now=2000, fetcher=self.fetch)
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(watch.DocsWatchError):
            watch._Redirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/docs/models")

    def test_fetch_byte_and_wall_clock_caps(self):
        class Response:
            status = 200
            headers = {"Content-Type": "text/html"}
            def __init__(self, chunks):
                self.chunks = iter(chunks)
            def geturl(self):
                return watch.URLS[0]
            def read1(self, amount):
                return next(self.chunks, b"")
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        class Opener:
            def __init__(self, chunks):
                self.chunks = chunks
            def open(self, request, timeout):
                return Response(self.chunks)
        with mock.patch.object(watch.urllib.request, "build_opener", return_value=Opener([b"x" * (watch.MAX_PAGE_BYTES + 1)])):
            with self.assertRaisesRegex(watch.DocsWatchError, "byte cap"):
                watch._fetch(watch.URLS[0])
        with mock.patch.object(watch.urllib.request, "build_opener", return_value=Opener([b"x"] * 100)):
            with mock.patch.object(watch.time, "monotonic", side_effect=[0, 1, 8]):
                with self.assertRaisesRegex(watch.DocsWatchError, "time cap"):
                    watch._fetch(watch.URLS[0])

    def test_lazy_schedule_returns_before_fetch_and_ack_is_explicit(self):
        gate = __import__("threading").Event()
        entered = __import__("threading").Event()
        def delayed(url, etag, last_modified):
            entered.set()
            gate.wait(2)
            return self.fetch(url, etag, last_modified)
        start = time.monotonic()
        result = watch.scan_if_due(now=1000, fetcher=delayed)
        self.assertEqual(result["status"], "scheduled")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(entered.wait(1))
        self.assertEqual(watch.scan_if_due(now=1000, fetcher=delayed)["status"], "deferred")
        gate.set()
        for _ in range(100):
            if watch.state_path().exists():
                break
            time.sleep(0.01)
        self.assertTrue(watch.state_path().exists())
        self.assertEqual(watch.scan_if_due(now=1000, fetcher=delayed)["status"], "not-due")
        with self.assertRaises(watch.DocsWatchError):
            watch.ack(ids=None)
