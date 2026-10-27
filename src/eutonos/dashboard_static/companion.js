"use strict";
const comparisonState = {experiment:null,role:"support",stage:"all",tab:new URLSearchParams(location.search).has("benchmark")?"side-by-side":"tests",testType:new URLSearchParams(location.search).get("testType"),task:"all",primary:"all",support:"all",baseline:"",period:"all"};
const knownNumber = x => x == null ? "Unknown" : num(x);
const usageNumber = (row,key) => row.usage?.[key]!=null ? knownNumber(row.usage[key]) : row.usage_trust==="investigate"&&row.usage_candidate?.[key]!=null ? `${knownNumber(row.usage_candidate[key])}*` : "Unknown";
const dollars = x => x == null ? "Unknown" : new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",minimumFractionDigits:4,maximumFractionDigits:4}).format(x);
function sumKnown(rows,key){return rows.length && rows.every(r=>r.usage?.[key]!=null) ? rows.reduce((n,r)=>n+r.usage[key],0) : null}
function horizontalBars(items,label,format=num,scaleMax=null){
  const max=scaleMax??Math.max(1e-9,...items.map(x=>x.value||0));
  return `<div class="comparison-bars" role="group" aria-label="${esc(label)}">${items.map((x,i)=>`<div class="comparison-bar"><div><span>${esc(x.label)}</span><strong>${esc(x.value==null?"Unknown":format(x.value))}</strong></div><svg viewBox="0 0 600 14" preserveAspectRatio="none" aria-hidden="true"><rect width="600" height="14" rx="4" class="bar-track"/><rect width="${x.value==null?0:600*x.value/max}" height="14" rx="4" class="model-${i}"/></svg></div>`).join("")}</div>`;
}
function toolInventory(row){
  if(row.tool_counts==null)return '<p class="quiet">Tool breakdown unavailable for this retained run.</p>';
  const items=Object.entries(row.tool_counts).map(([label,value])=>({label,value}));
  if(!items.length)return '<p class="quiet">No native tool calls recorded.</p>';
  return `<h3>Tools used</h3>${horizontalBars(items,"Native tool calls by name")}<p class="quiet">Native tool names only. A wrapper may contain several commands; their arguments and output are not collected.</p><details><summary>Call order</summary><ol>${(row.tool_sequence||[]).map(name=>`<li>${esc(name)}</li>`).join("")}</ol></details>`;
}
function companion(p){
  const data=p.modules.companion;
  if(comparisonState.tab==="tests")return benchmarkLibrary(data||{},p.modules.user_tests||{});
  if(comparisonState.tab==="side-by-side")return sideBySideComparison(data||{});
  if(data?.status!=="available")return heading("Companion lab","Understand the work delegated, the context returned and its potential cost.")+labTabs()+empty("No results connected",data?.reason||"Connecting a local usage log is available in a EUTONOS source checkout (tools/companion_results.py add-source); there is no installed command for it yet. New turns then appear on refresh; no website rebuild is needed.");
  if(comparisonState.tab==="pairs")return pairingComparison(data);
  if(comparisonState.tab==="history")return companionHistory(data);
  const experiments=data.experiments||[];
  if(!experiments.some(x=>x.id===comparisonState.experiment))comparisonState.experiment=experiments[0]?.id;
  const e=experiments.find(x=>x.id===comparisonState.experiment)||{};
  const all=data.runs.filter(x=>x.experiment_id===e.id&&x.role===comparisonState.role);
  const complete=all.filter(x=>x.state==="completed");
  if(comparisonState.stage!=="all"&&!complete.some(x=>x.id===comparisonState.stage))comparisonState.stage="all";
  const chosen=comparisonState.stage==="all"?complete:complete.filter(x=>x.id===comparisonState.stage);
  const input=sumKnown(chosen,"input_tokens"),cached=sumKnown(chosen,"cached_input_tokens"),output=sumKnown(chosen,"output_tokens"),reasoning=sumKnown(chosen,"reasoning_output_tokens");
  const handoff=chosen.length&&chosen.every(x=>x.handoff_tokens!=null)?chosen.reduce((n,x)=>n+x.handoff_tokens,0):null;
  const costRows=chosen.map(r=>({label:shortModel(r.models?.[0])+" · "+r.stage,value:r.api_estimate_usd}));
  const reads=data.runs.filter(x=>x.experiment_id===e.id&&x.role==="primary").flatMap(x=>x.followup_reads||[]);
  const readKinds=["verification","missing-context","incorrect-context","new-scope"];

  const rates=data.pricing;
  return heading("Companion lab","Follow the evidence from discovery to handoff. Compare cost without guessing quality.",status(e.kind||"Exploratory","warn"))+labTabs()+
  `<div class="comparison-controls card"><label>Experiment<select id="comparison-experiment">${experiments.map(x=>`<option value="${esc(x.id)}" ${x.id===e.id?"selected":""}>${esc(x.label)}</option>`).join("")}</select></label><label>Agent role<select id="comparison-role"><option value="support" ${comparisonState.role==="support"?"selected":""}>Support agent</option><option value="primary" ${comparisonState.role==="primary"?"selected":""}>Primary agent</option></select></label><label>Work included<select id="comparison-stage"><option value="all">All completed stages</option>${complete.map(x=>`<option value="${esc(x.id)}" ${comparisonState.stage===x.id?"selected":""}>${esc(x.stage)} · ${esc(new Date(x.start).toLocaleTimeString())}</option>`).join("")}</select></label></div>`+
  outputReview(e)+
  `<div class="grid stats">${stat("Input across calls",knownNumber(input),cached==null?"Cached breakdown unknown":`${num(cached)} included cached`)}${stat("Reasoning output",knownNumber(reasoning),`${knownNumber(output)} total output; reasoning included`)}${stat(comparisonState.role==="support"?"Returned handoff ≈":"Final response ≈",knownNumber(handoff),"Final output proxy, not primary billed input")}${stat("Matched sample savings",dollars(data.comparisons?.find(c=>c.experiment_id===e.id)?.savings_usd),"Only an actual matched baseline qualifies")}</div>`+
  `<div class="grid two comparison-main"><section class="card"><div class="section-head"><h2>Observed model cost</h2>${tag("API equivalent")}</div><p class="quiet">Each stage uses its actual model and observed tokens. Other model pairings require separate runs.</p>${horizontalBars(costRows,"Observed stage API equivalent",dollars)}<p class="quiet">Unknown for incomplete counters, mixed models or peak input above 272K. These dated standard rates are not your Codex bill.</p></section><section class="card"><div class="section-head"><h2>Where input went</h2></div>${horizontalBars([{label:"Uncached input",value:input==null||cached==null?null:input-cached},{label:"Cached input",value:cached}],"Input decomposition")}<p class="quiet">Repeated context counts on every call. Cached input is already inside the total.</p><div class="comparison-boundary"><strong>What we can conclude</strong><p>${esc(e.quality||"Quality not reviewed")}</p><span>Actual account charge: unknown<br>Codex credits used: unknown<br>Parent/child counter inclusion: unknown</span></div></section></div>`+
  `<section class="card comparison-section"><div class="section-head"><h2>Stages and findings</h2>${tag(`${all.length} observed turns`)}</div><p class="quiet">Only completed stages enter the comparison. Expand a stage for source identity and context coverage. An asterisk marks a preserved native counter that still needs investigation and is excluded from totals and prices.</p>${all.length?all.map(x=>`<details class="comparison-stage" data-persist="${esc(x.id)}"><summary><div><strong>${esc(x.stage)}</strong><p>${esc(x.models.map(m=>rates.rates.find(r=>r.model===m)?.label||m).join(", ")||"Unknown model")} · ${esc(x.state)} · ${x.elapsed_seconds==null?"Time pending":Math.round(x.elapsed_seconds)+" sec"}</p></div><span>${usageNumber(x,"input_tokens")} input</span></summary><p>${esc(x.finding)}</p><div class="meta">${tag(`${knownNumber(x.first_input)} first-call input`)}${tag(`${knownNumber(x.max_input)} peak context`)}${tag(`${knownNumber(x.handoff_tokens)} handoff ≈`)}${tag(`${x.tool_calls} tool calls`)}</div>${toolInventory(x)}<dl class="evidence-list"><dt>Native turn</dt><dd>${esc(x.id)}</dd><dt>Source fingerprint</dt><dd>${esc(x.source_prefix_sha256)}</dd><dt>Usage trust</dt><dd>${esc(x.usage_trust||"legacy/unknown")}</dd><dt>Coverage</dt><dd>${esc(x.coverage)}; ${esc(x.handoff_basis)}</dd><dt>Quality</dt><dd>${esc(x.quality)}</dd></dl>${x.gaps.length?`<div class="callout">${esc(x.gaps.join("; "))}</div>`:""}</details>`).join(""):empty("No observed turns for this role","Primary and support usage stay separate. Select another role or connect its explicit native log.")}</section>`+
  `<div class="grid two comparison-section"><section class="card"><h2>Extra primary discovery</h2><p class="quiet">Primary read annotations across this experiment, including in-progress work. Normal checking and missing packet information stay separate.</p>${reads.length?horizontalBars(readKinds.map(k=>({label:k.replaceAll("-"," "),value:reads.filter(x=>x.reason===k).length})),"Recorded follow-up read reasons"):empty("Not recorded yet","No read coverage means unknown, not zero extra work.")}${reads.map(x=>`<p class="read-evidence"><strong>${esc(x.reason)}</strong> · ${esc(x.source)}<br>${esc(x.note||"")} · ${knownNumber(x.tokens)} tokens</p>`).join("")}</section><section class="card"><h2>Quality before savings</h2><p>Compare fresh primary runs on the same task and source revision, with companion on and off.</p><ul class="quality-list"><li>Answer correctness and missing constraints</li><li>Evidence routes and unsupported claims</li><li>Implementation outcome and repair work</li><li>Fresh-worker pickup from RAM and source</li></ul><p class="quiet">A shorter packet alone does not pass these checks. No quality score is inferred from token counts.</p></section></div>`+
  capturePanel(data.capture)+
  `<details class="card comparison-section" data-persist="rates"><summary><strong>Rates, assumptions and sources</strong><span class="quiet">Checked ${esc(rates.checked_at)}</span></summary><p>${esc(rates.basis)} · USD per million tokens</p><div class="table-scroll"><table class="comparison-table"><thead><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead><tbody>${rates.rates.map(r=>`<tr><td><a href="${esc(r.source)}" target="_blank" rel="noreferrer">${esc(r.label)}</a></td><td>$${r.input.toFixed(2)}</td><td>$${r.cached.toFixed(2)}</td><td>$${r.output.toFixed(2)}</td></tr>`).join("")}</tbody></table></div><p class="quiet">${esc(rates.limits)}</p><p class="quiet">These are frozen rate assumptions. The dashboard makes no pricing network calls and never represents this estimate as your bill. Native model configuration is evidence of the requested model, not backend attestation.</p></details>`+
  (data.diagnostics.length?`<div class="callout comparison-section">${data.diagnostics.map(esc).join("<br>")}</div>`:"");
}
function capturePanel(data){
  const receipts=data?.receipts||[];
  const superseded=new Set(receipts.map(r=>r.supersedes).filter(Boolean));
  const active=receipts.filter(r=>!superseded.has(r.receipt_id));
  const complete=new Set(active.filter(r=>r.stage==="completed"&&["captured","no-op"].includes(r.disposition)).map(r=>r.thread_id+":"+r.turn_id));
  const pending=active.filter(r=>r.disposition==="gap"||(r.stage==="pre-final"&&!complete.has(r.thread_id+":"+r.turn_id)));
  return `<section class="card comparison-section"><div class="section-head"><h2>RAM closeout coverage</h2>${tag(`${pending.length} pending`,pending.length?"warn":"")}</div><p>The workflow calls for an assessment after every exchange. These receipts show the coverage actually recorded.</p><div class="meta">${tag(`${complete.size} completed assessments`)}${tag(`${receipts.length} retained receipts`)}</div>${receipts.length?active.map(r=>`<details class="comparison-stage" data-persist="${esc(r.receipt_id)}"><summary><div><strong>${esc(r.disposition||"Assessment")}${r.stage==="pre-final"?" · final still pending":""}</strong><p>${esc(r.coverage)} · ${esc(r.stage)}</p></div><span>${esc(when(r.recorded_at))}</span></summary><p>${esc(r.summary)}</p><dl class="evidence-list"><dt>Exchange identity</dt><dd>${esc(r.thread_id)} / ${esc(r.turn_id)}</dd><dt>RAM reference</dt><dd>${r.ram?esc(r.ram.path)+" · revision "+esc(r.ram.revision):"No RAM change"}</dd><dt>Native readback</dt><dd>${esc(r.assertion?.native_proof||"Not asserted")}</dd><dt>Receipt</dt><dd>${esc(r.receipt_id)}</dd></dl></details>`).join(""):empty("No local capture receipts yet","Earlier RAM writes remain in Memory. Missing receipts do not prove missing RAM, or complete capture.")}<p class="quiet">Pre-final checkpoints are partial. The actual final is assessed on the next invocation; this local workflow cannot enforce a post-response callback. Summary-only capture is not a complete transcript.</p></section>`;
}
document.addEventListener("change",event=>{
  if(event.target.id==="comparison-experiment"){comparisonState.experiment=event.target.value;comparisonState.stage="all";render()}
  if(event.target.id==="comparison-role"){comparisonState.role=event.target.value;comparisonState.stage="all";render()}
  if(event.target.id==="comparison-stage"){comparisonState.stage=event.target.value;render()}
});

