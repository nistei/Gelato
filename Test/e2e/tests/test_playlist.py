DESCRIPTION = "Add to playlist from a version page holds the movie, and the entry survives purge, re-sync and a row delete"

NAME = "jfapi-playlist"


def run(t):
    movie = t.movie()
    row = t.row(movie)
    for p in t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=Playlist&Recursive=true").get("Items", []):
        if p["Name"] == NAME:
            t.api.delete(f"/Items/{p['Id']}")

    pl = t.api.post("/Playlists", {"Name": NAME, "Ids": [row], "UserId": t.api.user, "MediaType": "Video"})["Id"]
    links = lambda: t.db.playlist_links(pl)
    items = lambda: [(i["Id"].lower(), "img" if i.get("ImageTags") else "noimg") for i in t.api.get(f"/Playlists/{pl}/Items?userId={t.api.user}").get("Items", [])]
    try:
        t.log("created with the row:", links())
        t.equal(links(), [(movie, "item")], "the playlist holds the movie, not the row")
        t.api.post(f"/Playlists/{pl}/Items?ids={movie}&userId={t.api.user}")
        t.api.post(f"/Playlists/{pl}/Items?ids={row}&userId={t.api.user}")
        t.equal(links(), [(movie, "item")], "adding the movie and the row again adds nothing")
        t.equal(items(), [(movie, "img")], "the API lists one entry with images")

        pi = t.api.post(f"/Items/{row}/PlaybackInfo?userId={t.api.user}", {"UserId": t.api.user})
        t.check((pi.get("MediaSources") or [{}])[0].get("Id") == row, "PlaybackInfo of the row lists the row first")

        t.equal(t.api.run_task("PurgeGelatoStreamsTask")[0], "Completed", "purge streams")
        t.equal(links(), [(movie, "item")], "entry survives the purge")
        srcs = t.api.sources(movie)
        t.check(row in srcs, "the row is back after the re-sync")
        t.equal(links(), [(movie, "item")], "entry survives the re-sync")

        other = next((s for s in srcs if s not in (movie, row)), None)
        if other is None:
            t.log("no third stream to delete")
            return
        t.api.delete(f"/Items/{other}")
        t.check(other not in t.api.sources(movie), "a row deleted through the API leaves the source list")
        t.equal(links(), [(movie, "item")], "entry survives the row delete")
    finally:
        t.api.delete(f"/Items/{pl}")
