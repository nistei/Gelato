"""Scheduled tasks, timed: python tools/perf/tasks.py <key>[:timeout] ...   Prints seconds, status and Gelato's summary lines."""
import datetime, sys, time, re
import perflib as p, trace as tr
a = p.api()
for arg in sys.argv[1:]:
    key, _, to = arg.partition(":")
    t0 = p.now_utc(); t = time.perf_counter()
    status, msg = a.run_task(key, timeout=int(to or 600))
    secs = time.perf_counter() - t
    time.sleep(1.5)
    rows = tr.logfile_tail(t0 - datetime.timedelta(milliseconds=5))
    addon = sum(1 for r in rows if "GetJsonAsync: requesting" in r[3])
    notes = [p.REDACT.sub("<url>", r[3].splitlines()[0])[:170] for r in rows if r[1] in ("INF", "WRN", "ERR") and re.search(r"Gelato|ScheduledTasks|TaskManager|completed|Scan|Validat", r[3].splitlines()[0])]
    print(f"{key:28} {secs:7.1f} s  {status} {msg}  addon requests {addon}, log lines {len(rows)}", flush=True)
    for n in notes[:3] + notes[-5:]:
        print("      ", n)
