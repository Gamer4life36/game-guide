"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  game: null,          // {name, appid}
  sources: [],         // from /api/wikis
  source: null,        // active source key
  page: null,          // {source, title, url}
  history: [],         // page history for Back
  missions: null,      // {source, page, groups}
  chats: {},           // per game: [{role, content, sources}]
  busy: false,
  model: "",
};

const PREFERRED_MODELS = ["mistral-small3.2:24b", "dolphin3:8b", "qwen2.5:3b-instruct"];
const proxied = (u) => "/api/img?u=" + encodeURIComponent(u);
const art = (appid) => appid ? [
  `/api/art?appid=${appid}`,
  proxied(`https://cdn.cloudflare.steamstatic.com/steam/apps/${appid}/header.jpg`),
] : [];

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function imgWithFallback(urls, cls, alt) {
  const img = document.createElement("img");
  img.className = cls; img.alt = alt || ""; img.loading = "lazy";
  let i = 0;
  const next = () => {
    if (i < urls.length) { img.src = urls[i++]; }
    else { const d = document.createElement("div"); d.className = "noart"; d.textContent = "◈"; img.replaceWith(d); }
  };
  img.onerror = next; next();
  return img;
}

/* ------------------------------------------------------------ library */
function gameCard(g) {
  const c = document.createElement("div");
  c.className = "gamecard"; c.title = g.name;
  c.append(imgWithFallback(art(g.appid), "art", g.name));
  const n = document.createElement("div"); n.className = "name"; n.textContent = g.name;
  c.append(n);
  c.onclick = () => openGame(g);
  return c;
}

async function loadLibrary() {
  try {
    const data = await api("/api/games");
    const inst = $("#installedGrid"); inst.innerHTML = "";
    data.installed.forEach((g) => inst.append(gameCard(g)));
    if (!data.installed.length) inst.innerHTML = '<div class="muted">No Steam games found. Use the search box above to find any PC game.</div>';
    const rec = data.recent.filter((r) => r.name);
    $("#recentTitle").hidden = !rec.length;
    const rg = $("#recentGrid"); rg.innerHTML = "";
    rec.slice(0, 8).forEach((g) => rg.append(gameCard(g)));
    state.model = data.model || "";
  } catch (e) {
    $("#installedGrid").innerHTML = `<div class="errmsg">Couldn't read the Steam library: ${esc(e.message)}</div>`;
  }
  loadModels();
}

async function loadModels() {
  const sel = $("#modelSelect");
  let names = [];
  try { names = await api("/api/models"); } catch { }
  sel.innerHTML = names.length ? names.map((n) => `<option>${esc(n)}</option>`).join("") : "<option>Ollama not running</option>";
  const pick = names.includes(state.model) ? state.model : PREFERRED_MODELS.find((m) => names.includes(m)) || names[0];
  if (pick) { sel.value = pick; state.model = pick; }
}
$("#modelSelect").onchange = (e) => { state.model = e.target.value; post("/api/model", { model: state.model }).catch(() => { }); };

function showLibrary() {
  state.game = null;
  $("#libraryView").hidden = false; $("#gameView").hidden = true;
  $("#crumb").innerHTML = "";
  setChatEnabled(false);
  loadLibrary();
}
$("#homeBtn").onclick = showLibrary;

/* ------------------------------------------------------------ game search */
let searchTimer, searchIdx = -1;
$("#gameSearch").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#gameResults").hidden = true; return; }
  searchTimer = setTimeout(async () => {
    const box = $("#gameResults");
    try {
      const items = await api("/api/store_search?q=" + encodeURIComponent(q));
      box.innerHTML = "";
      searchIdx = -1;
      if (!items.length) box.innerHTML = '<div class="dd-item muted">No PC games found</div>';
      items.forEach((g) => {
        const row = document.createElement("div"); row.className = "dd-item";
        row.append(imgWithFallback(art(g.appid), "", ""));
        const s = document.createElement("span"); s.textContent = g.name; row.append(s);
        row.onmousedown = () => { box.hidden = true; e.target.value = ""; openGame(g); };
        box.append(row);
      });
      box.hidden = false;
    } catch (err) { box.innerHTML = `<div class="dd-item errmsg">${esc(err.message)}</div>`; box.hidden = false; }
  }, 280);
});
$("#gameSearch").addEventListener("keydown", (e) => {
  const rows = $$("#gameResults .dd-item");
  if (!rows.length) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    searchIdx = (searchIdx + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
    rows.forEach((r, i) => r.classList.toggle("active", i === searchIdx));
  } else if (e.key === "Enter") {
    (rows[Math.max(0, searchIdx)]).onmousedown?.();
  } else if (e.key === "Escape") { $("#gameResults").hidden = true; }
});
$("#gameSearch").addEventListener("blur", () => setTimeout(() => ($("#gameResults").hidden = true), 150));

