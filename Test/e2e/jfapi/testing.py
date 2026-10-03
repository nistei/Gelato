"""The test harness: a context with the API, database, fixtures and checks, and the runner loop.

A test is a module in tests/ with `DESCRIPTION`, an optional `DESTRUCTIVE = True`, an optional
`LAST = True` (it leaves the instance unfit for other tests: nothing runs after it there, and the
failed tests are run again before it), and `run(t)`. It
observes through `t.api` / `t.db`, asks `t.movie()` and friends for items, and records verdicts
with `t.check(condition, "what should hold")`. `t.skip("why")` leaves the test out, `t.log()`
keeps notes that are shown on failure or with -v.

`t.require(condition, "why")` skips when the instance lacks what the test needs (artwork, a plugin,
enough matching items): a missing prerequisite is no failure. `t.known(condition, "what should
hold", "where it is written down")` is a check for a documented, open bug: when it fails the test
ends as KNOWN, not FAIL, and the summary names the record. Anything new still fails.
"""
import importlib
import os
import time
import traceback

from .api import Api, ApiError
from .fixtures import Fixtures

SECOND_USER = "jfapi-second"
FOLDER_MEMO_SECONDS = 10  # GelatoManager.FolderCacheTtl


class Skip(Exception):
    pass


class Context:
    def __init__(self, api, db, fixtures, user2_factory, verbose=False, sink=None):
        self.api, self.db, self.fixtures = api, db, fixtures
        self._user2_factory = user2_factory
        self._user2 = None
        self.verbose = verbose
        self.sink = sink  # takes the notes -v would have printed (the run's file)
        self.lines, self.failures, self.passed = [], [], 0
        self.known_failures = []

    # ---- reporting

    def log(self, *parts):
        line = " ".join(str(p) for p in parts)
        self.lines.append(line)
        if self.verbose:
            print("      " + line)
        elif self.sink:
            self.sink("      " + line)

    def check(self, condition, message):
        if condition:
            self.passed += 1
            self.log("ok  ", message)
        else:
            self.failures.append(message)
            self.log("FAIL", message)
        return bool(condition)

    def equal(self, actual, expected, message):
        return self.check(actual == expected, f"{message}: {actual!r}" + ("" if actual == expected else f", expected {expected!r}"))

    def delivers(self, status, body, source_id, message):
        """A check that a stream route answered with the stream's first byte (`body` None: the
        status alone). When it did not, the stream's own URL is asked: a link that is dead at the
        debrid service fails every route through Jellyfin and says nothing about Gelato, so the
        check is noted and left out. Such a link failed a test about once in two runs of the
        suite, each time with a second run of the test. True, False, or None for a dead link."""
        if status in (200, 206) and (body is None or len(body) == 1):
            return self.check(True, message)
        from .probe import dead_link
        dead = dead_link(self, source_id)
        if dead:
            self.log(f"not judged, the link is dead at the debrid service (its own URL answers {dead}): {message}")
            return None
        return self.check(False, message)

    def skip(self, reason):
        raise Skip(reason)

    def require(self, condition, reason):
        """Skips unless the instance has what the test needs."""
        if not condition:
            raise Skip(reason)

    def known(self, condition, message, record):
        """A check that fails because of a documented open bug (`record` says where): it does not
        fail the test, the test ends as KNOWN."""
        if condition:
            return self.check(True, message)
        self.known_failures.append((message, record))
        self.log("KNOWN", f"{message} [{record}]")
        return False

    # ---- items and users

    def movie(self):
        return self.fixtures.movie()

    def movie2(self):
        return self.fixtures.movie2()

    def movies(self, n):
        return self.fixtures.movies(n)

    def unsynced_movie(self):
        return self.fixtures.unsynced_movie()

    def row(self, movie=None):
        return self.fixtures.row(movie)

    def series(self):
        return self.fixtures.series()

    def episodes(self, series=None, season=1):
        return self.fixtures.episodes(series or self.series(), season)

    @property
    def user2(self):
        """The second user's API (created on the instance when missing)."""
        if self._user2 is None:
            self._user2 = self._user2_factory()
        return self._user2

    def sh(self, cmd):
        return self.db.sh(cmd)

    def wait(self, seconds):
        time.sleep(seconds)

    def settle(self, after=0.0, timeout=30):
        """Returns once the server has stopped writing (no task running, database files unchanged
        over two looks), after at least `after` seconds. Replaces a fixed sleep that waited for a
        task's or a refresh's background writes."""
        time.sleep(after)
        return quiesce(self.api, self.db, lambda m: self.log(m), timeout)

    def folders_ready(self, *paths, timeout=180):
        """Waits for the scan queued for new library folders, and until Gelato sees the folders.
        Gelato memoizes its folder lookup for 10 s, misses too, so a request from before the scan
        can still answer "no folder". That has run out 10 s after the folder items appeared, which
        is mostly over by the time the scan ends: a fixed sleep after the scan waited it twice.
        True when every folder item exists."""
        t0, seen = time.time(), None
        while seen is None and time.time() - t0 < min(timeout, 60):
            self.db.invalidate()
            if all(self.db.one("select count(*) from BaseItems where Path=? and Type like '%.Folder'", (p,))[0] for p in paths):
                seen = time.time()
            else:
                time.sleep(0.5)
        self.settle(after=0.5, timeout=timeout)
        if seen is None:
            return False
        time.sleep(max(0.0, FOLDER_MEMO_SECONDS + 0.5 - (time.time() - seen)))
        return True

    def import_catalog(self, catalog, timeout=600):
        """Imports one catalog as its Import button does and waits for it: the import the scheduled
        task runs for every enabled catalog, without the library scan the task queues after them
        (~9 s on a prod copy, and the test has to wait for it). The endpoint answers at once and
        imports in the background, so the end is read from the log. True when it completed."""
        ended = "cat /config/log/*.log | grep 'CatalogImportService: Catalog .* sync '"
        count = lambda: int(self.sh(ended + " | wc -l").strip() or 0)
        before, t0 = count(), time.time()
        self.api.post(f"/gelato/catalogs/{catalog['Id']}/{catalog['Type']}/import")
        while count() == before and time.time() - t0 < timeout:
            time.sleep(0.2)
        self.settle()
        return count() > before and 'sync "completed"' in self.sh(ended + " | tail -1")


