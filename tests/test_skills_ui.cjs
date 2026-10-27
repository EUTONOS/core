// Pure rendering/event tests, not a substitute for visual browser acceptance.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/eutonos/dashboard_static/app.js','utf8');
const code=source.slice(source.indexOf('const skillViews ='),source.indexOf('const views='));
const listeners={},pending=[];
const box={state:{project:0,view:'skills'},URLSearchParams,FormData,Intl,console,
 esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 num:v=>String(v??0),when:v=>v,heading:(a,b)=>a+b,empty:(a,b)=>a+b,
 render:()=>{},fetch:url=>new Promise(resolve=>pending.push({url,resolve})),
 content:{addEventListener:(name,fn)=>{listeners[name]=fn},querySelector:()=>null}};
box.status=box.tag=box.esc;vm.createContext(box);vm.runInContext(code,box);
const evaluate=s=>vm.runInContext(s,box);
const item={id:'one',title:'<img src=x onerror=alert(1)>',description:'<script>alert(1)</script>',version:'1',status:'usable',tools:['test'],relationships:{uses_tool:['<bad>']},source:{state:'fresh',path:'docs/one.md',sha256:'a'.repeat(64)},usage:{count:null}};
box.p={modules:{skills:{status:'available',items:[item]}}};
let html=evaluate('skills(p)');assert(html.includes('&lt;img'));assert(!html.includes('<script>'));assert(html.includes('Unknown'));assert(!html.includes('0 reported successes'));
item.usage={count:0,success_count:0,failure_count:0};html=evaluate('skills(p)');assert(html.includes('<strong>0</strong>'));assert(html.includes('0 reported successes'));
item.status='stale';html=evaluate('skills(p)');assert(/data-sha="[a]+" disabled/.test(html));
evaluate('skillView().data={catalog_status:"available",status:null,total:0,items:[]}');html=evaluate('skills(p)');assert(html.includes('No matching skills'));assert(!html.includes('Skill catalog unavailable'));
evaluate('skillView().data={catalog_status:"empty",items:[],total:0}');html=evaluate('skills(p)');assert(html.includes('No skills recorded'));assert(!html.includes('Skill catalog unavailable'));
evaluate('skillView().data=null; skillView().detail={body:"<script>execute()</script>"}');html=evaluate('skills(p)');assert(html.includes('&lt;script&gt;execute()'));assert(!html.includes('<script>'));
(async()=>{
 const first=evaluate('skillView().query="first";searchSkills(0)');
 const second=evaluate('skillView().query="second";searchSkills(0)');
 assert(pending[0].url.includes('query=first'));assert(pending[1].url.includes('query=second'));
 pending[1].resolve({ok:true,json:async()=>({catalog_status:'available',items:[],total:2})});await second;
 pending[0].resolve({ok:true,json:async()=>({catalog_status:'available',items:[],total:99})});await first;
 assert.equal(evaluate('skillView().data.total'),2);
 console.log('Skills UI: escaping, unknown/zero uses, stale controls, no-match, exact-body rendering and search race checks passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
