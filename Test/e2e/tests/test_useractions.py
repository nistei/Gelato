DESCRIPTION = "Mark played, favorite, rating and a user data update on a search result that was never opened: the write materializes the title instead of answering 404"

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


def stremio_of(hit):
    m = re.search(r"(tt\d+)", hit.get("Path") or "")
    return m.group(1) if m else None


def run(t):
    user = t.api.user
    inserted = set()

    def in_library(stremio):
        return t.db.one(
            "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
            "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

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
                    t.equal(in_library(s), 1, f"{w['name']} on {h['Name']}: still one item, the second call inserted nothing")
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

        # A title that is already in the library comes back from search under a synthetic id too:
        # the addon answers the Movie and Series search and Jellyfin's own results for those types
        # are excluded, so every hit is built from addon metadata. The write must find the
        # existing item instead of inserting a second one.
        known = t.movie()
        row = t.db.one(
            "select b.Name, p.ProviderValue from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
            "where lower(replace(b.Id,'-',''))=?", (known,))
        if row is None:
            t.log("the picked library movie has no stremio id, the already-in-the-library check is left out")
        else:
            name, stremio_known = row
            t.api.call("DELETE", f"/UserFavoriteItems/{known}?userId={user}")
            hit = next((h for h in t.api.search(name, "Movie", limit=10) if stremio_of(h) == stremio_known), None)
            if hit is None:
                t.log(f"the addon's search for \"{name}\" does not return {stremio_known}, the already-in-the-library check is left out")
            else:
                t.check(hit["Id"].replace("-", "").lower() != known,
                        f"the library movie {name} comes back from search under a synthetic id ({hit['Id'][:8]} vs {known[:8]})")
                t.check(hit.get("UserData") is None, f"the search hit for the library movie carries no UserData: {hit.get('UserData')!r}")
                st, d = t.api.call("POST", f"/UserFavoriteItems/{hit['Id']}?userId={user}")
                t.equal(st, 200, f"favoriting the search hit of a title already in the library ({name})")
                t.equal(in_library(stremio_known), 1, f"{name} is still one item, the write inserted no duplicate")
                t.check(t.api.user_data(known)["IsFavorite"] is True, f"{name} is favorited on the library item")
                # What the client shows afterwards: the search is answered from the addon, so the
                # hit carries no state whatever the write did. Recorded, not required.
                again = next((h for h in t.api.search(name, "Movie", limit=10) if stremio_of(h) == stremio_known), None)
                t.log(f"the search hit for {name} after the write: id {(again or {}).get('Id', '-')[:8]}, UserData {(again or {}).get('UserData')!r}")
                t.api.call("DELETE", f"/UserFavoriteItems/{known}?userId={user}")

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
