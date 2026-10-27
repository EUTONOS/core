"""Behavioral checks for shared reads at the dashboard and agent entry points."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from threading import Thread
from http.server import ThreadingHTTPServer
import unittest
from unittest import mock
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

from eutonos import current_work, dashboard, repository_state_cli, workflow, write_retry
from eutonos.host import enroll
from eutonos.dashboard_cli import app
from tests import test_dashboard as fixtures


class CurrentWorkConsumersTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.items = [{"id": "SHARED", "type": "slice", "title": "Shared slice", "summary": "A real source fixture", "status": "active"}]
        self.fixture.write("eutonos.boards.json", json.dumps({"profile": "eutonos.tokenslang.boards.v1", "owners": ["docs/board.md"]}))
        self.board(self.root, self.items)
        self.fixture.git("add", "--all"); self.fixture.git("commit", "-qm", "Native board")
        self.branch = self.root.parent / "branch"
        self.fixture.git("worktree", "add", "-qb", "topic", str(self.branch))
        self.board(self.branch, [*self.items, {"id": "BRANCH", "type": "slice", "title": "Branch-only work", "summary": "Not on Main", "status": "active"}])
        current_work.invalidate(self.root)

    def board(self, root, items):
        (root / "docs/board.md").write_text("# Board\n```tokenslang-board\n" + json.dumps({"profile": "eutonos.tokenslang.board.v1", "items": items}) + "\n```\n", encoding="utf8")

    def test_default_dashboard_and_pickup_see_branch_work_main_stays_separate(self):
        current = dashboard.project(self.root, refresh_state=True)
        delivered = dashboard.project(self.root, work_view="main")
        self.assertIn("BRANCH", {i["id"] for i in current["modules"]["boards"]["items"]})
        self.assertNotIn("BRANCH", {i["id"] for i in delivered["modules"]["boards"]["items"]})
        picked = workflow.pickup(self.root)
        self.assertEqual(picked["outcome"], "pickup")
        self.assertIn("BRANCH", {i["id"] for i in picked["current_tasks"]})
        self.assertTrue(picked["current_work"]["current"][0]["pins"])
        self.assertEqual(current["data_scope"]["view"], "current")

    def test_pickup_queues_active_catalogue_items_and_not_planned_or_completed_ones(self):
        def feature(identity, status):
            return {"id": identity, "type": "feature", "title": identity, "summary": "A named part",
                    "status": status, "extensions": {"category": "Workflow"}}
        self.board(self.branch, [*self.items, feature("FEAT-ON", "active"), feature("FEAT-LATER", "planned"),
                                 feature("FEAT-DONE", "completed")])
        current_work.invalidate(self.root)
        picked = workflow.pickup(self.root)
        listed = {i["id"] for i in picked["current_tasks"]}
        self.assertIn("FEAT-ON", listed)
        self.assertFalse(listed & {"FEAT-LATER", "FEAT-DONE"}, listed)
        self.assertIn("FEAT-ON", {i["id"] for i in picked["current_work"]["current"]})
        self.assertFalse({i["id"] for i in picked["current_work"]["current"]} & {"FEAT-LATER", "FEAT-DONE"})

    def test_http_source_selection_and_forced_refresh(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/v1/current-work?"
        def read(**query):
            with urlopen(base + urlencode({"project": 0, **query}), timeout=15) as response:
                return json.load(response)
        current = read(view="current", refresh="1")
        source = next(s for s in current["modules"]["current_work"]["sources"] if s.get("path") == str(self.branch))
        exact = read(view="source", source=source["id"])
        self.assertIn("BRANCH", {i["id"] for i in exact["modules"]["boards"]["items"]})
        self.board(self.branch, [*self.items, {"id": "LATER", "type": "slice", "title": "New native update", "summary": "Refresh observes it", "status": "active"}])
        self.assertIn("LATER", {i["id"] for i in read(view="current", refresh="1")["modules"]["boards"]["items"]})
        with self.assertRaises(HTTPError) as caught:
            read(view="source", source="../outside")
        self.assertIn(caught.exception.code, (400, 404))

    def test_http_document_pages_reconstruct_large_source_and_reject_changes(self):
        text = "# Large document\n" + "Greek ε and emoji 🙂\n" * 28000
        document = self.branch / "docs/large.md"
        document.write_text(text, encoding="utf-8")
        projection = current_work.project(self.root, refresh=True)
        row = next(row for row in projection["documents"]["items"] if row["path"] == "docs/large.md")
        variant = row["variants"][0]
        server = ThreadingHTTPServer(("127.0.0.1", 0), app([self.root]))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/api/v1/documents/get?"
        query = {"project": 0, "path": row["path"], "source": variant["source_id"], "sha256": variant["sha256"]}
        def read(offset):
            with urlopen(base + urlencode({**query, "offset": offset}), timeout=15) as response:
                return json.load(response)
        content, offset, pages = [], 0, 0
        while offset is not None:
            page = read(offset)
            self.assertEqual(page["offset"], offset)
            self.assertLessEqual(len(page["content"].encode("utf-8")), 131072)
            content.append(page["content"]); offset = page["next_offset"]; pages += 1
        self.assertGreater(pages, 4)
        self.assertEqual("".join(content), document.read_bytes().decode("utf-8"))
        for bad in (-1, "invalid"):
            with self.assertRaises(HTTPError):
                read(bad)
        document.write_text("Changed", encoding="utf-8")
        with self.assertRaises(HTTPError):
            read(0)

    def test_native_write_invalidates_shared_board_before_cache_expiry(self):
        host = self.root.parent / "branch-host"
        enroll(self.branch, host, "writer", ["docs/board.md"], shared=True)
        with mock.patch.object(current_work, "CACHE_SECONDS", 60):
            before = current_work.project(self.root, refresh=True, include_knowledge=False)
            self.assertEqual(next(i for i in before["boards"]["items"] if i["id"] == "BRANCH")["status"], "active")
            write_retry.enqueue(host, self.branch, "writer", "board-update", {
                "owner_path": "docs/board.md", "item_id": "BRANCH", "changes": {"status": "completed"}})
            self.assertEqual(write_retry.run_once(host, self.branch)["outcome"], "complete")
            after = current_work.project(self.root, include_knowledge=False)
            self.assertEqual(next(i for i in after["boards"]["items"] if i["id"] == "BRANCH")["status"], "completed")

    def test_second_clone_observes_published_branch_only_after_fetch(self):
        remote, clone = self.root.parent / "remote.git", self.root.parent / "second-clone"
        self.fixture.git("init", "--bare", "-q", "-b", "main", str(remote))
        self.fixture.git("remote", "add", "origin", str(remote))
        self.fixture.git("push", "-q", "origin", "main")
        self.fixture.git("clone", "-q", str(remote), str(clone))
        self.fixture.git("-C", str(self.branch), "add", "docs/board.md")
        self.fixture.git("-C", str(self.branch), "commit", "-qm", "Complete branch record")
        self.fixture.git("push", "-q", "origin", "topic")
        before = current_work.project(clone, refresh=True, include_knowledge=False)
        self.assertNotIn("BRANCH", {i["id"] for i in before["boards"]["items"]})
        self.fixture.git("-C", str(clone), "fetch", "-q", "origin")
        after = current_work.project(clone, refresh=True, include_knowledge=False)
        self.assertIn("BRANCH", {i["id"] for i in after["boards"]["items"]})
        main = current_work.project(clone, view="main", include_knowledge=False)
        self.assertNotIn("BRANCH", {i["id"] for i in main["boards"]["items"]})
        self.assertNotEqual(before["storage"]["path"], current_work.project(self.root, include_knowledge=False)["storage"]["path"])

    def test_shared_cli_reports_same_branch_work(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = repository_state_cli.main(["--root", str(self.root), "--view", "current", "--summary", "--refresh"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertIn("BRANCH", {i["id"] for i in result["current"]})
        self.assertEqual(result["storage"]["kind"], "rebuildable-device-cache")

    def test_source_controls_escape_untrusted_labels_and_show_conflicts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node required for dashboard behavior")
        app_path = Path(__file__).resolve().parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8');
const box=vm.createContext({state:{view:'overview',workView:'current'},workHashId:()=> 'work-target-123',workRoutePending:false,esc:v=>String(v??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c])),num:String,when:String});
vm.runInContext(text.slice(text.indexOf('function sharedWorkRevision('),text.indexOf('function currentWorkBar(')),box); // shared-work helpers that currentWorkBar and applyCurrentWorkPatch call
vm.runInContext(text.slice(text.indexOf('function currentWorkBar('),text.indexOf('function currentWorkFingerprint(')),box);
box.p={modules:{boards:{conflicts:[{id:'C'}]},current_work:{status:'partial',sources:[{id:'safe',label:'<img src=x>',kind:'local-ref'}],freshness:{kind:'stale'},diagnostics:['<script>bad</script>']}},data_scope:{runtime:'Local usage'}};
const html=vm.runInContext('currentWorkBar(p)',box);
assert(html.includes('Current Work'));assert(html.includes('conflicts to reconcile'));assert(html.includes('stale'));assert(!html.includes('<script>'));assert(!html.includes('<img '));
"""
        script += r"""
vm.runInContext(text.slice(text.indexOf('function applyCurrentWorkPatch('),text.indexOf('async function refreshCurrentWork(')),box);
box.record={modules:{boards:{items:[{id:'OLD'}]},ram:{records:[{id:'MEM'}]},current_work:{}},data_scope:{view:'current'}};
box.patch={shared_modules:['boards'],modules:{current_work:{status:'partial'}},data_scope:{view:'current'}};
vm.runInContext('applyCurrentWorkPatch(record,patch)',box);
assert.equal(box.record.modules.boards.status,'unavailable');assert.equal(box.record.modules.boards.items.length,0);assert.equal(box.record.modules.ram.records[0].id,'MEM');
box.newer={modules:{boards:{items:[{id:'NEW'}]},ram:{records:[{id:'OLDER_MEMORY'}]},current_work:{observed_at:'new'}},data_scope:{view:'current'}};
box.older={modules:{boards:{items:[{id:'OLD'}]},ram:{records:[{id:'FRESH_MEMORY'}]},current_work:{observed_at:'old'}},data_scope:{view:'current'}};
vm.runInContext('retainNewerWork(older,newer)',box);
assert.equal(box.older.modules.boards.items[0].id,'NEW');assert.equal(box.older.modules.ram.records[0].id,'FRESH_MEMORY');
"""
        script += r"""
box.state={project:0,view:'overview',workView:'current',workSource:null,workSelection:0};
Object.assign(box,{memorySearchState:new Map(),render:()=>{},toast:()=>{},load:async()=>false,project:()=>({modules:{current_work:{}}})});
vm.runInContext(text.slice(text.indexOf('function workViewKey('),text.indexOf('function currentWorkBar(')),box);
vm.runInContext(text.slice(text.indexOf('async function selectWorkView('),text.indexOf('content.addEventListener("change",async event=>')),box);
(async()=>{await vm.runInContext('selectWorkView("main")',box);assert.equal(box.state.workView,'current');assert.equal(box.state.workChanging,false);box.load=async()=>true;await vm.runInContext('selectWorkView("main")',box);assert.equal(box.state.workView,'main');assert.equal(box.workRoutePending,true,'source selection must re-arm the active work-target route')})().catch(e=>{console.error(e);process.exitCode=1});
"""
        ran = subprocess.run([node, "-e", script, str(app_path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_current_work_poll_skips_when_no_project_is_selected(self):
        """A fresh install has no selected project: the 2-second poll must not ask for project 0 and report a failure."""
        node = shutil.which("node")
        if not node:
            self.skipTest("Node required for dashboard behavior")
        app_path = Path(__file__).resolve().parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const text=fs.readFileSync(process.argv[1],'utf8');
const calls=[],stamp={textContent:'unchanged'};
const box=vm.createContext({state:{snapshot:{projects:[]},project:0,workRequest:0,workApplied:0,currentWorkLoading:false,workChanging:false},
  document:{hidden:false,activeElement:null},stamp,content:{contains:()=>false},render:()=>{},workViewKey:()=>'k',workQuery:()=>new URLSearchParams(),
  currentWorkFingerprint:()=>'',applyCurrentWorkPatch:()=>{},fetch:async url=>{calls.push(url);return {ok:false,status:400,json:async()=>({})}}});
vm.runInContext('const project=()=>state.snapshot?.projects?.[state.project];',box);
vm.runInContext(text.slice(text.indexOf('async function refreshCurrentWork('),text.indexOf('function documentsPanel(')),box);
(async()=>{
  await vm.runInContext('refreshCurrentWork()',box);
  assert.equal(calls.length,0,'no project selected: nothing is fetched');assert.equal(stamp.textContent,'unchanged');
  box.state.snapshot={projects:[{modules:{current_work:{}}}]};
  await vm.runInContext('refreshCurrentWork()',box);
  assert.equal(calls.length,1,'a selected project is still polled');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        ran = subprocess.run([node, "-e", script, str(app_path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(ran.returncode, 0, ran.stderr)
