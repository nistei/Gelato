DESCRIPTION = "With embedded titles on, probing a stream row keeps the movie's name instead of the container's title tag; every row has Name locked"
DESTRUCTIVE = True  # turns embedded titles on for the movie library for the run

import shlex

from jfapi.probe import movie_library, probe, row_paths, unprobed

NAME = 5  # MetadataField.Name
FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"


def name_locked(t, movie):
    """{row id: Name locked} for the movie's stream rows."""
    return dict(t.db.query(
        "select lower(replace(b.Id,'-','')), exists (select 1 from BaseItemMetadataFields f where f.ItemId=b.Id and f.Id=?) "
        "from BaseItems b where lower(replace(b.PrimaryVersionId,'-',''))=? and b.Tags like '%gelato-stream%'", (NAME, movie)))


def db_name(t, item):
    return t.db.one("select Name from BaseItems where lower(replace(Id,'-',''))=?", (item,))[0]


def title_tag(t, path):
    """The container's title tag, read with Jellyfin's ffprobe inside the container (the path never
    leaves it or the test's memory)."""
    cmd = f"timeout 90 {FFPROBE} -v error -show_entries format_tags=title -of default=nw=1:nk=1 {shlex.quote(path)} 2>/dev/null"
    return t.sh(cmd).strip()


def run(t):
    library = movie_library(t)
    if library is None:
        t.skip("no Gelato movie library")
    original = library["LibraryOptions"]
    t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": {**original, "EnableEmbeddedTitles": True}})
    try:
        movies = (t.movie(), t.movie2())
        for movie in movies:
            t.api.sources(movie)  # syncs the rows
            locked = name_locked(t, movie)
            t.check(locked, f"{movie[:8]} has stream rows")
            t.equal([r[:8] for r, v in locked.items() if not v], [], f"every stream row of {movie[:8]} has Name locked")

        tried = 0
        for movie, row in unprobed(t, movies):
            if tried >= 6:
                break
            tried += 1
            name = db_name(t, movie)
            title = title_tag(t, row_paths(t, [row]).get(row) or "")
            if not title or title == name:
                t.log(f"row {row[:8]}: no title tag differing from the name, next row")
                continue
            t.log(f"row {row[:8]} of {name!r}: container title {title[:60]!r}")
            if not probe(t, movie, row):
                t.log("the stream did not probe (dead link?), next row")
                continue
            t.equal(db_name(t, row), name, "the probed row keeps the movie's name in the database")
            t.equal(t.api.item(row).get("Name"), name, "the row's page shows the movie's name")
            t.equal(t.api.item(movie).get("Name"), name, "the movie keeps its name")
            return
        t.log(f"none of {tried} unprobed row(s) had a title tag that differs from the name: the probe itself was not exercised")
    finally:
        t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": original})
        restored = movie_library(t)["LibraryOptions"]
        t.equal(restored.get("EnableEmbeddedTitles"), original.get("EnableEmbeddedTitles"), "library embedded titles option restored")
