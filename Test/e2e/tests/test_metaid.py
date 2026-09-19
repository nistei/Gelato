DESCRIPTION = "A search result carrying a tmdb: id and an imdb_id opens on a meta addon that only answers for the tmdb: id"
DESTRUCTIVE = True  # points Gelato at a proxy on the host for the run of the test and restores the addon URL

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jfapi.bootstrap import GELATO

PORT = 8767  # the honest manifest is served on PORT + 1
TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs",
         "Dune", "Oppenheimer", "Past Lives", "The Zone of Interest"]


class TmdbOnlyAddon:
    """Forwards every request to the real addon, but makes it look like an AIOStreams identity whose
    only metadata preset is `tmdb-addon` (issue #207): the catalog hands out `tmdb:` ids with the
    `imdb_id` filled in beside them, and the meta resource answers 404 for every `tt` id.

    The addon behind it is IMDb-native, so the proxy keeps both ids: a catalog result's `tt` id
    becomes `tmdb:<n>`, and a meta request for that `tmdb:` id is served from the `tt` id upstream
    with the ids swapped back in the answer. `meta_id_prefixes` replaces what the manifest's meta
    resource declares - a real tmdb-addon preset declares `tmdb:` alone, but an addon may well
    declare a prefix it then refuses, so both are worth a run."""

    def __init__(self, upstream, port, ids, meta_id_prefixes=None):
        self.upstream = upstream.rstrip("/")
        if self.upstream.endswith("/manifest.json"):
            self.upstream = self.upstream[: -len("/manifest.json")]
        self.url = f"http://host.docker.internal:{port}"
        self.tmdb_of, self.imdb_of = ids  # tt id -> tmdb: id, and back
        self.calls = Counter()  # "meta imdb" (404ed), "meta tmdb" (served)
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

        def as_tmdb(meta):
            """tt12345 -> tmdb:912345, remembered both ways."""
            tt = meta.get("id") or ""
            if not tt.startswith("tt") or not tt[2:].isdigit():
                return meta
            tmdb = outer.tmdb_of.get(tt) or ("tmdb:9" + tt[2:])
            outer.tmdb_of[tt], outer.imdb_of[tmdb] = tmdb, tt
            meta["id"], meta["imdb_id"] = tmdb, tt
            return meta

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

                if mid is not None and mid.startswith("tt"):
                    # what a meta addon that only takes tmdb: ids does with an IMDb id
                    outer.calls["meta imdb"] += 1
                    return self.answer(404, b'{"err": "not found"}', "application/json")

                if mid is not None and mid in outer.imdb_of:
                    outer.calls["meta tmdb"] += 1
                    tt = outer.imdb_of[mid]
                    status, body, ctype = fetch("/".join(parts[:3]) + "/" + urllib.parse.quote(tt) + ".json")
                    if status == 200:
                        d = json.loads(body)
                        if isinstance(d.get("meta"), dict):
                            d["meta"]["id"], d["meta"]["imdb_id"] = mid, tt
                            for v in d["meta"].get("videos") or []:
                                if isinstance(v.get("id"), str):
                                    v["id"] = v["id"].replace(tt, mid, 1)
                            body = json.dumps(d).encode()
                    return self.answer(status, body, ctype or "application/json")

                status, body, ctype = fetch(self.path)
                if status == 200 and resource == "catalog":
                    d = json.loads(body)
                    if isinstance(d.get("metas"), list):
                        d["metas"] = [as_tmdb(m) for m in d["metas"]]
                        body = json.dumps(d).encode()
                elif status == 200 and meta_id_prefixes is not None and self.path == "/manifest.json":
                    d = json.loads(body)
                    for r in d.get("resources") or []:
                        if isinstance(r, dict) and r.get("name") == "meta":
                            r["idPrefixes"] = list(meta_id_prefixes)
                    body = json.dumps(d).encode()
                return self.answer(status, body, ctype or "application/json")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def run(t):
    cfg = t.api.get("/Plugins/" + GELATO + "/Configuration")
    old_url = cfg.get("Url")
    if not old_url:
        t.skip("Gelato has no addon URL")

    ids = ({}, {})
    # Gelato keeps one addon client per URL, manifest and all, so the two manifests need two ports.
    lying = TmdbOnlyAddon(old_url, PORT, ids)  # declares tt, then 404s it
    honest = TmdbOnlyAddon(old_url, PORT + 1, ids, meta_id_prefixes=["tmdb:", "aiostreamserror"])
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null http://host.docker.internal:{PORT}/manifest.json && echo ok"):
            t.skip(f"the container cannot reach the host on port {PORT}")

        in_library = lambda sid: t.db.one(
            "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
            "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (sid,))[0]

        def fresh_hit(taken):
            """A movie search result outside the library whose id the proxy turned into a tmdb: one."""
            for term in TERMS:
                for h in t.api.search(term, "Movie", limit=8):
                    m = re.search(r"(tmdb:\d+)", h.get("Path") or "")
                    tmdb = m.group(1) if m else None
                    if (tmdb in ids[1] and tmdb not in taken
                            and h.get("Name", "").lower().startswith(term.lower()[:5])
                            and not in_library(tmdb) and not in_library(ids[1][tmdb])):
                        return h, tmdb
            return None, None

        def round_(proxy, label, taken):
            """Points Gelato at the proxy, opens a fresh search result, and returns how often the
            addon was asked for a meta by IMDb id while it was opened."""
            t.api.post("/Plugins/" + GELATO + "/Configuration",
                       {**t.api.get("/Plugins/" + GELATO + "/Configuration"), "Url": proxy.url})
            hit, tmdb = fresh_hit(taken)
            if hit is None:
                t.log(f"{label}: no search result with a tmdb: id outside the library among the search terms")
                return None
            taken.add(tmdb)
            imdb = ids[1][tmdb]
            # The result's stub path carries the catalog id (the tmdb: one); the item it becomes
            # keeps the result's imdb_id as its Stremio id, so the library is asked about both.
            t.log(f"{label}: {hit['Name']}, id {tmdb}, imdb_id {imdb}, path {hit.get('Path')}")
            t.equal(proxy.calls["meta imdb"], 0, f"{label}: the search itself asked for no meta by IMDb id")

            proxy.calls.clear()
            st, d = t.api.call("GET", f"/Items/{hit['Id']}?userId={t.api.user}", timeout=90)
            inserted = d.get("Id", "").lower() if isinstance(d, dict) else None
            t.log(f"{label}: opening it answered HTTP {st}, meta calls {dict(proxy.calls)}")
            t.equal(st, 200, f"{label}: opening the result answers")
            t.check(proxy.calls["meta tmdb"] >= 1, f"{label}: the meta was fetched with the tmdb: id ({dict(proxy.calls)})")
            if not t.check(bool(inserted) and inserted != hit["Id"].lower(), f"{label}: the click inserted the movie under a library id"):
                return None
            try:
                t.equal(d.get("Type"), "Movie", f"{label}: the inserted item is a movie")
                t.check(d.get("Name"), f"{label}: it has a name ({d.get('Name')})")
                t.check("Primary" in (d.get("ImageTags") or {}), f"{label}: it has a poster")
                t.equal(in_library(imdb) + in_library(tmdb), 1, f"{label}: one movie item in the library")
                t.check(len(d.get("MediaSources") or []) >= 1, f"{label}: it has streams ({len(d.get('MediaSources') or [])})")
            finally:
                t.api.delete(f"/Items/{inserted}")
                t.equal(in_library(imdb) + in_library(tmdb), 0, f"{label}: the movie was removed again")
            return proxy.calls["meta imdb"]

        taken = set()
        try:
            round_(lying, "manifest declares tt", taken)
            asked = round_(honest, "manifest declares tmdb: only", taken)
            if asked is not None:
                t.equal(asked, 0, "with an honest manifest the IMDb id is never asked for")
        finally:
            t.api.post("/Plugins/" + GELATO + "/Configuration",
                       {**t.api.get("/Plugins/" + GELATO + "/Configuration"), "Url": old_url})
    finally:
        lying.close()
        honest.close()
