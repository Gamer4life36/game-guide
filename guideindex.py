"""Per-game guide index: everything we can find for a game, sorted into sections.

Sections: walkthrough, side quests, secrets & collectibles, maps (with MapGenie markers), cheats.
Built from the game's wikis plus a web search per section, and cached in data/guides/<key>.json
so the background builder (prebuild.py) can work down the catalog ahead of time.

Cheats are limited to official cheat codes and console commands for single-player play; hacks,
trainers and anything for online games are left out (bans, malware, and unfair to other players).
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

import webguides

srv = None
GUIDES = None
INDEX_TTL = 14 * 86400

SECTIONS = [
    ("walkthrough", "Walkthrough", ["walkthrough", "main story guide"],
     r"walkthrough|story|chapter|mission|act\b|guide hub|100%|complete guide"),
    ("sidequests", "Side quests", ["side quests guide", "all side quests"],
     r"side ?quest|side ?mission|optional|bounties|contracts|errands|tasks|jobs|activities"),
    ("secrets", "Secrets & collectibles", ["secrets collectibles locations", "all collectibles"],
     r"secret|collectible|hidden|easter egg|locations|all .* locations|trophy|achievement"),
    ("maps", "Maps", ["interactive map"], r"\bmaps?\b"),
    ("cheats", "Cheats & console commands", ["cheats console commands", "cheat codes"],
     r"cheat|console command|commands|codes|debug"),
]
CHEAT_BLOCK = re.compile(r"hack|aimbot|wallhack|\besp\b|trainer|injector|undetected|mod ?menu|unlock ?all|"
                         r"spoofer|bypass|exploit|glitch money|free (?:v-?bucks|coins|gems|robux)|generator", re.I)
CHEAT_HOSTS = re.compile(r"wemod|flingtrainer|cheathappens|mrantifun|unknowncheats|mpgh|iwantcheats|"
                         r"cheatengine|guidedhacking|elitepvpers|ownedcore|playerup|lavicheats|skycheats", re.I)
ONLINE_TAGS = {"Massively Multiplayer"}


def init(server_module):
    global srv, GUIDES
    srv = server_module
    GUIDES = os.path.join(srv.DATA, "guides")
    os.makedirs(GUIDES, exist_ok=True)


def key_for(game, appid=""):
    return (f"steam_{appid}" if appid else "g_" + webguides.compact(game))[:80]


def load(game, appid=""):
    path = os.path.join(GUIDES, key_for(game, appid) + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(idx):
    path = os.path.join(GUIDES, key_for(idx["game"], idx.get("appid", "")) + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
    os.replace(tmp, path)


def store_info(appid):
    """(genres, categories) from the Steam store, or empty sets."""
    if not appid:
        return set(), set()
    try:
        j = srv.cached_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english", srv.PAGE_TTL)
        d = j[str(appid)]["data"]
    except Exception:
        return set(), set()
    return {g["description"] for g in d.get("genres", [])}, {c["description"] for c in d.get("categories", [])}


def online_only(appid):
    """True for MMO / online-only games, where 'cheats' means cheating other players."""
    genres, cats = store_info(appid)
    single = "Single-player" in cats
    return bool(genres & ONLINE_TAGS) or ("Online PvP" in cats and not single) or (not single and "Multi-player" in cats)


def mapgenie_slug(game):
    name = re.sub(r"[®™©'’]", "", game.lower())
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-")


def mapgenie_maps(game):
    """MapGenie maps for the game: [{title, url, id}]. Tries mapgenie.io/<game-slug> first, then a web search."""
    slugs = [mapgenie_slug(game), mapgenie_slug(re.sub(r"\s*[:\-–].*$", "", game))]
    for h in webguides._safe_hits(game, "mapgenie") if not _game_page(slugs[0]) else []:
        m = re.match(r"https://mapgenie\.io/([^/?#]+)", h["url"])
        if m:
            slugs.append(m.group(1))
    for slug in dict.fromkeys(slugs):
        page = _game_page(slug)
        if not page:
            continue
        out = []
        for path in dict.fromkeys(re.findall(rf"https://mapgenie\.io/{re.escape(slug)}/maps/[a-z0-9-]+", page)):
            try:
                data = _map_data(srv.extras._get_html(path, INDEX_TTL)) or {}
            except Exception:
                continue
            mp = data.get("map") or {}
            if mp.get("id"):
                out.append({"title": mp.get("title") or path.rsplit("/", 1)[1], "id": mp["id"], "url": path})
        if out:
            return out
    return []


def _game_page(slug):
    if not slug:
        return ""
    try:
        page = srv.extras._get_html(f"https://mapgenie.io/{slug}", INDEX_TTL)
    except Exception:
        return ""
    return page if f"mapgenie.io/{slug}/maps/" in page else ""


def _map_data(page):
    i = page.find("window.mapData")
    if i < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(page[page.index("{", i):])
        return data
    except ValueError:
        return None


def map_markers(map_id, page_url=""):
    """Markers for one MapGenie map, grouped by category (fetched when the player opens it)."""
    d = srv.cached_json(f"https://mapgenie.io/api/v1/maps/{int(map_id)}/data", INDEX_TTL)
    regions = {r["id"]: r.get("title", "") for r in d.get("regions") or []}
    cats = {}
    meta = {}
    if page_url.startswith("https://mapgenie.io/"):
        try:
            meta = _map_data(srv.extras._get_html(page_url, INDEX_TTL)) or {}
        except Exception:
            meta = {}
    groups = {g["id"]: g.get("title", "") for g in meta.get("groups") or []}
    for c in (meta.get("categories") or {}).values():
        cats[c["id"]] = {"title": c.get("title", ""), "group": groups.get(c.get("group_id"), ""), "items": []}
    for loc in d.get("locations") or []:
        c = cats.setdefault(loc["category_id"], {"title": f"Category {loc['category_id']}", "group": "", "items": []})
        desc = re.sub(r"\*\*|__", "", loc.get("description") or "")
        desc = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", desc).strip()  # markdown links -> their text
        c["items"].append({"id": loc["id"], "title": loc.get("title") or c["title"], "desc": desc[:600],
                           "region": regions.get(loc.get("region_id"), ""),
                           "lat": loc.get("latitude"), "lng": loc.get("longitude"),
                           "media": [m.get("url") for m in loc.get("media") or [] if m.get("url")][:3]})
    groups = [c for c in cats.values() if c["items"]]
    groups.sort(key=lambda c: (c["group"], -len(c["items"])))
    return {"map": (d.get("map") or {}).get("title", ""), "count": sum(len(c["items"]) for c in groups), "categories": groups}


def build(game, appid="", force=False):
    if not force:
        old = load(game, appid)
        if old and time.time() - old.get("built", 0) < INDEX_TTL:
            return old
    wikis = srv.discover_wikis(game)
    idx = {"game": game, "appid": str(appid or ""), "built": time.time(),
           "wikis": [{"key": w["key"], "sitename": srv.source_label(w), "articles": w.get("articles", 0)} for w in wikis],
           "sections": {}}
    try:
        _, cats = store_info(appid)
        story = not appid or not cats or "Single-player" in cats   # no web story lists for multiplayer-only games
        m = srv.missions_for(game, appid, web=story)
        idx["missions"] = {"count": sum(len(g["items"]) for g in m["groups"]), "source": m.get("sitename") or m.get("source", ""),
                           "local": bool(m.get("local"))}
    except Exception:
        idx["missions"] = {"count": 0}
    no_cheats = online_only(appid)
    for key, label, queries, pattern in SECTIONS:
        if key == "cheats" and no_cheats:
            idx["sections"][key] = {"label": label, "items": [], "note": "Online game: cheats are not listed."}
            continue
        seen, items = set(), []
        for q in queries:
            for h in webguides._safe_hits(game, q):
                if h["url"] in seen:
                    continue
                if key == "cheats" and (CHEAT_BLOCK.search(h["title"] + " " + h["snippet"]) or CHEAT_HOSTS.search(h["url"])):
                    continue
                if not re.search(pattern, h["title"] + " " + h["url"], re.I):
                    continue
                seen.add(h["url"])
                items.append({"title": h["title"], "site": h["site"], "url": h["url"], "snippet": h["snippet"][:220]})
        # Wiki pages for the section (e.g. "Side Quests", "Collectibles", "Console commands").
        for w in wikis[:2]:
            try:
                for r in srv.search_source(w, label.split(" &")[0], 3):
                    if re.search(pattern, r["title"], re.I):
                        items.append({"title": r["title"], "site": srv.source_label(w), "wiki": w["key"],
                                      "open": r["title"], "snippet": r.get("snippet", "")[:220]})
            except Exception:
                pass
        idx["sections"][key] = {"label": label, "items": items[:12]}
    try:
        idx["mapgenie"] = mapgenie_maps(game)
    except Exception:
        idx["mapgenie"] = []
    idx["score"] = coverage(idx)
    save(idx)
    return idx


def coverage(idx):
    """0-100: how much guide material exists for the game."""
    s = min(len(idx["wikis"]), 2) * 15
    s += 20 if idx.get("missions", {}).get("count") else 0
    for key, weight in (("walkthrough", 15), ("sidequests", 8), ("secrets", 8), ("maps", 4), ("cheats", 5)):
        if idx["sections"].get(key, {}).get("items"):
            s += weight
    s += 10 if idx.get("mapgenie") else 0
    return min(s, 100)


# ================================================================== pages for the "Guide index" tab

def index_source(game, appid=""):
    return {"key": "index", "sitename": "Guide index", "kind": "index", "base": "", "game": game, "appid": str(appid or ""),
            "article": ""}


def _e(s):
    return html.escape(str(s or ""), quote=True)


def render_index(idx):
    parts = []
    m = idx.get("missions") or {}
    wikis = ", ".join(w["sitename"] for w in idx["wikis"]) or "none found"
    parts.append(f'<p class="covline"><span class="covbadge c{idx["score"] // 25}">{idx["score"]}% coverage</span> '
                 f'Wikis: {_e(wikis)} · Missions: {m.get("count", 0)}'
                 f'{" from " + _e(m["source"]) if m.get("source") else ""}</p>')
    if idx.get("mapgenie"):
        cards = "".join(f'<a class="guidecard nothumb" data-t="map:{mp["id"]}|{_e(mp["url"])}" href="#"><span><b>🗺 {_e(mp["title"])}</b>'
                        f'<small class="stars">MapGenie · every marker, searchable</small></span></a>' for mp in idx["mapgenie"])
        parts.append(f'<h2>Map markers</h2><div class="guidelist">{cards}</div>')
    for key, sec in idx["sections"].items():
        items = sec.get("items") or []
        parts.append(f"<h2>{_e(sec['label'])}</h2>")
        if sec.get("note"):
            parts.append(f'<p class="muted">{_e(sec["note"])}</p>')
        if not items:
            if not sec.get("note"):
                parts.append('<p class="muted">Nothing found yet.</p>')
            continue
        cards = []
        for it in items:
            target = f'wiki:{it["wiki"]}:{it["open"]}' if it.get("wiki") else f'web:{it["url"]}'
            cards.append(f'<a class="guidecard nothumb" data-t="{_e(target)}" href="#"><span><b>{_e(it["title"])}</b>'
                         f'<small class="stars">{_e(it["site"])}</small><em>{_e(it.get("snippet", ""))}</em></span></a>')
        parts.append(f'<div class="guidelist">{"".join(cards)}</div>')
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(idx.get("built", 0)))
    parts.append(f'<p class="muted">Index built {built}. Cheats are limited to official single-player codes and console commands.</p>')
    return "".join(parts)


def render_markers(map_id, url):
    data = map_markers(map_id, url)
    parts = [f'<p class="covline">{data["count"]} markers · <a data-ext="{_e(url)}">open the interactive map on MapGenie</a></p>',
             '<div class="msearch"><input type="search" class="markersearch" placeholder="Search markers (name, area, description)…"></div>']
    for c in data["categories"]:
        rows = []
        for it in c["items"]:
            media = "".join(f'<img src="/api/img?u={urllib.parse.quote(u, safe="")}" loading="lazy" alt="">' for u in it["media"])
            link = f'{url}?locationIds={it["id"]}'
            rows.append(f'<li class="marker"><b>{_e(it["title"])}</b>'
                        f'{" <small>· " + _e(it["region"]) + "</small>" if it["region"] else ""}'
                        f' <a data-ext="{_e(link)}" class="mlink">show on map ↗</a>'
                        f'{"<p>" + _e(it["desc"]).replace(chr(10), "<br>") + "</p>" if it["desc"] else ""}'
                        f'{"<div class=markermedia>" + media + "</div>" if media else ""}</li>')
        group = f'{_e(c["group"])} · ' if c["group"] else ""
        parts.append(f'<details class="markercat"><summary>{group}<b>{_e(c["title"])}</b> <small>{len(c["items"])}</small></summary>'
                     f'<ul>{"".join(rows)}</ul></details>')
    parts.append('<p class="muted">Marker data from MapGenie, shown for personal reference.</p>')
    return data, "".join(parts)


def page(src, title):
    """fetch_page for the Guide index tab. Titles: '' (index), map:<id>|<url>, web:<url>, wiki:<key>:<title>."""
    game, appid = src["game"], src.get("appid", "")
    base = {"url": "", "edited": "", "sections": [], "source": {k: src.get(k) for k in ("key", "sitename", "kind", "base")}}
    if title.startswith("map:"):
        map_id, _, url = title[4:].partition("|")
        data, body = render_markers(map_id, url)
        return {**base, "title": f"{game}: {data['map'] or 'Map'} markers", "html": body, "url": url}
    if title.startswith("web:"):
        return webguides.fetch(title[4:])
    if title.startswith("wiki:"):
        key, _, wtitle = title[5:].partition(":")
        return srv.fetch_page(srv.source_by_key(game, key, appid), wtitle)
    idx = build(game, appid)
    return {**base, "title": f"{game}: guide index", "html": render_index(idx)}
