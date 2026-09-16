DESCRIPTION = "Sync series trees brings back a deleted episode once with its watch state, replaces placeholder episode metadata, and changes nothing on a second run (prod finding 14)"
DESTRUCTIVE = True  # runs the tree sync over every continuing series of the library

from jfapi.db import STREAM_TAG, norm

TASK = "SyncSeriesTrees"
SEARCH_TERMS = ["Severance", "Slow Horses", "The Last of Us", "Silo", "Shrinking", "Abbott Elementary"]


def continuing(t, series_id):
    return t.api.item(series_id).get("Status") == "Continuing"


def pick_series(t):
    """(series id, inserted by the test): a continuing Gelato series from the library, or one
    inserted from the addon's search."""
    rows = t.db.query(
        "select lower(replace(s.Id,'-','')) from BaseItems s join BaseItemProviders p on p.ItemId=s.Id "
        "and lower(p.ProviderId)='stremio' where s.Type like '%TV.Series' order by random() limit 20")
    for (series_id,) in rows:
        if continuing(t, series_id) and len(t.episodes(series_id, 1)) >= 2:
            return series_id, False
    for term in SEARCH_TERMS:
        for hit in t.api.search(term, "Series", limit=3):
            if not hit.get("Name", "").lower().startswith(term.lower()[:5]):
                continue
            d = t.api.item(hit["Id"])
            series_id = d.get("Id", "").lower()
            if d.get("Status") == "Continuing" and len(t.episodes(series_id, 1)) >= 2:
                t.log(f"inserted from search: {d.get('Name')} ({series_id[:8]})")
                return series_id, True
            t.api.call("DELETE", f"/Items/{series_id}")
    return None, False


def tree(t, series_id):
    """{(season, episode): [ids]} of the series' episodes in the database, stream rows left out."""
    out = {}
    for ep_id, season, index in t.db.query(
            "select lower(replace(Id,'-','')), ParentIndexNumber, IndexNumber from BaseItems "
            "where Type like '%TV.Episode' and lower(replace(SeriesId,'-',''))=? and (Tags is null or Tags not like ?)",
            (norm(series_id), STREAM_TAG)):
        out.setdefault((season, index), []).append(ep_id)
    return out


def run(t):
    series_id, inserted = pick_series(t)
    if series_id is None:
        t.skip("no continuing Gelato series in the library or in the addon's search")
    try:
        before = tree(t, series_id)
        doubled = {k: v for k, v in before.items() if len(v) > 1}
        t.equal(doubled, {}, "no episode number twice before the sync")
        eps = t.episodes(series_id, 1)
        ep = eps[-1]
        ep_id, key = ep["Id"].lower(), (ep.get("ParentIndexNumber"), ep.get("IndexNumber"))
        stremio = t.db.stremio_id(ep_id)
        t.log(f"series {series_id[:8]}: {len(before)} episodes; deleting S{key[0]}E{key[1]} ({ep_id[:8]}, {stremio})")
        t.api.mark_played(ep_id, True)

        # finding 14: an episode created before its release keeps "Episode N" and no overview unless the sync updates it
        first = t.api.get(f"/Items/{eps[0]['Id']}?userId={t.api.user}&Fields=Overview")
        meta = (first.get("Name"), first.get("Overview"))
        placeholder = {**first, "Name": f"Episode {first.get('IndexNumber')}", "Overview": ""}
        t.api.post(f"/Items/{first['Id']}", placeholder)
        t.log(f"S1E{first.get('IndexNumber')} set to placeholder metadata, was {meta[0]!r} with {len(meta[1] or '')} characters of overview")

        t.api.delete(f"/Items/{ep_id}")
        t.equal(t.db.item_count(ep_id), 0, "the episode is gone")
        t.check(key not in tree(t, series_id), "no episode with its number left")

        t.log("== first run")
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"{TASK} finished {msg}")
        after = tree(t, series_id)
        back = after.get(key, [])
        t.equal(len(back), 1, f"S{key[0]}E{key[1]} is back once")
        t.equal(sorted(after), sorted(before), "the same episode numbers as before the delete")
        t.equal({k: v for k, v in after.items() if len(v) > 1}, {}, "no episode number twice")
        if back:
            t.equal(t.db.stremio_id(back[0]), stremio, "the new episode has the addon id of the deleted one")
            t.equal(t.api.user_data(back[0])["Played"], True, "the parked watch state is back on the new episode")
            unchanged = {k: v for k, v in before.items() if k != key and after.get(k) != v}
            t.equal(unchanged, {}, "every other episode kept its id")
        now = t.api.get(f"/Items/{first['Id']}?userId={t.api.user}&Fields=Overview")
        if meta[0] and meta[0] != placeholder["Name"]:
            t.equal(now.get("Name"), meta[0], "the placeholder name is replaced by the addon's")
        if meta[1]:
            t.equal(now.get("Overview"), meta[1], "the empty overview is filled from the addon")

        t.log("== second run")
        status, msg = t.api.run_task(TASK, timeout=1800)
        t.equal(status, "Completed", f"second {TASK} finished {msg}")
        t.check(tree(t, series_id) == after, f"the second run changes nothing ({len(after)} episodes, same ids)")
        if back:
            t.api.mark_played(back[0], False)
    finally:
        if inserted:
            t.api.call("DELETE", f"/Items/{series_id}")
