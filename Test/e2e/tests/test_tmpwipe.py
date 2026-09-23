DESCRIPTION = "A restart with Gelato's folders gone (a container whose /tmp does not persist), then a full library scan: every Gelato item, both folder items and the seed files survive"
DESTRUCTIVE = True  # deletes Gelato's folders on disk, restarts the server and runs a full library scan

import shlex
import subprocess

from jfapi.bootstrap import GELATO, wait_ready

SEED = "stub.txt"
SEED_CONTENT = "This is a seed file created by Gelato so that library scans are triggered. Do not remove."
TYPES = ("Movie", "Series", "Season", "Episode")


def counts(t):
    """Gelato's library items by type: items of the addon have a gelato:// or http path."""
    rows = t.db.query("select Type, count(*) from BaseItems where (Path like 'gelato://%' or Path like 'http%') "
                      "and Tags not like '%gelato-stream%' group by Type")
    n = {k.rsplit(".", 1)[-1]: v for k, v in rows}
    return {k: n.get(k, 0) for k in TYPES}


def folder_ids(t, folders):
    return {p: [r[0] for r in t.db.query("select Id from BaseItems where Path=?", (p,))] for p in folders}


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    folders = [p.rstrip("/") for p in (cfg.get("MoviePath"), cfg.get("SeriesPath")) if p]
    t.check(folders, "Gelato has a movie or series path configured")
    before, ids = counts(t), folder_ids(t, folders)
    t.log("Gelato items before:", before, "folder items:", ids)
    t.check(before["Movie"] and all(len(v) == 1 for v in ids.values()), "the instance has Gelato movies and one item per folder")

    t.log("deleting", ", ".join(folders), "and restarting, like a pod restart with an empty /tmp")
    t.sh("rm -rf " + " ".join(shlex.quote(p) for p in folders))
    t.check(not t.sh("ls -d " + " ".join(shlex.quote(p) for p in folders) + " 2>/dev/null").strip(), "the folders are gone")
    subprocess.run(["docker", "restart", t.db.container], capture_output=True)
    t.check(wait_ready(t.api.base, t.log) is not None, "the server came back")
    t.api.ensure()
    present = t.sh("ls -d " + " ".join(shlex.quote(p) for p in folders) + " 2>/dev/null").split()
    t.log("folders on disk right after the start:", present or "none")

    # The scan runs before anything has searched or imported: whatever puts the folders back has
    # to do it on its own.
    status, msg = t.api.run_task("RefreshLibrary", timeout=1800)
    t.equal(status, "Completed", f"library scan {msg}")
    after, ids_after = counts(t), folder_ids(t, folders)
    t.log("Gelato items after:", after, "folder items:", ids_after)
    t.equal(ids_after, ids, "both folder items survived with their ids")
    for k in TYPES:
        t.equal(after[k], before[k], f"every Gelato {k} survived the scan")

    t.api.search("star", kind="Movie,Series")  # a folder lookup, which seeds a folder that lacks its stub
    for p in folders:
        t.equal(t.sh(f"cat {shlex.quote(p + '/' + SEED)} 2>/dev/null"), SEED_CONTENT, f"{p} has its seed file again")
