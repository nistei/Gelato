DESCRIPTION = "Sync release dates and the watch-state repair task leave the stream rows alone"


def run(t):
    movie = t.movie()
    row = t.row(movie)
    t.api.mark_played(movie, False)
    t.api.report("start", movie, row, 0, "tasks")
    t.api.report("progress", movie, row, 600000000, "tasks")
    snapshot = lambda: (t.db.stream_rows(movie), t.db.one(
        "select count(*), max(DateLastSaved), max(DateModified) from BaseItems where Tags like '%gelato-stream%' "
        "and lower(replace(PrimaryVersionId,'-',''))=?", (movie,)), t.api.user_data(movie), t.api.user_data(row))
    before = snapshot()
    t.log("before:", before)

    keys = [x["Key"] for x in t.api.get("/ScheduledTasks")]
    for label, match in (("Sync release dates", "ReleaseDate"), ("Watch-state repair", "RepairWatchState")):
        key = next((k for k in keys if match.lower() in k.lower()), None)
        if key is None:
            t.log(f"{label}: no task with a key containing {match!r} among {keys}")
            t.check(False, f"{label}: task found")
            continue
        status, msg = t.api.run_task(key, timeout=900)
        t.equal(status, "Completed", f"{label} ({key}) finished {msg}")
        after = snapshot()
        t.equal(after[0], before[0], f"{label}: rows, owners, stamps and links unchanged")
        t.equal(after[1], before[1], f"{label}: no row was saved by the task")
        t.equal(after[2], before[2], f"{label}: the movie's watch state unchanged")
        t.equal(after[3], before[3], f"{label}: the row's watch state unchanged")
    t.equal(len(t.api.sources(movie)), before[0]["count"], "the movie still lists its streams")
