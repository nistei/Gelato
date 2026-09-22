DESCRIPTION = "Mark played, favorite, rating and a user data update from a search result's context menu: on a result the library does not have the write materializes the title, on one it has it lands on the library item and the next search shows the new state"

import re
import time

# Titles that are not in the prod dump's library. Only the ones the addon still answers with are
# used; the test needs one fresh hit per action and says so when there are too few.
TERMS = ["Heretic", "Nosferatu", "Anora", "Conclave", "Flow", "The Substance", "Civil War", "Longlegs",
         "Sinners", "Presence", "Companion", "The Monkey", "Novocaine", "Warfare", "Drop", "Babygirl",
         "Juror #2", "Y2K", "Wolfs", "Trap", "Speak No Evil", "Blink Twice", "Strange Darling"]
SERIES_TERMS = ["Adolescence", "Chernobyl", "Baby Reindeer", "The Queen's Gambit", "Beef", "Shogun", "Ripley",
                "Severance", "Andor", "Silo", "The Bear", "Slow Horses", "Dark Matter"]

# What a client sends when the user picks an entry from a search result's context menu, on the
# current route and on the legacy /Users/{userId}/... route both. `field` is the flag the answering
# UserItemDataDto must carry.
def writes(user):
    return [
        {"name": "MarkPlayedItem", "path": "/UserPlayedItems/{id}?userId=" + user, "field": "Played"},
        {"name": "MarkPlayedItemLegacy", "path": f"/Users/{user}/PlayedItems/{{id}}", "field": "Played"},
        {"name": "MarkFavoriteItem", "path": "/UserFavoriteItems/{id}?userId=" + user, "field": "IsFavorite"},
        {"name": "MarkFavoriteItemLegacy", "path": f"/Users/{user}/FavoriteItems/{{id}}", "field": "IsFavorite"},
        {"name": "UpdateUserItemRating", "path": "/UserItems/{id}/Rating?likes=true&userId=" + user, "field": "Likes"},
        {"name": "UpdateUserItemRatingLegacy", "path": f"/Users/{user}/Items/{{id}}/Rating?likes=true", "field": "Likes"},
        {"name": "UpdateItemUserData", "path": "/UserItems/{id}/UserData?userId=" + user, "field": "Played",
         "body": {"Played": True, "IsFavorite": True}},
        {"name": "UpdateItemUserDataLegacy", "path": f"/Users/{user}/Items/{{id}}/UserData", "field": "Played",
         "body": {"Played": True, "IsFavorite": True}},
    ]


# Undoing a write on a library item, so the instance is left the way it was found.
UNDO = {
    "Played": ("DELETE", "/UserPlayedItems/{id}?userId={user}", None),
    "IsFavorite": ("DELETE", "/UserFavoriteItems/{id}?userId={user}", None),
    "Likes": ("DELETE", "/UserItems/{id}/Rating?userId={user}", None),
}

CANDIDATE_SQL = (
    "select lower(replace(b.Id,'-','')), b.Name, p.ProviderValue from BaseItems b "
    "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
    "where b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like '%gelato-stream%') "
    "and b.PrimaryVersionId is null and p.ProviderValue like 'tt%' and length(b.Name) > 3 "
    "order by random() limit 40"
)


def stremio_of(hit):
    m = re.search(r"(tt\d+)", hit.get("Path") or "")
    return m.group(1) if m else None


