DESCRIPTION = "No response a client can ask for hands out a stream's addon URL: item DTOs and both PlaybackInfo actions stub it, and playback still goes through Jellyfin"

import re

URL = re.compile(r"^https?://", re.IGNORECASE)

# The addon/debrid URL carries the API key, so nothing here ever prints a path: a source is
# reported by its id and what it claims about itself.
def offenders(sources):
    """The sources that still carry a URL or are offered as a remote source clients may reach."""
    return [{"id": (s.get("Id") or "")[:8], "protocol": s.get("Protocol"), "remote": s.get("IsRemote"),
             "url": bool(URL.match(s.get("Path") or ""))}
            for s in sources or []
            if URL.match(s.get("Path") or "") or s.get("IsRemote") or s.get("Protocol") != "File"]


def masked(t, label, dto, playable=True):
    """The DTO names no URL: not in its own Path, not in any of its media sources."""
    srcs = dto.get("MediaSources") or []
    t.check(not URL.match(dto.get("Path") or ""), f"{label}: the item's own Path is not a URL")
    if not t.check(len(srcs) > 0, f"{label}: has media sources ({len(srcs)})"):
        return
    bad = offenders(srcs)
    t.check(not bad, f"{label}: {len(srcs)} sources, none with a URL or marked remote" + (f"; offenders {bad}" if bad else ""))
    if playable:
        # Masking must not take the source away: a client still has to be offered a way to play.
        t.check(all(s.get("SupportsDirectPlay") or s.get("SupportsDirectStream") for s in srcs),
                f"{label}: every source is still offered as playable")


def run(t):
    movie = t.movie()
    row = t.row(movie)
    episode = t.episodes()[0]["Id"]

    t.log("== item DTOs")
    masked(t, "GET /Items/{movie}", t.api.item(movie, "MediaSources"))
    masked(t, "GET /Items/{row}", t.api.item(row, "MediaSources"))
    masked(t, "GET /Items/{episode}", t.api.item(episode, "MediaSources"))

    # The legacy per-user route and the list route reach the same DTO service by another path.
    masked(t, "GET /Users/{user}/Items/{movie}", t.api.get(f"/Users/{t.api.user}/Items/{movie}?Fields=MediaSources"))
    listing = t.api.get(f"/Items?userId={t.api.user}&ids={movie},{episode}&Fields=MediaSources,Path")
    items = listing.get("Items") or []
    t.equal(len(items), 2, "the listing answers for both items")
    for item in items:
        masked(t, f"GET /Items?ids=… ({item.get('Type')})", item, playable=False)

    t.log("== both PlaybackInfo actions")
    st, get_pi = t.api.call("GET", f"/Items/{movie}/PlaybackInfo?userId={t.api.user}")
    t.equal(st, 200, "GET /Items/{id}/PlaybackInfo answers")
    masked(t, "GET /Items/{movie}/PlaybackInfo", get_pi)
    post_pi = t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user})
    masked(t, "POST /Items/{movie}/PlaybackInfo", post_pi)

    t.log("== playback still resolves the real URL inside Jellyfin")
    pi = t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": row})
    src = next((s for s in pi.get("MediaSources") or [] if s["Id"] == row), None)
    if t.check(src is not None and not pi.get("ErrorCode"), f"PlaybackInfo offers the row to play ({pi.get('ErrorCode')})"):
        st, _, body = t.api.request(f"/Videos/{movie}/stream?static=true&mediaSourceId={row}", {"Range": "bytes=0-0"}, max_bytes=1)
        t.check(st in (200, 206) and len(body) == 1, f"the stream endpoint still delivers bytes for the stubbed source ({st})")
