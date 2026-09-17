"""Background builder: prepares guide indexes for the catalog, most popular games first.

    py -3.12 prebuild.py            # run until the whole catalog is done (resumable)
    py -3.12 prebuild.py --limit 50 # just the top 50

Progress is written to data/prebuild_status.json, which the app shows in the Browse view.
Searches are rate-limited in webguides; when every engine is refusing, the builder waits.
"""
import json
import os
import sys
import time
import traceback

import catalog
import guideindex
import server
import webguides

STATUS = os.path.join(server.DATA, "prebuild_status.json")


def write_status(**kw):
    kw["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f)
    os.replace(tmp, STATUS)


def top_games():
    """The ranked catalog, or Steam's current top 100 while the catalog is still being built."""
    games = catalog.load()["games"]
    if games:
        return games
    ranks = catalog.steam_charts()
    out = []
    for i, appid in enumerate(ranks, 1):
        try:
            j = server.cached_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic",
                                   server.PAGE_TTL)
            name = j[str(appid)]["data"]["name"]
        except Exception:
            continue
        out.append({"name": name, "steam": appid, "rank": i})
    return out


def main(limit=None, deadline=None):
    """Build indexes down the ranking. limit: only the top N. deadline: epoch seconds to stop by (cloud runs)."""
    done = failed = skipped = 0
    started = time.time()
    while True:
        games = top_games()
        if limit:
            games = games[:limit]
        worked = False
        for g in games:
            if deadline and time.time() > deadline:
                write_status(state="paused", reason="run time limit reached", done=done, failed=failed,
                             skipped=skipped, total=len(games), next_rank=g.get("rank"),
                             elapsed=int(time.time() - started))
                return done
            appid = str(g.get("steam") or "")
            if catalog.is_nintendo(g["name"]):
                continue
            old = guideindex.load(g["name"], appid)
            if old and time.time() - old.get("built", 0) < guideindex.INDEX_TTL:
                skipped += 1
                continue
            while all(v > 0 for v in webguides.search_status().values()):
                if deadline and time.time() > deadline:
                    return done
                write_status(state="waiting", reason="search engines are rate-limiting", done=done, failed=failed,
                             current=g["name"], rank=g.get("rank"))
                time.sleep(60)
            write_status(state="building", current=g["name"], rank=g.get("rank"), done=done, failed=failed,
                         skipped=skipped, total=len(games), elapsed=int(time.time() - started))
            try:
                idx = guideindex.build(g["name"], appid, force=True)
                done += 1
                worked = True
                print(time.strftime("%H:%M:%S"), f"#{g.get('rank')} {g['name']}: coverage {idx['score']}", flush=True)
            except Exception as e:
                failed += 1
                print(time.strftime("%H:%M:%S"), f"#{g.get('rank')} {g['name']}: FAILED {e}", flush=True)
                traceback.print_exc(limit=1)
        if deadline:
            write_status(state="idle", done=done, failed=failed, skipped=skipped, total=len(games),
                         elapsed=int(time.time() - started))
            return done
        if limit or not worked:
            # Whole list is fresh: check again later (new catalog, expired indexes).
            write_status(state="idle", done=done, failed=failed, skipped=skipped, total=len(games),
                         elapsed=int(time.time() - started))
            if limit:
                return done
            time.sleep(3600)


if __name__ == "__main__":
    main(limit=int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None)
