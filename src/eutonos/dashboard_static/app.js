"use strict";
const state={workRequest:0,workApplied:0,snapshotApplied:0,snapshotLoading:0,workChanging:false,workSelection:0,workView:"current",workSource:null,currentWorkLoading:false,documentBodies:{},snapshot:null,project:0,view:"overview",runModule:"nav",timer:null,settings:null,settingsProject:null,settingsLoading:false,settingsFeedback:{},settingsDrafts:{},participantProfiles:{},settingsSection:"routing",settingsNavExpanded:false,boardsSection:"quick",boardsHorizon:"current",boardsNavExpanded:false,workHorizon:"active",overviewCampaignIndex:0,campaignPage:0,campaignSort:"priority",sliceStatus:"active",discardSettingsDraft:null,metricProjection:null,metricLoading:false,metricRanges:{},metricDrafts:{},metricRangeUpdating:null,metricExpanded:false,metricCustomize:false,metricDrag:null,metricLayouts:{},timelinePrefs:{},timelineScroll:null,timelineFollowNewest:true,crossTalk:{},crossTalkRoleDraft:null,crossTalkTab:"metrics",hooks:{recent:null,recentLoading:false,recentError:null},hooksEditingSet:null,offers:{data:null,loading:false,error:null},features:{data:null,loading:false,error:null,project:null,serial:0,busy:null,feedback:{},accountsDraft:null,suggest:null,stale:false,focus:null,refreshError:null,denyDraft:"",budgetDraft:null,listDrafts:{}}};
const content=document.getElementById("content"),select=document.getElementById("project-select"),stamp=document.getElementById("snapshot-state");
let repositoryStateSelection="latest",repositoryStateSearch="";
function workQuery(extra={}){const q=new URLSearchParams({project:String(state.project),view:state.workView,...extra});if(state.workSource)q.set("source",state.workSource);return q}
function sharedWorkRevision(p){return p?.modules?.current_work?.shared_work?.revision||p?.current_work?.shared_work?.revision||""}
function workViewKey(){const configured=state.settingsProject===state.project?(state.settings?.shared_work_revision||""):"";return String(state.project)+":"+state.workView+":"+(state.workSource||"")+":"+configured+":"+sharedWorkRevision(project())}
function sharedWorkValue(value){if(value===null||value===undefined||value==="")return "Unknown";if(typeof value==="string"||typeof value==="number"||typeof value==="boolean")return String(value);return String(value.status||value.state||value.kind||value.result||"Recorded")}
function sharedWorkObservationMarkup(w){
 const shared=w.shared_work,attempts=w.attempts,rows=[];
 if(shared?.canonical)rows.push({label:"Canonical repository",...shared.canonical});
 for(const row of shared?.peers||[])rows.push({label:"Peer source",...row});
 const sourceRows=rows.map(row=>"<article class=\"shared-work-source\"><div><strong>"+esc(row.label)+"</strong> "+tag(sharedWorkValue(row.status),row.status==="available"?"good":"warn")+"</div><p>"+esc(row.url||"Source URL unavailable")+" · "+esc(row.ref||"ref unavailable")+"</p><p>Commit "+esc(row.commit||"unknown")+" · Last successful fetch "+esc(row.last_successful_fetch?when(row.last_successful_fetch):"unknown")+"</p></article>").join("");
 const items=Array.isArray(attempts?.items)?attempts.items:[],shown=items.slice(0,50),byId=new Map();for(const item of items)for(const id of [item.id,item.attempt_id])if(id!==undefined&&id!==null)byId.set(id,item);
 const attemptRows=shown.map(item=>{const target=item.target||{},scope=Array.isArray(target.ancestors)?target.ancestors.join(" → "):target.ancestors;return "<article class=\"shared-work-attempt\"><div class=\"section-head\"><strong>"+esc(target.work_id||item.id||"Unpinned target")+"</strong>"+tag(sharedWorkValue(item.state),item.state==="active"?"good":"")+(item.intentional_parallel?tag("Intentional parallel","warn"):"")+"</div><p>"+esc(item.participant||"Participant unknown")+" · session "+esc(item.session||"unknown")+" · "+esc(item.branch||"branch unknown")+"</p><p>"+esc(item.intent||"No intent recorded")+" · checkpoint: "+esc(item.checkpoint||"none")+"</p><p>Target pin: "+esc(target.source_ref||"unknown ref")+" · commit "+esc(target.source_commit||"unknown")+" · revision "+esc(target.revision||"unknown")+(scope?" · scope "+esc(scope):"")+"</p><p>Observed canonical commit "+esc(item.canonical_commit||"unknown")+" · publication "+esc(sharedWorkValue(item.publication))+" · source integration "+esc(sharedWorkValue(item.source_integration))+"</p>"+(item.record_conflict?"<p class=\"status warn\">Record conflict: "+esc(sharedWorkValue(item.record_conflict))+"</p>":"")+"</article>"}).join("");
 const coverage=attempts?.coverage||"unknown",incomplete=coverage!=="complete"||Boolean(attempts?.truncated);
 const overlaps=Array.isArray(attempts?.overlaps)?attempts.overlaps.map(row=>{const left=byId.get(row.left),right=byId.get(row.right),label=(id,item)=>item?((item.participant||"Participant unknown")+" on "+(item.target?.work_id||"unknown target")):"attempt "+(id||"unknown");return "<p>Observed overlap: "+esc(label(row.left,left))+" and "+esc(label(row.right,right))+(row.intentional_parallel?" · intentional parallel acknowledged":"")+"</p>"}).join(""):"";
 if(!shared&&!attempts)return "";
 return "<section class=\"shared-work-observation\"><h3>Shared work</h3>"+(shared?"<p>"+(shared.enabled?tag("Enabled","good"):tag("Disabled"))+" · "+esc(shared.coverage||"unknown")+" coverage · "+esc(shared.fetch_state||"unknown")+" fetch</p>"+(shared.enabled?"":"Automatic peer fetch is off; local work remains available.")+(sourceRows||"<p>No configured sources are available.</p>")+(shared.diagnostics||[]).slice(0,8).map(d=>"<p>"+esc(d)+"</p>").join(""):"")+"<div class=\"shared-work-attempts\"><h4>Participant attempts · showing "+num(shown.length)+" of "+num(items.length)+" observed</h4>"+(incomplete?"<p class=\"status warn\">Attempt coverage is "+esc(coverage)+"; additional attempts or overlaps may be unobserved.</p>":"")+(items.length>shown.length?"<p class=\"quiet\">Only the first 50 observed attempts are shown here.</p>":"")+(attemptRows||(incomplete?"<p>No attempts found in the available sources.</p>":"<p>No shared attempts observed.</p>"))+overlaps+(attempts?.overlaps_truncated?"<p class=\"quiet\">Additional overlap pairs are not shown.</p>":"")+(attempts?.publication?"<p>Latest publication: "+esc(sharedWorkValue(attempts.publication))+"</p>":"")+"</div></section>";
}function currentWorkBar(p){
  if(!["overview","boards","work-order","memory","navigation","repository-state"].includes(state.view))return "";
  const w=p.modules.current_work||{},sources=w.sources||[],selected=state.workView==="source"?state.workSource:state.workView;
  const options=[{id:"current",label:"Current Work · all observed branches"},{id:"main",label:"Main · observed ref"},{id:"checkout",label:"This checkout"},...sources.map(s=>({id:s.id,label:(s.kind==="working-tree"?"Working tree":"Branch")+" · "+s.label+(s.kind==="working-tree"?" · "+s.path:"")}))];
  const count=(p.modules.boards?.conflicts||[]).length,warning=w.freshness?.kind==="stale"||w.status==="partial"||w.status==="unavailable"||["ram","nav","documents"].some(k=>["partial","unavailable"].includes(p.modules[k]?.status));
  return "<section class=\"current-work-scope\"><label>Work view <select data-work-view aria-label=\"Work source\" "+(state.workChanging?"disabled":"")+">"+options.map(s=>"<option value=\""+esc(s.id)+"\" "+(s.id===selected?"selected":"")+">"+esc(s.label)+"</option>").join("")+"</select></label><span class=\"quiet\">"+(warning?"Some observations are stale or unavailable":"Observed local branches and fetched remote work")+"</span>"+(count?"<a class=\"status warn\" href=\"#repository-state\">"+num(count)+" conflicts to reconcile</a>":"")+"<details data-persist=\"current-work-coverage\"><summary>Sources and freshness</summary><p>Work finished on a branch can still await integration into Main. Other developers’ work appears after a configured fetch.</p><p>"+esc(p.data_scope?.runtime)+"</p><p>"+esc(w.observed_at?"Observed "+when(w.observed_at):"Observation time unavailable")+" · "+esc(w.freshness?.kind||"unknown")+"</p>"+sharedWorkObservationMarkup(w)+[...(w.diagnostics||[]),...(w.knowledge?.diagnostics||[])].filter((v,i,a)=>a.indexOf(v)===i).slice(0,12).map(d=>"<p>"+esc(d)+"</p>").join("")+"<p class=\"board-source\">"+esc(w.storage?.path||w.storage?.cache_path||"")+"</p></details></section>";
}
function invalidateSharedWorkProjection(p,revision){
 if(!p)return;for(const key of Object.keys(state.lazy||{}))if(key.startsWith(String(state.project)+"|knowledge|"))delete state.lazy[key];
 for(const name of ["boards","ram","nav","documents"])p.modules[name]={status:"partial",items:[],records:[],diagnostics:["Shared work settings changed; refreshing the current source view."]};
 const current={...(p.modules.current_work||{})};delete current.knowledge;delete current.attempts;current.sources=[];
 const enabled=state.settings?.shared_work?.enabled??current.shared_work?.enabled??false;
 current.shared_work={enabled,revision:revision||current.shared_work?.revision||"",coverage:"unknown",fetch_state:enabled?"cached":"disabled",canonical:null,peers:[],diagnostics:["Shared work settings changed; refreshing the current source view."]};p.modules.current_work=current;
 p.data_scope={...(p.data_scope||{}),shared:[]};state.documentBodies={};if(typeof memorySearchState!=="undefined")memorySearchState.delete(state.project);
}
function currentWorkFingerprint(p){return JSON.stringify({boards:p.modules?.boards,ram:p.modules?.ram,nav:p.modules?.nav,documents:p.modules?.documents,scope:p.data_scope,shared_work:p.modules?.current_work?.shared_work,attempts:p.modules?.current_work?.attempts,diagnostics:p.modules?.current_work?.diagnostics})}
function applyCurrentWorkPatch(p,patch){
 const oldRevision=sharedWorkRevision(p),incoming=patch.modules?.current_work||{},nextRevision=incoming.shared_work?.revision||"";
 if(nextRevision&&oldRevision&&nextRevision!==oldRevision)invalidateSharedWorkProjection(p,nextRevision);
 for(const name of patch.shared_modules||[]){const module=patch.modules?.[name]||{status:"unavailable",items:[],records:[]};if(name==="nav"){for(const key of ["runs","run_count","pass_count","history_complete"])module[key]=p.modules.nav?.[key]}p.modules[name]=module;}
 const knowledge=incoming.knowledge||p.modules.current_work?.knowledge;
 p.modules.current_work={...incoming,...(knowledge?{knowledge}:{})};
 p.data_scope={...patch.data_scope,shared:[...new Set(patch.data_scope?.shared||[])]};
}
function retainNewerWork(next,previous){
 if(!next||!previous)return;
 const configured=state.settingsProject===state.project?state.settings?.shared_work_revision:"",nextRevision=sharedWorkRevision(next),previousRevision=sharedWorkRevision(previous);
 if((nextRevision&&previousRevision&&nextRevision!==previousRevision)||(configured&&(nextRevision!==configured||previousRevision!==configured)))return;
 const knowledge=next.modules?.current_work?.knowledge;for(const key of ["boards","current_work"]){if(previous.modules?.[key])next.modules[key]=previous.modules[key]}next.data_scope={...previous.data_scope,shared:[...new Set([...(next.data_scope?.shared||[]),...(previous.data_scope?.shared||[])])]};next.work=previous.work;if(knowledge)next.modules.current_work={...next.modules.current_work,knowledge};
}async function refreshCurrentWork(){
  if(!state.snapshot||!project()||state.currentWorkLoading||state.workChanging||document.hidden)return; // no selected project (a fresh install): nothing to poll
  const key=workViewKey(),request=++state.workRequest;state.currentWorkLoading=true;
  try{const response=await fetch(`/api/v1/current-work?${workQuery({knowledge:"0"})}`,{cache:"no-store"});if(!response.ok)throw new Error(`Current Work returned ${response.status}`);const patch=await response.json();if(key!==workViewKey()||request<state.workApplied)return;const p=project();if(!p)return;const before=currentWorkFingerprint(p);applyCurrentWorkPatch(p,patch);state.workApplied=request;if(before!==currentWorkFingerprint(p)&&!["settings","metrics","timeline"].includes(state.view)&&!content.contains(document.activeElement))render();}
  catch(error){stamp.textContent="Current Work refresh failed; last view retained";const p=project();if(p&&key===workViewKey()){p.modules.current_work={...p.modules.current_work,status:"partial",freshness:{kind:"stale"},diagnostics:[error.message]};if(!content.contains(document.activeElement))render()}}
  finally{state.currentWorkLoading=false;}
}
function documentsPanel(p){
 const docs=p?.modules.documents||{},rows=docs.items||[];
 return `<details class="card" data-persist="shared-documents"><summary>Documents across sources · ${num(rows.length)} paths</summary><p>Different versions stay visible. Read large documents in pages from the exact source and content hash.</p>${rows.slice(0,200).map((row,index)=>`<article class="document-variants"><strong>${esc(row.path)}</strong>${row.conflict?tag("Different versions","warn"):""}${(row.variants||[]).map((variant,n)=>{const key=`${row.path}:${variant.sha256}:${variant.source_id}`,body=state.documentBodies[key];return `<div><span class="quiet">${esc((variant.sources||[]).map(s=>s.label).join(", "))}</span> <button type="button" data-document-read="${index}:${n}">Read version</button>${body?`<p class="quiet">${num(body.loaded)} of ${num(body.total)} bytes loaded${body.next===null?" · Complete":""}</p><pre class="document-body">${esc(body.content)}</pre>${body.next===null?"":`<button type="button" data-document-read="${index}:${n}" data-document-more>Read next page</button>`}`:""}</div>`}).join("")}</article>`).join("")}${rows.length>200?"<p>Showing the first 200 paths. The shared CLI returns the bounded complete index.</p>":""}${(docs.diagnostics||[]).map(d=>`<p>${esc(d)}</p>`).join("")}</details>`;
}
const documentReads=new Set();
async function readDocumentPage(button){
 const [index,n]=button.dataset.documentRead.split(":").map(Number),row=project()?.modules.documents?.items?.[index],variant=row?.variants?.[n];
 if(!variant)return;
 const view=workViewKey(),selection=state.workSelection,bodyKey=`${row.path}:${variant.sha256}:${variant.source_id}`,requestKey=`${view}:${selection}:${bodyKey}`;
 if(documentReads.has(requestKey))return;
 const previous=Object.hasOwn(button.dataset,"documentMore")?state.documentBodies[bodyKey]:null,offset=previous?.next??0;
 if(previous?.next===null)return;
 documentReads.add(requestKey);button.disabled=true;
 try{
  const q=new URLSearchParams({project:String(state.project),path:row.path,source:variant.source_id,sha256:variant.sha256,offset:String(offset)}),response=await fetch(`/api/v1/documents/get?${q}`,{cache:"no-store"}),value=await response.json();
  if(!response.ok)throw new Error(value.message||"Source changed; refresh documents.");
  if(view!==workViewKey()||selection!==state.workSelection)return;
  if(value.offset!==offset||typeof value.content!=="string"||value.sha256!==variant.sha256||value.source_id!==variant.source_id)throw new Error("Unexpected document page; refresh documents.");
  state.documentBodies[bodyKey]={content:(previous?.content||"")+value.content,loaded:value.next_offset??value.total_bytes,total:value.total_bytes,next:value.next_offset};render();
 }catch(error){toast(error.message)}finally{documentReads.delete(requestKey);button.disabled=false}
}
async function selectWorkView(value){
  if(state.workChanging)return;
  const previous={view:state.workView,source:state.workSource},selection=++state.workSelection;
  state.workView=["current","main","checkout"].includes(value)?value:"source";
  state.workSource=state.workView==="source"?value:null;
  state.documentBodies={};memorySearchState.delete(state.project);state.workChanging=true;
  const key=workViewKey();render();
  try{const loaded=await load(true);if(!loaded&&selection===state.workSelection&&key===workViewKey()){state.workView=previous.view;state.workSource=previous.source;toast("Unable to load selected source; previous view restored.")}}
  finally{if(selection===state.workSelection){state.workChanging=false;if(workHashId())workRoutePending=true;render();if(state.view==="memory")refreshMemorySnapshot()}}
}
content.addEventListener("change",async event=>{if(event.target.matches("[data-work-view]"))await selectWorkView(event.target.value)});
content.addEventListener("click",async event=>{const button=event.target.closest("[data-document-read]");if(button)await readDocumentPage(button)});
setInterval(refreshCurrentWork,2000);
const esc=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const num=value=>new Intl.NumberFormat().format(value??0);
const compactNum=value=>new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:1}).format(value??0);
const exactValue=value=>Number.isFinite(value)?num(value):"Unknown";
const when=value=>value?new Date(value).toLocaleString():"Unknown";
const project=()=>state.snapshot?.projects?.[state.project];

// Summary-first loading: the snapshot defers heavy sections; each panel fetches its own on demand.
const lazyNames=["delivery","write_retry","attribution","companion","repository_state","repo_metrics","repo_health"];
const lazyNeeds={overview:["delivery"],boards:["delivery"],delivery:["delivery","write_retry","attribution"],navigation:["knowledge"],timeline:["companion","knowledge"],metrics:["companion"],companion:["companion"],"repository-state":["repository_state"],"repo-health":["repo_metrics","repo_health"],"repo-hotspots":["repo_metrics"],system:lazyNames};
const lazySoftViews=new Set(["overview","boards"]);
state.lazy={};
function lazyKey(name,index=state.project){return `${index}|${name}|${name==="knowledge"?workViewKey():""}`}
function lazyReady(name){return state.lazy[lazyKey(name)]?.status==="loaded"}
function applyKnowledge(p,patch){
 const incoming=sharedWorkRevision(patch),observed=sharedWorkRevision(p),configured=state.settingsProject===state.project?state.settings?.shared_work_revision:"";
 if(incoming&&observed&&incoming!==observed)return;
 if(incoming&&configured&&incoming!==configured)return;
 for(const name of ["ram","nav","documents"]){const module=patch.modules?.[name]||{status:"unavailable",items:[],records:[]};if(name==="nav"){for(const key of ["runs","run_count","pass_count","history_complete"])module[key]=p.modules.nav?.[key]}p.modules[name]=module}
 const knowledge=patch.modules?.current_work?.knowledge;if(knowledge)p.modules.current_work={...p.modules.current_work,knowledge};
 p.data_scope={...p.data_scope,shared:[...new Set([...(p.data_scope?.shared||[]),"ram","nav","documents"])]};
}
function applyLazy(p,name,data){if(!p||p.deferred_project)return;if(name==="knowledge")applyKnowledge(p,data);else Object.assign(p.modules,data.modules||{})}
function reapplyLazy(p,index=state.project){for(const name of [...lazyNames,"knowledge"]){const entry=state.lazy[lazyKey(name,index)];if(entry?.status==="loaded")applyLazy(p,name,entry.data)}}
function lazyUrl(name,fresh){return name==="knowledge"?`/api/v1/current-work?${workQuery({knowledge:"1"})}`:`/api/v1/module?${new URLSearchParams({project:String(state.project),name,...(fresh?{refresh_state:"1"}:{})})}`}
function ensureLazy(names,opts={}){
 const index=state.project,workKey=workViewKey();
 for(const name of names){
  const key=lazyKey(name),entry=state.lazy[key];
  const force=opts.force&&(name!=="knowledge"||opts.fresh);if(entry?.status==="loading"||(entry&&!force&&!(opts.retry&&entry.status==="error")))continue;
  const record={status:"loading",data:entry?.data};state.lazy[key]=record;
  fetch(lazyUrl(name,opts.fresh),{cache:"no-store"}).then(async response=>{if(!response.ok)throw new Error(`${name} returned ${response.status}`);const data=await response.json();const same=record.data!==undefined&&JSON.stringify(record.data)===JSON.stringify(data);state.lazy[key]={status:"loaded",data};if(index===state.project&&workKey===workViewKey()){const p=project();applyLazy(p,name,data);if(!same&&(!["settings","metrics","timeline"].includes(state.view)||!typingInContent()))lazyRender(name)}}).catch(error=>{state.lazy[key]={status:"error",error:error.message,data:record.data};if(index===state.project)lazyRender(name)});
 }
}
function typingInContent(){const el=document.activeElement;return el!==content&&content.contains(el)}
function lazyRender(name){if((lazyNeeds[state.view]||[]).includes(name)&&!typingInContent())render()}
function ensureViewLazy(opts={}){ensureLazy(lazyNeeds[state.view]||[],opts)}
function lazyGate(p){
 const need=(lazyNeeds[state.view]||[]).filter(name=>!lazyReady(name));
 if(!need.length||lazySoftViews.has(state.view)){if(need.length)queueMicrotask(()=>ensureLazy(need));return views[state.view](p)}
 queueMicrotask(()=>ensureLazy(need));
 const failed=need.filter(name=>state.lazy[lazyKey(name)]?.status==="error");
 return failed.length?empty("Section unavailable",`${failed.map(name=>state.lazy[lazyKey(name)].error).join("; ")}. Refresh to try again.`):`<div class="card empty" role="status" aria-live="polite"><strong>Loading ${esc(need.map(name=>name.replaceAll("_"," ")).join(", "))}…</strong>This section is read when opened; the rest of the dashboard stays available.</div>`;
}
const status=(text,type="")=>`<span class="status ${type}">${esc(text)}</span>`;
const tag=(text,type="")=>`<span class="tag ${type}">${esc(text)}</span>`;
function heading(title,description,extra=""){return `<header class="page-head"><div><h1>${esc(title)}</h1><p>${esc(description)}</p></div>${extra}</header>`}
function stat(label,value,detail){return `<section class="card"><div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-detail">${esc(detail)}</div></section>`}
function comparisonBars(title,items,note){
  const valid=value=>typeof value==="number"&&Number.isFinite(value)&&value>=0;
  const ceiling=Math.max(1,...items.filter(x=>valid(x.value)).map(x=>x.value));
  return `<section class="card visual-comparison"><div class="section-head"><h2>${esc(title)}</h2></div><div class="comparison-rows">${items.map(x=>`<div class="comparison-row"><div class="comparison-label"><span>${x.model?modelLabel(x.model,x.label):esc(x.label)}</span><strong>${valid(x.value)?num(x.value):"Unknown"}</strong></div><svg class="comparison-track" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true"><rect width="100" height="8" rx="2" class="comparison-track-bg"/>${valid(x.value)?`<rect width="${100*x.value/ceiling}" height="8" rx="2" class="comparison-fill ${x.tone||""} ${x.model?`model-${modelKey(x.model)}`:""}" ${x.model?`fill="${modelColor(x.model)}" data-model="${modelKey(x.model)}"`:""}/>`:""}</svg></div>`).join("")}</div><p class="quiet comparison-note">${esc(note)}</p></section>`;
}
function overviewComparisons(p){
  const board=p.modules.boards,items=board?.items,known=Array.isArray(items)&&["available","partial"].includes(board.status),slices=known?items.filter(x=>x.type==="slice"&&!x.parent_id):[];
  const groups=[["active","Active",""],["not-active","Not active","muted"],["done","Done","good"],["cancelled","Cancelled","muted"]];
  const changes=p.repository?.changes||{};
  return `<div class="grid two overview-comparisons">${comparisonBars("Slice status",groups.map(([key,label,tone])=>({label,tone,value:known?slices.filter(x=>workGroup(x)===key).length:null})),"Slice counts across selected board owners; equal counts do not imply equal effort.")}${comparisonBars("Working-tree changes",[{label:"Modified",value:changes.modified},{label:"Staged",value:changes.staged},{label:"Untracked",value:changes.untracked,tone:"muted"},{label:"Conflicted",value:changes.conflicts,tone:"warn"}],"Files on one shared scale. A file can be both modified and staged; categories are not additive.")}</div>`;
}
function metricsComparisons(tokens,coverage,models){
  const counter=key=>tokens[key]??tokens[`observed_${key}`]??null;
  const input=counter("input_tokens"),cached=counter("cached_input_tokens");
  const remainder=Number.isFinite(input)&&Number.isFinite(cached)&&cached>=0&&input>=cached?input-cached:null;
  return `<div class="grid two metrics-comparisons">${comparisonBars("Input reuse",[{label:"Cached input",value:cached,tone:"good"},{label:"Other input",value:remainder}],"Recorded token counts on a shared scale. Cached input is already included in total input; this is not measured cost savings.")}${comparisonBars("Usage coverage",[{label:"Complete counters",value:coverage.usage_complete_run_count,tone:"good"},{label:"Unknown counters",value:coverage.usage_unknown_run_count,tone:"warn"}],"Recorded runs in the selected range. Unknown usage is not counted as zero tokens.")}</div>`+(models.length?comparisonBars("Completed turns by model / role",models.map(x=>({label:`${shortModel(x.model)} · ${x.role||"unspecified role"}`,value:x.completed_run_count,model:x.model})),"Turn counts share one scale within this chart. Work size varies; counts do not measure model quality or efficiency."):"");
}
function empty(title,text){return `<div class="card empty"><strong>${esc(title)}</strong>${esc(text)}</div>`}
function chart(items,label){const max=Math.max(1,...items.map(x=>x.value));return `<div class="chart" role="img" aria-label="${esc(label)}"><div class="chart-bars">${items.map((x,i)=>{const tip=x.description||`${x.label}: ${num(x.value)} recorded`;return `<div class="chart-column" tabindex="0" title="${esc(tip)}" aria-label="${esc(tip)}"><div class="bar-value">${num(x.value)}</div><div class="bar ${i===2?"warn":i>2?"muted":""}" data-value="${Math.max(2,Math.round(120*x.value/max))}"></div><div class="bar-label">${esc(x.label)}</div><span class="chart-tip" role="tooltip">${esc(tip)}</span></div>`}).join("")}</div></div>`}
function applyBars(){document.querySelectorAll(".bar[data-value]").forEach(el=>el.style.height=el.dataset.value+"px");document.querySelectorAll(".microbars span[data-flex]").forEach(el=>el.style.flex=el.dataset.flex)}
function versionDate(value){return value?`Version date ${value}`:"Version date not recorded"}
function releasePanel(p){
  const r=p.release||{},configured=r.status==="available",runtime=new Map((r.runtime_modules||[]).map(m=>[m.id,m]));
  const alignment=r.runtime_alignment?.status==="matching-versions"?"Declared module versions match source":"Declared module versions differ from source";
  return `<section class="card release-panel"><div class="section-head"><h2>Versions</h2>${status(configured?(r.channel==="release"&&!r.dirty?"Release":"Development"):"Project version not configured",configured?"":"warn")}</div><div class="release-summary"><div><span>Project</span><strong>${esc(configured?r.version:"Not configured")}</strong><small>${esc(configured?`${r.name} · ${versionDate(r.date)}`:"Add eutonos.version.json to track this project")}</small></div><div><span>EUTONOS runtime</span><strong>${esc(r.runtime_version||"Unknown")}</strong><small>${esc(`${versionDate(r.runtime_date)} · ${alignment}`)}</small></div><div><span>Source</span><strong>${esc((r.commit||p.source?.commit||"").slice(0,8)||"Unknown")}</strong><small>${r.dirty===true?"Uncommitted changes":r.dirty===false?"Working tree clean":"Working state unknown"}</small></div></div>${r.status==="error"?`<p>${esc(r.reason)}</p>`:""}${configured?`<details class="release-modules" data-persist="release-modules"><summary>Module versions · ${(r.modules||[]).length} modules</summary><div class="release-module-grid">${(r.modules||[]).map(m=>{const installed=runtime.get(m.id),different=!installed||installed.version!==m.version||installed.date!==m.date;return `<div><strong>${esc(m.name)}</strong><span>${esc(m.version)}</span><small>${esc(versionDate(m.date))}</small><small>${esc(different?(installed?`Installed ${installed.version} · ${versionDate(installed.date)}`:"Absent from installed runtime"):(m.fingerprint?`Source ${m.fingerprint.slice(0,12)}`:"Incomplete source selection"))}</small></div>`}).join("")}</div><p class="release-note">Dates identify declared version milestones. Fingerprints detect source edits between milestones. Matching declared versions does not prove publication, installed byte parity, or platform acceptance.</p></details>`:""}</section>`
}
function workDone(item){return workGroup(item)==="done"}
function workEntries(p,context,horizon){
  const all=p.modules.boards?.items||[],byId=new Map(all.map(item=>[item.id,item]));
  const campaignFor=item=>{const seen=new Set();while(item&&!seen.has(item.id)){if(item.type==="campaign")return item;seen.add(item.id);item=byId.get(item.parent_id)}return null};
  const matches=item=>workGroup(item)===horizon,current=p.work?.work_id?byId.get(p.work.work_id):null,selected=campaignFor(current);
  const campaigns=all.filter(item=>item.type==="campaign"&&matches(item));
  const saved=p.modules.boards?.work_order||{},rank=new Map([...(saved.current||[]),...(saved.next||[])].map((id,index)=>[id,index]));
  campaigns.sort((a,b)=>(rank.get(a.id)??rank.size)-(rank.get(b.id)??rank.size));
  if(selected){const at=campaigns.findIndex(item=>item.id===selected.id);if(at>0)campaigns.unshift(...campaigns.splice(at,1))}
  const entries=campaigns.map(item=>{const stages=all.filter(child=>["stage","slice"].includes(child.display_type||child.type)&&campaignFor(child)?.id===item.id);return {...item,stages,slices:stages}});
  if(context==="overview"){
    const standalone=all.filter(item=>(["quick","task","slice"].includes(item.type)||workQueued(item))&&!campaignFor(item)&&matches(item));
    if(p.work?.work_id&&!current&&matches(p.work))standalone.unshift({id:p.work.work_id,title:p.work.heading||p.work.work_id,summary:p.work.goal||p.work.summary,status:p.work.status||"unknown",owner_path:p.work.path,date:p.work.recorded_on});
    if(standalone.length){const group={id:"standalone",type:"standalone",title:"Standalone work",items:standalone};if(standalone.some(item=>item.id===p.work?.work_id))entries.unshift(group);else entries.push(group)}
  }
  return entries;
}
function workBrowser(p,context){
  const horizon=state.workHorizon,entries=workEntries(p,context,horizon),key="overviewCampaignIndex";
  const toggle=`<div class="tabs work-horizon" role="group" aria-label="Work status">${workGroupOrder.map(name=>`<button data-work-horizon="${name}" aria-pressed="${horizon===name}">${workGroupLabels[name]}</button>`).join("")}</div>`;
  if(!entries.length)return toggle+empty(`No ${workGroupLabels[horizon].toLowerCase()} work`,context==="overview"?"Selected work and campaign records appear here. Browse another status to see its records.":"No selected campaign records have this status.");
  state[key]=Math.max(0,Math.min(state[key]||0,entries.length-1));const item=entries[state[key]],stages=item.stages||[],done=stages.filter(stage=>stage.status==="completed").length;
  const date=item=>esc(item.display?.display_date?item.display.date_kind+" "+item.display.display_date:item.date?"Recorded "+item.date:"Date not recorded");
  const codes=workCodes(p.modules.boards?.items||[]),body=item.type==="standalone"?`<div class="standalone-work">${item.items.map(task=>`<article class="list-item"><div><h3>${workLabel(task,codes)}${workKindTag(task)}</h3><p>${esc(task.summary)}</p>${workAttributionSummary(task)}${progressBar(task,task.title)}${completionChecks(task)}${workStatusDetails(task)}${workAttributionDetails(task)}<small class="quiet">${date(task)}</small><details data-persist="standalone-${esc(task.id)}"><summary>Open task view</summary><p class="board-source">${esc(task.id)}</p><p class="board-source">${esc(task.owner_path)}</p></details></div>${workStatusTag(task)}</article>`).join("")}</div>`:
    `<div class="work-browser-grid"><div><p class="quiet work-date">${date(item)}</p><p><strong>${stageCountLabel(stages)}</strong></p><p class="memory-summary">${esc(item.summary)}</p>${workAttributionSummary(item)}${progressBar(item,item.title)}${completionChecks(item)}<div class="meta">${workStatusTag(item)}</div>${workStatusDetails(item)}${workAttributionDetails(item)}</div><div class="work-slices"><h3>Stages</h3>${stages.map(stageRow).join("")||'<p class="quiet">No structured stages recorded.</p>'}</div></div><details data-persist="work-browser-${esc(context)}-${esc(item.id)}"><summary>Open campaign source</summary><p class="board-source">${esc(item.id)}</p><p class="board-source">${esc(item.owner_path||"Source path unavailable")}</p>${stages.map(stage=>`<section><strong>${workLabel(stage)}</strong> ${workStatusTag(stage)}<p class="quiet">${esc(stage.summary)}</p>${workStatusDetails(stage)}${workAttributionDetails(stage)}</section>`).join("")}</details>`;
  return toggle+`<section class="card work-browser"><div class="section-head"><div><span class="eyebrow">${item.type==="standalone"?"Standalone work":"Campaign"}</span><h2>${item.type==="campaign"?workLabel(item):esc(item.title)}</h2></div><div class="browser-arrows"><button type="button" data-work-browser="${context}" data-work-step="-1" ${state[key]===0?"disabled":""} aria-label="Previous work card">←</button><span>${state[key]+1} / ${entries.length}</span><button type="button" data-work-browser="${context}" data-work-step="1" ${state[key]===entries.length-1?"disabled":""} aria-label="Next work card">→</button></div></div>${body}</section>`;
}
content.addEventListener("click",event=>{const filter=event.target.closest("[data-work-horizon]");if(filter){state.workHorizon=filter.dataset.workHorizon;state.overviewCampaignIndex=0;state.campaignPage=0;render();return}const button=event.target.closest("[data-work-browser]");if(!button)return;state.overviewCampaignIndex+=Number(button.dataset.workStep);render()});
content.addEventListener("click",event=>{const button=event.target.closest("[data-repository-state-view]");if(!button)return;repositoryStateSelection=button.dataset.repositoryStateView==="main"?"main":"latest";render();content.querySelector(`[data-repository-state-view="${repositoryStateSelection}"]`)?.focus()});
content.addEventListener("input",event=>{const input=event.target.closest("[data-repository-state-search]");if(!input)return;const start=input.selectionStart??input.value.length,end=input.selectionEnd??start;repositoryStateSearch=String(input.value||"").slice(0,120);render();const next=content.querySelector("[data-repository-state-search]");next?.focus();if(next&&typeof next.setSelectionRange==="function")next.setSelectionRange(start,end)});
function repositoryOverview(p){
  const repo=p.repository||{},known=value=>Number.isFinite(value)?num(value):"Unknown",parts=repo.changes||{};
  const detail=[[parts.modified,"modified"],[parts.staged,"staged"],[parts.untracked,"untracked"],[parts.conflicts,"conflicted"]].filter(([count])=>count).map(([count,label])=>`${num(count)} ${label}`).join(" · ")||((repo.uncommitted===0)?"Working tree clean":"Change details unavailable");
  return `<div class="repository-overview"><section class="card repository-card"><div class="section-head"><h2>Repository structure</h2></div><div class="repository-metrics"><div><strong>${known(repo.worktrees)}</strong><span>worktrees</span></div><div><strong>${known(repo.branches)}</strong><span>local branches</span></div></div><p class="repository-detail">${esc(repo.current_branch||"Current branch unavailable")}</p></section><section class="card repository-card"><div class="section-head"><h2>Recent activity</h2><a href="#changes">Open Changelog &rarr;</a></div><div class="repository-metrics"><div><strong>${known(repo.recent_commits)}</strong><span>commits · ${known(repo.recent_days)} days</span></div><div class="repository-change ${repo.uncommitted?"has-changes":""}"><strong>${known(repo.uncommitted)}</strong><span>uncommitted files</span></div></div><p class="repository-detail">${esc(detail)}</p></section></div>`;
}
function repositoryStateRef(ref){return String(ref||"Unknown source")}
function repositoryStateSourceKind(kind){return ({"cached-remote-ref":"Cached remote ref","local-ref":"Local branch","working-tree":"Working tree"})[kind]||"Source kind unknown"}
function repositoryStateIntegration(value){return ({"source-commit-on-main":"Source commit on Main","retained-unintegrated":"Awaiting integration","unknown-retained-import":"Imported source pin · integration unknown","unknown":"Source integration unknown"})[value]||"Source integration unrecorded"}
function repositoryStateTone(value){return value==="completed"?"good":(["blocked","removed","cancelled"].includes(value)?"warn":"")}
function repositoryStateCount(value){return typeof value==="number"&&Number.isFinite(value)?num(value):"Unknown"}
function repositoryStatePin(pin){if(!pin||typeof pin!=="object")return `<li>Source evidence unavailable.</li>`;const source=repositoryStateSourceKind(pin.source_kind),location=pin.commit_on_main===true?"Source commit on Main":pin.commit_on_main===false?"Source commit not verified on Main":"Source commit status unknown";return `<li><strong>${esc(repositoryStateRef(pin.ref))}</strong> · ${esc(source)} · ${esc(location)}<br><code>${esc(pin.commit||"Commit unknown")}</code></li>`}
function repositoryStateItem(item,key){const id=item.id||"Unknown record",title=item.title||"Untitled work",owner=item.owner_path||"Owner path unavailable",statusText=item.status||"Unknown status",location=item.record_on_main===true?"Record on Main":item.record_on_main===false?"Record away from Main":"Record location unknown",pins=Array.isArray(item.provenance)?item.provenance:[],publication=item.publication==="cached-local-ref"?"Publication uses a cached local ref":item.publication==="unknown"?"Publication unknown":"Publication not recorded",imported=item.source_import?" · Imported source record":"";return `<article class="repository-state-item"><div class="repository-state-item-head"><div><h3>${esc(title)}</h3><p>${esc(id)} · ${esc(item.type||"Type unknown")}</p></div>${tag(statusText,repositoryStateTone(item.status))}</div><div class="repository-state-meta">${tag(location)}${tag(repositoryStateIntegration(item.source_integration))}${tag(publication)}${imported?tag("Imported source record"):""}</div><p class="quiet repository-state-owner">Owner: ${esc(owner)}</p><p class="repository-state-next"><strong>Next step</strong> ${esc(repositoryStateAdvice(item))}</p><details class="repository-state-evidence" data-persist="repository-evidence-${esc(key)}"><summary>Source evidence · ${pins.length} pin${pins.length===1?"":"s"}</summary><p><strong>Record</strong> ${esc(location)}${imported}</p><p><strong>Owner file hash</strong> <code>${esc(item.owner_sha256||"Not recorded")}</code></p><p><strong>Native record hash</strong> <code>${esc(item.record_sha256||"Not recorded")}</code></p><p><strong>Publication</strong> ${esc(publication)}</p><ul>${pins.map(repositoryStatePin).join("")||"<li>No source pin recorded.</li>"}</ul></details></article>`}
function repositoryStateAdvice(item){if(item.status==="blocked")return "Review the recorded blocker with the owning work item before integration.";if(item.status==="active")return item.record_on_main===false?"Continue in the recorded checkout, then use the work item’s usual review and integration step.":"Continue the recorded work and use its usual review and integration step.";if(item.status==="completed"&&item.source_integration==="source-commit-on-main")return "The source commit is on Main; product acceptance is still a separate check.";if(item.status==="completed"&&item.record_on_main===true)return "The native record is on Main; its source pin is unverified. Verify that pin if needed.";if(item.status==="completed"&&item.source_integration==="retained-unintegrated")return "Review the source pin, then use the work item’s usual review and integration step.";if(item.status==="completed")return "Source integration is unknown; inspect the source pin and coverage before drawing a conclusion.";return "Check the source evidence and coverage before treating this record as current."}
function repositoryStateMatches(item){const term=repositoryStateSearch.trim().toLocaleLowerCase();return !term||[item?.id,item?.title,item?.owner_path].some(value=>String(value||"").toLocaleLowerCase().includes(term))}
function repositoryStateFilter(items){if(!Array.isArray(items))return [];return repositoryStateSearch.trim()?items.filter(repositoryStateMatches):items}
function repositoryStateRecords(items,key,emptyText="No native work records are available."){const rows=repositoryStateFilter(items);if(!rows.length)return `<p class="quiet">${esc(repositoryStateSearch.trim()?`No records match “${repositoryStateSearch.trim()}”.`:emptyText)}</p>`;const visible=rows.slice(0,12),more=rows.slice(12),body=visible.map((item,index)=>repositoryStateItem(item,`${key}-${index}-${item.id||"record"}`)).join("");return `<div class="repository-state-items">${body}${more.length?`<details class="repository-state-more" data-persist="repository-more-${esc(key)}"><summary>More records · ${more.length}</summary><div class="repository-state-items">${more.map((item,index)=>repositoryStateItem(item,`${key}-more-${index}-${item.id||"record"}`)).join("")}</div></details>`:""}</div>`}
function repositoryStateObservation(module){const observation=module.observation_freshness||{},age=observation.age_seconds;let ageText="age unknown";if(typeof age==="number"&&Number.isFinite(age)&&age>=0)ageText=age<60?`${Math.round(age)} sec old`:age<3600?`${(age/60).toFixed(1)} min old`:age<86400?`${(age/3600).toFixed(1)} hr old`:`${(age/86400).toFixed(1)} days old`;const kind=observation.kind==="cached"?"Cached observation":observation.kind==="live"?"Fresh observation":"Observation freshness unknown";return `${kind} · ${ageText}`}
function repositoryStateCoverage(module){const c=module.coverage||{},partial=module.status==="partial"||Number(c.sources_partial)>0||Number(c.owners_partial)>0,flags=[c.refs_truncated?"More refs than shown":null,c.worktrees_truncated?"More worktrees than shown":null,c.changed_paths_truncated?"Changed paths were capped":null].filter(Boolean);return `<section class="card repository-state-coverage"><div class="section-head"><div><h2>Coverage</h2><p class="quiet">${partial?"Some local sources or native owners could not be fully observed.":"Observed local refs and worktrees within the configured bounds."}</p></div>${tag(partial?"Partial":"Observed",partial?"warn":"good")}</div><div class="repository-state-stats"><div><strong>${repositoryStateCount(c.refs_observed)}</strong><span>Refs observed</span></div><div><strong>${repositoryStateCount(c.worktrees_observed)}</strong><span>Worktrees observed</span></div><div><strong>${repositoryStateCount(c.sources_partial)}</strong><span>Sources partial</span></div><div><strong>${repositoryStateCount(c.owners_partial)}</strong><span>Owners partial</span></div></div>${flags.length?`<p class="callout">${esc(flags.join(" · "))}</p>`:""}<details data-persist="repository-state-coverage"><summary>Observation details</summary><p>Commands: ${repositoryStateCount(c.commands)} · Bytes read: ${repositoryStateCount(c.bytes)}</p><p>Ref list: ${c.refs_truncated===true?"truncated":c.refs_truncated===false?"within bound":"unknown"} · Worktree list: ${c.worktrees_truncated===true?"truncated":c.worktrees_truncated===false?"within bound":"unknown"} · Changed paths: ${c.changed_paths_truncated===true?"capped":c.changed_paths_truncated===false?"within bound":"unknown"}</p>${Array.isArray(module.diagnostics)&&module.diagnostics.length?`<ul>${module.diagnostics.map(line=>`<li>${esc(line)}</li>`).join("")}</ul>`:"<p>No observation diagnostics recorded.</p>"}</details></section>`}
function repositoryStateSources(module){const sources=module.sources||{},refs=Array.isArray(sources.refs)?sources.refs:[],worktrees=Array.isArray(sources.worktrees)?sources.worktrees:[];const refRows=refs.map(row=>`<li><strong>${esc(repositoryStateRef(row.ref))}</strong> · ${esc(repositoryStateSourceKind(row.source_kind))} · ${esc(row.status||"Status unknown")}<br><code>${esc(row.commit||"Commit unknown")}</code>${row.reason?`<p>${esc(row.reason)}</p>`:""}</li>`).join("")||"<li>No refs observed.</li>";const worktreeRows=worktrees.map((row,index)=>`<li><strong>${esc(row.branch?repositoryStateRef(row.branch):row.detached?"Detached checkout":"Branch unknown")}</strong> · ${esc(row.status||"Status unknown")} · ${esc(repositoryStateCount(row.dirty_paths))} changed files<br><span>${esc(row.path||"Checkout path unavailable")}</span><br><code>${esc(row.commit||"Commit unknown")}</code>${row.reason?`<p>${esc(row.reason)}</p>`:""}${Array.isArray(row.changed_paths)&&row.changed_paths.length?`<details data-persist="repository-worktree-${index}"><summary>Changed paths · ${row.changed_paths.length}${row.changed_paths.length===100?" or more":""}</summary><ul>${row.changed_paths.map(path=>`<li>${esc(path)}</li>`).join("")}</ul></details>`:""}</li>`).join("")||"<li>No worktrees observed.</li>";return `<section class="card repository-state-sources"><div class="section-head"><div><h2>Local sources</h2><p class="quiet">Branch pins and checkout status used for Latest work.</p></div></div><details data-persist="repository-state-sources"><summary>Show source refs (${refs.length}) and worktrees (${worktrees.length})</summary><div class="repository-state-source-columns"><div><h3>Refs</h3><ul>${refRows}</ul></div><div><h3>Worktrees</h3><ul>${worktreeRows}</ul></div></div></details></section>`}
function repositoryStateMain(module){const main=module.main||{},items=Array.isArray(main.items)?main.items:[],active=items.filter(item=>["active","blocked"].includes(item.status)).length,completed=items.filter(item=>item.status==="completed").length,freshness=main.freshness==="cached-local-ref"?"Cached local ref":main.freshness||"Unknown";return `<section class="repository-state-baseline"><section class="card"><div class="section-head"><div><h2>Main baseline</h2><p class="quiet">Native work recorded at the selected Main ref.</p></div>${tag(main.status||"Status unknown",main.status==="partial"?"warn":"")}</div><dl class="repository-state-baseline-grid"><div><dt>Branch or ref</dt><dd>${esc(repositoryStateRef(main.ref))}</dd></div><div><dt>Source</dt><dd>${esc(repositoryStateSourceKind(main.ref_source))}</dd></div><div><dt>Commit</dt><dd><code>${esc(main.commit||"Unknown")}</code></dd></div><div><dt>Main ref freshness</dt><dd>${esc(freshness)}${main.freshness==="cached-local-ref"?" · remote not refreshed":""}</dd></div><div><dt>Observed</dt><dd>${esc(module.observed_at||"Unknown")} · ${esc(repositoryStateObservation(module))}</dd></div></dl><div class="repository-state-stats"><div><strong>${repositoryStateCount(items.length)}</strong><span>Native records</span></div><div><strong>${repositoryStateCount(active)}</strong><span>Active or blocked</span></div><div><strong>${repositoryStateCount(completed)}</strong><span>Completed records</span></div></div></section><section class="card"><div class="section-head"><div><h2>Main native work</h2><p class="quiet">The records present at this baseline.</p></div></div>${repositoryStateRecords(items,"main","No native work records were found on this Main ref.")}</section></section>`}
function repositoryStateCampaignRow(item,index){return `<div class="repository-state-campaign"><div><strong>${esc(item.title||"Untitled campaign")}</strong><span>${esc(item.id||"Unknown ID")} · ${esc(item.owner_path||"Owner path unavailable")}</span></div>${tag(item.status||"Unknown status",repositoryStateTone(item.status))}</div>`}
function repositoryStateMainSummary(module){const main=module.main||{},all=Array.isArray(main.items)?main.items:[],rank=item=>["active","blocked"].includes(item.status)?0:item.status==="completed"?1:2,compare=(a,b)=>rank(a)-rank(b)||String(a.title||a.id||"").localeCompare(String(b.title||b.id||""),"en")||String(a.id||"").localeCompare(String(b.id||""),"en"),campaigns=all.filter(item=>item.type==="campaign").sort(compare),rows=repositoryStateFilter(campaigns),current=all.filter(item=>["active","blocked"].includes(item.status)).length,visible=rows.slice(0,10),more=rows.slice(10),freshness=main.freshness==="cached-local-ref"?"Cached local ref":main.freshness||"Unknown";return `<section class="card repository-state-main-summary"><div class="section-head"><div><h2>Main status</h2><p class="quiet">${repositoryStateCount(campaigns.length)} campaigns · ${repositoryStateCount(current)} active or blocked native records</p></div>${tag(main.status||"Status unknown",main.status==="partial"?"warn":"")}</div><p class="repository-state-main-ref"><strong>${esc(repositoryStateRef(main.ref))}</strong> · ${esc(freshness)} · <code>${esc(main.commit||"Commit unknown")}</code></p><p class="quiet">Observed ${esc(module.observed_at||"Unknown")} · ${esc(repositoryStateObservation(module))}</p><h3>Campaign status on Main</h3><div class="repository-state-campaigns">${visible.map(repositoryStateCampaignRow).join("")||`<p class="quiet">${esc(repositoryStateSearch.trim()?`No campaigns match “${repositoryStateSearch.trim()}”.`:"No Main campaign records were observed.")}</p>`}${more.length?`<details data-persist="repository-main-campaigns"><summary>More Main campaigns · ${more.length}</summary><div class="repository-state-campaigns">${more.map(repositoryStateCampaignRow).join("")}</div></details>`:""}</div></section>`}
function repositoryStateConflict(row,index){const all=Array.isArray(row.candidates)?row.candidates:[],idMatch=repositoryStateMatches({id:row.id}),candidates=repositoryStateSearch.trim()&&!idMatch?all.filter(repositoryStateMatches):all;if(repositoryStateSearch.trim()&&!idMatch&&!candidates.length)return "";return `<section class="repository-state-conflict"><div class="section-head"><div><h3>${esc(row.id||"Unknown record")}</h3><p class="quiet">${esc(row.reason||"Conflicting native record versions")}</p></div>${tag(`${candidates.length} candidates`,"warn")}</div><div class="repository-state-items">${candidates.map((item,candidateIndex)=>repositoryStateItem(item,`conflict-${index}-${candidateIndex}-${item.id||"record"}`)).join("")||"<p class=\"quiet\">No candidate details are available.</p>"}</div></section>`}
function repositoryStateUnclassified(rows){if(!Array.isArray(rows)||!rows.length)return `<p class="quiet">No unclassified changed paths were recorded.</p>`;return `<details data-persist="repository-state-unclassified"><summary>Unclassified changed paths · ${rows.length}</summary><ul>${rows.map(row=>`<li><strong>${esc(row.path||"Path unknown")}</strong>${Array.isArray(row.provenance)?`<ul>${row.provenance.map(repositoryStatePin).join("")}</ul>`:""}</li>`).join("")}</ul></details>`}
function repositoryStateLatest(module){const latest=module.latest||{},items=Array.isArray(latest.items)?latest.items:[],elsewhere=items.filter(item=>item.record_on_main===false&&["active","blocked"].includes(item.status)),completed=Array.isArray(latest.completed_unintegrated)?latest.completed_unintegrated:[],awaiting=completed.filter(item=>item.source_integration==="retained-unintegrated"),reconcile=completed.filter(item=>item.source_integration!=="retained-unintegrated"),conflicts=Array.isArray(latest.conflicts)?latest.conflicts:[],conflictRows=conflicts.map(repositoryStateConflict).filter(Boolean),summary=repositoryStateSearch.trim()?`<p class="quiet" role="status">Filtering records and conflicts for “${esc(repositoryStateSearch.trim())}”. Summary counts include all observed records.</p>`:"";return repositoryStateMainSummary(module)+`<section class="repository-state-latest">${summary}<div class="repository-state-stats repository-state-latest-stats"><div><strong>${repositoryStateCount(items.length)}</strong><span>Latest records</span></div><div><strong>${repositoryStateCount(elsewhere.length)}</strong><span>Active elsewhere</span></div><div><strong>${repositoryStateCount(awaiting.length)}</strong><span>Awaiting integration</span></div><div><strong>${repositoryStateCount(reconcile.length)}</strong><span>Completed to reconcile</span></div><div><strong>${repositoryStateCount(conflicts.length)}</strong><span>Conflicting records</span></div></div><section class="card"><div class="section-head"><div><h2>Active work elsewhere</h2><p class="quiet">Active or blocked records whose native record differs from Main.</p></div></div>${repositoryStateRecords(elsewhere,"latest-active","No active records away from Main were observed.")}</section><section class="card"><div class="section-head"><div><h2>Awaiting integration</h2><p class="quiet">Completed records explicitly marked as retained or not integrated.</p></div></div>${repositoryStateRecords(awaiting,"latest-awaiting","No completed records are explicitly waiting on source integration.")}</section><section class="card"><div class="section-head"><div><h2>Completed to reconcile</h2><p class="quiet">Completed records with unknown source integration, including imported records whose source pin is unverified.</p></div></div>${repositoryStateRecords(reconcile,"latest-reconcile","No completed records need source reconciliation.")}</section>${conflicts.length?`<section class="card repository-state-conflicts"><div class="section-head"><div><h2>Conflicting records</h2><p class="quiet">Review the source evidence before choosing which record to integrate.</p></div>${tag(String(conflicts.length),"warn")}</div>${conflictRows.join("")||`<p class="quiet">${esc(repositoryStateSearch.trim()?`No conflicts match “${repositoryStateSearch.trim()}”.`:"No conflict details are available.")}</p>`}</section>`:empty("No conflicting records","The observed sources contain no competing native record versions.")}<section class="card"><div class="section-head"><div><h2>Consolidated native work</h2><p class="quiet">Resolved latest records across the refs and worktrees that were observed.</p></div></div>${repositoryStateRecords(items,"latest-all","No consolidated native work records were available.")}</section><section class="card"><div class="section-head"><div><h2>Other changed files</h2><p class="quiet">Files changed in observed sources that are outside native work owners.</p></div></div>${repositoryStateUnclassified(latest.unclassified_changes)}</section></section>`}
function repositoryStateHistoricalCandidate(row,index){const item=row?.candidate||{},pins=Array.isArray(item.provenance)?item.provenance:[],classification=row.classification==="inferred-historical-checkpoint"?"Inferred earlier checkpoint":"Historical classification unknown";return `<article class="repository-state-item"><div class="repository-state-item-head"><div><h3>${esc(item.title||"Untitled historical record")}</h3><p>${esc(row.id||item.id||"Unknown record")} · ${esc(item.type||"Type unknown")} · ${esc(item.status||"Unknown status")}</p></div>${tag(classification,"warn")}</div><p class="quiet repository-state-owner">Owner: ${esc(item.owner_path||"Owner path unavailable")}</p><p>${esc(row.reason||"No explanation was recorded for this historical classification.")}</p><details class="repository-state-evidence" data-persist="repository-historical-${index}"><summary>Source evidence · ${pins.length} pin${pins.length===1?"":"s"}</summary><p><strong>Record on Main</strong> ${item.record_on_main===true?"Yes":item.record_on_main===false?"No":"Unknown"}</p><p><strong>Owner file hash</strong> <code>${esc(item.owner_sha256||"Not recorded")}</code></p><p><strong>Native record hash</strong> <code>${esc(item.record_sha256||"Not recorded")}</code></p><ul>${pins.map(repositoryStatePin).join("")||"<li>No source pin recorded.</li>"}</ul></details></article>`}
function repositoryStateHistorical(module){const all=Array.isArray(module.latest?.historical_candidates)?module.latest.historical_candidates:[],term=repositoryStateSearch.trim(),rows=term?all.filter(row=>repositoryStateMatches({id:row.id})||repositoryStateMatches(row.candidate||{})):all,visible=rows.slice(0,12),more=rows.slice(12);if(!all.length)return "";return `<details class="card repository-state-historical" data-persist="repository-state-historical"><summary>Historical candidates · ${rows.length}${term?` matching of ${all.length}`:""}</summary><p class="quiet">These inferred earlier records appear superseded by Main work history. They are shown separately from current work and conflicts; review source evidence before discarding them.</p>${visible.map(repositoryStateHistoricalCandidate).join("")}${more.length?`<details class="repository-state-more" data-persist="repository-historical-more"><summary>More historical candidates · ${more.length}</summary><div class="repository-state-items">${more.map(repositoryStateHistoricalCandidate).join("")}</div></details>`:""}${!rows.length?`<p class="quiet">${esc(term?`No historical candidates match “${term}”.`:"No historical candidates were recorded.")}</p>`:""}</details>`}
function repositoryState(p){const module=p.modules?.repository_state,usable=module&&["available","partial"].includes(module.status),selected=repositoryStateSelection==="latest"?"latest":"main",toggle=`<div class="tabs repository-state-toggle" role="group" aria-label="Repository state view"><button type="button" data-repository-state-view="main" aria-pressed="${selected==="main"}">Main</button><button type="button" data-repository-state-view="latest" aria-pressed="${selected==="latest"}">Latest work</button></div>`,search=`<div class="repository-state-controls"><label class="repository-state-search"><span>Find work</span><input type="search" maxlength="120" data-repository-state-search aria-label="Find work by ID, title or owner path" placeholder="ID, title or owner" value="${esc(repositoryStateSearch)}"></label><span class="quiet">Search IDs, titles, owners and conflict candidates. Counts include all observed records.</span></div>`;if(!usable){const reason=module?.reason||module?.message||(Array.isArray(module?.diagnostics)&&module.diagnostics.join(" · "))||"No repository-state observation is available for this checkout.";return heading("Repository state","Compare the Main baseline with native work across locally observed refs and worktrees.")+toggle+search+empty("Repository state unavailable",reason)}const view=selected==="latest"?repositoryStateLatest(module):repositoryStateMain(module),history=selected==="latest"?repositoryStateHistorical(module):"",qualification=module.qualification||"Local repository evidence does not establish installed runtime state or product acceptance.";return heading("Repository state","Compare Main with consolidated native work across locally observed refs and worktrees.")+toggle+search+view+history+repositoryStateCoverage(module)+repositoryStateSources(module)+`<p class="quiet repository-state-qualification">${esc(qualification)}</p>`}
function deliveryReadinessItems(p){
  const d=p.modules.delivery||{},commit=d.authority?.commit||{},publish=d.authority?.publish||{},div=d.repository?.divergence||{},paths=d.totals?.paths;
  const result=(label,state,detail,tone="")=>({label,state,detail,tone});
  const save=commit.pending===false?result("Save","Current","No local change is pending.","good"):commit.deliverable===true?result("Save","Ready","Current authority permits the recorded save step.","good"):commit.deliverable===false?result("Save","Needs attention",(commit.reasons||[])[0]||"A recorded blocker prevents saving.","warn"):result("Save","Unknown","Save authority has not been recorded.");
  const integrate=div.known===true?result("Integrate",div.behind>0?"Needs update":paths?"Review changes":"Current",`${d.repository?.branch||"Current branch"} · ${div.ahead||0} ahead · ${div.behind||0} behind`,div.behind>0||paths?"warn":"good"):result("Integrate","Unproven",`${d.repository?.branch||"Current branch"} has no observed upstream comparison.`);
  const publication=publish.pending===false?result("Publish","Current","No publication is pending.","good"):publish.deliverable===true?result("Publish","Ready","Current authority permits publication.","good"):publish.deliverable===false?result("Publish","Blocked",(publish.reasons||[])[0]||"Publication is blocked.","warn"):result("Publish","Unknown",(publish.reasons||[])[0]||"Publication authority or repository state is unknown.");
  const accept=result("Accept","Not recorded","Repository evidence does not establish product or platform acceptance.");
  return [save,integrate,publication,accept];
}
function deliveryReadiness(p,compact=false){const items=deliveryReadinessItems(p);return `<section class="card delivery-readiness ${compact?"is-compact":""}"><div class="section-head"><div><h2>Delivery readiness</h2><p class="quiet">Save, integrate, publish and accept remain separate decisions.</p></div>${compact?'<a href="#delivery">Open Delivery →</a>':''}</div><div class="readiness-grid">${items.map(item=>`<div class="readiness-step ${esc(item.tone)}"><span>${esc(item.label)}</span><strong>${esc(item.state)}</strong><p>${esc(item.detail)}</p></div>`).join("")}</div></section>`}
function controlCenter(p){const items=(p.modules.boards?.items||[]).filter(item=>!item.parent_id&&(["campaign","slice","quick","task"].includes(item.type)||workQueued(item))),counts=Object.fromEntries(workGroupOrder.map(group=>[group,items.filter(item=>workGroup(item)===group).length])),delivery=deliveryReadinessItems(p),attention=delivery.filter(item=>item.tone==="warn").length,gallery=p.modules.visual_atlas?.items||[],nav=p.modules.nav||{},ram=p.modules.ram||{},memoryCount=["available","partial"].includes(ram.status)&&Number.isSafeInteger(ram.totals?.records)&&ram.totals.records>=0?ram.status==="partial"?`${num(ram.totals.records)} retained memories observed · partial`:`${num(ram.totals.records)} retained memories`:ram.status==="deferred"?"Memory count not loaded":"Memory count unavailable";return `<section class="control-center" aria-label="Control center"><div class="section-head"><div><h2>Control center</h2><p class="quiet">Five direct routes into the repository’s current operational picture.</p></div></div><div class="control-center-grid"><a class="card control-tile" href="#boards"><span>Work</span><strong>${num(counts.active)} active</strong><small>${num(counts["not-active"])} not active · ${num(counts.done)} done · ${num(counts.cancelled)} cancelled</small></a><a class="card control-tile" href="#delivery"><span>Delivery</span><strong>${attention?num(attention)+" need attention":"Review ready"}</strong><small>Authority and repository evidence</small></a><a class="card control-tile" href="#navigation"><span>Knowledge</span><strong>${num(nav.records)} NAV routes</strong><small>${memoryCount}</small></a><a class="card control-tile" href="#gallery"><span>Surfaces</span><strong>${num(gallery.length)} captures</strong><small>Dated dashboard views</small></a><a class="card control-tile" href="#repository-state"><span>Repository state</span><strong>Main · Latest work</strong><small>Compare Main with Current Work across observed branches.</small></a></div></section>`}
function overview(p){const nav=p.modules.nav||{},host=p.host||{},release=p.release||{},atlasRead=(release.runtime_modules||[]).some(m=>m.id==="atlas"),held=(host.attempts||[]).reduce((n,a)=>n+(a.reservations||[]).length,0);return heading("Overview","Pick up the active work and see what needs attention.")+controlCenter(p)+deliveryReadiness(p,true)+workBrowser(p,"overview")+(typeof workRowList==="function"?workRowList(p,true):"")+repositoryOverview(p)+tokenHistory(p.modules.companion?.lifetime_metrics?.trends||[],"Recorded history · all available dates")+overviewComparisons(p)+`<section class="card"><div class="section-head"><h2>Attention</h2></div><div class="list">${(host.grants?.warnings||[]).map(message=>`<div class="list-item"><div><strong>Review write grants</strong><p>${esc(message)}</p></div>${tag("Advisory","warn")}</div>`).join("")}${p.modules.atlas?.status!=="available"?`<div class="list-item"><div><strong>${atlasRead?"ATLAS read CLI available":"ATLAS is optional"}</strong><p>${atlasRead?"No ATLAS engine, index, or run adapter is configured in this repository.":"The optional ATLAS read surface is absent from this runtime."}</p></div>${tag(atlasRead?"Read only":"Unavailable",atlasRead?"good":"")}</div>`:""}${p.modules.measurement?.token_evidence?.unknown?`<div class="list-item"><div><strong>Provider tokens unavailable</strong><p>No exposed provider usage was found. This is unknown, not zero.</p></div>${tag("Unknown","warn")}</div>`:""}${held?`<div class="list-item"><div><strong>Protected paths exist</strong><p>Inspect the owning attempt before recovery.</p></div>${tag(`${held} held`,"warn")}</div>`:`<div class="list-item"><div><strong>No held paths observed</strong><p>Based on the visible local host journal.</p></div>${tag("Observed","good")}</div>`}</div></section><div class="grid stats">${stat("Current work",p.work?.work_id||(p.data_scope?.shared?.includes("boards")?"Combined view":"Unresolved"),p.work?.status||"No structured status")}${stat("NAV records",num(nav.records),nav.status==="available"?"Current map inventory":"Unavailable")}${stat("Retained runs",num(nav.run_count),`${num(nav.pass_count)} recorded pass receipts`)}${stat("Held paths",num(held),host.status==="available"?"Local journal observed":"Local journal unavailable")}</div>`+releasePanel(p)}
function delivery(p){
  const d=p.modules.delivery||{},a=p.modules.attribution||{};
  if(!["available","partial"].includes(d.status))return heading("Delivery","See what changed, who owns it, and what can be delivered under current authority.")+empty("Delivery summary unavailable",d.reason||"The current Git and board state could not be projected.");
  const rows=d.paths||[],unknown=d.totals?.ownership_unknown||0,ambiguous=d.totals?.ownership_ambiguous||0;
  const action=name=>{const x=d.authority?.[name]||{},label=x.pending===false?"Nothing pending":x.deliverable===true?"Allowed":x.deliverable===false?"Blocked":"Authority unknown",reasons=x.reasons||[],full=reasons.join(" "),brief=full.length>240?full.slice(0,237)+"...":full||"No blocker recorded.";return `<section class="card"><div class="section-head"><h2>${esc(name[0].toUpperCase()+name.slice(1))}</h2>${status(label,x.pending===false||x.deliverable===true?"good":x.deliverable===false?"warn":"")}</div><p>${esc(brief)}</p>${full.length>240?`<details data-persist="delivery-${esc(name)}-reasons"><summary>All blockers</summary><p class="quiet">${esc(full)}</p></details>`:""}<p class="quiet">${esc(x.qualification||"")}</p></section>`};
  const w=p.modules.write_retry||{},wc=w.counts||{},wl=(w.requests||[])[0],writeQueue=`<section class="card portable-queue"><div class="section-head"><div><h2>Closeout writes</h2><p class="quiet">Blocked board, Docs and optional RAM writes retry through their guarded semantic operations.</p></div><span class="queue-signal ${esc(w.signal||"green")}"><i></i>${esc(w.label||"Ready")}</span></div><div class="grid three">${stat("Waiting",num((wc.pending||0)+(wc.waiting||0)),"Exact owner release required")}${stat("Applying",num(wc.working),w.worker?.stage||"Idle")}${stat("Latest",wl?.kind||"None",wl?.state||"No queued write")}</div>${w.worker?.error?`<p class="queue-error">${esc(w.worker.error)}</p>`:""}</section>`;
  return heading("Delivery","See what changed, who owns it, and what can be delivered under current authority.")+writeQueue+deliveryReadiness(p)+`<div class="grid stats">${stat("Changed paths",num(d.totals?.paths),d.disposition?.working_tree||"Unknown")}${stat("Staged",num(d.totals?.staged),"Current Git index")}${stat("Ahead",d.repository?.divergence?.ahead??"Unknown",d.disposition?.publication||"Unknown")}${stat("Ownership gaps",num(unknown+ambiguous),`${num(unknown)} unknown, ${num(ambiguous)} ambiguous`)}</div><div class="grid two">${action("commit")}${action("publish")}</div><section class="card"><div class="section-head"><h2>Changed paths</h2>${tag(`${rows.length} observed`)}</div><p class="quiet">Git state and linked campaign for each file. Unassigned files have no campaign match.</p>${rows.map(r=>`<div class="route"><div class="route-id">${esc((r.states||[]).join(" + ")||r.disposition)}</div><div><strong>${esc(r.path)}</strong><div class="meta">${(r.ownership?.campaigns||[]).map(c=>tag(c.id,c.status==="completed"?"good":"")).join("")||tag("Unassigned","warn")}</div></div></div>`).join("")||empty("No delivery pending","The working tree and upstream comparison contain no changed paths.")}</section><section class="card"><div class="section-head"><h2>Companion attribution</h2>${tag(`${a.observed_request_count??0} observed requests`,a.observed_request_count?"good":"")}</div><div class="grid three">${stat("Assignments",num(a.record_count),"Exact local assignment records")}${stat("Observed requests",num(a.observed_request_count),"Counted once across helpers")}${stat("Receipts",num(a.receipts?.accepted_count),"Exact native relationship evidence")}</div><p class="quiet">${esc(a.basis||a.reason||"No relationship evidence is available yet.")}</p>${(a.records||[]).map(r=>`<div class="list-item"><div><strong>${esc(r.request_id||"Unknown request")}</strong><p>${esc(r.companion?.observed_model||r.companion?.requested_model||"Worker model unknown")} · ${esc(r.result?.receipt_status||r.result?.assignment_status||"Result unknown")}</p></div>${tag(r.receipt?.status||"Unknown",r.receipt?.status==="observed"?"good":"warn")}</div>`).join("")}</section>`}
const memorySearchState = new Map();
function memorySearchCurrent(){if(!memorySearchState.has(state.project))memorySearchState.set(state.project,{query:"",data:null,loading:false,expanded:{},snapshot:"",offset:0,serial:0});return memorySearchState.get(state.project)}
function memoryFreshness(value){const states=(value||[]).map(i=>i.source_freshness||({match:"matching",stale:"changed"}[i.state]||i.state));return states.includes("changed")?"Source changed":states.includes("missing")?"Source missing":states.includes("unknown")||!states.length?"Freshness unknown":"Source bytes match"}
function memoryApplicability(value){return ({unassessed:"Applicability unassessed",revalidated:"Revalidated",superseded:"Superseded",invalid:"Invalid"})[value?.state]||"Applicability unassessed"}
function expandedMemory(value){if(value.outcome!=="ok")return `<p>${esc(value.message||value.outcome)}</p>`;const c=value.content||{},a=value.semantic_applicability||c.applicability;return `<section class="memory-expanded"><h4>Recorded experience</h4><p class="memory-summary">${esc(c.summary)}</p><p class="quiet">${esc(when(c.recorded_at))} · ${esc(c.kind)} · ${c.unfinished?"Unfinished context":"Recorded result"}</p><h4>Evidence status</h4><p>${tag(memoryFreshness(value.freshness))}${tag(memoryApplicability(a),a?.state==="invalid"||a?.state==="superseded"?"warn":"")}</p>${(value.freshness||[]).map(i=>`<p class="board-source">${esc(i.path)} · ${esc(i.source_freshness||i.state)}</p>`).join("")||"<p>No pinned sources.</p>"}<p class="quiet">${esc(a?.reason||"No explicit semantic assessment is recorded.")}</p><p class="quiet">${esc(value.qualification)}</p><details><summary>Exact provenance</summary><pre>${esc(JSON.stringify({path:value.path,revision:value.revision,sha256:value.sha256,work:c.work,sources:c.sources,actors:c.actors,applicability:a},null,2))}</pre></details></section>`}
function memoryPages(data){const total=Math.max(0,Number(data?.total)||0),size=Math.max(1,Number(data?.page_size)||50),current=Math.min(Math.max(1,Math.floor((Number(data?.offset)||0)/size)+1),Math.max(1,Math.ceil(total/size))),count=Math.max(1,Math.ceil(total/size)),start=Math.max(1,Math.min(current-3,count-6)),end=Math.min(count,start+6);return {current,count,size,numbers:Array.from({length:end-start+1},(_,i)=>start+i)}}
function memory(p){
  const m=memorySearchCurrent(),data=m.data,results=data?.results||[],pages=memoryPages(data),first=results.length?(Number(data.offset)||0)+1:0,last=(Number(data?.offset)||0)+results.length;
  const paging=data&&pages.count>1?`<nav class="memory-paging" aria-label="Memory result pages"><button type="button" data-memory-page="${Math.max(0,(pages.current-2)*pages.size)}" ${pages.current===1||m.loading?"disabled":""}>Previous</button>${pages.numbers.map(page=>`<button type="button" data-memory-page="${(page-1)*pages.size}" aria-current="${page===pages.current?"page":"false"}" ${m.loading?"disabled":""}>${page}</button>`).join("")}<button type="button" data-memory-page="${pages.current*pages.size}" ${pages.current===pages.count||m.loading?"disabled":""}>Next</button><span>Page ${pages.current} of ${pages.count}</span></nav>`:"";
  return heading("Memory","Find a useful experience, inspect its evidence, and leave unrelated memories unread.",`<button type="button" data-memory-refresh ${m.loading?"disabled":""}>${m.loading?"Refreshing…":"Refresh memories"}</button>`)+`<section class="card"><h2>Find relevant memories</h2><form id="memory-search-form" class="memory-query"><label for="memory-query">What are you working on?</label><div><input id="memory-query" type="search" value="${esc(m.query)}" placeholder="Task, feature, lesson or source path"><button type="submit" ${m.loading?"disabled":""}>${m.loading?"Searching…":"Search"}</button><button type="button" data-memory-browse ${m.loading?"disabled":""}>Browse</button></div></form><p class="quiet">Search and Browse are explicit reads, even when optional agent retrieval is off. Native visibility controls still apply. Results stay fixed while you page through them; refresh to observe current records.</p>${data?`<p>${esc(data.message||data.basis||"Lexical matches; relevance and accuracy need your judgment.")}</p><p class="quiet">${num(data.total)} matching records. Showing ${first}${results.length?`–${last}`:""}.</p>${results.map((r,i)=>`<article class="memory-result"><div class="section-head"><h3>${esc(r.identity?.memory_id||r.path)}</h3><div class="meta">${tag(memoryFreshness(r.freshness),(r.freshness||[]).some(i=>(i.source_freshness||i.state)==="changed"||i.state==="stale"||i.state==="missing")?"warn":"")}${tag(memoryApplicability(r.semantic_applicability),["invalid","superseded"].includes(r.semantic_applicability?.state)?"warn":"")}</div></div><p>${esc(r.preview)}</p><div class="meta">${tag(`Revision ${r.revision}`)}${(r.matched_terms||[]).map(t=>tag(t)).join("")}</div><p class="board-source">${esc(r.path)}</p><button type="button" data-memory-expand="${i}">Read exact entry</button>${m.expanded[`${r.path}:${r.sha256}`]?expandedMemory(m.expanded[`${r.path}:${r.sha256}`]):""}</article>`).join("")||'<p>No matching memories. Try more specific task or source terms, or continue directly with the source.</p>'}${paging}`:"<p>Enter a query to see matching previews, or browse the recorded library. Nothing is automatically selected for you.</p>"}</section><details class="card memory-library" data-persist="memory-library"><summary>Recorded memory library</summary>${memoryLibrary(p)}</details>`;
}
async function searchMemory(offset=0,browse=false,fresh=false){
  const selectedProject=state.project,m=memorySearchCurrent(),serial=++m.serial;if(browse)m.query="";if(fresh){m.snapshot="";m.expanded={}}m.loading=true;render();
  try{const q=new URLSearchParams({project:String(selectedProject),query:m.query,offset:String(offset),snapshot:m.snapshot,view:state.workView,...(state.workSource?{source:state.workSource}:{})});const response=await fetch(`/api/v1/ram/search?${q}`,{cache:"no-store"});const result=await response.json();if(!response.ok)throw new Error(result.message||"Search unavailable");if(serial!==m.serial)return;m.data=result;m.snapshot=result.snapshot||"";m.offset=result.offset||0;m.expanded={}}
  catch(error){if(serial===m.serial)m.data={results:[],total:0,offset:0,page_size:50,message:error.message}}
  finally{if(serial===m.serial){m.loading=false;if(state.project===selectedProject)render()}}
}
function refreshMemorySnapshot(){const m=memorySearchCurrent();m.snapshot="";m.offset=0;m.expanded={};return searchMemory(0,false,true)}
async function expandMemory(index){const selectedProject=state.project,m=memorySearchCurrent(),r=m.data?.results?.[index];if(!r)return;try{const q=new URLSearchParams({project:String(selectedProject),path:r.path,revision:String(r.revision),sha256:r.sha256,view:r.source_id?"current":"local",...(r.source_id?{source:r.source_id}:{})});const response=await fetch(`/api/v1/ram/get?${q}`,{cache:"no-store"});const result=await response.json();if(!response.ok)throw new Error(result.message||"Entry unavailable");m.expanded[`${r.path}:${r.sha256}`]=result;if(state.project===selectedProject)render()}catch(error){toast(error.message)}}
content.addEventListener("input",e=>{if(e.target.id==="memory-query")memorySearchCurrent().query=e.target.value});
content.addEventListener("submit",e=>{if(e.target.id==="memory-search-form"){e.preventDefault();searchMemory(0,false,true)}});
content.addEventListener("click",e=>{if(e.target.closest("[data-memory-refresh]"))refreshMemorySnapshot();if(e.target.closest("[data-memory-browse]"))searchMemory(0,true,true);const more=e.target.closest("[data-memory-page]");if(more&&!more.disabled)searchMemory(Number(more.dataset.memoryPage));const entry=e.target.closest("[data-memory-expand]");if(entry)expandMemory(Number(entry.dataset.memoryExpand))});
function memoryLibrary(p){const ram=p.modules.ram;return (ram?.status!=="available"?empty("RAM unavailable",ram?.reason||"No readable RAM store."):`<div class="grid stats">${stat("Records",num(ram.totals.records),"Current visible records")}${stat("Summary words",num(ram.totals.words),"Derived from latest summaries")}${stat("Stored bytes",num(ram.totals.bytes),"Complete record envelopes")}${stat("Unfinished",num(ram.records.filter(x=>x.unfinished).length),"Explicit working context")}</div><div class="grid">${ram.records.map(r=>`<details class="card run"><summary><div><p class="quiet work-date">${esc(when(r.recorded_at))}</p><h2>${esc(r.identity.memory_id)}</h2><p>${esc(r.identity.work_id)} · ${esc(r.kind)} · revision ${r.revision}</p></div>${tag(r.unfinished?"Unfinished":"Recorded",r.unfinished?"warn":"good")}</summary><p class="memory-summary">${esc(r.summary)}</p><div class="meta">${tag(`${r.words} words`)}${tag(`${r.bytes} bytes`)}${tag(`${r.versions.length} versions`)}${tag(r.visibility)}</div><div class="passes">${r.versions.map(v=>`<div class="pass"><div class="pass-head"><strong>Revision ${v.revision}</strong><span class="quiet">${esc(when(v.recorded_at))}</span></div><p>${esc(v.summary)}</p></div>`).join("")}</div></details>`).join("")||empty("No memories","The configured RAM store contains no visible records.")}</div>`)}
function navigation(p){const nav=p.modules.nav;if(nav?.status!=="available")return heading("Navigation","Follow reusable routes to source.")+empty("NAV unavailable",nav?.reason||"No readable map.");const kindHelp={gap:"A recorded place where useful source coverage or semantic review is still missing.",system:"A major repository system or responsibility with source-backed routes.",surface:"A human- or agent-facing page, command, interface, or entry point.",interaction:"A recorded action or relationship between a person, agent, and surface.",journey:"A multi-step path through repository work or product behavior.","source-family":"A group of related source files or evidence."},reviewHelp={gap:"An explicit gap whose coverage or review remains unproven.",candidate:"A source-linked route that still awaits the required review or acceptance.",accepted:"A route whose native owner records acceptance."};const kinds=Object.entries(nav.by_kind).map(([label,value])=>({label,value,description:kindHelp[label]||`Recorded NAV ${label} routes.`}));return heading("Navigation","Follow systems, surfaces and journeys back to their source evidence.")+`<div class="grid two"><section class="card"><div class="section-head"><h2>Map composition</h2>${status(`${nav.records} records`,"good")}</div>${chart(kinds,"NAV records by kind")}<div class="legend"><span>Record count</span></div></section><section class="card"><div class="section-head"><h2>Review state</h2></div>${chart(Object.entries(nav.review).map(([label,value])=>({label,value,description:reviewHelp[label]||`Recorded ${label} review state.`})),"NAV records by review state")}</section></div><section class="card"><div class="section-head"><h2>Routes</h2><span class="quiet">Source-linked descriptions</span></div>${nav.routes.slice(0,80).map(r=>`<div class="route"><div class="route-id">${esc(r.id)}</div><div><strong>${esc(r.title)}</strong><p>${esc(r.summary)}</p><div class="meta">${tag(r.kind)}${tag(r.review,r.review==="accepted"?"good":"")}${r.evidence.slice(0,2).map(x=>tag(x)).join("")}</div></div></div>`).join("")}</section>`}
function gallery(p){const data=p.modules.visual_atlas||{},items=data.items||[];return heading("Surface gallery","Browse dated dashboard captures and trace each view back to its shared style owner.")+(!["available","partial"].includes(data.status)?empty("No retained surface captures",data.message||data.reason||"Capture a reviewed dashboard surface to begin the gallery."):`<section class="gallery-intro card"><div><h2>${num(items.length)} retained views</h2><p>${esc(data.qualification||"Screenshots demonstrate appearance at one observed state.")}</p></div>${status("Local evidence","good")}</section><div class="surface-gallery">${items.map(item=>{const query=new URLSearchParams({project:String(state.project),name:item.name});return `<article class="card surface-card"><a class="surface-image" href="/api/v1/surface-image?${query}" target="_blank" rel="noopener"><img src="/api/v1/surface-image?${query}" alt="${esc(item.title)} dashboard capture" loading="lazy"></a><div class="surface-card-copy"><div class="section-head"><div><h2>${esc(item.title)}</h2><p>${esc(item.variant)} · observed ${esc(new Date(item.observed_at).toLocaleString())}</p></div>${tag(item.variant)}</div><details data-persist="surface-${esc(item.id)}"><summary>Source and guidance</summary><p class="board-source">${esc(item.name)}</p><p class="board-source">${esc(item.style_path)}</p></details></div></article>`}).join("")||empty("Gallery ready","No retained screenshots were found in the configured gallery folder.")}</div>`) }
function passRow(x){const disp=x.dispositions||{},positive=(disp.covered||0)+(disp.confirmed||0)+(disp.candidate||0),rejected=disp.rejected||0;return `<div class="pass"><div class="pass-head"><strong>${x.index}. ${esc(x.sweep_id||"Unknown sweep")}</strong>${tag(`${x.changed_records?.length||0} changed`)}</div><p>${esc(x.summary)}</p><div class="meta">${tag(`${x.inspected_paths?.length||0} paths`)}${Object.entries(disp).map(([k,v])=>tag(`${v} ${k}`,k==="rejected"?"warn":"")).join("")}</div><div class="microbars" aria-label="${positive} positive and ${rejected} rejected dispositions"><span data-flex="${Math.max(positive,1)}"></span>${rejected?`<span class="rejected" data-flex="${rejected}"></span>`:""}</div></div>`}
function runs(p){const module=state.runModule;if(module==="visual_atlas")return gallery(p);const data=p.modules[module];const tabs=`<div class="tabs" role="group" aria-label="Discovery module"><button data-module="nav" aria-pressed="${module==="nav"}">NAV</button><button data-module="atlas" aria-pressed="${module==="atlas"}">ATLAS</button><button data-module="visual_atlas" aria-pressed="${module==="visual_atlas"}">Visual ATLAS</button></div>`;if(data?.status!=="available")return heading("Runs","See what every discovery cycle and pass found.")+tabs+empty(`ATLAS ${data?.status||"unavailable"}`,data?.message||data?.reason||"No run adapter is available.");return heading("Runs","See what every discovery cycle and pass found.")+tabs+`<div class="grid stats">${stat("Cycles",num(data.run_count),"Current plus archived")}${stat("Pass receipts",num(data.pass_count),"Every retained packet")}${stat("Map records",num(data.records),"Current native records")}${stat("History",data.history_complete?"Complete":"Partial","For this selected view")}</div><section class="card"><div class="section-head"><h2>Pass receipts by cycle</h2><span class="quiet">Runs, unique sweeps and packets stay distinct</span></div>${chart(data.runs.map(x=>({label:x.run_id||"Unknown",value:x.pass_count})),"Recorded pass receipts by NAV cycle")}</section><div class="grid">${data.runs.map((r,i)=>`<details class="card run" ${i===0?"open":""}><summary><div><h2>${esc(r.run_id||"Unknown run")}</h2><p>${esc(r.mode||"Unknown mode")} · ${esc(r.status||"Unknown status")} · ${esc((r.source_commit||"").slice(0,8)||"No commit")}</p></div>${tag(`${r.sweep_count??"?"} sweeps · ${r.pass_count} receipts`,r.current?"good":"")}</summary>${r.detail_unavailable?`<div class="callout">${esc(r.detail_unavailable)}</div>`:`<div class="passes">${r.passes.map(passRow).join("")||`<div class="empty">No pass details were retained for this run.</div>`}</div>`}</details>`).join("")}</div>`}
function activity(p){const attempts=p.host?.attempts||[];return heading("Activity","Follow checkpoints and protected work through meaningful boundaries.")+(p.host?.status!=="available"?empty("Local activity unavailable",p.host?.reason||"No readable host journal."):`<div class="grid">${attempts.map(a=>`<section class="card"><div class="section-head"><div><h2>${esc(a.id)}</h2><span class="quiet">${esc(a.actor)} · generation ${a.generation}</span></div>${status(a.state,a.state==="active"?"good":a.state==="paused"?"warn":"")}</div><div class="grid three">${stat("Reserved paths",a.reservations.length,"Exact protected targets")}${stat("Unfinished effects",a.unfinished_effects.length,"Prepared or rolling back")}${stat("Checkpoint",Object.keys(a.checkpoint||{}).length?"Present":"Empty","Recorded restart context")}</div>${a.reservations.length?`<div class="callout">Protected: ${a.reservations.map(r=>esc(r.path)).join(", ")}. Inspect the owning attempt before recovery.</div>`:""}</section>`).join("")||empty("No attempts","The local journal has no attempts for this repository.")}</div>`)}
function metricRangeKey(p){return String(p?.repository_id||p?.scope_id||state.project)}
function metricRange(p){const key=metricRangeKey(p);if(state.metricRanges[key])return state.metricRanges[key];try{const saved=JSON.parse(localStorage.getItem(`eutonos.metrics.range.${key}`));if(saved&&typeof saved==="object")return state.metricRanges[key]=saved}catch(_error){}return state.metricRanges[key]={kind:"all"}}
function storeMetricRange(p,value){const key=metricRangeKey(p);state.metricRanges[key]=value;delete state.metricDrafts[key];try{localStorage.setItem(`eutonos.metrics.range.${key}`,JSON.stringify(value))}catch(_error){}}
function metricRangeSignature(p,value=metricRange(p)){return `${metricRangeKey(p)}:${JSON.stringify(value)}`}
function metricDate(value){if(!value)return null;const date=new Date(value);return Number.isNaN(date.valueOf())?null:date.toISOString()}
function captureMetricDrafts(){if(!["metrics","timeline"].includes(state.view)||!state.snapshot)return;const p=project(),toolbar=content.querySelector(".metrics-range");if(!p||!toolbar)return;const key=metricRangeKey(p);if(state.metricRangeUpdating===key)return;state.metricDrafts[key]={amount:toolbar.querySelector("#metric-range-amount")?.value||"",unit:toolbar.querySelector("#metric-range-unit")?.value||"hours",from:toolbar.querySelector("#metric-range-from")?.value||"",to:toolbar.querySelector("#metric-range-to")?.value||""}}
function metricToolbar(p,meta={}){const r=metricRange(p),d=state.metricDrafts[metricRangeKey(p)]||{},relative=r.kind==='relative',exact=r.kind==='exact';return `<section class="metrics-range" aria-label="Select range"><div class="metric-range-title"><h2>Select range</h2>${tag(relative?`Last ${r.amount} ${r.unit}`:exact?'Date range':'All time')}</div><div class="compact-range"><div class="range-choice ${relative?'selected':''}"><label>Last<input id="metric-range-amount" type="number" min="1" step="1" value="${esc(d.amount||(relative?r.amount:1))}"></label><label>Unit<select id="metric-range-unit">${['minutes','hours','days','weeks','months','years'].map(u=>`<option ${(d.unit||r.unit||'hours')===u?'selected':''}>${u}</option>`).join('')}</select></label></div><div class="range-choice ${exact?'selected':''}"><label>From<input id="metric-range-from" type="datetime-local" value="${esc(d.from??(exact?r.from:''))}"></label><label>To<input id="metric-range-to" type="datetime-local" value="${esc(d.to??(exact?r.to:''))}"></label></div><button data-metric-preset="all" aria-pressed="${r.kind==='all'}">All time</button></div><span class="sr-only" aria-live="polite">${state.metricLoading?'Refreshing metrics':meta.included_turn_count==null?'Range ready':num(meta.included_turn_count)+' completed turns included'}</span></section>`}
function metricsHeading(p,meta,coverage={}){return `<header class="page-head metrics-page-head"><div class="metrics-title"><div><h1>Metrics</h1><p>Usage assigned to chats in this project, including work performed elsewhere.</p></div><div class="metrics-title-actions">${status(coverage.status||"Unknown",coverage.status==="complete"?"good":"warn")}<button type="button" data-metrics-customize aria-pressed="${state.metricCustomize}">Customize</button></div></div>${metricToolbar(p,meta)}</header>`}
const metricModuleLabels={token:"Token usage",summary:"Summary",comparisons:"Usage comparisons",helpers:"Request-linked helpers",coverage:"Recorded coverage",estimates:"Cost comparisons",models:"Totals by model",efforts:"Totals by thinking level",pairs:"Totals by model pair",relationships:"Companion relationships",orchestration:"Orchestration activity",history:"Token history and categories",operations:"Other operations",diagnostics:"Diagnostics"};
const metricModuleOrder=Object.keys(metricModuleLabels);
function metricLayout(p,helpersEmpty=false){const key=metricRangeKey(p);if(state.metricLayouts[key])return state.metricLayouts[key];let saved;try{saved=JSON.parse(localStorage.getItem(`eutonos.metrics.layout.${key}`))}catch(_error){}const order=Array.isArray(saved?.order)?[...saved.order.filter(id=>metricModuleOrder.includes(id)),...metricModuleOrder.filter(id=>!saved.order.includes(id))]:[...metricModuleOrder],hidden=new Set(Array.isArray(saved?.hidden)?saved.hidden.filter(id=>metricModuleOrder.includes(id)):helpersEmpty?["helpers"]:[]);return state.metricLayouts[key]={order,hidden}}
function storeMetricLayout(p,layout){state.metricLayouts[metricRangeKey(p)]=layout;try{localStorage.setItem(`eutonos.metrics.layout.${metricRangeKey(p)}`,JSON.stringify({order:layout.order,hidden:[...layout.hidden]}))}catch(_error){}}
function metricsCustomizePanel(p,layout){if(!state.metricCustomize)return "";return `<section class="card metrics-customize"><div class="section-head"><div><h2>Arrange metrics</h2><p class="quiet">Drag modules or use the arrow buttons. Clear a checkbox to hide a module.</p></div></div><div class="metrics-module-list">${layout.order.map((id,index)=>`<div class="metrics-module-choice" draggable="true" data-metric-layout-row="${id}"><span class="drag-handle" aria-hidden="true">⋮⋮</span><label><input type="checkbox" data-metric-visible="${id}" ${layout.hidden.has(id)?"":"checked"}>${esc(metricModuleLabels[id])}</label><div><button type="button" data-metric-move="${id}" data-direction="-1" ${index===0?"disabled":""} aria-label="Move ${esc(metricModuleLabels[id])} up">↑</button><button type="button" data-metric-move="${id}" data-direction="1" ${index===layout.order.length-1?"disabled":""} aria-label="Move ${esc(metricModuleLabels[id])} down">↓</button></div></div>`).join("")}</div></section>`}
function renderMetricModules(p,modules,layout){return `<div class="metrics-modules ${state.metricCustomize?"is-arranging":""}">${layout.order.filter(id=>!layout.hidden.has(id)&&modules[id]).map(id=>`<div class="metric-module" draggable="${state.metricCustomize}" data-metric-module="${id}">${state.metricCustomize?`<div class="metric-drag-label"><span aria-hidden="true">⋮⋮</span> ${esc(metricModuleLabels[id])}</div>`:""}${modules[id]}</div>`).join("")}</div>`}
function calendarRangeStart(now,amount,unit){const start=new Date(now),day=start.getUTCDate();start.setUTCDate(1);start.setUTCMonth(start.getUTCMonth()-amount*(unit==='years'?12:1));const end=new Date(start);end.setUTCMonth(end.getUTCMonth()+1,0);start.setUTCDate(Math.min(day,end.getUTCDate()));return start}
async function loadMetricProjection(p,range=metricRange(p),background=false){if(state.metricLoading)return;const requestedProject=state.project,signature=metricRangeSignature(p,range),params=new URLSearchParams({project:String(requestedProject)});let changed=!background;if(range.kind==="relative"){if(['months','years'].includes(range.unit)){const now=new Date();params.set('from',calendarRangeStart(now,range.amount,range.unit).toISOString());params.set('to',now.toISOString())}else{params.set("amount",String(range.amount));params.set("unit",range.unit)}}else if(range.kind==="exact"){const from=metricDate(range.from),to=metricDate(range.to);if(!from||!to||from>=to){toast("Choose a valid From and To date.");return}params.set("from",from);params.set("to",to)}state.metricLoading=true;state.metricRangeUpdating=metricRangeKey(p);if(!background)render();state.metricRangeUpdating=null;try{const response=await fetch(`/api/v1/companion/metrics?${params}`,{cache:"no-store"});if(!response.ok)throw new Error(`Metrics service returned ${response.status}`);const data=await response.json();if(state.project!==requestedProject||metricRangeSignature(project())!==signature)return;changed=JSON.stringify(state.metricProjection?.data)!==JSON.stringify(data);state.metricProjection={project:requestedProject,signature,data}}catch(error){if(state.project===requestedProject&&!background){state.metricProjection={project:requestedProject,signature,data:null};toast(`Metrics unavailable: ${error.message}`)}}finally{state.metricLoading=false;if(state.project===requestedProject&&metricRangeSignature(project())!==signature){queueMicrotask(()=>loadMetricProjection(project()));return}if(changed&&state.project===requestedProject&&["metrics","timeline"].includes(state.view))render();else if(!background&&["metrics","timeline"].includes(state.view)&&project()&&state.project!==requestedProject)queueMicrotask(()=>loadMetricProjection(project()))}}
function assignmentMetrics(data){
  if(!data||data.status==="unavailable")return `<section class="card"><h2>Request-linked helpers</h2><p class="quiet">Assignment evidence is unavailable. Other recorded usage remains visible.</p></section>`;
  const pairRows=(data.pairs||[]).map(pair=>`<tr><td>${modelLabel(pair.primary_model)} + ${modelLabel(pair.helper_model)}</td><td>${num(pair.observed_request_count)}</td><td>${num(pair.observed_assignment_count)}</td><td>${pair.helper_tokens?.input_tokens==null?"Unknown":num(pair.helper_tokens.input_tokens)}</td><td>${pair.helper_tokens?.output_tokens==null?"Unknown":num(pair.helper_tokens.output_tokens)}</td></tr>`).join("");
  return `<section class="card metrics-coverage"><div class="section-head"><h2>Request-linked helpers</h2>${tag("Exact turn links")}</div><div class="grid stats">${stat("Registered assignments",num(data.assignment_count),"All retained assignment records")}${stat("Matched requests",num(data.observed_request_count),"Primary and helper evidence in this range")}${stat("Unmatched assignments",num(data.unverified_assignment_count),"Missing, conflicting or out-of-range evidence")}${stat("Ambiguous links",num(data.ambiguous_assignment_count),"Excluded from attributed usage")}</div>${pairRows?`<div class="table-wrap"><table><thead><tr><th>Primary + helper</th><th>Requests</th><th>Helper turns</th><th>Helper input</th><th>Helper output</th></tr></thead><tbody>${pairRows}</tbody></table></div>`:`<p class="quiet">No helper requests with matching native turn evidence in this range.</p>`}<p class="quiet">Requested models alone do not count as observed work. This covers registered assignments, not every request made in the project.</p></section>`;
}

function metrics(p){
  const data=p.modules.measurement,summary=data?.summary||{},levels=summary.levels||{},entries=Object.entries(levels).map(([label,x])=>({label,value:x.count||0}));
  const workflow=summary.workflow_efficiency||{};
  const workflowEfficiency=`<section class="card comparison-section"><div class="section-head"><h2>Workflow efficiency</h2>${tag("Observed metadata")}</div><div class="grid stats">${stat("Outer invocations",workflow.outer_invocations??"Unknown","Observed entry spans")}${stat("Logical operations",workflow.logical_operations??"Unknown","Unique retained logical IDs")}${stat("Repeated events",workflow.repeated_logical_events??"Unknown",`${workflow.repeated_failure_events??"Unknown"} repeated failures`)}${stat("Returned bytes",workflow.returned_bytes??"Unknown",`${workflow.unknown_entry_sizes??"Unknown"} entry sizes unknown`)}</div><p class="quiet">${esc(workflow.coverage||"Workflow efficiency is unavailable for this selection. Nested durations are not summed into task elapsed time.")}</p></section>`;
  const range=metricRange(p),signature=metricRangeSignature(p,range);if((state.metricProjection?.project!==state.project||state.metricProjection?.signature!==signature)&&!state.metricLoading)queueMicrotask(()=>loadMetricProjection(p,range));
  const ranged=state.metricProjection?.project===state.project&&state.metricProjection?.signature===signature?state.metricProjection.data:null,rangeMeta=ranged?.range||{};
  const lifetime=ranged?.lifetime_metrics||ranged?.metrics||ranged||p.modules.companion?.lifetime_metrics;
  if(!lifetime)return metricsHeading(p,rangeMeta)+`<div class="grid stats">${stat("Recorded lifetime","Unavailable","No lifetime metrics in this snapshot")}${stat("Helper usage","Unknown","Recorded completed runs only")}${stat("Observed savings","Unknown","Requires qualified matched samples")}${stat("Coverage","Unknown","Missing observations are never treated as zero")}</div>${workflowEfficiency}<section class="card"><div class="section-head"><h2>Observed operations</h2>${status(data?.status||"Unavailable",data?.status==="available"?"good":"warn")}</div>${entries.length?chart(entries,"Observed measurement events by level"):empty("No measured events","The dashboard will not turn missing observations into zero activity.")}</section>`;
  const coverage=lifetime.coverage||{},tokens=lifetime.tokens||{},helper=lifetime.helper||{},observed=lifetime.observed||{},counterfactual=lifetime.counterfactual||{},credits=lifetime.credits||{};
  const models=Array.isArray(lifetime.model_roles)?lifetime.model_roles:(Array.isArray(lifetime.models)?lifetime.models:[]),efforts=Array.isArray(lifetime.efforts)?lifetime.efforts:[],pairs=Array.isArray(lifetime.pairs)?lifetime.pairs:[],relationships=Array.isArray(lifetime.relationships)?lifetime.relationships:[],trends=Array.isArray(lifetime.trends)?lifetime.trends:[];
  const metricValue=value=>typeof value==="number"&&Number.isFinite(value)?num(value):"Unknown";
  const metricMoney=value=>typeof value==="number"&&Number.isFinite(value)?new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",minimumFractionDigits:4,maximumFractionDigits:4}).format(value):"Unknown",metricCredits=value=>typeof value==="number"&&Number.isFinite(value)?num(value):"Unknown";
  const helperFrequency=helper.frequency_of_recorded_completed_runs??helper.share_of_recorded_completed_agent_turns;
  const frequency=typeof helperFrequency==="number"?`${(helperFrequency*100).toFixed(1)}%`:"Unknown";
  const workgroups=helper.workgroups||{},workgroupFrequency=typeof workgroups.recorded_workgroup_helper_incidence==="number"?`${(workgroups.recorded_workgroup_helper_incidence*100).toFixed(1)}%`:"Unknown";
  const tokenValue=field=>typeof tokens[field]==="number"?tokens[field]:tokens[`observed_${field}`];
  const visibleOutput=visibleOutputTokens(tokens);
  const tokenBoundary=field=>typeof tokens[field]==="number"?"Recorded total":typeof tokens[`observed_${field}`]==="number"?`Observed subtotal from ${metricValue(tokens.measured_run_count)} complete runs; ${metricValue(tokens.unknown_run_count)} unknown`:"No complete token subtotal";
  const groupToken=(value,field)=>{const t=value?.tokens||{};const strict=t[field],subtotal=t[`observed_${field}`];return typeof strict==="number"?metricValue(strict):typeof subtotal==="number"?`${metricValue(subtotal)} subtotal`:"Unknown"};
  const groupVisibleOutput=value=>{const result=visibleOutputTokens(value),t=value?.tokens||{},partial=['output_tokens','reasoning_output_tokens'].some(field=>t[field]==null&&t[`observed_${field}`]!=null);return result==null?'Unknown':`${metricValue(result)}${partial?' subtotal':''}`};
  const tokenTotal=value=>{const t=value?.tokens||value||{},input=t.input_tokens??t.observed_input_tokens,output=t.output_tokens??t.observed_output_tokens;return typeof input==="number"&&typeof output==="number"?input+output:null};
  const coverageWindow=coverage.window?.start||coverage.window?.end?`${when(coverage.window?.start)} to ${when(coverage.window?.end)}`:"No recorded date window";
  const modelRows=models.map(x=>`<tr><td><strong>${modelLabel(x.model)}</strong><br><span class="quiet">${esc(x.model)}${x.role?` · ${esc(x.role)}`:""}</span></td><td>${esc(x.effort&&x.effort!=="unknown-or-multiple"?x.effort:"Unknown / multiple")}</td><td>${metricValue(x.completed_run_count)} / ${metricValue(x.run_count)}</td><td>${groupToken(x,"input_tokens")}</td><td>${groupVisibleOutput(x)}</td><td>${groupToken(x,"reasoning_output_tokens")}</td><td>${metricMoney(x.observed_standard_api_estimate_usd)}</td><td>${metricCredits(x.credits?.standard_rate_credit_estimate)}</td></tr>`).join("");
  const effortRows=efforts.map(x=>`<tr><td><strong>${esc(x.effort==="unknown-or-multiple"?"Unknown / multiple":x.effort)}</strong></td><td>${metricValue(x.completed_run_count)} / ${metricValue(x.run_count)}</td><td>${groupToken(x,"input_tokens")}</td><td>${groupVisibleOutput(x)}</td><td>${groupToken(x,"reasoning_output_tokens")}</td><td>${metricMoney(x.observed_standard_api_estimate_usd)}</td></tr>`).join("");
  const pairRows=pairs.map(x=>`<tr><td>${x.primary_model?`${modelLabel(x.primary_model)} + ${x.helper_model?modelLabel(x.helper_model):"companion off"}`:'<strong>Companion pairing not recorded</strong>'}</td><td>${metricValue(x.completed_run_count)} / ${metricValue(x.run_count)}</td><td>${groupToken(x,"input_tokens")}</td><td>${groupVisibleOutput(x)}</td><td>${groupToken(x,"reasoning_output_tokens")}</td><td>${metricMoney(x.observed_standard_api_estimate_usd)}</td><td>${metricCredits(x.credits?.standard_rate_credit_estimate)}</td></tr>`).join("");
  const relationshipRows=relationships.map(x=>`<tr><td>${modelLabel(x.primary_model)} → ${modelLabel(x.helper_model)}</td><td>${metricValue(x.completed_primary_turn_count)}</td><td>${metricValue(x.completed_helper_turn_count)}</td><td>${metricValue(x.recorded_assisted_workgroup_count)}</td></tr>`).join("");
  const assignments=lifetime?.assignments,helpersEmpty=!assignments||(!assignments.assignment_count&&!assignments.observed_request_count&&!assignments.unverified_assignment_count&&!assignments.ambiguous_assignment_count&&!(assignments.pairs||[]).length),layout=metricLayout(p,helpersEmpty);
  const modules={
    token:modelTokenChart(models,pairs),
    summary:`<div class="grid stats metrics-summary">${stat("Recorded input",metricValue(tokenValue("input_tokens")),`${tokenBoundary("input_tokens")}; ${metricValue(tokenValue("cached_input_tokens"))} cached included`)}${stat("Visible output",metricValue(visibleOutput),`Text and tool output; full output is ${metricValue(tokenValue("output_tokens"))}`)}${stat("Reasoning",metricValue(tokenValue("reasoning_output_tokens")),"Exposed reasoning tokens; included in full output")}${stat("Helper turn share",frequency,`${metricValue(helper.completed_run_count)} completed support turns; ${workgroupFrequency} of recorded workgroups used a helper`)}${stat("Coverage",coverage.status||"Unknown",`${metricValue(coverage.usage_complete_run_count)} runs with complete usage; ${metricValue(coverage.usage_unknown_run_count)} unknown`)}</div>`,
    comparisons:metricsComparisons(tokens,coverage,models),
    helpers:assignmentMetrics(assignments),
    coverage:`<section class="card metrics-coverage"><div class="section-head"><h2>Recorded lifetime coverage</h2>${tag(`${metricValue(coverage.registered_source_count)} registered + ${metricValue(coverage.discovered_source_count??0)} discovered sources`)}</div><p>${esc(coverage.scope||"Recorded turns from explicitly registered native logs plus retained history.")}</p><div class="meta">${tag(coverageWindow)}${tag(`${metricValue(coverage.recorded_run_count)} recorded runs`)}${tag(`${metricValue(coverage.duplicate_turns_excluded)} duplicates excluded`)}</div>${coverage.note?`<p class="quiet">${esc(coverage.note)}</p>`:""}</section>`,
    estimates:`<div class="grid two comparison-section"><section class="card"><div class="section-head"><h2>Observed Standard API equivalent</h2>${tag(`${metricValue(observed.priced_run_count)} priced · ${metricValue(observed.unpriced_run_count)} unpriced`)}</div><div class="comparison-highlight">${metricMoney(observed.observed_standard_api_estimate_usd)}<span>Observed recorded model usage at the frozen standard API rates</span></div><p>Standard-rate credit estimate: <strong>${metricCredits(credits.standard_rate_credit_estimate)}</strong> across ${metricValue(credits.priced_run_count)} priced runs.</p><p>Matched observed savings: <strong>${metricMoney(observed.matched_savings_usd)}</strong> across ${metricValue(observed.matched_savings_sample_count)} qualified sample(s).</p><p class="quiet">Credit estimates are not account deductions. Dollar estimates are not Codex account charges.</p></section><section class="card"><div class="section-head"><h2>Same-token all-primary scenario</h2>${tag("Hypothetical","warn")}</div><div class="comparison-highlight">${metricMoney(counterfactual.estimate_usd)}<span>${esc(counterfactual.scenario||"Same recorded tokens repriced as all primary")}</span></div><p>${metricValue(counterfactual.repriced_run_count)} runs repriced; ${metricValue(counterfactual.excluded_run_count)} excluded.</p><p class="quiet">${esc(counterfactual.assumption||"Same observed token vectors; hypothetical pricing only, not measured usage or savings.")} ${esc(counterfactual.exclusion_rule||"")}</p></section></div>`,
    models:`<section class="card comparison-section"><div class="section-head"><h2>Recorded totals by model</h2><span class="quiet">Primary and helper roles are separate</span></div>${modelRows?`<div class="table-scroll"><table class="comparison-table metrics-table"><thead><tr><th>Model / role</th><th>Thinking</th><th>Completed / recorded</th><th>Input</th><th>Visible output</th><th>Reasoning</th><th>Standard API equivalent</th><th>Credit estimate</th></tr></thead><tbody>${modelRows}</tbody></table></div>`:empty("No model totals","No model totals were recorded in this range.")}</section>`,
    efforts:`<section class="card comparison-section"><div class="section-head"><h2>Recorded totals by thinking level</h2><span class="quiet">Observed native turn configuration</span></div>${effortRows?`<div class="table-scroll"><table class="comparison-table metrics-table"><thead><tr><th>Thinking</th><th>Completed / recorded</th><th>Input</th><th>Visible output</th><th>Reasoning</th><th>Standard API equivalent</th></tr></thead><tbody>${effortRows}</tbody></table></div>`:empty("No thinking-level totals","No observed thinking-level metadata was recorded in this range.")}</section>`,
    pairs:`<section class="card comparison-section"><div class="section-head"><h2>Recorded totals by model pair</h2><span class="quiet">Observed configurations only</span></div>${pairRows?`<div class="table-scroll"><table class="comparison-table metrics-table"><thead><tr><th>Primary + helper</th><th>Completed / recorded</th><th>Input</th><th>Visible output</th><th>Reasoning</th><th>Standard API equivalent</th><th>Credit estimate</th></tr></thead><tbody>${pairRows}</tbody></table></div>`:empty("No pair totals","No model-pair totals were recorded in this range.")}</section>`,
    relationships:`<section class="card comparison-section"><div class="section-head"><h2>Observed primary → companion usage</h2><span class="quiet">Recorded workgroups, not user requests</span></div>${relationshipRows?`<div class="table-scroll"><table class="comparison-table metrics-table"><thead><tr><th>Primary → companion</th><th>Completed primary turns</th><th>Completed helper turns</th><th>Recorded assisted groups</th></tr></thead><tbody>${relationshipRows}</tbody></table></div>`:empty("No observed companion relationship","Configured helpers and unknown associations are not counted as observed companion usage.")}</section>`,
    orchestration:`<section class="card comparison-section"><div class="section-head"><h2>Orchestration activity</h2><span class="quiet">Recorded completed turns</span></div>${chart(Object.entries(lifetime.orchestration?.calls||{}).map(([label,value])=>({label:label.replaceAll("_"," "),value})),"Recognized native coordination calls")}<p>${metricValue(lifetime.orchestration?.observed_call_count)} recognized calls; ${metricValue(lifetime.orchestration?.unknown_turn_count)} turns without tool-count evidence.</p></section>`,
    history:`<div class="grid two comparison-section">${tokenHistory(trends,"Selected range · daily recorded usage")}<section class="card"><div class="section-head"><h2>Token categories</h2></div><div class="list"><div class="list-item"><div><strong>Input</strong><p>${esc(tokenBoundary("input_tokens"))}.</p></div>${tag(metricValue(tokenValue("input_tokens")))}</div><div class="list-item"><div><strong>Cached input</strong><p>Included within input; never added again.</p></div>${tag(metricValue(tokenValue("cached_input_tokens")))}</div><div class="list-item"><div><strong>Visible output</strong><p>Full output after subtracting exposed reasoning.</p></div>${tag(metricValue(visibleOutput))}</div><div class="list-item"><div><strong>Reasoning</strong><p>Exposed reasoning tokens; included in full output.</p></div>${tag(metricValue(tokenValue("reasoning_output_tokens")))}</div><div class="list-item"><div><strong>Full output</strong><p>${esc(tokenBoundary("output_tokens"))}; visible output plus exposed reasoning.</p></div>${tag(metricValue(tokenValue("output_tokens")))}</div></div></section></div>`,
    operations:workflowEfficiency+(entries.length?`<section class="card comparison-section"><div class="section-head"><h2>Other observed operations</h2>${status(data?.status||"Unavailable",data?.status==="available"?"good":"warn")}</div>${chart(entries,"Observed measurement events by level")}</section>`:""),
    diagnostics:(lifetime.diagnostics||[]).length?`<div class="callout comparison-section">${lifetime.diagnostics.map(esc).join("<br>")}</div>`:""
  };
  return metricsHeading(p,rangeMeta,coverage)+metricsCustomizePanel(p,layout)+renderMetricModules(p,modules,layout);
}
function timelinePrefs(p){const key=metricRangeKey(p);if(!state.timelinePrefs[key]){try{state.timelinePrefs[key]=JSON.parse(localStorage.getItem(`eutonos.timeline.${key}`))}catch(_error){}state.timelinePrefs[key]||={group:"auto",page:0};}return state.timelinePrefs[key]}
function timelineGroups(turns,mode){
  const dated=turns.filter(t=>metricDate(t.end||t.start));
  const span=dated.length?Math.max(...dated.map(t=>+new Date(t.end||t.start)))-Math.min(...dated.map(t=>+new Date(t.end||t.start))):0;
  const grouping=mode==="auto"?(span>2*86400000?"day":dated.length>30?"hour":"turn"):mode;
  const groups=new Map();
  for(const t of turns){const stamp=t.end||t.start,date=metricDate(stamp)?new Date(stamp):null;let key=t.id||`${t.thread_id}:${t.turn_id}`,label=date?when(stamp):"Time unavailable";
    if(date&&grouping!=="turn"){key=[date.getFullYear(),date.getMonth(),date.getDate(),...(grouping==="hour"?[date.getHours()]:[])].join("-");label=grouping==="day"?date.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric",year:"numeric"}):`${date.toLocaleDateString()} · ${date.getHours().toString().padStart(2,"0")}:00`;}
    if(!groups.has(key))groups.set(key,{id:key,label,time:date?+date:0,turns:[]});const group=groups.get(key);group.time=Math.max(group.time,date?+date:0);group.turns.push(t);
  }
  return {grouping,groups:[...groups.values()].sort((a,b)=>b.time-a.time)};
}
function timelineModels(turns){const models=new Map();for(const t of turns){const key=t.model||"Unknown model";if(!models.has(key))models.set(key,{model:key,roles:new Set(),turns:0,known:0,input:0,cached:0,output:0,reasoning:0});const m=models.get(key);m.turns++;m.roles.add(t.role||"unknown role");const u=t.usage;if(t.state!=="running"&&u&&["input_tokens","cached_input_tokens","output_tokens","reasoning_output_tokens"].every(k=>Number.isFinite(u[k])&&u[k]>=0)){m.known++;m.input+=u.input_tokens;m.cached+=u.cached_input_tokens;m.output+=u.output_tokens;m.reasoning+=u.reasoning_output_tokens;}}return [...models.values()];}
function timelineMemory(p,r,groupId){
  const record=(p.modules.ram?.records||[]).find(x=>x.path===r.path),version=record?.versions?.find(x=>x.revision===r.revision);
  return `<details data-persist="timeline-ram-${esc(groupId)}-${esc(r.receipt_id)}"><summary>${esc(r.stage||"Recorded")} · revision ${esc(r.revision)}</summary><p class="quiet">${version?"RAM summary at the linked revision":"Receipt-linked summary; RAM revision unavailable"}</p><p class="memory-summary">${esc(version?.summary||r.summary||"Summary unavailable")}</p><p class="quiet timeline-id">${esc(r.path)}</p><a href="#memory">Browse project memories</a></details>`;
}
function checkpointStats(records,turns){const checkpoints=(records||[]).flatMap(record=>(record.versions||[]).map(version=>({time:Date.parse(version.recorded_at||""),summary:version.summary||record.summary||"Checkpoint retained"}))).filter(item=>Number.isFinite(item.time)).sort((a,b)=>b.time-a.time),checkpoint=checkpoints[0];if(!checkpoint)return null;const selected=(turns||[]).filter(turn=>{const time=Date.parse(turn.end||turn.start||"");return Number.isFinite(time)&&time>checkpoint.time}),models=new Set(selected.map(turn=>turn.model).filter(Boolean));let input=0,output=0,known=0;for(const turn of selected){const usage=turn.usage;if(usage&&Number.isFinite(usage.input_tokens)&&Number.isFinite(usage.output_tokens)){known++;input+=usage.input_tokens;output+=usage.output_tokens}}return {checkpoint,turns:selected.length,models:models.size,running:selected.filter(turn=>turn.state==="running").length,known,input,output}}
function sinceCheckpoint(p,turns){const value=checkpointStats(p.modules.ram?.records||[],turns);if(!value)return `<section class="card checkpoint-summary"><div class="section-head"><h2>Since checkpoint</h2>${status("Not recorded")}</div><p>No dated retained RAM checkpoint is available, so later activity is not inferred.</p></section>`;const partial=value.known<value.turns;return `<section class="card checkpoint-summary"><div class="section-head"><div><h2>Since latest checkpoint</h2><p class="quiet">Latest retained RAM revision · ${esc(when(value.checkpoint.time))}</p></div>${tag(`${num(value.turns)} later turns`,value.turns?"good":"")}</div><div class="checkpoint-stats"><div><strong>${num(value.turns)}</strong><span>turns</span></div><div><strong>${num(value.models)}</strong><span>models</span></div><div><strong>${value.known?compactNum(value.input+value.output):"Unknown"}</strong><span>input + output${partial?" subtotal":""}</span></div><div><strong>${num(value.running)}</strong><span>running</span></div></div><details data-persist="checkpoint-summary"><summary>Checkpoint summary</summary><p>${esc(value.checkpoint.summary)}</p><p class="quiet">This is a time comparison against retained RAM evidence. It does not imply the later turns belong to the same task.</p></details></section>`}
function timelineToolbar(p,meta){return metricToolbar(p,meta).replace('class="metrics-range"','class="card metrics-range timeline-range"')}
function timeline(p){
  const range=metricRange(p),signature=metricRangeSignature(p,range),valid=state.metricProjection?.project===state.project&&state.metricProjection?.signature===signature;
  if(!valid&&!state.metricLoading)queueMicrotask(()=>loadMetricProjection(p,range));
  const ranged=valid?state.metricProjection.data:null,data=ranged?.timeline||ranged?.lifetime_metrics?.timeline,prefs=timelinePrefs(p);
  const top=heading("Timeline","See the models, tokens and memories behind this project’s activity.")+timelineToolbar(p,ranged?.range||{});
  if(!data)return top+empty(state.metricLoading?"Reading timeline…":"Timeline unavailable","Only registered project activity can appear here. Refresh to retry unavailable evidence.");
  const turns=data.turns||[],{grouping,groups}=timelineGroups(turns,prefs.group||"auto");const pageSize=10,pages=Math.max(1,Math.ceil(groups.length/pageSize));prefs.page=Math.max(0,Math.min(prefs.page||0,pages-1));const visible=groups.slice(prefs.page*pageSize,(prefs.page+1)*pageSize).reverse();
  const allModels=visible.map(g=>timelineModels(g.turns)),max=Math.max(1,...allModels.flat().filter(m=>m.known).map(m=>m.input+m.output));
  const value=(m,k)=>m.known?`<span class="exact-tooltip" tabindex="0" data-tooltip="${esc(exactValue(m[k]))}">${esc(compactNum(m[k]))}</span>${m.known<m.turns?" subtotal":""}`:"Unknown";
  const controls=sinceCheckpoint(p,turns)+`<section class="card timeline-controls"><div><label>Group by <select id="timeline-group" aria-label="Timeline grouping">${[["auto","Auto"],["turn","Agent turn"],["hour","Hour"],["day","Day"]].map(([v,l])=>`<option value="${v}" ${prefs.group===v?"selected":""}>${l}</option>`).join("")}</select></label><p class="quiet">Project chats count here even when work happens elsewhere. Observed project activity only. ${num(turns.length)} turns · ${num(groups.length)} ${grouping} cards. Local calendar time; token bars count completed turns by end time. Running cards show pending usage.</p></div><div class="timeline-pages"><button data-timeline-page="-1" ${prefs.page===0?"disabled":""}>Newer cards</button><span>${prefs.page+1} / ${pages}</span><button data-timeline-page="1" ${prefs.page===pages-1?"disabled":""}>Older cards</button></div></section>`;
  const cards=visible.map((g,i)=>{const models=allModels[i],ram=new Map();for(const t of g.turns)for(const r of t.ram||[])ram.set(`${r.path}:${r.revision}:${r.receipt_id}`,r);const calls=g.turns.reduce((sum,t)=>sum+Object.values(t.tool_counts||{}).filter(Number.isFinite).reduce((a,b)=>a+b,0),0),unknownTools=g.turns.filter(t=>!t.tool_counts).length;
    return `<article class="card timeline-card"><div class="section-head"><h2>${esc(g.label)}</h2>${tag(`${g.turns.length} ${g.turns.length===1?"turn":"turns"}`)}</div><div class="meta">${[...new Set(g.turns.map(t=>t.state||"unknown"))].map(x=>tag(x,x==="completed"?"good":"warn")).join("")}</div><div class="timeline-model-chart" aria-label="Input plus output by model; shared scale across visible cards">${models.map(m=>{const total=m.input+m.output,full=m.known?exactValue(total):"Unknown";return `<div class="timeline-model-column"><strong class="exact-tooltip" tabindex="0" data-tooltip="${esc(full)}" aria-label="${esc(full)} tokens">${m.known?esc(compactNum(total)):"?"}</strong><div class="timeline-bar-space"><div class="bar timeline-model-bar exact-tooltip model-${modelKey(m.model)}" data-value="${m.known?Math.max(2,Math.round(100*total/max)):0}" data-tooltip="${esc(full)} tokens" aria-label="${esc(shortModel(m.model))}: ${esc(full)} tokens" tabindex="0"></div></div>${modelLabel(m.model)}</div>`}).join("")}</div><p class="quiet">Input + output · cached and reasoning are included subsets.</p><div class="timeline-models">${models.map(m=>`<section><h3>${modelLabel(m.model)}</h3><p class="quiet">${esc([...m.roles].join(" / "))} · ${esc(m.model)}</p><dl><div><dt>Input</dt><dd>${value(m,"input")}</dd></div><div><dt>Cached input</dt><dd>${value(m,"cached")}</dd></div><div><dt>Output</dt><dd>${value(m,"output")}</dd></div><div><dt>Reasoning output</dt><dd>${value(m,"reasoning")}</dd></div></dl></section>`).join("")}</div><p>${num(calls)} observed tool calls${unknownTools?` · ${unknownTools} turns without tool counts`:""}</p><details data-persist="timeline-turns-${esc(g.id)}"><summary>Turn identities and timing</summary>${g.turns.map(t=>`<p class="quiet">${esc(t.role)} · ${modelLabel(t.model)}<br>${esc(when(t.start))} → ${esc(when(t.end))}<br><span class="timeline-id">${esc(t.id||`${t.thread_id}:${t.turn_id}`)}</span></p>`).join("")}</details><div class="timeline-memories"><h3>Memory</h3>${ram.size?[...ram.values()].map(r=>timelineMemory(p,r,g.id)).join(""):`<p class="quiet">No exact turn-linked RAM record. Capture may be off, absent or not linked; no summary has been invented.</p>`}</div></article>`;
  }).join("");
  return top+controls+(cards?`<p class="quiet">Older activity is on the left; the newest card is on the right. Scroll left to go back in time, or use Older cards. Ten cards are displayed per page; history is retained.</p><div class="timeline-scrollbar" tabindex="0" aria-label="Scroll timeline"><div></div></div><div class="timeline-rail" tabindex="0" aria-label="Timeline cards, oldest to newest">${cards}</div>`:empty("No recorded turns in this range","Choose a wider range or register the project's session sources."))+((data.diagnostics||[]).length?`<div class="callout">${data.diagnostics.map(esc).join("<br>")}</div>`:"");
}
content.addEventListener("change",event=>{if(event.target.id!=="timeline-group")return;const p=project(),prefs=timelinePrefs(p);prefs.group=event.target.value;prefs.page=0;state.timelineScroll=null;state.timelineFollowNewest=true;try{localStorage.setItem(`eutonos.timeline.${metricRangeKey(p)}`,JSON.stringify(prefs))}catch(_error){}render()});
content.addEventListener("click",event=>{const button=event.target.closest("[data-timeline-page]");if(!button)return;timelinePrefs(project()).page+=Number(button.dataset.timelinePage);state.timelineScroll=null;state.timelineFollowNewest=true;render()});
function settingOptions(values,current,labels={}){return values.map(value=>`<option value="${esc(value)}" ${value===current?"selected":""}>${esc(labels[value]||shortModel(value))}</option>`).join("")}
function participantProfileState(projectId=state.project){const profiles=state.participantProfiles||(state.participantProfiles={}),key=String(projectId);return profiles[key]||(profiles[key]={saved:null,draft:{given_name:"",family_name:""},loaded:false,attempted:false,loading:false,saving:false,dirty:false,needsReload:false,feedback:{type:"quiet",text:""}})}
// Routing area (owner redesign, 2026-10-14): harness switches, the "Who does what" table and presets.
// The table edits the role a job maps to (roles keep their other fields); jobs never share a role
// silently -- editing a row whose role is shared gives that job its own copy, worker-<job>.
const SETTINGS_JOBS=[["discovery","Discovery"],["coding","Coding"],["docs","Documentation"],["git","Git"],["review","Review"]];
const HARNESS_NAMES=["claude","codex","hermes"],HARNESS_LABELS={claude:"Claude",codex:"Codex",hermes:"Hermes"};
const HARNESS_DEFAULTS={claude:{enabled:true,max_workers:3},codex:{enabled:true,max_workers:4},hermes:{enabled:false,max_workers:1}};
// Preferences fill the table; each job is [primary, fallback] as [family choice, thinking].
const ROUTING_PRESETS={
  "cost-savings":{discovery:[["luna","xhigh"],["sonnet","medium"]],coding:[["luna","xhigh"],["sonnet","medium"]],docs:[["luna","xhigh"],["sonnet","medium"]],git:[["luna","xhigh"],["sonnet","medium"]],review:[["luna","xhigh"],["sonnet","high"]]},
  optimus:{discovery:[["luna","xhigh"],["sonnet","medium"]],coding:[["sol","high"],["sonnet","high"]],docs:[["sol","medium"],["sonnet","medium"]],git:[["luna","xhigh"],["sonnet","medium"]],review:[["sol","high"],["sonnet","high"]]},
  "high-quality":{discovery:[["astra","high"],["sol","high"]],coding:[["sol","high"],["opus","high"]],docs:[["astra","high"],["sol","high"]],git:[["luna","xhigh"],["sonnet","medium"]],review:[["sol","high"],["opus","high"]]}
};
function jobHarnessesCfg(){const base=state.settings?.harnesses||{},draft=state.settingsDrafts.harnesses||{},result={};for(const name of HARNESS_NAMES)result[name]={...HARNESS_DEFAULTS[name],...(base[name]||{}),...(draft[name]||{})};return result}
function jobCatalog(){return state.settings?.model_catalog?.models||{}}
function jobRulesView(){const saved=state.settings?.task_rules||{},draft=state.settingsDrafts.task_rules||{},rules={...saved,...draft};if(!rules.review)rules.review="reviewer";rules.closeout="orchestrator";return rules}
function autoModeSupported(name){
  const id=String(name||"").trim().toLowerCase().replace(/\[[^\]]*\]$/,"");
  if(id==="sonnet"||id==="opus"||id==="fable"||id==="haiku")return true;
  const m=/^claude-([a-z][a-z0-9]*)-(\d+(?:-\d+)*)$/.exec(id);
  if(!m)return false;
  const parts=m[2].split("-");
  if(/^\d{8}$/.test(parts[parts.length-1]))parts.pop();
  if(!parts.length)return false;
  const major=Number(parts[0]),minor=parts.length>1?Number(parts[1]):0;
  if(m[1]==="sonnet"||m[1]==="opus")return major>=4;
  if(m[1]==="fable")return true;
  if(m[1]==="haiku")return major>5||(major===5&&minor>=5);
  return false;
}
function autoModeRefused(name){const id=String(name||"").trim().toLowerCase();return !!id&&(id.startsWith("claude-")||id.includes("haiku"))&&!autoModeSupported(id)}
function jobWorkerModel(name){return !autoModeRefused(name)}
function jobModelOptions(current,allowNone){
  const catalog=jobCatalog(),cfg=jobHarnessesCfg();
  const live=Object.entries(catalog).filter(([name,e])=>e&&e.available!==false&&e.harness&&cfg[e.harness]?.enabled!==false&&jobWorkerModel(name));
  const families=live.filter(([,e])=>e.selection_kind==="family"),pins=live.filter(([,e])=>e.selection_kind!=="family").sort(([a,x],[b,y])=>x.harness.localeCompare(y.harness)||b.localeCompare(a));
  const known=live.some(([name])=>name===current);
  const missing=current&&!known?`<option value="${esc(current)}" selected disabled>${esc(catalog[current]?.resolved||current)} (${cfg[catalog[current]?.harness]?.enabled===false?"harness off":"unavailable"})</option>`:"";
  const none=allowNone?`<option value="" ${current?"":"selected"}>None</option>`:"";
  return none+missing+`<optgroup label="Latest">${families.map(([name,e])=>`<option value="${esc(name)}" ${name===current?"selected":""}>Latest ${esc(e.label||name)} (${esc(e.resolved||name)})</option>`).join("")}</optgroup><optgroup label="Exact versions">${pins.map(([name,e])=>`<option value="${esc(name)}" ${name===current?"selected":""}>${esc(HARNESS_LABELS[e.harness]||e.harness)} · ${esc(e.resolved||name)}</option>`).join("")}</optgroup>`;
}
function jobDefaultEffort(model,previous){
  const efforts=crossTalkEffortsForModel(state.settings?.model_catalog,model);
  if(previous&&efforts.includes(previous))return previous;
  const entry=jobCatalog()[model],wanted=entry?.family==="luna"?"xhigh":entry?.harness==="claude"?"medium":"high";
  return efforts.includes(wanted)?wanted:(efforts[0]||null);
}
function jobRow(job,label,rules,roles,value){
  const name=rules[job],role=roles[name],mainChat=name==="orchestrator",options=!mainChat&&role&&Array.isArray(role.options)?role.options:[];
  const primary=options[0]||{},fallback=options[1]||null,catalog=value?.model_catalog||{},efforts=primary.model?crossTalkEffortsForModel(catalog,primary.model):[];
  const shared=SETTINGS_JOBS.some(([other])=>other!==job&&rules[other]===name&&!mainChat);
  const modelCell=`<select data-job="${job}" data-job-field="model" aria-label="${esc(label)} model"><option value="__main__" ${mainChat?"selected":""}>Main chat (no worker)</option>${jobModelOptions(mainChat?"":(primary.model||""),false)}</select>`;
  const invalidEffort=primary.effort&&!efforts.includes(primary.effort)?`<option value="${esc(primary.effort)}" selected disabled>${esc(primary.effort)} (not available for this model)</option>`:"";
  const thinking=mainChat?`<span class="quiet">Main chat decides</span>`:`<select data-job="${job}" data-job-field="effort" aria-label="${esc(label)} thinking"><option value="" ${primary.effort?"":"selected"}>Auto</option>${invalidEffort}${settingOptions(efforts,primary.effort||"")}</select>`;
  const fallbackCell=mainChat?`<span class="quiet">None</span>`:`<select data-job="${job}" data-job-field="fallback" aria-label="${esc(label)} fallback">${jobModelOptions(fallback?.model||"",true)}</select>${fallback?.effort?` <span class="quiet">${esc(fallback.effort)}</span>`:""}`;
  const note=shared?`<span class="quiet job-note">Shares role ${esc(name)}; editing gives ${esc(label)} its own copy.</span>`:"";
  return `<tr data-job-row="${job}"><th scope="row">${esc(label)}${note}</th><td>${modelCell}</td><td>${thinking}</td><td>${fallbackCell}</td></tr>`;
}
function jobsTable(rules,roles,value){
  return `<div class="job-table-wrap"><table class="job-table"><thead><tr><th scope="col">Job</th><th scope="col">Model</th><th scope="col">Thinking</th><th scope="col">Fallback</th></tr></thead><tbody>${SETTINGS_JOBS.map(([job,label])=>jobRow(job,label,rules,roles,value)).join("")}<tr data-job-row="closeout"><th scope="row">Closeout</th><td colspan="3"><span>Main chat</span> <span class="quiet">Always done by the chat you are talking to; not configurable.</span></td></tr></tbody></table></div>`;
}
function harnessesCard(cfg){
  return `<section class="card" data-harness-card><h2>Harnesses</h2><p class="quiet">Choose which apps may take worker jobs and how many workers each can run at once (1 to 7). Turning one off removes its models from the table choices.</p><div class="harness-list">${HARNESS_NAMES.map(name=>`<div class="harness-row"><label class="check"><input type="checkbox" data-harness-enabled="${name}" ${cfg[name].enabled?"checked":""}> ${esc(HARNESS_LABELS[name])}${name==="hermes"?" (not wired yet)":""}</label><label class="harness-limit">Workers at once<input type="number" min="1" max="7" step="1" data-harness-limit="${name}" value="${cfg[name].max_workers}" ${cfg[name].enabled?"":"disabled"}></label></div>`).join("")}</div><p class="quiet">The host may allow fewer. Every worker still needs a suitable independent job.</p></section>`;
}
function routingAllowedModels(fixedModel){
  const rules=jobRulesView(),roles=crossTalkRolesDict(state.settings||{}),cfg=jobHarnessesCfg(),models=new Set();
  for(const [job] of SETTINGS_JOBS){const role=rules[job]!=="orchestrator"?roles[rules[job]]:null;for(const option of (role&&role.options)||[])if(option.model&&option.harness!=="hermes"&&cfg[option.harness]?.enabled!==false)models.add(option.model)}
  if(fixedModel)models.add(fixedModel);
  if(models.size)return [...models];
  const saved=state.settings?.routing?.allowed_models;return Array.isArray(saved)&&saved.length?saved:Object.keys(jobCatalog()).slice(0,1);
}
function jobEnsureOwnRole(job){
  const rules={...jobRulesView()},roles=crossTalkRolesFromDom(),name=rules[job];
  const shared=name==="orchestrator"||!roles[name]||SETTINGS_JOBS.some(([other])=>other!==job&&rules[other]===name);
  if(!shared)return name;
  let own=`worker-${job}`,n=2;while(own in roles)own=`worker-${job}-${n++}`;
  roles[own]=name!=="orchestrator"&&roles[name]?JSON.parse(JSON.stringify(roles[name])):{options:[crossTalkRoleDefaultOption(state.settings?.model_catalog)],preamble:"",autonomy:"ask",description:"",edits:"ask",brief_template:""};
  rules[job]=own;state.settingsDrafts.task_rules=rules;return own;
}
// Roles first, then rules, then routing: a rule must never reach the server before the role it names.
function jobSaveAll(){
  // Read every value first: the first submit re-captures drafts from the saver and would drop unsaved ones.
  const roles=crossTalkRolesFromDom(),rules=jobRulesView();state.settingsDrafts.task_rules=rules;
  const routing=settingValue("routing"),saver=settingsSavers.get(state.project);
  if(saver)for(const name of ["roles","task_rules","routing"])saver.pending.delete(name);
  submitSetting("roles",roles);submitSetting("task_rules",rules);submitSetting("routing",routing);render();
}
function jobEdit(job,field,value){
  const catalog=jobCatalog();
  if(field==="model"&&value==="__main__"){state.settingsDrafts.task_rules={...jobRulesView(),[job]:"orchestrator"};jobSaveAll();return}
  const name=jobEnsureOwnRole(job),role=crossTalkRolesFromDom()[name];if(!role)return;
  const options=role.options=Array.isArray(role.options)?role.options:[];
  if(field==="model"){const entry=catalog[value];if(!entry||!jobWorkerModel(value))return;options[0]={harness:entry.harness,model:value,effort:jobDefaultEffort(value,options[0]?.effort)}}
  else if(field==="effort"){if(!options[0])return;options[0].effort=value||null}
  else if(field==="fallback"){
    if(!value){if(options.length>1)options.splice(1,1)}
    else{const entry=catalog[value];if(!entry||!jobWorkerModel(value))return;const option={harness:entry.harness,model:value,effort:jobDefaultEffort(value,options[1]?.effort)};if(options.length>1)options[1]=option;else options.push(option)}
  }
  jobSaveAll();
}
function applyRoutingPreset(index){
  const preset=ROUTING_PRESETS[["cost-savings","optimus","high-quality"][index]];if(!preset)return;
  const catalog=jobCatalog(),cfg=jobHarnessesCfg(),rules={...jobRulesView()};state.settingsDrafts.task_rules=rules;
  for(const [job] of SETTINGS_JOBS){
    const options=preset[job].map(([model,effort])=>{const entry=catalog[model];if(!entry||entry.available===false||cfg[entry.harness]?.enabled===false||!jobWorkerModel(model))return null;return {harness:entry.harness,model,effort:jobDefaultEffort(model,effort)}}).filter(Boolean);
    if(!options.length)continue;
    const name=jobEnsureOwnRole(job),role=crossTalkRolesFromDom()[name];role.options=options;
  }
  jobSaveAll();
}
function installDoctorSummary(value){
  const doctor=value?.install_doctor,readiness=doctor?.readiness;if(!doctor)return empty("Install status unavailable","Reload settings to read the current install report.");
  const state=readiness?.state||"unknown",items=readiness?.blockers||[],next=doctor.next_action||"No next action recorded.";
  const patchLine=doctor.active_record?.local_patch_line,patchRow=patchLine?`<div class="list-item"><div><strong>Local patch</strong><p>${esc(patchLine)}</p></div>${tag("patched","warn")}</div>`:"";
  return patchRow+`<div class="list-item"><div><strong>Doctor readiness</strong><p>${esc(items.length?`${items.length} required item(s) need attention.`:"Required selected capabilities are ready.")} ${esc(next)}</p></div>${tag(state,state==="ready"?"good":"warn")}</div>`;
}
function installConsentSettings(value){
  const consent=value?.install_doctor?.consent;if(!consent)return `<section class="settings-section" data-settings-section="install_consent">${heading("Install consent","Review the current consent text and recorded receipt.")}${empty("Consent status unavailable","Reload settings to read the install record.")}</section>`;
  const receipt=consent.receipt,receiptText=receipt?`${receipt.surface||"unknown surface"} · ${receipt.accepted_at||"time unavailable"} · ${receipt.text_id||consent.text_id} · SHA-256 ${receipt.text_sha256||"unavailable"} · choices ${JSON.stringify(receipt.choices||{})}`:"No consent receipt is recorded in the active install record.";
  return `<section class="settings-section" data-settings-section="install_consent">${heading("Install consent","Review the same consent text used by the terminal installer.")}<section class="card"><div class="section-head"><h2>Consent wording</h2>${tag(consent.state,consent.state==="ok"?"good":"warn")}</div><p class="quiet">Text ID: ${esc(consent.text_id||"unknown")} · SHA-256: ${esc(consent.text_sha256||"unknown")}</p><div class="doctor-consent-text">${esc(consent.text||"Consent text unavailable.")}</div><div class="list">${installDoctorSummary(value)}<div class="list-item"><div><strong>Recorded receipt</strong><p>${esc(receiptText)}</p></div>${tag(receipt?"Recorded":"Not recorded",receipt?"good":"warn")}</div></div></section></section>`;
}
function permissionsSettings(value){
  const panel=value?.install_doctor?.declared?.permissions,items=panel?.items;if(!Array.isArray(items))return `<section class="settings-section" data-settings-section="permissions">${heading("Permissions","See declared permission items and their current readbacks.")}${empty("Permissions unavailable","Reload settings to read the permission plan.")}</section>`;
  return `<section class="settings-section" data-settings-section="permissions">${heading("Permissions","Declared settings and readbacks. This view does not change app permissions.")}<section class="card"><div class="section-head"><h2>Permission status</h2>${tag(value.install_doctor.readiness?.state||"unknown",value.install_doctor.readiness?.ready?"good":"warn")}</div>${installDoctorSummary(value)}<div class="list">${items.map(item=>`<div class="list-item"><div><strong>${esc(item.feature||item.id)}</strong><p>${esc(item.id)} · ${esc(item.app||"app")} · ${esc(item.scope||"scope")} · ${item.required?"required":"optional"}${item.selected?" · selected":" · not selected"}</p><p>${esc(item.reason||item.why||"No additional detail.")}</p></div>${tag(item.state||"unknown",item.state==="ok"?"good":item.state==="held"||item.state==="skipped"?"":"warn")}</div>`).join("")}</div></section></section>`;
}
function settings(p){
  const value=state.settings;
  const participantProfile=participantProfileState();
  if(!participantProfile.attempted&&!participantProfile.loading)queueMicrotask(()=>loadParticipantProfile());
  const profilePanel=participantProfileSettings(participantProfile);
  if(state.settingsLoading)return heading("Settings","Reading saved preferences.")+profilePanel+empty("Loading settings","Your current settings are being read.");
  if(state.settingsProject!==state.project&&!state.settingsLoading)queueMicrotask(()=>loadSettings());
  if(!value)return heading("Settings","Local instruction-driven preferences for the next task.")+profilePanel+empty(state.settingsLoading?"Loading settings":"Settings unavailable",state.settingsLoading?"Reading the current repository configuration.":"Use Reload settings to read the selected repository.")+`<p><button type="button" data-settings-reload>Reload settings</button></p>`;
  const baseRouting=value.routing||{},baseRam=value.ram||{},baseTracking=value.tracking||{},models=(Array.isArray(value.models)?value.models:[]).filter(name=>value.model_catalog?.models?.[name]?.available!==false),efforts=Array.isArray(value.efforts)?value.efforts:[];
  const rolesDict=crossTalkRolesDict(value),roleNames=Object.keys(rolesDict);
  const catalogModels=(value.model_catalog&&value.model_catalog.models)||{},effortsFor=model=>{const entry=catalogModels[model],allowed=entry&&(entry.efforts||entry.supported_efforts);return Array.isArray(allowed)?allowed:efforts;};
  const routingModelLabels=Object.fromEntries(models.map(model=>[model,crossTalkModelChoiceLabel(model,value.model_catalog)]));
  const harnessesCfg=jobHarnessesCfg(),routing={...baseRouting,...(state.settingsDrafts.routing||{})},rules=jobRulesView(),ram={...baseRam,...(state.settingsDrafts.ram||{})},tracking={...baseTracking,...(state.settingsDrafts.tracking||{})};
  const fixed=routing.routing==="fixed",preferences=["cost-savings","optimus","high-quality"],preference=Math.max(0,preferences.indexOf(routing.routing_profile||"optimus")),preferenceLabels=["Cost savings","Optimus","High quality"];
  const fixedModels=models;
  const invalidFixedModel=routing.model&&!fixedModels.includes(routing.model)?`<option value="${esc(routing.model)}" selected disabled>${esc(crossTalkModelChoiceLabel(routing.model,value.model_catalog))} (${catalogModels[routing.model]?.available===false?"unavailable":"no longer available"})</option>`:"";
  const modelOptions=`<option value="">Choose model</option>${invalidFixedModel}${settingOptions(fixedModels,routing.model||"",routingModelLabels)}`;
  const currentEffort=routing.effort||"",eligibleEfforts=(routing.routing==="fixed"&&routing.model?effortsFor(routing.model):efforts);
  const invalidGlobalEffort=currentEffort&&!eligibleEfforts.includes(currentEffort)?`<option value="${esc(currentEffort)}" selected disabled>${esc(currentEffort)} (not available for this model)</option>`:"";
  const effortOptions=`<option value="" ${currentEffort===""?"selected":""}>Auto (let primary decide)</option>${invalidGlobalEffort}${settingOptions(eligibleEfforts,currentEffort)}`;
  const preferenceScale=preferenceLabels.map((label,index)=>`<span class="${preference===index?"is-selected":""}" data-routing-preference-option="${index}">${esc(label)}</span>`).join("");
  const restore=section=>`<button type="button" class="secondary" data-settings-restore="${section}">Restore defaults</button>`;
  return heading("Settings","Choose how EUTONOS assigns work, uses RAM and tracks usage.",tag("Repository settings"))+`<p class="settings-boundary">Changes save automatically for this repository and its linked worktrees. Agents read saved preferences at the next task; task overrides take precedence.</p>`+profilePanel+
`<section class="settings-section" data-settings-section="routing">${settingsFeedback("routing")}<section class="card"><div class="section-head"><div><h2>Smart routing</h2><p class="quiet">Let EUTONOS assess each request and assign suitable bounded work to worker models.</p></div>${restore("routing")}</div><label class="check"><input type="checkbox" data-setting="routing-enabled" ${routing.enabled?"checked":""}> Enable Smart routing</label><p class="quiet">When off, the turn-start routing hook sends no routing prompt.</p><p class="settings-precedence"><strong>Which control wins:</strong> the Who does what table is the final word for each job. A Routing preference only fills that table (your later edits to a row stay until you pick a preference again). A harness you turn off is never used, whatever the table says. Closeout always stays with the main chat.</p></section><div class="grid two settings-modules"><section class="card settings-slider"><h2>Routing preference</h2><p class="quiet">Choose the balance between model cost and depth of judgment. Picking one fills the Who does what table.</p><label class="sr-only" for="routing-preference">Routing profile</label><input id="routing-preference" type="range" min="0" max="2" step="1" value="${preference}" data-setting="routing-preference" aria-valuetext="${esc(preferenceLabels[preference])}"><div class="settings-scale settings-preference-scale">${preferenceScale}</div><p class="quiet">Cost savings gives discovery, coding, documentation, Git and review to Luna (Sonnet as fallback), and rechecks useful worker output at stable checkpoints. Optimus uses Sol where broader judgment helps (coding, documentation, review) and Luna for discovery and Git. High quality prefers Astra or Sol for discovery and documentation and Sol for coding and review, while Git stays on lower-cost models.</p></section>${harnessesCard(harnessesCfg)}</div><section class="card"><details class="settings-advanced" data-persist="settings-routing-advanced"><summary>Advanced routing controls</summary><p class="quiet">The models the table uses are the models routing may pick; there is no separate eligible list.</p><div class="settings-fields"><label>Mode<select data-setting="routing-mode"><option value="profile" ${routing.routing==="profile"?"selected":""}>Use routing profile</option><option value="adaptive" ${routing.routing==="adaptive"?"selected":""}>Manual adaptive</option><option value="fixed" ${fixed?"selected":""}>Fixed model</option></select></label><div class="settings-fixed ${fixed?"":"is-disabled"}"><label>Fixed model<select data-setting="routing-model" ${fixed?"":"disabled"}>${modelOptions}</select></label></div><label>Global thinking<select data-setting="routing-effort">${effortOptions}</select></label></div></details></section></section>`+
    `<section class="card settings-section" data-settings-section="task_rules"><div class="section-head"><div><h2>Who does what</h2><p class="quiet">Pick the model, thinking level and fallback for each job. Models come from the harnesses you turned on, as the latest of a family or an exact version. Models that cannot run auto mode (Haiku 4.5 and older) are never offered for workers.</p></div>${restore("task_rules")}</div>${jobsTable(rules,rolesDict,value)}${settingsFeedback("task_rules")}</section>`+
    `<section class="card settings-section" data-settings-section="ram"><div class="section-head"><div><h2>RAM</h2><p class="quiet">Choose when to retrieve memories and when to save completed work.</p></div>${restore("ram")}</div><div class="settings-fields"><label>Retrieval<select data-setting="ram-retrieval"><option value="on-demand" ${ram.retrieval==="on-demand"?"selected":""}>On demand</option><option value="off" ${ram.retrieval==="off"?"selected":""}>Off</option></select></label><label class="check"><input type="checkbox" data-setting="ram-capture" ${ram.capture?"checked":""}> Enable memory capture</label><label>Capture timing<select data-setting="ram-cadence">${[["every-turn","Every turn"],["work-sized","Match the work"],["checkpoint","Checkpoints and milestones"],["milestone","Milestones only"]].map(([id,label])=>`<option value="${id}" ${(ram.cadence||"every-turn")===id?"selected":""}>${label}</option>`).join("")}</select></label><label>Summary detail<select data-setting="ram-detail">${[["brief","Brief"],["summary","Summary"],["detailed","Detailed"]].map(([id,label])=>`<option value="${id}" ${(ram.detail||"summary")===id?"selected":""}>${label}</option>`).join("")}</select></label></div><p class="quiet">Match the work groups quick edits and campaign updates until a checkpoint. Deferred notes stay local for pickup. Risks and decisions are retained at every detail level.</p><p class="quiet">Turning capture off preserves saved memories and skips optional capture work.</p>${settingsFeedback("ram")}</section>`+
    `<section class="card settings-section" data-settings-section="tracking"><div class="section-head"><div><h2>Metrics</h2><p class="quiet">Retain local metadata for explicitly registered usage observations.</p></div>${restore("tracking")}</div><div class="settings-fields"><label class="check"><input type="checkbox" data-setting="tracking-enabled" ${tracking.enabled?"checked":""}> Keep local usage tracking on</label></div><p class="quiet">Turning tracking off preserves previous observations. Coverage depends on registered usage sources.</p>${settingsFeedback("tracking")}</section>`+
    sharedWorkSettings(value)+gitDeliverySettings(value)+crossTalkSettingsSections(p,value,restore)+installConsentSettings(value)+permissionsSettings(value);
}
function participantProfileSettings(profile=participantProfileState()){
  const saved=profile.saved?.participant||{},draft=profile.draft||{},feedback=profile.feedback||{type:"quiet",text:""};
  return `<section class="card participant-profile"><div class="section-head"><div><h2>Participant profile</h2><p class="quiet">Save your first and last name once for recorded work attribution in this checkout and its linked worktrees.</p></div></div><div class="settings-fields"><label>First name<input type="text" autocomplete="given-name" data-participant-profile-field="given_name" value="${esc(draft.given_name||"")}" ${profile.loading||profile.saving?"disabled":""} required></label><label>Last name<input type="text" autocomplete="family-name" data-participant-profile-field="family_name" value="${esc(draft.family_name||"")}" ${profile.loading||profile.saving?"disabled":""} required></label><label>Stable participant ID<input type="text" value="${esc(saved.id||"")}" placeholder="Assigned when you save" readonly aria-readonly="true"></label></div><p class="quiet">The stable ID is assigned on first save and stays the same when you change your name. Separate clones share this ID only when you explicitly reuse it. This profile works while Shared Work is off and Git delivery is manual.</p><div class="settings-feedback ${esc(feedback.type)}" role="status"><span>${esc(feedback.text||(!profile.attempted||profile.loading?"Reading saved profile…":""))}</span></div><div class="settings-actions"><button type="button" data-participant-profile-save ${!profile.loaded||profile.loading||profile.saving||profile.needsReload?"disabled":""}>${profile.saving?"Saving…":"Save profile"}</button><button type="button" class="secondary" data-participant-profile-reload ${profile.loading||profile.saving?"disabled":""}>Reload saved profile</button></div>${saved.full_name?`<p class="quiet">Saved as ${esc(saved.full_name)}.</p>`:""}</section>`;
}
async function loadParticipantProfile(force=false){
  const projectId=state.project,profile=participantProfileState(projectId);if(profile.loading||profile.saving||(!force&&profile.attempted))return;
  profile.attempted=true;profile.loading=true;profile.feedback={type:"quiet",text:force?"Reloading saved profile…":"Loading saved profile…"};
  try{
    const response=await fetch(`/api/participant-profile?project=${encodeURIComponent(projectId)}`,{cache:"no-store",headers:{"X-Eutonos-Settings":state.snapshot?.settings_token||""}}),data=await response.json();
    if(!response.ok)throw new Error(data.message||`Participant profile returned ${response.status}`);
    profile.saved=data;profile.loaded=true;
    if(!profile.dirty)profile.draft={given_name:data.participant?.given_name||"",family_name:data.participant?.family_name||""};
    profile.needsReload=false;
    profile.feedback=data.configured?{type:"success",text:profile.dirty?"Saved profile reloaded; your unsaved edits remain.":"Saved profile loaded."}:{type:"quiet",text:data.setup?.instruction||"No participant profile is saved yet. Enter your first and last name once."};
  }catch(error){profile.feedback={type:"error",text:`Could not load profile: ${error.message}. Your unsaved edits are kept.`}}
  finally{profile.loading=false;if(state.project===projectId&&state.view==="settings")render()}
}
async function saveParticipantProfile(){
  const projectId=state.project,profile=participantProfileState(projectId);if(profile.saving||profile.loading||profile.needsReload||!profile.loaded)return;
  const given_name=String(profile.draft.given_name||"").trim(),family_name=String(profile.draft.family_name||"").trim();
  if(!given_name||!family_name){profile.feedback={type:"error",text:"Enter both your first and last name."};if(state.view==="settings")render();return}
  profile.saving=true;profile.feedback={type:"quiet",text:"Saving participant profile…"};if(state.view==="settings")render();
  try{
    const response=await fetch("/api/participant-profile",{method:"POST",headers:{"Content-Type":"application/json","X-Eutonos-Settings":state.snapshot?.settings_token||""},body:JSON.stringify({project:projectId,given_name,family_name,expected_revision:profile.saved?.revision??null})});
    const data=await response.json();
    if(!response.ok){const error=new Error(data.message||(response.status===409?"The saved profile changed elsewhere.":`Participant profile returned ${response.status}`));error.status=response.status;throw error}
    const saved=data.profile&&typeof data.profile==="object"?data.profile:data;
    profile.saved=saved;profile.loaded=true;profile.draft={given_name:saved.participant?.given_name||given_name,family_name:saved.participant?.family_name||family_name};profile.dirty=false;profile.needsReload=false;
    profile.feedback={type:"success",text:`Profile saved as ${[profile.draft.given_name,profile.draft.family_name].join(" ")}.`};
  }catch(error){profile.needsReload=error.status===409;profile.feedback={type:error.status===409?"conflict":"error",text:error.status===409?"The saved profile changed elsewhere. Reload it before saving again.":`Could not save profile: ${error.message}. Your edits are kept.`}}
  finally{profile.saving=false;if(state.project===projectId&&state.view==="settings")render()}
}
content.addEventListener("input",event=>{
  const input=event.target.closest("[data-participant-profile-field]");if(!input)return;
  const profile=participantProfileState();profile.draft={...profile.draft,[input.dataset.participantProfileField]:input.value};profile.dirty=true;profile.feedback={type:"quiet",text:"Unsaved profile changes."};
});
content.addEventListener("click",event=>{
  if(event.target.closest("[data-participant-profile-save]"))saveParticipantProfile();
  else if(event.target.closest("[data-participant-profile-reload]"))loadParticipantProfile(true);
});
function sharedWorkSettings(value){
  const saved=value.shared_work||{},config={...saved,...state.settingsDrafts.shared_work,...state.settingsDrafts.shared_auto_fetch},canonical=config.canonical||{},peers=Array.isArray(config.peers)?config.peers:[];
  const field=(key,label,record=canonical)=>`<label>${label}<input type="text" data-shared-work-field="${key}" value="${esc(record[key]||"")}"></label>`;
  const peerRows=peers.map((peer,index)=>`<div class="shared-work-peer"><div><strong>Peer source ${index+1}</strong><button type="button" class="secondary" data-shared-work-peer-remove="${index}">Remove</button></div><input type="hidden" data-shared-work-peer-id value="${esc(peer.id||"")}"><div class="settings-fields">${field("url","Repository URL",peer).replace('data-shared-work-field="url"',`data-shared-work-peer-url="${index}"`)}${field("ref","Branch ref (refs/heads/...)",peer).replace('data-shared-work-field="ref"',`data-shared-work-peer-ref="${index}"`)}</div></div>`).join("");
  return `<section class="card settings-section" data-settings-section="shared_work"><h2>Shared work settings</h2><label class="check"><input type="checkbox" data-shared-work-field="auto_fetch" ${(config.auto_fetch??true)?"checked":""}> Automatically fetch configured sources</label><p class="quiet">Recommended for shared repositories. Applies only to this checkout. When sharing is disabled, fetching stays inactive. Turning auto-fetch off keeps cached shared work visible; current remote coverage becomes unknown.</p><button type="button" class="secondary" data-shared-auto-save>Save auto-fetch preference</button>${settingsFeedback("shared_auto_fetch")}${settingsFeedback("shared_work")}
  <details class="shared-source-settings" data-persist="shared-source-settings"><summary>Configure sharing and sources</summary><label class="check"><input type="checkbox" data-shared-work-field="enabled" ${config.enabled?"checked":""}> Enable shared work on this checkout</label><label>Local participant name<input type="text" data-shared-work-field="participant" value="${esc(config.participant||"")}"></label><p class="quiet">This switch and participant name apply only here. Choose sharing explicitly. Turning sharing off hides peer rows from Current Work.</p><h3>Canonical repository</h3><div class="settings-fields">${field("url","Repository URL")}${field("ref","Accepted branch ref (refs/heads/...)")}</div><div class="section-head"><div><h3>Peer sources</h3><p class="quiet">Add the exact repository URL and branch ref for each source to observe.</p></div><button type="button" class="secondary" data-shared-work-peer-add>Add peer</button></div>${peerRows||'<p class="quiet">No peer sources configured. Other workers remain unknown.</p>'}<p>Only published work on configured branches can be observed. Unconfigured repositories and unpushed work remain unknown.</p><details><summary>Advanced shared-work settings and identity</summary><div class="settings-fields">${field("attempt_owner","Attempt owner file",config)}<label>Fetch interval in seconds<input type="number" min="5" max="300" data-shared-work-field="fetch_interval_seconds" value="${esc(config.fetch_interval_seconds??30)}"></label></div><dl>${[["Repository identity",config.repository_id],["Scope identity",config.scope_id],["Namespace",config.namespace],["Profile",config.profile||"eutonos.shared-work.config.v1"]].map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value||"Unavailable")}</dd></div>`).join("")}</dl><p>Native identity values are read-only. Peer IDs are generated from their URL and ref.</p></details><button type="button" data-settings-save="shared_work">Save shared-work settings</button></details>${value.shared_work_qualification?`<p class="quiet">${esc(value.shared_work_qualification)}</p>`:""}<details><summary>Latest saved result</summary><p>Native save: ${esc(sharedWorkValue(value.shared_work_native_save))} · Git delivery: ${esc(sharedWorkValue(value.shared_work_git_delivery))}</p></details></section>`;
}
function sharedRepositoryObservation(config,observation={}){
  const observed=[observation.canonical,...(observation.peers||[])].filter(Boolean),sources=[...(config.canonical?[{id:"canonical",...config.canonical}]:[]),...(config.peers||[])];
  const rows=sources.map(source=>{const row=observed.find(row=>row.id===source.id&&row.url===source.url&&row.ref===source.ref)||{},label=source.id==="canonical"?"Canonical repository":`Peer · ${source.id}`,status=!config.enabled?"Hidden while sharing is disabled":row.commit?"Cached source pin":"Not observed";return `<article class="shared-work-source"><h3>${esc(label)}</h3><p>${esc(source.url)}<br><code>${esc(source.ref)}</code></p><p>${esc(status)} · commit <code>${esc(row.commit||"unknown")}</code></p><p>Last successful fetch: ${esc(row.last_successful_fetch?when(row.last_successful_fetch):"unknown")}</p></article>`}).join("");
  const fetchState=!config.enabled?"Inactive · sharing disabled":config.auto_fetch===false?"Off · cached sources retained":({fresh:"Fetched",partial:"Fetch incomplete",cached:"Using cached observation",coalesced:"Observation in progress",unavailable:"Unavailable"})[observation.fetch_state]||"Not observed";
  return `<section class="card"><div class="section-head"><h2>Sources and freshness</h2>${tag(config.enabled?"Shared Work enabled":"Shared Work disabled",config.enabled?"good":"")}</div><dl class="shared-repository-facts"><div><dt>Auto-fetch preference</dt><dd>${config.auto_fetch===false?"Off":"On"}</dd></div><div><dt>Current fetch state</dt><dd>${esc(fetchState)}</dd></div><div><dt>Last source observation</dt><dd>${esc(observation.last_observed_at?when(observation.last_observed_at):"unknown")}</dd></div><div><dt>Source freshness</dt><dd>${esc(observation.freshness?.kind||"unknown")}</dd></div><div><dt>Configured source coverage</dt><dd>${esc(observation.coverage||"unknown")}</dd></div></dl><p class="quiet">Coverage applies only to configured sources. Unconfigured peers and unpushed work remain unknown.${!config.peers?.length?" No peer sources are configured; other workers remain unknown.":""}</p>${(observation.diagnostics||[]).slice(0,8).map(d=>`<p class="quiet">${esc(d)}</p>`).join("")}<div class="shared-repository-sources">${rows||'<p>No sources configured.</p>'}</div></section>`;
}
function sharedRepositories(p){
  const roster=state.snapshot?.shared_repositories||[],selected=roster.find(row=>row.project===state.project),value=state.settingsProject===state.project?state.settings:null;
  if(state.settingsProject!==state.project&&!state.settingsLoading)queueMicrotask(()=>loadSettings());
  const rows=roster.map(row=>{const config=row.configuration||(row.project===state.project?value?.shared_work:null),current=row.project===state.project?p.modules?.current_work?.shared_work:null,observation=current&&(!config?.revision||current.revision===config.revision)?current:row.observation,available=row.status==="available",sharing=available?(config?.enabled?"Enabled":"Disabled"):"Unknown",preference=available?(config?.auto_fetch===false?"Off":"On"):"Unknown",fetch=!available?"Unknown":!config?.enabled?"Inactive":config.auto_fetch===false?"Off":observation?.fetch_state||"Unknown";return `<tr ${row.project===state.project?'class="is-selected"':""}><td><strong>${esc(row.name)}</strong><small>${esc(row.root)}</small></td><td>${esc(sharing)}</td><td>${esc(preference)}<small>${esc(fetch)}</small></td><td>${esc(observation?.freshness?.kind||"unknown")}<small>${esc(observation?.coverage||"unknown")} coverage</small></td><td><button type="button" class="secondary" data-shared-project="${row.project}" ${row.project===state.project?"disabled":""}>${row.project===state.project?"Selected":"Open"}</button></td></tr>`}).join("");
  const config=selected?.configuration||value?.shared_work,current=p.modules?.current_work?.shared_work,observation=current&&(!config?.revision||current.revision===config.revision)?current:selected?.observation||{},staleForm=value&&config?.revision&&settingsConfigRevision(value)!==config.revision;
  return heading("Shared repositories","Repositories selected for this dashboard, their shared sources and local fetch preferences.")+`<section class="card shared-repositories-roster"><h2>Registered repositories</h2><p class="quiet">This list uses the dashboard's selected roots. It is not a complete list of other developers' repositories.</p><div class="table-scroll"><table><thead><tr><th>Repository</th><th>Shared Work</th><th>Auto-fetch</th><th>Freshness</th><th>Repository controls</th></tr></thead><tbody>${rows}</tbody></table></div></section><h2 class="shared-selected-heading">${esc(p.name)} · selected repository</h2>`+(config&&config.status!=="unavailable"?sharedRepositoryObservation(config,observation):empty("Sharing status unavailable",value?.shared_work?.error||selected?.reason||"Read the saved settings for this repository."))+(state.settingsLoading?empty("Loading settings","Reading the selected repository’s saved preferences."):value?(staleForm?`<p class="status warn">Saved settings changed elsewhere. Reload saved settings to replace your form and any unsaved edits.</p><p><button type="button" data-settings-reload>Reload saved settings</button></p>`:" ")+sharedWorkSettings(value):`<p><button type="button" data-settings-reload>Reload saved settings</button></p>`);
}
function gitDeliverySettings(value){
  const saved=value.delivery||{mode:"manual",revision:0},config=state.settingsDrafts.delivery||saved;
  const field=(key,label)=>`<label>${label}<input type="text" data-delivery-field="${key}" value="${esc(config[key]||"")}"></label>`;
  return `<section class="card settings-section" data-settings-section="delivery"><h2>Git delivery</h2><p>Choose who saves and publishes checked changes. Board and eligible RAM saves use the same publisher.</p><label>Mode<select data-delivery-field="mode">${settingOptions(["manual","commit-only","commit+push"],config.mode||"manual",{manual:"Manual","commit-only":"Commit only","commit+push":"Commit and push"})}</select></label><div class="settings-fields">${field("participant","Participant")}${field("remote_url","Destination URL or absolute local path")}${field("branch","Source branch (refs/heads/…)")}${field("remote_ref","Destination branch (refs/heads/…)")}${field("canonical_base","Accepted baseline commit")}</div><details><summary>Advanced delivery binding</summary><div class="settings-fields">${field("project_id","Stable project identity")}${field("records_ref","Records destination branch (refs/heads/…)")}</div><label>Allowed paths (one exact path or directory prefix per line)<textarea rows="5" data-delivery-field="allowed_paths">${esc((config.allowed_paths||[]).join("\n"))}</textarea></label><label>Existing check commands (JSON arrays of arguments)<textarea rows="5" data-delivery-field="checks">${esc(JSON.stringify(config.checks||[],null,2))}</textarea></label></details><p>Manual keeps the ordinary agent workflow. Commit only saves selected changes locally. Commit and push also publishes them to the explicit destination. Save applies this checkout's binding; enabling does not publish earlier native saves.</p><button type="button" data-settings-save="delivery">Save delivery settings</button><details><summary>Inspect binding and requests</summary><p>Checkout: ${esc(saved.root||"This selected checkout")} · Revision ${esc(value.delivery_revision??"unavailable")}</p>${saved.error?`<p>${esc(saved.error)}</p>`:""}<p><code>eutonos-settings delivery status</code> (in a EUTONOS source checkout: <code>python tools/settings.py delivery status</code>) shows requests; <code>delivery recover REQUEST_ID</code> recovers their original identity. Paused requests need explicit resume.</p></details>${settingsFeedback("delivery")}</section>`;
}
// Cross-app talk: the Crosstalk owner card plus named roles, each an ordered fallback chain of up to 4
// harness/model/effort options. Two sections ("crosstalk","roles") share one nav entry (settingsSectionsForNav).
// Neither has a settings.py "defaults" value, so their <section> head omits the Restore-defaults button.
//  - roles is a DICT of NAMED roles {name:{options:[{harness,model,effort}], preamble,
//    autonomy}} (settings.validate_roles/_validate_role/_validate_role_option).
//  - the model catalog for role options comes from value.model_catalog
//    (settings.MODEL_CATALOG, echoed by status()); harness/effort-per-model
//    are derived here from model_catalog.models[*].harness/.efforts (owner policy),
//    with supported_efforts retained as a backward-compatible fallback.
function crossTalkModelsByHarness(modelCatalog){
  const models=modelCatalog?.models||{},byHarness={};
  for(const [name,info] of Object.entries(models).sort(([a],[b])=>a.localeCompare(b))){const h=info?.harness;if(!h||info.available===false)continue;(byHarness[h]=byHarness[h]||[]).push(name)}
  return byHarness;
}
function crossTalkEffortsForModel(modelCatalog,model){const entry=modelCatalog?.models?.[model],efforts=entry&&(entry.efforts||entry.supported_efforts);return Array.isArray(efforts)?efforts:[]}
function crossTalkModelChoiceLabel(model,modelCatalog){
  const entry=modelCatalog?.models?.[model],label=entry?.label||model||"Unknown";
  if(entry?.selection_kind==="family")return `${label} (latest${entry.resolved?` · ${entry.resolved}`:""})`;
  return entry?.selection_kind==="pin"?(entry.resolved||label):label;
}
function crossTalkRoleDefaultOption(modelCatalog){
  const models=modelCatalog?.models||{},available=Object.keys(models).filter(name=>models[name]?.available!==false),
    choose=(name,harness)=>available.includes(name)&&models[name]?.harness===harness?name:"",
    model=choose("sonnet","claude")||choose("luna","codex")||choose("sol","codex")||choose("opus","claude")||choose("astra","codex")||"",
    harness=model?models[model].harness:"claude";
  return {harness,model,effort:null};
}
// roles is a dict keyed by role name; the current draft (authoritative once touched)
// falls back to the last-read server value, like the other CAS sections.
function crossTalkRolesDict(value){
  const draft=state.settingsDrafts.roles;
  if(draft&&typeof draft==="object"&&!Array.isArray(draft))return draft;
  const base=value.roles;
  return base&&typeof base==="object"&&!Array.isArray(base)?base:{};
}
// Mutators below assume crossTalkRolesFromDom() (defined further down, near the
// settings change/click listeners) is in scope by the time these ever run -- function
// declarations are hoisted for the whole module, so call order here is fine.
function crossTalkAddRole(desiredName){
  const roles=crossTalkRolesFromDom();let name=(desiredName||"").trim()||"new-role",n=2;
  while(name in roles){name=`${(desiredName||"").trim()||"new-role"}-${n++}`}
  roles[name]={options:[crossTalkRoleDefaultOption(state.settings?.model_catalog)],preamble:"",autonomy:"ask",
              description:"",edits:"ask",brief_template:""};
  saveSettings("roles");render();
}
function crossTalkDeleteRole(name){const roles=crossTalkRolesFromDom();delete roles[name];saveSettings("roles");render()}
function crossTalkRenameRole(oldName,newName){
  newName=(newName||"").trim();if(!newName||newName===oldName)return;
  const roles=crossTalkRolesFromDom();
  if(newName in roles){toast(`A role named "${newName}" already exists.`);render();return}
  const ordered={};for(const [key,val] of Object.entries(roles))ordered[key===oldName?newName:key]=val;
  state.settingsDrafts.roles=ordered;saveSettings("roles");render();
}
function crossTalkAddRoleOption(name){
  const roles=crossTalkRolesFromDom(),role=roles[name];if(!role||role.options.length>=settings_MAX_ROLE_OPTIONS)return;
  role.options.push(crossTalkRoleDefaultOption(state.settings?.model_catalog));
  saveSettings("roles");render();
}
function crossTalkRemoveRoleOption(name,index){
  const roles=crossTalkRolesFromDom(),role=roles[name];if(!role||role.options.length<=1)return;
  role.options.splice(index,1);saveSettings("roles");render();
}
function crossTalkReorderRoleOptions(name,from,to){
  const roles=crossTalkRolesFromDom(),role=roles[name];
  if(!role||from<0||to<0||from>=role.options.length||to>=role.options.length||from===to)return;
  role.options.splice(to,0,role.options.splice(from,1)[0]);
  saveSettings("roles");render();
}
const settings_MAX_ROLE_OPTIONS=4;
function crossTalkWorkerModelDisabledReason(name,harness,model){
  return name!=="orchestrator"&&harness==="claude"&&autoModeRefused(model)?`${model} cannot run auto mode and is not supported for workers; choose Sonnet, Opus or Haiku 5.5 or later.`:"";
}
function crossTalkRoleModelOptions(name,harness,models,current,catalog){
  const missing=current&&!models.includes(current)?`<option value="${esc(current)}" selected disabled>${esc(crossTalkModelChoiceLabel(current,catalog))} (unavailable)</option>`:"";
  return `<option value="">Choose model</option>${missing}`+models.map(model=>{
    const reason=crossTalkWorkerModelDisabledReason(name,harness,model);
    return `<option value="${esc(model)}" ${model===current?"selected":""} ${reason?`disabled title="${esc(reason)}"`:""}>${esc(crossTalkModelChoiceLabel(model,catalog))}${reason?" (not supported for workers)":""}</option>`;
  }).join("");
}
function crossTalkRoleOptionRow(name,option,index,count,modelsByHarness,modelCatalog){
  const harness=option.harness||"claude",model=option.model||"",effort=option.effort||"";
  const models=modelsByHarness[harness]||[],efforts=crossTalkEffortsForModel(modelCatalog,model);
  const workerReason=crossTalkWorkerModelDisabledReason(name,harness,model);
  const invalidEffort=effort&&!efforts.includes(effort)?`<option value="${esc(effort)}" selected disabled>${esc(effort)} (not available for this model)</option>`:"";
  const positionLabel=["1st (primary)","2nd (secondary)","3rd","4th"][index]||`${index+1}`;
  const modelField=harness==="hermes"
    ?`<input type="text" data-role-option-field="model" data-role-name="${esc(name)}" data-role-option-index="${index}" value="${esc(model)}" placeholder="Not wired yet; free text">`
    :`<select data-role-option-field="model" data-role-name="${esc(name)}" data-role-option-index="${index}">${crossTalkRoleModelOptions(name,harness,models,model,modelCatalog)}</select>${workerReason?`<p class="quiet">${esc(workerReason)} The saved choice is retained until you replace it.</p>`:""}`;
  return `<div class="card cross-talk-role-row" draggable="true" data-role-name="${esc(name)}" data-role-option-row="${index}"><div class="cross-talk-role-head"><span class="drag-handle" aria-hidden="true">⋮⋮</span><strong>${esc(positionLabel)}</strong><button type="button" class="secondary" data-role-option-remove data-role-name="${esc(name)}" data-role-option-index="${index}" aria-label="Remove option ${index+1}" ${count<=1?"disabled":""}>Remove</button></div><div class="cross-talk-role-fields"><label>App / harness<select data-role-option-field="harness" data-role-name="${esc(name)}" data-role-option-index="${index}"><option value="claude" ${harness==="claude"?"selected":""}>Claude</option><option value="codex" ${harness==="codex"?"selected":""}>Codex</option><option value="hermes" ${harness==="hermes"?"selected":""}>Hermes (not wired yet)</option></select></label><label>Model${modelField}</label><label>Thinking<select data-role-option-field="effort" data-role-name="${esc(name)}" data-role-option-index="${index}" ${harness==="hermes"?"disabled":""}><option value="">Auto</option>${invalidEffort}${settingOptions(efforts,effort)}</select></label></div></div>`;
}
function crossTalkRoleUsedByTasks(name,taskRules){
  return Object.entries(taskRules||{}).filter(([,value])=>value===name).map(([task])=>task);
}
function crossTalkRoleCard(name,role,modelsByHarness,modelCatalog,taskRules,hooksRoleEnabled=true,briefTemplateDefault=null){
  const options=Array.isArray(role.options)&&role.options.length?role.options:[crossTalkRoleDefaultOption(modelCatalog)];
  const autonomy=role.autonomy==="off"?"off":"ask",preamble=role.preamble||"";
  const description=role.description||"",edits=role.edits==="yes"?"yes":"ask",briefTemplate=role.brief_template||"";
  // Owner decision (2026-09-29): brief_template textarea always binds to and saves
  // the raw value above -- never the default -- so an empty saved template stays
  // empty on disk. briefTemplateDefault (from settings.default_brief_template via
  // the /api/v1/settings "role_brief_template_defaults" sibling map) is shown only
  // as a read-only hint when the saved field is empty, never written into it.
  const briefTemplateHint=(!briefTemplate&&briefTemplateDefault)?`<p class="quiet">(default) Empty uses this role's built-in template:</p><pre class="cross-talk-brief-default">${esc(briefTemplateDefault)}</pre>`:"";
  const usedByTasks=crossTalkRoleUsedByTasks(name,taskRules);
  const usedByLine=usedByTasks.length?`Used by tasks: ${usedByTasks.join(", ")}`:"Not currently assigned to any task (task rules can still name it, or another app can start it directly).";
  return `<div class="card cross-talk-role-card" data-role-card="${esc(name)}"><div class="section-head"><div class="cross-talk-role-name"><label class="sr-only" for="role-name-${esc(name)}">Role name</label><input id="role-name-${esc(name)}" type="text" data-role-name-input="${esc(name)}" value="${esc(name)}" placeholder="Role name, e.g. director"></div><button type="button" class="secondary" data-role-delete="${esc(name)}">Delete role</button></div><p class="quiet">${esc(usedByLine)}</p><label>Description<textarea data-role-description="${esc(name)}" maxlength="300" rows="2" placeholder="Short description of what this role is for">${esc(description)}</textarea></label><label>Autonomy<select data-role-autonomy="${esc(name)}"><option value="off" ${autonomy==="off"?"selected":""}>Off (never called automatically)</option><option value="ask" ${autonomy==="ask"?"selected":""}>Ask before acting</option></select></label><label>Edits default<select data-role-edits="${esc(name)}"><option value="yes" ${edits==="yes"?"selected":""}>Yes (default to making changes)</option><option value="ask" ${edits==="ask"?"selected":""}>Ask (default to proposing, not editing)</option></select></label><p class="quiet">This is only a default; no role is locked read-only, so a discovery or review agent already holding context may still be sent on to make the fix.</p><label class="check"><input type="checkbox" data-role-hooks-enabled="${esc(name)}" ${hooksRoleEnabled?"checked":""}> Hook reminders enabled for this role</label><label class="cross-talk-preamble">Standing preamble<textarea data-role-preamble="${esc(name)}" maxlength="2000" rows="2" placeholder="Optional instructions sent with every letter to this role">${esc(preamble)}</textarea></label><div class="cross-talk-role-rows">${options.map((option,i)=>crossTalkRoleOptionRow(name,option,i,options.length,modelsByHarness,modelCatalog)).join("")}</div><button type="button" class="secondary" data-role-option-add="${esc(name)}" ${options.length>=settings_MAX_ROLE_OPTIONS?"disabled":""}>+ Add option</button><label class="cross-talk-preamble">Brief template<textarea data-role-brief-template="${esc(name)}" maxlength="2000" rows="3" placeholder="Optional checks and report shape this role's brief should include">${esc(briefTemplate)}</textarea></label>${briefTemplateHint}</div>`;
}
function crossTalkSettingsSections(p,value,restore){
  const roles=crossTalkRolesDict(value),roleNames=Object.keys(roles);
  const hooksForRoleCards=crossTalkHooksDraft(value);
  const taskRules={...(value.task_rules||{}),...(state.settingsDrafts.task_rules||{})};
  const modelCatalog=value.model_catalog||{};
  const modelsByHarness=crossTalkModelsByHarness(modelCatalog);
  return     `<section class="card settings-section" data-settings-section="roles"><div class="section-head"><div><h2>Roles</h2><p class="quiet">Named roles, each with ordered App/harness → Model → Thinking options (a fallback chain, up to 4), plus a per-role autonomy and standing preamble.</p></div></div>${settingsFeedback("roles")}<div class="cross-talk-role-cards">${roleNames.map(name=>crossTalkRoleCard(name,roles[name],modelsByHarness,modelCatalog,taskRules,hooksForRoleCards.role_enabled?.[name]!==false,value.role_brief_template_defaults?.[name])).join("")||'<p class="quiet">No roles yet.</p>'}</div><div class="cross-talk-role-add"><input type="text" data-role-new-name placeholder="New role name, e.g. worker-hard"><button type="button" data-role-add>+ Add role</button></div></section>`+
    crossTalkHooksSection(p,value);
}
// Role hooks (AWR role hooks, board slice slice-d948010a127d): short reminder text
// injected into live Claude Code/Codex sessions at fixed trigger points. The 5
// trigger names are permanent and stable (settings.HOOK_TRIGGERS); "eutonos-default"
// (settings.BUILTIN_HOOK_SET_NAME) is a read-only built-in set -- duplicate it to
// get an editable copy. Draft-authoritative exactly like crossTalkRolesFromDom():
// once touched, state.settingsDrafts.hooks IS the draft (deep-cloned off
// value.hooks the first time), so every mutator below reads/writes through it.
const HOOK_TRIGGERS=["message","session_start","edit_nudge","stop_check","long_turn"];
const HOOK_TRIGGER_LABELS={message:"Message",session_start:"Session start",edit_nudge:"Edit nudge",stop_check:"Stop check",long_turn:"Long turn"};
const BUILTIN_HOOK_SET_NAME="eutonos-default";
const HOOK_SET_NAME_RE=/^[A-Za-z0-9._-]{1,64}$/;
const HOOK_BUILT_IN_ROLE_NAMES=["orchestrator","director","discovery","reviewer"];
function crossTalkHooksDraft(value){
  if(state.settingsDrafts.hooks&&typeof state.settingsDrafts.hooks==="object"&&!Array.isArray(state.settingsDrafts.hooks))return state.settingsDrafts.hooks;
  const base=value&&value.hooks;
  const cloned=base&&typeof base==="object"&&!Array.isArray(base)?JSON.parse(JSON.stringify(base)):
    {enabled:true,closeout_check:true,active_set:BUILTIN_HOOK_SET_NAME,role_enabled:{},trigger_enabled:{},edit_nudge_rate_limit_minutes:10,long_turn_threshold:20,sets:{}};
  state.settingsDrafts.hooks=cloned;return cloned;
}
function crossTalkHooksRoleNames(value){
  const names=Object.keys(crossTalkRolesDict(value));
  for(const name of HOOK_BUILT_IN_ROLE_NAMES)if(!names.includes(name))names.push(name);
  return names;
}
function crossTalkSetHooksField(patch){const draft=crossTalkHooksDraft(state.settings||{});Object.assign(draft,patch);saveSettings("hooks")}
function crossTalkSetHooksRoleEnabled(name,on){const draft=crossTalkHooksDraft(state.settings||{});draft.role_enabled=draft.role_enabled||{};draft.role_enabled[name]=Boolean(on);saveSettings("hooks")}
function crossTalkSetHooksTriggerEnabled(trigger,on){const draft=crossTalkHooksDraft(state.settings||{});draft.trigger_enabled=draft.trigger_enabled||{};draft.trigger_enabled[trigger]=Boolean(on);saveSettings("hooks")}
function crossTalkSetHooksSetText(setName,role,trigger,text){
  if(setName===BUILTIN_HOOK_SET_NAME)return;
  const draft=crossTalkHooksDraft(state.settings||{});draft.sets=draft.sets||{};
  const set=draft.sets[setName]=draft.sets[setName]||{roles:{}};set.roles=set.roles||{};
  if(!set.roles[role])set.roles[role]=Object.fromEntries(HOOK_TRIGGERS.map(t=>[t,null]));
  set.roles[role][trigger]=text?text:null;
  saveSettings("hooks");
}
function crossTalkAddHooksSet(cloneFromName){
  const draft=crossTalkHooksDraft(state.settings||{});draft.sets=draft.sets||{};
  let n=1,name=`custom-${n}`;while(name in draft.sets){n++;name=`custom-${n}`}
  const source=draft.sets[cloneFromName];
  draft.sets[name]=source?JSON.parse(JSON.stringify(source)):{roles:{}};
  state.hooksEditingSet=name;
  saveSettings("hooks");render();
  return name;
}
function crossTalkRenameHooksSet(oldName,newName){
  if(oldName===BUILTIN_HOOK_SET_NAME)return;
  newName=(newName||"").trim();if(!newName||newName===oldName)return;
  if(!HOOK_SET_NAME_RE.test(newName)){toast('Set names may only use letters, numbers, ".", "_" and "-" (max 64 characters).');render();return}
  const draft=crossTalkHooksDraft(state.settings||{});draft.sets=draft.sets||{};
  if(newName in draft.sets){toast(`A set named "${newName}" already exists.`);render();return}
  const ordered={};for(const [key,val] of Object.entries(draft.sets))ordered[key===oldName?newName:key]=val;
  draft.sets=ordered;
  if(draft.active_set===oldName)draft.active_set=newName;
  if(state.hooksEditingSet===oldName)state.hooksEditingSet=newName;
  saveSettings("hooks");render();
}
function crossTalkDeleteHooksSet(name){
  if(name===BUILTIN_HOOK_SET_NAME)return;
  const draft=crossTalkHooksDraft(state.settings||{});if(!draft.sets)return;
  delete draft.sets[name];
  if(draft.active_set===name)draft.active_set=BUILTIN_HOOK_SET_NAME;
  if(state.hooksEditingSet===name)state.hooksEditingSet=null;
  saveSettings("hooks");render();
}
async function loadHooksRecent(){
  if(state.hooks.recentLoading)return;const requestedProject=state.project;state.hooks.recentLoading=true;render();
  try{
    const response=await fetch(`/api/v1/hooks/recent?project=${encodeURIComponent(requestedProject)}&limit=50`,{cache:"no-store"});
    const data=await response.json();
    if(!response.ok)throw new Error(data?.message||`Hooks recent returned ${response.status}`);
    if(state.project!==requestedProject)return;
    state.hooks.recent=Array.isArray(data.events)?data.events:[];state.hooks.recentError=null;
  }catch(error){if(state.project===requestedProject){state.hooks.recent=[];state.hooks.recentError=error.message}}
  finally{state.hooks.recentLoading=false;if(state.project===requestedProject)render()}
}
function crossTalkHooksSection(p,value){
  const hooks=crossTalkHooksDraft(value);
  const sets=hooks.sets&&typeof hooks.sets==="object"?hooks.sets:{[BUILTIN_HOOK_SET_NAME]:{roles:{}}};
  const setNames=Object.keys(sets);
  const activeSet=hooks.active_set&&setNames.includes(hooks.active_set)?hooks.active_set:(setNames[0]||BUILTIN_HOOK_SET_NAME);
  const editingSet=state.hooksEditingSet&&setNames.includes(state.hooksEditingSet)?state.hooksEditingSet:activeSet;
  const readOnly=editingSet===BUILTIN_HOOK_SET_NAME;
  const roleNamesForHooks=crossTalkHooksRoleNames(value);
  const roleEnabled=hooks.role_enabled||{};
  const numberField=(key,label,min,max)=>`<label>${esc(label)}<input type="number" min="${min}" max="${max}" step="1" data-hooks-field="${key}" value="${esc(hooks[key]??"")}"></label>`;
  const triggerNote={message:"Fires at the start of a message/turn.",session_start:"Fires when a session starts or resumes.",edit_nudge:"Fires while editing, at most once per the rate limit below.",stop_check:"On by default. Gives every turn one extra end-turn pass with this reminder and, for the main chat, the recording policy. It never loops.",long_turn:"Fires once the tool-call threshold below is crossed in a turn."};
  const triggerRows=HOOK_TRIGGERS.map(trigger=>{
    const on=hooks.trigger_enabled?.[trigger]!==false;
    return `<div class="settings-task-rule"><label class="check"><input type="checkbox" data-hooks-trigger-enabled="${esc(trigger)}" ${on?"checked":""}> ${esc(HOOK_TRIGGER_LABELS[trigger]||trigger)}</label><p class="quiet">${esc(triggerNote[trigger]||"")}</p></div>`;
  }).join("");
  const roleToggles=roleNamesForHooks.map(name=>`<label class="check"><input type="checkbox" data-role-hooks-enabled="${esc(name)}" ${roleEnabled[name]!==false?"checked":""}> ${esc(name)}</label>`).join("")||'<p class="quiet">No roles yet.</p>';
  const setOptions=setNames.map(name=>`<option value="${esc(name)}" ${name===activeSet?"selected":""}>${esc(name)}</option>`).join("");
  const editingOptions=setNames.map(name=>`<option value="${esc(name)}" ${name===editingSet?"selected":""}>${esc(name)}${name===BUILTIN_HOOK_SET_NAME?" (read-only)":""}</option>`).join("");
  const editingSetObj=sets[editingSet]||{roles:{}};
  const setRoles=editingSetObj.roles&&typeof editingSetObj.roles==="object"?editingSetObj.roles:{};
  const gridRoleNames=[...new Set(["default",...Object.keys(setRoles)])];
  const grid=gridRoleNames.map(roleName=>{
    const texts=setRoles[roleName]||{};
    const cells=HOOK_TRIGGERS.map(trigger=>{
      const text=texts[trigger]||"";
      const placeholder=roleName==="default"?"Not set":"Not set — falls back to 'default'";
      return `<label>${esc(HOOK_TRIGGER_LABELS[trigger]||trigger)}<textarea maxlength="800" rows="2" data-hooks-set-text data-hooks-set-name="${esc(editingSet)}" data-hooks-set-role="${esc(roleName)}" data-hooks-set-trigger="${esc(trigger)}" placeholder="${esc(placeholder)}" ${readOnly?"disabled":""}>${esc(text)}</textarea></label>`;
    }).join("");
    return `<div class="card"><h3>${esc(roleName)}</h3><div class="grid">${cells}</div></div>`;
  }).join("");
  if(["roles","hooks"].includes(state.settingsSection)&&!state.hooks.recent&&!state.hooks.recentLoading&&!state.hooks.recentError)queueMicrotask(()=>loadHooksRecent());
  const recentRows=(state.hooks.recent||[]).map(ev=>`<tr><td>${esc(when(ev.t))}</td><td>${esc(ev.app||"Unknown")}</td><td>${esc(ev.role||"Unknown")}</td><td>${esc(ev.set||"Unknown")}</td><td>${esc(ev.trigger)}</td><td>${ev.fired?'<span class="status good">Fired</span>':'<span class="status warn">Skipped</span>'}</td><td>${esc(ev.reason||"")}</td></tr>`).join("");
  const recentBody=state.hooks.recentLoading&&!state.hooks.recent?"<p>Reading recent firings…</p>":state.hooks.recentError?`<p class="quiet">Recent firings unavailable: ${esc(state.hooks.recentError)}</p>`:recentRows?`<div class="table-wrap"><table><thead><tr><th>Time</th><th>App</th><th>Role</th><th>Set</th><th>Trigger</th><th>Fired</th><th>Reason</th></tr></thead><tbody>${recentRows}</tbody></table></div>`:'<p class="quiet">No recent hook firings recorded.</p>';
  return `<section class="card settings-section" data-settings-section="hooks"><div class="section-head"><div><h2>Role hooks</h2><p class="quiet">Closeout checks and short reminders in live Claude Code and Codex sessions.</p></div></div>${settingsFeedback("hooks")}`+
    `<label class="check"><input type="checkbox" data-setting="hooks-enabled" ${hooks.enabled?"checked":""}> Enable role hooks</label>`+
    `<label class="check"><input type="checkbox" data-setting="hooks-closeout-check" ${hooks.closeout_check!==false?"checked":""}> Include the recording policy in the end-turn pass</label><p class="quiet">Adds what to record (nothing, a small RAM entry, a checkpoint, or full closeout) to the main chat's end-turn reminder. It never checks closeout proof.</p>`+
    `<section class="card"><h2>Triggers</h2>${triggerRows}</section>`+
    `<section class="card"><h2>Limits</h2><div class="settings-fields">${numberField("edit_nudge_rate_limit_minutes","Edit-nudge rate limit (minutes)",1,1440)}${numberField("long_turn_threshold","Long-turn threshold (tool calls)",1,1000)}</div></section>`+
    `<section class="card"><h2>Active set</h2><label>Active set<select data-setting="hooks-active-set">${setOptions}</select></label><p class="quiet">Applies to every enabled role and trigger.</p></section>`+
    `<fieldset class="card"><legend>Per-role hook toggle</legend><p class="quiet">Disabling a role stops its hook reminders regardless of which set is active; an unlisted role stays enabled.</p><div class="settings-checks">${roleToggles}</div></fieldset>`+
    `<section class="card"><h2>Edit a set</h2><label>Editing<select data-hooks-editing-set>${editingOptions}</select></label><div class="cross-talk-list-add"><button type="button" data-hooks-set-duplicate="${esc(editingSet)}">Duplicate</button><input type="text" data-hooks-set-rename-input placeholder="Rename to…" ${readOnly?"disabled":""}><button type="button" data-hooks-set-rename="${esc(editingSet)}" ${readOnly?"disabled":""}>Rename</button><button type="button" class="secondary" data-hooks-set-delete="${esc(editingSet)}" ${readOnly?"disabled":""}>Delete set</button></div>`+
    (readOnly?'<p class="quiet">This is the built-in read-only set. Duplicate it to make an editable copy.</p>':"")+
    `<div class="grid">${grid}</div>`+
    `<div class="cross-talk-list-add"><input type="text" data-hooks-set-add-role-input placeholder="Add role row, e.g. director" ${readOnly?"disabled":""}><button type="button" data-hooks-set-add-role="${esc(editingSet)}" ${readOnly?"disabled":""}>+ Add role row</button></div>`+
    `</section>`+
    `<section class="card"><h2>Recent firings</h2><p class="quiet">Metadata only; never any reminder/prompt text.</p>${recentBody}</section>`+
    `</section>`;
}
function settingValue(section){
  const pick=selector=>content.querySelector(selector), checked=selector=>Boolean(pick(selector)?.checked);
  if(section==="shared_work"){
    const saved=state.settings?.shared_work||{},read=(selector,fallback="")=>pick(selector)?.value??fallback,peerIds=Array.from(content.querySelectorAll("[data-shared-work-peer-id]")),peers=Array.from(content.querySelectorAll("[data-shared-work-peer-url]")).map((input,index)=>({id:peerIds[index]?.value||"",url:input.value.trim(),ref:content.querySelector("[data-shared-work-peer-ref=\""+index+"\"]")?.value.trim()||""})).filter(peer=>peer.url||peer.ref);
    const config={...(state.settingsDrafts.shared_work||saved),enabled:checked("[data-shared-work-field=\"enabled\"]"),auto_fetch:pick("[data-shared-work-field=\"auto_fetch\"]")?.checked??saved.auto_fetch??true,participant:read("[data-shared-work-field=\"participant\"]").trim()||null,canonical:{url:read("[data-shared-work-field=\"url\"]").trim()||null,ref:read("[data-shared-work-field=\"ref\"]").trim()||null},peers,attempt_owner:read("[data-shared-work-field=\"attempt_owner\"]",saved.attempt_owner||"").trim()||null,fetch_interval_seconds:Number(read("[data-shared-work-field=\"fetch_interval_seconds\"]",saved.fetch_interval_seconds||30))};delete config.revision;delete config.status;delete config.diagnostics;return config;
  }
  if(section==="delivery"){
    const field=key=>pick(`[data-delivery-field="${key}"]`)?.value||"";
    try{return {profile:"eutonos.git-delivery.config.v1",project_id:field("project_id"),participant:field("participant"),mode:field("mode"),remote_url:field("remote_url"),remote_ref:field("remote_ref"),branch:field("branch"),canonical_base:field("canonical_base"),records_ref:field("records_ref"),allowed_paths:field("allowed_paths").split("\n").map(v=>v.trim()).filter(Boolean),checks:JSON.parse(field("checks")||"[]")}}
    catch(error){toast("Enter valid JSON arrays for existing check commands.");return null}
  }
  if(section==="routing"){const routing=pick('[data-setting="routing-mode"]')?.value,preferences=["cost-savings","optimus","high-quality"],routing_profile=preferences[Number(pick('[data-setting="routing-preference"]')?.value)]||"optimus",model=routing==="fixed"?(pick('[data-setting="routing-model"]')?.value||null):null,allowed_models=routingAllowedModels(model),saved=state.settings?.routing?.max_concurrent_companions,max_concurrent_companions=Number.isInteger(saved)?saved:1;return {enabled:checked('[data-setting="routing-enabled"]'),routing,model,effort:pick('[data-setting="routing-effort"]')?.value||null,allowed_models,routing_profile,max_concurrent_companions}}
  if(section==="task_rules")return jobRulesView();
  if(section==="harnesses"){const value={};for(const name of HARNESS_NAMES){const limit=Math.trunc(Number(pick(`[data-harness-limit="${name}"]`)?.value));value[name]={enabled:checked(`[data-harness-enabled="${name}"]`),max_workers:Number.isFinite(limit)?Math.max(1,Math.min(7,limit)):jobHarnessesCfg()[name].max_workers}}return value}
  if(section==="ram")return {retrieval:pick('[data-setting="ram-retrieval"]')?.value,capture:checked('[data-setting="ram-capture"]'),cadence:pick('[data-setting="ram-cadence"]')?.value||"every-turn",detail:pick('[data-setting="ram-detail"]')?.value||"summary"};
  if(section==="tracking")return {enabled:checked('[data-setting="tracking-enabled"]')};
  if(section==="hooks"){
    // enabled/trigger_enabled/limits/active_set are simple scalar-ish fields
    // scanned straight from the DOM (like routing/ram above); role_enabled and
    // sets are dict/list-shaped and maintained directly on the draft by the
    // mutators in crossTalkSetHooksRoleEnabled/crossTalkSetHooksSetText/etc.,
    // so they are read straight off that draft rather than re-scanned here.
    const base=crossTalkHooksDraft(state.settings||{});
    const closeoutCheck=pick('[data-setting="hooks-closeout-check"]');
    const trigger_enabled={};HOOK_TRIGGERS.forEach(trigger=>{trigger_enabled[trigger]=checked(`[data-hooks-trigger-enabled="${trigger}"]`)});
    const numberField=(key,fallback)=>{const input=pick(`[data-hooks-field="${key}"]`);if(!input||input.value==="")return fallback;const n=Number(input.value);return Number.isFinite(n)?n:fallback};
    return {...base,enabled:checked('[data-setting="hooks-enabled"]'),closeout_check:closeoutCheck?Boolean(closeoutCheck.checked):base.closeout_check!==false,trigger_enabled,
            edit_nudge_rate_limit_minutes:numberField("edit_nudge_rate_limit_minutes",base.edit_nudge_rate_limit_minutes),
            long_turn_threshold:numberField("long_turn_threshold",base.long_turn_threshold),
            active_set:pick('[data-setting="hooks-active-set"]')?.value||base.active_set,
            role_enabled:base.role_enabled,sets:base.sets};
  }
  if(section==="roles"){
    // Dict of named roles; state.settingsDrafts.roles is the single source of truth
    // once any mutator (add/delete/rename role, add/remove/reorder option, or a
    // simple field setter) has touched it -- see crossTalkRolesFromDom().
    return crossTalkRolesFromDom();
  }
  return null;
}
function settingsPresentation(saver){return {settings:saver.snapshot,drafts:Object.fromEntries(saver.drafts),feedback:{...saver.feedback}}}
function captureSettingsDrafts(){const saver=settingsSavers.get(state.project);if(!saver)return;const view=settingsPresentation(saver);state.settingsDrafts=view.drafts;state.settingsFeedback=view.feedback}
function settingsEqual(a,b){if(a===b)return true;if(!a||!b||typeof a!=="object"||typeof b!=="object")return false;const keys=Object.keys(a);return keys.length===Object.keys(b).length&&keys.every(key=>settingsEqual(a[key],b[key]))}
function settingsFocus(){const el=document.activeElement;if(!el||!content.contains(el))return null;for(const name of ["data-setting","data-setting-model","data-setting-rule-role","data-role-description","data-role-edits","data-role-brief-template","data-shared-work-field","data-shared-work-peer-url","data-shared-work-peer-ref"]){if(el.hasAttribute(name))return {name,value:el.getAttribute(name),start:el.selectionStart,end:el.selectionEnd}}return null}
function restoreSettingsFocus(focus){if(!focus)return;const el=Array.from(content.querySelectorAll(`[${focus.name}]`)).find(el=>el.getAttribute(focus.name)===focus.value);el?.focus({preventScroll:true});if(el&&typeof focus.start==="number"&&typeof el.setSelectionRange==="function")el.setSelectionRange(focus.start,focus.end)}
let modelCatalogLoading=false,modelWatchReviewing=false;
function modelWatchMarkup(hint){
  const count=Number.isSafeInteger(hint?.pending_count)?hint.pending_count:0,sources=hint?.sources||{},items=Array.isArray(hint?.items)?hint.items:[];
  const observed=at=>Number.isFinite(at)?when(at*1000):"Unknown";
  const rows=["codex","claude"].map(name=>{const source=sources[name]||{};return `<p>${name==="codex"?"Codex":"Claude"}: ${esc(source.status||"unknown")} · Last check: ${esc(observed(source.last_scan_at))} · Last good observation: ${esc(observed(source.last_good_at))}</p>`}).join("");
  const official=url=>typeof url==="string"&&/^https:\/\/(?:learn\.chatgpt\.com\/docs\/|code\.claude\.com\/docs\/en\/)[A-Za-z0-9/_-]+$/.test(url);
  const docs=hint?.documentation?.sources||{},docEntries=Object.entries(docs),docFailures=docEntries.filter(([,source])=>source.status!=="ok").length;
  const docSummary=docEntries.length?`<p>Official documentation: ${docEntries.length} pages checked · ${docFailures} unavailable · Latest check: ${esc(observed(Math.max(...docEntries.map(([,source])=>source.last_scan_at||0))))}</p>`:"";
  const changes=items.slice(0,16).map(item=>{
    if(item.source==="harness_docs"){
      const link=official(item.url)?`<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">Official source</a>`:"Official source unavailable";
      const date=typeof item.date==="string"&&/^\d{4}-\d{2}-\d{2}$/.test(item.date)?` · ${esc(item.date)}`:"";
      const detail=typeof item.diff==="string"&&item.diff?`<details><summary>Show bounded text change</summary><pre>${esc(item.diff.slice(0,900))}</pre></details>`:"";
      return `<li>Documentation · ${esc(item.kind)}${date} · ${link}${detail}</li>`;
    }
    return `<li>${esc(item.source)} · ${esc(item.kind)} · ${esc(item.key)}</li>`;
  }).join("");
  return `<details id="model-watch-status"><summary>${count?`${count} harness change${count===1?"":"s"} to review`:"Harness sources"}</summary><div class="card">${rows}${docSummary}${hint?.error?'<p class="quiet">Harness source metadata is unavailable; saved choices are preserved.</p>':""}${changes?`<ul>${changes}</ul><button type="button" data-model-watch-review ${modelWatchReviewing?"disabled":""}>Mark shown changes reviewed</button>`:""}${hint?.pending_truncated?'<p class="quiet">More changes remain; reviewing these keeps later changes pending.</p>':""}</div></details>`;
}
function refreshModelWatch(){
  const old=document.getElementById("model-watch-status"),markup=modelWatchMarkup(state.snapshot?.model_watch);
  if(old){const open=old.open;old.outerHTML=markup;document.getElementById("model-watch-status").open=open}
  else document.querySelector(".top-actions")?.insertAdjacentHTML("afterbegin",markup);
}
async function reviewModelWatch(){
  if(modelWatchReviewing)return;
  const ids=(state.snapshot?.model_watch?.items||[]).slice(0,16).map(item=>item.id).filter(id=>typeof id==="string");
  if(!ids.length)return;modelWatchReviewing=true;refreshModelWatch();
  try{const response=await fetch("/api/v1/model-watch/ack",{method:"POST",headers:{"Content-Type":"application/json","X-Eutonos-Settings":state.snapshot?.settings_token||""},body:JSON.stringify({ids})});if(!response.ok)throw new Error("Review unavailable");const data=await response.json();state.snapshot.model_watch=data.model_watch;if(data.status!=="ok")throw new Error("Review incomplete");toast("Shown harness changes marked reviewed.")}
  catch(error){toast("Could not finish reviewing harness changes; check what remains pending.")}
  finally{modelWatchReviewing=false;refreshModelWatch()}
}
function modelSelectOptions(models,current,catalog,emptyLabel="Choose model"){
  const missing=current&&!models.includes(current)?`<option value="${esc(current)}" selected disabled>${esc(crossTalkModelChoiceLabel(current,catalog))} (unavailable)</option>`:"";
  return `<option value="">${esc(emptyLabel)}</option>${missing}${settingOptions(models,current,Object.fromEntries(models.map(name=>[name,crossTalkModelChoiceLabel(name,catalog)])))}`;
}
function refreshModelChecks(kind,models,catalog){
  const list=content.querySelector(`[data-model-list="${kind}"]`);if(!list)return;
  const attribute=kind==="routing"?"data-setting-model":"data-setting-model",inputs=Array.from(list.querySelectorAll("input")),selected=new Set(inputs.filter(input=>input.checked).map(input=>input.getAttribute(attribute))),all=[...new Set([...models,...selected])],active=document.activeElement,key=active?.getAttribute(attribute);
  list.innerHTML=all.map(name=>{const available=models.includes(name)&&catalog.models?.[name]?.available!==false;return `<label class="check"><input type="checkbox" ${attribute}="${esc(name)}" ${selected.has(name)?"checked":""} ${available?"":"disabled"}> ${esc(crossTalkModelChoiceLabel(name,catalog))}${available?"":" (unavailable)"}</label>`}).join("");
  if(key)Array.from(list.querySelectorAll("input")).find(input=>input.getAttribute(attribute)===key)?.focus({preventScroll:true});
}
function refreshModelMenus(catalog,models){
  const byHarness=crossTalkModelsByHarness(catalog);
  for(const node of content.querySelectorAll('select[data-role-option-field="model"]')){
    const row=node.closest("[data-role-option-row]"),harness=row?.querySelector('[data-role-option-field="harness"]')?.value||"claude",current=node.value;
    node.innerHTML=crossTalkRoleModelOptions(row?.dataset.roleName||"",harness,byHarness[harness]||[],current,catalog);
    const thinking=row?.querySelector('[data-role-option-field="effort"]');if(thinking){const effort=thinking.value,efforts=crossTalkEffortsForModel(catalog,current),missing=effort&&!efforts.includes(effort)?`<option value="${esc(effort)}" selected disabled>${esc(effort)} (unavailable)</option>`:"";thinking.innerHTML=`<option value="">Auto</option>${missing}${settingOptions(efforts,effort)}`}
  }
  refreshModelChecks("routing",models,catalog);
  const fixed=content.querySelector('[data-setting="routing-model"]');if(fixed){const current=fixed.value;fixed.innerHTML=modelSelectOptions(models,current,catalog,"Choose model")}
  for(const node of content.querySelectorAll("select[data-job-field=model],select[data-job-field=fallback]")){if(!node.dataset?.jobField)continue;const current=node.value==="__main__"?"":node.value,main=node.dataset.jobField==="model";node.innerHTML=(main?`<option value="__main__" ${node.value==="__main__"?"selected":""}>Main chat (no worker)</option>`:"")+jobModelOptions(current,!main)}
  const thinking=content.querySelector('[data-setting="routing-effort"]');if(thinking&&!fixed?.disabled&&fixed?.value){const effort=thinking.value,efforts=crossTalkEffortsForModel(catalog,fixed.value),missing=effort&&!efforts.includes(effort)?`<option value="${esc(effort)}" selected disabled>${esc(effort)} (unavailable)</option>`:"";thinking.innerHTML=`<option value="">Auto (let primary decide)</option>${missing}${settingOptions(efforts,effort)}`}
}
function applyModelCatalog(value,projectId){
  if(state.project!==projectId||!state.settings||!value?.model_catalog)return;
  const catalog=value.model_catalog,models=Array.isArray(value.models)?value.models:[],changed=JSON.stringify(state.settings.model_catalog)!==JSON.stringify(catalog);
  const patch={model_catalog:catalog,models,catalog_live:true};state.settings={...state.settings,...patch};
  const saver=settingsSavers.get(projectId);if(saver)saver.snapshot={...saver.snapshot,...patch};
  if(value.model_watch!==undefined){state.snapshot.model_watch=value.model_watch;refreshModelWatch()}
  if(changed&&state.view==="settings")refreshModelMenus(catalog,models);
}
async function loadModelCatalog(){
  if(modelCatalogLoading)return;const projectId=state.project;modelCatalogLoading=true;
  try{const response=await fetch(`/api/v1/model-watch/catalog?project=${projectId}`,{cache:"no-store"});if(!response.ok)throw new Error("Catalog unavailable");applyModelCatalog(await response.json(),projectId)}
  catch(error){/* Retain the displayed catalog and every settings draft. */}
  finally{modelCatalogLoading=false}
}
function settingsSectionsForNav(nav){return nav==="roles"?["roles","hooks"]:nav==="routing"?["routing","task_rules"]:[nav]}
function applySettingsSection(){if(state.view!=="settings")return;const visible=new Set(settingsSectionsForNav(state.settingsSection));content.querySelectorAll("[data-settings-section]").forEach(section=>section.hidden=!visible.has(section.dataset.settingsSection))}
function syncSettingsNav(){const group=document.querySelector("[data-settings-nav-group]"),sub=document.getElementById("settings-subnav"),toggle=document.querySelector("[data-settings-nav-toggle]");if(!group||!sub||!toggle)return;sub.hidden=!state.settingsNavExpanded;toggle.setAttribute("aria-expanded",String(state.settingsNavExpanded));toggle.setAttribute("aria-label",`${state.settingsNavExpanded?"Hide":"Show"} Settings sections`);document.querySelectorAll("#settings-subnav [data-settings-nav]").forEach(link=>link.setAttribute("aria-current",state.view==="settings"&&link.dataset.settingsNav===state.settingsSection?"page":"false"))}
function syncBoardsNav(){const sub=document.getElementById("boards-subnav"),toggle=document.querySelector("[data-boards-nav-toggle]");if(!sub||!toggle)return;sub.hidden=!state.boardsNavExpanded;toggle.setAttribute("aria-expanded",String(state.boardsNavExpanded));toggle.setAttribute("aria-label",`${state.boardsNavExpanded?"Hide":"Show"} Boards sections`);document.querySelectorAll("#boards-subnav [data-boards-nav]").forEach(link=>link.setAttribute("aria-current",state.view==="boards"&&link.dataset.boardsNav===state.boardsSection?"page":"false"))}
// One queue per repository serializes revision-checked writes across every section.
class SettingsAutosave {
  constructor(snapshot,send,notify=()=>{}){this.snapshot=snapshot;this.send=send;this.notify=notify;this.pending=new Map();this.drafts=new Map();this.feedback={};this.blocked=false;this.running=null;this.problem=null}
  submit(section,value){const draft=JSON.parse(JSON.stringify(value));this.pending.set(section,draft);this.drafts.set(section,draft);this.report(section,this.blocked?"error":"pending",this.blocked?"Not saved. Resolve the previous save error to continue.":"Saving…");return this.drain()}
  report(section,type,text,fresh){this.feedback[section]={type,text};this.notify(section,type,text,fresh)}
  retry(){this.blocked=false;this.problem=null;return this.drain()}
  drain(){
    if(this.running||this.blocked||!this.pending.size)return this.running||Promise.resolve();
    // Start after assigning running, including when a transport throws synchronously.
    this.running=Promise.resolve().then(async()=>{
      while(this.pending.size&&!this.blocked){
        const [section,value]=this.pending.entries().next().value;this.pending.delete(section);this.report(section,"pending","Saving…");
        try{const result=await this.send(settingsRevisionFor(this.snapshot,section),section,value);this.snapshot=mergeSettingsResult(this.snapshot,section,result);if(this.drafts.get(section)===value)this.drafts.delete(section);this.report(section,this.pending.has(section)?"pending":"success",this.pending.has(section)?"Saving latest change…":"Saved",this.snapshot)}
        catch(error){if(!this.pending.has(section))this.pending.set(section,value);if(error.settings)this.snapshot=mergeSettingsResult(this.snapshot,section,error.settings);this.blocked=true;this.problem=error;for(const name of this.pending.keys())this.report(name,error.settings?"conflict":"error",error.settings?"Not saved: settings changed elsewhere. Your edits are kept. Retry to apply them, or reload to use the saved settings.":`Not saved: ${error.message}. Your edits are kept. Retry when ready.`,this.snapshot)}
      }
    }).finally(()=>{this.running=null});return this.running;
  }
}
// AWR-CT: settings.py keeps a SEPARATE CAS revision per section family --
// "roles" has its own read_roles()
// revision (roles_revision), distinct from the single
// top-level "revision" that covers routing/task_rules/ram/tracking (see
// settings.py's status()/update() vs. update_roles()).
// A section's POST must carry ITS OWN revision, and a section's response only
// carries that section's own updated slice -- never the whole settings shape --
// so the snapshot has to be merged back in per section, not replaced wholesale.
const SETTINGS_REVISION_KEY={roles:"roles_revision",hooks:"hooks_revision",delivery:"delivery_revision",shared_work:"shared_work_revision",shared_auto_fetch:"shared_work_revision"};
function settingsRevisionFor(snapshot,section){return snapshot?.[SETTINGS_REVISION_KEY[section]||"revision"]}
function mergeSettingsResult(snapshot,section,result){
  if(!result||typeof result!=="object")return snapshot;
if(section==="delivery")return {...snapshot,delivery:result.delivery,delivery_revision:result.revision};
  if(["shared_work","shared_auto_fetch"].includes(section))return {...snapshot,shared_work:result.shared_work,shared_work_revision:result.revision,shared_work_native_save:result.native_save,shared_work_git_delivery:result.git_delivery,shared_work_qualification:result.qualification};
  if(section==="roles")return {...snapshot,roles:result.roles,roles_revision:result.revision};
  if(section==="hooks")return {...snapshot,hooks:result.hooks,hooks_revision:result.revision};
  // routing/task_rules/ram/tracking: the server's update() returns the full
  // _effective() shape (including its own "revision"); spread over the
  // snapshot so roles and their separate revision are preserved.
  return {...snapshot,...result,...(snapshot.catalog_live?{models:snapshot.models,model_catalog:snapshot.model_catalog,catalog_live:true}:{})};
}
const settingsSavers=new Map();
function settingsFeedback(section){const value=state.settingsFeedback[section]||{type:"quiet",text:""};return `<div data-settings-feedback="${section}" class="settings-feedback ${esc(value.type)}" role="status"><span>${esc(value.text)}</span>${["error","conflict"].includes(value.type)?'<button type="button" class="secondary" data-settings-retry>Retry my changes</button><button type="button" class="secondary" data-settings-reload>Reload saved settings</button>':""}</div>`}
function settingsConfigRevision(value){return value?.shared_work_revision||value?.shared_work?.revision||""}
function sharedSourceSelection(config){if(!config)return null;const {revision,auto_fetch,participant,...sources}=config;return JSON.stringify(sources)}
function acceptSettingsSnapshot(projectId,value,authoritative=false){
  if(!value)return;const previous=state.settingsProject===projectId?state.settings:null,before=settingsConfigRevision(previous),after=settingsConfigRevision(value),p=state.snapshot?.projects?.[projectId],observed=sharedWorkRevision(p);
  state.settings=value;state.settingsProject=projectId;
  const row=state.snapshot?.shared_repositories?.find(row=>row.project===projectId);
  // Settings reads populate the form; observed summaries belong to the roster.
  // Only revision-checked save/conflict readback can optimistically replace it.
  if(!authoritative&&row?.configuration?.revision&&row.configuration.revision!==after)return;
  if((before&&after&&before!==after)||(after&&observed&&after!==observed)){
    if(previous&&sharedSourceSelection(previous.shared_work)===sharedSourceSelection(value.shared_work)&&p?.modules?.current_work?.shared_work){
      const shared=p.modules.current_work.shared_work;Object.assign(shared,{revision:after,auto_fetch:value.shared_work.auto_fetch,coverage:"unknown",fetch_state:value.shared_work.enabled?(value.shared_work.auto_fetch===false?"auto-fetch-off":"cached"):"disabled",freshness:{kind:"unknown",age_seconds:null}});
    }else invalidateSharedWorkProjection(p,after);
  }
  if(authoritative&&row)row.configuration=value.shared_work
}

function settingsNotice(projectId,section,type,text,fresh){
  if(state.project!==projectId)return;if(fresh)acceptSettingsSnapshot(projectId,fresh,["shared_work","shared_auto_fetch"].includes(section));captureSettingsDrafts();
  state.settingsFeedback[section]={type,text};const el=content.querySelector(`[data-settings-feedback="${section}"]`);if(el)el.outerHTML=settingsFeedback(section);
  // Refresh only when accepted/external values differ from the displayed forms.
  if(fresh&&["shared_work","shared_auto_fetch"].includes(section)&&type==="success"){
    const saver=settingsSavers.get(projectId),draft=saver?.drafts.get("shared_work");if(section==="shared_auto_fetch"&&draft)draft.auto_fetch=fresh.shared_work.auto_fetch;if(section==="shared_work"&&saver?.drafts.get("shared_auto_fetch")?.auto_fetch===fresh.shared_work.auto_fetch)saver.drafts.delete("shared_auto_fetch");captureSettingsDrafts();
    if(["settings","shared-repositories"].includes(state.view))render();queueMicrotask(refreshCurrentWork)
  }
  if(fresh&&state.view==="settings"&&content.querySelector("[data-settings-section]")&&["routing","harnesses","task_rules","ram","tracking"].some(name=>!settingsEqual(settingValue(name),state.settingsDrafts[name]||fresh[name])))render();
}
function settingsSaver(projectId,snapshot){
  const saver=new SettingsAutosave(snapshot,async(revision,section,value)=>{
    const token=state.snapshot?.settings_token;if(!token)throw new Error("Settings token unavailable; refresh the dashboard");
    const response=await fetch("/api/v1/settings",{method:"POST",headers:{"Content-Type":"application/json","X-Eutonos-Settings":token},body:JSON.stringify({project:projectId,revision,section,value})});
    const data=await response.json();if(!response.ok){const error=new Error(data.message||`Settings service returned ${response.status}`);if(response.status===409)error.settings=data.settings;throw error}return data;
  },(section,type,text,fresh)=>settingsNotice(projectId,section,type,text,fresh));settingsSavers.set(projectId,saver);return saver;
}
async function loadSettings(announce=false){
  if(state.settingsLoading)return;const requestedProject=state.project,existing=settingsSavers.get(requestedProject);state.settingsLoading=true;render();
  try{
    if(existing?.running)await existing.running;
    const response=await fetch(`/api/v1/settings?project=${encodeURIComponent(requestedProject)}`,{cache:"no-store"});if(!response.ok)throw new Error(`Settings service returned ${response.status}`);const value=await response.json();
    if(state.project!==requestedProject)return;
    acceptSettingsSnapshot(requestedProject,value);
    if(existing&&(existing.pending.size||existing.drafts.size)&&!announce){
      const changed=Array.from(existing.drafts.keys()).some(section=>settingsRevisionFor(existing.snapshot,section)!==settingsRevisionFor(value,section));
      // Keep the draft's original CAS when external settings changed. A later save
      // must conflict rather than rebasing an old full source form silently.
      if(!changed)existing.snapshot=value;
      captureSettingsDrafts();
      if(changed)for(const section of existing.drafts.keys())existing.report(section,"quiet","Saved settings changed elsewhere. Your unsaved edits and original save revision are kept. Review them before saving, or reload saved settings.");
    }
    else{settingsSaver(requestedProject,value);captureSettingsDrafts()}
    if(announce)toast("Saved settings reloaded.");
  }catch(error){if(state.project===requestedProject){state.settingsProject=requestedProject;if(existing){state.settings=existing.snapshot;captureSettingsDrafts();existing.report(state.settingsSection,"error",`Could not reload: ${error.message}. Your previous settings and unsaved edits are kept.`)}else state.settings=null;toast(`Settings unavailable: ${error.message}`)}}finally{state.settingsLoading=false;if(state.project!==requestedProject){if(["settings","shared-repositories"].includes(state.view))loadSettings();return}render()}
}
function saveSharedAutoFetch(){
  const value={auto_fetch:content.querySelector('[data-shared-work-field="auto_fetch"]')?.checked??true},saver=settingsSavers.get(state.project);
  if(!saver||!settingsRevisionFor(state.settings,"shared_work")){settingsNotice(state.project,"shared_auto_fetch","error","Reload saved settings before changing preferences.");return}
  // Use the same CAS and queue, without requiring a manifest for an unshared root.
  return saver.submit("shared_auto_fetch",value);
}
content.addEventListener("click",event=>{const button=event.target.closest("[data-shared-project]");if(button){select.value=button.dataset.sharedProject;select.dispatchEvent(new Event("change"));return}if(event.target.closest("[data-shared-auto-save]"))saveSharedAutoFetch()});
function saveSettings(section){return submitSetting(section,settingValue(section))}
function submitSetting(section,value){
  const revision=settingsRevisionFor(state.settings,section);
  if(value===null||revision===null||revision===undefined){settingsNotice(state.project,section,"error","Reload saved settings before changing preferences.");return}
  state.settingsDrafts[section]=value;
  const saver=settingsSavers.get(state.project)||settingsSaver(state.project,state.settings);return saver.submit(section,value);
}
function restoreSettingsDefaults(section){
  const value=state.settings?.defaults?.[section],saver=settingsSavers.get(state.project);
  if(!value||!saver){settingsNotice(state.project,section,"error","Reload saved settings before restoring defaults.");return}
  saver.submit(section,JSON.parse(JSON.stringify(value)));captureSettingsDrafts();render();
}
function updateRoutingSlider(input){
  const label=["Cost savings","Optimus","High quality"][Number(input.value)]||"";
  content.querySelectorAll("[data-routing-preference-option]").forEach(option=>option.classList.toggle("is-selected",Number(option.dataset.routingPreferenceOption)===Number(input.value)));
  input.setAttribute("aria-valuetext",label);
}
content.addEventListener("input",event=>{if(event.target.matches('[data-shared-work-field="auto_fetch"]')){const saver=settingsSavers.get(state.project);if(saver){saver.drafts.set("shared_auto_fetch",{auto_fetch:event.target.checked});captureSettingsDrafts()}return}if(event.target.matches("[data-shared-work-field],[data-shared-work-peer-url],[data-shared-work-peer-ref]")){const draft=settingValue("shared_work"),saver=settingsSavers.get(state.project);if(draft){state.settingsDrafts.shared_work=draft;if(saver){saver.drafts.set("shared_work",draft);captureSettingsDrafts()}}return}if(event.target.matches('[data-delivery-field]')){const draft=settingValue("delivery"),saver=settingsSavers.get(state.project);if(draft&&saver){saver.drafts.set("delivery",draft);captureSettingsDrafts()}return}if(event.target.matches('[data-setting][type="range"]'))updateRoutingSlider(event.target)});
content.addEventListener("click",event=>{const add=event.target.closest("[data-shared-work-peer-add]");if(add){const draft=settingValue("shared_work");draft.peers.push({id:"",url:"",ref:""});state.settingsDrafts.shared_work=draft;settingsSavers.get(state.project)?.drafts.set("shared_work",draft);render();return}const remove=event.target.closest("[data-shared-work-peer-remove]");if(remove){const draft=settingValue("shared_work");draft.peers.splice(Number(remove.dataset.sharedWorkPeerRemove),1);state.settingsDrafts.shared_work=draft;settingsSavers.get(state.project)?.drafts.set("shared_work",draft);render()}});
content.addEventListener("change",event=>{
  const input=event.target,section=input.closest("[data-settings-section]")?.dataset.settingsSection;
  if(!section||!input.matches('[data-setting],[data-setting-model],[data-setting-rule-model],[data-setting-rule-effort]'))return;
  if(input.matches('[data-setting="routing-preference"]')){const mode=content.querySelector('[data-setting="routing-mode"]');if(mode)mode.value="profile";applyRoutingPreset(Number(input.value));return}
  if(input.matches('[data-setting="routing-mode"],[data-setting="routing-model"]'))updateRoutingFields();
  saveSettings(section);
  // Only dependent option lists need rendering; range/checkbox saves keep focus.
  if(input.matches('[data-setting-model],[data-setting-rule-model],[data-setting="routing-model"],[data-setting="routing-mode"]'))render();
});
content.addEventListener("click",event=>{const restore=event.target.closest("[data-settings-restore]");if(restore){restoreSettingsDefaults(restore.dataset.settingsRestore);return}if(event.target.closest("[data-settings-retry]"))settingsSavers.get(state.project)?.retry()});

content.addEventListener("change",event=>{
  const input=event.target;
  if(input.matches("[data-job-field]")){jobEdit(input.dataset.job,input.dataset.jobField,input.value);return}
  if(input.matches("[data-harness-enabled],[data-harness-limit]")){saveSettings("harnesses");render()}
});
function updateRoutingFields(){const fixed=content.querySelector('[data-setting="routing-mode"]')?.value==="fixed",model=content.querySelector('[data-setting="routing-model"]');if(model)model.disabled=!fixed;content.querySelector('.settings-fixed')?.classList.toggle("is-disabled",!fixed)}

// Cross-app talk: roles settings wiring and the Crosstalk page.
// Every fetch here guards ok:false and network failure into an inline message; nothing throws into a blank page.
// crossTalkRolesFromDom() is the single mutation entry point for the roles dict: once
// touched, state.settingsDrafts.roles IS the draft (deep-cloned off state.settings.roles
// the first time), so every mutator below reads/writes through it instead of re-scanning
// DOM (which would be structurally stale immediately after add/remove/reorder, before the
// following render() runs).
function crossTalkRolesFromDom(){
  if(state.settingsDrafts.roles&&typeof state.settingsDrafts.roles==="object"&&!Array.isArray(state.settingsDrafts.roles))return state.settingsDrafts.roles;
  const base=state.settings?.roles;
  const cloned=base&&typeof base==="object"&&!Array.isArray(base)?JSON.parse(JSON.stringify(base)):{};
  state.settingsDrafts.roles=cloned;return cloned;
}
function crossTalkSetRoleField(name,field,value){const roles=crossTalkRolesFromDom(),role=roles[name];if(!role)return;role[field]=value;saveSettings("roles")}
function crossTalkSetRoleOptionField(name,index,field,value){const roles=crossTalkRolesFromDom(),role=roles[name],option=role?.options?.[index];if(!option)return;option[field]=value;if(field==="harness"){option.model="";option.effort=null}saveSettings("roles")}
content.addEventListener("change",event=>{
  const input=event.target;
  if(input.matches('[data-role-autonomy]'))crossTalkSetRoleField(input.dataset.roleAutonomy,"autonomy",input.value);
  if(input.matches('[data-role-preamble]'))crossTalkSetRoleField(input.dataset.rolePreamble,"preamble",input.value);
  if(input.matches('[data-role-description]'))crossTalkSetRoleField(input.dataset.roleDescription,"description",input.value);
  if(input.matches('[data-role-edits]'))crossTalkSetRoleField(input.dataset.roleEdits,"edits",input.value);
  if(input.matches('[data-role-brief-template]'))crossTalkSetRoleField(input.dataset.roleBriefTemplate,"brief_template",input.value);
  if(input.matches('[data-role-hooks-enabled]'))crossTalkSetHooksRoleEnabled(input.dataset.roleHooksEnabled,input.checked);
  if(input.matches('[data-hooks-trigger-enabled]'))crossTalkSetHooksTriggerEnabled(input.dataset.hooksTriggerEnabled,input.checked);
  if(input.matches('[data-hooks-field]'))saveSettings("hooks");
  if(input.matches('[data-hooks-editing-set]')){state.hooksEditingSet=input.value;render()}
  if(input.matches('[data-hooks-set-text]'))crossTalkSetHooksSetText(input.dataset.hooksSetName,input.dataset.hooksSetRole,input.dataset.hooksSetTrigger,input.value);
  if(input.matches('[data-role-option-field]')){
    const name=input.dataset.roleName,index=Number(input.dataset.roleOptionIndex),field=input.dataset.roleOptionField;
    crossTalkSetRoleOptionField(name,index,field,input.value);
    if(field==="harness"||field==="model")render(); // effort choices depend on the exact model
  }
  if(input.matches('[data-role-name-input]'))crossTalkRenameRole(input.dataset.roleNameInput,input.value);
});
content.addEventListener("click",event=>{
  if(event.target.closest('[data-role-add]')){
    const nameInput=content.querySelector('[data-role-new-name]'),desired=(nameInput?.value||"").trim();
    crossTalkAddRole(desired||undefined);if(nameInput)nameInput.value="";
  }
  const deleteRole=event.target.closest('[data-role-delete]');if(deleteRole)crossTalkDeleteRole(deleteRole.dataset.roleDelete);
  const addOption=event.target.closest('[data-role-option-add]');if(addOption)crossTalkAddRoleOption(addOption.dataset.roleOptionAdd);
  const removeOption=event.target.closest('[data-role-option-remove]');if(removeOption)crossTalkRemoveRoleOption(removeOption.dataset.roleName,Number(removeOption.dataset.roleOptionIndex));
  const duplicateSet=event.target.closest('[data-hooks-set-duplicate]');if(duplicateSet)crossTalkAddHooksSet(duplicateSet.dataset.hooksSetDuplicate);
  const renameSet=event.target.closest('[data-hooks-set-rename]');
  if(renameSet){const nameInput=content.querySelector('[data-hooks-set-rename-input]');crossTalkRenameHooksSet(renameSet.dataset.hooksSetRename,nameInput?.value||"");if(nameInput)nameInput.value=""}
  const deleteSet=event.target.closest('[data-hooks-set-delete]');if(deleteSet)crossTalkDeleteHooksSet(deleteSet.dataset.hooksSetDelete);
  const addSetRole=event.target.closest('[data-hooks-set-add-role]');
  if(addSetRole){const nameInput=content.querySelector('[data-hooks-set-add-role-input]'),roleName=(nameInput?.value||"").trim();if(roleName){crossTalkSetHooksSetText(addSetRole.dataset.hooksSetAddRole,roleName,"message",null);if(nameInput)nameInput.value="";render()}}
  const ctAction=event.target.closest('[data-crosstalk-action]');if(ctAction)crosstalkOwnerAction(ctAction.dataset.crosstalkAction);
  if(event.target.closest('[data-crosstalk-refresh]'))loadCrosstalkOwner(true);
});
// Drag reorder is scoped to one role's option rows: a drop under a different role's
// card than the one dragging started in is ignored rather than moving options across
// roles (which validate_roles has no concept of -- an option belongs to exactly one role).
content.addEventListener("dragstart",event=>{const row=event.target.closest('[data-role-option-row]');if(!row)return;state.crossTalkRoleDraft={name:row.dataset.roleName,index:Number(row.dataset.roleOptionRow)};event.dataTransfer?.setData('text/plain','role-option');row.classList.add('is-dragging')});
content.addEventListener("dragend",event=>{event.target.closest('[data-role-option-row]')?.classList.remove('is-dragging');state.crossTalkRoleDraft=null});
content.addEventListener("dragover",event=>{if(event.target.closest('[data-role-option-row]')&&state.crossTalkRoleDraft)event.preventDefault()});
content.addEventListener("drop",event=>{
  const row=event.target.closest('[data-role-option-row]');if(!row||!state.crossTalkRoleDraft)return;event.preventDefault();
  const name=row.dataset.roleName,to=Number(row.dataset.roleOptionRow),from=state.crossTalkRoleDraft.index,draftName=state.crossTalkRoleDraft.name;
  state.crossTalkRoleDraft=null;if(draftName!==name)return;crossTalkReorderRoleOptions(name,from,to);
});
function crossTalk(p){
  crosstalkOwnerLoadOnce();
  return `<div class="cross-talk-page">${heading("Cross-app talk","Crosstalk status and controls. Letters are metadata only here.")}${crosstalkOwnerCard()}</div>`;
}
// Crosstalk (tools/crosstalk): owner controls and recent letters, from /api/v1/crosstalk/*.
// Metadata only (ids, from/to keys, state, time); never a letter body. Stop/Resume write
// Crosstalk's own controls through its CLI.
function crosstalkOwnerLoadOnce(){const c=state.crossTalk;if(!c.ct&&!c.ctLoading&&!c.ctError)queueMicrotask(()=>loadCrosstalkOwner())}
async function loadCrosstalkOwner(force=false){
  if(state.crossTalk.ctLoading)return;const requestedProject=state.project;state.crossTalk.ctLoading=true;if(force)render();
  try{const response=await fetch(`/api/v1/crosstalk/status?project=${encodeURIComponent(requestedProject)}`,{cache:"no-store"});const data=await response.json();
    if(!response.ok||data?.ok===false)throw new Error(data?.error||`Crosstalk status returned ${response.status}`);
    if(state.project!==requestedProject)return;state.crossTalk.ct=data;state.crossTalk.ctError=null;
  }catch(error){if(state.project===requestedProject){state.crossTalk.ct=null;state.crossTalk.ctError=error.message}}
  finally{state.crossTalk.ctLoading=false;if(state.project===requestedProject&&["settings","cross-talk"].includes(state.view))render()}
}
async function crosstalkOwnerAction(action){
  const token=state.snapshot?.settings_token;if(!token){toast("Settings token unavailable; refresh the dashboard");return}
  state.crossTalk.ctMessage="Working…";render();
  try{
    const response=await fetch("/api/v1/crosstalk/action",{method:"POST",headers:{"Content-Type":"application/json","X-Eutonos-Settings":token},body:JSON.stringify({project:state.project,action})});
    const data=await response.json();
    if(!response.ok||data?.ok===false)throw new Error(data?.message||data?.error||`Action returned ${response.status}`);
    state.crossTalk.ctMessage={halt:"Crosstalk halted.",resume:"Crosstalk resumed.",enable:"Crosstalk messaging on.",off:"Crosstalk messaging off."}[action]||"Done.";
    loadCrosstalkOwner(true);
  }catch(error){state.crossTalk.ctMessage=`Action failed: ${error.message}`}
  finally{render()}
}
function crosstalkOwnerCard(){
  const c=state.crossTalk,d=c.ct;
  if(!d)return `<section class="card" data-crosstalk-owner><h2>Crosstalk</h2><p class="quiet">${c.ctError?`Status unavailable: ${esc(c.ctError)}`:"Reading Crosstalk status…"}</p></section>`;
  const msg=c.ctMessage&&typeof c.ctMessage==="string"?`<p class="quiet cross-talk-action-note">${esc(c.ctMessage)}</p>`:"";
  if(!d.installed)return `<section class="card" data-crosstalk-owner><h2>Crosstalk</h2><p class="quiet">${esc(d.summary)}.</p></section>`;
  const letters=Array.isArray(d.recent_letters)?d.recent_letters:[];
  const rows=letters.map(l=>`<div class="list-item"><div><strong>${esc(l.letter_id)}</strong><p>${esc(l.from||"?")} → ${esc(l.to||"?")} · ${l.ts_ns?esc(when(Math.floor(l.ts_ns/1e6))):"time unknown"}</p></div>${tag(l.state||"unknown",l.state==="arrived"?"good":"")}</div>`).join("");
  return `<section class="card" data-crosstalk-owner><div class="section-head"><div><h2>Crosstalk</h2><p class="quiet">The cross-app letter route. These controls write Crosstalk's own off, halt and deny state.</p></div></div>`+
    `<div class="cross-talk-status"><span class="status ${d.messaging==="on"&&!d.halted?"good":"warn"}">${esc(d.summary)}</span><div class="cross-talk-status-actions">`+
    `<button type="button" data-crosstalk-action="${d.halted?"resume":"halt"}">${d.halted?"Resume Crosstalk":"Stop Crosstalk"}</button>`+
    `<button type="button" class="secondary" data-crosstalk-action="${d.messaging==="on"?"off":"enable"}">${d.messaging==="on"?"Turn messaging off":"Turn messaging on"}</button>`+
    `<button type="button" class="secondary" data-crosstalk-refresh ${c.ctLoading?"disabled":""}>${c.ctLoading?"Reading…":"Refresh"}</button></div></div>${msg}`+
    `<div class="list">${rows||'<p class="quiet">No letters recorded yet.</p>'}</div></section>`;
}
function system(p){const modules=Object.entries(p.modules);return heading("System","See what is installed, observable and still unproven.")+`<div class="grid two"><section class="card"><div class="section-head"><h2>Capabilities</h2><span class="quiet">Selected repository</span></div><div class="list">${modules.map(([name,value])=>`<div class="list-item"><div><strong>${esc(name.replaceAll("_"," "))}</strong><p>${esc(value.message||value.reason||moduleSummary(name,value))}</p></div>${tag(value.status,value.status==="available"?"good":value.status==="error"?"warn":"")}</div>`).join("")}</div></section><section class="card"><div class="section-head"><h2>Source boundary</h2></div><div class="list"><div class="list-item"><div><strong>View</strong><p>${esc(p.source.kind)} · local overlay ${esc(p.source.local_overlay)}</p></div>${tag(p.source.dirty?"Dirty":"Clean",p.source.dirty?"warn":"good")}</div><div class="list-item"><div><strong>Revision</strong><p>${esc(p.source.commit||"Unavailable")}</p></div>${tag("Observed")}</div><div class="list-item"><div><strong>Snapshot</strong><p>${esc(when(p.generated_at))}</p></div>${tag("Refreshable")}</div></div></section></div>`}
const documentFilters={};
function docFilter(view){const key=state.project+":"+view;return documentFilters[key]||(documentFilters[key]={query:"",type:"",area:"",module:"",from:"",to:"",undated:true})}
function filteredDocs(items,view){const f=docFilter(view);return items.filter(i=>{const d=i.display||{},when=d.display_date;return (!f.query||[i.id,i.title,i.summary].join(" ").toLowerCase().includes(f.query.toLowerCase()))&&(!f.type||i.type===f.type)&&(!f.area||d.area===f.area)&&(!f.module||d.module===f.module)&&(!f.from||when&&when>=f.from||!when&&f.undated)&&(!f.to||when&&when<=f.to||!when&&f.undated)&&(when||f.undated)})}
function docControls(items,view){const f=docFilter(view),select=(key,label,values)=>`<label>${label}<select data-doc-filter="${key}" data-doc-view="${view}"><option value="">All</option>${[...new Set(values.filter(Boolean))].sort().map(x=>`<option value="${esc(x)}" ${f[key]===x?"selected":""}>${esc(x)}</option>`).join("")}</select></label>`;return `<form class="card doc-controls" data-doc-form="${view}"><label>Search<input name="query" type="search" value="${esc(f.query)}" placeholder="Title, ID or description"></label>${select("type","Category",items.map(i=>i.type))}${select("area","Area",items.map(i=>i.display?.area))}${select("module","Module",items.map(i=>i.display?.module))}<label>From<input name="from" type="date" value="${esc(f.from)}"></label><label>Through<input name="to" type="date" value="${esc(f.to)}"></label><label class="doc-undated"><input name="undated" type="checkbox" ${f.undated?"checked":""}>Include undated</label><button type="submit">Apply</button><button type="button" data-doc-clear="${view}">Clear</button></form>`}
function docMetadata(i){const d=i.display||{};return `<div class="meta">${d.area?tag(d.area):""}${d.module?tag(d.module):""}${tag(d.display_date?d.date_kind+" "+d.display_date:"Date not recorded")}</div>`}
function changeRecorded(i){const d=i.display||{},date=d.display_date,raw=typeof d.display_time==="string"?d.display_time:typeof i.recorded_at==="string"&&i.recorded_at.includes("T")?i.recorded_at:"",parsed=Date.parse(raw),time=raw?(Number.isFinite(parsed)?new Date(parsed).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"}):raw):"",text=date?`${d.date_kind||"Recorded"} ${date}${time?" · "+time:""}`:"Date not recorded",datetime=raw||date||"";return `<div class="meta change-recorded">${datetime?`<time datetime="${esc(datetime)}">${esc(text)}</time>`:esc(text)}</div>`}
function changes(p){const data=p.modules.changes||{},all=data.items||[],items=filteredDocs(all,"changes");return heading("Changelog","Meaningful changes from the native changelog. Recorded completion is separate from publication and deployment.")+docControls(all,"changes")+`<p>${items.length} of ${all.length} recorded changes</p>${(data.diagnostics||[]).length?`<section class="card"><h2>Coverage notes</h2>${data.diagnostics.map(x=>`<p>${esc(x)}</p>`).join("")}</section>`:""}<div class="change-feed">${items.map(i=>`<article class="card">${changeRecorded(i)}<div class="meta">${tag(i.id)}${tag(i.status)}</div><h2>${esc(i.title)}</h2><p>${esc(i.summary)}</p>${docMetadata(i)}<details data-persist="change-${esc(i.id)}"><summary>Source record</summary><p class="board-source">${esc(i.owner_path)} · ${esc(i.id)}</p></details></article>`).join("")||empty(all.length?"No matching changes":"No changes recorded yet",all.length?"Adjust the filters or include undated records.":"Closeout can file a short change entry; the dashboard reads it on refresh.")}</div>`}
content.addEventListener("submit",e=>{const form=e.target.closest("[data-doc-form]");if(!form)return;e.preventDefault();const f=docFilter(form.dataset.docForm),v=new FormData(form);for(const k of ["query","from","to"])f[k]=String(v.get(k)||"");f.undated=v.has("undated");for(const el of form.querySelectorAll("[data-doc-filter]"))f[el.dataset.docFilter]=el.value;render()});
content.addEventListener("click",e=>{const b=e.target.closest("[data-doc-clear]");if(b){delete documentFilters[state.project+":"+b.dataset.docClear];render()}});
const catalogFilters=new Map();
function catalogCategory(item){return typeof item.extensions?.category==="string"&&item.extensions.category.trim()?item.extensions.category.trim():"Uncategorized"}
function catalogDate(item,horizon){const x=item.extensions||{};return horizon==="current"?(x.implemented_on||x.completed_on||null):(x.recorded_on||x.date||null)}
function catalogHorizon(item){return item.status==="completed"?"current":["cancelled","superseded"].includes(item.status)?"historical":["active","blocked"].includes(item.status)?"active":"future"}
function catalogSelection(all,type,horizon,filter){
  const selected=all.filter(i=>i.type===type&&catalogHorizon(i)===horizon);
  const items=selected.filter(i=>(!filter.query||[i.title,i.summary,i.id].join(" ").toLowerCase().includes(filter.query.toLowerCase()))&&(!filter.category||catalogCategory(i)===filter.category));
  const order=filter.direction==="desc"?-1:1;
  items.sort((a,b)=>{
    let left,right;
    if(filter.sort==="date"){left=catalogDate(a,horizon);right=catalogDate(b,horizon);if(!left||!right)return left?-1:right?1:a.title.localeCompare(b.title)}
    else if(filter.sort==="category"){left=catalogCategory(a);right=catalogCategory(b)}
    else{left=a.title;right=b.title}
    return order*left.localeCompare(right,undefined,{numeric:true,sensitivity:"base"})||a.title.localeCompare(b.title)||a.id.localeCompare(b.id);
  });
  return {selected,items};
}
function catalogList(board,section){
  const horizon=state.boardsHorizon,type=section==="features"?"feature":"capability",key=`${state.project}:${section}:${horizon}`;
  if(!catalogFilters.has(key))catalogFilters.set(key,{query:"",category:"",sort:"name",direction:"asc"});
  const f=catalogFilters.get(key),{selected,items}=catalogSelection(board.items||[],type,horizon,f),categories=[...new Set(selected.map(catalogCategory))].sort();
  const title=section==="features"?"Features":"Capabilities",dateLabel=horizon==="current"?"Date implemented":"Date added";
  const option=(value,label,current)=>`<option value="${esc(value)}" ${value===current?"selected":""}>${esc(label)}</option>`;
  const titles=new Map((board.items||[]).map(x=>[x.id,x.title])),uses=i=>{const ids=Array.isArray(i.extensions?.features)?i.extensions.features:[];return i.type==="capability"&&ids.length?`<p class="catalog-uses"><span class="quiet">Uses</span> ${ids.map(id=>esc(titles.get(id)||id)).join(", ")}</p>`:""};
  return heading(title,section==="features"?"What EUTONOS has today, and ideas for what comes next.":"What you can do with EUTONOS today, and what is planned.")+
    `<div class="tabs board-horizon" role="group" aria-label="${title} lists"><button data-board-horizon="current" aria-pressed="${horizon==="current"}">Current</button><button data-board-horizon="future" aria-pressed="${horizon==="future"}">Future</button></div>`+
    `<form class="card catalog-controls" data-catalog-form="${key}"><label>Search<input name="query" type="search" value="${esc(f.query)}" placeholder="Name or description"></label><label>Category<select name="category">${option("","All categories",f.category)}${categories.map(c=>option(c,c,f.category)).join("")}</select></label><label>Sort by<select name="sort">${option("name","Name",f.sort)}${option("date",dateLabel,f.sort)}${option("category","Category",f.sort)}</select></label><label>Order<select name="direction">${option("asc",f.sort==="date"?"Oldest first":"A–Z",f.direction)}${option("desc",f.sort==="date"?"Newest first":"Z–A",f.direction)}</select></label><button type="submit">Search</button><button type="button" data-catalog-clear="${key}">Clear</button></form>`+
    `<div class="catalog-count"><strong>${items.length===selected.length?num(items.length):`${num(items.length)} of ${num(selected.length)}`} ${horizon} ${section}</strong><span>${horizon==="current"?"Implemented in the recorded scope":"Ideas not started yet. Work in progress is listed under Current Tasks"}</span></div>`+
    `<section class="card catalog-list" aria-label="${horizon} ${section}"><div class="catalog-columns" aria-hidden="true"><span>Name</span><span>Category</span><span>${dateLabel}</span></div>${items.map(i=>{const date=catalogDate(i,horizon);return `<article class="catalog-item"><div><h2>${esc(i.title)}</h2><p>${esc(i.summary)}</p>${uses(i)}</div><div class="catalog-category"><span class="catalog-mobile-label">Category</span>${tag(catalogCategory(i))}</div><div class="catalog-date"><span class="catalog-mobile-label">${dateLabel}</span>${date?`<time datetime="${esc(date)}">${esc(date)}</time>`:'<span class="quiet">Not recorded</span>'}<details data-persist="catalog-${esc(i.id)}"><summary>Source and evidence</summary><p class="board-source">${esc(i.owner_path)} · ${esc(i.id)}</p>${(i.evidence_refs||[]).map(r=>`<p class="board-source">${esc(r.path||JSON.stringify(r))}</p>`).join("")}${completionChecks(i)}${horizon==="future"&&i.next_action?`<p>${esc(i.next_action)}</p>`:""}</details></div></article>`}).join("")||'<p class="quiet catalog-empty">No matching items. Try another search or category.</p>'}</section>`+
    ((board.diagnostics||[]).length?`<details class="card"><summary>Some records could not be loaded</summary>${board.diagnostics.map(x=>`<p>${esc(typeof x==="string"?x:JSON.stringify(x))}</p>`).join("")}</details>`:"");
}
function updateCatalogForm(form){const f=catalogFilters.get(form.dataset.catalogForm);if(!f)return;const values=new FormData(form);for(const k of ["query","category","sort","direction"])f[k]=String(values.get(k)||"");render()}
content.addEventListener("submit",event=>{const form=event.target.closest("[data-catalog-form]");if(form){event.preventDefault();updateCatalogForm(form)}});
content.addEventListener("change",event=>{const form=event.target.closest("[data-catalog-form]");if(form&&event.target.tagName==="SELECT")updateCatalogForm(form)});
content.addEventListener("click",event=>{const clear=event.target.closest("[data-catalog-clear]");if(clear){catalogFilters.delete(clear.dataset.catalogClear);render()}});
// What EUTONOS offers: EUTONOS's own four lists, read from the installed package (never from an open project).
const offersSections=[
  {key:"current-feature",type:"feature",horizon:"current",title:"Current features",intro:"What EUTONOS does today."},
  {key:"future-feature",type:"feature",horizon:"future",title:"Future features",intro:"Ideas that are planned or on hold. Work that has started is marked in progress."},
  {key:"current-capability",type:"capability",horizon:"current",title:"Current capabilities",intro:"What you can do with EUTONOS today, with the features each one uses."},
  {key:"future-capability",type:"capability",horizon:"future",title:"Future capabilities",intro:"What is planned, with the features each one would use."}
];
function offersStatusTag(item){return item.horizon==="future"?tag(item.status==="in progress"?"In progress":item.status==="deferred"?"On hold":"Planned",item.status==="in progress"?"good":""):""}
function offersItem(item,titles){
  const uses=item.type==="capability"?(item.features||[]).map(id=>titles.get(id)).filter(Boolean):[];
  return `<article class="offers-item"><div class="offers-item-head"><h4>${esc(item.title)}</h4>${offersStatusTag(item)}</div><p>${esc(item.summary)}</p>${uses.length?`<p class="catalog-uses"><span class="quiet">Uses</span> ${uses.map(esc).join(", ")}</p>`:""}</article>`;
}
function offersSection(section,items,titles){
  const mine=items.filter(i=>i.type===section.type&&i.horizon===section.horizon),groups=new Map();
  mine.forEach(i=>{if(!groups.has(i.category))groups.set(i.category,[]);groups.get(i.category).push(i)});
  const body=[...groups].map(([category,rows])=>`<section class="card offers-group" aria-label="${esc(category)}"><h3>${esc(category)} <span class="quiet">${num(rows.length)}</span></h3>${rows.map(i=>offersItem(i,titles)).join("")}</section>`).join("")||'<p class="quiet catalog-empty">Nothing is listed here yet.</p>';
  return `<section class="offers-section" id="offers-${section.key}" aria-labelledby="offers-${section.key}-title"><div class="catalog-count"><h2 id="offers-${section.key}-title">${esc(section.title)} <span class="quiet">${num(mine.length)}</span></h2><span>${esc(section.intro)}</span></div>${body}</section>`;
}
function offersPage(){
  const o=state.offers,head=heading("What EUTONOS offers","The features and capabilities EUTONOS has today and the ones planned next. This list comes with EUTONOS itself, so it is the same whichever project is open.");
  if(o.error)return head+empty("The list is unavailable",o.error)+'<p><button type="button" data-offers-retry>Try again</button></p>';
  if(!o.data)return head+empty("Loading the list","Reading what EUTONOS offers...");
  const items=o.data.items||[],titles=new Map(items.map(i=>[i.id,i.title]));
  return head+`<div class="tabs offers-jump" role="group" aria-label="Jump to a list">${offersSections.map(s=>`<button type="button" data-offers-jump="${s.key}">${esc(s.title)}</button>`).join("")}</div>`+offersSections.map(s=>offersSection(s,items,titles)).join("");
}
async function loadOffers(force=false){
  const o=state.offers;if(o.loading||(o.data&&!force))return;
  o.loading=true;o.error=null;
  try{
    const response=await fetch("/api/v1/offers",{cache:"no-store"}),body=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(body.message||`Dashboard service returned ${response.status}`);
    if(!body||!Array.isArray(body.items))throw new Error("The list of what EUTONOS offers could not be read.");
    o.data=body;
  }catch(error){o.error=error.message||"The list could not be loaded."}
  finally{o.loading=false;if(state.view==="offers")renderOffers()}
}
function renderOffers(){
  document.querySelectorAll("#primary-nav a[data-view]").forEach(a=>a.setAttribute("aria-current",a.dataset.view==="offers"?"page":"false"));
  syncSettingsNav();syncBoardsNav();
  content.innerHTML=offersPage();
  if(!state.offers.data&&!state.offers.loading&&!state.offers.error)loadOffers();
}
content.addEventListener("click",event=>{
  const jump=event.target.closest("[data-offers-jump]");
  if(jump){document.getElementById(`offers-${jump.dataset.offersJump}`)?.scrollIntoView?.({block:"start"});return}
  if(event.target.closest("[data-offers-retry]")&&state.view==="offers"){state.offers.error=null;renderOffers()}
});
function progressInfo(item){const p=item.extensions?.progress,stopped=["cancelled","superseded"].includes(item.status);if(item.status==="completed"||item.status==="archived"&&item.extensions?.archived===true&&!stopped)return {percent:100,basis:p?.basis,title:p?.title};if(Number.isInteger(p?.percent)&&p.percent>=0&&p.percent<=100)return {percent:p.percent,basis:p.basis,title:p.title,stopped};return {percent:null,basis:p?.basis,title:p?.title,stopped}}
function progressBar(item,label){const p=progressInfo(item),complete=item.status==="completed",text=complete?"100% complete":p.stopped?(p.percent===null?"Stopped · not estimated":`Stopped at ${p.percent}%`):p.percent===null?"Not estimated":`${p.percent}% estimated`,extra=[p.title,p.basis].filter(Boolean).join(" · ");return `<div class="work-progress" title="${esc(extra||text)}"><div class="work-progress-label"><span>${esc(text)}</span>${extra?`<span class="quiet">${esc(extra)}</span>`:""}</div>${p.percent===null?`<div class="progress-unknown" role="img" aria-label="${esc(p.stopped?"Stopped progress not estimated":"Progress not estimated")}"></div>`:`<progress max="100" value="${p.percent}" aria-label="${esc(label||item.title||item.id)} progress: ${esc(text)}"></progress>`}</div>`}
function completionCheckReference(ref){return esc(ref&&typeof ref==="object"?(ref.path||JSON.stringify(ref)):ref)}
function completionCheckVerifier(verification){
  if(!verification||typeof verification!=="object")return "";
  const actor=verification.actor||verification.writer,person=actor?.human?attributionPerson(actor.human):typeof actor==="string"?actor:null;
  const context=actor?.context||{};
  return [person?`Verified by ${person}`:null,context.app?`App: ${context.app}`:null,context.model?`Model: ${context.model}`:null,context.session?`Session: ${context.session}`:null,actor?.recorded_at?`Recorded: ${actor.recorded_at}`:null,verification.definition_sha256?`Definition: ${verification.definition_sha256}`:null].filter(Boolean).map(esc).join(" · ");
}
function completionChecks(item){
  const checks=item?.extensions?.completion_checks;
  const counts=item?.completion_check_summary;
  if((!Array.isArray(checks)||!checks.length)&&!counts?.total)return "";
  const rows=Array.isArray(checks)?checks:[],required=rows.filter(check=>check.required===true),verified=required.filter(check=>check.state==="verified").length;
  const total=counts?.total??rows.length,requiredTotal=counts?.required_total??required.length,requiredVerified=counts?.required_verified??verified;
  const summary=`${requiredVerified} of ${requiredTotal} required checks verified${total>requiredTotal?` · ${total-requiredTotal} optional`:""}`;
  if(!rows.length)return `<p class="completion-check-summary">${esc(summary)}</p>`;
  const states={pending:"Pending",in_progress:"In progress",partial:"Partial",blocked:"Blocked",verified:"Verified"};
  const details=rows.map(check=>{
    const refs=Array.isArray(check.evidence)?check.evidence:[],related=Array.isArray(check.related_work_ids)?check.related_work_ids:[],verifier=completionCheckVerifier(check.verification);
    return `<li><strong>${esc(check.title||check.id||"Untitled check")}</strong> ${tag(states[check.state]||check.state||"Unknown",check.state==="verified"?"good":check.state==="blocked"?"warn":"")}<span class="quiet">${check.required===true?"Required":"Optional"}${check.id?` · ${esc(check.id)}`:""}</span>${refs.length?`<div>Evidence: ${refs.map(completionCheckReference).join(" · ")}</div>`:""}${check.remaining_action?`<div>Remaining: ${esc(check.remaining_action)}</div>`:""}${related.length?`<div>Related work: ${related.map(esc).join(", ")}</div>`:""}${verifier?`<div>${verifier}</div>`:""}</li>`;
  }).join("");
  return `<details class="completion-checks" data-persist="checks-${esc(item.id)}"><summary>${esc(summary)}</summary><ol>${details}</ol></details>`;
}
function workAttributionSummary(item){
  const labels=item?.attribution?.labels||{},workers=Array.isArray(labels.workers)?labels.workers.filter(value=>typeof value==="string"&&value.trim()):[],current=typeof project==="function"?project():null,coverage=current?.modules?.current_work?.attempts?.coverage||"unknown";
  const active=workers.length?`Active claimants observed: ${workers.join(", ")}${coverage==="complete"?"":` · ${coverage} coverage`}`:coverage==="complete"?"No active claimants":`Active claimants unknown${coverage==="unknown"?"":` · ${coverage} coverage`}`;
  const completion=item?.status==="completed"?(labels.completion||"Completed by Unknown"):null,summary=[labels.creator||"Created by Unknown",active,completion].filter(Boolean).map(esc).join(" · ");
  return `<p class="work-attribution-summary quiet">${summary}</p>`;
}
function attributionPerson(human){return human?.full_name||[human?.given_name,human?.family_name].filter(value=>typeof value==="string"&&value.trim()).join(" ")||"Unknown"}
function attributionContext(event){const context=event?.context||{},parts=[];if(context.app)parts.push(`App: ${context.app}`);if(context.model)parts.push(`Model: ${context.model}`);if(context.session)parts.push(`Session: ${context.session}`);return parts.length?parts.map(esc).join(" · "):"App, model and session unknown"}
function attributionRecorded(event){const value=event?.recorded_at;return value?`<time datetime="${esc(value)}">${esc(when(value))}</time>`:"Time unknown"}
function attributionEvent(event){
  const kind=event?.kind||"recorded",titles={completed:"Completion",completion:"Completion",reviewed:"Review",review:"Review",integrated:"Integration",integration:"Integration"},title=titles[kind]||kind;
  const human=event?.human||null,person=`${esc(attributionPerson(human))}${human?.id?` · participant ${esc(human.id)}`:" · participant ID unknown"}`;
  const context=`<p class="quiet">${attributionContext(event)}</p>`,actor=`<p>By ${person} · ${attributionRecorded(event)}</p>`;
  let evidence="";
  if(kind==="reviewed"||kind==="review"){
    const binding=event.binding_scope==="source-original"?"Review is preserved from the source record; no current board-content comparison is asserted.":event.current===true?"Review binding matches the current board content.":event.current===false?"Review binding is stale for the current board content.":"Review binding status unknown.";
    evidence=`<p>${esc(binding)} Source status: ${esc(event.source_status||"unknown")} · source commit ${esc(event.source_commit||"unknown")} · reviewed revision ${esc(event.reviewed_revision||"unknown")}</p>${event.decision?`<p>Decision: ${esc(event.decision)}</p>`:""}`;
  }else if(kind==="integrated"||kind==="integration"){
    evidence=`<p>Ancestry verification: ${esc(event.verification_status||"unknown")} · target status: ${esc(event.target_status||"unknown")}</p><p>Source commit ${esc(event.source_commit||"unknown")} · target ${esc(event.target_ref||"unknown ref")} at ${esc(event.target_commit||"unknown commit")}</p>`;
  }else if(kind==="completed"||kind==="completion")evidence=`<p>Completed revision: ${esc(event.content_revision||event.completed_revision||"unknown")}</p>`;
  const notes=Array.isArray(event.evidence)?event.evidence.filter(value=>typeof value==="string").map(value=>`<li>${esc(value)}</li>`).join(""):"";
  return `<section class="work-attribution-event"><h4>${esc(title)}${event.event_id?` · ${esc(event.event_id)}`:""}</h4>${actor}${context}${evidence}${notes?`<ul>${notes}</ul>`:""}</section>`;
}
function workAttributionDetails(item){
  const attribution=item?.attribution;
  if(!attribution)return `<details class="work-attribution-details" data-persist="attribution-${esc(item?.id||"unknown")}"><summary>Attribution history</summary><p>Creator, contributors and sign-off history are unknown because no attribution projection is available.</p></details>`;
  const creator=attribution.creator,creatorHuman=creator?.human||null,creatorDetails=`<section class="work-attribution-event"><h4>Creator</h4><p>Created by ${esc(attributionPerson(creatorHuman))}${creatorHuman?.id?` · participant ${esc(creatorHuman.id)}`:" · participant ID unknown"} · ${attributionRecorded(creator)}</p><p class="quiet">${attributionContext(creator)}</p></section>`;
  const contributors=Array.isArray(attribution.contributors)?attribution.contributors.map(attributionEvent).join(""):"";
  const events=[],seen=new Set(),add=event=>{if(!event||typeof event!=="object")return;const key=event.event_id||`${event.kind||""}:${event.recorded_at||""}`;if(seen.has(key))return;seen.add(key);events.push(event)};
  for(const event of attribution.events||[])add(event);
  if(attribution.completion&&!Array.isArray(attribution.completion))add(attribution.completion);
  for(const event of attribution.reviews||[])add(event);
  for(const event of attribution.integrations||[])add(event);
  const history=events.filter(event=>event.kind!=="created").map(attributionEvent).join("")||"<p>No completion, review or integration sign-offs are recorded.</p>";
  const scopeNote=attribution.provenance_scope==="source-original"?"This attribution is preserved from the original source record. Importing it did not create a new creator or sign-off; source-original review evidence does not assert a current comparison to this board.":"These are recorded native provenance events. They do not grant approval; review freshness binds to board content, while source and target state remain separate.";
  return `<details class="work-attribution-details" data-persist="attribution-${esc(item?.id||"unknown")}"><summary>Creator and sign-off history</summary><p class="quiet">${esc(scopeNote)}</p><p>Board content revision: ${esc(attribution.content_revision||"unknown")}</p>${creatorDetails}${contributors?`<section><h4>Contributors</h4>${contributors}</section>`:""}${history}</details>`;
}
function boardCard(i,label=i.display_type||i.type){return `<article class="board-item" id="work-${esc(i.id)}" tabindex="-1"><div class="meta"><span class="tag" title="${esc(i.id)}">${esc(workCode(i)||shortWorkId(i.id))}</span>${tag(label)}${workStatusTag(i)}</div><h2>${esc(i.title)}</h2><p>${esc(i.summary)}</p>${workAttributionSummary(i)}${typeof progressBar==="function"?progressBar(i,i.title):""}${completionChecks(i)}${workStatusDetails(i)}${i.next_action?`<p class="board-next"><strong>Next:</strong> ${esc(i.next_action)}</p>`:""}${workAttributionDetails(i)}<details data-persist="board-${esc(i.id)}"><summary>Source and evidence</summary><p class="board-source">${esc(i.id)}</p><p class="board-source">${esc(i.owner_path)}</p>${(i.evidence_refs||[]).map(r=>`<p class="board-source">${esc(r.path||JSON.stringify(r))}</p>`).join("")}</details></article>`}
const workGroupOrder=["active","not-active","done","cancelled"];
const workGroupLabels={active:"Active","not-active":"Not active",done:"Done",cancelled:"Cancelled"};
function workGroup(item){if(["cancelled","superseded"].includes(item?.status))return "cancelled";if(item?.status==="completed"||item?.status==="archived"||item?.extensions?.archived===true)return "done";if(["active","blocked"].includes(item?.status))return "active";return "not-active"}
function workStatusEvent(item){const history=item?.extensions?.work_history;return Array.isArray(history)?[...history].reverse().find(event=>["cancelled","superseded"].includes(event?.status)):null}
function workStatusTag(item){if(item?.status==="superseded"){const replacement=item.extensions?.superseded_by;return replacement?`<a class="tag" href="#work-${esc(replacement)}">Replaced by ${esc(replacement)}</a>`:tag("Replaced")};if(item?.status==="cancelled")return tag("Cancelled");return tag(item?.status,item?.status==="completed"?"good":item?.status==="blocked"?"warn":"")}
function workStatusDetails(item){if(!["cancelled","superseded"].includes(item?.status))return "";const event=workStatusEvent(item),date=event?.date||item.extensions?.progress?.updated_on,reason=event?.why||event?.result;return `<p class="quiet work-cancelled">${date?`Cancelled ${esc(date)}`:"Cancellation date not recorded"}${reason?` · ${esc(reason)}`:" · Reason not recorded"}</p>`}
function workQueued(item){return ["feature","capability"].includes(item?.type)&&["active","blocked"].includes(item?.status)&&!item?.parent_id}
function workKindTag(item){return ["feature","capability"].includes(item?.type)?tag(item.type):""}
function stageSkipped(stage){return ["cancelled","superseded"].includes(stage?.status)}
function workCodePattern(){return /\s*\(([A-Z][A-Z0-9]{1,4})\)\s*$/}
function workCode(item){const match=workCodePattern().exec(String(item?.title||""));return item?.type==="campaign"&&match?match[1]:""}
function shortWorkId(id){const text=String(id||""),match=/^([a-z]+)-([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.exec(text);return match?match[1]+"-"+match[2]:text}
function workCodes(items){const byOwner=new Map();for(const item of items||[]){const code=workCode(item);if(!code||!item.owner_path)continue;byOwner.set(item.owner_path,byOwner.has(item.owner_path)&&byOwner.get(item.owner_path)!==code?"":code)}return byOwner}
function workLabel(item,codes){const own=workCode(item),code=own||(codes?.get(item?.owner_path)||""),title=own?String(item.title).replace(workCodePattern(),""):String(item?.title||"");return `<span class="work-id" title="${esc(item?.id)}">${esc(code||shortWorkId(item?.id))}</span> · ${esc(title)}`}
function stageRow(stage){const done=stage.status==="completed",skipped=stageSkipped(stage);return `<div class="slice-row${skipped?" skipped":""}" id="work-${esc(stage.id)}" tabindex="-1"><span class="slice-check ${done?"done":skipped?"skipped":""}" aria-hidden="true">${done?"✓":skipped?"–":""}</span><div class="slice-row-content"><span class="slice-row-title">${workLabel(stage)}${skipped?` <span class="quiet">(${esc(stage.status)})</span>`:`<span class="sr-only"> · ${esc(stage.status)}</span>`}</span>${completionChecks(stage)}${workAttributionSummary(stage)}${workAttributionDetails(stage)}</div></div>`}
function stageCountLabel(stages){const skipped=stages.filter(stageSkipped).length,counted=stages.length-skipped,done=stages.filter(i=>i.status==="completed").length;return `${done} / ${counted} stages completed${skipped?` · ${skipped} ${skipped===1?"stage":"stages"} cancelled`:""}`}
function boardPaging(page,count,size,attribute){const pages=Math.max(1,Math.ceil(count/size)),current=Math.min(page,pages-1);return pages>1?`<nav class="board-paging" aria-label="Result pages"><button type="button" ${attribute}="${current-1}" ${current===0?"disabled":""}>Previous</button><span>Page ${current+1} of ${pages}</span><button type="button" ${attribute}="${current+1}" ${current===pages-1?"disabled":""}>Next</button></nav>`:""}
function workList(all,kind){const group=state.sliceStatus,items=all.filter(i=>kind==="slices"?i.type==="slice"&&!i.parent_id:["quick","task"].includes(i.type)&&!i.parent_id).filter(i=>workGroup(i)===group),toggle=`<div class="tabs work-state-toggle" role="group" aria-label="${kind==="slices"?"Slice":"Quick work"} status">${workGroupOrder.map(name=>`<button data-slice-status="${name}" aria-pressed="${group===name}">${workGroupLabels[name]}</button>`).join("")}</div>`,title=kind==="slices"?"Slices":"Quick work",copy=kind==="slices"?"Substantial standalone chunks of work, separate from campaigns.":"Small recorded edits that do not need a slice or campaign.";return heading(title,copy)+toggle+`<p>${num(items.length)} ${workGroupLabels[group].toLowerCase()} ${kind==="slices"?"slices":"items"}</p><div class="board-list">${items.map(i=>boardCard(i,kind==="slices"?"slice":"quick work")).join("")||empty(`No ${workGroupLabels[group].toLowerCase()} ${kind==="slices"?"slices":"quick work"}`,"The selected board owners have no recorded items in this group.")}</div><p class="quiet">Every item has a required description in its native board record.</p>`}
function campaignDateInfo(item){const x=item.extensions||{},src=x.source_import?.source_item||{},sx=src.extensions||{};const created=item.created_on||x.created_on||src.created_on||sx.created_on,recorded=item.recorded_on||x.recorded_on||src.recorded_on||sx.recorded_on||item.date;return created?{kind:"Created",date:created}:recorded?{kind:"Recorded",date:recorded}:null}
function campaignDate(item){return campaignDateInfo(item)?.date||null}
function sortedCampaigns(all,group,sort=state.campaignSort||"priority"){
  const campaigns=all.filter(i=>i.type==="campaign"&&workGroup(i)===group);
  campaigns.sort((a,b)=>{if(sort==="priority"){const laneRank=i=>i.work_lane==="current"?0:i.work_lane==="next"?1:2;const laneOrder=laneRank(a)-laneRank(b);if(laneOrder)return laneOrder}if(sort==="name")return (a.title||"").localeCompare(b.title||"",undefined,{numeric:true,sensitivity:"base"})||a.id.localeCompare(b.id);if(sort==="date"){const ad=campaignDate(a),bd=campaignDate(b);return (ad?0:1)-(bd?0:1)||(ad||"").localeCompare(bd||"")||a.id.localeCompare(b.id)}return (Number.isInteger(a.priority)?a.priority:Infinity)-(Number.isInteger(b.priority)?b.priority:Infinity)||(a.title||"").localeCompare(b.title||"",undefined,{numeric:true,sensitivity:"base"})||a.id.localeCompare(b.id)});
  return campaigns;
}
function campaignListV2(p){
  const all=p.modules.boards?.items||[],group=state.workHorizon,sort=state.campaignSort||"priority",campaigns=sortedCampaigns(all,group,sort),size=50,pages=Math.max(1,Math.ceil(campaigns.length/size));
  state.campaignPage=Math.min(state.campaignPage,pages-1);
  const shown=campaigns.slice(state.campaignPage*size,(state.campaignPage+1)*size),toggle=`<div class="tabs work-horizon" role="group" aria-label="Campaign status">${workGroupOrder.map(name=>`<button data-work-horizon="${name}" aria-pressed="${group===name}">${workGroupLabels[name]}</button>`).join("")}</div><label class="work-sort">Sort campaigns <select data-campaign-sort><option value="priority" ${sort==="priority"?"selected":""}>Saved priority</option><option value="name" ${sort==="name"?"selected":""}>Name</option><option value="date" ${sort==="date"?"selected":""}>Created or recorded date</option></select></label>`;
  const card=campaign=>{
    const stages=all.filter(i=>i.parent_id===campaign.id&&["stage","slice"].includes(i.display_type||i.type));
    return `<article class="card campaign-row" id="work-${esc(campaign.id)}" tabindex="-1"><div class="campaign-main"><div class="meta">${tag(campaign.id)}${workStatusTag(campaign)}</div><h2>${esc(campaign.title)}</h2>${(d=>d?`<p class="quiet work-date">${esc(d.kind)} ${esc(d.date)}</p>`:"")(campaignDateInfo(campaign))}<p>${esc(campaign.summary)}</p>${workAttributionSummary(campaign)}${progressBar(campaign,campaign.title)}${completionChecks(campaign)}${workStatusDetails(campaign)}${workAttributionDetails(campaign)}<details data-persist="campaign-${esc(campaign.id)}"><summary>Source and evidence</summary><p class="board-source">${esc(campaign.owner_path)}</p></details></div><div class="campaign-stages"><strong>${stageCountLabel(stages)}</strong>${stages.map(stageRow).join("")||'<p class="quiet">No stages recorded.</p>'}</div></article>`;
  };
  return heading("Campaigns","Recorded campaigns, sorted by the selected field, up to 50 per page.")+toggle+`<p>${num(campaigns.length)} ${workGroupLabels[group].toLowerCase()} campaigns</p><div class="campaign-list">${shown.map(card).join("")||empty(`No ${workGroupLabels[group].toLowerCase()} campaigns`,"No selected campaign records have this status.")}</div>`+boardPaging(state.campaignPage,campaigns.length,size,"data-campaign-page");
}
function boards(p){
  const board=p.modules.boards||{},all=board.items||[],section=state.boardsSection;
  if(section==="architecture"){const a=p.modules.architecture||{};return heading("Architecture","Canonical orientation and component boundaries from the registered architecture owner.")+(a.status==="available"?`<section class="card"><div class="section-head"><div><h2>${esc(a.title)}</h2><p>${esc(a.summary)}</p></div>${status("Canonical source","good")}</div><p class="board-source">${esc(a.path)} · SHA-256 ${esc(a.sha256)}</p><div class="architecture-sections">${(a.sections||[]).map((section,index)=>{const title=typeof section==="string"?section:section.title,detail=typeof section==="string"?"Open the canonical source for this section's full detail.":section.detail;return `<details data-persist="architecture-${index}"><summary><span>${index+1}</span><strong>${esc(title)}</strong></summary><div class="architecture-detail">${esc(detail||"No section detail was recorded.")}</div></details>`}).join("")}</div><p class="quiet">${esc(a.qualification)}</p></section>`:empty("Architecture unavailable",a.reason||"The registered architecture owner could not be read."))}
  if(!["available","partial"].includes(board.status))return heading("Boards","Current work, campaigns and future ideas from their native owners.")+empty("No structured board yet",board.reason||"Choose board owners and add a TokenSlang board through the CLI. Existing prose remains available in the repository.");
  if(section==="campaigns")return campaignListV2(p);
  if(section==="slices"||section==="quick")return workList(all,section);
  if(section==="features"||section==="capabilities")return catalogList(board,section);
  const selected=all.filter(i=>section==="bugs"?i.type==="bug":true),items=filteredDocs(selected,"boards"),label=section==="bugs"?["Known bugs","Reproduced unresolved defects only; limitations and future ideas stay with their owners."]:["Boards","Recorded repository work."];
  return heading(label[0],label[1])+docControls(selected,"boards")+`<p>${items.length} of ${selected.length} records in this view</p><div class="board-list">${items.map(i=>boardCard(i)).join("")||empty("No recorded items","Adjust the filters or add a source-backed record through the board CLI.")}</div>`;
}
content.addEventListener("click",event=>{const button=event.target.closest("[data-board-horizon]");if(button){state.boardsHorizon=button.dataset.boardHorizon;render();return}const campaignPage=event.target.closest("[data-campaign-page]");if(campaignPage&&!campaignPage.disabled){state.campaignPage=Number(campaignPage.dataset.campaignPage);render();return}const sliceStatus=event.target.closest("[data-slice-status]");if(sliceStatus){state.sliceStatus=sliceStatus.dataset.sliceStatus;render()}});
content.addEventListener("change",event=>{const sort=event.target.closest("[data-campaign-sort]");if(sort){state.campaignSort=sort.value;state.campaignPage=0;render()}});
function workRowList(p,compact){const board=p.modules.boards||{},byId=new Map((board.items||[]).map(i=>[i.id,i])),rows=board.work_order?.rows||{},chip=id=>{const i=byId.get(id);if(!i)return `<span class="work-row-chip">${tag(shortWorkId(id))}</span>`;const parent=i.type==="stage"?byId.get(i.parent_id):null;return `<span class="work-row-chip"><a href="${workKindTag(i)?"#work-order":`#work-${esc(i.id)}`}">${esc(i.title)}</a>${parent?tag(parent.title):""}${workKindTag(i)}${workStatusTag(i)}</span>`},list=entries=>`<ol class="work-rows">${entries.map((row,n)=>{const ids=typeof row==="string"?[row]:(row.together||[]),parents=ids.map(id=>byId.get(id)).filter(i=>i?.type==="stage").map(i=>i.parent_id),sequential=parents.length>new Set(parents).size;return `<li class="work-row"><span class="work-row-number">${n+1}</span>${typeof row==="string"?chip(row):`${tag("together")}<span class="work-row-chips">${ids.map(chip).join("")}</span>${sequential?'<span class="quiet">Stages of one campaign still run one after another.</span>':""}`}</li>`}).join("")}</ol>`,lane=(title,entries)=>entries?.length?`<h3>${title}</h3>${list(entries)}`:"";if(!(rows.current?.length||(!compact&&rows.next?.length)))return "";return `<section class="card work-rows-card"><div class="section-head"><h2>Work order rows</h2><span class="quiet">Priority only. Work starts when the owner assigns it; an assigned scope needs no further per-stage approval.</span></div>${lane("Current Tasks",rows.current)}${compact?"":lane("Next Work",rows.next)}</section>`}
function workOrder(p){const board=p.modules.boards||{},items=board.items||[],byId=new Map(items.map(i=>[i.id,i])),order=board.work_order||{},resolve=ids=>(Array.isArray(ids)?ids:[]).map(id=>byId.get(id)).filter(Boolean),top=items.filter(i=>(["campaign","slice","quick","task"].includes(i.type)||workQueued(i))&&!i.parent_id),saved=[...resolve(order.current),...resolve(order.next)],savedIds=new Set(saved.map(i=>i.id)),ordered=[...saved,...top.filter(i=>!savedIds.has(i.id))],groups=Object.fromEntries(workGroupOrder.map(name=>[name,ordered.filter(i=>workGroup(i)===name)])),pageSize=50,donePages=Math.max(1,Math.ceil(groups.done.length/pageSize));state.workOrderDonePage=Math.min(state.workOrderDonePage||0,donePages-1);const doneShown=groups.done.slice(state.workOrderDonePage*pageSize,(state.workOrderDonePage+1)*pageSize),section=(title,rows)=>`<section class="card work-order-group"><div class="section-head"><h2>${title}</h2><span class="quiet">${num(rows.length)} items</span></div><div class="board-list">${rows.map(i=>boardCard(i,i.display_type||i.type)).join("")||'<p class="quiet">No recorded items in this group.</p>'}</div></section>`;return heading("Work order","Native work grouped by lifecycle state while preserving saved priority within each live group.")+(!order.configured?`<p class="quiet">Saved work order is not configured; showing the recorded groups available.</p>`:"")+(typeof workRowList==="function"?workRowList(p,false):"")+section("Active",groups.active)+section("Not active",groups["not-active"])+section("Done",doneShown)+boardPaging(state.workOrderDonePage,groups.done.length,pageSize,"data-work-order-done-page")+section("Cancelled",groups.cancelled)}

const skillViews = new Map();
function skillView(){const key=state.project;if(!skillViews.has(key))skillViews.set(key,{query:"",tool:"",status:"",offset:0,data:null,loading:false,error:"",serial:0,detail:null});return skillViews.get(key)}
function skills(p){
  const v=skillView(),base=p.modules.skills||{},data=v.data||base,items=(data.items||[]).slice(0,20),all=base.items||[];
  const tools=[...new Set(all.flatMap(i=>i.tools||[]))].sort();
  const total=data.total??(data.items||[]).length;
  return heading("Skills","Reusable procedures for tasks and tools. Skills remain available independently of Memory.")+
    `<form class="card skill-search" data-skill-search><label>Task or keyword<input name="query" type="search" placeholder="Try closeout or NAV maintenance" value="${esc(v.query)}"></label><label>Tool<select name="tool"><option value="">All tools</option>${tools.map(t=>`<option${v.tool===t?" selected":""} value="${esc(t)}">${esc(t)}</option>`).join("")}</select></label><label>Recorded status<select name="status"><option value="">All statuses</option>${["usable","candidate","stale","retired"].map(t=>`<option${v.status===t?" selected":""} value="${t}">${t}</option>`).join("")}</select></label><button type="submit"${v.loading?" disabled":""}>${v.loading?"Searching…":"Search"}</button><button type="button" class="secondary" data-skill-clear>Clear</button></form>`+
    `<p class="muted">Use counts reflect explicit retained execution records for each shown version on this host, not searches or reads. Unknown means usage coverage is unavailable. Coverage: ${esc(data.usage_coverage?.status||"unknown")}.</p>`+
    (v.error?`<div role="alert" class="card">${esc(v.error)}</div>`:"")+
    ((data.diagnostics||[]).length?`<details class="card"><summary>Catalog coverage notes</summary>${data.diagnostics.map(x=>`<p>${esc(typeof x==="string"?x:JSON.stringify(x))}</p>`).join("")}</details>`:"")+
    (!["available","partial","empty"].includes(data.catalog_status||data.status)?empty("Skill catalog unavailable",data.reason||"Configure this repository’s durable skill catalog to browse its procedures."):
      `<p aria-live="polite">${num(total)} matching skills${v.offset?` · showing ${v.offset+1}–${v.offset+items.length}`:""}</p><div class="skill-grid">${items.map(i=>{
        const u=i.usage||{},fresh=i.source?.state==="fresh"||i.source?.state==="current",ready=i.status==="usable"&&fresh;
        return `<article class="card skill-card"><div class="meta">${status(i.status)}${tag("v"+i.version)}${tag(i.source?.state||"unknown source")}</div><h2>${esc(i.title)}</h2><p>${esc(i.description)}</p>${i.match?.terms?.length?`<p class="muted">Matched: ${i.match.terms.map(esc).join(", ")}</p>`:""}<div class="meta">${(i.tools||[]).map(t=>tag(t)).join("")}</div><div class="skill-use"><strong>${u.count==null?"Unknown":num(u.count)}</strong><span>recorded uses</span></div><p class="muted">${u.last_used?`Last recorded use ${esc(when(u.last_used))}`:"No last-use evidence"}${u.count!=null?` · ${num(u.success_count)} reported successes · ${num(u.failure_count)} reported failures`:""}</p><details data-persist="skill-meta-${esc(i.id)}"><summary>Source and relationships</summary><p class="board-source">${esc(i.id)} · ${esc(i.source?.path)}</p>${Object.entries(i.relationships||{}).filter(([,a])=>a.length).map(([k,a])=>`<p><strong>${esc(k.replaceAll("_"," "))}</strong>: ${a.map(esc).join(", ")}</p>`).join("")}${(i.evidence||[]).map(e=>`<p>${esc(e.summary)} <span class="muted">${esc(e.reference)}</span></p>`).join("")}</details><button type="button" data-skill-read="${esc(i.id)}" data-version="${esc(i.version)}" data-sha="${esc(i.source?.sha256)}"${ready?"":" disabled"}>Read procedure</button></article>`
      }).join("")||empty((data.catalog_status||data.status)==="empty"?"No skills recorded":"No matching skills",(data.catalog_status||data.status)==="empty"?"Add an explicit procedure record to this repository’s catalog.":"Try a different task phrase, tool or status.")}</div>`+
      `<div class="skill-paging"><button type="button" data-skill-page="${Math.max(0,v.offset-20)}"${v.offset<=0||v.loading?" disabled":""}>Previous</button><button type="button" data-skill-page="${v.offset+20}"${v.offset+items.length>=total||v.loading?" disabled":""}>Next</button></div>`)+
    (v.detail?`<section class="card skill-detail" aria-live="polite"><div class="skill-detail-head"><h2>${esc(v.detail.title||"Procedure")}</h2><button type="button" class="secondary" data-skill-close>Close procedure</button></div>${v.detail.loading?"Loading exact source…":v.detail.body!=null?`<pre>${esc(v.detail.body)}</pre>`:`<p>${esc(v.detail.message||"The procedure is unavailable or changed. Search again for its current state.")}</p>`}</section>`:"");
}
async function searchSkills(offset=0){
  const v=skillView(),idx=state.project,serial=++v.serial;v.offset=offset;v.loading=true;v.error="";v.detail=null;render();
  try{const q=new URLSearchParams({project:idx,query:v.query,tool:v.tool,status:v.status,offset});const r=await fetch("/api/v1/skills/search?"+q);const data=await r.json();if(!r.ok)throw new Error(data.message||"Skill search is unavailable.");if(serial!==v.serial)return;v.data=data}
  catch(e){if(serial===v.serial)v.error=e.message}
  finally{if(serial===v.serial){v.loading=false;if(state.project===idx&&state.view==="skills")render()}}
}
content.addEventListener("input",e=>{if(e.target.closest("[data-skill-search]")&&["query","tool","status"].includes(e.target.name))skillView()[e.target.name]=e.target.value});
content.addEventListener("submit",e=>{if(!e.target.matches("[data-skill-search]"))return;e.preventDefault();const f=new FormData(e.target),v=skillView();v.query=String(f.get("query")||"");v.tool=String(f.get("tool")||"");v.status=String(f.get("status")||"");searchSkills(0)});
content.addEventListener("click",async e=>{
  const clear=e.target.closest("[data-skill-clear]"),page=e.target.closest("[data-skill-page]"),read=e.target.closest("[data-skill-read]"),close=e.target.closest("[data-skill-close]");
  if(clear){const v=skillView();v.query="";v.tool="";v.status="";searchSkills(0)}
  if(page)searchSkills(Number(page.dataset.skillPage));
  if(close){const v=skillView();v.serial++;v.loading=false;v.detail=null;render()}
  if(read){const v=skillView(),idx=state.project,serial=++v.serial;v.detail={loading:true};render();try{
    const q=new URLSearchParams({project:idx,id:read.dataset.skillRead,version:read.dataset.version,sha256:read.dataset.sha});const r=await fetch("/api/v1/skills/read?"+q),data=await r.json();if(serial!==v.serial)return;v.detail=r.ok&&typeof data.body==="string"?{title:data.item?.title||data.skill?.title||read.dataset.skillRead,body:data.body}:{message:data.message||data.reason||"The procedure is unavailable or changed. Refresh the catalog."};
  }catch(err){if(serial===v.serial)v.detail={message:err.message}}finally{if(serial===v.serial&&state.project===idx&&state.view==="skills"){render();content.querySelector(".skill-detail")?.scrollIntoView({block:"start",behavior:"smooth"})}}}
});

function repoViewDispatch(name,p){return window.repoViews&&window.repoViews[name]?window.repoViews[name](p):empty("View unavailable","The repository view module failed to load.")}
const views={"shared-repositories":sharedRepositories,overview,delivery,boards,"work-order":workOrder,changes,skills,memory,navigation:p=>navigation(p)+documentsPanel(p),gallery,runs,activity,metrics,timeline,companion,"cross-talk":crossTalk,settings,system,"repository-state":repositoryState,"repo-health":p=>repoViewDispatch("repo-health",p),"repo-hotspots":p=>repoViewDispatch("repo-hotspots",p),"nav-graph":p=>repoViewDispatch("nav-graph",p),offers:offersPage};
let workRoutePending=false;
function workHashId(){if(!location.hash.startsWith("#work-")||Object.hasOwn(views,location.hash.slice(1)))return null;try{return decodeURIComponent(location.hash.slice(6))||null}catch{return null}}
function routeWorkTarget(p){
  const id=workHashId(),board=p.modules?.boards||{};
  if(!id)return null;
  if(!["available","partial"].includes(board.status))return {loading:board.status==="deferred",message:board.status==="deferred"?"Work records are loading.":board.reason||"Work records are unavailable for the selected source."};
  const all=board.items||[],byId=new Map(all.map(item=>[item.id,item])),item=byId.get(id);
  if(!item)return {message:`${id} is not available in the selected work source${board.status==="partial"?" (the observation is partial)":""}. Choose another Work view or refresh its records.`};
  let campaign=item.type==="campaign"?item:null,parent=item,seen=new Set();
  while(!campaign&&parent?.parent_id&&!seen.has(parent.id)){seen.add(parent.id);parent=byId.get(parent.parent_id);if(parent?.type==="campaign")campaign=parent}
  if(campaign){
    state.boardsSection="campaigns";state.workHorizon=workGroup(campaign);
    const index=sortedCampaigns(all,state.workHorizon).findIndex(row=>row.id===campaign.id);
    if(index<0)return {message:`${id} has no visible campaign in the selected work source.`};
    state.campaignPage=Math.floor(index/50);state.boardsNavExpanded=true;workRoutePending=false;
    return {anchor:item.parent_id===campaign.id||item===campaign?id:campaign.id};
  }
  if(item.parent_id||!["slice","quick","task"].includes(item.type))return {message:`${id} has no board card in the selected work source.`};
  state.boardsSection=item.type==="slice"?"slices":"quick";state.sliceStatus=workGroup(item);state.boardsNavExpanded=true;workRoutePending=false;
  return {anchor:id};
}
function setupTimelineScroll(){const rail=content.querySelector(".timeline-rail"),top=content.querySelector(".timeline-scrollbar");if(!rail||!top)return;top.firstElementChild.style.width=`${rail.scrollWidth}px`;const max=Math.max(0,rail.scrollWidth-rail.clientWidth),target=state.timelineFollowNewest||state.timelineScroll==null?max:Math.min(state.timelineScroll,max);rail.scrollLeft=top.scrollLeft=target;let syncing=false;const sync=(source,dest)=>{if(syncing)return;syncing=true;dest.scrollLeft=source.scrollLeft;state.timelineScroll=source.scrollLeft;state.timelineFollowNewest=max-source.scrollLeft<=2;syncing=false};top.addEventListener("scroll",()=>sync(top,rail),{passive:true});rail.addEventListener("scroll",()=>sync(rail,top),{passive:true})}
function render(){const settingsFocused=settingsFocus();if(state.view==="offers"){captureSettingsDrafts();captureMetricDrafts();return renderOffers()}if(featuresActive()){captureSettingsDrafts();captureMetricDrafts();return renderFeatures()}if(!state.snapshot)return;captureSettingsDrafts();captureMetricDrafts();const rail=content.querySelector(".timeline-rail");if(rail){state.timelineScroll=rail.scrollLeft;state.timelineFollowNewest=rail.scrollWidth-rail.clientWidth-rail.scrollLeft<=2}const p=project();document.querySelectorAll("#primary-nav a[data-view]").forEach(a=>a.setAttribute("aria-current",a.dataset.view===state.view?"page":"false"));if(!p){content.innerHTML=empty("No readable repositories","Check the selected roots and refresh.");syncSettingsNav();syncBoardsNav();return}const opened=new Set(Array.from(content.querySelectorAll("details[data-persist][open]")).map(x=>x.dataset.persist));if(p.deferred_project){content.innerHTML=empty("Loading project","Reading the summary for the selected repository…");if(!state.snapshotLoading)load(true);return}const route=state.view==="boards"&&workRoutePending&&!state.workChanging?routeWorkTarget(p):null;content.innerHTML=currentWorkBar(p)+(state.workChanging?empty("Loading selected source","Reading the requested work and knowledge view…"):route?.message?heading("Work item","Open a native work record from the selected source.")+empty(route.loading?"Loading work item":"Work item unavailable",route.message):lazyGate(p));content.querySelectorAll("details[data-persist]").forEach(x=>{x.open=opened.has(x.dataset.persist)});applySettingsSection();syncSettingsNav();syncBoardsNav();applyBars();setupTimelineScroll();restoreSettingsFocus(settingsFocused);if(route?.anchor){const target=document.getElementById(`work-${route.anchor}`);if(target&&content.contains(target)){target.focus({preventScroll:true});target.scrollIntoView?.({block:"center"})}else content.innerHTML=currentWorkBar(p)+empty("Work item unavailable",`${route.anchor} has no visible board card in this view.`)}}
function syncRoute(){let name=location.hash.slice(1),previous=state.view,workId=workHashId();if(name==="features"&&!workId){state.settingsSection="m_parts";state.settingsNavExpanded=true;history.replaceState(null,"","#settings");name="settings"}state.view=workId?"boards":views[name]?name:"overview";workRoutePending=Boolean(workId);queueMicrotask(()=>ensureViewLazy({retry:true}));if(state.view==="settings"&&previous!=="settings")state.settingsNavExpanded=true;if(state.view==="offers"&&previous!=="offers")state.offers.error=null;if(featuresActive()&&(previous!=="settings"||!state.features.data)){state.features.error=null;state.features.stale=true}if(state.view==="boards"&&previous!=="boards")state.boardsNavExpanded=true;render();if(!workId){content.focus({preventScroll:true});window.scrollTo({top:0,behavior:"instant"})}if(state.view==="memory"&&previous!=="memory")queueMicrotask(refreshMemorySnapshot)}
function visibleProjection(snapshot){const p=snapshot?.projects?.[state.project];if(!p)return null;if(state.view==="overview")return {work:p.work,boards:p.modules?.boards,repository:p.repository,release:p.release,nav:p.modules?.nav,ram:p.modules?.ram,delivery:p.modules?.delivery,visual_atlas:p.modules?.visual_atlas,host:p.host};if(state.view==="boards")return {boards:p.modules?.boards,delivery:p.modules?.delivery,architecture:p.modules?.architecture};if(state.view==="shared-repositories")return {roster:snapshot.shared_repositories,observation:p.modules?.current_work?.shared_work};if(state.view==="gallery")return p.modules?.visual_atlas;if(state.view==="repository-state")return p.modules?.repository_state;return p.modules?.[state.view]??p[state.view]??null}
async function load(announce=false){if(!announce&&(state.workChanging||state.snapshotLoading))return false;state.snapshotLoading++;const workKey=workViewKey(),request=++state.workRequest;const background=!announce&&Boolean(state.snapshot),button=document.getElementById("refresh"),before=background?JSON.stringify(visibleProjection(state.snapshot)):null;button.disabled=true;if(!background)stamp.textContent="Reading current records…";try{const response=await fetch(`/api/v1/snapshot?${workQuery({detail:"summary",...(announce?{refresh_state:"1"}:{})})}`,{cache:"no-store"});if(!response.ok)throw new Error(`Dashboard service returned ${response.status}`);const next=await response.json();{const np=next.projects?.[state.project];if(np&&!np.deferred_project)reapplyLazy(np)}const changed=!background||before!==JSON.stringify(visibleProjection(next));if(workKey!==workViewKey()||request<state.snapshotApplied)return false;state.snapshotApplied=request;if(request<state.workApplied)retainNewerWork(next.projects?.[state.project],project());else state.workApplied=request;state.snapshot=next;if(announce&&workHashId())workRoutePending=true;refreshModelWatch();if(state.view==="settings"&&state.settings)loadModelCatalog();if(!background)state.metricProjection=null;const names=state.snapshot.projects.map(p=>p.name),current=Array.from(select.options).map(o=>o.textContent);if(JSON.stringify(names)!==JSON.stringify(current)){select.innerHTML="";names.forEach((name,i)=>{const option=document.createElement("option");option.value=String(i);option.textContent=name;select.append(option)})}if(state.project>=state.snapshot.projects.length)state.project=0;select.value=String(state.project);stamp.textContent=`Observed ${new Date(state.snapshot.generated_at).toLocaleTimeString()}`;if(changed&&(state.view!=="settings"||!content.querySelector("[data-settings-section]")))render();if(background&&["metrics","timeline"].includes(state.view)&&project())loadMetricProjection(project(),metricRange(project()),true);if(announce)toast("Dashboard refreshed from native records.");ensureViewLazy({force:Boolean(background||announce),fresh:announce,retry:true});return true}catch(error){if(!background&&state.view!=="offers"&&!featuresActive())content.innerHTML=empty("Could not refresh",error.message);stamp.textContent=background?"Background refresh failed":"Refresh failed";return false}finally{state.snapshotLoading--;button.disabled=state.snapshotLoading>0}}
function toast(text){const el=document.getElementById("toast");el.textContent=text;el.classList.add("show");setTimeout(()=>el.classList.remove("show"),2200)}
function ensureSettingsNav(){const nav=document.getElementById("primary-nav");if(nav&&!nav.querySelector('[data-view="settings"]')){const link=document.createElement("a");link.href="#settings";link.dataset.view="settings";link.textContent="Settings";const systemLink=nav.querySelector('[data-view="system"]');nav.insertBefore(link,systemLink)}}
function ensureChangelogNav(){const link=document.querySelector('#primary-nav [data-view="changes"]');if(link)link.textContent="Changelog"}
document.addEventListener("click",event=>{if(event.target.closest("[data-model-watch-review]"))reviewModelWatch()});
select.addEventListener("change",()=>{state.timelineFollowNewest=true},{capture:true});
wireFeatures();window.addEventListener("hashchange",syncRoute);select.addEventListener("change",()=>{state.project=Number(select.value);state.workView="current";state.workSource=null;state.documentBodies={};state.overviewCampaignIndex=0;state.campaignPage=0;state.settingsFeedback={};state.settings=null;state.settingsProject=null;state.settingsDrafts={};state.discardSettingsDraft=null;state.metricProjection=null;state.timelineScroll=null;state.hooks={recent:null,recentLoading:false,recentError:null};if(workHashId())workRoutePending=true;render();if(state.view==="memory")refreshMemorySnapshot()});document.getElementById("refresh").addEventListener("click",async()=>{await load(true);if(state.view==="memory")refreshMemorySnapshot()});content.addEventListener("click",event=>{const section=event.target.closest("[data-settings-nav]");if(section){event.preventDefault();captureSettingsDrafts();state.settingsSection=section.dataset.settingsNav;state.settingsNavExpanded=true;if(location.hash!=="#settings")location.hash="settings";else render();return}const button=event.target.closest("button[data-module]");if(button){state.runModule=button.dataset.module;render()}const save=event.target.closest("[data-settings-save]");if(save)saveSettings(save.dataset.settingsSave);if(event.target.closest("[data-settings-reload]"))loadSettings(true)});document.getElementById("primary-nav").addEventListener("click",event=>{const memoryLink=event.target.closest('a[data-view="memory"]'),settingsToggle=event.target.closest("[data-settings-nav-toggle]"),settingsSection=event.target.closest("[data-settings-nav]"),boardsToggle=event.target.closest("[data-boards-nav-toggle]"),boardsSection=event.target.closest("[data-boards-nav]");if(memoryLink&&state.view==="memory"){event.preventDefault();refreshMemorySnapshot();return}if(settingsToggle){event.preventDefault();state.settingsNavExpanded=!state.settingsNavExpanded;syncSettingsNav();return}if(settingsSection){event.preventDefault();captureSettingsDrafts();state.settingsSection=settingsSection.dataset.settingsNav;state.settingsNavExpanded=true;if(location.hash!=="#settings")location.hash="settings";else render();return}if(boardsToggle){event.preventDefault();state.boardsNavExpanded=!state.boardsNavExpanded;syncBoardsNav();return}if(boardsSection){event.preventDefault();state.boardsSection=boardsSection.dataset.boardsNav;state.boardsNavExpanded=true;state.campaignPage=0;if(location.hash!=="#boards")location.hash="boards";else render()}});ensureSettingsNav();ensureChangelogNav();syncSettingsNav();syncBoardsNav();syncRoute();load();state.timer=setInterval(load,30000);
content.addEventListener("click",event=>{const p=project(),preset=event.target.closest("[data-metric-preset]"),apply=event.target.closest("[data-metric-apply]");if(!p)return;if(preset){const values={"1h":{kind:"relative",amount:1,unit:"hours"},"24h":{kind:"relative",amount:24,unit:"hours"},"7d":{kind:"relative",amount:7,unit:"days"},all:{kind:"all"}};const range=values[preset.dataset.metricPreset];storeMetricRange(p,range);state.metricProjection=null;loadMetricProjection(p,range)}if(apply?.dataset.metricApply==="relative"){const amount=Number(content.querySelector("#metric-range-amount")?.value),unit=content.querySelector("#metric-range-unit")?.value;if(!Number.isFinite(amount)||amount<=0||!["minutes","hours","days","weeks"].includes(unit)){toast("Enter a positive amount and a supported unit.");return}const range={kind:"relative",amount,unit};storeMetricRange(p,range);state.metricProjection=null;loadMetricProjection(p,range)}if(apply?.dataset.metricApply==="exact"){const range={kind:"exact",from:content.querySelector("#metric-range-from")?.value||"",to:content.querySelector("#metric-range-to")?.value||""};if(!metricDate(range.from)||!metricDate(range.to)||metricDate(range.from)>=metricDate(range.to)){toast("Choose a valid From and To date.");return}storeMetricRange(p,range);state.metricProjection=null;loadMetricProjection(p,range)}});

content.addEventListener('change',event=>{if(!event.target.id?.startsWith('metric-range-'))return;const p=project();if(!p)return;captureMetricDrafts();const d=state.metricDrafts[metricRangeKey(p)],rolling=['metric-range-amount','metric-range-unit'].includes(event.target.id);let range;if(rolling){const amount=Number(d.amount);if(!Number.isSafeInteger(amount)||amount<1||amount>1200){toast('Choose a whole number from 1 to 1200.');return}range={kind:'relative',amount,unit:d.unit}}else{if(!metricDate(d.from)||!metricDate(d.to)||metricDate(d.from)>=metricDate(d.to))return;range={kind:'exact',from:d.from,to:d.to}}storeMetricRange(p,range);state.metricDrafts[metricRangeKey(p)]=d;state.metricProjection=null;loadMetricProjection(p,range)});
content.addEventListener('click',event=>{const group=event.target.closest('[data-token-group]');if(group){state.tokenGroup=group.dataset.tokenGroup;render();return}if(event.target.closest('[data-token-expand]')){state.metricExpanded=!state.metricExpanded;render();return}if(event.target.closest('[data-metrics-customize]')){state.metricCustomize=!state.metricCustomize;render();return}const move=event.target.closest('[data-metric-move]');if(move){const p=project(),layout=metricLayout(p),from=layout.order.indexOf(move.dataset.metricMove),to=Math.max(0,Math.min(layout.order.length-1,from+Number(move.dataset.direction)));if(from!==to){layout.order.splice(to,0,layout.order.splice(from,1)[0]);storeMetricLayout(p,layout);render()}}});
content.addEventListener('change',event=>{const input=event.target.closest('[data-metric-visible]');if(!input)return;const p=project(),layout=metricLayout(p);if(input.checked)layout.hidden.delete(input.dataset.metricVisible);else layout.hidden.add(input.dataset.metricVisible);storeMetricLayout(p,layout);render()});
content.addEventListener('dragstart',event=>{const item=event.target.closest('[data-metric-module],[data-metric-layout-row]');if(!item||!state.metricCustomize)return;state.metricDrag=item.dataset.metricModule||item.dataset.metricLayoutRow;event.dataTransfer?.setData('text/plain',state.metricDrag);event.dataTransfer?.setDragImage(item,18,18);item.classList.add('is-dragging')});
content.addEventListener('dragend',event=>{event.target.closest('[data-metric-module],[data-metric-layout-row]')?.classList.remove('is-dragging');state.metricDrag=null});
content.addEventListener('dragover',event=>{if(state.metricCustomize&&event.target.closest('[data-metric-module],[data-metric-layout-row]'))event.preventDefault()});
content.addEventListener('drop',event=>{const target=event.target.closest('[data-metric-module],[data-metric-layout-row]');if(!target||!state.metricDrag)return;event.preventDefault();const targetId=target.dataset.metricModule||target.dataset.metricLayoutRow,p=project(),layout=metricLayout(p),from=layout.order.indexOf(state.metricDrag),to=layout.order.indexOf(targetId);if(from>=0&&to>=0&&from!==to){layout.order.splice(to,0,layout.order.splice(from,1)[0]);storeMetricLayout(p,layout);state.metricDrag=null;render()}});
function tokenCounter(value,field){const t=value?.tokens||value||{},exact=t[field],observed=t[`observed_${field}`],number=exact??observed;return typeof number==='number'&&Number.isFinite(number)&&number>=0?number:null}
function visibleOutputTokens(value){const output=tokenCounter(value,'output_tokens'),reasoning=tokenCounter(value,'reasoning_output_tokens');return output!=null&&reasoning!=null&&reasoning<=output?output-reasoning:null}
function tokenGroups(models,pairs,paired){const groups=new Map();for(const row of paired?pairs:models){const label=paired?(row.primary_model?shortModel(row.primary_model)+' + '+(row.helper_model?shortModel(row.helper_model):'companion off'):'Unknown association'):shortModel(row.model);const g=groups.get(label)||{label,model:paired?row.primary_model:row.model,helperModel:paired?row.helper_model:null,paired,input:0,output:0,reasoning:0,fullOutput:0,unknown:false};const values={input:tokenCounter(row,'input_tokens'),output:visibleOutputTokens(row),reasoning:tokenCounter(row,'reasoning_output_tokens'),fullOutput:tokenCounter(row,'output_tokens')};for(const [key,value] of Object.entries(values)){if(value!=null){if(g[key]!==null)g[key]+=value}else{g[key]=null;g.unknown=true}}const t=row.tokens||{};if(['input_tokens','output_tokens','reasoning_output_tokens'].some(field=>t[field]==null&&t[`observed_${field}`]!=null))g.unknown=true;groups.set(label,g)}return [...groups.values()]}
function modelUsageSlices(models){const groups=new Map();for(const row of models){const label=shortModel(row.model),group=groups.get(label)||{label,model:row.model,value:0,partial:false};const tokens=row.tokens||{};for(const field of ['input_tokens','output_tokens']){const exact=tokens[field],observed=tokens[`observed_${field}`],value=exact??observed;if(typeof value==='number'&&Number.isFinite(value)&&value>=0)group.value+=value;else group.partial=true;if(exact==null&&observed!=null)group.partial=true}groups.set(label,group)}const rows=[...groups.values()].filter(row=>row.value>0).sort((a,b)=>b.value-a.value),total=rows.reduce((sum,row)=>sum+row.value,0);return rows.map(row=>({...row,share:total?row.value/total:0}))}
function modelUsagePie(models){const rows=modelUsageSlices(models),total=rows.reduce((sum,row)=>sum+row.value,0);if(!rows.length)return `<div class="model-usage-pie is-empty"><h3>Usage share by model</h3><p class="quiet">No recorded model token totals in this range.</p></div>`;let cursor=0;const arcs=rows.map(row=>{const start=cursor,length=row.share*100;cursor+=length;return `<circle cx="50" cy="50" r="41" fill="none" stroke="${modelColor(row.model)}" stroke-width="16" pathLength="100" stroke-dasharray="${length.toFixed(4)} ${(100-length).toFixed(4)}" stroke-dashoffset="${(-start).toFixed(4)}" transform="rotate(-90 50 50)"><title>${esc(row.label)}: ${(row.share*100).toFixed(1)}% · ${num(row.value)} tokens${row.partial?' observed subtotal':''}</title></circle>`}).join('');const aria=rows.map(row=>`${row.label} ${(row.share*100).toFixed(1)} percent`).join(', ');return `<div class="model-usage-pie"><div><h3>Usage share by model</h3><p class="quiet">Input + output · selected range</p></div><div class="model-pie-wrap"><svg class="model-pie" viewBox="0 0 100 100" role="img" aria-label="Recorded token usage by model: ${esc(aria)}"><circle cx="50" cy="50" r="41" fill="none" stroke="var(--line)" stroke-width="16"/>${arcs}</svg><div class="model-pie-center"><strong>${compactNum(total)}</strong><span>tokens</span></div></div><div class="model-pie-legend">${rows.map(row=>`<div><svg class="model-pie-swatch" width="9" height="9" aria-hidden="true"><circle cx="4.5" cy="4.5" r="4.5" fill="${modelColor(row.model)}"/></svg><span class="model-label model-${modelKey(row.model)}">${esc(row.label)}</span><strong>${(row.share*100).toFixed(1)}%</strong><small>${num(row.value)}${row.partial?' observed':''}</small></div>`).join('')}</div><p class="quiet">Shares use recorded input plus output. Cached input and reasoning already belong to those totals; partial counters remain labeled observed.</p></div>`}
function tokenGroupLabel(row){return row.paired?`${modelLabel(row.model)} + ${row.helperModel?modelLabel(row.helperModel):'companion off'}`:modelLabel(row.model,row.label)}
function reasoningPattern(id,model){return `<pattern id="${id}" width="6" height="6" patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="var(--panel)"/><path d="M-1 1L1-1 M0 6L6 0 M5 7L7 5" stroke="${modelColor(model)}" stroke-width="2"/></pattern>`}
function responseComposition(rows){
  return `<div class="response-composition"><div class="section-head"><h3>Response composition</h3><span class="quiet">Visible output / reasoning</span></div>${rows.map((r,i)=>{
    const known=r.output!=null&&r.reasoning!=null,total=known?r.output+r.reasoning:0,share=total?r.reasoning/total*100:0,color=modelColor(r.model),id=`response-reasoning-${i}`;
    return `<div class="response-row"><div><span>${tokenGroupLabel(r)}</span><strong>${known?`${share.toFixed(1)}% reasoning`:'Unknown'}</strong></div><svg viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label="${esc(r.label)} response composition"><defs>${reasoningPattern(id,r.model)}</defs><rect width="100" height="8" rx="2" fill="var(--line)"/>${known&&total?`<rect x="0.2" y="0.5" width="${Math.max(0,100-share-.4)}" height="7" rx="1" fill="var(--panel)" stroke="${color}" vector-effect="non-scaling-stroke"/><rect x="${100-share}" width="${share}" height="8" rx="1" fill="url(#${id})"><title>Visible output ${num(r.output)}; reasoning ${num(r.reasoning)}</title></rect>`:''}</svg><small>${known?`${num(r.output)} visible · ${num(r.reasoning)} reasoning`:'Complete output and reasoning counters are required'}</small></div>`;
  }).join('')||'<p class="quiet">No recorded model response counters in this range.</p>'}</div>`;
}
function modelTokenChart(models,pairs){
  const paired=state.tokenGroup==='pairs',rows=tokenGroups(models,paired?pairs.filter(p=>p.primary_model):pairs,paired),max=Math.max(1,...rows.flatMap(r=>[r.input||0,r.output||0,r.reasoning||0])),w=Math.max(640,rows.length*135);
  const columns=rows.map((r,i)=>{
    const x=65+i*(w-65)/Math.max(rows.length,1),h=v=>(v||0)/max*170,color=modelColor(r.model),id=`token-reasoning-${i}`;
    const secondLine=paired?`<tspan x="${x+14}" dy="16" fill="${r.helperModel?modelColor(r.helperModel):'var(--muted)'}">+ ${esc(r.helperModel?shortModel(r.helperModel):'companion off')}</tspan>`:'';
    return `<g data-model="${modelKey(r.model)}"><title>${esc(r.label)}: input ${r.input==null?'Unknown':num(r.input)}, visible output ${r.output==null?'Unknown':num(r.output)}, reasoning ${r.reasoning==null?'Unknown':num(r.reasoning)}</title><defs>${reasoningPattern(id,r.model)}</defs><rect x="${x}" y="${200-h(r.input)}" width="28" height="${h(r.input)}" rx="3" fill="${color}"/><rect x="${x+34}" y="${200-h(r.output)}" width="28" height="${h(r.output)}" rx="3" fill="var(--panel)" stroke="${color}" stroke-width="1.5"/><rect x="${x+68}" y="${200-h(r.reasoning)}" width="28" height="${h(r.reasoning)}" rx="3" fill="url(#${id})"/><circle cx="${x+4}" cy="228" r="4" fill="${color}"/><text x="${x+14}" y="232" font-size="12" fill="${color}">${esc(paired?shortModel(r.model):r.label)}${secondLine}</text></g>`;
  }).join('');
  return `<section class="card token-usage ${state.metricExpanded?'is-expanded':''}"><div class="section-head"><div class="token-title"><h2>Token usage</h2><button type="button" class="icon-button" data-token-expand aria-pressed="${state.metricExpanded}" aria-label="${state.metricExpanded?'Contract':'Expand'} token usage chart"><span aria-hidden="true">${state.metricExpanded?'↘ ↖':'↖ ↘'}</span></button></div><div class="tabs"><button data-token-group="models" aria-pressed="${!paired}">Model</button><button data-token-group="pairs" aria-pressed="${paired}">Model + companion</button></div></div><div class="token-visuals"><div class="token-column-panel">${paired&&!rows.length?'<div class="pairing-empty"><strong>Companion pairing not recorded</strong><p>Token totals are available by model, but these records do not identify which primary and companion worked together. Select Model to compare their usage.</p></div>':''}<div class="token-chart-scroll" ${paired&&!rows.length?'hidden':''}><svg class="token-columns" viewBox="0 0 ${w} 260" role="img" aria-label="Input, visible output and reasoning tokens by model">${[0,.5,1].map(n=>`<line x1="55" x2="${w}" y1="${200-n*170}" y2="${200-n*170}" stroke="var(--line)"/><text x="0" y="${204-n*170}" fill="var(--muted)" font-size="11">${compactNum(n*max)}</text>`).join('')}${columns}</svg></div><p class="quiet token-key">Solid: input · Outline: visible output · Stripes: exposed reasoning.${paired?' Bars show combined usage in the primary model’s color.':''}</p>${responseComposition(rows)}</div>${modelUsagePie(models)}</div><details><summary>Exact counts and coverage</summary>${rows.map(r=>`<p>${tokenGroupLabel(r)} — Input: ${r.input==null?'Unknown':num(r.input)} · Visible output: ${r.output==null?'Unknown':num(r.output)} · Reasoning: ${r.reasoning==null?'Unknown':num(r.reasoning)} · Full output: ${r.fullOutput==null?'Unknown':num(r.fullOutput)}${r.unknown?' · Partial counters':''}</p>`).join('')||'<p>No recorded groups in this range.</p>'}</details></section>`;
}

// Shared with Tests and exported reports. Palette values live in app.css.
function modelColor(model){return `var(--model-${modelKey(model)})`}
function modelKey(model){const name=String(model||'').toLowerCase();return ['astra','sol','terra','luna','claude','gemini'].find(key=>name.includes(key))||'unknown'}
function modelLabel(model,label=shortModel(model)){return `<span class="model-label model-${modelKey(model)}">${modelMark(model)}${esc(label)}</span>`}
function modelMark(model){return `<svg class="model-mark" width="10" height="10" aria-hidden="true"><circle cx="5" cy="5" r="5" fill="${modelColor(model)}"/></svg>`}
function historyPoints(trends){return trends.filter(r=>/^\d{4}-\d{2}-\d{2}$/.test(r.date||'')).map(r=>{const t=r.tokens||{},get=field=>{const value=t[field]??t[`observed_${field}`];return Number.isFinite(value)&&value>=0?value:null},input=get('input_tokens'),fullOutput=get('output_tokens'),thinking=get('reasoning_output_tokens'),output=fullOutput!=null&&thinking!=null&&thinking<=fullOutput?fullOutput-thinking:null,cost=r.observed_standard_api_estimate_usd;return {date:r.date,time:Date.parse(r.date+'T00:00:00Z'),input,output,thinking,fullOutput,total:input==null||fullOutput==null?null:input+fullOutput,cost:Number.isFinite(cost)&&cost>=0?cost:null}}).filter(r=>Number.isFinite(r.time)).sort((a,b)=>a.time-b.time)}
function tokenHistory(trends,scope){
  const rows=historyPoints(trends),series=[['input','Input','#377ba8'],['thinking','Thinking','#a17bc0'],['output','Output','#359473'],['total','Total','#8670c1']],max=Math.max(1,...rows.flatMap(r=>series.map(([k])=>r[k]||0))),first=rows[0]?.time||0,last=rows.at(-1)?.time||first,x=r=>60+(last===first?340:(r.time-first)/(last-first)*680),y=v=>220-v/max*190;
  const cost=value=>Number.isFinite(value)?new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:4,maximumFractionDigits:4}).format(value):'Unknown';
  const point=(r,key,label,color)=>{const value=r[key],cx=x(r),cy=y(value),boxX=cx>535?cx-202:cx+12,boxY=Math.max(8,Math.min(162,cy-32)),tip=`${r.date}. ${label}: ${num(value)} tokens. Standard API equivalent: ${cost(r.cost)}.`;return `<g class="history-point" tabindex="0" role="img" aria-label="${esc(tip)}"><circle cx="${cx}" cy="${cy}" r="5" fill="${color}" stroke="var(--panel)" stroke-width="2"/><g class="history-tooltip" role="tooltip" aria-hidden="true"><rect x="${boxX}" y="${boxY}" width="190" height="58" rx="7"/><text x="${boxX+10}" y="${boxY+17}"><tspan font-weight="700">${r.date}</tspan><tspan x="${boxX+10}" dy="16">${esc(label)}: ${num(value)} tokens</tspan><tspan x="${boxX+10}" dy="16">API equivalent: ${cost(r.cost)}</tspan></text></g></g>`};
  return `<section class="card token-history"><div class="section-head"><div><h2>Token usage over time</h2><p class="quiet">${esc(scope)}</p></div><span class="tag">${rows.length} recorded days</span></div>${rows.length?`<svg viewBox="0 0 780 265" preserveAspectRatio="xMinYMid meet" role="img" aria-label="Daily input, thinking, output and total token usage" class="history-chart">${[0,.5,1].map(n=>`<line x1="60" x2="740" y1="${y(n*max)}" y2="${y(n*max)}" stroke="var(--line)"/><text x="0" y="${y(n*max)+4}" fill="var(--muted)" font-size="11">${compactNum(n*max)}</text>`).join('')}${series.map(([key,label,color])=>{let previous=null;return rows.map(r=>{const value=r[key];if(value==null){previous=null;return ''}const line=previous&&r.time-previous.time<=86400000?`<line x1="${x(previous)}" y1="${y(previous[key])}" x2="${x(r)}" y2="${y(value)}" stroke="${color}" stroke-width="2" ${key==='total'?'stroke-dasharray="5 4"':''}/>`:'';previous=r;return line+point(r,key,label,color)}).join('')}).join('')}<text x="60" y="250" font-size="11" fill="var(--muted)">${rows[0].date}</text><text x="740" y="250" text-anchor="end" font-size="11" fill="var(--muted)">${rows.at(-1).date}</text></svg>`:'<p class="quiet">No dated token counters are available.</p>'}<div class="history-legend">${series.map(([,label,color])=>`<span><svg width="20" height="10" aria-hidden="true"><line x1="0" x2="20" y1="5" y2="5" stroke="${color}" stroke-width="3"/></svg>${label}</span>`).join('')}</div><details><summary>Daily counts</summary><div class="table-wrap"><table><thead><tr><th>Date</th><th>Total</th><th>Input</th><th>Thinking</th><th>Output</th><th>Full output</th><th>Standard API equivalent</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${r.date}</td><td>${exactValue(r.total)}</td><td>${exactValue(r.input)}</td><td>${exactValue(r.thinking)}</td><td>${exactValue(r.output)}</td><td>${exactValue(r.fullOutput)}</td><td>${cost(r.cost)}</td></tr>`).join('')}</tbody></table></div></details><p class="quiet">All four lines share one scale, so output can look flat beside much larger input totals. Output means visible response tokens after exposed thinking; full output = output + thinking, and total = input + full output. Cached input is already included. Point cost is the recorded Standard API equivalent, not an account charge. Gaps mean no recorded value; partial counters are observed subtotals.</p></section>`
}

function moduleSummary(name,value){const descriptions={skills:'Reusable procedures, search and recorded uses.',changes:'Dated change records with source evidence.',boards:'Campaigns, slices and product catalogs.',architecture:'Component boundaries and architecture references.',delivery:'Changed files, campaign links and delivery readiness.',ram:'Saved working context and exact source entries.',nav:'Repository routes, surfaces and relationships.',measurement:'Recorded operation events and usage counters.',companion:'Agent runs, model usage and benchmark results.',attribution:'Primary and helper links from recorded assignments.',atlas:'Indexed source observations and read evidence.',visual_atlas:'Recorded screen and surface observations.'};if(Array.isArray(value.items))return `${value.items.length} records · ${descriptions[name]||'Source records'}`;if(Number.isFinite(value.records))return `${value.records} records · ${descriptions[name]||'Source records'}`;return descriptions[name]||'Open the corresponding page for available records.'}

// Features: the machine pages (Settings > This computer > Parts, Crosstalk, Claude). Setup installs every part; these
// switches only enable or disable parts and set the Crosstalk and Claude switches, and list the steps a switch left for
// an agent. They belong to this computer, not to a repository, so they render with no project open and when EUTONOS is
// not installed. (The old Features page, #features, redirects to Parts.)
const MACHINE_SECTIONS=["m_parts","m_crosstalk","m_claude"];
function featuresActive(){return state.view==="settings"&&MACHINE_SECTIONS.includes(state.settingsSection)}
function featuresProjectIndex(){const p=project();return p&&typeof p.root==="string"&&p.root?state.project:null}   // a project that could not be summarized still has a folder; a private one has no root
function featuresOutcome(result,label){
  const r=result||{};let text,tone="";
  switch(r.outcome){
    case "enabled":text=r.setting==="halt"?"Crosstalk is stopped.":r.setting==="deny"?"That chat is now blocked.":`${label} is now on.`;tone="good";break;
    case "disabled":text=r.setting==="halt"?"Crosstalk is running again.":r.setting==="deny"?"That chat is allowed again.":`${label} is now off.`;tone="good";break;
    case "changed":text=`${label} was changed.`;tone="good";break;
    case "unchanged":text=r.detail||`${label} was already set that way.`;break;
    case "done":text="Marked done.";tone="good";break;
    case "dismissed":text="Dismissed.";tone="good";break;
    case "busy":text=`Another EUTONOS operation is running, so nothing was changed. ${r.reason||""}`.trim();tone="warn";break;
    case "refused":text=`Nothing was changed. ${r.detail||r.reason||"The request was refused."}`;tone="warn";break;
    case "failed":text=`That did not finish. ${r.error||r.detail||""}`.trim();tone="warn";break;
    default:text=r.detail||r.next_action||"EUTONOS gave an answer this page does not recognize.";tone="warn"
  }
  const notes=Array.isArray(r.notes)?r.notes.filter(Boolean):[];
  if(Array.isArray(r.turned_off)&&r.turned_off.length)notes.unshift(`Switched off: ${r.turned_off.join(", ")}.`);
  return {tone,text,next:r.next_action&&r.next_action!==r.detail&&r.next_action!==text?r.next_action:"",notes,pending:Array.isArray(r.pending)?r.pending:[],switches:Array.isArray(r.switches)?r.switches.map(String):[],offer:r.offer==="everything-off"?"everything-off":""};
}
function featuresFeedback(key){
  const f=state.features.feedback[key];if(!f)return "";
  const known=state.features.data?.pending?.actions||[],steps=f.pending.map(id=>known.find(a=>a.id===id)).filter(Boolean);
  const offer=f.offer?`<div class="features-actions"><button type="button" data-feature-everything-off data-feature-focus="everything-off:${esc(key)}"${featuresLocked()?" disabled":""}>Turn everything off</button></div>`:"";
  return `<div class="features-feedback ${esc(f.tone)}" role="status"><p>${esc(f.text)}</p>${f.switches?.length?`<ul>${f.switches.map(s=>`<li>${esc(s)}</li>`).join("")}</ul>`:""}${f.next?`<p class="quiet">${esc(f.next)}</p>`:""}${f.notes.map(n=>`<p class="quiet">${esc(n)}</p>`).join("")}${f.pending.length?`<p>${num(f.pending.length)} follow-up step${f.pending.length===1?"":"s"} waiting for an agent or for you (listed under Pending steps on the Parts page).</p>${steps.length?`<ul>${steps.map(s=>`<li>${esc(s.action)}</li>`).join("")}</ul>`:""}`:""}${offer}</div>`;
}
function featuresLocked(){return Boolean(state.features.busy)||!state.snapshot?.settings_token}
function featuresSwitch(on,busyKey,label,attrs,off=false){
  return `<button type="button" class="features-switch${on?" is-on":""}" role="switch" aria-checked="${on?"true":"false"}" aria-label="${esc(label)}" data-feature-focus="${esc(busyKey)}" ${attrs}${featuresLocked()||off?" disabled":""}><span class="features-switch-knob" aria-hidden="true"></span><span class="features-switch-text">${state.features.busy===busyKey?"Working...":on?"On":"Off"}</span></button>`;
}
function featuresUnavailable(block,what){
  const text=block?.detail||block?.reason||block?.error||"No answer was given.";
  return `<p>${esc(what)} ${esc(text)}</p>${block?.next_action&&block.next_action!==text?`<p class="quiet">${esc(block.next_action)}</p>`:""}`;
}
function featuresRow(title,badge,help,control,extra=""){
  return `<div class="features-row"><div class="features-row-main"><div><strong>${esc(title)}</strong> ${badge}</div>${help?`<p class="quiet">${esc(help)}</p>`:""}${extra}</div>${control}</div>`;
}
function featuresPart(d,id){return (d.components?.components||[]).find(row=>row.id===id)||null}
function featuresPartItem(row){
  const key=`component:${row.id}`,on=Boolean(row.enabled);
  const reason=row.kind==="core"?"Always on. This is the EUTONOS core.":!row.toggleable&&row.state==="installed"?"Always on. It cannot be switched.":row.state==="held"?`On hold${row.reason?`: ${row.reason}`:""}. It cannot be switched yet.`:row.state==="not-installed"?`Not installed${row.reason?`: ${row.reason}`:""}.`:row.state!=="installed"?`Not available (${row.state})${row.reason?`: ${row.reason}`:""}.`:"";
  const badge=row.state!=="installed"?tag(row.state==="held"?"On hold":row.state==="not-installed"?"Not installed":String(row.state)):tag(on?"On":"Off",on?"good":"");
  const control=row.toggleable?featuresSwitch(on,key,row.name||row.id,`data-feature-component="${esc(row.id)}" data-feature-to="${on?"false":"true"}"`):`<span class="features-locked">${tag("Locked")}</span>`;
  const note=!row.toggleable?reason:!on&&row.reason?row.reason:"";
  return `<li class="features-item" data-feature-part="${esc(row.id)}"><div class="features-row"><div class="features-row-main"><div><strong>${esc(row.name||row.id)}</strong> ${badge}</div>${note?`<p class="quiet">${esc(note)}</p>`:""}</div>${control}</div>${featuresFeedback(key)}</li>`;
}
function featuresParts(c){
  if(c.outcome==="not-installed")return `<p>EUTONOS is not installed on this computer, so there are no parts to switch yet.</p><p class="quiet">Install it with Setup; then each part can be switched here.</p>`;
  if(c.outcome!=="ok")return featuresUnavailable(c,"The parts cannot be read right now.");
  return `<ul class="features-list">${(c.components||[]).map(featuresPartItem).join("")||'<li class="quiet">No parts are listed.</li>'}</ul>`;
}
function featuresPartRows(d,id,missing){
  const c=d.components||{},row=featuresPart(d,id);
  if(c.outcome==="not-installed")return `<p>EUTONOS is not installed on this computer yet, so there is nothing to switch. Install it with Setup first.</p>`;
  if(c.outcome!=="ok")return featuresUnavailable(c,"The parts cannot be read right now.");
  return row?`<ul class="features-list">${featuresPartItem(row)}</ul>`:`<p class="quiet">${esc(missing)}</p>`;
}
function featuresPartOff(d){const part=featuresPart(d,"crosstalk");return Boolean(part&&part.state==="installed"&&!part.enabled)}
function featuresOffLine(){return `<p class="features-note" role="status">Crosstalk is off on this computer. Your switches are kept and take effect again when you turn it on.</p>`}
function featuresMessaging(m){
  if(m.outcome!=="ok")return featuresUnavailable(m,"Crosstalk messaging cannot be read right now.");
  const on=Boolean(m.enabled);
  return featuresRow("Messaging between your chats",tag(on?"On":"Off",on?"good":""),"Lets your Claude, Codex and Hermes chats send letters to each other. Off: nothing is sent, and no chat is woken or created for a letter.",featuresSwitch(on,"messaging","Crosstalk messaging",`data-feature-messaging data-feature-to="${on?"false":"true"}"`))
    +(m.notes||[]).filter(n=>!/halted/i.test(n)).map(n=>`<p class="quiet">${esc(n)}</p>`).join("")+featuresFeedback("messaging");
}
// One Crosstalk or Claude switch from the helper's own table (feature_settings.HELPER_SWITCHES).
function featuresHelperRow(row){
  const f=state.features,key=`helper:${row.key}`,can=row.available!==false,lock=featuresLocked()||!can?" disabled":"";
  let control="",badge="",extra="";
  if(row.kind==="bool"){
    const on=Boolean(row.value);badge=can?tag(on?"On":"Off",on?"good":""):tag("Not available");
    control=featuresSwitch(on,key,row.label,`data-feature-helper="${esc(row.key)}" data-feature-to="${on?"false":"true"}"`,!can);
  }else if(row.kind==="choice"){
    const labels=row.choice_labels||{};
    control=`<label class="features-field"><span class="quiet">${esc(row.label)}</span><select data-feature-helper-choice="${esc(row.key)}" data-feature-label="${esc(row.label)}" data-feature-focus="${esc(key)}"${lock}>${(row.choices||[]).map(c=>`<option value="${esc(c)}"${row.value===c?" selected":""}>${esc(labels[c]||c)}</option>`).join("")}</select></label>`;
  }else if(row.kind==="number"){
    control=`<label class="features-field"><span class="quiet">${esc(row.label)}</span><input type="number" inputmode="numeric" min="${esc(row.minimum)}" max="${esc(row.maximum)}" step="1" value="${esc(row.value)}" data-feature-helper-number="${esc(row.key)}" data-feature-label="${esc(row.label)}" data-feature-focus="${esc(key)}"${lock}></label>`;
  }else if(row.kind==="list"){
    const draft=f.listDrafts?.[row.key],text=draft!==undefined?draft:(row.value||[]).join("\n"),dirty=text!==(row.value||[]).join("\n");
    extra=`<textarea rows="3" spellcheck="false" aria-label="${esc(row.label)}" data-feature-helper-list="${esc(row.key)}" data-feature-focus="${esc(key)}-text"${lock}>${esc(text)}</textarea><div class="features-actions"><button type="button" data-feature-helper-list-save="${esc(row.key)}" data-feature-label="${esc(row.label)}" data-feature-focus="${esc(key)}"${lock||!dirty?" disabled":""}>${f.busy===key?"Saving...":"Save"}</button></div>`;
  }
  const help=can?row.help:`${row.help} Not available on this computer: it only acts on Windows.`;
  return `<div class="features-helper" data-helper-key="${esc(row.key)}">${featuresRow(row.label,badge,help,control,extra)}${featuresFeedback(key)}</div>`;
}
// The rows of one page and section, as {plain, advanced} html, or the reason the switches cannot be read.
function featuresRows(d,page,section){
  const sw=d.settings?.["crosstalk-switches"]||{};
  if(sw.outcome!=="ok")return {plain:featuresUnavailable(sw,"The switches cannot be read right now."),advanced:""};
  const rows=(sw.switches||[]).filter(r=>r.page===page&&r.section===section&&r.kind!=="accounts");
  return {plain:rows.filter(r=>!r.advanced).map(featuresHelperRow).join(""),advanced:rows.filter(r=>r.advanced).map(featuresHelperRow).join("")};
}
function featuresAdvanced(id,title,body){return body?`<details class="features-advanced" data-persist="features-${esc(id)}"><summary>${esc(title)}</summary>${body}</details>`:""}
function featuresAccountList(saved){return (state.features.accountsDraft||saved||[]).map(String)}
function featuresCleaned(list){return list.map(x=>x.trim()).filter(Boolean)}
function featuresAccounts(l,notes){
  if(l.outcome!=="ok")return featuresUnavailable(l,"The linked accounts cannot be read right now.");
  const f=state.features,saved=l.accounts||[],list=featuresAccountList(saved),cleaned=featuresCleaned(list),dirty=JSON.stringify(cleaned)!==JSON.stringify(saved),lock=featuresLocked()?" disabled":"";
  const rows=list.map((value,i)=>`<li class="features-account"><label><span class="quiet">Account ${i+1}</span><input type="text" value="${esc(value)}" data-feature-account="${i}" data-feature-focus="account:${i}" autocomplete="off" spellcheck="false" placeholder="you@example.com"></label><button type="button" data-feature-account-remove="${i}" data-feature-focus="account-remove:${i}"${lock}>Remove</button></li>`).join("");
  const sug=f.suggest,offered=sug?(sug.emails||[]).filter(e=>!cleaned.some(x=>x.toLowerCase()===e.toLowerCase())):[];
  const suggestion=sug?`<div class="features-suggest" role="status"><p><strong>Suggestions.</strong> ${offered.length?"These are accounts Crosstalk has seen in your Claude chats. Adding one only puts it in the list above; nothing is saved until you press Save.":esc(sug.error||"No new accounts to suggest.")}</p>${offered.length?`<ul>${offered.map((e,i)=>`<li>${esc(e)} <button type="button" data-feature-suggest-add="${i}" data-feature-focus="suggest-add:${i}"${lock}>Add to list</button></li>`).join("")}</ul>`:""}</div>`:"";
  return `<p>${esc(notes?.shared_login||"")}</p>`
    +`<p class="quiet">${cleaned.length?"Crosstalk links these accounts to keep chat history across a switch between them.":"No accounts are linked, so this is off."}${dirty?" You have changes that are not saved yet.":""}</p>`
    +`<ul class="features-accounts" aria-label="Linked Claude accounts">${rows||'<li class="quiet">The list is empty.</li>'}</ul>`
    +`<div class="features-actions"><button type="button" data-feature-account-add data-feature-focus="account-add"${lock}>Add an account</button><button type="button" data-feature-suggest data-feature-focus="suggest"${lock}>Suggest</button><button type="button" data-feature-accounts-save data-feature-focus="accounts-save"${lock||!dirty?" disabled":""}>${f.busy==="accounts"?"Saving...":"Save"}</button></div>`
    +`${dirty&&!cleaned.length?'<p class="quiet">Saving an empty list turns linked accounts off.</p>':""}${suggestion}${featuresFeedback("accounts")}`;
}
function featuresUpdates(u){
  if(!u||u.outcome!=="ok")return featuresUnavailable(u||{},"The update setting cannot be read right now.");
  const labels=u.labels||{},modes=u.modes||["manual","notify","auto"],lock=featuresLocked()?" disabled":"";
  const options=modes.map(m=>`<option value="${esc(m)}"${u.mode===m?" selected":""}>${esc(labels[m]||m)}</option>`).join("");
  const found=u.last_available?`<p>EUTONOS ${esc(u.last_available.version)} is available${u.staged?" and already downloaded and checked":""}. The companion asks before installing it.</p>`:"";
  const last=u.last_checked?`Last checked ${esc(u.last_checked)}.`:"Not checked yet.";
  return `<div class="features-row"><div class="features-row-main"><label><strong>How EUTONOS updates</strong><br><select data-feature-update-mode data-feature-focus="update-mode"${lock}>${options}</select></label>`
    +`<p class="quiet">Manual: nothing runs on its own. Check and notify: a check about once a day tells you when a newer release is available. Check, download, then ask: the same check also downloads and verifies the release in the background, then asks before installing. Nothing is ever installed without your click.</p>`
    +`<p class="quiet">The daily check runs inside the EUTONOS companion, so it needs the companion switched on (see Parts). ${last} Releases come from ${esc(u.repo||"")}.</p>${found}</div></div>${featuresFeedback("update-mode")}`;
}
function featuresPending(p){
  if(p.outcome!=="ok")return featuresUnavailable(p,"The pending steps cannot be read right now.");
  const lock=featuresLocked()?" disabled":"";
  const rows=(p.actions||[]).map(a=>`<li class="features-item features-step" data-pending-id="${esc(a.id)}"><div class="features-row"><div class="features-row-main"><div><code>${esc(a.id)}</code> ${tag(a.harness||"any")} <span class="quiet">${esc(a.source||"")}</span></div><p>${esc(a.action)}</p>${a.why?`<p class="quiet">Why: ${esc(a.why)}</p>`:""}</div><div class="features-actions"><button type="button" data-feature-pending="done" data-feature-pending-id="${esc(a.id)}" data-feature-focus="pending-done:${esc(a.id)}"${lock}>Done</button><button type="button" data-feature-pending="dismiss" data-feature-pending-id="${esc(a.id)}" data-feature-focus="pending-dismiss:${esc(a.id)}"${lock}>Dismiss</button></div></div></li>`).join("");
  const shown=(p.actions||[]).length,more=p.count>shown?`<p class="quiet">${num(p.count-shown)} more are waiting and not shown here.</p>`:"";
  return `${rows?`<ul class="features-list">${rows}</ul>`:'<p class="quiet">Nothing is waiting.</p>'}${more}${featuresFeedback("pending")}`;
}
function featuresDeny(c){
  const f=state.features,lock=featuresLocked()?" disabled":"",list=Array.isArray(c.deny)?c.deny:[];
  const rows=list.map(k=>`<li class="features-account"><span><code>${esc(k)}</code></span><button type="button" data-feature-deny="allow" data-feature-deny-key="${esc(k)}" data-feature-focus="deny-allow:${esc(k)}"${lock}>Allow again</button></li>`).join("");
  return `<div class="features-helper"><p><strong>Blocked chats</strong> ${tag(String(list.length),list.length?"warn":"")}</p><p class="quiet">Chats that never receive letters or Crosstalk actions. Use the chat key, such as claude:... , codex:... or hermes:... .</p><ul class="features-accounts" aria-label="Blocked chats">${rows||'<li class="quiet">No chats are blocked.</li>'}</ul><div class="features-actions"><input type="text" value="${esc(f.denyDraft||"")}" data-feature-deny-input data-feature-focus="deny-input" autocomplete="off" spellcheck="false" placeholder="claude:..." aria-label="Chat key to block"${lock}><button type="button" data-feature-deny="block" data-feature-focus="deny-block"${lock}>Block this chat</button></div>${featuresFeedback("deny")}</div>`;
}
function featuresWakeBudget(c){
  const f=state.features,lock=featuresLocked()?" disabled":"",saved=c.wake_budget||{},draft=f.budgetDraft||{},limits=c.wake_budget_limits||{};
  const fields=[["per_chat","Wakes per chat"],["total","Wakes in total"],["window_s","Window (seconds)"]];
  const dirty=fields.some(([name])=>draft[name]!==undefined&&Number(draft[name])!==Number(saved[name]));
  const inputs=fields.map(([name,label])=>`<label class="features-field"><span class="quiet">${esc(label)}</span><input type="number" inputmode="numeric" step="1"${limits[name]?` min="${esc(limits[name][0])}" max="${esc(limits[name][1])}"`:""} value="${esc(draft[name]!==undefined?draft[name]:saved[name])}" data-feature-budget="${name}" data-feature-focus="budget:${name}"${lock}></label>`).join("");
  return `<div class="features-helper"><p><strong>Wake budget</strong></p><p class="quiet">How often sleeping chats may be woken: at most this many wakes per chat and in total within the window.</p><div class="features-fields">${inputs}</div><div class="features-actions"><button type="button" data-feature-budget-save data-feature-focus="budget-save"${lock||!dirty?" disabled":""}>${f.busy==="wake-budget"?"Saving...":"Save"}</button></div>${featuresFeedback("wake-budget")}</div>`;
}
function featuresRecentLetters(c){
  const letters=Array.isArray(c.recent_letters)?c.recent_letters:[];
  const rows=letters.map(l=>`<div class="list-item"><div><strong>${esc(l.letter_id)}</strong><p>${esc(l.from||"?")} → ${esc(l.to||"?")} · ${l.ts_ns?esc(when(Math.floor(l.ts_ns/1e6))):"time unknown"}</p></div>${tag(l.state||"unknown",l.state==="arrived"?"good":"")}</div>`).join("");
  return `<div class="list">${rows||'<p class="quiet">No letters recorded yet.</p>'}</div>`;
}
function featuresSection(id,title,intro,body){
  return `<section class="card features-section" id="features-${id}" aria-labelledby="features-${id}-title"><div class="section-head"><div><h2 id="features-${id}-title">${esc(title)}</h2><p class="quiet">${esc(intro)}</p></div></div>${body}</section>`;
}
function featuresFrame(title,description,build){
  const f=state.features,head=heading(title,description,`<button type="button" data-features-refresh>Check again</button>`);
  if(f.error)return head+empty("This page is unavailable",f.error)+'<p><button type="button" data-features-retry>Try again</button></p>';
  if(!f.data)return head+empty("Loading","Reading what is switched on...");
  const token=state.snapshot?.settings_token,
    gate=!token?`<p class="features-note" role="status">${state.snapshot?"Settings token unavailable; refresh the dashboard to turn things on or off.":"The dashboard is still starting. The switches unlock when it has loaded."}</p>`:"",
    stale=f.refreshError?`<p class="features-note" role="status">The last refresh failed (${esc(f.refreshError)}). What is shown may be out of date.</p>`:"";
  return head+gate+stale+build(f.data);
}
function featuresPartsPage(){
  return featuresFrame("Parts","Setup installs every part of EUTONOS. These switches only turn parts on or off; nothing here installs or removes a part. These settings belong to this computer.",d=>{
    const pendingCount=d.pending?.outcome==="ok"?d.pending.count:0;
    return featuresSection("parts","Parts","Each part of EUTONOS that Setup installed. Switching one off undoes what it set up outside the EUTONOS folder; switching it on sets that up again.",featuresParts(d.components||{})+featuresFeedback("everything"))
      +featuresSection("updates","Updates","How EUTONOS keeps itself up to date. It never installs an update without asking you first.",featuresUpdates(d.settings?.["update-mode"]))
      +featuresSection("pending",`Pending steps${pendingCount?` (${num(pendingCount)})`:""}`,"Steps a switch could not finish itself. An agent or you carries each one out, then marks it done.",featuresPending(d.pending||{}));
  });
}
function featuresCrosstalkPage(){
  return featuresFrame("Crosstalk","Letters between your Claude, Codex and Hermes chats, and the helper that does the Claude Desktop jobs. These settings belong to this computer.",d=>{
    const c=d.settings?.["crosstalk-controls"]||{},lock=featuresLocked()?" disabled":"",part=featuresPart(d,"crosstalk");
    const control=c.outcome==="ok"?`<div class="cross-talk-status"><span class="status ${c.halt?"warn":c.idle?"":"good"}">${esc(c.summary)}</span><div class="cross-talk-status-actions"><button type="button"${c.halt?"":' class="secondary"'} data-feature-halt data-feature-to="${c.halt?"false":"true"}" data-feature-focus="halt"${lock}>${state.features.busy==="halt"?"Working...":c.halt?"Resume Crosstalk":"Stop Crosstalk"}</button></div></div>${c.halt?'<p class="features-halt" role="status"><strong>Stopped.</strong> Nothing Crosstalk does will run until you resume.</p>':""}${featuresFeedback("halt")}`:featuresUnavailable(c,"The Crosstalk status cannot be read right now.");
    const off=featuresPartOff(d)?featuresOffLine():"";
    const everything=part?`<div class="features-actions"><button type="button" class="secondary" data-feature-everything-off data-feature-focus="everything-off:page"${lock}>Turn everything off</button></div><p class="quiet">Switches messaging and every Crosstalk and Claude switch off, then the Crosstalk part. Your stop, blocked chats, wake budget and linked accounts stay as they are.</p>`:"";
    const letters=featuresRows(d,"crosstalk","letters"),apps=featuresRows(d,"crosstalk","apps"),adv=featuresRows(d,"crosstalk","advanced");
    const lettersAdvanced=c.outcome==="ok"?featuresAdvanced("letters-advanced","Blocked chats and wake budget",featuresDeny(c)+featuresWakeBudget(c)):"";
    return featuresSection("status","Crosstalk on this computer","The part, and your stop. Turning the part off keeps your switches; they take effect again when you turn it back on.",featuresPartRows(d,"crosstalk","The Crosstalk part is not available on this computer.")+off+control+everything+featuresFeedback("everything"))
      +featuresSection("letters","Letters","What letters between chats may do.",featuresMessaging(d.settings?.["crosstalk-messaging"]||{})+letters.plain+lettersAdvanced+letters.advanced)
      +featuresSection("apps","Apps","Settings for one app's chats. Claude Desktop jobs are on the Claude page.",`<h3>Hermes</h3>${apps.plain}${apps.advanced}`)
      +featuresSection("advanced","Advanced","Rarely changed.",adv.plain+adv.advanced||'<p class="quiet">Nothing here.</p>')
      +featuresSection("recent","Recent letters","The newest letters, as metadata only (ids, from, to, state, time). Never a letter's text.",c.outcome==="ok"?featuresRecentLetters(c):featuresUnavailable(c,"The letters cannot be read right now."))
      +`<p class="quiet features-link">Claude Desktop jobs (Remote Control, chat names, chat copying) are on the <a href="#settings" data-settings-nav="m_claude">Claude page</a>.</p>`;
  });
}
function featuresClaudePage(){
  return featuresFrame("Claude","Everything that only exists because Claude Desktop does. These jobs run inside the Crosstalk helper, so they stop with Stop Crosstalk and while the Crosstalk part is off. These settings belong to this computer.",d=>{
    const sw=d.settings?.["crosstalk-switches"]||{},c=d.settings?.["crosstalk-controls"]||{};
    const notWin=sw.outcome==="ok"&&sw.windows===false?`<p class="features-note" role="status">These jobs drive Claude Desktop and only act on Windows. Their switches are shown below but cannot be changed on this computer.</p>`:"";
    const stopped=c.outcome==="ok"&&c.halt?`<p class="features-halt" role="status"><strong>Crosstalk is stopped.</strong> None of these jobs will run until you resume it on the <a href="#settings" data-settings-nav="m_crosstalk">Crosstalk page</a>.</p>`:"";
    const env=(sw.environment||[]).length?`<p class="features-note" role="status">${(sw.environment||[]).map(esc).join(" ")}</p>`:"";
    const accounts=featuresRows(d,"claude","accounts"),remote=featuresRows(d,"claude","remote"),names=featuresRows(d,"claude","names"),copy=featuresRows(d,"claude","copy"),signin=featuresRows(d,"claude","signin");
    const legacy=sw.legacy_copier?.found?`<p class="features-note" role="status">Your personal chat copier is still in the Startup folder (${(sw.legacy_copier.files||[]).map(esc).join(", ")}). Remove that shortcut and stop its watcher before you turn EUTONOS chat copying on; two copiers on the same folders would fight.</p>`:"";
    return notWin+(featuresPartOff(d)?featuresOffLine():"")+stopped+env
      +featuresSection("accounts","Accounts","Your own Claude logins, so a chat's history follows you across an account switch.",featuresAccounts(d.settings?.["linked-accounts"]||{},d.notes)+accounts.plain+accounts.advanced)
      +featuresSection("remote","Remote Control","Start Claude chats from the web or your phone, and keep it on after Desktop restarts.",featuresPartRows(d,"remote-control","The Remote Control launcher is not available on this computer.")+remote.plain+featuresAdvanced("remote-advanced","Advanced",remote.advanced))
      +featuresSection("names","Chat names","Make the phone show each chat's real title.",names.plain+featuresAdvanced("names-advanced","Advanced",names.advanced))
      +featuresSection("copy","Chat copying","Keep the chat lists of your linked accounts identical. Needs two linked accounts (see Accounts above). Off until you turn it on.",legacy+copy.plain+copy.advanced)
      +featuresSection("signin","Sign-in","The Claude command-line tool and Claude Desktop can be signed in to different accounts.",signin.plain+signin.advanced);
  });
}
function featuresPage(){
  const section=state.settingsSection;
  return section==="m_crosstalk"?featuresCrosstalkPage():section==="m_claude"?featuresClaudePage():featuresPartsPage();
}
async function loadFeatures(force=false){
  const f=state.features;if(f.loading||(f.data&&!force))return;
  const index=featuresProjectIndex(),serial=++f.serial;
  f.loading=true;f.error=null;f.project=index;
  try{
    const response=await fetch(index===null?"/api/v1/features":`/api/v1/features?project=${encodeURIComponent(index)}`,{cache:"no-store"}),body=await response.json().catch(()=>({}));
    if(serial!==f.serial)return;
    if(!response.ok)throw new Error(body.message||`Dashboard service returned ${response.status}`);
    if(!body||typeof body!=="object"||!body.components||!body.settings)throw new Error("The features could not be read.");
    f.data=body;f.refreshError=null;
  }catch(error){if(serial===f.serial){if(f.data)f.refreshError=error.message||"unknown error";else f.error=error.message||"The features could not be loaded."}}
  finally{if(serial===f.serial){f.loading=false;if(featuresActive())renderFeatures()}}
}
function renderFeatures(){
  const f=state.features,active=document.activeElement,all=selector=>Array.from(content.querySelectorAll?.(selector)||[]);
  if(active?.dataset?.featureFocus)f.focus=active.dataset.featureFocus;
  const opened=new Set(all("details[data-persist][open]").map(x=>x.dataset.persist));
  document.querySelectorAll("#primary-nav a[data-view]").forEach(a=>a.setAttribute("aria-current",a.dataset.view==="settings"?"page":"false"));
  syncSettingsNav();syncBoardsNav();
  content.innerHTML=featuresPage();
  all("details[data-persist]").forEach(x=>{if(opened.has(x.dataset.persist))x.open=true});
  if(f.focus){const hit=all("[data-feature-focus]").find(e=>e.dataset.featureFocus===f.focus);if(hit&&!hit.disabled)hit.focus?.({preventScroll:true})}
  if(f.stale&&!f.loading){f.stale=false;loadFeatures(true)}
  else if(!f.loading&&!f.error&&(!f.data||f.project!==featuresProjectIndex()))loadFeatures(true);
}
async function featuresPost(name,body){
  const token=state.snapshot?.settings_token;if(!token)throw new Error("Settings token unavailable; refresh the dashboard.");
  const response=await fetch(`/api/v1/features/${name}`,{method:"POST",headers:{"Content-Type":"application/json","X-Eutonos-Settings":token},body:JSON.stringify(body)});
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.message||`The dashboard did not accept that request (${response.status}${data.outcome?`, ${data.outcome}`:""}). Refresh the page and try again.`);
  return data;
}
async function featuresAct(name,body,key,label,after){
  const f=state.features;if(f.busy)return null;
  const feedbackKey=key.startsWith("ram:")?"ram":key;
  f.busy=key;delete f.feedback[feedbackKey];if(key==="everything")delete f.feedback["component:crosstalk"];renderFeatures();
  let result=null;
  try{result=await featuresPost(name,body);if(result?.outcome!=="ok")f.feedback[feedbackKey]=featuresOutcome(result,label);if(after)after(result)}
  catch(error){f.feedback[feedbackKey]={tone:"warn",text:error.message||"The request could not be sent.",next:"",notes:[],pending:[],switches:[],offer:""}}
  finally{f.busy=null;f.serial++;f.loading=false;await loadFeatures(true);if(featuresActive())renderFeatures()}
  return result;
}
function featuresPartName(id){const row=(state.features.data?.components?.components||[]).find(x=>x.id===id);return row?.name||id}
// Registered once at startup (a function, so the file has no top-level side effect past its own start).
function wireFeatures(){
  content.addEventListener("input",event=>{
    if(!featuresActive())return;
    const f=state.features,target=event.target,hit=selector=>target.closest?.(selector);
    const input=hit("[data-feature-account]");
    if(input){if(!f.accountsDraft)f.accountsDraft=featuresAccountList(f.data?.settings?.["linked-accounts"]?.accounts);f.accountsDraft[Number(input.dataset.featureAccount)]=input.value;return}
    if(hit("[data-feature-deny-input]")){f.denyDraft=hit("[data-feature-deny-input]").value;return}
    const budget=hit("[data-feature-budget]");
    if(budget){f.budgetDraft={...(f.budgetDraft||{}),[budget.dataset.featureBudget]:budget.value};return}
    const list=hit("[data-feature-helper-list]");
    if(list){f.listDrafts={...(f.listDrafts||{}),[list.dataset.featureHelperList]:list.value}}
  });
  content.addEventListener("change",event=>{
    if(!featuresActive())return;
    const hit=selector=>event.target.closest?.(selector);let el;
    if(el=hit("[data-feature-update-mode]")){featuresAct("update-mode",{mode:el.value},"update-mode","The update setting");return}
    if(el=hit("[data-feature-helper-choice]")){const key=el.dataset.featureHelperChoice;featuresAct("helper-switch",{key,value:el.value},`helper:${key}`,el.dataset.featureLabel||key);return}
    if(el=hit("[data-feature-helper-number]")){
      const key=el.dataset.featureHelperNumber,value=el.value.trim()===""?NaN:Number(el.value);
      if(!Number.isFinite(value)){state.features.feedback[`helper:${key}`]={tone:"warn",text:"Nothing was changed. Enter a number.",next:"",notes:[],pending:[],switches:[],offer:""};renderFeatures();return}
      featuresAct("helper-switch",{key,value},`helper:${key}`,el.dataset.featureLabel||key);
    }
  });
  content.addEventListener("click",event=>{
    if(!featuresActive())return;
    const f=state.features,hit=selector=>event.target.closest(selector),saved=()=>f.data?.settings?.["linked-accounts"]?.accounts;
    let el;
    if(hit("[data-features-retry]")){f.error=null;f.data=null;renderFeatures();return}
    if(hit("[data-features-refresh]")){f.stale=true;f.refreshError=null;renderFeatures();return}
    if(el=hit("[data-feature-component]")){const id=el.dataset.featureComponent;featuresAct("component",{component:id,enabled:el.dataset.featureTo==="true"},`component:${id}`,featuresPartName(id));return}
    if(hit("[data-feature-everything-off]")){featuresAct("everything-off",{},"everything","Crosstalk");return}
    if(hit("[data-feature-messaging]")){featuresAct("messaging",{enabled:hit("[data-feature-messaging]").dataset.featureTo==="true"},"messaging","Crosstalk messaging");return}
    if(el=hit("[data-feature-halt]")){featuresAct("halt",{halt:el.dataset.featureTo==="true"},"halt","The stop");return}
    if(el=hit("[data-feature-helper]")){const key=el.dataset.featureHelper,row=(f.data?.settings?.["crosstalk-switches"]?.switches||[]).find(r=>r.key===key);featuresAct("helper-switch",{key,value:el.dataset.featureTo==="true"},`helper:${key}`,row?.label||key);return}
    if(el=hit("[data-feature-helper-list-save]")){
      const key=el.dataset.featureHelperListSave,text=f.listDrafts?.[key]??"",items=text.split(/[\n,]/).map(x=>x.trim()).filter(Boolean);
      featuresAct("helper-switch",{key,value:items},`helper:${key}`,el.dataset.featureLabel||key,result=>{if(["changed","unchanged"].includes(result.outcome)&&f.listDrafts)delete f.listDrafts[key]});return;
    }
    if(el=hit("[data-feature-deny]")){
      const action=el.dataset.featureDeny,key=action==="allow"?el.dataset.featureDenyKey:(f.denyDraft||"").trim();
      if(!key){f.feedback.deny={tone:"warn",text:"Nothing was changed. Type the chat key to block.",next:"",notes:[],pending:[],switches:[],offer:""};renderFeatures();return}
      featuresAct("deny",{action,key},"deny","The blocked list",result=>{if(["enabled","changed","unchanged"].includes(result.outcome)&&action==="block")f.denyDraft=""});return;
    }
    if(hit("[data-feature-budget-save]")){
      const body={};for(const name of ["per_chat","total","window_s"]){const v=f.budgetDraft?.[name];if(v!==undefined){const n=Number(v);if(!Number.isInteger(n)){f.feedback["wake-budget"]={tone:"warn",text:"Nothing was changed. Use whole numbers.",next:"",notes:[],pending:[],switches:[],offer:""};renderFeatures();return}body[name]=n}}
      if(!Object.keys(body).length)return;
      featuresAct("wake-budget",body,"wake-budget","The wake budget",result=>{if(["changed","unchanged"].includes(result.outcome))f.budgetDraft=null});return;
    }
    if(el=hit("[data-feature-ram]")){const which=el.dataset.featureRam,index=featuresProjectIndex();if(index===null)return;featuresAct("ram",{project:index,[which]:el.dataset.featureTo==="true"},`ram:${which}`,which==="capture"?"RAM capture":"RAM retrieval");return}
    if(el=hit("[data-feature-pending]")){featuresAct("pending",{id:el.dataset.featurePendingId,action:el.dataset.featurePending},"pending","The step");return}
    if(hit("[data-feature-account-add]")){f.accountsDraft=[...featuresAccountList(saved()),""];renderFeatures();return}
    if(el=hit("[data-feature-account-remove]")){const list=featuresAccountList(saved());list.splice(Number(el.dataset.featureAccountRemove),1);f.accountsDraft=list;renderFeatures();return}
    if(el=hit("[data-feature-suggest-add]")){const list=featuresAccountList(saved()),cleaned=featuresCleaned(list),offered=(f.suggest?.emails||[]).filter(e=>!cleaned.some(x=>x.toLowerCase()===e.toLowerCase())),email=offered[Number(el.dataset.featureSuggestAdd)];if(email)f.accountsDraft=[...cleaned,email];renderFeatures();return}
    if(hit("[data-feature-suggest]")){featuresAct("linked-accounts",{action:"suggest"},"accounts","Linked accounts",result=>{f.suggest=result.outcome==="ok"?{emails:result.suggested_emails||[]}:{emails:[],error:result.detail||result.reason||"Suggestions are not available right now."}});return}
    if(hit("[data-feature-accounts-save]")){featuresAct("linked-accounts",{action:"save",accounts:featuresCleaned(featuresAccountList(saved()))},"accounts","Linked accounts",result=>{if(["enabled","disabled","unchanged"].includes(result.outcome)){f.accountsDraft=null;f.suggest=null}});return}
  });
}
// end Features
