DESCRIPTION = "Deleting stream rows (one row, a movie, Purge streams) removes them and never writes a stream URL to the log"
DESTRUCTIVE = True  # Purge streams deletes every stream row of the instance; the fixture movie is synced again

from jfapi.db import STREAM_TAG

# Jellyfin logs the path of every item it deletes. A row's path is the stream URL, and a debrid
# addon's URL carries the API key, so a row must reach Jellyfin with the redacted path
# (scheme://host/<redacted>). Counted before and after: a dump from prod has old lines in its logs.
DELETE_LINES = "Removing item|Deleting missing alternate version|Deleting item path|Purging stream"
LEAK = "https?://[^/ ]+/[^<]"


def leaks(t):
    """Delete lines in Jellyfin's logs with a URL that goes on past the host."""
    out = t.sh(f"cat /config/log/log_*.log 2>/dev/null | grep -E '{DELETE_LINES}' | grep -cE '{LEAK}'").strip()
    return int(out or 0)


def redacted(t):
    out = t.sh("cat /config/log/log_*.log 2>/dev/null | grep 'Removing item' | grep -c -F '/<redacted>'").strip()
    return int(out or 0)


def rows_total(t):
    return t.db.one("select count(*) from BaseItems where Tags like ?", (STREAM_TAG,))[0]


def exists(t, item_id):
    return t.db.one("select count(*) from BaseItems where lower(replace(Id,'-',''))=?", (item_id,))[0] > 0


def run(t):
    movie = t.movie()
    leaked, shown = leaks(t), redacted(t)
    t.log(f"delete lines with a URL before the test: {leaked}, redacted: {shown}")

    t.log("== one row, deleted through the API")
    row = t.row(movie)
    st, _ = t.api.call("DELETE", f"/Items/{row}")
    t.log(f"DELETE row {row[:8]}: {st}")
    t.equal(st, 204, "the row's delete answered 204")
    t.check(not exists(t, row), "the row is gone from the database")
    t.check(exists(t, movie), "its movie stays")

    other = t.unsynced_movie()
    if other is None:
        t.log("no unsynced movie to delete with its rows, skipping that part")
    else:
        t.log("== a movie with its rows, deleted through the API")
        n = len(t.api.sources(other))
        rows = t.db.stream_rows(other)["count"]
        t.log(f"{other[:8]}: {n} sources, {rows} rows")
        st, _ = t.api.call("DELETE", f"/Items/{other}")
        t.equal(st, 204, "the movie's delete answered 204")
        t.equal(t.db.stream_rows(other)["count"], 0, "none of its rows left")
        t.check(not exists(t, other), "the movie is gone")

    t.log("== Purge streams")
    before = rows_total(t)
    t.check(before > 0, f"rows to purge: {before}")
    status, msg = t.api.run_task("PurgeGelatoStreamsTask", timeout=1800)
    t.equal(status, "Completed", f"purge finished {msg}")
    t.equal(rows_total(t), 0, "no stream row left")
    summary = t.sh("cat /config/log/log_*.log 2>/dev/null | grep -F 'stream purge completed' | tail -1").strip()
    t.log("purge summary:", summary[summary.find("stream purge"):])
    t.check(f"{before} of {before} stream(s) deleted, 0 failed" in summary, "the summary counts every row as deleted")

    t.equal(leaks(t) - leaked, 0, "no delete line with a stream URL")
    t.check(redacted(t) - shown >= before, f"the removals are logged with the redacted path ({redacted(t) - shown})")

    n = len(t.api.sources(movie))
    t.check(n >= 2, f"the fixture movie syncs its streams again: {n} sources")
