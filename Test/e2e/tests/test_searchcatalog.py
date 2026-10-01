DESCRIPTION = "Search inside a catalog's library: the addon answers for the kind the library takes, and a hit opened from that search lands in that library; the same hit from a search of everything lands in the default one (adds a library, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration and adds a library

import re
import time
import urllib.parse

from jfapi.bootstrap import GELATO, SERIES_PATH

LIB = "Scope shows jfapi"
SERIES_TERMS = ["Adolescence", "Chernobyl", "Baby Reindeer", "The Queen's Gambit", "Beef", "Shogun", "Ripley"]
MOVIE_TERM = "Heretic"


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = cfg.get("Catalogs")
    cat = next((c for c in old or [] if c.get("Type") == "series"), None)
    t.require(cat, "no series catalog configured")

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def parent_path(item_id):
        row = db.one("select (select f.Path from BaseItems f where f.Id=b.ParentId) from BaseItems b "
                     "where lower(replace(b.Id,'-',''))=?", (item_id,))
        return row[0] if row else None

    def in_library(stremio):
        return db.one("select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                      "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

    def search(term, kind, scope=None):
        path = (f"/Items?userId={api.user}&searchTerm={urllib.parse.quote(term)}&IncludeItemTypes={kind}"
                f"&Recursive=true&Limit=10&Fields=Path")
        if scope:
            path += f"&parentId={scope}"
        st, d = api.call("GET", path)
        return d.get("Items", []) if st == 200 and isinstance(d, dict) else []

    def fresh(kind, terms, scope=None):
        """Addon hits for titles the library does not have: (hit, stremio id)."""
        for term in terms:
            for hit in search(term, kind, scope):
                m = re.search(r"(tt\d+)", hit.get("Path") or "")
                if m and (hit.get("Path") or "").startswith("gelato://stub/") and not in_library(m.group(1)):
                    yield hit, m.group(1)

    path, created, inserted = None, False, []
    try:
        if library() is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=tvshows&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
            created = True
        path = api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"]
        time.sleep(5)
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "the scan the folder queued finished")
        t.check(folder_id(path), "the library's Gelato folder exists")
        scope = library()["ItemId"]

        # The library has Gelato's folder and no catalog uses it (yet, or any more): it is searched
        # all the same. The seed file is gone, as after a restart.
        t.sh(f"rm -f '{path}/stub.txt'")
        t.check(list(fresh("Series", SERIES_TERMS[:2], scope)), "a search inside a library with Gelato's folder is answered by the addon")

        # The library is the series catalog's: its folder is one Gelato imports into.
        cfg["Catalogs"] = [{**c, "Path": path} if c is cat else c for c in old]
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        t.wait(12)  # Gelato memoizes its folder lookup for 10 s

        hits = list(fresh("Series", SERIES_TERMS, scope))
        t.check(hits, "a search inside the catalog's library is answered by the addon")
        t.equal(list(fresh("Movie", [MOVIE_TERM], scope)), [], "but not with movies: it is a shows library")
        if not hits:
            return

        hit, stremio = hits[0]
        item = api.item(hit["Id"]).get("Id", "").replace("-", "").lower()
        inserted.append(item)
        t.log(f"opened {hit['Name']} ({stremio}) from the search inside {LIB}")
        t.equal(parent_path(item), path, "a hit opened from that search is in the library it was searched in")

        # The same search without a library: the hit goes to the default series library.
        default = cfg.get("SeriesPath") or SERIES_PATH
        other = next(((h, s) for h, s in fresh("Series", SERIES_TERMS) if s != stremio), None)
        if other:
            item2 = api.item(other[0]["Id"]).get("Id", "").replace("-", "").lower()
            inserted.append(item2)
            t.equal(parent_path(item2), default, "a hit opened from a search of everything is in the default series library")
            # And a scoped search does not redirect a title somebody found everywhere a moment ago.
            again = next(((h, s) for h, s in fresh("Series", SERIES_TERMS, scope) if s not in (stremio, other[1])), None)
            if again:
                list(fresh("Series", [again[0]["Name"]]))  # the same title, searched everywhere after it
                item3 = api.item(again[0]["Id"]).get("Id", "").replace("-", "").lower()
                inserted.append(item3)
                t.equal(parent_path(item3), default, "the last search a hit came from decides where it goes")
        else:
            t.log("no second series hit the library does not have, the unscoped half is skipped")
    finally:
        for item in inserted:
            api.delete_inserted(item)
        cfg["Catalogs"] = old
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        if created:
            api.call("DELETE", f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&refreshLibrary=false")
        if path:
            t.sh(f"rm -rf '{path}'")
