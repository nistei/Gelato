DESCRIPTION = "Paging a search: the pages of one term do not repeat an item between them, they are the one long answer cut in two, and both report the same total"

import urllib.parse

GLOBAL_TYPES = "Movie,Series,Episode,Playlist,MusicAlbum,Audio,TvChannel,PhotoAlbum,Photo,AudioBook,Book,BoxSet"
# Broad terms, so both halves of the answer are long enough to page: the addon's results and the
# library's own items behind them.
TERMS = ["star", "the", "man", "der", "love", "day", "girl", "life"]
PAGE = 10


def search(t, term, start=None, limit=PAGE, types=GLOBAL_TYPES):
    path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}"
            f"&Recursive=true&Limit={limit}&IncludeItemTypes={types}&Fields=Path")
    if start is not None:
        path += f"&StartIndex={start}"
    st, d = t.api.call("GET", path)
    if st != 200 or not isinstance(d, dict):
        return st, [], None
    return st, d.get("Items", []), d.get("TotalRecordCount")


def ids(items):
    return [i["Id"].lower() for i in items]


def run(t):
    term = None
    for candidate in TERMS:
        st, items, total = search(t, candidate, limit=2 * PAGE + 1)
        if st == 200 and len(items) > 2 * PAGE:
            term = candidate
            t.log(f"term {candidate!r}: {len(items)} items in one answer, total {total}")
            break
    if term is None:
        t.skip("no term with more than two pages of results")

    st, whole, whole_total = search(t, term, limit=2 * PAGE)
    t.equal(st, 200, "the long answer answers")
    t.equal(len(set(ids(whole))), len(whole), "the long answer lists no item twice")

    st, first, first_total = search(t, term, start=0)
    t.equal(st, 200, "the first page answers")
    st, second, second_total = search(t, term, start=PAGE)
    t.equal(st, 200, "the second page answers")
    t.log(f"page 1: {len(first)} items, total {first_total}; "
          f"page 2: {len(second)} items, total {second_total}")

    both = set(ids(first)) & set(ids(second))
    t.check(not both, f"no item is on both pages ({[i[:8] for i in both]})")
    t.equal(len(first), PAGE, "the first page is full")
    t.equal(len(second), PAGE, "the second page is full")
    t.equal(ids(first) + ids(second), ids(whole),
            "the two pages are the long answer cut in two")
    t.equal(second_total, first_total, "both pages report the same total")
    t.check(first_total is None or first_total >= len(whole),
            f"the total is not smaller than what was handed out ({first_total} for {len(whole)} items)")
