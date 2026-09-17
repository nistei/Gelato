DESCRIPTION = "Extend local series trees: a local series the sync task extended gets its tree back after a scan dropped the added seasons"
DESTRUCTIVE = True  # adds a library and runs the tree sync over every series of the library; removes the library again

from jfapi.bootstrap import GELATO
from jfapi.native import (add_library, episode_tree, open_local_series, remove_libraries, rows_under,
                          season_numbers, series_in, write_videos)

LIBRARY, PATH = "jfapi-localtree-tag", "/tmp/jfapi-localtree-tag"
LIBRARIES = [(LIBRARY, "tvshows", PATH)]
SHOW = "Breaking Bad (2008) [imdbid-tt0903747]"
LOCAL_EPISODE = f"{SHOW}/Season 01/Breaking Bad S01E01.mkv"
TASK = "SyncSeriesTrees"
TREE_TAG = "gelato-tree-synced"  # set by the task on a series it extended; it skips tagged series afterwards


def tags(t, series_id):
    return (t.db.one("select Tags from BaseItems where lower(replace(Id,'-',''))=?", (series_id,))[0] or "").split("|")


def drop_added_seasons(t, series_id):
    """The state a library scan leaves behind: the seasons Gelato added are gone (their paths do not
    exist on disk), the episodes it put in the local season stay."""
    for season in t.api.get(f"/Shows/{series_id}/Seasons?userId={t.api.user}").get("Items", []):
        if season.get("IndexNumber") != 1:
            t.api.call("DELETE", f"/Items/{season['Id']}")
    t.wait(2)
    return len(episode_tree(t, series_id))


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{PATH}/{LOCAL_EPISODE}"])
    try:
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": True})
        lib, episodes = add_library(t, LIBRARY, "tvshows", PATH, "Episode", 1)
        series = series_in(t, lib)
        if len(episodes) != 1 or len(series) != 1:
            return
        series_id = series[0]
        if t.api.item(series_id).get("Status") == "Continuing":
            t.skip("the local series is continuing: the sync task extends it on every run")

        # The tree has to be the task's work: opening the series extends it too, and only the task
        # marks the series, which is what makes it skip the series from then on.
        drop_added_seasons(t, series_id)
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees {msg}")
        t.wait(5)
        extended = episode_tree(t, series_id)
        t.log(f"after the task: seasons {season_numbers(t, series_id)}, {len(extended)} episodes, tags {tags(t, series_id)[-1:]}")
        t.check(len(extended) > 10, f"the task extends the local series ({len(extended)} episodes)")
        if len(extended) <= 10:
            return
        if TREE_TAG not in tags(t, series_id):
            t.skip("the series was extended without the task's mark, so the task would not skip it")

        left = drop_added_seasons(t, series_id)
        t.log(f"seasons dropped as a scan does: {left} episodes left, tags {tags(t, series_id)[-1:]}")
        t.check(left < len(extended), f"the added seasons are gone ({left} episodes left)")
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees again {msg}")
        t.wait(5)
        again = episode_tree(t, series_id)
        t.log(f"after the second task run: {len(again)} episodes, tags {tags(t, series_id)[-1:]}")
        t.equal(len(again), len(extended), "the task brings the tree back")

        # The other way a user gets it back: turn the option off and on and open the series again.
        if len(again) == len(extended):
            drop_added_seasons(t, series_id)
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": False})
        t.api.item(series_id)
        t.wait(5)
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": True})
        back = open_local_series(t, series_id)
        t.log(f"after the option was turned off and on: {len(back)} episodes, tags {tags(t, series_id)[-1:]}")
        t.equal(len(back), len(extended), "turning the option off and on again brings the tree back")
    finally:
        t.api.post(f"/Plugins/{GELATO}/Configuration",
                   {**t.api.get(f"/Plugins/{GELATO}/Configuration"), "ExtendLocalSeriesTrees": cfg.get("ExtendLocalSeriesTrees", False)})
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [PATH]), 0, "the local series and its tree removed again")
