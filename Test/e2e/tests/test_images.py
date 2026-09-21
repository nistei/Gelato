DESCRIPTION = "Every kind of image renders: a sample per item type, image type and storage (a Gelato placeholder or a file Jellyfin holds itself), each rendered twice, plus a stream row's poster and a search result's"

SAMPLE = 2  # items per (item type, image type, storage)
WIDTH = 200  # a resized render keeps the bodies small

# MediaBrowser.Model.Entities.ImageType
TYPES = {0: "Primary", 1: "Art", 2: "Backdrop", 3: "Banner", 4: "Logo", 5: "Thumb",
         6: "Disc", 7: "Box", 8: "Screenshot", 9: "Menu", 10: "Chapter", 11: "BoxRear"}

# The AggregateFolder ("root", /config/root) is Jellyfin's internal root over the physical library
# folders: no client ever renders it, and its image is whatever Jellyfin's folder provider copied from
# a child. On a Gelato-only library that child image is a lazy placeholder, so the root ends up with a
# zero-byte poster while every folder a user sees (UserRootFolder, the CollectionFolders) gets a real
# collage. Out of the sample: it says nothing about Gelato's images.
IMAGES = (
    "select replace(replace(b.Type,'MediaBrowser.Controller.Entities.',''),'Jellyfin.Data.Entities.','') t, "
    "i.ImageType, case when i.Path like '%/gelato/images/%' then 'gelato' else 'local' end store, "
    "lower(replace(b.Id,'-','')), b.Name, i.Path "
    "from BaseItemImageInfos i join BaseItems b on b.Id=i.ItemId "
    "where i.ImageType in (0,1,2,3,4,5,6,7,11) and b.Type not like '%AggregateFolder' order by b.Id"
)


def sample(t):
    """[(item type, image type, storage, id, name, path)], at most SAMPLE per group."""
    groups, out = {}, []
    for kind, image_type, store, item_id, name, path in t.db.query(IMAGES):
        key = (kind, image_type, store)
        if groups.get(key, 0) >= SAMPLE:
            continue
        groups[key] = groups.get(key, 0) + 1
        out.append((kind, image_type, store, item_id, name, path))
    return out


def on_disk(t, path):
    """'missing', 'empty', or the file's size."""
    out = t.sh(f"wc -c < '{path}' 2>/dev/null || true").strip()
    if not out:
        return "missing"
    return "empty" if out == "0" else out


def remote_status(t, path):
    """What the image's remote answers today, for an image that did not render: an HTTP status,
    'no sidecar' when there is none, or 'unreachable'. The URL itself is never printed — it can
    carry an API key."""
    url = t.sh(f"cat '{path}.url' 2>/dev/null || true").strip()
    if not url:
        return "no sidecar"
    # -L: metahub answers 307 to a URL that is itself a 404, and the last status is the answer.
    return t.sh(f"curl -sL -o /dev/null -m 20 -w '%{{http_code}}' '{url}' || true").strip() or "unreachable"


def render(t, item_id, image_type):
    st, _, body = t.api.request(f"/Items/{item_id}/Images/{TYPES[image_type]}?maxWidth={WIDTH}")
    return st, len(body)


def run(t):
    picks = sample(t)
    if not picks:
        t.skip("no images on this instance")
    kinds = sorted({(k, TYPES[i], s) for k, i, s, _, _, _ in picks})
    t.log(f"{len(picks)} images over {len(kinds)} combinations of item type, image type and storage:")
    for k in kinds:
        t.log("   ", "/".join(k))

    rendered, gone, broken, empty200 = [], [], [], []
    for kind, image_type, store, item_id, name, path in picks:
        label = f"{kind}/{TYPES[image_type]}/{store} {name} ({item_id[:8]})"
        before = on_disk(t, path)
        st, size = render(t, item_id, image_type)
        if st == 200 and size > 0:
            rendered.append((item_id, image_type, label))
        elif st == 200:
            empty200.append(label)  # what issue 226 was about: a body no client can tell from an image
        else:
            # A remote that is really gone is not a broken render: the client draws its own placeholder.
            status = remote_status(t, path)
            line = f"{label}: {st}, on disk {before}, remote {status}"
            (broken if status in ("200", "no sidecar") else gone).append(line)

    for line in gone:
        t.log("remote gone ", line)
    for line in broken:
        t.log("broken      ", line)
    t.check(not broken, f"{len(rendered)} of {len(picks)} images render, {len(gone)} whose remote is gone answer not found, {len(broken)} broken: {broken[:3]}")
    t.check(not empty200, f"no image answers 200 with an empty body ({len(empty200)}: {empty200[:3]})")

    # The second render is what a client gets most of the time: the file is there now.
    again = [label for item_id, image_type, label in rendered if render(t, item_id, image_type)[0] != 200]
    t.check(not again, f"every image that rendered renders again ({len(again)} did not: {again[:3]})")

    # Gelato's own two paths on top of the item images: a stream row hands out its movie's poster,
    # and a search result that is not in the library is proxied from the addon.
    row = t.row()
    st, size = render(t, row, 0)
    t.check(st == 200 and size > 0, f"a stream row's poster renders: {st}, {size} bytes")

    hits = t.api.search("The Matrix", "Movie", limit=5)
    rows_all = t.db.stream_row_ids()
    hit = next((h["Id"].lower() for h in hits
                if h["Id"].lower() not in rows_all and t.db.item_count(h["Id"]) == 0), None)
    if hit is None:
        t.log("no search result outside the library: the proxied image is not covered in this run")
        return
    st, size = render(t, hit, 0)
    t.check(st == 200 and size > 0, f"a search result's poster is proxied: {st}, {size} bytes")
