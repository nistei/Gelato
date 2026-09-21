DESCRIPTION = "The details-page reads a client issues with a search result's id (ancestors, similar, theme media) answer for the library item the result is, while it is being inserted too, and read nothing into the library"

import threading
import time

from jfapi.bootstrap import GELATO

# Titles for the half that opens a result the library does not have yet.
FRESH_TERMS = ["Nosferatu", "Heretic", "Anora", "Conclave", "Flow", "The Substance", "Longlegs", "Civil War"]

# What jellyfin-web asks for on a details page besides the item itself. The page URL keeps the id
# the page was opened with, so a page reached from search names the search result in all of them.
READS = [
    ("ancestors", "/Items/{id}/Ancestors?userId={user}"),
    ("similar", "/Items/{id}/Similar?userId={user}&limit=5"),
    ("theme media", "/Items/{id}/ThemeMedia?userId={user}"),
    ("intros", "/Items/{id}/Intros?userId={user}"),
    ("special features", "/Items/{id}/SpecialFeatures?userId={user}"),
    ("user data", "/UserItems/{id}/UserData?userId={user}"),
]

CANDIDATE_SQL = (
    "select lower(replace(b.Id,'-','')), b.Name, p.ProviderValue from BaseItems b "
    "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
    "where b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like '%gelato-stream%') "
    "and b.PrimaryVersionId is null and p.ProviderValue like 'tt%' order by random() limit 6"
)


def count_items(t):
    t.db.invalidate()
    return t.db.one("select count(*) from BaseItems")[0]


def in_library(t, stremio):
    return t.db.one(
        "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]


def run(t):
    # The search result's id is derived from the stremio id, so it is the same guid every time and
    # an earlier test may have opened it already: then the filter remembers the mapping and every
    # read is redirected for that reason alone. Saving the configuration drops Gelato's caches,
    # which is the state a client reaches the page in after a server restart.
    cfg = t.api.get("/Plugins/" + GELATO + "/Configuration")
    t.api.post("/Plugins/" + GELATO + "/Configuration", cfg)

    movie = search_id = None
    for item_id, name, stremio in t.db.query(CANDIDATE_SQL):
        hit = next((h for h in t.api.search(name, "Movie", limit=25)
                    if (h.get("Path") or "") == f"gelato://stub/{stremio}"), None)
        if hit is not None:
            movie, search_id = item_id, hit["Id"]
            t.log(f"the search returns {name} ({stremio}) as {search_id[:8]}, the library item is {item_id[:8]}")
            break
    if movie is None:
        t.skip("the search returned none of six library movies under its own stremio id")
    if not t.check(search_id.lower() != movie, "the search result carries a search-result id, not the library item's"):
        return

    def read(item_id, path):
        st, d = t.api.call("GET", path.format(id=item_id, user=t.api.user))
        items = d.get("Items") if isinstance(d, dict) and "Items" in d else d if isinstance(d, list) else None
        return st, items, f"{st}" + (f", {len(items)} items" if items is not None else "")

    def ids_of(items):
        return [i["Id"].lower() for i in items or []]

    def fresh_hits(t):
        """Search hits for movies the library does not have."""
        for term in FRESH_TERMS:
            for h in t.api.search(term, "Movie", limit=8):
                stub_id = (h.get("Path") or "")[len("gelato://stub/"):]
                if (h.get("Path") or "").startswith("gelato://stub/tt") and not in_library(t, stub_id):
                    yield h

    before = count_items(t)

    # What the call answers on the library item is the answer the search result's id owes.
    for label, path in READS:
        want_st, want, want_note = read(movie, path)
        got_st, got, got_note = read(search_id, path)
        t.log(f"{label}: library item {want_note}, search result {got_note}")
        t.equal(got_st, want_st, f"{label} with the search result's id answers like the library item's")
        if label == "ancestors" and got_st == want_st == 200:
            t.equal(ids_of(got), ids_of(want), "the ancestors of the search result's id are the library item's")

    t.equal(count_items(t), before, "reading a details page added nothing to the library")

    # What the client really does when a result the library does not have is opened: jellyfin-web
    # asks for the ancestors before the item itself (seen in the browser on 2026-09-22, the call
    # answered 404 and the breadcrumb stayed empty). That one read has to materialize the title
    # like the item call does.
    hits = fresh_hits(t)
    hit = next(hits, None)
    if hit is None:
        t.log("no movie outside the library among the hits for the not-in-library half")
        return
    t.log(f"opening {hit['Name']} ({(hit.get('Path') or '')[len('gelato://stub/'):]}) the way the web does")

    # The reads beside it do not insert: they answer for what the library has.
    st, _ = t.api.call("GET", f"/Items/{hit['Id']}/Similar?userId={t.api.user}&limit=5")
    t.equal(count_items(t), before, f"similar items of a result nobody opened insert nothing ({st})")

    st, items, note = read(hit["Id"], "/Items/{id}/Ancestors?userId={user}")
    t.equal(st, 200, f"the ancestors a client asks for first answer for the title it opened ({note})")
    inserted = None
    try:
        d = t.api.item(hit["Id"])
        inserted = d.get("Id", "").lower()
        t.check(inserted and inserted != hit["Id"].lower(), f"the item call lands on the same library item ({(inserted or '-')[:8]})")
        if st == 200 and inserted:
            _, want, _ = read(inserted, "/Items/{id}/Ancestors?userId={user}")
            t.equal(ids_of(items), ids_of(want), "they are the ancestors of the item the page ends up on")
    finally:
        if inserted:
            t.api.delete(f"/Items/{inserted}")
        t.db.invalidate()
