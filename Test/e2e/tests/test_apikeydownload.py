DESCRIPTION = "Download with an API key (no user behind the token): the native library's items answer, and the download filter is out of the way (issue 187)"
DESTRUCTIVE = True  # adds a library with local files and creates an API key; removes both again

import urllib.error
import urllib.request

from jfapi.native import add_library, remove_libraries, rows_under, write_audio, write_videos
from jfapi.probe import log_count

FILTER = "Gelato.Filters.DownloadFilter"

APP = "jfapi-apikey"
MOVIES_LIB, MOVIES_PATH = "jfapi-apikey-movies", "/tmp/jfapi-apikey-movies"
SHOWS_LIB, SHOWS_PATH = "jfapi-apikey-shows", "/tmp/jfapi-apikey-shows"
MUSIC_LIB, MUSIC_PATH = "jfapi-apikey-music", "/tmp/jfapi-apikey-music"
LIBRARIES = [(MOVIES_LIB, "movies", MOVIES_PATH), (SHOWS_LIB, "tvshows", SHOWS_PATH),
             (MUSIC_LIB, "music", MUSIC_PATH)]
MOVIE = "Jfapi Apikey Movie (2001)"
SHOW = "Jfapi Apikey Show (2003)"
EPISODE = f"{SHOW}/Season 01/Jfapi Apikey Show S01E01.mkv"
# The issue names audio as well, and the filter never looks at the item's type: it fails before
# that. One track, to have the third kind of item the report lists.
TRACK = "Jfapi Apikey Artist/Jfapi Apikey Album/01 Jfapi Apikey Track.mp3"


def new_key(t):
    """A fresh API key: a token that authenticates without a user, as a script or another server
    uses it. Jellyfin puts the empty guid in the request's UserId claim for it."""
    drop_keys(t)
    t.api.post(f"/Auth/Keys?app={APP}")
    return next(k["AccessToken"] for k in t.api.get("/Auth/Keys")["Items"] if k.get("AppName") == APP)


def drop_keys(t):
    for k in t.api.get("/Auth/Keys").get("Items", []):
        if k.get("AppName") == APP:
            t.api.delete(f"/Auth/Keys/{k['AccessToken']}")


def download(t, item_id, token):
    """(status, first byte) of a download request made with this token alone."""
    req = urllib.request.Request(
        f"{t.api.base}/Items/{item_id}/Download",
        headers={"Authorization": f'MediaBrowser Client="jfapi", Device="cli", '
                                  f'DeviceId="jfapi-apikey", Version="1.0", Token="{token}"',
                 "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(1)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]


def run(t):
    remove_libraries(t, LIBRARIES)
    write_videos(t, [f"{MOVIES_PATH}/{MOVIE}/{MOVIE}.mkv", f"{SHOWS_PATH}/{EPISODE}"])
    write_audio(t, [f"{MUSIC_PATH}/{TRACK}"])
    key = None
    try:
        _, movies = add_library(t, MOVIES_LIB, "movies", MOVIES_PATH, "Movie", 1)
        _, episodes = add_library(t, SHOWS_LIB, "tvshows", SHOWS_PATH, "Episode", 1)
        _, tracks = add_library(t, MUSIC_LIB, "music", MUSIC_PATH, "Audio", 1)
        if len(movies) != 1 or len(episodes) != 1 or len(tracks) != 1:
            return
        key = new_key(t)

        for kind, item in (("movie", movies[0]), ("episode", episodes[0]), ("track", tracks[0])):
            status, body = download(t, item, key)
            t.log(f"native {kind} with the API key: {status} {body[:60]}")
            # 400 is what the filter answered: it read the empty guid out of the claim and asked
            # the user manager for it, which throws, and the exception ends the request.
            t.check(status in (200, 206), f"the native {kind} downloads with an API key: {status}")

        # The same items through a user's token, so a failure above is about the API key and not
        # about downloads in general.
        for kind, item in (("movie", movies[0]), ("episode", episodes[0]), ("track", tracks[0])):
            status, _ = download(t, item, t.api.token)
            t.check(status in (200, 206), f"the native {kind} downloads with a user token: {status}")

        # Gelato's own items: the movie's stream row is what a client downloads from a version
        # page, and it is served by the filter, which needs the user a token carries.
        movie = t.movie()
        row = t.row(movie)
        status, _ = download(t, row, t.api.token)
        t.check(status in (200, 206), f"a stream row downloads with a user token: {status}")
        # With an API key there is no user whose streams could be handed out, so the filter has to
        # leave the request to Jellyfin, which refuses it: a Gelato item has no file to download.
        # What must not happen is the filter itself ending the request, which is what the stack
        # traces in the log are counted for.
        before = log_count(t, FILTER)
        status, body = download(t, row, key)
        t.log(f"stream row with the API key: {status} {body[:60]}")
        t.equal(log_count(t, FILTER), before, "a stream row with an API key does not fail inside the filter")
    finally:
        if key:
            drop_keys(t)
        remove_libraries(t, LIBRARIES)
        t.equal(rows_under(t, [MOVIES_PATH, SHOWS_PATH, MUSIC_PATH]), 0, "native items removed again")
