DESCRIPTION = "The stream row that is published under the item's id can still be asked for by its own id"

RECORD = "PROD-FINDINGS #24"


def run(t):
    movie = t.movie()
    srcs = t.api.sources(movie)
    rows = t.db.query("select lower(replace(Id,'-','')) from BaseItems where Tags like '%gelato-stream%' "
                      "and lower(replace(PrimaryVersionId,'-',''))=?", (movie,))
    row_ids = [r[0] for r in rows]
    # Gelato lists the selected row under the item's own id, so one row of the movie is not in the list.
    hidden = [r for r in row_ids if r not in [s.replace("-", "").lower() for s in srcs]]
    t.log(f"{len(row_ids)} rows, {len(srcs)} sources, {len(hidden)} row(s) not listed by their own id")
    t.require(hidden, "every row is listed by its own id")
    row = hidden[0]

    pi = t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": row})
    t.known(len(pi.get("MediaSources") or []) > 0 and not pi.get("ErrorCode"),
            f"PlaybackInfo naming the item-id row by its own id offers a source ({pi.get('ErrorCode')})", RECORD)
    st, _, body = t.api.request(f"/Videos/{movie}/stream?static=true&mediaSourceId={row}", {"Range": "bytes=0-0"}, max_bytes=1)
    t.known(st in (200, 206), f"/Videos/{{id}}/stream naming that row answers {st}", RECORD)

    # Controls: the row under its own id, and the item id as the source, both play.
    st, _, _ = t.api.request(f"/Videos/{row}/stream?static=true&mediaSourceId={row}", {"Range": "bytes=0-0"}, max_bytes=1)
    t.check(st in (200, 206), f"the row opened under its own id plays ({st})")
