"""How long ffprobe takes on stream rows' links with Gelato's probe settings and with smaller ones, and what
the smaller ones lose. Runs ffprobe inside the container, as Jellyfin does. Never prints a URL.

    python tools/perf/probesize.py [rows] [--state unprobed|probe|remuxdb]
"""
import json
import subprocess
import sys
import time

import dbq
import perflib as p

N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 6
STATE = sys.argv[sys.argv.index("--state") + 1] if "--state" in sys.argv else "unprobed"
SETTINGS = [("5M/40M", "5M", "40M"), ("5M/5M", "5M", "5M"), ("1M/1M", "1M", "1M")]


def rows():
    out = []
    for path, ext, owner in dbq.q("select b.Path, b.ExternalId, p.Name from BaseItems b join BaseItems p on p.Id=b.PrimaryVersionId "
                                  "where b.Tags like '%gelato-stream%' and b.Path like 'http%' order by b.PrimaryVersionId"):
        d = json.loads(ext) if ext and ext.startswith("{") else {}
        if (d.get("mediaInfo") or "unprobed") == STATE:
            out.append((path, owner, d.get("size")))
    seen, picked = set(), []
    for path, owner, size in out:  # one row per title
        if owner not in seen:
            seen.add(owner)
            picked.append((path, owner, size))
    return picked[:N]


def ffprobe(url, analyze, probesize):
    cmd = ["docker", "exec", p.CONTAINER, "/usr/lib/jellyfin-ffmpeg/ffprobe", "-analyzeduration", analyze, "-probesize", probesize,
           "-i", url, "-threads", "0", "-v", "warning", "-print_format", "json", "-show_streams", "-show_chapters", "-show_format"]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    ms = round((time.perf_counter() - t0) * 1000)
    try:
        return ms, json.loads(r.stdout)
    except Exception:
        return ms, None


KEYS = ("codec_name", "profile", "width", "height", "pix_fmt", "color_transfer", "color_primaries", "channels", "channel_layout", "sample_rate",
        "r_frame_rate", "avg_frame_rate", "bit_rate", "level", "field_order")


def digest(j):
    if not j:
        return None
    out = []
    for s in j.get("streams", []):
        side = sorted(x.get("side_data_type", "") for x in s.get("side_data_list") or [])
        out.append((s.get("codec_type"), tuple(str(s.get(k)) for k in KEYS), tuple(side), (s.get("tags") or {}).get("language"),
                    tuple(sorted((s.get("disposition") or {}).items()))))
    return {"streams": out, "duration": (j.get("format") or {}).get("duration"), "chapters": len(j.get("chapters") or []),
            "format": (j.get("format") or {}).get("format_name")}


def diff(a, b):
    if a is None or b is None:
        return ["no output"]
    d = []
    if len(a["streams"]) != len(b["streams"]):
        d.append(f"streams {len(a['streams'])} vs {len(b['streams'])}")
    for i, (x, y) in enumerate(zip(a["streams"], b["streams"])):
        if x != y:
            fields = [KEYS[k] for k in range(len(KEYS)) if x[1][k] != y[1][k]]
            if x[2] != y[2]:
                fields.append(f"side_data {x[2]} vs {y[2]}")
            if x[3:] != y[3:]:
                fields.append("lang/disposition")
            d.append(f"#{i} {x[0]}: {', '.join(fields)}")
    for k in ("duration", "chapters", "format"):
        if a[k] != b[k]:
            d.append(f"{k} {a[k]} vs {b[k]}")
    return d


if __name__ == "__main__":
    for url, owner, size in rows():
        name = (owner or "")[:26].encode("ascii", "replace").decode()
        # First touch of the link (a debrid link answers slowly the first time), then each setting once, smallest first.
        first_ms, base = ffprobe(url, "5M", "40M")
        times, digests = {}, {}
        for label, a, s in SETTINGS[::-1]:
            ms, j = ffprobe(url, a, s)
            times.setdefault(label, []).append(ms)
            digests[label] = digest(j)
        ref = digests["5M/40M"]
        v = next((s for s in (ref or {}).get("streams", []) if s[0] == "video"), None)
        what = f"{v[1][0]} {v[1][2]}x{v[1][3]}" if v else "no video"
        line = f"{name:27} {what:16} size {round((size or 0) / 1e9, 1):5} GB  first touch {first_ms:6} ms |"
        for label, _, _ in SETTINGS:
            line += f" {label} {min(times[label]):6} ms"
        print(line, flush=True)
        for label, _, _ in SETTINGS[1:]:
            d = diff(ref, digests[label])
            if d:
                print(f"      {label} differs: {'; '.join(d)[:300]}", flush=True)
