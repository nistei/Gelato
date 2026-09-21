"""Brings an empty Jellyfin instance to the state the tests need: startup wizard done, Gelato
configured against the addon with one small movie catalog and one small series catalog, both of them
creating a collection, the two libraries on Gelato's folders, a library scan, one catalog import.

Gelato itself is not installed: it must be in the container's plugin folder already, e.g. mounted
from a build. The Webhook plugin is installed here, so the event test runs on a fresh instance too.
"""
import json
import subprocess
import time
import urllib.request

from .api import Api

GELATO = "94ea4e14-8163-4989-96fe-0a2094bc2d6a"
WEBHOOK_GUID = "71552a5a-5c5c-4350-a2ae-ebe451a30173"
MOVIE_PATH = "/tmp/gelato/movies"
SERIES_PATH = "/tmp/gelato/series"
CATALOG_ITEMS = 20
SERIES_ITEMS = 1  # a series brings every season and episode; one is enough to see the import work


def wait_ready(base, log, timeout=180):
    """The server answers /System/Info/Public: not yet started (connection refused) or still
    loading (503) are waited out. Returns the info, or None after the timeout.

    Jellyfin 12 answers /System/Info/Public while it is still starting and every other endpoint
    with a 503 page, so a guarded endpoint has to answer too: /Startup/Configuration is 200 before
    the wizard and 401 after it, and 503 only while the server starts."""
    t0, waiting = time.time(), False
    while time.time() - t0 < timeout:
        try:
            st, d = Api(base).call("GET", "/System/Info/Public", timeout=10)
            if st == 200 and isinstance(d, dict):
                st = Api(base).call("GET", "/Startup/Configuration", timeout=10)[0]
                if st not in (None, 503):
                    return d
        except OSError:
            st, d = None, None
        if not waiting:
            log(f"waiting for {base} ({'not reachable' if st is None else f'HTTP {st}'})")
            waiting = True
        time.sleep(3)
    return None


def wizard_done(base):
    st, d = Api(base).call("GET", "/System/Info/Public")
    return st == 200 and bool(d.get("StartupWizardCompleted"))


DEFAULT_PASSWORD = "jfapi"  # Jellyfin refuses an empty password in the wizard and in a reset through the API


def complete_wizard(base, adminuser, adminpassword, log):
    """The startup wizard through its API (open until it is completed). Returns the
    administrator's password: the given one, or DEFAULT_PASSWORD when none was given."""
    api = Api(base)
    api.post("/Startup/Configuration", {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
    api.get("/Startup/User")
    api.post("/Startup/User", {"Name": adminuser, "Password": adminpassword or DEFAULT_PASSWORD})
    api.post("/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    api.post("/Startup/Complete")
    log(f"startup wizard completed, administrator {adminuser}" + ("" if adminpassword else f" with the password {DEFAULT_PASSWORD}"))
    return adminpassword or DEFAULT_PASSWORD


def gelato_libraries(api):
    return [v for v in api.get("/Library/VirtualFolders") if any(p in (MOVIE_PATH, SERIES_PATH) for p in v.get("Locations", []))]


def needs_setup(api):
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    return not cfg.get("Url") or len(gelato_libraries(api)) < 2


def manifest_catalogs(addon_url):
    url = addon_url.rstrip("/")
    if not url.endswith("/manifest.json"):
        url += "/manifest.json"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "jfapi"}), timeout=60) as r:
        return json.load(r).get("catalogs", [])


def install_webhook(api, db, log):
    """Installs the Webhook plugin and restarts the container. Jellyfin loads a plugin only at startup,
    so without this every fresh instance skips the event test. Failures are not fatal: the test skips
    itself when the plugin is missing or not Active."""
    plugins = {p.get("Name"): p for p in api.get("/Plugins")}
    if plugins.get("Webhook", {}).get("Status") == "Active":
        return
    if "Webhook" not in plugins:
        st, _ = api.call("POST", f"/Packages/Installed/Webhook?assemblyGuid={WEBHOOK_GUID}")
        if st not in (200, 204):
            log(f"Webhook plugin not installed (HTTP {st}): the event test will skip itself")
            return
        for _ in range(30):  # the package is downloaded in the background
            time.sleep(2)
            if "Webhook" in {p.get("Name") for p in api.get("/Plugins")}:
                break
        else:
            log("Webhook plugin did not appear after the install: the event test will skip itself")
            return
    # A fresh install comes up Disabled on an instance from the prod dump, where the plugin is off.
    # Enabling needs a restart to take effect, so it happens before the one below.
    plugin = next((p for p in api.get("/Plugins") if p.get("Name") == "Webhook"), None)
    if plugin is not None and plugin.get("Status") != "Active":
        api.call("POST", f"/Plugins/{plugin['Id']}/{plugin['Version']}/Enable")

    subprocess.run(["docker", "restart", db.container], capture_output=True)
    if wait_ready(api.base, log) is None:
        raise RuntimeError(f"{api.base} did not come back after the restart for the Webhook plugin")
    api.ensure()
    status = next((p.get("Status") for p in api.get("/Plugins") if p.get("Name") == "Webhook"), None)
    log(f"Webhook plugin installed, status {status}")


