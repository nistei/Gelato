DESCRIPTION = "Next Up and Continue Watching after episodes were finished through a non-first stream"


def run(t):
    series = t.series()
    eps = t.episodes(series)
    e1, e2, e3 = (e["Id"] for e in eps[:3])
    nextup = lambda: [i["Id"] for i in t.api.get(f"/Shows/NextUp?seriesId={series}&userId={t.api.user}").get("Items", [])]

    # Only the episodes used: marking a whole series unplayed walks every episode and takes minutes on a long one.
    for e in (e1, e2, e3):
        t.api.mark_played(e, False)
    t.equal(nextup(), [e1], "Next Up starts at episode 1")

    for label, ep, following in (("E1", e1, e2), ("E2", e2, e3)):
        srcs = t.api.sources(ep)
        runtime = t.api.item(ep).get("RunTimeTicks") or 0
        rows = [s for s in srcs if s != ep]
        if not rows or not runtime:
            t.skip(f"{label} has no second stream or no runtime")
        row = rows[0]
        t.log(f"== {label} {ep[:8]} through row {row[:8]}")
        t.api.report("start", ep, row, 0, f"nextup-{label}")
        t.api.report("progress", ep, row, int(runtime * 0.4), f"nextup-{label}")
        t.equal(t.api.user_data(ep)["PlaybackPositionTicks"], int(runtime * 0.4), f"{label} position mid-play")
        resume = t.api.resume()
        t.check(ep in resume and row not in resume, f"{label} in Continue Watching, its row not")
        t.equal(nextup(), [ep], f"Next Up stays at {label} mid-play")
        t.api.report("stop", ep, row, int(runtime * 0.97), f"nextup-{label}")
        ud = t.api.user_data(ep)
        t.check(ud["Played"] and ud["PlaybackPositionTicks"] == 0, f"{label} played after the stop at 97%: {ud}")
        t.check(ep not in t.api.resume(), f"{label} left Continue Watching")
        t.equal(nextup(), [following], f"Next Up moved past {label}")

    season = next(s for s in t.api.get(f"/Shows/{series}/Seasons?userId={t.api.user}").get("Items", []) if s.get("IndexNumber") == 1)
    ud = season.get("UserData") or {}
    t.equal(ud.get("UnplayedItemCount"), len(eps) - 2, "season 1 unplayed count")
    t.equal(ud.get("Played"), False, "season 1 not played")
    for e in (e1, e2, e3):
        t.api.mark_played(e, False)
