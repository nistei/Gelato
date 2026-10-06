"""Opening a title from search: the first item request inserts it, the page then loads it again and asks for more.

    python tools/perf/insert.py Movie|Series <term> [n] [--keep] [--lines] [--sql]

Needs proxy.py running and dbq.py --pull. Per title: the addon's meta is fetched through the proxy first (so it replays), then the page's requests run as
Jellyfin Web sends them. Prints per-request ms, Gelato's steps from the log and the addon/TMDB requests in the window.
The inserted titles are deleted again unless --keep.
"""
import datetime
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import dbq
import perflib as p
import sqlagg
import trace as tr


def stand_ins(kind, term, limit=40):
    """Search results the library does not hold: [(search id, name)]."""
    have = {uuid.UUID(r[0]).hex for r in dbq.q("select Id from BaseItems")}
    r = p.get(f"/Items?userId={{user}}&searchTerm={urllib.parse.quote(term)}&IncludeItemTypes={kind}&Recursive=true&Limit={limit}&Fields=ProviderIds")
    live = {i["Id"] for i in p.get(f"/Items?userId={{user}}&IncludeItemTypes={kind}&Recursive=true&EnableImages=false").get("Items", [])}
    return [(i["Id"], i["Name"], i.get("ProviderIds") or {}) for i in r["Items"] if i["Id"] not in have and i["Id"] not in live]


def prewarm(kind, provider_ids):
    """The addon's meta for the title through the proxy, so the insert replays it."""
    port = open(os.path.join(p.WORK, "proxy.port")).read().strip()
    ext = provider_ids.get("Imdb") or provider_ids.get("Stremio")
    if not ext:
        return None
    path = f"/meta/{'movie' if kind == 'Movie' else 'series'}/{ext}.json"
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=60) as r:
            r.read()
    except Exception as e:
        return f"{type(e).__name__}"
    return round((time.perf_counter() - t0) * 1000)


def open_title(kind, search_id):
    a = p.api()
    u = a.user
    t0 = p.now_utc()
    steps = {}
    ms, (st, item) = p.timed(lambda: a.call("GET", f"/Users/{u}/Items/{search_id}", timeout=180))
    steps["first item (insert)"] = round(ms)
    if st != 200:
        return steps, None, t0
    real = item["Id"]

    def get(path):
        ms, r = p.timed(lambda: a.call("GET", path, timeout=180))
        return round(ms), r

    # The page: the item three more times, then its rows.
    with ThreadPoolExecutor(8) as ex:
        t1 = time.perf_counter()
        again = [ex.submit(get, f"/Users/{u}/Items/{search_id}") for _ in range(3)]
        rest = {"similar": ex.submit(get, f"/Items/{search_id}/Similar?userId={u}&limit=12&fields=PrimaryImageAspectRatio%2CCanDelete")}
        if kind == "Series":
            rest["seasons"] = ex.submit(get, f"/Shows/{search_id}/Seasons?userId={u}&Fields=ItemCounts%2CPrimaryImageAspectRatio%2CCanDelete%2CMediaSourceCount")
            rest["nextup"] = ex.submit(get, f"/Shows/NextUp?SeriesId={search_id}&UserId={u}&Fields=MediaSourceCount")
        steps["item again x3 (max)"] = max(f.result()[0] for f in again)
        for k, f in rest.items():
            steps[k] = f.result()[0]
        steps["page rest (wall)"] = round((time.perf_counter() - t1) * 1000)
    steps["total"] = steps["first item (insert)"] + steps["page rest (wall)"]
    steps["sources"] = len(item.get("MediaSources") or [])
    return steps, real, t0


LINES = r"Gelato\.|TMDB|tmdb|Refresh|inserted|Intercepted"

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kind, term, n = args[0], args[1], int(args[2]) if len(args) > 2 else 5
    picks = stand_ins(kind, term)[:n]
    print(f"{len(picks)} {kind} results of '{term}' the library does not hold")
    runs, inserted = [], []
    for sid, name, pids in picks:
        warm = prewarm(kind, pids)
        time.sleep(0.5)
        steps, real, t0 = open_title(kind, sid)
        time.sleep(2.5)
        rows = tr.logfile_tail(t0 - datetime.timedelta(milliseconds=5))
        if real:
            inserted.append(real)
            runs.append(steps)
        sql = [r for r in rows if "Executed DbCommand" in r[3]]
        sql_ms = sum(int(re.search(r'\("?(\d+)"?ms\)', r[3]).group(1)) for r in sql)
        print(f"\n{name[:40].encode('ascii', 'replace').decode()}: meta prewarm {warm} ms; {steps}; SQL {len(sql)} cmds {sql_ms} ms", flush=True)
        if "--lines" in sys.argv:
            for ts, lvl, th, txt in rows:
                first = txt.splitlines()[0]
                if "Executed DbCommand" in txt or not re.search(LINES, first) or "GetStaticMediaSources" in first or "Found " in first:
                    continue
                print(f"   {(ts - t0).total_seconds() * 1000:7.0f} [{th:>3}] {p.REDACT.sub('<url>', first)[:220]}")
        if "--sql" in sys.argv:
            sqlagg.agg(rows, 14)
        time.sleep(4)
    if runs:
        keys = [k for k in runs[0] if all(k in r for r in runs)]
        print("\nmedian", {k: round(statistics.median(r[k] for r in runs)) for k in keys})
    if "--keep" not in sys.argv:
        time.sleep(3)
        for real in inserted:
            p.api().call("DELETE", f"/Items/{real}")
        print(f"deleted {len(inserted)} inserted titles")
