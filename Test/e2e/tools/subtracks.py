"""External subtitle tracks of a movie's stream versions, as a player sees them in PlaybackInfo."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import sys
import urllib.request

from jfapi.api import BASE, call, session

token, user = session()
movie = sys.argv[1]
st, d = call("GET", f"/Items/{movie}?userId={user}", token=token)
print(d.get("Name"), "| sources", len(d.get("MediaSources") or []))
for s in (d.get("MediaSources") or [])[:12]:
    st, pi = call("POST", f"/Items/{movie}/PlaybackInfo?userId={user}", {"MediaSourceId": s["Id"]}, token=token)
    src = next((m for m in pi.get("MediaSources", []) if m["Id"] == s["Id"]), None)
    if src is None:
        print(f"  {s['Id'][:8]}: no source in PlaybackInfo ({st})")
        continue
    subs = [m for m in src.get("MediaStreams", []) if m.get("Type") == "Subtitle"]
    ext = [m for m in subs if m.get("IsExternal")]
    print(f"  {s['Id'][:8]} {s.get('Name', '')[:40]!r}: subtitle tracks {len(subs)}, external {len(ext)}"
          + "".join(f" | {m.get('Language')} {m.get('Codec')} {m.get('DeliveryUrl', '')[:60]}" for m in ext[:2]))
    for m in ext[:1]:
        url = m.get("DeliveryUrl")
        if url:
            req = urllib.request.Request(BASE + url, headers={"Authorization": f'MediaBrowser Token="{token}"'})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            print(f"      GET {url[:60]}: {r.status} {len(body)} bytes, starts with {body[:12]!r}")
