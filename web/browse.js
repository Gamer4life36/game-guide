"use strict";
/* Browse every PC game (Steam + Epic), most popular first, with guide coverage.
   Uses app.js globals: $, $$, esc, api, openGame, art, imgWithFallback. */
(() => {
  const view = { offset: 0, total: 0, q: "", store: "", indie: false, loading: false, done: false };
  const PAGE = 60;

  async function loadPage(reset) {
    if (view.loading || (!reset && view.done)) return;
    if (reset) { view.offset = 0; view.done = false; $("#browseList").innerHTML = ""; }
    view.loading = true;
    $("#browseMore").hidden = false;
    try {
      const params = new URLSearchParams({ offset: view.offset, limit: PAGE, q: view.q, store: view.store, indie: view.indie ? "1" : "" });
      const r = await api("/api/catalog?" + params);
      view.total = r.total;
      $("#browseInfo").textContent = r.all
        ? `${r.total.toLocaleString()} of ${r.all.toLocaleString()} games · catalog built ${r.built}`
        : "The catalog is still being built (Steam and Epic lists download in the background).";
      r.games.forEach((g) => $("#browseList").append(row(g)));
      view.offset += r.games.length;
      view.done = view.offset >= r.total || !r.games.length;
    } catch (e) {
      $("#browseInfo").textContent = "Couldn't load the catalog: " + e.message;
    }
    view.loading = false;
    $("#browseMore").hidden = view.done;
  }

  function row(g) {
    const el = document.createElement("div");
    el.className = "brow";
    const stores = [g.steam ? '<span class="store steam">Steam</span>' : "", g.epic ? '<span class="store epic">Epic</span>' : "",
      g.indie ? '<span class="store indie">Indie</span>' : ""].join("");
    const cov = g.coverage == null ? '<span class="covbadge pending" title="Guide index not built yet">—</span>'
      : `<span class="covbadge c${Math.floor(g.coverage / 25)}" title="Guide coverage">${g.coverage}%</span>`;
    const stats = [g.ccu ? `${g.ccu.toLocaleString()} playing` : "", g.owners ? `~${short(g.owners)} owners` : ""].filter(Boolean).join(" · ");
    el.innerHTML = `<span class="brank">#${g.rank}</span><span class="bart"></span>
      <span class="bname"><b>${esc(g.name)}</b><small>${stats}</small></span><span class="bstores">${stores}</span>${cov}`;
    $(".bart", el).append(imgWithFallback(g.steam ? art(g.steam) : [], "", ""));
    el.onclick = () => openGame({ name: g.name, appid: g.steam ? String(g.steam) : "" });
    return el;
  }

  const short = (n) => n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "K" : String(n);

  async function status() {
    try {
      const s = await api("/api/prebuild_status");
      const box = $("#prebuildStatus");
      const cloud = s.cloud?.published ? ` · cloud data from ${s.cloud.published} UTC` : "";
      if (!s.state) { box.textContent = `${s.guides ?? 0} games indexed${cloud}`; return; }
      const what = s.state === "building" ? `building #${s.rank ?? "?"} ${s.current}`
        : s.state === "waiting" ? `paused: ${s.reason}` : "up to date";
      box.textContent = `Local builder: ${what} · ${s.guides ?? 0} games indexed${cloud}`;
    } catch { /* optional */ }
  }

  let timer;
  $("#browseSearch").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { view.q = e.target.value.trim(); loadPage(true); }, 250);
  });
  $$("#browseStores button").forEach((b) => (b.onclick = () => {
    $$("#browseStores button").forEach((x) => x.classList.toggle("active", x === b));
    view.store = b.dataset.store;
    loadPage(true);
  }));
  $("#browseIndie").onchange = (e) => { view.indie = e.target.checked; loadPage(true); };
  $("#browseMore").onclick = () => loadPage(false);
  new IntersectionObserver((entries) => {
    if (entries.some((x) => x.isIntersecting) && !$("#libraryView").hidden) loadPage(false);
  }, { rootMargin: "600px" }).observe($("#browseMore"));

  loadPage(true);
  status();
  setInterval(() => { if (!$("#libraryView").hidden) status(); }, 15000);

  // Marker search on MapGenie marker pages (content is rendered by the server).
  document.addEventListener("input", (e) => {
    if (!e.target.classList?.contains("markersearch")) return;
    const words = e.target.value.toLowerCase().split(/\s+/).filter(Boolean);
    $$("#page details.markercat").forEach((d) => {
      let shown = 0;
      $$("li.marker", d).forEach((li) => {
        const hit = words.every((w) => li.textContent.toLowerCase().includes(w));
        li.hidden = !hit;
        if (hit) shown++;
      });
      d.hidden = words.length > 0 && !shown;
      if (words.length) d.open = shown > 0 && shown <= 40;
    });
  });
})();
