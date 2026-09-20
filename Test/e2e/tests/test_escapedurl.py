DESCRIPTION = "A stream URL with percent escapes in its file name reaches the addon unchanged: no path is encoded a second time, every path still hashes to its guid, the addon's own URLs are stored verbatim, and such a row probes, plays and downloads"

import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid

from jfapi.db import STREAM_TAG
from jfapi.probe import probe

PLUGIN = "94ea4e14-8163-4989-96fe-0a2094bc2d6a"
VIDEO = 1  # MediaStreamTypeEntity.Video

# What a release name leaves in a URL: spaces, parentheses, brackets. Encoding such a URL once more
# turns every one of them into %25xx, which a proxying addon then answers with 400.
ESCAPED = re.compile(r"%(20|28|29|5B|5D)", re.I)
DOUBLE = re.compile(r"%25[0-9A-Fa-f]{2}")

# Stream URLs carry the debrid API key: this test reports rows by id and counts, never a path.


def stream_rows(t):
    """(id, owner, path, guid, probed, movie) of the stream rows that play from an addon URL."""
    rows = t.db.query(
        "select lower(replace(b.Id,'-','')), lower(replace(b.PrimaryVersionId,'-','')), b.Path, b.ExternalId, "
        "exists (select 1 from MediaStreamInfos m where m.ItemId=b.Id and m.StreamType=?), b.Type "
        "from BaseItems b where b.Tags like ? and b.Path is not null and b.PrimaryVersionId is not null",
        (VIDEO, STREAM_TAG))
    out = []
    for item, owner, path, data, probed, kind in rows:
        # A torrent row plays through Gelato's own port, not the addon's URL.
        if not path.lower().startswith(("http://", "https://")) or "/gelato/stream?" in path:
            continue
        try:
            guid = ((json.loads(data) if data else {}) or {}).get("guid")
        except ValueError:
            guid = None
        out.append((item, owner, path, str(guid).lower() if guid else None, bool(probed),
                    kind.endswith("Movies.Movie")))
    return out


def hashed(url):
    """The guid Gelato derives from a stream URL: MD5 of the URL, read the way .NET reads the bytes."""
    return str(uuid.UUID(bytes_le=hashlib.md5(url.encode()).digest()))


def reachable(url):
    """The status of a one-byte range request straight at the addon, which tells a dead debrid link
    apart from a URL Jellyfin cannot fetch."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "jfapi"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # name resolution, TLS, timeout
        return type(e).__name__


def addon_urls(t, stremio_id):
    """The stream URLs the addon answers for a movie, asked the way Gelato asks."""
    base = (t.api.get(f"/Plugins/{PLUGIN}/Configuration").get("Url") or "").rstrip("/")
    if base.endswith("/manifest.json"):
        base = base[: -len("/manifest.json")]
    if not base:
        return []
    req = urllib.request.Request(f"{base}/stream/movie/{stremio_id}.json", headers={"User-Agent": "jfapi"})
    with urllib.request.urlopen(req, timeout=90) as r:
        answer = json.loads(r.read().decode())
    return [s["url"] for s in answer.get("streams") or [] if s.get("url")]


def run(t):
    rows = stream_rows(t)
    escaped = [r for r in rows if ESCAPED.search(r[2])]
    episodes = sum(1 for r in escaped if not r[5])
    t.log(f"{len(rows)} stream rows play from an addon URL, {len(escaped)} of them with percent escapes "
          f"({len(escaped) - episodes} of a movie, {episodes} of an episode)")
    if not escaped:
        t.skip("no stream row whose URL carries percent escapes")

    t.log("== the URLs in the database")
    twice = [r[0][:8] for r in rows if DOUBLE.search(r[2])]
    t.check(not twice, f"no stored URL is encoded a second time ({len(rows)} rows)" + (f"; {twice[:5]}" if twice else ""))
    # Gelato hashes the addon's URL into the row's guid and stores that same string as the path. A
    # path that still hashes to its guid is the addon's string, byte for byte.
    without = [r[0][:8] for r in rows if not r[3]]
    t.check(not without, f"every row carries the guid its URL was hashed into" + (f"; {len(without)} without: {without[:5]}" if without else ""))
    changed = [r[0][:8] for r in rows if r[3] and hashed(r[2]) != r[3]]
    t.check(not changed, f"every stored URL still hashes to its row's guid ({len(rows) - len(without)} rows)" + (f"; {changed[:5]}" if changed else ""))

    t.log("== against what the addon answers now")
    movie = next(r[1] for r in escaped if r[5])
    stremio_id = t.db.stremio_id(movie)
    t.api.item(movie)  # a visit syncs the movie's streams, so its rows are the current answer
    urls = addon_urls(t, stremio_id) if stremio_id else []
    wanted = {hashed(u): u for u in urls if ESCAPED.search(u)}
    t.log(f"movie {movie[:8]} ({stremio_id}): {len(urls)} streams, {len(wanted)} with escapes")
    stored = {guid: path for _, owner, path, guid, _, _ in stream_rows(t) if owner == movie and guid}
    hits = [g for g in wanted if g in stored]
    if t.check(hits, f"the addon's escaped URLs are among the movie's {len(stored)} rows ({len(hits)} of {len(wanted)})"):
        wrong = [g[:8] for g in hits if stored[g] != wanted[g]]
        t.check(not wrong, f"each one is stored byte for byte as the addon wrote it ({len(hits)} URLs)" + (f"; {wrong}" if wrong else ""))

    t.log("== an escaped URL through Jellyfin")
    # Unprobed rows first, and an episode's before a movie's: PlaybackInfo runs ffprobe against the
    # URL there, which is where a re-encoded one fails. A link the addon itself does not answer is
    # dead at the debrid service and says nothing about the URL, so the next candidate is tried.
    played = downloaded = None
    tried, dead, unusable = 0, 0, 0
    for item, owner, path, _, was_probed, is_movie in sorted(escaped, key=lambda r: (r[4], r[5])):
        if tried >= 6 or played:
            break
        tried += 1
        status = reachable(path)
        if status not in (200, 206):
            dead += 1
            t.log(f"row {item[:8]}: the addon answers {status} for the URL itself, skipped")
            continue
        kind = "movie" if is_movie else "episode"
        if downloaded is None:
            # Gelato fetches the URL itself here, with no ffmpeg in between.
            st, _, body = t.api.request(f"/Items/{item}/Download", {"Range": "bytes=0-0"}, max_bytes=1)
            downloaded = (item, st, len(body))
        if not probe(t, owner, item):
            unusable += 1
            t.log(f"row {item[:8]} of {kind} {owner[:8]}: the probe read no video stream")
            continue
        st, _, body = t.api.request(f"/Videos/{owner}/stream?static=true&mediaSourceId={item}", {"Range": "bytes=0-0"}, max_bytes=1)
        t.log(f"row {item[:8]} of {kind} {owner[:8]}, {'already probed' if was_probed else 'probed now'}: playback {st}")
        if st in (200, 206) and len(body) == 1:
            played = (item, owner, kind)
        else:
            unusable += 1

    t.log(f"{tried} candidates: {dead} dead at the debrid service, {unusable} without a playable source")
    if tried == dead:
        t.skip(f"every one of the {tried} escaped stream URLs tried is a dead link at the debrid service")

    if downloaded:
        item, st, got = downloaded
        t.check(st in (200, 206) and got == 1, f"row {item[:8]}: Gelato's download fetches the escaped URL ({st})")
    t.check(played, "an escaped URL probes and then delivers bytes"
            + (f" (row {played[0][:8]} of the {played[2]})" if played else f": {tried - dead} live candidates, none playable"))
