DESCRIPTION = "'Download missing subtitles' twice per save setting, on a small library of its own: stream rows get one file in their metadata folder, placeholders nothing, the second run downloads nothing again (prod findings 5 and 15)"
DESTRUCTIVE = True  # a movies and a shows library for the second user, subtitle download languages on them, none on the others meanwhile

import re

from jfapi.bootstrap import GELATO
from jfapi.db import norm

LANGUAGES = ["eng", "ger"]  # prod's libraries download English and German
SUBTITLE = r"-name '*.vtt' -o -name '*.srt' -o -name '*.ass' -o -name '*.ssa' -o -name '*.sub' -o -name '*.smi'"
IN_LIBRARY = re.compile(r"/library/[0-9a-f]{2}/([0-9a-f]{32})/[^/]+$")
NUMBERED = re.compile(r"\.[a-z]{2,3}\.\d+\.[a-z]+$")
LANGUAGE = re.compile(r"\.([a-z]{2,3})\.[a-z]+$")

# The task looks at every movie and episode of a library that has download languages, and asks the
# addon for each: on a copy of a real library that is thousands of requests and minutes per run. One
# movie and one short series in libraries of their own show the same.
MOVIE_LIB, SERIES_LIB = "Subs movies jfapi", "Subs shows jfapi"
MOVIE_PATH, SERIES_PATH = "/tmp/gelato/jfapi-subs-movies", "/tmp/gelato/jfapi-subs-series"
MOVIE_TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs"]
SERIES_TERMS = ["Adolescence", "Chernobyl", "Baby Reindeer", "Beef", "Ripley"]  # a few episodes each


def files(t):
    """Subtitle files Jellyfin can have written: under /config and /media, and in / (the process's
    working directory, where a stream row's subtitle went with SaveSubtitlesWithMedia on)."""
    found = t.sh(f"find /config /media \\( {SUBTITLE} \\) -type f 2>/dev/null").split("\n")
    cwd = t.sh(f"find / -maxdepth 1 \\( {SUBTITLE} \\) -type f 2>/dev/null").split("\n")
    return sorted(f for f in found if f), sorted(f for f in cwd if f)


def invalid_paths(t):
    """Downloads Jellyfin rejected because the target path was invalid (a placeholder's gelato:// path)."""
    out = t.sh("cat /config/log/log_*.log 2>/dev/null | grep -c 'resulting path was invalid'").strip()
    return int(out or 0)


def run_twice(t, label, stream_rows):
    before, _ = files(t)
    invalid = invalid_paths(t)
    status, msg = t.api.run_task("DownloadSubtitles", timeout=3600)
    t.equal(status, "Completed", f"{label}, first run finished {msg}")
    first, cwd = files(t)
    new = sorted(set(first) - set(before))
    t.log(f"{label}, first run: {len(new)} new file(s), {len(first)} in all")
    status, msg = t.api.run_task("DownloadSubtitles", timeout=3600)
    t.equal(status, "Completed", f"{label}, second run finished {msg}")
    second, cwd2 = files(t)
    # What a row has after the first run it is not given again: that is the skip. A subtitle the
    # addon only answered with on the second run is late, not a repeat.
    key = lambda f: (IN_LIBRARY.search(f).group(1) if IN_LIBRARY.search(f) else f, LANGUAGE.search(f).group(1) if LANGUAGE.search(f) else "")
    had = {key(f) for f in first}
    late = sorted(set(second) - set(first))
    if late:
        t.log(f"{label}: {len(late)} file(s) came with the second run only")
    t.equal([f for f in late if key(f) in had], [], f"{label}: the second run downloads nothing a row already has")
    t.equal(cwd + cwd2, [], f"{label}: no subtitle in the working directory")
    t.equal(invalid_paths(t) - invalid, 0, f"{label}: no download rejected for an invalid path")

    # Only what these two runs wrote: the metadata folder is shared with earlier runs (on Linux it is a
    # volume every instance mounts), and a file some older build left there says nothing about this one.
    written = sorted(set(second) - set(before))
    t.log(f"{label}: {len(written)} file(s) written by the two runs, {len(before)} already there")
    misplaced = [f for f in written if (m := IN_LIBRARY.search(f)) and m.group(1) not in stream_rows]
    t.equal(misplaced, [], f"{label}: every saved subtitle belongs to a stream row, none to a placeholder")
    t.equal([f for f in written if NUMBERED.search(f)], [], f"{label}: no numbered copies (.en.0.vtt)")
    return new


