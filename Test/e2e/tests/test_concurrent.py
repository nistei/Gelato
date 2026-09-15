DESCRIPTION = "Parallel first visits of three titles by two users leave one consistent set of rows each"

import threading
import time
from collections import Counter

N = 4


def run(t):
    movies = t.movies(3)
    u1, u2 = t.api, t.user2
    t.log("purging streams so every visit is a first visit")
    t.equal(t.api.run_task("PurgeGelatoStreamsTask")[0], "Completed", "purge streams task")

    results, lock = [], threading.Lock()

    def visit(movie, api):
        t0 = time.time()
        st, d = api.call("GET", f"/Items/{movie}?userId={api.user}")
        n = len(d.get("MediaSources") or []) if isinstance(d, dict) else None
        with lock:
            results.append((movie, api.name, st, n, time.time() - t0))

    threads = [threading.Thread(target=visit, args=(m, api)) for m in movies for _ in range(N) for api in (u1, u2)]
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t.log(f"{len(threads)} requests in {time.time() - t0:.1f}s")
    for m in movies:
        r = [x for x in results if x[0] == m]
        t.log(f"  {m[:8]}: status {dict(Counter(x[2] for x in r))}, sources per user {dict(Counter((x[1], x[3]) for x in r))}")
        t.check(all(x[2] == 200 for x in r), f"{m[:8]} every request answered 200")
        t.check(any(x[3] and x[3] >= 2 for x in r), f"{m[:8]} the syncing user got streams")

    t.log("second round: one visit per user")
    for m in movies:
        a, b = len(u1.sources(m)), len(u2.sources(m))
        t.check(a >= 2 and b >= 2, f"{m[:8]} both users get streams afterwards ({a}, {b})")
        rows = t.db.row_users(m)
        guids = [v[3] for v in rows.values()]
        t.equal(len(set(guids)), len(guids), f"{m[:8]} no duplicate stream guids")
        t.check(all(v[1] == m for v in rows.values()), f"{m[:8]} every row owned by the movie")
        t.equal(t.db.stream_rows(m)["links"], len(rows), f"{m[:8]} one link per row")
