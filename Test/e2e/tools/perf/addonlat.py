"""Live addon latency, straight at the addon: a term's first search and its repeats. Never prints the URL.

    python tools/perf/addonlat.py "blade runner" godfather ...

Reads the addon URL proxy.py saved (.cache/addon-url-<port>) or, without a proxy, Gelato's configuration.
"""
import concurrent.futures as cf
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

import perflib as p
from jfapi.addon import CACHE

saved = os.path.join(CACHE, f"addon-url-{p.port()}")
up = open(saved).read().strip() if os.path.exists(saved) else p.cfg()["Url"]
base = up[:-len("/manifest.json")] if up.endswith("/manifest.json") else up.rstrip("/")


def fetch(path):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers={"User-Agent": "jfapi"}), timeout=60) as r:
            body, st = r.read(), r.status
    except Exception as e:
        body, st = b"", type(e).__name__
    return round((time.perf_counter() - t0) * 1000), st, body


def search_catalog(kind):
    manifest = json.loads(fetch("/manifest.json")[2])
    return next(c["id"] for c in manifest["catalogs"] if c["type"] == kind and any(e.get("name") == "search" for e in c.get("extra") or []))


M, S = (f"/catalog/{k}/{search_catalog(k)}/search=%s.json" for k in ("movie", "series"))


def search(term):
    q = urllib.parse.quote(term)
    with cf.ThreadPoolExecutor(2) as ex:
        t0 = time.perf_counter()
        a, b = ex.map(fetch, (M % q, S % q))
        both = round((time.perf_counter() - t0) * 1000)
    return both, a, b


if __name__ == "__main__":
    for term in sys.argv[1:]:
        first = search(term)
        rest = [search(term)[0] for _ in range(5)]
        print(f"search '{term}': first both={first[0]} ms (movie {first[1][0]} ms {first[1][1]}, series {first[2][0]} ms {first[2][1]}), "
              f"repeats median {round(statistics.median(rest))} ms {rest}")
