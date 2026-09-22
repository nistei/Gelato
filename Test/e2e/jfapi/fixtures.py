"""Test items picked from the instance: movies with several streams, a series with a full first
season, a second user. Explicit ids from the command line take precedence; picks are memoized
for the run."""
import random

from .db import STREAM_TAG, norm

MOVIE_SQL = (
    "select lower(replace(b.Id,'-','')), b.Name from BaseItems b "
    "join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
    "where b.Type like '%Movies.Movie' and (b.Tags is null or b.Tags not like ?) and b.PrimaryVersionId is null "
    "and b.ProductionYear between 2005 and 2024 and b.CommunityRating >= 6.5 "
)


class Fixtures:
    def __init__(self, api, db, overrides=None, log=print):
        self.api, self.db, self.log = api, db, log
        self.given = {k: norm(v) for k, v in (overrides or {}).items() if v}
        self.cache = {}
        self.used = set()

    def _pick_movie(self, key, min_sources=2, unsynced=False):
        if key in self.cache:
            return self.cache[key]
        if key in self.given:
            self.cache[key] = self.given[key]
            self.used.add(self.given[key])
            return self.given[key]
        extra = " and not exists (select 1 from BaseItems r where r.Tags like ? and lower(replace(r.PrimaryVersionId,'-',''))=lower(replace(b.Id,'-','')))" if unsynced else ""
        params = (STREAM_TAG, STREAM_TAG) if unsynced else (STREAM_TAG,)
        rows = self.db.query(MOVIE_SQL + extra + " order by random() limit 12", params)
        if not rows:  # a small library: any Gelato movie, then one inserted from search
            rows = self.db.query(MOVIE_SQL.replace("and b.ProductionYear between 2005 and 2024 and b.CommunityRating >= 6.5 ", "") + extra + " order by random() limit 12", params)
        if not rows and not unsynced:
            rows = self.insert_from_search("Movie")
        for item_id, name in rows:
            if item_id in self.used:
                continue
            n = len(self.api.sources(item_id)) if not unsynced else min_sources
            if n >= min_sources:
                self.log(f"fixture {key}: {name} ({item_id[:8]}, {n} sources)" if not unsynced else f"fixture {key}: {name} ({item_id[:8]}, not synced yet)")
                self.cache[key] = item_id
                self.used.add(item_id)
                return item_id
        if unsynced:
            return None
        for item_id, name in self.insert_from_search("Movie"):
            if item_id not in self.used:
                self.log(f"fixture {key}: {name} ({item_id[:8]})")
                self.cache[key] = item_id
                self.used.add(item_id)
                return item_id
        raise RuntimeError(f"no movie with at least {min_sources} streams among 12 random picks; pass --{key}")

    def movie(self):
        """A Gelato movie with at least two streams, synced for the first user."""
        return self._pick_movie("movie")

    def movie2(self):
        return self._pick_movie("movie2")

    def movies(self, n):
        return [self._pick_movie(f"movie{i}" if i > 1 else "movie") for i in range(1, n + 1)]

    def unsynced_movie(self):
        """A Gelato movie without stream rows yet (its first visit runs the full sync), or None
        when the library has none."""
        return self._pick_movie("unsynced", unsynced=True)

    def row(self, movie=None):
        """A non-first stream row of the movie."""
        movie = movie or self.movie()
        if self.given.get("row") and movie == self.given.get("movie"):
            return self.given["row"]
        srcs = self.api.sources(movie)
        rows = [s for s in srcs if s != movie]
        if not rows:
            raise RuntimeError(f"{movie[:8]} has no second stream")
        return rows[min(1, len(rows) - 1)] if len(rows) > 1 else rows[0]

    def series(self, min_episodes=3):
        """A Gelato series of at most 60 episodes, none watched by the user, whose season 1 has at
        least three episodes with streams."""
        if "series" in self.cache:
            return self.cache["series"]
        if "series" in self.given:
            self.cache["series"] = self.given["series"]
            return self.given["series"]
        rows = self.db.query(
            "select lower(replace(s.Id,'-','')), s.Name from BaseItems s "
            "join BaseItemProviders p on p.ItemId=s.Id and lower(p.ProviderId)='stremio' "
            "where s.Type like '%TV.Series' and (select count(*) from BaseItems e where e.Type like '%TV.Episode' "
            "and e.ParentIndexNumber=1 and (e.Tags is null or e.Tags not like ?) and e.SeriesId=s.Id) >= ? "
            # short series only: the tests that mark or delete episodes get slow on a long one
            "and (select count(*) from BaseItems e where e.Type like '%TV.Episode' and e.SeriesId=s.Id) <= 60 "
            # nothing watched by the user yet: Next Up would start after the last watched episode
            "and not exists (select 1 from UserData u join BaseItems e on e.Id=u.ItemId where e.SeriesId=s.Id "
            "and u.Played=1 and lower(replace(u.UserId,'-',''))=?) "
            "order by random() limit 8", (STREAM_TAG, min_episodes, norm(self.api.user)))
        if not rows:
            rows = self.insert_from_search("Series")
        for item_id, name in rows:
            eps = self.episodes(item_id, 1)
            if len(eps) < min_episodes:
                continue
            if len(self.api.sources(eps[0]["Id"])) >= 2:
                self.log(f"fixture series: {name} ({item_id[:8]}, {len(eps)} episodes in season 1)")
                self.cache["series"] = item_id
                return item_id
        raise RuntimeError("no series with a streamed first season among 8 random picks; pass --series")

    SEARCH_TERMS = {"Movie": ["Inception", "Interstellar", "The Dark Knight", "Dune", "Oppenheimer", "Parasite"],
                    "Series": ["Chernobyl", "Severance", "Adolescence", "Fallout", "Silo", "The Bear"]}

    def insert_from_search(self, kind):
        """[(id, name)] of one title inserted from the addon's search, for an instance without
        candidates of its own."""
        for term in self.SEARCH_TERMS[kind]:
            for hit in self.api.search(term, kind, limit=5):
                if not hit.get("Name", "").lower().startswith(term.lower()[:5]):
                    continue
                d = self.api.item(hit["Id"])
                item = d.get("Id", "").lower()
                if item in self.used:
                    continue
                if kind == "Movie":
                    ok = len(d.get("MediaSources") or []) >= 2
                else:
                    eps = self.episodes(item, 1)
                    ok = len(eps) >= 3 and len(self.api.sources(eps[0]["Id"])) >= 2
                if ok:
                    self.log(f"inserted from search for the run: {d.get('Name')} ({item[:8]})")
                    return [(item, d.get("Name"))]
                self.api.delete_inserted(item)
        return []

    def episodes(self, series, season_number=1):
        """Episode DTOs of one season, in order."""
        st, d = self.api.call("GET", f"/Shows/{series}/Seasons?userId={self.api.user}")
        if st == 404 and series == self.cache.get("series"):  # gone (a test deleted it): pick another
            del self.cache["series"]
            return self.episodes(self.series(), season_number)
        seasons = d.get("Items", []) if st == 200 else []
        season = next((s for s in seasons if s.get("IndexNumber") == season_number), None)
        if season is None:
            return []
        eps = self.api.get(f"/Shows/{series}/Episodes?seasonId={season['Id']}&userId={self.api.user}").get("Items", [])
        return sorted(eps, key=lambda e: e.get("IndexNumber") or 0)

    def stremio_id(self, item_id):
        return self.db.stremio_id(item_id)
