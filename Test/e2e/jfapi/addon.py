"""A recording proxy in front of the Stremio addon, so a run sees the same addon from start to end.

The addon is the one part of a run nobody controls: its search results, metas and catalogs change
from one day to the next, and it answers in anything from 0.2 to 7 seconds. A test that found five
results yesterday found four today, and a slow answer ran into a timeout. The proxy answers the
manifest, catalogs and metas from a recording (`.cache/addon/<addon>/`, filled on the first request
and kept across runs) and forwards everything else, streams above all: those carry debrid links
that expire, so a recorded one would fail playback.

The run points Gelato at the proxy and back at the addon when it ends. The addon's URL is written
to `.cache/addon-url-<port>` first, so a run that died half way puts it back on the next start.
The recording holds the addon's answers and its configuration is in the URL: it stays in `.cache`,
which git ignores, and the URL is never printed.
"""
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(HERE, ".cache")
RECORDED = ("/manifest.json", "/catalog/", "/meta/")
PROXY_HOST = "host.docker.internal"


def _base(url):
    url = url.rstrip("/")
    return url[: -len("/manifest.json")] if url.endswith("/manifest.json") else url


class AddonRecorder:
    def __init__(self, upstream, refresh=False):
        self.upstream = _base(upstream)
        self.dir = os.path.join(CACHE, "addon", hashlib.sha1(self.upstream.encode()).hexdigest()[:12])
        os.makedirs(self.dir, exist_ok=True)
        if refresh:
            for f in os.listdir(self.dir):
                os.remove(os.path.join(self.dir, f))
        self.stats = {"replayed": 0, "recorded": 0, "forwarded": 0}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, ctype, body = outer.answer(self.path, self.headers.get("User-Agent"))
                self.send_response(status)
                if ctype:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        # Port 0: two runs against two instances each get their own proxy.
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://{PROXY_HOST}:{self.port}/manifest.json"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _fetch(self, path, agent):
        req = urllib.request.Request(self.upstream + path, headers={"User-Agent": agent or "jfapi"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()
        except OSError:
            return 502, None, b""

    def answer(self, path, agent=None):
        if not path.startswith(RECORDED):
            self.stats["forwarded"] += 1
            return self._fetch(path, agent)
        f = os.path.join(self.dir, hashlib.sha1(path.encode()).hexdigest())
        if os.path.exists(f):
            with open(f, "rb") as h:
                head, body = h.read().split(b"\n", 1)
            self.stats["replayed"] += 1
            return 200, json.loads(head).get("ctype"), body
        status, ctype, body = self._fetch(path, agent)
        if status == 200:  # only answers worth keeping: a failure is asked again next time
            with open(f + ".tmp", "wb") as h:
                h.write(json.dumps({"ctype": ctype}).encode() + b"\n" + body)
            os.replace(f + ".tmp", f)
            self.stats["recorded"] += 1
        return status, ctype, body

    def close(self):
        self.server.shutdown()


def _saved(port):
    return os.path.join(CACHE, f"addon-url-{port}")


def restore_left_over(api, gelato_id, port, log):
    """Points Gelato back at the addon when a run that died left it on its proxy."""
    cfg = api.get(f"/Plugins/{gelato_id}/Configuration")
    saved = _saved(port)
    if f"//{PROXY_HOST}:" in (cfg.get("Url") or "") and os.path.exists(saved):
        with open(saved, encoding="utf-8") as h:
            api.post(f"/Plugins/{gelato_id}/Configuration", {**cfg, "Url": h.read().strip()})
        log("Gelato was still on the proxy of a run that died: pointed back at the addon")
    return api.get(f"/Plugins/{gelato_id}/Configuration")


def switch(api, gelato_id, url, port, keep=None):
    """Points Gelato at `url`; `keep` is the addon URL to remember for a crash."""
    if keep:
        os.makedirs(CACHE, exist_ok=True)
        with open(_saved(port), "w", encoding="utf-8") as h:
            h.write(keep)
    cfg = api.get(f"/Plugins/{gelato_id}/Configuration")
    api.post(f"/Plugins/{gelato_id}/Configuration", {**cfg, "Url": url})
    if not keep and os.path.exists(_saved(port)):
        os.remove(_saved(port))
