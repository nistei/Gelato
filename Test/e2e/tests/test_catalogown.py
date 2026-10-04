DESCRIPTION = "A catalog's titles are its own: an import marks them, they follow its library also past a lowered limit, a second catalog that lists the same titles takes none of them, and a catalog given a library leaves what another one has (adds libraries, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration, adds libraries, runs the catalog import

import time

from jfapi.bootstrap import GELATO, MOVIE_PATH

LIB_A, LIB_B = "Own first jfapi", "Own second jfapi"
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
MARK = "gelatocatalog"  # GelatoManager.CatalogProviderId
LIMIT, FEWER = 6, 2


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    first = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "movie"), None)
    t.require(first, "no movie catalog configured")
    # A second catalog with the same list: two catalogs of an addon rarely share a title, and the
    # rules below are about one they share. A catalog is its id and type, and the type is
    # lower-cased where the addon is asked, so this entry is another catalog that lists the same.
    twin = {**first, "Type": "Movie", "CreateCollection": False}
    key = {c["Type"]: f"{c['Type']}:{c['Id']}" for c in (first, twin)}
    default_path = cfg.get("MoviePath") or MOVIE_PATH

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def in_folder(fid):
        return {r[0] for r in db.query(f"select lower(replace(b.Id,'-','')) from BaseItems b where {GELATO_MOVIE} "
                                        "and lower(replace(b.ParentId,'-',''))=?", (fid,))}

    def owned(catalog):
        return {r[0] for r in db.query(
            f"select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id "
            f"where lower(p.ProviderId)='{MARK}' and p.ProviderValue=? and {GELATO_MOVIE}", (key[catalog["Type"]],))}

    def owner(item):
        row = db.one(f"select ProviderValue from BaseItemProviders where lower(replace(ItemId,'-',''))=? and lower(ProviderId)='{MARK}'", (item,))
        return row[0] if row else None

    def marked_rows(movie):
        return db.one(f"select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id where lower(p.ProviderId)='{MARK}' "
                      "and lower(replace(b.PrimaryVersionId,'-',''))=?", (movie,))[0]

    def configure(*catalogs):
        """(catalog, path, limit) for every catalog the settings hold: one left out is gone."""
        cfg["Catalogs"] = [{**c, "Enabled": True, "MaxItems": limit, "Path": path} for c, path, limit in catalogs]
        cfg["CatalogMaxItems"] = LIMIT
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    def sync(catalog, label):
        t.check(t.import_catalog(catalog), f"catalog import ({label}) completed")

    added, dirs = [], {}
    try:
        for name in (LIB_A, LIB_B):
            if library(name) is None:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType=movies&refreshLibrary=false",
                         {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
            dirs[name] = api.post(f"/gelato/libraries/{library(name)['ItemId']}/folder")["Path"]
        configure((first, "", LIMIT))
        t.check(t.folders_ready(*dirs.values()), "both libraries' Gelato folders exist")
        a, b, default = folder_id(dirs[LIB_A]), folder_id(dirs[LIB_B]), folder_id(default_path)

        # 1. The import marks what it brings in, or finds within its limit, as the catalog's.
        configure((first, dirs[LIB_A], LIMIT))
        sync(first, "the catalog gets a library")
        mine = in_folder(a)
        t.check(len(mine) >= FEWER + 1, f"the catalog's movies are in its library ({len(mine)})")
        if len(mine) <= FEWER:
            return
        t.equal(len(mine - owned(first)), 0, "every one of them carries the catalog's mark, not marked")
        movie = sorted(mine)[0]
        t.check(len(api.sources(movie)) >= 1, "one of them has streams")
        t.equal(marked_rows(movie), 0, "the mark is the movie's, stream rows carrying it")

        # 2. What is the catalog's follows its library, also what the import no longer reaches:
        # the limit is lowered, so the list is only read up to its first titles.
        configure((first, "", FEWER))
        sync(first, f"library cleared, limit {FEWER}")
        t.equal(len(in_folder(a)), 0, f"with the library cleared and the limit at {FEWER}, movies left in the library")
        t.check(mine <= in_folder(default), "all of the catalog's movies are back in the default library")
        configure((first, dirs[LIB_A], FEWER))
        sync(first, f"library again, limit {FEWER}")
        t.check(mine <= in_folder(a), f"and all {len(mine)} follow into the library again")

        # 3. A second catalog lists the same titles: the first one to import a title keeps it.
        configure((first, dirs[LIB_A], FEWER), (twin, dirs[LIB_B], LIMIT))
        sync(twin, "a second catalog with the same list")
        t.equal(len(owned(twin)), 0, "titles a second catalog took from the first")
        t.equal(len(in_folder(b)), 0, "movies in the second catalog's library")
        t.check(mine <= in_folder(a), "the first catalog's movies are where they were")

        # 4. The first catalog is gone from the settings: what it had is nobody's, and the import
        # that lists it within its limit takes it.
        configure((twin, dirs[LIB_B], LIMIT))
        sync(twin, "the first catalog is gone")
        taken = owned(twin)
        t.check(taken and taken <= mine, f"the second catalog took over titles of the gone one ({len(taken)})")
        t.check(taken <= in_folder(b), "and they are in its library")
        t.check((mine - taken) <= in_folder(a), "what it does not list stayed where it was")

        # 5. The first catalog is back with a library and lists those titles: they are another
        # catalog's now. Moving whatever a catalog lists took the other catalogs' titles along.
        configure((first, dirs[LIB_A], LIMIT), (twin, dirs[LIB_B], LIMIT))
        sync(first, "the first catalog is back")
        t.check(taken <= in_folder(b), "a catalog given a library leaves what another catalog has, though it lists it")
        t.equal(len(owned(first) & taken), 0, "titles it took back")

        # 6. The mark is no metadata: replacing a title's metadata keeps it.
        kept = sorted(taken)[0]
        stamp = lambda: db.one("select DateLastRefreshed from BaseItems where lower(replace(Id,'-',''))=?", (kept,))[0]
        before, t0 = stamp(), time.time()
        api.post(f"/Items/{kept}/Refresh?metadataRefreshMode=FullRefresh&imageRefreshMode=None&replaceAllMetadata=true")
        while stamp() == before and time.time() - t0 < 60:
            db.invalidate()
            time.sleep(0.3)
        t.check(stamp() != before, "the metadata refresh ran")
        t.settle(after=0.5)
        t.equal(owner(kept), key["Movie"], "the mark after a metadata replace")
    finally:
        # Removing a library deletes what is in it: both catalogs bring their movies back first.
        try:
            configure((first, "", LIMIT), (twin, "", LIMIT))
            t.import_catalog(first)
            t.import_catalog(twin)
        finally:
            cfg.update(old)
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            for name in added:
                api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
            for d in dirs.values():
                t.sh(f"rm -rf '{d}'")