def run(t):
    user = t.api.user
    inserted = set()

    def in_library(stremio, retry=False):
        """How many library items the title has. With retry, a zero is asked again on a new
        snapshot: one copied out between the database and its write-ahead log misses a row the
        server has."""
        sql = ("select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
               "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')")
        for attempt in range(4 if retry else 1):
            n = t.db.one(sql, (stremio,))[0]
            if n:
                return n
            t.db.invalidate()
            time.sleep(1)
        return n

    def library_item(stremio, kind="Movie"):
        """The library id of the title, retried: a snapshot taken right after the write can be
        copied out between the database and its write-ahead log and then misses the new row."""
        sql = ("select lower(replace(b.Id,'-','')) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
               "where p.ProviderValue=? and " + ("b.Type like '%TV.Series'" if kind == "Series" else "(b.Tags is null or b.Tags not like '%gelato-stream%')"))
        for attempt in range(4):
            row = t.db.one(sql, (stremio,))
            if row:
                return row[0]
            t.db.invalidate()
            time.sleep(1)
        return None

    def has_row(item_id):
        return t.db.one("select count(*) from BaseItems where lower(replace(Id,'-',''))=?", (item_id.replace("-", "").lower(),))[0]

    def fresh_hits(terms, kind="Movie"):
        """Search hits whose title is not in the library yet, so their id is synthetic."""
        seen = set()
        for term in terms:
            try:
                hits = t.api.search(term, kind, limit=8)
            except Exception as e:
                t.log(f"search \"{term}\" failed: {e}")
                continue
            for hit in hits:
                stremio = stremio_of(hit)
                if not stremio or hit["Id"] in seen or not hit.get("Name", "").lower().startswith(term.lower()[:4]):
                    continue
                seen.add(hit["Id"])
                if not in_library(stremio):
                    yield hit, stremio

    def detail_works(hit_id):
        """Opening the hit is the action that has always materialized a search result. It is the
        control: an addon that has no meta for a title fails this too, and then the title says
        nothing about the user data writes."""
        st, d = t.api.call("GET", f"/Items/{hit_id}?userId={user}")
        return (st, d.get("Id", "").lower() if isinstance(d, dict) and d.get("Id") else None)

    hits = fresh_hits(TERMS)

    # The premise: a search hit is not a row in the database and carries no user data, so a client
    # renders its played and favorite buttons in the off state and sends the write with this id.
    first = next(hits, None)
    if first is None:
        t.skip("no search hit outside the library among the terms")
    hit, stremio = first
    t.equal(has_row(hit["Id"]), 0, f"the search hit {hit['Name']} ({hit['Id'][:8]}) is no row in the database")
    t.check(hit.get("UserData") is None, f"the search hit carries no UserData: {hit.get('UserData')!r}")

    covered, skipped = [], []
    all_writes = writes(user)

    # One fresh hit per write: the first write materializes the title, so the same hit cannot show
    # the synthetic path twice.
    pending = [(hit, stremio)]
    try:
        for w in all_writes:
            while True:
                if not pending:
                    nxt = next(hits, None)
                    if nxt is None:
                        skipped.append(w["name"])
                        break
                    pending.append(nxt)
                h, s = pending.pop(0)
                st, d = t.api.call("POST", w["path"].format(id=h["Id"]), w.get("body"))
                if st != 200:
                    ctrl_st, ctrl_id = detail_works(h["Id"])
                    if ctrl_st != 200:
                        t.log(f"{w['name']}: {h['Name']} is not insertable at all (detail {ctrl_st}), taking the next hit")
                        continue
                    if ctrl_id:
                        inserted.add(ctrl_id)
                    t.check(False, f"{w['name']} on the never-opened hit {h['Name']}: {st}, while opening the same hit works")
                    covered.append(w["name"])
                    break

                real = library_item(s)
                if real:
                    inserted.add(real)
                t.check(real is not None, f"{w['name']} on {h['Name']}: 200 and the title is in the library")
                t.check(isinstance(d, dict) and d.get(w['field']) is True,
                        f"{w['name']} on {h['Name']}: the answer has {w['field']}=True ({d.get(w['field']) if isinstance(d, dict) else d})")
                if real:
                    t.check(real != h["Id"].replace("-", "").lower(), f"{w['name']} on {h['Name']}: the library id differs from the search hit's id")
                    state = t.api.get(f"/UserItems/{real}/UserData?userId={user}")
                    t.check(state.get(w["field"]) is True, f"{w['name']} on {h['Name']}: the library item has {w['field']}=True")
                    # The client keeps using the search hit's id: the second call must land on the
                    # same item, this time through the remembered redirect and not a new insert.
                    st2, d2 = t.api.call("POST", w["path"].format(id=h["Id"]), w.get("body"))
                    t.check(st2 == 200, f"{w['name']} on {h['Name']}: the hit's id still works after the insert ({st2})")
                    t.equal(in_library(s, retry=True), 1, f"{w['name']} on {h['Name']}: still one item, the second call inserted nothing")
                covered.append(w["name"])
                break

        t.log(f"covered: {', '.join(covered) or 'none'}")
        if skipped:
            t.log(f"no fresh search hit left for: {', '.join(skipped)}")
        t.check(len(covered) >= 4, f"enough fresh hits to cover the writes ({len(covered)} of {len(all_writes)})")

        # A series hit inserts a whole tree, not one item. The write must answer with the series
        # marked, and the episodes must follow as they do for a series in the library.
        for sh, ss in fresh_hits(SERIES_TERMS, "Series"):
            t0 = time.time()
            st, d = t.api.call("POST", f"/UserPlayedItems/{sh['Id']}?userId={user}")
            if st != 200:
                ctrl_st, ctrl_id = detail_works(sh["Id"])
                if ctrl_st != 200:
                    t.log(f"series {sh['Name']} is not insertable at all (detail {ctrl_st}), taking the next hit")
                    continue
                if ctrl_id:
                    inserted.add(ctrl_id)
                t.check(False, f"MarkPlayedItem on the never-opened series hit {sh['Name']}: {st}, while opening the same hit works")
                break
            real = library_item(ss, "Series")
            if real:
                inserted.add(real)
            t.check(real is not None, f"MarkPlayedItem on the series hit {sh['Name']}: 200 and the series is in the library ({time.time() - t0:.0f}s)")
            t.check(isinstance(d, dict) and d.get("Played") is True, f"the series hit {sh['Name']}: the answer has Played=True")
            if real:
                t.check(t.api.item(real, "RecursiveItemCount").get("RecursiveItemCount", 0) > 0,
                        f"the series hit {sh['Name']}: the tree was inserted, not only the series")
            break
        else:
            t.log("no series hit outside the library among the terms")

        # The other half of the context menu: a result of a title the library already has. The
        # search answers for it with the library item, so the write is sent with that id, the
        # state lands on the item itself, and the next search shows it — which is what draws the
        # tick and the resume bar on the card the menu was opened from.
        def materialized_hits():
            """(search hit, library id, stremio id) for titles the library has and the addon
            answers for, one per write. The hit is what a client would open the menu on."""
            for item_id, name, stremio in t.db.query(CANDIDATE_SQL):
                try:
                    hits = t.api.search(name, "Movie", limit=25, fields="Path,ProviderIds")
                except Exception as e:
                    t.log(f"search \"{name}\" failed: {e}")
                    continue
                hit = next((h for h in hits if h["Id"].replace("-", "").lower() == item_id), None)
                if hit is not None:
                    yield hit, item_id, stremio

        known_hits = materialized_hits()
        touched = []
        done, missing = [], []
        for w in all_writes:
            nxt = next(known_hits, None)
            if nxt is None:
                missing.append(w["name"])
                continue
            hit, item_id, stremio = nxt
            name = hit.get("Name")

            t.check(hit.get("UserData") is not None,
                    f"{w['name']}: the search result for {name!r} is the library item, with its user data")

            st, d = t.api.call("POST", w["path"].format(id=hit["Id"]), w.get("body"))
            touched.append((item_id, w["field"]))
            if not t.equal(st, 200, f"{w['name']} on the library title {name!r}"):
                continue
            t.check(isinstance(d, dict) and d.get(w["field"]) is True,
                    f"{w['name']} on {name!r}: the answer has {w['field']}=True")
            # Read whole, not through api.user_data(): that one keeps the played and favourite
            # keys only, and a rating is neither.
            state = t.api.get(f"/UserItems/{item_id}/UserData?userId={user}")
            t.check(state.get(w["field"]) is True,
                    f"{w['name']} on {name!r}: the state is on the library item ({state.get(w['field'])!r})")
            t.equal(in_library(stremio, retry=True), 1, f"{w['name']} on {name!r}: still one item, nothing was inserted")

            again = next((h for h in t.api.search(name, "Movie", limit=25, fields="Path,ProviderIds")
                          if h["Id"].replace("-", "").lower() == item_id), None)
            t.check(again is not None and (again.get("UserData") or {}).get(w["field"]) is True,
                    f"{w['name']} on {name!r}: the next search shows {w['field']} on the result "
                    f"({(again or {}).get('UserData')})")
            done.append(w["name"])

        t.log(f"covered on library titles: {', '.join(done) or 'none'}")
        if missing:
            t.log(f"no library title the addon answers for left for: {', '.join(missing)}")
        t.check(len(done) >= 4, f"enough library titles to cover the writes ({len(done)} of {len(all_writes)})")

        for item_id, field in touched:
            method, path, body = UNDO[field]
            t.api.call(method, path.format(id=item_id, user=user), body)
            if field == "Played":  # the user data update sets the favorite too
                t.api.call("DELETE", f"/UserFavoriteItems/{item_id}?userId={user}")

        # InsertableActionNames also gates the stream sync in the media source decorator, so a
        # user data write must not make Gelato pull an addon's streams for a title nobody asked
        # to play.
        unsynced = t.unsynced_movie()
        if unsynced is None:
            t.log("no unsynced movie in the library, the stream sync check is left out")
        else:
            before = t.db.stream_rows(unsynced)["count"]
            t.api.call("POST", f"/UserFavoriteItems/{unsynced}?userId={user}")
            t.api.call("POST", f"/UserPlayedItems/{unsynced}?userId={user}")
            t.equal(t.db.stream_rows(unsynced)["count"], before,
                    "a favorite and a played write pull no streams for a library movie that was never opened")
            t.api.call("DELETE", f"/UserFavoriteItems/{unsynced}?userId={user}")
            t.api.call("DELETE", f"/UserPlayedItems/{unsynced}?userId={user}")

        # The unmark writes are deliberately not routed through the insert filter. A client offers
        # them only for an item that already carries the state, and that item is in the library --
        # reached through the hit's id, which the filter redirects. Pinned here so the gap is a
        # decision and not a surprise.
        nxt = next(hits, None)
        if nxt is None:
            t.log("no fresh hit left for the unmark check")
        else:
            h, s = nxt
            st, _ = t.api.call("DELETE", f"/UserFavoriteItems/{h['Id']}?userId={user}")
            ctrl_st, ctrl_id = detail_works(h["Id"])
            if ctrl_st != 200:
                t.log(f"unmark check: {h['Name']} is not insertable at all (detail {ctrl_st}), not checked")
            else:
                if ctrl_id:
                    inserted.add(ctrl_id)
                t.equal(st, 404, f"unmarking a never-opened hit ({h['Name']}) is not routed through the insert filter")
                st2, _ = t.api.call("DELETE", f"/UserFavoriteItems/{h['Id']}?userId={user}")
                t.check(st2 == 200, f"unmarking works once the hit's id is known ({st2})")
    finally:
        for item in inserted:
            try:
                t.api.delete(f"/Items/{item}")
            except Exception as e:
                t.log(f"could not remove {item}: {e}")
        t.log(f"removed {len(inserted)} inserted item(s) again")
