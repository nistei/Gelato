DESCRIPTION = "Opening a movie syncs its streams: sources listed, the first one under the movie's id, rows linked"

from jfapi.probe import row_paths


def run(t):
    movie = t.movie()
    d = t.api.item(movie)
    srcs = d.get("MediaSources") or []
    t.log(f"{d['Name']}: {len(srcs)} sources, MediaSourceCount {d.get('MediaSourceCount')}")
    t.check(len(srcs) >= 2, "at least two sources")
    t.check(srcs and srcs[0]["Id"] == movie, "the first source carries the movie's id")
    t.check(srcs and srcs[0]["Type"] == "Default" and all(s["Type"] == "Grouping" for s in srcs[1:]),
            "first source Default, the others Grouping")
    # The DTO stubs the addon URL on purpose (issue 168), so the streams are checked where they
    # are kept. Paths carry the debrid API key: counted, never logged.
    paths = row_paths(t, [s["Id"] for s in srcs])
    urls = sum(1 for p in paths.values() if (p or "").startswith("http"))
    t.check(urls >= len(srcs) - 1, f"the listed sources are stream rows with a URL: {urls} of {len(srcs)}")
    t.check(not any((s.get("Path") or "").startswith("http") for s in srcs), "no source hands out its URL")
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
