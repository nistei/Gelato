DESCRIPTION = "Download and a full metadata refresh taken on a version page (a stream row)"

import time


def run(t):
    movie = t.movie()
    row = t.row(movie)
    n = len(t.api.sources(movie))

    st, headers, body = t.api.request(f"/Items/{row}/Download", {"Range": "bytes=0-0"}, max_bytes=1)
    t.log("download:", st, headers.get("Content-Type"), (headers.get("Content-Disposition") or "")[:60])
    t.check(st == 200, f"download from the row's page answers {st}")
    t.check("attachment" in (headers.get("Content-Disposition") or ""), "download has a file name")

    t.api.post(f"/Items/{row}/Refresh?metadataRefreshMode=FullRefresh&imageRefreshMode=FullRefresh&replaceAllMetadata=true&replaceAllImages=true")
    time.sleep(8)
    r = t.db.one("select Name, Tags, lower(replace(PrimaryVersionId,'-','')), (select count(*) from BaseItemProviders p where p.ItemId=b.Id) "
                 "from BaseItems b where lower(replace(Id,'-',''))=?", (row,))
    t.log("row after refresh:", r)
    t.check(r is not None, "the row still exists")
    t.equal(r[2], movie, "the row still belongs to the movie")
    t.check("gelato-stream" in (r[1] or ""), "the row keeps its stream tag")
    t.check(r[3] >= 2, "the row keeps its provider ids")
    d = t.api.item(row)
    t.check("Primary" in (d.get("ImageTags") or {}), "the row's page still shows the movie's images")
    t.equal(len(t.api.sources(movie)), n, "the movie lists the same sources")
