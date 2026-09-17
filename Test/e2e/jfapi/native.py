"""Native libraries: folders of small real video files, written with the container's ffmpeg and added
as Jellyfin libraries, for tests of how Gelato treats items that are not its own.

A library is `(name, collection type, path)`. `add_library` scans it and waits for its items,
`remove_libraries` takes everything out again: the items first, because a library removed without a
scan leaves its items in the database until the next one, where a later test counts them as lost.
"""
import time
import urllib.parse

FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"


def write_videos(t, paths, seconds=2):
    """Writes a short test video to every path (folders are created)."""
    cmds = [f"mkdir -p \"$(dirname '{p}')\" && {FFMPEG} -y -loglevel error -f lavfi "
            f"-i testsrc=duration={seconds}:size=320x240:rate=10 -c:v libx264 '{p}'" for p in paths]
    t.sh(" && ".join(cmds))
    written = t.sh("for f in " + " ".join(f"'{p}'" for p in paths) + "; do [ -s \"$f\" ] && echo x; done").split()
    t.equal(len(written), len(paths), "local video files written")


def library_id(t, name):
    return next((v["ItemId"] for v in t.api.get("/Library/VirtualFolders") if v["Name"] == name), None)


def add_library(t, name, kind, path, expect_type, expect_count, timeout=120):
    """Adds the library, scans only it and waits for `expect_count` items of `expect_type`.
    Returns (library id, item ids)."""
    t.api.post(f"/Library/VirtualFolders?name={urllib.parse.quote(name)}&collectionType={kind}"
               f"&paths={urllib.parse.quote(path, safe='')}&refreshLibrary=false",
               {"LibraryOptions": {"EnableRealtimeMonitor": False}})
    lib = library_id(t, name)
    t.api.post(f"/Items/{lib}/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default")
    ids, t0 = [], time.time()
    while time.time() - t0 < timeout:
        ids = [i["Id"].lower() for i in t.api.get(
            f"/Items?userId={t.api.user}&ParentId={lib}&IncludeItemTypes={expect_type}&Recursive=true").get("Items", [])]
        if len(ids) >= expect_count:
            break
        t.wait(2)
    t.equal(len(ids), expect_count, f"native {expect_type} items scanned into {name}")
    return lib, ids


def rows_under(t, paths):
    """Items (folders left out) whose path lies below any of the paths, virtual seasons included."""
    where = " or ".join("Path like ?" for _ in paths)
    return t.db.one(f"select count(*) from BaseItems where ({where}) and Type not like '%Folder'",
                    tuple(p + "/%" for p in paths))[0]


def remove_libraries(t, libraries):
    """Removes the movies and series below the libraries' paths, the libraries and their folders."""
    paths = [path for _, _, path in libraries]
    where = " or ".join("Path like ?" for _ in paths)
    for (item_id,) in t.db.query(
            f"select lower(replace(Id,'-','')) from BaseItems where ({where}) "
            "and (Type like '%Movies.Movie' or Type like '%TV.Series')", tuple(p + "/%" for p in paths)):
        t.api.call("DELETE", f"/Items/{item_id}")
    names = {name for name, _, _ in libraries}
    for v in t.api.get("/Library/VirtualFolders"):
        if v["Name"] in names:
            t.api.call("DELETE", f"/Library/VirtualFolders?name={urllib.parse.quote(v['Name'])}&refreshLibrary=false")
    t.sh("rm -rf " + " ".join(f"'{p}'" for p in paths))


# ---- local series whose tree Gelato extends ("Extend local series trees")

def series_in(t, library):
    return [i["Id"].lower() for i in t.api.get(
        f"/Items?userId={t.api.user}&ParentId={library}&IncludeItemTypes=Series&Recursive=true").get("Items", [])]


def episode_tree(t, series_id):
    """{(season, episode): episode id} as a client sees it."""
    return {(e.get("ParentIndexNumber"), e.get("IndexNumber")): e["Id"].lower()
            for e in t.api.get(f"/Shows/{series_id}/Episodes?userId={t.api.user}").get("Items", [])}


def season_numbers(t, series_id):
    return sorted(s.get("IndexNumber") for s in t.api.get(f"/Shows/{series_id}/Seasons?userId={t.api.user}").get("Items", []))


def open_local_series(t, series_id, local_episodes=1, timeout=120):
    """Opens the series page, which extends the tree when the option is on, and returns the episode
    tree once it grew past the local episodes (or after the timeout)."""
    t.api.item(series_id)
    waited = 0
    while waited < timeout:
        if len(episode_tree(t, series_id)) > local_episodes:
            t.wait(5)  # seasons are saved before their episodes
            break
        t.wait(3)
        waited += 3
    return episode_tree(t, series_id)


def gelato_episodes(t, series_id):
    """Ids of the episodes Gelato added below the series (the ones with a Stremio id)."""
    return [r[0] for r in t.db.query(
        "select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where b.Type like '%TV.Episode' and lower(replace(b.SeriesId,'-',''))=?", (series_id,))]
