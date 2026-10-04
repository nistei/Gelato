DESCRIPTION = "Catalog moves keep to their rules: an item in a user's own folder stays there, a catalog's movies follow it out of a library another catalog got, and movies opened while a sync moves them keep their stream rows with them (adds libraries, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration and a user's access, adds libraries, runs the catalog import

import threading

from jfapi.api import search_result_id
from jfapi.bootstrap import GELATO, MOVIE_PATH

LIB_A, LIB_B, LIB_USER = "Rules first jfapi", "Rules second jfapi", "Rules user jfapi"
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
ITEMS = 8


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems", "UserConfigs")}
    movie_cats = [c for c in cfg.get("Catalogs", []) if c.get("Type") == "movie"]
    t.require(len(movie_cats) >= 2, "needs two movie catalogs configured")
    first, second = movie_cats[0], movie_cats[1]
    default_path = cfg.get("MoviePath") or MOVIE_PATH

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def in_folder(fid):
        return {r[0] for r in db.query(f"select lower(replace(b.Id,'-','')) from BaseItems b where {GELATO_MOVIE} "
                                        "and lower(replace(b.ParentId,'-',''))=?", (fid,))}

    def parent_of(item):
        return db.one("select lower(replace(ParentId,'-','')) from BaseItems where lower(replace(Id,'-',''))=?", (item,))[0]

    def rows_of(movie):
        return [r[0] for r in db.query(
            "select lower(replace(ParentId,'-','')) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=?", (movie,))]

    def configure(first_path, second_path, users=None):
        cfg["Catalogs"] = [
            {**first, "Enabled": True, "MaxItems": ITEMS, "Path": first_path},
            {**second, "Enabled": False, "MaxItems": ITEMS, "Path": second_path},
        ]
        cfg["CatalogMaxItems"] = ITEMS
        if users is not None:
            cfg["UserConfigs"] = users
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    def sync(label):
        # The first catalog is the only enabled one: its own import is what the task would run,
        # without the library scan after it (test_catalogfolders runs the task).
        t.check(t.import_catalog(first), f"catalog sync ({label}) completed")

    def open_from_search(user, movie):
        """Opens the title under the id a search result of the addon carries: what a client holds
        that found the title before the library had it. The search makes Gelato know the id."""
        stremio = db.stremio_id(movie)
        user.search(stremio)
        return user.call("GET", f"/Items/{search_result_id(stremio)}?userId={user.user}")[0]

    added, dirs, policy = [], {}, None
    u2 = t.user2
    try:
        for name in (LIB_A, LIB_B, LIB_USER):
            if library(name) is None:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType=movies&refreshLibrary=false",
                         {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
            dirs[name] = api.post(f"/gelato/libraries/{library(name)['ItemId']}/folder")["Path"]
        users = [u for u in (old["UserConfigs"] or []) if u["UserId"].replace("-", "").lower() != u2.user.replace("-", "").lower()]
        users.append({"UserId": u2.user, "Url": cfg["Url"], "MoviePath": dirs[LIB_USER],
                      "SeriesPath": cfg.get("SeriesPath"), "DisableSearch": False})
        configure("", "", users)
        t.folders_ready(*dirs.values())
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "the scan the folders queued finished")
        a, b, own, default = (folder_id(dirs[LIB_A]), folder_id(dirs[LIB_B]), folder_id(dirs[LIB_USER]), folder_id(default_path))
        t.check(a and b and own and default, "all four folders are in the library")

        # 1. A user's own folder. The user cannot open the default movies library, so opening one of
        # its titles from search puts it into the user's folder, as it always has.
        policy = api.get(f"/Users/{u2.user}")["Policy"]
        hidden = next(v["ItemId"] for v in api.get("/Library/VirtualFolders") if default_path in v.get("Locations", []))
        api.post(f"/Users/{u2.user}/Policy", {**policy, "EnableAllFolders": False, "EnabledFolders": [
            v["ItemId"] for v in api.get("/Library/VirtualFolders") if v["ItemId"] != hidden]})

        # 2. Everything the first catalog lists moves into its library: the items it lists are the
        # ones that are there afterwards.
        configure(dirs[LIB_A], "")
        sync("first catalog picks its library")
        moved = in_folder(a)
        t.check(moved, "the first catalog's movies are in its library")
        if not moved:
            return
        mine = sorted(moved)[0]
        configure("", "")
        sync("back to the default folder")
        t.check(mine in in_folder(default), "and back in the default folder")
        t.log("opened by the user:", open_from_search(u2, mine))
        api.settle_insert()
        t.equal(parent_of(mine), own, "a title opened by a user with a folder of their own is in that folder")
        api.post(f"/Users/{u2.user}/Policy", policy)
        policy = None

        # 3. The first catalog's movies are in a library the second catalog then gets, while the
        # first one picks another: they are the first catalog's (test_catalogown has the marks), so
        # they follow it out of there. What is in the user's folder stays.
        configure(dirs[LIB_B], "")
        sync("first catalog fills the other library")
        theirs = in_folder(b)
        t.check(theirs, "movies are in the library the second catalog gets")
        t.check(mine not in theirs, "the movie in the user's folder was left there")

        # Stream rows for a few of them, and a client that keeps opening them while the sync moves.
        opened = sorted(theirs)[:4]
        for movie in opened:
            api.sources(movie)
        configure(dirs[LIB_A], dirs[LIB_B])
        stop = threading.Event()

        def keep_opening():
            while not stop.is_set():
                for movie in opened:
                    api.call("GET", f"/Items/{movie}?userId={api.user}")

        thread = threading.Thread(target=keep_opening, daemon=True)
        thread.start()
        try:
            sync("moves while the movies are opened")
        finally:
            stop.set()
            thread.join(60)
        t.check(theirs <= in_folder(a), "a catalog's movies follow it out of a library another catalog got")
        t.equal(parent_of(mine), own, "what is in a user's folder stays there")
        for movie in opened:
            api.sources(movie)
            rows = rows_of(movie)
            t.check(rows and all(r == a for r in rows), f"movie {movie[:8]}: its {len(rows)} stream rows are with it after the move")
            links = db.stream_rows(movie)
            t.check(links["count"] == links["owned"] == links["links"], f"movie {movie[:8]}: every row is a linked version of it ({links})")
        counted = db.one(f"select count(*) from BaseItems b where {GELATO_MOVIE} and lower(replace(b.ParentId,'-',''))=?", (a,))[0]
        shown = api.get(f"/Items?userId={api.user}&ParentId={library(LIB_A)['ItemId']}&IncludeItemTypes=Movie&Recursive=true&Limit=0")
        t.equal(shown.get("TotalRecordCount"), counted, "the library lists its movies once, no stream rows")
    finally:
        if policy:
            api.post(f"/Users/{u2.user}/Policy", policy)
        # Removing a library deletes what is in it: bring everything back to the default folder first.
        try:
            # Without the user's override its folder is nobody's, so the sync takes that movie too.
            cfg["UserConfigs"] = old["UserConfigs"]
            configure("", "")
            t.import_catalog(first)
        finally:
            cfg.update(old)
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            for name in added:
                api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
            for d in dirs.values():
                t.sh(f"rm -rf '{d}'")
