"""Queries against the instance's database, read in place by a sidecar container.

The sidecar (`<container>-sql`, `python:3-slim` with `--volumes-from` the instance) runs one Python
process that answers a query per line on stdin, against the live `jellyfin.db`. SQLite in WAL mode
lets it read next to Jellyfin, and a query takes milliseconds. Before, every check copied the whole
database out of the container (`docker cp` and `backup()`), 500 MB on a copy of prod, several times
per test: that was most of a run's time. `connect()` opens a read transaction in the sidecar, which
sees one consistent state until it is closed, as the copy did.

Without the sidecar (the image is missing, the container cannot be started) the copy is the fallback:
`jellyfin.db` with its -wal/-shm files goes to `.cache/db/<pid>/` and is snapshotted with `backup()`.
Ids are stored as GUIDs with dashes: compare with `norm()`.

`JF_CONTAINER` names the container for the module-level `query()` used by the scripts in `tools/`.

Random picks are seeded: SQLite's `random()` takes no seed, so a query ending in
`order by random() limit n` is run in a fixed order and shuffled here with the run's seed, reset per
test (`reseed`). The same seed on the same instance state picks the same items, alone or in a run.
"""
import atexit
import base64
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "db")
CONTAINER = os.environ.get("JF_CONTAINER", "")
STREAM_TAG = "%gelato-stream%"
LINKED_ALTERNATE_VERSION = 3  # LinkedChildren.ChildType
RANDOM_PICK = re.compile(r"\s+order by random\(\)\s+limit\s+(\d+)\s*$", re.IGNORECASE)


SIDECAR_IMAGE = "python:3-slim"
SIDECAR_SCRIPT = r"""
import base64, json, sqlite3, sys
def connect():
    return sqlite3.connect("file:/config/data/jellyfin.db?mode=ro", uri=True, timeout=30, isolation_level=None)
live, cons, n = connect(), {}, 0
enc = lambda v: {"$b": base64.b64encode(bytes(v)).decode()} if isinstance(v, (bytes, memoryview)) else v
for line in sys.stdin:
    r = json.loads(line)
    try:
        if r["op"] == "open":
            n += 1
            cons[n] = connect()
            cons[n].execute("BEGIN")
            out = {"con": n}
        elif r["op"] == "close":
            c = cons.pop(r["con"], None)
            if c is not None:
                c.close()
            out = {}
        else:
            c = cons[r["con"]] if r.get("con") else live
            out = {"rows": [[enc(v) for v in row] for row in c.execute(r["sql"], r.get("params") or []).fetchall()]}
    except Exception as e:
        out = {"error": type(e).__name__ + ": " + str(e)}
    print(json.dumps(out))
    sys.stdout.flush()
"""


