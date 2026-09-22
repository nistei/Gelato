DESCRIPTION = "The tree sync refreshes a series whose only id is the addon's own one, the state an anime catalog leaves behind"
DESTRUCTIVE = True  # deletes an episode, edits the series' provider ids and runs the tree sync over the library

TASK = "SyncSeriesTrees"
SEARCH_TERMS = ["Severance", "Slow Horses", "The Last of Us", "Silo", "Shrinking", "Abbott Elementary"]


def pick_series(t):
    """(series id, inserted by the test): a continuing Gelato series with episodes in season 1."""
    rows = t.db.query(
        "select lower(replace(s.Id,'-','')) from BaseItems s join BaseItemProviders p on p.ItemId=s.Id "
        "and lower(p.ProviderId)='stremio' where s.Type like '%TV.Series' order by random() limit 20")
    for (series_id,) in rows:
        if t.api.item(series_id).get("Status") == "Continuing" and len(t.episodes(series_id, 1)) >= 2:
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
            t.api.delete_inserted(series_id)
    return None, False


def provider_ids(t, series_id):
    return t.api.get(f"/Items/{series_id}").get("ProviderIds") or {}


def set_provider_ids(t, series_id, ids):
    """Replaces the series' provider ids, the way the metadata editor does."""
    item = t.api.get(f"/Items/{series_id}")
    item["ProviderIds"] = ids
    t.api.post(f"/Items/{series_id}", item)


def gelato_numbers(t, series_id, season):
    """Episode numbers of that season that Gelato itself put there. Jellyfin's own
    TmdbMissingEpisodeProvider fills a hole with a virtual episode of its own, so counting every
    episode the season lists would call a deleted Gelato episode restored when it is not."""
    season_id = next((s["Id"] for s in t.api.get(f"/Shows/{series_id}/Seasons?userId={t.api.user}").get("Items", [])
                      if s.get("IndexNumber") == season), None)
    if season_id is None:
        return []
    eps = t.api.get(f"/Shows/{series_id}/Episodes?userId={t.api.user}&seasonId={season_id}&Fields=Path").get("Items", [])
    return sorted(e.get("IndexNumber") for e in eps
                  if e.get("IndexNumber") is not None and (e.get("Path") or "").startswith("gelato://"))


def series_with_stremio_id(t, stremio_id):
    """Ids of the library's series carrying that Stremio id — a second one means the sync
    created a copy instead of finding the series again."""
    return [r[0] for r in t.db.query(
        "select lower(replace(s.Id,'-','')) from BaseItems s join BaseItemProviders p on p.ItemId=s.Id "
        "where s.Type like '%TV.Series' and lower(p.ProviderId)='stremio' and p.ProviderValue=?", (stremio_id,))]


def run(t):
    series_id, inserted = pick_series(t)
    if series_id is None:
        t.skip("no continuing Gelato series in the library or in the addon's search")

    before = provider_ids(t, series_id)
    stremio_id = next((v for k, v in before.items() if k.lower() == "stremio"), None)
    if not stremio_id:
        t.skip("the picked series carries no Stremio id")

    episode = t.episodes(series_id, 1)[-1]
    season, index = episode.get("ParentIndexNumber"), episode.get("IndexNumber")
    t.log(f"series {series_id[:8]}: provider ids {before}, deleting S{season}E{index} ({episode['Id'][:8]})")

    try:
        t.api.call("DELETE", f"/Items/{episode['Id']}")
        t.check(index not in gelato_numbers(t, series_id, season), f"S{season}E{index} gone after the delete")

        # What an anime catalog leaves behind: the addon hands out a kitsu:/mal:/anilist: id and
        # many titles have no IMDb or TMDB id, so the item carries the addon's id and nothing else.
        set_provider_ids(t, series_id, {"Stremio": stremio_id})
        t.equal(provider_ids(t, series_id), {"Stremio": stremio_id}, "series left with the addon's id only")

        status, message = t.api.run_task(TASK)
        t.equal(status, "Completed", f"{TASK} finished{(': ' + message) if message else ''}")

        after = gelato_numbers(t, series_id, season)
        t.check(index in after, f"S{season}E{index} back after the sync (season {season}: {len(after)} episodes)")
        t.equal(len(series_with_stremio_id(t, stremio_id)), 1, "still one series for that Stremio id")
    finally:
        if inserted:
            t.api.delete_inserted(series_id)
        else:
            set_provider_ids(t, series_id, before)
            t.log(f"provider ids restored: {provider_ids(t, series_id)}")
