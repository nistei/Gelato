DESCRIPTION = "Search for a title that is not in the library, open it, play it right away: a movie and a whole series (both removed again)"

import re
import time

MOVIE_TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs"]
SERIES_TERMS = ["Adolescence", "Chernobyl", "Baby Reindeer", "The Queen's Gambit", "Beef", "Shogun", "Ripley"]


def stremio_of(hit):
    m = re.search(r"(tt\d+)", hit.get("Path") or "")
    return m.group(1) if m else None


def run(t):
    in_library = lambda stremio: t.db.one(
        "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

    def fresh_hits(terms, kind):
        for term in terms:
            for hit in t.api.search(term, kind, limit=8):
                stremio = stremio_of(hit)
                if stremio and hit.get("Name", "").lower().startswith(term.lower()[:5]) and not in_library(stremio):
                    yield hit, stremio

    def open_fresh(terms, kind, streams_of):
        """Opens search hits not in the library until one has streams (the addon has none for
        some titles); the others are removed again. Returns (hit, stremio, dto) or Nones."""
        for hit, stremio in fresh_hits(terms, kind):
            t0 = time.time()
            d = t.api.item(hit["Id"])
            item = d.get("Id", "").lower()
            n = streams_of(d)
            t.log(f"{kind} not in the library yet: {hit['Name']} ({stremio}), opened in {time.time() - t0:.1f}s as {item[:8]}, {n} streams")
            if n >= 2:
                return hit, stremio, d
            t.api.delete(f"/Items/{item}")
        return None, None, None

    def like_a_client(hit, inserted, label, series=False):
        """What a client does next: its URL still names the search hit, so the page reload, the
        seasons, the episodes, similar items and playback all ask with the hit's id."""
        calls = [("page reload", "GET", f"/Items/{hit}?userId={t.api.user}"),
                 ("similar", "GET", f"/Items/{hit}/Similar?userId={t.api.user}&limit=5"),
                 ("poster", "GET", f"/Items/{hit}/Images/Primary?maxWidth=50")]
        if series:
            calls += [("seasons", "GET", f"/Shows/{hit}/Seasons?userId={t.api.user}"),
                      ("episodes", "GET", f"/Shows/{hit}/Episodes?userId={t.api.user}&season=1"),
                      ("next up", "GET", f"/Shows/NextUp?seriesId={hit}&userId={t.api.user}")]
        else:
            calls += [("playback info", "POST", f"/Items/{hit}/PlaybackInfo?userId={t.api.user}")]
        for name, method, path in calls:
            if name == "poster":
                st, headers, body = t.api.request(path)
                t.check(st == 200 and len(body) > 0, f"{label}, {name} with the hit's id: {st}")
                continue
            st, d = t.api.call(method, path, {"UserId": t.api.user} if method == "POST" else None)
            got = d.get("Id", "").lower() if isinstance(d, dict) and "Id" in d else None
            n = len(d.get("Items", d.get("MediaSources", []))) if isinstance(d, dict) else None
            detail = f"id {got[:8]}" if got else f"{n} items" if n is not None else ""
            ok = st == 200 and (got == inserted if got else n is not None and (n > 0 or name in ("next up", "similar")))
            t.check(ok, f"{label}, {name} with the hit's id: {st} {detail}")

    def play(item, label):
        srcs = t.api.item(item).get("MediaSources") or []
        t.check(len(srcs) >= 1 and all(s.get("Path", "").startswith("http") for s in srcs), f"{label}: {len(srcs)} playable streams right away")
        # PlaybackInfo hands out a stub path on purpose (clients stream through Jellyfin, which
        # resolves the URL), so the proof is the stream endpoint itself: the first byte.
        pi = t.api.post(f"/Items/{item}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user})
        ms = pi.get("MediaSources") or []
        t.check(ms and not pi.get("ErrorCode") and (ms[0].get("SupportsDirectPlay") or ms[0].get("SupportsDirectStream")),
                f"{label}: PlaybackInfo offers a source to play ({pi.get('ErrorCode')})")
        if ms:
            st, headers, body = t.api.request(f"/Videos/{item}/stream?static=true&mediaSourceId={ms[0]['Id']}", {"Range": "bytes=0-0"}, max_bytes=1)
            t.check(st in (200, 206) and len(body) == 1, f"{label}: the stream endpoint delivers bytes right away ({st})")
        runtime = t.api.item(item).get("RunTimeTicks") or 0
        row = srcs[1]["Id"] if len(srcs) > 1 else srcs[0]["Id"]
        t.api.report("start", item, row, 0, "insert")
        t.api.report("progress", item, row, int(runtime * 0.3) if runtime else 600000000, "insert")
        pos = t.api.user_data(item)["PlaybackPositionTicks"]
        t.check(pos > 0, f"{label}: progress saved ({pos})")
        t.api.report("stop", item, row, pos, "insert")  # leave no session behind
        t.check(item in t.api.resume(), f"{label}: in Continue Watching")
        rows = t.db.stream_rows(item)
        t.check(rows["count"] >= len(srcs) and rows["owned"] == rows["count"] and rows["links"] == rows["count"], f"{label}: rows owned and linked {rows}")
        return runtime, row

    hit, stremio, d = open_fresh(MOVIE_TERMS, "Movie", lambda d: len(d.get("MediaSources") or []))
    if hit:
        movie = d.get("Id", "").lower()
        try:
            t.check(d.get("Type") == "Movie" and movie != hit["Id"].lower(), "the click inserted the movie under a library id")
            t.check("Primary" in (d.get("ImageTags") or {}), "the movie has a poster")
            t.equal(in_library(stremio), 1, "one movie item in the library")
            listed = t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=Movie&Recursive=true&Ids={movie}").get("Items", [])
            t.equal(len(listed), 1, "the movie is in the library listing")
            play(movie, "movie")
            like_a_client(hit["Id"], movie, "movie")
        finally:
            t.api.delete(f"/Items/{movie}")
            t.equal(in_library(stremio), 0, "the movie was removed again")
    else:
        t.log("no movie with streams outside the library among the search terms")

    def series_streams(d):
        eps = t.episodes(d["Id"].lower(), 1)
        return len(t.api.sources(eps[0]["Id"])) if eps else 0

    hit, stremio, d = open_fresh(SERIES_TERMS, "Series", series_streams)
    if not hit:
        t.skip("no series with streams outside the library among the search terms")
    series = d.get("Id", "").lower()
    try:
        t.check(d.get("Type") == "Series" and series != hit["Id"].lower(), "the click inserted the series under a library id")
        seasons = t.api.get(f"/Shows/{series}/Seasons?userId={t.api.user}&Fields=ChildCount").get("Items", [])
        t.check(seasons, f"seasons are there right away ({len(seasons)})")
        eps = t.episodes(series, next((s.get("IndexNumber") for s in seasons if (s.get("IndexNumber") or 0) > 0), 1))
        t.check(eps, f"episodes are there right away ({len(eps)} in the first season)")
        if not eps:
            return
        # Jellyfin restores parked watch state when a title comes back under the same ids (see
        # seriesdelete), so a series that was here before starts where it was left: reset the
        # episodes used here.
        for e in eps[:2]:
            if t.api.user_data(e["Id"])["Played"]:
                t.log(f"episode {e.get('IndexNumber')} came back played (parked state restored), resetting it")
            t.api.mark_played(e["Id"], False)
        total = t.db.one("select count(*) from BaseItems where Type like '%TV.Episode' and (Tags is null or Tags not like '%gelato-stream%') "
                         "and lower(replace(SeriesId,'-',''))=?", (series,))[0]
        s = t.api.item(series, "RecursiveItemCount")
        t.equal(s.get("RecursiveItemCount"), total, "series episode count right after the insert")
        t.equal((s.get("UserData") or {}).get("UnplayedItemCount"), total, "series unplayed badge right after the insert")
        e1, e2 = eps[0]["Id"], eps[1]["Id"] if len(eps) > 1 else None
        nextup = lambda: [i["Id"] for i in t.api.get(f"/Shows/NextUp?seriesId={series}&userId={t.api.user}").get("Items", [])]
        t.equal(nextup(), [e1], "Next Up starts at the first episode")
        like_a_client(hit["Id"], series, "series", series=True)
        runtime, row = play(e1, "episode 1")
        if runtime and e2:
            t.api.report("stop", e1, row, int(runtime * 0.97), "insert")
            t.equal(t.api.user_data(e1)["Played"], True, "episode 1 finished")
            t.equal(nextup(), [e2], "Next Up moves to episode 2")
            t.check(len(t.api.sources(e2)) >= 1, "episode 2 has streams too")
        listed = t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=Series&Recursive=true&Ids={series}").get("Items", [])
        t.equal(len(listed), 1, "the series is in the library listing")
    finally:
        t0 = time.time()
        t.api.delete(f"/Items/{series}")
        t.log(f"series removed again in {time.time() - t0:.0f}s")
        t.equal(in_library(stremio), 0, "the series was removed again")