/* ------------------------------------------------------------ game view */
async function openGame(g) {
  state.game = { name: g.name, appid: g.appid || "" };
  state.history = []; state.page = null;
  post("/api/recent", state.game).catch(() => { });
  $("#libraryView").hidden = true; $("#gameView").hidden = false;
  $("#crumb").innerHTML = `/ <b>${esc(g.name)}</b>`;
  const head = $("#gameHead");
  const url = art(g.appid)[0];
  head.style.backgroundImage = url ? `url("${url}")` : "none";
  head.innerHTML = `<div><h1>${esc(g.name)}</h1><div class="meta" id="gameMeta">Looking for wikis…</div></div>
    <div class="actions"><button class="btn" id="wikiFix">Wiki settings</button><button class="btn" id="openExt">Open in browser ↗</button></div>`;
  $("#wikiFix").onclick = openWikiModal;
  $("#openExt").onclick = () => state.page && openExternal(state.page.url);
  $("#sourceTabs").innerHTML = "";
  $("#toc").innerHTML = "";
  state.missions = null;
  $("#missions").innerHTML = `<div class="side-title">Missions</div><div class="side-loading"><div class="spinner"></div>Waiting for wikis…</div>`;
  $("#page").innerHTML = loadingHTML("Finding every wiki for this game…");
  setChatEnabled(true);
  renderChat();
  try {
    const data = await api(`/api/wikis?game=${encodeURIComponent(g.name)}&appid=${g.appid || ""}`);
    applySources(data);
    const first = state.sources.find((x) => x.kind !== "index") || state.sources[0];
    openPage(first.key, "");
    loadMissions(g);
  } catch (e) {
    $("#page").innerHTML = `<div class="empty errmsg">${esc(e.message)}</div>`;
  }
}

function applySources(data) {
  state.sources = data.sources;
  const wikis = data.wikis;
  $("#gameMeta").textContent = wikis.length
    ? `${wikis.length} game wiki${wikis.length > 1 ? "s" : ""} found · plus PCGamingWiki, Wikipedia${state.game.appid ? " and Steam" : ""}`
    : `No dedicated wiki found · using PCGamingWiki, Wikipedia${state.game.appid ? " and Steam" : ""}`;
  // Games without their own wiki: show Steam first, it's the most useful page.
  if (!wikis.length) state.sources.sort((a, b) => (b.kind === "steam") - (a.kind === "steam"));
  renderTabs();
  $("#chatSub").textContent = `${state.game.name} · ${state.sources.length} sources`;
  renderSuggestions();
}

