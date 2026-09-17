"""Web guides for Game Guide: walkthrough sites found with a keyless web search (DuckDuckGo Lite).

Covers games whose wikis are thin, e.g. new early-access releases that only have walkthroughs on
guide sites. Imported by server.py; uses its helpers through `srv` (set in init()).
"""
import concurrent.futures as cf
import html
import json
import os
import re
import threading
import time
import urllib.parse

import extras

srv = None

WEB_TTL = 3 * 24 * 3600
SITES = {  # known guide publishers -> display name (ranked first)
    "intoindiegames.com": "Into Indie Games", "techraptor.net": "TechRaptor", "game8.co": "Game8",
    "fextralife.com": "Fextralife", "gamerant.com": "Game Rant", "thegamer.com": "TheGamer", "ign.com": "IGN",
    "powerpyx.com": "PowerPyx", "polygon.com": "Polygon", "pcgamer.com": "PC Gamer", "gamenero.com": "Game Nero",
    "rockpapershotgun.com": "Rock Paper Shotgun", "pcgamesn.com": "PCGamesN", "dexerto.com": "Dexerto",
    "gamesradar.com": "GamesRadar+", "eurogamer.net": "Eurogamer", "screenrant.com": "Screen Rant",
    "destructoid.com": "Destructoid", "gamefaqs.gamespot.com": "GameFAQs", "primagames.com": "Prima Games",
    "shacknews.com": "Shacknews", "gamepressure.com": "Gamepressure", "gameskinny.com": "GameSkinny",
    "progameguides.com": "Pro Game Guides", "twinfinite.net": "Twinfinite", "sportskeeda.com": "Sportskeeda",
    "vg247.com": "VG247", "gosunoob.com": "GosuNoob", "neoseeker.com": "Neoseeker", "gamewith.net": "GameWith",
    "segmentnext.com": "SegmentNext", "gameplay.tips": "Gameplay.tips", "steamah.com": "SteamAH",
    "gamerempire.net": "Gamer Empire", "levelwinner.com": "Level Winner", "attackofthefanboy.com": "Attack of the Fanboy",
    "mapgenie.io": "MapGenie", "gamemappers.com": "GameMappers", "9puz.com": "9Puz", "apocanow.com": "Apocanow",
    "noobfeed.com": "NoobFeed", "thegamer.com": "TheGamer",
}
SKIP_HOSTS = re.compile(r"(aigameguides|ludo\.guide|antmag\.net|legendcreate|sports360news|fortunerapps|html-tester|"
                        r"gameguide\.ai|guidegame\.ai|gamerevolution\.ai|"
                        r"youtube|youtu\.be|reddit|article\.wn\.com|bing\.com|microsoft\.com/.*bing|steampowered|steamcommunity|fandom\.com|wiki\.gg|wikipedia|"
                        r"twitch|tiktok|facebook|twitter|x\.com|instagram|discord|amazon|g2a|kinguin|eneba|"
                        r"cdkeys|humblebundle|gog\.com|epicgames|metacritic|pcgamingwiki|nexusmods|duckduckgo)", re.I)
BOILER_HEADS = re.compile(r"^(table of contents|contents|frequently asked questions|faqs?|wrapping up|conclusion|"
                          r"final thoughts|related (posts|articles|guides)|share( by)?|comments?|leave a (reply|comment)|"
                          r"more .*guides?|you may also like|read (more|next)|wanna keep track.*|about the author|"
                          r"latest|trending|recommended|popular|sign up.*|newsletter.*|summary)\W*$", re.I)
CONTENT_CLASS = re.compile(r'<div[^>]+class="[^"]*\b(entry-content|post-content|article-content|article-body|'
                           r'articleBody|content-block-regular|single-content|td-post-content|article__content|'
                           r'wiki-content|post-body|elementor-widget-theme-post-content|c-entry-content|'
                           r'content-body|article-body-commercial-selector)\b[^"]*"', re.I)
END_MARKERS = re.compile(r"Related Posts|Wanna keep track|<footer|id=\"comments\"|class=\"comments|"
                         r"class=\"[^\"]*(related-posts|post-navigation|share-buttons)", re.I)
HUB_TITLE = re.compile(r"walkthrough|all (main )?(quests|missions)|quest (list|guide)|mission (list|guide)|"
                       r"story (quests|missions)|chapters?", re.I)


def init(server_module):
    global srv
    srv = server_module


def web_source(game):
    return {"key": "web", "sitename": "Web Guides", "kind": "web", "base": "", "game": game,
            "article": "https://duckduckgo.com/?q=" + urllib.parse.quote(game + " guide")}


