DESCRIPTION = "Deleting a movie while its first stream sync runs leaves nothing behind (the movie is inserted again afterwards)"

import threading
import time

DELAY = 0.4


def run(t):
    movie = t.unsynced_movie()
    if movie is None:
        t.skip("no movie without stream rows in the library")
    d = t.db.one("select Name from BaseItems where lower(replace(Id,'-',''))=?", (movie,))
    name, stremio = d[0], t.fixtures.stremio_id(movie)
    got = {}

    def visit():
        st, d = t.api.call("GET", f"/Items/{movie}?userId={t.api.user}")
        got["visit"] = (st, len(d.get("MediaSources") or []) if isinstance(d, dict) else None)

    th = threading.Thread(target=visit)
    th.start()
    time.sleep(DELAY)
    t0 = time.time()
    st, _ = t.api.call("DELETE", f"/Items/{movie}")
    took = time.time() - t0
    th.join()
    t.log(f"GET (sync) {got['visit']}, DELETE after {DELAY}s: {st} in {took:.1f}s")
    t.equal(st, 204, "delete answered 204")
    time.sleep(3)
    t.equal(t.db.item_count(movie), 0, "the movie is gone")
    rows = t.db.stream_rows(movie)
    t.equal((rows["count"], rows["links"]), (0, 0), "no rows and no links left")
    t.equal(t.api.call("GET", f"/Items/{movie}?userId={t.api.user}")[0], 404, "the movie answers 404")

    hits = [i for i in t.api.search(stremio) if i.get("Path", "").endswith(stremio)]
    if hits:
        back = t.api.item(hits[0]["Id"])
        t.log(f"inserted again from search: {back.get('Name')} ({back.get('Id', '')[:8]})")
        t.equal(back.get("Id", "").lower(), movie, f"{name} is back under the same id")
    else:
        t.log(f"could not find {name} ({stremio}) in search to insert it again")
