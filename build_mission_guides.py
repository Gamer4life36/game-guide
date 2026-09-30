"""One-time pass: for every mission of a game, find and VERIFY the best mission-specific illustrated
guide, and save a guide_url mapping into data/mission_notes.json. Afterwards the reader uses the
mapped URL (no live search) and the fetched content is cached, so each mission opens instantly.

    python build_mission_guides.py "The Last Caretaker" 1783560

Prefers per-mission pages (NoobFeed/thegameslayer "How to complete <mission>", Neoseeker per-mission)
with images, and rejects the generic whole-game walkthrough. Missions with no good per-mission guide
are printed as NEEDS-NOTE so a picture+tip can be hand-added instead. Existing notes are preserved.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8770"
HERE = os.path.dirname(os.path.abspath(__file__))
NOTES = os.path.join(HERE, "data", "mission_notes.json")
# Generic whole-game walkthroughs to avoid mapping to a single mission.
GENERIC = re.compile(r"/walkthrough/?$|/the-last-caretaker/?$|roadmap|/guides/?$", re.I)


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def q(s):
    return urllib.parse.quote(s, safe="")


def candidates(game, appid, step):
    out, seen = [], set()
    def add(url, name):
        u = (url or "").replace("web:", "")
        if u and u not in seen:
            seen.add(u); out.append({"url": u, "name": name or ""})
    try:
        for r in api(f"/api/step_refs?game={q(game)}&step={q(step)}&region="):
            add(r.get("open") or r.get("url"), r.get("title"))
    except Exception:
        pass
    for query in (step, step + " walkthrough", step + " " + game + " how to complete"):
        try:
            for r in api(f"/api/search?game={q(game)}&source=web&appid={q(str(appid))}&q={q(query)}"):
                add(r.get("title"), (r.get("name", "") + " " + r.get("snippet", "")))
        except Exception:
            pass
        time.sleep(1.0)
    return out


def score(step, name, url):
    words = [w for w in re.findall(r"[a-z0-9]+", step.lower()) if len(w) > 3]
    s = sum(2 for w in words if w in (name or "").lower())
    if GENERIC.search(url or ""):
        s -= 5
    if re.search(r"how[- ]to[- ]complete|how[- ]to[- ]bring|/[^/]*" + re.escape(words[0] if words else "x"), (url or ""), re.I):
        s += 1
    return s


def verify(game, appid, url, step):
    """Fetch the candidate and return (ok, images, len) — ok if it has images and looks about this mission."""
    try:
        wp = api(f"/api/page?game={q(game)}&appid={q(str(appid))}&source=web&title={q('web:' + url)}")
    except Exception:
        return (False, 0, 0)
    html = wp.get("html") or ""
    text = re.sub(r"<[^>]+>", " ", html).lower()
    imgs = len(re.findall(r"<img\b", html))
    words = [w for w in re.findall(r"[a-z0-9]+", step.lower()) if len(w) > 3]
    names = sum(1 for w in words if w in text)
    ok = imgs >= 2 and (names >= max(1, len(words) - 1) or ("how to" in text and names >= 1)) and len(html) > 1500
    return (ok, imgs, len(html))


def main():
    game = sys.argv[1] if len(sys.argv) > 1 else "The Last Caretaker"
    appid = sys.argv[2] if len(sys.argv) > 2 else "1783560"
    try:
        notes = json.load(open(NOTES, encoding="utf-8"))
    except Exception:
        notes = {}
    gkey = game.lower()
    notes.setdefault(gkey, {})
    m = api(f"/api/missions?game={q(game)}&appid={q(appid)}")
    missions = [it.get("title") or it.get("anchor") or it.get("label")
                for g in m.get("groups", []) for it in g.get("items", []) if not g.get("local")]
    print(f"{len(missions)} missions for {game}", flush=True)
    mapped = needs = kept = 0
    for i, step in enumerate(missions, 1):
        existing = notes[gkey].get(step) or {}
        if existing.get("guide_url") or existing.get("image"):
            kept += 1; print(f"[{i}/{len(missions)}] {step}: already set", flush=True); continue
        cands = candidates(game, appid, step)
        cands.sort(key=lambda c: score(step, c["name"], c["url"]), reverse=True)
        best = None
        for c in cands[:4]:
            ok, imgs, ln = verify(game, appid, c["url"], step)
            if ok:
                best = (c["url"], imgs); break
        if best:
            notes[gkey].setdefault(step, {})["guide_url"] = best[0]
            mapped += 1
            print(f"[{i}/{len(missions)}] {step}: MAPPED ({best[1]} imgs) {best[0][:70]}", flush=True)
        else:
            needs += 1
            print(f"[{i}/{len(missions)}] {step}: NEEDS-NOTE (no per-mission illustrated guide found)", flush=True)
        tmp = NOTES + ".tmp"
        json.dump(notes, open(tmp, "w", encoding="utf-8"), indent=2)
        os.replace(tmp, NOTES)
        time.sleep(1.5)
    print(f"\nDONE: {mapped} mapped, {kept} already set, {needs} need a hand note.", flush=True)


if __name__ == "__main__":
    main()
