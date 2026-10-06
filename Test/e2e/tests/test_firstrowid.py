DESCRIPTION = "The stream row that is published under the item's id can still be asked for by its own id"


def listed(t, item):
    """The item's sources as listed, and the one that carries the item's id: Gelato publishes the
    first stream under it and names the stream's row in the ETag only."""
    srcs = t.api.item(item, fields="Path").get("MediaSources") or []
    return srcs, next((s for s in srcs if s["Id"].replace("-", "").lower() == item), None)


def playback_info(t, item, source):
    pi = t.api.post(f"/Items/{item}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": source})
    return pi.get("MediaSources") or [], pi.get("ErrorCode")


def stream(t, item, source):
    st, _, _ = t.api.request(f"/Videos/{item}/stream?static=true&mediaSourceId={source}", {"Range": "bytes=0-0"}, max_bytes=1)
    return st


def asked_by_own_id(t, item, what):
    """Asks for the row behind the item-id source by the row's id, and returns that row."""
    srcs, own = listed(t, item)
    t.require(own and own.get("ETag"), f"the {what} has no source under its own id")
    row = own["ETag"].replace("-", "").lower()
    rows = [r[0] for r in t.db.query("select lower(replace(Id,'-','')) from BaseItems where Tags like '%gelato-stream%' "
                                     "and lower(replace(PrimaryVersionId,'-',''))=?", (item,))]
    t.log(f"{what} {item[:8]}: {len(rows)} rows, {len(srcs)} sources, row {row[:8]} is listed under the item's id")
    t.check(row in rows and row != item, f"the {what}'s item-id source names one of its rows")
    t.check(row not in [s["Id"].replace("-", "").lower() for s in srcs], f"that row of the {what} is not listed by its own id")

    got, err = playback_info(t, item, row)
    t.check([s["Id"] for s in got] == [row] and not err,
            f"PlaybackInfo naming the {what}'s item-id row by its own id answers with that id ({err}, {[s['Id'][:8] for s in got]})")
    t.check(got and got[0].get("Name") == own.get("Name"), f"and it is that row's stream ({(got[0].get('Name') if got else None)!r})")
    st = stream(t, item, row)
    t.delivers(st, None, row, f"/Videos/{{{what}}}/stream naming that row plays ({st})")

    # Controls: the item's id as the source, and the row opened under its own id, both play.
    got, err = playback_info(t, item, item)
    t.check([s["Id"] for s in got] == [item] and not err, f"PlaybackInfo naming the {what}'s id still answers with it ({err})")
    st = stream(t, item, item)
    t.delivers(st, None, row, f"/Videos/{{{what}}}/stream naming the {what}'s id plays ({st})")
    st = stream(t, row, row)
    t.delivers(st, None, row, f"the {what}'s row opened under its own id plays ({st})")
    return row, own


def run(t):
    movie = t.movie()
    row, own = asked_by_own_id(t, movie, "movie")

    # Another stream is resumed and moves first: the item-id row is no longer the default source,
    # and asking for it by its own id must not answer with the resumed one.
    other = next(s["Id"] for s in listed(t, movie)[0] if s["Id"] not in (movie, row))
    runtime = t.api.item(movie).get("RunTimeTicks") or 0
    t.api.mark_played(movie, False)
    try:
        t.api.report("start", movie, other, 0)
        t.api.report("stop", movie, other, int(runtime * 0.3))
        t.equal(t.api.sources(movie)[0], other, "the resumed stream is listed first")
        got, err = playback_info(t, movie, row)
        t.check([s["Id"] for s in got] == [row] and not err and got[0].get("Name") == own.get("Name"),
                f"PlaybackInfo naming the item-id row answers with it behind a resumed stream ({err}, {[s['Id'][:8] for s in got]})")
        st = stream(t, movie, row)
        t.delivers(st, None, row, f"/Videos/{{movie}}/stream naming that row plays behind a resumed stream ({st})")
    finally:
        t.api.mark_played(movie, False)

    asked_by_own_id(t, t.episodes()[0]["Id"], "episode")
