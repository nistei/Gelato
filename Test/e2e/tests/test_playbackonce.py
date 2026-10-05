DESCRIPTION = "starting a playback prepares its source once for PlaybackInfo and once for the HLS playlists: the variant playlist reuses the master's answer for 10 seconds from the end of its preparation, also when a first probe makes that take longer than the 10 seconds, another version is prepared on its own, and the player reloading the item does not pre-probe the source it plays"
DESTRUCTIVE = True  # points Gelato's addon at a stub on the host for the slow first probe, then restores and resyncs; without Gelato's Debug lines, sets Gelato to Debug in logging.json and restarts, both ways

# Counts Gelato's Debug lines. Instances from dev/jf.py log Gelato at Debug; elsewhere the test sets it in
# logging.json and restarts the server, since Jellyfin applies a changed level only on a restart.

import base64
import json
import re
import subprocess
import time
import uuid

from jfapi.bootstrap import GELATO, wait_ready
from tests.test_remuxdb import Stub, by_name, dashed, make_clip

LOGGING = "/config/config/logging.json"
SHARED_FOR = 10  # seconds a prepared source answers the streaming requests that follow it
DEVICE = "jfapi-playbackonce"
FLUSH = 3  # seconds the log file may lag behind
SLOW = SHARED_FOR + 2  # seconds the stub holds back a clip's first answer, which is the probe's
CLIP_SECONDS = 150  # over the 2 minutes below which playback always probes


def count(t, pattern):
    """Lines in Jellyfin's logs matching the extended regex. The log file quotes string values."""
    out = t.sh(f"cat /config/log/log_*.log 2>/dev/null | grep -c -E '{pattern}'").strip()
    return int(out or 0)


def settled(read):
    """The value once the log file has caught up: unchanged over a second, at most FLUSH seconds."""
    value, deadline = read(), time.time() + FLUSH
    while time.time() < deadline:
        time.sleep(1)
        value, last = read(), value
        if value == last:
            break
    return value


def write_file(t, path, text):
    t.sh(f"echo {base64.b64encode(text.encode()).decode()} | base64 -d > {path}")


def restart(t):
    subprocess.run(["docker", "restart", t.db.container], capture_output=True)
    t.require(wait_ready(t.api.base, t.log) is not None, f"{t.api.base} is back after the restart")
    t.api.ensure()


def slow_stub(upstream, clip):
    """The addon with every clip's first request answered only after SLOW seconds."""
    stub = Stub(upstream, clip, clip)
    handler, held = stub.server.RequestHandlerClass, set()
    serve = handler.serve_clip

    def serve_slowly(self, data):
        with stub.lock:
            first = self.path not in held
            held.add(self.path)
        if first:
            time.sleep(SLOW)
        return serve(self, data)

    handler.serve_clip = serve_slowly
    return stub


def slow_first_probe(t, movie, playlist, state):
    """A version nobody probed yet is probed by its master playlist, and that can take longer than
    the 10 seconds its answer is shared for: a variant playlist that came 11.3 s after its master
    prepared the source again while the 10 seconds counted from the start of the preparation."""
    cfg_path = "/Plugins/" + GELATO + "/Configuration"
    original = t.api.get(cfg_path)
    imdb = t.fixtures.stremio_id(movie) or ""
    if not original.get("Url") or not re.fullmatch(r"tt\d+", imdb):
        return t.log(f"no addon URL or no IMDb Stremio id for the fixture movie ({imdb}): the slow first probe is left out")
    clip = make_clip(t, CLIP_SECONDS, "128x72", 2)
    t.require(clip, "ffmpeg in the container could not make the test clip")
    stub = slow_stub(original["Url"], clip)
    restore = {k: original[k] for k in ("Url", "PreProbe") if k in original}
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {stub.base}/reachable && echo ok"):  # a 404, not a held clip
            return t.log(f"the container cannot reach the host on port {stub.port}: the slow first probe is left out")

        # No RemuxDB data for the stub's streams: each needs a probe. Nothing is probed ahead.
        streams = [{"name": f"once-{k}", "description": f"Test.PlaybackOnce.{k}.mkv", "url": f"{stub.base}/clip/{k}.mkv",
                    "behaviorHints": {"bingeGroup": f"once-{k}", "filename": f"Test.PlaybackOnce.{k}.mkv"}} for k in "AB"]
        stub.streams[f"/stream/movie/{imdb}.json"] = streams
        t.api.post(cfg_path, {**original, "Url": f"{stub.base}/addon/manifest.json", **({"PreProbe": False} if "PreProbe" in original else {})})
        cold = by_name(t.api.item(movie)).get("once-B")
        t.require(cold, "the movie lists the stub's streams")

        before = settled(lambda: state(cold))
        started = time.time()
        st_m, _ = playlist("master", cold)
        took = time.time() - started
        st_v, _ = playlist("main", cold)
        t.equal((st_m, st_v), (200, 200), "the playlists of a version nobody probed yet answer")
        t.check(took > SHARED_FOR, f"its master playlist takes longer than the {SHARED_FOR} seconds ({took:.1f} s)")
        after = settled(lambda: state(cold))
        t.equal((after[0] - before[0], after[1] - before[1]), (1, 0),
                "the variant playlist does not prepare it again, however long the master took")
        t.check(after[2] - before[2] >= 1, "it takes the master's answer")
    finally:
        t.api.post(cfg_path, {**t.api.get(cfg_path), **restore})
        t.api.call("GET", f"/Items/{movie}?userId={t.api.user}", timeout=90)
        stub.close()
        left = t.db.one("select count(*) from BaseItems where Path like ?", (f"%host.docker.internal:{stub.port}%",))[0]
        t.equal(left, 0, "no stub row is left after the real streams are synced again")


