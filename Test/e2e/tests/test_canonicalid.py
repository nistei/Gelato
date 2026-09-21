DESCRIPTION = "A movie a search returns twice, once under a tmdb: id without an imdb_id and once under its tt id, opens on one library item in either order"
DESTRUCTIVE = True  # points Gelato at a proxy on the host for the run of the test and restores the addon URL

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jfapi.bootstrap import GELATO

TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs",
         "Dune", "Oppenheimer", "Past Lives", "Furiosa", "Twisters", "Wicked", "Gladiator"]
# what an insert that cannot reconcile its provider ids with the canonical item's leaves in the log
COLLISION = "UNIQUE constraint failed: BaseItemProviders"


def tmdb_twin(tt):
    """The id the proxy files a catalog result under a second time. Kept out of the real TMDB
    number space (a 9 in front), so nothing in the library can already carry it."""
    return "tmdb:9" + tt[2:]


class TwinCatalogAddon:
    """Forwards every request to the real addon, but returns each catalog result twice: once as it
    is, and once as a `tmdb:` id with no `imdb_id` beside it - what a library gets when AIOStreams
    merges a TMDB-based catalog into an IMDb-native one.

    The two entries are one movie, and Gelato can only see that after it has fetched the twin's
    meta: the catalog entry alone carries no id the other one shares. Opening both must still end
    on a single library item, whichever of the two was opened first.
    """

    def __init__(self, upstream):
        self.upstream = upstream.rstrip("/")
        if self.upstream.endswith("/manifest.json"):
            self.upstream = self.upstream[: -len("/manifest.json")]
        self.imdb_of = {}  # tmdb: twin -> the tt id it was made from
        outer = self

        def fetch(path):
            req = urllib.request.Request(outer.upstream + path, headers={"User-Agent": "jfapi"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, r.read(), r.headers.get("Content-Type")
            except urllib.error.HTTPError as e:
                return e.code, e.read(), e.headers.get("Content-Type")
            except OSError:
                return 502, b"", None

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status, body, ctype):
                self.send_response(status)
                if ctype:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = self.path.split("/")
                resource = parts[1] if len(parts) > 2 else ""
                mid = (urllib.parse.unquote(parts[3][: -len(".json")])
                       if resource == "meta" and len(parts) > 3 and parts[3].endswith(".json") else None)

                if mid is not None and mid in outer.imdb_of:
                    # the twin's meta is the real one, under the twin's id: only here does the
                    # imdb_id show up, which is what ties the two catalog entries together
                    tt = outer.imdb_of[mid]
                    status, body, ctype = fetch("/".join(parts[:3]) + "/" + urllib.parse.quote(tt) + ".json")
                    if status == 200:
                        d = json.loads(body)
                        if isinstance(d.get("meta"), dict):
                            d["meta"]["id"], d["meta"]["imdb_id"] = mid, tt
                            body = json.dumps(d).encode()
                    return self.answer(status, body, ctype or "application/json")

                status, body, ctype = fetch(self.path)
                if status == 200 and resource == "catalog":
                    d = json.loads(body)
                    if isinstance(d.get("metas"), list):
                        out = []
                        for m in d["metas"]:
                            out.append(m)
                            tt = m.get("id") or ""
                            if not (tt.startswith("tt") and tt[2:].isdigit()):
                                continue
                            twin = dict(m)
                            twin["id"] = tmdb_twin(tt)
                            twin.pop("imdb_id", None)
                            outer.imdb_of[twin["id"]] = tt
                            out.append(twin)
                        d["metas"] = out
                        body = json.dumps(d).encode()
                return self.answer(status, body, ctype or "application/json")

            def log_message(self, *a):
                pass

        # Port 0: the operating system hands out a free one, so two suite runs against two
        # instances do not fight over a fixed port (on Windows the second bind succeeds and
        # silently receives nothing).
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://host.docker.internal:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()



def server_log(container, since):
    r = subprocess.run(["docker", "logs", "--since", str(int(since)), container],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout + r.stderr


def run(t):
    cfg = t.api.get("/Plugins/" + GELATO + "/Configuration")
    old_url = cfg.get("Url")
    if not old_url:
        t.skip("Gelato has no addon URL")

    proxy = TwinCatalogAddon(old_url)
    started = time.time()
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {proxy.url}/manifest.json && echo ok"):
            t.skip(f"the container cannot reach the host on port {proxy.port}")

        def set_url(url):
            t.api.post("/Plugins/" + GELATO + "/Configuration",
                       {**t.api.get("/Plugins/" + GELATO + "/Configuration"), "Url": url})

        def in_library(stremio):
            return t.db.one(
                "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
                "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

        def items_of(imdb):
            """Library items (not stream rows) carrying that IMDb id."""
            return t.db.query(
                "select lower(replace(b.Id,'-','')), b.Path from BaseItems b join BaseItemProviders p on p.ItemId=b.Id "
                "where lower(p.ProviderId)='imdb' and p.ProviderValue=? "
                "and (b.Tags is null or b.Tags not like '%gelato-stream%')", (imdb,))

        set_url(proxy.url)
        taken = set()

        def fresh_pair():
            """A search that returned the same movie twice: (tt hit, twin hit, tt id, twin id)."""
            for term in TERMS:
                hits = t.api.search(term, "Movie", limit=12)
                by_path = {h.get("Path"): h for h in hits}
                for h in hits:
                    path = h.get("Path") or ""
                    tt = path[len("gelato://stub/"):]
                    if not (path.startswith("gelato://stub/tt") and tt[2:].isdigit()) or tt in taken:
                        continue
                    twin = by_path.get(f"gelato://stub/{tmdb_twin(tt)}")
                    t.db.invalidate()
                    if twin is not None and not in_library(tt) and not in_library(tmdb_twin(tt)):
                        return h, twin, tt, tmdb_twin(tt)
            return None, None, None, None

        def round_(first, label):
            """Opens the two results for one movie, `first` ("tt" or "tmdb:") first. Both must end
            on the same library item, and the library must hold that movie once."""
            tt_hit, twin_hit, tt, twin = fresh_pair()
            if tt_hit is None:
                t.log(f"{label}: no movie outside the library that the search returned twice")
                return
            taken.add(tt)
            t.log(f"{label}: {tt_hit['Name']} as {tt} and as {twin}")

            order = [(tt_hit, tt), (twin_hit, twin)]
            if first != "tt":
                order.reverse()

            inserted = None
            try:
                for n, (hit, which) in enumerate(order, start=1):
                    st, d = t.api.call("GET", f"/Items/{hit['Id']}?userId={t.api.user}", timeout=240)
                    item = d.get("Id", "").lower() if isinstance(d, dict) else None
                    sources = len(d.get("MediaSources") or []) if isinstance(d, dict) else 0
                    t.log(f"{label}: opening the {which} result answered HTTP {st}, item {(item or '?')[:8]}, {sources} streams")
                    if not t.equal(st, 200, f"{label}: the {which} result opens"):
                        return
                    if not t.check(item and item != hit["Id"].lower(), f"{label}: the {which} result became a library item"):
                        return
                    t.check(sources >= 1, f"{label}: the {which} result has streams ({sources})")
                    if n == 1:
                        inserted = item
                    else:
                        t.equal(item, inserted, f"{label}: both results open the same library item")

                rows = items_of(tt)
                t.equal(len(rows), 1, f"{label}: one library item carries {tt} ({[r[1] for r in rows]})")
            finally:
                for item in {r[0] for r in items_of(tt)} | ({inserted} if inserted else set()):
                    t.api.delete(f"/Items/{item}")
                t.db.invalidate()
                t.equal(in_library(tt) + in_library(twin), 0, f"{label}: the movie was removed again")

        try:
            round_("tt", "tt id first")
            round_("tmdb:", "tmdb: id first")
        finally:
            set_url(old_url)

        log = server_log(t.db.container, started)
        t.equal(log.count(COLLISION), 0, f"no insert collided on a canonical id ({COLLISION})")
    finally:
        proxy.close()
