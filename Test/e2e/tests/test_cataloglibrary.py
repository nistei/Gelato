DESCRIPTION = "A catalog's library as it is used: browse, latest, search and open, play a movie and an episode, resume and next up, the collection, a second user, a scan, a server restart, a delete and re-import (adds two libraries, restarts the server)"
DESTRUCTIVE = True  # changes the plugin configuration, adds two libraries, restarts the container

import re
import subprocess
import urllib.parse

from jfapi import bootstrap
from jfapi.bootstrap import CATALOG_ITEMS, GELATO, SERIES_ITEMS

MOVIE_LIB, SERIES_LIB = "Walk movies jfapi", "Walk shows jfapi"
GELATO_MOVIE = "b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%')"
GELATO_SERIES = "b.Type like '%TV.Series' and b.Path like 'gelato://%'"
MOVIE_TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs"]


def run(t):
    api, db, u2 = t.api, t.db, t.user2
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    movie_cat = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "movie"), None)
    series_cat = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "series"), None)
    if movie_cat is None or series_cat is None:
        t.skip("needs a movie and a series catalog configured")

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

    def folder_id(path):
        row = db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%.Folder'", (path,))
        return row[0] if row else None

    def in_folder(where, fid):
        return {r[0] for r in db.query(f"select lower(replace(b.Id,'-','')) from BaseItems b where {where} "
                                        "and lower(replace(b.ParentId,'-',''))=?", (fid,))}

    def ids(d):
        return [i["Id"].replace("-", "").lower() for i in (d.get("Items", []) if isinstance(d, dict) else d)]

    def listed(user, lib, kind):
        return ids(user.get(f"/Items?userId={user.user}&ParentId={lib}&IncludeItemTypes={kind}&Recursive=true&Limit=5000"))

    def configure(movie_path, series_path):
        cfg["Catalogs"] = [
            {**movie_cat, "Enabled": True, "MaxItems": CATALOG_ITEMS, "Path": movie_path, "CreateCollection": True},
            {**series_cat, "Enabled": True, "MaxItems": SERIES_ITEMS, "Path": series_path},
        ]
        cfg["CatalogMaxItems"] = CATALOG_ITEMS
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

    def sync(label):
        status, msg = api.run_task("GelatoCatalogItemsSync", timeout=1800)
        t.equal(status, "Completed", f"catalog sync ({label}) {msg}")
        t.settle(after=1, timeout=300)  # the scan the import queues

    def plays(item, label):
        """Streams are offered and the stream endpoint delivers. Returns (runtime, source id)."""
        srcs = api.item(item).get("MediaSources") or []
        t.check(len(srcs) >= 1, f"{label}: {len(srcs)} streams")
        pi = api.post(f"/Items/{item}/PlaybackInfo?userId={api.user}", {"UserId": api.user})
        ms = pi.get("MediaSources") or []
        t.check(ms and not pi.get("ErrorCode"), f"{label}: PlaybackInfo offers a source ({pi.get('ErrorCode')})")
        if not ms:
            return 0, None
        st, _, body = api.request(f"/Videos/{item}/stream?static=true&mediaSourceId={ms[0]['Id']}", {"Range": "bytes=0-0"}, max_bytes=1)
        t.check(st in (200, 206) and len(body) == 1, f"{label}: the stream delivers bytes ({st})")
        return api.item(item).get("RunTimeTicks") or 0, (srcs[1]["Id"] if len(srcs) > 1 else srcs[0]["Id"])

    added, dirs, touched = [], {}, []
    try:
        for name, kind in ((MOVIE_LIB, "movies"), (SERIES_LIB, "tvshows")):
            if library(name) is None:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType={kind}&refreshLibrary=false",
                         {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
            dirs[name] = api.post(f"/gelato/libraries/{library(name)['ItemId']}/folder")["Path"]
        configure(dirs[MOVIE_LIB], dirs[SERIES_LIB])
        t.folders_ready(*dirs.values())
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "the scan the folders queued finished")
        movies_f, series_f = folder_id(dirs[MOVIE_LIB]), folder_id(dirs[SERIES_LIB])
        t.check(movies_f and series_f, "both libraries have their Gelato folder")
        if not (movies_f and series_f):
            return
        sync("libraries picked")
        movie_lib, series_lib = library(MOVIE_LIB)["ItemId"], library(SERIES_LIB)["ItemId"]
        movies, series = in_folder(GELATO_MOVIE, movies_f), in_folder(GELATO_SERIES, series_f)
        t.check(movies and series, f"the import filled both libraries ({len(movies)} movies, {len(series)} series)")
        if not (movies and series):
            return
        show = sorted(series)[0]

        # ---- browsing
        t.check(any(v.get("Id", "").replace("-", "").lower() == movie_lib.lower() for v in api.get(f"/UserViews?userId={api.user}")["Items"]),
                "the library is among the user's views")
        shown = listed(api, movie_lib, "Movie")
        t.equal(sorted(shown), sorted(movies), "the movies library lists its movies, each once, no stream rows")
        t.equal(sorted(listed(api, series_lib, "Series")), sorted(series), "the shows library lists its series")
        latest = ids(api.get(f"/Items/Latest?userId={api.user}&ParentId={movie_lib}&Limit=50"))
        t.check(latest and set(latest) <= movies, f"Latest in the library shows its movies ({len(latest)})")
        t.equal(sorted(listed(u2, movie_lib, "Movie")), sorted(movies), "a second user sees the same library")

        # ---- search inside the library: an item it has, and a title it does not have yet
        own = api.get(f"/Items/{sorted(movies)[0]}?userId={api.user}")
        found = ids(api.get(f"/Items?userId={api.user}&searchTerm={urllib.parse.quote(own['Name'])}&parentId={movie_lib}"
                            f"&IncludeItemTypes=Movie&Recursive=true&Limit=20"))
        t.equal(found.count(sorted(movies)[0]), 1, "a search inside the library finds its own movie, once")
        fresh = None
        for term in MOVIE_TERMS:
            hits = api.get(f"/Items?userId={api.user}&searchTerm={urllib.parse.quote(term)}&parentId={movie_lib}"
                           f"&IncludeItemTypes=Movie&Recursive=true&Limit=8&Fields=Path")["Items"]
            fresh = next((h for h in hits if (h.get("Path") or "").startswith("gelato://stub/") and re.search(r"tt\d+", h["Path"])
                          and not db.one("select count(*) from BaseItemProviders where lower(ProviderId)='stremio' and ProviderValue=?",
                                         (re.search(r"(tt\d+)", h["Path"]).group(1),))[0]), None)
            if fresh:
                break
        t.check(fresh, "a search inside the library offers a title it does not have")
        if fresh:
            new = api.item(fresh["Id"]).get("Id", "").replace("-", "").lower()
            touched.append(new)
            api.settle_insert()
            t.check(new in in_folder(GELATO_MOVIE, movies_f), f"opening it puts it into this library ({fresh['Name']})")

        # ---- a movie: play, resume
        movie = sorted(movies)[1 % len(movies)]
        runtime, row = plays(movie, "a movie of the library")
        if row:
            api.report("start", movie, row, 0, "walk")
            api.report("progress", movie, row, int(runtime * 0.3) if runtime else 600000000, "walk")
            pos = api.user_data(movie)["PlaybackPositionTicks"]
            api.report("stop", movie, row, pos, "walk")
            t.check(pos > 0, "the movie's progress is saved")
            t.check(movie in [i.replace("-", "").lower() for i in api.resume()], "it is in Continue Watching")
            in_lib = ids(api.get(f"/UserItems/Resume?userId={api.user}&parentId={movie_lib}&mediaTypes=Video&limit=50"))
            t.check(movie in in_lib, "and in the library's own Continue Watching")
            t.equal(sorted(listed(api, movie_lib, "Movie")), sorted(in_folder(GELATO_MOVIE, movies_f)),
                    "the library still lists each movie once after its streams were synced")

        # ---- a series: seasons, episodes, play episode 1 to the end, next up
        api.mark_played(show, False)
        eps = t.episodes(show, 1)
        t.check(len(eps) >= 1, f"the series has episodes ({len(eps)} in season 1)")
        if len(eps) >= 2:
            e1, e2 = eps[0]["Id"].replace("-", "").lower(), eps[1]["Id"].replace("-", "").lower()
            runtime, row = plays(e1, "episode 1")
            if row and runtime:
                api.report("start", e1, row, 0, "walk")
                api.report("stop", e1, row, int(runtime * 0.97), "walk")
                t.check(api.user_data(e1)["Played"], "episode 1 finished")
                nextup = ids(api.get(f"/Shows/NextUp?seriesId={show}&userId={api.user}"))
                t.equal(nextup, [e2], "Next Up is episode 2")
                in_lib = ids(api.get(f"/Shows/NextUp?userId={api.user}&parentId={series_lib}"))
                t.check(e2 in in_lib, "also in the library's Next Up")

        # ---- the catalog's collection holds the movies where they are now
        box = db.one("select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id "
                     "and lower(p.ProviderId)='stremio' where b.Type like '%BoxSet' and p.ProviderValue=?",
                     (f"{movie_cat['Type']}.{movie_cat['Id']}",))
        t.check(box, "the catalog has its collection")
        if box:
            members = set(ids(api.get(f"/Items?userId={api.user}&ParentId={box[0]}&Limit=5000")))
            t.check(movies <= members, f"the movies that moved into the library are still among its {len(members)} members")

        # ---- a scan and a restart leave everything in place
        episodes = lambda: db.one("select count(*) from BaseItems e join AncestorIds a on a.ItemId=e.Id where e.Type like '%TV.Episode' "
                                  "and (e.Tags is null or e.Tags not like '%gelato-stream%') and lower(replace(a.ParentItemId,'-',''))=?", (show,))[0]
        before = (in_folder(GELATO_MOVIE, movies_f), in_folder(GELATO_SERIES, series_f), episodes())
        api.post("/Library/Refresh")
        t.settle(after=1, timeout=300)
        t.check(api.wait_tasks_idle("RefreshLibrary", 1800), "library scan finished")
        t.check((in_folder(GELATO_MOVIE, movies_f), in_folder(GELATO_SERIES, series_f)) == before[:2], "a library scan keeps both libraries' items")
        t.check(episodes() >= before[2], f"and the series' episodes ({before[2]})")

        subprocess.run(["docker", "restart", db.container], capture_output=True)
        t.check(bootstrap.wait_ready(api.base, t.log) is not None, "the server came back after a restart")
        api.ensure()
        db.invalidate()
        api.search("gelato")  # a request, which is when Gelato looks its folders up and seeds them
        t.check((in_folder(GELATO_MOVIE, movies_f), in_folder(GELATO_SERIES, series_f)) == before[:2], "a restart keeps both libraries' items")
        t.equal(sorted(listed(api, movie_lib, "Movie")), sorted(before[0]), "the library lists the same movies after the restart")
        t.check("stub.txt" in t.sh(f"ls '{dirs[MOVIE_LIB]}'"), "the folder has its seed file again")
        plays(movie, "the movie after the restart")
        t.check(api.get(f"/Items?userId={api.user}&searchTerm=one%20piece&parentId={series_lib}&IncludeItemTypes=Series&Recursive=true&Limit=5")["Items"],
                "a search inside the shows library is answered after the restart")

        # ---- delete a movie: it is gone with its rows, and the next import brings it back here
        victim = movie
        stremio = db.stremio_id(victim)
        api.delete(f"/Items/{victim}")
        t.equal(api.call("GET", f"/Items/{victim}?userId={api.user}")[0], 404, "a deleted movie is gone")
        t.equal(db.one("select count(*) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=?", (victim,))[0], 0, "with its stream rows")
        sync("after the delete")
        back = db.one(f"select lower(replace(b.ParentId,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id "
                      f"and lower(p.ProviderId)='stremio' where {GELATO_MOVIE} and p.ProviderValue=?", (stremio,))
        t.check(back and back[0] == movies_f, "the next import brings it back into the catalog's library")
    finally:
        for item in touched:
            api.delete_inserted(item)
        # Removing a library deletes what is in it: bring everything back first.
        try:
            left = [f for f in (folder_id(d) for d in dirs.values()) if f]
            if any(in_folder(GELATO_MOVIE, f) or in_folder(GELATO_SERIES, f) for f in left):
                configure("", "")
                api.run_task("GelatoCatalogItemsSync", timeout=1800)
        finally:
            cfg.update(old)
            api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            for name in added:
                api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
            for d in dirs.values():
                t.sh(f"rm -rf '{d}'")
