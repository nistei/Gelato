"""DTO pairing when the DTO stage drops an item the user may not see.

Blocks one of <blockedMovie>'s tags for the second user, then asks for [blockedMovie, streamRow]
by id as that user. TotalRecordCount vs returned items shows whether the query or the DTO stage
dropped the movie; the row's DTO must still get its movie's images and people.
Runs as the administrator; the second user comes from JF_SECONDUSER (default jfapi-second) and JF_SECONDPASSWORD.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import os
import sys

from jfapi import api as jf
from jfapi.db import query
from jfapi.api import call, session

blocked_movie, visible_movie = sys.argv[1], sys.argv[2]
user2_name = os.environ.get("JF_SECONDUSER", "jfapi-second")

admin_token, admin = session()

# The stream row: sync the visible movie first (as the administrator) and take its first row.
st, d = call("GET", f"/Items/{visible_movie}?userId={admin}", token=admin_token)
print("sync", d.get("Name"), "sources", len(d.get("MediaSources") or []))
cols, rows = query(
    "select lower(replace(Id,'-','')) from BaseItems where lower(replace(PrimaryVersionId,'-',''))=? order by Id", (visible_movie,))
row = rows[0][0]
print("row", row)

st, users = call("GET", "/Users", token=admin_token)
user2 = next(u for u in users if u["Name"] == user2_name)
policy = user2["Policy"]
st, m = call("GET", f"/Items/{blocked_movie}?userId={admin}&Fields=Tags", token=admin_token)
tag = (m.get("Tags") or [None])[0]
print("blocking tag", repr(tag), "of", m.get("Name"), "for", user2_name)
policy["BlockedTags"] = [tag]
st, _ = call("POST", f"/Users/{user2['Id']}/Policy", policy, token=admin_token)
print("policy update", st)

try:
    jf.USER, jf.PW = user2_name, os.environ.get("JF_SECONDPASSWORD", "")
    jf.TOKEN_FILE = jf.TOKEN_FILE.replace("-the administrator.json", f"-{user2_name}.json")
    jf.AUTH = jf.AUTH.replace("jfapi-cli-the administrator", f"jfapi-cli-{user2_name}")
    token2, uid2 = jf.session()
    st, d = call("GET", f"/Items?ids={blocked_movie},{row}&userId={uid2}&Fields=Tags,People", token=token2)
    items = d.get("Items", [])
    print(f"as {user2_name}: {st} TotalRecordCount {d.get('TotalRecordCount')} returned {len(items)}")
    for i in items:
        print("  ", i["Id"][:8], i["Name"], "| ImageTags", sorted((i.get("ImageTags") or {}).keys()),
              "| People", len(i.get("People") or []), "| Tags", (i.get("Tags") or [])[:3], "| MediaSourceCount", i.get("MediaSourceCount"))
    st, d = call("GET", f"/Items/{blocked_movie}?userId={uid2}", token=token2)
    print(f"  single GetItem of the blocked movie as {user2_name}: {st}")
finally:
    policy["BlockedTags"] = []
    st, _ = call("POST", f"/Users/{user2['Id']}/Policy", policy, token=admin_token)
    print("policy restored", st)
