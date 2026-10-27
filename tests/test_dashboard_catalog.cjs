const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const escapeHtml=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={project:0,boardsHorizon:'current'};
const ctx=vm.createContext({state,catalogFilters:new Map(),esc:escapeHtml,num:v=>String(v??0),tag:v=>`[${escapeHtml(v)}]`,heading:(a,b)=>`<h1>${a}</h1><p>${b}</p>`});
vm.runInContext(source.slice(source.indexOf('function catalogCategory'),source.indexOf('function updateCatalogForm')),ctx);
vm.runInContext(source.slice(source.indexOf('function completionCheckReference'),source.indexOf('function workAttributionSummary')),ctx);
const select=vm.runInContext('catalogSelection',ctx),date=vm.runInContext('catalogDate',ctx);
const catalogList=vm.runInContext('catalogList',ctx);
const rows=[
 {id:'a',type:'feature',title:'Alpha',summary:'one',status:'completed',owner_path:'FUTURE_FEATURES.md',extensions:{category:'Workflow',horizon:'future',implemented_on:'2026-01-01'}},
 {id:'b',type:'feature',title:'Beta',summary:'two',status:'completed',extensions:{category:'Knowledge',recorded_on:'2026-09-14'}},
 {id:'c',type:'feature',title:'Gamma',summary:'three',status:'planned',extensions:{category:'Knowledge',horizon:'current'}},
 {id:'d',type:'feature',title:'Delta',summary:'four',status:'completed',extensions:{category:'Knowledge',implemented_on:'2026-02-01'}}];
const ids=(h,f)=>Array.from(select(rows,'feature',h,{sort:'name',direction:'asc',...f}).items,x=>x.id);
assert.deepEqual(Array.from(select([...rows,{id:'old',type:'feature',title:'Retired',summary:'x',status:'superseded',extensions:{category:'Workflow'}}],'feature','future',{sort:'name',direction:'asc'}).items,x=>x.id),['c']);
assert.deepEqual(ids('current',{}),['a','b','d']);
assert.deepEqual(ids('future',{}),['c']);
assert.deepEqual(ids('current',{sort:'date',direction:'desc'}),['d','a','b']);
assert.deepEqual(ids('current',{sort:'date'}),['a','d','b']);
assert.deepEqual(ids('current',{sort:'category'}),['b','d','a']);
assert.deepEqual(ids('current',{category:'Workflow'}),['a']);
assert.deepEqual(ids('current',{query:'TWO'}),['b']);
assert.equal(date(rows[1],'current'),null);
assert.equal(date(rows[1],'future'),'2026-09-14');
const checked={id:'checked',type:'feature',title:'Checked',summary:'Outcome',status:'completed',owner_path:'FUTURE_FEATURES.md',extensions:{completion_checks:[
  {id:'source',title:'Source integrated',required:true,state:'verified',evidence:[{path:'src/app.py'}],related_work_ids:[]},
  {id:'paired',title:'Paired acceptance',required:true,state:'partial',evidence:[],remaining_action:'Pair with owner',related_work_ids:[]}
]}};
let html=catalogList({items:[checked]},'features');
assert.match(html,/2 required checks/);
assert.match(html,/1 of 2 required checks verified/);
assert.match(html,/Source and evidence[\s\S]*1 of 2 required checks verified/);
assert.match(html,/Paired acceptance/);
assert.match(html,/Remaining: Pair with owner/);
state.boardsHorizon='future';
html=catalogList({items:[{...checked,id:'capability',type:'capability',status:'planned'}]},'capabilities');
assert.match(html,/1 of 2 required checks verified/);
assert.doesNotMatch(catalogList({items:[rows[2]]},'features'),/required checks verified/);
state.boardsHorizon='current';
const linked={id:'cap',type:'capability',title:'Get started fast',summary:'Install in minutes',status:'completed',extensions:{category:'Delivery',features:['feat-setup','feat-gone']}};
html=catalogList({items:[linked,{id:'feat-setup',type:'feature',title:'Setup app',summary:'Installer',status:'completed',extensions:{category:'Delivery'}}]},'capabilities');
assert.match(html,/catalog-uses[^>]*><span class="quiet">Uses<\/span> Setup app, feat-gone/);
assert.doesNotMatch(catalogList({items:[{...linked,type:'feature'}]},'features'),/catalog-uses/);
console.log('Catalog: status grouping, category/search, sorting and honest dates passed.');
