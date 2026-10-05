"""Queries against the instance's database, read in place by a sidecar container.

The sidecar (`<container>-sql`, `python:3-slim` with `--volumes-from` the instance) is one Python
process that answers a query per line on stdin, against the live `jellyfin.db`, and ends with the
run. SQLite in WAL mode lets it read next to Jellyfin, and a query takes milliseconds. Before, every check copied the whole
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
import tempfile
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
        elif r["op"] == "stat":
            import os
            out = {"stat": [[f, os.stat("/config/data/" + f).st_size, os.stat("/config/data/" + f).st_mtime_ns]
                            for f in ("jellyfin.db", "jellyfin.db-wal", "jellyfin.db-shm") if os.path.exists("/config/data/" + f)]}
        elif r["op"] == "close":
            c = cons.pop(r["con"], None)
            if c is not None:
                c.close()
            out = {}
        else:
            c = cons[r["con"]] if r.get("con") else live
            cur = c.execute(r["sql"], r.get("params") or [])
            out = {"rows": [[enc(v) for v in row] for row in cur.fetchall()], "cols": [d[0] for d in cur.description or []]}
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
        # The script is the container's own process, on our pipe, and the container is --rm: when
        # this run ends, however it ends, the script reads the end of its input and the container
        # goes with it. As a `sleep infinity` container with the script exec'ed into it, a killed
        # run left it behind, holding the instance's volumes against a cleanup.
        self.errors = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(["docker", "run", "-i", "--rm", "--name", self.name, "--volumes-from", container,
                                      SIDECAR_IMAGE, "python", "-u", "-c", SIDECAR_SCRIPT],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
                                     text=True, encoding="utf-8")
        atexit.register(self.close)
        try:
            self.ask({"op": "q", "sql": "select 1"})
        except (sqlite3.DatabaseError, OSError):
            self.errors.seek(0)
            said = self.errors.read().decode("utf-8", errors="replace").strip().splitlines()
            self.close()
            raise RuntimeError(said[-1] if said else "docker run failed")

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
        return self.rows_and_cols(sql, params, con)[0]

    def rows_and_cols(self, sql, params=(), con=None):
        dec = lambda v: base64.b64decode(v["$b"]) if isinstance(v, dict) else v
        out = self.ask({"op": "q", "sql": sql, "params": list(params), "con": con})
        return [tuple(dec(v) for v in row) for row in out["rows"]], out.get("cols") or []

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


class Shell:
    """One long-lived `sh` inside the instance container. `docker exec` per command costs ~160 ms on
    Windows; a command through this shell costs milliseconds. Each command runs in its own `sh -c`
    (base64 over the pipe, so quoting and heredocs behave as before) with stdin closed."""

    END = "__JFAPI_END__"

    def __init__(self, container):
        self.container = container
        self.lock = threading.Lock()
        self.proc = None
        atexit.register(self.close)

    def _start(self):
        self.proc = subprocess.Popen(["docker", "exec", "-i", self.container, "sh"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def run(self, cmd):
        enc = base64.b64encode(cmd.encode("utf-8")).decode()
        line = f"echo {enc} | base64 -d > /tmp/.jfapi_cmd; sh /tmp/.jfapi_cmd < /dev/null; echo; echo {self.END}\n"
        with self.lock:
            for attempt in range(2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                try:
                    self.proc.stdin.write(line.encode())
                    self.proc.stdin.flush()
                    out = bytearray()
                    while True:
                        chunk = self.proc.stdout.readline()
                        if not chunk:
                            raise BrokenPipeError("shell closed")
                        if chunk.strip() == self.END.encode():
                            break
                        out += chunk
                    text = out.decode("utf-8", errors="replace")
                    return text[:-1] if text.endswith("\n") else text  # the marker's own blank line
                except (BrokenPipeError, OSError):
                    self.proc = None
                    if attempt:
                        raise

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            pass


class SidecarConnection:
    """What `connect()` hands out with a sidecar: a read transaction, one consistent state."""

    def __init__(self, sidecar):
        self.sidecar = sidecar
        self.id = sidecar.ask({"op": "open"})["con"]

    def execute(self, sql, params=()):
        rows, cols = self.sidecar.rows_and_cols(sql, params, self.id)
        return type("Cursor", (), {"fetchall": lambda _: rows, "fetchone": lambda _: rows[0] if rows else None,
                                   "description": [(c,) for c in cols] or None})()

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
        self.shell = Shell(container) if container else None
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
        if self.shell is not None:
            return self.shell.run(cmd)
        return subprocess.run(["docker", "exec", self.container, "sh", "-c", cmd], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout

    def db_stat(self):
        """Sizes and mtimes of the database files, read inside the sidecar (no docker exec)."""
        if self.sidecar is not None:
            return self.sidecar.ask({"op": "stat"})["stat"]
        return self.sh("stat -c '%n %s %y' /config/data/jellyfin.db* 2>/dev/null")

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

    def listed_movie_count(self, cfg):
        """How many movies the library listing should show: every movie item, minus (with FilterUnreleased) the Gelato
        titles whose end date is within the buffer or ahead -- the plugin's UnreleasedListingFilter hides those."""
        base = ("Type like '%Movies.Movie' and (Tags is null or Tags not like ?) and PrimaryVersionId is null and IsVirtualItem=0")
        total = self.one(f"select count(*) from BaseItems where {base}", (STREAM_TAG,))[0]
        if not cfg.get("FilterUnreleased"):
            return total
        buffer_days = int(cfg.get("FilterUnreleasedBufferDays") or 0)
        hidden = self.one(
            f"select count(*) from BaseItems b where {base} and EndDate > strftime('%Y-%m-%d 00:00:00', 'now', '-{buffer_days} day') "
            "and exists (select 1 from BaseItemProviders p where p.ItemId=b.Id and lower(p.ProviderId)='stremio')", (STREAM_TAG,))[0]
        return total - hidden

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
