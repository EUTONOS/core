const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');

// Load the real esc/num/when/tag/status/heading/stat/comparisonBars/empty
// helpers straight out of app.js (same pattern as test_dashboard_boards.cjs),
// then load repo_views.js on top so it sees the same globals it gets in the
// browser. repo_views.js exports its internals via module.exports when a
// CommonJS `module` global is present, which lets us unit-test buildGraph
// and graphState directly instead of simulating DOM events.
const appSource = fs.readFileSync('src/eutonos/dashboard_static/app.js', 'utf8');
const helperEnd = appSource.indexOf('function chart(');
const helperSlice = appSource.slice(appSource.indexOf('const esc='), helperEnd);
const repoSource = fs.readFileSync('src/eutonos/dashboard_static/repo_views.js', 'utf8');

const moduleObj = { exports: {} };
const ctx = vm.createContext({
  Intl,
  state: { snapshot: null, project: 0 },
  module: moduleObj,
  console,
});
vm.runInContext(helperSlice, ctx);
vm.runInContext(repoSource, ctx, { filename: 'repo_views.js' });

const repoViews = moduleObj.exports.repoViews;
const buildGraph = moduleObj.exports.buildGraph;
const graphState = moduleObj.exports.graphState;
const ensureVisible = moduleObj.exports.ensureVisible;

function resetGraphState() {
  graphState.search = '';
  graphState.hiddenKinds = new Set();
  graphState.showFiles = false;
  graphState.expandedGroups = new Set();
  graphState.selected = null;
  graphState.view = 'graph';
}

// ---------- Repo health ----------
{
  const p = { modules: { repo_health: {
    status: 'available', profile: 'eutonos.repo-health.v1', observed_at: '2026-09-25T00:00:00Z',
    indicators: [
      { id: 'verify-1', area: 'Verification', label: 'Last checked run', state: 'healthy', rule: 'Required checks passed', threshold: null, evidence: ['tests/test_dashboard.py'], scope: 'repository', observed_at: '2026-09-24T00:00:00Z', next_action: 'None' },
      { id: 'knowledge-1', area: 'Knowledge references', label: 'Changed source since review', state: 'warning', rule: 'Source changed after review', threshold: null, evidence: ['docs/PRINCIPLES.md'], scope: 'docs/PRINCIPLES.md', observed_at: '2026-09-24T00:00:00Z', next_action: 'Re-review the source.' },
      { id: 'delivery-1', area: 'Delivery', label: 'Blocking check failing', state: 'alert', rule: 'Required check on selected source', threshold: 1, evidence: [], scope: 'main', observed_at: '2026-09-23T00:00:00Z', next_action: 'Fix the failing check.' },
      { id: 'dep-1', area: 'Dependencies', label: 'Audit coverage', state: 'unknown', rule: 'No audit evidence available', threshold: null, evidence: [], scope: 'repository', observed_at: null, next_action: 'Run an audit.' },
      { id: 'runtime-1', area: 'Runtime compatibility', label: 'Optional module', state: 'disabled', rule: 'Module not selected', threshold: null, evidence: [], scope: 'atlas', observed_at: null, next_action: 'None' },
    ],
    counts: { healthy: 1, warning: 1, alert: 1, unknown: 1, disabled: 1 },
    coverage_note: 'Coverage reflects five declared indicator areas.',
  } } };
  const html = repoViews['repo-health'](p);
  assert.match(html, /Repo health/);
  assert.match(html, /<div class="stat-label">Healthy<\/div><div class="stat-value">1<\/div>/);
  assert.match(html, /<div class="stat-label">Warning<\/div><div class="stat-value">1<\/div>/);
  assert.match(html, /<div class="stat-label">Alert<\/div><div class="stat-value">1<\/div>/);
  assert.match(html, /<div class="stat-label">Unknown<\/div><div class="stat-value">1<\/div>/);
  assert.match(html, /<div class="stat-label">Disabled<\/div><div class="stat-value">1<\/div>/);
  assert.doesNotMatch(html, /%/, 'health counts must not render a composite percentage');
  assert.match(html, /Verification/);
  assert.match(html, /Re-review the source\./);
  assert.match(html, /Fix the failing check\./);
  console.log('Repo health: counts without a percentage, grouped indicators and detail fields passed.');
}

