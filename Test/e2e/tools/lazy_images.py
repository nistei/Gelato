"""Lazy images end to end through a stream row: insert a new movie from search (its images are saved
as zero-byte placeholders with .url sidecars), sync its streams, request the poster and backdrop via a
row, and check the placeholders got filled."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import os
import subprocess
import sys
import urllib.error
import urllib.request

from jfapi.db import query
from jfapi.api import BASE, call, session

CONTAINER = os.environ.get("JF_CONTAINER", "")
token, user = session()
term = sys.argv[1] if len(sys.argv) > 1 else "Heretic"


def sh(cmd):
    return subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", cmd], capture_output=True, text=True).stdout


def files(item):
    out = []
    for d in (f"/media/metadata/library/{item[:2]}/{item}", f"/config/data/gelato/images/{item}"):
        listing = sh(f"ls -la {d}/ 2>/dev/null | grep -vE '^total|^d' | awk '{{print $5, $9}}'").replace("\n", " | ")
        if listing:
            out.append(d.split("/")[2] + ": " + listing)
    return " || ".join(out)


if len(sys.argv) > 2:
    guid = sys.argv[2]
else:
    st, d = call("GET", f"/Items?userId={user}&searchTerm={term}&IncludeItemTypes=Movie&Recursive=true&Limit=10&Fields=Path", token=token)
    cols, known = query("select lower(replace(Id,'-','')) from BaseItems")
    known = {k[0] for k in known}
    cand = [i for i in d.get("Items", []) if i["Id"].lower() not in known]
    print("search results:", len(d.get("Items", [])), "| not yet in the library:", [(c["Name"], c.get("ProductionYear")) for c in cand[:3]])
    if not cand:
        raise SystemExit("pick another search term")
    guid = cand[0]["Id"]

st, item = call("GET", f"/Items/{guid}?userId={user}", token=token)
movie = item["Id"].lower()
print("inserted:", st, item.get("Name"), movie, "| sources", len(item.get("MediaSources") or []), "| ImageTags", sorted((item.get("ImageTags") or {}).keys()), "| backdrops", len(item.get("BackdropImageTags") or []))
print("image files after insert:", files(movie))

cols, r = query("select lower(replace(Id,'-','')) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=? order by Id limit 1", (movie,))
if not r:
    st, item = call("GET", f"/Items/{movie}?userId={user}", token=token)
    cols, r = query("select lower(replace(Id,'-','')) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=? order by Id limit 1", (movie,))
row = r[0][0]
st, rd = call("GET", f"/Items/{row}?userId={user}", token=token)
print("row DTO:", st, "ImageTags", sorted((rd.get("ImageTags") or {}).keys()), "| backdrops", len(rd.get("BackdropImageTags") or []), "| people", len(rd.get("People") or []))

for path in (f"/Items/{row}/Images/Primary?maxWidth=200", f"/Items/{row}/Images/Backdrop/0?maxWidth=300", f"/Items/{row}/Images/Logo?maxWidth=200"):
    label = path.split("?")[0].replace(row, "<row>")
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as resp:
            print(f"GET {label}: {resp.status} {len(resp.read())} bytes {resp.headers.get('Content-Type')}")
    except urllib.error.HTTPError as e:
        print(f"GET {label}: {e.code} {e.read()[:100]}")

print("image files after requests:", files(movie))
print("row has an image folder of its own:", "yes" if files(row) else "no")
