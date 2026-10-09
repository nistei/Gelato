"""What a change of build does to a library that was set up before it: nothing may be lost.

Two runs on one throwaway copy of a real instance, the build swapped in between:

    python tools/upgrade.py prep  --container jf-up --adminuser nistei --scenario custom
    (deploy the other build, restart)
    python tools/upgrade.py check --container jf-up --adminuser nistei --scenario custom

or both with the swap, on a container:

    python tools/upgrade.py run --container jf-up --adminuser nistei --scenario custom --old old/Gelato.dll --new new/Gelato.dll

`prep` reconfigures the instance the way an install of some age looks (the scenario), puts titles
in (search, a catalog import, stream syncs, a played and a favourite one) and records every item with
its folder, every provider id and all user data in `.cache/upgrade-<scenario>.json`. `check` compares
after the restart, after a scan, after stream syncs, after the catalogs were imported again and after
new titles were opened. It reports what is gone, what is in another folder and what a library counts.

Scenarios:
- `custom`    movie and series folders of the admin's own choosing, a folder of its own for the second
              user, local files in a library of their own, the folders used before left as libraries
- `mixed`     Gelato's folders are the folders the local files are in; the second user's is a folder
              inside a library's folder
- `single`    one mixed library for everything: movie and series folder are the same folder
- `catalog`   (for a way back to an older build) a catalog with a library of its own, its titles
              moved there

`--same-build` on `check` says the build did not change (a control run: what a restart alone does)
or is one without the library endpoints, and leaves out what only the newer build answers.
`--win <folder>` instead of `--container` takes a Windows instance of dev/jf.py by its folder (`_<name>`
beside the repository), with `--url`: the database file and the log are read in place, shell commands
run in Git Bash.

Local series are extended when "Extend local series trees" is on, and one that is also a Gelato
series takes that series' episodes: that is what a restart does on any build, and shows as moves.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import jfapi.native as native  # noqa: E402
from jfapi import bootstrap  # noqa: E402
from jfapi.api import Api  # noqa: E402
from jfapi.bootstrap import GELATO  # noqa: E402
from jfapi.db import Db  # noqa: E402
from jfapi.native import add_library, library_id, write_videos  # noqa: E402
from jfapi.testing import Context, make_user2  # noqa: E402

MATRIX = "The Matrix (1999) [imdbid-tt0133093]"
INCEPTION = "Inception (2010) [imdbid-tt1375666]"
SHOW = "Breaking Bad (2008) [imdbid-tt0903747]"

# libs: (name, kind, path, local movies, local series). movie/series: Gelato's folders, None for
# the configured ones. user2: the second user's own movie folder and the only library that user
# may open. {r} is the media root of the instance.
SCENARIOS = {
    "custom": dict(
        libs=[("Stremio Filme", "movies", "{r}/media/stremio-filme", [], []),
              ("Stremio Serien", "tvshows", "{r}/media/Stremio Serien", [], []),
              ("Kids", "movies", "{r}/kids/k-movies", [], []),
              ("Lokal", "movies", "{r}/local/filme", [MATRIX], [])],
        movie="{r}/media/stremio-filme", series="{r}/media/Stremio Serien",
        user2=("{r}/kids/k-movies", "Kids")),
    "mixed": dict(
        libs=[("Lokale Filme", "movies", "{r}/media-local/movies", [MATRIX], []),
              ("Lokale Serien", "tvshows", "{r}/media-local/shows", [], [SHOW]),
              ("Gross", "movies", "{r}/media-local/big", [INCEPTION], [])],
        movie="{r}/media-local/movies", series="{r}/media-local/shows",
        user2=("{r}/media-local/big/gelato", "Gross")),
    "single": dict(
        libs=[("Alles", "mixed", "{r}/data/all", [MATRIX], [SHOW])],
        movie="{r}/data/all", series="{r}/data/all", user2=None),
    "catalog": dict(libs=[], movie=None, series=None, user2=None, catalog_library="Katalogfilme"),
}

MOVIE_TERMS = {"prep": ["Heretic", "Nosferatu", "Anora"], "prep2": ["Conclave", "Longlegs"],
               "check": ["The Substance", "Civil War", "Flow"], "check2": ["Challengers", "Wicked", "Gladiator II"]}
SERIES_TERMS = {"prep": ["Adolescence", "Chernobyl"], "check": ["Baby Reindeer", "Ripley", "Beef"]}
KEYS = ("MoviePath", "SeriesPath", "EnableMixed", "UserConfigs")

norm = lambda g: (g or "").replace("-", "").lower() or None
fs = lambda p: p.replace("\\", "/")  # a path for the shell


class WinDb:
    """jfapi's Db for a Windows instance of dev/jf.py: the database file read in place, shell
    commands in Git Bash with /config standing for the instance's data folder."""

    def __init__(self, config_dir):
        self.config = config_dir
        self.bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        self.seed = None

    def invalidate(self):
        pass

    def query(self, sql, params=(), con=None):
        for attempt in range(5):
            try:
                c = sqlite3.connect("file:" + fs(os.path.join(self.config, "data", "jellyfin.db")) + "?mode=ro", uri=True, timeout=30)
                try:
                    return c.execute(sql, params).fetchall()
                finally:
                    c.close()
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(1)

    def one(self, sql, params=(), con=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def sh(self, cmd):
        cmd = cmd.replace("/config/", fs(self.config) + "/")
        return subprocess.run([self.bash, "-c", cmd], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

    def db_stat(self):
        d = os.path.join(self.config, "data")
        return [[f, os.stat(os.path.join(d, f)).st_size, os.stat(os.path.join(d, f)).st_mtime_ns]
                for f in ("jellyfin.db", "jellyfin.db-wal", "jellyfin.db-shm") if os.path.exists(os.path.join(d, f))]


def snapshot(t):
    t.db.invalidate()
    items = {r[0]: list(r[1:]) for r in t.db.query(
        "select lower(replace(Id,'-','')), Type, Path, lower(replace(ParentId,'-','')), lower(replace(TopParentId,'-','')), "
        "lower(replace(PrimaryVersionId,'-','')), coalesce(Tags,'') like '%gelato-stream%' from BaseItems")}
    providers = sorted(f"{r[0]}|{r[1]}|{r[2]}" for r in t.db.query(
        "select lower(replace(ItemId,'-','')), ProviderId, ProviderValue from BaseItemProviders"))
    userdata = sorted(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5]}" for r in t.db.query(
        "select lower(replace(ItemId,'-','')), lower(replace(UserId,'-','')), CustomDataKey, Played, IsFavorite, PlaybackPositionTicks "
        "from UserData"))
    return {"items": items, "providers": providers, "userdata": userdata}