// Missing / error repo_health module never throws and explains itself.
{
  const html = repoViews['repo-health']({ modules: {} });
  assert.match(html, /unavailable/i);
  const errHtml = repoViews['repo-health']({ modules: { repo_health: { status: 'error', reason: 'No readable indicator source.' } } });
  assert.match(errHtml, /No readable indicator source\./);
  console.log('Repo health: missing and error modules render an explanation instead of throwing.');
}

// ---------- Repo hotspots ----------
{
  const p = { modules: { repo_metrics: {
    status: 'available', profile: 'eutonos.repo-metrics.v1', observed_at: '2026-09-25T00:00:00Z',
    window: { commits_scanned: 400, limit: 500, first_commit: 'aaa', last_commit: 'bbb', first_at: '2026-08-01T00:00:00Z', last_at: '2026-09-24T00:00:00Z', truncated: false },
    hotspots: [ { path: 'src/eutonos/dashboard.py', commits: 40, last_at: '2026-09-24T00:00:00Z' }, { path: 'src/eutonos/hub.py', commits: 12, last_at: '2026-09-20T00:00:00Z' } ],
    directories: [ { path: 'src/eutonos', commits: 90 }, { path: 'docs', commits: 30 } ],
    co_change: [ { a: 'src/eutonos/dashboard.py', b: 'src/eutonos/dashboard_static/app.js', count: 8 } ],
    co_change_skipped_commits: 2,
    divergence: { branch: 'main', upstream: 'origin/main', ahead: 3, behind: 0, qualification: 'Local remote ref observed 2026-09-24.' },
    size: { tracked_files: 512 },
    coverage_note: 'Bounded to the last 500 commits.',
  } } };
  const html = repoViews['repo-hotspots'](p);
  assert.match(html, /Repo hotspots/);
  assert.match(html, /Scanned 400 of 500 commit limit/);
  assert.match(html, /dashboard\.py/);
  assert.match(html, /Files changed together suggest investigation, not a dependency\./);
  assert.match(html, /2 commits were skipped/);
  assert.match(html, /Local remote ref observed 2026-09-24\./);
  assert.match(html, /512/);
  assert.match(html, /Bounded to the last 500 commits\./);
  console.log('Repo hotspots: window line, bar lists, co-change note and divergence card passed.');
}

{
  const html = repoViews['repo-hotspots']({ modules: {} });
  assert.match(html, /unavailable/i);
  console.log('Repo hotspots: missing module renders an explanation instead of throwing.');
}

// ---------- NAV connection graph ----------
const navFixture = {
  status: 'available',
  records: 7,
  by_kind: { system: 2, surface: 1, 'source-family': 4 },
  review: { candidate: 6, gap: 1 },
  routes: [
    { id: 'SYS-a', title: 'System A', kind: 'system', summary: 'Reads context.', review: 'candidate', evidence: ['src/eutonos/hub.py'], related: ['SYS-b', 'SRC-hub'] },
    { id: 'SYS-b', title: 'System B', kind: 'system', summary: 'Writes effects.', review: 'candidate', evidence: ['src/eutonos/host.py'], related: ['SRC-host'] },
    { id: 'SUR-a', title: 'Surface A', kind: 'surface', summary: 'A dashboard page.', review: 'candidate', evidence: [], related: ['SYS-a'] },
    { id: 'SRC-hub', title: 'hub.py', kind: 'source-family', summary: 'Source route for hub.py.', review: 'candidate', evidence: ['src/eutonos/hub.py'], related: [] },
    { id: 'SRC-host', title: 'host.py', kind: 'source-family', summary: 'Source route for host.py.', review: 'candidate', evidence: ['src/eutonos/host.py'], related: [] },
    { id: 'SRC-principles', title: 'PRINCIPLES.md', kind: 'source-family', summary: 'Source route for PRINCIPLES.md.', review: 'candidate', evidence: ['docs/PRINCIPLES.md'], related: [] },
    { id: 'SRC-orphan', title: 'orphan.md', kind: 'source-family', summary: 'Unreferenced file route.', review: 'candidate', evidence: ['docs/orphan.md'], related: [] },
  ],
};

