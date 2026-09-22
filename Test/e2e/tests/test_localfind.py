DESCRIPTION = "A file-backed movie the addon does not answer for is still findable in a search that names item types"
DESTRUCTIVE = True  # adds a library with one generated video file and removes it again

import urllib.parse

from jfapi.native import add_library, remove_libraries, write_videos

GLOBAL_TYPES = "Movie,Series,Episode,Playlist,MusicAlbum,Audio,TvChannel,PhotoAlbum,Photo,AudioBook,Book,BoxSet"
LIBRARY_TYPES = "Movie,Series,Episode"

LIBRARY, PATH = "jfapi-localfind", "/tmp/jfapi-localfind"
LIBRARIES = [(LIBRARY, "movies", PATH)]
TITLE = "Zappelfrosch Hausvideo (2019)"  # a title no addon answers for
TERM = "Zappelfrosch"


def search(t, term, types=None):
    path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}"
            f"&Recursive=true&Limit=200&Fields=Path")
    if types:
        path += f"&IncludeItemTypes={types}"
    st, d = t.api.call("GET", path)
    return st, (d.get("Items", []) if st == 200 and isinstance(d, dict) else [])


def run(t):
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{PATH}/{TITLE}/{TITLE}.mkv"])
    try:
        lib, items = add_library(t, LIBRARY, "movies", PATH, "Movie", 1)
        movie = items[0]
        t.log(f"local movie {movie[:8]}")

        for label, types in [("the global search", GLOBAL_TYPES),
                             ("a library search", LIBRARY_TYPES),
                             ("the movie-and-series search", "Movie,Series"),
                             ("a search that names no type", None)]:
            st, found = search(t, TERM, types)
            t.equal(st, 200, f"{label} answers")
            t.log(f"{label}: {[(i['Id'][:8], i.get('Type'), i['Name']) for i in found]}")
            t.check(any(i["Id"] == movie for i in found),
                    f"{label} finds the local file the addon has nothing to say about")
    finally:
        remove_libraries(t, LIBRARIES)
        t.sh(f"rm -rf {PATH}")
