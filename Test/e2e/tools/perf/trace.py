"""One request, then its slice of the log file: step lines with ms offsets, SQL count and time.
    python tools/perf/trace.py GET "<path>" [--sql] [--body json]"""
import re, sys, time, json, datetime
import perflib as p

def logfile_tail(since):
    out = p.sh("cat /config/log/log_*.log")
    rows = []
    for line in out.splitlines():
        m = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \+00:00\] \[(\w+)\] \[(\d+)\] (.*)", line)
        if m:
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=datetime.timezone.utc)
            if ts >= since:
                rows.append([ts, m.group(2), m.group(3), m.group(4)])
        elif rows:
            rows[-1][3] += "\n" + line
    return rows

def trace(method, path, body=None, show_sql=False, wait=1.5, quiet=False, maxlines=200):
    p.api()
    time.sleep(0.3)
    t0 = p.now_utc()
    ms, r = p.timed(lambda: p.api().call(method, path, body))
    time.sleep(wait)
    rows = logfile_tail(t0 - datetime.timedelta(milliseconds=5))
    sql = [(ts, txt) for ts, lvl, th, txt in rows if "Executed DbCommand" in txt]
    sql_ms = sum(int(re.search(r'Executed DbCommand \("?(\d+)"?ms\)', t).group(1)) for _, t in sql)
    if not quiet:
        print(f"{method} {path[:110]} -> {r[0]} in {round(ms)} ms; {len(sql)} SQL commands, {sql_ms} ms in SQL (EF's own timing)")
        n = 0
        for ts, lvl, th, txt in rows:
            off = (ts - t0).total_seconds() * 1000
            if "Executed DbCommand" in txt:
                if show_sql:
                    d = re.search(r'\("?(\d+)"?ms\)', txt).group(1)
                    body_ = p.REDACT.sub("<url>", " ".join(txt.split("\n")[1:]))[:show_sql if isinstance(show_sql, int) and show_sql > 1 else 230]
                    print(f"  {off:7.0f} [{th:>3}] SQL {d:>3}ms {body_}")
                continue
            n += 1
            if n <= maxlines:
                print(f"  {off:7.0f} [{th:>3}] {lvl} {p.REDACT.sub('<url>', txt)[:200]}")
    return ms, r, rows, sql

if __name__ == "__main__":
    a = sys.argv[1:]
    show = "--sql" in a
    body = json.loads(a[a.index("--body") + 1]) if "--body" in a else None
    trace(a[0], a[1], body, show_sql=show)