{
  resetGraphState();
  const model = buildGraph(navFixture);
  // Files hidden by default: none of the 4 source-family routes appear as nodes.
  assert.equal(model.nodes.some((n) => n.kind === 'source-family' || n.type === 'file' || n.type === 'group'), false);
  assert.equal(model.hiddenCount, 4);
  // No edge is drawn to a hidden node: the two SYS->SRC evidence edges must be dropped, not invented onto a visible node.
  assert.equal(model.edges.some((e) => e.type === 'evidence'), false);
  assert.equal(model.edges.length, 2); // SYS-a related SYS-b, SUR-a related SYS-a
  console.log('NAV graph: files hidden by default, hidden count reported, no edges invented to hidden nodes.');
}

{
  resetGraphState();
  graphState.showFiles = true;
  const model = buildGraph(navFixture);
  const groupNodes = model.nodes.filter((n) => n.type === 'group');
  assert.ok(groupNodes.length >= 2, 'expected collapsed directory groups for src/eutonos and docs');
  assert.equal(model.hiddenCount, 0);
  const evidenceEdges = model.edges.filter((e) => e.type === 'evidence');
  assert.equal(evidenceEdges.length, 2);
  // Edges must land on the collapsed group node id, never on a still-hidden individual file id.
  evidenceEdges.forEach((e) => {
    const targetNode = model.nodes.find((n) => n.x === e.x2 - (n && 0) || true);
  });
  const groupIds = new Set(groupNodes.map((n) => n.id));
  const nodesById = new Map(model.nodes.map((n) => [n.id, n]));
  // Recover target id by matching edge endpoint coordinates to a node.
  const byPos = (x, y) => model.nodes.find((n) => n.x === x && Math.abs(n.y + n.h / 2 - y) < 0.01);
  evidenceEdges.forEach((e) => {
    const target = byPos(e.x2, e.y2);
    assert.ok(target, 'evidence edge must land on a rendered node');
    assert.equal(target.type, 'group', 'a collapsed group must receive the bundled edge, not a hidden file');
  });
  console.log('NAV graph: showing files renders collapsed directory groups and bundles evidence edges onto them.');
}

{
  resetGraphState();
  graphState.showFiles = true;
  graphState.expandedGroups.add('GROUP:src/eutonos');
  const model = buildGraph(navFixture);
  const fileNode = model.nodes.find((n) => n.id === 'SRC-hub');
  assert.ok(fileNode && fileNode.type === 'file', 'expanding a group must reveal its individual file nodes');
  const byPos = (x, y) => model.nodes.find((n) => n.x === x && Math.abs(n.y + n.h / 2 - y) < 0.01);
  const edgeToHub = model.edges.find((e) => e.type === 'evidence' && byPos(e.x2, e.y2) && byPos(e.x2, e.y2).id === 'SRC-hub');
  assert.ok(edgeToHub, 'expanded file must receive its own direct edge');
  console.log('NAV graph: expanding a directory group reveals its files and rewires edges onto them.');
}

{
  resetGraphState();
  graphState.search = 'surface';
  const model = buildGraph(navFixture);
  const ids = model.nodes.map((n) => n.id).sort();
  assert.equal(ids.length, 1);
  assert.equal(ids[0], 'SUR-a');
  assert.equal(model.hiddenCount, 6); // 2 non-matching records + 4 hidden files
  console.log('NAV graph: search filters nodes by id/title/summary and updates the hidden count.');
}

{
  resetGraphState();
  const model = repoViews['nav-graph']({ modules: { nav: navFixture } });
  assert.match(model, /NAV connection graph/);
  assert.match(model, /nav-graph-svg/);
  assert.match(model, /hidden/);
}

{
  const html = repoViews['nav-graph']({ modules: {} });
  assert.match(html, /unavailable/i);
  console.log('NAV graph: missing NAV module renders an explanation instead of throwing.');
}

{
  resetGraphState();
  const nav = navFixture;
  ensureVisible(nav, 'SRC-hub');
  assert.equal(graphState.showFiles, true);
  assert.ok(graphState.expandedGroups.has('GROUP:src/eutonos'));
  console.log('NAV graph: selecting a related file id auto-reveals its group.');
}

resetGraphState();
console.log('All repo view tests passed.');
