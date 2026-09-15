"""Metadata folders holding subtitle files: which items they belong to, and what happened to them."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import os
import subprocess
import sys
from collections import Counter

from jfapi.db import query

CONTAINER = os.environ.get("JF_CONTAINER", "")
log = sys.argv[1] if len(sys.argv) > 1 else None

out = subprocess.run(
    ["docker", "exec", CONTAINER, "sh", "-c",
     "find /media/metadata/library -type f \\( -name '*.vtt' -o -name '*.srt' \\) -printf '%TH:%TM %p\\n'"],
    capture_output=True, text=True).stdout
files = [l.split(" ", 1) for l in out.splitlines() if l.strip()]
by_folder = {}
for t, p in files:
    by_folder.setdefault(p.split("/")[5], []).append(t)
print("files:", len(files), "| folders:", len(by_folder), "| file times:", Counter(t[:2] + "h" for t, _ in files).most_common(4))

ids = list(by_folder)
cols, rows = query(
    "select lower(replace(Id,'-','')), substr(Type, length(Type)-6), Tags, PrimaryVersionId is not null, Name from BaseItems "
    "where lower(replace(Id,'-','')) in (" + ",".join("'" + i + "'" for i in ids) + ")")
found = {r[0]: r for r in rows}
print("folders whose item exists in the db:", len(found), "| by type/tag:", Counter((r[1], "stream" if (r[2] or "").find("gelato-stream") >= 0 else "plain", "owned" if r[3] else "unowned") for r in rows))
missing = [i for i in ids if i not in found]
print("folders without an item:", len(missing), "sample:", missing[:3])
if log and missing:
    text = open(log, encoding="utf-8", errors="replace").read()
    removed = sum(1 for i in missing if f"Id: {i[:8]}" in text and "Removing item" in text)
    deleted_paths = sum(1 for i in missing if f"/{i}" in text and "Deleting metadata path" in text)
    print("of those, 'Removing item' logged:", removed, "| 'Deleting metadata path' logged:", deleted_paths)
for r in list(found.values())[:3]:
    print("  existing:", r[4], r[1], "owned" if r[3] else "unowned", "files at", by_folder[r[0]][:2])
