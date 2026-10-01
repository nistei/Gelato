DESCRIPTION = "Library picker: a library's real media folder is never taken for Gelato's, a folder name holding files is skipped, a Gelato folder whose seed went with a shutdown is reused (adds a library, removed again)"
DESTRUCTIVE = True  # adds a library and folders in the container

from jfapi.bootstrap import GELATO

LIB = "Real media jfapi"
REAL = "/tmp/jfapi-real/movies"


def run(t):
    api = t.api

    def library():
        return next((v for v in api.get("/Library/VirtualFolders") if v.get("Name") == LIB), None)

    def gelato_path():
        libs = api.get("/gelato/libraries")["Libraries"]
        return next((l.get("GelatoPath") for l in libs if l["Name"] == LIB), None)

    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    base = (cfg.get("BasePath") or api.get("/gelato/libraries")["DefaultBasePath"]).rstrip("/")
    occupied = f"{base}/real-media-jfapi"
    stray = f"{base}/jfapi-stray-mount"
    created = None
    try:
        # A movies library on a folder with media in it, and a folder of the library's name under
        # the base path that holds a file of someone else's.
        t.sh(f"mkdir -p {REAL} '{occupied}' && echo x > {REAL}/notes.txt && echo x > '{occupied}/notes.txt'")
        if library() is None:
            api.post(f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&collectionType=movies"
                     f"&paths={REAL.replace('/', '%2F')}&refreshLibrary=false",
                     {"LibraryOptions": {"EnableRealtimeMonitor": False}})
        t.equal(gelato_path(), None, "the library's real media folder is not taken for Gelato's")

        # An empty folder under the base path that is not named after the library (a mount point
        # that is not mounted, say) is not Gelato's either.
        t.sh(f"mkdir -p '{stray}'")
        api.call("POST", "/Library/VirtualFolders/Paths?refreshLibrary=false", {"Name": LIB, "Path": stray})
        t.check(stray in library().get("Locations", []), "the library has an empty folder under the base path")
        t.equal(gelato_path(), None, "an empty folder of another name under the base path is not taken for Gelato's")

        # The base path as typed: a relative one is refused, one with detours is saved resolved.
        lib_id = library()["ItemId"]
        t.equal(api.call("POST", f"/gelato/libraries/{lib_id}/folder?basePath=gelato-relative")[0], 400,
                "a relative base path is refused")
        st, d = api.call("POST", f"/gelato/libraries/{lib_id}/folder?basePath=%2Ftmp%2Fjfapi-real%2F..%2Fjfapi-base")
        detour = d.get("Path") if isinstance(d, dict) else None
        t.equal(detour, "/tmp/jfapi-base/real-media-jfapi", "a base path with detours gives a resolved folder")
        for location in library().get("Locations", []):
            if "jfapi-base" in location:
                api.call("DELETE", f"/Library/VirtualFolders/Paths?name={LIB.replace(' ', '%20')}"
                                   f"&path={location.replace('/', '%2F')}&refreshLibrary=false")
        t.sh("rm -rf /tmp/jfapi-base")

        created = api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"]
        t.log("Gelato's folder:", created)
        t.check(created != REAL, "picking the library does not hand out its media folder")
        t.equal(created, f"{occupied}-2", "a folder of the library's name that holds files is skipped")
        t.check(created in library().get("Locations", []), "the new folder is in the library, next to the media folder")
        t.check(REAL in library().get("Locations", []), "the media folder is still in the library")
        t.equal(t.sh(f"ls {REAL}").split(), ["notes.txt"], "nothing was written into the media folder")
        t.equal(t.sh(f"ls '{occupied}'").split(), ["notes.txt"], "nothing was written into the skipped folder")

        # A shutdown takes the seed file out of Gelato's folders; picking the library again still
        # finds the folder instead of creating another one.
        t.sh(f"rm -f '{created}/stub.txt'")
        t.equal(gelato_path(), created, "an empty Gelato folder under the base path is still Gelato's")
        t.equal(api.post(f"/gelato/libraries/{library()['ItemId']}/folder")["Path"], created,
                "picking the library again reuses it")
    finally:
        if library() is not None:
            api.call("DELETE", f"/Library/VirtualFolders?name={LIB.replace(' ', '%20')}&refreshLibrary=false")
        t.sh(f"rm -rf /tmp/jfapi-real /tmp/jfapi-base '{occupied}' '{stray}'" + (f" '{created}'" if created else ""))
