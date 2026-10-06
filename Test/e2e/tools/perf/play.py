"""Playback start as the web client does it, timed per request, with Gelato's steps counted from the log.

    python tools/perf/play.py <item id> [direct|hls] [n] [--source <id>] [--quiet]
"""
import datetime
import re
import sys
import time
import urllib.request
import uuid

import perflib as p
import trace as tr

DIRECT = {
    "MaxStreamingBitrate": 140000000, "MaxStaticBitrate": 140000000, "MusicStreamingTranscodingBitrate": 384000,
    "DirectPlayProfiles": [{"Container": "mkv,webm,mp4,m4v,mov,avi,ts", "Type": "Video",
                            "VideoCodec": "h264,hevc,vp8,vp9,av1,mpeg4,mpeg2video,vc1", "AudioCodec": "aac,mp3,ac3,eac3,dts,truehd,flac,opus,vorbis,pcm_s16le,pcm_s24le"}],
    "TranscodingProfiles": [{"Container": "ts", "Type": "Video", "AudioCodec": "aac", "VideoCodec": "h264", "Context": "Streaming",
                             "Protocol": "hls", "MaxAudioChannels": "2", "MinSegments": "1", "BreakOnNonKeyFrames": True}],
    "ContainerProfiles": [], "CodecProfiles": [], "SubtitleProfiles": [{"Format": "vtt", "Method": "External"}, {"Format": "ass", "Method": "External"},
                                                                        {"Format": "srt", "Method": "External"}, {"Format": "subrip", "Method": "External"}],
}
HLS = {**DIRECT, "DirectPlayProfiles": []}


def first_bytes(path, n=65536, headers=None, timeout=120):
    a = p.api()
    req = urllib.request.Request(a.base + path, headers={**a.headers(), **(headers or {})})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ttfb = (time.perf_counter() - t0) * 1000
            data = r.read(n)
            return r.status, ttfb, (time.perf_counter() - t0) * 1000, data
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000, (time.perf_counter() - t0) * 1000, e.read()[:300]


def start(item, mode="direct", source=None, device="perf-play"):
    """One playback start. Returns {step: ms} and the source that played."""
    a = p.api()
    u = a.user
    steps = {}
    t_all = time.perf_counter()

    def step(name, fn):
        ms, r = p.timed(fn)
        steps[name] = round(ms)
        return r

    step("intros", lambda: a.call("GET", f"/Users/{u}/Items/{item}/Intros"))
    it = step("item", lambda: a.call("GET", f"/Users/{u}/Items/{item}"))[1]
    source = source or it["MediaSources"][0]["Id"]
    body = {"UserId": u, "StartTimeTicks": 0, "IsPlayback": True, "AutoOpenLiveStream": True, "MediaSourceId": source,
            "MaxStreamingBitrate": 140000000, "AlwaysBurnInSubtitleWhenTranscoding": False,
            "DeviceProfile": DIRECT if mode == "direct" else HLS}
    st, pi = step("playbackinfo", lambda: a.call(
        "POST", f"/Items/{item}/PlaybackInfo?UserId={u}&StartTimeTicks=0&IsPlayback=true&AutoOpenLiveStream=true&MediaSourceId={source}&MaxStreamingBitrate=140000000", body))
    if st != 200:
        return steps, {"error": f"PlaybackInfo {st} {str(pi)[:200]}"}
    ms = pi["MediaSources"][0]
    session = pi.get("PlaySessionId")
    info = {"source": ms["Id"], "container": ms.get("Container"), "direct": ms.get("SupportsDirectPlay"), "transcode": bool(ms.get("TranscodingUrl")),
            "video": next((s.get("Codec") for s in ms.get("MediaStreams") or [] if s.get("Type") == "Video"), None)}
    if mode == "direct":
        path = (f"/Videos/{item}/stream.{ms.get('Container') or 'mkv'}?Static=true&mediaSourceId={ms['Id']}&deviceId={device}"
                f"&api_key={a.token}&Tag={ms.get('ETag') or ''}")
        st, ttfb, done, data = first_bytes(path, headers={"Range": "bytes=0-"})
        steps["stream ttfb"] = round(ttfb)
        steps["stream 64k"] = round(done)
        info["stream"] = st
    else:
        url = ms.get("TranscodingUrl")
        if not url:
            return steps, {**info, "error": "no TranscodingUrl"}
        st, ttfb, done, data = first_bytes(url, n=1 << 20)
        steps["master"] = round(done)
        m = re.search(r"^(main\.m3u8\?.*)$", data.decode(errors="replace"), re.M)
        base = url.split("master.m3u8")[0]
        if st != 200 or not m:
            return steps, {**info, "error": f"master {st}"}
        st, ttfb, done, data = first_bytes(base + m.group(1), n=1 << 20)
        steps["main"] = round(done)
        seg = next((l for l in data.decode(errors="replace").splitlines() if l and not l.startswith("#")), None)
        if st != 200 or not seg:
            return steps, {**info, "error": f"main {st}"}
        st, ttfb, done, data = first_bytes(base + seg, n=65536, timeout=180)
        steps["segment ttfb"] = round(ttfb)
        info["segment"] = st
    steps["to first byte"] = round((time.perf_counter() - t_all) * 1000)
    # What the player does once it plays: report, reload the item, ask for segments.
    step("playing", lambda: a.call("POST", "/Sessions/Playing", {"ItemId": item, "MediaSourceId": ms["Id"], "PositionTicks": 0,
                                                              "PlaySessionId": session, "CanSeek": True, "IsPaused": False,
                                                              "PlayMethod": "DirectPlay" if mode == "direct" else "Transcode"}))
    step("item reload", lambda: a.call("GET", f"/Users/{u}/Items/{item}"))
    step("segments", lambda: a.call("GET", f"/MediaSegments/{item}?includeSegmentTypes=Outro&includeSegmentTypes=Intro"))
    info["session"] = session
    return steps, info