def counts(t):
    out = {}
    for v in t.api.get("/Library/VirtualFolders"):
        for kind in ("Movie", "Series", "Episode"):
            d = t.api.get(f"/Items?userId={t.api.user}&ParentId={v['ItemId']}&Recursive=true&IncludeItemTypes={kind}&Limit=0")
            out[f"{v['Name']}/{kind}"] = d.get("TotalRecordCount")
    return out


def diff(t, label, before, after, moves_ok=False):
    """Compares two snapshots: items that are gone, items in another folder, lost ids and user
    data. Returns the moves of anything but stream rows, which come and go with every sync."""
    b, a = before["items"], after["items"]
    path = lambda snap, i: (snap["items"].get(i) or [None, None])[1]
    kind = lambda row: ("row " if row[5] else "") + row[0].split(".")[-1]
    gone, moved, new = {}, {}, {}
    for i in b:
        if i not in a:
            gone[kind(b[i])] = gone.get(kind(b[i]), 0) + 1
        elif (b[i][2], b[i][3]) != (a[i][2], a[i][3]):
            key = (kind(b[i]), path(before, b[i][2]), path(after, a[i][2]))
            moved[key] = moved.get(key, 0) + 1
    for i in a:
        if i not in b:
            k = f"{kind(a[i])} in {path(after, a[i][2])}"
            new[k] = new.get(k, 0) + 1
    t.log(f"-- {label}: {len(b)} -> {len(a)} items, gone {gone}, new {dict(sorted(new.items())) if len(new) < 12 else len(new)}")
    for (k, src, dst), n in sorted(moved.items(), key=lambda kv: -kv[1])[:40]:
        t.log(f"   moved: {n} x {k}: {src} -> {dst}")
    kept = {k: n for k, n in gone.items() if not k.startswith("row ")}
    t.check(not kept, f"{label}: no title, season, episode or folder is gone ({kept})")
    real_moves = {k: n for k, n in moved.items() if not k[0].startswith("row ")}
    if not moves_ok:
        t.check(not real_moves, f"{label}: nothing is in another folder than before ({sum(real_moves.values())} items)")
    bp, ap = set(before["providers"]), set(after["providers"])
    lost = [p for p in bp - ap if p.split("|")[0] in a and not b.get(p.split("|")[0], [0] * 6)[5]]
    added = {}
    for p in ap - bp:
        if p.split("|")[0] in b:
            added[p.split("|")[1]] = added.get(p.split("|")[1], 0) + 1
    t.log(f"   provider ids: {len(lost)} gone from titles that are still there, added to old items {added}")
    bu, au = set(before["userdata"]), set(after["userdata"])
    lost_ud = [u for u in bu - au if u.split("|")[0] in a]
    t.check(not lost_ud, f"{label}: no watch state or favourite is lost or changed ({len(lost_ud)}: {lost_ud[:3]})")
    return real_moves


