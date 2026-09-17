"""Game Guide: a PC game guide with multi-source wiki pages on the left and a local AI chat on the right.

Stdlib only. Serves a small web UI on localhost and opens it in an Edge app window.
Sources per game: every matching wiki (wiki.gg, Fandom), PCGamingWiki and Wikipedia.
The chat pulls relevant pages from all of them, labels each with its source and
last-edit date, and asks the local model (Ollama) to cross-check them.
"""
import concurrent.futures as cf
import glob
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import catalog
import extras
import guideindex
import sync
import localquests
import webguides

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
DATA = os.path.join(HERE, "data")
CACHE = os.path.join(DATA, "cache")
IMG_CACHE = os.path.join(DATA, "img")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
EDGE_PROFILE = os.path.join(DATA, "edge-profile")
CHROME_PROFILE = os.path.join(DATA, "chrome-profile")
CHROME_PATHS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
PORT = 8765
OLLAMA = "http://127.0.0.1:11434"
UA = "GameGuide/0.1 (personal desktop app; contact: local user)"
PAGE_TTL = 3 * 24 * 3600
SEARCH_TTL = 24 * 3600
EDGE_PATHS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]

for d in (DATA, CACHE, IMG_CACHE):
    os.makedirs(d, exist_ok=True)

PCGW = {"key": "pcgw", "api": "https://www.pcgamingwiki.com/w/api.php", "base": "https://www.pcgamingwiki.com",
        "article": "https://www.pcgamingwiki.com/wiki/", "sitename": "PCGamingWiki", "kind": "pcgw"}
WIKIPEDIA = {"key": "wikipedia", "api": "https://en.wikipedia.org/w/api.php", "base": "https://en.wikipedia.org",
             "article": "https://en.wikipedia.org/wiki/", "sitename": "Wikipedia", "kind": "wikipedia"}

# ------------------------------------------------------------------ settings

_settings_lock = threading.Lock()


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def update_settings(**kw):
    with _settings_lock:
        s = load_settings()
        s.update(kw)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        return s

# ------------------------------------------------------------------ http helpers


