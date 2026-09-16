"""Stream rows that went through Gelato's probe: rows with a video stream in the database, and a
first PlaybackInfo on a row that has none to get more of them.

Jellyfin's library tasks (trickplay, chapter images) only look at videos with media streams, so a
test of what they do to Gelato's rows needs probed rows.
"""
from .bootstrap import MOVIE_PATH
from .db import STREAM_TAG

VIDEO = 1  # MediaStreamTypeEntity.Video


def probed_rows(t):
    """{row id: owner id} of the stream rows with a video stream in the database."""
    return dict(t.db.query(
        "select lower(replace(b.Id,'-','')), lower(replace(b.PrimaryVersionId,'-','')) from BaseItems b "
        "where b.Tags like ? and exists (select 1 from MediaStreamInfos m where m.ItemId=b.Id and m.StreamType=?)",
        (STREAM_TAG, VIDEO)))


def probe(t, movie, row):
    """PlaybackInfo for the row, which probes it when it has no video stream yet: True when it has one
    afterwards (a dead link at the debrid service leaves it without)."""
    pi = t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": row})
    src = next((s for s in pi.get("MediaSources", []) if s["Id"] == row), None)
    return any(m.get("Type") == "Video" for m in (src or {}).get("MediaStreams") or [])


def unprobed(t, movies):
    """(movie, row) for the rows of the movies that have no video stream yet, second source on."""
    for movie in movies:
        for s in (t.api.item(movie).get("MediaSources") or [])[1:]:
            if not any(m.get("Type") == "Video" for m in s.get("MediaStreams") or []):
                yield movie, s["Id"]


def ensure_probed(t, wanted=1, accept=lambda row: True, tries=6):
    """Probed rows for which accept(row) holds, probing up to `tries` unprobed rows of the fixture
    movies until there are `wanted` of them. Returns {row id: owner id}, possibly fewer."""
    found = {r: o for r, o in probed_rows(t).items() if accept(r)}
    if len(found) >= wanted:
        return found
    for i, (movie, row) in enumerate(unprobed(t, (t.movie(), t.movie2()))):
        if i >= tries:
            break
        ok = probe(t, movie, row)
        t.log(f"probed row {row[:8]} of {movie[:8]}: {'video stream' if ok else 'no video stream (dead link?)'}")
        if ok and accept(row):
            found[row] = movie
            if len(found) >= wanted:
                break
    return found


def movie_library(t):
    return next((v for v in t.api.get("/Library/VirtualFolders")
                 if v.get("CollectionType") == "movies" and MOVIE_PATH in v.get("Locations", [])), None)


def log_count(t, pattern):
    """Lines in Jellyfin's logs containing the fixed string."""
    out = t.sh(f"cat /config/log/log_*.log 2>/dev/null | grep -c -F '{pattern}'").strip()
    return int(out or 0)


def row_paths(t, rows):
    """{row id: path} from the database. Paths of debrid streams carry the API key: never log them."""
    if not rows:
        return {}
    return dict(t.db.query(
        "select lower(replace(Id,'-','')), Path from BaseItems where lower(replace(Id,'-','')) in ({})".format(",".join("?" * len(rows))),
        tuple(rows)))
