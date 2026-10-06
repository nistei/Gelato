"""A local copy of the instance's database for EXPLAIN QUERY PLAN and fixture picks.

    python tools/perf/dbq.py --pull        copy jellyfin.db (and -wal/-shm) out of the container into .cache/perf
    python tools/perf/dbq.py "select ..."  query the copy

Stream rows' paths in it carry the debrid key: never print a Path.
"""
import os
import sqlite3
import subprocess
import sys

import perflib as p

DB = os.environ.get("PERF_DB") or os.path.join(p.WORK, "jellyfin.db")


def pull():
    for f in ("jellyfin.db", "jellyfin.db-wal", "jellyfin.db-shm"):
        target = os.path.join(os.path.dirname(DB), f)
        if os.path.exists(target):
            os.remove(target)
        subprocess.run(["docker", "cp", f"{p.CONTAINER}:/config/data/{f}", target], capture_output=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    print("journal_mode", q("pragma journal_mode")[0][0], "items", q("select count(*) from BaseItems")[0][0])


def con():
    return sqlite3.connect(DB)


def q(sql, params=()):
    c = con()
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


if __name__ == "__main__":
    if sys.argv[1] == "--pull":
        pull()
    else:
        for r in q(sys.argv[1]):
            print(*r, sep=" | ")