def host_of(url):
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def site_name(url):
    h = host_of(url)
    for dom, name in SITES.items():
        if h == dom or h.endswith("." + dom):
            return name
    return h


def compact(s):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"[®™©]", "", s.lower()))


def about_game(game, *texts):
    """True if the game's name (compact form, or all its words) appears in any of the texts."""
    g = compact(re.sub(r"\s*[:\-–].*$", "", game)) or compact(game)
    words = [w for w in re.findall(r"[a-z0-9]+", game.lower()) if len(w) > 2 and w != "the"]
    for t in texts:
        c = compact(urllib.parse.unquote(t))
        if g and g in c:
            return True
        if words and all(w in c for w in words):
            return True
    return False


class SearchBlocked(Exception):
    pass


_search_lock = threading.Lock()
_engine_state = {"ddg": {"last": 0.0, "blocked_until": 0.0}, "bing": {"last": 0.0, "blocked_until": 0.0}}
MIN_GAP = {"ddg": 4.0, "bing": 4.0}


def _fetch_search(engine, url):
    """GET a search page politely: one request at a time, a gap between requests per engine."""
    import urllib.request
    with _search_lock:
        st = _engine_state[engine]
        wait = st["last"] + MIN_GAP[engine] - time.time()
        if wait > 0:
            time.sleep(wait)
        st["last"] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": extras.BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")


def _ddg(query):
    status, page = _fetch_search("ddg", "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query, "kl": "us-en"}))
    if status == 202 or "anomaly" in page.lower() or "result-link" not in page and "No results" not in page:
        raise SearchBlocked("ddg")
    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r"<td class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.S)
    out = []
    for i, (href, title) in enumerate(links):
        href = html.unescape(href)
        m = re.search(r"uddg=([^&]+)", href)
        real = urllib.parse.unquote(m.group(1)) if m else href
        if real.startswith("//"):
            real = "https:" + real
        out.append((real, srv.strip_tags(title), srv.strip_tags(snippets[i]) if i < len(snippets) else ""))
    return out


def _bing_url(href):
    """Bing wraps results in /ck/a?...&u=a1<base64url>; unwrap to the real address."""
    import base64
    href = html.unescape(href)
    m = re.search(r"[?&]u=a1([^&]+)", href)
    if not m:
        return href
    b = m.group(1)
    try:
        return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "replace")
    except Exception:
        return href


def _bing(query):
    status, page = _fetch_search("bing", "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "en", "cc": "US", "count": "20"}))
    blocks = re.findall(r'<li class="b_algo"(.*?)</li>', page, re.S)
    if not blocks and re.search(r"captcha|unusual traffic|verify you are", page, re.I):
        raise SearchBlocked("bing")
    out = []
    for b in blocks:
        a = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not a:
            continue
        snip = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
        out.append((_bing_url(a.group(1)), srv.strip_tags(a.group(2)), srv.strip_tags(snip.group(1)) if snip else ""))
    return out


def _search_cache_path(query):
    import hashlib
    return os.path.join(srv.CACHE, "search_" + hashlib.sha1(query.lower().encode()).hexdigest() + ".json")


def search(query, limit=10):
    """Web results [{url, title, snippet, site}] from DuckDuckGo, or Bing when DuckDuckGo is rate-limiting.
    Only real result pages are cached (a bot-check page is never stored as 'no results')."""
    path = _search_cache_path(query)
    raw = None
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < srv.SEARCH_TTL:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except ValueError:
            raw = None
    if raw is None:
        for engine, fn in (("ddg", _ddg), ("bing", _bing)):
            st = _engine_state[engine]
            if time.time() < st["blocked_until"]:
                continue
            try:
                raw = fn(query)
                break
            except SearchBlocked:
                st["blocked_until"] = time.time() + 15 * 60
            except Exception:
                continue
        if raw is None:
            if os.path.exists(path):  # stale results beat none
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            else:
                raise SearchBlocked("all search engines are rate-limiting; try again later")
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f)
    out = []
    for real, title, snippet in raw:
        if not real.startswith("http") or SKIP_HOSTS.search(host_of(real)):
            continue
        out.append({"url": real, "title": title, "site": site_name(real), "snippet": snippet})
    return out[:limit]


def search_status():
    now = time.time()
    return {e: max(0, int(st["blocked_until"] - now)) for e, st in _engine_state.items()}


GAME_SEGMENT = {  # sites that put the game in a fixed URL segment: /switch/258613-rune-factory-5/faqs/...
    "gamefaqs.gamespot.com": 1, "game8.co": 1, "neoseeker.com": 0, "fextralife.com": None,
}


