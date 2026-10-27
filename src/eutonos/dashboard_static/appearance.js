/* Browser-only preference. Apply before styles paint; no repository settings writes. */
(() => {
  const key = "eutonos.appearance.v1";
  const choices = new Set(["system", "light", "dark"]);
  const root = document.documentElement;
  let preference = "system";
  try {
    const saved = localStorage.getItem(key);
    if (choices.has(saved)) preference = saved;
  } catch (_) { /* Storage can be unavailable in restricted browser contexts. */ }

  function apply(value) {
    preference = choices.has(value) ? value : "system";
    root.dataset.appearance = preference;
    const select = document.getElementById("appearance-select");
    if (select) select.value = preference;
  }
  apply(preference);
  document.addEventListener("DOMContentLoaded", () => {
    // Static, decorative line icons; labels remain the navigation's accessible names.
    const paths = {
      overview: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12"/>',
      'work-order': '<path d="M9 5h12M9 12h12M9 19h12M3 5h1M3 12h1M3 19h1"/>',
      delivery: '<path d="m12 3 9 5v8l-9 5-9-5V8ZM3 8l9 5 9-5M12 13v8M7.5 5.5l9 5v4"/>',
      boards: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18"/>',
      changes: '<path d="M3 11a9 9 0 1 1 3 8M3 4v7h7M12 7v5l3 2"/>',
      skills: '<path d="M12 5v16M3 3h5a4 4 0 0 1 4 2 4 4 0 0 1 4-2h5v16h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3Z"/>',
      memory: '<rect x="5" y="3" width="15" height="18" rx="2"/><path d="M3 7h4M3 12h4M3 17h4M11 7h5M11 11h5"/>',
      navigation: '<circle cx="12" cy="12" r="9"/><path d="m16 8-3 5-5 3 3-5Z"/>',
      runs: '<path d="m7 3 14 9-14 9Z"/>',
      activity: '<path d="M2 12h5l3-9 4 18 3-9h5"/>',
      metrics: '<path d="M4 20V10M12 20V4M20 20v-7"/>',
      timeline: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18M7 15h2M15 15h2"/>',
      companion: '<path d="M9 3h6M10 3v7L4 20q0 1 2 1h12q2 0 2-1l-6-10V3M8 15h8"/>',
      settings: '<path d="M4 6h7M15 6h5M4 12h2M10 12h10M4 18h10M18 18h2"/><circle cx="13" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="18" r="2"/>',
      system: '<rect x="3" y="3" width="18" height="13" rx="2"/><path d="M12 16v5M8 21h8"/>'
    };
    document.querySelectorAll("#primary-nav a[data-view]").forEach(link => {
      const shape = paths[link.dataset.view];
      if (!shape) return;
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("width", "16");
      icon.setAttribute("height", "16");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "1.6");
      icon.setAttribute("stroke-linecap", "round");
      icon.setAttribute("stroke-linejoin", "round");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = shape;
      link.prepend(icon);
    });
    const select = document.getElementById("appearance-select");
    if (!select) return;
    select.value = preference;
    select.addEventListener("change", () => {
      apply(select.value);
      try { localStorage.setItem(key, preference); } catch (_) { /* Keep this tab usable. */ }
    });
  });
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();
