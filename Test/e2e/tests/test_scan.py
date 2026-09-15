DESCRIPTION = "A full library scan after a purge and a sync keeps the rows, their refresh stamps and links"
DESTRUCTIVE = True  # changes the instance: a full library scan


def run(t):
    movie = t.movie()
    t.equal(t.api.run_task("PurgeGelatoStreamsTask")[0], "Completed", "purge streams")
    total = lambda: t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%'")[0]
    t.equal(total(), 0, "no rows after the purge")
    n = len(t.api.sources(movie))
    t.check(n >= 2, f"synced again: {n} sources")
    before = t.db.stream_rows(movie)
    t.equal(before["unstamped"], 0, "rows carry a refresh stamp")
    status, msg = t.api.run_task("RefreshLibrary", timeout=1800)
    t.equal(status, "Completed", f"library scan {msg}")
    after = t.db.stream_rows(movie)
    t.log("rows before the scan", before, "after", after)
    t.equal(after, before, "rows, owners, stamps and links unchanged by the scan")
    t.equal(len(t.api.sources(movie)), n, "sources after the scan")
    t.equal(t.db.item_count(movie), 1, "the movie survived the scan")
