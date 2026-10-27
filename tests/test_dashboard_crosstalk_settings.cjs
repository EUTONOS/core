// AWR-CT: Cross-app talk settings (Crosstalk owner card + roles), reconciled against the
// real backend (settings.py validate_roles, dashboard_cli.py). roles is a DICT of named roles, each an ordered
// options[] fallback chain (harness/model/effort), not a flat array of rows keyed by
// {app,model,effort,autonomy,preamble}. Run directly with Node or through
// the test_dashboard_scripts.py unittest bridge.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');

// Minimal fake DOM: supports the attribute selectors this module actually
// uses ([data-x], [data-x="y"], optionally suffixed :checked). Elements
// carry both `attrs` (for matching) and a `dataset` map (camelCase) so
// dataset.roleOptionIndex etc. resolve like the real DOM.
function el(attrs,extra={}){
  const dataset={};
  for(const k in attrs){
    if(k.startsWith('data-'))dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=attrs[k];
  }
  return {attrs,dataset,checked:false,value:"",hidden:false,...extra};
}
function matchesOne(node,sel){
  let checkedOnly=false;
  if(sel.endsWith(':checked')){checkedOnly=true;sel=sel.slice(0,-8)}
  const m=sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  if(!m)throw new Error(`Unsupported test selector: ${sel}`);
  const [,attr,val]=m;
  if(!(attr in node.attrs))return false;
  if(val!==undefined&&node.attrs[attr]!==val)return false;
  if(checkedOnly&&!node.checked)return false;
  return true;
}
function fakeContent(elements){
  return {
    querySelector(sel){return elements.find(node=>matchesOne(node,sel))||null},
    querySelectorAll(sel){return elements.filter(node=>matchesOne(node,sel))},
    // Several top-level "content.addEventListener(...)" registrations sit textually
    // between the functions under test (routing's generic change handler, the
    // routing-slider input handler, etc.) -- they only need to register without
    // throwing here; none of their callbacks are invoked by this file.
    addEventListener(){},
    contains(){return false},
  };
}
function assertJsonEqual(actual,expected){
  assert.deepEqual(JSON.parse(JSON.stringify(actual)),JSON.parse(JSON.stringify(expected)));
}

const ctx=vm.createContext({
  state:{settingsDrafts:{},settingsFeedback:{},crossTalk:{},hooks:{recent:null,recentLoading:false,recentError:null},hooksEditingSet:null,view:"settings",settingsSection:"cross_talk",project:0,settings:null},
  Array,Object,Number,String,Boolean,Map,Set,JSON,
});
ctx.esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
ctx.num=v=>String(v??0);
ctx.tag=(v,type="")=>`<span class="tag ${type}">${ctx.esc(v)}</span>`;
ctx.shortModel=v=>String(v??"");
ctx.settingOptions=(values,current)=>values.map(v=>`<option value="${ctx.esc(v)}" ${v===current?"selected":""}>${ctx.esc(v)}</option>`).join("");
ctx.metricRangeKey=p=>String(p?.repository_id||p?.scope_id||0);
ctx.queueMicrotask=()=>{}; // drop the deferred status/seen loads; this file tests pure rendering/DOM-reading only
ctx.project=()=>({});
ctx.toast=()=>{};
ctx.render=()=>{};
ctx.saveSettings=()=>{}; // mutators call this; no autosave transport needed for these assertions
ctx.localStorage={getItem(){return null},setItem(){},removeItem(){}};
ctx.document={getElementById(){return null},activeElement:null};
ctx.content=fakeContent([]);
ctx.crosstalkOwnerLoadOnce=()=>{};ctx.crosstalkOwnerCard=()=>"";
// crossTalkWorkerModelDisabledReason calls autoModeRefused/autoModeSupported, which live in the
// jobs section of app.js, outside the crossTalkModelsByHarness..crossTalk(p) slice below.
function fnSource(name){
  const start=source.indexOf('function '+name+'(');
  assert(start>=0,name);
  let depth=0,end=source.indexOf('){',start)+1;
  for(;end<source.length;end++){
    if(source[end]==='{')depth++;
    else if(source[end]==='}'&&--depth===0)break;
  }
  return source.slice(start,end+1);
}
vm.runInContext(['autoModeSupported','autoModeRefused'].map(fnSource).join('\n'),ctx);
vm.runInContext(source.slice(
  source.indexOf('function crossTalkModelsByHarness'),
  source.indexOf('function crossTalk(p){')
),ctx);

