"""Ranked catalog of PC games on Steam and the Epic Games Store, most popular first.

Sources (no API keys needed):
  * SteamSpy `request=all` pages: every Steam game, 1000 per page, ordered by owner count.
    SteamSpy allows one `all` request per minute, so a full refresh runs slowly in the background.
  * SteamSpy genre lists: which games are Indie (and which apps are software, to skip).
  * Steam charts (ISteamChartsService): games people are playing right now, to lift current hits.
  * Epic Games Store GraphQL search: Epic's base games, merged with Steam entries by name.

The result is data/catalog.json: [{name, steam, epic, rank, owners, ccu, indie, score}, ...].
Run `py -3.12 catalog.py` to build or refresh it (it resumes where it stopped).
"""
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CATALOG = os.path.join(DATA, "catalog.json")
PARTS = os.path.join(DATA, "catalog_parts")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
NON_GAMES = ("Utilities", "Design & Illustration", "Animation & Modeling", "Video Production", "Audio Production",
             "Software Training", "Web Publishing", "Photo Editing", "Education", "Accounting")
EPIC_QUERY = """query searchStoreQuery($count:Int,$start:Int,$category:String,$country:String!,$locale:String,$sortBy:String,$sortDir:String){
 Catalog{searchStore(count:$count,start:$start,category:$category,country:$country,locale:$locale,sortBy:$sortBy,sortDir:$sortDir){
  elements{title id namespace productSlug offerMappings{pageSlug pageType} tags{name} categories{path}}
  paging{count total}}}}"""


# Nintendo games are left out of the catalog entirely (by developer/publisher or franchise name).
NINTENDO_COMPANY = re.compile(r"\bnintendo\b|\bhal laboratory\b|"
                              r"\bmonolith soft\b|\bthe pok[eé]mon company\b|\bretro studios\b", re.I)
NINTENDO_FRANCHISE = re.compile(r"\b(super mario|mario kart|mario party|legend of zelda|zelda|pok[eé]mon|kirby|metroid|"
                                r"donkey kong|animal crossing|smash bros|splatoon|fire emblem|xenoblade|pikmin|star fox|"
                                r"wario|yoshi'?s|luigi'?s mansion|punch-out|earthbound|kid icarus)\b", re.I)


def is_nintendo(name, developer="", publisher=""):
    return bool(NINTENDO_COMPANY.search(f"{developer} | {publisher}") or NINTENDO_FRANCHISE.search(name or ""))


