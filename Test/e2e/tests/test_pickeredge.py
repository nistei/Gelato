DESCRIPTION = "Library picker at its edges: a saved base path that is not absolute breaks neither the library list nor a search inside a library, a location that only starts with the library's name is not Gelato's, and a music library gets no Gelato folder (adds two libraries, removed again)"
DESTRUCTIVE = True  # changes the plugin configuration, adds libraries and folders in the container

from jfapi.bootstrap import GELATO

LIB, MUSIC = "Edge movies jfapi", "Edge music jfapi"


def run(t):
    api = t.api
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old_base = cfg.get("BasePath")
    base = (old_base or api.get("/gelato/libraries")["DefaultBasePath"]).rstrip("/")
    lookalike = f"{base}/edge-movies-jfapi-old"
    expected = f"{base}/edge-movies-jfapi"

    def library(name):
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == name), None)

    def gelato_path():
        st, d = api.call("GET", "/gelato/libraries")
        return next((l.get("GelatoPath") for l in d["Libraries"] if l["Name"] == LIB), None) if st == 200 else f"HTTP {st}"

    made = []
    try:
        # A movies library whose only folder is empty, under the base path, and named like the
        # library with something appended: an old folder, a mount point.
        t.sh(f"mkdir -p '{lookalike}'")
        if library(LIB) is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=movies"
                     f"&paths={lookalike.replace('/', '%2F')}&refreshLibrary=false", {"LibraryOptions": {"EnableRealtimeMonitor": False}})
        lib_id = library(LIB)["ItemId"]

        # The base path as the settings page saves it: whatever was typed.
        api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "BasePath": "gelato-relative"})
        t.equal(api.call("GET", "/gelato/libraries")[0], 200, "the library list is answered with a relative base path saved")
        st, _ = api.call("GET", f"/Items?userId={api.user}&parentId={lib_id}&searchTerm=heretic&IncludeItemTypes=Movie&Recursive=true&Limit=5")
        t.equal(st, 200, "a search inside a library is answered with a relative base path saved")
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)

        t.equal(gelato_path(), None, "a location that only starts with the library's name is not taken for Gelato's")
        st, d = api.call("POST", f"/gelato/libraries/{lib_id}/folder")
        got = d.get("Path") if isinstance(d, dict) else None
        made.append(got)
        t.equal(got, expected, "picking the library gives it a folder of its own name")
        t.equal(t.sh(f"ls -A '{lookalike}'").split(), [], "nothing was written into the other folder")

        # Gelato has movies and series: a music library is none of its business.
        if library(MUSIC) is None:
            api.post(f"/Library/VirtualFolders?name={MUSIC.replace(' ', '%20')}&collectionType=music&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
        st, d = api.call("POST", f"/gelato/libraries/{library(MUSIC)['ItemId']}/folder")
        made.append(d.get("Path") if isinstance(d, dict) else None)
        t.equal(st, 400, "a music library is refused a Gelato folder")
        t.equal(library(MUSIC).get("Locations", []), [], "and gets no folder")
    finally:
        cfg["BasePath"] = old_base
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        for name in (LIB, MUSIC):
            if library(name) is not None:
                api.call("DELETE", f"/Library/VirtualFolders?name={name.replace(' ', '%20')}&refreshLibrary=false")
        t.sh("rm -rf " + " ".join(f"'{p}'" for p in {lookalike, expected, *(m for m in made if m and m.startswith(base + "/edge-"))}))
