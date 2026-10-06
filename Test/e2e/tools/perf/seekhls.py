"""Seeking in an HLS playback (video copied, audio transcoded: what a browser that plays the codec gets):
first segment, then a segment far ahead, which makes Jellyfin start ffmpeg again at that position.
    python tools/perf/seekhls.py <item id> [n]"""
import datetime, re, statistics, sys, time
import perflib as p, play, trace as tr

item, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3
a = p.api(); u = a.user
PROFILE = {**play.HLS, "TranscodingProfiles": [{"Container": "mp4", "Type": "Video", "AudioCodec": "aac", "VideoCodec": "hevc,h264", "Context": "Streaming",
                                                "Protocol": "hls", "MaxAudioChannels": "2", "MinSegments": "1", "BreakOnNonKeyFrames": True}]}
out = []
for i in range(n):
    it = a.call("GET", f"/Users/{u}/Items/{item}")[1]
    src = it["MediaSources"][0]["Id"]
    pi = a.call("POST", f"/Items/{item}/PlaybackInfo?UserId={u}&MediaSourceId={src}",
                {"UserId": u, "StartTimeTicks": 0, "IsPlayback": True, "AutoOpenLiveStream": True, "MediaSourceId": src, "MaxStreamingBitrate": 140000000, "DeviceProfile": PROFILE})[1]
    ms_ = pi["MediaSources"][0]
    url = ms_["TranscodingUrl"]
    base = url.split("master.m3u8")[0]
    st, _, _, data = play.first_bytes(url, n=1 << 20)
    main = re.search(r"^(main\.m3u8\?.*)$", data.decode(errors="replace"), re.M).group(1)
    st, _, _, data = play.first_bytes(base + main, n=1 << 22)
    segs = [l for l in data.decode(errors="replace").splitlines() if l and not l.startswith("#")]
    t0 = p.now_utc()
    st0, first, _, _ = play.first_bytes(base + segs[0], timeout=180)
    time.sleep(2)
    target = segs[int(len(segs) * 0.6)]
    t1 = p.now_utc()
    st1, seek, _, _ = play.first_bytes(base + target, timeout=180)
    time.sleep(1.5)
    rows = tr.logfile_tail(t1 - datetime.timedelta(milliseconds=5))
    marks = [(round((ts - t1).total_seconds() * 1000), txt.splitlines()[0][:60]) for ts, lvl, th, txt in rows
             if re.search(r"ffmpeg|Stopping|Killing|GetPlaybackMediaSources|Deleting|StartFfMpeg|Starting", txt.splitlines()[0]) and "DbCommand" not in txt]
    out.append((first, seek))
    reason = [k for k in ("TranscodeReasons=",) if k in url]
    print(f"run {i}: video {'copy' if 'hevc' in url.lower() else '?'} {len(segs)} segments; first segment {st0} ttfb {round(first)} ms; seek to 60% {st1} ttfb {round(seek)} ms; steps {marks[:8]}", flush=True)
    a.call("DELETE", f"/Videos/ActiveEncodings?deviceId=jfapi-cli-{a.name}&playSessionId={pi.get('PlaySessionId')}")
    time.sleep(11)
print("median first", round(statistics.median(x[0] for x in out)), "ms, seek", round(statistics.median(x[1] for x in out)), "ms")
