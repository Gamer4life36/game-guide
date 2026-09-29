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
import urllib.error
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


def _epic_page(start, page_size):
    """One page of Epic's store search. Epic sometimes answers with partial errors ("socket hang up"):
    retry those, and never keep them in the cache."""
    name = f"epic_{start:05d}"
    path = part_path(name)
    bad = False
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            bad = bool(json.load(f).get("errors"))
    if bad:
        os.remove(path)
    variables = {"count": page_size, "start": start, "category": "games/edition/base", "country": "US",
                 "locale": "en-US", "sortBy": "releaseDate", "sortDir": "DESC"}

    def fetch():
        for attempt in range(4):
            page = http_json("https://store.epicgames.com/graphql",
                             json.dumps({"query": EPIC_QUERY, "variables": variables}).encode(),
                             {"Content-Type": "application/json"})
            if not page.get("errors"):
                return page
            time.sleep(3 * (attempt + 1))
        raise ValueError("Epic kept returning errors")

    j = cached_part(name, fetch)
    return ((j.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}


def epic_all(log, page_size=40):
    out = []
    start, total, failures = 0, None, 0
    while total is None or start < total:
        try:
            store = _epic_page(start, page_size)
            failures = 0
        except Exception as e:
            failures += 1
            log(f"Epic page at {start} failed: {e}")
            if total is None or failures >= 5:
                break
            start += page_size  # skip this page, keep the rest
            continue
        els = store.get("elements") or []
        for e in els:
            if not e or not e.get("title"):
                continue  # Epic sometimes returns empty entries
            slug = e.get("productSlug") or next(
                (m["pageSlug"] for m in e.get("offerMappings") or [] if m and m.get("pageSlug")), "")
            out.append({"name": e["title"], "id": e.get("id"), "namespace": e.get("namespace"), "slug": slug,
                        "tags": [t["name"] for t in e.get("tags") or [] if t and t.get("name")]})
        total = (store.get("paging") or {}).get("total", 0)
        if start % 400 == 0:
            log(f"Epic: {len(out)} / {total}")
        start += page_size
        if not els:
            break
        time.sleep(1)
    log(f"Epic: {len(out)} games")
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


def owners_low(s):
    """Low bound of a SteamSpy owners range string, e.g. '1,000,000 .. 2,000,000' -> 1000000."""
    nums = [int(x.replace(",", "")) for x in re.findall(r"[\d,]+", s or "")]
    return nums[0] if nums else 0


def steamspy_cached():
    """Read only the SteamSpy 'all' pages already on disk (no network). Each page = 1000 games
    ranked by owners, so pages 000+001 already give the top ~2000 by owners."""
    games = {}
    for page in range(200):
        p = part_path(f"steamspy_all_{page:03d}")
        if not os.path.exists(p):
            break
        with open(p, encoding="utf-8") as f:
            for appid, g in json.load(f).items():
                games[int(appid)] = g
    return games


def build_from_cache(log=print, limit=1000, min_players=1000):
    """Fast catalog build from cached SteamSpy pages: PC-only, non-Nintendo, at least `min_players`
    owners, ranked by popularity, top `limit`. No slow network refetch. This is the working set;
    the 'has guides' and '>=3 images' cuts are applied per game at prebuild time (see qualifies())."""
    steam = steamspy_cached()
    if not steam:
        log("No cached SteamSpy pages yet — run catalog.py once to fetch them.")
        return []
    try:
        ccu = steam_charts()
    except Exception:
        ccu = {}
    entries = []
    for appid, g in steam.items():
        name = (g.get("name") or "").strip()
        if not name or is_nintendo(name, g.get("developer", ""), g.get("publisher", "")):
            continue
        owners = owners_mid(g.get("owners"))
        low = owners_low(g.get("owners"))
        if low < min_players:                       # drop tiny games (< min_players owners)
            continue
        players = max(ccu.get(appid, 0), int(g.get("ccu") or 0))
        reviews = int(g.get("positive") or 0) + int(g.get("negative") or 0)
        score = owners + reviews * 20 + players * 200
        entries.append({"name": name, "steam": appid, "epic": None, "owners": owners, "ccu": players,
                        "reviews": reviews, "indie": False, "score": score})
    # Popularity order: how many own it, then who's playing now, then how many reviewed it.
    entries.sort(key=lambda e: (-e["owners"], -e["ccu"], -e["reviews"]))
    entries = entries[:limit]
    for i, e in enumerate(entries, 1):
        e["rank"] = i
    os.makedirs(DATA, exist_ok=True)
    tmp = CATALOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built": time.strftime("%Y-%m-%d %H:%M"), "count": len(entries),
                   "top_of": len(steam), "games": entries}, f)
    os.replace(tmp, CATALOG)
    log(f"Catalog (fast): top {len(entries)} of {len(steam)} cached games, each with >= {min_players} owners.")
    return entries


def _appdetails_cached(appid, filt, keyname, parse, ttl=14 * 86400):
    """Fetch Steam appdetails once, cache it — but ONLY on success. On a 429 rate-limit, back off and
    retry; on any failure, return None WITHOUT caching, so a later pass can try again (never cache a
    rate-limited miss as if the game genuinely had nothing)."""
    path = part_path(f"{keyname}_{appid}")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    for attempt in range(5):
        try:
            j = http_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters={filt}")
            data = (j.get(str(appid)) or {}).get("data")
            res = parse(data if isinstance(data, dict) else {})
            with open(path, "w", encoding="utf-8") as f:
                json.dump(res, f)
            return res
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            return None
    return None  # kept getting rate-limited: unknown, do not cache


