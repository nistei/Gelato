DESCRIPTION = "Extend local series trees: a local episode's slot is listed once, for a series opened while its scan still runs and for a file added to an extended tree"
DESTRUCTIVE = True  # adds a library, turns the option on and runs the tree sync over every series of the library; removes the library again

import time
import urllib.parse
from collections import Counter

from jfapi.bootstrap import GELATO
from jfapi.native import library_id, remove_libraries, rows_under, series_in, write_videos

LIBRARY, PATH = "jfapi-localtree-slot", "/tmp/jfapi-localtree-slot"
LIBRARIES = [(LIBRARY, "tvshows", PATH)]
# One local episode of a show the addon knows: Gelato adds the other seasons and episodes.
SHOW = "Breaking Bad (2008) [imdbid-tt0903747]"
FILES = [f"{PATH}/{SHOW}/Season 01/Breaking Bad S01E0{n}.mkv" for n in (1, 2, 3)]
TASK = "SyncSeriesTrees"
TREE_TAG = "gelato-tree-synced"  # set by the task on a series it extended; the series page leaves such a series' tree alone


def listed(t, series_id):
    """What a client is shown for the series, one entry per listed item: (season, episode, location
    type, id). Not a dictionary by slot: two items in one slot are what this test looks for."""
    return [(e.get("ParentIndexNumber"), e.get("IndexNumber"), e.get("LocationType"), e["Id"].lower())
            for e in t.api.get(f"/Shows/{series_id}/Episodes?userId={t.api.user}").get("Items", [])]


def twice(items):
    """The numbered slots more than one listed item holds."""
    return sorted(slot for slot, n in Counter((s, e) for s, e, _, _ in items).items() if n > 1 and None not in slot)


def local_rows(t):
    """{path: (season, episode)} of the library's own episodes as the database has them, None while unnumbered."""
    return {r[0]: (r[1], r[2]) for r in t.db.query(
        "select Path, ParentIndexNumber, IndexNumber from BaseItems where Type like '%TV.Episode' and Path like ?", (PATH + "/%",))}


def scan(t, lib):
    """Asks for the library's scan and returns at once: what the test is about happens while it runs."""
    t.api.post(f"/Items/{lib}/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default")


def add_file(t, lib, number):
    """Writes the season's next episode file and scans it in, until the database has its numbers."""
    write_videos(t, [FILES[number - 1]])
    scan(t, lib)
    want, t0 = {f: (1, n + 1) for n, f in enumerate(FILES[:number])}, time.time()
    while time.time() - t0 < 120 and local_rows(t) != want:
        t.wait(0.2)
    t.settle(after=1)
    return t.equal(sorted(local_rows(t).values()), sorted(want.values()), f"the scan adds the file of S01E0{number}")


def visit(t, series_id, local, timeout=120):
    """Opens the series page again and again, as a user who comes back to it, until the tree is past
    the local episodes, lists every one of them with its numbers and stopped growing. Returns the
    last listing and every slot that was listed twice on the way."""
    t0, size, items, seen_twice = time.time(), None, [], set()
    while time.time() - t0 < timeout:
        t.api.item(series_id)  # opening the page is what extends the tree
        items = listed(t, series_id)
        seen_twice.update(twice(items))
        files = [i for i in items if i[2] == "FileSystem"]
        settled = len(items) > local and len(files) == local and all(s is not None and e is not None for s, e, _, _ in items)
        if settled and len(items) == size:
            break
        size = len(items) if settled else None
        t.settle(after=0.5, timeout=3)
    return items, sorted(seen_twice)


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    remove_libraries(t, LIBRARIES)
    write_videos(t, FILES[:1])
    try:
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "ExtendLocalSeriesTrees": True})
        # Not add_library: it waits until the scan has named the items, and the series is opened
        # here before that, the moment a client can click it.
        t.api.post(f"/Library/VirtualFolders?name={urllib.parse.quote(LIBRARY)}&collectionType=tvshows"
                   f"&paths={urllib.parse.quote(PATH, safe='')}&refreshLibrary=false",
                   {"LibraryOptions": {"EnableRealtimeMonitor": False}})
        lib = library_id(t, LIBRARY)
        scan(t, lib)
        t0, series = time.time(), []
        while time.time() - t0 < 120 and not series:
            series = series_in(t, lib)
            if not series:
                t.wait(0.1)
        if not t.equal(len(series), 1, "the scan lists the local series"):
            return
        series_id = series[0]
        t.api.item(series_id)
        t.log(f"opened {time.time() - t0:.1f}s after the scan was asked for; the local episode's row then: {list(local_rows(t).values()) or 'none yet'}")

        items, seen = visit(t, series_id, local=1)
        t.settle(after=1)
        items = listed(t, series_id)
        t.log(f"extended: {len(items)} episodes listed in {len({i[:2] for i in items})} slots")
        t.check(len(items) > 10, f"the series is extended ({len(items)} episodes)")
        t.equal(seen, [], "no slot was listed twice while the scan ran")
        t.equal(twice(items), [], "no slot is listed twice once the server settled")
        t.equal([i[2] for i in items if i[:2] == (1, 1)], ["FileSystem"], "S01E01 is listed once, as the file")
        if len(items) <= 10 or (1, 2) not in {i[:2] for i in items}:
            return

        # A file for an episode Gelato added: the file takes the slot, and what was watched stays watched.
        # Played at a time of its own: the file can come with a watch state Jellyfin kept from an
        # earlier copy of it, and that one must not pass for the one moved here.
        added = next(i[3] for i in items if i[:2] == (1, 2))
        played_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        t.api.post(f"/UserPlayedItems/{added}?userId={t.api.user}&datePlayed={played_at}Z")
        if not add_file(t, lib, 2):
            return
        after, _ = visit(t, series_id, local=2)
        t.equal(twice(after), [], "no slot is listed twice after a file was added")
        second = [i for i in after if i[:2] == (1, 2)]
        t.equal([i[2] for i in second], ["FileSystem"], "S01E02 is listed once, as the file")
        t.equal(len(after), len(items), "the tree keeps its size")
        if len(second) == 1:
            state = t.api.user_data(second[0][3])
            t.check(state.get("Played") is True and (state.get("LastPlayedDate") or "")[:19] == played_at,
                    f"the file is played when Gelato's episode was ({state.get('Played')}, {state.get('LastPlayedDate')}, expected {played_at})")

        # The same for a series the task has marked as extended, which is every local series after
        # the task's first run: its page does not sync the tree again.
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"sync series trees {msg}")
        t.settle(after=1)
        tags = (t.db.one("select Tags from BaseItems where lower(replace(Id,'-',''))=?", (series_id,))[0] or "").split("|")
        if TREE_TAG not in tags or (1, 3) not in {i[:2] for i in listed(t, series_id)}:
            t.log(f"not judged for a marked series: the task left it unmarked (tags {tags[-1:]}) or the tree has no S01E03")
            return
        if not add_file(t, lib, 3):
            return
        marked, _ = visit(t, series_id, local=3)
        t.equal(twice(marked), [], "no slot is listed twice after a file was added to a marked series")
        t.equal([i[2] for i in marked if i[:2] == (1, 3)], ["FileSystem"], "S01E03 is listed once, as the file")
        t.equal(len(marked), len(items), "the marked series' tree keeps its size")
    finally:
        t.api.post(f"/Plugins/{GELATO}/Configuration",
                   {**t.api.get(f"/Plugins/{GELATO}/Configuration"), "ExtendLocalSeriesTrees": cfg.get("ExtendLocalSeriesTrees", False)})
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [PATH]), 0, "the local series and its tree removed again")
