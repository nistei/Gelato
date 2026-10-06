DESCRIPTION = "Search results added to a collection or a playlist, or a new one made with them: every title arrives, never-opened ones too, one or several per request"

import re

TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs",
         "Sinners", "Presence", "Companion", "The Monkey", "Novocaine", "Warfare", "Drop", "Babygirl"]


def run(t):
    user = t.api.user
    made = []  # ids to delete again

    def in_db(item_id):
        return t.db.one("select count(*) from BaseItems where lower(replace(Id,'-',''))=?", (item_id.replace("-", "").lower(),))[0]

    def imdb(hit):
        return re.search(r"tt\d+", hit["Path"]).group(0)

    def fresh_hits():
        """Search hits that are no row in the database, so their id is synthetic. One per title:
        two hits of one title become one item, and the counts below would be off."""
        seen = set()
        for term in TERMS:
            try:
                hits = t.api.search(term, "Movie", limit=8)
            except Exception as e:
                t.log(f"search \"{term}\" failed: {e}")
                continue
            for hit in hits:
                if not re.search(r"tt\d+", hit.get("Path") or "") or hit["Id"] in seen or imdb(hit) in seen:
                    continue
                seen.update((hit["Id"], imdb(hit)))
                if not in_db(hit["Id"]):
                    yield hit

    hits = fresh_hits()

    def take(n, what):
        got = [hit for _, hit in zip(range(n), hits)]
        if len(got) < n:
            t.skip(f"no {n} search hit(s) outside the library left for {what}")
        return got

    def members(path):
        got = t.api.get(path + "&Fields=ProviderIds")
        return sorted((i.get("ProviderIds") or {}).get("Imdb") or i["Name"] for i in got.get("Items", []))

    def add(group, path, listing, wanted, new, what):
        """Adds `new` in one request and expects the group to hold `wanted` afterwards."""
        st, _ = t.api.call("POST", f"{path}{'&' if '?' in path else '?'}ids={','.join(h['Id'] for h in new)}")
        wanted += new
        names = ", ".join(h["Name"] for h in new)
        t.check(st == 204 and members(listing) == sorted(imdb(h) for h in wanted),
                f"{group} add of {what} ({names}): {st}, the {group} holds {members(listing)}")

    try:
        playlist = t.api.post("/Playlists", {"Name": "jfapi-unopened", "Ids": [], "UserId": user, "MediaType": "Video"})["Id"]
        made.append(playlist)
        st, col = t.api.call("POST", "/Collections?name=jfapi-unopened")
        t.equal(st, 200, "an empty collection is created")
        collection = col["Id"]
        made.append(collection)

        # Playlist: the id of a hit nothing has materialized, then two of them in one request.
        path, listing, held = f"/Playlists/{playlist}/Items?userId={user}", f"/Playlists/{playlist}/Items?userId={user}", []
        add("playlist", path, listing, held, take(1, "the playlist"), "a never-opened hit")
        add("playlist", path, listing, held, take(2, "the playlist"), "two never-opened hits in one request")

        # Collection: the same.
        path, listing, held = f"/Collections/{collection}/Items", f"/Items?userId={user}&ParentId={collection}", []
        add("collection", path, listing, held, take(1, "the collection"), "a never-opened hit")
        add("collection", path, listing, held, take(2, "the collection"), "two never-opened hits in one request")

        # An opened hit next to a never-opened one: the opened one's id is known already, and the
        # other must not be passed on as it is because of that.
        opened, unopened = take(2, "the mixed add")
        st, _ = t.api.call("GET", f"/Items/{opened['Id']}?userId={user}")
        t.equal(st, 200, f"{opened['Name']} opens")
        add("collection", path, listing, held, [opened, unopened], "an opened hit and a never-opened one in one request")

        # A new collection or playlist made with items in it, the "new" choice of a client's add
        # dialog: the collection takes its ids as strings in the query, the playlist in its body.
        # The opened hit still goes by the id the search gave it.
        new = [opened] + take(1, "the new collection")
        st, col = t.api.call("POST", f"/Collections?name=jfapi-unopened-new&ids={','.join(h['Id'] for h in new)}")
        got = []
        if st == 200:
            made.append(col["Id"])
            got = members(f"/Items?userId={user}&ParentId={col['Id']}")
        t.check(st == 200 and got == sorted(imdb(h) for h in new),
                f"a new collection with an opened hit and a never-opened one ({', '.join(h['Name'] for h in new)}): {st}, it holds {got}")

        new = [opened] + take(1, "the new playlist")
        st, pl = t.api.call("POST", "/Playlists", {"Name": "jfapi-unopened-new", "Ids": [h["Id"] for h in new], "UserId": user, "MediaType": "Video"})
        got = []
        if st == 200:
            made.append(pl["Id"])
            got = members(f"/Playlists/{pl['Id']}/Items?userId={user}")
        t.check(st == 200 and got == sorted(imdb(h) for h in new),
                f"a new playlist with an opened hit and a never-opened one ({', '.join(h['Name'] for h in new)}): {st}, it holds {got}")
    finally:
        for item_id in made:
            t.api.call("DELETE", f"/Items/{item_id}")