def http_json(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def norm(name):
    name = re.sub(r"[®™©]", "", name.lower())
    name = re.sub(r"\b(game of the year|goty|definitive|complete|deluxe|remastered|enhanced|edition)\b", " ", name)
    return re.sub(r"[^a-z0-9]", "", name)


def part_path(name):
    os.makedirs(PARTS, exist_ok=True)
    return os.path.join(PARTS, name + ".json")


def cached_part(name, fetch, max_age=7 * 86400):
    """Fetch once and keep on disk, so an interrupted build resumes without refetching."""
    path = part_path(name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = fetch()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def steamspy_all(log, max_pages=200):
    games = {}
    for page in range(max_pages):
        name = f"steamspy_all_{page:03d}"
        fresh = not os.path.exists(part_path(name))
        try:
            data = cached_part(name, lambda: http_json(f"https://steamspy.com/api.php?request=all&page={page}"))
        except Exception as e:
            log(f"SteamSpy page {page} failed: {e}")
            break
        if not data:
            break
        for appid, g in data.items():
            games[int(appid)] = g
        log(f"SteamSpy page {page}: {len(data)} games (total {len(games)})")
        if len(data) < 1000:
            break
        if fresh:
            time.sleep(61)  # SteamSpy: one `all` request per minute
    return games


def steamspy_genre(genre):
    return cached_part("genre_" + re.sub(r"\W", "_", genre),
                       lambda: http_json("https://steamspy.com/api.php?request=genre&genre=" + urllib.request.quote(genre)))


def steam_charts():
    ranks = {}
    try:
        j = http_json("https://api.steampowered.com/ISteamChartsService/GetGamesByConcurrentPlayers/v1/")
        for r in j["response"]["ranks"]:
            ranks[int(r["appid"])] = r.get("concurrent_in_game", 0)
    except Exception:
        pass
    return ranks


def epic_all(log, page_size=40):
    out = []
    start = 0
    while True:
        name = f"epic_{start:05d}"
        variables = {"count": page_size, "start": start, "category": "games/edition/base", "country": "US",
                     "locale": "en-US", "sortBy": "releaseDate", "sortDir": "DESC"}
        try:
            j = cached_part(name, lambda: http_json("https://store.epicgames.com/graphql",
                                                     json.dumps({"query": EPIC_QUERY, "variables": variables}).encode(),
                                                     {"Content-Type": "application/json"}))
            store = j["data"]["Catalog"]["searchStore"]
        except Exception as e:
            log(f"Epic page at {start} failed: {e}")
            break
        els = store["elements"]
        for e in els:
            slug = e.get("productSlug") or next((m["pageSlug"] for m in e.get("offerMappings") or [] if m.get("pageSlug")), "")
            out.append({"name": e["title"], "id": e["id"], "namespace": e["namespace"], "slug": slug,
                        "tags": [t["name"] for t in e.get("tags") or [] if t.get("name")]})
        total = store["paging"]["total"]
        log(f"Epic: {len(out)} / {total}")
        start += page_size
        if not els or start >= total:
            break
        time.sleep(2)
    return out


def owners_mid(s):
    nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+", s or "")]
    return (nums[0] + nums[-1]) // 2 if nums else 0


def build(log=print):
    steam = steamspy_all(log)
    indie = {int(a) for a in steamspy_genre("Indie")}
    software = set()
    for g in NON_GAMES:
        try:
            software |= {int(a) for a in steamspy_genre(g)}
        except Exception:
            pass
    games_genres = set()
    for g in ("Action", "Adventure", "RPG", "Strategy", "Simulation", "Casual", "Racing", "Sports",
              "Massively Multiplayer", "Free to Play", "Early Access", "Indie"):
        try:
            games_genres |= {int(a) for a in steamspy_genre(g)}
        except Exception:
            pass
    ccu = steam_charts()
    entries, by_name = [], {}
    for appid, g in steam.items():
        if appid in software and appid not in games_genres:
            continue
        name = (g.get("name") or "").strip()
        if not name or is_nintendo(name, g.get("developer", ""), g.get("publisher", "")):
            continue
        owners = owners_mid(g.get("owners"))
        players = max(ccu.get(appid, 0), int(g.get("ccu") or 0))
        reviews = int(g.get("positive") or 0) + int(g.get("negative") or 0)
        # Popularity: owners and reviews, with a strong lift for games people are playing right now.
        score = owners + reviews * 20 + players * 200
        e = {"name": name, "steam": appid, "epic": None, "owners": owners, "ccu": players, "reviews": reviews,
             "indie": appid in indie, "score": score}
        entries.append(e)
        by_name.setdefault(norm(name), e)
    try:
        epic = epic_all(log)
    except Exception as e:
        log(f"Epic skipped: {e}")
        epic = []
    for x in epic:
        key = norm(x["name"])
        if not key or is_nintendo(x["name"]):
            continue
        link = {"slug": x["slug"], "namespace": x["namespace"], "id": x["id"]}
        if key in by_name:
            by_name[key]["epic"] = link
        else:
            e = {"name": x["name"], "steam": None, "epic": link, "owners": 0, "ccu": 0, "reviews": 0,
                 "indie": "Indie" in x["tags"], "score": 0}
            entries.append(e)
            by_name[key] = e
    entries.sort(key=lambda e: -e["score"])
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    tmp = CATALOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built": time.strftime("%Y-%m-%d %H:%M"), "count": len(entries), "games": entries}, f)
    os.replace(tmp, CATALOG)
    log(f"Catalog: {len(entries)} games ({sum(1 for e in entries if e['epic'])} on Epic, "
        f"{sum(1 for e in entries if e['indie'])} indie)")
    return entries


def load():
    try:
        with open(CATALOG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"built": "", "count": 0, "games": []}


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    build(lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
