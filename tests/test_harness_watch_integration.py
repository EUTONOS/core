"""The native and official-document watches share review surfaces, not state."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from eutonos import harness_docs_watch, model_watch


DOC_ITEM = {"id": "harness-docs-watch-1", "source": "harness_docs", "kind": "changed",
            "key": "https://code.claude.com/docs/en/hooks", "url": "https://code.claude.com/docs/en/hooks",
            "date": "2026-10-01", "before": "old", "after": "new", "diff": "-old\n+new",
            "first_seen": 101, "updated_at": 101}
DOC_SOURCES = {DOC_ITEM["url"]: {"status": "ok", "last_scan_at": 101, "last_good_at": 101}}


class HarnessWatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {"EUTONOS_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def native_notice(self):
        state = model_watch._fresh_state()
        model_watch._event(state, "codex", "added", "gpt-6.1-sol", None,
                           {"supported_efforts": ["high"], "family": "sol", "effort_basis": "native"}, 100)
        model_watch._write_state(model_watch.state_path(), state)
        return next(iter(state["pending"].values()))["id"]

    def test_lazy_schedules_docs_without_changing_explicit_native_fixture(self):
        with mock.patch.object(model_watch, "_scan", return_value={"status": "not-due"}), \
                mock.patch.object(harness_docs_watch, "scan_if_due", return_value={"status": "scheduled"}) as docs:
            self.assertEqual(model_watch.scan_if_due(now=100), {"status": "not-due"})
            docs.assert_not_called()
            result = model_watch.scan_if_due(root=self.root)
        self.assertEqual(result["documentation"]["status"], "scheduled")
        docs.assert_called_once_with(root=self.root)

    def test_mixed_pending_pickup_and_ack_are_separate_from_catalog(self):
        native_id = self.native_notice()
        docs_items = [dict(DOC_ITEM)]

        def docs_pending(root=None):
            return {"status": "ok", "count": len(docs_items), "items": list(docs_items)}

        def docs_hint(root=None):
            return {"status": "ok", "pending_count": len(docs_items), "items": list(docs_items),
                    "pending_truncated": False, "sources": DOC_SOURCES}

        def docs_ack(root=None, ids=None):
            acknowledged = [item["id"] for item in docs_items if item["id"] in ids]
            docs_items[:] = [item for item in docs_items if item["id"] not in ids]
            return {"status": "ok", "acknowledged": acknowledged, "count": len(docs_items)}

        with mock.patch.object(harness_docs_watch, "pending", side_effect=docs_pending), \
                mock.patch.object(harness_docs_watch, "pickup_hint", side_effect=docs_hint), \
                mock.patch.object(harness_docs_watch, "ack", side_effect=docs_ack):
            self.assertEqual(model_watch.pending()["count"], 2)
            hint = model_watch.pickup_hint()
            self.assertEqual([item["id"] for item in hint["items"]], [native_id, DOC_ITEM["id"]])
            self.assertEqual(hint["documentation"]["sources"], DOC_SOURCES)
            self.assertEqual(model_watch.catalog_snapshot()["pending_count"], 1)
            result = model_watch.ack(ids=[native_id, DOC_ITEM["id"]])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["acknowledged"], sorted([native_id, DOC_ITEM["id"]]))
            self.assertEqual(model_watch.pending()["count"], 0)

    def test_partial_ack_reports_what_cleared_and_keeps_native_notice(self):
        native_id = self.native_notice()
        docs_items = [dict(DOC_ITEM)]

        def docs_pending(root=None):
            return {"status": "ok", "count": len(docs_items), "items": list(docs_items)}

        def docs_ack(root=None, ids=None):
            docs_items.clear()
            return {"status": "ok", "acknowledged": [DOC_ITEM["id"]], "count": 0}

        with mock.patch.object(harness_docs_watch, "pending", side_effect=docs_pending), \
                mock.patch.object(harness_docs_watch, "ack", side_effect=docs_ack), \
                mock.patch.object(model_watch, "local_lock", side_effect=TimeoutError("busy")):
            result = model_watch.ack(ids=[native_id, DOC_ITEM["id"]])
            self.assertEqual(result["status"], "attention-required")
            self.assertEqual(result["acknowledged"], [DOC_ITEM["id"]])
            self.assertEqual(result["count"], 1)
        self.assertEqual(model_watch.catalog_snapshot()["pending_count"], 1)

    def test_corrupt_native_state_does_not_hide_documentation_candidate(self):
        path = model_watch.state_path()
        path.parent.mkdir(parents=True)
        path.write_text("{bad", encoding="utf-8")
        with mock.patch.object(harness_docs_watch, "pending", return_value={
                    "status": "ok", "count": 1, "items": [DOC_ITEM]}), \
                mock.patch.object(harness_docs_watch, "pickup_hint", return_value={
                    "status": "ok", "pending_count": 1, "items": [DOC_ITEM],
                    "pending_truncated": False, "sources": DOC_SOURCES}):
            self.assertEqual(model_watch.pending()["items"], [DOC_ITEM])
            hint = model_watch.pickup_hint()
        self.assertEqual(hint["status"], "attention-required")
        self.assertEqual(hint["items"], [DOC_ITEM])
        self.assertEqual(path.read_text(encoding="utf-8"), "{bad")

    def test_explicit_check_runs_both_foreground_without_rewriting_catalog(self):
        output = io.StringIO()
        with mock.patch.object(model_watch, "scan", return_value={"status": "ok"}) as native, \
                mock.patch.object(harness_docs_watch, "scan", return_value={"status": "ok"}) as docs, \
                mock.patch.object(model_watch, "pending", return_value={"status": "ok", "count": 0, "items": []}), \
                contextlib.redirect_stdout(output):
            exit_code = model_watch.main(["check", "--root", str(self.root)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["documentation"]["status"], "ok")
        native.assert_called_once_with(str(self.root), codex_cache_path=None, claude_models_path=None)
        docs.assert_called_once_with(str(self.root))

    def test_explicit_check_reports_documentation_failure_without_traceback(self):
        output = io.StringIO()
        with mock.patch.object(model_watch, "scan", return_value={"status": "ok"}), \
                mock.patch.object(harness_docs_watch, "scan", side_effect=harness_docs_watch.DocsWatchError("PRIVATE")), \
                mock.patch.object(model_watch, "pending", return_value={"status": "ok", "count": 0, "items": []}), \
                contextlib.redirect_stdout(output):
            exit_code = model_watch.main(["check", "--root", str(self.root)])
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["documentation"]["status"], "attention-required")
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_dashboard_renders_only_trusted_official_links_and_escaped_changes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard rendering check")
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const box={modelWatchReviewing:false,when:n=>String(n),esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
vm.createContext(box);
vm.runInContext(source.slice(source.indexOf('let modelCatalogLoading='),source.indexOf('function refreshModelWatch')),box);
const item={source:'harness_docs',kind:'changed',url:'https://code.claude.com/docs/en/hooks',date:'2026-10-01',diff:'<script>alert(1)</script>'};
const hint={pending_count:1,items:[item],documentation:{sources:{[item.url]:{status:'ok',last_scan_at:100}}}};
const good=box.modelWatchMarkup(hint);
assert(good.includes('harness change'));
assert(good.includes('rel="noopener noreferrer"'));
assert(good.includes('https://code.claude.com/docs/en/hooks'));
assert(good.includes('&lt;script&gt;'));
assert(!good.includes('<script>'));
item.url='https://evil.example/docs/en/hooks" onclick="alert(1)';
const bad=box.modelWatchMarkup(hint);
assert(!bad.includes('href="https://evil.example'));
'''
        path = Path(__file__).parents[1] / "src/eutonos/dashboard_static/app.js"
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
