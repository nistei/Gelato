DESCRIPTION = "Subtitle search and download on an episode's stream row (what the download task does per item)"

import time
import urllib.parse

LANGUAGES = ("de", "en")


def run(t):
    ep = t.episodes()[0]["Id"]
    srcs = t.api.sources(ep)
    row = next((x for x in srcs if x != ep), None)
    if row is None:
        t.skip("episode 1 has no second stream")
    external = lambda item: t.db.one("select count(*) from MediaStreamInfos where StreamType='Subtitle' and IsExternal=1 and lower(replace(ItemId,'-',''))=?", (item,))[0]

    found = None
    for target, label in ((row, "row"), (ep, "episode")):
        for lang in LANGUAGES:
            st, res = t.api.call("GET", f"/Items/{target}/RemoteSearch/Subtitles/{lang}")
            t.check(st == 200 and isinstance(res, list), f"subtitle search on the {label} in {lang} answers ({st}, {len(res) if isinstance(res, list) else res})")
            if res and found is None:
                found = (target, label, lang, res[0])
    if found is None:
        t.log("the addon offers no subtitle in", LANGUAGES, "for this episode; download not exercised")
        return
    target, label, lang, sub = found
    before = external(target)
    st, d = t.api.call("POST", f"/Items/{target}/RemoteSearch/Subtitles/{urllib.parse.quote(sub['Id'], safe='')}")
    t.check(st < 300, f"download of the first {lang} subtitle on the {label}: {st}")
    time.sleep(4)
    # The database gets the stream with Jellyfin's follow-up refresh of the item; players see the
    # file through PlaybackInfo right away.
    t.log(f"external subtitle streams in the database on the {label}: {before} before, {external(target)} after")
    pi = t.api.post(f"/Items/{ep}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": target})
    src = next((s for s in pi.get("MediaSources", []) if s["Id"] == target), None)
    ext = [m for m in (src or {}).get("MediaStreams", []) if m.get("Type") == "Subtitle" and m.get("IsExternal")]
    t.check(ext, f"PlaybackInfo lists the external subtitle on the {label}")