class Sidecar:
    """A container next to the instance that reads its database in place (see the module doc)."""

    def __init__(self, container):
        self.name = f"{container}-sql"
        self.lock = threading.Lock()
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        r = subprocess.run(["docker", "run", "-d", "--name", self.name, "--volumes-from", container,
                            SIDECAR_IMAGE, "sleep", "infinity"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "docker run failed")
        self.proc = subprocess.Popen(["docker", "exec", "-i", self.name, "python", "-u", "-c", SIDECAR_SCRIPT],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8")
        atexit.register(self.close)
        self.ask({"op": "q", "sql": "select 1"})

    def ask(self, request):
        with self.lock:
            print(json.dumps(request), file=self.proc.stdin)
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        if not line:
            raise sqlite3.DatabaseError(f"the database sidecar {self.name} stopped answering")
        out = json.loads(line)
        if "error" in out:
            raise sqlite3.DatabaseError(out["error"])
        return out

    def rows(self, sql, params=(), con=None):
        dec = lambda v: base64.b64decode(v["$b"]) if isinstance(v, dict) else v
        out = self.ask({"op": "q", "sql": sql, "params": list(params), "con": con})
        return [tuple(dec(v) for v in row) for row in out["rows"]]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


class SidecarConnection:
    """What `connect()` hands out with a sidecar: a read transaction, one consistent state."""

    def __init__(self, sidecar):
        self.sidecar = sidecar
        self.id = sidecar.ask({"op": "open"})["con"]

    def execute(self, sql, params=()):
        rows = self.sidecar.rows(sql, params, self.id)
        return type("Cursor", (), {"fetchall": lambda _: rows, "fetchone": lambda _: rows[0] if rows else None})()

    def close(self):
        self.sidecar.ask({"op": "close", "con": self.id})


def norm(guid):
    return (guid or "").replace("-", "").lower()


class Db:
    def __init__(self, container=CONTAINER):
        self.container = container
        self.work = os.path.join(CACHE, str(os.getpid()))
        self._fresh = False  # the last snapshot still reflects the server (no API call since)
        self.seed = None
        self.rng = random.Random()
        self.sidecar = None
        if container:
            try:
                self.sidecar = Sidecar(container)
            except Exception as e:
                print(f"  database sidecar not available ({e}): copying the database for every check instead")
        atexit.register(lambda: shutil.rmtree(self.work, ignore_errors=True))

    def reseed(self, seed, scope=""):
        """Picks from here on follow `seed`, per `scope` (a test's name): a test run alone with the
        run's seed picks what it picked in the run."""
        self.seed = seed
        self.rng = random.Random(f"{seed}:{scope}")

    def invalidate(self):
        """Called on every API call: the next query takes a new snapshot."""
        self._fresh = False

    def connect(self):
        """A connection that sees one consistent state of the database; close it when done. With
        the sidecar a read transaction, without it a fresh copy (consecutive queries without an API
        call in between share one)."""
        if self.sidecar is not None:
            return SidecarConnection(self.sidecar)
        snap = os.path.join(self.work, "snapshot.db")
        if self._fresh and os.path.exists(snap):
            return sqlite3.connect(snap)
        os.makedirs(self.work, exist_ok=True)
        for f in ("jellyfin.db", "jellyfin.db-wal", "jellyfin.db-shm"):
            p = os.path.join(self.work, f)
            if os.path.exists(p):
                os.remove(p)
            subprocess.run(["docker", "cp", f"{self.container}:/config/data/{f}", p], capture_output=True)
        src = sqlite3.connect(os.path.join(self.work, "jellyfin.db"))
        snap = os.path.join(self.work, "snapshot.db")
        if os.path.exists(snap):
            os.remove(snap)
        dst = sqlite3.connect(snap)
        src.backup(dst)
        src.close()
        self._fresh = True
        return dst

    def query(self, sql, params=(), con=None):
        """Rows of one query, on a fresh snapshot unless a connection is given. A copy taken while
        the server writes can be unreadable: it is taken again."""
        pick = RANDOM_PICK.search(sql)
        if pick:
            # All candidates in a fixed order, then the seeded shuffle: the rows' own order only
            # decides which ones come first before shuffling, so it has to be stable too.
            rows = self.query(sql[: pick.start()] + " order by 1", params, con)
            self.rng.shuffle(rows)
            return rows[: int(pick.group(1))]
        if con is not None:
            return con.execute(sql, params).fetchall()
        if self.sidecar is not None:
            return self.sidecar.rows(sql, params)
        for attempt in range(3):
            con = self.connect()
            try:
                return con.execute(sql, params).fetchall()
            except sqlite3.DatabaseError:
                self._fresh = False  # take a new copy, not the unreadable one again
                if attempt == 2:
                    raise
                # Three copies in a row can all land in the middle of the same write (an insert
                # from search writes for a while), so give the server a moment.
                time.sleep(1)
            finally:
                con.close()

    def one(self, sql, params=(), con=None):
        rows = self.query(sql, params, con)
        return rows[0] if rows else None

    def sh(self, cmd):
        """A shell command inside the container. Its output is decoded as UTF-8: with the default
        (the Windows code page) one file name with a non-ASCII character fails the reader thread and
        the output comes back as None."""
        return subprocess.run(["docker", "exec", self.container, "sh", "-c", cmd], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout

    # ---- Gelato specifics

    def stremio_id(self, item_id, con=None):
        r = self.one("select ProviderValue from BaseItemProviders where lower(replace(ItemId,'-',''))=? and lower(ProviderId)='stremio'",
                     (norm(item_id),), con)
        return r[0] if r else None

    def stream_rows(self, item_id):
        """The stream rows of a movie/episode's title: count, owned by it, unowned, without a
        refresh stamp, and its version links."""
        con = self.connect()
        try:
            item = norm(item_id)
            stremio = self.stremio_id(item, con)
            r = self.one(
                "select count(*), sum(case when lower(replace(b.PrimaryVersionId,'-',''))=? then 1 else 0 end), "
                "sum(case when b.PrimaryVersionId is null then 1 else 0 end), "
                "sum(case when b.DateLastRefreshed is null or b.DateLastRefreshed < '0002' then 1 else 0 end) "
                "from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                "where b.Tags like ? and p.ProviderValue=?", (item, STREAM_TAG, stremio or ""), con)
            links = self.one("select count(*) from LinkedChildren where lower(replace(ParentId,'-',''))=? and ChildType=?",
                             (item, LINKED_ALTERNATE_VERSION), con)
            return {"count": r[0], "owned": r[1] or 0, "unowned": r[2] or 0, "unstamped": r[3] or 0, "links": links[0]}
        finally:
            con.close()

    def row_users(self, item_id):
        """{rowId: (set of user ids, owner id, index)} for the rows of a movie's title."""
        import json
        con = self.connect()
        try:
            stremio = self.stremio_id(item_id, con)
            rows = self.query(
                "select lower(replace(b.Id,'-','')), lower(replace(b.PrimaryVersionId,'-','')), b.ExternalId from BaseItems b "
                "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' where b.Tags like ? and p.ProviderValue=?",
                (STREAM_TAG, stremio or ""), con)
        finally:
            con.close()
        out = {}
        for id_, pv, ext in rows:
            try:
                g = json.loads(ext or "{}")
            except Exception:
                g = {}
            out[id_] = ({norm(u) for u in g.get("userIds") or []}, pv or None, g.get("index"), g.get("guid"))
        return out

    def stream_row_ids(self, con=None):
        return {r[0] for r in self.query("select lower(replace(Id,'-','')) from BaseItems where Tags like ?", (STREAM_TAG,), con)}

    def item_count(self, item_id, con=None):
        return self.one("select count(*) from BaseItems where lower(replace(Id,'-',''))=?", (norm(item_id),), con)[0]

    def playlist_links(self, playlist_id, con=None):
        """[(childId, 'row' | 'item' | 'MISSING')] of a playlist or collection."""
        rows = self.query(
            "select lower(replace(l.ChildId,'-','')), case when c.Id is null then 'MISSING' when c.Tags like ? then 'row' else 'item' end "
            "from LinkedChildren l left join BaseItems c on c.Id=l.ChildId where lower(replace(l.ParentId,'-',''))=? order by l.SortOrder",
            (STREAM_TAG, norm(playlist_id)), con)
        return [(r[0], r[1]) for r in rows]


_default = None


def query(sql, params=()):
    """Rows of one query on the environment's container, for the scripts in tools/: (columns, rows)."""
    global _default
    if _default is None:
        if not CONTAINER:
            raise SystemExit("set JF_CONTAINER to the instance's Docker container")
        _default = Db(CONTAINER)
    con = _default.connect()
    try:
        cur = con.execute(sql, params)
        cols = [c[0] for c in cur.description] if cur.description else []
        return cols, cur.fetchall()
    finally:
        con.close()
