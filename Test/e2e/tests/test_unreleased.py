DESCRIPTION = "Filter unreleased items hides no native items: libraries with local files, a collection and a playlist list the same with the filter on"
DESTRUCTIVE = True  # adds two libraries with local files, turns the filter on; removes them again

from jfapi.bootstrap import GELATO
from jfapi.native import add_library, remove_libraries, rows_under, write_videos

LIBRARY = "jfapi-native"
PATH = "/tmp/jfapi-native"
MOVIES = ["Jfapi Native One (2001)", "Jfapi Native Two (2002)"]
SHOWS = "jfapi-native-shows"
SHOWS_PATH = "/tmp/jfapi-native-shows"
SHOW = "Jfapi Native Show (2003)"
EPISODES = [f"{SHOW}/Season 01/Jfapi Native Show S01E0{n}.mkv" for n in (1, 2)]
LIBRARIES = [(LIBRARY, "movies", PATH), (SHOWS, "tvshows", SHOWS_PATH)]
COLLECTION = "jfapi-native-collection"
PLAYLIST = "jfapi-native-playlist"


def run(t):
    u = t.api.user
    api = t.api
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    remove_libraries(t, LIBRARIES)
    for kind, name in (("BoxSet", COLLECTION), ("Playlist", PLAYLIST)):
        for i in api.get(f"/Items?userId={u}&IncludeItemTypes={kind}&Recursive=true").get("Items", []):
            if i["Name"] == name:
                api.delete(f"/Items/{i['Id']}")
    write_videos(t, [f"{PATH}/{m}/{m}.mkv" for m in MOVIES] + [f"{SHOWS_PATH}/{e}" for e in EPISODES])

    boxset = pl = None
    try:
        lib, native = add_library(t, LIBRARY, "movies", PATH, "Movie", len(MOVIES))
        shows_lib, episodes = add_library(t, SHOWS, "tvshows", SHOWS_PATH, "Episode", len(EPISODES))
        if len(native) != len(MOVIES) or len(episodes) != len(EPISODES):
            return
        show = [i["Id"].lower() for i in api.get(f"/Items?userId={u}&ParentId={shows_lib}&IncludeItemTypes=Series&Recursive=true").get("Items", [])]
        seasons = [i["Id"].lower() for i in api.get(f"/Shows/{show[0]}/Seasons?userId={u}").get("Items", [])] if show else []
        t.equal((len(show), len(seasons)), (1, 1), "native series and season scanned")
        if not seasons:
            return
        t.log("native items, EndDate/PremiereDate:",
              t.db.query("select Name, EndDate, PremiereDate from BaseItems where Path like ? or Path like ?", (PATH + "/%", SHOWS_PATH + "/%")))

        gelato_movie = t.movie()
        boxset = api.post(f"/Collections?name={COLLECTION}&ids={','.join(native + [gelato_movie])}")["Id"].lower()
        pl = api.post("/Playlists", {"Name": PLAYLIST, "Ids": native, "UserId": u, "MediaType": "Video"})["Id"].lower()
        t.wait(2)
        views = {v.get("CollectionType"): v["Id"].lower() for v in api.get(f"/UserViews?userId={u}").get("Items", [])}

        listings = {
            "collections (BoxSet)": (f"/Items?userId={u}&IncludeItemTypes=BoxSet&Recursive=true", [boxset]),
            "playlists (Playlist)": (f"/Items?userId={u}&IncludeItemTypes=Playlist&Recursive=true", [pl]),
            "collection items": (f"/Items?userId={u}&ParentId={boxset}", native + [gelato_movie]),
            "playlist items": (f"/Playlists/{pl}/Items?userId={u}", native),
            "playlist children": (f"/Items?userId={u}&ParentId={pl}", native),
            "native library": (f"/Items?userId={u}&ParentId={lib}", native),
            "native library movies": (f"/Items?userId={u}&ParentId={lib}&IncludeItemTypes=Movie&Recursive=true", native),
            "latest in the native library": (f"/Items/Latest?userId={u}&ParentId={lib}", native),
            "native shows library": (f"/Items?userId={u}&ParentId={shows_lib}", show),
            "series (Series)": (f"/Items?userId={u}&IncludeItemTypes=Series&Recursive=true&Limit=5000", show),
            "seasons of the native series": (f"/Shows/{show[0]}/Seasons?userId={u}", seasons),
            "season children": (f"/Items?userId={u}&ParentId={seasons[0]}", episodes),
            "episodes of the native series": (f"/Shows/{show[0]}/Episodes?userId={u}", episodes),
            "episodes (Episode)": (f"/Items?userId={u}&IncludeItemTypes=Episode&Recursive=true&Limit=5000", episodes),
            "latest episodes": (f"/Items/Latest?userId={u}&ParentId={shows_lib}&IncludeItemTypes=Episode&GroupItems=false", episodes),
            "all items, no types": (f"/Items?userId={u}&Recursive=true&Limit=5000", native + show + episodes + [boxset, pl]),
        }
        if views.get("boxsets"):
            listings["collections view"] = (f"/Items?userId={u}&ParentId={views['boxsets']}", [boxset])
        if views.get("playlists"):
            listings["playlists view"] = (f"/Items?userId={u}&ParentId={views['playlists']}", [pl])

        def listed():
            out = {}
            for label, (path, _) in listings.items():
                d = api.get(path)
                items = d if isinstance(d, list) else d.get("Items", [])
                out[label] = {i["Id"].lower() for i in items}
            return out

        off = listed()
        for label, (_, expect) in listings.items():
            t.check(set(expect) <= off[label], f"filter off, {label}: {len(set(expect) & off[label])}/{len(expect)} expected items")

        api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "FilterUnreleased": True, "FilterUnreleasedBufferDays": 0})
        on = listed()
        for label, (_, expect) in listings.items():
            missing = set(expect) - on[label]
            t.log(f"filter on, {label}: {len(on[label])} items (off: {len(off[label])}), missing {sorted(missing)}")
            t.check(not missing, f"filter on, {label}: {len(set(expect) & on[label])}/{len(expect)} expected items")

        # The filter must still do its job for Gelato items without a known release.
        unreleased = {r[0] for r in t.db.query(
            "select lower(replace(Id,'-','')) from BaseItems where Type like '%Movies.Movie' and (Tags is null or Tags not like '%gelato-stream%') "
            "and Path not like ? and EndDate > datetime('now', '+1 day')", (PATH + "/%",))}
        movies_on = {i["Id"].lower() for i in api.get(f"/Items?userId={u}&IncludeItemTypes=Movie&Recursive=true&Limit=5000").get("Items", [])}
        t.log(f"Gelato movies with a future EndDate: {len(unreleased)}")
        if unreleased:
            t.check(not (unreleased & movies_on), f"filter on: Gelato movies with a future EndDate stay hidden ({len(unreleased & movies_on)} listed)")
    finally:
        api.post(f"/Plugins/{GELATO}/Configuration", {**api.get(f"/Plugins/{GELATO}/Configuration"),
                                                      "FilterUnreleased": cfg.get("FilterUnreleased", False),
                                                      "FilterUnreleasedBufferDays": cfg.get("FilterUnreleasedBufferDays", 0)})
        for i in (boxset, pl):
            if i:
                api.call("DELETE", f"/Items/{i}")
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [PATH, SHOWS_PATH]), 0, "native movies, series and episodes removed again")
