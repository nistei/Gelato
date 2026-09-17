DESCRIPTION = "'Sync release dates' leaves native items alone: EndDate of local movies, series, seasons and episodes stays as their library set it"
DESTRUCTIVE = True  # adds two libraries with local files and runs the task over the whole library; removes the libraries again

from jfapi.native import add_library, remove_libraries, rows_under, write_videos

TASK = "SyncReleaseDates"
MOVIES_LIB, MOVIES_PATH = "jfapi-dates-movies", "/tmp/jfapi-dates-movies"
SHOWS_LIB, SHOWS_PATH = "jfapi-dates-shows", "/tmp/jfapi-dates-shows"
LIBRARIES = [(MOVIES_LIB, "movies", MOVIES_PATH), (SHOWS_LIB, "tvshows", SHOWS_PATH)]
# A title the addon knows, so the task finds a digital release for it, and one it cannot look up.
MOVIES = ["The Dark Knight (2008) [imdbid-tt0468569]", "Jfapi Dates Plain (2001)"]
SHOW = "Jfapi Dates Show (2003)"
EPISODES = [f"{SHOW}/Season 01/Jfapi Dates Show S01E0{n}.mkv" for n in (1, 2)]


def end_dates(t, own=None):
    """{(type, name): EndDate} of the native items. `own` False keeps the ones Gelato does not own
    (no Stremio id), True the ones it does; None every item."""
    rows = t.db.query(
        "select Type, Name, EndDate, exists(select 1 from BaseItemProviders p where p.ItemId=b.Id and lower(p.ProviderId)='stremio') "
        "from BaseItems b where (Path like ? or Path like ?) and Type not like '%Folder'",
        (MOVIES_PATH + "/%", SHOWS_PATH + "/%"))
    return {(kind.rsplit(".", 1)[-1], name): end for kind, name, end, stremio in rows
            if own is None or bool(stremio) == own}


def run(t):
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{MOVIES_PATH}/{m}/{m}.mkv" for m in MOVIES] + [f"{SHOWS_PATH}/{e}" for e in EPISODES])
    try:
        _, movies = add_library(t, MOVIES_LIB, "movies", MOVIES_PATH, "Movie", len(MOVIES))
        shows_lib, episodes = add_library(t, SHOWS_LIB, "tvshows", SHOWS_PATH, "Episode", len(EPISODES))
        series = [i["Id"] for i in t.api.get(
            f"/Items?userId={t.api.user}&ParentId={shows_lib}&IncludeItemTypes=Series&Recursive=true").get("Items", [])]
        if len(movies) != len(MOVIES) or len(episodes) != len(EPISODES) or len(series) != 1:
            return
        # What a metadata provider gives a running show: a premiere date, no end date.
        show = t.api.get(f"/Items/{series[0]}?userId={t.api.user}")
        show.update({"PremiereDate": "2003-05-01T00:00:00.0000000Z", "ProductionYear": 2003, "Status": "Continuing", "EndDate": None})
        t.api.post(f"/Items/{series[0]}", show)
        t.wait(2)

        # The library scan is the second place an EndDate can land on a native item: Gelato's
        # metadata providers answer for every item with an imdb or tmdb id, and build their result
        # from a Gelato item, which always carries one.
        ids = {t.api.item(m).get("Name"): sorted((t.api.item(m, "ProviderIds").get("ProviderIds") or {})) for m in movies}
        t.check(any(("Imdb" in v or "Tmdb" in v) for v in ids.values()), f"a native movie has an id to look up: {ids}")
        before = end_dates(t)
        t.log("EndDate before the task:", before)
        # The rule is about the items Gelato does not own, so an item that picked up a Stremio id
        # along the way is left to the checks below rather than failing here.
        scanned = end_dates(t, own=False)
        t.equal(set(scanned.values()) or {None}, {None}, f"the scan leaves native EndDate empty: {scanned}")
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync release dates {msg}")
        after = end_dates(t)
        t.log("EndDate after the task:", after)
        for key in sorted(before, key=str):
            t.equal(after.get(key), before[key], f"{key[0]} {key[1]!r}: EndDate unchanged")
        t.equal(t.api.item(series[0]).get("EndDate"), None, "the running show reports no end date")
    finally:
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [MOVIES_PATH, SHOWS_PATH]), 0, "native items removed again")
