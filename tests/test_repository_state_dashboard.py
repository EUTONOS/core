"""Read-only Repository state dashboard projection behavior."""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "eutonos" / "dashboard_static" / "app.js"
INDEX = ROOT / "src" / "eutonos" / "dashboard_static" / "index.html"


class RepositoryStateDashboardTests(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        syntax = subprocess.run([node, "--check", str(APP)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        result = subprocess.run([node, "-e", script, str(APP)], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_route_and_overview_offer_projectwide_main_latest_choice(self):
        html = INDEX.read_text(encoding="utf-8")
        source = APP.read_text(encoding="utf-8")
        self.assertIn('href="#repository-state" data-view="repository-state"', html)
        self.assertIn('href="#repository-state"', source)
        self.assertIn('"repository-state":repositoryState', source)
        self.assertIn('if(state.view==="repository-state")return p.modules?.repository_state', source)
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('function repositoryStateRef('),end=source.indexOf('function deliveryReadinessItems(');
assert(start>=0&&end>start,'Repository state renderer missing');
const box=vm.createContext({});
box.esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
box.num=v=>String(v??0);
box.tag=(text,tone='')=>`<span class="tag ${box.esc(tone)}">${box.esc(text)}</span>`;
box.heading=(title,description)=>`<header><h1>${box.esc(title)}</h1><p>${box.esc(description)}</p></header>`;
box.empty=(title,text)=>`<div class="empty"><strong>${box.esc(title)}</strong>${box.esc(text)}</div>`;
vm.runInContext('let repositoryStateSelection="latest",repositoryStateSearch="";'+source.slice(start,end),box);
const pin=(ref,kind='cached-remote-ref',onMain=true)=>({ref,commit:'a'.repeat(40),source_kind:kind,commit_on_main:onMain});
const baseline={id:'AWR1C',type:'campaign',title:'AWR campaign',status:'completed',owner_path:'docs/CURRENT_TASKS.md',
 owner_sha256:'b'.repeat(64),record_sha256:'c'.repeat(64),record_on_main:true,source_integration:'source-commit-on-main',
 publication:'cached-local-ref',provenance:[pin('refs/remotes/origin/main')]};
const rci={id:'RCI1',type:'campaign',title:'RCI pilot',status:'active',owner_path:'docs/WORK_ORDER.md',record_on_main:true,
 source_integration:'source-commit-on-main',publication:'cached-local-ref',provenance:[pin('refs/remotes/origin/main')]};
const awaiting={id:'SLICE-7',type:'slice',title:'Completed slice',status:'completed',owner_path:'docs/WORK_ORDER.md',
 owner_sha256:'d'.repeat(64),record_sha256:'e'.repeat(64),record_on_main:false,source_integration:'retained-unintegrated',
 publication:'unknown',provenance:[pin('refs/heads/feature/slice','local-ref',false)]};
const imported={id:'CAMPAIGN-9',type:'campaign',title:'<img src=x onerror=alert(1)>',status:'completed',owner_path:'docs/<script>.md',
 owner_sha256:'f'.repeat(64),record_sha256:'1'.repeat(64),record_on_main:true,source_import:'import-9',
 source_integration:'unknown-retained-import',publication:'unknown',
 provenance:[pin('refs/heads/imported"><script>','local-ref',false)]};
const conflict={id:'SLICE-C',reason:'Incomparable versions <b>',candidates:[{...awaiting,id:'SLICE-C',title:'Candidate A'}]};
const module={profile:'eutonos.repository-state.v1',status:'partial',observed_at:'2026-10-04T10:00:00Z',
 observation_freshness:{kind:'cached',age_seconds:32},
 qualification:'Local refs do not prove acceptance.',
 main:{ref:'refs/remotes/origin/main',commit:'a'.repeat(40),ref_source:'cached-remote-ref',freshness:'cached-local-ref',status:'available',items:[baseline,rci]},
 latest:{items:[awaiting,imported],completed_unintegrated:[awaiting,imported],conflicts:[conflict],
  historical_candidates:[{id:'AWR1C',classification:'inferred-historical-checkpoint',reason:'Earlier lower-progress checkpoint; review sources.',
   candidate:{...baseline,title:'AWR earlier active checkpoint',status:'active',record_on_main:false,
    provenance:[pin('refs/heads/awr-history','local-ref',false)]}}],
  unclassified_changes:[{path:'src/<iframe>',provenance:[pin('refs/heads/feature/slice','local-ref',false)]}]},
 sources:{refs:[{ref:'refs/heads/feature/slice',commit:'a'.repeat(40),source_kind:'local-ref',status:'observed'}],
  worktrees:[{path:'C:/work/repository-state',branch:'refs/heads/feature/slice',commit:'a'.repeat(40),dirty_paths:2,status:'observed',changed_paths:['docs/<unsafe>.md']}]},
 coverage:{refs_observed:2,worktrees_observed:1,sources_partial:1,owners_partial:0,commands:12,bytes:345,
  refs_truncated:false,worktrees_truncated:false,changed_paths_truncated:false},diagnostics:['Source <partial>']};
box.fixture={modules:{repository_state:module}};
let latest=vm.runInContext('repositoryState(fixture)',box);
assert(latest.includes('data-repository-state-view="main" aria-pressed="false">Main'));
assert(latest.includes('data-repository-state-view="latest" aria-pressed="true">Latest work'));
assert(latest.includes('aria-label="Find work by ID, title or owner path"'));
assert(latest.includes('Active work elsewhere'));
assert(latest.includes('Awaiting integration'));
assert(latest.includes('Completed to reconcile'));
assert(latest.includes('Conflicting records'));
assert(latest.includes('Coverage'));
assert(latest.includes('refs/heads/feature/slice'));
assert(latest.includes('AWR campaign'));
assert(latest.includes('RCI pilot'));
assert(latest.indexOf('RCI pilot')<latest.indexOf('AWR campaign'),'Active Main campaign should lead completed Main work');
assert(latest.includes('Cached observation · 32 sec old'));
assert(latest.includes('2026-10-04T10:00:00Z'));
assert(latest.includes('<details class="card repository-state-historical"'));
assert(latest.includes('Inferred earlier checkpoint'));
assert(latest.includes('AWR earlier active checkpoint'));
assert(latest.includes('refs/heads/awr-history'));
assert(latest.includes('Record on Main'));
assert(latest.includes('Imported source record'));
assert(latest.includes('Imported source pin · integration unknown'));
assert(latest.includes('its source pin is unverified'));
assert(latest.includes('Source commit not verified on Main'));
assert(!latest.includes('<img src=x'));
assert(latest.includes('&lt;img src=x onerror=alert(1)&gt;'));
assert(!latest.includes('src/<iframe>'));
assert(latest.includes('src/&lt;iframe&gt;'));
assert(!latest.includes('refs/heads/imported"><script>'));
assert(latest.includes('refs/heads/imported&quot;&gt;&lt;script&gt;'));
vm.runInContext('repositoryStateSelection="main"',box);
const main=vm.runInContext('repositoryState(fixture)',box);
assert(main.includes('data-repository-state-view="main" aria-pressed="true">Main'));
assert(main.includes('data-repository-state-view="latest" aria-pressed="false">Latest work'));
assert(main.includes('Main baseline'));
assert(main.includes('AWR campaign'));
assert(main.includes('RCI pilot'));
assert(main.includes('remote not refreshed'));
assert(!main.includes('Completed slice'));
assert(!main.includes('Conflicting records'));
vm.runInContext('repositoryStateSelection="latest";repositoryStateSearch="SLICE-7"',box);
const filtered=vm.runInContext('repositoryState(fixture)',box);
assert(filtered.includes('Completed slice'));
assert(!filtered.includes('AWR campaign'));
assert(!filtered.includes('Candidate A'));
vm.runInContext('repositoryStateSearch="Candidate A"',box);
const conflictSearch=vm.runInContext('repositoryState(fixture)',box);
assert(conflictSearch.includes('Candidate A'));
assert(conflictSearch.includes('Filtering records and conflicts'));
assert(!conflictSearch.includes('Completed slice'));
const unavailable=vm.runInContext('repositoryState({modules:{repository_state:{status:"error",reason:"Read failed: <details>"}}})',box);
assert(unavailable.includes('Repository state unavailable'));
assert(unavailable.includes('Read failed: &lt;details&gt;'));
console.log('Main/latest remain distinct, source and record integration stay separate, coverage is visible, and untrusted text is escaped.');
''')


if __name__ == "__main__":
    unittest.main()
