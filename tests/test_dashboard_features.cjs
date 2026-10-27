const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const html=fs.readFileSync('src/eutonos/dashboard_static/index.html','utf8');
const between=(from,to)=>{const a=source.indexOf(from),b=source.indexOf(to,a);assert.ok(a>=0&&b>a,`missing ${from} .. ${to}`);return source.slice(a,b)};

// The machine pages' own code (Settings > This computer > Parts, Crosstalk, Claude), run against the real helpers (esc,
// num, tag, heading, empty) and a stubbed browser.
const handlers={},fetched=[];
let reply=async()=>({ok:true,status:200,json:async()=>({})});
const content={innerHTML:'',addEventListener(type,fn){(handlers[type]||(handlers[type]=[])).push(fn)}};
const document={querySelectorAll:()=>[],activeElement:null};
const freshFeatures=()=>({data:null,loading:false,error:null,project:null,serial:0,busy:null,feedback:{},accountsDraft:null,suggest:null,stale:false,focus:null,refreshError:null,denyDraft:'',budgetDraft:null,listDrafts:{}});
const state={features:freshFeatures(),view:'settings',settingsSection:'m_parts',project:0,snapshot:{settings_token:'tok',projects:[{name:'demo',root:'C:/demo'}]}};
const ctx=vm.createContext({state,content,document,console,Intl,String,Map,Array,Error,Number,JSON,Boolean,Date,Object,Set,
  syncSettingsNav(){},syncBoardsNav(){},fetch:async(url,options)=>{fetched.push([url,options?{method:options.method,headers:options.headers,body:options.body}:null]);return reply(url,options)}});
vm.runInContext(between('const esc=','const project='),ctx);
vm.runInContext('const project=()=>state.snapshot?.projects?.[state.project];',ctx);
vm.runInContext(between('const tag=(text','function heading('),ctx);
vm.runInContext(between('function heading(','\n'),ctx);
vm.runInContext(between('function empty(','function chart('),ctx);
vm.runInContext(between('// Features: the machine pages','// end Features'),ctx);
const run=name=>vm.runInContext(name,ctx);
const page=run('featuresPage'),renderFeatures=run('renderFeatures'),loadFeatures=run('loadFeatures'),featuresActive=run('featuresActive');
run('wireFeatures')();   // the page registers its handlers once, at startup
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setTimeout(r,0))};
const click=(selector,dataset)=>{const el={dataset:{...dataset}};handlers.click.at(-1)({target:{closest:s=>s===selector?el:null}})};
const change=(selector,dataset,value)=>handlers.change.at(-1)({target:{closest:s=>s===selector?{dataset,value}:null}});
const typed=(selector,dataset,value)=>handlers.input.at(-1)({target:{closest:s=>s===selector?{dataset,value}:null}});
const posts=()=>fetched.filter(f=>f[1]&&f[1].method==='POST');
const lastBody=()=>JSON.parse(posts().at(-1)[1].body);
const nonAscii=text=>/[^\x00-\x7f]/.test(text);
const slice=(text,from,to)=>{const a=text.indexOf(from);assert.ok(a>=0,`missing ${from}`);const b=to?text.indexOf(to,a+1):-1;return b>a?text.slice(a,b):text.slice(a)};
const rowOf=(text,key)=>slice(text,`data-helper-key="${key}"`,'data-helper-key="');
const buttons=(text,pattern)=>[...text.matchAll(new RegExp(`<(?:button|select|input|textarea)[^>]*(?:${pattern})[^>]*>`,'g'))].map(m=>m[0]);

const NOTES={shared_login:'Leave linked accounts empty on a Windows login that other people use.',ram_project:'x',ram_capture:'y'};
const PARTS={outcome:'ok',home:'C:/home',components:[
  {id:'core',name:'EUTONOS core',state:'installed',enabled:true,toggleable:false,kind:'core'},
  {id:'crosstalk',name:'Crosstalk',state:'installed',enabled:true,toggleable:true,kind:'bundled'},
  {id:'remote-control',name:'Remote Control launcher',state:'installed',enabled:true,toggleable:true,kind:'bundled'},
  {id:'companion',name:'EUTONOS companion',state:'installed',enabled:false,toggleable:true,kind:'bundled',reason:'turned off'},
  {id:'role-hooks',name:'Role reminder hooks',state:'held',enabled:false,toggleable:false,kind:'bundled',reason:'held for later'}]};
