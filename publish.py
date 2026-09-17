"""Pack / unpack the shared guide data (catalog + guide indexes) as compressed bundles.

The cloud builder (GitHub Actions) publishes these to the repo's `data` branch; the app downloads
them with sync.py, so nobody has to build the whole catalog on their own machine.

    python publish.py pack out/     # data/ -> out/{catalog.json.gz, guides.json.gz, status.json}
    python publish.py unpack DIR    # DIR/{...} -> data/  (keeps whichever copy of each index is newer)
"""
import glob
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
GUIDES = os.path.join(DATA, "guides")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_gz(path, obj):
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(obj, f, separators=(",", ":"))


def _read_gz(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pack(out):
    os.makedirs(out, exist_ok=True)
    guides = {}
    for f in glob.glob(os.path.join(GUIDES, "*.json")):
        d = _read_json(f)
        if d:
            guides[os.path.basename(f)[:-5]] = d
    _write_gz(os.path.join(out, "guides.json.gz"), guides)
    cat = _read_json(os.path.join(DATA, "catalog.json"))
    if cat:
        _write_gz(os.path.join(out, "catalog.json.gz"), cat)
    status = _read_json(os.path.join(DATA, "prebuild_status.json")) or {}
    status.update(published=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), guides=len(guides),
                  catalog_built=(cat or {}).get("built", ""), catalog_count=(cat or {}).get("count", 0))
    with open(os.path.join(out, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, indent=1)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write("# Game Guide data\n\nBuilt automatically by GitHub Actions: the ranked Steam + Epic catalog and a guide "
                "index per game (links to walkthroughs, side quests, secrets, maps and single-player cheats). "
                "Game Guide downloads these bundles when it starts.\n")
    print(f"packed {len(guides)} guide indexes, catalog {status['catalog_count']} games")


def unpack(src):
    os.makedirs(GUIDES, exist_ok=True)
    added = 0
    guides = _read_gz(os.path.join(src, "guides.json.gz")) or {}
    for key, d in guides.items():
        path = os.path.join(GUIDES, key + ".json")
        old = _read_json(path)
        if old and old.get("built", 0) >= d.get("built", 0):
            continue
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
        added += 1
    cat = _read_gz(os.path.join(src, "catalog.json.gz"))
    local = _read_json(os.path.join(DATA, "catalog.json"))
    updated = bool(cat and (not local or cat.get("built", "") > local.get("built", "")))
    if updated:
        tmp = os.path.join(DATA, "catalog.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cat, f)
        os.replace(tmp, os.path.join(DATA, "catalog.json"))
    print(f"unpacked {added} newer guide indexes of {len(guides)}; catalog {'updated' if updated else 'unchanged'}")
    return added


if __name__ == "__main__":
    cmd, where = sys.argv[1], sys.argv[2]
    pack(where) if cmd == "pack" else unpack(where)