function renderTabs() {
  const tabs = $("#sourceTabs"); tabs.innerHTML = "";
  state.sources.forEach((s) => {
    const t = document.createElement("button");
    t.className = `tab kind-${s.kind}` + (s.key === state.source ? " active" : "");
    const extra = s.articles ? ` <small>${s.articles.toLocaleString()} pages</small>` : "";
    t.innerHTML = `<span class="dot"></span>${esc(s.label || s.sitename)}${extra}`;
    t.title = s.lastEdit ? `Last edited ${s.lastEdit.slice(0, 10)}` : s.sitename;
    t.onclick = () => {
      // Same topic on another source when possible.
      const general = ["pcgw", "wikipedia", "steam", "steamguides", "web", "index"];
      const same = state.page && state.page.title && !general.includes(s.kind)
        && !general.includes(sourceOf(state.page.source)?.kind) ? state.page.title : "";
      openPage(s.key, same, { fallbackHome: true });
    };
    tabs.append(t);
  });
  const src = sourceOf(state.source);
  $("#wikiSearch").placeholder = src && src.kind !== "steam" ? `Search ${src.label || src.sitename}…` : "Search not available";
  if (src && src.kind === "steamguides") $("#wikiSearch").placeholder = "Search Steam guides…";
  if (src && src.kind === "web") $("#wikiSearch").placeholder = "Search guide websites…";
  if (src && src.kind === "index") $("#wikiSearch").placeholder = "Search guides on the web…";
  $("#wikiSearch").disabled = !src || src.kind === "steam";
}

const sourceOf = (key) => state.sources.find((s) => s.key === key);
const loadingHTML = (msg) => `<div class="loading"><div class="spinner"></div>${esc(msg)}</div>`;

async function openPage(sourceKey, title, opts = {}) {
  const game = state.game;
  if (!game) return;
  if (state.page && !opts.noHistory) state.history.push(state.page);
  state.source = sourceKey;
  renderTabs();
  const pageEl = $("#page");
  pageEl.innerHTML = loadingHTML(title ? `Loading “${title}”…` : sourceOf(sourceKey)?.kind === "index"
    ? "Collecting guides, maps and cheats for this game (first time takes about a minute)…" : "Loading…");
  $("#toc").innerHTML = "";
  const token = (openPage.token = (openPage.token || 0) + 1);  // only the latest request may render
  try {
    const p = await api(`/api/page?game=${encodeURIComponent(game.name)}&appid=${game.appid}&source=${encodeURIComponent(sourceKey)}&title=${encodeURIComponent(title || "")}`);
    if (state.game !== game || token !== openPage.token) return;
    renderPage(p, title);
  } catch (e) {
    if (token !== openPage.token) return;
    if (opts.fallbackHome && title) return openPage(sourceKey, "", { noHistory: true });
    if (state.game !== game) return;
    pageEl.innerHTML = `<div class="empty">${esc(e.message)}<br><br><button class="btn" onclick="history_back()">← Back</button></div>`;
  }
}

function renderPage(p, requested) {
  state.page = { source: p.source.key, title: p.title, open: requested || p.title, url: p.url };
  const src = sourceOf(p.source.key) || p.source;
  const edited = p.edited ? `edited ${p.edited.slice(0, 10)}` : "";
  const back = state.history.length ? `<button class="iconbtn" title="Back" onclick="history_back()">←</button>` : "";
  const pageEl = $("#page");
  pageEl.innerHTML = `
    <div class="pagehead">${back}<h1>${esc(p.title)}</h1></div>
    <div class="pagemeta">
      <span class="pill kind-${src.kind}"><span class="dot" style="width:7px;height:7px;border-radius:50%;display:inline-block"></span>${esc(src.label || src.sitename)}</span>
      ${edited ? `<span>${esc(edited)}</span>` : ""}
      <button class="btn" id="askPage">✦ Ask about this page</button>
    </div>
    <div class="wiki">${p.html}</div>
    <div class="attribution">Content from <a data-ext="${esc(p.url)}">${esc(src.sitename)}: ${esc(p.title)}</a>${
      src.kind === "steam" ? " (official store listing)."
        : ["web", "steamguides"].includes(src.kind) ? ". Text and images belong to the original author."
        : ", licensed under the wiki's Creative Commons license (usually CC BY-SA)."}
      Images belong to their respective owners.</div>`;
  pageEl.scrollTop = 0;
  $("#askPage").onclick = () => { $("#input").value = `Summarise the key things I need to know from the "${p.title}" page.`; $("#input").focus(); autosize(); };
  // TOC from rendered headings
  const toc = $("#toc");
  const heads = $$(".wiki h2, .wiki h3", pageEl).filter((h) => h.textContent.trim() && !h.closest("aside, table, [class*=infobox]"));
  toc.innerHTML = heads.length ? '<div class="side-title">On this page</div>' : "";
  heads.forEach((h, i) => {
    h.id = "sec-" + i;
    const a = document.createElement("a");
    a.textContent = h.textContent.trim();
    if (h.tagName === "H3") a.style.paddingLeft = "22px";
    a.onclick = () => h.scrollIntoView({ behavior: "smooth", block: "start" });
    toc.append(a);
  });
  highlightMission();
  if (window.MissionRefs) MissionRefs.afterRender();
  if (state.pendingAnchor) { scrollToHeading(state.pendingAnchor); state.pendingAnchor = null; }
  // Broken images: hide quietly.
  $$(".wiki img", pageEl).forEach((img) => (img.onerror = () => (img.style.display = "none")));
}

