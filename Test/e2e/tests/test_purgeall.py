DESCRIPTION = "Purge all Gelato items: every Gelato item and stream row goes, watch state is cleared, not parked, and a catalog import brings the library back"
DESTRUCTIVE = True  # deletes every Gelato item of the instance, then imports the catalogs again

from jfapi.db import STREAM_TAG, norm

TYPES = ("Movies.Movie", "TV.Series", "TV.Season", "TV.Episode", "Movies.BoxSet")


def counts(t):
    """{type: (Gelato items, other items)} plus the stream rows."""
    out = {}
    for tp in TYPES:
        gelato, other = t.db.one(
            "select sum(case when p.ItemId is not null then 1 else 0 end), sum(case when p.ItemId is null then 1 else 0 end) "
            "from BaseItems b left join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
            "where b.Type like ? and (b.Tags is null or b.Tags not like ?)", ("%" + tp, STREAM_TAG))
        out[tp.split(".")[1]] = (gelato or 0, other or 0)
    out["stream rows"] = t.db.one("select count(*) from BaseItems where Tags like ?", (STREAM_TAG,))[0]
    return out


def state_by_key(t, item_id):
    """{CustomDataKey: (Played, IsFavorite, PlaybackPositionTicks)} of the user's watch state, found by
    the item's keys wherever it sits (on the item, or parked on the placeholder after a delete)."""
    return {k: (p, f, pos) for k, p, f, pos in t.db.query(
        "select CustomDataKey, Played, IsFavorite, PlaybackPositionTicks from UserData where lower(replace(UserId,'-',''))=? "
        "and CustomDataKey in (select CustomDataKey from UserData where lower(replace(ItemId,'-',''))=?)",
        (norm(t.api.user), norm(item_id)))}


def run(t):
    movie = t.movie()
    t.api.mark_played(movie, True)
    t.api.post(f"/UserFavoriteItems/{movie}?userId={t.api.user}")
    movie_stremio = t.db.stremio_id(movie)
    keys = state_by_key(t, movie)
    t.log("the movie's watch state by key:", keys)
    t.check(any(p for p, _, _ in keys.values()), "the movie is played before the purge")

    before = counts(t)
    t.log("before:", before)
    t.check(before["Movie"][0] > 0, "Gelato movies to purge")

    status, msg = t.api.run_task("PurgeGelatoTask", timeout=1800)
    t.equal(status, "Completed", f"purge finished {msg}")
    after = counts(t)
    t.log("after:", after)
    t.equal({k: v[0] for k, v in after.items() if k != "stream rows"}, {k: 0 for k in after if k != "stream rows"},
            "no Gelato movie, series, season, episode or collection left")
    t.equal(after["stream rows"], 0, "no stream row left")
    t.equal({k: v[1] for k, v in after.items() if k != "stream rows"}, {k: v[1] for k, v in before.items() if k != "stream rows"},
            "items without a Stremio id kept")
    t.equal(t.api.call("GET", f"/Items/{movie}?userId={t.api.user}")[0], 404, "the movie's page is gone")

    parked = t.db.query(
        "select CustomDataKey, Played, IsFavorite, PlaybackPositionTicks from UserData where lower(replace(UserId,'-',''))=? "
        "and CustomDataKey in ({})".format(",".join("?" * len(keys))), (norm(t.api.user), *keys)) if keys else []
    t.log("the movie's watch state after the purge:", parked)
    t.equal([r for r in parked if r[1] or r[2] or r[3]], [], "no played flag, favourite or position kept for the movie's keys")

    t.log("== import the catalogs again")
    status, msg = t.api.run_task("GelatoCatalogItemsSync", timeout=3600)
    t.equal(status, "Completed", f"catalog import finished {msg}")
    t.api.wait_tasks_idle("RefreshLibrary", 1800)
    again = counts(t)
    t.log("after the import:", again)
    t.check(again["Movie"][0] > 0, "Gelato movies are back")
    back = t.db.one(
        "select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where p.ProviderValue=? and b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like ?)", (movie_stremio, STREAM_TAG))
    if back is None:
        t.log(f"the movie ({movie_stremio}) is not in a catalog (inserted from search), so it does not come back")
        return
    d = t.api.item(back[0])
    ud = d.get("UserData") or {}
    t.check(not ud.get("Played") and not ud.get("IsFavorite") and not ud.get("PlaybackPositionTicks"),
            f"the imported movie starts fresh: {ud}")
    t.check(len(d.get("MediaSources") or []) >= 1, "opening the imported movie syncs its streams again")
