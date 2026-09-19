DESCRIPTION = "Split versions and merge versions from the movie menu: rows come back on the next visit"


def run(t):
    a, b = t.movie(), t.movie2()

    def rows(movie):
        """Stream rows in the database, not the cached source list: an earlier test may have deleted
        a row, and the purge in test_playlist leaves every movie it did not re-sync without any. A
        visit syncs them again, so both sides of the merge have rows of their own."""
        n = t.db.stream_rows(movie)["count"]
        if n == 0:
            t.api.sources(movie)
            n = t.db.stream_rows(movie)["count"]
        return n

    na, nb = rows(a), rows(b)
    t.log(f"A {a[:8]} {na} rows, B {b[:8]} {nb} rows")
    t.check(na > 0 and nb > 0, "both movies have stream rows before the merge")
    pv = lambda m: t.db.one("select lower(replace(PrimaryVersionId,'-','')) from BaseItems where lower(replace(Id,'-',''))=?", (m,))[0]

    t.log("== split A")
    t.api.delete(f"/Videos/{a}/AlternateSources")
    r = t.db.stream_rows(a)
    t.equal((r["unowned"], r["links"]), (na, 0), "after the split: rows unowned, links gone")
    # The first request after the split triggers the sync; a page load then lists every stream.
    t.api.post(f"/Items/{a}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user})
    r = t.db.stream_rows(a)
    t.check(r["count"] >= na and r["owned"] == r["count"] and r["links"] == r["count"],
            f"the first request after the split adopts and links the rows again: {r}")
    na = r["count"]
    t.equal(len(t.api.sources(a)), na, "the page lists every stream")

    t.log("== merge B into A")
    t.api.post(f"/Videos/MergeVersions?ids={a},{b}")
    primary, other = (b, a) if pv(a) == b else (a, b)
    t.check(pv(other) == primary, f"{other[:8]} became a version of {primary[:8]}")
    srcs = t.api.sources(other)
    t.log(f"merged page of {other[:8]}: {len(srcs)} sources")
    t.check(len(srcs) >= na, "the merged page lists the streams of both")
    # Jellyfin 12.1 makes every linked version of a merged movie a version of the primary when it
    # saves the primary (ItemPersistenceService sets PrimaryVersionId), so the rows move with it.
    owners = {owner for _, owner, _, _ in t.db.row_users(other).values()}
    t.equal(owners, {primary}, "the merged movie's rows are versions of the primary")

    t.log("== split again")
    t.api.delete(f"/Videos/{primary}/AlternateSources")
    t.check(pv(a) is None and pv(b) is None, "both are their own primary again")
    for m in (a, b):
        n = len(t.api.sources(m))
        r = t.db.stream_rows(m)
        t.check(n >= 2 and n == r["count"], f"{m[:8]} lists its streams again ({n})")
        t.equal((r["owned"], r["unowned"], r["links"]), (n, 0, n), f"{m[:8]} rows owned and linked")
