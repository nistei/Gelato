DESCRIPTION = "Catalog folders: a movie and a series catalog with a folder of their own move their items there on sync, watch state kept, and back when the folder is cleared (adds two libraries and scans)"
DESTRUCTIVE = True  # changes the plugin configuration, adds two libraries, runs the catalog import and a scan

import time

from jfapi.bootstrap import CATALOG_ITEMS, GELATO, MOVIE_PATH, SERIES_ITEMS, SERIES_PATH

MOVIE_LIB, MOVIE_DIR = "Catalog movies jfapi", "/tmp/gelato/catalog-movies"
SERIES_LIB, SERIES_DIR = "Catalog shows jfapi", "/tmp/gelato/catalog-series"

GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
GELATO_SERIES = "b.Type like '%TV.Series' and b.Path like 'gelato://%'"


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    movie_cat = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "movie"), None)
    series_cat = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "series"), None)
    if movie_cat is None or series_cat is None:
        t.skip("needs a movie and a series catalog configured")
    default_movies = cfg.get("MoviePath") or MOVIE_PATH
    default_series = cfg.get("SeriesPath") or SERIES_PATH

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def in_folder(where, fid):
        return {r[0] for r in db.query(f"select lower(replace(b.Id,'-','')) from BaseItems b where {where} "
                                        "and lower(replace(b.ParentId,'-',''))=?", (fid,))}

    def configure(movie_path, series_path):
        cfg["Catalogs"] = [
            {**movie_cat, "Enabled": True, "MaxItems": CATALOG_ITEMS, "Path": movie_path},
            {**series_cat, "Enabled": True, "MaxItems": SERIES_ITEMS, "Path": series_path},
        ]
        cfg["CatalogMaxItems"] = CATALOG_ITEMS
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    def sync(label):
        status, msg = api.run_task("GelatoCatalogItemsSync", timeout=1800)
        t.equal(status, "Completed", f"catalog sync ({label}) {msg}")
        time.sleep(5)
        api.wait_tasks_idle("RefreshLibrary", 1800)

    def scan():
        api.post("/Library/Refresh")
        time.sleep(5)
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "library scan finished")

    def duplicates():
        """Stremio ids held by more than one Gelato movie or series. Counting items instead does not
        work: the sync also imports what the catalog lists and the library does not have yet."""
        return [r[0] for where in (GELATO_MOVIE, GELATO_SERIES) for r in db.query(
            "select p.ProviderValue from BaseItems b join BaseItemProviders p on p.ItemId=b.Id "
            f"and lower(p.ProviderId)='stremio' where {where} group by 1 having count(*)>1")]

    def listed(folder_path, kind):
        lib = next(v for v in api.get("/Library/VirtualFolders") if folder_path in v.get("Locations", []))
        items = api.get(f"/Items?userId={api.user}&ParentId={lib['ItemId']}&IncludeItemTypes={kind}&Recursive=true&Limit=5000")["Items"]
        return [i["Id"].replace("-", "").lower() for i in items]

    def tree_mismatches(series_ids):
        """Seasons, episodes and their stream rows whose library (TopParentId) is not their series'."""
        if not series_ids:
            return 0
        marks = ",".join("?" * len(series_ids))
        return db.one(
            "select count(*) from BaseItems d join AncestorIds a on a.ItemId=d.Id "
            "join BaseItems s on s.Id=a.ParentItemId "
            f"where lower(replace(s.Id,'-','')) in ({marks}) and coalesce(d.TopParentId,'')<>coalesce(s.TopParentId,'')",
            tuple(series_ids))[0]

    added, watched = [], None
    try:
        # Two libraries on the catalogs' folders. Gelato seeds a catalog's folder when it looks it
        # up, which it does on every request once the configuration names the folder.
        configure(MOVIE_DIR, SERIES_DIR)
        t.sh(f"mkdir -p {MOVIE_DIR} {SERIES_DIR}")
        have = {p for v in api.get("/Library/VirtualFolders") for p in v.get("Locations", [])}
        for name, kind, path in ((MOVIE_LIB, "movies", MOVIE_DIR), (SERIES_LIB, "tvshows", SERIES_DIR)):
            if path not in have:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType={kind}"
                         f"&paths={path.replace('/', '%2F')}&refreshLibrary=false",
                         {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
                t.log("library created:", name, "on", path)
        api.search("gelato")  # a request, so Gelato looks its folders up and seeds them
        t.check(t.sh(f"ls {MOVIE_DIR}").strip(), "Gelato seeded the catalog's folder")
        scan()
        cat_movies, cat_series = folder_id(MOVIE_DIR), folder_id(SERIES_DIR)
        t.check(cat_movies and cat_series, "the folder items of both catalog folders exist")
        if not (cat_movies and cat_series):
            return
        t.wait(12)  # Gelato memoizes its folder lookup for 10 s; the seeding request saw no folder yet
        def_movies, def_series = folder_id(default_movies), folder_id(default_series)

        before_movies = in_folder(GELATO_MOVIE, def_movies)
        dupes_before = set(duplicates())
        watched = next(iter(sorted(before_movies)), None)
        if watched:
            api.mark_played(watched)

        # 1. The folders are set: the catalogs' items move in, nothing is created twice.
        sync("folders set")
        moved_movies = in_folder(GELATO_MOVIE, cat_movies)
        moved_series = in_folder(GELATO_SERIES, cat_series)
        t.log(f"movies in the catalog folder: {len(moved_movies)}, series: {len(moved_series)}")
        t.check(moved_movies, "movies are in the movie catalog's folder")
        t.check(moved_series, "series are in the series catalog's folder")
        t.check(moved_movies & before_movies, "movies imported before the folder was set moved into it (same ids)")
        t.equal(sorted(set(duplicates()) - dupes_before), [], "moving created no movie or series a second time")
        t.equal(tree_mismatches(moved_series), 0, "every season, episode and row is in the library of its series")
        if watched in moved_movies:
            t.check(api.user_data(watched).get("Played"), "a moved movie is still played")
        if not moved_movies:
            return

        # The catalog's library lists its movies, and a moved movie's streams stay with it.
        lib_before = listed(MOVIE_DIR, "Movie")
        t.check(moved_movies <= set(lib_before), "the catalog's library lists its movies")
        probe = sorted(moved_movies)[0]
        n = len(api.sources(probe))  # opening the movie syncs its streams
        t.check(n >= 1, f"a moved movie still plays ({n} sources)")
        rows = db.query("select lower(replace(ParentId,'-','')) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=?", (probe,))
        t.check(all(r[0] == cat_movies for r in rows), f"its {len(rows)} stream rows are in the catalog's folder")
        t.equal(len(listed(MOVIE_DIR, "Movie")), len(lib_before), "its stream rows are not listed as movies of their own")

        # 2. A scan keeps them, a second sync moves nothing.
        scan()
        t.check(in_folder(GELATO_MOVIE, cat_movies) == moved_movies, "a library scan keeps the catalog folder's movies")
        sync("folders unchanged")
        t.check(in_folder(GELATO_MOVIE, cat_movies) == moved_movies, "a second sync leaves them where they are")

        # 3. The folders are cleared: the items go back to the movie and series folders.
        configure("", "")
        sync("folders cleared")
        t.equal(in_folder(GELATO_MOVIE, cat_movies), set(), "no movie is left in the cleared folder")
        t.equal(in_folder(GELATO_SERIES, cat_series), set(), "no series is left in the cleared folder")
        t.check(moved_movies <= in_folder(GELATO_MOVIE, def_movies), "the movies are back in the movie folder")
        t.check(moved_series <= in_folder(GELATO_SERIES, def_series), "the series are back in the series folder")
        t.equal(sorted(set(duplicates()) - dupes_before), [], "moving back created nothing a second time")
        t.equal(tree_mismatches(moved_series), 0, "the series' trees are back in the series library")
        if watched in moved_movies:
            t.check(api.user_data(watched).get("Played"), "a movie moved back is still played")
    finally:
        # Removing a library deletes what is in it: bring anything still there back first.
        leftovers = [f for f in (folder_id(MOVIE_DIR), folder_id(SERIES_DIR)) if f]
        if any(in_folder(GELATO_MOVIE, f) or in_folder(GELATO_SERIES, f) for f in leftovers):
            t.log("items left in the catalog folders, syncing them back before the libraries go")
            configure("", "")
            api.run_task("GelatoCatalogItemsSync", timeout=1800)
        cfg.update(old)
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        if watched:
            api.mark_played(watched, False)
        for name in added:
            api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
            t.log("library removed:", name)
