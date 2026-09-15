"""Run 'Download missing subtitles' twice and check the lookups find linked rows.

After each run: how often the provider used the row's stored file name, how often it matched
against "n" (the stream URL's last segment), subtitle files written in the container, numbered
copies (.en.0.vtt) that would mean the already-saved check failed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import os
import subprocess
import sys
import time

from jfapi.db import query
from jfapi.api import call, session

CONTAINER = os.environ.get("JF_CONTAINER", "")
LOG = sys.argv[1]  # the attached task output file
token, user = session()


def sh(cmd):
    return subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", cmd], capture_output=True, text=True).stdout


def log_lines():
    with open(LOG, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def run_task():
    st, tasks = call("GET", "/ScheduledTasks", token=token)
    task = next(t for t in tasks if t["Key"] == "DownloadSubtitles")
    st, _ = call("POST", f"/ScheduledTasks/Running/{task['Id']}", token=token)
    t0 = time.time()
    while time.time() - t0 < 1500:
        time.sleep(5)
        st, t = call("GET", f"/ScheduledTasks/{task['Id']}", token=token)
        if t["State"] == "Idle":
            r = t.get("LastExecutionResult", {})
            return f"{r.get('Status')} in {int(time.time() - t0)}s {(r.get('ErrorMessage') or '')[:120]}"
    return "still running after 25 min"


def summarize(start_line, label):
    lines = log_lines()[start_line:]
    used = sum("Using GelatoData filename" in l for l in lines)
    matched = [l.split("release name: ", 1)[1] for l in lines if "Matching subtitles against release name: " in l]
    n = sum(m.strip() == "n" for m in matched)
    errs = [l for l in lines if "[ERR]" in l or "[WRN]" in l]
    saved = sum("Subtitle saved" in l or "Saved subtitle" in l or "Downloaded subtitle" in l for l in lines)
    print(f"{label}: matches {len(matched)} | against 'n' {n} | stored filename used {used} | save log lines {saved} | ERR/WRN {len(errs)}")
    for e in errs[:3]:
        print("   ", e.split("| ", 1)[-1][:160])


def files(label):
    out = sh("find /media/metadata /config/data -type f \\( -name '*.vtt' -o -name '*.srt' \\) -newer /config/config/system.xml 2>/dev/null | wc -l; "
             "find /media/metadata /config/data -type f -name '*.[a-z][a-z].[0-9].*' 2>/dev/null | wc -l; "
             "find / -maxdepth 1 -type f \\( -name '*.vtt' -o -name '*.srt' \\) 2>/dev/null | wc -l")
    total, numbered, root = out.split()
    cols, r = query("select count(*), count(distinct ItemId) from MediaStreamInfos where StreamType='Subtitle' and IsExternal=1")
    print(f"{label}: subtitle files (new) {total} | numbered copies {numbered} | files in / {root} | db external subtitle streams {r[0][0]} on {r[0][1]} items")


sh("touch /config/config/system.xml")
files("before")
start = len(log_lines())
print("run 1:", run_task())
summarize(start, "run 1")
files("after run 1")
start = len(log_lines())
print("run 2:", run_task())
summarize(start, "run 2")
files("after run 2")
