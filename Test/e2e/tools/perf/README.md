# Performance measurements

Scripts that time real client requests against a throwaway instance and split the time by step. They are not
tests: nothing here asserts, and `run.py` does not pick them up. They use `jfapi/` read-only.

## Setup

```bash
py dev/jf.py start-container perf                 # a prod copy of its own; deploy the build to measure (docker cp, restart, md5)
cd Test/e2e
python tools/perf/proxy.py                        # in the background: replays the addon's catalogs and metas, streams stay live
python tools/perf/dbq.py --pull                   # a copy of the database for EXPLAIN QUERY PLAN and fixture picks
```

`JF_CONTAINER` names the instance (default `jf-perf`), `JF_ADMINUSER` the user (default `nistei`). Working files go
to `.cache/perf`. Stop the proxy with `echo > .cache/perf/proxy.stop`: it points Gelato back at the addon.

Two things decide whether a number can be believed:

- **Journal mode.** `dev/jf.py dump` leaves the dump's database in `journal_mode=delete`, and Jellyfin keeps what the
  file has. Prod runs WAL. In delete mode a write blocks every reader and costs two fsyncs: `/Sessions/Playing` took
  278 to 322 ms instead of 32 ms, an insert's saves the same way. Put the instance on WAL before measuring anything
  that writes: stop the container, `docker run --rm -v jf-<name>-config:/config python:3-slim python -c "import sqlite3;
  sqlite3.connect('/config/data/jellyfin.db').execute('pragma journal_mode=WAL')"`, start it with `docker start`.
- **Logging.** EF's command log costs about 20 % on a request with hundreds of queries. Take medians with the plain
  `logging.json` and switch to `logging-sql.json` (copy it to `/config/config/logging.json`, `docker restart`) only to
  split a request by step. `py dev/jf.py start-container` writes the plain one back and deploys the primary checkout's
  build again.

The recording proxy closes every connection, and a request Gelato sends down a connection that is just closing is
lost until Gelato's 30 s addon timeout: a single 30 s outlier in a search series is that, not the build.

## Scripts

| Script | What it measures |
|---|---|
| `search.py [n] [filter]` | Search as Jellyfin Web 12.2 and Infuse ask: many results, owned titles, inside a library, page 2, suggestions, the web client's side requests. First call and median of n. |
| `pages.py [n] [filter]` | Home rows, movie, series, season and person pages, library grids, and the home-section scripts' listings, one request at a time. |
| `play.py <item> [direct\|hls] [n]` | A playback start request by request (Intros, item, PlaybackInfo, stream or playlists and first segment, playing report, reload, segments), with Gelato's steps and SQL counted from the log. `--lines` prints the log slice. |
| `insert.py Movie\|Series <term> [n]` | Opening search results the library does not hold: the inserting request, then the page's requests. `--lines`, `--sql`. Deletes what it inserted unless `--keep`. |
| `trace.py GET <path> [--sql]` | One request and its slice of the log file with millisecond offsets; SQL count and time. |
| `slowsql.py [ms] [minutes]` | The slowest SQL shapes in the log, and their text in `.cache/perf/sql` for `EXPLAIN QUERY PLAN` on the copy. |
| `addonlat.py <term> ...` | The addon's own latency for a search, first and repeated, straight at the addon. |
| `seek.py <item> [n]` | A Range request deep into a direct-played stream through Jellyfin and straight at the stream's URL. |
| `seekhls.py <item> [n]` | First segment and a segment at 60 % of an HLS playback with the video copied. |
| `seekffmpeg.py <item> [hh:mm:ss]` | ffmpeg opening the stream's link at a position, per analyzeduration/probesize. |
| `probesize.py [rows]` | ffprobe on stream rows' links per probesize, and what a smaller one changes in the result. |
| `images.py [term]` | 50 library posters in a size nobody asked for yet and again, and the proxied posters of search results the library does not hold, with their response headers. |
| `tasks.py <key>[:timeout] ...` | Scheduled tasks by key (`RefreshLibrary`, `SyncSeriesTrees`, `SyncReleaseDates`, `GelatoCatalogItemsSync`), seconds and Gelato's summary lines. |
| `pick.py` | Titles by the state of the row that plays first (probed, RemuxDB, never probed), from the database copy. |

`perflib.py`, `sqlagg.py` and `dbq.py` are their helpers.

## Secrets

Stream rows' paths and the addon URL carry the debrid key. The scripts never print a URL (`perflib.REDACT` goes over
every log line they show); keep it that way, and do not paste a `Path` from the database copy anywhere.
