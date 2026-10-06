"""Runs the recording addon proxy (jfapi.addon) and points the instance's Gelato at it until the stop file exists,
so the measurements see replayed catalogs and metas and only streams stay live.

    python tools/perf/proxy.py                 start (leave it running in the background); writes .cache/perf/proxy.port
    echo > Test/e2e/.cache/perf/proxy.stop     stop: Gelato is pointed back at the addon
"""
import os
import time

import perflib
from jfapi import addon
from jfapi.bootstrap import GELATO

STOP = os.path.join(perflib.WORK, "proxy.stop")
if os.path.exists(STOP):
    os.remove(STOP)

api = perflib.api()
cfg = addon.restore_left_over(api, GELATO, api.port, print)
upstream = cfg["Url"]
rec = addon.AddonRecorder(upstream)
addon.switch(api, GELATO, rec.url, api.port, keep=upstream)
with open(os.path.join(perflib.WORK, "proxy.port"), "w") as h:
    h.write(str(rec.port))
print("proxy on", rec.port, "log", os.path.basename(rec.log_file), flush=True)
try:
    while not os.path.exists(STOP):
        time.sleep(1)
finally:
    addon.switch(api, GELATO, upstream, api.port)
    rec.close()
    print("restored", rec.stats, flush=True)