function labTabs(){return `<div class="tabs" role="group" aria-label="Companion lab view"><button data-lab-tab="tests" aria-pressed="${comparisonState.tab==="tests"}">Tests</button><button data-lab-tab="runs" aria-pressed="${comparisonState.tab==="runs"}">Agent turn details</button><button data-lab-tab="side-by-side" aria-pressed="${comparisonState.tab==="side-by-side"}">Side-by-side</button><button data-lab-tab="pairs" aria-pressed="${comparisonState.tab==="pairs"}">Model pairings & quality</button><button data-lab-tab="history" aria-pressed="${comparisonState.tab==="history"}">History & tracking</button></div>`}
function testReportActions(){return `<div class="test-report-actions" aria-label="Test report actions"><button type="button" data-test-print>Print</button><button type="button" data-test-pdf>Export to PDF</button></div>`}

function benchmarkLibrary(data,userTests={}){
  const suites=data.benchmark_suites||[];
  const kinds={companion:{label:"Companion comparisons",description:"Compare primary agents working alone and with a support agent on the same request."},"delegation-cost":{label:"Delegation cost",description:"Compare primary-only work, compact companion handoffs and opaque companion reports on one pinned task."},ram:{label:"RAM comparisons",description:"Compare memory retrieval with RAM on and off, using continuing and fresh agents."}};
  for(const s of suites)if(!kinds[s.test_type])kinds[s.test_type]={label:s.test_type||"Other tests",description:"Retained experimental runs and their measured results."};
  const selected=comparisonState.testType;
  const date=value=>value?new Date(value).toLocaleString():"Not recorded";
  if(!selected)return heading("Tests","Browse your project tests and recorded benchmark runs.")+testReportActions()+labTabs()+renderProjectUserTests(userTests)+`<section class="comparison-section"><h2>Benchmark library</h2><div class="grid two">${Object.entries(kinds).map(([id,k])=>{const runs=suites.filter(s=>s.test_type===id);return `<section class="card"><h2>${esc(k.label)}</h2><p>${esc(k.description)}</p><p class="quiet">${runs.length} run${runs.length===1?"":"s"} · ${runs.filter(s=>s.completed_at).length} completed</p><button data-test-type="${esc(id)}">View runs</button></section>`}).join("")}</div><p class="quiet">New benchmark types and runs appear from recorded benchmark data.</p></section>`;
  const runs=suites.filter(s=>s.test_type===selected).sort((a,b)=>(b.started_at||"").localeCompare(a.started_at||""));
  return heading(kinds[selected]?.label||selected,kinds[selected]?.description||"Recorded test runs.")+testReportActions()+labTabs()+`<p class="test-screen-only"><button data-test-type="">← All test types</button></p><section class="card"><h2>Run history</h2><p class="quiet">Dates and times use your browser's local time zone. Completion is recorded separately from the start of a test.</p>${runs.length?`<div class="table-scroll"><table class="comparison-table"><thead><tr><th>Run and description</th><th>Started</th><th>Completed</th><th>Status</th></tr></thead><tbody>${runs.map(s=>`<tr><td><a href="?benchmark=${encodeURIComponent(s.id)}#companion" data-benchmark-open="${esc(s.id)}">${esc(s.label||s.id)}</a><p>${esc(s.description||"Description not recorded for this historical run.")}</p><span class="quiet">${esc(s.id)} · ${s.results?.length||0} configurations</span></td><td>${esc(date(s.started_at))}</td><td>${esc(s.completed_at?date(s.completed_at):"Pending")}</td><td>${esc(s.status||"Not recorded")}</td></tr>`).join("")}</tbody></table></div>`:empty("No runs yet","Recorded runs of this test type will appear here.")}</section>`;
}

