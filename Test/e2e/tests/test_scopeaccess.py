DESCRIPTION = "Search inside a catalog's library keeps to that library: a title of another library is not among its results, and a user who cannot open the library gets no addon answer in it and puts nothing into it (adds a library, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration and a user's access, adds a library

import re
import urllib.parse

from jfapi.bootstrap import GELATO

LIB = "Access movies jfapi"
MOVIE_TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs"]


def run(t):
    api, db, u2 = t.api, t.db, t.user2
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = cfg.get("Catalogs")
    cat = next((c for c in old or [] if c.get("Type") == "movie"), None)
    t.require(cat, "no movie catalog configured")

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def title(stremio):
        """(id, folder path) of the library's item for a stremio id."""
        return db.one("select lower(replace(b.Id,'-','')), (select f.Path from BaseItems f where f.Id=b.ParentId) from BaseItems b "
                      "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                      "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))

    def search(user, term, scope):
        st, d = user.call("GET", f"/Items?userId={user.user}&searchTerm={urllib.parse.quote(term)}&IncludeItemTypes=Movie"
                                 f"&Recursive=true&Limit=10&Fields=Path&parentId={scope}")
        return d.get("Items", []) if st == 200 and isinstance(d, dict) else []

    def fresh(user, scope):
        """Addon hits of a search inside the library for titles the library does not have: (hit, stremio id)."""
        for term in MOVIE_TERMS:
            for hit in search(user, term, scope):
                m = re.search(r"(tt\d+)", hit.get("Path") or "")
                if m and (hit.get("Path") or "").startswith("gelato://stub/") and not title(m.group(1)):
                    return hit, m.group(1)
        return None

    path, created, policy, inserted = None, False, None, []
    try:
        if library() is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=movies&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
            created = True
        path = api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"]
        cfg["Catalogs"] = [{**c, "Path": path} if c is cat else c for c in old]
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        t.check(t.folders_ready(path), "the library's Gelato folder exists")
        scope = library()["ItemId"]

        # A movie of the default library, searched for inside the catalog's library.
        known = t.movie()
        name = api.item(known)["Name"]
        elsewhere = db.one("select (select f.Path from BaseItems f where f.Id=b.ParentId) from BaseItems b where lower(replace(b.Id,'-',''))=?", (known,))[0]
        t.check(elsewhere != path, f"the movie {name} is in another library ({elsewhere})")
        found = [h["Id"].replace("-", "").lower() for h in search(api, name, scope)]
        t.check(known not in found, "a search inside the library does not answer with a movie of another library")

        # A user who cannot open the library searches inside it and opens what comes back.
        policy = api.get(f"/Users/{u2.user}")["Policy"]
        api.post(f"/Users/{u2.user}/Policy", {**policy, "EnableAllFolders": False, "EnabledFolders": [
            v["ItemId"] for v in api.get("/Library/VirtualFolders") if v["ItemId"] != scope]})
        hit = fresh(u2, scope)
        t.check(hit is None, "a user who cannot open the library gets no addon answer inside it")
        if hit:
            status = u2.call("GET", f"/Items/{hit[0]['Id']}?userId={u2.user}")[0]
            api.settle_insert()
            row = title(hit[1])
            t.log(f"{hit[0]['Name']} ({hit[1]}) opened by the user: {status}, now in {row[1] if row else 'no library'}")
            if row:
                inserted.append(row[0])
            t.check(not row or row[1] != path, "and puts no title into it")
    finally:
        if policy:
            api.post(f"/Users/{u2.user}/Policy", policy)
        for item in inserted:
            api.delete_inserted(item)
        cfg["Catalogs"] = old
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        if created:
            api.call("DELETE", f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&refreshLibrary=false")
        if path:
            t.sh(f"rm -rf '{path}'")
