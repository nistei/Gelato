"""Jellyfin API access for the dev instances.

`Api(base, user, password)` is one logged-in user. `call()` returns `(status, json or text)`;
`get()`, `post()` and `delete()` return the body and raise `ApiError` on a non-2xx status. The
token is cached in `.cache/` per port and user; delete it after an instance `-Reset`.

The module-level `call()` / `session()` keep the single-user scripts in `tools/` working; they
read `JF_URL` (default http://localhost:8096), `JF_ADMINUSER` (admin) and `JF_ADMINPASSWORD` (empty).
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid


def search_result_id(external_id, kind="movie"):
    """The id a search result carries for a stremio id: MD5 of the item's stremio uri, read as a
    guid the way .NET does (StremioUri.ToGuid). A title the library already has is answered with
    the library item instead, so this is the id of a stand-in — and the id such a result keeps in
    the page URL after it was opened."""
    uri = f"stremio://{kind}/{external_id}"
    return uuid.UUID(bytes_le=hashlib.md5(uri.encode()).digest()).hex


CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
BASE = os.environ.get("JF_URL", "http://localhost:8096")
USER = os.environ.get("JF_ADMINUSER", "admin")
PW = os.environ.get("JF_ADMINPASSWORD", "")


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path} -> {status} {str(body)[:200]}")
        self.status, self.body = status, body


class Api:
    def __init__(self, base=BASE, user=USER, password=PW):
        self.base = base.rstrip("/")
        self.name = user
        self.password = password
        self.token = None
        self.user = None  # the user's id, set by ensure()
        self.on_call = None  # e.g. Db.invalidate: a call may change what the database shows

    @property
    def port(self):
        return self.base.rsplit(":", 1)[-1]

    @property
    def token_file(self):
        return os.path.join(CACHE, f"token-{self.port}-{self.name}.json")

    def headers(self, token=None):
        auth = f'MediaBrowser Client="jfapi", Device="cli", DeviceId="jfapi-cli-{self.name}", Version="1.0"'
        if token or self.token:
            auth += f', Token="{token or self.token}"'
        return {"Content-Type": "application/json", "Authorization": auth}

    def call(self, method, path, body=None, raw=False, token=None, timeout=120):
        """(status, parsed json | text). Errors return (status, body text). {user} in the path
        becomes the user id."""
        if self.user:
            path = path.replace("{user}", self.user)
        if self.on_call:
            self.on_call()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers(token))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode()
                return r.status, txt if raw else (json.loads(txt) if txt else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    def request(self, path, headers=None, timeout=120, max_bytes=None):
        """(status, headers, bytes) for binary answers such as images and downloads. Pass
        max_bytes for a download: the body is a whole stream file otherwise."""
        if self.on_call:
            self.on_call()
        req = urllib.request.Request(self.base + path, headers={**self.headers(), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers, r.read(max_bytes) if max_bytes else r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def login(self):
        st, d = self.call("POST", "/Users/AuthenticateByName", {"Username": self.name, "Pw": self.password})
        if st != 200:
            raise ApiError("POST", "/Users/AuthenticateByName", st, d)
        self.token, self.user = d["AccessToken"], d["User"]["Id"]
        os.makedirs(CACHE, exist_ok=True)
        with open(self.token_file, "w") as f:
            json.dump({"token": self.token, "user": self.user}, f)
        return self

    def ensure(self):
        """Logged in, from the cached token when it still works."""
        if self.token:
            return self
        if os.path.exists(self.token_file):
            with open(self.token_file) as f:
                d = json.load(f)
            self.token, self.user = d["token"], d["user"]
            if self.call("GET", "/Users/Me")[0] == 200:
                return self
            self.token = self.user = None
        return self.login()

    # ---- convenience: raise on errors

    def _ok(self, method, path, body=None):
        st, d = self.call(method, path, body)
        if st >= 300:
            raise ApiError(method, path, st, d)
        return d

    def get(self, path):
        return self._ok("GET", path)

    def post(self, path, body=None):
        return self._ok("POST", path, body)

    def delete(self, path):
        return self._ok("DELETE", path)

    # Opening a title answers before Gelato's background save of the insert has run. A delete that
    # lands in between is undone: the save writes the item back, without its stream rows, and a
    # series tree fails on a foreign key (500). Known and pre-existing (PROD-FINDINGS #25); the
    # tests wait it out instead of tripping over it. The save was seen ~0.7 s after the insert.
    INSERT_SETTLE_SECONDS = 3

    def settle_insert(self):
        """Waits until an insert's background save has run, before a delete."""
        time.sleep(self.INSERT_SETTLE_SECONDS)

    def delete_inserted(self, item_id):
        """Deletes an item the test inserted, once the insert has settled. (status, body); never
        raises, so it is safe in a finally."""
        self.settle_insert()
        return self.call("DELETE", f"/Items/{item_id}")

    # ---- items

    def item(self, item_id, fields=None):
        """The item's DTO for this user (opening a Gelato movie/episode syncs its streams)."""
        q = f"&Fields={fields}" if fields else ""
        return self.get(f"/Items/{item_id}?userId={self.user}{q}")

    def sources(self, item_id):
        """Ids of the item's media sources as this user sees them."""
        return [s["Id"] for s in self.item(item_id).get("MediaSources") or []]

    def user_data(self, item_id):
        d = self.get(f"/UserItems/{item_id}/UserData?userId={self.user}")
        return {k: d.get(k) for k in ("Played", "PlaybackPositionTicks", "LastPlayedDate", "PlayCount", "IsFavorite")}

    def resume(self):
        """Ids in Continue Watching."""
        d = self.get(f"/UserItems/Resume?userId={self.user}&mediaTypes=Video&limit=50")
        return [i["Id"] for i in d.get("Items", [])]

    def mark_played(self, item_id, played=True):
        return self._ok("POST" if played else "DELETE", f"/UserPlayedItems/{item_id}?userId={self.user}")

    def report(self, kind, item_id, source_id, position, session="jfapi"):
        """A playback report through the session endpoints, like a client's."""
        body = {"ItemId": item_id, "MediaSourceId": source_id, "PositionTicks": position, "PlaySessionId": session,
                "CanSeek": True, "IsPaused": False, "PlayMethod": "DirectStream"}
        path = {"start": "/Sessions/Playing", "progress": "/Sessions/Playing/Progress", "stop": "/Sessions/Playing/Stopped"}[kind]
        return self.post(path, body)

    def search(self, term, kind="Movie", limit=5, fields="Path", retries=1):
        """Search results (Gelato answers from the addon, which can time out: one retry)."""
        path = (f"/Items?userId={self.user}&searchTerm={urllib.request.quote(term)}&IncludeItemTypes={kind}"
                f"&Recursive=true&Limit={limit}&Fields={fields}")
        for attempt in range(retries + 1):
            st, d = self.call("GET", path)
            if st == 200:
                return d.get("Items", [])
            if attempt == retries:
                raise ApiError("GET", path, st, d)
            time.sleep(3)

    # ---- tasks and users

    def task(self, key):
        return next(t for t in self.get("/ScheduledTasks") if t["Key"] == key)

    def run_task(self, key, timeout=600):
        """Runs the scheduled task and waits: (status, message)."""
        task = self.task(key)
        self.post(f"/ScheduledTasks/Running/{task['Id']}")
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(2)
            t = self.get(f"/ScheduledTasks/{task['Id']}")
            if t["State"] == "Idle":
                r = t.get("LastExecutionResult") or {}
                return r.get("Status"), (r.get("ErrorMessage") or "")[:200]
        return "Running", f"still running after {timeout}s"

    def wait_tasks_idle(self, key, timeout=900):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.task(key)["State"] == "Idle":
                return True
            time.sleep(3)
        return False

    def users(self):
        return self.get("/Users")

    def reset_password(self, user_id):
        """Clears another user's password (administrator only)."""
        return self.post(f"/Users/{user_id}/Password", {"ResetPassword": True})


# ---- compatibility for the scripts in tools/ (single user from the environment)

_default = None


def _api():
    global _default
    if _default is None:
        _default = Api(BASE, USER, PW).ensure()
    return _default


def session():
    """(token, userId) of the environment's user, logging in once per port and user."""
    a = _api()
    return a.token, a.user


def call(method, path, body=None, token=None, raw=False):
    """One API call as the environment's user; {user} in the path becomes the user id."""
    return _api().call(method, path, body, raw=raw, token=token)


def _headers(token=None):
    return _api().headers(token)
