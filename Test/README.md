# Gelato Tests

Python 3, standard library only. The tests require Docker.


## Running against a fresh instance

```shell

# Build the plugin
dotnet build Gelato.csproj -c Release

# Start a throwaway Jellyfin 12.1 with the plugin installed (Jellyfin writes a meta.json into the
# mounted folder on the first start, so the mount is not read-only)
docker run -d --name jf-tests -p 8096:8096 -v jf-tests-config:/config -v jf-tests-cache:/cache \
  -v "$PWD/bin/Release/net10.0:/config/plugins/Gelato" jellyfin/jellyfin:12.1

# Run the tests
# The addon URL is the AIOStreams manifest. Can also be set via ENV JF_ADDON_URL, or come with the
# instance: an addon URL in its /config/plugins/configurations/Gelato.xml is used when none is given.
python Test/e2e/run.py --container jf-tests --destructive --addon-url <addon URL>

# throw the instance away
docker rm -f jf-tests && docker volume rm jf-tests-config jf-tests-cache
```

The addon URL is the AIOStreams manifest.
The setup creates the administrator `admin` with the password `jfapi` unless given otherwise.
Tests that count Gelato's Debug lines set Gelato to Debug in `/config/config/logging.json` and restart
the server when it does not log at Debug yet: Jellyfin applies a log level only on a restart. An
instance started with that file in place saves them the restarts.


## Running against an existing instance

The instance must run in a Docker container: the checks copy its database out with `docker cp`.
The destructive tests reconfigure the instance, so use a throwaway instance or a backup.

```shell
# Every non-destructive test, server on http://localhost:8096
python Test/e2e/run.py --container <name>                  
python Test/e2e/run.py --container <name> --url http://host:8096 --adminuser admin --adminpassword secret

# Some tests, verbose. A name that is a test runs that test alone (play is not also playlist);
# any other name stands for every test starting with it (search)
python Test/e2e/run.py --container <name> play nextup -v

# Also the tests that reconfigure the instance and are destructive
python Test/e2e/run.py --container <name> --destructive

# Explicit items instead of automatic picks
python Test/e2e/run.py --container <name> play --movie <id> --row <id>
```

Every run keeps its whole output, with every test's notes, in `Test/e2e/.cache/run-<container>.txt`,
whatever `-v` says and wherever stdout went: read a failure there instead of running again.

`python Test/e2e/tools/parallel.py --adminuser <user> --destructive <container> <container> ...` runs the suite
on several instances at the same time (instances built from the same state): each takes its next test from one
shared queue, so the run takes the suite's time divided by the instances whatever a single test does. It prints
each test's verdict as it comes in, and at the end the notes of every failed or flaky test and the totals; each
instance's full output is in `.cache/shard-<n>.txt`.

## Test structure for agents

Everything lives in `Test/e2e`. `run.py` puts that folder on `sys.path`, so imports are `from jfapi...`.

