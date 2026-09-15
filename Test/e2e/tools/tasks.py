"""List Gelato tasks; optionally run one and wait for it."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the jfapi package
import sys
import time
from jfapi.api import call, session

token, user = session()
st, tasks = call("GET", "/ScheduledTasks", token=token)
gelato = [t for t in tasks if "Gelato" in (t.get("Category") or "")]
for t in gelato:
    print(t["Id"], t["Key"], "|", t["Name"], "|", t["State"])

if len(sys.argv) > 1:
    key = sys.argv[1]
    task = next(t for t in gelato if t["Key"] == key)
    st, _ = call("POST", f"/ScheduledTasks/Running/{task['Id']}", token=token)
    print("run", key, st)
    for _ in range(120):
        time.sleep(2)
        st, t = call("GET", f"/ScheduledTasks/{task['Id']}", token=token)
        if t["State"] == "Idle":
            print("done:", t.get("LastExecutionResult", {}).get("Status"), t.get("LastExecutionResult", {}).get("ErrorMessage"))
            break
    else:
        print("still running")
