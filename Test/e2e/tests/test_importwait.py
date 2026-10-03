DESCRIPTION = "A catalog imported right after its library was picked waits for the scan that adds the folder: its movies move in instead of staying in the default library (adds a library, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration, adds a library, runs a catalog import

from jfapi.bootstrap import GELATO

LIB = "Wait movies jfapi"
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
ITEMS = 5


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    cat = next((c for c in old["Catalogs"] or [] if c.get("Type") == "movie"), None)
    t.require(cat, "no movie catalog configured")

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def movies_in(fid):
        return db.one(f"select count(*) from BaseItems b where {GELATO_MOVIE} and lower(replace(b.ParentId,'-',''))=?", (fid,))[0]

    def configure(path):
        cfg["Catalogs"] = [{**cat, "Enabled": True, "MaxItems": ITEMS, "Path": path}]
        cfg["CatalogMaxItems"] = ITEMS
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    path, created = None, False
    try:
        if library() is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=movies&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
            created = True
        # What the Import button of a catalog does when its library was picked a moment ago: the
        # folder is made and added to the library, the row is saved, the import starts. Nothing
        # waits for the scan in between.
        path = api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"]
        configure(path)
        t.log("folder item at the start of the import:", folder_id(path))
        t.check(t.import_catalog(cat), "catalog import completed")
        folder = folder_id(path)
        t.check(folder, "the library's Gelato folder is there after the import")
        moved = movies_in(folder) if folder else 0
        t.check(moved > 0, f"the catalog's movies are in the library that was just picked ({moved})")
        t.log(t.sh("grep -h 'for the scan that adds' /config/log/*.log | tail -1").strip()[-130:])
    finally:
        # Removing a library deletes what is in it: bring the movies back first.
        try:
            folder = folder_id(path) if path else None
            if folder and movies_in(folder):
                configure("")
                t.import_catalog(cat)
        finally:
            cfg.update(old)
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            if created:
                api.call("DELETE", f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&refreshLibrary=false")
            if path:
                t.sh(f"rm -rf '{path}'")
