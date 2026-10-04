DESCRIPTION = "New library: Gelato makes the folder for a library that is not there yet, a catalog given that folder moves nothing until the library exists, and once Jellyfin's library is created with the folder it is Gelato's and the catalog's movies move in (adds a library, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration, adds a library and folders, runs a catalog import

from jfapi.bootstrap import GELATO

LIB = "New movies jfapi"
NAME = LIB.replace(" ", "%20")
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
ITEMS = 5


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    cat = next((c for c in old["Catalogs"] or [] if c.get("Type") == "movie"), None)
    t.require(cat, "no movie catalog configured")
    base = (cfg.get("BasePath") or api.get("/gelato/libraries")["DefaultBasePath"]).rstrip("/")
    expected = f"{base}/new-movies-jfapi"

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def holder(path):
        """The library Gelato lists the folder under, as (name, its Gelato folder)."""
        return next(((l["Name"], l.get("GelatoPath")) for l in api.get("/gelato/libraries")["Libraries"]
                     if path in l.get("Locations", [])), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def movies_in(fid):
        return db.one(f"select count(*) from BaseItems b where {GELATO_MOVIE} and lower(replace(b.ParentId,'-',''))=?", (fid,))[0]

    def movies():
        return db.one(f"select count(*) from BaseItems b where {GELATO_MOVIE}")[0]

    def configure(path):
        cfg["Catalogs"] = [{**cat, "Enabled": True, "MaxItems": ITEMS, "Path": path}]
        cfg["CatalogMaxItems"] = ITEMS
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    made = set()
    try:
        t.sh(f"rm -rf '{expected}' '{expected}-2'")
        t.equal(t.user2.call("POST", f"/gelato/libraries/folder?name={NAME}")[0], 403, "a user cannot have a folder made")
        t.equal(api.call("POST", "/gelato/libraries/folder")[0], 400, "a folder without a library name is refused")
        t.equal(api.call("POST", "/gelato/libraries/folder?name=%20")[0], 400, "a blank library name is refused")
        t.equal(api.call("POST", f"/gelato/libraries/folder?name={NAME}&basePath=gelato-relative")[0], 400,
                "a relative base path is refused")
        t.check(expected not in t.sh(f"ls -d {base}/*"), "a refused request creates no folder")

        # "New library…" on the settings page: the folder comes first, because Jellyfin's dialog
        # creates no library without a folder and takes only one that exists.
        path = api.post(f"/gelato/libraries/folder?name={NAME}")["Path"]
        made.add(path)
        t.equal(path, expected, "the folder is named after the library, under the Gelato folder")
        t.equal(t.sh(f"ls -A '{path}'").split(), ["stub.txt"], "it exists and holds Gelato's seed file")
        t.equal(holder(path), None, "it is in no library yet")
        t.equal(api.post(f"/gelato/libraries/folder?name={NAME}")["Path"], path,
                "asked again before the library exists, it is the same folder")

        # The settings are saved with the folder before the library is created: the catalog's
        # movies stay where they are.
        total = movies()
        configure(path)
        t.check(t.import_catalog(cat), "catalog import completed with the folder in no library")
        t.equal(folder_id(path), None, "the folder is no item of a library")
        t.equal(movies(), total, "the import neither lost nor added a movie")

        # The library as Jellyfin's dialog creates it: with the folder, and a scan.
        api.post(f"/Library/VirtualFolders?name={NAME}&collectionType=movies"
                 f"&paths={path.replace('/', '%2F')}&refreshLibrary=true", {"LibraryOptions": {"EnableRealtimeMonitor": False}})
        t.check(path in (library() or {}).get("Locations", []), "Jellyfin takes the folder for a new library")
        t.equal(holder(path), (LIB, path), "the settings page shows the library, the folder as Gelato's in it")

        t.check(t.import_catalog(cat), "catalog import completed with the library there")
        folder = folder_id(path)
        t.check(folder, "the folder is an item of the library after the import")
        moved = movies_in(folder) if folder else 0
        t.check(moved > 0, f"the catalog's movies moved into the new library ({moved})")
        t.equal(movies(), total, "none was lost or doubled on the way")

        # The name again, now that a library holds the folder: never the same folder.
        second = api.post(f"/gelato/libraries/folder?name={NAME}")["Path"]
        made.add(second)
        t.equal(second, f"{expected}-2", "asked again once a library holds the folder, it is another one")
    finally:
        # Removing a library deletes what is in it: bring the movies back first.
        try:
            folder = folder_id(expected)
            if folder and movies_in(folder):
                configure("")
                t.import_catalog(cat)
        finally:
            cfg.update(old)
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            if library() is not None:
                api.call("DELETE", f"/Library/VirtualFolders?name={NAME}&refreshLibrary=false")
            t.sh("rm -rf " + " ".join(f"'{p}'" for p in made | {expected}))
