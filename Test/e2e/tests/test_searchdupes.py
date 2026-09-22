DESCRIPTION = "A movie or series the library already has is listed once in a search, as the library's own item with its user data, in the place the addon's result for it had"

import urllib.parse

# What the web client asks for in a global search, and what it asks for inside a library: both
# name the types. Jellyfin drops excludeItemTypes as soon as includeItemTypes is set, so a
# library half that only excludes movies and series still answers with the library's own copy of
# a title the addon just answered for.
GLOBAL_TYPES = "Movie,Series,Episode,Playlist,MusicAlbum,Audio,TvChannel,PhotoAlbum,Photo,AudioBook,Book,BoxSet"
LIBRARY_TYPES = "Movie,Series,Episode"

# A title to open from the addon's search, so the collision is built by the test instead of
# waited for: the library has it afterwards, and the addon still answers for its name.
FRESH_TERMS = ["Nosferatu", "Heretic", "Anora", "Conclave", "Flow", "Longlegs", "Civil War"]

CANDIDATE_SQL = (
    "select lower(replace(b.Id,'-','')), b.Name, p.ProviderValue from BaseItems b "
    "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
    "where b.Type like ? and (b.Tags is null or b.Tags not like '%gelato-stream%') "
    "and b.PrimaryVersionId is null and p.ProviderValue like 'tt%' and length(b.Name) > 3 "
    "order by random() limit 8"
)


def search(t, term, types=None, limit=200):
    path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}"
            f"&Recursive=true&Limit={limit}&Fields=Path&Fields=ProviderIds")
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
    return [(i, n) for n, i in enumerate(items) if key(i) == wanted]


def check_once(t, label, name, item_id, stremio, items):
    """One result for the title, and it is the library's own item, not a stand-in for it."""
    same = copies(items, stremio)
    t.log(f"{label}: {len(items)} items, {len(same)} for {stremio}: "
          f"{[(i['Id'][:8], i.get('Type'), n) for i, n in same]}")
    if not t.equal(len(same), 1, f"{label} lists {name!r} once"):
        return None
    found, at = same[0]
    t.equal(found["Id"], item_id, f"{label} answers with the library's own item for {name!r}")
    t.check(found.get("UserData") is not None,
            f"{label} carries the item's user data (watched state, resume position)")
    return at


def pick_existing(t, sql_type):
    """A title the library owns, as (item id, name, stremio id), that Jellyfin's own search finds
    under its name. None when no candidate does."""
    for item_id, name, stremio in t.db.query(CANDIDATE_SQL, (sql_type,)):
        st, local = search(t, "local:" + name, LIBRARY_TYPES)
        if st == 200 and any(i["Id"] == item_id for i in local):
            t.log(f"candidate: {name!r} ({item_id[:8]}, {stremio})")
            return item_id, name, stremio
        t.log(f"skipped {name!r}: the library search does not find the item itself")
    return None


def run(t):
    picked = pick_existing(t, "%Movies.Movie")
    if picked is None:
        t.skip("no library movie the library search finds under its own name")
    item_id, name, stremio = picked

    for label, types in [("the global search", GLOBAL_TYPES),
                         ("a library search", LIBRARY_TYPES),
                         ("the movie-and-series search", "Movie,Series"),
                         ("a search that names no type", None)]:
        st, items = search(t, name, types)
        t.equal(st, 200, f"{label} for {name!r} answers")
        check_once(t, label, name, item_id, stremio, items)

    picked = pick_existing(t, "%TV.Series")
    if picked is None:
        t.log("no library series the library search finds under its own name, that half is left out")
    else:
        series_id, series_name, series_stremio = picked
        st, items = search(t, series_name, GLOBAL_TYPES)
        t.equal(st, 200, f"the global search for the series {series_name!r} answers")
        check_once(t, "the global search", series_name, series_id, series_stremio, items)

    # The collision built here, so the substitution is observed from both sides: the addon's
    # result before the library has the title, the library's item in its place afterwards.
    for term in FRESH_TERMS:
        hits = t.api.search(term, "Movie", limit=10, fields="Path,ProviderIds")
        fresh = next((h for h in hits if h.get("UserData") is None and key(h)), None)
        if fresh is None:
            continue
        before = [h["Id"] for h in hits]
        at_before = before.index(fresh["Id"])
        stremio = key(fresh)
        t.log(f"opening {fresh['Name']!r} ({stremio}), result {at_before} of {len(hits)} for {term!r}")

        opened = t.api.item(fresh["Id"])
        library_id = (opened.get("Id") or "").lower()
        if not t.check(library_id and library_id != fresh["Id"].lower(),
                       "the result opened on a library item"):
            return

        hits = t.api.search(term, "Movie", limit=10, fields="Path,ProviderIds")
        at_after = check_once(t, f"the search for {term!r} after the title was opened",
                              fresh["Name"], library_id, stremio, hits)
        if at_after is not None:
            t.equal(at_after, at_before,
                    f"the library's item took the addon result's place for {fresh['Name']!r}")
        return

    t.log("no fresh title the addon answers for, the substitution half is left out")
