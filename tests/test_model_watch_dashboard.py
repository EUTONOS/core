"""Portable watch hooks and live picker refresh; local metadata/fake transports only."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from eutonos import dashboard, dashboard_cli, model_catalog, model_watch, workflow
import test_workflow as workflow_fixtures


HINT = {"status": "ok", "pending_count": 1, "items": [{"id": "change-1", "source": "codex",
        "kind": "added", "key": "gpt-6.1-sol"}], "pending_truncated": False,
        "sources": {"codex": {"status": "ok", "basis": "native", "last_scan_at": 100,
                              "last_good_at": 100}, "claude": {"status": "unknown"}}}
CATALOG = {"models": {"sol": {"harness": "codex", "available": True, "label": "Sol"},
                      "old-pin": {"harness": "codex", "available": False}}}


class WatchDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scan = mock.patch.object(model_watch, "scan_if_due", return_value={"status": "not-due"}, create=True)
        self.scan = scan.start()
        self.addCleanup(scan.stop)
        hint = mock.patch.object(model_watch, "pickup_hint", return_value=HINT, create=True)
        hint.start()
        self.addCleanup(hint.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_cli.app([self.root, self.root]))
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return json.load(response)

    def post(self, ids, token, origin=None):
        request = Request(self.base + "/api/v1/model-watch/ack", data=json.dumps(ids).encode(),
                          headers={"Content-Type": "application/json", "Origin": origin or self.base,
                                   "X-Eutonos-Settings": token}, method="POST")
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_snapshot_scans_once_outside_project_projection(self):
        with mock.patch.object(dashboard, "snapshot", return_value={"projects": [{}, {}]}):
            result = self.get("/api/v1/snapshot")
        self.scan.assert_called_once_with(root=self.root)
        self.assertEqual(result["model_watch"], HINT)

    def test_watch_failure_isolated_from_dashboard_and_private_details(self):
        self.scan.side_effect = ValueError("PRIVATE account/path/prompt")
        with mock.patch.object(dashboard, "snapshot", return_value={"projects": []}):
            result = self.get("/api/v1/snapshot")
        self.assertEqual(result["projects"], [])
        self.assertIn("error", result["model_watch"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_catalog_refresh_is_read_only_and_has_no_settings_revisions(self):
        with mock.patch.object(model_catalog, "describe_catalog", return_value=CATALOG):
            result = self.get("/api/v1/model-watch/catalog?project=0")
        self.scan.assert_not_called()
        self.assertEqual(result["models"], ["sol"])
        self.assertEqual(result["model_catalog"], CATALOG)
        self.assertNotIn("revision", result)
        with self.assertRaises(HTTPError) as rejected:
            self.get("/api/v1/model-watch/catalog?project=999")
        self.assertEqual(rejected.exception.code, 400)

    def test_review_requires_token_origin_and_explicit_shown_ids(self):
        with mock.patch.object(dashboard, "snapshot", return_value={"projects": []}):
            token = self.get("/api/v1/snapshot")["settings_token"]
        with mock.patch.object(model_watch, "ack", return_value={"status": "ok"}, create=True) as ack:
            with self.assertRaises(HTTPError) as denied:
                self.post({"ids": ["change-1"]}, "wrong-token")
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(HTTPError) as wrong_origin:
                self.post({"ids": ["change-1"]}, token, origin="http://untrusted.invalid")
            self.assertEqual(wrong_origin.exception.code, 403)
            with self.assertRaises(HTTPError) as invalid:
                self.post({}, token)
            self.assertEqual(invalid.exception.code, 400)
            ack.assert_not_called()
            result = self.post({"ids": ["change-1"]}, token)
            ack.assert_called_once_with(ids=["change-1"])
            self.assertEqual(result["status"], "ok")

    def test_busy_scan_keeps_last_good_hint_and_review_failure_stays_pending(self):
        self.scan.return_value = {"status": "attention-required", "error": "PRIVATE lock path"}
        with mock.patch.object(dashboard, "snapshot", return_value={"projects": []}):
            snapshot = self.get("/api/v1/snapshot")
        self.assertEqual(snapshot["model_watch"]["pending_count"], 1)
        self.assertEqual(snapshot["model_watch"]["sources"], HINT["sources"])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        with mock.patch.object(model_watch, "ack", side_effect=OSError("PRIVATE"), create=True):
            result = self.post({"ids": ["change-1"]}, snapshot["settings_token"])
        self.assertEqual(result["status"], "attention-required")
        self.assertEqual(result["model_watch"]["pending_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_pickup_watch_failure_never_changes_successful_boot(self):
        fixture = workflow_fixtures.WorkflowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.scan.side_effect = ValueError("corrupt or busy metadata")
        result = workflow.pickup(fixture.root)
        self.assertEqual(result["outcome"], "pickup")
        self.assertIn("error", result["model_watch"])


class WatchPickerTests(unittest.TestCase):
    def test_live_refresh_preserves_drafts_save_and_cursor_without_post(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        path = Path(__file__).parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const original={models:{old:{harness:'codex',available:true,efforts:['high']}}};
const next={models:{old:{harness:'codex',available:false,efforts:['high']},fresh:{harness:'codex',available:true,efforts:['low']}}};
const text={value:'unsaved text',selectionStart:4,selectionEnd:7};
const selected={value:'old',innerHTML:'',closest(){return row}};
const thinking={value:'high',innerHTML:''};
const row={dataset:{roleName:'desk'},querySelector(s){return s.includes('harness')?{value:'codex'}:thinking}};
const checkbox={checked:true,dataset:{settingModel:'old'},getAttribute(){return 'old'}};
const state={project:0,view:'settings',settings:{revision:'cas',roles_revision:'roles-cas',model_catalog:original,models:['old']},settingsDrafts:{roles:{desk:{description:'draft'}}},snapshot:{model_watch:null}};
const saver={snapshot:state.settings,drafts:new Map([['roles',state.settingsDrafts.roles]]),pending:new Map([['roles',state.settingsDrafts.roles]]),running:Promise.resolve(),blocked:false};
let posts=0;
const box={state,settingsSavers:new Map([[0,saver]]),document:{activeElement:text,addEventListener(){},getElementById(){return null},querySelector(){return null}},
  content:{querySelectorAll(s){return s.startsWith('select')?[selected]:s.includes(':checked')?[checkbox]:[]},querySelector(){return null}},
  fetch(){posts++;throw Error('No refresh POST allowed')},when:n=>String(n),esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  crossTalkModelChoiceLabel:n=>n,settingOptions:(names,current)=>names.map(n=>`<option value="${n}" ${n===current?'selected':''}>${n}</option>`).join(''),
  crossTalkModelsByHarness:c=>({codex:Object.keys(c.models).filter(n=>c.models[n].available!==false)}),crossTalkEffortsForModel:(c,n)=>c.models[n]?.efforts||[],console};
vm.createContext(box);
const roleHelpersStart=source.indexOf('function crossTalkWorkerModelDisabledReason('),roleHelpersEnd=source.indexOf('function crossTalkRoleOptionRow(',roleHelpersStart);
assert(roleHelpersStart>=0&&roleHelpersEnd>roleHelpersStart);
vm.runInContext(source.slice(roleHelpersStart,roleHelpersEnd),box);
vm.runInContext(source.slice(source.indexOf('let modelCatalogLoading='),source.indexOf('function settingsSectionsForNav')),box);
const drafts=saver.drafts,pending=saver.pending,running=saver.running;
box.applyModelCatalog({model_catalog:next,models:['fresh']},0);
assert.equal(state.settings.revision,'cas');assert.equal(saver.snapshot.roles_revision,'roles-cas');
assert.strictEqual(saver.drafts,drafts);assert.strictEqual(saver.pending,pending);assert.strictEqual(saver.running,running);
assert.strictEqual(state.settingsDrafts.roles,saver.drafts.get('roles'));
assert(selected.innerHTML.includes('value="old" selected disabled'));assert(selected.innerHTML.includes('value="fresh"'));
assert.equal(text.value,'unsaved text');assert.equal(text.selectionStart,4);assert.equal(text.selectionEnd,7);assert.equal(posts,0);
const before=state.settings;box.applyModelCatalog({model_catalog:original,models:['old']},1);assert.strictEqual(state.settings,before);
const i=source.indexOf('function mergeSettingsResult('),j=source.indexOf('const settingsSavers=',i);
vm.runInContext(source.slice(i,j),box);
const merged=box.mergeSettingsResult(saver.snapshot,'routing',{revision:'accepted-cas',models:['old'],model_catalog:original});
assert.equal(merged.revision,'accepted-cas');assert.strictEqual(merged.model_catalog,next);
const hostile=box.modelWatchMarkup({pending_count:1,items:[{source:'codex',kind:'added',key:'<img src=x>'}],sources:{claude:{status:'unknown'}}});
assert(hostile.includes('&lt;img src=x&gt;'));assert(!hostile.includes('<img src=x>'));assert(hostile.includes('Unknown'));
'''
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