document.addEventListener("click",event=>{
  if(event.target.closest('a[data-view="companion"]')){
    event.preventDefault();setLabTab("tests");
  }
  const kind=event.target.closest("[data-test-type]");
  const run=event.target.closest("[data-benchmark-open]");
  if(event.target.closest("[data-test-print]")){window.print();return}
  if(event.target.closest("[data-test-pdf]")){toast('Choose “Save as PDF” as the print destination.');setTimeout(()=>window.print(),80);return}
  if(kind){comparisonState.testType=kind.dataset.testType||null;comparisonState.tab="tests";const url=new URL(location.href);url.searchParams.delete("benchmark");if(comparisonState.testType)url.searchParams.set("testType",comparisonState.testType);else url.searchParams.delete("testType");history.pushState({},"",url);render()}
  if(run){event.preventDefault();sideBySideState.task=run.dataset.benchmarkOpen;comparisonState.tab="side-by-side";history.pushState({},"",run.href);render()}
});
let testPrintClosedDetails=[];
window.addEventListener("beforeprint",()=>{testPrintClosedDetails=[...document.querySelectorAll("main details:not([open])")];testPrintClosedDetails.forEach(detail=>detail.open=true)});
window.addEventListener("afterprint",()=>{testPrintClosedDetails.forEach(detail=>detail.open=false);testPrintClosedDetails=[]});
window.addEventListener("popstate",()=>{const params=new URLSearchParams(location.search);sideBySideState.task=params.get("benchmark");comparisonState.testType=params.get("testType");comparisonState.tab=sideBySideState.task?"side-by-side":"tests";render()});
function shortModel(model){return ({"gpt-6-luna":"Luna","gpt-6-sol":"Sol","gpt-6-astra":"Astra"})[model]||model||"Unknown"}
function pairLabel(e){return `${shortModel(e.primary_model)} + ${e.mode==="off"?"companion off":shortModel(e.support_model)}`}
function outputReview(e){
  const a=e.assessment,score=a?.score;
  return `<section class="card output-review"><div class="output-review-head"><div><div class="eyebrow">Run output</div><h2>${esc(e.primary_model?pairLabel(e):e.label||"Selected experiment")}</h2></div><div class="quality-score"><strong>${score==null?"Ungraded":score+"%"}</strong><span>${score==null?(a?.status==="stale"?"Review is stale":"No completed assessment"):"Rubric score · not a probability"}</span></div></div><p class="memory-summary">${esc(a?.output_summary||e.output_summary||"No output summary recorded yet.")}</p>${a?.reviewer?`<div class="meta">${tag(a.review_kind+" review",a.review_kind==="contributor"?"warn":"")}${tag(a.retrospective?"Retrospective":"Prospective rubric")}${tag(a.status)}<span>Reviewer: ${esc(a.reviewer)}</span></div>`:""}${score!=null?`<details class="review-details" data-persist="quality-${esc(e.id)}"><summary>How this score was assessed</summary><p class="quiet">${esc(e.rubric.meaning)} ${esc(e.rubric.anchors)}</p>${horizontalBars(e.rubric.dimensions.map(d=>({label:`${d.label} · ${d.weight}% weight`,value:a.dimensions[d.id].score})),"Quality rubric dimensions",x=>x+" / 100",100)}${e.rubric.dimensions.map(d=>`<p><strong>${esc(d.label)}</strong> — ${esc(a.dimensions[d.id].rationale)}<br><span class="quiet">Evidence: ${a.dimensions[d.id].evidence.map(esc).join(" · ")}</span></p>`).join("")}<p class="quiet">Reviewed ${esc(when(a.evaluated_at))} · ${esc(a.rubric_id)} · ${e.review_history.length} retained assessment(s)</p></details>`:""}${a?.review_id?`<details class="review-details" data-persist="review-source-${esc(e.id)}"><summary>Review source evidence</summary><dl class="evidence-list"><dt>Review identity</dt><dd>${esc(a.review_id)}</dd><dt>Experiment fingerprint</dt><dd>${esc(a.experiment_fingerprint)}</dd><dt>Reviewed outputs</dt><dd>${(a.output_refs||[]).map(ref=>`${esc(ref.run_id)} · ${esc(ref.final_sha256)}`).join("<br>")||"None recorded"}</dd></dl></details>`:""}${a?.limitations?.length?`<p class="review-limits">${a.limitations.map(esc).join(" · ")}</p>`:""}${a?.gaps?.length?`<div class="callout">${a.gaps.map(esc).join(" · ")}</div>`:""}</section>`;
}
function pairingComparison(data){
  const experiments=data.experiments||[],models=data.pricing.rates;
  const tasks=[...new Set(experiments.map(e=>e.task_id).filter(Boolean))];
  const filtered=experiments.filter(e=>(comparisonState.task==="all"||e.task_id===comparisonState.task)&&(comparisonState.primary==="all"||e.primary_model===comparisonState.primary)&&(comparisonState.support==="all"||(comparisonState.support==="off"?e.mode==="off":e.support_model===comparisonState.support)));
  const baseline=experiments.find(e=>e.id===comparisonState.baseline);
  const selectOptions=(items,value)=>items.map(x=>`<option value="${esc(x.value)}" ${value===x.value?"selected":""}>${esc(x.label)}</option>`).join("");
  const match=(e,b)=>Boolean(e?.comparison_key&&b?.comparison_key&&JSON.stringify(e.comparison_key)===JSON.stringify(b.comparison_key));
  return heading("Model pairings & quality","Compare real outputs on the same task. Every pairing requires its own measured run.")+labTabs()+
  `<div class="card comparison-controls"><label>Task<select id="pair-task">${selectOptions([{value:"all",label:"All tasks"},...tasks.map(t=>({value:t,label:t}))],comparisonState.task)}</select></label><label>Primary model<select id="pair-primary">${selectOptions([{value:"all",label:"All primary models"},...models.map(m=>({value:m.model,label:m.label}))],comparisonState.primary)}</select></label><label>Support model<select id="pair-support">${selectOptions([{value:"all",label:"All helpers / off"},{value:"off",label:"Companion off"},...models.map(m=>({value:m.model,label:m.label}))],comparisonState.support)}</select></label><label>Compare quality against<select id="pair-baseline">${selectOptions([{value:"",label:"No baseline selected"},...filtered.map(e=>({value:e.id,label:e.label}))],comparisonState.baseline)}</select></label></div>`+
  `<section class="card"><div class="section-head"><h2>Which pairings have we tried?</h2>${tag(`${filtered.length} registered experiments`)}</div><p class="quiet">Rows are the primary model; columns are its companion. Empty cells mean no observed run, not a zero quality score.</p><div class="table-scroll"><table class="pair-matrix comparison-table"><thead><tr><th>Primary ↓ / Support →</th><th>Off</th>${models.map(m=>`<th>${esc(m.label)}</th>`).join("")}</tr></thead><tbody>${models.map(primary=>`<tr><th>${esc(primary.label)}</th>${[null,...models.map(m=>m.model)].map(helper=>{const cell=filtered.filter(e=>e.primary_model===primary.model&&(helper?e.support_model===helper&&e.mode==="on":e.mode==="off"));return `<td>${cell.length?cell.map(e=>`<button class="pair-cell" data-experiment="${esc(e.id)}"><strong>${e.pairing_status==="unobserved"?"Planned":e.assessment?.score==null?"Ungraded":e.assessment.score+"%"}</strong><span>${esc(e.label)}</span><small>${esc(e.pairing_status)}</small></button>`).join(""):'<span class="quiet">Not run</span>'}</td>`}).join("")}</tr>`).join("")}</tbody></table></div></section>`+
  `<section class="card comparison-section"><h2>Output quality · 0–100%</h2>${filtered.some(e=>e.assessment?.score!=null)?horizontalBars(filtered.map(e=>({label:pairLabel(e)+" · "+e.label,value:e.assessment?.score})),"Reviewed output quality by model pairing",x=>x+"%",100):empty("No graded outputs in this selection","Record an assessment using the shared rubric. Token counts never assign a quality score.")}<p class="quiet">Correctness 40% · Task fulfillment 30% · Evidence and honesty 20% · Clarity 10%. Inspect reviewer and limitations before interpreting a score as evidence of equal quality.</p>${filtered.map(e=>`<div class="quality-comparison-row"><strong>${esc(e.label)}</strong><span>${!baseline?"No baseline selected":e.id===baseline.id?"Selected baseline":match(e,baseline)?`${(e.assessment.score-baseline.assessment.score).toFixed(1)} percentage points vs baseline`:"Not a matched comparison"}</span><p class="quiet">${esc(e.comparison_limits.join(" · ")||"Task, source, conditions and rubric match the recorded comparison contract; reviewer independence is declared.")}</p></div>`).join("")}</section>`+
  `<div class="comparison-section">${filtered.map(outputReview).join("")||empty("No experiments match","Choose another pairing or register an actual experiment. Filters do not start model runs.")}</div>`;
}
document.addEventListener("click",event=>{
  const tab=event.target.closest("button[data-lab-tab]");
  if(tab)setLabTab(tab.dataset.labTab);
  const experiment=event.target.closest("button[data-experiment]");
  if(experiment){comparisonState.experiment=experiment.dataset.experiment;comparisonState.stage="all";setLabTab("runs")}
});
function setLabTab(tab){
  comparisonState.tab=tab;
  state.view="companion";
  const url=new URL(location.href);
  url.hash="companion";
  if(tab==="tests"){
    comparisonState.testType=null;sideBySideState.task=null;
    url.searchParams.delete("benchmark");url.searchParams.delete("testType");
  }else if(tab==="side-by-side"){
    comparisonState.testType=null;url.searchParams.delete("testType");
    if(sideBySideState.task)url.searchParams.set("benchmark",sideBySideState.task);else url.searchParams.delete("benchmark");
  }else{
    comparisonState.testType=null;url.searchParams.delete("benchmark");url.searchParams.delete("testType");
  }
  history.pushState({},"",url);render();
  if(tab==="side-by-side"&&sideBySideState.task&&!url.searchParams.has("benchmark")){
    url.searchParams.set("benchmark",sideBySideState.task);history.replaceState({},"",url);
  }
}
document.addEventListener("change",event=>{
  const key={"pair-task":"task","pair-primary":"primary","pair-support":"support","pair-baseline":"baseline"}[event.target.id];
  if(key){comparisonState[key]=event.target.value;if(key!=="baseline")comparisonState.baseline="";render()}
});