const settingValue=vm.runInContext('settingValue',ctx);
const settingsSectionsForNav=vm.runInContext('settingsSectionsForNav',ctx);
const applySettingsSection=vm.runInContext('applySettingsSection',ctx);
const crossTalkSettingsSections=vm.runInContext('crossTalkSettingsSections',ctx);
const crossTalkRolesFromDom=vm.runInContext('crossTalkRolesFromDom',ctx);

// settingValue("roles") no longer scans data-role-row/data-role-field out of the DOM
// at all: roles is a dict, and crossTalkRolesFromDom() is the single source of truth
// once touched -- it clones state.settings.roles into state.settingsDrafts.roles the
// first time anything reads or writes it, then keeps returning that same object.
ctx.state.settings={};
ctx.state.settingsDrafts={};
ctx.state.settings.roles={
  director:{options:[{harness:"claude",model:"claude-sonnet-5",effort:"high"}],preamble:"Standing note",autonomy:"ask"},
};
ctx.state.settingsDrafts={};
const rolesValue=settingValue("roles");
assertJsonEqual(rolesValue,ctx.state.settings.roles); // same content
assert.notEqual(rolesValue,ctx.state.settings.roles); // but a clone, not the same reference
assert.equal(ctx.state.settingsDrafts.roles,rolesValue); // and now cached as the draft
assert.equal(settingValue("roles"),rolesValue); // second read returns the identical cached draft, not a fresh clone

// settingsSectionsForNav / applySettingsSection: the "roles" nav tab shows both
// the "roles" and "hooks" settings-section blocks (Roles sits beside Role hooks);
// every other nav tab still maps 1:1 as before.
assertJsonEqual(settingsSectionsForNav("roles"),["roles","hooks"]);
assertJsonEqual(settingsSectionsForNav("ram"),["ram"]);
const sections=[
  el({"data-settings-section":"hooks"}),
  el({"data-settings-section":"roles"}),
  el({"data-settings-section":"routing"}),
];
ctx.state.settingsSection="roles";
ctx.content=fakeContent(sections);
applySettingsSection();
assert.equal(sections[0].hidden,false);
assert.equal(sections[1].hidden,false);
assert.equal(sections[2].hidden,true);

// crossTalkSettingsSections: neither section has a settings.py "defaults" value
// (unlike routing/task_rules/ram/tracking), so -- per the reconciliation invariant --
// their section head omits the Restore-defaults button entirely rather than
// inventing one that would silently no-op.
const modelCatalog={models:{
  "claude-sonnet-5":{harness:"claude",efforts:["low","medium","high"]},
  "gpt-6-luna":{harness:"codex",efforts:["low","high"]},
}};
const baseValue={
  roles:{director:{options:[{harness:"claude",model:"claude-sonnet-5",effort:"high"}],preamble:"",autonomy:"ask"}},
  model_catalog:modelCatalog,models:["claude-sonnet-5","gpt-6-luna"],
};
ctx.state.settings=baseValue;ctx.state.settingsDrafts={};ctx.state.settingsFeedback={};
ctx.state.crossTalk={};
let html=crossTalkSettingsSections({},baseValue,()=>"");
assert.doesNotMatch(html,/Allowed Claude Desktop names|data-list-add="allowed_claude_desktop_names"/);
assert.doesNotMatch(html,/data-settings-restore="crosstalk"/);
assert.doesNotMatch(html,/data-settings-restore="roles"/);
assert.match(html,/data-role-card="director"/);
assert.match(html,/<option value="claude" selected>Claude<\/option>/);
assert.match(html,/data-role-option-remove[^>]*disabled/); // only one option: remove is disabled
assert.doesNotMatch(html,/data-role-option-add="director"[^>]*disabled/); // 1 of 4 options: add stays enabled

// At the 4-option cap, add-option is disabled; with more than one option, remove is enabled.
const fourOptions={...baseValue,roles:{director:{options:[0,1,2,3].map(()=>({harness:"claude",model:"claude-sonnet-5",effort:"high"})),preamble:"",autonomy:"ask"}}};
ctx.state.settings=fourOptions;ctx.state.settingsDrafts={};
html=crossTalkSettingsSections({},fourOptions,()=>"");
assert.match(html,/data-role-option-add="director"[^>]*disabled/);
assert.doesNotMatch(html,/data-role-option-remove[^>]*disabled/); // 4 options: remove stays enabled

console.log('Cross-talk settings: roles-dict draft caching, no-restore-defaults invariant, and the 4-option cap passed.');
