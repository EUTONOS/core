"""The picker presents owner-allowed efforts while retaining native facts."""
import shutil
import subprocess
import unittest
from pathlib import Path


class LunaEffortPickerTests(unittest.TestCase):
    def test_role_and_routing_pickers_use_allowed_efforts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        source = Path(__file__).resolve().parents[1] / "src/eutonos/dashboard_static/app.js"
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const catalog={models:{luna:{harness:'codex',efforts:['xhigh','max'],supported_efforts:['low','medium','high','xhigh','max']},
 sol:{harness:'codex',efforts:['high','ultra'],supported_efforts:['high','ultra']},
 legacy:{harness:'codex',supported_efforts:['medium']},blocked:{harness:'codex',efforts:[],supported_efforts:['high']}}};
const ctx=vm.createContext({catalog,Object,Array,Set,String,Number,Boolean,esc:x=>String(x),shortModel:x=>x});
vm.runInContext(source.split('\n').find(line=>line.startsWith('function settingOptions')),ctx);
vm.runInContext(source.slice(source.indexOf('function crossTalkModelsByHarness'),source.indexOf('function crossTalkRoleUsedByTasks')),ctx);
const call=expr=>vm.runInContext(expr,ctx);
assert.deepEqual(Array.from(call('crossTalkEffortsForModel(catalog,"luna")')),['xhigh','max']);
assert.deepEqual(Array.from(call('crossTalkEffortsForModel(catalog,"legacy")')),['medium']);
assert.deepEqual(Array.from(call('crossTalkEffortsForModel(catalog,"blocked")')),[]);
const html=call('crossTalkRoleOptionRow("worker",{harness:"codex",model:"luna",effort:"xhigh"},0,1,crossTalkModelsByHarness(catalog),catalog)');
assert.match(html,/value="xhigh"/); assert.match(html,/value="max"/);
assert.doesNotMatch(html,/value="(?:low|medium|high)"/);
const old=call('crossTalkRoleOptionRow("worker",{harness:"codex",model:"luna",effort:"high"},0,1,crossTalkModelsByHarness(catalog),catalog)');
assert.match(old,/value="high" selected disabled/);
ctx.value={model_catalog:catalog};ctx.efforts=['low','medium','high','xhigh','max'];
vm.runInContext(source.split('\n').find(line=>line.includes('catalogModels=')&&line.includes('effortsFor=')),ctx);
assert.deepEqual(Array.from(call('effortsFor("luna")')),['xhigh','max']);
assert.deepEqual(Array.from(call('effortsFor("sol")')),['high','ultra']);
'''
        result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
