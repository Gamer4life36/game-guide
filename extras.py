"""Steam Community guides and ordered mission lists for Game Guide.

Imported by server.py; uses its helpers through the `srv` module reference set in `init()`.
"""
import html
import json
import re
import time
import urllib.parse

srv = None  # server module, injected by init()

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) GameGuide/0.1"
GUIDE_TTL = 3 * 24 * 3600


def init(server_module):
    global srv
    srv = server_module

# ================================================================== Steam Community guides


def guides_source(appid):
    return {"key": "steamguides", "sitename": "Steam Guides", "kind": "steamguides", "appid": str(appid),
            "base": "https://steamcommunity.com",
            "article": f"https://steamcommunity.com/app/{appid}/guides/"}


def _get_html(url, ttl):
    """Fetch an HTML page with a disk cache (same folder as the JSON cache)."""
    import hashlib
    import os
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(srv.CACHE, key + ".html")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        return open(path, encoding="utf-8").read()
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        if os.path.exists(path):
            return open(path, encoding="utf-8").read()
        raise
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def list_guides(appid, query="", limit=30):
    params = {"browsefilter": "trend" if query else "toprated", "requiredtags[]": "english", "numperpage": 30}
    if query:
        params["searchText"] = query
    url = f"https://steamcommunity.com/app/{appid}/guides/?" + urllib.parse.urlencode(params)
    page = _get_html(url, srv.SEARCH_TTL if query else GUIDE_TTL)
    ratings = re.findall(r'class="fileRating" src="[^"]*/(\d|not-yet)[-_]?star', page)
    previews = re.findall(r'<img class="workshopItemPreviewImage" src="([^"]+)"', page)
    items = []
    for i, m in enumerate(re.finditer(r'SharedFileBindMouseHover\(\s*"[^"]+",\s*\w+,\s*(\{.*?\})\s*\);', page, re.S)):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        stars = ratings[i] if i < len(ratings) else ""
        items.append({"id": d["id"], "title": html.unescape(d.get("title", "")).strip(),
                      "desc": html.unescape(d.get("description", "")).strip(),
                      "stars": int(stars) if stars.isdigit() else 0,
                      "preview": previews[i] if i < len(previews) else ""})
    return items[:limit]


def _div_inner(text, start):
    """Inner HTML of the <div> whose opening tag starts at or before `start`, honouring nesting."""
    open_end = text.index(">", start) + 1
    depth, pos = 1, open_end
    tag = re.compile(r"<(/?)div\b", re.I)
    while depth:
        m = tag.search(text, pos)
        if not m:
            return text[open_end:]
        depth += -1 if m.group(1) else 1
        pos = m.end()
    return text[open_end:m.start()]


def fetch_guide(src, guide_id):
    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={guide_id}"
    page = _get_html(url, GUIDE_TTL)
    title = re.search(r'<div class="workshopItemTitle">([^<]+)', page)
    title = html.unescape(title.group(1)).strip() if title else f"Guide {guide_id}"
    author = re.search(r'<div class="friendBlockContent">\s*([^<]+?)\s*<br', page)
    ratings = re.search(r"([\d,]+) ratings", page)
    dates = re.findall(r'<div class="detailsStatRight">([^<]+)</div>', page)
    updated = _steam_date(dates[-1]) if dates else ""
    intro = re.search(r'<div class="guideTopDescription"[^>]*>(.*?)</div>', page, re.S)
    parts = []
    meta = []
    if author:
        meta.append(f"by {html.escape(html.unescape(author.group(1)))}")
    if ratings:
        meta.append(f"{ratings.group(1)} ratings")
    if meta:
        parts.append(f'<p class="guidemeta">{" · ".join(meta)}</p>')
    if intro:
        parts.append(f"<blockquote>{srv.clean_html(intro.group(1), src)}</blockquote>")
    sections = []
    for m in re.finditer(r'<div class="subSectionTitle">', page):
        head = srv.strip_tags(_div_inner(page, m.start())).strip()
        desc_at = page.find('<div class="subSectionDesc">', m.end())
        if desc_at < 0:
            continue
        body = _div_inner(page, desc_at)
        sections.append(head)
        parts.append(f"<h2>{html.escape(head)}</h2>{srv.clean_html(body, src)}")
    return {"title": title, "html": "".join(parts), "sections": sections, "url": url, "edited": updated,
            "source": {k: src.get(k) for k in ("key", "sitename", "kind", "base")}}


