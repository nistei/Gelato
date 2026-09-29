DESCRIPTION = "RemuxDB: a stream it knows gets its tracks, runtime, size and chapters when synced and plays without a probe; a probed file it does not know is submitted anonymously, only with a torrent and only when contributing is on; episodes are looked up by season and episode"
DESTRUCTIVE = True  # points Gelato's addon and RemuxDB at a stub on the host for the run of the test, then restores and resyncs

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jfapi.bootstrap import GELATO
from jfapi.probe import log_count, movie_library

CLIP_SECONDS = 150  # over the 2 minutes below which playback always probes
CLIP_IN_CONTAINER = "/tmp/jfapi-remuxdb-clip.mkv"
FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"

HASH_A = "a" * 39 + "1"  # known to RemuxDB, matched by torrent
HASH_D = "d" * 39 + "4"  # unknown: probed and submitted
HASH_E = "e" * 39 + "5"  # unknown, played with contributing off
HASH_EP = "f" * 39 + "6"  # an episode's file, known
SIZE_A, SIZE_B, SIZE_C, SIZE_D, SIZE_E, SIZE_EP = (
    41_000_000_001, 9_000_000_002, 7_000_000_003, 5_000_000_004, 4_000_000_005, 2_000_000_006)


def movie_streams(base):
    """The stub's streams for the movie: A known by torrent, B known by size only, C unknown and
    without a torrent, D and E unknown with one."""
    def stream(key, filename, video_size, data=None):
        s = {"name": f"remuxdb-{key}", "description": filename, "url": f"{base}/clip/{key}.mkv",
             "behaviorHints": {"bingeGroup": f"remuxdb-{key}", "filename": filename, "videoSize": video_size}}
        if data:
            s["streamData"] = data
        return s
    return [
        stream("A", "Test.Movie.A.2160p.DV.mkv", SIZE_A,
               {"type": "debrid", "size": SIZE_A, "torrent": {"infoHash": HASH_A.upper(), "fileIdx": 0}}),
        stream("B", "Test.Movie.B.1080p.mkv", SIZE_B),
        stream("C", "Test.Movie.C.mkv", SIZE_C),
        stream("D", "Test.Movie.D.mkv", SIZE_D, {"size": SIZE_D, "torrent": {"infoHash": HASH_D}}),
        stream("E", "Test.Movie.E.mkv", SIZE_E, {"size": SIZE_E, "torrent": {"infoHash": HASH_E}}),
    ]


def version_a(seconds):
    """A 2160p Dolby Vision file with a gap in its stream indexes (a data stream RemuxDB does not
    record) and three chapters."""
    return {
        "content_hash": "a" * 64, "container": "matroska,webm", "duration": seconds, "size": SIZE_A,
        "bitrate": 38_000_000, "virtual_chapters": False,
        "chapters": [{"id": 1, "start_time": 0.0, "end_time": 60.0, "title": "Opening"},
                     {"id": 2, "start_time": 60.0, "end_time": 120.0, "title": "Middle"},
                     {"id": 3, "start_time": 120.0, "end_time": seconds, "title": "00:02:00.000"}],
        "sources": [{"kind": "torrent", "filename": "Pack/Test.Movie.A.2160p.DV.mkv",
                     "torrent_info_hash": HASH_A, "torrent_file_idx": 0}],
        "tracks": [
            {"kind": "video", "idx": 0, "codec": "hevc", "width": 3840, "height": 2160, "fps": 23.976,
             "level": 153, "profile": "Main 10", "pixel_format": "yuv420p10le", "color_primaries": "bt2020",
             "color_range": "limited", "color_space": "bt2020nc", "color_transfer": "smpte2084", "dv_profile": 8,
             "is_default": True, "is_forced": False, "is_external": False, "is_hearing_impaired": False,
             "is_anamorphic": False, "hdr10_plus_present": False},
            {"kind": "audio", "idx": 1, "codec": "eac3", "channels": 6, "channel_layout": "5.1(side)",
             "sample_rate": 48000, "language": "ger", "is_default": True, "is_forced": False, "is_external": False,
             "is_hearing_impaired": False, "is_anamorphic": False, "hdr10_plus_present": False},
            {"kind": "audio", "idx": 3, "codec": "truehd", "channels": 8, "channel_layout": "7.1",
             "sample_rate": 48000, "language": "eng", "is_default": False, "is_forced": False, "is_external": False,
             "is_hearing_impaired": False, "is_anamorphic": False, "hdr10_plus_present": False},
            {"kind": "subtitle", "idx": 4, "codec": "hdmv_pgs_subtitle", "language": "eng", "is_default": False,
             "is_forced": True, "is_external": False, "is_hearing_impaired": False, "is_anamorphic": False,
             "hdr10_plus_present": False},
            {"kind": "subtitle", "idx": 5, "codec": "subrip", "language": "ger", "is_default": False,
             "is_forced": False, "is_external": False, "is_hearing_impaired": True, "is_anamorphic": False,
             "hdr10_plus_present": False},
        ],
    }


