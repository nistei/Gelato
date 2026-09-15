DESCRIPTION = "Two users on one title: each sees the rows of their own sync, all rows belong to the movie"


def run(t):
    movie = t.movie()
    u1, u2 = t.api, t.user2
    n1 = len(u1.sources(movie))
    n2 = len(u2.sources(movie))
    t.log(f"{u1.name}: {n1} sources, {u2.name}: {n2} sources")
    t.check(n1 >= 2 and n2 >= 2, "both users get streams")

    rows = t.db.row_users(movie)
    t.log(f"rows {len(rows)}, user sets {sorted({len(v[0]) for v in rows.values()})}")
    t.check(all(v[1] == movie for v in rows.values()), "every row is owned by the movie")
    guids = [v[3] for v in rows.values()]
    t.equal(len(guids), len(set(guids)), "distinct stream guids")
    t.equal(t.db.stream_rows(movie)["links"], len(rows), "one version link per row")

    for api, n in ((u1, n1), (u2, n2)):
        mine = {r for r, v in rows.items() if api.user.replace("-", "").lower() in v[0]}
        listed = set(api.sources(movie))
        # the first stream is listed under the movie's id: swap it for its row
        first = min(mine, key=lambda r: rows[r][2] if rows[r][2] is not None else 999) if mine else None
        listed_rows = (listed - {movie}) | ({first} if movie in listed and first else set())
        t.equal(listed_rows, mine, f"{api.name} lists exactly the rows synced for them ({len(mine)})")
