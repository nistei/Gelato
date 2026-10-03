DESCRIPTION = "Catalog libraries: Gelato adds its folder to a picked library, a movie and a series catalog picking one move their items there on sync, watch state kept, and back when cleared (adds two libraries and scans)"
DESTRUCTIVE = True  # changes the plugin configuration, adds two libraries, runs the catalog import and a scan

from jfapi.api import search_result_id
from jfapi.bootstrap import CATALOG_ITEMS, GELATO, MOVIE_PATH, SERIES_ITEMS, SERIES_PATH

MOVIE_LIB, SERIES_LIB = "Catalog movies jfapi", "Catalog shows jfapi"

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

    def sync(label, only=None):
        """The scheduled task, or with `only` that catalog's own import: the same import, without
        the library scan the task queues after the catalogs."""
        if only:
            t.check(t.import_catalog(only), f"catalog sync ({label}) completed")
            return
        status, msg = api.run_task("GelatoCatalogItemsSync", timeout=1800)
        t.equal(status, "Completed", f"catalog sync ({label}) {msg}")
        t.settle(after=1, timeout=300)  # the scan the import queues

    def scan():
        api.post("/Library/Refresh")
        t.settle(after=1, timeout=300)
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

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

    def gelato_folder(name):
        """What the settings page does when a library is picked: Gelato's folder in it."""
        return api.post(f"/gelato/libraries/{library(name)['ItemId']}/folder")["Path"]

    def rows_of(movie):
        return [r[0] for r in db.query(
            "select lower(replace(ParentId,'-','')) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=?", (movie,))]

    def parent_of(item):
        return db.one("select lower(replace(ParentId,'-','')) from BaseItems where lower(replace(Id,'-',''))=?", (item,))[0]

    def open_from_search(user, movie):
        """Opens the title under the id a search result of the addon carries: what a client holds
        that found the title before the library had it. The search makes Gelato know the id."""
        stremio = db.stremio_id(movie)
        user.search(stremio)
        return user.call("GET", f"/Items/{search_result_id(stremio)}?userId={user.user}")[0]

    added, watched, kept, episode, policy = [], None, None, None, None
    MOVIE_DIR = SERIES_DIR = None
    try:
        # Two empty libraries, as a user creates them, and Gelato's folder in each.
        for name, kind in ((MOVIE_LIB, "movies"), (SERIES_LIB, "tvshows")):
            if library(name) is None:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType={kind}&refreshLibrary=false",
                         {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
                t.log("library created:", name)
        MOVIE_DIR, SERIES_DIR = gelato_folder(MOVIE_LIB), gelato_folder(SERIES_LIB)
        t.log("Gelato's folders:", MOVIE_DIR, SERIES_DIR)
        t.check(MOVIE_DIR in library(MOVIE_LIB).get("Locations", []), "the folder is in the picked library")
        t.check("stub.txt" in t.sh(f"ls '{MOVIE_DIR}'"), "Gelato seeded the folder")
        t.equal(gelato_folder(MOVIE_LIB), MOVIE_DIR, "picking the library again gives the same folder")
        t.check(MOVIE_DIR != SERIES_DIR, "each library gets a folder of its own")
        t.equal(MOVIE_DIR.rsplit("/", 1)[-1], "catalog-movies-jfapi", "the folder is named after the library, lowercase without spaces")
        configure(MOVIE_DIR, SERIES_DIR)
        t.folders_ready(MOVIE_DIR, SERIES_DIR)  # the seeding request saw no folder yet
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "the scan the folder queued finished")
        cat_movies, cat_series = folder_id(MOVIE_DIR), folder_id(SERIES_DIR)
        t.check(cat_movies and cat_series, "the folder items of both catalog folders exist")
        if not (cat_movies and cat_series):
            return
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
        rows = rows_of(probe)
        t.check(rows and all(r == cat_movies for r in rows), f"its {len(rows)} stream rows are in the catalog's folder")
        t.equal(len(listed(MOVIE_DIR, "Movie")), len(lib_before), "its stream rows are not listed as movies of their own")

        # 2. A scan keeps them, a second sync moves nothing.
        scan()
        t.check(in_folder(GELATO_MOVIE, cat_movies) == moved_movies, "a library scan keeps the catalog folder's movies")
        sync("folders unchanged", only=movie_cat)
        t.check(in_folder(GELATO_MOVIE, cat_movies) == moved_movies, "a second sync leaves them where they are")

        # 3. A catalog folder that cannot be found (not in a library yet, or its disk is gone) must
        # not send the catalog's items back to the movie folder.
        t.sh("mkdir -p /tmp/gelato/in-no-library")
        configure("/tmp/gelato/in-no-library", SERIES_DIR)
        sync("movie folder in no library", only=movie_cat)
        t.check(in_folder(GELATO_MOVIE, cat_movies) == moved_movies, "a folder in no library leaves the movies where they are")
        configure(MOVIE_DIR, SERIES_DIR)

        # 4. A user who cannot open the catalog's library opens one of its movies from search: the
        # movie is not pulled out of the library.
        u2 = t.user2
        user = api.get(f"/Users/{u2.user}")
        policy = user["Policy"]
        hidden = library(MOVIE_LIB)["ItemId"]
        api.post(f"/Users/{u2.user}/Policy", {**policy, "EnableAllFolders": False, "EnabledFolders": [
            v["ItemId"] for v in api.get("/Library/VirtualFolders") if v["ItemId"] != hidden]})
        taken = sorted(moved_movies)[1 % len(moved_movies)]
        t.log("opened by the user:", open_from_search(u2, taken))
        api.settle_insert()
        t.equal(parent_of(taken), cat_movies, "a user without access to the library does not pull a movie out of it")
        api.post(f"/Users/{u2.user}/Policy", policy)
        policy = None

        # 5. The catalogs are gone from the configuration (set back, or dropped by the addon) while
        # their items are still in the folders. A series tree sync must not half-move the series,
        # and a movie's streams still land next to it.
        cfg["Catalogs"] = []
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        for sid in moved_series:
            dto = api.get(f"/Items/{sid}?userId={api.user}")
            if dto.get("Status") != "Continuing":  # the tree sync takes continuing series
                api.post(f"/Items/{sid}", {**dto, "Status": "Continuing"})
        t.settle(after=1, timeout=300)
        status, msg = api.run_task("SyncSeriesTrees", timeout=1800)
        t.equal(status, "Completed", f"series tree sync {msg}")
        t.check(in_folder(GELATO_SERIES, cat_series) == moved_series, "the tree sync leaves a series in the folder it is in")
        t.equal(tree_mismatches(moved_series), 0, "and its seasons and episodes with it")
        t.equal(sorted(set(duplicates()) - dupes_before), [], "and creates it nowhere else")
        late = sorted(moved_movies)[-1]
        t.check(len(api.sources(late)) >= 1, "a movie left in a former catalog folder still plays")
        rows = rows_of(late)
        t.check(rows and all(r == cat_movies for r in rows), f"its {len(rows)} stream rows are next to it")

        # 6. The folders are cleared: the items go back to the movie and series folders, with their
        # stream rows and their watch state.
        kept = sorted(moved_movies)[len(moved_movies) // 2]
        api.mark_played(kept)
        row = db.one("select lower(replace(e.Id,'-','')) from BaseItems e join AncestorIds a on a.ItemId=e.Id "
                     "where e.Type like '%TV.Episode' and (e.Tags is null or e.Tags not like '%gelato-stream%') "
                     "and lower(replace(a.ParentItemId,'-',''))=? limit 1", (sorted(moved_series)[0],))
        episode = row[0] if row else None
        if episode:
            api.mark_played(episode)
        configure("", "")
        sync("folders cleared")
        t.check(api.user_data(kept).get("Played"), "a movie played in the catalog's library is still played after moving back")
        t.check(episode and api.user_data(episode).get("Played"), "and so is an episode")
        rows = rows_of(probe)
        t.check(rows and all(r == def_movies for r in rows), f"a movie's {len(rows)} stream rows moved back with it")
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
        leftovers = [f for f in (folder_id(d) for d in (MOVIE_DIR, SERIES_DIR) if d) if f]
        if any(in_folder(GELATO_MOVIE, f) or in_folder(GELATO_SERIES, f) for f in leftovers):
            t.log("items left in the catalog folders, syncing them back before the libraries go")
            configure("", "")
            api.run_task("GelatoCatalogItemsSync", timeout=1800)
        cfg.update(old)
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        if policy:
            api.post(f"/Users/{t.user2.user}/Policy", policy)
        for item in (watched, kept, episode):
            if item:
                api.mark_played(item, False)
        for name in added:
            api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
            t.log("library removed:", name)
        for d in (MOVIE_DIR, SERIES_DIR):
            if d:
                t.sh(f"rm -rf '{d}'")
