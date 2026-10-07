DESCRIPTION = "Unreleased filter: a listing follows a release date as soon as it is written, by Jellyfin's item update and by Gelato's release date task, with other listings running alongside and no configuration save in between"
DESTRUCTIVE = True  # turns the filter on, edits a movie's release date and runs the release date task over the whole library

import threading

from jfapi.bootstrap import GELATO, MOVIE_PATH
from tests.test_lockmeta import DATES_TASK, dto, edit

SENTINEL = "9999-01-01T00:00:00.0000000Z"  # Gelato's "no release date known"
ROUNDS = 5
# A movie the filter lists and the release date task dates in the past whatever TMDB says: one that
# premiered over a year ago takes its premiere when it has no digital release.
PICK = (
    "select lower(replace(b.Id,'-','')), b.Name, b.EndDate from BaseItems b "
    "join BaseItemProviders s on s.ItemId=b.Id and lower(s.ProviderId)='stremio' "
    "where b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like '%gelato-stream%') "
    "and b.PrimaryVersionId is null and b.IsLocked=0 and b.EndDate < datetime('now', '-30 day') "
    "and b.PremiereDate < datetime('now', '-2 year') order by b.Id limit 1"
)
LIGHT = "&EnableImages=false&EnableUserData=false&Limit=5000"


def run(t):
    api, u = t.api, t.api.user
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    row = t.db.one(PICK)
    t.require(row, "no released, unlocked Gelato movie that premiered over two years ago")
    movie, name, _ = row
    original = dto(t, movie).get("EndDate")
    t.log(f"movie {name!r} ({movie}), EndDate {original}")

    # The recursive listing excludes the unreleased ids in its query, the flat one is answered
    # from the folder's children and filtered on the way out: both read the same kept set.
    lib = next((v["ItemId"].lower() for v in api.get("/Library/VirtualFolders") if MOVIE_PATH in (v.get("Locations") or [])), None)
    shapes = {"all movies": f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true{LIGHT}"}
    if lib:
        shapes["library, flat"] = f"/Items?userId={u}&ParentId={lib}{LIGHT}"

    def listed(path):
        return movie in {i["Id"].lower() for i in api.get(path).get("Items", [])}

    def end_date():
        t.db.invalidate()
        return t.db.one("select EndDate from BaseItems where lower(replace(Id,'-',''))=?", (movie,))[0] or ""

    try:
        # The only configuration save before the checks: saving clears what Gelato keeps, so
        # from here on a listing can only learn of a change through the item write itself.
        api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "FilterUnreleased": True, "FilterUnreleasedBufferDays": 0})
        for label in [l for l, path in shapes.items() if not listed(path)]:
            t.log(f"{label}: does not list the movie to begin with, left out")
            del shapes[label]
        t.require(shapes, "no listing shows the movie with the filter on")

        # Listings that run while the date is written: one of them reading the set just before
        # a write and keeping it just after is what would leave the next listing stale.
        stop, errors = threading.Event(), []

        def hammer():
            while not stop.is_set():
                for path in shapes.values():
                    try:
                        api.get(path)
                    except Exception as e:
                        errors.append(repr(e)[:120])

        threads = [threading.Thread(target=hammer, daemon=True) for _ in range(2)]
        for th in threads:
            th.start()
        stale = []
        try:
            for n in range(ROUNDS):
                edit(t, movie, EndDate=SENTINEL)
                stale += [(n, "still listed", label) for label, path in shapes.items() if listed(path)]
                edit(t, movie, EndDate=original)
                stale += [(n, "still hidden", label) for label, path in shapes.items() if not listed(path)]
        finally:
            stop.set()
            for th in threads:
                th.join(timeout=30)
        t.log(f"{ROUNDS} rounds of hiding and showing over {sorted(shapes)}, stale answers: {stale}, listing errors alongside: {errors[:3]}")
        t.check(not stale, f"Jellyfin's item update: every listing followed each of {2 * ROUNDS} changes of the release date ({len(stale)} stale)")
        t.check(not errors, f"the listings running alongside all answered ({len(errors)} failed)")

        # Gelato's own write: the task saves through the persistence service, which raises no
        # library event.
        edit(t, movie, EndDate=SENTINEL)
        t.check(end_date() > "9000", "the movie carries the sentinel before the release date task runs")
        hidden = [label for label, path in shapes.items() if not listed(path)]
        t.equal(sorted(hidden), sorted(shapes), "the movie is hidden before the release date task runs")
        status, msg = api.run_task(DATES_TASK, timeout=600)
        t.equal(status, "Completed", f"Sync release dates finished {msg}")
        after = end_date()
        t.log(f"EndDate after the task: {after}")
        if after > "9000":
            t.log("the task left the sentinel (the addon has no meta for the movie): Gelato's own write is not covered")
        else:
            back = [label for label, path in shapes.items() if listed(path)]
            t.equal(sorted(back), sorted(shapes), "Gelato's release date task: the movie is listed again as soon as the task has dated it")
    finally:
        api.post(f"/Plugins/{GELATO}/Configuration", {**api.get(f"/Plugins/{GELATO}/Configuration"),
                                                      "FilterUnreleased": cfg.get("FilterUnreleased", False),
                                                      "FilterUnreleasedBufferDays": cfg.get("FilterUnreleasedBufferDays", 0)})
        if dto(t, movie).get("EndDate") != original:
            edit(t, movie, EndDate=original)
