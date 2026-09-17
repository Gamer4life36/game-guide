"use strict";
/* Mission references: screenshots, maps and related guides for the mission you're on.
   Uses app.js globals: state, $, $$, esc, api, openPage, openExternal. */
window.MissionRefs = (() => {
  let refs = null;          // /api/mission_refs result for the current game
  let refsGame = "";
  let selected = null;      // {item, group}
  const stepCache = new Map();

  async function load(game) {
    refs = null;
    refsGame = game.name;
    addHeaderButton();
    try {
      const r = await api(`/api/mission_refs?game=${encodeURIComponent(game.name)}&appid=${game.appid || ""}`);
      if (state.game?.name !== game.name) return;
      refs = r;
      addHeaderButton();
    } catch { /* references are optional */ }
  }

  function addHeaderButton() {
    const title = $("#missions .side-title");
    if (!title || $("#refsBtn")) return;
    const b = document.createElement("button");
    b.id = "refsBtn";
    b.className = "refsbtn";
    b.title = "Maps, screenshots and guides for this game";
    b.textContent = "🗺 Maps & refs";
    b.onclick = showOverview;
    title.append(b);
  }

  function select(item, group) {
    selected = item.anchor || item.title ? { item, group } : null;
  }

  /* ---------- per-step panel, placed right above the step's heading ---------- */
  function afterRender() {
    $$("#page .mrefs").forEach((el) => el.remove());
    if (!selected || !state.page) return;
    const { item, group } = selected;
    const target = item.title || item.page || state.missions?.page;
    if (state.page.open !== target) return;
    const heading = item.anchor
      ? $$(".wiki h2, .wiki h3", $("#page")).find((h) => h.textContent.trim() === item.anchor.trim())
      : null;
    const panel = buildPanel(item, group);
    if (heading) heading.before(panel);
    else $("#page .wiki")?.prepend(panel);
  }

  function regionOf(group) {
    return (group?.name || "").replace(/\s*·.*$/, "").replace(/\s*\(.*\)\s*/, " ").trim();
  }

  function buildPanel(item, group) {
    const step = item.anchor || item.label || "";
    const region = regionOf(group);
    const panel = document.createElement("section");
    panel.className = "mrefs";
    panel.innerHTML = `<div class="mrefs-head"><span>📌 Mission references</span><b>${esc(step)}</b>
      <small>${esc(group?.name || "")}</small></div>`;

    const shots = (item.images || []);
    if (shots.length) {
      const strip = document.createElement("div");
      strip.className = "mrefs-shots";
      shots.forEach((src, i) => {
        const img = document.createElement("img");
        img.src = src; img.loading = "lazy"; img.alt = `${step} screenshot ${i + 1}`;
        img.onerror = () => img.remove();
        img.onclick = () => lightbox(shots.map((s, n) => ({ src: s, caption: `${step} · screenshot ${n + 1} of ${shots.length}` })), i);
        strip.append(img);
      });
      panel.append(label("Screenshots from the walkthrough"), strip);
    }

    const links = document.createElement("div");
    links.className = "mrefs-links";
    panel.append(label("Guides & maps"), links);
    // Other walkthroughs with a section for this act (9Puz: "Arcadia Act 1: ...").
    for (const g of refs?.guides || []) {
      const sec = (g.sections || []).find((s) => region && s.toLowerCase().startsWith(region.toLowerCase() + ":"))
        || (g.sections || []).find((s) => region && s.toLowerCase().startsWith(region.toLowerCase()));
      if (sec) links.append(chip(`${g.site}: ${sec}`, () => { state.pendingAnchor = sec; openPage("web", g.open); }, "guide"));
    }
    // Guides whose title shares a distinctive word with this step.
    const words = new Set(((item.anchor || "") + " " + (item.label || "")).toLowerCase().match(/[a-z]{4,}/g) || []);
    const regionWords = new Set(region.toLowerCase().match(/[a-z]{4,}/g) || []);
    for (const g of refs?.guides || []) {
      if ((g.keywords || []).some((k) => words.has(k) && !regionWords.has(k))) {
        links.append(chip(`${g.site}: ${g.title}`, () => openPage("web", g.open), "guide"));
      }
    }
    // Wiki map screenshots whose name matches a place mentioned in this step.
    const text = ((item.text || "") + " " + step).toLowerCase();
    const maps = (refs?.wikiMaps || []).filter((m) => {
      const place = m.name.toLowerCase().replace(/\s*(chests?|map)\s*\d*|\d+/g, " ").trim().split(/\s+/)[0];
      return place && place.length >= 4 && text.includes(place);
    });
    if (maps.length) {
      links.append(chip(`🗺 ${maps.length} wiki map${maps.length > 1 ? "s" : ""} for this area`,
        () => lightbox(maps.map((m) => ({ src: m.src, caption: `${m.name} · ${m.wiki}` })), 0), "map"));
    }
    const game = state.game?.name || "";
    for (const m of refs?.maps || []) {
      const escRe = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const name = m.title.replace(new RegExp("[-|:]?\\s*(" + escRe(game) + "|" + escRe(m.site) + ")\\s*[-|:]?", "gi"), " ")
        .replace(/\s+/g, " ").trim();
      links.append(chip(`🗺 ${m.site}: ${name || "map"}`, () => openExternal(m.url), "map ext", m.title));
    }
    links.append(chip("▶ Video walkthroughs", () => openExternal(
      "https://www.youtube.com/results?search_query=" + encodeURIComponent(`${game} ${step} ${region}`.trim())), "video ext"));

    // Step-specific guides found by a web search (e.g. a dark glyph locations guide).
    if (item.anchor && game) {
      const key = `${game}|${step}|${region}`;
      const fill = (list) => {
        const shown = new Set($$(".mrefs-links .chip", panel).map((c) => c.dataset.url));
        list.filter((r) => !shown.has(r.url) && r.site !== state.missions?.sitename).forEach((r) => {
          const c = chip(`${r.site}: ${r.title.replace(/\s*[-|]\s*[^-|]+$/, "")}`, () => openPage("web", r.open), "guide found");
          c.dataset.url = r.url;
          links.prepend(c);
        });
      };
      if (stepCache.has(key)) fill(stepCache.get(key));
      else api(`/api/step_refs?game=${encodeURIComponent(game)}&step=${encodeURIComponent(step)}&region=${encodeURIComponent(region)}`)
        .then((r) => { stepCache.set(key, r); if (panel.isConnected) fill(r); }).catch(() => { });
    }
    return panel;
  }

  function label(text) {
    const d = document.createElement("div");
    d.className = "mrefs-label";
    d.textContent = text;
    return d;
  }

  function chip(text, onclick, kind = "", title = "") {
    const a = document.createElement("a");
    a.className = "chip " + kind;
    a.textContent = text;
    a.title = title || text;
    a.onclick = onclick;
    return a;
  }

  /* ---------- whole-game overview page ---------- */
  function showOverview() {
    const page = $("#page");
    if (!refs) {
      page.innerHTML = `<div class="empty"><div class="spinner"></div> Collecting maps and guides…</div>`;
      const wait = setInterval(() => { if (refs || state.game?.name !== refsGame) { clearInterval(wait); if (refs) showOverview(); } }, 500);
      return;
    }
    const game = state.game?.name || "";
    const shots = [];
    for (const g of state.missions?.groups || []) {
      for (const it of g.items) (it.images || []).forEach((src) => shots.push({ src, caption: `${g.name} · ${it.anchor || it.label}`, item: it, group: g }));
    }
    page.innerHTML = `
      <div class="pagehead"><h1>Maps &amp; references</h1></div>
      <div class="pagemeta"><span>${esc(game)} · everything found for the mission list</span></div>
      <div class="wiki refs-overview">
        <h2>Interactive maps</h2><div class="refs-cards" id="ovMaps"></div>
        <h2>Map screenshots <small>${refs.wikiMaps.length}</small></h2><div class="refs-grid" id="ovWikiMaps"></div>
        <h2>Guides <small>${refs.guides.length}</small></h2><div class="refs-cards" id="ovGuides"></div>
        <h2>Mission screenshots <small>${shots.length}</small></h2><div id="ovShots"></div>
        <h2>Videos</h2><div class="refs-cards" id="ovVideo"></div>
      </div>
      <div class="attribution">Maps, screenshots and guides belong to their sites and authors; they are shown here for personal reference.</div>`;
    const card = (title, sub, onclick) => {
      const a = document.createElement("a");
      a.className = "refcard";
      a.innerHTML = `<b>${esc(title)}</b><small>${esc(sub)}</small>`;
      a.onclick = onclick;
      return a;
    };
    refs.maps.forEach((m) => $("#ovMaps").append(card(m.title, `${m.site} · opens in your browser ↗`, () => openExternal(m.url))));
    if (!refs.maps.length) $("#ovMaps").innerHTML = `<p class="muted">No interactive maps found.</p>`;
    const wm = refs.wikiMaps.map((m) => ({ src: m.src, caption: `${m.name} · ${m.wiki}` }));
    refs.wikiMaps.forEach((m, i) => $("#ovWikiMaps").append(thumb(m.src, m.name, () => lightbox(wm, i))));
    if (!refs.wikiMaps.length) $("#ovWikiMaps").innerHTML = `<p class="muted">No map images on this game's wikis.</p>`;
    refs.guides.forEach((g) => $("#ovGuides").append(card(g.title, g.site + (g.sections?.length ? ` · ${g.sections.length} sections` : ""), () => openPage("web", g.open))));
    // Screenshots grouped by act, so they read like a picture walkthrough.
    const byGroup = new Map();
    shots.forEach((s, i) => { if (!byGroup.has(s.group)) byGroup.set(s.group, []); byGroup.get(s.group).push(i); });
    for (const [g, idx] of byGroup) {
      const h = document.createElement("h3");
      h.textContent = g.name;
      const grid = document.createElement("div");
      grid.className = "refs-grid";
      idx.forEach((i) => grid.append(thumb(shots[i].src, shots[i].caption.split(" · ")[1], () => lightbox(shots, i))));
      $("#ovShots").append(h, grid);
    }
    if (!shots.length) $("#ovShots").innerHTML = `<p class="muted">The mission list has no screenshots.</p>`;
    $("#ovVideo").append(card(`${game} walkthrough videos`, "YouTube search · opens in your browser ↗", () => openExternal(refs.video)));
    $("#toc").innerHTML = "";
    page.scrollTop = 0;
    selected = null;
    state.page = { source: "refs", title: "Maps & references", open: "refs", url: "" };
    highlightMission();
  }

  function thumb(src, caption, onclick) {
    const f = document.createElement("figure");
    f.className = "refthumb";
    const img = document.createElement("img");
    img.src = src; img.loading = "lazy"; img.alt = caption;
    img.onerror = () => f.remove();
    const c = document.createElement("figcaption");
    c.textContent = caption;
    f.append(img, c);
    f.onclick = onclick;
    return f;
  }

  /* ---------- lightbox ---------- */
  let lb = { list: [], i: 0 };
  function lightbox(list, i) {
    lb = { list, i };
    $("#lightbox").hidden = false;
    showLb();
  }
  function showLb() {
    const it = lb.list[lb.i];
    const box = $("#lightbox");
    $("img", box).src = it.src;
    $("figcaption", box).textContent = `${it.caption}  (${lb.i + 1}/${lb.list.length})`;
    $(".lb-prev", box).hidden = $(".lb-next", box).hidden = lb.list.length < 2;
  }
  const step = (d) => { lb.i = (lb.i + d + lb.list.length) % lb.list.length; showLb(); };
  const close = () => { $("#lightbox").hidden = true; };
  $("#lightbox .lb-close").onclick = close;
  $("#lightbox .lb-prev").onclick = (e) => { e.stopPropagation(); step(-1); };
  $("#lightbox .lb-next").onclick = (e) => { e.stopPropagation(); step(1); };
  $("#lightbox").onclick = (e) => { if (e.target.id === "lightbox") close(); };
  document.addEventListener("keydown", (e) => {
    if ($("#lightbox").hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
  });

  return { load, select, afterRender, showOverview, decorate: addHeaderButton };
})();
