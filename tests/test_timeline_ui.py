"""Behavior checks for timeline aggregation without a browser dependency."""
import shutil
import subprocess
import unittest
from importlib.resources import files


class TimelineUITests(unittest.TestCase):
    def test_grouping_and_partial_usage_preserve_accounting_boundaries(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the timeline aggregation check")
        path = files("eutonos").joinpath("dashboard_static", "app.js")
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('function timelineGroups('),source.indexOf('function timeline(p)'));
const sandbox={metricDate:v=>v&&!Number.isNaN(+new Date(v))?new Date(v).toISOString():null,when:v=>v};
vm.createContext(sandbox);vm.runInContext(code,sandbox);
const rows=[
 {id:'a',end:'2026-09-12T10:00:00Z',model:'astra',role:'primary',usage:{input_tokens:100,cached_input_tokens:80,output_tokens:20,reasoning_output_tokens:5}},
 {id:'b',end:'2026-09-12T10:30:00Z',model:'astra',role:'primary',usage:null},
 {id:'c',end:'2026-09-12T10:35:00Z',model:'luna',role:'support',usage:{input_tokens:50,cached_input_tokens:0,output_tokens:10,reasoning_output_tokens:2}},
 {id:'d',model:'luna',role:'support',usage:null}
];
assert.equal(sandbox.timelineGroups(rows,'turn').groups.length,4);
const grouped=sandbox.timelineGroups(rows,'day');
assert.equal(grouped.groups.length,2); // undated evidence remains its own card
assert.equal(grouped.groups[0].turns.length,3);
assert.equal(grouped.groups[1].id,'d');
const models=sandbox.timelineModels(rows),a=models.find(x=>x.model==='astra');
assert.equal(a.input+a.output,120); // cached and reasoning are subsets, never extra totals
assert.equal(a.known,1);assert.equal(a.turns,2);
const zero=sandbox.timelineModels([{model:'x',usage:{input_tokens:0,cached_input_tokens:0,output_tokens:0,reasoning_output_tokens:0}}])[0];
assert.equal(zero.known,1);assert.equal(zero.input,0);
const original=rows.length;sandbox.timelineGroups(rows,'auto');assert.equal(rows.length,original);
'''
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
