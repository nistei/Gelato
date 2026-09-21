DESCRIPTION = "Plugins listening to user data and playback events see the movie only, once per change (needs the Webhook plugin)"

import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer

WEBHOOK = "71552a5a-5c5c-4350-a2ae-ebe451a30173"

class Listener:
    def __init__(self):
        self.events = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
                try:
                    outer.events.append(json.loads(raw))
                except Exception:
                    outer.events.append({"_raw": raw})
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        # Port 0: the operating system hands out a free one, so two suite runs against two
        # instances do not fight over a fixed port (on Windows the second bind succeeds and
        # silently receives nothing).
        self.server = HTTPServer(("0.0.0.0", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://host.docker.internal:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def drain(self, wait=5):
        time.sleep(wait)
        out, self.events = self.events, []
        return out

    def close(self):
        self.server.shutdown()


def run(t):
    plugin = next((p for p in t.api.get("/Plugins")
                   if p.get("Id", "").replace("-", "").lower() == WEBHOOK.replace("-", "")), None)
    if plugin is None:
        t.skip("Webhook plugin not installed (POST /Packages/Installed/Webhook?assemblyGuid=71552a5a-5c5c-4350-a2ae-ebe451a30173, then restart)")
    # A disabled plugin is listed but not loaded, so its configuration endpoint answers 404 (prod has
    # it off, so every instance from the dump starts that way).
    if plugin.get("Status") != "Active":
        t.skip(f"Webhook plugin is {plugin.get('Status')}: enable it in the dashboard and restart")
    listener = Listener()
    try:
        if "ok" not in t.sh(f"curl -s -m 5 {listener.url}/ && echo ok"):
            t.skip(f"the container cannot reach the host on port {listener.port}")
        old = t.api.get(f"/Plugins/{WEBHOOK}/Configuration")
        cfg = {**old, "GenericOptions": [{
            "WebhookName": "jfapi", "WebhookUri": f"{listener.url}/hook",
            "NotificationTypes": ["PlaybackStart", "PlaybackProgress", "PlaybackStop", "UserDataSaved"],
            "EnableMovies": True, "EnableEpisodes": True, "EnableVideos": True, "SendAllProperties": True, "EnableWebhook": True,
            "Headers": [], "Fields": [], "UserFilter": []}]}
        t.api.post(f"/Plugins/{WEBHOOK}/Configuration", cfg)
        try:
            movie = t.movie()
            row = t.row(movie)
            runtime = t.api.item(movie).get("RunTimeTicks") or 0
            listener.drain(3)

            def saved(events):
                """Counter of (item, reason) of the UserDataSaved events."""
                return Counter(((e.get("ItemId") or "").replace("-", "").lower()[:8], e.get("SaveReason"))
                               for e in events if e.get("NotificationType") == "UserDataSaved")

            row_ids = {r[:8] for r in t.db.row_users(movie)}

            def expect(label, events, movie_reasons, other=0):
                # Only this movie and its rows: sessions of other items (left by other tests or
                # clients) keep producing events of their own.
                s = saved(events)
                rows = sum(n for (item, _), n in s.items() if item in row_ids)
                t.log(f"{label}: {dict(s)} + {Counter(e.get('NotificationType') for e in events if e.get('NotificationType') != 'UserDataSaved')}")
                t.equal(Counter(r for (item, r), n in s.items() for _ in range(n) if item == movie[:8]), Counter(movie_reasons), f"{label}: saves on the movie")
                t.equal(rows, other, f"{label}: saves on rows")

            t.api.mark_played(movie, False)
            expect("mark unplayed on the movie", listener.drain(), ["TogglePlayed"])
            t.api.report("start", row, row, 0, "hook")
            expect("playback start on the row", listener.drain(), ["PlaybackStart"])
            t.api.report("progress", row, row, int(runtime * 0.4), "hook")
            expect("progress on the row", listener.drain(), ["PlaybackProgress"])
            t.api.report("stop", row, row, int(runtime * 0.97), "hook")
            ev = listener.drain(6)
            expect("stop at 97% on the row", ev, ["PlaybackFinished", "TogglePlayed"])
            t.equal(sum(e.get("NotificationType") == "PlaybackStop" and (e.get("ItemId") or "").replace("-", "").lower()[:8] in row_ids | {movie[:8]} for e in ev), 1,
                    "one PlaybackStop session event")
            t.api.mark_played(row, False)
            expect("mark unplayed from the row's page", listener.drain(), ["TogglePlayed"])
            t.api.mark_played(row, True)
            expect("mark played from the row's page", listener.drain(), ["TogglePlayed"])
            t.api.post(f"/UserFavoriteItems/{row}?userId={t.api.user}")
            expect("favourite from the row's page", listener.drain(), ["UpdateUserRating"])
            t.api.delete(f"/UserFavoriteItems/{row}?userId={t.api.user}")
            listener.drain(2)
            t.api.mark_played(movie, False)
        finally:
            t.api.post(f"/Plugins/{WEBHOOK}/Configuration", old)
    finally:
        listener.close()
