DESCRIPTION = "a movie/episode played without picking a version answers its segments (skip intro) with those of the stream that plays under its id"

# The segment providers run for the stream row being played, so its segments are stored under the
# row, while the first stream is listed with the movie's id and the player asks
# /MediaSegments/{movie id}. Segments are written straight into the database (marked by their
# ticks, removed again afterwards): IntroDB only knows some episodes, and its answer is not the
# point here.

import hashlib
import json
import subprocess
import uuid

from jfapi.db import norm

INTRO, OUTRO = "Intro", "Outro"
TYPE = {INTRO: 5, OUTRO: 4}  # MediaSegmentType in the database
PROVIDER = "Gelato IntroDB"


def provider_id(name):
    """Jellyfin's id of a segment provider: the MD5 of its lower-case name, as a Guid."""
    return uuid.UUID(bytes_le=hashlib.md5(name.lower().encode("utf-16-le")).digest()).hex


def write(t, sql, params=()):
    """One write to the instance's database, through its sidecar (which only reads)."""
    script = ("import json,sqlite3,sys;r=json.loads(sys.argv[1]);"
              "c=sqlite3.connect('/config/data/jellyfin.db',timeout=30);c.execute(r[0],r[1]);c.commit();c.close()")
    r = subprocess.run(["docker", "exec", t.db.sidecar.name, "python", "-c", script, json.dumps([sql, list(params)])],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"database write failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")


def dashed(guid):
    return str(uuid.UUID(guid)).upper()


def run(t):
    t.require(t.db.sidecar is not None, "no database sidecar to write the segments with")
    added = []

    def add(item_id, kind, start):
        seg = str(uuid.uuid4()).upper()
        write(t, "insert into MediaSegments (Id, ItemId, SegmentProviderId, StartTicks, EndTicks, Type) values (?,?,?,?,?,?)",
              (seg, dashed(item_id), provider_id(PROVIDER), start * 10**7, (start + 17) * 10**7, TYPE[kind]))
        added.append(seg)
        return start * 10**7

    def segments(item_id, types=None):
        q = "".join(f"&includeSegmentTypes={x}" for x in types or [])
        return {(s["Type"], s["StartTicks"]) for s in t.api.get(f"/MediaSegments/{item_id}?{q.lstrip('&')}").get("Items", [])}

    def first_row(item_id):
        """The row the item's own id plays, and another one picked from the dropdown."""
        sources = t.api.item(item_id).get("MediaSources") or []
        own = next((s for s in sources if norm(s["Id"]) == norm(item_id)), None)
        t.require(own and norm(own.get("ETag")) not in ("", norm(item_id)),
                  f"{item_id[:8]} lists no stream under its own id")
        others = [norm(s["Id"]) for s in sources if norm(s["Id"]) not in (norm(item_id), norm(own["ETag"]))]
        return norm(own["ETag"]), (others[0] if others else None)

    try:
        movie = t.movie()
        row, other = first_row(movie)
        t.log(f"movie {movie[:8]} plays its row {row[:8]} under its id; picked version {(other or '-')[:8]}")
        outro = add(row, OUTRO, 4321)
        t.check((OUTRO, outro) in segments(row), "the row answers the segment stored under it")
        t.check((OUTRO, outro) in segments(movie), "the movie answers the segment of the row it plays")
        t.check((OUTRO, outro) not in segments(movie, ["Intro"]), "the movie keeps the asked-for segment types")
        if other:
            t.check((OUTRO, outro) not in segments(other), "a picked version answers its own segments, not the first stream's")
            other_intro = add(other, INTRO, 1234)
            t.check((INTRO, other_intro) in segments(other), "a picked version answers the segment stored under it")
            t.check((INTRO, other_intro) not in segments(movie), "the movie does not answer a picked version's segment")

        series = t.series()
        episode = next(iter(t.episodes(series)), None)
        t.require(episode, "the fixture series has no season 1 episode")
        ep_row, _ = first_row(episode["Id"])
        before = segments(episode["Id"])
        intro = add(ep_row, INTRO, 2345)
        t.check((INTRO, intro) in segments(episode["Id"]), "the episode answers the intro of the row it plays")
        t.check((INTRO, intro) in segments(episode["Id"], ["Intro"]), "with includeSegmentTypes=Intro, as Jellyfin Web asks")
        t.equal(segments(episode["Id"]) - before, {(INTRO, intro)}, "nothing else is added to the episode's answer")

        # A movie with segments of its own (a provider that ran for it) keeps them while its row has none.
        movie2 = t.movie2()
        row2, _ = first_row(movie2)
        if not segments(row2):
            own = add(movie2, OUTRO, 3456)
            t.check((OUTRO, own) in segments(movie2), "a movie whose row has no segments answers its own")
    finally:
        for seg in added:
            write(t, "delete from MediaSegments where Id=?", (seg,))
        left = t.db.one(f"select count(*) from MediaSegments where Id in ({','.join('?' * len(added)) or 'null'})", tuple(added))[0]
        t.equal(left, 0, "the test's segments are removed again")
