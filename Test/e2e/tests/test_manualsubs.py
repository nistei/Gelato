DESCRIPTION = "The subtitle dialog on a movie's or episode's own page: the search is ranked by the release that plays, a downloaded or uploaded subtitle is offered on the playing source and on every version, and its delete removes the file"

import base64
import os
import urllib.parse

from jfapi.db import norm
from jfapi.probe import log_count

LANGUAGES = ("eng", "ger")
UPLOAD_LANGUAGE = "swe"  # one the download does not use, so the two files are told apart by name
UPLOAD = b"1\n00:00:01,000 --> 00:00:04,000\nuploaded on the title's page\n"
SUBTITLE = r"-name '*.vtt' -o -name '*.srt' -o -name '*.ass' -o -name '*.ssa' -o -name '*.sub' -o -name '*.smi'"


def folder_files(t, item):
    """Subtitle files in the item's metadata folder (library/<2>/<id>), with their paths."""
    out = t.sh(f"find /config /media -type f -path '*/library/{item[:2]}/{item}/*' \\( {SUBTITLE} \\) 2>/dev/null")
    return sorted(f for f in out.split("\n") if f)


def external(streams):
    """{path: index} of the external subtitle streams."""
    return {m["Path"]: m["Index"] for m in streams or [] if m.get("Type") == "Subtitle" and m.get("IsExternal") and m.get("Path")}


def listing(t, item):
    """The item as the dialog loads it. Asked with a field list, which Jellyfin answers with every
    field anyway: without one Gelato takes the request for a page visit and pre-probes the stream."""
    return t.api.item(item, fields="Path")


def saved_for(t, item, before):
    """Files the dialog's requests added to the item's folder."""
    return [f for f in folder_files(t, item) if f not in before]


def dialog(t, item, label):
    """The requests Jellyfin Web's subtitle dialog sends for the item, and what a player gets after
    each. Returns False when the addon has no subtitle for the title."""
    sources = listing(t, item).get("MediaSources") or []
    rows = [s for s in sources if norm(s.get("ETag") or "") != item]
    if not rows or sources[0] is not rows[0]:
        t.log(f"{label}: no stream row to play")
        return False
    first, row = sources[0], norm(sources[0]["ETag"])
    stremio = t.fixtures.stremio_id(item)

    # ---- search: ranked against the playing release, not against the placeholder's "tt…" name
    by_id = log_count(t, f'release name: "{stremio}"')
    found = None
    for lang in LANGUAGES:
        st, res = t.api.call("GET", f"/Items/{item}/RemoteSearch/Subtitles/{lang}")
        t.check(st == 200 and isinstance(res, list), f"{label}: the search in {lang} answers ({st})")
        if st == 200 and res:
            found = (lang, res)
            break
    if found is None:
        t.log(f"{label}: the addon has no subtitle in {LANGUAGES}")
        return False
    lang, res = found
    t.equal(log_count(t, f'release name: "{stremio}"') - by_id, 0, f"{label}: no search ranked against the placeholder's name {stremio}")
    st, on_row = t.api.call("GET", f"/Items/{row}/RemoteSearch/Subtitles/{lang}")
    t.check(st == 200 and [s["Id"] for s in on_row] == [s["Id"] for s in res],
            f"{label}: {len(res)} results, in the order of a search on the row that plays")

    before = folder_files(t, item)
    try:
        # ---- download: on the source the page plays, and on every other version
        st, _ = t.api.call("POST", f"/Items/{item}/RemoteSearch/Subtitles/{urllib.parse.quote(res[0]['Id'], safe='')}")
        t.check(st < 300, f"{label}: the download of the first result answers ({st})")
        downloaded = saved_for(t, item, before)
        t.equal(len(downloaded), 1, f"{label}: one file saved in the {label}'s folder {[os.path.basename(f) for f in downloaded]}")
        if not downloaded:
            return True

        pi = t.api.post(f"/Items/{item}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": first["Id"]})
        playing = next((s for s in pi.get("MediaSources") or [] if s["Id"] == first["Id"]), None)
        offered = external((playing or {}).get("MediaStreams"))
        t.check(downloaded[0] in offered, f"{label}: PlaybackInfo offers the downloaded subtitle on the playing source ({len(offered)} external)")
        if downloaded[0] in offered:
            st, body = t.api.call("GET", f"/Videos/{item}/{first['Id']}/Subtitles/{offered[downloaded[0]]}/0/Stream.vtt", raw=True)
            t.check(st == 200 and "-->" in (body or ""), f"{label}: the player's request for it delivers cues ({st})")
        listed = [s for s in listing(t, item).get("MediaSources") or [] if downloaded[0] in external(s.get("MediaStreams"))]
        t.equal(len(listed), len(rows), f"{label}: listed on every one of the {len(rows)} versions")

        # ---- upload
        st, _ = t.api.call("POST", f"/Videos/{item}/Subtitles", {"Data": base64.b64encode(UPLOAD).decode(), "Language": UPLOAD_LANGUAGE,
                                                                 "Format": "srt", "IsForced": False, "IsHearingImpaired": False})
        t.check(st < 300, f"{label}: the upload answers ({st})")
        uploaded = [f for f in saved_for(t, item, before) if f not in downloaded]
        t.equal(len(uploaded), 1, f"{label}: one uploaded file in the {label}'s folder {[os.path.basename(f) for f in uploaded]}")
        streams = external(listing(t, item).get("MediaStreams"))
        t.check(uploaded and uploaded[0] in streams, f"{label}: the dialog lists the uploaded subtitle ({UPLOAD_LANGUAGE})")

        # ---- delete: by the index the dialog shows, which a removed file shifts
        for path in downloaded + uploaded:
            index = external(listing(t, item).get("MediaStreams")).get(path)
            if index is None:
                t.check(False, f"{label}: {os.path.basename(path)} is listed, so it can be deleted")
                continue
            st, _ = t.api.call("DELETE", f"/Videos/{item}/Subtitles/{index}")
            t.check(st < 300, f"{label}: the delete of subtitle {index} answers ({st})")
        t.equal(saved_for(t, item, before), [], f"{label}: the deleted files are gone")
        t.equal([p for p in downloaded + uploaded if p in external(listing(t, item).get("MediaStreams"))], [], f"{label}: and no longer listed")
    finally:
        for path in saved_for(t, item, before):
            t.sh("rm -f '" + path.replace("'", "'\\''") + "'")
    return True


def run(t):
    episodes = t.episodes()
    titles = [(t.movie(), "movie")] + ([(episodes[0]["Id"], "episode")] if episodes else [])
    answered = [label for item, label in titles if dialog(t, norm(item), label)]
    t.require(answered, f"the addon has no subtitle in {LANGUAGES} for the movie or the episode")