/* ------------------------------------------------------------ missions sidebar */
async function loadMissions(g) {
  const box = $("#missions");
  box.innerHTML = `<div class="side-title">Missions</div><div class="side-loading"><div class="spinner"></div>Finding the mission list…</div>`;
  try {
    const m = await api(`/api/missions?game=${encodeURIComponent(g.name)}&appid=${g.appid || ""}`);
    if (state.game?.name !== g.name) return;
    state.missions = m;
    renderMissions();
    if (window.MissionRefs) MissionRefs.load(g);
  } catch (e) {
    if (state.game?.name === g.name) box.innerHTML = `<div class="side-title">Missions</div><div class="side-empty">Couldn't load missions: ${esc(e.message)}</div>`;
  }
}

function renderMissions() {
  const box = $("#missions");
  const m = state.missions;
  if (!m || !m.groups.length) {
    box.innerHTML = `<div class="side-title">Missions</div><div class="side-empty">No mission list found on this game's wikis. Try the Steam Guides tab for walkthroughs.</div>`;
    return;
  }
  const total = m.groups.reduce((n, g) => n + g.items.length, 0);
  const src = sourceOf(m.source);
  box.innerHTML = m.local
    ? `<div class="side-title">Missions <span class="count">${total}</span></div>
       <div class="side-note">No guide lists them yet, so these come from the game's own files.${m.hasSave ? " ● = started in your save." : ""} Click one to ask the AI.</div>`
    : `<div class="side-title">Missions <span class="count">${total}</span></div>
       <div class="side-note">In order, from <a data-open-list>${esc(m.pageLabel || m.page)}</a> · ${esc(m.source === "web" ? m.sitename : (src ? (src.label || src.sitename) : m.sitename))}${
         m.groups.some((g) => g.local) ? `. Side quests the guide skips are under “all quests in the game files”${m.hasSave ? " (● = started in your save)" : ""}.` : ""}
         Hover a mission and press ✎ to rename it.</div>`;
  box.insertAdjacentHTML("beforeend", `<div class="msearch"><input type="search" id="missionSearch" placeholder="Search missions…" autocomplete="off"></div>
    <div class="side-empty" id="missionNone" hidden>No missions match.</div>`);
  $("#missionSearch", box).oninput = (e) => filterMissions(e.target.value);
  const listLink = $("[data-open-list]", box);
  if (listLink) listLink.onclick = () => openPage(m.source, m.page);
  m.groups.forEach((g, gi) => {
    const d = document.createElement("details");
    d.className = "mgroup";
    d.open = gi === 0 || m.groups.length <= 2;
    if (g.local) d.classList.add("localgroup");
    d.innerHTML = `<summary>${esc(g.name)}<small>${g.items.length}</small></summary>`;
    if (g.page) {
      const open = document.createElement("a");
      open.className = "mgroup-open";
      open.textContent = "Open walkthrough ↗";
      open.onclick = () => openPage(m.source, g.page);
      d.append(open);
    }
    const ol = document.createElement("ol");
    ol.className = "mlist";
    g.items.forEach((it, n) => {
      const li = document.createElement("li");
      const num = n + 1;
      const a = document.createElement("a");
      a.dataset.n = num;
      const original = it.label || it.title || it.anchor;
      const key = it.id || (it.anchor ? `${it.page || m.page}#${it.anchor}` : it.title || original);
      const custom = (m.labels || {})[key];
      const nameEl = document.createElement("span");
      nameEl.className = "mname";
      nameEl.textContent = custom || original;
      a.append(nameEl);
      if (custom) {
        const orig = document.createElement("small");
        orig.className = "morig";
        orig.textContent = original;
        a.append(orig);
      }
      a.title = (custom ? `${custom}
(${original})` : original) + (it.started ? `
Started ${it.started}` : "");
      if (it.title) a.dataset.title = it.title;
      if (it.anchor) { a.dataset.anchor = it.anchor; a.dataset.page = it.page || m.page; }
      if (it.seen) li.classList.add("seen");
      li._text = it.text || "";
      const edit = document.createElement("button");
      edit.className = "mrename";
      edit.title = "Rename (e.g. to the name the game shows)";
      edit.textContent = "✎";
      edit.onclick = (e) => { e.stopPropagation(); renameMission(key, custom || "", original); };
      li.append(edit);
      a.onclick = () => {
        if (m.local || g.local) return askAbout(`${custom || it.label} (${g.name})`);
        const target = it.page || m.page;
        if (window.MissionRefs) MissionRefs.select(it, g);
        if (it.title) openPage(m.source, it.title);
        else if (state.page && state.page.open === target) { window.MissionRefs?.afterRender(); scrollToHeading(it.anchor); }
        else { state.pendingAnchor = it.anchor; openPage(m.source, target); }
        activeAnchor = it.anchor ? target + "#" + it.anchor : null;
        highlightMission();
      };
      li.append(a); ol.append(li);
    });
    d.append(ol);
    box.append(d);
  });
  highlightMission();
  window.MissionRefs?.decorate();
}

