"""Queries against a snapshot of the instance's database, copied out of its Docker container.

The database is in use, so `jellyfin.db` with its -wal/-shm files is copied out of the container
(`docker cp`) into `.cache/db/<pid>/` and snapshotted with sqlite3's `backup()`; every `connect()`
takes a fresh copy. Ids are stored as GUIDs with dashes: compare with `norm()`.

`JF_CONTAINER` names the container for the module-level `query()` used by the scripts in `tools/`.
"""
import atexit
import os
import shutil
import sqlite3
import subprocess

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "db")
CONTAINER = os.environ.get("JF_CONTAINER", "")
STREAM_TAG = "%gelato-stream%"
LINKED_ALTERNATE_VERSION = 3  # LinkedChildren.ChildType


def norm(guid):
    return (guid or "").replace("-", "").lower()


class Db:
    def __init__(self, container=CONTAINER):
        self.container = container
        self.work = os.path.join(CACHE, str(os.getpid()))
        self._fresh = False  # the last snapshot still reflects the server (no API call since)
        atexit.register(lambda: shutil.rmtree(self.work, ignore_errors=True))

    def invalidate(self):
        """Called on every API call: the next query takes a new snapshot."""
        self._fresh = False

    def connect(self):
        """A connection to a fresh snapshot; close it when done. Consecutive queries without an
        API call in between share one copy."""
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
        if con is not None:
            return con.execute(sql, params).fetchall()
        for attempt in range(3):
            con = self.connect()
            try:
                return con.execute(sql, params).fetchall()
            except sqlite3.DatabaseError:
                self._fresh = False  # take a new copy, not the unreadable one again
                if attempt == 2:
                    raise
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