def simple_version(seconds, size, sources, width=1920, height=1080):
    return {
        "content_hash": f"{size:064d}", "container": "matroska,webm", "duration": seconds, "size": size,
        "bitrate": 8_000_000, "virtual_chapters": False, "sources": sources,
        "tracks": [
            {"kind": "video", "idx": 0, "codec": "h264", "width": width, "height": height, "fps": 24.0,
             "is_default": True, "is_forced": False, "is_external": False, "is_hearing_impaired": False,
             "is_anamorphic": False, "hdr10_plus_present": False},
            {"kind": "audio", "idx": 1, "codec": "aac", "channels": 2, "sample_rate": 48000, "language": "eng",
             "is_default": True, "is_forced": False, "is_external": False, "is_hearing_impaired": False,
             "is_anamorphic": False, "hdr10_plus_present": False},
        ],
    }


class Stub:
    """The addon and RemuxDB in one server. The addon part forwards to the real addon except the
    streams of the test's movie and episode; the RemuxDB part answers their versions and records
    every request. /clip/ serves a short real video, so playback has something to probe."""

    def __init__(self, upstream, clip):
        self.upstream = upstream.rstrip("/")
        if self.upstream.endswith("/manifest.json"):
            self.upstream = self.upstream[: -len("/manifest.json")]
        self.clip = clip
        self.streams = {}  # "/stream/<type>/<id>.json" -> streams
        self.versions = {}  # (imdb, season, episode) -> versions
        self.lookups, self.submissions = [], []  # (path, headers) / (body, headers)
        self.stream_agents = []  # User-Agent of each stream request the stub answered
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body=b"", ctype="application/json", extra=None):
                self.send_response(status)
                if ctype:
                    self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path.startswith("/clip/"):
                    return self.serve_clip()
                if url.path.startswith("/remuxdb/api/media/") and url.path.endswith("/versions"):
                    # Like RemuxDB: an episode's season and episode are part of the id
                    # (tt0903747:1:2), query parameters are ignored and a bare id is the title.
                    media_id = urllib.parse.unquote(url.path.split("/")[4])
                    parts = media_id.split(":")
                    key = ((parts[0], int(parts[1]), int(parts[2])) if len(parts) == 3
                           else (media_id, None, None))
                    with outer.lock:
                        outer.lookups.append((urllib.parse.unquote(self.path), dict(self.headers)))
                    versions = outer.versions.get(key)
                    if versions is None:
                        return self.reply(404, b"not found", "text/plain")
                    return self.reply(200, json.dumps(versions).encode())
                if url.path.startswith("/addon/"):
                    path = url.path[len("/addon"):]
                    if path in outer.streams:
                        with outer.lock:
                            outer.stream_agents.append(self.headers.get("User-Agent") or "")
                        return self.reply(200, json.dumps({"streams": outer.streams[path]}).encode())
                    return self.forward(path + (f"?{url.query}" if url.query else ""))
                return self.reply(404, b"", None)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/remuxdb/api/mediainfo":
                    with outer.lock:
                        outer.submissions.append((body.decode("utf-8", "replace"), dict(self.headers)))
                    return self.reply(201, json.dumps({"id": "00000000-0000-0000-0000-000000000000"}).encode())
                return self.reply(404, b"", None)

            def forward(self, path):
                req = urllib.request.Request(outer.upstream + path, headers={"User-Agent": self.headers.get("User-Agent") or "jfapi"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        return self.reply(r.status, r.read(), r.headers.get("Content-Type"))
                except urllib.error.HTTPError as e:
                    return self.reply(e.code, e.read(), e.headers.get("Content-Type"))
                except OSError:
                    return self.reply(502, b"", None)

            def serve_clip(self):
                data = outer.clip
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
                if not m:
                    return self.reply(200, data, "video/x-matroska", {"Accept-Ranges": "bytes"})
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                if start >= len(data):
                    return self.reply(416, b"", None, {"Content-Range": f"bytes */{len(data)}"})
                return self.reply(206, data[start:end + 1], "video/x-matroska",
                                  {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(data)}"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://host.docker.internal:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def make_clip(t):
    """A 150 s 320x180 h264/aac mkv, made by the container's ffmpeg and copied out."""
    t.sh(f"{FFMPEG} -v error -y -f lavfi -i testsrc2=duration={CLIP_SECONDS}:size=320x180:rate=24 "
         f"-f lavfi -i sine=frequency=440:duration={CLIP_SECONDS} -c:v libx264 -preset ultrafast -crf 40 "
         f"-c:a aac -b:a 32k {CLIP_IN_CONTAINER}")
    local = os.path.join(tempfile.gettempdir(), "jfapi-remuxdb-clip.mkv")
    subprocess.run(["docker", "cp", f"{t.db.container}:{CLIP_IN_CONTAINER}", local], capture_output=True)
    t.sh(f"rm -f {CLIP_IN_CONTAINER}")
    if not os.path.exists(local):
        return None
    with open(local, "rb") as f:
        data = f.read()
    os.remove(local)
    return data


def by_name(item):
    """{stream name: media source} of an item DTO."""
    return {(s.get("Name") or "").split("\n")[0]: s for s in item.get("MediaSources") or []}


def streams_of(source, kind):
    return [m for m in source.get("MediaStreams") or [] if m.get("Type") == kind]


def playback_info(t, item, source):
    return t.api.post(f"/Items/{item}/PlaybackInfo?userId={t.api.user}",
                      {"UserId": t.api.user, "MediaSourceId": source["Id"]})


def dashed(hex_id):
    h = hex_id.replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def probes_of(t, source):
    """Lines logging a probe of the source's row (the row is the source's ETag)."""
    return log_count(t, f"Probing stream for {dashed(source['ETag'])}")


def wait_for(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(1)
    return cond()


def run(t):
    cfg_path = "/Plugins/" + GELATO + "/Configuration"
    original = t.api.get(cfg_path)
    if not original.get("Url"):
        t.skip("Gelato has no addon URL")
    t.require("RemuxDbEnabled" in original, "this Gelato build has no RemuxDB support")

    library = movie_library(t)
    t.require(library, "no Gelato movie library")
    # The probe leaves out subtitles a library does not allow, and such a list is never submitted.
    t.require(library["LibraryOptions"].get("AllowEmbeddedSubtitles", "AllowAll") == "AllowAll",
              "the movie library does not allow all embedded subtitles")
    movie = t.movie()
    imdb = t.fixtures.stremio_id(movie) or ""
    t.require(re.fullmatch(r"tt\d+", imdb), f"the fixture movie has no IMDb Stremio id ({imdb})")
    seconds = (t.api.item(movie).get("RunTimeTicks") or 7200 * 10**7) / 10**7

    episode = next((e for e in t.episodes() if re.fullmatch(r"tt\d+:1:\d+", t.fixtures.stremio_id(e["Id"]) or "")), None)

    clip = make_clip(t)
    t.require(clip, "ffmpeg in the container could not make a test clip")
    stub = Stub(original["Url"], clip)
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {stub.base}/clip/x.mkv && echo ok"):
            t.skip(f"the container cannot reach the host on port {stub.port}")

        stub.streams[f"/stream/movie/{imdb}.json"] = movie_streams(stub.base)
        stub.versions[(imdb, None, None)] = [
            version_a(seconds),
            simple_version(seconds, SIZE_B, [{"kind": "torrent", "filename": "Other.Name.mkv",
                                              "torrent_info_hash": "b" * 40, "torrent_file_idx": 2}]),
            # the same torrent as D, another file of it: D must not take its tracks
            simple_version(seconds, SIZE_D * 3, [{"kind": "torrent", "filename": "Test.Movie.D.Extras.mkv",
                                                  "torrent_info_hash": HASH_D, "torrent_file_idx": 1}], 1280, 720),
        ]
        ep_id = t.fixtures.stremio_id(episode["Id"]) if episode else None
        if episode:
            series_imdb, season, number = ep_id.split(":")
            ep_seconds = (episode.get("RunTimeTicks") or 3000 * 10**7) / 10**7
            stub.streams[f"/stream/series/{ep_id}.json"] = [
                {"name": "remuxdb-EP", "url": f"{stub.base}/clip/EP.mkv",
                 "behaviorHints": {"bingeGroup": "remuxdb-EP", "filename": "Test.Show.S01E01.mkv", "videoSize": SIZE_EP},
                 "streamData": {"size": SIZE_EP, "torrent": {"infoHash": HASH_EP, "fileIdx": 3}}}]
            stub.versions[(series_imdb, int(season), int(number))] = [
                simple_version(ep_seconds, SIZE_EP, [{"kind": "torrent", "filename": "Show.S01/Test.Show.S01E01.mkv",
                                                      "torrent_info_hash": HASH_EP, "torrent_file_idx": 3}], 3840, 2160)]

        # Saving the configuration clears Gelato's stream cache, so the next visit syncs again.
        t.api.post(cfg_path, {**original, "Url": f"{stub.base}/addon/manifest.json",
                              "RemuxDbUrl": f"{stub.base}/remuxdb", "RemuxDbEnabled": True, "RemuxDbContribute": True})

        # ---- sync: a lookup, and the tracks of the known files
        item = t.api.item(movie)
        sources = by_name(item)
        t.equal(sorted(k for k in sources if k.startswith("remuxdb-")), [f"remuxdb-{k}" for k in "ABCDE"],
                "the movie lists the stub's five streams")
        # AIOStreams only sends its stream data (the torrent's hash) to a User-Agent it takes for
        # another AIOStreams, unless its operator turns it on for everyone.
        t.check(stub.stream_agents and all(a.startswith("AIOStreams/") for a in stub.stream_agents),
                f"streams are asked for as AIOStreams ({sorted(set(stub.stream_agents))})")
        movie_lookups = [(p, h) for p, h in stub.lookups if f"/api/media/{imdb}/versions" in p]
        t.equal(len(movie_lookups), 1, "one RemuxDB lookup for the movie")
        if movie_lookups:
            path, headers = movie_lookups[0]
            headers = {k.lower(): v for k, v in headers.items()}
            t.check("?" not in path, f"a movie is looked up without season or episode ({path})")
            t.check(re.fullmatch(r"[0-9a-f]{32}", headers.get("x-client-id", "")),
                    f"the lookup sends a random client id ({headers.get('x-client-id')})")
            t.check("authorization" not in headers, "the lookup sends no token")
            t.check(headers.get("user-agent", "").startswith("Gelato/"), f"User-Agent {headers.get('user-agent')}")

        a, b, c = sources.get("remuxdb-A", {}), sources.get("remuxdb-B", {}), sources.get("remuxdb-C", {})
        video = streams_of(a, "Video")
        t.equal([(v.get("Codec"), v.get("Width"), v.get("Height")) for v in video], [("hevc", 3840, 2160)],
                "A (matched by torrent): its video track")
        t.equal(video[0].get("VideoRange") if video else None, "HDR", "A: HDR from the Dolby Vision profile")
        t.equal([(s.get("Codec"), s.get("Channels"), s.get("Language"), s.get("Index")) for s in streams_of(a, "Audio")],
                [("eac3", 6, "deu", 1), ("truehd", 8, "eng", 3)],
                "A: audio tracks (ger stored as deu, as Jellyfin does), the second one at ffmpeg's index 3 behind the unrecorded stream")
        t.equal([(s.get("Codec"), s.get("IsForced"), s.get("Index")) for s in streams_of(a, "Subtitle") if not s.get("IsExternal")],
                [("PGSSUB", True, 4), ("subrip", False, 5)], "A: embedded subtitles with Jellyfin's codec names")
        t.equal(a.get("RunTimeTicks"), round(seconds * 10**7), "A: runtime of the file")
        t.equal(a.get("Container"), "mkv", "A: container")
        t.equal(a.get("Size"), SIZE_A, "A: size")
        t.equal(a.get("Bitrate"), 38_000_000, "A: bitrate")
        row_a = a.get("ETag") or ""
        chapters = t.api.item(row_a, "Chapters").get("Chapters") if row_a else None
        t.equal([ch.get("Name") for ch in chapters or []], ["Opening", "Middle", "Chapter 3"], "A: chapters, a time as title renamed")

        t.equal([(v.get("Codec"), v.get("Height")) for v in streams_of(b, "Video")], [("h264", 1080)],
                "B (no torrent, matched by exact size): its video track")
        t.equal(streams_of(c, "Video"), [], "C (unknown): no tracks before playback")
        t.equal(streams_of(sources.get("remuxdb-D", {}), "Video"), [],
                "D: another file of the same torrent is not taken for it")

        # ---- playback: A plays without a probe, C and D are probed, only D is submitted
        before = probes_of(t, a)
        pi = playback_info(t, movie, a)
        played = next((s for s in pi.get("MediaSources", []) if s.get("Id") == a.get("Id")), {})
        t.equal(probes_of(t, a) - before, 0, "A plays without a probe")
        t.equal([v.get("Codec") for v in streams_of(played, "Video")], ["hevc"], "A's PlaybackInfo has RemuxDB's tracks")

        for key in "CD":
            s = sources.get(f"remuxdb-{key}", {})
            before = probes_of(t, s)
            pi = playback_info(t, movie, s)
            played = next((x for x in pi.get("MediaSources", []) if x.get("Id") == s.get("Id")), {})
            t.equal(probes_of(t, s) - before, 1, f"{key} is probed at playback")
            t.equal([(v.get("Codec"), v.get("Width")) for v in streams_of(played, "Video")], [("h264", 320)],
                    f"{key}: the probe found the clip's video")

        t.check(wait_for(lambda: len(stub.submissions) >= 1, 30), "a submission arrives")
        time.sleep(3)
        t.equal(len(stub.submissions), 1, "one submission: D, not C (no torrent)")
        if stub.submissions:
            raw, headers = stub.submissions[0]
            headers = {k.lower(): v for k, v in headers.items()}
            sub = json.loads(raw)
            t.equal((sub.get("kind"), sub.get("filename"), sub.get("torrent_info_hash"), sub.get("size"), sub.get("container")),
                    ("movie", "Test.Movie.D.mkv", HASH_D, SIZE_D, "mkv"), "D's submission: kind, file, torrent, size, container")
            t.check(abs((sub.get("duration") or 0) - CLIP_SECONDS) < 2, f"D's submission: the probed duration ({sub.get('duration')})")
            t.equal((sub.get("external_ids") or {}).get("imdb_id"), imdb, "D's submission: the IMDb id")
            tracks = sub.get("tracks") or []
            t.equal([(x.get("kind"), x.get("idx"), x.get("codec")) for x in tracks],
                    [("video", 0, "h264"), ("audio", 1, "aac")], "D's submission: the probed tracks with ffmpeg's indexes")
            v = next((x for x in tracks if x.get("kind") == "video"), {})
            t.check(v.get("width") == 320 and v.get("height") == 180 and abs((v.get("fps") or 0) - 24) < 0.01,
                    f"D's submission: video 320x180 at 24 fps ({v.get('width')}x{v.get('height')} {v.get('fps')})")
            aud = next((x for x in tracks if x.get("kind") == "audio"), {})
            t.check(aud.get("channels") and aud.get("sample_rate"), f"D's submission: audio channels and sample rate ({aud})")
            t.equal(sub.get("client_id"), headers.get("x-client-id"), "the submission's client id is the header's")
            t.check("authorization" not in headers, "the submission sends no token")
            flat = lambda x: (x or "").lower().replace("-", "")
            server_id = t.api.get("/System/Info/Public").get("Id")
            leaks = [w for w in ("http", "host.docker.internal", server_id, t.api.user, "remuxdb-D", "remuxdb-d")
                     if w and flat(w) in flat(raw)]
            t.equal(leaks, [], "the submission carries no URL, server id, user id or stream name")

        # ---- contributing off: E is probed, nothing is sent
        t.api.post(cfg_path, {**t.api.get(cfg_path), "RemuxDbContribute": False})
        e = by_name(t.api.item(movie)).get("remuxdb-E", {})
        before = probes_of(t, e)
        playback_info(t, movie, e)
        t.equal(probes_of(t, e) - before, 1, "E is probed at playback")
        time.sleep(5)
        t.equal(len(stub.submissions), 1, "with contributing off E is not submitted")

        # ---- an episode: looked up by the series' IMDb id with season and episode
        if episode:
            ep = by_name(t.api.item(episode["Id"])).get("remuxdb-EP", {})
            ep_lookups = [p for p, _ in stub.lookups if f"/api/media/{series_imdb}" in p]
            t.equal(ep_lookups, [f"/remuxdb/api/media/{series_imdb}:{season}:{number}/versions"],
                    "the episode is looked up by series, season and episode in the id")
            t.equal([(v.get("Codec"), v.get("Width")) for v in streams_of(ep, "Video")], [("h264", 3840)],
                    "the episode's stream gets its tracks")
            t.equal(ep.get("Size"), SIZE_EP, "the episode's stream: size")
        else:
            t.log("no season 1 episode with an IMDb Stremio id in the fixture series: episode part left out")

        # ---- lookups off: a sync asks RemuxDB nothing
        t.api.post(cfg_path, {**t.api.get(cfg_path), "RemuxDbEnabled": False})
        asked = len(stub.lookups)
        t.api.item(movie)
        t.equal(len(stub.lookups) - asked, 0, "with lookups off a sync does not ask RemuxDB")
    finally:
        t.api.post(cfg_path, {**t.api.get(cfg_path), **{k: original.get(k) for k in
                   ("Url", "RemuxDbUrl", "RemuxDbEnabled", "RemuxDbContribute")}})
        # The next visit syncs the real streams again, which drops the stub's rows.
        t.api.call("GET", f"/Items/{movie}?userId={t.api.user}", timeout=90)
        if episode:
            t.api.call("GET", f"/Items/{episode['Id']}?userId={t.api.user}", timeout=90)
        stub.close()
        left = t.db.one("select count(*) from BaseItems where Path like ?", (f"%host.docker.internal:{stub.port}%",))[0]
        t.equal(left, 0, "no stub row is left after the real streams are synced again")
