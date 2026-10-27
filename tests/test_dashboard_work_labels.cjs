const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const ctx=vm.createContext({state:{},esc:value=>String(value??''),tag:value=>`[${value}]`});
vm.runInContext(source.slice(source.indexOf('function workCodePattern'),source.indexOf('function stageRow')),ctx);
const workCode=vm.runInContext('workCode',ctx),shortWorkId=vm.runInContext('shortWorkId',ctx),workCodes=vm.runInContext('workCodes',ctx),workLabel=vm.runInContext('workLabel',ctx);

// Long shared-work IDs shorten to type plus the first UUID group; short IDs stay whole.
assert.equal(shortWorkId('slice-3281346e-4261-4c93-ad35-1f2327634dc1'),'slice-3281346e');
assert.equal(shortWorkId('campaign-e133ab88e449'),'campaign-e133ab88e449');
assert.equal(shortWorkId('HAO'),'HAO');

// A campaign code comes from a trailing "(CODE)" on a campaign title only.
const ctr={id:'campaign-e133ab88e449',type:'campaign',title:'Crosstalk recovery and reliable pickup (CTR)',owner_path:'docs/campaigns/CROSSTALK_RECOVERY.md'};
assert.equal(workCode(ctr),'CTR');
assert.equal(workCode({type:'slice',title:'Something (CTR)'}),'');
assert.equal(workCode({type:'campaign',title:'No code here'}),'');

// Campaign label: code then title without the parenthetical; full ID on hover.
assert.equal(workLabel(ctr),'<span class="work-id" title="campaign-e133ab88e449">CTR</span> · Crosstalk recovery and reliable pickup');

// A standalone slice in a coded campaign's owner shows the code; the long ID stays on hover.
const slice={id:'slice-3281346e-4261-4c93-ad35-1f2327634dc1',type:'slice',title:'3B.2 Bring chats back',owner_path:ctr.owner_path};
const codes=workCodes([ctr,slice]);
assert.equal(workLabel(slice,codes),'<span class="work-id" title="slice-3281346e-4261-4c93-ad35-1f2327634dc1">CTR</span> · 3B.2 Bring chats back');

// Without a code the short ID is shown.
assert.equal(workLabel({...slice,owner_path:'docs/FUTURE_FEATURES.md'},codes),'<span class="work-id" title="slice-3281346e-4261-4c93-ad35-1f2327634dc1">slice-3281346e</span> · 3B.2 Bring chats back');

// Two different codes in one owner are ambiguous, so no code is borrowed.
const mixed=workCodes([ctr,{id:'campaign-x',type:'campaign',title:'Other (OTH)',owner_path:ctr.owner_path}]);
assert.equal(mixed.get(ctr.owner_path),'');
console.log('work labels ok');