def parent_path(t, item_id):
    r = t.db.one("select (select Path from BaseItems f where f.Id=b.ParentId) from BaseItems b where lower(replace(b.Id,'-',''))=?",
                 (norm(item_id),))
    return r[0] if r else None


def rows_of(t, item_id):
    """[count, folders] of the item's stream rows."""
    rows = t.db.query("select (select Path from BaseItems f where f.Id=b.ParentId) from BaseItems b "
                      "where lower(replace(b.PrimaryVersionId,'-',''))=? and b.Tags like '%gelato-stream%'", (norm(item_id),))
    return [len(rows), sorted({r[0] for r in rows})]


def open_new(t, api, terms, kind, scope=None):
    """Opens the first search hit that is not in the library: (item id, name) or (None, None)."""
    in_library = lambda s: t.db.one(
        "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (s,))[0]
    for term in terms:
        if scope:
            st, d = api.call("GET", f"/Items?userId={api.user}&searchTerm={term.replace(' ', '%20')}&Recursive=true&Limit=8&Fields=Path"
                                    f"&parentId={scope}&IncludeItemTypes={kind}")
            hits = d.get("Items", []) if st == 200 else []
        else:
            hits = api.search(term, kind, limit=8)
        for hit in hits:
            m = re.search(r"(tt\d+)", hit.get("Path") or "")
            if m and (hit.get("Path") or "").startswith("gelato://stub/") and not in_library(m.group(1)):
                st, d = api.call("GET", f"/Items/{hit['Id']}?userId={api.user}&Fields=Path")
                if st == 200 and d.get("Id"):
                    t.api.settle_insert()
                    return norm(d["Id"]), d.get("Name")
                t.log(f"   opening {hit.get('Name')} answered {st}")
    return None, None


def scoped(t, api, lib, kind, term="star"):
    """What a search inside the library answers with: (status, addon results, library items)."""
    st, d = api.call("GET", f"/Items?userId={api.user}&searchTerm={term}&Recursive=true&Limit=30&Fields=Path"
                            f"&parentId={lib}&IncludeItemTypes={kind}")
    items = d.get("Items", []) if st == 200 and isinstance(d, dict) else []
    stubs = sum(1 for i in items if (i.get("Path") or "").startswith("gelato://stub/"))
    return st, stubs, len(items) - stubs


def search_shape(t, apis):
    """Per user, library and kind: the status, and whether the addon answers."""
    out = {}
    for who, api in apis.items():
        for v in t.api.get("/Library/VirtualFolders"):
            for kind in ("Movie", "Series"):
                st, stubs, own = scoped(t, api, v["ItemId"], kind)
                out[f"{who}/{v['Name']}/{kind}"] = [st, stubs > 0]
        for kind in ("Movie", "Series"):
            hits = api.search("star", kind, limit=30)
            out[f"{who}/everything/{kind}"] = [200, any((h.get("Path") or "").startswith("gelato://stub/") for h in hits)]
    return out


def local_ids(t, sc):
    out = {}
    for name, kind, path, movies, shows in sc["libs"]:
        for m in movies:
            r = t.db.one("select lower(replace(Id,'-','')) from BaseItems where Path like ? and Type like '%Movies.Movie'", (f"{path}{SEP}{m}{SEP}%",))
            out[m] = r[0] if r else None
        for s in shows:
            r = t.db.one("select lower(replace(Id,'-','')) from BaseItems where Path=? and Type like '%TV.Series'", (f"{path}{SEP}{s}",))
            out[s] = r[0] if r else None
    return out


def restrict(t, user_id, library):
    user = t.api.get(f"/Users/{user_id}")
    t.api.post(f"/Users/{user_id}/Policy", {**user["Policy"], "EnableAllFolders": False, "EnabledFolders": [library_id(t, library)]})


def user2_opens_old(t, u2, old_folder, taken):
    """The second user, who cannot open the library of the folder used before, searches a title
    that folder holds and opens the hit."""
    row = next((r for r in t.db.query(
        "select lower(replace(b.Id,'-','')), p.ProviderValue from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and p.ProviderId='Imdb' "
        "where b.Type like '%Movies.Movie' and b.Path like 'gelato://%' and (b.Tags is null or b.Tags not like '%gelato-stream%') "
        "and b.ParentId=(select Id from BaseItems where Path=? and Type like '%.Folder') order by b.Id limit 40", (old_folder,))
        if r[0] not in taken), None)
    if not row:
        return None
    item, imdb = row
    before = parent_path(t, item)
    hits = u2.search(imdb, "Movie", limit=5)
    if not hits:
        return {"item": item, "hits": 0}
    st, d = u2.call("GET", f"/Items/{hits[0]['Id']}?userId={u2.user}&Fields=Path")
    t.api.settle_insert()
    return {"item": item, "hits": len(hits), "status": st, "same_item": (norm(d.get("Id")) if st == 200 else None) == item,
            "stays": parent_path(t, item) == before}


def scan(t, label):
    status, msg = t.api.run_task("RefreshLibrary", timeout=1800)
    t.equal(status, "Completed", f"library scan {label} {msg}")
    t.settle(after=2, timeout=180)


def enabled_catalogs(t, cfg):
    """One enabled movie and one series catalog; a series catalog is enabled when none is."""
    if not any(c.get("Enabled") and c["Type"] == "series" for c in cfg.get("Catalogs") or []):
        first = next((c for c in cfg.get("Catalogs") or [] if c["Type"] == "series"), None)
        if first:
            first["Enabled"], first["MaxItems"] = True, 2
            t.api.post(f"/Plugins/{GELATO}/Configuration", cfg)
            cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    cats = [c for c in cfg.get("Catalogs") or [] if c.get("Enabled")]
    return cfg, [c for c in (next((c for c in cats if c["Type"] == k), None) for k in ("movie", "series")) if c]


def prep(t, sc, state_file):
    u1 = t.api
    cfg = u1.get(f"/Plugins/{GELATO}/Configuration")
    state = {"old_movie": cfg["MoviePath"], "old_series": cfg["SeriesPath"]}
    movie_path, series_path = sc["movie"] or cfg["MoviePath"], sc["series"] or cfg["SeriesPath"]
    for name, kind, path, movies, shows in sc["libs"]:
        t.sh(f"mkdir -p '{fs(path)}'")
        files = [f"{path}/{m}/{m}.mkv" for m in movies] + [f"{path}/{s}/Season 01/{s.split(' (')[0]} S01E01.mkv" for s in shows]
        if files:
            write_videos(t, [fs(f) for f in files])
    u2 = t.user2 if sc["user2"] else None
    if sc["libs"]:
        cfg["MoviePath"], cfg["SeriesPath"], cfg["EnableMixed"] = movie_path, series_path, True
        if u2:
            t.sh(f"mkdir -p '{fs(sc['user2'][0])}'")
            cfg["UserConfigs"] = [{"UserId": u2.user, "Url": cfg["Url"], "MoviePath": sc["user2"][0], "SeriesPath": series_path, "DisableSearch": False}]
        u1.post(f"/Plugins/{GELATO}/Configuration", cfg)
        for api in filter(None, (u1, u2)):
            api.search("star")  # seeds the folders through Gelato's lookup
        for name, kind, path, movies, shows in sc["libs"]:
            add_library(t, name, kind, path, "Series" if shows and not movies else "Movie", len(movies) or len(shows))
        scan(t, "with the new libraries")
        paths = {movie_path, series_path} | ({sc["user2"][0]} if u2 else set())
        t.check(t.folders_ready(*paths), f"Gelato's folders are items: {sorted(paths)}")
    state["local"] = local_ids(t, sc)

    movie, name = open_new(t, u1, MOVIE_TERMS["prep"], "Movie")
    t.check(movie and parent_path(t, movie) == movie_path, f"a movie opened from search is in the movie folder: {name} in {parent_path(t, movie) if movie else None}")
    series, sname = open_new(t, u1, SERIES_TERMS["prep"], "Series")
    t.check(series and parent_path(t, series) == series_path, f"a series opened from search is in the series folder: {sname} in {parent_path(t, series) if series else None}")
    state.update(movie=movie, series=series)
    if movie:
        n = len(u1.sources(movie))
        state["movie_rows"] = rows_of(t, movie)
        t.log(f"the movie has {n} sources, rows {state['movie_rows']}")
        u1.mark_played(movie)
        u1.post(f"/UserFavoriteItems/{movie}?userId={u1.user}")
    if series:
        eps = u1.get(f"/Shows/{series}/Episodes?userId={u1.user}&season=1").get("Items", [])
        if eps:
            state["episode"] = norm(eps[0]["Id"])
            u1.mark_played(eps[0]["Id"])
            u1.sources(eps[0]["Id"])
            state["episode_rows"] = rows_of(t, eps[0]["Id"])
    for title, item in state["local"].items():
        if item and "Movies" in (t.db.one("select Type from BaseItems where lower(replace(Id,'-',''))=?", (item,)) or [""])[0]:
            n = len(u1.sources(item))
            state.setdefault("local_rows", {})[title] = [n, rows_of(t, item), parent_path(t, item)]
            t.log(f"local movie {title}: {n} sources, rows {rows_of(t, item)}, its folder {parent_path(t, item)}")
    if u2:
        m2, n2 = open_new(t, u2, MOVIE_TERMS["prep2"], "Movie")
        t.check(m2 and parent_path(t, m2) == sc["user2"][0], f"{u2.name}'s movie is in that user's folder: {n2} in {parent_path(t, m2) if m2 else None}")
        state["movie2"] = m2
        restrict(t, u2.user, sc["user2"][1])
        state["restricted_open"] = user2_opens_old(t, u2, state["old_movie"], set())
        t.log("the restricted user opens a title of the folder used before:", state["restricted_open"])

    cfg, state_catalogs = enabled_catalogs(t, u1.get(f"/Plugins/{GELATO}/Configuration"))
    state["catalogs"] = [{"Id": c["Id"], "Type": c["Type"], "Name": c.get("Name")} for c in state_catalogs]
    if sc.get("catalog_library"):
        # A library of its own for the movie catalog, the way the settings page makes one.
        folder = u1.post(f"/gelato/libraries/folder?name={sc['catalog_library']}")["Path"]
        add_library(t, sc["catalog_library"], "movies", folder, "Movie", 0)
        scan(t, "with the catalog's library")
        t.check(t.folders_ready(folder), f"the catalog's folder is an item: {folder}")
        cfg = u1.get(f"/Plugins/{GELATO}/Configuration")
        next(c for c in cfg["Catalogs"] if c["Type"] == "movie" and c["Id"] == state["catalogs"][0]["Id"])["Path"] = folder
        u1.post(f"/Plugins/{GELATO}/Configuration", cfg)
        state["catalog_folder"] = folder
    for c in state["catalogs"]:
        t.check(t.import_catalog(c), f"catalog {c['Name']} ({c['Type']}) imported")
    if sc.get("catalog_library"):
        inside = t.db.query("select lower(replace(b.Id,'-','')) from BaseItems b where b.Type like '%Movies.Movie' and b.Path like 'gelato://%' "
                            "and (b.Tags is null or b.Tags not like '%gelato-stream%') and b.ParentId=(select Id from BaseItems where Path=?)", (state["catalog_folder"],))
        t.check(len(inside) >= 1, f"the catalog's movies are in its folder: {len(inside)}")
        state["catalog_movie"] = inside[0][0] if inside else None
        if inside:
            n = len(u1.sources(inside[0][0]))
            u1.mark_played(inside[0][0])
            state["catalog_movie_rows"] = rows_of(t, inside[0][0])
            t.log(f"a movie of the catalog has {n} sources, rows {state['catalog_movie_rows']}")
    scan(t, "after the import")

    cfg = u1.get(f"/Plugins/{GELATO}/Configuration")
    apis = {"admin": u1, **({"user2": u2} if u2 else {})}
    state["search"] = search_shape(t, apis)
    state["counts"] = counts(t)
    state["config"] = {k: cfg.get(k) for k in KEYS}
    state["catalog_config"] = sorted(f"{c['Type']}:{c['Id']}:{c.get('Enabled')}:{c.get('CreateCollection')}" for c in cfg.get("Catalogs") or [])
    state["snapshot"] = snapshot(t)
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as h:
        json.dump(state, h)
    t.log("counts:", state["counts"])
    t.log(f"recorded {len(state['snapshot']['items'])} items in {state_file}")


def check(t, sc, state_file, same_build):
    u1 = t.api
    with open(state_file, encoding="utf-8") as h:
        state = json.load(h)
    u2 = t.user2 if sc["user2"] else None
    base = state["snapshot"]
    cfg = u1.get(f"/Plugins/{GELATO}/Configuration")
    movie_path, series_path = sc["movie"] or cfg["MoviePath"], sc["series"] or cfg["SeriesPath"]

    t.log("== right after the start")
    diff(t, "after the restart", base, snapshot(t))
    now = {k: cfg.get(k) for k in KEYS}
    t.equal(now, {k: state["config"][k] for k in KEYS}, "the folders of the configuration are the saved ones")
    t.equal(sorted(f"{c['Type']}:{c['Id']}:{c.get('Enabled')}:{c.get('CreateCollection')}" for c in cfg.get("Catalogs") or []),
            state["catalog_config"], "the catalogs are the saved ones")
    libs = []
    if not same_build:
        t.log("== the settings page: libraries, and what a save without a change stores")
        res = u1.get("/gelato/libraries")
        libs = res.get("Libraries") or []
        t.log("default Gelato folder:", res.get("DefaultBasePath"))
        for lib in libs:
            t.log(f"   {lib['Name']} ({lib.get('CollectionType')}): {lib.get('Locations')} Gelato's: {lib.get('GelatoPath')}")
        saved = [("movie folder", cfg["MoviePath"]), ("series folder", cfg["SeriesPath"])] + [
            (f"user {u['UserId'][:8]} {k}", u[k]) for u in cfg.get("UserConfigs") or [] for k in ("MoviePath", "SeriesPath")]
        for label, p in saved:
            lib = next((l for l in libs if p in (l.get("Locations") or [])), None)
            if lib:  # the page shows the library, and a save stores the path the library holds
                t.equal(lib.get("GelatoPath"), p, f"{label} {p}: library {lib['Name']}, whose Gelato folder is the saved one")
            else:
                inside = next((l["Name"] for l in libs for loc in l.get("Locations") or [] if fs(p).startswith(fs(loc).rstrip("/") + "/")), None)
                t.log(f"   {label} {p}: " + (f"a folder inside library {inside}" if inside else "in no library") + ", saved as it is")
        u1.post(f"/Plugins/{GELATO}/Configuration", cfg)  # what Save stores when nothing was picked
        t.wait(1)
        after_save = u1.get(f"/Plugins/{GELATO}/Configuration")
        t.equal({k: after_save.get(k) for k in KEYS}, now, "a save keeps the folders")

    t.log("== a library scan")
    scan(t, "")
    diff(t, "after a scan", base, snapshot(t))
    now_counts = counts(t)
    t.check(now_counts == state["counts"], f"every library lists as many movies, series and episodes as before "
            f"({ {k: (state['counts'].get(k), v) for k, v in now_counts.items() if state['counts'].get(k) != v} })")
    apis = {"admin": u1, **({"user2": u2} if u2 else {})}
    shape = search_shape(t, apis)
    changed = {k: (state["search"].get(k), v) for k, v in shape.items() if state["search"].get(k) != v}
    t.check(not changed, f"a search inside each library and of everything is answered by the addon where it was before ({changed})")

    t.log("== streams of what was there")
    for key, label in (("movie", "movie"), ("episode", "episode"), ("catalog_movie", "catalog's movie")):
        if state.get(key):
            n = len(u1.sources(state[key]))
            rows = rows_of(t, state[key])
            t.check(n >= 2 and rows[1] == state[key + "_rows"][1], f"the {label} has its streams, in the folder they were in: {n} sources, rows {rows}, before {state[key + '_rows']}")
            t.check(u1.user_data(state[key])["Played"], f"the {label} is still played")
            if key != "episode":
                st, d = u1.call("POST", f"/Items/{state[key]}/PlaybackInfo?userId={u1.user}", {"UserId": u1.user})
                t.check(st == 200 and d.get("MediaSources"), f"PlaybackInfo of the {label}: {st}")
    if state.get("movie"):
        t.check(u1.user_data(state["movie"])["IsFavorite"], "the movie is still a favourite")
    for title, (n0, rows0, folder0) in (state.get("local_rows") or {}).items():
        item = state["local"][title]
        n = len(u1.sources(item))
        rows = rows_of(t, item)
        t.equal(n, n0, f"the local movie {title} lists as many sources as before")
        t.log(f"   its rows: {rows0[1]} -> {rows[1]} (a local movie's rows follow it into its folder), its folder {folder0} -> {parent_path(t, item)}")
    synced = snapshot(t)
    diff(t, "after the stream syncs", base, synced)

    t.log("== the catalogs are imported again")
    for c in state["catalogs"]:
        t.check(t.import_catalog(c), f"catalog {c['Name']} ({c['Type']}) imported")
        t.log("   " + (t.sh("cat /config/log/*.log | grep 'processed .* items' | tail -1").strip()[-230:]))
    scan(t, "after the import")
    moves = diff(t, "after the import", synced, snapshot(t), moves_ok=True)
    t.log(f"   the import moved {sum(moves.values())} titles (those a catalog lists that sat in a folder used before go to the configured one)")
    now_counts = counts(t)
    t.log("   counts that differ from before:", {k: (state["counts"].get(k), v) for k, v in now_counts.items() if state["counts"].get(k) != v})

    t.log("== new titles go where they went before")
    movie, name = open_new(t, u1, MOVIE_TERMS["check"], "Movie")
    t.check(movie and parent_path(t, movie) == movie_path, f"a movie opened from search is in the movie folder: {name} in {parent_path(t, movie) if movie else None}")
    if movie:
        n = len(u1.sources(movie))
        t.check(n >= 2 and rows_of(t, movie)[1] == [movie_path], f"and has streams, their rows next to it: {n}, {rows_of(t, movie)}")
    series, sname = open_new(t, u1, SERIES_TERMS["check"], "Series")
    t.check(series and parent_path(t, series) == series_path, f"a series opened from search is in the series folder: {sname} in {parent_path(t, series) if series else None}")
    if u2:
        m2, n2 = open_new(t, u2, MOVIE_TERMS["check2"], "Movie")
        t.check(m2 and parent_path(t, m2) == sc["user2"][0], f"{u2.name}'s movie is in that user's folder: {n2} in {parent_path(t, m2) if m2 else None}")
        if state.get("movie2"):
            st, d = u2.call("GET", f"/Items/{state['movie2']}?userId={u2.user}&Fields=Path")
            t.check(st == 200 and len(d.get("MediaSources") or []) >= 1, f"{u2.name} opens the movie from before: {st}")
        first = state.get("restricted_open")
        again = user2_opens_old(t, u2, state["old_movie"], {(first or {}).get("item")})
        if first and again:
            keys = ("hits", "status", "same_item", "stays")
            t.equal({k: again.get(k) for k in keys}, {k: first.get(k) for k in keys},
                    "a user who cannot open the library of the folder used before gets a title it holds as before")

    t.log("== a last scan")
    before_scan = snapshot(t)
    scan(t, "at the end")
    diff(t, "after the last scan", before_scan, snapshot(t))
    t.log("error lines in the log:")
    t.log(t.sh("cat /config/log/*.log | grep '\\[ERR\\]' | sed -E 's#https?://[^\" ]+#<url>#g' | cut -c1-240 | cut -d' ' -f4- | sort | uniq -c | sort -rn | head -12"))


def deploy(container, dll):
    """Copies the build into the container's Gelato folder and restarts it."""
    folder = subprocess.run(["docker", "exec", container, "sh", "-c", "ls -d /config/plugins/Gelato*"], capture_output=True, text=True,
                            env={**os.environ, "MSYS_NO_PATHCONV": "1"}).stdout.split()[0]
    subprocess.run(["docker", "cp", dll, f"{container}:{folder}/Gelato.dll"], check=True, capture_output=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    subprocess.run(["docker", "restart", container], check=True, capture_output=True)
    there = subprocess.run(["docker", "exec", container, "md5sum", f"{folder}/Gelato.dll"], capture_output=True, text=True,
                           env={**os.environ, "MSYS_NO_PATHCONV": "1"}).stdout.split()[0]
    with open(dll, "rb") as h:
        here = hashlib.md5(h.read()).hexdigest()
    if here != there:
        raise SystemExit(f"{container} does not run {dll}: {there} instead of {here}")
    while subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", container], capture_output=True, text=True).stdout.strip() != "healthy":
        time.sleep(2)
    print(f"  {container} runs {dll} ({here[:12]})")


def phase(args, name):
    global SEP
    if args.win:
        inst = os.path.abspath(args.win)
        db, root, SEP = WinDb(os.path.join(inst, "config")), os.path.join(inst, "media"), "\\"
        ffmpeg = shutil.which("ffmpeg") or next(iter(glob.glob(os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "ffmpeg.exe"), recursive=True)), None)
        if ffmpeg:
            native.FFMPEG = "'" + fs(ffmpeg) + "'"
        url = args.url or "http://localhost:8096"
    else:
        db, root, SEP = Db(args.container), "", "/"
        url = args.url or f"http://localhost:{subprocess.run(['docker', 'port', args.container, '8096/tcp'], capture_output=True, text=True).stdout.split(':')[-1].strip()}"
    sc = json.loads(json.dumps(SCENARIOS[args.scenario]).replace("{r}", fs(root)))
    if args.win:
        sc = json.loads(json.dumps(sc).replace("/", "\\\\"))
    api = Api(url, args.adminuser, args.adminpassword).ensure()
    api.on_call = db.invalidate
    t = Context(api, db, None, make_user2(api, on_call=db.invalidate), verbose=True)
    state_file = os.path.join(HERE, ".cache", f"upgrade-{args.scenario}-{os.path.basename(inst) if args.win else args.container}.json")
    print(f"[{name}] {args.scenario} on {url}")
    bootstrap.cap_catalogs(api, api.port, lambda m: print("  " + m))
    try:
        if name == "prep":
            prep(t, sc, state_file)
        else:
            check(t, sc, state_file, args.same_build)
    finally:
        bootstrap.restore_catalogs(api, api.port)
    print(f"[{name}] {t.passed} check(s) passed, {len(t.failures)} failed")
    for f in t.failures:
        print("  FAIL " + str(f)[:600])
    return not t.failures


SEP = "/"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("phase", choices=("prep", "check", "run"))
    p.add_argument("--container")
    p.add_argument("--win", help="the folder of a Windows instance of dev/jf.py")
    p.add_argument("--url")
    p.add_argument("--adminuser", default="admin")
    p.add_argument("--adminpassword", default="")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), default="custom")
    p.add_argument("--same-build", action="store_true", help="check: leave out what only a build with the library endpoints answers")
    p.add_argument("--old", help="run: the build to prepare with")
    p.add_argument("--new", help="run: the build to check")
    args = p.parse_args()
    if bool(args.container) == bool(args.win):
        p.error("one of --container and --win")
    if args.phase != "run":
        return 0 if phase(args, args.phase) else 1
    if not (args.container and args.old and args.new):
        p.error("run needs --container, --old and --new")
    deploy(args.container, args.old)
    phase(args, "prep")
    deploy(args.container, args.new)
    return 0 if phase(args, "check") else 1


if __name__ == "__main__":
    sys.exit(main())
