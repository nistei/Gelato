DESCRIPTION = "A library that is a catalog's and a user's own at once: a movie in it stays when a user without access opens it, with its stream rows, and a hit opened from a search inside it lands in it (adds a library and a user, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration, adds a library and a user, runs a catalog import

import re
import urllib.parse

from jfapi.api import search_result_id
from jfapi.bootstrap import GELATO
from jfapi.testing import make_user2

LIB = "Shared movies jfapi"
THIRD_USER = "jfapi-third"
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
MOVIE_TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs"]
ITEMS = 5


def run(t):
    api, db, u2 = t.api, t.db, t.user2
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems", "UserConfigs")}
    cat = next((c for c in old["Catalogs"] or [] if c.get("Type") == "movie"), None)
    t.require(cat, "no movie catalog configured")

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def in_folder(fid):
        return {r[0] for r in db.query(f"select lower(replace(b.Id,'-','')) from BaseItems b where {GELATO_MOVIE} "
                                        "and lower(replace(b.ParentId,'-',''))=?", (fid,))}

    def parent_of(item):
        return db.one("select lower(replace(ParentId,'-','')) from BaseItems where lower(replace(Id,'-',''))=?", (item,))[0]

    def title(stremio):
        return db.one("select lower(replace(b.Id,'-','')), lower(replace(b.ParentId,'-','')) from BaseItems b "
                      "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                      "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))

    def configure(path, users):
        cfg["Catalogs"] = [{**cat, "Enabled": True, "MaxItems": ITEMS, "Path": path}]
        cfg["CatalogMaxItems"] = ITEMS
        cfg["UserConfigs"] = users
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    path, created, u3, inserted = None, False, None, []
    try:
        if library() is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=movies&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
            created = True
        path = api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"]
        # The library is the catalog's, and the second user's movie library too: the settings page
        # hands out one Gelato folder per library, whoever picks it.
        users = [u for u in (old["UserConfigs"] or []) if u["UserId"].replace("-", "").lower() != u2.user.replace("-", "").lower()]
        users.append({"UserId": u2.user, "Url": cfg["Url"], "MoviePath": path, "SeriesPath": cfg.get("SeriesPath"), "DisableSearch": False})
        configure(path, users)
        t.check(t.folders_ready(path), "the library's Gelato folder exists")
        shared, scope = folder_id(path), library()["ItemId"]
        t.check(t.import_catalog(cat), "catalog import completed")
        movies = in_folder(shared)
        t.check(movies, f"the catalog's movies are in the library ({len(movies)})")
        if not movies:
            return
        movie = sorted(movies)[0]
        t.check(len(api.sources(movie)) >= 1, "a movie of the library has streams")  # its stream rows exist now

        # A third user, who cannot open the library and has no folder of their own, opens the movie
        # from a search of everything.
        u3 = make_user2(api, THIRD_USER, on_call=db.invalidate)()
        policy = api.get(f"/Users/{u3.user}")["Policy"]
        api.post(f"/Users/{u3.user}/Policy", {**policy, "EnableAllFolders": False, "EnabledFolders": [
            v["ItemId"] for v in api.get("/Library/VirtualFolders") if v["ItemId"] != scope]})
        stremio = db.stremio_id(movie)
        u3.search(stremio)
        t.log("opened by the third user:", u3.call("GET", f"/Items/{search_result_id(stremio)}?userId={u3.user}")[0])
        api.settle_insert()
        t.equal(parent_of(movie), shared, "a user without access to the library does not pull a movie out of it")
        counted = db.one(f"select count(*) from BaseItems b where {GELATO_MOVIE} and lower(replace(b.ParentId,'-',''))=?", (shared,))[0]
        shown = api.get(f"/Items?userId={api.user}&ParentId={scope}&IncludeItemTypes=Movie&Recursive=true&Limit=0").get("TotalRecordCount")
        t.equal(shown, counted, "the library lists its movies, no stream rows left behind")

        # The administrator, whose movie library is the default one, searches inside this library.
        hit = None
        for term in MOVIE_TERMS:
            items = api.get(f"/Items?userId={api.user}&searchTerm={urllib.parse.quote(term)}&parentId={scope}"
                            f"&IncludeItemTypes=Movie&Recursive=true&Limit=8&Fields=Path")["Items"]
            hit = next((h for h in items if (h.get("Path") or "").startswith("gelato://stub/") and re.search(r"tt\d+", h["Path"])
                        and not title(re.search(r"(tt\d+)", h["Path"]).group(1))), None)
            if hit:
                break
        t.check(hit, "a search inside the library is answered by the addon")
        if hit:
            new = api.item(hit["Id"]).get("Id", "").replace("-", "").lower()
            inserted.append(new)
            api.settle_insert()
            t.equal(parent_of(new), shared, f"a hit opened from that search is in the library it was searched in ({hit['Name']})")
    finally:
        for item in inserted:
            api.delete_inserted(item)
        if u3 is not None:
            api.call("DELETE", f"/Users/{u3.user}")
        # Removing a library deletes what is in it: bring the catalog's movies back first.
        try:
            configure("", old["UserConfigs"])
            t.import_catalog(cat)
        finally:
            cfg.update(old)
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            if created:
                api.call("DELETE", f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&refreshLibrary=false")
            if path:
                t.sh(f"rm -rf '{path}'")
