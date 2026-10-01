DESCRIPTION = "Adding a never-opened search result to a collection or a playlist: the title arrives, or the request fails; it is never a success that adds nothing"

import re
import time

TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs",
         "Sinners", "Presence", "Companion", "The Monkey", "Novocaine", "Warfare", "Drop", "Babygirl"]

RECORD = "PROD-FINDINGS #20"


def run(t):
    user = t.api.user
    made = []  # (kind, id) to delete again

    def in_db(item_id):
        return t.db.one("select count(*) from BaseItems where lower(replace(Id,'-',''))=?", (item_id.replace("-", "").lower(),))[0]

    def fresh_hits():
        """Search hits that are no row in the database, so their id is synthetic."""
        seen = set()
        for term in TERMS:
            try:
                hits = t.api.search(term, "Movie", limit=8)
            except Exception as e:
                t.log(f"search \"{term}\" failed: {e}")
                continue
            for hit in hits:
                if hit["Id"] in seen or not re.search(r"tt\d+", hit.get("Path") or ""):
                    continue
                seen.add(hit["Id"])
                if not in_db(hit["Id"]):
                    yield hit

    def openable(hit_id):
        """The control: opening the hit is what has always materialized it."""
        st, _ = t.api.call("GET", f"/Items/{hit_id}?userId={user}")
        return st == 200

    hits = fresh_hits()
    try:
        playlist = t.api.post("/Playlists", {"Name": "jfapi-unopened", "Ids": [], "UserId": user, "MediaType": "Video"})["Id"]
        made.append(("playlist", playlist))
        st, col = t.api.call("POST", "/Collections?name=jfapi-unopened")
        t.equal(st, 200, "an empty collection is created")
        collection = col["Id"]
        made.append(("collection", collection))

        # Playlist: the id of a hit nothing has materialized.
        hit = next(hits, None)
        if hit is None:
            t.skip("no search hit outside the library among the terms")
        st, _ = t.api.call("POST", f"/Playlists/{playlist}/Items?ids={hit['Id']}&userId={user}")
        time.sleep(2)
        items = t.api.get(f"/Playlists/{playlist}/Items?userId={user}")
        n = items.get("TotalRecordCount", len(items.get("Items", [])))
        if st < 300 and n == 0:
            t.known(False, f"playlist add of the never-opened hit {hit['Name']}: {st} but the playlist is empty", RECORD)
        else:
            t.check(st >= 400 or n == 1, f"playlist add of the never-opened hit {hit['Name']}: {st}, {n} item(s) in the playlist")

        # Collection: same, with a second hit.
        hit = next(hits, None)
        if hit is None:
            t.log("no second fresh hit left for the collection")
            return
        st, _ = t.api.call("POST", f"/Collections/{collection}/Items?ids={hit['Id']}")
        time.sleep(2)
        got = t.api.get(f"/Items?userId={user}&ParentId={collection}")
        n = got.get("TotalRecordCount", 0)
        t.known(st < 300 and n == 1, f"collection add of the never-opened hit {hit['Name']}: {st}, {n} item(s) in the collection", RECORD)

        # Control: once the hit was opened the same add works, which is why the web client does.
        hit = next(hits, None)
        if hit is not None and openable(hit["Id"]):
            st, _ = t.api.call("POST", f"/Collections/{collection}/Items?ids={hit['Id']}")
            t.equal(st, 204, f"collection add of {hit['Name']} after opening it")
    finally:
        for kind, item_id in made:
            t.api.call("DELETE", f"/Items/{item_id}")