def run(t):
    original = t.sh(f"cat {LOGGING} 2>/dev/null").strip()
    changed = False

    movie = t.movie()
    # Asked with a field list, which Gelato does not take for a page visit: a visit schedules a
    # pre-probe of the first source, and that one ran 9 s later (behind other probes) and was
    # counted as the player's.
    sources = t.api.item(movie, fields="Path").get("MediaSources") or []
    first = next((s for s in sources if s["Id"] == movie), None)
    t.require(first, "the movie lists no stream under its own id")
    other = next((s for s in sources if s["Id"] != movie), None)

    def fresh(source, action):
        return count(t, f"GetPlaybackMediaSources {dashed(movie)} mediaSourceId={dashed(source['Id'])} action=\"?{action}\"? ")

    def shared(source):
        return count(t, f"GetPlaybackMediaSources {dashed(movie)} mediaSourceId=\"?{source['Id']}\"?: prepared")

    preprobes = lambda: count(t, f"GetPlaybackMediaSources {dashed(movie)} .* preProbe=True")
    play_session = uuid.uuid4().hex

    def playlist(name, source):
        query = (f"MediaSourceId={source['Id']}&PlaySessionId={play_session}&DeviceId={DEVICE}&VideoCodec=h264"
                 f"&AudioCodec=aac&SegmentContainer=ts&TranscodingMaxAudioChannels=2&api_key={t.api.token}")
        st, _, body = t.api.request(f"/Videos/{movie}/{name}.m3u8?{query}")
        return st, body.decode(errors="replace")

    def playback_info():
        """Whether Gelato logged the PlaybackInfo with its action."""
        before = settled(lambda: fresh(first, "GetPostedPlaybackInfo"))
        t.api.post(f"/Items/{movie}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user, "MediaSourceId": movie})
        return settled(lambda: fresh(first, "GetPostedPlaybackInfo")) == before + 1

    try:
        if not playback_info():
            try:
                config = json.loads(original) if original else {}
            except ValueError:
                t.skip(f"{LOGGING} is not JSON")
            config.setdefault("Serilog", {}).setdefault("MinimumLevel", {}).setdefault("Override", {})["Gelato"] = "Debug"
            t.log("Gelato does not log at Debug: setting it in logging.json and restarting")
            write_file(t, LOGGING, json.dumps(config, indent=4))
            changed = True
            restart(t)
            t.require(playback_info(), "Gelato's Debug lines with the request's action did not show up (build?)")

        # The player loads the item again as it starts, which schedules a pre-probe of the source.
        pre = settled(preprobes)
        t.api.item(movie)

        state = lambda s: (fresh(s, "GetMasterHlsVideoPlaylist"), fresh(s, "GetVariantHlsVideoPlaylist"), shared(s))
        counts = settled(lambda: state(first))
        st, body = playlist("master", first)
        t.equal(st, 200, "the master playlist answers")
        st, body = playlist("main", first)
        shared_until = time.time() + SHARED_FOR
        t.check(st == 200 and "#EXTINF" in body, f"the variant playlist answers with segments ({st})")
        after = settled(lambda: state(first))
        t.equal(after[0] - counts[0], 1, "the master playlist prepares the source")
        t.equal(after[1] - counts[1], 0, "the variant playlist does not prepare it again")
        t.check(after[2] - counts[2] >= 1, "it takes the master's answer")

        time.sleep(3)  # the pre-probe waits 1 second
        t.equal(settled(preprobes) - pre, 0, "the player reloading the item does not pre-probe the source it plays")

        if other:
            o_before = settled(lambda: state(other))
            asked = time.time()
            st_m, _ = playlist("master", other)
            took = time.time() - asked
            st_v, _ = playlist("main", other)
            t.equal((st_m, st_v), (200, 200), "another version's playlists answer")
            o_after = settled(lambda: state(other))
            prepared = (o_after[0] - o_before[0], o_after[1] - o_before[1])
            if took < SHARED_FOR - 2:
                t.equal(prepared, (1, 0), "another version is prepared on its own, once")
            else:
                # The answer is shared for 10 seconds from when its preparation starts. A stream that is
                # slow to open (26 s and 40 s per playlist seen, 11 s on a first probe) has used them up
                # before the variant playlist is asked, which then prepares again.
                t.log(f"not judged, the master playlist took {took:.0f} s, longer than its answer is shared: prepared {prepared}")

        # The slow first probe fills the 10 seconds the first source's answer is shared for.
        slow_first_probe(t, movie, playlist, state)
        time.sleep(max(0, shared_until + 1 - time.time()))
        m = settled(lambda: fresh(first, "GetMasterHlsVideoPlaylist"))
        st, _ = playlist("master", first)
        t.equal((st, settled(lambda: fresh(first, "GetMasterHlsVideoPlaylist")) - m), (200, 1),
                f"after {SHARED_FOR} seconds the source is prepared again")
    finally:
        t.api.call("DELETE", f"/Videos/ActiveEncodings?deviceId={DEVICE}&playSessionId={play_session}")
        if changed:
            if original:
                write_file(t, LOGGING, original)
            else:
                t.sh(f"rm -f {LOGGING}")
            restart(t)
            t.equal(t.sh(f"cat {LOGGING} 2>/dev/null").strip(), original, "logging.json is restored")
