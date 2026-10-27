"use strict";
/* Repository visibility, health and exploration views (Repo Health, Cheap
   repository metrics, NAV connection graph). Loaded after app.js; reuses its
   globals (esc, num, tag, status, heading, stat, empty, when, content,
   project, render) rather than redefining them. Exposes window.repoViews for
   the app.js router hook. */
(function () {
  const KIND_LABEL = {
    gap: "Gaps",
    system: "Systems",
    surface: "Surfaces",
    interaction: "Interactions",
    journey: "Journeys",
    "source-family": "Files",
  };
  const RECORD_KINDS = ["gap", "system", "surface", "interaction", "journey"];

  function graphKindOrder() {
    return RECORD_KINDS.slice();
  }

  function truncateLabel(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function fileTopDir(route) {
    const path = (route.evidence || [])[0] || route.id;
    const idx = String(path).lastIndexOf("/");
    return idx >= 0 ? path.slice(0, idx) : "(root)";
  }

  // ---------- Repo health ----------
  const STATE_ICON = { healthy: "✓", warning: "!", alert: "✕", unknown: "?", disabled: "–" };
  const STATE_TONE = { healthy: "good", warning: "warn", alert: "bad" };
  const STATE_LABEL = { healthy: "Healthy", warning: "Warning", alert: "Alert", unknown: "Unknown", disabled: "Disabled" };

  function indicatorRow(ind) {
    const icon = STATE_ICON[ind.state] || "?";
    const tone = STATE_TONE[ind.state] || "";
    const evidence = ind.evidence || [];
    return `<details class="indicator-row" data-persist="health-${esc(ind.id)}">` +
      `<summary><span class="indicator-icon ${esc(tone)}" aria-hidden="true">${icon}</span>` +
      `<span class="indicator-label">${esc(ind.label)}</span>` +
      `<span class="indicator-state-text">${esc(STATE_LABEL[ind.state] || "Unknown")}</span></summary>` +
      `<div class="indicator-detail">` +
      `<p><strong>Rule</strong> ${esc(ind.rule || "Not recorded")}${ind.threshold != null ? ` &middot; Threshold ${esc(ind.threshold)}` : ""}</p>` +
      `<p><strong>Scope</strong> ${esc(ind.scope || "Not recorded")}</p>` +
      `<p><strong>Last observed</strong> ${esc(when(ind.observed_at))}</p>` +
      (evidence.length
        ? `<p><strong>Evidence</strong></p><ul>${evidence.map((e) => `<li class="board-source">${esc(e)}</li>`).join("")}</ul>`
        : `<p class="quiet">No evidence recorded.</p>`) +
      `<p><strong>Next action</strong> ${esc(ind.next_action || "None recorded")}</p>` +
      `</div></details>`;
  }

  function repoHealth(p) {
    const m = p.modules && p.modules.repo_health;
    if (!m || !["available", "partial"].includes(m.status)) {
      return heading(
        "Repo health",
        "Verification, work-record integrity, knowledge, delivery, recovery, dependency and runtime indicators."
      ) + empty("Repo health unavailable", (m && (m.reason || m.message)) || "No repo health module is available in this snapshot.");
    }
    const counts = m.counts || {};
    const order = ["healthy", "warning", "alert", "unknown", "disabled"];
    const countsRow = `<div class="grid stats health-counts">${order
      .map((k) => stat(STATE_LABEL[k], counts[k] ?? 0, "Indicators in this state"))
      .join("")}</div>`;
    const groups = new Map();
    (m.indicators || []).forEach((ind) => {
      const key = ind.area || "General";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(ind);
    });
    const areaSections =
      [...groups.entries()]
        .map(
          ([area, list]) =>
            `<section class="card health-area"><div class="section-head"><h2>${esc(area)}</h2><span class="quiet">${list.length} indicator${list.length === 1 ? "" : "s"}</span></div><div class="indicator-list">${list
              .map(indicatorRow)
              .join("")}</div></section>`
        )
        .join("") || empty("No indicators recorded", "No repo health indicators were recorded for this repository.");
    return (
      heading(
        "Repo health",
        "Verification, work-record integrity, knowledge, delivery, recovery, dependency and runtime indicators. Coverage reflects declared checks, not a whole-repository correctness guarantee."
      ) +
      countsRow +
      (m.coverage_note ? `<p class="quiet">${esc(m.coverage_note)}</p>` : "") +
      areaSections
    );
  }

  // ---------- Repo hotspots (cheap repository metrics) ----------
  function barList(title, items, note) {
    const valid = (v) => typeof v === "number" && Number.isFinite(v) && v >= 0;
    const ceiling = Math.max(1, ...items.filter((x) => valid(x.value)).map((x) => x.value));
    return `<section class="card visual-comparison"><div class="section-head"><h2>${esc(title)}</h2></div><div class="comparison-rows">${
      items.length
        ? items
            .map(
              (x) =>
                `<div class="comparison-row"><div class="comparison-label"><span>${esc(x.label)}${
                  x.detail ? `<br><small class="quiet">${esc(x.detail)}</small>` : ""
                }</span><strong>${valid(x.value) ? num(x.value) : "Unknown"}</strong></div><svg class="comparison-track" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true"><rect width="100" height="8" rx="2" class="comparison-track-bg"/>${
                  valid(x.value) ? `<rect width="${(100 * x.value) / ceiling}" height="8" rx="2" class="comparison-fill"/>` : ""
                }</svg></div>`
            )
            .join("")
        : `<p class="quiet">No data recorded.</p>`
    }</div><p class="quiet comparison-note">${esc(note)}</p></section>`;
  }

  function repoHotspots(p) {
    const m = p.modules && p.modules.repo_metrics;
    if (!m || !["available", "partial"].includes(m.status)) {
      return heading(
        "Repo hotspots",
        "Cheap, bounded repository metrics from Git history, existing reports and file metadata."
      ) + empty("Repo metrics unavailable", (m && (m.reason || m.message)) || "No repository metrics module is available in this snapshot.");
    }
    const w = m.window || {};
    const windowLine = `<p class="quiet repo-hotspots-window">Scanned ${num(w.commits_scanned)} of ${
      w.limit != null ? num(w.limit) + " commit limit" : "an unbounded scan"
    }${w.truncated ? " (truncated; more history exists)" : ""}${
      w.first_at && w.last_at ? ` &middot; ${esc(when(w.first_at))} – ${esc(when(w.last_at))}` : ""
    }.</p>`;
    const hotspotBars = barList(
      "Change frequency (hotspots)",
      (m.hotspots || []).map((h) => ({ label: h.path, value: h.commits, detail: h.last_at ? `Last changed ${when(h.last_at)}` : null })),
      "Files changed together suggest investigation, not a dependency. Change frequency alone is not a defect score."
    );
    const dirBars = barList(
      "Directory activity",
      (m.directories || []).map((d) => ({ label: d.path, value: d.commits })),
      "Directory-level commit counts across the scanned window."
    );
    const coChangeRows = (m.co_change || [])
      .map((c) => `<tr><td>${esc(c.a)}</td><td>${esc(c.b)}</td><td>${num(c.count)}</td></tr>`)
      .join("");
    const coChange = `<section class="card"><div class="section-head"><h2>Files changed together</h2>${tag(`${(m.co_change || []).length} pairs`)}</div>${
      coChangeRows
        ? `<div class="table-wrap"><table><thead><tr><th>File A</th><th>File B</th><th>Co-changes</th></tr></thead><tbody>${coChangeRows}</tbody></table></div>`
        : empty("No co-change pairs recorded", "No files were observed changing together in the scanned window.")
    }<p class="quiet">Files changed together suggest investigation, not a dependency.${
      m.co_change_skipped_commits ? ` ${num(m.co_change_skipped_commits)} commits were skipped from this analysis.` : ""
    }</p></section>`;
    const div = m.divergence || {};
    const divergenceCard = `<section class="card"><div class="section-head"><h2>Branch divergence</h2></div><p><strong>${esc(
      div.branch || "Current branch"
    )}</strong> vs ${esc(div.upstream || "no recorded upstream")}</p><div class="grid stats">${stat(
      "Ahead",
      div.ahead ?? "Unknown",
      "Commits not yet upstream"
    )}${stat("Behind", div.behind ?? "Unknown", "Upstream commits not yet merged")}</div><p class="quiet">${esc(
      div.qualification || "Local remote refs need an observation/fetch qualification."
    )}</p></section>`;
    const sizeCard = `<section class="card"><div class="section-head"><h2>Size</h2></div><div class="grid stats">${stat(
      "Tracked files",
      num(m.size && m.size.tracked_files),
      "Current working tree"
    )}</div></section>`;
    return (
      heading("Repo hotspots", "Cheap, bounded repository metrics from Git history, existing reports and file metadata.") +
      windowLine +
      `<div class="grid two">${hotspotBars}${dirBars}</div>` +
      coChange +
      `<div class="grid two">${divergenceCard}${sizeCard}</div>` +
      (m.coverage_note ? `<p class="quiet">${esc(m.coverage_note)}</p>` : "")
    );
  }

  // ---------- NAV connection graph ----------
  const graphState = {
    search: "",
    hiddenKinds: new Set(),
    showFiles: false,
    expandedGroups: new Set(),
    selected: null,
    view: "graph",
  };

  function ensureVisible(nav, id) {
    const route = (nav.routes || []).find((r) => r.id === id);
    if (!route) return;
    if (graphState.hiddenKinds.has(route.kind)) graphState.hiddenKinds.delete(route.kind);
    if (route.kind === "source-family") {
      graphState.showFiles = true;
      graphState.expandedGroups.add("GROUP:" + fileTopDir(route));
    }
  }

  function buildGraph(nav) {
    const routes = nav.routes || [];
    const byId = new Map(routes.map((r) => [r.id, r]));
    const query = graphState.search.trim().toLowerCase();
    const matches = (r) =>
      !query ||
      r.id.toLowerCase().includes(query) ||
      (r.title || "").toLowerCase().includes(query) ||
      (r.summary || "").toLowerCase().includes(query);
    const kindVisible = (k) => !graphState.hiddenKinds.has(k);

    const recordRoutes = routes.filter((r) => r.kind !== "source-family");
    const fileRoutes = routes.filter((r) => r.kind === "source-family");
    const visibleRecords = recordRoutes.filter((r) => kindVisible(r.kind) && matches(r));
    const visibleRecordIds = new Set(visibleRecords.map((r) => r.id));

    const laneWidth = 200, rowHeight = 42, nodeW = 172, nodeH = 32;
    const lanes = [];
    const nodes = [];
    const nodePosById = new Map();

    graphKindOrder()
      .filter(kindVisible)
      .forEach((k, laneIndex) => {
        const laneX = 20 + laneIndex * laneWidth;
        lanes.push({ key: k, label: KIND_LABEL[k], x: laneX });
        const items = visibleRecords
          .filter((r) => r.kind === k)
          .sort((a, b) => a.id.localeCompare(b.id));
        items.forEach((r, row) => {
          const pos = { id: r.id, type: "record", kind: k, x: laneX, y: 34 + row * rowHeight, w: nodeW, h: nodeH, label: r.title || r.id, sub: r.id, title: `${r.id}: ${r.title || ""}` };
          nodes.push(pos);
          nodePosById.set(r.id, pos);
        });
      });

    // Files lane
    const groupOf = new Map();
    const visibleFileIds = new Set();
    const visibleGroupIds = new Set();
    let hiddenFileCount = 0;
    if (graphState.showFiles) {
      const laneIndex = lanes.length;
      const laneX = 20 + laneIndex * laneWidth;
      lanes.push({ key: "source-family", label: KIND_LABEL["source-family"], x: laneX });
      const groups = new Map();
      fileRoutes.forEach((r) => {
        const dir = fileTopDir(r);
        if (!groups.has(dir)) groups.set(dir, []);
        groups.get(dir).push(r);
      });
      let row = 0;
      [...groups.keys()].sort().forEach((dir) => {
        const members = groups.get(dir);
        const shown = members.filter(matches).sort((a, b) => a.id.localeCompare(b.id));
        hiddenFileCount += members.length - shown.length;
        if (!shown.length) return;
        const groupId = "GROUP:" + dir;
        shown.forEach((m) => groupOf.set(m.id, groupId));
        const expanded = graphState.expandedGroups.has(groupId);
        const groupPos = { id: groupId, type: "group", x: laneX, y: 34 + row * rowHeight, w: nodeW, h: nodeH, label: `${dir} (${shown.length})`, sub: expanded ? "expanded" : "collapsed", title: `${dir}: ${shown.length} file(s)` };
        nodes.push(groupPos);
        nodePosById.set(groupId, groupPos);
        row += 1;
        if (expanded) {
          shown.forEach((m) => {
            const pos = { id: m.id, type: "file", kind: "source-family", x: laneX + 14, y: 34 + row * rowHeight, w: nodeW - 14, h: nodeH, label: m.title || m.id, sub: (m.evidence || [])[0] || "", title: `${m.id}: ${m.title || ""}` };
            nodes.push(pos);
            nodePosById.set(m.id, pos);
            visibleFileIds.add(m.id);
            row += 1;
          });
        } else {
          visibleGroupIds.add(groupId);
        }
      });
    } else {
      hiddenFileCount = fileRoutes.length;
    }

    const hiddenRecordCount = recordRoutes.length - visibleRecords.length;

    // Edges: only ever connect two currently-visible node ids. Hidden targets
    // are dropped, never rerouted onto an unrelated remaining node.
    const edges = [];
    visibleRecords.forEach((r) => {
      (r.related || []).forEach((relId) => {
        if (relId === r.id) return;
        const target = byId.get(relId);
        if (!target) return;
        if (target.kind === "source-family") {
          let toId = null;
          if (visibleFileIds.has(relId)) toId = relId;
          else if (groupOf.has(relId)) toId = groupOf.get(relId);
          if (!toId) return;
          edges.push({ from: r.id, to: toId, type: "evidence" });
        } else if (visibleRecordIds.has(relId)) {
          edges.push({ from: r.id, to: relId, type: "related" });
        }
      });
    });
    // De-duplicate bundled edges (multiple files in one collapsed group).
    const seen = new Set();
    const dedupedEdges = edges.filter((e) => {
      const key = e.from + ">" + e.to + ">" + e.type;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });

    const selected = graphState.selected;
    const neighborIds = new Set();
    if (selected) {
      neighborIds.add(selected);
      dedupedEdges.forEach((e) => {
        if (e.from === selected) neighborIds.add(e.to);
        if (e.to === selected) neighborIds.add(e.from);
      });
    }
    nodes.forEach((n) => {
      n.selected = n.id === selected;
      n.dimmed = Boolean(selected) && !neighborIds.has(n.id);
    });
    const edgesWithPos = dedupedEdges.map((e) => {
      const a = nodePosById.get(e.from), b = nodePosById.get(e.to);
      if (!a || !b) return null;
      return {
        type: e.type,
        x1: a.x + a.w, y1: a.y + a.h / 2,
        x2: b.x, y2: b.y + b.h / 2,
        dimmed: Boolean(selected) && !(e.from === selected || e.to === selected),
      };
    }).filter(Boolean);

    const maxRow = Math.max(1, ...nodes.map((n) => Math.round((n.y - 34) / rowHeight) + 1), 1);
    const width = Math.max(640, lanes.length * laneWidth + 40);
    const height = 60 + maxRow * rowHeight;

    return {
      nodes,
      edges: edgesWithPos,
      lanes,
      width,
      height,
      hiddenCount: hiddenRecordCount + hiddenFileCount,
      byId,
    };
  }

  function graphToolbar(model) {
    const kinds = graphKindOrder();
    return `<div class="nav-graph-toolbar">` +
      `<input type="search" id="nav-graph-search" placeholder="Search id, title or summary" value="${esc(graphState.search)}">` +
      `<div class="nav-graph-kinds">${kinds
        .map(
          (k) =>
            `<label><input type="checkbox" data-graph-kind-toggle="${k}" ${graphState.hiddenKinds.has(k) ? "" : "checked"}> ${esc(
              KIND_LABEL[k] || k
            )}</label>`
        )
        .join("")}</div>` +
      `<label><input type="checkbox" data-graph-toggle-files ${graphState.showFiles ? "checked" : ""}> Show files</label>` +
      `<div class="tabs nav-graph-view-toggle" role="group" aria-label="Graph display"><button type="button" data-graph-view="graph" aria-pressed="${
        graphState.view !== "list"
      }">Graph</button><button type="button" data-graph-view="list" aria-pressed="${graphState.view === "list"}">List</button></div>` +
      `<span class="nav-graph-hidden">${num(model.hiddenCount)} hidden</span>` +
      `</div>`;
  }

  function graphLegend() {
    return `<div class="nav-graph-legend">` +
      `<span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" class="graph-edge evidence"/></svg> Source / evidence reference</span>` +
      `<span><svg width="26" height="8" aria-hidden="true"><line x1="1" y1="4" x2="25" y2="4" class="graph-edge related"/></svg> Generic Related</span>` +
      `<span>Dashed border = collapsed group, showing a count</span>` +
      `</div>`;
  }

  function graphNodeSvg(n) {
    const cls = ["graph-node"];
    if (n.type === "group") cls.push("group");
    if (n.selected) cls.push("selected");
    if (n.dimmed) cls.push("dimmed");
    const attr = n.type === "group" ? `data-graph-group="${esc(n.id)}"` : `data-graph-node="${esc(n.id)}"`;
    return `<g class="${cls.join(" ")}" ${attr} tabindex="0" role="button" aria-label="${esc(n.title || n.label)}"><title>${esc(
      n.title || n.label
    )}</title><rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="6"/><text x="${n.x + 8}" y="${n.y + 14}">${esc(
      truncateLabel(n.label, 24)
    )}</text>${n.sub ? `<text class="graph-node-kind" x="${n.x + 8}" y="${n.y + 27}">${esc(truncateLabel(n.sub, 26))}</text>` : ""}</g>`;
  }

  function graphEdgeSvg(e) {
    const cls = ["graph-edge", e.type];
    if (e.dimmed) cls.push("dimmed");
    return `<line class="${cls.join(" ")}" x1="${e.x1}" y1="${e.y1}" x2="${e.x2}" y2="${e.y2}"/>`;
  }

  function graphSvgView(model) {
    if (!model.nodes.length) return empty("No matching records", "No NAV records match the current search and filters.");
    const lanesHtml = model.lanes.map((l) => `<text class="graph-lane-label" x="${l.x + 4}" y="18">${esc(l.label)}</text>`).join("");
    const edgesHtml = model.edges.map(graphEdgeSvg).join("");
    const nodesHtml = model.nodes.map(graphNodeSvg).join("");
    return `<div class="nav-graph-scroll"><svg class="nav-graph-svg" viewBox="0 0 ${model.width} ${model.height}" width="${model.width}" height="${model.height}" role="img" aria-label="NAV connection graph">${lanesHtml}<g class="graph-edges">${edgesHtml}</g><g class="graph-nodes">${nodesHtml}</g></svg></div>`;
  }

  function graphListView(model) {
    const items = model.nodes.filter((n) => n.type !== "group");
    if (!items.length) return empty("No matching records", "No NAV records match the current search and filters.");
    return `<div class="nav-graph-list">${items
      .map((n) => {
        const route = model.byId.get(n.id);
        if (!route) return "";
        return `<div class="route ${n.id === graphState.selected ? "selected" : ""}" data-graph-node="${esc(n.id)}"><div class="route-id">${esc(
          n.id
        )}</div><div><strong>${esc(route.title)}</strong><p>${esc(route.summary)}</p><div class="meta">${tag(route.kind)}${tag(
          route.review,
          route.review === "accepted" ? "good" : ""
        )}${(route.related || []).slice(0, 6).map((r) => tag(r)).join("")}</div></div></div>`;
      })
      .join("")}</div>`;
  }

  function graphDetailPanel(model) {
    const sel = graphState.selected;
    if (!sel) return "";
    const route = model.byId.get(sel);
    if (!route) return "";
    const relatedButtons = (route.related || [])
      .map((id) => {
        const t = model.byId.get(id);
        return `<button type="button" class="related-link" data-graph-related="${esc(id)}">${esc(id)}${t ? ` · ${esc(t.title)}` : ""}</button>`;
      })
      .join("");
    return `<section class="card nav-graph-detail"><div class="section-head"><h2>${esc(route.id)} · ${esc(route.title)}</h2>${tag(
      route.kind
    )}${tag(route.review, route.review === "accepted" ? "good" : "")}</div><p>${esc(route.summary)}</p>${
      (route.evidence || []).length
        ? `<p><strong>Evidence</strong></p><ul>${route.evidence.map((e) => `<li class="board-source">${esc(e)}</li>`).join("")}</ul>`
        : `<p class="quiet">No evidence recorded.</p>`
    }${
      relatedButtons
        ? `<p><strong>Related</strong></p><div class="related-links">${relatedButtons}</div>`
        : `<p class="quiet">No related records recorded.</p>`
    }</section>`;
  }

  function repoNavGraph(p) {
    const nav = p.modules && p.modules.nav;
    if (!nav || nav.status !== "available") {
      return heading(
        "NAV connection graph",
        "Browse recorded NAV records and their explicit related-item and source/evidence links."
      ) + empty("NAV unavailable", (nav && nav.reason) || "No readable NAV map is available in this snapshot.");
    }
    const model = buildGraph(nav);
    const body = graphState.view === "list" ? graphListView(model) : graphSvgView(model);
    return (
      heading(
        "NAV connection graph",
        "Systems, surfaces, journeys and their recorded relationships and source bindings. A generic Related edge does not prove navigation or runtime reachability."
      ) +
      graphToolbar(model) +
      graphLegend() +
      `<section class="card">${body}</section>` +
      graphDetailPanel(model)
    );
  }

  // ---------- Interaction wiring (delegated on the shared #content element) ----------
  if (typeof content !== "undefined" && content && content.addEventListener) {
    content.addEventListener("input", (e) => {
      if (e.target && e.target.id === "nav-graph-search") {
        graphState.search = e.target.value;
        if (typeof render === "function") render();
      }
    });
    content.addEventListener("change", (e) => {
      const kindToggle = e.target.closest && e.target.closest("[data-graph-kind-toggle]");
      if (kindToggle) {
        const k = kindToggle.dataset.graphKindToggle;
        if (kindToggle.checked) graphState.hiddenKinds.delete(k);
        else graphState.hiddenKinds.add(k);
        if (typeof render === "function") render();
        return;
      }
      const filesToggle = e.target.closest && e.target.closest("[data-graph-toggle-files]");
      if (filesToggle) {
        graphState.showFiles = filesToggle.checked;
        if (typeof render === "function") render();
      }
    });
    content.addEventListener("click", (e) => {
      const viewBtn = e.target.closest && e.target.closest("[data-graph-view]");
      if (viewBtn) {
        graphState.view = viewBtn.dataset.graphView;
        if (typeof render === "function") render();
        return;
      }
      const groupEl = e.target.closest && e.target.closest("[data-graph-group]");
      if (groupEl) {
        const id = groupEl.dataset.graphGroup;
        if (graphState.expandedGroups.has(id)) graphState.expandedGroups.delete(id);
        else graphState.expandedGroups.add(id);
        if (typeof render === "function") render();
        return;
      }
      const relatedEl = e.target.closest && e.target.closest("[data-graph-related]");
      if (relatedEl) {
        const id = relatedEl.dataset.graphRelated;
        const p = typeof project === "function" ? project() : null;
        const nav = p && p.modules && p.modules.nav;
        if (nav) ensureVisible(nav, id);
        graphState.selected = id;
        if (typeof render === "function") render();
        return;
      }
      const nodeEl = e.target.closest && e.target.closest("[data-graph-node]");
      if (nodeEl) {
        const id = nodeEl.dataset.graphNode;
        graphState.selected = graphState.selected === id ? null : id;
        if (typeof render === "function") render();
      }
    });
  }

  const repoViews = {
    "repo-health": repoHealth,
    "repo-hotspots": repoHotspots,
    "nav-graph": repoNavGraph,
  };
  if (typeof window !== "undefined") window.repoViews = repoViews;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { repoViews, buildGraph, graphState, ensureVisible, fileTopDir, graphKindOrder };
  }
})();