function companionHistory(data){
  const cutoff=comparisonState.period==="all"?0:Date.now()-Number(comparisonState.period)*86400000;
  const rows=data.runs.filter(r=>new Date(r.start).getTime()>=cutoff);
  const complete=rows.filter(r=>r.state==="completed");
  const pairs=(data.comparisons||[]).filter(c=>new Date(c.end).getTime()>=cutoff);
  const eligible=pairs.filter(c=>c.savings_usd!=null);
  const savings=eligible.length?eligible.reduce((n,c)=>n+c.savings_usd,0):null;
  const measured=complete.filter(r=>r.usage?.input_tokens!=null);
  const models=data.pricing.rates.map(m=>({label:m.label,value:sumKnown(measured.filter(r=>r.models?.length===1&&r.models[0]===m.model),"input_tokens")}));
  const tracking=data.tracking||{};
  return heading("History & tracking","Retained observations from explicitly connected local sessions. New turns appear on refresh.")+labTabs()+
  `<section class="card comparison-controls"><label>Period<select id="history-period"><option value="all" ${comparisonState.period==="all"?"selected":""}>All time</option><option value="30" ${comparisonState.period==="30"?"selected":""}>Last 30 days</option><option value="7" ${comparisonState.period==="7"?"selected":""}>Last 7 days</option></select></label><div><strong>Usage tracking: ${tracking.enabled?"On":"Off"}</strong><p class="quiet">Pause collection and retain existing history. Companion delegation is controlled separately.</p><button id="tracking-toggle" data-enabled="${!tracking.enabled}">${tracking.enabled?"Pause tracking":"Resume tracking"}</button></div></section>`+
  `<div class="grid stats">${stat("Primary requests",num(rows.filter(r=>r.role==="primary").length),"Within connected sessions only")}${stat("Known input subtotal",knownNumber(sumKnown(measured,"input_tokens")),`${measured.length} measured turns; ${complete.length-measured.length} unknown. Counters may overlap outside paired tests.`)}${stat("Matched sample savings",dollars(savings),`${eligible.length} qualifying paired sample(s); API equivalent`)}${stat("Savings not established",num(rows.filter(r=>r.role==="primary"&&!eligible.some(c=>c.experiment_id===r.experiment_id||c.baseline_id===r.experiment_id)).length),"Unmatched or below baseline quality")}</div>`+
  `<div class="grid two"><section class="card"><h2>Known input by model</h2><p class="quiet">Partial observations; missing counters are excluded.</p>${horizontalBars(models,"Actual model input tokens")}</section><section class="card"><h2>Actual paired runs</h2>${pairs.length?pairs.map(c=>`<div class="quality-comparison-row"><strong>${esc(c.experiment_id)}</strong>${horizontalBars([{label:"Companion off",value:c.baseline_cost_usd},{label:"Companion on · helper + primary",value:c.companion_cost_usd}],"Measured paired API equivalents",dollars)}<p>Savings: ${dollars(c.savings_usd)} · Quality change: ${c.quality_delta==null?"Unknown":c.quality_delta.toFixed(1)+" points"}</p><details data-persist="limits-${esc(c.experiment_id)}"><summary>Comparison limits and cache conditions</summary><p class="quiet">${esc([...c.limits,...(c.notes||[])].join(" · ")||"Matched task/source/conditions; equal-or-higher rubric score. One sample is not general equivalence.")}</p></details></div>`).join(""):empty("No paired samples yet","Unmatched requests can show usage but have no measured savings.")}</section></div>`+
  `<section class="card comparison-section"><h2>Request history</h2><p class="quiet">Metadata and concise assessed findings only. No automatic deletion or history cap. Pausing skips requests started while off; resuming does not backfill them. Collection happens while a dashboard or status reader runs; no background service. An asterisk preserves a native counter that needs investigation and excludes it from totals and prices.</p><div class="table-scroll"><table class="comparison-table"><thead><tr><th>Started</th><th>Role / model</th><th>Work / output summary</th><th>Input</th><th>API equivalent</th></tr></thead><tbody>${rows.slice().reverse().map(r=>`<tr><td>${esc(when(r.start))}<br>${esc(r.state)}</td><td>${esc(r.role)}<br>${esc(r.models.map(shortModel).join(", "))}</td><td><button data-experiment="${esc(r.experiment_id)}">${esc(r.stage)}</button><p>${esc(r.finding)}</p></td><td>${usageNumber(r,"input_tokens")}</td><td>${dollars(r.api_estimate_usd)}</td></tr>`).join("")}</tbody></table></div></section>`+
  `<p class="quiet">Savings totals cover qualified matched samples, not every request or account billing. Separate worker logs are required to combine helper and primary cost without unknown overlap. Rates checked ${esc(data.pricing.checked_at)}. Unchanged native logs reuse cached projections; summaries and reviews are data, not website rebuilds.</p>`;
}
document.addEventListener("change",event=>{if(event.target.id==="history-period"){comparisonState.period=event.target.value;render()}});
document.addEventListener("click",async event=>{
 const button=event.target.closest("#tracking-toggle");if(!button)return;
 button.disabled=true;
 try{const response=await fetch("/api/v1/companion/tracking",{method:"POST",headers:{"Content-Type":"application/json","X-Eutonos-Settings":state.snapshot.settings_token},body:JSON.stringify({project:state.project,enabled:button.dataset.enabled==="true"})});if(!response.ok)throw new Error("Tracking setting was not saved");await load();toast("Tracking setting saved.")}catch(error){toast(error.message);button.disabled=false}
});

