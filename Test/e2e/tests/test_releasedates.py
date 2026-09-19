DESCRIPTION = "Release dates: a movie TMDB has a past digital release for does not keep the 'no digital release date' sentinel"

import json
import urllib.request

# The key the plugin falls back to when the Jellyfin TMDB plugin has none configured.
TMDB_KEY = "4219e299c89411838049ab0dab19ebd5"
SENTINEL_SQL = (
    "select lower(replace(b.Id,'-','')), b.Name, date(b.PremiereDate), p.ProviderValue from BaseItems b "
    "join BaseItemProviders s on s.ItemId=b.Id and lower(s.ProviderId)='stremio' "
    "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='tmdb' "
    "where b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like '%gelato-stream%') "
    "and b.PrimaryVersionId is null and b.EndDate > '9000' "
    "and b.PremiereDate < datetime('now', '-60 day') order by b.PremiereDate desc limit ?"
)
SAMPLE = 15


def digital_release(tmdb_id):
    """The earliest TMDB digital (type 4) release date for the movie, or None."""
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates?api_key={TMDB_KEY}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "jfapi"}), timeout=30) as r:
        d = json.load(r)
    dates = [x["release_date"][:10] for c in d.get("results") or [] for x in c.get("release_dates") or [] if x.get("type") == 4]
    return min(dates) if dates else None


def run(t):
    import datetime

    status, msg = t.api.run_task("SyncReleaseDates", timeout=3600)
    t.equal(status, "Completed", f"Sync release dates finished {msg}")

    total = t.db.one("select count(*) from BaseItems b join BaseItemProviders s on s.ItemId=b.Id and lower(s.ProviderId)='stremio' "
                     "where b.Type like '%Movies.Movie' and b.PrimaryVersionId is null")[0]
    sentinel = t.db.one("select count(*) from BaseItems b join BaseItemProviders s on s.ItemId=b.Id and lower(s.ProviderId)='stremio' "
                        "where b.Type like '%Movies.Movie' and b.PrimaryVersionId is null and b.EndDate > '9000'")[0]
    t.log(f"Gelato movies: {total}, without a digital release date: {sentinel}")

    rows = t.db.query(SENTINEL_SQL, (SAMPLE,))
    if not rows:
        t.log("no Gelato movie that premiered over 60 days ago still carries the sentinel")
        t.check(True, "every movie that premiered over 60 days ago has a release date")
        return

    # EndDate is what the filter reads, so comparing against it proves nothing about the rule that
    # writes it. Ask TMDB instead: a movie it has a past digital release date for must not be
    # sitting on the "no digital release date" sentinel.
    today = datetime.date.today().isoformat()
    missed, checked, unreachable = [], 0, 0
    for item_id, name, premiere, tmdb_id in rows:
        try:
            digital = digital_release(tmdb_id)
        except Exception as e:
            unreachable += 1
            t.log(f"  {name}: TMDB unreachable ({type(e).__name__})")
            continue
        checked += 1
        if digital is not None and digital <= today:
            missed.append((name, premiere, digital))
            t.log(f"  MISSED {name}: premiered {premiere}, TMDB digital {digital}, EndDate still the sentinel")
    if not checked:
        t.skip(f"TMDB not reachable ({unreachable} attempts)")
    t.log(f"checked {checked} of {len(rows)} sentinel movies against TMDB, {len(missed)} with a past digital release")
    t.check(not missed, f"no movie keeps the sentinel while TMDB has a past digital release date ({len(missed)} of {checked})")
