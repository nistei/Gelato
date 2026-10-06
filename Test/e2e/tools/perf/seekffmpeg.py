"""ffmpeg opening a stream's link at a position (what an HLS seek starts), per analyzeduration/probesize: seconds until
6 s of stream-copied video are out. Runs in the container; never prints the URL. python tools/perf/seekffmpeg.py <item id> [hh:mm:ss]"""
import subprocess, sys, time, statistics
import perflib as p, dbq
item = sys.argv[1]; pos = sys.argv[2] if len(sys.argv) > 2 else "01:06:36"
a = p.api()
src = a.call("GET", f"/Users/{a.user}/Items/{item}")[1]["MediaSources"][0]
url = dbq.q("select Path from BaseItems where lower(replace(Id,'-',''))=?", (src.get("ETag") or src["Id"],))[0][0]
def run(analyze, probe, ss):
    cmd = ["docker", "exec", p.CONTAINER, "/usr/lib/jellyfin-ffmpeg/ffmpeg", "-v", "error", "-analyzeduration", analyze, "-probesize", probe]
    if ss: cmd += ["-ss", ss]
    cmd += ["-i", url, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-t", "6", "-f", "null", "-"]
    t0 = time.perf_counter(); r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return round((time.perf_counter() - t0) * 1000), r.returncode
run("5M", "40M", None)  # first touch
for ss in (None, pos):
    line = f"{'from the start' if not ss else 'seek to ' + ss:18}"
    for analyze, probe in (("5M", "40M"), ("5M", "5M"), ("1M", "1M"), ("200M", "1G")):
        ts = [run(analyze, probe, ss) for _ in range(3)]
        line += f" | {analyze}/{probe}: {round(statistics.median(t for t, _ in ts))} ms (rc {ts[-1][1]})"
    print(line, flush=True)
