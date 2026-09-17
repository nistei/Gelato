DESCRIPTION = "Extend local series trees: a library scan keeps the seasons and episodes Gelato added to a local series, watch state included"
DESTRUCTIVE = True  # adds a library, turns the option on and runs a full library scan; removes the library again

from jfapi.bootstrap import GELATO
from jfapi.native import add_library, episode_tree, open_local_series, remove_libraries, rows_under, season_numbers, write_videos

LIBRARY, PATH = "jfapi-localtree-scan", "/tmp/jfapi-localtree-scan"
LIBRARIES = [(LIBRARY, "tvshows", PATH)]
# One local episode of a show the addon knows: Gelato adds the other seasons and episodes.
SHOW = "Breaking Bad (2008) [imdbid-tt0903747]"
LOCAL_EPISODE = f"{SHOW}/Season 01/Breaking Bad S01E01.mkv"


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{PATH}/{LOCAL_EPISODE}"])
    try:
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": True})
        lib, episodes = add_library(t, LIBRARY, "tvshows", PATH, "Episode", 1)
        series = [i["Id"].lower() for i in t.api.get(
            f"/Items?userId={t.api.user}&ParentId={lib}&IncludeItemTypes=Series&Recursive=true").get("Items", [])]
        if len(episodes) != 1 or len(series) != 1:
            return
        series_id = series[0]

        before = open_local_series(t, series_id)
        before_seasons = season_numbers(t, series_id)
        t.log(f"extended: seasons {before_seasons}, {len(before)} episodes")
        t.check(len(before) > 10 and 2 in before_seasons, f"opening the series extends its tree ({len(before)} episodes)")
        watched = before.get((2, 1))
        if watched is None:
            return
        t.api.mark_played(watched)

        status, msg = t.api.run_task("RefreshLibrary", timeout=1800)
        t.equal(status, "Completed", f"library scan {msg}")
        after = episode_tree(t, series_id)
        t.log(f"after the scan: seasons {season_numbers(t, series_id)}, {len(after)} episodes; "
              f"removed seasons in the database: {rows_under(t, [PATH])} items left below the library")
        t.equal(season_numbers(t, series_id), before_seasons, "the scan keeps the added seasons")
        t.equal(len(after), len(before), "the scan keeps the added episodes")
        st, _ = t.api.call("GET", f"/Items/{watched}?userId={t.api.user}")
        t.equal(st, 200, "S02E01 still exists after the scan")
        if st == 200:
            t.check(t.api.user_data(watched).get("Played") is True, "S02E01 is still played")
    finally:
        t.api.post(f"/Plugins/{GELATO}/Configuration",
                   {**t.api.get(f"/Plugins/{GELATO}/Configuration"), "ExtendLocalSeriesTrees": cfg.get("ExtendLocalSeriesTrees", False)})
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [PATH]), 0, "the local series and its tree removed again")
