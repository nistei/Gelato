"""Helpers of the scripts in tools/perf: one logged-in Api on the instance, medians, log slices.

The instance is JF_CONTAINER (default jf-perf), the user JF_ADMINUSER (default nistei, empty password).
Working files (database copy, proxy port, dumped SQL) go to .cache/perf, which git ignores.
"""
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

E2E = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # Test/e2e
sys.path.insert(0, E2E)
WORK = os.path.join(E2E, ".cache", "perf")
os.makedirs(WORK, exist_ok=True)

from jfapi.api import Api  # noqa: E402
from jfapi.bootstrap import GELATO  # noqa: E402

CONTAINER = os.environ.get("JF_CONTAINER", "jf-perf")


def port():
    out = subprocess.run(["docker", "port", CONTAINER, "8096/tcp"], capture_output=True, text=True).stdout
    return out.strip().splitlines()[0].rsplit(":", 1)[-1]


_api = None


def api():
    global _api
    if _api is None:
        _api = Api(f"http://localhost:{port()}", os.environ.get("JF_ADMINUSER", "nistei"), os.environ.get("JF_ADMINPASSWORD", "")).ensure()
    return _api


def timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return (time.perf_counter() - t0) * 1000, r


def med(fn, n=10, label="", quiet=False):
    """First call (cold-ish) and the median of the n calls after it, in ms."""
    first, r = timed(fn)
    ts = []
    for _ in range(n):
        ms, r = timed(fn)
        ts.append(ms)
    out = {"first": round(first), "median": round(statistics.median(ts)), "min": round(min(ts)), "max": round(max(ts))}
    if not quiet:
        print(f"{label:58} first {out['first']:6} ms   median {out['median']:6} ms   min {out['min']:6}   max {out['max']:6}")
    return out, r


def get(path, timeout=120):
    a = api()
    st, d = a.call("GET", path, timeout=timeout)
    if st >= 300:
        raise RuntimeError(f"GET {path[:120]} -> {st} {str(d)[:200]}")
    return d


def post(path, body=None, timeout=120):
    a = api()
    st, d = a.call("POST", path, body, timeout=timeout)
    if st >= 300:
        raise RuntimeError(f"POST {path[:120]} -> {st} {str(d)[:200]}")
    return d


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


REDACT = re.compile(r"https?://[^\" ]+")


def logs(since, grep=None):
    """Container log lines since the datetime, URLs redacted."""
    out = subprocess.run(["docker", "logs", "--since", since.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), CONTAINER],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = (out.stdout + out.stderr).splitlines()
    lines = [REDACT.sub("<url>", l) for l in lines]
    if grep:
        rx = re.compile(grep)
        lines = [l for l in lines if rx.search(l)]
    return lines


def sh(cmd):
    return subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", cmd], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def cfg():
    return get(f"/Plugins/{GELATO}/Configuration")


def views():
    """{collection type: library id} of the user's libraries."""
    return {v.get("CollectionType"): v["Id"] for v in get("/UserViews?userId={user}")["Items"]}


def biggest_series():
    """(series id, id of its first numbered season): the series with the most episodes."""
    s = get("/Items?userId={user}&IncludeItemTypes=Series&Recursive=true&Limit=500&Fields=RecursiveItemCount&EnableImages=false")["Items"]
    show = max(s, key=lambda i: i.get("RecursiveItemCount") or 0)["Id"]
    seasons = get(f"/Shows/{show}/Seasons?userId={{user}}")["Items"]
    season = next((x["Id"] for x in seasons if x.get("IndexNumber")), seasons[0]["Id"])
    return show, season


def movie_with_streams():
    """A movie that lists several stream rows (PERF_MOVIE names one)."""
    if os.environ.get("PERF_MOVIE"):
        return os.environ["PERF_MOVIE"]
    for m in get("/Items?userId={user}&IncludeItemTypes=Movie&Recursive=true&Limit=40&SortBy=DatePlayed&SortOrder=Descending&EnableImages=false")["Items"]:
        if len(get(f"/Items/{m['Id']}?userId={{user}}&Fields=Path").get("MediaSources") or []) > 2:
            return m["Id"]
    raise RuntimeError("no movie with stream rows found: set PERF_MOVIE")
