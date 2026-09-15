DESCRIPTION = "Deleting a series with linked rows parks the watch state, and inserting it again restores it"

import time


def run(t):
    series = t.series()
    s = t.api.item(series, "ProviderIds")
    name, imdb = s["Name"], s.get("ProviderIds", {}).get("Imdb") or t.fixtures.stremio_id(series)
    eps = t.episodes(series)
    e1 = eps[0]["Id"]
    srcs = t.api.sources(e1)
    runtime = t.api.item(e1).get("RunTimeTicks") or 0
    row = next((x for x in srcs if x != e1), None)
    if row is None or not runtime:
        t.skip("episode 1 has no second stream or no runtime")
    t.api.mark_played(e1, False)
    t.api.report("start", e1, row, 0, "sd")
    t.api.report("stop", e1, row, int(runtime * 0.97), "sd")
    t.equal(t.api.user_data(e1)["Played"], True, "episode 1 played through its row")

    tree = lambda: t.db.one(
        "select count(*) from BaseItems where lower(replace(Id,'-',''))=? or lower(replace(SeriesId,'-',''))=?", (series, series))[0]
    rows = lambda: t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%' and lower(replace(SeriesId,'-',''))=?", (series,))[0]
    parked = lambda: t.db.one(
        "select count(*), max(Played) from UserData u where lower(replace(u.UserId,'-',''))=? and u.CustomDataKey like ? "
        "and u.ItemId like '00000000-0000%'",
        (t.api.user.replace("-", "").lower(), f"{imdb}%"))
    t.log(f"before: {tree()} items in the tree, {rows()} rows")
    t.check(rows() > 0, "the series has linked rows")

    t.api.delete(f"/Items/{series}")
    time.sleep(3)
    t.equal(tree(), 0, "series, seasons, episodes gone")
    t.equal(rows(), 0, "rows gone")
    p = parked()
    t.log("parked user data for the series' keys:", p)
    t.check(p and p[0] >= 1 and p[1] == 1, "the played state is parked under the placeholder item")

    hits = [i for i in t.api.search(imdb, "Series") if i.get("Name") == name]
    if not hits:
        t.log(f"{name} not found in search, cannot insert it again")
        return
    back = t.api.item(hits[0]["Id"])
    t.equal(back.get("Id", "").lower(), series, "inserted again under the same id")
    time.sleep(4)
    t.check(tree() > 1, f"the tree is back ({tree()} items)")
    t.equal(t.api.user_data(e1)["Played"], True, "episode 1 is played again")
    nextup = [i["Id"] for i in t.api.get(f"/Shows/NextUp?seriesId={series}&userId={t.api.user}").get("Items", [])]
    t.equal(nextup, [eps[1]["Id"]], "Next Up is episode 2")
    t.api.mark_played(e1, False)