| Path | What |
|---|---|
| `run.py` | Entry point: parses options, waits for the server, runs the setup on an empty instance (`--addon-url`), preflight (Gelato loaded, database readable), then the selected tests in order. Exit code 1 on any FAIL or ERROR, 2 on a setup problem. |
| `jfapi/api.py` | `Api(base, user, password)`, one logged-in user. `call()` returns `(status, body)` and never raises; `get`/`post`/`delete` raise `ApiError` on a non-2xx status. Helpers: `item`, `sources`, `user_data`, `resume`, `mark_played`, `report` (playback start/progress/stop), `search`, `run_task`, `wait_tasks_idle`. |
| `jfapi/db.py` | `Db(container)`: read-only queries on a snapshot of `jellyfin.db`, copied out with `docker cp` and taken again after every API call. Gelato helpers: `stream_rows`, `row_users`, `stream_row_ids`, `playlist_links`. `sh()` runs a shell command in the container. |
| `jfapi/fixtures.py` | Picks the test items from the instance: movies with at least two streams, a short unwatched series with a streamed season 1. Falls back to inserting a title from the addon's search on a small library. Picks are memoized for the run and never handed out twice. |
| `jfapi/probe.py` | Stream rows that went through Gelato's probe (a video stream in the database), probing more rows of the fixture movies when needed, for tests of Jellyfin's library tasks. Also log line counts and row paths (never log those: debrid URLs carry the API key). |
| `jfapi/native.py` | Native libraries for tests of items that are not Gelato's: small video files written with the container's ffmpeg (`write_videos`), a library scanned and waited for (`add_library`), removed again items first (`remove_libraries`, `rows_under`), and the episode tree of a local series that Gelato extends (`open_local_series`, `episode_tree`, `season_numbers`, `gelato_episodes`). |
| `jfapi/bootstrap.py` | Setup of an empty instance: wizard (admin password `jfapi`), Gelato config with one movie catalog (20 items) and one series, libraries on `/tmp/gelato/movies` and `/tmp/gelato/series`, scan, catalog import, then waits until the WAL stops growing. |
| `jfapi/testing.py` | The harness: `Context` (the `t` passed to a test), `load_tests`, `ORDER`, `run_test`, the second user `jfapi-second`. |
| `tests/test_<name>.py` | One test per module. `python Test/e2e/run.py list` prints them with their descriptions. |
| `tools/` | Ad-hoc scripts for one API call (`jf.py`), one query (`db.py`), a movie's state (`state.py`) and single investigations. Configured through `JF_URL`, `JF_ADMINUSER`, `JF_ADMINPASSWORD`, `JF_CONTAINER`; not run by `run.py`. |
| `.cache/` | Login tokens per port and user, database snapshots per process. Ignored by git. Delete the tokens after recreating an instance. |

### Writing a test

A test module has `DESCRIPTION`, optionally `DESTRUCTIVE = True`, and `run(t)`:

```python
DESCRIPTION = "Opening a movie syncs its streams: sources listed, rows linked"


def run(t):
    movie = t.movie()                      # fixture: a Gelato movie with at least two streams
    srcs = t.api.sources(movie)            # opening the item runs Gelato's sync
    t.check(len(srcs) >= 2, "at least two sources")
    rows = t.db.stream_rows(movie)         # fresh snapshot, the API call above invalidated the old one
    t.log("rows in the database:", rows)   # shown on failure or with -v
    t.equal(rows["unowned"], 0, "rows without an owner")
```

- `t.api` is the admin, `t.user2` the second user (created on first use). `t.movie()`, `t.movie2()`, `t.movies(n)`,
  `t.unsynced_movie()`, `t.row(movie)` (a non-first stream row), `t.series()`, `t.episodes(series, season)` give items.
- `t.delivers(status, body, source_id, "what")` for a check that a stream route answered with bytes: when it did
  not and the stream's own URL does not answer either, the link is dead at the debrid service and the check is
  noted, not failed.
- `t.check` and `t.equal` record a verdict and go on; the test fails if any failed. An exception makes it ERROR.
  `t.skip("why")` for a missing prerequisite (e.g. the Webhook plugin).
- `LAST = True` for a test that leaves the instance unfit for the others (`purgeall`): nothing runs after it on
  its instance, and the failed tests get their second run before it.
- Add the name to `ORDER` in `jfapi/testing.py`; unlisted tests run last, alphabetically. Cheap read-only tests go
  first, the ones that delete, split or scan go late, so a broken instance shows up in the early ones.
- Leave the items as found (mark unplayed, re-insert what was deleted): the fixtures are shared across tests
  within a run. Anything that reconfigures the plugin, adds libraries or runs a library-wide task is `DESTRUCTIVE`
  and restores the previous configuration.
- Tests talk to the server only through the API, as a client would, and read the database to check what the API
  does not show. Never write to the database.

### Keeping a test fast

A slow test is almost always waiting, not working: the catalog tests spent about 10 s in their calls and
90 s in sleeps and scans. Before adding a test, and when one takes more than about 30 s:

- **No fixed sleeps.** After anything that leaves background work behind (a task, a scan, a refresh, an item
  update) call `t.settle(after=1)`: it returns once no task runs and the database files stopped changing.
  `time.sleep(5)` followed by `api.wait_tasks_idle(...)` costs the 5 s every time; keep `wait_tasks_idle`
  only as the check after the settle.
