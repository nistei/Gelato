"""One query on a snapshot of the container's database (JF_CONTAINER).

    python tools/db.py "select count(*) from BaseItems where Tags like '%gelato-stream%'"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
from jfapi.db import query

sys.stdout.reconfigure(encoding="utf-8")  # item names; the Windows console's code page mangles them
cols, rows = query(sys.argv[1])
print(" | ".join(cols))
for r in rows:
    print(" | ".join(str(v) for v in r))
