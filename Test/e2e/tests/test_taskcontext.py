DESCRIPTION = "A task started from the dashboard runs to the end after its request has finished, Gelato's tasks and Jellyfin's own; requests keep their listing filters and id lookups"

import subprocess
import time

from jfapi.bootstrap import wait_ready

# Tasks whose first repository query goes through Gelato's decorator. The request that starts one
# (POST /ScheduledTasks/Running/{id}) answers 204 at once and the task keeps its HttpContext; when
# the task reaches that query after the request has finished, reading it failed the run at 0 s.
# On a warm server the task usually wins that race (1 failure in 24 starts under load); right
# after a restart its code is cold and the first starts lost it (2 of the first 3). So every task
# gets a restarted server. SyncSeriesTrees and SyncReleaseDates are Gelato's, DownloadLyrics is
# Jellyfin's own (an audio query, quick on a library without music, the same path the subtitle
# task failed on in prod).
TASKS = ("SyncSeriesTrees", "SyncReleaseDates", "DownloadLyrics")
STARTS = 4
DISPOSED = ("ObjectDisposedException", "HttpContext disposed", "IFeatureCollection has been disposed")
LOGS = "/config/log/log_*.log"


def log_lines(t):
    return int((t.sh(f"cat {LOGS} 2>/dev/null | wc -l") or "0").strip() or 0)


def start_and_wait(t, task, timeout=600):
    """Starts the task over the API and waits for this run's result: (status, seconds, message)."""
    before = (t.api.get(f"/ScheduledTasks/{task['Id']}").get("LastExecutionResult") or {}).get("StartTimeUtc")
    t.api.post(f"/ScheduledTasks/Running/{task['Id']}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        cur = t.api.get(f"/ScheduledTasks/{task['Id']}")
        r = cur.get("LastExecutionResult") or {}
        if cur["State"] == "Idle" and r.get("StartTimeUtc") != before:
            return r.get("Status"), round(time.time() - t0, 1), (r.get("ErrorMessage") or "")[:160]
    return "Running", timeout, f"no new result after {timeout}s"


def stream_tagged(items):
    return [i["Id"] for i in items if any("gelato-stream" in x.lower() for x in i.get("Tags") or [])]


def run(t):
    movie = t.movie()
    row = t.row(movie)
    tasks = {x["Key"]: x for x in t.api.get("/ScheduledTasks")}

    t.log("== tasks started over the API, several times in a row")
    first_line = None
    for key in TASKS:
        task = tasks.get(key)
        if not t.check(task is not None, f"task {key} exists"):
            continue
        subprocess.run(["docker", "restart", t.db.container], capture_output=True)
        t.require(wait_ready(t.api.base, t.log) is not None, f"{t.api.base} is back after the restart")
        t.api.ensure()
        # A restart starts a new log file only on a new day; count from here either way.
        first_line = log_lines(t) if first_line is None else first_line
        results = [start_and_wait(t, task) for _ in range(STARTS)]
        t.log(f"{key}:", results)
        failed = [r for r in results if r[0] != "Completed"]
        t.check(not failed, f"{key}: {STARTS - len(failed)} of {STARTS} starts completed" + (f"; failed {failed[:2]}" if failed else ""))

    new = t.sh(f"cat {LOGS} 2>/dev/null | tail -n +{(first_line or 0) + 1}") or ""
    hits = [line.strip()[:160] for line in new.splitlines() if any(d in line for d in DISPOSED)]
    t.check(not hits, f"no disposed-request error in the server log since the first start ({len(hits)})" + (f": {hits[:3]}" if hits else ""))

    # The same repository filter still has to see a live request: listings hide stream rows,
    # ids the caller names are answered as asked. (A stream row asked for by its own id is not
    # listed either: Jellyfin leaves linked versions out of every list, before Gelato's filter.)
    t.log("== listing filters and id lookups on live requests")
    listing = t.api.get(f"/Items?userId={t.api.user}&Recursive=true&IncludeItemTypes=Movie&Fields=Tags&Limit=5000")
    items = listing.get("Items") or []
    t.check(any(i["Id"] == movie for i in items), "the movie is in the library listing")
    t.equal(stream_tagged(items), [], "the library listing holds no stream row")

    episode = t.episodes()[0]["Id"]
    both = t.api.get(f"/Items?userId={t.api.user}&ids={movie},{episode}").get("Items") or []
    t.equal(sorted(i["Id"] for i in both), sorted([movie, episode]), "ids=<movie>,<episode> answers both")
    single = t.api.get(f"/Items?userId={t.api.user}&ids={movie}").get("Items") or []
    t.equal([i["Id"] for i in single], [movie], "ids=<movie> answers the movie")

    name = t.api.item(movie)["Name"]
    found = t.api.search(name, limit=50, fields="Tags")
    t.check(all(i["Id"] != row for i in found) and not stream_tagged(found),
            f"a search for {name!r} lists no stream row ({len(found)} results)")

    t.log("== media sources and playback info resolve the request's user and source")
    sources = t.api.sources(movie)
    t.check(row in sources, f"the movie lists the row among its {len(sources)} sources")
    pi = t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": row})
    srcs = pi.get("MediaSources") or []
    t.check(any(s["Id"] == row for s in srcs) and not pi.get("ErrorCode"),
            f"PlaybackInfo with MediaSourceId=<row> offers the row ({pi.get('ErrorCode')})")
    t.check(all(not (s.get("Path") or "").lower().startswith("http") for s in srcs),
            "PlaybackInfo stubs every source's path")
