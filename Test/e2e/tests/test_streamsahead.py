DESCRIPTION = "Opening a movie from search asks the addon for its streams while the meta is still on its way: one stream request, started before the meta is answered, taken by the stream sync, and asked again by the next open instead of answered from the first"
DESTRUCTIVE = True  # points Gelato's addon at a stub on the host, inserts a movie from search twice and removes it again

import time
import urllib.parse

from jfapi.bootstrap import GELATO
from tests.test_insert import MOVIE_TERMS, stremio_of
from tests.test_remuxdb import Stub, by_name

META_DELAY = 2.0  # seconds the stub holds back the meta
STREAM_DELAY = 3.0  # and the streams: one after the other they add up, next to each other they do not


def timed_stub(upstream):
    """The addon with answers held back per path prefix (stub.delays: what -> (prefix, seconds))
    and the moment each of them was asked for and answered (stub.events)."""
    stub = Stub(upstream, b"", b"")
    stub.delays, stub.events = {}, []
    handler = stub.server.RequestHandlerClass
    serve = handler.do_GET

    def do_get(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        what = next((w for w, (prefix, _) in stub.delays.items() if path.startswith(prefix)), None)
        if what:
            with stub.lock:
                stub.events.append((what, "asked", time.time()))
            time.sleep(stub.delays[what][1])
        serve(self)
        if what:
            with stub.lock:
                stub.events.append((what, "answered", time.time()))

    handler.do_GET = do_get
    return stub


def run(t):
    cfg_path = f"/Plugins/{GELATO}/Configuration"
    original = t.api.get(cfg_path)
    t.require(original.get("Url"), "no addon URL configured")
    stub = timed_stub(original["Url"])
    restore = {k: original[k] for k in ("Url", "PreProbe") if k in original}
    inserted = []

    in_library = lambda stremio: t.db.one(
        "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

    def find(term=None):
        """(term, hit, stremio id) of a movie the library does not hold."""
        for candidate in ([term] if term else MOVIE_TERMS):
            for hit in t.api.search(candidate, "Movie", limit=8):
                stremio = stremio_of(hit)
                if stremio and not in_library(stremio):
                    return candidate, hit, stremio
        return None, None, None

    def streams(tag):
        return [{"name": f"ahead-{tag}{k}", "description": f"Test.Ahead.{tag}{k}.mkv", "url": f"{stub.base}/clip/{tag}{k}.mkv",
                 "behaviorHints": {"bingeGroup": f"ahead-{tag}{k}", "filename": f"Test.Ahead.{tag}{k}.mkv"}} for k in "12"]

    def events(what, kind):
        with stub.lock:
            return [at for w, k, at in stub.events if (w, k) == (what, kind)]

    try:
        if "ok" not in t.sh(f"curl -s -m 5 -o /dev/null {stub.base}/reachable && echo ok"):
            t.skip(f"the container cannot reach the host on port {stub.port}")
        # No pre-probe: the stub's streams are no videos. Saving also starts Gelato without
        # anything kept from the real addon.
        t.api.post(cfg_path, {**original, "Url": f"{stub.base}/addon/manifest.json", **({"PreProbe": False} if "PreProbe" in original else {})})
        term, hit, stremio = find()
        t.require(hit, "the addon's search offers no movie the library does not hold")
        t.log(f"movie not in the library: {hit['Name']} ({stremio}), search id {hit['Id']}")

        path = f"/stream/movie/{stremio}.json"
        stub.streams[path] = streams("A")
        stub.delays = {"meta": ("/addon/meta/movie/", META_DELAY), "stream": (f"/addon{path}", STREAM_DELAY)}
        started = time.time()
        d = t.api.item(hit["Id"])
        took = time.time() - started
        inserted.append(d.get("Id", "").lower())

        meta_asked, meta_answered = events("meta", "asked"), events("meta", "answered")
        stream_asked = events("stream", "asked")
        t.require(meta_asked and meta_answered, "the open asked the stub for the meta")
        meta_wait = meta_answered[-1] - meta_asked[0]
        t.log(f"open took {took:.1f} s; meta asked {len(meta_asked)}x, {meta_wait:.1f} s in all; streams asked "
              f"{[round(at - started, 2) for at in stream_asked]} s after the open, first meta answer {meta_answered[0] - started:.2f} s after it")
        t.equal(len(stream_asked), 1, "the addon is asked for the movie's streams once")
        if stream_asked:
            t.check(stream_asked[0] < meta_answered[0], "the streams are asked for before the meta is answered")
        t.check(took < meta_wait + STREAM_DELAY,
                f"the open does not wait for the meta and then for the streams ({took:.1f} s, one after the other at least {meta_wait + STREAM_DELAY:.1f} s)")
        t.equal(sorted(by_name(d)), ["ahead-A1", "ahead-A2"], "the movie lists the streams the addon answered")

        # The answer was taken by that sync. Whoever syncs next asks again: the same search
        # result, opened anew after its movie was removed, lists what the addon answers now.
        t.api.delete_inserted(inserted[-1])
        stub.streams[path] = streams("B")
        stub.delays = {"stream": (f"/addon{path}", 0)}
        _, again, _ = find(term)
        t.require(again and stremio_of(again) == stremio, f"the search offers {stremio} again once its movie is removed")
        d = t.api.item(again["Id"])
        inserted.append(d.get("Id", "").lower())
        t.equal(len(events("stream", "asked")), 2, "the second open asks the addon for the streams again")
        t.equal(sorted(by_name(d)), ["ahead-B1", "ahead-B2"], "and lists what the addon answers now, not the first open's answer")
    finally:
        t.api.post(cfg_path, {**t.api.get(cfg_path), **restore})
        for item in {i for i in inserted if i}:
            t.api.delete_inserted(item)
        stub.close()
        left = t.db.one("select count(*) from BaseItems where Path like ?", (f"%host.docker.internal:{stub.port}%",))[0]
        t.equal(left, 0, "no stub row is left")
