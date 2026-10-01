DESCRIPTION = "Catalog and library settings are the administrator's: another user can neither set a catalog's folder, nor start an import, nor read or create library folders"

from jfapi.bootstrap import GELATO


def run(t):
    api, u2 = t.api, t.user2
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    cat = next(iter(cfg.get("Catalogs", [])), None)
    t.require(cat, "no catalog configured")
    path = f"/gelato/catalogs/{cat['Id']}/{cat['Type']}"
    try:
        st, _ = u2.call("POST", f"{path}/config", {**cat, "Path": "/tmp/gelato/not-yours"})
        t.equal(st, 403, "a user cannot change a catalog's settings")
        saved = next(c for c in api.get(f"/Plugins/{GELATO}/Configuration")["Catalogs"]
                     if (c["Id"], c["Type"]) == (cat["Id"], cat["Type"]))
        t.equal(saved.get("Path") or "", cat.get("Path") or "", "the catalog's folder is unchanged")
        api.search("gelato")  # a request, which is when Gelato would seed a catalog's folder
        t.check("not-yours" not in t.sh("ls /tmp/gelato"), "no folder was created for it")
        t.equal(u2.call("POST", f"{path}/import")[0], 403, "a user cannot start a catalog import")
        t.equal(u2.call("GET", "/gelato/catalogs")[0], 403, "a user cannot list the catalogs")
        t.equal(u2.call("GET", "/gelato/libraries")[0], 403, "a user cannot list the libraries' folders")
        lib = api.get("/Library/VirtualFolders")[0]["ItemId"]
        t.equal(u2.call("POST", f"/gelato/libraries/{lib}/folder")[0], 403, "a user cannot have a library folder created")
        t.equal(api.call("GET", "/gelato/catalogs")[0], 200, "the administrator still can")
    finally:
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
        t.sh("rm -rf /tmp/gelato/not-yours")