let activeAnchor = null;

async function renameMission(key, current, original) {
  const label = prompt(`Name for “${original}”
(leave empty to restore the original)`, current || "");
  if (label === null) return;
  const game = state.game;
  try {
    await post("/api/mission_label", { game: game.name, key, label });
  } catch (e) { alert("Couldn't save the name: " + e.message); return; }
  if (state.game !== game || !state.missions) return;
  state.missions.labels = state.missions.labels || {};
  if (label.trim()) state.missions.labels[key] = label.trim(); else delete state.missions.labels[key];
  const q = $("#missionSearch")?.value || "";
  renderMissions();
  if (q) { $("#missionSearch").value = q; filterMissions(q); }
}

const SEARCH_STOP = new Set("a an the to of in on at and or for with from by is it my i how where do".split(" "));

function filterMissions(q) {
  const raw = q.trim();
  const words = raw.toLowerCase().split(/[^a-z0-9']+/).filter((w) => w && !SEARCH_STOP.has(w));
  const groups = $$("#missions .mgroup");
  const note = $("#missionNone");
  // Score every step: a word in the step or act name counts 3, a word only in the walkthrough text counts 1.
  let best = 0;
  const scored = [];
  groups.forEach((d) => {
    if (d.dataset.wasOpen === undefined) d.dataset.wasOpen = d.open ? "1" : "";
    const groupName = d.querySelector("summary").childNodes[0].textContent.toLowerCase();
    $$(".mlist li", d).forEach((li) => {
      const name = groupName + " " + li.textContent.toLowerCase();
      const body = li._text || "";
      let score = 0, all = true;
      for (const w of words) {
        if (name.includes(w)) score += 3;
        else if (body.includes(w)) score += 1;
        else all = false;
      }
      scored.push({ d, li, score, all });
      if (score > best) best = score;
    });
  });
  const exact = scored.some((x) => x.all);
  const cutoff = exact ? 0 : Math.max(1, Math.ceil(best / 2));
  const shown = new Map();
  for (const x of scored) {
    const hit = !words.length || (exact ? x.all : x.score >= cutoff);
    x.li.hidden = !hit;
    x.li.classList.toggle("textmatch", !!words.length && hit && x.score < words.length * 3);
    if (hit) shown.set(x.d, (shown.get(x.d) || 0) + 1);
  }
  groups.forEach((d) => {
    const n = shown.get(d) || 0;
    const total = $$(".mlist li", d).length;
    d.hidden = !n;
    d.querySelector("summary small").textContent = words.length ? `${n}/${total}` : total;
    d.open = words.length ? n > 0 : !!d.dataset.wasOpen;
    if (!words.length) delete d.dataset.wasOpen;
  });
  if (!words.length) { note.hidden = true; return; }
  const any = shown.size > 0;
  note.hidden = exact;
  note.innerHTML = (any ? "No step is named that. Closest matches from the walkthrough text:"
    : "No steps match.") + ` <a class="askai">Ask the AI about “${esc(raw)}” →</a>`;
  $(".askai", note).onclick = () => askAbout(raw, true);
}

function askAbout(what, send = false) {
  const input = $("#input");
  if (input.disabled || state.busy) return;
  input.value = `How do I complete the quest "${what}"?`;
  input.focus();
  input.dispatchEvent(new Event("input"));
  if (send) $("#composer").requestSubmit();
}

function highlightMission() {
  const m = state.missions;
  $$("#missions .mlist a").forEach((a) => {
    const byTitle = !!(m && state.page && state.page.source === m.source && a.dataset.title
      && a.dataset.title.toLowerCase() === (state.page.open || state.page.title).toLowerCase());
    const byAnchor = !!(activeAnchor && state.page && a.dataset.anchor && a.dataset.page === state.page.open
      && activeAnchor === state.page.open + "#" + a.dataset.anchor);
    const on = byTitle || byAnchor;
    a.classList.toggle("active", on);
    if (on) {
      const det = a.closest("details");
      if (det && !det.open) det.open = true;
      a.scrollIntoView({ block: "nearest" });
    }
  });
}

function scrollToHeading(text) {
  let h = $$(".wiki h2, .wiki h3", $("#page")).find((x) => x.textContent.trim() === text.trim());
  if (!h) return;
  if (h.previousElementSibling?.classList.contains("mrefs")) h = h.previousElementSibling;
  // Images above the heading load lazily and push it down, so re-align a few times while they settle.
  const token = (scrollToHeading.token = (scrollToHeading.token || 0) + 1);
  const align = () => { if (token === scrollToHeading.token) h.scrollIntoView({ block: "start", behavior: "instant" }); };
  align();
  [150, 500, 1200, 2500].forEach((ms) => setTimeout(align, ms));
  const stop = () => { scrollToHeading.token++; };
  ["wheel", "touchstart", "keydown", "mousedown"].forEach((ev) =>
    $("#page").addEventListener(ev, stop, { once: true, passive: true }));
}

window.history_back = () => {
  const prev = state.history.pop();
  if (prev) openPage(prev.source, prev.title, { noHistory: true });
};

$("#page").addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (!a) return;
  e.preventDefault();
  if (a.dataset.t) openPage(state.source, a.dataset.t);
  else if (a.dataset.ext) openExternal(a.dataset.ext);
});

