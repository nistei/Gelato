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


def write_audio(t, paths, seconds=2):
    """Writes a short test track to every path (folders are created)."""
    cmds = [f"mkdir -p \"$(dirname '{p}')\" && {FFMPEG} -y -loglevel error -f lavfi "
            f"-i sine=frequency=440:duration={seconds} -c:a libmp3lame '{p}'" for p in paths]
    t.sh(" && ".join(cmds))
    written = t.sh("for f in " + " ".join(f"'{p}'" for p in paths) + "; do [ -s \"$f\" ] && echo x; done").split()
    t.equal(len(written), len(paths), "local audio files written")


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


# The top of each library's tree: deleting these takes their seasons, albums and tracks with them.
TOP_TYPES = ("%Movies.Movie", "%TV.Series", "%Audio.MusicArtist", "%Audio.MusicAlbum", "%Audio.Audio")


def remove_libraries(t, libraries):
    """Removes the items below the libraries' paths, the libraries and their folders."""
    paths = [path for _, _, path in libraries]
    where = " or ".join("Path like ?" for _ in paths)
    types = " or ".join("Type like ?" for _ in TOP_TYPES)
    for (item_id,) in t.db.query(
            f"select lower(replace(Id,'-','')) from BaseItems where ({where}) and ({types})",
            tuple(p + "/%" for p in paths) + TOP_TYPES):
        # An album or track already gone with its artist answers 404, which call() does not raise.
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
    tree once it stopped growing (or after the timeout).

    The tree has to settle before it is compared with anything: for a moment after the scan the local
    episode is listed under its file name with no season and episode number (prod finding 17), so it
    counts as an entry of its own next to the Gelato episode of the same slot and a snapshot taken
    then holds one entry more than the finished tree (seen as a 81 -> 80 "the scan removed an
    episode" failure). The tree counts as settled when it is past the local episodes, has no entry
    without numbers, and two polls agree on its size."""
    t.api.item(series_id)
    waited, size = 0, None
    while waited < timeout:
        tree = episode_tree(t, series_id)
        settled = len(tree) > local_episodes and all(s is not None and e is not None for s, e in tree)
        if settled and len(tree) == size:
            return tree
        size = len(tree) if settled else None
        t.wait(3)
        waited += 3
    return episode_tree(t, series_id)


def gelato_episodes(t, series_id):
    """Ids of the episodes Gelato added below the series (the ones with a Stremio id)."""
    return [r[0] for r in t.db.query(
        "select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where b.Type like '%TV.Episode' and lower(replace(b.SeriesId,'-',''))=?", (series_id,))]
