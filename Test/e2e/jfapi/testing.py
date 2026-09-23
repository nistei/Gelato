"""The test harness: a context with the API, database, fixtures and checks, and the runner loop.

A test is a module in tests/ with `DESCRIPTION`, an optional `DESTRUCTIVE = True`, and `run(t)`. It
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


class Skip(Exception):
    pass


class Context:
    def __init__(self, api, db, fixtures, user2_factory, verbose=False):
        self.api, self.db, self.fixtures = api, db, fixtures
        self._user2_factory = user2_factory
        self._user2 = None
        self.verbose = verbose
        self.lines, self.failures, self.passed = [], [], 0
        self.known_failures = []

    # ---- reporting

    def log(self, *parts):
        line = " ".join(str(p) for p in parts)
        self.lines.append(line)
        if self.verbose:
            print("      " + line)

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
    over two looks two seconds apart. A test left a refresh, a scan or a task behind it, and the
    next one ran into it: its counts moved under it and its queries waited on the database.
    Returns the seconds waited; gives up after `timeout` and says so."""
    t0 = time.time()
    stat = "stat -c '%n %s %y' /config/data/jellyfin.db* 2>/dev/null"
    last, running = None, []
    while time.time() - t0 < timeout:
        try:
            running = [t["Name"] for t in api.get("/ScheduledTasks") if t.get("State") != "Idle"]
        except ApiError:
            running = []
        now = db.sh(stat)
        if not running and now == last:
            return time.time() - t0
        last = now
        time.sleep(2)
    log(f"server not idle after {timeout}s (running: {', '.join(running) or 'none'}, database still written), going on")
    return time.time() - t0


def load_tests(tests_dir):
    """{name: module} for tests/test_*.py, in the order of ORDER then alphabetically."""
    names = sorted(f[5:-3] for f in os.listdir(tests_dir) if f.startswith("test_") and f.endswith(".py"))
    order = {n: i for i, n in enumerate(ORDER)}
    names.sort(key=lambda n: (order.get(n, len(ORDER)), n))
    return {n: importlib.import_module(f"tests.test_{n}") for n in names}


ORDER = ["sync", "rowpage", "images", "deadimage", "people", "seasons", "seasonposters", "counts", "lists", "insert", "useractions", "tasks", "catalogs", "searchfail", "searchtypes", "searchdupes", "localfind", "searchpage", "searchscope", "metaid", "canonicalid", "searchreads", "unreleased", "releasedates", "nativedates", "play", "streamurl", "escapedurl", "nextup", "users",
         "concurrent", "searchrace", "playlist", "rowops", "apikeydownload", "apikeyinsert", "splitmerge", "webhook", "race", "seriesdelete", "subrow", "upgrade", "purgelog", "scan", "localtreescan", "playsubs", "subs", "seriestrees", "addonid", "lockmeta", "localtreetag", "localtreeclean", "embeddedtitles", "trickplay", "chapters", "peruser", "taskcontext", "purgeall"]


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
