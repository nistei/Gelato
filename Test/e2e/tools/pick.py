import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import re
from jfapi.api import call, session

token, user = session()
red = lambda s: re.sub(r"https?://[^\s\"]+", "<url>", str(s))

for kind in ("Movie", "Series"):
    st, d = call("GET", f"/Items?userId={user}&IncludeItemTypes={kind}&Recursive=true&Limit=4&Fields=Path,ProviderIds&SortBy=Random", token=token)
    print(kind, st, "total", d.get("TotalRecordCount") if isinstance(d, dict) else d)
    for i in d["Items"]:
        print(" ", i["Id"], "|", i["Name"], "|", red(i.get("Path")), "|", i.get("ProviderIds", {}).get("Stremio"))
