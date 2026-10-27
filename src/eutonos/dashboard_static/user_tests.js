"use strict";

// Read-only HTML over validated native user-test records. Every record value is escaped.
function renderProjectUserTests(data){
  const badge=(label,tone="")=>`<span class="tag ${tone}">${esc(label)}</span>`;
  const outcomeLabel=value=>({"not-run":"Not run",passed:"Passed",failed:"Failed",blocked:"Blocked",inconclusive:"Inconclusive"})[value]||"Unknown";
  const values=(items,none)=>Array.isArray(items)&&items.length?`<ul>${items.map(item=>`<li>${esc(item)}</li>`).join("")}</ul>`:`<p class="quiet">${none}</p>`;
  const date=value=>{const parsed=new Date(value);return Number.isNaN(parsed.getTime())?esc(value||"Time unknown"):esc(parsed.toLocaleString())};
  const sources=sources=>Array.isArray(sources)&&sources.length?`<ul>${sources.map(source=>`<li class="read-evidence"><strong>${esc(source.path)}</strong><br><span class="quiet">Revision ${esc(source.revision)} · SHA-256 ${esc(source.sha256)}</span></li>`).join("")}</ul>`:'<p class="quiet">No selected source yet.</p>';
  const steps=steps=>Array.isArray(steps)&&steps.length?`<ol>${steps.map(step=>`<li><strong>${esc(step.action)}</strong><br><span class="quiet">Expected: ${esc(step.expected)}</span></li>`).join("")}</ol>`:'<p class="quiet">Steps still need to be specified.</p>';
  const criteria=criteria=>Array.isArray(criteria)&&criteria.length?`<ul>${criteria.map(item=>`<li><strong>${esc(item.id)}</strong> · ${esc(item.description)}</li>`).join("")}</ul>`:'<p class="quiet">Criteria still need to be specified.</p>';
  const observations=items=>Array.isArray(items)&&items.length?`<ul>${items.map(item=>`<li><strong>${esc(item.criterion_id)}</strong> · ${esc(item.outcome)}<br>${esc(item.note)}</li>`).join("")}</ul>`:'<p class="quiet">No criterion observations recorded.</p>';
  const evidence=value=>Array.isArray(value?.items)&&value.items.length?`<ul>${value.items.map(item=>`<li class="read-evidence">${item.path?`<strong>${esc(item.path)}</strong><br><span class="quiet">Declared SHA-256 ${esc(item.sha256)}</span>`:""}${item.note?`<p>${esc(item.note)}</p>`:""}</li>`).join("")}</ul>`:'<p class="quiet">No evidence recorded.</p>';
  const history=entry=>{
    const run=entry.record||{},earlier=entry.status==="stale";
    return `<details class="card read-evidence"><summary><strong>${esc(outcomeLabel(run.outcome))}</strong> · ${date(run.recorded_at)} · ${esc(run.id||"Unknown run")}${earlier?" · Earlier setup":""}</summary><div class="meta">${badge(earlier?"Earlier setup":"Current setup",earlier?"warn":"good")}</div>${earlier?'<p class="callout">This result belongs to an earlier setup and does not assess the current criteria.</p>':""}<p>${esc(run.summary)}</p><h4>Criterion observations</h4>${observations(run.observations)}<h4>Evidence</h4><p class="quiet">References and digests are recorded claims; this screen does not verify file bytes.</p>${evidence(run.evidence)}<h4>Run limits</h4>${values(run.limits,"No additional run limits recorded.")}${earlier?`<details><summary>Earlier source and criteria</summary><h4>Source pins</h4>${sources(run.setup?.sources)}<h4>Criteria</h4>${criteria(run.setup?.criteria)}</details>`:""}</details>`;
  };
  const row=entry=>{
    const setup=entry.setup||{},runs=Array.isArray(entry.runs)?entry.runs:[],complete=entry.history_complete===true;
    const current=complete?entry.latest_run:null;
    const latest=!complete?"Unknown · history incomplete":current?outcomeLabel(current.outcome):runs.some(run=>run.status==="stale")?"No result for current setup":"No result recorded";
    const tone=!complete||setup.readiness==="draft"?"warn":current?.outcome==="passed"?"good":current?.outcome==="failed"?"warn":"";
    const templateNames={"behavior-regression":"Behavior or regression","user-workflow":"User workflow","comparison-experiment":"Comparison or experiment"};
    const template=setup.template?`${templateNames[setup.template.id]||setup.template.id} v${setup.template.version}`:"Custom";
    return `<section class="card read-evidence"><div class="section-head"><div><h3>${esc(setup.title)}</h3><p>${esc(setup.purpose)}</p></div>${badge(latest,tone)}</div><div class="meta">${badge(setup.readiness==="ready"?"Ready to assess":"Draft",setup.readiness==="ready"?"good":"warn")}${badge(template)}<span>Work: ${esc(setup.work_id||"No board item linked")}</span></div>${!complete?'<p class="callout">Run history is incomplete or could not be read. The latest result is unknown.</p>':""}<details><summary>Setup, sources and run history</summary><h4>Source pins</h4>${sources(setup.sources)}<h4>Prerequisites</h4>${values(setup.prerequisites,"None recorded.")}<h4>Inputs</h4>${values(setup.inputs,"None recorded.")}<h4>Steps</h4>${steps(setup.steps)}<h4>Evaluation criteria</h4>${criteria(setup.criteria)}<h4>Setup limits</h4>${values(setup.limits,"None recorded.")}<h4>Run history · ${esc(runs.length)} retained${entry.run_count_partial?" (partial)":""}</h4>${runs.length?runs.map(history).join(""):'<p class="quiet">No valid run records available.</p>'}</details></section>`;
  };
  const introduction='<section class="card"><div class="section-head"><h2>Your project tests</h2></div><p>Ask your agent to create a test for selected work or adapt a starter; its setup and results appear here.</p></section>';
  if(data?.status==="error")return introduction+`<div class="callout">Project tests are unavailable: ${esc(data.reason||"Reader error")}</div>`;
  if(data?.status!=="available")return introduction+'<div class="callout">Project test coverage is unknown.</div>';
  const rows=Array.isArray(data.tests)?data.tests:[],diagnostics=Array.isArray(data.diagnostics)?data.diagnostics:[];
  const problems=diagnostics.length?`<details class="card read-evidence"><summary>${esc(diagnostics.length)} test record issue${diagnostics.length===1?"":"s"}</summary><p class="quiet">Malformed, unsafe or truncated records are excluded from confirmed results.</p><ul>${diagnostics.map(item=>`<li><strong>${esc(item.path)}</strong> · ${esc(item.error)}</li>`).join("")}</ul></details>`:"";
  return introduction+(rows.length?`<div class="comparison-section">${rows.map(row).join("")}</div>`:'<div class="card empty"><strong>No project tests yet</strong><p>Ask your agent to turn a selected piece of work into a test setup.</p></div>')+problems;
}
