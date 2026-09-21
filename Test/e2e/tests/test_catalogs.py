DESCRIPTION = "Import Catalogs with one small movie catalog: items arrive, no stream rows are created, the limit holds"
DESTRUCTIVE = True  # changes the plugin configuration for the run of the task and restores it

import time

from jfapi.bootstrap import CATALOG_ITEMS, GELATO


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    old = {k: cfg.get(k) for k in ("Catalogs", "CatalogMaxItems")}
    first = next((c for c in cfg.get("Catalogs", []) if c.get("Type") == "movie"), None)
    if first is None:
        t.skip("no movie catalog configured")
    counts = lambda: (
        t.db.one("select count(*) from BaseItems where Type like '%Movies.Movie' and (Tags is null or Tags not like '%gelato-stream%')")[0],
        t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%'")[0])
    before = counts()
    cfg["Catalogs"] = [{**first, "Enabled": True, "MaxItems": CATALOG_ITEMS}]
    cfg["CatalogMaxItems"] = CATALOG_ITEMS
    t.api.post(f"/Plugins/{GELATO}/Configuration", cfg)
    try:
        status, msg = t.api.run_task("GelatoCatalogItemsSync", timeout=1800)
        t.equal(status, "Completed", f"import of {first.get('Name')} {msg}")
        time.sleep(5)
        t.api.wait_tasks_idle("RefreshLibrary", 1800)
        after = counts()
        t.log(f"movies {before[0]} -> {after[0]}, rows {before[1]} -> {after[1]}")
        t.check(after[0] >= before[0], "no movie was lost")
        t.check(after[0] - before[0] <= CATALOG_ITEMS, f"at most {CATALOG_ITEMS} movies added")
        t.equal(after[1], before[1], "the import created no stream rows")
        # Gelato keeps items whose release is still ahead out of every listing (FilterUnreleased), so
        # they are in the database and not in this answer. Earlier tests insert titles from search,
        # and an unreleased one among them made this check fail with a count one too low.
        unreleased = t.db.one(
            "select count(*) from BaseItems where Type like '%Movies.Movie' and (Tags is null or Tags not like '%gelato-stream%') "
            "and PrimaryVersionId is null and EndDate > datetime('now', '+1 day')")[0]
        listed = t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=Movie&Recursive=true&Limit=5000")
        t.log(f"{after[0]} movies in the database, {unreleased} of them unreleased")
        t.equal(listed.get("TotalRecordCount"), after[0] - unreleased,
                "the library lists every released movie once, no rows")
    finally:
        cfg.update(old)
        t.api.post(f"/Plugins/{GELATO}/Configuration", cfg)
