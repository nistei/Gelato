DESCRIPTION = "Gelato's tasks leave an item with Lock metadata alone: the tree sync neither rewrites nor renumbers a locked episode or season whose season/episode number was cleared, and the release date task keeps a locked EndDate (issue 73)"
DESTRUCTIVE = True  # runs the tree sync and the release date task over the whole library

from jfapi.db import STREAM_TAG, norm
from tests.test_seriestrees import pick_series  # one implementation of "a continuing Gelato series"

TREE_TASK = "SyncSeriesTrees"
DATES_TASK = "SyncReleaseDates"
EDITED = ("Name", "Overview", "IndexNumber", "ParentIndexNumber", "LockData")


def dto(t, item_id):
    return t.api.get(f"/Items/{item_id}?userId={t.api.user}&Fields=Overview")


def edit(t, item_id, **changes):
    """Saves the item's DTO with the changes, the way the edit dialog does."""
    t.api.post(f"/Items/{item_id}", {**dto(t, item_id), **changes})
    t.db.invalidate()


def state(t, item_id):
    """(name, overview, season, episode, locked) of the item in the database."""
    name, overview, season, index, locked = t.db.one(
        "select Name, Overview, ParentIndexNumber, IndexNumber, IsLocked from BaseItems "
        "where lower(replace(Id,'-',''))=?", (norm(item_id),))
    return name, overview or "", season, index, locked


def episode_rows(t, series_id):
    """{(season, episode): [ids]} of the series' episodes, stream rows left out. An episode whose
    numbers were cleared sits under (None, None)."""
    out = {}
    for ep_id, season, index in t.db.query(
            "select lower(replace(Id,'-','')), ParentIndexNumber, IndexNumber from BaseItems "
            "where Type like '%TV.Episode' and lower(replace(SeriesId,'-',''))=? and (Tags is null or Tags not like ?)",
            (norm(series_id), STREAM_TAG)):
        out.setdefault((season, index), []).append(ep_id)
    return out


def run(t):
    series_id, inserted = pick_series(t)
    if series_id is None:
        t.skip("no continuing Gelato series in the library or in the addon's search")
    items, movies = [], []
    try:
        eps = t.episodes(series_id, 1)
        if len(eps) < 3:
            t.skip(f"{series_id[:8]} has fewer than three episodes in season 1")
        cleared, kept, unlocked = (dto(t, e["Id"]) for e in eps[:3])
        seasons = [s for s in t.api.get(f"/Shows/{series_id}/Seasons?userId={t.api.user}")["Items"]
                   if (s.get("IndexNumber") or 0) >= 1]
        season = dto(t, seasons[-1]["Id"]) if seasons else None
        items = [cleared, kept, unlocked] + ([season] if season is not None else [])
        movies = [dto(t, m) for m in t.movies(2)]
        t.log(f"series {series_id[:8]}: locked+cleared {cleared['Id'][:8]}, locked {kept['Id'][:8]}, "
              f"unlocked+cleared {unlocked['Id'][:8]}, season {season['Id'][:8] if season is not None else '-'}")

        # A locked episode whose numbers someone cleared or changed: the sync misses it in its
        # number lookup, and the episode it builds for that number takes the same id, since the id
        # is the hash of the path. Saving it replaced the row, lock and all.
        edit(t, cleared["Id"], LockData=True, Name="MY OWN TITLE", Overview="my own overview",
             IndexNumber=None, ParentIndexNumber=None)
        # A locked episode with its numbers intact, carrying placeholder metadata (finding 14).
        edit(t, kept["Id"], LockData=True, Name=f"Episode {kept.get('IndexNumber')}", Overview="")
        # The same edit without the lock: the sync repairs it, and writes no second row for it.
        edit(t, unlocked["Id"], LockData=False, IndexNumber=None, ParentIndexNumber=None)
        if season is not None:
            edit(t, season["Id"], LockData=True, Name="MY SEASON", IndexNumber=None)
        for m in movies:
            edit(t, m["Id"], EndDate=None)
        edit(t, movies[0]["Id"], LockData=True)

        t.equal(state(t, cleared["Id"]), ("MY OWN TITLE", "my own overview", None, None, 1), "the locked episode is saved as edited")
        before = episode_rows(t, series_id)
        t.log(f"{sum(len(v) for v in before.values())} episode rows before the sync")

        status, msg = t.api.run_task(TREE_TASK, timeout=1800)
        t.equal(status, "Completed", f"{TREE_TASK} finished {msg}")

        t.equal(state(t, cleared["Id"]), ("MY OWN TITLE", "my own overview", None, None, 1),
                "the locked episode keeps its name, overview, cleared numbers and its lock")
        t.equal(state(t, kept["Id"]), (f"Episode {kept.get('IndexNumber')}", "", kept.get("ParentIndexNumber"), kept.get("IndexNumber"), 1),
                "the locked episode with its numbers intact keeps its placeholder metadata and its lock")
        t.equal(state(t, unlocked["Id"])[2:], (unlocked.get("ParentIndexNumber"), unlocked.get("IndexNumber"), 0),
                "the unlocked episode gets its numbers back from the addon")

        after = episode_rows(t, series_id)
        t.equal(sum(len(v) for v in after.values()), sum(len(v) for v in before.values()), "the same number of episode rows as before the sync")
        t.equal({k: v for k, v in after.items() if len(v) > 1}, {}, "no episode number twice")
        key = (cleared.get("ParentIndexNumber"), cleared.get("IndexNumber"))
        t.equal(after.get(key, []), [], f"no episode carries S{key[0]:02d}E{key[1]:02d} while the locked one has no numbers")

        if season is not None:
            t.equal(t.db.one("select Name, IndexNumber, IsLocked from BaseItems where lower(replace(Id,'-',''))=?", (norm(season["Id"]),)),
                    ("MY SEASON", None, 1), "the locked season keeps its name, cleared number and its lock")
            t.equal(len(t.db.query("select 1 from BaseItems where Type like '%TV.Season' and IndexNumber=? "
                                   "and lower(replace(SeriesId,'-',''))=?", (season.get("IndexNumber"), norm(series_id)))),
                    0, f"no season carries number {season.get('IndexNumber')} while the locked one has none")

        status, msg = t.api.run_task(DATES_TASK, timeout=1800)
        t.equal(status, "Completed", f"{DATES_TASK} finished {msg}")
        t.equal(t.db.one("select EndDate, IsLocked from BaseItems where lower(replace(Id,'-',''))=?", (norm(movies[0]["Id"]),)),
                (None, 1), "the locked movie keeps its empty EndDate")
        t.check(t.db.one("select EndDate from BaseItems where lower(replace(Id,'-',''))=?", (norm(movies[1]["Id"]),))[0] is not None,
                "the unlocked movie gets an EndDate")
    finally:
        for d in items:
            edit(t, d["Id"], **{k: d.get(k) for k in EDITED})
        for d in movies:
            edit(t, d["Id"], EndDate=d.get("EndDate"), LockData=d.get("LockData"))
        if inserted:
            t.api.call("DELETE", f"/Items/{series_id}")
