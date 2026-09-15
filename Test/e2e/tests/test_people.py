DESCRIPTION = "People links survive the stream sync and a person's page lists the movie, not its rows"


def run(t):
    movie = t.movie()
    people_map = lambda item: t.db.one("select count(*) from PeopleBaseItemMap where lower(replace(ItemId,'-',''))=?", (item,))[0]
    t.api.item(movie)  # a freshly inserted movie is still being refreshed: let that settle first
    t.wait(3)
    before = t.api.item(movie, "People").get("People") or []
    map_before = people_map(movie)
    t.log(f"before: {len(before)} people in the API, {map_before} map rows")
    if not before:
        t.skip("the movie has no people")
    t.api.item(movie)  # the sync
    after = t.api.item(movie, "People").get("People") or []
    t.equal(len(after), len(before), "people after the sync")
    t.equal(people_map(movie), map_before, "people map rows after the sync")

    row = t.row(movie)
    t.equal(len(t.api.item(row, "People").get("People") or []), len(before), "people on the row's page")
    t.equal(people_map(row), 0, "people map rows of the row")

    person = before[0]
    listed = t.api.get(f"/Items?userId={t.api.user}&personIds={person['Id']}&Recursive=true&IncludeItemTypes=Movie").get("Items", [])
    ids = {i["Id"].lower() for i in listed}
    rows = t.db.stream_row_ids()
    t.check(movie in ids, f"person page of {person['Name']} lists the movie")
    t.check(not ids & rows, "person page lists no stream rows")
