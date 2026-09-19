DESCRIPTION = "A library image whose remote is gone: the endpoint must not answer 200 with the empty placeholder, and the dead URL must not be fetched again on every request (issue 226)"

from jfapi.db import norm

REQUESTS = 3
# The instance's own server answers 404 with an empty body for an unknown path, exactly like the
# metahub stills of issue 226 — without the test depending on a remote host.
DEAD_URL = "http://127.0.0.1:8096/gelato-e2e-missing-still.jpg"
BACKUP = "/tmp/jfapi-deadimage-backup"

PICK = (
    "select lower(replace(b.Id,'-','')), b.Name, i.Path from BaseItemImageInfos i "
    "join BaseItems b on b.Id=i.ItemId where i.ImageType=0 and i.Path like '%/gelato/images/%' "
    "and b.Type like ? and (b.Tags is null or b.Tags not like '%gelato-stream%') limit 1"
)


def pick(t):
    """(id, name, image path) of a library item whose Primary image is a Gelato placeholder, an
    episode for choice — the issue's own case is an episode still that has not appeared yet."""
    for kind in ("%TV.Episode", "%Movies.Movie"):
        row = t.db.one(PICK, (kind,))
        if row:
            return row
    return None, None, None


def attempts(t, log, item_id):
    """How often the decorator logged a failed download for the item."""
    dashed = t.db.one("select Id from BaseItems where lower(replace(Id,'-',''))=?", (norm(item_id),))[0]
    out = t.sh(f"grep -i -c 'download failed for {dashed}' {log} || true").strip()
    return int(out or 0)


def run(t):
    item_id, name, path = pick(t)
    if item_id is None:
        t.skip("no library item with a Gelato image placeholder on this instance")
    sidecar = path + ".url"
    if "No such file" in t.sh(f"cat {sidecar} 2>&1 || true"):
        t.skip(f"{name} has no .url sidecar next to {path}")
    original = t.sh(f"cat {sidecar}").strip()
    log = t.sh("ls -t /config/log/log_*.log | head -1").strip()
    t.log(f"item {item_id[:8]} ({name}), placeholder {path}, log {log}")

    try:
        # The remote is gone: the placeholder is empty again and its URL answers 404.
        t.sh(f"cp {path} {BACKUP}; : > {path}; printf '%s' '{DEAD_URL}' > {sidecar}")
        before = attempts(t, log, item_id)

        answers = []
        for _ in range(REQUESTS):
            st, headers, body = t.api.request(f"/Items/{item_id}/Images/Primary")
            answers.append((st, len(body), headers.get("Content-Type")))
            t.wait(1)
        t.wait(2)  # the log is written asynchronously
        tried = attempts(t, log, item_id) - before
        t.log(f"{REQUESTS} requests: {answers}, {tried} download attempt(s) logged")

        if any(st == 200 and size > 0 for st, size, _ in answers):
            t.skip("the image was served with content: the dead URL did not reach the download path")

        empty = [(st, ct) for st, size, ct in answers if st == 200 and size == 0]
        t.check(not empty, f"no request answers 200 with an empty body ({len(empty)} of {REQUESTS} did: {empty[:1]})")
        t.check(tried <= 1, f"the dead URL is fetched at most once for {REQUESTS} requests: {tried} attempt(s)")

        size = t.sh(f"wc -c < {path}").strip()
        tags = (t.api.item(item_id).get("ImageTags") or {})
        t.log(f"placeholder after the requests: {size} bytes, ImageTags {sorted(tags)}")
    finally:
        # Put the real URL back and leave the placeholder empty, so the next render downloads it.
        t.sh(f"printf '%s' '{original}' > {sidecar}; cp {BACKUP} {path}; rm -f {BACKUP}")
        t.check(t.sh(f"cat {sidecar}").strip() == original, "the item's own image URL is restored")
