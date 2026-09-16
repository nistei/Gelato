DESCRIPTION = "'Extract chapter images' with extraction on runs no ffmpeg on probed stream rows: no stream URL in chapter-failures.txt or the error log, chapters kept"
DESTRUCTIVE = True  # turns chapter image extraction on for the movie library and runs the task over the whole library

from jfapi.probe import ensure_probed, log_count, movie_library, row_paths

TASK = "RefreshChapterImages"
FAILURES = "/cache/chapter-failures.txt"
ERROR = "Error extracting chapter images for"
EXTRACTING = "Extracting chapter image for"


def chapters(t, row):
    """(chapters, chapters with an image) of the row."""
    return t.db.one("select count(*), sum(case when ImagePath is not null then 1 else 0 end) from Chapters "
                    "where lower(replace(ItemId,'-',''))=?", (row,))


def has_chapters(t):
    return lambda row: chapters(t, row)[0] >= 2


def run(t):
    library = movie_library(t)
    if library is None:
        t.skip("no Gelato movie library")
    rows = ensure_probed(t, accept=has_chapters(t), tries=8)
    if not rows:
        t.skip("no probed stream row with chapters (releases without chapters or dead links)")
    before = {r: chapters(t, r) for r in rows}
    t.log("probed rows with chapters (chapters, with image):", {r[:8]: c for r, c in before.items()})

    original = library["LibraryOptions"]
    options = {**original, "EnableChapterImageExtraction": True, "ExtractChapterImagesDuringLibraryScan": True}
    t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": options})
    try:
        errors, extracting = log_count(t, ERROR), log_count(t, EXTRACTING)
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"{TASK} finished {msg}")

        failures = t.sh(f"cat {FAILURES} 2>/dev/null")
        paths = row_paths(t, rows)
        # compared here, never printed: debrid URLs carry the API key
        t.equal(sum(1 for p in paths.values() if p and p in failures), 0, f"no probed row's path in {FAILURES}")
        t.equal(failures.count("http"), 0, f"no URL at all in {FAILURES}")
        t.equal(log_count(t, ERROR) - errors, 0, f"no new '{ERROR}' error (it logs the stream URL)")
        t.equal(log_count(t, EXTRACTING) - extracting, 0, "ffmpeg was not started for any chapter")
        after = {r: chapters(t, r) for r in rows}
        t.equal({r: c[0] for r, c in after.items()}, {r: c[0] for r, c in before.items()}, "the rows keep their chapters")
        t.equal([r[:8] for r, c in after.items() if c[1]], [], "no chapter image on a probed row")
        dirs = [d for d in t.sh("find /config /media -type d -name chapters 2>/dev/null").split() if any(r in d for r in rows)]
        t.equal(dirs, [], "no chapter image folder for a probed row")
    finally:
        t.api.post("/Library/VirtualFolders/LibraryOptions", {"Id": library["ItemId"], "LibraryOptions": original})
        restored = movie_library(t)["LibraryOptions"]
        t.equal(restored.get("EnableChapterImageExtraction"), original.get("EnableChapterImageExtraction"), "library chapter option restored")