function openExternal(url) { api("/api/open?u=" + encodeURIComponent(url)).catch(() => { }); }

$("#wikiSearchForm").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#wikiSearch").value.trim();
  if (!q || !state.game) return;
  const src = sourceOf(state.source);
  const pageEl = $("#page");
  pageEl.innerHTML = loadingHTML(`Searching ${src.label || src.sitename}…`);
  $("#toc").innerHTML = "";
  try {
    const hits = await api(`/api/search?game=${encodeURIComponent(state.game.name)}&appid=${state.game.appid}&source=${encodeURIComponent(state.source)}&q=${encodeURIComponent(q)}`);
    pageEl.innerHTML = `<div class="pagehead"><h1>Results for “${esc(q)}”</h1></div>
      <div class="pagemeta"><span class="pill">${esc(src.label || src.sitename)}</span><span>${hits.length} pages</span></div>
      <div class="results">${hits.map((h) => `<div class="hit" data-title="${esc(h.title)}"><b>${esc(h.name || h.title)}</b><div>${esc(h.snippet)}…</div></div>`).join("") || '<div class="empty">Nothing found. Try another source tab or different words.</div>'}</div>`;
    $$(".hit", pageEl).forEach((h) => (h.onclick = () => openPage(state.source, h.dataset.title)));
  } catch (err) { pageEl.innerHTML = `<div class="empty errmsg">${esc(err.message)}</div>`; }
};

