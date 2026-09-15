"""One line per movie: what the page lists and what the database holds for its stream rows.

    python t_state.py <movieId> [<movieId> ...] [--visit]

--visit opens each movie first (triggers the sync when the cache allows it).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import sys

from jfapi.db import query
from jfapi.api import call, session

token, user = session()
movies = [m for m in sys.argv[1:] if not m.startswith("--")]

for movie in movies:
    name, sources = None, None
    if "--visit" in sys.argv:
        st, d = call("GET", f"/Items/{movie}?userId={user}", token=token)
        name, sources = d.get("Name"), len(d.get("MediaSources") or [])
    cols, rows = query(
        "select count(*), sum(case when lower(replace(b.PrimaryVersionId,'-',''))=? then 1 else 0 end), "
        "sum(case when b.PrimaryVersionId is null then 1 else 0 end), "
        "sum(case when b.DateLastRefreshed is null or b.DateLastRefreshed < '0002' then 1 else 0 end) "
        "from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where b.Tags like '%gelato-stream%' and p.ProviderValue=(select ProviderValue from BaseItemProviders "
        "where lower(replace(ItemId,'-',''))=? and lower(ProviderId)='stremio')", (movie, movie))
    cols, links = query("select count(*) from LinkedChildren where lower(replace(ParentId,'-',''))=? and ChildType=3", (movie,))
    n, owned, unowned, unstamped = rows[0]
    print(f"{movie[:8]} {name or ''}: sources {sources} | rows {n} owned {owned or 0} unowned {unowned or 0} "
          f"unstamped {unstamped or 0} | version links {links[0][0]}")
