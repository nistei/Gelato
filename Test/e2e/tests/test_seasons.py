DESCRIPTION = "Season and episode counts of a series ignore the stream rows, before and after a sync"


def run(t):
    series = t.series()
    seasons = t.api.get(f"/Shows/{series}/Seasons?userId={t.api.user}&Fields=ChildCount,RecursiveItemCount").get("Items", [])
    t.check(seasons, f"{len(seasons)} seasons")
    con = t.db.connect()
    try:
        for s in seasons:
            total, rows = t.db.one(
                "select count(*), sum(case when Tags like '%gelato-stream%' then 1 else 0 end) from BaseItems "
                "where Type like '%Episode' and lower(replace(SeasonId,'-',''))=?", (s["Id"].lower(),), con)
            ud = s.get("UserData") or {}
            t.log(f"season {s.get('IndexNumber')}: ChildCount {s.get('ChildCount')} recursive {s.get('RecursiveItemCount')} "
                  f"unplayed {ud.get('UnplayedItemCount')} | db episodes {total - (rows or 0)} rows {rows or 0}")
            t.equal(s.get("ChildCount"), total - (rows or 0), f"season {s.get('IndexNumber')} ChildCount = episodes without rows")
            t.check((ud.get("UnplayedItemCount") or 0) <= (s.get("ChildCount") or 0), f"season {s.get('IndexNumber')} unplayed <= ChildCount")
    finally:
        con.close()

    eps = t.episodes(series)
    season1 = next(s for s in seasons if s.get("IndexNumber") == 1)
    before = season1.get("ChildCount")
    d = t.api.item(eps[0]["Id"])
    n = len(d.get("MediaSources") or [])
    t.check(n >= 2, f"episode 1 synced: {n} sources")
    t.check(d.get("MediaSourceCount") in (None, 1), "no version count badge on the episode")
    after = next(s for s in t.api.get(f"/Shows/{series}/Seasons?userId={t.api.user}&Fields=ChildCount").get("Items", [])
                 if s["Id"] == season1["Id"])
    t.equal(after.get("ChildCount"), before, "season 1 ChildCount after the sync")
    eps_after = t.api.get(f"/Shows/{series}/Episodes?seasonId={season1['Id']}&userId={t.api.user}").get("Items", [])
    t.equal(len(eps_after), len(eps), "episodes listed after the sync")
    rows_all = t.db.stream_row_ids()
    t.check(all(e["Id"].lower() not in rows_all for e in eps_after), "no rows in the episode list")
