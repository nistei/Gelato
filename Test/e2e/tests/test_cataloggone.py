DESCRIPTION = "A catalog the addon no longer lists keeps its settings: its library pick survives loading the catalog list, the list itself does not show it, and one without settings is dropped"
DESTRUCTIVE = True  # changes the plugin configuration and restores it

from jfapi.bootstrap import GELATO


def run(t):
    api = t.api
    cfg = api.get(f"/Plugins/{GELATO}/Configuration")
    old = cfg.get("Catalogs")
    # The movie folder: a path that is in a library already, so nothing is created for it.
    gone = {"Id": "jfapi-gone", "Type": "movie", "Name": "Gone", "Enabled": True, "MaxItems": 5,
            "CreateCollection": False, "Url": "", "Path": cfg.get("MoviePath")}
    blank = {**gone, "Id": "jfapi-blank", "Name": "Blank", "Enabled": False, "Path": ""}
    find = lambda catalogs, cid: next((c for c in catalogs if c.get("Id") == cid), None)
    try:
        api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "Catalogs": (old or []) + [gone, blank]})
        listed = api.get("/gelato/catalogs")  # what the Catalogs tab and the import load
        t.check(listed, "the addon's catalogs are listed")
        t.equal(find(listed, "jfapi-gone"), None, "a catalog the addon does not have is not listed")
        saved = api.get(f"/Plugins/{GELATO}/Configuration")["Catalogs"]
        kept = find(saved, "jfapi-gone")
        t.check(kept is not None, "its settings are still in the configuration")
        if kept:
            t.equal(kept.get("Path"), gone["Path"], "with its library's folder")
            t.check(kept.get("Enabled"), "and still enabled")
        t.equal(find(saved, "jfapi-blank"), None, "one without settings is dropped as before")
        for c in old or []:
            t.check(find(saved, c["Id"]) is not None, f"catalog {c.get('Name')} is still configured")
    finally:
        cfg["Catalogs"] = old
        api.post(f"/Plugins/{GELATO}/Configuration", cfg)
