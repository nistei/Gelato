"""Runs the Gelato checks against a Jellyfin instance running in a Docker container.

    python run.py --addon-url <addon URL>            # a bare instance (see Test/README.md): set up, then every non-destructive test
    python run.py --container <name>                 # every non-destructive test, server on http://localhost:8096
    python run.py --container <name> --url http://host:8096 --adminuser admin --adminpassword secret
    python run.py --container <name> play nextup -v  # some tests, with their notes
    python run.py --container <name> search          # every test whose name starts with it
    python run.py --container <name> --destructive   # also the tests that reconfigure the instance
    python run.py list                               # what there is
    python run.py --container <name> play --movie <id> --row <id>   # explicit items instead of picks
    python run.py --container <name> --seed 1234 lockmeta   # the picks lockmeta got in a run with that seed

How a run keeps its results comparable:
- Picks are seeded. The seed is printed at the start; a test run alone with it picks what it
  picked in the run (on the same instance state). Each test gets its own picks, and an item an
  earlier test was handed is only handed out again when the candidates run out.
- Before each test the run waits until the server is idle (no scheduled task, database quiet).
- The addon is recorded: catalogs, metas and the manifest come from `.cache/addon/` after their
  first request, streams always from the addon (--addon live asks the addon for everything,
  --addon refresh records anew).
- A test that fails runs once more alone at the end, on items no other test touched. Passing then
  makes it "flaky", reported but not failing the run; failing again is a failure. --no-rerun skips it.
- Catalogs are limited to a handful of items for the run and get their configured limits back after
  it (--full-catalogs keeps them): two tests run the import as configured, and on a copy of a real
  instance the refresh of hundreds of new items keeps every later test waiting.
- A check for a documented open bug ends as KNOWN; a missing prerequisite (artwork, a plugin) skips.
- The whole output, with every test's notes, is kept in `.cache/run-<container>.txt` whatever -v says
  and wherever stdout goes.

Environment variables stand in for the options: JF_CONTAINER, JF_URL, JF_ADMINUSER, JF_ADMINPASSWORD,
JF_ADDON_URL. An empty instance (wizard not completed, or Gelato without addon URL and libraries)
is set up first when --addon-url is given, or the instance's Gelato config file has one: wizard, Gelato config with one movie and one series
catalog of 20 items, libraries, scan, catalog import. Gelato must already be in the plugin folder.
The tests change the instance (they play, mark, purge and delete things); use a throwaway one. The
run creates a second user for the multi-user tests when it is missing.
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jfapi import addon, bootstrap  # noqa: E402
from jfapi.api import Api  # noqa: E402
from jfapi.db import Db  # noqa: E402
from jfapi.fixtures import Fixtures  # noqa: E402
from jfapi.testing import SECOND_USER, Context, load_tests, make_user2, quiesce, run_test  # noqa: E402


class RunLog:
    """stdout plus the run's own record, .cache/run-<container>.txt. The file gets every test's notes
    whether or not -v shows them, so a run that was piped through `tail`, or run without -v, still
    leaves its failure blocks behind: a 16-minute run had to be repeated twice for want of them."""

    def __init__(self, console, path):
        self.console, self.path = console, path
        self.file = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
        self.console_only = False  # what is printed now is in the file already

    def write(self, text):
        self.console.write(text)
        if not self.console_only:
            self.file.write(text)

    def note(self, line):
        """A line for the file alone."""
        self.file.write(line + "\n")

    def flush(self):
        self.console.flush()
        self.file.flush()


def select(tests, wanted, destructive):
    """[(name, module)] for the names asked for, in run order. A name that is a test selects that test
    alone (`play` is not also playlist, playsubs and playbackonce), any other one every test it starts."""
    selected = []
    for name, mod in tests.items():
        if wanted:
            if not any(name == x or (x not in tests and name.startswith(x)) for x in wanted):
                continue
        elif getattr(mod, "DESTRUCTIVE", False) and not destructive:
            continue
        selected.append((name, mod))
    return selected


def preflight(api, db, selected):
    """The instance has what the tests need: Gelato, the database readable, the Webhook plugin
    when its test is selected (that test skips itself otherwise)."""
    plugins = {p.get("Name"): p.get("Version") for p in api.get("/Plugins")}
    ok = True
    if "Gelato" in plugins:
        print(f"  Gelato {plugins['Gelato']}")
    else:
        print("  Gelato is not installed on the instance")
        ok = False
    if "webhook" in selected:
        print(f"  Webhook {plugins['Webhook']}" if "Webhook" in plugins else "  Webhook plugin missing: the webhook test will be skipped")
    try:
        items = db.one("select count(*) from BaseItems where Tags like '%gelato-stream%'")[0]
        print(f"  database readable: {items} stream rows")
    except Exception as e:
        print(f"  cannot read the database from container {db.container}: {e}")
        ok = False
    return ok


def container_url(container):
    """http://localhost:<port> for the port the container publishes Jellyfin's 8096 on."""
    import subprocess
    r = subprocess.run(["docker", "port", container, "8096/tcp"], capture_output=True, text=True)
    ports = [line.rsplit(":", 1)[-1] for line in r.stdout.split() if ":" in line] if r.returncode == 0 else []
    return f"http://localhost:{ports[0]}" if ports else "http://localhost:8096"


