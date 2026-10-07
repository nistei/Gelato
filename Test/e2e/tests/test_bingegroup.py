DESCRIPTION = "the next episode follows the binge group of the stream an episode is played with: from the start of the playback its stream of that group is listed first and has the episode's id, it is the one pre-probed near the end and the one a playback gets that names the episode's id (Jellyfin Web's autoplay) or no source; a group the next episode lacks leaves its order alone"
DESTRUCTIVE = True  # points Gelato's addon at a stub on the host for the run of the test, then restores and resyncs

import re

from jfapi.bootstrap import GELATO
from tests.test_remuxdb import Stub, make_clip, probes_of, wait_for

CLIP_SECONDS = 150  # over the 2 minutes below which playback always probes


def stream(key, group, base):
    return {"name": f"binge-{key}", "description": f"Test.Binge.{key}.mkv", "url": f"{base}/clip/{key}.mkv",
            "behaviorHints": {"bingeGroup": f"binge-{group}", "filename": f"Test.Binge.{key}.mkv"}}


def run(t):
    cfg_path = "/Plugins/" + GELATO + "/Configuration"
    original = t.api.get(cfg_path)
    if not original.get("Url"):
        t.skip("Gelato has no addon URL")

    episodes = sorted((e for e in t.episodes() if re.fullmatch(r"tt\d+:1:\d+", t.fixtures.stremio_id(e["Id"]) or "")),
                      key=lambda e: e.get("IndexNumber") or 0)
    pair = next(((a, b) for a, b in zip(episodes, episodes[1:])
                 if (b.get("IndexNumber") or 0) == (a.get("IndexNumber") or 0) + 1), None)
    t.require(pair, "the fixture series has no two consecutive season 1 episodes with IMDb Stremio ids")
    first, second = pair
    runtime = first.get("RunTimeTicks") or 1500 * 10**7

    clip = make_clip(t, CLIP_SECONDS, "128x72", 2)
    t.require(clip, "ffmpeg in the container could not make the test clip")
    stub = Stub(original["Url"], clip, clip)

    # With a field list: not a page visit, so the looks this test takes pre-probe nothing.
    def sources(ep):
        return {(s.get("Name") or "").split("\n")[0]: s
                for s in t.api.item(ep["Id"], fields="Path").get("MediaSources") or []}

    def order(ep):
        return [n for n in sources(ep) if n.startswith("binge-")]

    playing = []

    def play(source):
        if playing:
            t.api.report("stop", first["Id"], playing.pop()["Id"], 0)
        t.api.report("start", first["Id"], source["Id"], 0)
        playing.append(source)

    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {stub.base}/clip/x.mkv && echo ok"):
            t.skip(f"the container cannot reach the host on port {stub.port}")

        # Group 1 is the first episode's default stream and the next episode's second, group 2
        # the next episode's third, and group 3 is not among the next episode's.
        stub.streams[f"/stream/series/{t.fixtures.stremio_id(first['Id'])}.json"] = [
            stream("1a", 1, stub.base), stream("1b", 2, stub.base), stream("1c", 3, stub.base)]
        stub.streams[f"/stream/series/{t.fixtures.stremio_id(second['Id'])}.json"] = [
            stream("2z", 9, stub.base), stream("2a", 1, stub.base), stream("2b", 2, stub.base)]
        t.api.post(cfg_path, {**original, "Url": f"{stub.base}/addon/manifest.json", "PreProbe": True})

        one = sources(first)
        t.equal(order(first), ["binge-1a", "binge-1b", "binge-1c"], "the first episode lists the stub's streams")
        t.equal(order(second), ["binge-2z", "binge-2a", "binge-2b"],
                "the next episode lists its streams in the addon's order while nothing plays")
        t.equal(one.get("binge-1a", {}).get("Id"), first["Id"], "the first episode's default stream has the episode's id")

        # ---- a stream picked by hand: the next episode lists its group first
        play(one["binge-1b"])
        t.check(wait_for(lambda: order(second)[:1] == ["binge-2b"], 20),
                "from the start of an episode the next one lists the stream of the playing one's group first")
        t.equal(order(second), ["binge-2b", "binge-2z", "binge-2a"], "and the rest in the addon's order")
        t.equal(order(first), ["binge-1a", "binge-1b", "binge-1c"], "the episode that plays keeps its order")
        two = sources(second)
        t.equal(two["binge-2b"]["Id"], second["Id"], "the group's stream is the one with the next episode's id")
        t.equal([probes_of(t, two[f"binge-{k}"]) for k in ("2z", "2a", "2b")], [0, 0, 0],
                "nothing is probed before the episode nears its end")

        # ---- near the end the group's stream is the one prepared, and the one that plays
        t.api.report("progress", first["Id"], one["binge-1b"]["Id"], int(runtime * 0.9))
        t.check(wait_for(lambda: probes_of(t, two["binge-2b"]) >= 1, 60),
                "near the end the next episode's stream of the group is probed")
        t.equal((probes_of(t, two["binge-2z"]), probes_of(t, two["binge-2a"])), (0, 0),
                "the stream the addon lists first is not probed")
        for body, what in (({"MediaSourceId": second["Id"]}, "the episode's id as its source, as Jellyfin Web's autoplay does,"),
                           ({}, "no source")):
            pi = t.api.post(f"/Items/{second['Id']}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, **body})
            t.equal([(s.get("Name") or "").split("\n")[0] for s in pi.get("MediaSources") or []], ["binge-2b"],
                    f"a playback of the next episode that names {what} gets the group's stream")
        t.equal(probes_of(t, two["binge-2b"]), 1, "which is not probed again")

        # ---- the default stream, played under the episode's id
        play(one["binge-1a"])
        t.check(wait_for(lambda: order(second)[:1] == ["binge-2a"], 20),
                "a playback of the source with the episode's id is followed by its row's group")

        # ---- a group the next episode has no stream of
        play(one["binge-1c"])
        t.check(wait_for(lambda: order(second) == ["binge-2z", "binge-2a", "binge-2b"], 20),
                "a group the next episode lacks leaves it in the addon's order")
        t.equal(sources(second)["binge-2z"]["Id"], second["Id"], "with the episode's id back on the addon's first stream")
    finally:
        if playing:
            t.api.report("stop", first["Id"], playing.pop()["Id"], 0)
        t.api.post(cfg_path, {**t.api.get(cfg_path), **{k: original.get(k) for k in ("Url", "PreProbe")}})
        for ep in pair:
            t.api.call("GET", f"/Items/{ep['Id']}?userId={t.api.user}", timeout=90)
        stub.close()
        left = t.db.one("select count(*) from BaseItems where Path like ?", (f"%host.docker.internal:{stub.port}%",))[0]
        t.equal(left, 0, "no stub row is left after the real streams are synced again")
