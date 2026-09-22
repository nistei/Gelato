DESCRIPTION = "A search that names item types keeps the library's own copy of a title out of the answer, so a movie or series the library already has is listed once, not twice"

import urllib.parse

# What the web client asks for in a global search, and what it asks for inside a library: both
# name the types. Jellyfin drops excludeItemTypes as soon as includeItemTypes is set, so a
# pass-through that only excludes movies and series still answers with the library's own copy
# of a title the addon just answered for.
GLOBAL_TYPES = "Movie,Series,Episode,Playlist,MusicAlbum,Audio,TvChannel,PhotoAlbum,Photo,AudioBook,Book,BoxSet"
LIBRARY_TYPES = "Movie,Series,Episode"

CANDIDATE_SQL = (
    "select lower(replace(b.Id,'-','')), b.Name, p.ProviderValue from BaseItems b "
    "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
    "where b.Type like ? and (b.Tags is null or b.Tags not like '%gelato-stream%') "
    "and b.PrimaryVersionId is null and p.ProviderValue like 'tt%' and length(b.Name) > 3 "
    "order by random() limit 8"
)


def search(t, term, types=None):
    path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}"
            f"&Recursive=true&Limit=200&Fields=Path&Fields=ProviderIds")
    if types:
        path += f"&IncludeItemTypes={types}"
    st, d = t.api.call("GET", path)
    return st, (d.get("Items", []) if st == 200 and isinstance(d, dict) else [])


def key(item):
    """What the two copies of one title share: the stremio id. The library's own item and the
    addon's result for it carry it in different places — the item as a provider id, the search
    result in its gelato:// path — so both are read."""
    stremio = (item.get("ProviderIds") or {}).get("Stremio")
    if stremio:
        return stremio
    path = item.get("Path") or ""
    if path.startswith("gelato://stub/"):
        return path[len("gelato://stub/"):]
    return path or item["Id"]


def copies(items, wanted):
    return [i for i in items if key(i) == wanted]


def pick(t, sql_type):
    """A title the library owns and the addon answers for, as (item id, name, stremio id): the
    two preconditions of the duplicate. None when no candidate has both."""
    for item_id, name, stremio in t.db.query(CANDIDATE_SQL, (sql_type,)):
        st, local = search(t, "local:" + name, LIBRARY_TYPES)
        if st != 200 or not any(i["Id"] == item_id for i in local):
            t.log(f"skipped {name!r}: the library search does not find the item itself")
            continue
        # The movie-and-series search is the one shape the addon answers alone, so a hit there
        # is the addon having the title.
        st, addon = search(t, name, "Movie,Series")
        if st == 200 and any(key(i) == stremio and i["Id"] != item_id for i in addon):
            t.log(f"candidate: {name!r} ({item_id[:8]}, {stremio})")
            return item_id, name, stremio
        t.log(f"skipped {name!r} ({stremio}): the addon does not answer for it")
    return None


def check_once(t, label, name, item_id, stremio, items):
    same = copies(items, stremio)
    t.log(f"{label}: {len(items)} items, {len(same)} for {stremio}: "
          f"{[(i['Id'][:8], i.get('Type')) for i in same]}")
    t.equal(len(same), 1, f"{label} lists {name!r} once")
    t.check(not any(i["Id"] == item_id for i in same),
            f"{label} answers with the addon's result, not the library's own item")


def run(t):
    picked = pick(t, "%Movies.Movie")
    if picked is None:
        t.skip("no library movie that the addon also answers for")
    item_id, name, stremio = picked

    for label, types in [("the global search", GLOBAL_TYPES),
                         ("a library search", LIBRARY_TYPES),
                         ("the movie-and-series search", "Movie,Series"),
                         ("a search that names no type", None)]:
        st, items = search(t, name, types)
        t.equal(st, 200, f"{label} for {name!r} answers")
        check_once(t, label, name, item_id, stremio, items)

    # The same for a series: a show the library has must not come back next to the addon's.
    picked = pick(t, "%TV.Series")
    if picked is None:
        t.log("no library series that the addon also answers for, the series half is left out")
        return
    series_id, series_name, series_stremio = picked
    st, items = search(t, series_name, GLOBAL_TYPES)
    t.equal(st, 200, f"the global search for the series {series_name!r} answers")
    check_once(t, "the global search", series_name, series_id, series_stremio, items)
