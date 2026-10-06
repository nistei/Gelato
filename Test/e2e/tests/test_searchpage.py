DESCRIPTION = "Paging a search: the pages of one term do not repeat an item between them, they are the one long answer cut in two, and every answer reports the same total, which is where the list ends"

import re
import shlex
import urllib.parse

GLOBAL_TYPES = "Movie,Series,Episode,Playlist,MusicAlbum,Audio,TvChannel,PhotoAlbum,Photo,AudioBook,Book,BoxSet"
# Broad terms, so both halves of the answer are long enough to page: the addon's results and the
# library's own items behind them.
TERMS = ["star", "the", "man", "der", "love", "day", "girl", "life"]
PAGE = 10
# The whole list is fetched once to see where it ends; a term matching more than this is not.
WHOLE_LIST = 300


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


def owned(t, term):
    """How many of the addon's results for the last search of `term` were titles the library has
    (Gelato logs owned=N for every search)."""
    line = t.sh("cat /config/log/log_*.log 2>/dev/null | grep -F " + shlex.quote(f'Intercepted /Items search ""{term}""') + " | tail -1")
    m = re.search(r"owned=(\d+) ", line)
    return int(m.group(1)) if m else 0


def run(t):
    # The total is only at risk where the library's half is cut at the end of the page and has to
    # be counted, and where that count has titles to leave out: the ones the addon's results
    # already answered with. So a term whose library matches fill a page and overlap the addon's
    # results is the one to page; any long answer still shows the pages fit together.
    term = fallback = None
    for candidate in TERMS:
        st, items, total = search(t, candidate, limit=2 * PAGE + 1)
        if st != 200 or len(items) <= 2 * PAGE:
            continue
        overlap = owned(t, candidate)
        _, library, _ = search(t, "local:" + candidate, limit=WHOLE_LIST)
        t.log(f"term {candidate!r}: {len(items)} items in one answer, total {total}, "
              f"{len(library)} library matches, {overlap} of them answered by the addon's results")
        if overlap and len(library) >= PAGE:
            term = candidate
            break
        fallback = fallback or candidate
    if term is None:
        if fallback is None:
            t.skip("no term with more than two pages of results")
        term = fallback
        t.log(f"no term whose library matches fill a page and overlap the addon's results: paging {term!r} without")

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
    t.equal(second_total, first_total, "the second page reports the first page's total")
    t.equal(whole_total, first_total, "the long answer reports the first page's total")
    t.check(first_total is None or first_total >= len(whole),
            f"the total is not smaller than what was handed out ({first_total} for {len(whole)} items)")

    # A client pages until it has as many items as the total says: a total one off ends the list
    # an item early or asks for a page that is empty.
    if first_total is None or first_total > WHOLE_LIST:
        t.log(f"the whole list is not fetched: total {first_total}")
        return
    st, everything, everything_total = search(t, term, limit=first_total + PAGE)
    t.equal(st, 200, "the whole list answers")
    t.equal(len(everything), first_total, "the list ends where the first page's total says")
    t.equal(everything_total, first_total, "the whole list reports the first page's total")
    t.equal(len(set(ids(everything))), len(everything), "the whole list lists no item twice")
    t.check(ids(everything)[:2 * PAGE] == ids(whole), "the whole list starts with the long answer")