def http_get(url, timeout=15, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return (data, r.headers.get("Content-Type", "")) if binary else data.decode("utf-8", "replace")


def cached_json(url, ttl, timeout=15):
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(CACHE, key + ".json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    try:
        data = json.loads(http_get(url, timeout))
    except Exception:
        if os.path.exists(path):  # stale is better than nothing when offline
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        raise
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def mw(source, ttl=PAGE_TTL, **params):
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    return cached_json(source["api"] + "?" + urllib.parse.urlencode(params), ttl)

# ------------------------------------------------------------------ steam


def steam_root():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        return os.path.normpath(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:  # no registry key, or not Windows (the cloud builder runs on Linux)
        return r"C:\Program Files (x86)\Steam"


SKIP_APPS = re.compile(r"steamworks|redistributable|steamvr|proton|steam linux runtime|dedicated server|"
                       r"soundtrack|sdk\b|obs studio|wallpaper engine", re.I)


def steam_libs():
    root = steam_root()
    libs = {os.path.normcase(os.path.join(root, "steamapps"))}
    try:
        vdf = open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8").read()
        for p in re.findall(r'"path"\s+"([^"]+)"', vdf):
            libs.add(os.path.normcase(os.path.join(p.replace("\\\\", "\\"), "steamapps")))
    except OSError:
        pass
    return libs


def installed_games():
    games = {}
    for lib in steam_libs():
        for f in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            try:
                t = open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]+)"', t)
            appid = re.search(r'"appid"\s+"(\d+)"', t)
            played = re.search(r'"LastPlayed"\s+"(\d+)"', t)
            if name and appid and not SKIP_APPS.search(name.group(1)):
                games[appid.group(1)] = {"appid": appid.group(1), "name": name.group(1),
                                         "lastPlayed": int(played.group(1)) if played else 0}
    out = sorted(games.values(), key=lambda g: -g["lastPlayed"])
    return out + [g for g in epic_installed() if g["name"].lower() not in {x["name"].lower() for x in out}]


def epic_installed():
    """Games installed through the Epic Games Launcher (engines, plugins and add-ons skipped)."""
    found = []
    for f in glob.glob(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item"):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        cats = d.get("AppCategories") or []
        if "games" not in cats or any(c.startswith(("addons", "plugins", "engines")) for c in cats):
            continue
        name = d.get("DisplayName", "").strip()
        if not name or any(g["name"] == name for g in found):
            continue
        appid = ""
        try:  # Steam art and store data when the game is on Steam too
            hit = next((h for h in store_search(name) if h["name"].lower() == name.lower()), None)
            appid = hit["appid"] if hit else ""
        except Exception:
            pass
        found.append({"appid": appid, "name": name, "lastPlayed": 0, "store": "epic"})
    return found


def image_candidates(url):
    """The image URL, then the original file behind a WordPress Photon CDN link (i0.wp.com often 404s)."""
    out = [url]
    m = re.match(r"https://i\d\.wp\.com/([^?]+)", url)
    if m:
        out.append("https://" + m.group(1))
    return out


def header_url(appid):
    """Steam header art. Newer games use hashed asset paths, so ask the store API when the classic path is missing."""
    appid = re.sub(r"\D", "", appid)
    classic = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
    if os.path.exists(os.path.join(IMG_CACHE, hashlib.sha1(classic.encode()).hexdigest())):
        return classic
    try:
        req = urllib.request.Request(classic, method="HEAD", headers={"User-Agent": UA})
        urllib.request.urlopen(req, timeout=8).close()
        return classic
    except Exception:
        pass
    try:
        j = cached_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic", PAGE_TTL)
        return j[appid]["data"]["header_image"]
    except Exception:
        return classic


def store_search(q):
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode(
        {"term": q, "l": "english", "cc": "US"})
    items = cached_json(url, SEARCH_TTL).get("items", [])
    return [{"appid": str(i["id"]), "name": i["name"]} for i in items if i.get("type") in (None, "app")][:12]

# ------------------------------------------------------------------ wiki discovery


SEQUEL = re.compile(r"\s+(\d+|ii|iii|iv|v|vi|vii|viii|ix|x|remastered|remake|definitive edition|"
                    r"enhanced edition|goty edition|game of the year edition)$", re.I)


def name_slugs(name):
    """Wiki subdomain guesses, most specific first: 'Dead Island 2' -> deadisland2, dead-island-2, deadisland..."""
    name = re.sub(r"[Â®â„¢Â©]", "", name).strip()
    base = re.split(r"\s*[:â€“â€”]\s*|\s+-\s+", name)[0]
    no_the = re.sub(r"^the\s+", "", base, flags=re.I)
    variants = [name, base, SEQUEL.sub("", base), no_the, SEQUEL.sub("", no_the)]
    out = []
    for v in variants:
        words = re.findall(r"[a-z0-9]+", v.lower().replace("'", ""))
        for slug in ("".join(words), "-".join(words)):
            if len(slug) >= 4 and slug not in out:
                out.append(slug)
    return out


def relevant(wiki, name):
    """The wiki's name or host should mention the game, so a guessed subdomain can't hijack it."""
    words = [w for w in re.findall(r"[a-z0-9]+", SEQUEL.sub("", name.lower())) if len(w) >= 3 and w != "the"]
    hay = (wiki["sitename"] + " " + wiki["key"]).lower().replace("-", "")
    return any(w in hay for w in words) if words else True


def probe_wiki(base, api_path):
    api = base + api_path
    try:
        j = json.loads(http_get(api + "?action=query&meta=siteinfo&siprop=general|statistics&format=json"
                                "&formatversion=2", timeout=7))
        g, st = j["query"]["general"], j["query"]["statistics"]
        article = g.get("server", base)
        if article.startswith("//"):
            article = "https:" + article
        article += g.get("articlepath", "/wiki/$1").replace("$1", "")
        rc = json.loads(http_get(api + "?action=query&list=recentchanges&rclimit=1&rcprop=timestamp"
                                 "&rctype=edit&format=json&formatversion=2", timeout=7))
        last = (rc["query"]["recentchanges"] or [{}])[0].get("timestamp", "")
        host = urllib.parse.urlparse(article).netloc or urllib.parse.urlparse(base).netloc  # canonical host
        kind = "wikigg" if host.endswith("wiki.gg") else "fandom" if host.endswith("fandom.com") else "wiki"
        return {"key": host, "api": api, "base": base, "article": article, "sitename": g["sitename"],
                "mainpage": g.get("mainpage", "Main Page"), "articles": st.get("articles", 0),
                "lastEdit": last, "kind": kind}
    except Exception:
        return None


def discover_wikis(name):
    """Find every wiki for a game. Cached per game name; can be overridden by the user."""
    cache = load_settings().get("wikis", {})
    key = name.lower()
    if key in cache and time.time() - cache[key].get("at", 0) < 7 * 24 * 3600:
        return cache[key]["wikis"]
    slugs = name_slugs(name)
    tasks = []
    for rank, s in enumerate(slugs):
        for base, path in ((f"https://{s}.wiki.gg", "/api.php"), (f"https://{s}.fandom.com", "/api.php"),
                           (f"https://{s}game.fandom.com", "/api.php"), (f"https://{s}.wiki.gg", "/w/api.php")):
            tasks.append((rank, base, path))
    found = {}
    with cf.ThreadPoolExecutor(12) as ex:
        for (rank, base, path), res in zip(tasks, ex.map(lambda t: probe_wiki(t[1], t[2]), tasks)):
            if res and res["articles"] >= 5 and res["key"] not in found and relevant(res, name):
                res["rank"] = rank
                found[res["key"]] = res
    wikis = list(found.values())
    # Full-name wikis first; a generic series wiki (short slug) only if nothing specific exists.
    top = min((w["rank"] for w in wikis), default=0)
    exact = [w for w in wikis if w["rank"] <= top + 1]  # compact + hyphenated forms of the best name
    wikis = exact if any(w["articles"] >= 20 for w in exact) else wikis
    wikis.sort(key=lambda w: -activity_score(w))
    for w in wikis:
        w.pop("rank", None)
    s = load_settings()
    allw = s.get("wikis", {})
    allw[key] = {"at": time.time(), "wikis": wikis}
    update_settings(wikis=allw)
    return wikis


def activity_score(w):
    """Bigger + more recently edited wikis rank higher (Fandom forks are often abandoned)."""
    age_days = 9999
    try:
        age_days = (time.time() - time.mktime(time.strptime(w["lastEdit"][:19], "%Y-%m-%dT%H:%M:%S"))) / 86400
    except (ValueError, KeyError):
        pass
    return w.get("articles", 0) / (1 + max(0, age_days) / 30)


def set_wiki_override(name, url):
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    base = f"{u.scheme}://{u.netloc}"
    res = probe_wiki(base, "/api.php") or probe_wiki(base, "/w/api.php")
    if not res:
        raise ValueError("That site doesn't look like a MediaWiki wiki")
    s = load_settings()
    allw = s.get("wikis", {})
    current = [w for w in allw.get(name.lower(), {}).get("wikis", []) if w["key"] != res["key"]]
    allw[name.lower()] = {"at": time.time(), "wikis": [res] + current}
    update_settings(wikis=allw)
    return allw[name.lower()]["wikis"]


def steam_source(appid):
    return {"key": "steam", "sitename": "Steam Store", "kind": "steam", "appid": str(appid),
            "base": "https://store.steampowered.com", "article": f"https://store.steampowered.com/app/{appid}/"}


def sources_for(name, wikis, appid=None):
    guides = [extras.guides_source(appid)] if appid else []
    store = [steam_source(appid)] if appid else []
    return ([guideindex.index_source(name, appid)] + wikis + guides + [webguides.web_source(name)]
            + [dict(PCGW), dict(WIKIPEDIA)] + store)


def source_by_key(name, key, appid=None):
    for src in sources_for(name, discover_wikis(name), appid):
        if src["key"] == key:
            return src
    raise KeyError(key)


def steam_page(src):
    j = cached_json(f"https://store.steampowered.com/api/appdetails?appids={src['appid']}&l=english", PAGE_TTL)
    d = j.get(src["appid"], {})
    if not d.get("success"):
        raise KeyError("Steam has no store page for this game")
    d = d["data"]

    def img(u):
        return "/api/img?u=" + urllib.parse.quote(u, safe="")
    facts = [("Developer", ", ".join(d.get("developers", []))), ("Publisher", ", ".join(d.get("publishers", []))),
             ("Released", d.get("release_date", {}).get("date", "")),
             ("Genres", ", ".join(g["description"] for g in d.get("genres", []))),
             ("Features", ", ".join(c["description"] for c in d.get("categories", [])[:10]))]
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in facts if v)
    shots = "".join(f'<img src="{img(sh["path_thumbnail"])}" loading="lazy" alt="Screenshot">'
                    for sh in d.get("screenshots", [])[:8])
    req = d.get("pc_requirements") or {}
    req = req if isinstance(req, dict) else {}
    header = d.get("header_image")
    parts = [f'<p><img src="{img(header)}" alt="{html.escape(d["name"])}"></p>' if header else "",
             f"<p>{html.escape(d.get('short_description', ''))}</p>",
             f'<table class="infobox">{rows}</table>',
             "<h2>About the game</h2>", clean_html(d.get("about_the_game", ""), src),
             f'<h2>Screenshots</h2><div class="gallery">{shots}</div>' if shots else "",
             "<h2>PC requirements</h2>", clean_html(req.get("minimum", ""), src),
             clean_html(req.get("recommended", ""), src)]
    return {"title": d["name"], "html": "".join(parts), "sections": ["About the game", "Screenshots", "PC requirements"],
            "url": src["article"], "edited": "",
            "source": {k: src.get(k) for k in ("key", "sitename", "kind", "base")}}

# ------------------------------------------------------------------ page cleaning

ALLOWED = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead",
           "tbody", "tfoot", "tr", "th", "td", "caption", "b", "strong", "i", "em", "u", "s", "small", "sup", "sub",
           "code", "pre", "blockquote", "a", "img", "figure", "figcaption", "div", "span", "aside", "section",
           "hr", "center", "big", "abbr", "kbd"}