def stop(item, info, mode="direct"):
    a = p.api()
    if info.get("session"):
        a.call("POST", "/Sessions/Playing/Stopped", {"ItemId": item, "MediaSourceId": info["source"], "PositionTicks": 0, "PlaySessionId": info["session"]})
        if mode != "direct":
            a.call("DELETE", f"/Videos/ActiveEncodings?deviceId=jfapi-cli-{a.name}&playSessionId={info['session']}")


COUNTS = [("GetStaticMediaSources", r"GetStaticMediaSources [0-9a-f-]{36}$"),
          ("GetPlaybackMediaSources fresh", r"GetPlaybackMediaSources .* action="),
          ("GetPlaybackMediaSources shared", r"GetPlaybackMediaSources .*: prepared"),
          ("probe", r"Probing stream"),
          ("stream sync", r"refreshing streams"),
          ("addon request", r"GetJsonAsync: requesting"),
          ("segment lookup", r"IntroDb|segment"),
          ("SQL", r"Executed DbCommand")]


def counted(rows):
    out = {}
    for name, rx in COUNTS:
        n = sum(1 for _, _, _, txt in rows if re.search(rx, txt.split("\n")[0]))
        if n:
            out[name] = n
    sql_ms = sum(int(re.search(r'\("?(\d+)"?ms\)', t).group(1)) for _, _, _, t in rows if "Executed DbCommand" in t)
    if sql_ms:
        out["SQL ms"] = sql_ms
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    item, mode, n = args[0], (args[1] if len(args) > 1 else "direct"), int(args[2]) if len(args) > 2 else 1
    source = sys.argv[sys.argv.index("--source") + 1] if "--source" in sys.argv else None
    import statistics
    runs = []
    for i in range(n):
        t0 = p.now_utc()
        steps, info = start(item, mode, source)
        time.sleep(1.5)
        rows = tr.logfile_tail(t0 - datetime.timedelta(milliseconds=5))
        stop(item, info, mode)
        runs.append(steps)
        print(i, steps, {k: v for k, v in info.items() if k != "session"}, counted(rows), flush=True)
        if "--lines" in sys.argv:
            for ts, lvl, th, txt in rows:
                if "Executed DbCommand" not in txt and "Authentication" not in txt:
                    print(f"   {(ts - t0).total_seconds() * 1000:7.0f} [{th:>3}] {p.REDACT.sub('<url>', txt.splitlines()[0])[:210]}")
        time.sleep(11 if n > 1 else 0)  # past the 10 s a prepared source is shared for
    if n > 1:
        keys = [k for k in runs[0] if all(k in r for r in runs)]
        print("median", {k: round(statistics.median(r[k] for r in runs)) for k in keys})
