DESCRIPTION = "starting a playback prepares its source once for PlaybackInfo and once for the HLS playlists: the variant playlist reuses the master's answer for 10 seconds, another version is prepared on its own, and the player reloading the item does not pre-probe the source it plays"
DESTRUCTIVE = True  # without Gelato's Debug lines, sets Gelato to Debug in logging.json and restarts, both ways

# Counts Gelato's Debug lines. Instances from dev/jf.py log Gelato at Debug; elsewhere the test sets it in
# logging.json and restarts the server, since Jellyfin applies a changed level only on a restart.

import base64
import json
import subprocess
import time
import uuid

from jfapi.bootstrap import wait_ready
from tests.test_remuxdb import dashed

LOGGING = "/config/config/logging.json"
SHARED_FOR = 10  # seconds a prepared source answers the streaming requests that follow it
DEVICE = "jfapi-playbackonce"
FLUSH = 3  # seconds the log file may lag behind


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


def run(t):
    original = t.sh(f"cat {LOGGING} 2>/dev/null").strip()
    changed = False

    movie = t.movie()
    sources = t.api.item(movie).get("MediaSources") or []
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
        time.sleep(3)  # the pre-probe of the fixture's page visit is over by then
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
        t.check(st == 200 and "#EXTINF" in body, f"the variant playlist answers with segments ({st})")
        after = settled(lambda: state(first))
        t.equal(after[0] - counts[0], 1, "the master playlist prepares the source")
        t.equal(after[1] - counts[1], 0, "the variant playlist does not prepare it again")
        t.check(after[2] - counts[2] >= 1, "it takes the master's answer")

        time.sleep(3)  # the pre-probe waits 1 second
        t.equal(settled(preprobes) - pre, 0, "the player reloading the item does not pre-probe the source it plays")

        if other:
            o_before = settled(lambda: state(other))
            st_m, _ = playlist("master", other)
            st_v, _ = playlist("main", other)
            t.equal((st_m, st_v), (200, 200), "another version's playlists answer")
            o_after = settled(lambda: state(other))
            t.equal((o_after[0] - o_before[0], o_after[1] - o_before[1]), (1, 0),
                    "another version is prepared on its own, once")

        time.sleep(SHARED_FOR + 1)
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
