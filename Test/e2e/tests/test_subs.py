DESCRIPTION = "'Download missing subtitles' twice per save setting: stream rows get one file in their metadata folder, placeholders nothing, the second run downloads nothing (prod findings 5 and 15)"
DESTRUCTIVE = True  # sets subtitle download languages on the Gelato libraries, runs the task over the whole library four times

import re

from jfapi.bootstrap import gelato_libraries
from jfapi.db import norm

LANGUAGES = ["eng", "ger"]  # prod's libraries download English and German
SUBTITLE = r"-name '*.vtt' -o -name '*.srt' -o -name '*.ass' -o -name '*.ssa' -o -name '*.sub' -o -name '*.smi'"
IN_LIBRARY = re.compile(r"/library/[0-9a-f]{2}/([0-9a-f]{32})/[^/]+$")
NUMBERED = re.compile(r"\.[a-z]{2,3}\.\d+\.[a-z]+$")


def files(t):
    """Subtitle files Jellyfin can have written: under /config and /media, and in / (the process's
    working directory, where a stream row's subtitle went with SaveSubtitlesWithMedia on)."""
    found = t.sh(f"find /config /media \\( {SUBTITLE} \\) -type f 2>/dev/null").split("\n")
    cwd = t.sh(f"find / -maxdepth 1 \\( {SUBTITLE} \\) -type f 2>/dev/null").split("\n")
    return sorted(f for f in found if f), sorted(f for f in cwd if f)


def invalid_paths(t):
    """Downloads Jellyfin rejected because the target path was invalid (a placeholder's gelato:// path)."""
    out = t.sh("cat /config/log/log_*.log 2>/dev/null | grep -c 'resulting path was invalid'").strip()
    return int(out or 0)


def run_twice(t, label, stream_rows):
    before, _ = files(t)
    invalid = invalid_paths(t)
    status, msg = t.api.run_task("DownloadSubtitles", timeout=3600)
    t.equal(status, "Completed", f"{label}, first run finished {msg}")
    first, cwd = files(t)
    new = sorted(set(first) - set(before))
    t.log(f"{label}, first run: {len(new)} new file(s), {len(first)} in all")
    status, msg = t.api.run_task("DownloadSubtitles", timeout=3600)
    t.equal(status, "Completed", f"{label}, second run finished {msg}")
    second, cwd2 = files(t)
    t.equal(sorted(set(second) - set(first)), [], f"{label}: the second run downloads nothing")
    t.equal(cwd + cwd2, [], f"{label}: no subtitle in the working directory")
    t.equal(invalid_paths(t) - invalid, 0, f"{label}: no download rejected for an invalid path")

    misplaced = [f for f in second if (m := IN_LIBRARY.search(f)) and m.group(1) not in stream_rows]
    t.equal(misplaced, [], f"{label}: every saved subtitle belongs to a stream row, none to a placeholder")
    t.equal([f for f in second if NUMBERED.search(f)], [], f"{label}: no numbered copies (.en.0.vtt)")
    return new


def run(t):
    libraries = [v for v in gelato_libraries(t.api)]
    if not libraries:
        t.skip("no Gelato libraries")
    # stream rows to download for: a movie and an episode, synced by opening them
    for item in (t.movie(), t.movie2(), t.episodes()[0]["Id"]):
        t.api.sources(item)
    stream_rows = t.db.stream_row_ids()
    t.log(f"{len(stream_rows)} stream rows, libraries {[v['Name'] for v in libraries]}")

    originals = {v["ItemId"]: v["LibraryOptions"] for v in libraries}
    saved = []
    try:
        # prod runs with SaveSubtitlesWithMedia on; "off" is the setup where subtitles showed up before the fix
        for with_media in (True, False):
            label = f"SaveSubtitlesWithMedia {'on' if with_media else 'off'}"
            for v in libraries:
                opts = {**originals[v["ItemId"]], "SubtitleDownloadLanguages": LANGUAGES, "SaveSubtitlesWithMedia": with_media,
                        "SkipSubtitlesIfAudioTrackMatches": False, "RequirePerfectSubtitleMatch": False,
                        "DisabledSubtitleFetchers": [f for f in originals[v["ItemId"]].get("DisabledSubtitleFetchers") or [] if f != "Gelato Subtitles"]}
                t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": v["ItemId"], "LibraryOptions": opts})
            saved += run_twice(t, label, stream_rows)
    finally:
        for item_id, opts in originals.items():
            t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": item_id, "LibraryOptions": opts})

    if not saved:
        t.log("the addon had no subtitle for any stream row in", LANGUAGES, "so the skip on an already saved file was not exercised")
        return
    rows = {IN_LIBRARY.search(f).group(1) for f in saved if IN_LIBRARY.search(f)}
    linked = t.db.query("select lower(replace(Id,'-','')), lower(replace(PrimaryVersionId,'-','')) from BaseItems "
                        "where PrimaryVersionId is not null and lower(replace(Id,'-','')) in ({})".format(",".join("?" * len(rows))), tuple(rows))
    t.log(f"{len(saved)} file(s) saved for {len(rows)} stream row(s), {len(linked)} of them linked to their title")
    # finding 15: the "already saved" skip has to find linked rows, which Jellyfin 12 leaves out of plain queries
    t.check(linked, "subtitles were saved for linked stream rows, so the second runs checked the skip on them")
    # rows belong to the user whose visit synced them, so look for one the admin's PlaybackInfo lists
    for row, owner in linked:
        pi = t.api.post(f"/Items/{owner}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": row})
        src = next((s for s in pi.get("MediaSources", []) if norm(s["Id"]) == row), None)
        if src is None:
            continue
        ext = [m for m in src.get("MediaStreams", []) if m.get("Type") == "Subtitle" and m.get("IsExternal")]
        t.check(ext, f"PlaybackInfo offers the saved subtitle on row {row[:8]}: {[(m.get('Language'), m.get('Codec')) for m in ext]}")
        break
    else:
        t.log(f"none of the {len(linked)} rows with a subtitle belongs to {t.api.name}; PlaybackInfo not checked")
