"""One API call as the environment's user (JF_URL, JF_ADMINUSER, JF_ADMINPASSWORD).

    python tools/jf.py GET "/Items?userId={user}&IncludeItemTypes=Movie&Limit=3"
    python tools/jf.py POST "/UserPlayedItems/<id>?userId={user}"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import json

from jfapi.api import call, session

token, user = session()
method, path = sys.argv[1], sys.argv[2].replace("{user}", user)
body = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
status, d = call(method, path, body)
print(status)
print(json.dumps(d, indent=1) if not isinstance(d, str) else d[:2000])