def is_about(game, hit):
    """Title or address names the game, and on multi-game sites the game segment is this game.
    (A 'Cloudheim' dungeon page for Rune Factory 5 must not count.)"""
    u = urllib.parse.urlparse(hit["url"])
    host = host_of(hit["url"])
    for dom, idx in GAME_SEGMENT.items():
        if host == dom or host.endswith("." + dom):
            if idx is None:  # game is the subdomain: eldenring.wiki.fextralife.com
                return about_game(game, host.split(".")[0])
            segs = [s for s in u.path.split("/") if s]
            return len(segs) > idx and about_game(game, segs[idx])
    return about_game(game, hit["title"], u.path)


def game_hits(game, query, limit=8):
    hits = [h for h in search(f"{game} {query}".strip(), 20) if is_about(game, h)]
    known = [h for h in hits if h["site"] in SITES.values()]
    return (known + [h for h in hits if h not in known])[:limit]


def _content(page):
    """Main article HTML of a web page, best effort."""
    best = ""
    for m in CONTENT_CLASS.finditer(page):
        inner = extras._div_inner(page, m.start())
        if len(srv.strip_tags(inner)) > len(srv.strip_tags(best)):
            best = inner
    if len(srv.strip_tags(best)) > 400:
        return best
    for tag in ("article", "main"):
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", page, re.S | re.I)
        if m and len(srv.strip_tags(m.group(1))) > 400:
            return m.group(1)
    h1 = re.search(r"<h1\b", page, re.I)
    body = page[h1.start():] if h1 else page
    end = END_MARKERS.search(body)
    return body[: end.start()] if end else body


