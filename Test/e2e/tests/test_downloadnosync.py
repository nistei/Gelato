DESCRIPTION = "Download of a movie that has no stream rows yet: a clean answer with a user, never a 500 out of the filter; an API key alone is still refused"

import urllib.error
import urllib.request

from jfapi.probe import log_count
from tests.test_apikeydownload import download, drop_keys, new_key
from tests.test_remuxdb import wait_for

# Jellyfin's error when the stream's own server does not answer: its HttpClient gives up after 100 s.
DEAD_LINK = "HttpClient.Timeout"

RECORD = "PROD-FINDINGS #21"


def run(t):
    movie = t.unsynced_movie()
    t.require(movie, "no movie without stream rows left")
    has_rows = lambda: t.db.one("select count(*) from BaseItems where Tags like '%gelato-stream%' "
                                "and lower(replace(PrimaryVersionId,'-',''))=?", (movie,))[0]
    t.equal(has_rows(), 0, "the movie has no stream rows before the download")

    key = None
    try:
        dead_before = log_count(t, DEAD_LINK)
        st, headers, body = t.api.request(f"/Items/{movie}/Download", {"Range": "bytes=0-0"}, max_bytes=1)
        # A 500 then is the debrid link of the movie's first stream, not Gelato's filter.
        if st == 500 and wait_for(lambda: log_count(t, DEAD_LINK) > dead_before, 6):  # the log file lags
            t.skip("the movie's first stream did not answer within Jellyfin's 100 s (a dead link)")
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
            with urllib.request.urlopen(req, timeout=120) as r:  # over Jellyfin's 100 s for a slow link
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        t.check(status in (200, 206), f"download with an API key that names a user ({status})")
    finally:
        drop_keys(t)
