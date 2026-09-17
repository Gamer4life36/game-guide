"""Download the shared guide data built by GitHub Actions (the repo's `data` branch).

Game Guide calls sync_in_background() on start: if the published data is newer than what we have,
the catalog and guide indexes are downloaded and merged (newer copy of each index wins).
"""
import json
import os
import tempfile
import threading
import time
import urllib.request

import publish

REPO = "Gamer4life36/game-guide"
RAW = f"https://raw.githubusercontent.com/{REPO}/data/"
STATE = os.path.join(publish.DATA, "sync.json")
UA = "GameGuide/0.1 (+https://github.com/Gamer4life36/game-guide)"
CHECK_EVERY = 3 * 3600


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sync_once(log=print):
    try:
        remote = json.loads(_get(RAW + "status.json", 30))
    except Exception as e:
        log(f"sync: no published data ({e})")
        return False
    state = _state()
    if remote.get("published", "") <= state.get("published", ""):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("guides.json.gz", "catalog.json.gz"):
            try:
                with open(os.path.join(tmp, name), "wb") as f:
                    f.write(_get(RAW + name))
            except Exception as e:
                log(f"sync: {name} unavailable ({e})")
        publish.unpack(tmp)
    state.update(published=remote.get("published", ""), synced=time.strftime("%Y-%m-%d %H:%M:%S"),
                 guides=remote.get("guides", 0), catalog_count=remote.get("catalog_count", 0))
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)
    log(f"sync: downloaded data published {state['published']} ({state['guides']} guide indexes)")
    return True


def status():
    return _state()


def sync_in_background():
    def loop():
        while True:
            try:
                sync_once(lambda m: None)
            except Exception:
                pass
            time.sleep(CHECK_EVERY)
    threading.Thread(target=loop, daemon=True, name="data-sync").start()
