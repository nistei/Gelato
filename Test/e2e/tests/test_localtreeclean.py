DESCRIPTION = "The tree clean-up ('Sync series trees' with 'Extend local series trees' off) takes back the virtual items and leaves the local series' own episodes and seasons, a Stremio id on them included (issue 153)"
DESTRUCTIVE = True  # adds a library and runs the tree sync over every series of the library; removes the library again

from jfapi.bootstrap import GELATO
from jfapi.native import (add_library, episode_tree, remove_libraries, rows_under, season_numbers,
                          series_in, write_videos)

LIBRARY, PATH = "jfapi-treeclean", "/tmp/jfapi-treeclean"
LIBRARIES = [(LIBRARY, "tvshows", PATH)]
SHOW = "Breaking Bad (2008) [imdbid-tt0903747]"
EPISODES = [f"{SHOW}/Season 01/Breaking Bad S01E0{n}.mkv" for n in (1, 2)]
TASK = "SyncSeriesTrees"
# A Stremio id on a file the library scanned. Gelato's metadata providers used to leave theirs on
# local items, so a library that ran an older version carries them; mixed mode is the other way an
# item of the library and Gelato's own data meet on one row.
STREMIO_ID = "series:tt0903747:1:1"


def local_rows(t):
    """The file-backed items below the library path, as the database has them."""
    return sorted(t.db.query(
        "select Type, Name, Path, ParentIndexNumber, IndexNumber from BaseItems "
        "where Path like ? and Type not like '%Folder'", (PATH + "/%",)))


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{PATH}/{e}" for e in EPISODES])
    try:
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": False})
        lib, episodes = add_library(t, LIBRARY, "tvshows", PATH, "Episode", len(EPISODES))
        series = series_in(t, lib)
        if len(episodes) != len(EPISODES) or len(series) != 1:
            return
        series_id = series[0]
        scanned = local_rows(t)
        tree = episode_tree(t, series_id)
        t.log(f"after the scan: seasons {season_numbers(t, series_id)}, episodes {sorted(tree)}")
        t.equal(sorted(tree), [(1, 1), (1, 2)], "the local series has its two episodes")

        # 1. The clean-up has nothing of Gelato's to remove here, and must leave the library alone.
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees {msg}")
        t.wait(5)
        t.equal(local_rows(t), scanned, "the task leaves the local series as the scan left it")

        # 2. The clean-up still does its work: extend the tree, turn the option off again, and the
        #    virtual seasons and episodes go while the two local episodes stay.
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": True})
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees with the option on {msg}")
        t.wait(5)
        extended = episode_tree(t, series_id)
        t.log(f"extended: seasons {season_numbers(t, series_id)}, {len(extended)} episodes")
        t.check(len(extended) > len(tree), f"the task extends the local series ({len(extended)} episodes)")
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": False})
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees with the option off again {msg}")
        t.wait(5)
        t.log(f"cleaned: seasons {season_numbers(t, series_id)}, episodes {sorted(episode_tree(t, series_id))}")
        t.equal(sorted(episode_tree(t, series_id)), sorted(tree), "the clean-up takes the added episodes back")
        t.equal(local_rows(t), scanned, "the clean-up leaves the local episodes and their season")

        # 3. A local episode that carries a Stremio id is still the library's file, not an item
        #    Gelato added: the clean-up must not delete it, and must not take the season with it.
        first = episode_tree(t, series_id)[(1, 1)]
        dto = t.api.item(first, "ProviderIds")
        t.api.post(f"/Items/{first}", {**dto, "ProviderIds": {**(dto.get("ProviderIds") or {}), "Stremio": STREMIO_ID}})
        t.wait(2)
        stamped = t.db.one("select exists(select 1 from BaseItemProviders p where p.ItemId=b.Id and lower(p.ProviderId)='stremio') "
                           "from BaseItems b where lower(replace(Id,'-',''))=?", (first,))[0]
        if not stamped:
            t.skip("the local episode did not take a Stremio id, so the clean-up has nothing to spare")
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees after the id was set {msg}")
        t.wait(5)
        after = episode_tree(t, series_id)
        t.log(f"after the clean-up: seasons {season_numbers(t, series_id)}, episodes {sorted(after)}")
        t.equal(t.api.call("GET", f"/Items/{first}?userId={t.api.user}")[0], 200,
                "the local episode with a Stremio id is still in the library")
        t.equal(sorted(after), sorted(tree), "both local episodes are still under the series")
        t.equal(season_numbers(t, series_id), [1], "the local season is still there")
        t.equal([r[2] for r in local_rows(t)], [r[2] for r in scanned], "no local file lost its item")
    finally:
        t.api.post(f"/Plugins/{GELATO}/Configuration",
                   {**t.api.get(f"/Plugins/{GELATO}/Configuration"),
                    "ExtendLocalSeriesTrees": cfg.get("ExtendLocalSeriesTrees", False)})
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [PATH]), 0, "the local series and its tree removed again")
