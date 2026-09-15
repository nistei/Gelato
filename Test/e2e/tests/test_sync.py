DESCRIPTION = "Opening a movie syncs its streams: sources listed, the first one under the movie's id, rows linked"


def run(t):
    movie = t.movie()
    d = t.api.item(movie)
    srcs = d.get("MediaSources") or []
    t.log(f"{d['Name']}: {len(srcs)} sources, MediaSourceCount {d.get('MediaSourceCount')}")
    t.check(len(srcs) >= 2, "at least two sources")
    t.check(srcs and srcs[0]["Id"] == movie, "the first source carries the movie's id")
    t.check(srcs and srcs[0]["Type"] == "Default" and all(s["Type"] == "Grouping" for s in srcs[1:]),
            "first source Default, the others Grouping")
    t.check(all(s.get("Path", "").startswith("http") for s in srcs), "every source has a stream URL")
    t.check(d.get("MediaSourceCount") in (None, 1), "no version count badge on the movie")

    rows = t.db.stream_rows(movie)
    t.log("rows in the database:", rows)
    t.check(rows["count"] >= len(srcs), "a row per listed source")
    t.equal(rows["unowned"], 0, "rows without an owner")
    t.equal(rows["owned"], rows["count"], "rows owned by this movie")
    t.equal(rows["links"], rows["count"], "version links on the movie")
    t.equal(rows["unstamped"], 0, "rows without a refresh stamp")

    listed = t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=Movie&Recursive=true&Ids={movie}").get("Items", [])
    t.equal(len(listed), 1, "the movie appears once in a list query")
