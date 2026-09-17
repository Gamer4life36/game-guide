"""Cloud build entry point (GitHub Actions): refresh the catalog weekly, then build guide indexes
until the time budget runs out.

    python ci.py --minutes 320
"""
import json
import os
import sys
import time

START = time.time()
MINUTES = float(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 320

import catalog   # noqa: E402
import prebuild  # noqa: E402

CATALOG_MAX_AGE = 7 * 86400


def catalog_age():
    try:
        return time.time() - os.path.getmtime(catalog.CATALOG)
    except OSError:
        return float("inf")


def main():
    deadline = START + MINUTES * 60
    if catalog_age() > CATALOG_MAX_AGE:
        print("Refreshing the catalog...", flush=True)
        try:
            catalog.build(lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
        except Exception as e:  # keep building guides from the old catalog
            print("Catalog refresh failed:", e, flush=True)
    done = prebuild.main(deadline=deadline)
    try:
        with open(prebuild.STATUS, encoding="utf-8") as f:
            print("Status:", json.load(f), flush=True)
    except OSError:
        pass
    print(f"Built {done} guide indexes in {(time.time() - START) / 60:.0f} minutes", flush=True)


if __name__ == "__main__":
    main()