def screenshot_count(appid, ttl=14 * 86400):
    """How many artwork screenshots Steam lists (cached). Returns None if unknown (rate-limited)."""
    r = _appdetails_cached(appid, "screenshots", "shots",
                           lambda d: {"n": len(d.get("screenshots") or [])}, ttl)
    return None if r is None else r.get("n", 0)


def has_guides(idx):
    """True if a game has guide material: a dedicated wiki, a walkthrough/secrets/side-quest/cheat
    section, a mission list, or decent overall coverage. A dedicated game wiki (Fandom / wiki.gg)
    IS guide material, so its presence alone qualifies."""
    if not idx:
        return False
    if idx.get("wikis"):                       # a dedicated game wiki = guides exist
        return True
    secs = idx.get("sections") or {}
    if any((secs.get(k) or {}).get("items") for k in ("walkthrough", "secrets", "sidequests", "cheats")):
        return True
    if (idx.get("missions") or {}).get("count"):
        return True
    return idx.get("score", 0) >= 40


def requalify_all(log=print, min_images=3, sleep=0.7):
    """Recompute qualifies for every already-built guide index (after changing the rule). Throttled
    (a small sleep between games) so Steam's appdetails doesn't rate-limit; the robust fetch also
    backs off on 429. `ok` may be None (undecided) when Steam stayed unavailable — those are retried
    the next time this runs, never dropped."""
    ok = drop = undecided = 0
    paths = glob_guides()
    for i, path in enumerate(paths):
        try:
            with open(path, encoding="utf-8") as f:
                idx = json.load(f)
        except (OSError, ValueError):
            continue
        appid = idx.get("appid") or ""
        q = qualifies(appid, idx, min_images=min_images)
        idx["qualifies"] = q
        with open(path, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        if q["ok"] is True:
            ok += 1
        elif q["ok"] is False:
            drop += 1
        else:
            undecided += 1
        if sleep and i % 1 == 0:
            time.sleep(sleep)
    log(f"Re-qualified {len(paths)} guides: KEEP {ok}, DROP {drop}, UNDECIDED {undecided}.")
    return ok, drop, undecided


def glob_guides():
    import glob
    return glob.glob(os.path.join(DATA, "guides", "*.json"))


def game_categories(appid, ttl=14 * 86400):
    """Steam's category names (cached), e.g. 'Single-player', 'Multi-player', 'PvP', 'Co-op'.
    Returns None if unknown (rate-limited) so callers don't mistake it for 'no categories'."""
    r = _appdetails_cached(appid, "categories", "cats",
                           lambda d: {"cats": [c.get("description", "") for c in (d.get("categories") or [])]}, ttl)
    return None if r is None else r.get("cats", [])


# Hand-curated keep/drop for games the automatic rule can't judge (e.g. PvPvE-with-missions vs pure PvP).
# Editable at data/qualify_overrides.json: {"keep_appids": [...], "drop_appids": [...]}.
_DEFAULT_OVERRIDES = {
    "keep_appids": ["1808500"],   # ARC Raiders — PvPvE extraction shooter WITH a mission/quest campaign
    "drop_appids": [],
}
def load_overrides():
    path = os.path.join(DATA, "qualify_overrides.json")
    if not os.path.exists(path):
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_DEFAULT_OVERRIDES, f, indent=2)
        except Exception:
            pass
        return _DEFAULT_OVERRIDES
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _DEFAULT_OVERRIDES


def override_for(appid):
    ov = load_overrides()
    a = str(appid)
    if a in {str(x) for x in ov.get("keep_appids", [])}:
        return True
    if a in {str(x) for x in ov.get("drop_appids", [])}:
        return False
    return None


def has_campaign(appid, idx):
    """The user's rule — keep games with a real single-player/PvE mission campaign; drop pure-PvP and
    non-game software. Signal: Steam's 'Single-player' category. Returns True/False, or None if the
    Steam lookup was rate-limited AND no walkthrough/mission was found (so we retry rather than drop).
    Campaign games (CoD, Elden Ring, Hades, GTA, Left 4 Dead) -> True; PvP-only (Apex, CS:GO, PUBG,
    Dota) and software (Wallpaper Engine, Blender) -> False. ARC-Raiders-style PvPvE-with-missions are
    handled by overrides."""
    cats = game_categories(appid)
    if cats is not None:
        return "Single-player" in cats
    # categories unknown (rate-limited): infer from a real walkthrough/mission list, else undecided.
    if (idx.get("missions") or {}).get("count"):
        return True
    if ((idx.get("sections") or {}).get("walkthrough") or {}).get("items"):
        return True
    return None


def qualifies(appid, idx, min_images=3):
    """Keep a game only if it has a single-player/PvE CAMPAIGN (real guides, not PvP/how-to reference)
    AND at least `min_images` artwork images. `ok` is None when Steam data was rate-limited (undecided,
    retry later) so a game is never dropped just because a lookup failed."""
    ov = override_for(appid)
    imgs = screenshot_count(appid)
    if ov is not None:                     # explicit user decision wins
        return {"campaign": ov, "has_guides": ov, "images": imgs, "override": True,
                "ok": bool(ov) and (imgs is None or imgs >= min_images)}
    campaign = has_campaign(appid, idx)
    if campaign is None or imgs is None:   # a signal is unknown -> undecided, don't drop
        return {"campaign": campaign, "has_guides": campaign, "images": imgs, "ok": None}
    return {"campaign": campaign, "has_guides": campaign, "images": imgs,
            "ok": bool(campaign) and imgs >= min_images}


def load():
    try:
        with open(CATALOG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"built": "", "count": 0, "games": []}


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    build(lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
