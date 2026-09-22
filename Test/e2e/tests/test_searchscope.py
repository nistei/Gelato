DESCRIPTION = "A search inside one library answers for that library: its own items are found, nothing is listed twice, and no item of another library is handed back"
DESTRUCTIVE = True  # adds a library with two generated video files and removes it again

import time
import urllib.parse

from jfapi.bootstrap import GELATO
from jfapi.native import add_library, library_id, remove_libraries, write_videos

# What the web client sends inside a library: the library as topParentId, its collection type, and
# the types that collection holds.
TYPES = {"movies": "Movie", "tvshows": "Series,Episode", "boxsets": "BoxSet"}

LIBRARY, PATH = "jfapi-searchscope", "/tmp/jfapi-searchscope"
LIBRARIES = [(LIBRARY, "movies", PATH)]
# A title no addon answers for, and one it does: the local library holds both, so the scoped
# search has something of its own to find either way.
LOCAL_ONLY = "Zappelfrosch Hausvideo (2019)"
ADDON_TERM = "star"  # a term the addon answers for, and the Gelato libraries have titles for

# A mixed library: local files beside Gelato's folder in one library, which is what the scope rule
# has to keep answering for.
MIXED_PATH = "/tmp/jfapi-searchscope-mixed"
MIXED_TITLE = "Zappelfroschkonzert (2021)"


def search(t, term, scope=None, collection_type=None, types=None, limit=50, by="topParentId"):
    path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}"
            f"&Recursive=true&Limit={limit}&Fields=Path&Fields=ParentId")
    if scope:
        path += f"&{by}={scope}"
    if collection_type:
        path += f"&collectionType={collection_type}"
    if types:
        path += f"&IncludeItemTypes={types}"
    st, d = t.api.call("GET", path)
    return st, (d.get("Items", []) if st == 200 and isinstance(d, dict) else [])


def views(t):
    return t.api.get(f"/UserViews?userId={t.api.user}").get("Items", [])


def library_of(t, item_id):
    """The library an item belongs to, as its top ancestor, or None for a result the addon
    answers with (nothing in the library holds it yet)."""
    st, ancestors = t.api.call("GET", f"/Items/{item_id}/Ancestors?userId={t.api.user}")
    if st != 200 or not ancestors:
        return None
    return ancestors[-2]["Id"].lower() if len(ancestors) > 1 else ancestors[-1]["Id"].lower()


def is_stub(item):
    """A result the addon answers with, not an item of the library: it carries no user data."""
    return item.get("UserData") is None and (item.get("Path") or "").startswith("gelato://stub/")


def check_scope(t, label, scope_id, items, scoped=True):
    """No item twice, and — for a request that really scopes, which means parentId — every item
    is either the addon's answer or one of this library's."""
    ids = [i["Id"].lower() for i in items]
    t.equal(len(set(ids)), len(ids), f"{label} lists no item twice")
    if not scoped:
        return

    foreign = []
    for item in items:
        if is_stub(item):
            continue
        owner = library_of(t, item["Id"])
        if owner and owner != scope_id:
            foreign.append((item["Id"][:8], item.get("Name"), owner[:8]))
    t.check(not foreign, f"{label} hands back no item of another library ({foreign[:4]})")


def gelato_library(t):
    """The library that holds Gelato's movie folder, and that folder's path."""
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    path = (cfg.get("MoviePath") or "").rstrip("/")
    if not path:
        return None, None
    for v in t.api.get("/Library/VirtualFolders"):
        if any(path == loc.rstrip("/") or path.startswith(loc.rstrip("/") + "/") for loc in v["Locations"]):
            return v, path
    return None, path


def add_path(t, name, path):
    t.api.post(f"/Library/VirtualFolders/Paths?refreshLibrary=false",
               {"Name": name, "Path": path, "PathInfo": {"Path": path}})


def remove_path(t, name, path):
    t.api.call("DELETE", f"/Library/VirtualFolders/Paths?name={urllib.parse.quote(name)}"
                         f"&path={urllib.parse.quote(path, safe='')}&refreshLibrary=false")


