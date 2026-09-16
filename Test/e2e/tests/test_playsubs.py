DESCRIPTION = "Starting a stream that was never probed downloads its missing subtitles, and PlaybackInfo lists them once"
DESTRUCTIVE = True  # sets subtitle download languages on the movie library for the run and restores them

import re

from jfapi.bootstrap import MOVIE_PATH

LANGUAGES = ("eng", "ger")  # library options use three-letter codes
SUBTITLE_FILE = re.compile(r"\.(vtt|srt|ass|ssa|sub|idx|smi)$", re.IGNORECASE)


def subtitle_files(t, item_id):
    """Subtitle files in the item's internal metadata folder (library/<2>/<id>), where Gelato saves
    them and GetGelatoSubtitleFiles looks."""
    out = t.sh(f"find /config /media -type d -path '*/library/{item_id[:2]}/{item_id}' -exec ls -1 {{}} \\; 2>/dev/null")
    return sorted(f for f in out.split() if SUBTITLE_FILE.search(f))


def playback_info(t, movie, source):
    pi = t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": source})
    return next((s for s in pi.get("MediaSources", []) if s["Id"] == source), None)


def external_subs(src):
    return [m for m in (src or {}).get("MediaStreams", []) if m.get("Type") == "Subtitle" and m.get("IsExternal")]


def has_video(src):
    return any(m.get("Type") == "Video" for m in (src or {}).get("MediaStreams") or [])


ALIASES = {"eng": {"eng", "en"}, "ger": {"ger", "deu", "de"}}


def candidates(t):
    """(movie, row, languages the addon has a subtitle in) for stream rows that were never probed and
    have no saved subtitle."""
    for movie in (t.movie(), t.movie2()):
        srcs = t.api.item(movie).get("MediaSources") or []
        for s in [s for s in srcs[1:] if not has_video(s)][:3]:
            if subtitle_files(t, s["Id"]):
                continue
            langs = []
            for lang in LANGUAGES:
                st, res = t.api.call("GET", f"/Items/{s['Id']}/RemoteSearch/Subtitles/{lang}")
                if st == 200 and res:
                    langs.append(lang)
            if langs:
                yield movie, s["Id"], langs


def embedded_text(src, lang):
    """Jellyfin downloads nothing for a language the file already has a text subtitle in."""
    return any(m.get("Type") == "Subtitle" and not m.get("IsExternal") and m.get("IsTextSubtitleStream")
               and (m.get("Language") or "").lower() in ALIASES[lang] for m in (src or {}).get("MediaStreams") or [])


def movie_library(t):
    return next((v for v in t.api.get("/Library/VirtualFolders")
                 if v.get("CollectionType") == "movies" and MOVIE_PATH in v.get("Locations", [])), None)


def run(t):
    library = movie_library(t)
    if library is None:
        t.skip(f"no movie library on {MOVIE_PATH}")

    original = library["LibraryOptions"]
    options = {**original,
               "SubtitleDownloadLanguages": list(LANGUAGES),
               "DisabledSubtitleFetchers": [f for f in original.get("DisabledSubtitleFetchers") or [] if f != "Gelato Subtitles"],
               # the stream's own audio or embedded image tracks must not make Jellyfin skip the download
               "SkipSubtitlesIfAudioTrackMatches": False,
               "SkipSubtitlesIfEmbeddedSubtitlesPresent": False,
               # Gelato's results are never hash matches
               "RequirePerfectSubtitleMatch": False}
    t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": options})
    try:
        tried = 0
        for movie, row, langs in candidates(t):
            tried += 1
            t.log(f"== first playback of row {row[:8]} of {movie[:8]} (addon has {'/'.join(langs)})")
            src = playback_info(t, movie, row)
            if src is None or not has_video(src):
                t.log("the stream did not probe (dead link at the debrid service?), next row")
                continue
            wanted = [lang for lang in langs if not embedded_text(src, lang)]
            if not wanted:
                t.log("the release has embedded text subtitles in", "/".join(langs), "so nothing is downloaded, next row")
                continue
            t.log("expecting a download for", "/".join(wanted))
            files = subtitle_files(t, row)
            t.log("saved subtitle files:", files)
            t.check(files, f"a subtitle was saved for the row during the probe ({'/'.join(wanted)})")
            subs = external_subs(src)
            t.log("external subtitle streams:", [(m.get("Language"), m.get("Codec")) for m in subs])
            t.check(subs, "the same PlaybackInfo lists the external subtitle")
            t.equal(len(subs), len(files), "one external stream per saved file")

            t.log("== second and third playback of the row")
            src2 = playback_info(t, movie, row)
            t.equal(subtitle_files(t, row), files, "no further subtitle files (no .0 copies)")
            t.equal(len(external_subs(src2)), len(files), "external streams still one per file")
            t.equal(len(external_subs(playback_info(t, movie, row))), len(files), "a third PlaybackInfo lists the same")
            return
        t.skip(f"none of {tried} unprobed row(s) with an addon subtitle needed a download (embedded subtitles or dead links)"
               if tried else "no unprobed stream row without saved subtitles whose title has a subtitle in " + "/".join(LANGUAGES))
    finally:
        t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": original})
        restored = movie_library(t)["LibraryOptions"]
        t.equal(restored.get("SubtitleDownloadLanguages"), original.get("SubtitleDownloadLanguages"), "library subtitle languages restored")
