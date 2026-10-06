"""Page requests as Jellyfin Web 12.2 sends them (and the home-section scripts of the prod copy), one at a time,
median of n.

    python tools/perf/pages.py [n] [label filter]

The series is the one with the most episodes, the movie one with stream rows (PERF_MOVIE), the person PERF_PERSON
or the first match for "tom hanks".
"""
import os
import sys

import perflib as p

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
ONLY = sys.argv[2] if len(sys.argv) > 2 else None
MOVIES, SERIES = p.views().get("movies"), p.views().get("tvshows")
SHOW, SEASON = p.biggest_series()
MOVIE = p.movie_with_streams()
PERSON = os.environ.get("PERF_PERSON") or p.get("/Persons?limit=1&searchTerm=tom%20hanks&userId={user}")["Items"][0]["Id"]
IMG = "&imageTypeLimit=1&enableImageTypes=Primary&enableImageTypes=Backdrop&enableImageTypes=Thumb"

PAGES = [
    # home
    ("home UserViews", "/UserViews?userId={user}"),
    ("home Resume video", "/UserItems/Resume?userId={user}&limit=12&fields=PrimaryImageAspectRatio&mediaTypes=Video" + IMG + "&enableTotalRecordCount=false"),
    ("home NextUp", "/Shows/NextUp?userId={user}&limit=24&fields=PrimaryImageAspectRatio&fields=DateCreated&fields=Path&fields=MediaSourceCount" + IMG
     + "&nextUpDateCutoff=2025-10-06&enableTotalRecordCount=false&enableResumable=false&enableRewatching=false"),
    ("home Latest movies", "/Items/Latest?userId={user}&limit=16&fields=PrimaryImageAspectRatio&fields=Path" + IMG + f"&parentId={MOVIES}"),
    ("home Latest series", "/Items/Latest?userId={user}&limit=16&fields=PrimaryImageAspectRatio&fields=Path" + IMG + f"&parentId={SERIES}"),
    # home sections of the prod copy's scripts
    ("script movies Fields=People limit=500", "/Items?userId={user}&IncludeItemTypes=Movie&Recursive=true&Fields=People&Limit=500&StartIndex=0"),
    ("script movies Fields=ProviderIds (all)", "/Users/{user}/Items?IncludeItemTypes=Movie&Recursive=true&Fields=ProviderIds"),
    ("script random unplayed movies", "/Users/{user}/Items?IncludeItemTypes=Movie&Fields=PrimaryImageAspectRatio%2CDateCreated%2COverview%2CProductionYear&Limit=16"
     "&SortBy=Random&SortOrder=Ascending&userId={user}&Recursive=true&isPlayed=false&isUnaired=false&isMissing=false&minPremiereDate=2000-01-01"),
    ("script genre movies", "/Items?userId={user}&Genres=Thriller&IncludeItemTypes=Movie&Recursive=true&SortBy=Random&Fields=UserData&Limit=16"),
    ("script movie Similar limit=32", f"/Movies/{MOVIE}/Similar?limit=32"),
    ("script Upcoming", "/Shows/Upcoming?Limit=48&Fields=AirTime,SeriesName,ParentIndexNumber,IndexNumber&UserId={user}&ImageTypeLimit=1"
     f"&EnableImageTypes=Primary,Backdrop,Banner,Thumb&EnableTotalRecordCount=false&ParentIds={SERIES}"),
    # movie page
    ("movie item (10 rows)", f"/Users/{{user}}/Items/{MOVIE}"),
    ("movie Similar", f"/Items/{MOVIE}/Similar?userId={{user}}&limit=12&fields=PrimaryImageAspectRatio%2CCanDelete"),
    # series page
    ("series item", f"/Users/{{user}}/Items/{SHOW}"),
    ("series Seasons", f"/Shows/{SHOW}/Seasons?userId={{user}}&Fields=ItemCounts%2CPrimaryImageAspectRatio%2CCanDelete%2CMediaSourceCount"),
    ("series NextUp", f"/Shows/NextUp?SeriesId={SHOW}&UserId={{user}}&Fields=MediaSourceCount"),
    ("series Similar", f"/Items/{SHOW}/Similar?userId={{user}}&limit=12&fields=PrimaryImageAspectRatio%2CCanDelete"),
    # season page
    ("season item", f"/Users/{{user}}/Items/{SEASON}"),
    ("season Episodes", f"/Shows/{SHOW}/Episodes?seasonId={SEASON}&userId={{user}}&Fields=ItemCounts%2CPrimaryImageAspectRatio%2CCanDelete%2CMediaSourceCount%2COverview"),
    # library grids
    ("grid movies 100", "/Items?userId={user}&startIndex=0&limit=100&recursive=true&sortOrder=Ascending" + f"&parentId={MOVIES}"
     "&fields=MediaSourceCount&fields=PrimaryImageAspectRatio&includeItemTypes=Movie&sortBy=SortName&imageTypeLimit=1&enableImageTypes=Primary&enableImageTypes=Backdrop"),
    ("grid series 100", "/Items?userId={user}&startIndex=0&limit=100&recursive=true&sortOrder=Ascending" + f"&parentId={SERIES}"
     "&fields=MediaSourceCount&fields=PrimaryImageAspectRatio&includeItemTypes=Series&sortBy=SortName&imageTypeLimit=1&enableImageTypes=Primary&enableImageTypes=Backdrop"),
    ("grid Filters", f"/Items/Filters?userId={{user}}&parentId={MOVIES}&includeItemTypes=Movie"),
    # person page
    ("person item", f"/Users/{{user}}/Items/{PERSON}"),
    ("person movies", "/Users/{user}/Items?SortOrder=Descending%2CDescending%2CAscending&IncludeItemTypes=Movie&Recursive=true&Fields=ParentId%2CPrimaryImageAspectRatio"
     f"&Limit=10&StartIndex=0&CollapseBoxSetItems=false&SortBy=PremiereDate%2CProductionYear%2CSortName&PersonIds={PERSON}"),
]

if __name__ == "__main__":
    for label, path in PAGES:
        if ONLY and ONLY not in label:
            continue
        out, r = p.med(lambda: p.get(path), N, label, quiet=True)
        items = r.get("Items", r) if isinstance(r, dict) else r
        n = len(items) if isinstance(items, list) else "-"
        print(f"{label:40} first {out['first']:5}  median {out['median']:5}  min {out['min']:5}  max {out['max']:5}  items {n}", flush=True)
