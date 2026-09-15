"""Playlists in a Jellyfin database (snapshotted with backup(), so a live copy with -wal is fine)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import json
import os
import re
import sqlite3
import sys

src_path = sys.argv[1]
target = sys.argv[2].lower() if len(sys.argv) > 2 else None
snap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plq-snapshot.db")
if os.path.exists(snap):
    os.remove(snap)
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
dst = sqlite3.connect(snap)
src.backup(dst)
src.close()
con = dst
con.row_factory = sqlite3.Row

users = {r["Id"].lower().replace("-", ""): r["Username"] for r in con.execute("select Id, Username from Users")}
print("users:", users)

norm = lambda g: (g or "").lower().replace("-", "")
rows = con.execute(
    "select Id, Name, Path, DateCreated, DateModified, DateLastSaved, Tags, IsFolder, IsVirtualItem, ParentId, "
    "TopParentId, Data, ExternalId, MediaType from BaseItems where Type like '%.Playlist' order by DateCreated").fetchall()
print(f"\nplaylists: {len(rows)}")
for r in rows:
    data = {}
    try:
        data = json.loads(r["Data"] or "{}")
    except Exception:
        pass
    owner = norm(data.get("OwnerUserId"))
    shares = data.get("Shares") or []
    children = con.execute("select count(*) from LinkedChildren where ParentId=?", (r["Id"],)).fetchone()[0]
    mark = "  <==" if target and norm(r["Id"]) == target else ""
    print(f"  {norm(r['Id'])} | {r['Name']} | owner {users.get(owner, owner)} | open {data.get('OpenAccess')} | shares {len(shares)} "
          f"| created {r['DateCreated']} | modified {r['DateModified']} | saved {r['DateLastSaved']} | children {children} "
          f"| path {re.sub(r'https?://\\S+', '<url>', r['Path'] or '')} | tags {r['Tags']} | virtual {r['IsVirtualItem']}{mark}")

if target:
    r = con.execute("select * from BaseItems where lower(replace(Id,'-',''))=?", (target,)).fetchone()
    if r is None:
        print("\ntarget not in this database")
    else:
        print("\ntarget columns:")
        for k in r.keys():
            v = r[k]
            if v is None or v == "" or v == 0:
                continue
            if k == "Data":
                d = json.loads(v)
                d = {kk: vv for kk, vv in d.items() if vv not in (None, "", [], False, 0)}
                print("  Data:", json.dumps(d)[:800])
            else:
                print(f"  {k}: {str(v)[:120]}")
        kids = con.execute(
            "select b.Name, b.Type, lc.SortOrder from LinkedChildren lc left join BaseItems b on b.Id=lc.ChildId where lc.ParentId=? order by lc.SortOrder",
            (r["Id"],)).fetchall()
        print("  linked children:", [(k["Name"], (k["Type"] or "").rsplit(".", 1)[-1]) for k in kids][:20])
