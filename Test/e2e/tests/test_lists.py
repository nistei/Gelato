DESCRIPTION = "No stream row shows up as an item, and nothing is doubled: library, search, latest, recommendations, collections, filters, resume, next up"

import urllib.parse

COLLECTION = "jfapi-collection"


def run(t):
    movie = t.movie()
    row = t.row(movie)
    m = t.api.item(movie, "Genres,ProductionYear,SortName")
    name, year = m["Name"], m.get("ProductionYear")
    # The letter filter matches the sort name, which has no leading article ("The Gray Man" sorts as "Gray Man").
    letter = (m.get("SortName") or name)[0].upper()
    genre = (m.get("Genres") or [None])[0]
    series = t.series()
    e1 = t.episodes(series)[0]["Id"]
    t.check(len(t.api.sources(e1)) >= 2, "episode 1 has stream rows")
    rows_all = t.db.stream_row_ids()
    legacy = {r[0] for r in t.db.query("select lower(replace(Id,'-','')) from BaseItems where Tags like '%gelato-stream%' and PrimaryVersionId is null")}
    if legacy:
        t.log(f"{len(legacy)} legacy rows (no owner yet) on the instance: Next Up can show one until its title is opened (known, pre-existing)")
    u = t.api.user
    q = urllib.parse.quote

    def listing(label, path, expect_movie=None, total=None):
        d = t.api.get(path)
        items = d if isinstance(d, list) else d.get("Items", [])
        ids = [i["Id"].lower() for i in items]
        leaked = [i for i in ids if i in rows_all and not (label == "next up" and i in legacy)]
        dups = len(ids) - len(set(ids))
        t.log(f"{label}: {len(ids)} items, total {d.get('TotalRecordCount') if isinstance(d, dict) else '-'}")
        t.check(not leaked, f"{label}: no stream rows ({len(leaked)} leaked)")
        t.equal(dups, 0, f"{label}: duplicate ids")
        if expect_movie is not None:
            t.equal(ids.count(expect_movie), 1, f"{label}: the movie appears once")
        if total is not None and isinstance(d, dict) and d.get("TotalRecordCount") is not None:
            t.equal(d["TotalRecordCount"], total, f"{label}: TotalRecordCount")
        return ids

    db_movies = t.db.one("select count(*) from BaseItems where Type like '%Movies.Movie' and (Tags is null or Tags not like '%gelato-stream%') "
                         "and PrimaryVersionId is null and IsVirtualItem=0")[0]
    listing("library movies", f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true&Limit=5000&Fields=ProviderIds", movie, db_movies)
    listing("recently added movies", f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true&SortBy=DateCreated&SortOrder=Descending&Limit=100")
    listing("latest movies", f"/Users/{u}/Items/Latest?IncludeItemTypes=Movie&Limit=100")
    # Gelato answers a search from the addon with result ids of its own, so the library id is not
    # what appears there: count the title by its Stremio id instead.
    stremio = t.fixtures.stremio_id(movie)
    d = t.api.get(f"/Items?userId={u}&searchTerm={q(name)}&IncludeItemTypes=Movie&Recursive=true&Limit=50&Fields=Path")
    ids = [i["Id"].lower() for i in d.get("Items", [])]
    t.log(f"search by name: {len(ids)} items")
    t.check(not [i for i in ids if i in rows_all], "search by name: no stream rows")
    t.equal(len(ids) - len(set(ids)), 0, "search by name: duplicate ids")
    t.equal(sum(1 for i in d.get("Items", []) if (i.get("Path") or "").endswith(stremio)), 1, "search by name: the title appears once")
    hints = t.api.get(f"/Search/Hints?userId={u}&searchTerm={q(name)}&includeItemTypes=Movie&limit=50")
    ids = [h.get("ItemId", h.get("Id", "")).lower() for h in hints.get("SearchHints", [])]
    t.check(not [i for i in ids if i in rows_all], "search hints: no stream rows")
    t.equal(ids.count(movie), 1, "search hints: the movie appears once")
    recs = t.api.get(f"/Movies/Recommendations?userId={u}&itemLimit=10&categoryLimit=6")
    rec_ids = [i["Id"].lower() for r in recs for i in r.get("Items", [])]
    t.check(not [i for i in rec_ids if i in rows_all], f"recommendations: no stream rows ({len(rec_ids)} items)")
    if genre:
        listing(f"genre {genre}", f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true&Genres={q(genre)}&Limit=2000", movie)
    if year:
        listing(f"year {year}", f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true&Years={year}&Limit=2000", movie)
    listing(f"name starts with {letter}", f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true&NameStartsWith={q(letter)}&Limit=2000", movie)
    listing("by ids (movie and row)", f"/Items?userId={u}&Ids={movie},{row}", movie)
    listing("resume", f"/UserItems/Resume?userId={u}&mediaTypes=Video&limit=100")
    listing("next up", f"/Shows/NextUp?userId={u}&limit=100")
    listing("library episodes of the series", f"/Items?userId={u}&ParentId={series}&IncludeItemTypes=Episode&Recursive=true&Limit=1000")
    season = t.api.get(f"/Shows/{series}/Seasons?userId={u}").get("Items", [])[0]["Id"]
    listing("season children", f"/Items?userId={u}&ParentId={season}&Limit=1000")
    listing("latest episodes", f"/Users/{u}/Items/Latest?IncludeItemTypes=Episode&Limit=100")

    for c in t.api.get(f"/Items?userId={u}&IncludeItemTypes=BoxSet&Recursive=true").get("Items", []):
        if c["Name"] == COLLECTION:
            t.api.delete(f"/Items/{c['Id']}")
    boxset = t.api.post(f"/Collections?name={COLLECTION}&ids={row}")["Id"]
    t.wait(2)
    try:
        t.equal(t.db.playlist_links(boxset), [(movie, "item")], "a collection made from a version page holds the movie")
        listing("collection items", f"/Items?userId={u}&ParentId={boxset}", movie)
        listing("collections containing the row", f"/Items?userId={u}&IncludeItemTypes=BoxSet&Recursive=true&Ids={boxset}")
    finally:
        t.api.delete(f"/Items/{boxset}")
