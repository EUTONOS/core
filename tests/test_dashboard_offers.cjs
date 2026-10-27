const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const html=fs.readFileSync('src/eutonos/dashboard_static/index.html','utf8');
const between=(from,to)=>{const a=source.indexOf(from),b=source.indexOf(to,a);assert.ok(a>=0&&b>a,`missing ${from} .. ${to}`);return source.slice(a,b)};

// The page's own code, run against the real helpers (esc, num, tag, heading, empty) and a stubbed browser.
const handlers={},scrolled=[],fetched=[];
let reply=async()=>({ok:true,status:200,json:async()=>({items:[]})});
const content={innerHTML:'',addEventListener(type,fn){(handlers[type]||(handlers[type]=[])).push(fn)}};
const document={querySelectorAll:()=>[],getElementById:id=>({id,scrollIntoView:options=>scrolled.push([id,options])})};
const state={offers:{data:null,loading:false,error:null},view:'offers'};
const ctx=vm.createContext({state,content,document,console,Intl,String,Map,Array,Error,
  syncSettingsNav(){},syncBoardsNav(){},fetch:async(url,options)=>{fetched.push([url,options]);return reply()}});
vm.runInContext(between('const esc=','const compactNum='),ctx);
vm.runInContext(between('const status=(text','function stat('),ctx);
vm.runInContext(between('function empty(','function chart('),ctx);
vm.runInContext(between('const offersSections','function progressInfo'),ctx);
const page=vm.runInContext('offersPage',ctx),load=vm.runInContext('loadOffers',ctx),renderOffers=vm.runInContext('renderOffers',ctx);

const rows=[
 {id:'f-a',type:'feature',title:'Alpha tool',summary:'Does alpha.',category:'Workflow',status:'completed',horizon:'current'},
 {id:'f-b',type:'feature',title:'Beta tool',summary:'Does beta.',category:'Workflow',status:'completed',horizon:'current'},
 {id:'f-c',type:'feature',title:'Gamma view',summary:'Shows gamma.',category:'Dashboard',status:'completed',horizon:'current'},
 {id:'f-p',type:'feature',title:'Planned one',summary:'Later.',category:'Workflow',status:'planned',horizon:'future'},
 {id:'f-h',type:'feature',title:'Held one',summary:'Paused.',category:'Workflow',status:'deferred',horizon:'future'},
 {id:'f-g',type:'feature',title:'Going one',summary:'Started.',category:'Dashboard',status:'in progress',horizon:'future'},
 {id:'c-now',type:'capability',title:'Get things done',summary:'Use it today.',category:'Workflow',status:'completed',horizon:'current',features:['f-a','f-c','f-nowhere']},
 {id:'c-soon',type:'capability',title:'Do more later',summary:'Planned.',category:'Workflow',status:'planned',horizon:'future',features:['f-p']}];
const loaded=()=>{state.offers.data={profile:'eutonos.offers.v1',items:rows};state.offers.error=null};

// Four sections, in order, each with its own count, grouped by category.
loaded();
let out=page();
const heads=[...out.matchAll(/<h2 id="offers-([a-z-]+)-title">([^<]*) <span class="quiet">(\d+)<\/span><\/h2>/g)].map(m=>[m[1],m[2],m[3]]);
assert.deepEqual(heads,[['current-feature','Current features','3'],['future-feature','Future features','3'],['current-capability','Current capabilities','1'],['future-capability','Future capabilities','1']]);
assert.match(out,/<h1>What EUTONOS offers<\/h1>/);
const sectionOf=key=>out.slice(out.indexOf(`id="offers-${key}"`));
const currentFeatures=out.slice(out.indexOf('id="offers-current-feature"'),out.indexOf('id="offers-future-feature"'));
assert.deepEqual([...currentFeatures.matchAll(/<h3>([^<]*) <span/g)].map(m=>m[1]),['Workflow','Dashboard']);   // the file's own order
assert.deepEqual([...currentFeatures.matchAll(/<h4>([^<]*)<\/h4>/g)].map(m=>m[1]),['Alpha tool','Beta tool','Gamma view']);
assert.match(currentFeatures,/<h3>Workflow <span class="quiet">2<\/span><\/h3>/);

