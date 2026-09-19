DESCRIPTION = "A global search that asks for more than movies and series keeps the library's results for the other types (Live TV channels, collections)"
DESTRUCTIVE = True  # adds an M3U tuner with two channels for the run of the test and removes it again

import time
import urllib.parse

# What the web client asks for in a global search: getItemTypesFromCollectionType() with no
# collection type. Inside a library it asks for that library's types only, and inside Live TV for
# TvChannel alone — which is why lostb1t/Gelato#162 sees the channels there and nowhere else.
GLOBAL_TYPES = "Movie,Series,Episode,Playlist,MusicAlbum,Audio,TvChannel,PhotoAlbum,Photo,AudioBook,Book,BoxSet"
M3U = "/tmp/gelato-e2e-channels.m3u"
CHANNELS = ["Zappelfrosch News", "Zappelfrosch Kino"]
CHANNEL_TERM = "Zappelfrosch"  # a word no addon answers for, so every hit is the library's own
ADDON_TERM = "star"  # a term the addon does answer for


def search(t, term, types, limit=800):
    path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}"
            f"&IncludeItemTypes={types}&Recursive=true&Limit={limit}")
    st, d = t.api.call("GET", path)
    return st, (d.get("Items", []) if st == 200 and isinstance(d, dict) else [])


def add_tuner(t):
    """An M3U tuner on a file in the container: two channels, whose stream URLs are never opened.
    Returns the tuner's id, or None when Live TV did not pick the channels up."""
    body = "#EXTM3U\n" + "".join(
        f'#EXTINF:-1 tvg-id="gelatoe2e{i}" tvg-name="{name}",{name}\nhttp://127.0.0.1:1/gelatoe2e{i}.ts\n'
        for i, name in enumerate(CHANNELS, 1))
    t.sh(f"cat > {M3U} <<'EOF'\n{body}EOF\n")
    st, tuner = t.api.call("POST", "/LiveTv/TunerHosts", {
        "Type": "m3u", "Url": M3U, "FriendlyName": "gelato-e2e",
        "AllowHWTranscoding": False, "EnableStreamLooping": False})
    if st != 200 or not isinstance(tuner, dict):
        t.log(f"the tuner was not accepted: HTTP {st} {str(tuner)[:160]}")
        return None
    for _ in range(30):
        found = t.api.get(f"/LiveTv/Channels?userId={t.api.user}").get("Items", [])
        if len(found) >= len(CHANNELS):
            t.log(f"channels: {sorted(c['Name'] for c in found)}")
            return tuner["Id"]
        time.sleep(2)
    t.log("the tuner was added but no channel showed up")
    return tuner["Id"]


def remove_tuner(t, tuner_id):
    st, d = t.api.call("DELETE", f"/LiveTv/TunerHosts?id={tuner_id}")
    t.log(f"tuner removed: HTTP {st}")
    t.sh(f"rm -f {M3U}")


def run(t):
    # Gelato answers the search for movies and series itself. The request is one call for every
    # type the client wants at once, so replacing the whole answer drops the types Gelato has
    # nothing to say about, and they are gone from the global search (lostb1t/Gelato#162).
    tuner_id = add_tuner(t)
    try:
        channels = t.api.get(f"/LiveTv/Channels?userId={t.api.user}").get("Items", [])
        if channels:
            st, scoped = search(t, CHANNEL_TERM, "TvChannel")
            t.equal(st, 200, "the channel-only search answers")
            t.equal(len(scoped), len(CHANNELS), "the channel-only search, what Live TV sends, finds the channels")

            st, items = search(t, CHANNEL_TERM, GLOBAL_TYPES)
            names = sorted(i["Name"] for i in items if i.get("Type") == "TvChannel")
            t.equal(st, 200, "the global search answers")
            t.log(f"global search for '{CHANNEL_TERM}': {len(items)} items, channels {names}")
            t.equal(len(names), len(CHANNELS), "the global search finds the same channels")
        else:
            t.log("no Live TV channel on this instance, the channel checks are left out")

        # The same hole, without a tuner: a collection the library owns.
        boxsets = t.api.get(f"/Items?userId={t.api.user}&IncludeItemTypes=BoxSet&Recursive=true&Limit=50").get("Items", [])
        if not boxsets:
            t.log("no collection in the library, the collection check is left out")
        else:
            box = boxsets[0]
            st, scoped = search(t, box["Name"], "BoxSet")
            t.check(any(i["Id"] == box["Id"] for i in scoped), f"the collection-only search finds '{box['Name']}'")

            st, items = search(t, box["Name"], GLOBAL_TYPES)
            t.log(f"global search for '{box['Name']}': {len(items)} items, "
                  f"{sorted({i.get('Type') for i in items})}")
            t.check(any(i["Id"] == box["Id"] for i in items), f"the global search finds '{box['Name']}' too")

        # And the addon's own results are still what the search is for.
        st, items = search(t, ADDON_TERM, GLOBAL_TYPES)
        kinds = {}
        for i in items:
            kinds[i.get("Type")] = kinds.get(i.get("Type"), 0) + 1
        t.log(f"global search for '{ADDON_TERM}': {kinds}")
        t.check(kinds.get("Movie", 0) > 0 and kinds.get("Series", 0) > 0,
                f"the addon's movies and series are still in the global search: {kinds}")
    finally:
        if tuner_id:
            remove_tuner(t, tuner_id)
