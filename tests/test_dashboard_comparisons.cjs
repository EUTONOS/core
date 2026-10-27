const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const ctx=vm.createContext({
  esc:value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])),
  num:value=>String(value??0),shortModel:value=>String(value??''),
});
const start=source.indexOf('function comparisonBars'),end=source.indexOf('function empty(',start);
assert.ok(start>=0&&end>start,'Could not locate the comparison rendering helpers in app.js');
vm.runInContext(source.slice(start,end),ctx);
const bars=vm.runInContext('comparisonBars',ctx),metrics=vm.runInContext('metricsComparisons',ctx);
const rendered=bars('Counts',[{label:'Zero',value:0},{label:'Missing',value:null},{label:'Small',value:5},{label:'Large',value:10}],'scope');
assert.match(rendered,/width="0" height="8"/);
assert.match(rendered,/width="50" height="8"/);
assert.match(rendered,/width="100" height="8"/);
assert.match(rendered,/Missing<\/span><strong>Unknown/);
assert.doesNotMatch(rendered,/NaN|Infinity/);
assert.match(bars('<bad>',[{label:'<script>',value:-1}],''),/&lt;script&gt;/);
assert.match(metrics({observed_input_tokens:100,observed_cached_input_tokens:80},{},[]),/Other input<\/span><strong>20/);
assert.match(metrics({input_tokens:10,cached_input_tokens:20},{},[]),/Other input<\/span><strong>Unknown/);
assert.match(metrics({},{},[]),/Cached input<\/span><strong>Unknown/);
console.log('Comparison charts: shared scale, zero, unknown, escaped labels and token subsets passed.');
