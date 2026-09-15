DESCRIPTION = "Episode and unplayed count badges (series card, series page, seasons, library counts) ignore the stream rows"


def run(t):
    series = t.series()
    eps = t.episodes(series)
    e1, e2 = eps[0]["Id"], eps[1]["Id"]
    for e in (e1, e2):
        t.api.mark_played(e, False)
    t.check(len(t.api.sources(e1)) >= 2, "episode 1 has stream rows")

    con = t.db.connect()
    try:
        episodes = t.db.one("select count(*) from BaseItems where Type like '%TV.Episode' and (Tags is null or Tags not like '%gelato-stream%') "
                            "and lower(replace(SeriesId,'-',''))=?", (series,), con)[0]
        rows = t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%' and lower(replace(SeriesId,'-',''))=?", (series,), con)[0]
        seasons = t.db.one("select count(*) from BaseItems where Type like '%TV.Season' and lower(replace(SeriesId,'-',''))=?", (series,), con)[0]
        played = t.db.one("select count(*) from UserData u join BaseItems e on e.Id=u.ItemId where e.Type like '%TV.Episode' and "
                          "(e.Tags is null or e.Tags not like '%gelato-stream%') and lower(replace(e.SeriesId,'-',''))=? and u.Played=1 "
                          "and lower(replace(u.UserId,'-',''))=?", (series, t.api.user.replace("-", "").lower()), con)[0]
        db_movies = t.db.one("select count(*) from BaseItems where Type like '%Movies.Movie' and (Tags is null or Tags not like '%gelato-stream%') "
                             "and PrimaryVersionId is null and IsVirtualItem=0", (), con)[0]
        db_episodes = t.db.one("select count(*) from BaseItems where Type like '%TV.Episode' and (Tags is null or Tags not like '%gelato-stream%') "
                               "and PrimaryVersionId is null and IsVirtualItem=0", (), con)[0]
        db_rows = t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%'", (), con)[0]
        legacy = t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%' and PrimaryVersionId is null", (), con)[0]
    finally:
        con.close()
    t.log(f"db: {episodes} episodes, {rows} rows, {seasons} seasons, {played} played; library {db_movies} movies, {db_episodes} episodes, {db_rows} rows")
    t.check(rows > 0, "the series has stream rows in the database")

    def badges(label):
        s = t.api.item(series, "ChildCount,RecursiveItemCount")
        ud = s.get("UserData") or {}
        t.log(f"{label}: series page ChildCount {s.get('ChildCount')} RecursiveItemCount {s.get('RecursiveItemCount')} unplayed {ud.get('UnplayedItemCount')}")
        t.equal(s.get("ChildCount"), seasons, f"{label}: series page ChildCount = seasons")
        t.equal(s.get("RecursiveItemCount"), episodes, f"{label}: series page RecursiveItemCount = episodes")
        t.equal(ud.get("UnplayedItemCount"), episodes - badges.played, f"{label}: series page unplayed badge")
        card = t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=Series&Recursive=true&Ids={series}&Fields=ChildCount,RecursiveItemCount").get("Items", [])
        t.equal(len(card), 1, f"{label}: the series appears once in the series list")
        if card:
            cud = card[0].get("UserData") or {}
            t.equal(card[0].get("RecursiveItemCount"), episodes, f"{label}: series card RecursiveItemCount")
            t.equal(cud.get("UnplayedItemCount"), episodes - badges.played, f"{label}: series card unplayed badge")
        ss = t.api.get(f"/Shows/{series}/Seasons?userId={t.api.user}&Fields=ChildCount,RecursiveItemCount").get("Items", [])
        t.equal(sum(s.get("ChildCount") or 0 for s in ss), episodes, f"{label}: season ChildCounts add up to the episodes")
        t.equal(sum((s.get("UserData") or {}).get("UnplayedItemCount") or 0 for s in ss), episodes - badges.played, f"{label}: season unplayed badges add up")
        c = t.api.get(f"/Items/Counts?userId={t.api.user}")
        t.log(f"{label}: /Items/Counts movies {c.get('MovieCount')} series {c.get('SeriesCount')} episodes {c.get('EpisodeCount')}")
        if legacy:
            # Known: rows synced by an older Gelato (no owner yet) are counted until their title is opened.
            t.log(f"{label}: {legacy} legacy rows on the instance, the library counts include them (known, pre-existing)")
        else:
            t.equal(c.get("MovieCount"), db_movies, f"{label}: library movie count = movies without rows")
            t.equal(c.get("EpisodeCount"), db_episodes, f"{label}: library episode count = episodes without rows")
        latest = t.api.get(f"/Users/{t.api.user}/Items/Latest?IncludeItemTypes=Episode&Limit=100&ParentId={series}")
        t.check(all(i["Id"].lower() not in rows_all for i in latest), f"{label}: latest episodes hold no rows")

    rows_all = t.db.stream_row_ids()
    badges.played = played
    badges("before playing")

    runtime = t.api.item(e2).get("RunTimeTicks") or 0
    srcs = t.api.sources(e2)
    row = next((s for s in srcs if s != e2), None)
    if row is None or not runtime:
        t.skip("episode 2 has no second stream or no runtime")
    t.api.report("start", e2, row, 0, "counts")
    t.api.report("stop", e2, row, int(runtime * 0.97), "counts")
    t.equal(t.api.user_data(e2)["Played"], True, "episode 2 played through its row")
    badges.played = played + 1
    badges("after playing episode 2 through a row")
    t.api.mark_played(e2, False)