def setup(api, db, addon_url, log):
    install_webhook(api, db, log)  # before the library work: the plugin needs a restart to load
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    catalogs = manifest_catalogs(addon_url)
    chosen = []
    def needs_query(c):  # search catalogs list nothing without a query
        return any(e.get("name") == "search" and e.get("isRequired") for e in c.get("extra") or []) or "search" in (c.get("extraRequired") or [])

    for kind in ("movie", "series"):
        c = next((c for c in catalogs if c.get("type") == kind and not needs_query(c)), None)
        if c:
            chosen.append({"Id": c["id"], "Type": kind, "Name": c.get("name") or c["id"], "Enabled": True,
                           "MaxItems": CATALOG_ITEMS if kind == "movie" else SERIES_ITEMS,
                           # The import creates a collection per catalog, as a configured instance does:
                           # without one a fresh instance has no Gelato BoxSet and never covers what
                           # happens to collections (test_purgeall, prod finding 18).
                           "CreateCollection": True, "Url": ""})
    cfg.update({"Url": addon_url, "MoviePath": MOVIE_PATH, "SeriesPath": SERIES_PATH, "Catalogs": chosen, "CatalogMaxItems": CATALOG_ITEMS})
    api.post(f"/Plugins/{GELATO}/Configuration", cfg)
    log(f"Gelato configured: addon set, catalogs {[(c['Name'], c['MaxItems']) for c in chosen]}")

    db.sh(f"mkdir -p {MOVIE_PATH} {SERIES_PATH}")
    have = {p for v in api.get("/Library/VirtualFolders") for p in v.get("Locations", [])}
    for name, kind, path in (("Movies", "movies", MOVIE_PATH), ("Shows", "tvshows", SERIES_PATH)):
        if path not in have:
            api.post(f"/Library/VirtualFolders?name={name}&collectionType={kind}&paths={path.replace('/', '%2F')}&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
            log(f"library {name} on {path}")
    api.search("gelato")  # Gelato seeds its folders (stub file) when it looks them up
    api.post("/Library/Refresh")
    time.sleep(5)
    api.wait_tasks_idle("RefreshLibrary", 1800)
    folders = db.one("select count(*) from BaseItems where Path in (?, ?) and Type like '%.Folder'", (MOVIE_PATH, SERIES_PATH))[0]
    log(f"library scan done, Gelato folder items: {folders}")
    time.sleep(12)  # Gelato memoizes its folder lookup for 10 s; the seeding search saw no folder yet

    status, msg = api.run_task("GelatoCatalogItemsSync", timeout=3600)
    time.sleep(5)
    api.wait_tasks_idle("RefreshLibrary", 1800)
    movies = db.one("select count(*) from BaseItems where Type like '%Movies.Movie' and (Tags is null or Tags not like '%gelato-stream%')")[0]
    series = db.one("select count(*) from BaseItems where Type like '%TV.Series'")[0]
    boxsets = db.one("select count(*) from BaseItems b where b.Type like '%BoxSet' and exists "
                     "(select 1 from BaseItemProviders p where p.ItemId=b.Id and lower(p.ProviderId)='stremio')")[0]
    log(f"catalog import {status} {msg}: {movies} movies, {series} series, {boxsets} collection(s) in the library")
    settle(db, log)


def settle(db, log, timeout=900):
    """Waits until the server stops writing: the metadata refresh of freshly imported items runs
    in the background for minutes and makes every list query wait on the database meanwhile."""
    size = lambda: db.sh("stat -c %s /config/data/jellyfin.db-wal 2>/dev/null || echo 0").strip()
    last, quiet, t0 = size(), 0, time.time()
    while quiet < 2 and time.time() - t0 < timeout:
        time.sleep(10)
        now = size()
        quiet = quiet + 1 if now == last else 0
        last = now
    log(f"server settled after {time.time() - t0:.0f}s (write-ahead log {int(last or 0) // 1048576} MB)")