const sw=(key,kind,value,label,page,section,more={})=>({key,kind,label,help:label+' help.',page,section,platform:'windows',default:value,available:true,value,stored:false,advanced:false,...more});
const SWITCHES=[
  sw('letter_wake','bool',true,'Wake asleep Claude chats for letters','crosstalk','letters'),
  sw('link_repair_offer','choice','ask','Hermes link repair','crosstalk','apps',{platform:'any',choices:['ask','auto','off'],choice_labels:{ask:'Ask me',auto:'Repair automatically',off:'Only report'}}),
  sw('focus_notice_seconds','number',3,'Focus notice seconds','crosstalk','advanced',{minimum:0,maximum:30}),
  sw('switch_history','accounts',[],'Linked Claude accounts','claude','accounts',{managed_by:'linked-accounts'}),
  sw('history_fix','bool',true,'Keep phone history across an account switch','claude','accounts'),
  sw('relink','bool',true,'Re-enable Remote Control after a Desktop start','claude','remote'),
  sw('relink_kick','bool',true,'Open one chat to start the relink','claude','remote'),
  sw('relink_alert','bool',false,'Show the Enabling Remote Control corner window','claude','remote'),
  sw('always_remote','list',['local_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'],'Always relink these chats','claude','remote',{advanced:true}),
  sw('rename_titles','bool',true,'Fix chat names for the phone','claude','names'),
  sw('rename_now','bool',true,'Fix names right after a Desktop start','claude','names'),
  sw('rename_min_idle_s','number',15,'Idle seconds before a rename','claude','names',{advanced:true,minimum:2,maximum:3600}),
  sw('login_prompt','bool',true,'Ask to sign the CLI in to the Desktop account','claude','signin'),
  sw('chat_copy','bool',false,'Copy chats between linked accounts','claude','copy'),
  sw('chat_copy_deletions','bool',true,'Mirror deletions','claude','copy'),
  sw('chat_copy_snapshots','bool',true,'Daily snapshot of both chat lists','claude','copy')];
const SWITCH_BLOCK=(over={})=>({outcome:'ok',setting:'crosstalk-switches',windows:true,switches:SWITCHES,legacy_copier:{found:false,files:[]},environment:[],...over});
const DENY_KEY='claude:0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d';
const CONTROLS=(over={})=>({outcome:'ok',setting:'crosstalk-controls',halt:false,messaging:true,deny:[DENY_KEY],wake_budget:{per_chat:6,total:20,window_s:3600},wake_budget_limits:{per_chat:[0,1000],total:[0,10000],window_s:[60,86400]},idle:false,summary:'On',recent_letters:[{letter_id:'L-1',from:'claude:a',to:'codex:b',state:'arrived',ts_ns:1760000000000000000}],...over});
const full=(over={})=>({profile:'eutonos.dashboard-features.v1',components:PARTS,notes:NOTES,
  settings:{'crosstalk-messaging':{outcome:'ok',enabled:true,halt:false,notes:[]},
    ram:{outcome:'ok',scope:'project',root:'C:/demo',capture:true,retrieval:'on-demand'},
    'linked-accounts':{outcome:'ok',enabled:true,accounts:['me@example.com'],suggested:[]},
    'update-mode':{outcome:'ok',mode:'auto',modes:['manual','notify','auto'],labels:{manual:'Manual (nothing runs on its own)',notify:'Check and notify',auto:'Check, download, then ask before installing'},enabled:true,notice:'notify',repo:'Owner/Repo',last_checked:null,last_result:null,last_available:null,staged:null},
    'crosstalk-switches':SWITCH_BLOCK(),'crosstalk-controls':CONTROLS()},
  pending:{outcome:'ok',count:1,actions:[{id:'pa-aaaa1111',harness:'claude',source:'component:crosstalk',action:'Ask the person to restart Claude.',why:'Claude reads plugins at start.',created_at:'2026-10-25T00:00:00Z'}]},...over});
const withSettings=(patch,over={})=>full({settings:{...full().settings,...patch},...over});
const fresh=(data,section='m_parts')=>{state.features=freshFeatures();state.features.data=data;state.features.project=0;state.snapshot={settings_token:'tok',projects:[{name:'demo',root:'C:/demo'}]};state.view='settings';state.settingsSection=section;fetched.length=0};
const ok=answer=>async(url,options)=>options&&options.method==='POST'?{ok:true,status:200,json:async()=>answer}:{ok:true,status:200,json:async()=>full()};

// ---- Which page is showing: only the three machine sections of Settings.
state.view='settings';
for(const [section,expected] of [['m_parts',true],['m_crosstalk',true],['m_claude',true],['routing',false],['roles',false],['install_consent',false]]){state.settingsSection=section;assert.equal(featuresActive(),expected,section)}
state.view='boards';state.settingsSection='m_parts';assert.equal(featuresActive(),false);

// ---- A fresh computer: no project, EUTONOS not installed, no Crosstalk kit. Plain states on every page.
const NO_KIT={outcome:'refused',reason:'not-installed',detail:'EUTONOS is not installed here. Install first: eutonos-install install'};
const emptyMachine=()=>({profile:'x',components:{outcome:'not-installed',next_action:'EUTONOS is not installed here.'},notes:NOTES,
  settings:{'crosstalk-messaging':{outcome:'refused',detail:'Crosstalk is not installed here.',next_action:'Install it first.'},
    ram:{outcome:'no-project',detail:'No project is selected.'},'linked-accounts':{outcome:'refused',detail:'Crosstalk is not installed here.'},
    'update-mode':{outcome:'failed',error:'The update settings file is damaged.'},'crosstalk-switches':NO_KIT,'crosstalk-controls':NO_KIT},
  pending:{outcome:'ok',count:0,actions:[]}});
