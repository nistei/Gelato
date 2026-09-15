DESCRIPTION = "Jellyfin's 'Download missing subtitles' task runs over the whole library without errors"
DESTRUCTIVE = True  # runs the subtitle task over the whole library


def run(t):
    subs = lambda: t.db.one(
        "select count(*), count(distinct m.ItemId), sum(case when b.Tags like '%gelato-stream%' then 1 else 0 end) "
        "from MediaStreamInfos m join BaseItems b on b.Id=m.ItemId where m.StreamType='Subtitle' and m.IsExternal=1")
    before = subs()
    t.log("external subtitle streams before:", before)
    status, msg = t.api.run_task("DownloadSubtitles", timeout=1800)
    t.equal(status, "Completed", f"task finished: {msg}")
    after = subs()
    t.log("external subtitle streams after:", after)
    t.check(after[0] >= before[0], "no external subtitle streams lost")
    numbered = t.sh("find /media/metadata /config/data -type f -name '*.[a-z][a-z].[0-9].*' 2>/dev/null | wc -l").strip()
    t.equal(numbered, "0", "no numbered subtitle copies (the already-saved check works)")
