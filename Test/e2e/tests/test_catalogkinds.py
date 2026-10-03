DESCRIPTION = "A catalog's library only takes the kind it is for: a movie catalog that picked a shows library keeps its movies in the movies library, and a catalog without its own item limit uses the global one (adds a library, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration, adds a library, runs the catalog import

from jfapi.bootstrap import GELATO, MOVIE_PATH

LIB = "Kinds shows jfapi"
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
LIMIT = 3


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    cat = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "movie"), None)
    t.require(cat, "no movie catalog configured")

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def movies_in(fid):
        return db.one(f"select count(*) from BaseItems b where {GELATO_MOVIE} and lower(replace(b.ParentId,'-',''))=?", (fid,))[0]

    path, created = None, False
    try:
        if library() is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=tvshows&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
            created = True
        path = api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"]
        # MaxItems 0 is "use the global limit", which used to import nothing at all.
        cfg["Catalogs"] = [{**cat, "Enabled": True, "MaxItems": 0, "Path": path}]
        cfg["CatalogMaxItems"] = LIMIT
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        t.folders_ready(path)
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "the scan the folder queued finished")
        shows = folder_id(path)
        t.check(shows, "the shows library's folder exists")
        movies = folder_id(cfg.get("MoviePath") or MOVIE_PATH)
        before = movies_in(movies)

        # The catalog's own import: what the task runs, without the library scan after it.
        t.check(t.import_catalog(cat), "catalog sync completed")
        line = t.sh("grep -h ': processed ' /config/log/*.log | tail -1").strip()
        t.log(line[-170:])
        t.check(f"processed {LIMIT} items" in line, f"a catalog without a limit of its own imports the global {LIMIT}")
        t.equal(movies_in(shows), 0, "no movie went into the shows library")
        t.check(movies_in(movies) >= before, "the movies library kept its movies")
    finally:
        # Removing a library deletes what is in it: bring any movie that did go there back first.
        shows = folder_id(path) if path else None
        if shows and movies_in(shows):
            cfg["Catalogs"] = [{**cat, "Enabled": True, "MaxItems": LIMIT, "Path": ""}]
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            api.run_task("GelatoCatalogItemsSync", timeout=1800)
        cfg.update(old)
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        if created:
            api.call("DELETE", f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&refreshLibrary=false")
        if path:
            t.sh(f"rm -rf '{path}'")
