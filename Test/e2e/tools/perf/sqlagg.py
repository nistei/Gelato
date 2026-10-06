"""Group the SQL commands of a log slice by shape: count, total ms. Used by other scripts."""
import re, collections
def shape(txt):
    body = " ".join(txt.split("\n")[1:])
    body = re.sub(r'\?"', "", body)
    m = re.search(r"FROM (\w+)", body)
    sel = re.sub(r"\s+", " ", body)
    # keep the tables and the where clause's columns
    tables = re.findall(r"(?:FROM|JOIN) (\w+)", sel)
    where = re.findall(r"WHERE (.{0,110})", sel)
    return (sel[:6], ",".join(dict.fromkeys(tables)), where[-1] if where else "")
def agg(rows, top=25):
    c, ms = collections.Counter(), collections.Counter()
    for ts, lvl, th, txt in rows:
        if "Executed DbCommand" in txt:
            s = shape(txt)
            c[s] += 1
            ms[s] += int(re.search(r'\("?(\d+)"?ms\)', txt).group(1))
    for s, n in c.most_common(top):
        print(f"  {n:4}x {ms[s]:4}ms  {s[0]} {s[1][:60]:60} | {s[2][:110]}")
