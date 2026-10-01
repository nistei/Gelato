DESCRIPTION = "an episode stream that plays on RemuxDB's media info has its segment lookup (IntroDB) run before the first play, not 30 seconds into it, and only once"
DESTRUCTIVE = True  # points Gelato's addon and RemuxDB at a stub on the host for the run of the test, then restores and resyncs

import re
import time

from jfapi.bootstrap import GELATO
from jfapi.probe import log_count
from tests.test_remuxdb import Stub, by_name, dashed, make_clip, playback_info, simple_version, wait_for

HASH_S = "5" * 40
SIZE_S = 3_000_000_011
LATER = 30  # Gelato's delayed probe of such a row, in seconds
LOOKUP = ("No intro from IntroDB for {}", "Skipping IntroDB lookup for {}", "IntroDB lookup failed for {}")


def run(t):
    cfg_path = "/Plugins/" + GELATO + "/Configuration"
    original = t.api.get(cfg_path)
    if not original.get("Url"):
        t.skip("Gelato has no addon URL")
    t.require("RemuxDbEnabled" in original, "this Gelato build has no RemuxDB support")

    episode = next((e for e in t.episodes() if re.fullmatch(r"tt\d+:1:\d+", t.fixtures.stremio_id(e["Id"]) or "")), None)
    t.require(episode, "no season 1 episode with an IMDb Stremio id in the fixture series")
    ep_id = t.fixtures.stremio_id(episode["Id"])
    series_imdb, season, number = ep_id.split(":")
    seconds = (episode.get("RunTimeTicks") or 3000 * 10**7) / 10**7
    t.require(seconds > 150, "the episode runs under 2 minutes, which always probes before playback")

    clip = make_clip(t, round(seconds), "128x72", 2)
    t.require(clip, "ffmpeg in the container could not make the test clip")
    stub = Stub(original["Url"], clip, clip)
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {stub.base}/clip/x.mkv && echo ok"):
            t.skip(f"the container cannot reach the host on port {stub.port}")

        stub.streams[f"/stream/series/{ep_id}.json"] = [
            {"name": "intro-S", "url": f"{stub.base}/clip/S.mkv",
             "behaviorHints": {"bingeGroup": "intro-S", "filename": "Test.Show.S01.Intro.mkv", "videoSize": SIZE_S},
             "streamData": {"size": SIZE_S, "torrent": {"infoHash": HASH_S, "fileIdx": 0}}}]
        known = simple_version(seconds, SIZE_S, [{"kind": "torrent", "filename": "Show/Test.Show.S01.Intro.mkv",
                                                  "torrent_info_hash": HASH_S, "torrent_file_idx": 0}])
        # Complete info for playback: a non-H.264 video track with a bitrate, so nothing is probed first
        video = known["tracks"][0]
        video.update({"codec": "hevc", "bit_rate": 7_000_000})
        stub.versions[(series_imdb, int(season), int(number))] = [known]

        # Pre-probing off: opening the episode would do the lookup before the playback this tests
        t.api.post(cfg_path, {**original, "Url": f"{stub.base}/addon/manifest.json",
                              "RemuxDbUrl": f"{stub.base}/remuxdb", "RemuxDbEnabled": True, "RemuxDbContribute": False,
                              **({"PreProbe": False} if "PreProbe" in original else {})})

        row = by_name(t.api.item(episode["Id"])).get("intro-S", {})
        t.require(row, "the episode lists the stub's stream")
        row_id = dashed(row["ETag"])
        segments = lambda: t.db.one("select count(*) from MediaSegments where lower(replace(ItemId,'-',''))=?",
                                    (row["ETag"].replace("-", "").lower(),))[0]
        lookups = lambda: sum(log_count(t, p.format(row_id)) for p in LOOKUP)
        t.equal((segments(), lookups()), (0, 0), "the row has no segments and no lookup before it plays")

        started = time.time()
        pi = playback_info(t, episode["Id"], row)
        played = next((s for s in pi.get("MediaSources", []) if s.get("Id") == row["Id"]), {})
        t.check(any(m.get("Type") == "Video" for m in played.get("MediaStreams") or []), "the row plays")
        t.equal(log_count(t, f"Probing stream for {row_id}"), 0, "it plays on RemuxDB's media info, without a probe first")

        # IntroDB answers within seconds, or the lookup fails and says so: either leaves a trace.
        # Playback waits 1.5 s for it at most, a slow answer lands in the background.
        found = wait_for(lambda: segments() > 0 or lookups() > 0, 15)
        t.check(found, "the segment lookup ran after the playback info")
        t.check(time.time() - started < LATER - 5, "and well before the delayed probe's 30 seconds")

        # The delayed probe no longer looks segments up a second time
        done = lookups()
        t.check(wait_for(lambda: log_count(t, f"Probing stream for {row_id}") >= 1, 90), "the row is probed in the background afterwards")
        time.sleep(3)
        t.equal(lookups(), done, "the delayed probe does not repeat the segment lookup")

        # A row that has segments is not looked up again on the next playback
        if segments() > 0:
            playback_info(t, episode["Id"], row)
            time.sleep(2)
            t.equal(lookups(), done, "a row with segments is not looked up again")
    finally:
        t.api.post(cfg_path, {**t.api.get(cfg_path), **{k: original.get(k) for k in
                   ("Url", "RemuxDbUrl", "RemuxDbEnabled", "RemuxDbContribute", "PreProbe")}})
        t.api.call("GET", f"/Items/{episode['Id']}?userId={t.api.user}", timeout=90)
        stub.close()
        left = t.db.one("select count(*) from BaseItems where Path like ?", (f"%host.docker.internal:{stub.port}%",))[0]
        t.equal(left, 0, "no stub row is left after the real streams are synced again")
