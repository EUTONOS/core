const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const ctx=vm.createContext({esc:value=>String(value??''),tag:value=>`[${value}]`});
vm.runInContext(source.slice(source.indexOf('function workStatusTag'),source.indexOf('function workCodePattern')),ctx);
vm.runInContext(source.slice(source.indexOf('function shortWorkId'),source.indexOf('function workCodes')),ctx);
vm.runInContext(source.slice(source.indexOf('function workRowList'),source.indexOf('function workOrder(')),ctx);
const workRowList=vm.runInContext('workRowList',ctx);
const items=[
  {id:'campaign-ipc',type:'campaign',title:'Install',status:'active'},
  {id:'stage-6',type:'stage',parent_id:'campaign-ipc',title:'IPC6 trial',status:'planned'},
  {id:'stage-7',type:'stage',parent_id:'campaign-ipc',title:'IPC7 Mac',status:'planned'},
  {id:'stage-8',type:'stage',parent_id:'campaign-ipc',title:'IPC8 installer',status:'active'},
  {id:'campaign-gap',type:'campaign',title:'Goals',status:'planned'},
  {id:'stage-g5',type:'stage',parent_id:'campaign-gap',title:'GAP5',status:'planned'}];
const rows={current:['stage-6',{together:['stage-g5','stage-7','stage-8']}],next:['campaign-ipc','campaign-gap']};
const p={modules:{boards:{items,work_order:{configured:true,rows}}}};

// Stage row: campaign tag and status; together row: one row of chips labelled together.
const full=workRowList(p,false);
assert.match(full,/<li class="work-row"><span class="work-row-number">1<\/span><span class="work-row-chip"><a href="#work-stage-6">IPC6 trial<\/a>\[Install\]\[planned\]/);
assert.match(full,/<span class="work-row-number">2<\/span>\[together\]<span class="work-row-chips">/);
assert.equal((full.match(/work-row-chip"/g)||[]).length,6);
assert.match(full,/Stages of one campaign still run one after another/);
assert.match(full,/Next Work/);
assert.match(full,/no further per-stage approval/);

// A together row across different campaigns carries no sequential note; overview shows the current lane only.
const mixed=workRowList({modules:{boards:{items,work_order:{rows:{current:[{together:['stage-6','stage-g5']}],next:['campaign-gap']}}}}},true);
assert.doesNotMatch(mixed,/one after another/);
assert.doesNotMatch(mixed,/Next Work/);

// No rows: nothing is rendered; an unknown ID still shows as a short chip.
assert.equal(workRowList({modules:{boards:{items,work_order:{rows:{current:[],next:[]}}}}},false),'');
assert.equal(workRowList({modules:{}},true),'');
assert.match(workRowList({modules:{boards:{items,work_order:{rows:{current:['gone'],next:[]}}}}},true),/\[gone\]/);
console.log('work rows ok');