DROP = {"script", "style", "noscript", "iframe", "form", "input", "button", "select", "textarea", "svg", "video",
        "audio", "link", "meta", "map", "object", "template"}
DROP_CLASS = re.compile(r"\b(navbox|mw-editsection|noprint|toc|printfooter|catlinks|mw-empty-elt|reference|"
                        r"references|reflist|metadata|ambox|mbox-small|navigation-not-searchable|wds-|"
                        r"global-navigation|mw-references-wrap|portal|sister|stub)\b", re.I)
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "col", "track", "param", "embed"}


class Cleaner(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.src = source
        self.out = []
        self.skip = 0
        self.stack = []

    def _abs(self, url):
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.src["base"] + url
        return url

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        void = tag in VOID
        if self.skip:
            if not void:
                self.skip += 1
            return
        cls = a.get("class", "") or ""
        if tag in DROP or DROP_CLASS.search(cls) or a.get("id") in ("toc", "catlinks") or \
                "display:none" in (a.get("style") or "").replace(" ", ""):
            if not void:
                self.skip = 1
            return
        if tag not in ALLOWED:
            if not void:
                self.stack.append(None)
            return
        keep = []
        if tag == "a":
            href = a.get("href", "")
            title = self.internal_title(href)
            if title:
                keep.append(("data-t", title))
                keep.append(("href", "#"))
            elif href.startswith(("http", "//")):
                keep.append(("data-ext", self._abs(href)))
                keep.append(("href", "#"))
        elif tag == "img":
            src = a.get("data-src") or a.get("src", "")
            if not src or src.startswith("data:"):
                return
            src = self._abs(src)
            keep.append(("src", "/api/img?u=" + urllib.parse.quote(src, safe="")))
            keep.append(("loading", "lazy"))
            for k in ("alt", "width", "height"):
                if a.get(k):
                    keep.append((k, a[k]))
        elif tag in ("td", "th"):
            for k in ("colspan", "rowspan"):
                if a.get(k):
                    keep.append((k, a[k]))
        if cls:
            safe = re.sub(r"[^A-Za-z0-9 _-]", "", cls)[:120]
            if safe:
                keep.append(("class", safe))
        attr = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in keep)
        self.out.append(f"<{tag}{attr}>")
        if not void:
            self.stack.append(tag)

    def internal_title(self, href):
        if not href or href.startswith("#"):
            return None
        u = urllib.parse.urlparse(self._abs(href))
        if u.netloc and u.netloc != urllib.parse.urlparse(self.src["base"]).netloc:
            return None
        path = urllib.parse.unquote(u.path)
        m = re.match(r"^(?:/wiki/|/w/)(.+)$", path)
        if m:
            t = m.group(1)
        else:
            q = urllib.parse.parse_qs(u.query)
            if "title" not in q or "action" in q:
                return None
            t = q["title"][0]
        t = t.replace("_", " ")
        if re.match(r"^(File|Special|Category|Template|User|Talk|Help|MediaWiki|Module):", t, re.I):
            return None
        return t

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.skip:
            self.skip -= 1
            return
        if not self.stack:
            return
        top = self.stack.pop()
        if top:
            self.out.append(f"</{top}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            top = self.stack.pop()
            if top:
                self.out.append(f"</{top}>")
        return "".join(self.out)


def clean_html(raw, source):
    c = Cleaner(source)
    c.feed(raw)
    c.close()
    return c.result()


def html_to_sections(cleaned):
    """Split cleaned HTML into (heading, plain text) sections for chat retrieval."""
    parts = re.split(r"<h[23][^>]*>(.*?)</h[23]>", cleaned, flags=re.S)
    sections = [("Overview", parts[0])]
    for i in range(1, len(parts) - 1, 2):
        sections.append((strip_tags(parts[i]), parts[i + 1]))
    out = []
    for head, body in sections:
        text = strip_tags(re.sub(r"</(p|li|tr|h\d|dd|dt|div)>", "\n", body))
        text = re.sub(r"[ \t]+", " ", text)
        lines, prev = [], None  # infoboxes repeat labels line after line
        for line in (l.strip() for l in text.split("\n")):
            if line and line != prev:
                lines.append(line)
            prev = line or prev
        text = "\n".join(lines)
        if text:
            out.append((head.strip() or "Section", text))
    return out


def strip_tags(s):
    return re.sub(r"\s{2,}", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()

# ------------------------------------------------------------------ pages & search


def fetch_page(src, title):
    if src["kind"] == "steam":
        return steam_page(src)
    if src["kind"] == "steamguides":
        return extras.guides_page(src, title or "")
    if src["kind"] == "web":
        return webguides.page(src["game"], title or "")
    if src["kind"] == "index":
        return guideindex.page(src, title or "")
    j = mw(src, action="parse", page=title, prop="text|sections|displaytitle|revid", redirects=1,
           disableeditsection=1, disabletoc=1)
    if "error" in j:
        raise KeyError(j["error"].get("info", "Page not found"))
    p = j["parse"]
    cleaned = clean_html(p["text"], src)
    t = p["title"]
    edited = ""
    try:
        r = mw(src, action="query", prop="revisions", rvprop="timestamp", titles=t)
        edited = r["query"]["pages"][0]["revisions"][0]["timestamp"]
    except (KeyError, IndexError):
        pass
    return {"title": t, "html": cleaned, "sections": [s["line"] for s in p.get("sections", []) if s.get("toclevel") == 1],
            "url": src["article"] + urllib.parse.quote(t.replace(" ", "_")), "edited": edited,
            "source": {k: src.get(k) for k in ("key", "sitename", "kind", "base")}}


def search_source(src, q, limit=8):
    if src["kind"] == "steam":
        return []
    if src["kind"] == "steamguides":
        return extras.search_guides(src, q, limit)
    if src["kind"] == "web":
        return webguides.search_results(src["game"], q, limit)
    if src["kind"] == "index":
        return webguides.search_results(src["game"], q, limit)
    j = mw(src, ttl=SEARCH_TTL, action="query", list="search", srsearch=q, srlimit=limit, srprop="snippet|timestamp")
    return [{"title": r["title"], "snippet": strip_tags(r.get("snippet", "")), "edited": r.get("timestamp", "")}
            for r in j.get("query", {}).get("search", [])]


def find_game_page(src, game):
    """Best page for the game itself on a general wiki (PCGamingWiki / Wikipedia)."""
    q = game + (" video game" if src["kind"] == "wikipedia" else "")
    hits = search_source(src, q, 5)
    norm = re.sub(r"[^a-z0-9]", "", re.sub(r"[Â®â„¢Â©]", "", game.lower()))
    loose = None
    for h in hits:
        t = re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", h["title"]).lower())
        if t == norm:
            return h["title"]
        if loose is None and len(norm) >= 5 and (norm in t or t in norm) and len(t) >= 5:
            loose = h["title"]
    return loose  # never fall back to an unrelated page

# ------------------------------------------------------------------ chat retrieval

STOP = set("""a an the and or but if then so to of in on at by for with from about into over under is are was were be
been being do does did how what where when why which who whom can could should would will i me my we you your it its this
that these those there here get got find best way any some more most much many need want tell please game""".split())
TECH = re.compile(r"\b(crash|fps|lag|stutter|settings?|fix|bug|mods?|modding|controller|gamepad|resolution|"
                  r"ultrawide|save (file|location)|config|ini|vsync|dlss|fsr|hdr|performance|launch|install|"
                  r"steam deck|linux|requirements?|multiplayer|co-?op|cross-?play)\b", re.I)


INTENTS = [  # question words -> section headings / body words that usually hold the answer
    (r"\b(where|find|found|locat\w*|spawn\w*|get|obtain\w*|farm\w*|catch|tame|buy|source)\b",
     ["obtain", "location", "habitat", "spawn", "where", "acquisition", "how to get", "sources", "vendor",
      "shop", "found"]),
    (r"\b(drops?|loot)\b", ["drop", "loot"]),
    (r"\b(craft\w*|make|build\w*|recipe|ingredients?|materials?)\b",
     ["craft", "recipe", "materials", "ingredients", "requires", "building"]),
    (r"\b(beat|defeat|kill|fight\w*|boss\w*|weak\w*|counter|strateg\w*)\b",
     ["strategy", "combat", "weakness", "weak", "tips", "attacks", "moveset", "phases", "resist"]),
    (r"\b(unlock\w*|requir\w*|prerequisite)\b", ["unlock", "requirement", "prerequisite", "technology"]),
    (r"\b(breed\w*|eggs?)\b", ["breeding", "egg", "parents", "combination"]),
    (r"\b(stats?|damage|health|hp)\b", ["stats", "damage", "health", "attributes"]),
]


def intent_words(question):
    extra = []
    for pattern, words in INTENTS:
        if re.search(pattern, question, re.I):
            extra.extend(words)
    return extra


def keywords(text):
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOP]


def title_candidates(question):
    """Names the player mentioned, as possible page titles: 'lamball drops' -> Lamball, Lamball Drops, ..."""
    words = re.findall(r"[A-Za-z0-9'][A-Za-z0-9'\-]*", question)
    cands = []
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            chunk = words[i:i + n]
            if chunk[0].lower() in STOP or chunk[-1].lower() in STOP or any(len(w) < 3 for w in chunk if n == 1):
                continue
            for form in (" ".join(chunk), " ".join(w[:1].upper() + w[1:] for w in chunk)):
                if form not in cands:
                    cands.append(form)
    return cands[:45]


def existing_titles(src, question):
    cands = title_candidates(question)
    if not cands:
        return []
    try:
        j = mw(src, ttl=SEARCH_TTL, action="query", titles="|".join(cands), redirects=1)
    except Exception:
        return []
    pages = [p["title"] for p in j.get("query", {}).get("pages", []) if not p.get("missing") and not p.get("invalid")
             and p.get("ns", 0) == 0]
    # Longer, more specific names first ("Lamball Mutton" before "Lamball").
    return sorted(set(pages), key=lambda t: -len(t))


def best_sections(sections, kws, budget, intents=()):
    scored = []
    for i, (head, text) in enumerate(sections):
        low = (head + " " + text).lower()
        hl = head.lower()
        score = sum(min(low.count(k), 6) for k in kws) + sum(3 for k in kws if k in hl)
        score += sum(8 for w in intents if w in hl) + sum(min(low.count(w), 3) for w in intents)
        if i == 0:
            score += 1
        scored.append((score, i, head, text))
    picked, used = [], 0
    for score, i, head, text in sorted(scored, key=lambda x: (-x[0], x[1])):
        if score <= 0 and picked:
            break
        chunk = text[: max(300, budget - used)]
        picked.append((i, head, chunk))
        used += len(chunk)
        if used >= budget:
            break
    return "\n".join(f"## {h}\n{t}" for _, h, t in sorted(picked))


def gather_context(game, question, current=None, appid=None):
    """Pull matching material from every source in parallel. Returns a list of labelled excerpts."""
    wikis = discover_wikis(game)
    kws = keywords(question)
    intents = intent_words(question)
    query = " ".join(kws[:8]) or question
    tech = bool(TECH.search(question))
    jobs = []

    def from_wiki(src):
        found = []
        titles = existing_titles(src, question)[:2]
        try:
            hits = search_source(src, query, 4)
            if not hits and len(kws) > 2:
                hits = search_source(src, " ".join(kws[:3]), 4)
        except Exception:
            hits = []
        for h in hits:
            if len(titles) >= 3:
                break
            if h["title"] not in titles:
                titles.append(h["title"])
        if current and current.get("source") == src["key"]:
            titles = [t for t in titles if t != current.get("title")]
        for title in titles[:3]:
            try:
                page = fetch_page(src, title)
                text = best_sections(html_to_sections(page["html"]), kws, 1600, intents)
                found.append(excerpt(src, page, text))
            except Exception:
                continue
        return found

    def from_general(src):
        try:
            title = find_game_page(src, game)
            if not title:
                return []
            page = fetch_page(src, title)
            return [excerpt(src, page, best_sections(html_to_sections(page["html"]), kws, 1500, intents))]
        except Exception:
            return []

    def from_steam(app):
        try:
            src = steam_source(app)
            page = steam_page(src)
            return [excerpt(src, page, best_sections(html_to_sections(page["html"]), kws, 1500, intents))]
        except Exception:
            return []

    def from_guides(app):
        """Community walkthroughs: search the game's Steam guides for this question."""
        src = extras.guides_source(app)
        found = []
        try:
            hits = extras.list_guides(app, query, 4) or (extras.list_guides(app, " ".join(kws[:2]), 4) if kws else [])
        except Exception:
            return found
        for g in hits[:2]:
            try:
                page = extras.fetch_guide(src, g["id"])
                text = best_sections(html_to_sections(page["html"]), kws, 1600, intents)
                found.append(excerpt(src, page, text, open_as=f"guide:{g['id']}"))
            except Exception:
                continue
        return found

    def from_web():
        """Guide websites (walkthroughs, wikis the discovery missed) found by a web search."""
        found = []
        try:
            hits = webguides.game_hits(game, query, 4)
        except Exception:
            return found
        for h in hits[:2]:
            try:
                page = webguides.fetch(h["url"])
                src = {"key": "web", "sitename": f"Web Â· {h['site']}", "kind": "web"}
                text = best_sections(html_to_sections(page["html"]), kws, 1600, intents)
                found.append(excerpt(src, page, text, open_as="web:" + h["url"]))
            except Exception:
                continue
        return found

    def from_current():
        try:
            src = source_by_key(game, current["source"], appid)
            if current["title"] == src.get("mainpage"):
                return []  # a wiki's front page is noise, not an answer
            if src["kind"] == "steamguides" and not current["title"].startswith("guide:"):
                return []  # the guide list itself isn't an answer
            if src["kind"] == "index" and not current["title"]:
                return []
            if src["kind"] == "web" and not current["title"].startswith("web:"):
                return []
            page = fetch_page(src, current["title"])
            if src["kind"] == "web":
                src = dict(src, sitename=f"Web Â· {page['site']}")
            return [excerpt(src, page, best_sections(html_to_sections(page["html"]), kws, 2600, intents),
                            current=True, open_as=current["title"])]
        except Exception:
            return []

    with cf.ThreadPoolExecutor(8) as ex:
        if current and current.get("title"):
            jobs.append(ex.submit(from_current))
        for w in wikis[:3]:
            jobs.append(ex.submit(from_wiki, w))
        if appid:
            jobs.append(ex.submit(from_guides, appid))
        jobs.append(ex.submit(from_web))
        if tech:
            jobs.append(ex.submit(from_general, dict(PCGW)))
        if not wikis:
            jobs.append(ex.submit(from_general, dict(WIKIPEDIA)))
            if not tech:
                jobs.append(ex.submit(from_general, dict(PCGW)))
            if appid:
                jobs.append(ex.submit(from_steam, appid))
        results = []
        for j in jobs:
            if j:
                results.extend(j.result())
    seen, unique = set(), []
    for r in results:
        k = (r["source"], r["title"])
        if k not in seen and r["text"].strip():
            seen.add(k)
            unique.append(r)
    return unique, wikis


def missions_for(game, appid="", web=True):
    """Ordered mission list: a wiki quest list, else a web walkthrough hub, else the installed game's files.
    Game-file quests are also merged under matching web regions, since walkthroughs skip side quests."""
    labels = load_settings().get("missionLabels", {}).get(game, {})
    appid = re.sub(r"\D", "", appid or "")
    local = {"groups": []}
    folder = localquests.install_dir(steam_libs(), appid) if appid else None
    if folder:
        try:
            local = localquests.quests_for(folder)
        except Exception:
            pass
    result = {"groups": []}
    try:
        result = extras.missions_for(game, discover_wikis(game))
    except Exception:
        pass
    if not result["groups"] and web:
        try:
            result = webguides.missions(game)
            if result["groups"] and local["groups"]:
                merge_local(result, local)
        except Exception:
            result = {"groups": []}
    if not result["groups"]:
        result = local
    result["labels"] = labels
    return result


def merge_local(web, local):
    """Put each game-file region (Dustvaldr) after the walkthrough's acts for that region (Dustlanger Act 3)."""
    def region(name):
        return re.sub(r"[^a-z]", "", name.split()[0].lower())[:4] if name.split() else ""
    groups = web["groups"]
    for lg in local["groups"]:
        if " Â· " in lg["name"] or not any(i.get("seen") for i in lg["items"]) and len(lg["items"]) < 3:
            continue
        spots = [i for i, g in enumerate(groups) if region(g["name"]) == region(lg["name"])]
        if not spots:
            continue
        web_name = groups[spots[0]]["name"].split()[0]
        words = lg["name"].split()
        shown = web_name if len(words) == 1 else lg["name"]
        if len(words) == 1 and words[0] != web_name:
            shown += f" ({words[0]})"
        groups.insert(spots[-1] + 1, {"name": f"{shown} Â· all quests in the game files", "local": True,
                                      "items": lg["items"]})
    web["hasSave"] = local.get("hasSave", False)


_catalog_cache = {"mtime": -1, "data": None}


def catalog_page(q):
    """Slice of the ranked catalog with guide coverage: ?offset=&limit=&q=&store=steam|epic&indie=1"""
    path = catalog.CATALOG
    mtime = os.path.getmtime(path) if os.path.exists(path) else 0
    if _catalog_cache["mtime"] != mtime:
        _catalog_cache.update(mtime=mtime, data=catalog.load())
    data = _catalog_cache["data"]
    games = [g for g in data["games"] if not catalog.is_nintendo(g["name"])]
    text = (q.get("q") or "").strip().lower()
    if text:
        games = [g for g in games if text in g["name"].lower()]
    if q.get("store") == "steam":
        games = [g for g in games if g["steam"]]
    elif q.get("store") == "epic":
        games = [g for g in games if g["epic"]]
    if q.get("indie") == "1":
        games = [g for g in games if g["indie"]]
    offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 60)), 200)
    rows = []
    for g in games[offset:offset + limit]:
        idx = guideindex.load(g["name"], g["steam"] or "")
        rows.append({**g, "coverage": idx["score"] if idx else None})
    return {"built": data["built"], "total": len(games), "all": data["count"], "offset": offset, "games": rows}


