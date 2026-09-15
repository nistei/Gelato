"""A playlist entry that names a stream row (made before the playlist fix) must move to the movie
when Gelato deletes the row.

    python t_playlist_reroute.py <movieId> --prepare   # on the old build: playlist with a row
    python t_playlist_reroute.py <movieId> --check     # after purge/re-sync on the new build
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import sys

from jfapi.db import query
from jfapi.api import call, session

token, user = session()
movie = sys.argv[1]
NAME = "versions-reroute"


def playlist():
    st, pls = call("GET", f"/Items?userId={user}&IncludeItemTypes=Playlist&Recursive=true", token=token)
    return next((p["Id"] for p in pls.get("Items", []) if p["Name"] == NAME), None)


def links(pl):
    cols, rows = query(
        "select lower(replace(l.ChildId,'-','')), case when c.Id is null then 'MISSING' when c.Tags like '%gelato-stream%' "
        "then 'row' else 'item' end from LinkedChildren l left join BaseItems c on c.Id=l.ChildId "
        "where lower(replace(l.ParentId,'-',''))=?", (pl,))
    return [(r[0][:8], r[1]) for r in rows]


if "--prepare" in sys.argv:
    if (pl := playlist()) is not None:
        call("DELETE", f"/Items/{pl}", token=token)
    st, d = call("GET", f"/Items/{movie}?userId={user}", token=token)
    srcs = [s["Id"] for s in d.get("MediaSources") or []]
    row = next(s for s in srcs if s != movie)
    st, d = call("POST", "/Playlists", {"Name": NAME, "Ids": [row], "UserId": user, "MediaType": "Video"}, token=token)
    print("playlist", d["Id"][:8], "with row", row[:8], "| links", links(d["Id"]))
else:
    pl = playlist()
    print("playlist", pl[:8], "| links", links(pl), "| movie is", movie[:8])
    st, d = call("GET", f"/Playlists/{pl}/Items?userId={user}", token=token)
    print("api items:", [(i["Id"][:8], i["Name"][:25]) for i in d.get("Items", [])])