def mixed_library(t):
    """Gelato's movie library with a folder of local files added to it, as the library id and the
    id of the local movie in it. The library is left the way it was found."""
    view, _ = gelato_library(t)
    if view is None:
        return None, None, None
    write_videos(t, [f"{MIXED_PATH}/{MIXED_TITLE}/{MIXED_TITLE}.mkv"])
    add_path(t, view["Name"], MIXED_PATH)
    lib = library_id(t, view["Name"])
    # The library is the instance's whole movie collection, and refreshing all of it takes longer
    # than this test may: report the one new folder instead, the way an external tool does.
    t.api.post("/Library/Media/Updated",
               {"Updates": [{"Path": MIXED_PATH, "UpdateType": "Created"}]})
    local, t0 = None, time.time()
    while time.time() - t0 < 60:
        hits = t.api.get(f"/Items?userId={t.api.user}&searchTerm=Zappelfroschkonzert"
                         f"&IncludeItemTypes=Movie&Recursive=true&Fields=Path").get("Items", [])
        local = next((i["Id"].lower() for i in hits if MIXED_PATH in (i.get("Path") or "")), None)
        if local:
            break
        t.wait(3)
    return view, lib.lower(), local


def run(t):
    # Every library the instance has, searched with the term the addon answers for.
    for view in views(t):
        kind = view.get("CollectionType")
        if kind not in TYPES:
            t.log(f"skipped {view['Name']!r}: nothing is searched inside a {kind} library")
            continue
        scope = view["Id"].lower()
        for by in ("topParentId", "parentId"):
            st, items = search(t, ADDON_TERM, scope, kind, TYPES[kind], by=by)
            t.equal(st, 200, f"the search inside {view['Name']!r} by {by} answers")
            t.log(f"{view['Name']!r} by {by}: {len(items)} items, "
                  f"{sum(1 for i in items if is_stub(i))} of them the addon's")
            # topParentId is the web client's route, not a parameter of /Items: Jellyfin drops it
            # and the answer is the unscoped one, so only parentId is held to the library.
            check_scope(t, f"the search inside {view['Name']!r} by {by}", scope, items,
                        scoped=(by == "parentId"))

    # A library of local files only: what it holds is findable inside it, and the catalogs do not
    # fill it with titles it does not have.
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{PATH}/{LOCAL_ONLY}/{LOCAL_ONLY}.mkv"])
    try:
        lib, items = add_library(t, LIBRARY, "movies", PATH, "Movie", 1)
        local, scope = items[0], lib.lower()

        st, found = search(t, "Zappelfrosch", scope, "movies", "Movie", by="parentId")
        t.equal(st, 200, "the search inside the local library answers")
        t.check(any(i["Id"].lower() == local for i in found),
                "the local library finds the file it holds")
        check_scope(t, "the search inside the local library", scope, found)

        st, found = search(t, ADDON_TERM, scope, "movies", "Movie", by="parentId")
        t.equal(st, 200, "the search inside the local library for an addon term answers")
        t.log(f"the local library for {ADDON_TERM!r}: {len(found)} items, "
              f"{sum(1 for i in found if is_stub(i))} of them the addon's")
        t.check(not any(is_stub(i) for i in found),
                "a library without a Gelato folder is not filled with the catalogs' titles")
        check_scope(t, f"the search inside the local library for {ADDON_TERM!r}", scope, found)
    finally:
        remove_libraries(t, LIBRARIES)
        t.sh(f"rm -rf {PATH}")

    # A mixed library — local files beside Gelato's folder — keeps both halves: the catalogs
    # answer for it, because the folder their results land in is one of its own.
    view, scope, local = mixed_library(t)
    if view is None:
        t.log("no library holds Gelato's movie folder, the mixed library is left out")
        return
    try:
        # Scanning the file in is the instance's business and it may take longer than this test:
        # what the scope rule decides is the half below, which needs no scan.
        if local is None:
            t.log(f"the local file was not scanned into {view['Name']!r} in time, "
                  "the library's own half is left out")
        else:
            st, found = search(t, "Zappelfroschkonzert", scope, "movies", "Movie", by="parentId")
            t.equal(st, 200, "the mixed library answers")
            t.check(any(i["Id"].lower() == local for i in found),
                    "the mixed library finds the local file it holds")

        st, found = search(t, ADDON_TERM, scope, "movies", "Movie", by="parentId")
        t.equal(st, 200, "the mixed library answers for an addon term")
        t.log(f"the mixed library for {ADDON_TERM!r}: {len(found)} items, "
              f"{sum(1 for i in found if is_stub(i))} of them the addon's")
        t.check(any(is_stub(i) for i in found),
                "the catalogs still answer inside a library that holds Gelato's folder")
        check_scope(t, f"the search inside the mixed library for {ADDON_TERM!r}", scope, found)
    finally:
        remove_path(t, view["Name"], MIXED_PATH)
        for (item_id,) in t.db.query(
                "select lower(replace(Id,'-','')) from BaseItems where Path like ?",
                (MIXED_PATH + "/%",)):
            t.api.call("DELETE", f"/Items/{item_id}")
        t.sh(f"rm -rf {MIXED_PATH}")
        t.db.invalidate()