// Status marks appear only on future items.
assert.doesNotMatch(currentFeatures,/class="tag/);
const future=out.slice(out.indexOf('id="offers-future-feature"'),out.indexOf('id="offers-current-capability"'));
assert.match(future,/Planned one<\/h4><span class="tag ">Planned<\/span>/);
assert.match(future,/Held one<\/h4><span class="tag ">On hold<\/span>/);
assert.match(future,/Going one<\/h4><span class="tag good">In progress<\/span>/);

// Capabilities list their features by title, resolved from the IDs; an unknown ID is left out, never shown raw.
const caps=out.slice(out.indexOf('id="offers-current-capability"'),out.indexOf('id="offers-future-capability"'));
assert.match(caps,/<span class="quiet">Uses<\/span> Alpha tool, Gamma view<\/p>/);
assert.doesNotMatch(out,/f-nowhere|f-a<|>f-c</);
assert.match(sectionOf('future-capability'),/Uses<\/span> Planned one<\/p>/);
assert.doesNotMatch(currentFeatures,/Uses/);   // features never carry a uses line

// A section with nothing in it says so.
state.offers.data={profile:'eutonos.offers.v1',items:rows.filter(r=>r.type==='feature')};
out=page();
assert.match(sectionOf('current-capability'),/Nothing is listed here yet\./);
assert.match(out,/<h2 id="offers-current-capability-title">Current capabilities <span class="quiet">0<\/span>/);

// All text is escaped, and the page offers no way to change anything.
state.offers.data={profile:'eutonos.offers.v1',items:[
 {id:'x',type:'feature',title:'<script>alert(1)</script>',summary:'"quoted" & <b>bold</b>',category:'<img src=x onerror=1>',status:'planned',horizon:'future'},
 {id:'y',type:'capability',title:'Cap',summary:'s',category:'C',status:'completed',horizon:'current',features:['x']}]};
out=page();
assert.doesNotMatch(out,/<script|<img|<b>/);
assert.match(out,/&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.match(out,/&quot;quoted&quot; &amp; &lt;b&gt;bold&lt;\/b&gt;/);
assert.match(out,/Uses<\/span> &lt;script&gt;/);
assert.doesNotMatch(out,/<form|<input|<textarea|<select|contenteditable|data-setting|method=|<a /i);
loaded();
out=page();
assert.deepEqual([...out.matchAll(/<button[^>]*>/g)].map(m=>m[0].replace(/data-offers-jump="[a-z-]+"/,'data-offers-jump')),Array(4).fill('<button type="button" data-offers-jump>'));
assert.doesNotMatch(out,/[^\x00-\x7f]/);   // plain ASCII in everything the page itself says

// Loading and failure states.
state.offers.data=null;state.offers.error=null;
assert.match(page(),/Loading the list/);
state.offers.error='Dashboard service returned 500 <b>';
out=page();
assert.match(out,/The list is unavailable/);
assert.match(out,/Dashboard service returned 500 &lt;b&gt;/);
assert.match(out,/<button type="button" data-offers-retry>Try again<\/button>/);
assert.doesNotMatch(out,/data-offers-jump/);

(async()=>{
  // Opening the page reads the shipped list once, with no project and no query, and draws it.
  state.offers={data:null,loading:false,error:null};
  reply=async()=>({ok:true,status:200,json:async()=>({profile:'eutonos.offers.v1',items:rows})});
  renderOffers();
  assert.match(content.innerHTML,/Loading the list/);
  await new Promise(r=>setTimeout(r,0));
  assert.deepEqual(fetched.map(f=>f[0]),['/api/v1/offers']);
  assert.deepEqual(JSON.parse(JSON.stringify(fetched[0][1])),{cache:'no-store'});
  assert.match(content.innerHTML,/Alpha tool/);
  assert.equal(state.offers.loading,false);
  renderOffers();await load();
  assert.equal(fetched.length,1);   // already loaded: no second read unless forced
  await load(true);
  assert.equal(fetched.length,2);

  // A refused or damaged answer shows the service's own words, never a half-read list; Try again reads it again.
  state.offers={data:null,loading:false,error:null};
  reply=async()=>({ok:false,status:500,json:async()=>({outcome:'unavailable',message:'The list of what EUTONOS offers is missing or damaged in this installation.'})});
  renderOffers();
  await new Promise(r=>setTimeout(r,0));
  assert.match(content.innerHTML,/missing or damaged in this installation/);
  assert.equal(state.offers.data,null);
  const before=fetched.length;
  renderOffers();await new Promise(r=>setTimeout(r,0));
  assert.equal(fetched.length,before);   // an error is not retried in a loop
  reply=async()=>({ok:true,status:200,json:async()=>({profile:'eutonos.offers.v1',items:rows})});
  const click=handlers.click.at(-1);
  click({target:{closest:selector=>selector==='[data-offers-retry]'?{}:null}});
  await new Promise(r=>setTimeout(r,0));
  assert.equal(fetched.length,before+1);
  assert.match(content.innerHTML,/Alpha tool/);
  assert.equal(state.offers.error,null);

  state.offers={data:null,loading:false,error:null};
  reply=async()=>({ok:true,status:200,json:async()=>({items:'nope'})});
  await load();
  assert.match(state.offers.error,/could not be read/);
  state.offers={data:null,loading:false,error:null};
  reply=async()=>({ok:false,status:502,json:async()=>{throw new Error('not json')}});
  await load();
  assert.equal(state.offers.error,'Dashboard service returned 502');
  state.offers={data:null,loading:false,error:null};
  reply=async()=>{throw new Error('Failed to fetch')};
  await load();
  assert.equal(state.offers.error,'Failed to fetch');

  // The jump buttons scroll to their section and change nothing else.
  loaded();
  click({target:{closest:selector=>selector==='[data-offers-jump]'?{dataset:{offersJump:'future-capability'}}:null}});
  assert.deepEqual(JSON.parse(JSON.stringify(scrolled)),[['offers-future-capability',{block:'start'}]]);

  // Wiring: the route needs no project or snapshot, and a failed snapshot read must not overwrite the page.
  assert.match(source,/function render\(\)\{const settingsFocused=settingsFocus\(\);if\(state\.view==="offers"\)\{captureSettingsDrafts\(\);captureMetricDrafts\(\);return renderOffers\(\)\}(?:if\(featuresActive\(\)\)\{captureSettingsDrafts\(\);captureMetricDrafts\(\);return renderFeatures\(\)\})?if\(!state\.snapshot\)return;/);
  assert.match(source,/"nav-graph":p=>repoViewDispatch\("nav-graph",p\),offers:offersPage(?:,features:featuresPage)?\}/);
  assert.match(source,/if\(!background&&state\.view!=="offers"(?:&&!featuresActive\(\))?\)content\.innerHTML=empty\("Could not refresh"/);
  assert.match(html,/<a href="#offers" data-view="offers">What EUTONOS offers<\/a>/);
  assert.doesNotMatch(source.slice(source.indexOf('const offersSections'),source.indexOf('function progressInfo')),/method:\s*["']POST|PUT|DELETE|localStorage|sessionStorage/);
  console.log('What EUTONOS offers: sections, categories, feature links, escaping, states and routing passed.');
})().catch(error=>{console.error(error);process.exit(1)});
