DESCRIPTION = "Download of a movie that has no stream rows yet: a clean answer with a user, never a 500 out of the filter; an API key alone is still refused"

import urllib.error
import urllib.request

from tests.test_apikeydownload import download, drop_keys, new_key

RECORD = "PROD-FINDINGS #21"


def run(t):
    movie = t.unsynced_movie()
    t.require(movie, "no movie without stream rows left")
    has_rows = lambda: t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%' "
                                "and lower(replace(PrimaryVersionId,'-',''))=?", (movie,))[0]
    t.equal(has_rows(), 0, "the movie has no stream rows before the download")

    key = None
    try:
        st, headers, body = t.api.request(f"/Items/{movie}/Download", {"Range": "bytes=0-0"}, max_bytes=1)
        t.check(st != 500, f"download with a user token is no 500 ({st})")
        t.check(st in (200, 206), f"download with a user token delivers bytes ({st})")

        key = new_key(t)
        status, _ = download(t, movie, key)
        t.known(status in (200, 206), f"download with an API key and no user answers {status}", RECORD)
        req = urllib.request.Request(
            f"{t.api.base}/Items/{movie}/Download?userId={t.api.user}",
            headers={"Authorization": f'MediaBrowser Client="jfapi", Device="cli", DeviceId="jfapi-apikey", Version="1.0", Token="{key}"',
                     "Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        t.check(status in (200, 206), f"download with an API key that names a user ({status})")
    finally:
        drop_keys(t)