def load_weights():
    """Seconds per test: the committed jfapi/weights.json, overlaid with what tools/parallel.py measured on this
    machine (.cache/weights.json). The queue of a parallel run plans its end with them."""
    with open(os.path.join(HERE, "jfapi", "weights.json"), encoding="utf-8") as h:
        weights = json.load(h)
    try:
        with open(os.path.join(HERE, ".cache", "weights.json"), encoding="utf-8") as h:
            weights.update(json.load(h))
    except (OSError, ValueError):
        pass
    return weights


def main():
    p = argparse.ArgumentParser(description="Gelato checks against a Jellyfin instance", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n", 1)[1])
    p.add_argument("tests", nargs="*", help="test names, or 'list'; a name that is no test stands for every test starting with it")
    p.add_argument("--container", default=os.environ.get("JF_CONTAINER", "jf-tests"), help="Docker container of the instance, its database is read for the checks (default jf-tests, the README's docker run)")
    p.add_argument("--url", default=os.environ.get("JF_URL"), help="server URL (default: the container's published port on localhost, else http://localhost:8096)")
    p.add_argument("--adminuser", default=os.environ.get("JF_ADMINUSER", "admin"), help="administrator (default admin)")
    p.add_argument("--adminpassword", default=os.environ.get("JF_ADMINPASSWORD", ""), help="the administrator's password (default empty)")
    p.add_argument("--addon-url", default=os.environ.get("JF_ADDON_URL"), help="the Stremio addon URL, needed to set up an empty instance (wizard, Gelato config, libraries, a small catalog import)")
    for k in ("movie", "movie2", "row", "series"):
        p.add_argument(f"--{k}", dest=f"fx_{k}", help=f"use this {k} id instead of a random pick")
    p.add_argument("--destructive", action="store_true", help="include the tests that reconfigure the instance (full library scan, library-wide subtitle task, a per-user library)")
    p.add_argument("-v", "--verbose", action="store_true", help="print every test's notes, not only on failure")
    p.add_argument("-x", "--exitfirst", action="store_true", help="stop at the first failure")
    p.add_argument("--seed", type=int, default=None, help="seed for the picks (default: a new one, printed)")
    p.add_argument("--addon", choices=("replay", "live", "refresh"), default="replay",
                   help="replay: catalogs and metas from the recording, streams live (default); live: the addon for everything; refresh: record anew")
    p.add_argument("--queue", metavar="URL", help="take the tests one at a time from tools/parallel.py, which hands the selected ones "
                   "out to all its instances (set by parallel.py)")
    p.add_argument("--no-rerun", action="store_true", help="do not run failed tests again alone")
    p.add_argument("--full-catalogs", action="store_true", help="import the catalogs with their configured limits instead of a handful of items each")
    args = p.parse_args()
    args.url = args.url or container_url(args.container)

    os.environ.setdefault("PYTHONUTF8", "1")
    # UTF-8: names and paths in the notes carry non-ASCII characters, and printing one to a
    # redirected stdout raised UnicodeEncodeError on the Windows code page (test_subs died on
    # the message of a failing check). line_buffering: progress shows up in a file or a pipe.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    tests = load_tests(os.path.join(HERE, "tests"))
    if args.tests == ["list"]:
        for name, mod in tests.items():
            print(f"  {name:12} {'(destructive) ' if getattr(mod, 'DESTRUCTIVE', False) else ''}{mod.DESCRIPTION}")
        return 0
    if not args.container:
        print("--container (or JF_CONTAINER) is required: the checks read the instance's database")
        return 2

    selected = select(tests, args.tests, args.destructive)
    unknown = [x for x in args.tests if not any(n.startswith(x) for n in tests)]
    if unknown:
        print("unknown tests:", ", ".join(unknown), "(see 'list')")
        return 2

    os.makedirs(os.path.join(HERE, ".cache"), exist_ok=True)
    sys.stdout = RunLog(sys.stdout, os.path.join(HERE, ".cache", f"run-{args.container}.txt"))
    say = lambda m: print("  " + m)
    info = bootstrap.wait_ready(args.url, say)
    if info is None:
        print(f"{args.url} did not answer within 3 minutes: is the instance running, and is --url its address?")
        return 2
    if not args.addon_url and args.container:
        args.addon_url = bootstrap.configured_addon_url(args.container)
    if not info.get("StartupWizardCompleted"):
        if not args.addon_url:
            print("the instance is empty (startup wizard not completed): pass --addon-url to set it up")
            return 2
        args.adminpassword = bootstrap.complete_wizard(args.url, args.adminuser, args.adminpassword, say)
    api = Api(args.url, args.adminuser, args.adminpassword).ensure()
    db = Db(args.container)
    api.on_call = db.invalidate
    if bootstrap.needs_setup(api):
        if not args.addon_url:
            print("Gelato is not configured on the instance (no addon URL or no libraries on its folders): pass --addon-url to set it up")
            return 2
        bootstrap.setup(api, db, args.addon_url, say)
    print(f"jfapi: {len(selected)} test(s) against {args.url} ({args.container}) as {args.adminuser}, second user {SECOND_USER}")
    if not preflight(api, db, [n for n, _ in selected]):
        return 2
    overrides = {k: getattr(args, f"fx_{k}") for k in ("movie", "movie2", "row", "series")}
    fixtures = Fixtures(api, db, overrides, log=lambda m: print("  " + m))
    seed = args.seed if args.seed is not None else random.randrange(1, 10**6)
    print(f"  seed {seed} (a test run alone with --seed {seed} picks what it picked here)")

    port = args.url.rstrip("/").rsplit(":", 1)[-1]
    cfg = addon.restore_left_over(api, bootstrap.GELATO, port, say)
    recorder = None
    if args.addon != "live" and cfg.get("Url"):
        recorder = addon.AddonRecorder(cfg["Url"], refresh=args.addon == "refresh")
        reach = db.sh(f"curl -s -m 5 -o /dev/null -w '%{{http_code}}' {recorder.url}").strip()
        if reach == "200":
            addon.switch(api, bootstrap.GELATO, recorder.url, port, keep=cfg["Url"])
            print(f"  addon: catalogs and metas recorded, streams live (proxy on port {recorder.port})")
        else:
            print(f"  addon: the container cannot reach the proxy on port {recorder.port} ({reach or 'no answer'}), asking the addon directly")
            recorder.close()
            recorder = None
    if args.full_catalogs:
        bootstrap.restore_catalogs(api, port)
    else:
        bootstrap.cap_catalogs(api, port, lambda m: print("  " + m))
    try:
        return run_selected(args, api, db, fixtures, selected, seed)
    finally:
        bootstrap.restore_catalogs(api, port)
        if recorder:
            addon.switch(api, bootstrap.GELATO, cfg["Url"], port)
            s = recorder.stats
            print(f"  addon: {s['replayed']} answers replayed, {s['recorded']} recorded, {s['forwarded']} forwarded")
            recorder.close()


TIMINGS = []  # (test, seconds in the test, seconds waiting for an idle server, other harness seconds)


def run_one(args, api, db, fixtures, name, mod, seed, scope, label):
    """Waits for an idle server, then runs one test with its own seeded picks."""
    t_start = time.time()
    waited = quiesce(api, db, lambda m: print("      " + m))
    db.reseed(seed, scope)
    ctx = Context(api, db, fixtures.for_test(scope), make_user2(api, on_call=db.invalidate), verbose=args.verbose,
                  sink=getattr(sys.stdout, "note", None))
    note = f"  (waited {waited:.0f}s for the server)" if waited >= 5 else ""
    print(f"{label} {name:12} {mod.DESCRIPTION}{note}")
    status, seconds = run_test(name, mod, ctx)
    print(f"           -> {status} ({ctx.passed} check(s) passed, {len(ctx.failures)} failed, {seconds:.0f}s)")
    TIMINGS.append((name, seconds, waited, time.time() - t_start - seconds - waited))
    if not args.verbose:  # the notes that explain the verdict; the run's file has them all already
        sys.stdout.console_only = True
        for line in ctx.lines:
            if status in ("FAIL", "ERROR") or (status in ("SKIP", "KNOWN") and line.startswith(("skipped", "KNOWN"))):
                print("      " + line)
        sys.stdout.console_only = False
    return status, ctx


def in_order(selected):
    """The selected tests one after the other: (label, name, module)."""
    for i, (name, mod) in enumerate(selected, 1):
        yield f"[{i:2}/{len(selected)}]", name, mod


def shared_queue(url, worker, selected):
    """The tests tools/parallel.py hands this instance, one at a time until it has none left for it:
    every instance of the run draws from the same list, so none sits idle while another still has
    minutes of tests ahead of it. A test with `LAST = True` is the last one an instance gets."""
    def ask(path, body):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    mods = dict(selected)
    try:
        ask("/tests", {"tests": [[n, bool(getattr(m, "LAST", False))] for n, m in selected]})
        while True:
            a = ask("/next", {"worker": worker})
            if not a.get("test"):
                return
            yield a["label"], a["test"], mods[a["test"]]
    except OSError as e:
        print(f"  the queue at {url} does not answer ({e}): stopping here")


def run_selected(args, api, db, fixtures, selected, seed):
    results, known = {}, []
    t0 = time.time()
    interrupted, again = False, set()

    def rerun_failed():
        # A failure in a run is not yet a finding: the tests before it churned the library. Once more
        # alone, on items nobody touched, tells a bug from interference.
        failed = [(n, m) for n, m in selected if results.get(n) in ("FAIL", "ERROR") and n not in again]
        if failed and not args.no_rerun and not args.exitfirst and len(selected) > 1:
            print(f"\n== {len(failed)} failed test(s) once more, alone")
            for name, mod in failed:
                again.add(name)
                status, _ = run_one(args, api, db, fixtures, name, mod, seed, f"{name} (alone)", "[alone]")
                if status in ("ok", "KNOWN"):
                    results[name] = "flaky"

    try:
        for label, name, mod in shared_queue(args.queue, args.container, selected) if args.queue else in_order(selected):
            if getattr(mod, "LAST", False):
                rerun_failed()  # it leaves no library to run them on
            status, ctx = run_one(args, api, db, fixtures, name, mod, seed, name, label)
            results[name] = status
            known += [(name, m, r) for m, r in ctx.known_failures]
            if status in ("FAIL", "ERROR") and args.exitfirst:
                break
        rerun_failed()
    except KeyboardInterrupt:
        # What ran so far is still worth its summary.
        interrupted = True
        print(f"\n== interrupted after {len(results)} of {len(selected)} test(s)")

    if TIMINGS:
        print("\n  slowest tests (test s / idle wait s / other harness s):")
        for n, sec, w, o in sorted(TIMINGS, key=lambda x: -x[1])[:15]:
            print(f"    {n:14} {sec:6.1f} {w:6.1f} {o:6.1f}")
        print(f"  total: tests {sum(x[1] for x in TIMINGS):.0f}s, idle wait {sum(x[2] for x in TIMINGS):.0f}s, "
              f"other {sum(x[3] for x in TIMINGS):.0f}s")
    counts = {s: sum(1 for st in results.values() if st == s) for s in ("ok", "FAIL", "ERROR", "flaky", "KNOWN", "SKIP")}
    print(f"\n{counts['ok']} passed, {counts['FAIL']} failed, {counts['ERROR']} errored, {counts['flaky']} flaky, "
          f"{counts['KNOWN']} known, {counts['SKIP']} skipped in {time.time() - t0:.0f}s (seed {seed})")
    for name, st in results.items():
        if st in ("FAIL", "ERROR"):
            print(f"  {st}: {name}")
        elif st == "flaky":
            print(f"  flaky: {name} (failed in the run, passed alone)")
    for name, message, record in known:
        print(f"  known: {name}: {message} [{record}]")
    print(f"  output with every test's notes: {sys.stdout.path}")
    return 130 if interrupted else 1 if counts["FAIL"] or counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
