DESCRIPTION = "'Generate Trickplay Images' with extraction on leaves probed stream rows alone: no 'Media not found' warning with the stream URL, no trickplay data"
DESTRUCTIVE = True  # turns trickplay extraction on for the movie library and runs the task over the whole library

from jfapi.db import STREAM_TAG
from jfapi.probe import ensure_probed, log_count, movie_library, probed_rows

TASK = "RefreshTrickplayImages"
WARNING = "Media not found at"


def trickplay_rows(t):
    return t.db.one("select count(*) from TrickplayInfos i join BaseItems b on b.Id=i.ItemId where b.Tags like ?", (STREAM_TAG,))[0]


def trickplay_dirs(t, rows):
    return [d for d in t.sh("find /config /media -type d -name trickplay 2>/dev/null").split() if any(r in d for r in rows)]


def run(t):
    library = movie_library(t)
    if library is None:
        t.skip("no Gelato movie library")
    rows = ensure_probed(t)
    if not rows:
        t.skip("no stream row could be probed (dead links?)")
    t.log(f"{len(rows)} probed row(s): {[r[:8] for r in rows]}")

    original = library["LibraryOptions"]
    options = {**original, "EnableTrickplayImageExtraction": True, "ExtractTrickplayImagesDuringLibraryScan": True,
               "SaveTrickplayWithMedia": False}
    t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": options})
    try:
        warnings = log_count(t, WARNING)
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"{TASK} finished {msg}")
        t.equal(log_count(t, WARNING) - warnings, 0, f"no new '{WARNING}' warning (it logs the stream URL)")
        t.equal(trickplay_rows(t), 0, "no trickplay data saved for a stream row")
        t.equal(trickplay_dirs(t, rows), [], "no trickplay folder for a probed row")
        t.equal([r[:8] for r in rows if r not in probed_rows(t)], [], "the probed rows keep their media streams")
    finally:
        t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": original})
        restored = movie_library(t)["LibraryOptions"]
        t.equal(restored.get("EnableTrickplayImageExtraction"), original.get("EnableTrickplayImageExtraction"), "library trickplay option restored")