// Presentation preferences only; experiment evidence remains in the native snapshot.
const sideBySideState={task:new URLSearchParams(location.search).get("benchmark"),preferences:new Map(),dragKey:null};
const benchmarkNumber=value=>typeof value==="number"&&Number.isFinite(value)&&value>=0?value:null;
const benchmarkSeconds=value=>benchmarkNumber(value)==null?"Unknown":`${num(Math.round(value*10)/10)} s`;
const benchmarkCount=value=>benchmarkNumber(value)==null?"Unknown":num(value);
const benchmarkMoney=value=>dollars(benchmarkNumber(value));
const benchmarkCredits=value=>benchmarkNumber(value)==null?"Unknown":num(value);
const benchmarkLabel=r=>r.context_condition&&r.label?r.label:r.primary_model&&r.support_model?`${shortModel(r.primary_model)} + ${shortModel(r.support_model)}`:r.label||r.id;
const benchmarkModelKey=model=>["astra","sol","terra","luna","claude","gemini"].find(name=>String(model||"").toLowerCase().includes(name))||"unknown";
const benchmarkModelName=(model,fallback="Unknown")=>model?`<span class="benchmark-model-name model-label model-${benchmarkModelKey(model)}">${modelMark(model)}${esc(shortModel(model))}</span>`:esc(fallback);
function benchmarkLabelMarkup(r){
  if(r.context_condition&&r.label)return esc(r.label);
  if(r.primary_model&&r.support_model)return `${benchmarkModelName(r.primary_model)}<span class="benchmark-pair-join"> + </span>${benchmarkModelName(r.support_model)}`;
  if(r.primary_model)return `${benchmarkModelName(r.primary_model)}${/\balone\b/i.test(r.label||"")?'<span class="benchmark-pair-join"> alone</span>':""}`;
  return esc(r.label||r.id);
}
function benchmarkCostBreakdown(result,phase){
  const stage=result?.[phase],total=benchmarkNumber(stage?.cost_usd);
  if(total==null)return null;
  const entries=stage?.cost_by_model&&typeof stage.cost_by_model==="object"?Object.entries(stage.cost_by_model):[];
  const known=entries.map(([model,value])=>({model:model&&model!=="unknown"?model:null,value:benchmarkNumber(value)})).filter(part=>part.value!=null&&part.value>0);
  const knownTotal=known.reduce((sum,part)=>sum+part.value,0),epsilon=Math.max(1e-9,total*1e-9);
  if(knownTotal>total+epsilon)return {total,parts:[{model:null,value:total}]};
  const remainder=Math.max(0,total-knownTotal),parts=[...known];
  if(remainder>epsilon||(!parts.length&&total>0))parts.push({model:null,value:remainder||total});
  return {total,parts};
}
function benchmarkCostRects(result,phase,height,baseY=56){
  const breakdown=benchmarkCostBreakdown(result,phase);
  if(!breakdown||breakdown.total<=0||height<=0)return null;
  let cursor=baseY;
  const rects=breakdown.parts.map((part,index)=>{const partHeight=height*part.value/breakdown.total;cursor-=partHeight;const label=part.model?shortModel(part.model):"Unattributed";return `<rect class="benchmark-cost-segment benchmark-cost-${benchmarkModelKey(part.model)}" x="25" y="${cursor}" width="50" height="${partHeight}" ${index===breakdown.parts.length-1?'rx="3"':""}><title>${esc(label)}: ${esc(benchmarkMoney(part.value))}</title></rect>`}).join("");
  const label=breakdown.parts.map(part=>`${part.model?shortModel(part.model):"Unattributed"} ${benchmarkMoney(part.value)}`).join(", ");
  return {rects,label:`${phase==="closeout"?"Closeout":"Answer"} cost breakdown: ${label}`};
}
const benchmarkMetrics={
  quality:{label:"Answer quality",value:r=>r.quality?.score,format:v=>`${num(v)} / 100`,max:100,note:"Recorded rubric score; higher is better. Quality is not inferred from usage."},
  cost:{label:"Answer cost",value:r=>r.answer?.cost_usd,format:benchmarkMoney,note:"Observed API equivalent for the answer. Lower cost alone does not establish savings."},
  elapsed:{label:"Time to answer",value:r=>r.answer?.elapsed_seconds,format:benchmarkSeconds,note:"Wall time to the answer, including helper work when recorded."},
  tools:{label:"Answer tool calls",value:r=>r.answer?.tool_calls,format:benchmarkCount,note:"Recorded native tool calls. A wrapper may contain several commands."},
  review_cost:{label:"Review cost",value:r=>r.review?.cost_usd,format:benchmarkMoney,note:"Semantic-review API equivalent is measured separately from answering."},
  review_elapsed:{label:"Review time",value:r=>r.review?.elapsed_seconds,format:benchmarkSeconds,note:"Wall time for the recorded semantic review, separate from time to answer."},
  closeout_cost:{label:"Closeout cost",value:r=>r.closeout?.cost_usd,format:benchmarkMoney,note:"Closeout API equivalent is measured separately from answering."},
  closeout_elapsed:{label:"Closeout time",value:r=>r.closeout?.elapsed_seconds,format:benchmarkSeconds,note:"Wall time for the recorded closeout, separate from time to answer."}
};
function benchmarkPreferences(task,keys){
  let preferences=sideBySideState.preferences.get(task);
  if(!preferences){
    try{preferences=JSON.parse(localStorage.getItem(`eutonos.companion.side-by-side.v1:${task}`)||"null")}catch{}
    if(!preferences||typeof preferences!=="object")preferences={};
    preferences={order:Array.isArray(preferences.order)?preferences.order.filter(x=>typeof x==="string"):[],hidden:Array.isArray(preferences.hidden)?preferences.hidden.filter(x=>typeof x==="string"):[],metric:benchmarkMetrics[preferences.metric]?preferences.metric:"quality",rowBars:Array.isArray(preferences.rowBars)?preferences.rowBars.filter(x=>typeof x==="string"):[],showTopGraph:preferences.showTopGraph!==false};
    sideBySideState.preferences.set(task,preferences);
  }
  preferences.order=[...new Set([...preferences.order.filter(x=>keys.includes(x)),...keys])];
  return preferences;
}
function saveBenchmarkPreferences(){
  try{localStorage.setItem(`eutonos.companion.side-by-side.v1:${sideBySideState.task}`,JSON.stringify(sideBySideState.preferences.get(sideBySideState.task)))}catch{toast("Browser storage is unavailable; choices are kept for this session.")}
}
function benchmarkTools(value){
  if(value==null||typeof value!=="object")return '<span class="quiet">Unknown</span>';
  const entries=Object.entries(value);
  return entries.length?`<ul class="benchmark-tools">${entries.map(([name,count])=>`<li><span>${esc(name)}</span><strong>${esc(benchmarkCount(count))}</strong></li>`).join("")}</ul>`:'<span class="quiet">No recorded calls</span>';
}
function benchmarkText(value,fallback="Unknown"){return esc(typeof value==="string"&&value.trim()?value:fallback)}
function benchmarkSummary(value,id,fallback){
  if(typeof value!=="string"||value.length<=130)return benchmarkText(value,fallback);
  return `<details data-persist="${esc(id)}"><summary>${esc(value.slice(0,110))}…</summary>${esc(value)}</details>`;
}
function benchmarkSigned(value,format=benchmarkCount){
  const number=benchmarkNumber(Math.abs(value));
  return number==null?"Unknown":`${value>0?"+":value<0?"−":""}${format(number)}`;
}
function benchmarkAnalysisDetail(r){
  const a=r.analysis;
  if(!a)return '<span class="quiet">Not projected</span>';
  return `<strong class="benchmark-analysis-${esc(a.status)}">${esc(a.label)}</strong><p>${esc(a.summary)}</p>`;
}
function benchmarkAnalysisDelta(r,key,kind){
  const a=r.analysis,value=a?.[key];
  if(value==null)return '<span class="quiet">Unknown</span>';
  if(a.status==="baseline")return "Baseline";
  if(kind==="money")return `${benchmarkSigned(value,benchmarkMoney)}${a.cost_delta_percent==null?"":` (${benchmarkSigned(a.cost_delta_percent,v=>`${num(Math.round(v*10)/10)}%`)})`}`;
  if(kind==="points")return `${benchmarkSigned(value,v=>`${num(Math.round(v*10)/10)} points`)}`;
  return benchmarkSigned(value);
}
function benchmarkReviewDetail(r){
  const q=r.quality;
  if(!q)return '<span class="quiet">No matching review</span>';
  const reviewer=q.reviewer||{},dimensions=q.dimensions&&typeof q.dimensions==="object"?Object.entries(q.dimensions):[];
  return `<details data-persist="benchmark-review-${esc(r.key)}"><summary>${esc(q.kind||"Review")} · ${esc(reviewer.role||"Reviewer not recorded")}</summary><p class="quiet">Requested reviewer: ${esc(reviewer.requested_model||"Unknown")} · ${esc(reviewer.requested_reasoning||"thinking unknown")}<br>${esc(reviewer.identity_basis||"Runtime identity basis not recorded")}<br>Rubric: ${esc(q.rubric||"Not recorded")}</p>${dimensions.map(([name,value])=>`<p><strong>${esc(name.replaceAll("_"," "))}: ${esc(benchmarkCount(value?.score))} / 100</strong><br>${esc(value?.rationale||"No rationale recorded")}</p>`).join("")}<p class="quiet">Answer hash: ${esc(q.answer_sha256||"Not recorded")}<br>Blinded answer hash: ${esc(q.blinded_answer_sha256||"Not recorded")}</p></details>`;
}
function benchmarkReportLink(r){
  if(typeof r.report_path!=="string"||typeof r.report_sha256!=="string"||!r.report_path.startsWith(".eutonos-local/reports/")||r.report_path.includes("\\\\")||r.report_path.split("/").some(part=>part==="."||part==="..")||!/^[0-9a-f]{64}$/i.test(r.report_sha256))return "Not retained";
  const query=new URLSearchParams({project:String(state.project||0),report_path:r.report_path,report_sha256:r.report_sha256});
  return `<a href="/api/v1/benchmark-report?${esc(query.toString())}" target="_blank" rel="noopener">Read opaque report</a>`;
}
const benchmarkUsageTotal=usage=>{
  const input=benchmarkNumber(usage?.input_tokens),output=benchmarkNumber(usage?.output_tokens);
  return input==null||output==null?null:input+output;
};
function benchmarkTokenSplitCard(model,role,usage){
  const visibleOutput=value=>{const output=benchmarkNumber(value?.output_tokens),reasoning=benchmarkNumber(value?.reasoning_output_tokens);return output==null||reasoning==null||reasoning>output?null:output-reasoning};
  const value=(label,tokens)=>`<div><dt>${esc(label)}</dt><dd>${esc(benchmarkCount(tokens))}</dd></div>`;
  return `<section class="benchmark-token-split-card"><div class="benchmark-token-split-head">${benchmarkModelName(model)}<span>${esc(role)}</span></div><dl>${value("Input",usage?.input_tokens)}${value("Cached input",usage?.cached_input_tokens)}${value("Visible output",visibleOutput(usage))}${value("Reasoning",usage?.reasoning_output_tokens)}${value("Full output",usage?.output_tokens)}${value("Total tokens",benchmarkUsageTotal(usage))}</dl></section>`;
}
function benchmarkTokenSplit(result){
  const support=result.support_model?benchmarkTokenSplitCard(result.support_model,"Support",result.support):"";
  return `<div class="benchmark-token-split">${benchmarkTokenSplitCard(result.primary_model,"Primary",result.primary)}${support}<section class="benchmark-token-split-card benchmark-token-split-total"><div class="benchmark-token-split-head"><strong>Combined answer usage</strong><span>All measured agents</span></div><dl><div><dt>Total tokens</dt><dd>${esc(benchmarkCount(benchmarkUsageTotal(result.answer)))}</dd></div><div><dt>Answer cost</dt><dd>${esc(benchmarkMoney(result.answer?.cost_usd))}</dd></div></dl></section></div>`;
}
function sideBySideComparison(data){
  const suites=Array.isArray(data.benchmark_suites)?data.benchmark_suites:[];
  const tasks=[...new Set(suites.map(s=>s.id))];
  if(!tasks.includes(sideBySideState.task))sideBySideState.task=tasks[0]||null;
  const selected=suites.filter(s=>(s.id)===sideBySideState.task);
  const results=selected.flatMap(s=>(s.results||[]).map(r=>({...r,key:JSON.stringify([s.id,r.id])})));
  const isMemoryStudy=results.some(r=>r.context_condition);
  const hasReview=results.some(r=>Array.isArray(r.review?.run_ids)&&r.review.run_ids.length);
  const hasCloseout=results.some(r=>r.closeout_applicable!==false);
  const prefs=benchmarkPreferences(sideBySideState.task,results.map(r=>r.key));
  if(!hasReview&&prefs.metric.startsWith("review_"))prefs.metric="quality";
  if(!hasCloseout&&prefs.metric.startsWith("closeout_"))prefs.metric="quality";
  const ordered=prefs.order.map(key=>results.find(r=>r.key===key)).filter(Boolean);
  const visible=ordered.filter(r=>!prefs.hidden.includes(r.key));
  const metric=benchmarkMetrics[prefs.metric];
  const max=metric.max||Math.max(1e-9,...visible.map(r=>benchmarkNumber(metric.value(r))??0));
  const row=(label,value,className="")=>`<tr class="${className}"><th scope="row">${esc(label)}</th>${visible.map(r=>`<td>${value(r)}</td>`).join("")}</tr>`;
  const numericRow=(key,label,value,format=benchmarkCount,options={})=>{
    const graphed=prefs.rowBars.includes(key),values=visible.map(r=>benchmarkNumber(value(r)));
    const scale=options.max||Math.max(1e-9,...values.map(v=>v??0));
    const accessibleLabel=options.scope?`${options.scope} ${label.toLowerCase()}`:label;
    return `<tr data-benchmark-row="${esc(key)}" class="${graphed?"benchmark-row-graphed":""}"><th scope="row"><span>${esc(label)}</span><label class="benchmark-row-toggle"><input type="checkbox" data-benchmark-row-bars="${esc(key)}" aria-label="Show bars for ${esc(accessibleLabel)}" ${graphed?"checked":""}>Bars</label></th>${visible.map((r,i)=>{
      const v=values[i],emptyText=options.empty?options.empty(r):"Unknown";
      const display=v==null?emptyText:format(v);
      const height=v==null?0:Math.min(54,v/scale*54);
      const cost=options.costPhase?benchmarkCostRects(r,options.costPhase,height):null;
      const bar=graphed&&v!=null?`<svg class="benchmark-row-bar" viewBox="0 0 100 58" preserveAspectRatio="none" ${cost?`role="img" aria-label="${esc(cost.label)}"`:'aria-hidden="true"'}><line class="benchmark-row-baseline" x1="0" x2="100" y1="56" y2="56"/>${cost?cost.rects:`<rect class="benchmark-row-fill" x="25" y="${56-height}" width="50" height="${height}" rx="3"/>`}</svg>`:"";
      return `<td><span class="benchmark-row-value">${esc(display)}</span>${bar}</td>`;
    }).join("")}</tr>`;
  };
  const section=label=>row(label,()=>"","benchmark-section-row");
  const visibleOutput=value=>{const output=benchmarkNumber(value?.output_tokens),reasoning=benchmarkNumber(value?.reasoning_output_tokens);return output==null||reasoning==null||reasoning>output?null:output-reasoning};
  const usageRows=(phase)=>[
    ["input_tokens","Input tokens",r=>r[phase]?.input_tokens],
    ["cached_input_tokens","Cached input",r=>r[phase]?.cached_input_tokens],
    ["uncached_input_tokens","Uncached input",r=>{const a=benchmarkNumber(r[phase]?.input_tokens),b=benchmarkNumber(r[phase]?.cached_input_tokens);return a==null||b==null||b>a?null:a-b}],
    ["visible_output_tokens","Visible output",r=>visibleOutput(r[phase])],
    ["reasoning_output_tokens","Reasoning",r=>r[phase]?.reasoning_output_tokens],
    ["output_tokens","Full output",r=>r[phase]?.output_tokens],
    ["tool_calls","Tool calls",r=>r[phase]?.tool_calls],
    ["worker_seconds","Worker time",r=>r[phase]?.worker_seconds,benchmarkSeconds]
  ].map(([key,label,value,format])=>numericRow(`${phase}.${key}`,label,value,format,{scope:phase==="answer"?"Answer":"Closeout"})).join("");
  const helperEmpty=r=>r.support_model?"Unknown":"Companion off";
  const limits=selected.flatMap(s=>Array.isArray(s.limits)?s.limits:s.limits?[String(s.limits)]:[]);
  const analysis=selected[0]?.analysis;
  return heading(selected[0]?.label||"Run results",selected[0]?.description||"Compare recorded results within this test run.")+testReportActions()+labTabs()+
    `<p class="test-screen-only"><button data-test-type="${esc(selected[0]?.test_type||"companion")}">← Run history</button></p>`+
    `<section class="card benchmark-controls"><div class="comparison-controls"><label>Run<select id="benchmark-task">${tasks.map(t=>`<option value="${esc(t)}" ${t===sideBySideState.task?"selected":""}>${esc(suites.find(s=>s.id===t)?.label||t)}</option>`).join("")}</select></label><label>Graph metric<select id="benchmark-metric">${Object.entries(benchmarkMetrics).filter(([key])=>(hasReview||!key.startsWith("review_"))&&(hasCloseout||!key.startsWith("closeout_"))).map(([key,m])=>`<option value="${key}" ${prefs.metric===key?"selected":""}>${m.label}</option>`).join("")}</select></label><div class="benchmark-count"><strong>${visible.length} / ${results.length}</strong><span>experiments visible</span></div></div><label class="benchmark-top-toggle"><input type="checkbox" id="benchmark-top-graph" ${prefs.showTopGraph?"checked":""}>Show top graph</label><fieldset class="benchmark-visibility"><legend>Show experiments</legend>${ordered.map(r=>`<label><input type="checkbox" data-benchmark-visible="${esc(r.key)}" ${prefs.hidden.includes(r.key)?"":"checked"}>${benchmarkLabelMarkup(r)}</label>`).join("")}<button id="benchmark-show-all" type="button">Show all</button></fieldset><p class="quiet">Drag column headers or use their left/right buttons to reorder. Visibility, order and graph choices are saved for this task. Use Bars beside any numeric row to compare it independently.</p></section>`+
    (results.length?`<div class="benchmark-context">${selected.map(s=>`<span><strong>${esc(s.label||s.id)}</strong> · ${esc(s.status||"Unknown")}</span>`).join("")}</div>`:"")+
    (analysis?`<section class="card benchmark-analysis-summary"><div class="section-head"><h2>Analytical interpretation</h2>${tag(`${analysis.interpreted_count} matched comparisons`)}</div><div class="meta">${tag(`${analysis.candidate} cost-and-quality candidates`)}${tag(`${analysis.tradeoff} tradeoffs`,analysis.tradeoff?"warn":"")}${tag(`${analysis.baseline_favored} baseline-favored`,analysis.baseline_favored?"warn":"")}${tag(`${analysis.insufficient_count} insufficient`,analysis.insufficient_count?"warn":"")}</div><p class="quiet">${esc(analysis.scope)}</p></section>`:"")+
    (!results.length?empty("No benchmark results connected",data.reason||"Results will appear here when a benchmark suite is present in the native snapshot."):!visible.length?empty("All experiments are hidden","Select an experiment above or choose Show all to compare the results."):
    `<p class="quiet benchmark-metric-note">${prefs.showTopGraph?esc(metric.note)+" ":""}Each row uses its own scale across visible experiments; quality uses 0–100. Unknown values have no bar.</p><div class="benchmark-scroll" tabindex="0" role="region" aria-label="Side-by-side experiment comparison"><table class="benchmark-table"><caption class="benchmark-sr-only">${esc(sideBySideState.task)}. ${visible.length} visible experiments. Top graph: ${prefs.showTopGraph?esc(metric.label):"hidden"}.</caption><thead><tr><th scope="col">Experiment</th>${visible.map((r,i)=>`<th scope="col" draggable="true" data-benchmark-column="${esc(r.key)}"><div class="benchmark-heading"><span class="benchmark-drag" aria-hidden="true">⠿</span><strong>${benchmarkLabelMarkup(r)}</strong></div><div class="benchmark-model">Primary: ${benchmarkModelName(r.primary_model)} · ${esc(r.primary_effort||"thinking unknown")}<br>Helper: ${r.support_model?benchmarkModelName(r.support_model)+" · "+esc(r.support_effort||"thinking unknown"):"off"}</div><div class="benchmark-column-actions"><button type="button" data-benchmark-move="${esc(r.key)}" data-direction="-1" aria-label="Move ${esc(benchmarkLabel(r))} left" ${i===0?"disabled":""}>←</button><button type="button" data-benchmark-move="${esc(r.key)}" data-direction="1" aria-label="Move ${esc(benchmarkLabel(r))} right" ${i===visible.length-1?"disabled":""}>→</button><span>${esc(r.status||"Unknown")}</span></div></th>`).join("")}</tr></thead><tbody>`+
    (prefs.showTopGraph?row(metric.label,r=>{const value=benchmarkNumber(metric.value(r));const height=value==null?0:Math.max(0,Math.min(100,value/max*100)),phase=prefs.metric==="cost"?"answer":prefs.metric==="closeout_cost"?"closeout":null,cost=phase?benchmarkCostRects(r,phase,height,102):null;return `<div class="benchmark-graph"><strong>${value==null?"Unknown":esc(metric.format(value))}</strong><svg viewBox="0 0 100 104" preserveAspectRatio="none" ${cost?`role="img" aria-label="${esc(cost.label)}"`:'aria-hidden="true"'}><line x1="0" x2="100" y1="102" y2="102"/>${cost?cost.rects:`<rect x="25" y="${102-height}" width="50" height="${height}" rx="3"/>`}</svg></div>`},"benchmark-graph-row"):"")+
    numericRow("answer.quality","Answer quality",r=>r.quality?.score,v=>`${benchmarkCount(v)} / 100`,{max:100,empty:()=>"Ungraded"})+
    numericRow("answer.elapsed_seconds","Time to answer",r=>r.answer?.elapsed_seconds,benchmarkSeconds)+
    numericRow("answer.handoff_seconds","Dispatch gaps",r=>r.answer?.handoff_seconds,benchmarkSeconds,{scope:"Answer"})+
    numericRow("answer.cost_usd","Answer cost",r=>r.answer?.cost_usd,benchmarkMoney,{costPhase:"answer"})+
    section("Analytical interpretation")+
    row("Observed pattern",r=>benchmarkAnalysisDetail(r),"benchmark-prose-row")+
    row("Cost vs matched baseline",r=>benchmarkAnalysisDelta(r,"cost_delta_usd","money"))+
    row("Quality vs matched baseline",r=>benchmarkAnalysisDelta(r,"quality_delta","points"))+
    row("Input / reasoning / output / total token change",r=>{const a=r.analysis;return a?.status==="baseline"?"Baseline":`${benchmarkAnalysisDelta(r,"input_delta")} / ${benchmarkAnalysisDelta(r,"reasoning_delta")} / ${benchmarkAnalysisDelta(r,"output_delta")} / ${benchmarkAnalysisDelta(r,"token_delta")}`})+
    row("Routing signal",r=>benchmarkText(r.analysis?.routing_signal,"No routing interpretation available"),"benchmark-prose-row")+
    section("Standard-rate credit estimates")+
    numericRow("answer.credit_estimate","Answer credit estimate",r=>r.answer?.credit_estimate,benchmarkCredits)+
    (hasReview?numericRow("review.credit_estimate","Review credit estimate",r=>r.review?.credit_estimate,benchmarkCredits):"")+
    numericRow("total.credit_estimate","Total credit estimate",r=>r.total?.credit_estimate,benchmarkCredits)+
    (hasCloseout?numericRow("closeout.elapsed_seconds","Closeout time",r=>r.closeout?.elapsed_seconds,benchmarkSeconds)+
    numericRow("closeout.cost_usd","Closeout cost",r=>r.closeout?.cost_usd,benchmarkMoney,{costPhase:"closeout"})+
    numericRow("closeout.credit_estimate","Closeout credit estimate",r=>r.closeout?.credit_estimate,benchmarkCredits)+
    row("Closeout readback",r=>r.closeout?.verified===true?'<span class="benchmark-verified">Verified</span>':r.closeout?.verified===false?'<span class="benchmark-issue">Failed verification</span>':'<span class="quiet">Unknown / pending</span>')+
    numericRow("closeout.quality","Closeout quality",r=>r.closeout?.quality,v=>`${benchmarkCount(v)} / 100`,{max:100,empty:()=>"Ungraded"}):"")+
    section("Answer usage")+usageRows("answer")+
    section("Model token split")+
    row("Primary / support / combined",r=>benchmarkTokenSplit(r),"benchmark-token-split-row")+
    (hasReview?section("Semantic review usage")+usageRows("review")+row("Review tools",r=>benchmarkTools(r.review?.tool_counts)):"")+
    (isMemoryStudy?section("Context & RAM")+
    row("Conversation context",r=>benchmarkText(r.context_condition))+
    row("RAM access",r=>benchmarkText(r.ram_condition))+
    numericRow("memory.first_input_tokens","First input tokens",r=>r.memory_metrics?.first_input_tokens)+
    numericRow("memory.first_cached_tokens","First cached tokens",r=>r.memory_metrics?.first_cached_tokens)+
    numericRow("memory.compactions","Context compactions",r=>r.memory_metrics?.compactions)+
    numericRow("memory.compaction_input_tokens","Compaction input",r=>r.memory_metrics?.compaction_input_tokens)+
    numericRow("memory.compaction_output_tokens","Compaction output",r=>r.memory_metrics?.compaction_output_tokens)+
    numericRow("memory.read_calls","RAM lookup calls",r=>r.memory_metrics?.read_calls)+
    numericRow("memory.unique_records","Records returned",r=>r.memory_metrics?.unique_records)+
    (results.some(r=>r.memory_metrics?.records_opened!=null)?numericRow("memory.records_opened","Records opened",r=>r.memory_metrics?.records_opened):"")+
    numericRow("memory.output_bytes","RAM bytes returned",r=>r.memory_metrics?.output_bytes)+
    numericRow("memory.source_files","Source files (reported)",r=>r.memory_metrics?.source_files):"")+
    (!isMemoryStudy||results.some(r=>r.support_model)?section("Who did the discovery")+
    numericRow("primary.input_tokens","Primary input",r=>r.primary?.input_tokens,benchmarkCount,{scope:"Answer"})+
    numericRow("support.input_tokens","Helper input",r=>r.support_model?r.support?.input_tokens:null,benchmarkCount,{scope:"Answer",empty:helperEmpty})+
    numericRow("primary.reasoning_output_tokens","Primary reasoning",r=>r.primary?.reasoning_output_tokens,benchmarkCount,{scope:"Answer"})+
    numericRow("support.reasoning_output_tokens","Helper reasoning",r=>r.support_model?r.support?.reasoning_output_tokens:null,benchmarkCount,{scope:"Answer",empty:helperEmpty})+
    numericRow("primary.cost_usd","Primary cost",r=>r.primary?.cost_usd,benchmarkMoney,{scope:"Answer",costPhase:"primary"})+
    numericRow("support.cost_usd","Helper cost",r=>r.support_model?r.support?.cost_usd:null,benchmarkMoney,{scope:"Answer",empty:helperEmpty,costPhase:"support"})+
    numericRow("packet.words","Packet words",r=>r.support_model?r.packet?.words:null,benchmarkCount,{empty:helperEmpty})+
    numericRow("packet.bytes","Packet bytes",r=>r.support_model?r.packet?.bytes:null,benchmarkCount,{empty:helperEmpty}):"")+
    row("Primary source reads",r=>benchmarkSummary(r.primary_reads,`benchmark-reads-${r.key}`,"Not assessed"),"benchmark-prose-row")+
    section("Output & findings")+
    row("Output summary",r=>benchmarkSummary(r.output_summary,`benchmark-output-${r.key}`,"Answer pending"),"benchmark-prose-row")+
    row("Quality assessment",r=>benchmarkSummary(r.quality?.summary,`benchmark-quality-${r.key}`,"No assessment recorded"),"benchmark-prose-row")+
    row("Review basis and dimensions",r=>benchmarkReviewDetail(r),"benchmark-prose-row")+
    row("Material issues",r=>Array.isArray(r.quality?.issues)?r.quality.issues.length?`<ul class="benchmark-issues">${r.quality.issues.map(issue=>`<li>${benchmarkText(typeof issue==="string"?issue:[issue?.summary||issue?.issue,issue?.sources].filter(Boolean).join(" — "))}</li>`).join("")}</ul>`:"No issues recorded":"Unknown / not reviewed","benchmark-prose-row")+
    row("Answer tools",r=>benchmarkTools(r.answer?.tool_counts))+
    (hasCloseout?section("Closeout usage")+usageRows("closeout")+
    row("Closeout summary",r=>benchmarkSummary(r.closeout?.summary,`benchmark-closeout-${r.key}`,"Closeout pending"),"benchmark-prose-row")+
    row("Closeout tools",r=>benchmarkTools(r.closeout?.tool_counts)):"") +
    section("Source detail")+
    row("Primary model",r=>benchmarkText(r.primary_model))+
    row("Primary thinking",r=>benchmarkText(r.primary_effort,"Unknown"))+
    row("Support model",r=>benchmarkText(r.support_model,"Companion off"))+
    row("Support thinking",r=>r.support_model?benchmarkText(r.support_effort,"Unknown"):"Companion off")+
    row("Review thinking",r=>hasReview?benchmarkText(r.review?.effort,"Unknown"):"Not run")+
    row("Opaque report",r=>benchmarkReportLink(r))+
    row("Full answer",r=>r.answer_text?`<details data-persist="benchmark-answer-${esc(r.key)}"><summary>Read answer</summary><div class="benchmark-answer">${esc(r.answer_text)}</div></details>`:"Not retained")+
    `</tbody></table></div><p class="quiet">Cached input is included in input tokens. Full output is visible output plus exposed reasoning. Worker time can sum multiple agents and differs from wall time. Costs are API equivalents, not account charges. Credit estimates use standard rates and are not account deductions. Recorded compaction tokens are included in usage totals; the separate compaction rows are a breakdown, not additional tokens.</p>`)+
    (limits.length?`<div class="callout benchmark-limits"><strong>Comparison limits</strong><ul>${[...new Set(limits)].map(limit=>`<li>${esc(limit)}</li>`).join("")}</ul></div>`:"");
}
function refreshBenchmark(focusKey,direction){
  const scroll=document.querySelector(".benchmark-scroll"),position=scroll?{left:scroll.scrollLeft,top:scroll.scrollTop}:null;
  render();
  const updated=document.querySelector(".benchmark-scroll");
  if(position&&updated){updated.scrollLeft=position.left;updated.scrollTop=position.top}
  if(focusKey){const candidates=[...document.querySelectorAll("[data-benchmark-move]")].filter(b=>b.dataset.benchmarkMove===focusKey);const button=candidates.find(b=>b.dataset.direction===direction&&!b.disabled)||candidates.find(b=>!b.disabled);button?.focus({preventScroll:true})}
}
function moveBenchmarkColumn(key,target){
  const prefs=sideBySideState.preferences.get(sideBySideState.task);
  if(!prefs||key===target||!prefs.order.includes(key)||!prefs.order.includes(target))return;
  const from=prefs.order.indexOf(key),to=prefs.order.indexOf(target);
  prefs.order.splice(from,1);prefs.order.splice(to,0,key);
  saveBenchmarkPreferences();refreshBenchmark(key,from<to?"1":"-1");
  toast("Experiment order saved.");
}
document.addEventListener("change",event=>{
  if(event.target.id==="benchmark-task"){sideBySideState.task=event.target.value;const url=new URL(location.href);url.searchParams.set("benchmark",sideBySideState.task);history.replaceState(null,"",url);render()}
  if(event.target.id==="benchmark-metric"){sideBySideState.preferences.get(sideBySideState.task).metric=event.target.value;saveBenchmarkPreferences();refreshBenchmark();document.getElementById("benchmark-metric")?.focus({preventScroll:true})}
  if(event.target.id==="benchmark-top-graph"){
    sideBySideState.preferences.get(sideBySideState.task).showTopGraph=event.target.checked;
    saveBenchmarkPreferences();refreshBenchmark();document.getElementById("benchmark-top-graph")?.focus({preventScroll:true});
  }
  if(event.target.matches("[data-benchmark-row-bars]")){
    const key=event.target.dataset.benchmarkRowBars,prefs=sideBySideState.preferences.get(sideBySideState.task);
    prefs.rowBars=prefs.rowBars.filter(x=>x!==key);if(event.target.checked)prefs.rowBars.push(key);
    saveBenchmarkPreferences();refreshBenchmark();[...document.querySelectorAll("[data-benchmark-row-bars]")].find(e=>e.dataset.benchmarkRowBars===key)?.focus({preventScroll:true});
  }
  if(event.target.matches("[data-benchmark-visible]")){
    const key=event.target.dataset.benchmarkVisible,prefs=sideBySideState.preferences.get(sideBySideState.task);
    prefs.hidden=prefs.hidden.filter(x=>x!==key);if(!event.target.checked)prefs.hidden.push(key);
    saveBenchmarkPreferences();refreshBenchmark();[...document.querySelectorAll("[data-benchmark-visible]")].find(e=>e.dataset.benchmarkVisible===key)?.focus({preventScroll:true});
  }
});
document.addEventListener("click",event=>{
  const button=event.target.closest("[data-benchmark-move]");
  if(button){const prefs=sideBySideState.preferences.get(sideBySideState.task),visible=prefs.order.filter(k=>!prefs.hidden.includes(k));moveBenchmarkColumn(button.dataset.benchmarkMove,visible[visible.indexOf(button.dataset.benchmarkMove)+Number(button.dataset.direction)])}
  if(event.target.closest("#benchmark-show-all")){sideBySideState.preferences.get(sideBySideState.task).hidden=[];saveBenchmarkPreferences();refreshBenchmark();document.getElementById("benchmark-show-all")?.focus({preventScroll:true})}
});
document.addEventListener("dragstart",event=>{
  const column=event.target.closest("[data-benchmark-column]");if(!column)return;
  sideBySideState.dragKey=column.dataset.benchmarkColumn;event.dataTransfer.effectAllowed="move";event.dataTransfer.setData("text/plain",sideBySideState.dragKey);column.classList.add("benchmark-dragging");
});
document.addEventListener("dragover",event=>{
  const column=event.target.closest("[data-benchmark-column]");if(!column||!sideBySideState.dragKey)return;
  event.preventDefault();event.dataTransfer.dropEffect="move";
});
document.addEventListener("drop",event=>{
  const column=event.target.closest("[data-benchmark-column]");if(!column||!sideBySideState.dragKey)return;
  event.preventDefault();moveBenchmarkColumn(sideBySideState.dragKey,column.dataset.benchmarkColumn);sideBySideState.dragKey=null;
});
document.addEventListener("dragend",()=>{sideBySideState.dragKey=null;document.querySelectorAll(".benchmark-dragging").forEach(e=>e.classList.remove("benchmark-dragging"))});
