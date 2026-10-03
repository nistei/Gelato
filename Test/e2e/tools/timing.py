"""Runs run.py with timers on sleeps, tasks, settles and HTTP calls: where does a test's time go?

    python tools/timing.py --container jf-search --adminuser nistei --destructive -v catalogfolders

Takes run.py's arguments and prints, after its output, the seconds and calls per sleep (by the function
that slept), per scheduled task, in the idle waits and per API path. Sleeps and calls inside a task or
an idle wait count for that task or wait only.
"""
import collections
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import jfapi.api as api_module  # noqa: E402
import jfapi.testing as testing  # noqa: E402

seconds, calls = collections.Counter(), collections.Counter()
nested = [0]
real_sleep = time.sleep


def add(key, t0):
    seconds[key] += time.time() - t0
    calls[key] += 1


def sleep(s):
    frame = sys._getframe(1)
    t0 = time.time()
    real_sleep(s)
    if not nested[0]:
        add(f"sleep {os.path.basename(frame.f_code.co_filename)}:{frame.f_code.co_name}", t0)


def timed(owner, name, label, outer=False):
    real = getattr(owner, name)

    def wrapper(*a, **k):
        t0 = time.time()
        nested[0] += outer
        try:
            return real(*a, **k)
        finally:
            nested[0] -= outer
            if outer or not nested[0]:
                add(label(a) if callable(label) else label, t0)

    setattr(owner, name, wrapper)
    return wrapper


def http(a):
    return f"http {a[1]} " + re.sub(r"[0-9a-f]{32}|[0-9a-f-]{36}", "{id}", a[2].split("?")[0])


time.sleep = sleep
timed(api_module.Api, "run_task", lambda a: f"run_task {a[1]}", outer=True)
timed(api_module.Api, "wait_tasks_idle", "wait_tasks_idle", outer=True)
timed(api_module.Api, "call", http)
quiesce = timed(testing, "quiesce", "settle / idle wait", outer=True)

import run  # noqa: E402

run.quiesce = quiesce
sys.argv = ["run.py"] + sys.argv[1:]
try:
    code = run.main()
finally:
    print("\n  where the time went (s, calls):")
    for key, value in seconds.most_common(25):
        print(f"  {value:7.1f} {calls[key]:4}  {key}")
sys.exit(code)