def make_user2(api, name=SECOND_USER, on_call=None):
    """The second user for the multi-user tests, created on the instance when missing (access to
    every library, empty password; the password is reset when a login fails)."""
    def factory():
        u2 = Api(api.base, name, "")
        u2.on_call = on_call
        try:
            return u2.ensure()
        except ApiError:
            pass
        user = next((u for u in api.users() if u["Name"] == name), None)
        if user is None:
            user = api.post("/Users/New", {"Name": name, "Password": ""})
            policy = {**user["Policy"], "EnableAllFolders": True, "EnableMediaPlayback": True, "IsAdministrator": False}
            api.post(f"/Users/{user['Id']}/Policy", policy)
        api.reset_password(user["Id"])
        return u2.login()
    return factory


def quiesce(api, db, log, timeout=180):
    """Waits until the server is idle: no scheduled task running and the database files unchanged
    over two looks half a second apart. A test left a refresh, a scan or a task behind it, and the
    next one ran into it: its counts moved under it and its queries waited on the database.
    Returns the seconds waited; gives up after `timeout` and says so."""
    t0 = time.time()
    last, running = None, []
    while time.time() - t0 < timeout:
        try:
            running = [t["Name"] for t in api.get("/ScheduledTasks") if t.get("State") != "Idle"]
        except ApiError:
            running = []
        except OSError:
            # No answer at all: a test restarted the server, or took it down. That is not idle, and
            # raising here ended the whole run without a summary instead of failing the next test.
            running = ["the server does not answer"]
        now = db.db_stat()
        if not running and now == last:
            return time.time() - t0
        last = now
        time.sleep(0.5)
    log(f"server not idle after {timeout}s (running: {', '.join(running) or 'none'}, database still written), going on")
    return time.time() - t0


def load_tests(tests_dir):
    """{name: module} for tests/test_*.py, in the order of ORDER then alphabetically."""
    names = sorted(f[5:-3] for f in os.listdir(tests_dir) if f.startswith("test_") and f.endswith(".py"))
    order = {n: i for i, n in enumerate(ORDER)}
    names.sort(key=lambda n: (order.get(n, len(ORDER)), n))
    return {n: importlib.import_module(f"tests.test_{n}") for n in names}


ORDER = ["sync", "rowpage", "images", "deadimage", "people", "seasons", "seasonposters", "counts", "lists", "insert", "streamsahead", "useractions", "unopenedgroups", "tasks", "catalogs", "searchfail", "searchtypes", "searchdupes", "localfind", "searchpage", "searchscope", "metaid", "canonicalid", "searchreads", "unreleased", "unreleasedcache", "releasedates", "nativedates", "play", "streamurl", "firstrowid", "escapedurl", "nextup", "users",
         "concurrent", "searchrace", "seedrace", "playlist", "rowops", "apikeydownload", "downloadnosync", "apikeyinsert", "splitmerge", "webhook", "race", "seriesdelete", "subrow", "manualsubs", "upgrade", "purgelog", "scan", "tmpwipe", "localtreescan", "playsubs", "subs", "seriestrees", "addonid", "lockmeta", "localtreetag", "localtreeclean", "localtreeslot", "embeddedtitles", "trickplay", "chapters", "remuxdb", "introsegments", "defaultsegments", "playbackonce", "preprobe", "bingegroup", "peruser", "catalogfolders", "searchlibrary", "libraryfolders", "pickeredge", "catalogauth", "catalogkinds", "cataloggone", "catalogrules", "sharedfolder", "searchcatalog", "scopeaccess", "cataloglibrary", "taskcontext", "purgeall"]


def run_test(name, module, ctx):
    """Runs one test module: (status, seconds). status is ok, FAIL, KNOWN, SKIP or ERROR."""
    t0 = time.time()
    try:
        module.run(ctx)
        status = "FAIL" if ctx.failures else "KNOWN" if ctx.known_failures else "ok"
    except Skip as e:
        ctx.log("skipped:", e)
        status = "SKIP"
    except Exception:
        ctx.log(traceback.format_exc())
        status = "ERROR"
    return status, time.time() - t0