/* ------------------------------------------------------------ wiki settings modal */
function openWikiModal() {
  $("#wikiErr").textContent = ""; $("#wikiUrl").value = "";
  $("#wikiModal").hidden = false; $("#wikiUrl").focus();
}
$("#wikiCancel").onclick = () => ($("#wikiModal").hidden = true);
$("#wikiForm").onsubmit = async (e) => {
  e.preventDefault();
  $("#wikiErr").textContent = "Checking…";
  try {
    const data = await post("/api/wiki_override", { game: state.game.name, appid: state.game.appid, url: $("#wikiUrl").value.trim() });
    $("#wikiModal").hidden = true;
    applySources(data); openPage(state.sources[0].key, "", { noHistory: true });
  } catch (err) { $("#wikiErr").textContent = err.message; }
};
$("#wikiRefresh").onclick = async () => {
  $("#wikiErr").textContent = "Searching again…";
  try {
    const data = await post("/api/refresh_wikis", { game: state.game.name, appid: state.game.appid });
    $("#wikiModal").hidden = true;
    applySources(data); openPage(state.sources[0].key, "", { noHistory: true });
  } catch (err) { $("#wikiErr").textContent = err.message; }
};

/* ------------------------------------------------------------ chat */
function setChatEnabled(on) {
  $("#input").disabled = !on; $("#sendBtn").disabled = !on;
  $("#input").placeholder = on ? `Ask about ${state.game.name}…` : "Pick a game first…";
  if (!on) { $("#chatSub").textContent = "Open a game to start"; $("#suggestions").innerHTML = ""; renderChat(); }
}
const chatLog = () => (state.game ? (state.chats[state.game.name] ||= []) : []);

function renderSuggestions() {
  const box = $("#suggestions");
  if (chatLog().length) { box.innerHTML = ""; return; }
  const noWiki = !state.sources.some((s) => ["wikigg", "fandom", "wiki"].includes(s.kind));
  const ideas = noWiki
    ? ["What is this game about?", "What are the PC requirements?", "Any known fixes or best settings?"]
    : ["Tips for a new player", "Best early-game gear", "How do I make money fast?", "Fix crashes / best settings"];
  box.innerHTML = ideas.map((i) => `<button class="chip">${esc(i)}</button>`).join("");
  $$(".chip", box).forEach((c) => (c.onclick = () => send(c.textContent)));
}

function renderChat() {
  const box = $("#messages");
  box.innerHTML = "";
  const log = chatLog();
  if (!log.length) {
    box.innerHTML = `<div class="welcome"><div class="bigicon">✦</div><h3>${state.game ? `Ask anything about ${esc(state.game.name)}` : "Ask anything about your game"}</h3>
      <p>Where to find items, how to beat a boss, build tips, crash fixes. Answers come from the wikis on the left, compared against each other.</p></div>`;
  }
  log.forEach((m) => box.append(messageEl(m)));
  box.scrollTop = box.scrollHeight;
  renderSuggestions();
}

function messageEl(m) {
  const d = document.createElement("div");
  d.className = "msg " + (m.role === "user" ? "user" : "bot");
  if (m.role === "user") d.textContent = m.content;
  else fillBot(d, m);
  return d;
}