def source_label(src):
    host = {"wikigg": "wiki.gg", "fandom": "Fandom"}.get(src["kind"])
    return f"{src['sitename']} ({host})" if host else src["sitename"]


def excerpt(src, page, text, current=False, open_as=None):
    return {"source": src["key"], "sitename": source_label(src), "kind": src["kind"], "title": page["title"],
            "open": open_as or page["title"], "url": page["url"], "edited": page["edited"][:10], "text": text,
            "current": current}


SYSTEM = """You are Game Guide, an expert assistant helping a player with the PC game "{game}".
You are given SOURCES pulled live from several independent wikis, guide websites ("Web Â· ..."), and Steam Community guides written by players. Rules:
1. Base every factual claim (items, stats, locations, recipes, quest steps, numbers) on the SOURCES. Never invent them. Never name a place, item, character, enemy or number that does not appear in the SOURCES, not even in general tips.
2. Cross-check the sources. If they agree, answer confidently. If they disagree, say so briefly, show what each source says, and prefer the one edited more recently or from the more active wiki; say which one you trust and why.
3. Cite sources inline using their tag, like [S1] or [S2][S3].
4. If the sources don't answer the question, say that clearly, then give general advice and label it "General tip (not from the wikis)".
5. Be practical and concise: short steps, bullet lists, exact names. No filler.
6. Steam Guides and guide websites are written by individual authors: great for walkthroughs and strategy, but they can be outdated. Check their date, and when they conflict with a wiki, prefer the newer, better-supported information.
Wiki activity (bigger and more recently edited usually means more up to date):
{activity}"""


