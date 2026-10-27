const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const escapeHtml=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={project:0,boardsHorizon:'current',campaignPage:0,workHorizon:'active',campaignSort:'priority'};
const ctx=vm.createContext({state,catalogFilters:new Map(),Intl,Date,esc:escapeHtml,num:v=>String(v??0),tag:v=>`[${escapeHtml(v)}]`,
  heading:(a,b)=>`<h1>${a}</h1><p>${b}</p>`,empty:(a,b)=>`EMPTY:${a}:${b}`});
ctx.content={addEventListener(){}};
function evaluate(startMarker,endMarker){
  const start=source.indexOf(startMarker),end=source.indexOf(endMarker,start);
  assert.ok(start>=0&&end>start,`Could not locate app.js range ${startMarker} to ${endMarker}`);
  vm.runInContext(source.slice(start,end),ctx);
}
const run=name=>vm.runInContext(name,ctx);

// Catalogue tabs: Future holds only planned and deferred; started work is not listed there.
evaluate('function catalogCategory','function updateCatalogForm');
evaluate('function completionCheckReference','function workAttributionSummary');
const select=run('catalogSelection'),horizon=run('catalogHorizon'),catalogList=run('catalogList');
const item=(id,status,type='feature')=>({id,type,title:id.toUpperCase(),summary:'About '+id,status,owner_path:'FUTURE_FEATURES.md',extensions:{category:'Workflow'}});
const rows=[item('planned','planned'),item('deferred','deferred'),item('active','active'),item('blocked','blocked'),
  item('done','completed'),item('retired','superseded'),item('dropped','cancelled'),item('other','planned','capability')];
const ids=(h,type='feature')=>Array.from(select(rows,type,h,{sort:'name',direction:'asc'}).items,x=>x.id).sort();
assert.deepEqual(ids('future'),['deferred','planned']);
assert.deepEqual(ids('active'),['active','blocked']);
assert.deepEqual(ids('current'),['done']);
assert.deepEqual(ids('historical'),['dropped','retired']);
assert.deepEqual(ids('future','capability'),['other']);
assert.deepEqual(Array.from(rows,x=>horizon(x)),['future','future','active','active','current','historical','historical','future']);
state.boardsHorizon='future';
let html=catalogList({items:rows},'features');
assert.match(html,/PLANNED/);assert.match(html,/DEFERRED/);
assert.doesNotMatch(html,/ACTIVE|BLOCKED|DONE|RETIRED/);
assert.match(html,/Work in progress is listed under Current Tasks/);
state.boardsHorizon='current';
html=catalogList({items:rows},'features');
assert.match(html,/DONE/);assert.doesNotMatch(html,/ACTIVE|BLOCKED|PLANNED/);

// Queue helpers: only a top-level active or blocked feature or capability is queued work.
evaluate('function workStatusTag','function workCodePattern');
const queued=run('workQueued'),kind=run('workKindTag');
assert.equal(queued({type:'feature',status:'active'}),true);
assert.equal(queued({type:'capability',status:'blocked'}),true);
for(const status of ['planned','deferred','completed','cancelled','superseded'])assert.equal(queued({type:'feature',status}),false,status);
assert.equal(queued({type:'slice',status:'active'}),false);
assert.equal(queued({type:'feature',status:'active',parent_id:'x'}),false);
assert.equal(queued(null),false);
assert.equal(kind({type:'feature'}),'[feature]');assert.equal(kind({type:'capability'}),'[capability]');
assert.equal(kind({type:'slice'}),'');assert.equal(kind(undefined),'');

// Work order rows: a queued catalogue chip names its kind and opens the Work order view; stage chips are unchanged.
evaluate('function shortWorkId','function workCodes');
evaluate('function workRowList','function workOrder(');
const workRowList=run('workRowList');
const work=[{id:'campaign-a',type:'campaign',title:'Campaign A',status:'active'},
  {id:'stage-1',type:'stage',parent_id:'campaign-a',title:'Stage one',status:'active'},
  {id:'feature-x',type:'feature',title:'Feature X',status:'active'},{id:'cap-y',type:'capability',title:'Capability Y',status:'blocked'}];
const order={configured:true,rows:{current:['feature-x',{together:['stage-1','cap-y']}],next:[]}};
html=workRowList({modules:{boards:{items:work,work_order:order}}},false);
assert.match(html,/<a href="#work-order">Feature X<\/a>\[feature\]\[active\]/);
assert.match(html,/<a href="#work-order">Capability Y<\/a>\[capability\]\[blocked\]/);
assert.match(html,/<a href="#work-stage-1">Stage one<\/a>\[Campaign A\]\[active\]/);
assert.doesNotMatch(html,/#work-feature-x|#work-cap-y/);

// Work order view and overview: queued catalogue items sit with the active work, planned ones do not appear.
evaluate('function progressInfo','function workList');
evaluate('function campaignDateInfo','function boards(p)');
evaluate('function workOrder','const skillViews');
const workOrderView=run('workOrder');
const board=[{id:'slice',type:'slice',title:'Slice live',summary:'s',status:'active'},
  {id:'feature-x',type:'feature',title:'Feature X',summary:'f',status:'active',owner_path:'FUTURE_FEATURES.md'},
  {id:'cap-y',type:'capability',title:'Capability Y',summary:'c',status:'blocked',owner_path:'FUTURE_CAPABILITIES.md'},
  {id:'feature-p',type:'feature',title:'Feature planned',summary:'p',status:'planned'},
  {id:'feature-d',type:'feature',title:'Feature shipped',summary:'d',status:'completed'}];
html=workOrderView({modules:{boards:{items:board,work_order:{configured:true,revision:'r',current:['slice','feature-x','cap-y'],next:[]}}}});
const active=html.slice(html.indexOf('<h2>Active</h2>'),html.indexOf('<h2>Not active</h2>'));
assert.match(active,/Slice live/);assert.match(active,/Feature X/);assert.match(active,/Capability Y/);
assert.doesNotMatch(html,/Feature planned|Feature shipped/);
html=workOrderView({modules:{boards:{items:board.slice(0,1).concat(board.slice(3)),work_order:{configured:false,current:[],next:[]}}}});
assert.doesNotMatch(html,/Feature planned|Feature shipped/);
evaluate('function workEntries(','function workBrowser(');
const entriesOf=run('workEntries'),project={modules:{boards:{items:board}}};
const standalone=entries=>Array.from(entries).find(entry=>entry.type==='standalone');
assert.deepEqual(Array.from(standalone(entriesOf(project,'overview','active')).items,i=>i.id).sort(),['cap-y','feature-x','slice']);
assert.equal(standalone(entriesOf(project,'overview','not-active')),undefined);
assert.equal(standalone(entriesOf(project,'overview','done')),undefined);

console.log('Catalogue lifecycle: Future shows only planned and deferred; active and blocked items join the work queue passed.');
