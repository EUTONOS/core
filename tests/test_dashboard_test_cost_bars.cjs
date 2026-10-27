const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/companion.js','utf8');
const palette={astra:'#8670c1',sol:'#c78526',terra:'#328b73',luna:'#478ebc'};
const modelColor=model=>Object.entries(palette).find(([name])=>String(model||'').toLowerCase().includes(name))?.[1]||'#78838f';
const ctx=vm.createContext({
  Intl,URL,URLSearchParams,location:{search:'',href:'http://localhost/'},history:{},
  document:{addEventListener(){},querySelectorAll(){return[]},getElementById(){return null}},
  window:{addEventListener(){},print(){}},
  num:value=>String(value),esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
  modelColor,modelMark:model=>`<i data-color="${modelColor(model)}"></i>`
});
vm.runInContext(source,ctx);
const breakdown=vm.runInContext('benchmarkCostBreakdown',ctx);
const rects=vm.runInContext('benchmarkCostRects',ctx);
const label=vm.runInContext('benchmarkLabelMarkup',ctx);

const result={answer:{cost_usd:4,cost_by_model:{'gpt-6-astra':2.5,'gpt-6-luna':1}}};
assert.deepEqual(JSON.parse(JSON.stringify(breakdown(result,'answer'))),{
  total:4,
  parts:[{model:'gpt-6-astra',value:2.5},{model:'gpt-6-luna',value:1},{model:null,value:.5}]
});
const rendered=rects(result,'answer',80,102);
assert.match(rendered.rects,/benchmark-cost-astra/);
assert.match(rendered.rects,/benchmark-cost-luna/);
assert.match(rendered.rects,/benchmark-cost-unknown/);
assert.match(rendered.label,/Astra \$2\.5000, Luna \$1\.0000, Unattributed \$0\.5000/);
assert.match(label({primary_model:'gpt-6-astra',support_model:'gpt-6-luna'}),/model-astra[\s\S]*Astra[\s\S]*model-luna[\s\S]*Luna/);
assert.deepEqual(JSON.parse(JSON.stringify(breakdown({answer:{cost_usd:1,cost_by_model:{'gpt-6-astra':2}}},'answer'))),{total:1,parts:[{model:null,value:1}]});
console.log('Tests cost bars: model colors, stacked attribution, gray remainder and colored names passed.');
