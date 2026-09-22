DESCRIPTION = "A series inserted from search gives every season the addon's poster for that season number (keyed map or legacy list), and each one renders the addon's image (series removed again)"

import hashlib
import json
import re
import urllib.request

PLUGIN = "94ea4e14-8163-4989-96fe-0a2094bc2d6a"
# Long-running shows with specials: the season numbers and the list positions part ways there.
SERIES_TERMS = ["Breaking Bad", "Better Call Saul", "The Sopranos", "Rick and Morty", "Game of Thrones",
                "Stranger Things", "The Office", "Friends", "Doctor Who", "The Simpsons"]


def addon_meta(t, stremio_id):
    """The series meta the addon answers, asked the way Gelato asks."""
    base = (t.api.get(f"/Plugins/{PLUGIN}/Configuration").get("Url") or "").rstrip("/")
    if base.endswith("/manifest.json"):
        base = base[: -len("/manifest.json")]
    req = urllib.request.Request(f"{base}/meta/series/{stremio_id}.json", headers={"User-Agent": "jfapi"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode()).get("meta") or {}


def expected_posters(meta):
    """{season number: url} and the format it came in. AIOMetadata 3.0 keys seasonPosters by
    season number; before that it sent seasonPosterByNumber next to a list ordered like the
    provider's seasons, and before that the list alone."""
    extras = meta.get("app_extras") or {}
    for field in ("seasonPosterByNumber", "seasonPosters"):
        keyed = extras.get(field)
        if isinstance(keyed, dict):
            out = {int(k): v for k, v in keyed.items() if re.fullmatch(r"\d+", str(k)) and isinstance(v, str) and v.strip()}
            return out, f"keyed ({field})"
    ordered = extras.get("seasonPosters")
    if not isinstance(ordered, list) or not ordered:
        return {}, "none"
    seasons = sorted({v["season"] for v in meta.get("videos") or [] if isinstance(v.get("season"), int)})
    if len(seasons) == len(ordered):
        pairs = zip(seasons, ordered)
    else:
        first = 0 if 0 in seasons else 1
        pairs = ((first + i, url) for i, url in enumerate(ordered))
    return {n: url for n, url in pairs if isinstance(url, str) and url.strip()}, "list"


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "jfapi"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def stored_poster(t, season):
    """The URL in the season's primary image sidecar, or "". Retried on a new snapshot: one copied
    out between the database and its write-ahead log misses rows the server has."""
    for attempt in range(4):
        row = t.db.query(
            "select Path from BaseItemImageInfos where ImageType=0 and lower(replace(ItemId,'-',''))=?", (season,))
        path = row[0][0] if row else None
        sidecar = t.sh(f"cat '{path}.url' 2>/dev/null || true").strip() if path else ""
        if sidecar:
            return sidecar
        t.db.invalidate()
        t.wait(1)
    return ""


def digest(body):
    return hashlib.sha256(body).hexdigest()[:12]


def run(t):
    in_library = lambda stremio: t.db.one(
        "select count(*) from BaseItems b join BaseItemProviders p on p.ItemId=b.Id and lower(p.ProviderId)='stremio' "
        "where p.ProviderValue=? and (b.Tags is null or b.Tags not like '%gelato-stream%')", (stremio,))[0]

    # A season keeps the poster it was created with, so only a series inserted now shows what this
    # build does with the addon's answer. The first title outside the library whose meta carries
    # season posters for at least two seasons.
    pick = None
    for term in SERIES_TERMS:
        for hit in t.api.search(term, "Series", limit=5):
            m = re.search(r"(tt\d+)", hit.get("Path") or "")
            if not m or not hit.get("Name", "").lower().startswith(term.lower()[:5]) or in_library(m.group(1)):
                continue
            meta = addon_meta(t, m.group(1))
            posters, shape = expected_posters(meta)
            if len(posters) >= 2:
                pick = (hit, m.group(1), meta, posters, shape)
                break
        if pick:
            break
    if pick is None:
        t.skip("no series outside the library whose meta carries season posters")
    hit, stremio, meta, posters, shape = pick
    t.log(f"{hit['Name']} ({stremio}): the addon sends season posters {shape} for seasons {sorted(posters)}")

    # A meta the plugin cannot read fails the whole click, not only the posters: a 500 here.
    st, d = t.api.call("GET", f"/Items/{hit['Id']}?userId={t.api.user}")
    series = d.get("Id", "").lower() if st == 200 and isinstance(d, dict) else ""
    try:
        t.check(series and series != hit["Id"].lower(), f"the click inserted the series ({st}, {series[:8]})")
        if not series:
            return
        # The click can answer before the season tree is built: on an addon meta that is not
        # cached yet the seasons follow a few seconds later.
        want = {v["season"] for v in meta.get("videos") or [] if isinstance(v.get("season"), int)}
        for attempt in range(30):
            seasons = t.api.get(f"/Shows/{series}/Seasons?userId={t.api.user}").get("Items", [])
            numbered = {s["IndexNumber"]: s["Id"].lower().replace("-", "") for s in seasons if s.get("IndexNumber") is not None}
            if want <= set(numbered):
                break
            t.wait(1)
        t.log(f"{len(seasons)} seasons in the library: {sorted(numbered)}")
        covered = sorted(set(numbered) & set(posters))
        t.check(len(covered) >= 2, f"seasons with an addon poster: {covered}")

        series_poster = meta.get("poster")
        rendered = {}
        for n in covered:
            season = numbered[n]
            sidecar = stored_poster(t, season)
            # URLs are compared, never printed: an addon URL can carry a key.
            t.check(sidecar == posters[n], f"season {n}: the stored poster is the addon's poster for season {n}"
                    + ("" if sidecar == posters[n] else
                       f" (stored is {'none' if not sidecar else 'season ' + str(next((k for k, v in posters.items() if v == sidecar), '?')) if sidecar != series_poster else 'the series poster'})"))

            st, _, body = t.api.request(f"/Items/{season}/Images/Primary")
            t.check(st == 200 and len(body) > 0, f"season {n}: the poster renders ({st}, {len(body)} bytes)")
            if st != 200 or not body:
                continue
            want = download(posters[n])
            same = digest(body) == digest(want)
            t.log(f"season {n}: rendered {len(body)} bytes {digest(body)}, addon image {len(want)} bytes {digest(want)}")
            t.check(same, f"season {n}: the rendered poster is the addon's image, byte for byte")
            rendered[n] = digest(body)

        # A season off by one would still render: the other seasons prove each got its own image.
        distinct = len(set(rendered.values()))
        t.check(distinct == len(rendered), f"{len(rendered)} seasons render {distinct} different posters")
    finally:
        if series:
            t.api.delete(f"/Items/{series}")
        t.equal(in_library(stremio), 0, "the series was removed again")
