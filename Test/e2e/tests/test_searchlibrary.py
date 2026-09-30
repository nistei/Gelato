DESCRIPTION = "Default libraries: with other movies and series libraries picked, a movie and a series opened from search land in them, a title the library has stays where it is (adds two libraries, removed again)"
DESTRUCTIVE = True  # changes the plugin's default folders for the run and adds two libraries

import re
import time

from jfapi.bootstrap import GELATO

MOVIE_LIB, SERIES_LIB = "Search movies jfapi", "Search shows jfapi"
MOVIE_TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs"]
SERIES_TERMS = ["Adolescence", "Chernobyl", "Baby Reindeer", "The Queen's Gambit", "Beef", "Shogun", "Ripley"]


def run(t):
    api, db = t.api, t.db
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("MoviePath", "SeriesPath")}

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

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

    def open_fresh(terms, kind):
        """Opens the first search hit the library does not have: the id of the item it became."""
        for term in terms:
            for hit in api.search(term, kind, limit=8):
                m = re.search(r"(tt\d+)", hit.get("Path") or "")
                if m and hit.get("Name", "").lower().startswith(term.lower()[:5]) and not in_library(m.group(1)):
                    item = api.item(hit["Id"]).get("Id", "").replace("-", "").lower()
                    t.log(f"{kind} opened from search: {hit['Name']} ({m.group(1)}) as {item[:8]}")
                    return item
        return None

    added, inserted = [], []
    movie_dir = series_dir = None
    try:
        for name, kind in ((MOVIE_LIB, "movies"), (SERIES_LIB, "tvshows")):
            if library(name) is None:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType={kind}&refreshLibrary=false",
                         {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
        # What the settings page does when the two libraries are picked as the defaults.
        movie_dir = api.post(f"/gelato/libraries/{library(MOVIE_LIB)['ItemId']}/folder")["Path"]
        series_dir = api.post(f"/gelato/libraries/{library(SERIES_LIB)['ItemId']}/folder")["Path"]
        cfg.update({"MoviePath": movie_dir, "SeriesPath": series_dir})
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        time.sleep(5)
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "the scan the folders queued finished")
        t.check(folder_id(movie_dir) and folder_id(series_dir), "both folders are in the library")
        t.wait(12)  # Gelato memoizes its folder lookup for 10 s

        # A title the library has already: opening it from search leaves it in the old folder.
        known = t.movie()
        before = parent_path(known)
        api.item(known)
        t.equal(parent_path(known), before, "a movie the library had stays in its folder")

        movie = open_fresh(MOVIE_TERMS, "Movie")
        t.require(movie, "no search hit the library does not have yet")
        inserted.append(movie)
        t.equal(parent_path(movie), movie_dir, "a movie opened from search is in the default movies library's folder")
        listed = {i["Id"].replace("-", "").lower() for i in api.get(
            f"/Items?userId={api.user}&ParentId={library(MOVIE_LIB)['ItemId']}&IncludeItemTypes=Movie&Recursive=true")["Items"]}
        t.check(movie in listed, "that library lists it")

        series = open_fresh(SERIES_TERMS, "Series")
        if series:
            inserted.append(series)
            t.equal(parent_path(series), series_dir, "a series opened from search is in the default series library's folder")
        else:
            t.log("no series search hit the library does not have yet, series part skipped")
    finally:
        for item in inserted:
            api.delete_inserted(item)
        cfg.update(old)
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        for name in added:
            api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
        for d in (movie_dir, series_dir):
            if d:
                t.sh(f"rm -rf '{d}'")