def fetch(url):
    page = extras._get_html(url, WEB_TTL)
    t = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S | re.I) or re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    title = srv.strip_tags(t.group(1)) if t else url
    edited = re.search(r'(?:article:modified_time|dateModified|article:published_time|datePublished)'
                       r'["\']?\s*(?:content=|:)\s*["\']([0-9]{4}-[0-9]{2}-[0-9]{2})', page)
    body = _content(page)
    body = re.sub(r"<h1\b.*?</h1>", "", body, count=1, flags=re.S | re.I)
    parts = urllib.parse.urlparse(url)
    src = {"key": "web", "sitename": site_name(url), "kind": "web", "base": f"{parts.scheme}://{parts.netloc}"}
    cleaned = srv.clean_html(body, src)
    # Same-site guide pages open inside the app; everything else in the browser.
    host = parts.netloc

    def relink(m):
        target = html.unescape(m.group(1))
        if urllib.parse.urlparse(target).netloc == host and target.rstrip("/") != url.rstrip("/"):
            return f'data-t="web:{html.escape(target, quote=True)}"'
        return m.group(0)
    cleaned = re.sub(r'data-ext="([^"]+)"', relink, cleaned)
    cleaned = re.sub(r"<h2[^>]*>\s*(Table of contents|Contents)\s*</h2>", "", cleaned, flags=re.I)
    heads = [srv.strip_tags(h) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", cleaned, re.S)]
    return {"title": title, "html": cleaned, "sections": [h for h in heads if not BOILER_HEADS.match(h)],
            "url": url, "edited": edited.group(1) if edited else "", "site": site_name(url),
            "source": {"key": "web", "sitename": site_name(url), "kind": "web", "base": src["base"]}}


def index_page(game):
    rows = []
    seen = set()
    for q in ("walkthrough", "guide", "tips"):
        try:
            hits = game_hits(game, q, 10)
        except Exception:
            continue
        for h in hits:
            if h["url"] in seen:
                continue
            seen.add(h["url"])
            rows.append(f'<a class="guidecard nothumb" data-t="web:{html.escape(h["url"], quote=True)}" href="#"><span>'
                        f'<b>{html.escape(h["title"])}</b><small class="stars">{html.escape(h["site"])}</small>'
                        f'<em>{html.escape(h["snippet"][:200])}</em></span></a>')
    body = "".join(rows) or "<p>No guide websites found for this game.</p>"
    return {"title": "Guides from around the web", "html": f'<div class="guidelist">{body}</div>', "sections": [],
            "url": web_source(game)["article"], "edited": "",
            "source": {"key": "web", "sitename": "Web Guides", "kind": "web", "base": ""}}


def page(game, title):
    if title.startswith("web:"):
        return fetch(title[4:])
    return index_page(game)


def search_results(game, q, limit=8):
    return [{"title": "web:" + h["url"], "name": h["title"], "snippet": f'{h["site"]} · {h["snippet"]}'}
            for h in game_hits(game, q, limit)]

# ================================================================== missions from walkthrough hubs


def _hub_parts(game, hub_url):
    """Ordered (label, url) links from a walkthrough hub to its per-part pages on the same site."""
    raw = extras._get_html(hub_url, WEB_TTL)
    body = _content(raw)
    host = host_of(hub_url)
    parts, seen, heading = [], set(), None
    for m in re.finditer(r"<h[2-4][^>]*>(.*?)</h[2-4]>|<a\b[^>]*href=\"([^\"#]+)\"[^>]*>(.*?)</a>", body, re.S | re.I):
        if m.group(1) is not None:
            heading = srv.strip_tags(m.group(1))
            continue
        url = urllib.parse.urljoin(hub_url, html.unescape(m.group(2)))
        if host_of(url) != host or url.rstrip("/") == hub_url.rstrip("/") or url in seen:
            continue
        if not about_game(game, urllib.parse.urlparse(url).path):
            continue
        text = srv.strip_tags(m.group(3))
        label = heading if heading and not BOILER_HEADS.match(heading) and (
            not text or re.fullmatch(r"read more|continue( reading)?|here|guide|walkthrough", text, re.I)) else text
        if not label or BOILER_HEADS.match(label):
            continue
        seen.add(url)
        label = re.sub(rf"^{re.escape(game)}\s*[:\-–—]\s*", "", label, flags=re.I)
        parts.append((label, url))
    return parts


def step_texts(cleaned):
    """{h2 heading: plain text of everything under it, h3 subsections included} for mission search."""
    parts = re.split(r"<h2[^>]*>(.*?)</h2>", cleaned, flags=re.S)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        text = srv.strip_tags(re.sub(r"<[^>]+>", " ", parts[i + 1]))
        out[srv.strip_tags(parts[i])] = re.sub(r"\s+", " ", text)[:4000].lower()
    return out


def _part_steps(url):
    """[(heading, searchable text)] for the story steps of one walkthrough part."""
    try:
        page = fetch(url)
    except Exception:
        return []
    texts = step_texts(page["html"])
    images = step_images(page["html"])
    return [(h, texts.get(h, ""), images.get(h, [])[:6]) for h in page["sections"] if not h.endswith("?")]


def missions(game):
    cands, seen = [], set()
    for q in ("walkthrough", "all missions quests"):
        try:
            for h in game_hits(game, q, 8):
                if h["url"] not in seen and HUB_TITLE.search(h["title"]):
                    seen.add(h["url"])
                    cands.append(h)
        except Exception:
            continue
    hubs = sorted(cands, key=lambda h: 0 if re.search(r"hub|all|complete|full", h["title"], re.I) else 1)
    for h in hubs[:4]:
        try:
            parts = _hub_parts(game, h["url"])
        except Exception:
            continue
        if len(parts) < 3:
            continue
        with cf.ThreadPoolExecutor(6) as ex:
            steps = list(ex.map(lambda p: _part_steps(p[1]), parts[:30]))
        groups = []
        for (label, url), st in zip(parts, steps):
            items = [{"anchor": h, "page": "web:" + url, "text": t, "images": im} for h, t, im in st] \
                or [{"title": "web:" + url, "label": label}]
            groups.append({"name": label, "page": "web:" + url, "items": items})
        return {"source": "web", "sitename": h["site"], "page": "web:" + h["url"], "pageLabel": h["title"],
                "groups": groups}
    # No hub: a single long walkthrough page still gives an ordered list of steps.
    for h in cands[:3]:
        try:
            p = fetch(h["url"])
        except Exception:
            continue
        if len(p["sections"]) >= 4:
            texts = step_texts(p["html"])
            images = step_images(p["html"])
            return {"source": "web", "sitename": h["site"], "page": "web:" + h["url"], "pageLabel": h["title"],
                    "groups": [{"name": p["title"], "page": "web:" + h["url"],
                                "items": [{"anchor": s, "page": "web:" + h["url"], "text": texts.get(s, ""),
                                           "images": images.get(s, [])[:6]}
                                          for s in p["sections"]]}]}
    return {"groups": []}

# ================================================================== references for missions (maps, images, guides)

GENERIC_WORDS = set("""the a an and of to in on at for with from how where what all your you guide guides walkthrough
walkthroughs locations location solution tips tricks beginner beginners map maps interactive complete full part act
chapter quest quests mission missions list best more other game games wiki chest chests start starter stop
gameplay classes controls world story hoarding kicking""".split())


def step_images(cleaned):
    """{h2 heading: [image src]} — screenshots under each walkthrough step (avatars and icons skipped)."""
    parts = re.split(r"<h2[^>]*>(.*?)</h2>", cleaned, flags=re.S)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        imgs = []
        for tag in re.findall(r"<img[^>]*>", parts[i + 1]):
            src = re.search(r'src="([^"]+)"', tag)
            width = re.search(r'width="(\d+)"', tag)
            if not src or (width and int(width.group(1)) < 200):
                continue
            real = urllib.parse.unquote(src.group(1))
            if re.search(r"gravatar|avatar|[?&]s=\d{2,3}(&|$)|logo|icon", real, re.I):
                continue
            imgs.append(html.unescape(src.group(1)))
        out[srv.strip_tags(parts[i])] = imgs
    return out


def _keywords(text, game):
    skip = GENERIC_WORDS | set(re.findall(r"[a-z0-9]+", game.lower()))
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in skip}


