"""Fixture picks from the local DB copy: titles by the state of the row that plays first."""
import json, uuid
import dbq

def n(g):
    return uuid.UUID(g).hex if g else None

def rows():
    out = []
    for id, ext, pv, name, ptype, pname, series in dbq.q(
        "select b.Id, b.ExternalId, b.PrimaryVersionId, b.Name, p.Type, p.Name, p.SeriesName from BaseItems b join BaseItems p on p.Id=b.PrimaryVersionId "
            "where b.Tags like '%gelato-stream%'"):
        d = json.loads(ext) if ext and ext.startswith("{") else {}
        out.append({"row": n(id), "owner": n(pv), "kind": ptype.split(".")[-1], "state": d.get("mediaInfo") or "unprobed",
                    "index": d.get("index", 999), "users": d.get("userIds") or [], "title": (series + " / " if series else "") + (pname or "")})
    return out

def titles(kind, state, user=None):
    """Owners whose first row (lowest index) is in the state: [(owner, row, title, nrows)]"""
    by = {}
    for r in rows():
        if r["kind"] == kind and (user is None or any(n(u) == user for u in r["users"])):
            by.setdefault(r["owner"], []).append(r)
    out = []
    for owner, rs in by.items():
        rs.sort(key=lambda r: r["index"])
        if rs[0]["state"] == state:
            out.append((owner, rs[0]["row"], rs[0]["title"], len(rs)))
    return sorted(out)

if __name__ == "__main__":
    import perflib as p
    u = p.api().user
    for kind in ("Movie", "Episode"):
        for state in ("probe", "remuxdb", "unprobed"):
            t = titles(kind, state, u)
            print(kind, state, len(t), [x[2][:28] for x in t[:4]])
