DESCRIPTION = "A stream row's own page (what a JF 12 client shows for a picked version) looks like the movie"


def run(t):
    movie = t.movie()
    row = t.row(movie)
    m = t.api.item(movie, "People,Tags")
    d = t.api.item(row, "People,Tags")
    srcs = d.get("MediaSources") or []
    t.log(f"row {row[:8]} of {m['Name']}: type {d.get('Type')}, {len(srcs)} sources")
    t.equal(d.get("Name"), m.get("Name"), "name")
    t.check(srcs and srcs[0]["Id"] == row, "the row's own source comes first")
    t.equal(len(srcs), len(m.get("MediaSources") or []), "same number of versions as the movie")
    t.check(any(s["Id"] == movie for s in srcs), "the first stream is listed under the movie's id")
    t.equal(sorted((d.get("ImageTags") or {}).keys()), sorted((m.get("ImageTags") or {}).keys()), "image tags of the movie")
    t.equal(len(d.get("BackdropImageTags") or []), len(m.get("BackdropImageTags") or []), "backdrops of the movie")
    t.equal(len(d.get("People") or []), len(m.get("People") or []), "people of the movie")
    t.equal(d.get("Tags") or [], m.get("Tags") or [], "tags of the movie (stream tag hidden)")
    t.equal(d.get("MediaSourceCount"), m.get("MediaSourceCount"), "MediaSourceCount")
    for k in ("Played", "PlaybackPositionTicks", "IsFavorite"):
        t.equal((d.get("UserData") or {}).get(k), (m.get("UserData") or {}).get(k), f"UserData.{k} of the movie")

    # The first render of a lazy poster can miss while Jellyfin still validates the movie's image
    # files on a fresh instance: try a few times, and only expect what the movie itself delivers.
    for attempt in range(3):
        st, headers, body = t.api.request(f"/Items/{row}/Images/Primary?maxWidth=50")
        if st == 200 and body:
            break
        t.wait(2)
    mst, _, mbody = t.api.request(f"/Items/{movie}/Images/Primary?maxWidth=50")
    t.check((st == 200 and len(body) > 0) or mst != 200, f"primary image through the row: {st}, {len(body)} bytes (movie's own: {mst})")

    similar = t.api.get(f"/Items/{row}/Similar?userId={t.api.user}&limit=10").get("Items", [])
    rows_all = t.db.stream_row_ids()
    t.log(f"similar items: {len(similar)} (can be empty on a small library)")
    t.check(not any(i["Id"].lower() in rows_all for i in similar), "no stream rows among similar items")
    t.check(not any(i["Id"].lower() == movie for i in similar), "the movie itself is not among its similar items")
