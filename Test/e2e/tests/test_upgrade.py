DESCRIPTION = "Legacy rows (the window after an upgrade): playback on a row before its title is adopted ends up on the movie"


def legacy(t, movie):
    """Rows without an owner and without links, like rows synced by an older Gelato: a split
    leaves exactly that behind."""
    t.api.delete(f"/Videos/{movie}/AlternateSources")
    r = t.db.stream_rows(movie)
    t.check(r["unowned"] == r["count"] and r["links"] == 0, f"legacy state: {r['count']} rows, none owned, no links")


def run(t):
    movie = t.movie()
    u1, u2 = t.api, t.user2
    n = len(u1.sources(movie))
    row = t.row(movie)
    runtime = u1.item(movie).get("RunTimeTicks") or 0
    pct = lambda p: int(runtime * p / 100)
    u1.mark_played(movie, False)

    t.log("== A: playback goes on while the other user's visit adopts the rows")
    legacy(t, movie)
    u1.report("start", row, row, 0, "up-a")
    u1.report("progress", row, row, pct(30), "up-a")
    t.log("before adoption: movie", u1.user_data(movie), "| row", u1.user_data(row))
    t.equal(u1.user_data(row)["PlaybackPositionTicks"], pct(30), "the legacy row holds the position")
    t.check(len(u2.sources(movie)) >= 2, f"{u2.name}'s visit syncs the title")
    r = t.db.stream_rows(movie)
    t.check(r["owned"] == r["count"] and r["links"] == r["count"], "the visit adopted and linked the rows")
    u1.report("progress", row, row, pct(40), "up-a")
    t.equal(u1.user_data(movie)["PlaybackPositionTicks"], pct(40), "the next progress report lands on the movie")
    u1.report("stop", row, row, pct(45), "up-a")
    t.equal(u1.user_data(movie)["PlaybackPositionTicks"], pct(45), "the stop lands on the movie")
    resume = u1.resume()
    t.check(movie in resume and row not in resume, "Continue Watching has the movie, not the row")
    t.equal(u1.sources(movie)[0], row, "the resumed stream is listed first")

    t.log("== B: playback stopped before the adoption")
    u1.mark_played(movie, False)
    legacy(t, movie)
    u1.report("start", row, row, 0, "up-b")
    u1.report("progress", row, row, pct(30), "up-b")
    u1.report("stop", row, row, pct(35), "up-b")
    t.log("before adoption: movie", u1.user_data(movie), "| row", u1.user_data(row))
    t.check(len(u2.sources(movie)) >= 2, f"{u2.name}'s visit syncs the title")
    m = u1.user_data(movie)
    t.log("after adoption: movie", m, "| row", u1.user_data(row))
    t.equal(m["PlaybackPositionTicks"], pct(35), "the adoption moved the row's resume point to the movie")
    resume = u1.resume()
    t.check(movie in resume and row not in resume, "Continue Watching has the movie, not the row")
    t.equal(u1.sources(movie)[0], row, "the resumed stream is listed first")
    d = u1.item(row)
    t.equal((d.get("UserData") or {}).get("PlaybackPositionTicks"), pct(35), "the row's page shows the resume point")

    t.log("== C: a legacy row finished before the adoption")
    u1.mark_played(movie, False)
    legacy(t, movie)
    u1.report("start", row, row, 0, "up-c")
    u1.report("stop", row, row, pct(97), "up-c")
    t.check(len(u2.sources(movie)) >= 2, f"{u2.name}'s visit syncs the title")
    m = u1.user_data(movie)
    t.check(m["Played"] and m["PlaybackPositionTicks"] == 0, f"the movie is played after the adoption: {m}")
    t.check(movie not in u1.resume(), "not in Continue Watching")
    u1.mark_played(movie, False)
    t.equal(len(u1.sources(movie)), n, "the movie lists its streams")
