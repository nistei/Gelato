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


def stream_rows(t, item_id):
    """How many stream rows are linked to the item, straight from the database."""
    return t.db.one("select count(*) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=? and Tags like ?",
                    (item_id, "%gelato-stream%"))[0]


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

        # What the clean-up goes by: an episode it added has a gelato:// path, a season it added
        # carries a Stremio id. It keeps everything else, so a version of the sync that gave its
        # items other paths would leave them below the local series for good.
        marks = t.db.query(
            "select b.Type, b.Path, exists(select 1 from BaseItemProviders p where p.ItemId=b.Id and lower(p.ProviderId)='stremio') "
            "from BaseItems b where lower(replace(b.SeriesId,'-',''))=? and (b.Type like '%TV.Episode' or b.Type like '%TV.Season')",
            (series_id,))
        added = [(kind.rsplit(".", 1)[-1], path or "", bool(stremio)) for kind, path, stremio in marks
                 if not (path or "").startswith(PATH)]
        t.log(f"items Gelato added: {len(added)}, first {added[:2]}")
        t.check(len(added) > 0, f"the task added items of its own ({len(added)})")
        t.equal([p for _, p, _ in added if not p.startswith("gelato://")], [],
                "every item Gelato added has a gelato:// path")
        t.equal([k for k, _, has_id in added if not has_id], [], "every item Gelato added carries a Stremio id")

        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": False})
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees with the option off again {msg}")
        t.wait(5)
        t.log(f"cleaned: seasons {season_numbers(t, series_id)}, episodes {sorted(episode_tree(t, series_id))}")
        t.equal(sorted(episode_tree(t, series_id)), sorted(tree), "the clean-up takes the added episodes back")
        t.equal(local_rows(t), scanned, "the clean-up leaves the local episodes and their season")

        # 3. Mixed mode puts Gelato's streams on the library's own episodes as linked rows. The
        #    clean-up runs in that state too, and has to leave the rows alone.
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": False, "EnableMixed": True})
        local_first = episode_tree(t, series_id)[(1, 1)]
        sources = len(t.api.sources(local_first))
        t.wait(3)
        rows_before = stream_rows(t, local_first)
        t.log(f"mixed mode: {sources} sources, {rows_before} stream rows on the local episode")
        if rows_before == 0:
            t.log("no streams synced for the local episode, so the clean-up has none to spare here")
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees in mixed mode {msg}")
        t.wait(5)
        # From the database: asking the API for the sources again would sync them anew and hide a
        # deletion.
        t.equal(stream_rows(t, local_first), rows_before, "the local episode keeps its stream rows")
        t.equal(local_rows(t), scanned, "mixed mode does not change the local items either")
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": False, "EnableMixed": False})

        # 4. A local episode that carries a Stremio id is still the library's file, not an item
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
                    "ExtendLocalSeriesTrees": cfg.get("ExtendLocalSeriesTrees", False),
                    "EnableMixed": cfg.get("EnableMixed", False)})
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [PATH]), 0, "the local series and its tree removed again")
