"""Poster grids as Jellyfin Web loads them: 50 library posters (a size nobody asked for yet, then again), and the
posters of search results the library does not hold, which Gelato proxies from the addon's poster URL.

    python tools/perf/images.py [term]
"""
import random
import statistics
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import perflib as p
import play

TERM = sys.argv[1] if len(sys.argv) > 1 else "star"
a = p.api()


def fetch(path):
    st, ttfb, done, data = play.first_bytes(path, n=1 << 24)
    return st, done, len(data)


def grid(label, paths, parallel=6):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(parallel) as ex:
        res = list(ex.map(fetch, paths))
    wall = (time.perf_counter() - t0) * 1000
    ok = [r for r in res if r[0] == 200]
    ms = [r[1] for r in res]
    print(f"{label:52} {len(paths):3} images, {len(ok)} ok, wall {round(wall):6} ms ({parallel} at once), per image median {round(statistics.median(ms)):5} ms "
          f"max {round(max(ms)):5} ms, {round(sum(r[2] for r in res) / 1024):6} KiB")


movies = p.get("/Items?userId={user}&IncludeItemTypes=Movie&Recursive=true&Limit=400&SortBy=SortName&EnableImages=true&ImageTypeLimit=1")["Items"]
with_poster = [m for m in movies if (m.get("ImageTags") or {}).get("Primary")]
random.seed(7)
pick = random.sample(with_poster, 50)
h = random.randint(300, 440)  # a size the image cache does not hold yet
size = f"fillHeight={h}&fillWidth={round(h * 2 / 3)}&quality=96"
paths = [f"/Items/{m['Id']}/Images/Primary?{size}&tag={m['ImageTags']['Primary']}" for m in pick]
grid(f"library posters, new size (h={h})", paths)
grid("library posters, same size again", paths)

res = p.get(f"/Items?userId={{user}}&searchTerm={urllib.parse.quote(TERM)}&IncludeItemTypes=Movie&IncludeItemTypes=Series&Recursive=true&Limit=100"
            "&fields=PrimaryImageAspectRatio&imageTypeLimit=1&enableTotalRecordCount=false")["Items"]
library = {m["Id"] for m in p.get("/Items?userId={user}&IncludeItemTypes=Movie&IncludeItemTypes=Series&Recursive=true&EnableImages=false")["Items"]}
stand_ins = [i for i in res if i["Id"] not in library and (i.get("ImageTags") or {}).get("Primary")]
paths = [f"/Items/{i['Id']}/Images/Primary?fillHeight=446&fillWidth=297&quality=96&tag={i['ImageTags']['Primary']}" for i in stand_ins]
print(f"search '{TERM}': {len(res)} results, {len(stand_ins)} not in the library")
if paths:
    grid("search-result posters (proxied), first", paths)
    grid("search-result posters (proxied), again", paths)
    st, hdrs, body = a.request(paths[0])
    print("  proxied poster headers:", {k: v for k, v in hdrs.items() if k.lower() in ("cache-control", "content-type", "content-length", "etag", "last-modified", "age")}, len(body), "bytes")
    st, hdrs, body = a.request(f"/Items/{pick[0]['Id']}/Images/Primary?{size}&tag={pick[0]['ImageTags']['Primary']}")
    print("  library poster headers:", {k: v for k, v in hdrs.items() if k.lower() in ("cache-control", "content-type", "content-length", "etag", "last-modified", "age")}, len(body), "bytes")
