DESCRIPTION = "A search whose series or movie catalog fails still answers with the other catalog's results; only a search where every catalog failed fails"
DESTRUCTIVE = True  # points Gelato at a proxy on the host for the run of the test and restores the addon URL

import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jfapi.bootstrap import GELATO

TERM = "star"  # a term the addon answers with movies and series


class AddonProxy:
    """Forwards every request to the real addon, except the search requests of the catalog types in
    `fail`, which it answers with HTTP 503 (what an addon or reverse proxy says when a catalog times out)."""

    def __init__(self, upstream):
        self.upstream = upstream.rstrip("/")
        if self.upstream.endswith("/manifest.json"):
            self.upstream = self.upstream[: -len("/manifest.json")]
        self.fail = set()
        self.failed = Counter()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = self.path.split("/")
                kind = parts[2] if len(parts) > 3 and parts[1] == "catalog" else None
                if kind in outer.fail and "search=" in self.path:
                    outer.failed[kind] += 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                req = urllib.request.Request(outer.upstream + self.path, headers={"User-Agent": self.headers.get("User-Agent") or "jfapi"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        status, body, ctype = r.status, r.read(), r.headers.get("Content-Type")
                except urllib.error.HTTPError as e:
                    status, body, ctype = e.code, e.read(), e.headers.get("Content-Type")
                except OSError:
                    status, body, ctype = 502, b"", None
                self.send_response(status)
                if ctype:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    old_url = cfg.get("Url")
    if not old_url:
        t.skip("Gelato has no addon URL")
    proxy = AddonProxy(old_url)
    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {proxy.url}/manifest.json && echo ok"):
            t.skip(f"the container cannot reach the host on port {proxy.port}")
        t.api.post(f"/Plugins/{GELATO}/Configuration", {**cfg, "Url": proxy.url})
        try:
            def search(label):
                path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(TERM)}&IncludeItemTypes=Movie,Series"
                        f"&Recursive=true&Limit=50")
                st, d = t.api.call("GET", path)
                kinds = Counter(i.get("Type") for i in d.get("Items", [])) if st == 200 and isinstance(d, dict) else None
                t.log(f"{label}: HTTP {st}, {dict(kinds) if kinds is not None else str(d)[:120]}, 503s sent {dict(proxy.failed)}")
                return st, kinds or Counter()

            proxy.fail = set()
            st, both = search("no catalog failing")
            if st != 200 or not both["Movie"] or not both["Series"]:
                t.skip(f"the addon does not answer '{TERM}' with movies and series through the proxy (HTTP {st}, {dict(both)})")

            # The movie catalog can list series too (and the other way round), so the failing catalog's type
            # does not vanish from the answer, it only shrinks.
            for failing, answering, lost in (("series", "Movie", "Series"), ("movie", "Series", "Movie")):
                proxy.fail, proxy.failed = {failing}, Counter()
                st, kinds = search(f"{failing} catalog failing")
                t.check(proxy.failed[failing] >= 1, f"{failing} failing: the proxy failed the {failing} search")
                t.equal(st, 200, f"{failing} failing: the search still answers")
                t.check(kinds[answering] > 0, f"{failing} failing: the {answering.lower()} results are there ({kinds[answering]})")
                t.check(kinds[lost] < both[lost], f"{failing} failing: the {failing} catalog's results are missing ({kinds[lost]} of {both[lost]})")

            proxy.fail, proxy.failed = {"movie", "series"}, Counter()
            st, _ = search("both catalogs failing")
            t.check(st >= 500, f"both failing: the search fails instead of looking empty (HTTP {st})")

            proxy.fail = set()
            st, kinds = search("recovered")
            t.check(st == 200 and kinds["Movie"] and kinds["Series"], "afterwards both catalogs answer again")
        finally:
            t.api.post(f"/Plugins/{GELATO}/Configuration", {**t.api.get(f"/Plugins/{GELATO}/Configuration"), "Url": old_url})
    finally:
        proxy.close()
