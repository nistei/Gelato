"""Runs the Gelato checks against a Jellyfin instance running in a Docker container.

    python run.py --addon-url <addon URL>            # a bare instance from Test/docker-compose.tests.yml: set up, then every non-destructive test
    python run.py --container <name>                 # every non-destructive test, server on http://localhost:8096
    python run.py --container <name> --url http://host:8096 --adminuser admin --adminpassword secret
    python run.py --container <name> play nextup -v  # some tests, with their notes
    python run.py --container <name> --destructive   # also the tests that reconfigure the instance
    python run.py list                               # what there is
    python run.py --container <name> play --movie <id> --row <id>   # explicit items instead of picks

Environment variables stand in for the options: JF_CONTAINER, JF_URL, JF_ADMINUSER, JF_ADMINPASSWORD,
JF_ADDON_URL. An empty instance (wizard not completed, or Gelato without addon URL and libraries)
is set up first when --addon-url is given: wizard, Gelato config with one movie and one series
catalog of 20 items, libraries, scan, catalog import. Gelato must already be in the plugin folder.
The tests change the instance (they play, mark, purge and delete things); use a throwaway one. The
run creates a second user for the multi-user tests when it is missing.
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jfapi import bootstrap  # noqa: E402
from jfapi.api import Api  # noqa: E402
from jfapi.db import Db  # noqa: E402
from jfapi.fixtures import Fixtures  # noqa: E402
from jfapi.testing import SECOND_USER, Context, load_tests, make_user2, run_test  # noqa: E402


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


def main():
    p = argparse.ArgumentParser(description="Gelato checks against a Jellyfin instance", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n", 1)[1])
    p.add_argument("tests", nargs="*", help="test names (prefixes work), or 'list'")
    p.add_argument("--container", default=os.environ.get("JF_CONTAINER", "jf-tests"), help="Docker container of the instance, its database is copied out for the checks (default jf-tests, the compose file's)")
    p.add_argument("--url", default=os.environ.get("JF_URL", "http://localhost:8096"), help="server URL (default http://localhost:8096)")
    p.add_argument("--adminuser", default=os.environ.get("JF_ADMINUSER", "admin"), help="administrator (default admin)")
    p.add_argument("--adminpassword", default=os.environ.get("JF_ADMINPASSWORD", ""), help="the administrator's password (default empty)")
    p.add_argument("--addon-url", default=os.environ.get("JF_ADDON_URL"), help="the Stremio addon URL, needed to set up an empty instance (wizard, Gelato config, libraries, a small catalog import)")
    for k in ("movie", "movie2", "row", "series"):
        p.add_argument(f"--{k}", dest=f"fx_{k}", help=f"use this {k} id instead of a random pick")
    p.add_argument("--destructive", action="store_true", help="include the tests that reconfigure the instance (full library scan, library-wide subtitle task, a per-user library)")
    p.add_argument("-v", "--verbose", action="store_true", help="print every test's notes, not only on failure")
    p.add_argument("-x", "--exitfirst", action="store_true", help="stop at the first failure")
    args = p.parse_args()

    os.environ.setdefault("PYTHONUTF8", "1")
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up when the output is a file or pipe
    tests = load_tests(os.path.join(HERE, "tests"))
    if args.tests == ["list"]:
        for name, mod in tests.items():
            print(f"  {name:12} {'(destructive) ' if getattr(mod, 'DESTRUCTIVE', False) else ''}{mod.DESCRIPTION}")
        return 0
    if not args.container:
        print("--container (or JF_CONTAINER) is required: the checks read the instance's database")
        return 2

    selected = []
    for name, mod in tests.items():
        if args.tests:
            if not any(name.startswith(x) for x in args.tests):
                continue
        elif getattr(mod, "DESTRUCTIVE", False) and not args.destructive:
            continue
        selected.append((name, mod))
    unknown = [x for x in args.tests if not any(n.startswith(x) for n in tests)]
    if unknown:
        print("unknown tests:", ", ".join(unknown), "(see 'list')")
        return 2

    say = lambda m: print("  " + m)
    info = bootstrap.wait_ready(args.url, say)
    if info is None:
        print(f"{args.url} did not answer within 3 minutes: is the instance running, and is --url its address?")
        return 2
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

    results = []
    t0 = time.time()
    for i, (name, mod) in enumerate(selected, 1):
        ctx = Context(api, db, fixtures, make_user2(api, on_call=db.invalidate), verbose=args.verbose)
        print(f"[{i:2}/{len(selected)}] {name:12} {mod.DESCRIPTION}")
        status, seconds = run_test(name, mod, ctx)
        results.append((name, status))
        print(f"           -> {status} ({ctx.passed} check(s) passed, {len(ctx.failures)} failed, {seconds:.0f}s)")
        if status in ("FAIL", "ERROR") and not args.verbose:
            for line in ctx.lines:
                print("      " + line)
        if status == "SKIP" and not args.verbose:
            print("      " + ctx.lines[-1])
        if status in ("FAIL", "ERROR") and args.exitfirst:
            break

    counts = {s: sum(1 for _, st in results if st == s) for s in ("ok", "FAIL", "ERROR", "SKIP")}
    print(f"\n{counts['ok']} passed, {counts['FAIL']} failed, {counts['ERROR']} errored, {counts['SKIP']} skipped in {time.time() - t0:.0f}s")
    for name, st in results:
        if st in ("FAIL", "ERROR"):
            print(f"  {st}: {name}")
    return 1 if counts["FAIL"] or counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
