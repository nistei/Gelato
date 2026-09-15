DESCRIPTION = "Per-user movie folders: a second user inserting a title the first already has (adds a library and scans)"
DESTRUCTIVE = True  # changes the instance: a plugin override and a library for the second user

import time

GELATO = "94ea4e14-8163-4989-96fe-0a2094bc2d6a"
LIBRARY = "Filme jfapi-user2"
PATH = "/tmp/gelato/movies-user2"


def run(t):
    movie = t.movie()
    stremio = t.fixtures.stremio_id(movie)
    u1, u2 = t.api, t.user2
    user2_id = u2.user

    cfg = u1.get(f"/Plugins/{GELATO}/Configuration")
    override = next((u for u in cfg.get("UserConfigs", []) if u["UserId"].replace("-", "").lower() == user2_id.replace("-", "").lower()), None)
    if override is None:
        cfg["UserConfigs"] = [{"UserId": user2_id, "Url": cfg["Url"], "MoviePath": PATH, "SeriesPath": PATH + "-series", "DisableSearch": False}]
        u1.post(f"/Plugins/{GELATO}/Configuration", cfg)
        path = PATH
        t.log("per-user override set for", u2.name, "at", path)
    else:
        path = override["MoviePath"]
        t.log(f"{u2.name} already has an override at {path}, using it")
    t.sh(f"mkdir -p {path} {path}-series")
    if not any(path in v.get("Locations", []) for v in u1.get("/Library/VirtualFolders")):
        u1.post(f"/Library/VirtualFolders?name={LIBRARY.replace(' ', '%20')}&collectionType=movies&paths={path.replace('/', '%2F')}&refreshLibrary=false",
                {"LibraryOptions": {"EnableRealtimeMonitor": False}})
        t.log("library created:", LIBRARY, "on", path)
    u2.search(stremio)  # seeds the folder (stub.txt) through Gelato's folder lookup
    folder = lambda: t.db.one("select count(*) from BaseItems where Path=? and Type like '%.Folder'", (path,))[0]
    if not folder():
        u1.post("/Library/Refresh")
        time.sleep(5)
        t.check(u1.wait_tasks_idle("RefreshLibrary", 1800), "library scan finished")
    t.check(folder() == 1, f"the folder item for {path} exists")
    t.wait(12)  # Gelato memoizes its folder lookup for 10 s; the seeding search saw no folder yet

    hits = u2.search(stremio)
    t.check(hits, f"{u2.name} finds the title in search")
    if not hits:
        return
    d = u2.item(hits[0]["Id"])
    n2 = len(d.get("MediaSources") or [])
    t.equal(d.get("Id", "").lower(), movie, "the insert found the existing movie (one item per title)")
    movies = t.db.query("select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                        "and p.ProviderValue=? where b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))
    t.equal(movies[0][0], 1, "one movie item for the title")
    n1 = len(u1.sources(movie))
    t.check(n1 >= 2 and n2 >= 2, f"both users get streams ({u1.name} {n1}, {u2.name} {n2})")
    rows = t.db.row_users(movie)
    t.check(all(v[1] == movie for v in rows.values()), "every row is owned by the movie")
    folders = t.db.query("select distinct (select Path from BaseItems f where f.Id=b.ParentId) from BaseItems b where lower(replace(b.PrimaryVersionId,'-',''))=?", (movie,))
    t.log("row folders:", [f[0] for f in folders])
    t.equal(len(folders), 1, "the rows sit in one folder (the last syncing user's)")
    both = {path, cfg.get("MoviePath")}
    t.check(folders and folders[0][0] in both, f"which is one of the two users' folders (the last syncing user's): {folders[0][0] if folders else None}")