def run(t):
    api, db, u2 = t.api, t.db, t.user2
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old_users = cfg.get("UserConfigs")

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

    def in_library(stremio):
        return db.one("select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                      "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

    def parent_path(item):
        row = db.one("select (select f.Path from BaseItems f where f.Id=b.ParentId) from BaseItems b where lower(replace(b.Id,'-',''))=?", (item,))
        return row[0] if row else None

    def open_fresh(terms, kind):
        """The second user opens the first search hit the library does not have: it goes into that
        user's folder, the small library."""
        for term in terms:
            for hit in u2.search(term, kind, limit=8):
                m = re.search(r"(tt\d+)", hit.get("Path") or "")
                if m and hit.get("Name", "").lower().startswith(term.lower()[:5]) and not in_library(m.group(1)):
                    item = u2.item(hit["Id"]).get("Id", "").replace("-", "").lower()
                    t.log(f"{kind} opened from search: {hit['Name']} ({m.group(1)}) as {item[:8]}")
                    return item
        return None

    added, inserted, originals, small, saved = [], [], {}, set(), []
    try:
        t.sh(f"mkdir -p {MOVIE_PATH} {SERIES_PATH}")
        for name, kind, path in ((MOVIE_LIB, "movies", MOVIE_PATH), (SERIES_LIB, "tvshows", SERIES_PATH)):
            if library(name) is None:
                api.post(f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&collectionType={kind}"
                         f"&paths={path.replace('/', '%2F')}&refreshLibrary=false", {"LibraryOptions": {"EnableRealtimeMonitor": False}})
                added.append(name)
        users = [u for u in old_users or [] if norm(u["UserId"]) != norm(u2.user)]
        users.append({"UserId": u2.user, "Url": cfg["Url"], "MoviePath": MOVIE_PATH, "SeriesPath": SERIES_PATH, "DisableSearch": False})
        api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "UserConfigs": users})
        u2.search("gelato")  # a request of the user: Gelato seeds the user's folders, an empty one is no library folder
        api.post("/Library/Refresh")
        t.require(t.folders_ready(MOVIE_PATH, SERIES_PATH), "the folders of the two small libraries did not appear")

        # stream rows to download for: a movie and an episode, synced by opening them
        movie = open_fresh(MOVIE_TERMS, "Movie")
        t.require(movie, "no movie search hit the library does not have yet")
        inserted.append(movie)
        api.settle_insert()
        t.equal(parent_path(movie), MOVIE_PATH, "the movie is in the small movies library")
        u2.sources(movie)
        series = open_fresh(SERIES_TERMS, "Series")
        if series:
            inserted.append(series)
            t.settle(after=1, timeout=120)  # the series' tree is saved in the background
            episodes = t.episodes(series, 1)
            if episodes:
                u2.sources(episodes[0]["Id"])
        else:
            t.log("no series search hit the library does not have yet, movie rows only")
        stream_rows = db.stream_row_ids()

        libraries = api.get("/Library/VirtualFolders")
        originals = {v["ItemId"]: v["LibraryOptions"] for v in libraries}
        small = {v["ItemId"] for v in libraries if v.get("Name") in (MOVIE_LIB, SERIES_LIB)}
        # The task takes every library that has download languages: a copy of prod's has them on
        # the Gelato libraries.
        for v in libraries:
            if v["ItemId"] not in small and v["LibraryOptions"].get("SubtitleDownloadLanguages"):
                api.post("/Library/VirtualFolders/LibraryOptions", {"Id": v["ItemId"], "LibraryOptions": {**v["LibraryOptions"], "SubtitleDownloadLanguages": []}})
        t.log(f"{len(stream_rows)} stream rows, the task runs on {[v['Name'] for v in libraries if v['ItemId'] in small]}")

        # prod runs with SaveSubtitlesWithMedia on; "off" is the setup where subtitles showed up before the fix
        for with_media in (True, False):
            label = f"SaveSubtitlesWithMedia {'on' if with_media else 'off'}"
            for item_id in small:
                opts = {**originals[item_id], "SubtitleDownloadLanguages": LANGUAGES, "SaveSubtitlesWithMedia": with_media,
                        "SkipSubtitlesIfAudioTrackMatches": False, "RequirePerfectSubtitleMatch": False,
                        "DisabledSubtitleFetchers": [f for f in originals[item_id].get("DisabledSubtitleFetchers") or [] if f != "Gelato Subtitles"]}
                api.post("/Library/VirtualFolders/LibraryOptions", {"Id": item_id, "LibraryOptions": opts})
            saved += run_twice(t, label, stream_rows)

        check_saved(t, saved)
    finally:
        for item_id, opts in originals.items():
            if item_id not in small and opts.get("SubtitleDownloadLanguages"):
                api.post("/Library/VirtualFolders/LibraryOptions", {"Id": item_id, "LibraryOptions": opts})
        for item in inserted:
            api.delete_inserted(item)
        api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "UserConfigs": old_users})
        for name in added:
            api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
        t.sh(f"rm -rf {MOVIE_PATH} {SERIES_PATH}")


def check_saved(t, saved):
    u2 = t.user2
    if not saved:
        t.log("the addon had no subtitle for any stream row in", LANGUAGES, "so the skip on an already saved file was not exercised")
        return
    rows = {IN_LIBRARY.search(f).group(1) for f in saved if IN_LIBRARY.search(f)}
    linked = t.db.query("select lower(replace(Id,'-','')), lower(replace(PrimaryVersionId,'-','')) from BaseItems "
                        "where PrimaryVersionId is not null and lower(replace(Id,'-','')) in ({})".format(",".join("?" * len(rows))), tuple(rows))
    t.log(f"{len(saved)} file(s) saved for {len(rows)} stream row(s), {len(linked)} of them linked to their title")
    # finding 15: the "already saved" skip has to find linked rows, which Jellyfin 12 leaves out of plain queries
    t.check(linked, "subtitles were saved for linked stream rows, so the second runs checked the skip on them")
    # rows belong to the user whose visit synced them: the second user's PlaybackInfo lists them
    for row, owner in linked:
        st, pi = u2.call("POST", f"/Items/{owner}/PlaybackInfo?userId={u2.user}", {"UserId": u2.user, "MediaSourceId": row}, timeout=300)
        t.check(st == 200, f"PlaybackInfo answers ({st})")
        if st != 200:
            return
        src = next((s for s in pi.get("MediaSources", []) if norm(s["Id"]) == row), None)
        if src is None:
            continue
        ext = [m for m in src.get("MediaStreams", []) if m.get("Type") == "Subtitle" and m.get("IsExternal")]
        t.check(ext, f"PlaybackInfo offers the saved subtitle on row {row[:8]}: {[(m.get('Language'), m.get('Codec')) for m in ext]}")
        break
    else:
        t.log(f"none of the {len(linked)} rows with a subtitle belongs to {u2.name}; PlaybackInfo not checked")
