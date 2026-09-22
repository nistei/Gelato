DESCRIPTION = "A search result opened with an API key (no user behind the token) materializes, on the /Items route and on the user-scoped one"
DESTRUCTIVE = True  # creates an API key and inserts the titles it opens; removes both again

import json
import re
import urllib.error
import urllib.request

APP = "jfapi-apikeyinsert"
DEVICE = "jfapi-apikeyinsert"
TERMS = ["Arrival", "Whiplash", "Sicario", "Prisoners", "Gone Girl", "Drive", "Moonlight", "Tenet"]


def new_key(t):
    """A fresh API key: a token that authenticates without a user, as a script or another server
    uses it. Jellyfin puts the empty guid in the request's UserId claim for it."""
    drop_keys(t)
    t.api.post(f"/Auth/Keys?app={APP}")
    return next(k["AccessToken"] for k in t.api.get("/Auth/Keys")["Items"] if k.get("AppName") == APP)


def drop_keys(t):
    for k in t.api.get("/Auth/Keys").get("Items", []):
        if k.get("AppName") == APP:
            t.api.delete(f"/Auth/Keys/{k['AccessToken']}")


def get(t, path, token):
    """(status, json or text) for a request made with this token alone. The database snapshot is
    dropped by hand: these calls do not go through the API object that does it."""
    req = urllib.request.Request(
        t.api.base + path,
        headers={"Authorization": f'MediaBrowser Client="jfapi", Device="cli", '
                                  f'DeviceId="{DEVICE}", Version="1.0", Token="{token}"'})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            txt = r.read().decode()
            status, body = r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode(errors="replace")[:200]
    t.db.invalidate()
    return status, body


def stremio_of(hit):
    m = re.search(r"(tt\d+)", hit.get("Path") or "")
    return m.group(1) if m else None


def run(t):
    def in_library(stremio):
        return t.db.one(
            "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
            "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

    def item_of(stremio):
        rows = t.db.query(
            "select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id "
            "and lower(p.ProviderId)='stremio' where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')",
            (stremio,))
        return rows[0][0] if rows else None

    taken = set()

    def fresh_hit():
        """A search result for a movie the library does not hold yet, used once per run."""
        for term in TERMS:
            for hit in t.api.search(term, "Movie", limit=8):
                stremio = stremio_of(hit)
                if not stremio or stremio in taken or not hit.get("Name", "").lower().startswith(term.lower()[:5]):
                    continue
                t.db.invalidate()
                if not in_library(stremio):
                    taken.add(stremio)
                    return hit, stremio
        return None, None

    def opens_with(label, path_of, token):
        """Opens a result nobody opened before through `path_of`, and removes what it inserted."""
        hit, stremio = fresh_hit()
        if hit is None:
            t.log(f"{label}: no movie outside the library among the search terms")
            return
        status, d = get(t, path_of(hit["Id"]), token)
        item = d.get("Id", "").lower() if isinstance(d, dict) and "Id" in d else None
        t.log(f"{label}: {hit['Name']} ({stremio}) answered HTTP {status} as {(item or '-')[:8]}")
        try:
            if not t.equal(status, 200, f"{label}: the search result opens"):
                return
            t.check(item and item != hit["Id"].lower(), f"{label}: the result became a library item")
            t.equal(in_library(stremio), 1, f"{label}: one library item for the title")
        finally:
            for gone in {item, item_of(stremio)} - {None}:
                t.api.delete_inserted(gone)
            t.equal(in_library(stremio), 0, f"{label}: the title was removed again")

    key = new_key(t)
    try:
        # Jellyfin's two detail routes. The user is only named in the query and in the route: the
        # claim an API key leaves behind is the empty guid, and taking it as the answer left the
        # filter without a user, so nothing materialized and the request was a 404.
        opens_with("API key, /Items/{id}?userId=", lambda hit: f"/Items/{hit}?userId={t.api.user}", key)
        opens_with("API key, /Users/{user}/Items/{id}", lambda hit: f"/Users/{t.api.user}/Items/{hit}", key)

        # The same route with a user's token, so a failure above is about the API key and not
        # about opening search results in general.
        opens_with("user token, /Items/{id}?userId=", lambda hit: f"/Items/{hit}?userId={t.api.user}", t.api.token)

        # An API key that names no user stays without one: the plugin must not pick a user of its
        # own, and Jellyfin answers the synthetic id itself.
        hit, stremio = fresh_hit()
        if hit is None:
            return
        status, _ = get(t, f"/Items/{hit['Id']}", key)
        t.log(f"API key, no user named: {hit['Name']} ({stremio}) answered HTTP {status}")
        t.equal(in_library(stremio), 0, "a request that names no user materializes nothing")
        if in_library(stremio):
            t.api.delete_inserted(item_of(stremio))
    finally:
        drop_keys(t)
