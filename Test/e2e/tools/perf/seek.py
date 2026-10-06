"""Seeking in a direct-played stream: a Range request deep into the file through Jellyfin, and the same range straight
at the stream's own URL from inside the container (never printed). python tools/perf/seek.py <item id> [n]"""
import statistics, subprocess, sys, time, uuid
import perflib as p, play, dbq

item, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5
a = p.api(); u = a.user
it = a.call("GET", f"/Users/{u}/Items/{item}")[1]
src = it["MediaSources"][0]
row = src.get("ETag") or src["Id"]
size = src.get("Size") or 0
url = dbq.q("select Path from BaseItems where lower(replace(Id,'-',''))=?", (row,))
url = url[0][0] if url else None
body = {"UserId": u, "StartTimeTicks": 0, "IsPlayback": True, "AutoOpenLiveStream": True, "MediaSourceId": src["Id"], "MaxStreamingBitrate": 140000000, "DeviceProfile": play.DIRECT}
pi = a.call("POST", f"/Items/{item}/PlaybackInfo?UserId={u}&MediaSourceId={src['Id']}", body)[1]
ms_ = pi["MediaSources"][0]
size = ms_.get("Size") or size
path = f"/Videos/{item}/stream.{ms_.get('Container') or 'mkv'}?Static=true&mediaSourceId={ms_['Id']}&deviceId=perf-seek&api_key={a.token}&Tag={ms_.get('ETag') or ''}"
print(f"{it['Name'][:30]!r} size {round(size/1e9,2)} GB, {ms_.get('Container')}, row url {'known' if url else 'not in the db copy'}")

def upstream(offset):
    if not url:
        return None
    r = subprocess.run(["docker", "exec", p.CONTAINER, "curl", "-s", "-L", "-o", "/dev/null", "-r", f"{offset}-{offset + 65535}",
                        "-w", "%{time_starttransfer} %{time_total} %{http_code} %{num_redirects}", url], capture_output=True, text=True)
    try:
        ttfb, total, code, redirects = r.stdout.split()
        return round(float(ttfb) * 1000), round(float(total) * 1000), code, redirects
    except Exception:
        return None

for label, frac in (("start", 0.0), ("25%", 0.25), ("60%", 0.6), ("90%", 0.9)):
    off = int(size * frac)
    via, direct = [], []
    for i in range(n):
        st, ttfb, done, data = play.first_bytes(path, headers={"Range": f"bytes={off}-"})
        via.append(ttfb)
        up = upstream(off)
        if up:
            direct.append(up[0])
        time.sleep(0.5)
    print(f"  {label:6} via Jellyfin: status {st}, ttfb median {round(statistics.median(via))} ms (min {round(min(via))}, max {round(max(via))})"
          + (f" | straight at the stream URL: ttfb median {round(statistics.median(direct))} ms (status {up[2]}, {up[3]} redirects)" if direct else ""))
