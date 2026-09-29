"""End-to-end Game Guide pipeline — one command builds and ships the whole catalog.

    python pipeline.py [--limit N] [--no-push]

Stages, in order (self-completing, ~2-3h for the top 1000):
  1. catalog     — rank the top-N PC games by popularity (from cached SteamSpy) -> data/catalog.json
  2. build       — build a guide index per game (walkthroughs/secrets/maps) + KEEP/DROP qualification
  3. requalify   — throttled clean pass: campaign + >=3 images, retries rate-limited games (never
                   drops on a failed lookup)
  4. commit      — force-add the built catalog + guides past .gitignore, commit, and push to origin

Launch it DETACHED (it outlives the launcher and runs to completion on its own):
  PowerShell:  Start-Process -FilePath <python3.12> -ArgumentList 'pipeline.py' -WindowStyle Hidden
Progress: data/pipeline_status.json (+ stdout). Resumable — reruns skip fresh guides.
"""
import json
import os
import subprocess
import sys
import time

import catalog
import prebuild

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATUS = os.path.join(DATA, "pipeline_status.json")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def status(**kw):
    os.makedirs(DATA, exist_ok=True)
    kw["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f)
    os.replace(tmp, STATUS)


def git(*args):
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if out.strip():
        log("git " + args[0] + ": " + out.strip()[:400])
    return r.returncode == 0


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 1000
    push = "--no-push" not in sys.argv
    t0 = time.time()

    log(f"[1/4] catalog: top {limit} by popularity")
    status(stage="catalog", limit=limit)
    catalog.build_from_cache(limit=limit, log=log)

    log("[2/4] building guide indexes (long)…")
    status(stage="build", limit=limit)
    prebuild.main(limit=limit)

    log("[3/4] re-qualifying (throttled, retries rate-limited games)…")
    status(stage="requalify")
    ok, drop, und = catalog.requalify_all(log=log)
    log(f"    KEEP {ok} · DROP {drop} · UNDECIDED {und}")

    log("[4/4] commit + push")
    status(stage="commit", keep=ok, drop=drop, undecided=und)
    git("add", "--", "catalog.py", "prebuild.py", "pipeline.py", "web/app.js", "web/style.css")
    git("add", "-f", "--", "data/catalog.json", "data/guides", "data/qualify_overrides.json")
    msg = (f"Guide catalog: top {limit} PC games — {ok} qualify (single-player campaign + >=3 images), "
           f"{drop} dropped (PvP/software), {und} undecided")
    committed = git("commit", "-m", msg)
    if committed and push:
        git("push", "origin", "main")

    status(stage="done", keep=ok, drop=drop, undecided=und, committed=committed,
           pushed=bool(committed and push), elapsed=int(time.time() - t0))
    log(f"DONE in {int(time.time() - t0)}s — KEEP {ok}, DROP {drop}, UNDECIDED {und}"
        + (" (committed+pushed)" if committed and push else ""))


if __name__ == "__main__":
    main()
