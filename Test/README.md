# Gelato Tests

Python 3, standard library only. The tests require Docker.


## Running against a fresh instance

```shell

# Build the plugin
dotnet build Gelato.csproj -c Release

# Start a throwaway Jellyfin instance with the plugin installed
docker compose -f Test/docker-compose.tests.yml up -d

# Run the tests
# The addon URL is the AIOStreams manifest. Can also be set via ENV JF_ADDON_URL
python Test/e2e/run.py --destructive --addon-url <addon URL>

# throw the instance away
docker compose -f Test/docker-compose.tests.yml down -v
```

The addon URL is the AIOStreams manifest.
The setup creates the administrator `admin` with the password `jfapi` unless given otherwise.


## Running against an existing instance

The instance must run in a Docker container: the checks copy its database out with `docker cp`.
The destructive tests reconfigure the instance, so use a throwaway instance or a backup.

```shell
# Every non-destructive test, server on http://localhost:8096
python Test/e2e/run.py --container <name>                  
python Test/e2e/run.py --container <name> --url http://host:8096 --adminuser admin --adminpassword secret

# Some tests, verbose
python Test/e2e/run.py --container <name> play nextup -v

# Also the tests that reconfigure the instance and are destructive
python Test/e2e/run.py --container <name> --destructive

# Explicit items instead of automatic picks
python Test/e2e/run.py --container <name> play --movie <id> --row <id>
```

## Test structure for agents

Everything lives in `Test/e2e`. `run.py` puts that folder on `sys.path`, so imports are `from jfapi...`.

| Path | What |
|---|---|
| `run.py` | Entry point: parses options, waits for the server, runs the setup on an empty instance (`--addon-url`), preflight (Gelato loaded, database readable), then the selected tests in order. Exit code 1 on any FAIL or ERROR, 2 on a setup problem. |
| `jfapi/api.py` | `Api(base, user, password)`, one logged-in user. `call()` returns `(status, body)` and never raises; `get`/`post`/`delete` raise `ApiError` on a non-2xx status. Helpers: `item`, `sources`, `user_data`, `resume`, `mark_played`, `report` (playback start/progress/stop), `search`, `run_task`, `wait_tasks_idle`. |
| `jfapi/db.py` | `Db(container)`: read-only queries on a snapshot of `jellyfin.db`, copied out with `docker cp` and taken again after every API call. Gelato helpers: `stream_rows`, `row_users`, `stream_row_ids`, `playlist_links`. `sh()` runs a shell command in the container. |
| `jfapi/fixtures.py` | Picks the test items from the instance: movies with at least two streams, a short unwatched series with a streamed season 1. Falls back to inserting a title from the addon's search on a small library. Picks are memoized for the run and never handed out twice. |
| `jfapi/probe.py` | Stream rows that went through Gelato's probe (a video stream in the database), probing more rows of the fixture movies when needed, for tests of Jellyfin's library tasks. Also log line counts and row paths (never log those: debrid URLs carry the API key). |
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
- `t.check` and `t.equal` record a verdict and go on; the test fails if any failed. An exception makes it ERROR.
  `t.skip("why")` for a missing prerequisite (e.g. the Webhook plugin).
- Add the name to `ORDER` in `jfapi/testing.py`; unlisted tests run last, alphabetically. Cheap read-only tests go
  first, the ones that delete, split or scan go late, so a broken instance shows up in the early ones.
- Leave the items as found (mark unplayed, re-insert what was deleted): the fixtures are shared across tests
  within a run. Anything that reconfigures the plugin, adds libraries or runs a library-wide task is `DESTRUCTIVE`
  and restores the previous configuration.
- Tests talk to the server only through the API, as a client would, and read the database to check what the API
  does not show. Never write to the database.

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
- Playback reports use fixed session ids, so a test can run while a real client plays, but not two runs at once
  against the same instance.