def _steam_date(s):
    """'Aug 22 @ 2:27am' or 'Jul 1, 2024 @ 8:40am' -> '2026-08-22'."""
    s = s.split("@")[0].strip()
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%b %d", "%d %b"):
        try:
            t = time.strptime(s, fmt)
            year = t.tm_year if "%Y" in fmt else time.localtime().tm_year
            return f"{year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        except ValueError:
            continue
    return ""


def guides_index_page(src):
    items = list_guides(src["appid"])
    rows = []
    for g in items:
        stars = "★" * g["stars"] + "☆" * (5 - g["stars"]) if g["stars"] else ""
        img = ('<img src="/api/img?u=' + urllib.parse.quote(g["preview"], safe="") + '" alt="">') if g["preview"] else ""
        rows.append(f'<a class="guidecard" data-t="guide:{g["id"]}" href="#">{img}<span><b>{html.escape(g["title"])}</b>'
                    f'<small class="stars">{stars}</small><em>{html.escape(g["desc"][:180])}</em></span></a>')
    body = "".join(rows) or "<p>No English community guides found for this game.</p>"
    return {"title": "Top-rated Steam guides", "html": f'<div class="guidelist">{body}</div>', "sections": [],
            "url": src["article"], "edited": "",
            "source": {k: src.get(k) for k in ("key", "sitename", "kind", "base")}}


def guides_page(src, title):
    if title.startswith("guide:"):
        return fetch_guide(src, re.sub(r"\D", "", title))
    return guides_index_page(src)


def search_guides(src, q, limit=15):
    return [{"title": f"guide:{g['id']}", "name": g["title"], "snippet": g["desc"], "edited": ""}
            for g in list_guides(src["appid"], q, limit)]

# ================================================================== missions

LIST_TITLES = ["Main Missions", "Main Quests", "Main Story Quests", "Story Quests", "Story Missions", "Missions",
               "Quests", "Quest List", "List of quests", "Walkthrough", "Chapters", "Campaign", "Main Story"]
SKIP_SECTIONS = re.compile(r"^(history|references?|notes?|gallery|see also|trivia|navigation|external links|"
                           r"bugs?|videos?|achievements?|changelog|patch|sources)\b", re.I)
QUESTY = re.compile(r"quest|mission|chapter|walkthrough|story|campaign", re.I)


def game_specific(wiki, game):
    """True when the wiki is about this exact game, not a whole series (a sequel number must match too)."""
    words = [w for w in re.findall(r"[a-z0-9]+", srv.SEQUEL.sub("", game.lower())) if len(w) >= 3 and w != "the"]
    hay = (wiki["sitename"] + " " + wiki["key"]).lower().replace("-", "")
    if not all(w in hay for w in words):
        return False
    num = re.search(r"\s(\d+|ii|iii|iv|v|vi|vii|viii|ix|x)\b", re.split(r"\s*[:–—]\s*", game)[0].lower())
    return not num or re.search(r"(?<![a-z0-9])" + re.escape(num.group(1)) + r"(?![a-z0-9])", hay) is not None


