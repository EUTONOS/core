"""Focused dashboard model-family and pinned-model picker behavior."""
import shutil
import subprocess
import unittest
from pathlib import Path


class DashboardModelPickerTests(unittest.TestCase):
    def run_js(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for dashboard behavior checks")
        path = Path(__file__).parents[1] / "src" / "eutonos" / "dashboard_static" / "app.js"
        syntax = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_catalog_choices_labels_efforts_and_unavailable_pins(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),box={};vm.createContext(box);
const fn=name=>{const i=source.indexOf('function '+name+'(');assert(i>=0,name);let d=0,j=source.indexOf('){',i)+1;for(;j<source.length;j++){if(source[j]==='{')d++;else if(source[j]==='}'&&--d===0)break}return source.slice(i,j+1)};
box.esc=v=>String(v??'').replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
box.shortModel=v=>String(v||'Unknown');
vm.runInContext(['settingOptions','crossTalkModelsByHarness','crossTalkEffortsForModel','crossTalkModelChoiceLabel','crossTalkRoleDefaultOption','autoModeSupported','autoModeRefused','crossTalkWorkerModelDisabledReason','crossTalkRoleModelOptions','crossTalkRoleOptionRow'].map(fn).join('\n'),box);
const catalog={models:{
  'gpt-5.6-sol':{harness:'codex',label:'gpt-5.6-sol',selection_kind:'pin',resolved:'gpt-5.6-sol',available:true,efforts:['low','medium']},
  'sol':{harness:'codex',label:'Sol',selection_kind:'family',resolved:'gpt-6.1-sol',available:true,supported_efforts:['low','high']},
  'gpt-6.1-sol':{harness:'codex',label:'gpt-6.1-sol',selection_kind:'pin',resolved:'gpt-6.1-sol',available:true,supported_efforts:['low','high']},
  'luna':{harness:'codex',label:'Luna',selection_kind:'family',resolved:'gpt-6-luna',available:true,efforts:['low']},
  'sonnet':{harness:'claude',label:'<img src=x onerror=alert(1)>',selection_kind:'family',resolved:'claude-sonnet-5',available:true,supported_efforts:['low','high']},
  'claude-sonnet-5':{harness:'claude',label:'claude-sonnet-5',selection_kind:'pin',resolved:'claude-sonnet-5',available:true,supported_efforts:['low']},
  'gpt-6-astra':{harness:'codex',label:'gpt-6-astra',selection_kind:'pin',resolved:'gpt-6-astra',available:true,efforts:['medium']},
  'retired-model':{harness:'codex',label:'Retired model',available:false,efforts:['high']}
}};
assert.equal(box.crossTalkModelChoiceLabel('sol',catalog),'Sol (latest · gpt-6.1-sol)');
assert.equal(box.crossTalkModelChoiceLabel('gpt-5.6-sol',catalog),'gpt-5.6-sol');
const choices=box.crossTalkModelsByHarness(catalog);
assert(choices.codex.includes('gpt-5.6-sol')); // older pinned versions stay available
assert(choices.codex.includes('gpt-6.1-sol'));
assert(!choices.codex.includes('retired-model'));
const row=box.crossTalkRoleOptionRow('routine',{harness:'codex',model:'sol',effort:''},0,1,choices,catalog);
for(const model of ['gpt-5.6-sol','gpt-6.1-sol','gpt-6-astra','luna','sol'])assert(row.includes(`value="${model}"`),model);
assert(row.includes('Sol (latest · gpt-6.1-sol)'));
assert(!row.includes('retired-model'));
const lowEffort=box.crossTalkRoleOptionRow('routine',{harness:'claude',model:'claude-sonnet-5',effort:''},0,1,choices,catalog);
assert(lowEffort.includes('value="low"'));
assert(!lowEffort.includes('value="high"')); // unsupported effort is not offered
const unavailable=box.crossTalkRoleOptionRow('routine',{harness:'codex',model:'gpt-5.5-sol',effort:'medium'},0,1,choices,catalog);
assert(unavailable.includes('value="gpt-5.5-sol" selected disabled'));
assert(unavailable.includes('(unavailable)'));
assert(unavailable.includes('value="medium" selected disabled')); // preserve saved effort visibly
const hostile=box.crossTalkRoleOptionRow('routine',{harness:'claude',model:'sonnet',effort:''},0,1,choices,catalog);
assert(hostile.includes('&lt;img src=x onerror=alert(1)&gt;'));
assert(!hostile.includes('<img src=x'));
const reversed={models:Object.fromEntries(Object.entries(catalog.models).reverse())};
assert.equal(box.crossTalkRoleDefaultOption(catalog).model,'sonnet');
assert.deepEqual(box.crossTalkRoleDefaultOption(catalog),box.crossTalkRoleDefaultOption(reversed));
assert.equal(box.crossTalkRoleDefaultOption({models:{luna:catalog.models.luna,sol:catalog.models.sol}}).model,'luna');
''')


if __name__ == "__main__":
    unittest.main()