def build_messages(game, history, context, wikis):
    activity = "\n".join(f"- {w['sitename']} ({w['key']}): {w['articles']} articles, last edit {w['lastEdit'][:10]}"
                         for w in wikis) or "- no game-specific wiki found"
    blocks = []
    for i, c in enumerate(context, 1):
        tag = f"S{i}"
        c["tag"] = tag
        mark = " (the page the player is reading)" if c["current"] else ""
        blocks.append(f"[{tag}] {c['sitename']} â€” \"{c['title']}\" (last edited {c['edited'] or 'unknown'}){mark}\n{c['text']}")
    sources = "\n\n".join(blocks) or "(no sources found for this question)"
    msgs = [{"role": "system", "content": SYSTEM.format(game=game, activity=activity)}]
    for m in history[-8:-1]:
        msgs.append({"role": m["role"], "content": m["content"][:2000]})
    question = history[-1]["content"]
    msgs.append({"role": "user", "content": f"SOURCES:\n{sources}\n\nQUESTION: {question}"})
    return msgs

def labelled(sources):
    for src in sources:
        src["label"] = source_label(src)
    return sources

_ollama_started = False


def ensure_ollama():
    """Start Ollama in the background if it isn't running (once per session)."""
    global _ollama_started
    try:
        http_get(OLLAMA + "/api/version", 2)
        return True
    except Exception:
        pass
    if _ollama_started:
        return False
    _ollama_started = True
    import shutil
    exe = shutil.which("ollama") or os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe")
    if not os.path.exists(exe):
        return False
    subprocess.Popen([exe, "serve"], creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        try:
            http_get(OLLAMA + "/api/version", 2)
            return True
        except Exception:
            continue
    return False

# ------------------------------------------------------------------ HTTP handler


class Handler(BaseHTTPRequestHandler):
    server_version = "GameGuide/0.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send_file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8")
            if u.path.startswith("/static/"):
                name = os.path.basename(u.path)
                ctype = {"js": "text/javascript", "css": "text/css", "png": "image/png", "svg": "image/svg+xml"}.get(
                    name.rsplit(".", 1)[-1], "application/octet-stream")
                return self.send_file(os.path.join(WEB, name), ctype + "; charset=utf-8")
            if u.path == "/api/games":
                s = load_settings()
                return self.send_json({"installed": installed_games(), "recent": s.get("recent", []),
                                       "model": s.get("model", "")})
            if u.path == "/api/store_search":
                return self.send_json(store_search(q.get("q", "")))
            if u.path == "/api/wikis":
                wikis = discover_wikis(q["game"])
                return self.send_json({"wikis": wikis,
                                       "sources": labelled(sources_for(q["game"], wikis, q.get("appid")))})
            if u.path == "/api/search":
                src = source_by_key(q["game"], q["source"], q.get("appid"))
                return self.send_json(search_source(src, q["q"], 15))
            if u.path == "/api/page":
                src = source_by_key(q["game"], q["source"], q.get("appid"))
                title = q.get("title") or (find_game_page(src, q["game"]) if src["kind"] in ("pcgw", "wikipedia")
                                           else src.get("mainpage", "Main Page"))
                if not title:
                    return self.send_json({"error": f"No page for this game on {src['sitename']}"}, 404)
                return self.send_json(fetch_page(src, title))
            if u.path == "/api/img":
                return self.proxy_image(q["u"])
            if u.path == "/api/art":
                return self.proxy_image(header_url(q["appid"]))
            if u.path == "/api/missions":
                return self.send_json(missions_for(q["game"], q.get("appid", "")))
            if u.path == "/api/mission_refs":
                m = missions_for(q["game"], q.get("appid", ""))
                hub = m.get("sitename", "") if m.get("source") == "web" else ""
                return self.send_json(webguides.references(q["game"], discover_wikis(q["game"]), hub))
            if u.path == "/api/guide_index":
                idx = guideindex.build(q["game"], q.get("appid", ""), force=q.get("force") == "1")
                return self.send_json(idx)
            if u.path == "/api/map_markers":
                return self.send_json(guideindex.map_markers(q["id"], q.get("url", "")))
            if u.path == "/api/prebuild_status":
                try:
                    with open(os.path.join(DATA, "prebuild_status.json"), encoding="utf-8") as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    st = {}
                st["guides"] = len(glob.glob(os.path.join(DATA, "guides", "*.json")))
                st["cloud"] = sync.status()
                return self.send_json(st)
            if u.path == "/api/catalog":
                return self.send_json(catalog_page(q))
            if u.path == "/api/step_refs":
                return self.send_json(webguides.step_refs(q["game"], q.get("step", ""), q.get("region", "")))
            if u.path == "/api/models":
                ensure_ollama()
                try:
                    names = [m["name"] for m in json.loads(http_get(OLLAMA + "/api/tags", 5))["models"]]
                except Exception:
                    names = []
                return self.send_json(names)
            if u.path == "/api/open":
                url = q.get("u", "")
                if url.startswith(("https://", "http://")):
                    open_in_browser(url)
                return self.send_json({"ok": True})
            self.send_error(404)
        except KeyError as e:
            self.send_json({"error": str(e).strip("'\"")}, 404)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 502)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/api/chat":
                return self.chat(body)
            if self.path == "/api/recent":
                s = load_settings()
                recent = [g for g in s.get("recent", []) if g["name"] != body["name"]]
                recent.insert(0, {"name": body["name"], "appid": body.get("appid", "")})
                update_settings(recent=recent[:12])
                return self.send_json({"ok": True})
            if self.path == "/api/mission_label":
                with _settings_lock:
                    st = load_settings()
                    game_labels = st.setdefault("missionLabels", {}).setdefault(body["game"], {})
                    label = (body.get("label") or "").strip()[:120]
                    if label:
                        game_labels[body["key"]] = label
                    else:
                        game_labels.pop(body["key"], None)
                    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                        json.dump(st, f, indent=2)
                return self.send_json({"ok": True})
            if self.path == "/api/model":
                update_settings(model=body["model"])
                return self.send_json({"ok": True})
            if self.path == "/api/wiki_override":
                wikis = set_wiki_override(body["game"], body["url"])
                return self.send_json({"wikis": wikis,
                                       "sources": labelled(sources_for(body["game"], wikis, body.get("appid")))})
            if self.path == "/api/refresh_wikis":
                s = load_settings()
                s.get("wikis", {}).pop(body["game"].lower(), None)
                update_settings(wikis=s.get("wikis", {}))
                wikis = discover_wikis(body["game"])
                return self.send_json({"wikis": wikis,
                                       "sources": labelled(sources_for(body["game"], wikis, body.get("appid")))})
            self.send_error(404)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 400)

    def proxy_image(self, url):
        if not url.startswith("https://"):
            return self.send_error(400)
        key = hashlib.sha1(url.encode()).hexdigest()
        path = os.path.join(IMG_CACHE, key)
        meta = path + ".type"
        if os.path.exists(path) and os.path.exists(meta):
            ctype = open(meta).read()
            data = open(path, "rb").read()
        else:
            data = None
            for candidate in image_candidates(url):
                for ua in (UA, extras.BROWSER_UA):
                    try:
                        req = urllib.request.Request(candidate, headers={"User-Agent": ua, "Accept": "image/*,*/*"})
                        with urllib.request.urlopen(req, timeout=20) as r:
                            data, ctype = r.read(), r.headers.get("Content-Type", "")
                        break
                    except Exception:
                        continue
                if data is not None:
                    break
            if data is None:
                return self.send_error(404)
            if not ctype.startswith("image/"):
                return self.send_error(415)
            with open(path, "wb") as f:
                f.write(data)
            with open(meta, "w") as f:
                f.write(ctype)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def emit(self, obj):
        self.wfile.write((json.dumps(obj) + "\n").encode())
        self.wfile.flush()

    def chat(self, body):
        game, model = body["game"], body["model"]
        history = body["messages"]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.emit({"status": "Searching the wikisâ€¦"})
        threading.Thread(target=ensure_ollama, daemon=True).start()
        context, wikis = gather_context(game, history[-1]["content"], body.get("current"), body.get("appid"))
        msgs = build_messages(game, history, context, wikis)
        self.emit({"sources": [{k: c[k] for k in ("tag", "sitename", "title", "open", "url", "edited", "source", "kind")}
                               for c in context]})
        self.emit({"status": f"Reading {len(context)} source{'s' if len(context) != 1 else ''}â€¦"})
        req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps({
            "model": model, "messages": msgs, "stream": True, "keep_alive": "30m",
            "options": {"temperature": 0.3, "num_ctx": 12288}}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                for line in r:
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        self.emit({"t": piece})
                    if chunk.get("done"):
                        break
        except (urllib.error.URLError, OSError) as e:
            self.emit({"error": f"Ollama isn't reachable ({e}). Start Ollama and try again."})
        self.emit({"done": True})

# ------------------------------------------------------------------ launch


def app_browser():
    """(exe, process name, profile dir): Chrome if installed, else Edge."""
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if chrome:
        return chrome, "chrome.exe", CHROME_PROFILE
    edge = next((p for p in EDGE_PATHS if os.path.exists(p)), None)
    return (edge, "msedge.exe", EDGE_PROFILE) if edge else (None, None, None)


def open_in_browser(url):
    """External links go to the user's normal Chrome (their tabs and sign-ins), else the default browser."""
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if chrome:
        subprocess.Popen([chrome, url], creationflags=0x08000000)
    else:
        os.startfile(url)


def edge_windows_open():
    """True while any browser process is using our private app profile."""
    _, proc_name, profile = app_browser()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"Name='{proc_name}'\" | "
             f"Where-Object {{ $_.CommandLine -like '*{os.path.basename(profile)}*' }}).Count"],
            capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout.strip()
        return int(out or 0) > 0
    except Exception:
        return True


def open_window():
    exe, _, profile = app_browser()
    url = f"http://127.0.0.1:{PORT}/"
    if not exe:
        os.startfile(url)
        return None
    return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1600,980",
                             "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])


extras.init(sys.modules[__name__])
webguides.init(sys.modules[__name__])
guideindex.init(sys.modules[__name__])


def main():
    headless = "--no-window" in sys.argv
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # already running: just open another window
        if not headless:
            open_window()
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if "--no-sync" not in sys.argv:
        sync.sync_in_background()  # shared guide data built by GitHub Actions
    if headless:
        print(f"Game Guide running on http://127.0.0.1:{PORT}/", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    proc = open_window()
    if proc:
        proc.wait()
        time.sleep(3)
        while edge_windows_open():
            time.sleep(5)
    else:
        while True:
            time.sleep(3600)
    server.shutdown()


if __name__ == "__main__":
    main()
