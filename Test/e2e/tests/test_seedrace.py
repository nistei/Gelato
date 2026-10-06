DESCRIPTION = "Parallel first lookups of Gelato's folders seed stub.txt once: every caller sees Gelato configured, no sharing violation"

import shlex
import subprocess
import threading
import time
import urllib.parse
from collections import Counter

from jfapi.bootstrap import GELATO

ROUNDS = 4
PARALLEL = 16
TERM = "star"
SEED = "stub.txt"
SEED_CONTENT = "This is a seed file created by Gelato so that library scans are triggered. Do not remove."
CONFIG_ERROR = "Error getting config"  # GetConfig's catch: that caller got a blank configuration
SHARING = "being used by another process"  # Windows: a second writer on the file the first holds open


def server_log(container, since):
    r = subprocess.run(["docker", "logs", "--since", str(int(since)), container], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout + r.stderr


def run(t):
    cfg = t.api.get(f"/Plugins/{GELATO}/Configuration")
    folders = [p.rstrip("/") for p in (cfg.get("MoviePath"), cfg.get("SeriesPath")) if p]
    t.check(folders, "Gelato has a movie or series path configured")
    seeds = [f"{p}/{SEED}" for p in folders]
    t.log("seed files:", ", ".join(seeds))

    try:
        for n in range(1, ROUNDS + 1):
            t.sh("rm -f " + " ".join(shlex.quote(s) for s in seeds))
            t.check(not t.sh("ls " + " ".join(shlex.quote(s) for s in seeds) + " 2>/dev/null").strip(), f"round {n}: seed files removed")
            # Every request of the round has to miss the folder memo, since Gelato seeds only on a
            # miss. Saving the configuration drops the memo at once (and the configuration memo with
            # it, so the callers race for that too); waiting its 10 s out cost 44 s of the test's 49.
            # That it was dropped shows below: the seed file is back after the round.
            t.api.post(f"/Plugins/{GELATO}/Configuration", cfg)

            results, lock = [], threading.Lock()
            gate = threading.Event()

            def search():
                gate.wait()
                path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(TERM)}&IncludeItemTypes=Movie,Series"
                        f"&Recursive=true&Limit=10&Fields=Path")
                st, d = t.api.call("GET", path)
                items = d.get("Items", []) if st == 200 and isinstance(d, dict) else []
                with lock:
                    results.append((st, len(items)))

            threads = [threading.Thread(target=search) for _ in range(PARALLEL)]
            for th in threads:
                th.start()
            t0 = time.time()
            gate.set()
            for th in threads:
                th.join()

            log = server_log(t.db.container, t0 - 1)
            blank = [line for line in log.splitlines() if CONFIG_ERROR in line]
            sharing = [line for line in log.splitlines() if SHARING in line and SEED in line]
            t.log(f"round {n}: {len(results)} searches in {time.time() - t0:.1f}s, status {dict(Counter(st for st, _ in results))}, "
                  f"config errors {len(blank)}, sharing violations {len(sharing)}")
            for line in (blank + sharing)[:3]:
                t.log("    " + line[:200])
            t.equal(len(blank), 0, f"round {n}: no caller got a blank configuration")
            t.equal(len(sharing), 0, f"round {n}: no sharing violation on {SEED}")
            t.check(all(st == 200 for st, _ in results), f"round {n}: every search answered 200")
            t.check(any(hits for _, hits in results), f"round {n}: the searches found something")

            for s in seeds:
                t.equal(t.sh(f"cat {shlex.quote(s)} 2>/dev/null"), SEED_CONTENT, f"round {n}: {s} seeded once, with Gelato's content")
    finally:
        for s in seeds:  # a failed round must not leave a folder without its seed
            t.sh(f"[ -e {shlex.quote(s)} ] || printf %s {shlex.quote(SEED_CONTENT)} > {shlex.quote(s)}")