for(const section of ['m_parts','m_crosstalk','m_claude']){
  fresh(emptyMachine(),section);state.snapshot={settings_token:'tok',projects:[]};
  const out=page();
  assert.doesNotMatch(out,/role="switch"|undefined|\[object/,section);
  assert.ok(!nonAscii(out.replace(/\u2192|\u00b7/g,'')),section);
  if(section==='m_parts'){
    assert.match(out,/<h1>Parts<\/h1>/);
    assert.match(out,/EUTONOS is not installed on this computer, so there are no parts to switch yet\./);
    assert.match(out,/Nothing is waiting\./);
    assert.equal([...out.matchAll(/<section class="card features-section"/g)].length,3);
  }else{
    assert.match(out,/EUTONOS is not installed on this computer yet, so there is nothing to switch\. Install it with Setup first\.|Crosstalk status cannot be read right now|switches cannot be read right now/);
    assert.match(out,/EUTONOS is not installed here\./);
  }
}

// ---- Before any answer, and a failed read.
for(const section of ['m_parts','m_crosstalk','m_claude']){
  fresh(null,section);
  assert.match(page(),/Loading/);
  state.features.error='Dashboard service returned 500 <b>';
  const out=page();
  assert.match(out,/This page is unavailable/);
  assert.match(out,/Dashboard service returned 500 &lt;b&gt;/);
  assert.match(out,/data-features-retry/);
}

// ---- Parts page.
fresh(full());
let out=page();
assert.match(out,/<h1>Parts<\/h1>/);
const part=id=>{const a=out.indexOf(`data-feature-part="${id}"`);return out.slice(a,out.indexOf('</li>',a))};
assert.match(part('crosstalk'),/<strong>Crosstalk<\/strong> <span class="tag good">On<\/span>/);
assert.match(part('crosstalk'),/role="switch" aria-checked="true" aria-label="Crosstalk"[^>]*data-feature-component="crosstalk" data-feature-to="false"/);
assert.match(part('companion'),/aria-checked="false"[^>]*data-feature-component="companion" data-feature-to="true"/);
assert.doesNotMatch(part('core'),/role="switch"/);
assert.match(part('core'),/Always on\. This is the EUTONOS core\./);
assert.doesNotMatch(part('role-hooks'),/role="switch"/);
assert.match(part('role-hooks'),/On hold: held for later\. It cannot be switched yet\./);
assert.doesNotMatch(out,/stays on while any/);
assert.match(out,/<option value="auto" selected>Check, download, then ask before installing<\/option>/);
const pending=slice(out,'id="features-pending"');
assert.match(pending,/Pending steps \(1\)/);
assert.match(pending,/<code>pa-aaaa1111<\/code>/);
assert.match(pending,/Why: Claude reads plugins at start\./);
assert.match(pending,/data-feature-pending="done" data-feature-pending-id="pa-aaaa1111"/);
assert.match(pending,/data-feature-pending="dismiss" data-feature-pending-id="pa-aaaa1111"/);
assert.doesNotMatch(out,/Messaging between your chats|Wake asleep|Linked Claude accounts|data-feature-ram/);

// ---- Crosstalk page.
fresh(full(),'m_crosstalk');
out=page();
assert.match(out,/<h1>Crosstalk<\/h1>/);
const status=slice(out,'id="features-status"','id="features-letters"');
assert.match(status,/data-feature-part="crosstalk"/);
assert.match(status,/<span class="status good">On<\/span>/);
assert.match(status,/data-feature-halt data-feature-to="true"[^>]*>Stop Crosstalk<\/button>/);
const letters=slice(out,'id="features-letters"','id="features-apps"');
assert.match(letters,/data-feature-messaging data-feature-to="false"/);
assert.match(rowOf(letters,'letter_wake'),/aria-checked="true"[^>]*data-feature-helper="letter_wake" data-feature-to="false"/);
const advanced=slice(letters,'<details class="features-advanced" data-persist="features-letters-advanced">');
assert.match(advanced,/<code>claude:0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d<\/code>/);
assert.match(advanced,/data-feature-deny="allow" data-feature-deny-key="claude:0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"/);
assert.match(advanced,/data-feature-deny="block"/);
assert.match(advanced,/min="0" max="1000" value="6" data-feature-budget="per_chat"/);
assert.match(advanced,/data-feature-budget-save[^>]* disabled>Save<\/button>/);
const apps=slice(out,'id="features-apps"','id="features-advanced"');
assert.match(apps,/<select data-feature-helper-choice="link_repair_offer"/);
assert.match(apps,/<option value="ask" selected>Ask me<\/option>/);
assert.match(apps,/<option value="auto">Repair automatically<\/option>/);
assert.match(slice(out,'id="features-advanced"','id="features-recent"'),/min="0" max="30" step="1" value="3" data-feature-helper-number="focus_notice_seconds"/);
const recent=slice(out,'id="features-recent"');
assert.match(recent,/<strong>L-1<\/strong>/);
assert.match(recent,/claude:a \u2192 codex:b/);
assert.match(recent,/<span class="tag good">arrived<\/span>/);
assert.doesNotMatch(out,/Chat copying|data-feature-account=/);
assert.match(out,/data-settings-nav="m_claude">Claude page<\/a>/);
state.features.data.settings['crosstalk-controls']=CONTROLS({halt:true,summary:"Halted: the owner's stop is set"});
out=page();
assert.match(out,/<span class="status warn">Halted: the owner&#39;s stop is set<\/span>/);
assert.match(out,/data-feature-halt data-feature-to="false"[^>]*>Resume Crosstalk<\/button>/);
assert.match(out,/<strong>Stopped\.<\/strong> Nothing Crosstalk does will run until you resume\./);
state.features.data.settings['crosstalk-controls']=CONTROLS({idle:true,summary:'Idle: everything is switched off'});
assert.match(page(),/<span class="status ">Idle: everything is switched off<\/span>/);
fresh(full({components:{...PARTS,components:PARTS.components.map(p=>p.id==='crosstalk'?{...p,enabled:false}:p)}}),'m_crosstalk');
out=page();
assert.match(out,/Crosstalk is off on this computer\. Your switches are kept and take effect again when you turn it on\./);
assert.match(slice(out,'data-feature-part="crosstalk"','id="features-letters"'),/aria-checked="false"[^>]*data-feature-component="crosstalk" data-feature-to="true"/);
assert.equal([...out.matchAll(/Crosstalk is off on this computer/g)].length,1);   // one plain line
fresh(full({components:{...PARTS,components:PARTS.components.map(p=>p.id==='crosstalk'?{...p,enabled:false}:p)}}),'m_claude');
assert.match(page(),/Crosstalk is off on this computer\. Your switches are kept and take effect again when you turn it on\./);
fresh(full(),'m_claude');
assert.doesNotMatch(page(),/Crosstalk is off on this computer/);
fresh(withSettings({'crosstalk-controls':{outcome:'failed',detail:'KeyError: x'}}),'m_crosstalk');
out=page();
assert.match(out,/The Crosstalk status cannot be read right now\. KeyError: x/);
assert.match(out,/data-feature-messaging/);
assert.doesNotMatch(out,/data-feature-halt/);

// ---- Claude page.
fresh(full(),'m_claude');
out=page();
assert.match(out,/<h1>Claude<\/h1>/);
const accounts=slice(out,'id="features-accounts"','id="features-remote"');
assert.match(accounts,/Leave linked accounts empty on a Windows login that other people use\./);
assert.match(accounts,/<input type="text" value="me@example\.com" data-feature-account="0"/);
assert.match(accounts,/data-feature-suggest /);
assert.match(accounts,/data-feature-accounts-save[^>]* disabled>Save<\/button>/);
assert.match(rowOf(accounts,'history_fix'),/data-feature-helper="history_fix" data-feature-to="false"/);
assert.doesNotMatch(accounts,/data-helper-key="switch_history"/);
const remote=slice(out,'id="features-remote"','id="features-names"');
assert.match(remote,/data-feature-part="remote-control"/);
assert.match(rowOf(remote,'relink'),/aria-checked="true"[^>]*data-feature-helper="relink"/);
assert.match(rowOf(remote,'relink_alert'),/aria-checked="false"[^>]*data-feature-helper="relink_alert" data-feature-to="true"/);
const remoteAdvanced=slice(remote,'<details class="features-advanced" data-persist="features-remote-advanced">');
assert.match(rowOf(remoteAdvanced,'always_remote'),/<textarea[^>]*data-feature-helper-list="always_remote"[^>]*>local_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa<\/textarea>/);
assert.match(rowOf(remoteAdvanced,'always_remote'),/data-feature-helper-list-save="always_remote"[^>]* disabled>Save<\/button>/);
const names=slice(out,'id="features-names"','id="features-copy"');
assert.match(rowOf(names,'rename_titles'),/data-feature-helper="rename_titles"/);
assert.match(slice(names,'data-persist="features-names-advanced"'),/data-feature-helper-number="rename_min_idle_s"/);
const copy=slice(out,'id="features-copy"','id="features-signin"');
assert.match(rowOf(copy,'chat_copy'),/aria-checked="false"[^>]*data-feature-helper="chat_copy" data-feature-to="true"/);   // off by default
assert.match(rowOf(copy,'chat_copy_deletions'),/aria-checked="true"/);
assert.match(rowOf(copy,'chat_copy_snapshots'),/aria-checked="true"/);
assert.doesNotMatch(copy,/personal chat copier/);
assert.match(slice(out,'id="features-signin"'),/data-feature-helper="login_prompt"/);
assert.doesNotMatch(out,/Messaging between your chats|data-feature-halt|Blocked chats/);
state.features.data.settings['crosstalk-switches']=SWITCH_BLOCK({legacy_copier:{found:true,files:['Claude chat sync.lnk']}});
assert.match(slice(page(),'id="features-copy"'),/Your personal chat copier is still in the Startup folder \(Claude chat sync\.lnk\)\. Remove that shortcut and stop its watcher before you turn EUTONOS chat copying on/);
state.features.data.settings['crosstalk-controls']=CONTROLS({halt:true});
assert.match(page(),/<strong>Crosstalk is stopped\.<\/strong> None of these jobs will run until you resume it/);
state.features.data.settings['crosstalk-switches']=SWITCH_BLOCK({environment:['CROSSTALK_P0_RENAME is off in the environment.']});
assert.match(page(),/CROSSTALK_P0_RENAME is off in the environment\./);

// ---- Not Windows: every Claude switch is listed and disabled with the reason, never hidden; Hermes stays usable.
const offWindows=SWITCHES.map(s=>({...s,available:s.platform==='any'}));
fresh(withSettings({'crosstalk-switches':SWITCH_BLOCK({windows:false,switches:offWindows})}),'m_claude');
out=page();
assert.match(out,/only act on Windows\. Their switches are shown below but cannot be changed on this computer\./);
for(const key of ['history_fix','relink','relink_kick','relink_alert','rename_titles','rename_now','login_prompt','chat_copy','chat_copy_deletions','chat_copy_snapshots']){
  const row=rowOf(out,key);
  assert.ok(row.length>0,key);
  assert.match(row,/<span class="tag ">Not available<\/span>/,key);
  assert.match(row,/Not available on this computer: it only acts on Windows\./,key);
  assert.match(row,/<button[^>]*role="switch"[^>]* disabled>/,key);
}
assert.match(rowOf(out,'always_remote'),/<textarea[^>]* disabled>/);
assert.match(rowOf(out,'rename_min_idle_s'),/<input type="number"[^>]* disabled>/);
fresh(withSettings({'crosstalk-switches':SWITCH_BLOCK({windows:false,switches:offWindows})}),'m_crosstalk');
out=page();
assert.match(rowOf(out,'letter_wake'),/<button[^>]*role="switch"[^>]* disabled>/);
assert.doesNotMatch(rowOf(out,'link_repair_offer'),/<select[^>]* disabled>/);

// ---- Everything is escaped.
fresh(full({components:{outcome:'ok',components:[{id:'x',name:'<script>alert(1)</script>',state:'installed',enabled:true,toggleable:true}]},
  pending:{outcome:'ok',count:1,actions:[{id:'pa-<b>',harness:'any',source:'<i>',action:'"quoted" & <img src=x>',why:'<u>'}]}}));
out=page();
assert.doesNotMatch(out,/<script|<img|<b>|<i>|<u>/);
assert.match(out,/&quot;quoted&quot; &amp; &lt;img src=x&gt;/);
fresh(withSettings({'crosstalk-controls':CONTROLS({deny:['<b>x</b>'],recent_letters:[{letter_id:'<i>',from:'<u>',to:'a',state:'<s>'}]})}),'m_crosstalk');
assert.doesNotMatch(page(),/<b>x|<i>|<u>|<s>/);
fresh(withSettings({'crosstalk-switches':SWITCH_BLOCK({switches:SWITCHES.map(s=>s.key==='always_remote'?{...s,value:['</textarea><script>1</script>']}:s)})}),'m_claude');
assert.doesNotMatch(page(),/<script/);

// ---- Without the dashboard's token, or while a request runs, nothing can be switched on any page.
for(const section of ['m_parts','m_crosstalk','m_claude']){
  fresh(full(),section);
  state.snapshot=null;
  out=page();
  assert.match(out,/The dashboard is still starting\./,section);
  assert.ok(buttons(out,'role="switch"').every(b=>/ disabled/.test(b)),section);
  state.snapshot={projects:[]};
  out=page();
  assert.match(out,/Settings token unavailable; refresh the dashboard/,section);
  assert.ok(buttons(out,'role="switch"|data-feature-helper|data-feature-halt|data-feature-deny|data-feature-budget-save|data-feature-pending=').every(b=>/ disabled/.test(b)),section);
}
fresh(full());
state.features.busy='component:crosstalk';
out=page();
assert.match(out,/Working\.\.\./);
assert.ok(buttons(out,'role="switch"').every(b=>/ disabled/.test(b)));

(async()=>{
  // ---- Opening a machine page with no project reads /api/v1/features with no query and draws it.
  for(const section of ['m_parts','m_crosstalk','m_claude']){
    state.features=freshFeatures();state.snapshot={settings_token:'tok',projects:[]};state.view='settings';state.settingsSection=section;fetched.length=0;
    reply=async()=>({ok:true,status:200,json:async()=>emptyMachine()});
    renderFeatures();
    assert.match(content.innerHTML,/Loading/,section);
    await flush();
    assert.deepEqual(fetched.map(f=>f[0]),['/api/v1/features'],section);
    assert.match(content.innerHTML,/not installed/,section);
  }
  // A project arrives later: the page asks again with that project.
  state.features=freshFeatures();state.snapshot={settings_token:'tok',projects:[]};state.settingsSection='m_parts';fetched.length=0;
  reply=async()=>({ok:true,status:200,json:async()=>full()});
  renderFeatures();await flush();
  state.snapshot={settings_token:'tok',projects:[{name:'demo',root:'C:/demo'}]};
  renderFeatures();await flush();
  assert.deepEqual(fetched.map(f=>f[0]),['/api/v1/features','/api/v1/features?project=0']);

  // ---- Update mode sends only the mode, with the settings token.
  fresh(full());
  reply=ok({outcome:'changed',setting:'update-mode',mode:'notify'});
  change('[data-feature-update-mode]',{},'notify');await flush();
  assert.equal(posts().length,1);
  assert.equal(posts()[0][0],'/api/v1/features/update-mode');
  assert.equal(posts()[0][1].headers['X-Eutonos-Settings'],'tok');
  assert.deepEqual(lastBody(),{mode:'notify'});

  // ---- A part switch: guarded POST, switches lock while it runs, the answer and follow-up steps show.
  fresh(full());
  let release;const gate=new Promise(r=>release=r);
  reply=async(url,options)=>{
    if(options&&options.method==='POST'){await gate;return {ok:true,status:200,json:async()=>({outcome:'disabled',component:'companion',enabled:false,pending:['pa-aaaa1111'],notes:['Some note.']})}}
    return {ok:true,status:200,json:async()=>full()};
  };
  click('[data-feature-component]',{featureComponent:'companion',featureTo:'false'});
  assert.equal(state.features.busy,'component:companion');
  assert.ok(buttons(content.innerHTML,'role="switch"').every(b=>/ disabled/.test(b)));
  click('[data-feature-component]',{featureComponent:'crosstalk',featureTo:'false'});   // a second click while busy does nothing
  await flush();
  assert.equal(posts().length,1);
  release();await flush();
  assert.equal(posts()[0][0],'/api/v1/features/component');
  assert.deepEqual(lastBody(),{component:'companion',enabled:false});
  assert.equal(state.features.busy,null);
  assert.match(content.innerHTML,/EUTONOS companion is now off\./);
  assert.match(content.innerHTML,/Some note\./);
  assert.match(content.innerHTML,/1 follow-up step waiting/);
  assert.match(content.innerHTML,/<li>Ask the person to restart Claude\.<\/li>/);

  // ---- The part switch is just a switch: switching Crosstalk off posts the request and nothing blocks it.
  fresh(full(),'m_crosstalk');
  reply=ok({outcome:'disabled',component:'crosstalk',enabled:false,notes:[]});
  click('[data-feature-component]',{featureComponent:'crosstalk',featureTo:'false'});await flush();
  assert.deepEqual(lastBody(),{component:'crosstalk',enabled:false});
  assert.match(content.innerHTML,/Crosstalk is now off\./);
  assert.doesNotMatch(content.innerHTML,/stays on while any/);

  // ---- "Turn everything off" is a separate button on the Crosstalk page, never a precondition.
  fresh(full(),'m_crosstalk');
  assert.match(page(),/<button type="button" class="secondary" data-feature-everything-off[^>]*>Turn everything off<\/button>/);
  reply=async(url,options)=>options&&options.method==='POST'?{ok:true,status:200,json:async()=>({outcome:'disabled',setting:'crosstalk-everything',turned_off:['Crosstalk messaging (letters)'],notes:[]})}:{ok:true,status:200,json:async()=>full()};
  click('[data-feature-everything-off]',{});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/everything-off');
  assert.deepEqual(lastBody(),{});
  assert.equal(posts().at(-1)[1].headers['X-Eutonos-Settings'],'tok');
  assert.match(content.innerHTML,/Crosstalk is now off\./);
  assert.match(content.innerHTML,/Switched off: Crosstalk messaging \(letters\)\./);

  // ---- Refused, busy, failed and unchanged answers are shown plainly, never as success.
  for(const [answer,pattern] of [
    [{outcome:'refused',reason:'held',detail:'role-hooks is not installed here.'},/Nothing was changed\. role-hooks is not installed here\./],
    [{outcome:'busy',reason:'another run holds the lock'},/Another EUTONOS operation is running, so nothing was changed\. another run holds the lock/],
    [{outcome:'failed',error:'OSError: disk full'},/That did not finish\. OSError: disk full/],
    [{outcome:'unchanged',detail:'companion is already off.'},/companion is already off\./]]){
    fresh(full());reply=ok(answer);
    click('[data-feature-component]',{featureComponent:'companion',featureTo:'true'});await flush();
    assert.match(content.innerHTML,pattern);
    assert.doesNotMatch(content.innerHTML,/is now (on|off)\./);
  }
  // The dashboard refusing the request, or the network failing, shows plainly and unlocks.
  fresh(full(),'m_crosstalk');
  reply=async(url,options)=>options&&options.method==='POST'?{ok:false,status:403,json:async()=>({outcome:'denied'})}:{ok:true,status:200,json:async()=>full()};
  click('[data-feature-messaging]',{featureTo:'false'});await flush();
  assert.match(content.innerHTML,/The dashboard did not accept that request \(403, denied\)/);
  assert.equal(state.features.busy,null);
  fresh(full(),'m_crosstalk');
  reply=async(url,options)=>{if(options&&options.method==='POST')throw new Error('Failed to fetch');return {ok:true,status:200,json:async()=>full()}};
  click('[data-feature-messaging]',{featureTo:'false'});await flush();
  assert.match(content.innerHTML,/Failed to fetch/);
  assert.equal(state.features.busy,null);

  // ---- Crosstalk page writes.
  fresh(full(),'m_crosstalk');
  reply=ok({outcome:'enabled'});
  click('[data-feature-messaging]',{featureTo:'true'});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/messaging');assert.deepEqual(lastBody(),{enabled:true});
  reply=ok({outcome:'disabled'});
  click('[data-feature-helper]',{featureHelper:'letter_wake',featureTo:'false'});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/helper-switch');assert.deepEqual(lastBody(),{key:'letter_wake',value:false});
  assert.match(content.innerHTML,/Wake asleep Claude chats for letters is now off\./);
  reply=ok({outcome:'changed'});
  change('[data-feature-helper-choice]',{featureHelperChoice:'link_repair_offer',featureLabel:'Hermes link repair'},'auto');await flush();
  assert.deepEqual(lastBody(),{key:'link_repair_offer',value:'auto'});
  change('[data-feature-helper-number]',{featureHelperNumber:'focus_notice_seconds',featureLabel:'Focus notice seconds'},'7');await flush();
  assert.deepEqual(lastBody(),{key:'focus_notice_seconds',value:7});
  const count=posts().length;
  change('[data-feature-helper-number]',{featureHelperNumber:'focus_notice_seconds',featureLabel:'Focus notice seconds'},'  ');await flush();
  assert.equal(posts().length,count);
  assert.match(content.innerHTML,/Nothing was changed\. Enter a number\./);
  reply=ok({outcome:'enabled',setting:'halt'});
  click('[data-feature-halt]',{featureTo:'true'});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/halt');assert.deepEqual(lastBody(),{halt:true});
  assert.match(content.innerHTML,/Crosstalk is stopped\./);
  reply=ok({outcome:'disabled',setting:'halt'});
  click('[data-feature-halt]',{featureTo:'false'});await flush();
  assert.deepEqual(lastBody(),{halt:false});
  assert.match(content.innerHTML,/Crosstalk is running again\./);
  // Deny list.
  fresh(full(),'m_crosstalk');
  reply=ok({outcome:'enabled',setting:'deny'});
  typed('[data-feature-deny-input]',{},'  claude:1111 ');
  click('[data-feature-deny]',{featureDeny:'block'});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/deny');assert.deepEqual(lastBody(),{action:'block',key:'claude:1111'});
  assert.equal(state.features.denyDraft,'');
  assert.match(content.innerHTML,/That chat is now blocked\./);
  const before=posts().length;
  click('[data-feature-deny]',{featureDeny:'block'});await flush();
  assert.equal(posts().length,before);
  assert.match(content.innerHTML,/Nothing was changed\. Type the chat key to block\./);
  reply=ok({outcome:'disabled',setting:'deny'});
  click('[data-feature-deny]',{featureDeny:'allow',featureDenyKey:DENY_KEY});await flush();
  assert.deepEqual(lastBody(),{action:'allow',key:DENY_KEY});
  assert.match(content.innerHTML,/That chat is allowed again\./);
  // Wake budget: only changed fields, as whole numbers, only on Save.
  fresh(full(),'m_crosstalk');
  reply=ok({outcome:'changed',setting:'wake-budget'});
  typed('[data-feature-budget]',{featureBudget:'per_chat'},'3');
  typed('[data-feature-budget]',{featureBudget:'window_s'},'600');
  assert.equal(posts().length,0);
  click('[data-feature-budget-save]',{});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/wake-budget');assert.deepEqual(lastBody(),{per_chat:3,window_s:600});
  assert.equal(state.features.budgetDraft,null);
  assert.match(content.innerHTML,/The wake budget was changed\./);
  fresh(full(),'m_crosstalk');
  typed('[data-feature-budget]',{featureBudget:'total'},'2.5');
  click('[data-feature-budget-save]',{});await flush();
  assert.equal(posts().length,0);
  assert.match(content.innerHTML,/Nothing was changed\. Use whole numbers\./);

  // ---- Claude page writes: a list is saved as one ID per line, only on Save; a refused chat copy keeps its step shown.
  fresh(full(),'m_claude');
  reply=ok({outcome:'changed'});
  typed('[data-feature-helper-list]',{featureHelperList:'always_remote'},'local_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb\n\n  local_cccccccc-cccc-4ccc-8ccc-cccccccccccc ,local_dddddddd-dddd-4ddd-8ddd-dddddddddddd');
  assert.equal(posts().length,0);
  renderFeatures();
  assert.doesNotMatch(rowOf(content.innerHTML,'always_remote'),/data-feature-helper-list-save="always_remote"[^>]* disabled>/);   // changed: Save is on
  click('[data-feature-helper-list-save]',{featureHelperListSave:'always_remote',featureLabel:'Always relink these chats'});await flush();
  assert.deepEqual(lastBody(),{key:'always_remote',value:['local_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','local_cccccccc-cccc-4ccc-8ccc-cccccccccccc','local_dddddddd-dddd-4ddd-8ddd-dddddddddddd']});
  assert.equal(state.features.listDrafts.always_remote,undefined);
  fresh(withSettings({'crosstalk-switches':SWITCH_BLOCK({legacy_copier:{found:true,files:['Claude chat sync.lnk']}})}),'m_claude');
  reply=ok({outcome:'refused',reason:'legacy-copier',detail:'Your personal chat copier is still in the Startup folder (Claude chat sync.lnk). Remove that shortcut first.',pending:['pa-aaaa1111']});
  click('[data-feature-helper]',{featureHelper:'chat_copy',featureTo:'true'});await flush();
  assert.deepEqual(lastBody(),{key:'chat_copy',value:true});
  const copyRow=rowOf(content.innerHTML,'chat_copy');
  assert.match(copyRow,/Nothing was changed\. Your personal chat copier is still in the Startup folder/);
  assert.match(copyRow,/1 follow-up step waiting/);
  assert.match(copyRow,/aria-checked="false"/);   // still off
  // Turning one on while the part was off: the answer says when the helper starts.
  fresh(full(),'m_claude');
  reply=ok({outcome:'enabled',notes:['The Crosstalk part was switched off, so it was switched on first. The helper itself starts at your next Windows login.']});
  click('[data-feature-helper]',{featureHelper:'relink_alert',featureTo:'true'});await flush();
  assert.match(rowOf(content.innerHTML,'relink_alert'),/switched on first\. The helper itself starts at your next Windows login/);

  // ---- Suggest writes nothing; adding one only edits the draft; only Save sends the list.
  fresh(full(),'m_claude');
  reply=async(url,options)=>{
    if(options&&options.method==='POST'){const body=JSON.parse(options.body);
      if(body.action==='suggest')return {ok:true,status:200,json:async()=>({outcome:'ok',suggested_emails:['me@example.com','other@example.com']})};
      return {ok:true,status:200,json:async()=>({outcome:'enabled',accounts:body.accounts})}}
    return {ok:true,status:200,json:async()=>full()};
  };
  click('[data-feature-suggest]',{});await flush();
  assert.equal(posts().length,1);
  assert.equal(posts()[0][0],'/api/v1/features/linked-accounts');
  assert.deepEqual(JSON.parse(posts()[0][1].body),{action:'suggest'});
  assert.match(content.innerHTML,/other@example\.com <button type="button" data-feature-suggest-add="0"/);
  assert.doesNotMatch(content.innerHTML,/me@example\.com <button/);
  click('[data-feature-suggest-add]',{featureSuggestAdd:'0'});
  assert.equal(posts().length,1);   // adding is local only
  assert.deepEqual(JSON.parse(JSON.stringify(state.features.accountsDraft)),['me@example.com','other@example.com']);
  assert.match(content.innerHTML,/You have changes that are not saved yet\./);
  typed('[data-feature-account]',{featureAccount:'1'},'  third@example.com ');
  click('[data-feature-accounts-save]',{});await flush();
  assert.equal(posts().length,2);
  assert.deepEqual(JSON.parse(posts()[1][1].body),{action:'save',accounts:['me@example.com','third@example.com']});
  assert.equal(state.features.accountsDraft,null);
  assert.match(content.innerHTML,/Linked accounts is now on\./);
  fresh(full(),'m_claude');
  click('[data-feature-account-remove]',{featureAccountRemove:'0'});
  assert.match(content.innerHTML,/Saving an empty list turns linked accounts off\./);

  // ---- Pending steps: Done is a guarded POST and the list is re-read.
  fresh(full());
  let listed=full();
  reply=async(url,options)=>{
    if(options&&options.method==='POST'){listed=full({pending:{outcome:'ok',count:0,actions:[]}});return {ok:true,status:200,json:async()=>({outcome:'done',id:'pa-aaaa1111',state:'done'})}}
    return {ok:true,status:200,json:async()=>listed};
  };
  click('[data-feature-pending]',{featurePending:'done',featurePendingId:'pa-aaaa1111'});await flush();
  assert.equal(posts().at(-1)[0],'/api/v1/features/pending');
  assert.deepEqual(lastBody(),{id:'pa-aaaa1111',action:'done'});
  assert.match(content.innerHTML,/Nothing is waiting\./);
  assert.match(content.innerHTML,/Marked done\./);

  // ---- A failed read keeps the last good page and says so; a first failed read offers Try again.
  fresh(full());
  reply=async()=>({ok:false,status:500,json:async()=>({})});
  await loadFeatures(true);
  assert.match(content.innerHTML,/The last refresh failed \(Dashboard service returned 500\)/);
  state.features=freshFeatures();
  await loadFeatures(true);
  assert.match(content.innerHTML,/This page is unavailable/);
  const reads=fetched.length;renderFeatures();await flush();
  assert.equal(fetched.length,reads);   // an error is not retried in a loop
  reply=async()=>({ok:true,status:200,json:async()=>full()});
  click('[data-features-retry]',{});await flush();
  assert.match(content.innerHTML,/<h1>Parts<\/h1>/);

  // ---- Handlers only act while a machine page is showing.
  fresh(full());state.settingsSection='routing';fetched.length=0;
  click('[data-feature-component]',{featureComponent:'companion',featureTo:'true'});await flush();
  assert.equal(fetched.length,0);

  // ---- Wiring: the machine pages render without a snapshot or a project, #features redirects, the nav carries the pages.
  assert.match(source,/if\(featuresActive\(\)\)\{captureSettingsDrafts\(\);captureMetricDrafts\(\);return renderFeatures\(\)\}/);
  assert.match(source,/history\.replaceState\(null,"","#settings"\)/);
  assert.match(source,/state\.settingsSection="m_parts"/);
  assert.doesNotMatch(source,/features:featuresPage/);
  assert.match(source,/!featuresActive\(\)\)content\.innerHTML=empty\("Could not refresh"/);
  assert.equal([...source.matchAll(/wireFeatures\(\)/g)].length,2);   // the call at startup and the definition
  for(const [section,label] of [['m_parts','Parts'],['m_crosstalk','Crosstalk'],['m_claude','Claude']])assert.match(html,new RegExp(`<a href="#settings" data-settings-nav="${section}">${label}</a>`));
  assert.doesNotMatch(html,/data-view="features"/);
  assert.equal([...html.matchAll(/data-settings-nav="delivery"/g)].length,1);   // the duplicated link is gone
  assert.match(source,/function settingsSectionsForNav\(nav\)\{return nav==="roles"\?\["roles","hooks"\]/);
  const block=between('// Features: the machine pages','// end Features');
  assert.deepEqual([...block.matchAll(/method:"([A-Z]+)"/g)].map(m=>m[1]),['POST']);   // one write helper, and it is guarded
  assert.match(block,/"X-Eutonos-Settings":token/);
  assert.doesNotMatch(block,/localStorage|sessionStorage|"\/api\/v1\/(?!features)/);
  console.log('Machine pages: states, switches, the part switch and Turn everything off, guarded writes, suggestions, pending steps and routing passed.');
})().catch(error=>{console.error(error);process.exit(1)});