def _guide_sections(url):
    try:
        return fetch(url)["sections"]
    except Exception:
        return []


def _safe_hits(game, q):
    try:
        return game_hits(game, q, 10)
    except Exception:
        return []


def references(game, wikis, hub_site=""):
    """Maps, related guides (with keywords for matching steps) and wiki map images for a game."""
    seen, titles, guides, maps = set(), set(), [], []
    queries = ("interactive map", "map locations", "guide", "locations guide", "puzzle solution", "walkthrough",
               "quest", "boss guide", "dungeon guide", "puzzle", "locations", "all locations")
    with cf.ThreadPoolExecutor(5) as ex:
        results = list(ex.map(lambda q: _safe_hits(game, q), queries))
    for hits in results:
        for h in hits:
            site_words = set(re.findall(r"[a-z]+", (h["site"] + " " + host_of(h["url"])).lower()))
            title = re.sub(r"\s*[|\-–]\s*(%s|[\w.]+\.(com|net|org|io|co))\s*$" % re.escape(h["site"]), "", h["title"], flags=re.I)
            norm = compact(title)
            if h["url"] in seen or norm in titles:
                continue  # same article re-posted by aggregator sites
            seen.add(h["url"])
            titles.add(norm)
            entry = {"url": h["url"], "title": title, "site": h["site"], "open": "web:" + h["url"]}
            is_map = (re.search(r"mapgenie|gamemappers|/maps?(/|$)", h["url"], re.I)
                      or re.search(r"interactive map", h["title"], re.I))
            if is_map:
                entry["interactive"] = True
                maps.append(entry)
            else:
                entry["keywords"] = sorted(_keywords(title, game) - site_words)
                guides.append(entry)
    # Aggregators (article.wn.com etc.) sort after the known publishers.
    guides.sort(key=lambda g: 0 if g["site"] in SITES.values() else 1)
    # Other walkthroughs whose headings name the acts ("Arcadia Act 1: ...") can be linked per act.
    walkthroughs = [g for g in guides if re.search(r"walkthrough", g["title"], re.I) and g["site"] != hub_site
                    and not re.search(r"hub", g["title"], re.I)][:4]
    with cf.ThreadPoolExecutor(4) as ex:
        for g, secs in zip(walkthroughs, ex.map(lambda g: _guide_sections(g["url"]), walkthroughs)):
            g["sections"] = secs
    images = []
    for w in wikis[:2]:
        try:
            j = srv.mw(w, action="query", list="allimages", ailimit=500, aiprop="url|size")
        except Exception:
            continue
        for im in j.get("query", {}).get("allimages", []):
            if not re.search(r"map", im["name"], re.I) or im.get("width", 0) < 300:
                continue
            images.append({"name": re.sub(r"\.\w+$", "", im["name"]).replace("_", " "), "wiki": w["sitename"],
                           "src": "/api/img?u=" + urllib.parse.quote(im["url"], safe=""),
                           "page": w["article"] + urllib.parse.quote("File:" + im["name"])})
    return {"maps": maps, "guides": guides, "wikiMaps": images[:80],
            "video": "https://www.youtube.com/results?search_query=" + urllib.parse.quote(game + " walkthrough")}


def step_refs(game, step, region=""):
    """Guides specifically about one mission step, e.g. 'Destroying The Dark Magic Glyph' -> a dark glyph locations guide."""
    words = _keywords(step, game) - _keywords(region, game)
    if not words:
        return []
    out, seen = [], set()
    for q in (f"{step} {region}".strip(), step):
        for h in _safe_hits(game, q):
            title_words = _keywords(h["title"], game)
            if h["url"] in seen or not (words & title_words):
                continue
            seen.add(h["url"])
            out.append({"url": h["url"], "title": h["title"], "site": h["site"], "open": "web:" + h["url"],
                        "match": sorted(words & title_words)})
    return out[:6]