function fillBot(el, m) {
  let h = m.error ? `<div class="errmsg">${esc(m.error)}</div>` : markdown(m.content || "");
  if (m.status && !m.content) h = `<div class="status"><div class="spinner"></div>${esc(m.status)}</div>`;
  if (m.sources && m.sources.length) {
    h += `<div class="srclist">${m.sources.map((s) =>
      `<span class="src kind-${s.kind}" data-tag="${s.tag}" title="${esc(s.sitename)}: ${esc(s.title)}${s.edited ? " (edited " + s.edited + ")" : ""}"><span class="dot"></span><b>${s.tag}</b><span>${esc(s.sitename)}: ${esc(s.title)}</span></span>`).join("")}</div>`;
  } else if (m.sources && !m.streaming) {
    h += `<div class="srclist"><span class="muted" style="font-size:12px">No wiki pages matched: treat this answer with care.</span></div>`;
  }
  el.innerHTML = h;
  $$("[data-tag]", el).forEach((c) => (c.onclick = () => {
    const s = (m.sources || []).find((x) => x.tag === c.dataset.tag);
    if (s) openPage(s.source, s.kind === "steam" ? "" : (s.open || s.title));
  }));
}

function markdown(src) {
  const lines = esc(src).split("\n");
  let out = "", list = null, code = false;
  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[(S\d+)\]/g, '<span class="cite" data-tag="$1">$1</span>');
  const close = () => { if (list) { out += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (line.startsWith("```")) { close(); out += code ? "</pre>" : "<pre>"; code = !code; continue; }
    if (code) { out += line + "\n"; continue; }
    let m;
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== "ul") { close(); out += "<ul>"; list = "ul"; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); out += "<ol>"; list = "ol"; } out += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^#{1,4}\s+(.*)/))) { close(); out += `<h4>${inline(m[1])}</h4>`; }
    else if (!line.trim()) { close(); }
    else { close(); out += `<p>${inline(line)}</p>`; }
  }
  close();
  if (code) out += "</pre>";
  return out;
}

async function send(text) {
  text = (text ?? $("#input").value).trim();
  if (!text || state.busy || !state.game) return;
  state.busy = true; $("#sendBtn").disabled = true;  // before any await, so a double trigger can't send twice
  if (!state.model || state.model.startsWith("Ollama")) {
    try { await loadModels(); } catch { /* the chat call reports model problems */ }
  }
  const game = state.game;
  const log = chatLog();
  log.push({ role: "user", content: text });
  const bot = { role: "assistant", content: "", status: "Thinking…", streaming: true };
  log.push(bot);
  $("#input").value = ""; autosize();
  renderChat();
  const el = $("#messages").lastElementChild;
  state.busy = true; $("#sendBtn").disabled = true;
  const body = {
    game: game.name, appid: game.appid, model: state.model,
    current: state.page ? { source: state.page.source, title: state.page.open || state.page.title } : null,
    messages: log.filter((m) => m !== bot).map((m) => ({ role: m.role, content: m.content })),
  };
  try {
    const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.status) bot.status = ev.status;
        if (ev.sources) bot.sources = ev.sources;
        if (ev.t) bot.content += ev.t;
        if (ev.error) bot.error = ev.error;
        if (state.game === game) {
          fillBot(el, bot);
          const box = $("#messages");
          if (box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
        }
      }
    }
  } catch (e) {
    bot.error = "Chat failed: " + e.message;
  }
  bot.streaming = false; bot.status = "";
  if (!bot.content && !bot.error) bot.error = "No answer came back. Is the model loaded in Ollama?";
  if (state.game === game) fillBot(el, bot);
  state.busy = false; $("#sendBtn").disabled = false;
}

$("#composer").onsubmit = (e) => { e.preventDefault(); send(); };
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(160, t.scrollHeight) + "px"; }
$("#input").addEventListener("input", autosize);
$("#clearChat").onclick = () => { if (state.game && !state.busy) { state.chats[state.game.name] = []; renderChat(); } };

/* ------------------------------------------------------------ resizable split */
(() => {
  const div = $("#divider"), guide = $("#guide");
  const saved = localStorage.getItem("split");
  if (saved) guide.style.flexBasis = saved;
  div.addEventListener("mousedown", (e) => {
    e.preventDefault(); div.classList.add("dragging");
    const move = (ev) => {
      const pct = Math.min(78, Math.max(35, (ev.clientX / window.innerWidth) * 100));
      guide.style.flexBasis = pct + "%";
    };
    const up = () => {
      div.classList.remove("dragging");
      localStorage.setItem("split", guide.style.flexBasis);
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  });
})();

loadLibrary();
