"""SQL commands in the instance's log at or over a threshold, grouped by shape; each shape's text goes to
.cache/perf/sql/<hash>.sql for EXPLAIN QUERY PLAN on the copy (dbq.py). Needs EF's command log: see README.

    python tools/perf/slowsql.py [ms] [minutes]
"""
import re, sys, collections, datetime, hashlib, os
import perflib as p, trace as tr
ms_min = int(sys.argv[1]) if len(sys.argv) > 1 else 5
minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 60
rows = tr.logfile_tail(p.now_utc() - datetime.timedelta(minutes=minutes))
groups = collections.defaultdict(list)
for ts, lvl, th, txt in rows:
    if "Executed DbCommand" not in txt:
        continue
    ms = int(re.search(r'\("?(\d+)"?ms\)', txt).group(1))
    if ms < ms_min:
        continue
    body = "\n".join(txt.split("\n")[1:])
    norm = re.sub(r"@\w+", "@p", re.sub(r"\s+", " ", body))
    norm = re.sub(r"(@p, )+@p", "@p..", norm)
    groups[hashlib.sha1(norm.encode()).hexdigest()[:8]].append((ms, body, txt.split("\n")[0]))
OUT = os.path.join(p.WORK, "sql")
os.makedirs(OUT, exist_ok=True)
for h, g in sorted(groups.items(), key=lambda kv: -sum(m for m, _, _ in kv[1])):
    mss = sorted(m for m, _, _ in g)
    body = g[0][1]
    open(os.path.join(OUT, f"{h}.sql"), "w", encoding="utf-8").write(g[-1][2] + "\n" + body)
    flat = re.sub(r'\?"', "", re.sub(r"\s+", " ", body))
    tables = ",".join(dict.fromkeys(re.findall(r"(?:FROM|JOIN) (\w+)", flat)))
    w = flat.split(" WHERE ", 1)[1] if " WHERE " in flat else ""
    print(f"{h} {len(g):4}x total {sum(mss):5}ms median {mss[len(mss)//2]:4} max {mss[-1]:4} | {tables[:50]} | {p.REDACT.sub('<url>', w)[:210]}")
