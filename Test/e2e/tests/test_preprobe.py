DESCRIPTION = "a stream that needs a probe is probed before it is played: when its item is opened (the default stream) or another version is picked (the web client loads the row), and the next episode while an episode nears its end; playback then does not probe again; with the setting off nothing is probed ahead"
DESTRUCTIVE = True  # points Gelato's addon at a stub on the host for the run of the test, then restores and resyncs

import re
import time

from jfapi.bootstrap import GELATO
from jfapi.probe import log_count
from tests.test_remuxdb import Stub, by_name, dashed, make_clip, playback_info, probes_of, streams_of, wait_for

CLIP_SECONDS = 150  # over the 2 minutes below which playback always probes


def stream(key, base):
    return {"name": f"pre-{key}", "description": f"Test.PreProbe.{key}.mkv", "url": f"{base}/clip/{key}.mkv",
            "behaviorHints": {"bingeGroup": f"pre-{key}", "filename": f"Test.PreProbe.{key}.mkv"}}


def run(t):
    cfg_path = "/Plugins/" + GELATO + "/Configuration"
    original = t.api.get(cfg_path)
    if not original.get("Url"):
        t.skip("Gelato has no addon URL")
    t.require("PreProbe" in original, "this Gelato build has no pre-probe setting")

    movie = t.movie()
    imdb = t.fixtures.stremio_id(movie) or ""
    t.require(re.fullmatch(r"tt\d+", imdb), f"the fixture movie has no IMDb Stremio id ({imdb})")
    episodes = sorted((e for e in t.episodes() if re.fullmatch(r"tt\d+:1:\d+", t.fixtures.stremio_id(e["Id"]) or "")),
                      key=lambda e: e.get("IndexNumber") or 0)
    pair = next(((a, b) for a, b in zip(episodes, episodes[1:])
                 if (b.get("IndexNumber") or 0) == (a.get("IndexNumber") or 0) + 1), None)

    clip = make_clip(t, CLIP_SECONDS, "128x72", 2)
    t.require(clip, "ffmpeg in the container could not make the test clip")
    stub = Stub(original["Url"], clip, clip)
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {stub.base}/clip/x.mkv && echo ok"):
            t.skip(f"the container cannot reach the host on port {stub.port}")

        stub.streams[f"/stream/movie/{imdb}.json"] = [stream(k, stub.base) for k in "ABC"]
        if pair:
            for ep in pair:
                ep_id = t.fixtures.stremio_id(ep["Id"])
                stub.streams[f"/stream/series/{ep_id}.json"] = [
                    stream(f"E{ep.get('IndexNumber')}{k}", stub.base) for k in "ab"]

        # No RemuxDB data for any of them: every stream needs a probe. Saving the configuration
        # clears Gelato's stream cache, so the next visit syncs again.
        t.api.post(cfg_path, {**original, "Url": f"{stub.base}/addon/manifest.json", "PreProbe": True})

        # ---- opening the movie probes its default stream, and only that one
        sources = by_name(t.api.item(movie))
        t.equal(sorted(k for k in sources if k.startswith("pre-")), ["pre-A", "pre-B", "pre-C"],
                "the movie lists the stub's streams")
        a, b, c = (sources.get(f"pre-{k}", {}) for k in "ABC")
        t.check(wait_for(lambda: probes_of(t, a) >= 1, 60), "opening the movie probes its default stream")
        time.sleep(3)
        t.equal((probes_of(t, b), probes_of(t, c)), (0, 0), "the other versions are not probed by opening the movie")

        # ---- playing it does not probe again, and has the probe's tracks
        pi = playback_info(t, movie, a)
        played = next((s for s in pi.get("MediaSources", []) if s.get("Id") == a.get("Id")), {})
        t.equal(probes_of(t, a), 1, "playing the pre-probed stream does not probe it again")
        t.equal([(v.get("Codec"), v.get("Width")) for v in streams_of(played, "Video")], [("h264", 128)],
                "its PlaybackInfo has the probe's tracks")

        # ---- picking another version: the web client loads the version's row as an item
        t.api.item(b["ETag"])
        t.check(wait_for(lambda: probes_of(t, b) >= 1, 60), "picking a version probes it")
        playback_info(t, movie, b)
        time.sleep(2)
        t.equal(probes_of(t, b), 1, "playing the picked version does not probe again")

        # ---- a playback that arrives while the pre-probe runs waits for it: one probe
        t.api.item(c["ETag"])
        playback_info(t, movie, c)
        time.sleep(3)
        t.equal(probes_of(t, c), 1, "a playback during the pre-probe shares its probe")

        # ---- setting off: nothing is probed ahead
        stub.streams[f"/stream/movie/{imdb}.json"] = [stream(k, stub.base) for k in "DEF"]
        t.api.post(cfg_path, {**t.api.get(cfg_path), "PreProbe": False})
        sources = by_name(t.api.item(movie))
        d, e = sources.get("pre-D", {}), sources.get("pre-E", {})
        t.api.item(e["ETag"])
        time.sleep(8)
        t.equal((probes_of(t, d), probes_of(t, e)), (0, 0),
                "with the setting off opening an item or picking a version probes nothing")
        playback_info(t, movie, d)
        t.equal(probes_of(t, d), 1, "and playback probes as before")
        t.api.post(cfg_path, {**t.api.get(cfg_path), "PreProbe": True})

        # ---- flicking through the versions: the page and the versions passed are not probed, the
        # one stopped on is
        stub.streams[f"/stream/movie/{imdb}.json"] = [stream(k, stub.base) for k in "GHI"]
        t.api.post(cfg_path, {**t.api.get(cfg_path), "PreProbe": True})
        sources = by_name(t.api.item(movie))
        g, h, i = (sources.get(f"pre-{k}", {}) for k in "GHI")
        t.api.item(h["ETag"])
        t.api.item(i["ETag"])
        t.check(wait_for(lambda: probes_of(t, i) >= 1, 60), "the version stopped on is probed")
        time.sleep(4)
        t.equal((probes_of(t, g), probes_of(t, h)), (0, 0), "the page and the version passed on the way are not probed")

        # ---- the next episode, while an episode nears its end
        if pair:
            first, second = pair
            runtime = first.get("RunTimeTicks") or 1500 * 10**7
            first_src = next(iter(by_name(t.api.item(first["Id"])).values()), {})
            key = f"E{second.get('IndexNumber')}"
            row = lambda: t.db.one("select lower(replace(Id,'-','')) from BaseItems where Path like ?",
                                   (f"%/clip/{key}a.mkv%",))
            t.api.report("start", first["Id"], first_src.get("Id"), 0)
            t.api.report("progress", first["Id"], first_src.get("Id"), int(runtime * 0.5))
            time.sleep(5)
            t.check(row() is None, "halfway through an episode the next one is not looked at")
            t.api.report("progress", first["Id"], first_src.get("Id"), int(runtime * 0.9))
            t.check(wait_for(lambda: row() is not None, 60), "near its end the next episode's streams are synced")
            if row():
                row_id = dashed(row()[0])
                t.check(wait_for(lambda: log_count(t, f"Probing stream for {row_id}") >= 1, 60),
                        "and its default stream is probed before anyone plays it")
            t.api.report("stop", first["Id"], first_src.get("Id"), int(runtime * 0.9))
        else:
            t.log("no two consecutive season 1 episodes with IMDb Stremio ids in the fixture series: episode part left out")
    finally:
        t.api.post(cfg_path, {**t.api.get(cfg_path), **{k: original.get(k) for k in ("Url", "PreProbe")}})
        t.api.call("GET", f"/Items/{movie}?userId={t.api.user}", timeout=90)
        if pair:
            for ep in pair:
                t.api.call("GET", f"/Items/{ep['Id']}?userId={t.api.user}", timeout=90)
        stub.close()
        left = t.db.one("select count(*) from BaseItems where Path like ?", (f"%host.docker.internal:{stub.port}%",))[0]
        t.equal(left, 0, "no stub row is left after the real streams are synced again")