def list_page_candidates(src, game, specific):
    """Possible mission-list pages, best first. Only game-named pages on series wikis."""
    base = re.split(r"\s*[:–—]\s*", game)[0]
    named = [f"{game} main quests", f"{base} main quests", f"{game} main missions", f"{base} main missions",
             f"{game} quests", f"{base} quests", f"Quests ({game})", f"Quest ({base})", f"Quests ({base})",
             f"{game} missions", f"{base} missions", f"{game} walkthrough", f"{base} walkthrough"]
    cands = list(dict.fromkeys(named + (LIST_TITLES if specific else [])))
    j = srv.mw(src, ttl=srv.SEARCH_TTL, action="query", titles="|".join(cands), redirects=1)
    q = j.get("query", {})
    exist = {p["title"].lower(): p["title"] for p in q.get("pages", []) if not p.get("missing")}
    redirects = {r["from"].lower(): r["to"] for r in q.get("redirects", [])}
    out = []
    for c in cands:
        t = exist.get(c.lower()) or exist.get(redirects.get(c.lower(), "").lower())
        if t and t not in out:
            out.append(t)
    if not specific or not out:
        words = [w for w in re.findall(r"[a-z0-9]+", base.lower()) if w != "the"]
        try:
            hits = srv.search_source(src, f"intitle:quests {base}", 10) + srv.search_source(src, f"intitle:missions {base}", 5)
        except Exception:
            hits = []
        found = [h["title"] for h in hits if QUESTY.search(h["title"])
                 and all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", h["title"].lower()) for w in words)]
        found.sort(key=lambda t: (0 if "main" in t.lower() else 1))
        for t in found:
            if t not in out:
                out.append(t)
    return out[:5]


LIST_LINK = re.compile(r"\b(quests|missions|contracts|treasure hunts|chapters|walkthrough|achievements)$", re.I)


def parse_missions(cleaned):
    """Ordered mission groups from a cleaned list page: first link of each table row / list item."""
    parts = re.split(r"<h([23])[^>]*>(.*?)</h\1>", cleaned, flags=re.S)
    chunks = [("", "", parts[0])]
    parent = ""
    for i in range(1, len(parts) - 2, 3):
        level, head = parts[i], srv.strip_tags(parts[i + 1]).strip()
        if level == "2":
            parent = head
            chunks.append((head, "", parts[i + 2]))
        else:
            chunks.append((parent, head, parts[i + 2]))
    groups = []
    for parent, head, body in chunks:
        name = head or parent
        if SKIP_SECTIONS.search(name) or (head and SKIP_SECTIONS.search(parent)):
            continue
        items, seen = [], set()
        for row in re.findall(r"<(?:tr|li)\b[^>]*>(.*?)</(?:tr|li)>", body, re.S):
            m = re.search(r'data-t="([^"]+)"', row)
            if not m:
                continue
            t = html.unescape(m.group(1))
            if t not in seen and not LIST_LINK.search(t):
                seen.add(t)
                items.append({"title": t})
        if len(items) >= 2:
            groups.append({"name": name or "Missions", "parent": parent if head else "", "items": items,
                           "lead": not name})
    if len(groups) > 1:  # links above the first heading are usually hatnotes, not missions
        groups = [g for g in groups if not g["lead"]]
    for g in groups:
        g["name"] = re.sub(r"\s+", " ", g["name"]).strip()
        g["parent"] = re.sub(r"\s+", " ", g["parent"]).strip()
    names = [g["name"] for g in groups]
    for g in groups:  # "Main Quests" three times: say which act/area each one is
        if names.count(g["name"]) > 1 and g["parent"]:
            g["name"] = f"{g['parent']} · {g['name']}"
    return groups


def headings_as_missions(cleaned):
    heads = [srv.strip_tags(h).strip() for _, h in re.findall(r"<h([23])[^>]*>(.*?)</h\1>", cleaned, re.S)]
    heads = [h for h in heads if h and not SKIP_SECTIONS.search(h)]
    return [{"name": "Missions", "items": [{"anchor": h} for h in heads]}] if len(heads) >= 2 else []


def missions_for(game, wikis):
    for src in wikis[:3]:
        specific = game_specific(src, game)
        try:
            titles = list_page_candidates(src, game, specific)
        except Exception:
            continue
        for title in titles:
            try:
                page = srv.fetch_page(src, title)
            except Exception:
                continue
            groups = parse_missions(page["html"])
            # Main story first when a page mixes main and side content (stable sort keeps page order).
            groups.sort(key=lambda g: 0 if re.search(r"main|story|tutorial|chapter|prologue|act\b|primary", g["name"], re.I)
                        else 1 if re.search(r"side|faction|companion|secondary", g["name"], re.I) else 2)
            if not groups and specific:
                groups = headings_as_missions(page["html"])
            if groups:
                return {"source": src["key"], "sitename": src["sitename"], "page": title, "groups": groups}
    return {"groups": []}
