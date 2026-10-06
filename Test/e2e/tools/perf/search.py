"""Search as Jellyfin Web 12.2 and Infuse ask for it, median of n per scenario.

    python tools/perf/search.py [n] [label filter]

Run proxy.py first: a term the proxy has not recorded goes to the live addon once (the "first" column).
"""
import sys
import urllib.parse

import perflib as p

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
ONLY = sys.argv[2] if len(sys.argv) > 2 else None
q = urllib.parse.quote

WEB_TYPES = "".join(f"&includeItemTypes={t}" for t in
                    "Movie Series Episode Playlist MusicAlbum Audio TvChannel PhotoAlbum Photo AudioBook Book BoxSet".split())
WEB_FIELDS = "&fields=PrimaryImageAspectRatio&fields=CanDelete&fields=MediaSourceCount"


def web_main(t, extra=""):
    return (f"/Items?userId={{user}}&isMissing=false&limit=800&recursive=true&searchTerm={q(t)}{WEB_FIELDS}{WEB_TYPES}"
            f"&imageTypeLimit=1&enableTotalRecordCount=false{extra}")


def web_aux(t):
    return [
        f"/Artists?limit=100&searchTerm={q(t)}{WEB_FIELDS}&imageTypeLimit=1&userId={{user}}&enableTotalRecordCount=false",
        f"/Persons?limit=100&searchTerm={q(t)}{WEB_FIELDS}&imageTypeLimit=1&excludePersonTypes=Artist&excludePersonTypes=AlbumArtist&userId={{user}}",
        f"/Studios?limit=100&searchTerm={q(t)}{WEB_FIELDS}&imageTypeLimit=1&userId={{user}}&enableTotalRecordCount=false",
        f"/Items?userId={{user}}&limit=100&recursive=true&searchTerm={q(t)}{WEB_FIELDS}&excludeItemTypes=Movie&excludeItemTypes=Episode&excludeItemTypes=TvChannel&mediaTypes=Video&imageTypeLimit=1&enableTotalRecordCount=false",
        f"/Items?userId={{user}}&limit=100&recursive=true&searchTerm={q(t)}{WEB_FIELDS}&includeItemTypes=LiveTvProgram&imageTypeLimit=1&enableTotalRecordCount=false",
    ]


def infuse(t, start=0, limit=50, extra=""):
    return (f"/Users/{{user}}/Items?searchTerm={q(t)}&Recursive=true&Fields=MediaSources,RecursiveItemCount,ChildCount"
            f"&StartIndex={start}&Limit={limit}{extra}")


def run(label, path):
    if ONLY and ONLY not in label:
        return
    out, r = p.med(lambda: p.get(path), N, label, quiet=True)
    items = r.get("Items", [])
    print(f"{label:46} first {out['first']:5}  median {out['median']:5}  min {out['min']:5}  max {out['max']:5}  "
          f"items {len(items):3} total {r.get('TotalRecordCount')}")


if __name__ == "__main__":
    MOVIES, SERIES = p.views().get("movies"), p.views().get("tvshows")
    for term in ("star", "harry potter", "spider-man", "top gear", "marvel", "love"):
        run(f"web main   '{term}'", web_main(term))
        run(f"infuse     '{term}'", infuse(term))
    for i, path in enumerate(web_aux("star")):
        run(f"web aux {i}  'star' {path.split('?')[0]}", path)
    run("web main   'star' parentId=Filme", web_main("star", f"&parentId={MOVIES}"))
    run("infuse     'star' parentId=Filme", infuse("star", extra=f"&ParentId={MOVIES}"))
    run("infuse     'star' parentId=Serien", infuse("star", extra=f"&ParentId={SERIES}"))
    run("infuse     'star' page 2 (50..100)", infuse("star", start=50))
    run("infuse     'love' page 2 (50..100)", infuse("love", start=50))
    run("infuse     'star' Movie only", infuse("star", extra="&IncludeItemTypes=Movie"))
    run("infuse     'star' Series only", infuse("star", extra="&IncludeItemTypes=Series"))
    run("web suggestions", "/Items?userId={user}&limit=20&recursive=true&includeItemTypes=Movie&includeItemTypes=Series&includeItemTypes=MusicArtist"
        "&sortBy=IsFavoriteOrLiked&sortBy=Random&imageTypeLimit=0&enableTotalRecordCount=false&enableImages=false")
    run("local: 'star' (no addon) web", web_main("local:star"))