- **Poll the thing you wait for** (a search hit, a database row, a log line) every 0.2 to 0.5 s with a timeout,
  instead of sleeping its worst case. `t.sh` and `t.db` cost milliseconds.
- **A new library folder: `t.folders_ready(path, ...)`.** Gelato memoizes its folder lookup for 10 s, misses too.
  The helper waits for the folder items, the scan, and only what is left of the 10 s since the folders appeared.
  `t.wait(12)` after the scan waited the memo a second time.
- **Count the library scans.** One takes about 9 s on a prod copy and the test has to wait for it. The
  `GelatoCatalogItemsSync` task queues one after the catalogs, `POST /gelato/libraries/{id}/folder` one for a new
  folder, `POST /Library/Refresh` is one. Adding or removing a library with `refreshLibrary=false` queues none.
- **A catalog import: `t.import_catalog(catalog)`.** It runs the one catalog's import as its Import button does,
  without the scan. Run the task only where the task or the scan after it is what the test is about
  (`test_tasks`, `test_catalogfolders`, the collection in `test_cataloglibrary`).
- **Give a library-wide task a small library.** A task that walks a library (subtitle download, trickplay,
  chapter images) costs per item, and on a prod copy the Gelato library has thousands. Where the task is switched
  per library, make one of your own, put one movie and one short series into it (the second user's per-user
  folders, opened from search: `test_subs`) and switch the task off on the others for the run. The four task
  runs of `subs` went from one to eight minutes down to 5 to 22 s that way, and stopped failing on a slow addon.
- **A PlaybackInfo that names a stream row probes it.** A remote stream that was never probed costs up to a
  minute, a dead one the whole minute and no sources. Ask for as few rows as the check needs.
- **Import few items.** Every title the library does not have costs a metadata fetch and a refresh: 20 new movies
  took 145 s on a fresh instance. 5 to 8 per catalog are enough.
- **Clean up without leaving work behind.** A scan or task started in a `finally` is paid by the next test as
  idle wait.
- **Measure.** The run's summary lists the slowest tests as `test s / idle wait s / other`; a large idle wait
  belongs to the test before it. `python tools/timing.py <run.py arguments>` runs the same and prints where the
  time went: sleeps by caller, tasks, settles, HTTP calls by path.
- **Give the test its weight.** Add its seconds to `jfapi/weights.json`. A parallel run hands the tests out as
  the instances get free, but it plans its end (the longest of the last tests first, `purgeall` in time) by the
  weights, and an unlisted test counts as the average (about 20 s).

### Gelato in the database

- Ids are GUIDs with dashes in the database and without in the API: compare with `norm()`.
- A stream row is a `BaseItems` row with `gelato-stream` in `Tags`. Its owner is `PrimaryVersionId` (the movie or
  episode), and the owner has a `LinkedChildren` row with `ChildType = 3` (alternate version) per stream row.
  `ExternalId` holds JSON with `userIds`, `index` and `guid`. Rows without an owner or links are legacy rows from
  an older Gelato (`test_upgrade` builds them with a split).
- Every Gelato item has a `BaseItemProviders` row with `ProviderId = 'stremio'`, which is how rows and their title
  are matched.
- In the API the first media source of a movie carries the movie's id (`Type` `Default`), the others are rows
  (`Grouping`).

### Environment

- The instance has to run in Docker: the database is copied out with `docker cp` and `sh()` uses `docker exec`.
- The webhook test listens on port 8765 on the host, `searchfail` runs an addon proxy on port 8766; both need the
  container to reach `host.docker.internal`.
- The server's clock has to be steady. Under Docker Desktop the containers run on the WSL2 VM's clock, and that one
  was seen running 4.9 % fast and being set back by 1.5 s every half minute. Gelato orders "synced" and "reset" by
  the clock, so a step back between the two skipped a sync and failed `upgrade`. The run compares the two clocks
  every two seconds: it says so at the end when they disagree, and on a failed test when the clock was set back
  during it. Another clocksource for the VM is the likely cure (`kernelCommandLine = clocksource=hyperv_clocksource_tsc_page`
  under `[wsl2]` in `%USERPROFILE%\.wslconfig`, then `wsl --shutdown`).
- Playback reports use fixed session ids, so a test can run while a real client plays, but not two runs at once
  against the same instance.
