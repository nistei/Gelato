"""Stream rows that have saved subtitle files, and whether a player gets them as external tracks.

Lists the metadata folders holding .vtt/.srt files, keeps the ones that belong to a current stream
row, then asks PlaybackInfo for that row's source and prints its external subtitle streams.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import os
import subprocess
import urllib.request

from jfapi.db import query
from jfapi.api import BASE, call, session

CONTAINER = os.environ.get("JF_CONTAINER", "")
token, user = session()

out = subprocess.run(
    ["docker", "exec", CONTAINER, "sh", "-c",
     "find /media/metadata/library -type f \\( -name '*.vtt' -o -name '*.srt' \\) | awk -F/ '{print $6}' | sort | uniq -c"],
    capture_output=True, text=True).stdout
folders = {l.split()[1]: int(l.split()[0]) for l in out.splitlines() if l.strip()}
print("metadata folders with subtitle files:", len(folders))
if not folders:
    raise SystemExit("nothing saved yet: run 'Download missing subtitles' first")

cols, rows = query(
    "select lower(replace(Id,'-','')), lower(replace(PrimaryVersionId,'-','')), Name from BaseItems "
    "where Tags like '%gelato-stream%' and lower(replace(Id,'-','')) in (" + ",".join("'" + f + "'" for f in folders) + ")")
print("of which current stream rows:", len(rows), "| folders of deleted rows:", len(folders) - len(rows))

shown = 0
for row, owner, name in rows:
    if not owner or shown >= 3:
        continue
    st, pi = call("POST", f"/Items/{owner}/PlaybackInfo?userId={user}", {"MediaSourceId": row}, token=token)
    src = next((m for m in pi.get("MediaSources", []) if m["Id"].lower() == row), None)
    if src is None:
        print(f"  {name} row {row[:8]}: source not in PlaybackInfo ({st})")
        continue
    ext = [m for m in src.get("MediaStreams", []) if m.get("Type") == "Subtitle" and m.get("IsExternal")]
    print(f"  {name} row {row[:8]} ({folders[row]} file(s)): external subtitle tracks {len(ext)}: "
          + ", ".join(f"{m.get('Language')}/{m.get('Codec')} idx {m.get('Index')}" for m in ext))
    for m in ext[:1]:
        req = urllib.request.Request(BASE + m["DeliveryUrl"], headers={"Authorization": f'MediaBrowser Token="{token}"'})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
        print(f"      {m['DeliveryUrl'][:70]} -> {r.status}, {len(body)} bytes, starts {body[:10]!r}")
    shown += 1
