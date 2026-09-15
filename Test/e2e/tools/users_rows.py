"""Users of the instance, and which of them the existing stream rows belong to (Gelato's userIds)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import json
from collections import Counter, defaultdict

from jfapi.db import query
from jfapi.api import call, session

token, user = session()
st, users = call("GET", "/Users", token=token)
names = {}
for u in users:
    p = u["Policy"]
    names[u["Id"].lower()] = u["Name"]
    print(u["Id"], u["Name"], "| admin", p.get("IsAdministrator"), "| all folders", p.get("EnableAllFolders"),
          "| folders", len(p.get("EnabledFolders") or []), "| blocked tags", p.get("BlockedTags"),
          "| max rating", p.get("MaxParentalRating"), "| disabled", p.get("IsDisabled"))

cols, rows = query(
    "select lower(replace(b.Id,'-','')) id, b.Type, b.ExternalId, lower(replace(b.PrimaryVersionId,'-','')) pv, p.ProviderValue stremio "
    "from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
    "where b.Tags like '%gelato-stream%'")
print("\nstream rows:", len(rows))
per_title = defaultdict(Counter)
kinds = Counter()
for id_, typ, ext, pv, stremio in rows:
    try:
        uids = json.loads(ext or "{}").get("userIds") or []
    except Exception:
        uids = ["?"]
    key = tuple(sorted(names.get(u.lower(), u[:8]) for u in uids))
    per_title[stremio][key] += 1
    kinds[(typ.rsplit(".", 1)[-1], pv is not None)] += 1
print("by type / has PrimaryVersionId:", dict(kinds))
print("\ntitles whose rows are split between user sets (movie titles first):")
shown = 0
for stremio, sets in sorted(per_title.items(), key=lambda kv: (":" in kv[0], -len(kv[1]))):
    if len(sets) > 1 or any(len(k) > 1 for k in sets):
        print(" ", stremio, dict(sets))
        shown += 1
    if shown >= 12:
        break
