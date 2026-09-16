DESCRIPTION = "Parallel searches for the same term, fanned out over item types like a client does: no file sharing violation, no 500 the addon did not cause"

import subprocess
import threading
import time
import urllib.parse
from collections import Counter

TERMS = ["star", "love", "night", "man", "war"]
KINDS = ["Movie", "Series", "Movie,Series"]
PER_KIND = 4  # 12 requests per term, all started together
SHARING = "being used by another process"  # the IOException of two writers on one image sidecar
ADDON_ERROR = "GetJsonAsync: error fetching or parsing"  # the addon failed or timed out


def server_log(container, since):
    r = subprocess.run(["docker", "logs", "--since", str(int(since)), container], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout + r.stderr


def run(t):
    for term in TERMS:
        results, lock = [], threading.Lock()
        gate = threading.Event()

        def search(kind):
            gate.wait()
            path = (f"/Items?userId={t.api.user}&searchTerm={urllib.parse.quote(term)}&IncludeItemTypes={kind}"
                    f"&Recursive=true&Limit=25&Fields=Path")
            st, d = t.api.call("GET", path)
            with lock:
                results.append((kind, st, len(d.get("Items", [])) if st == 200 and isinstance(d, dict) else str(d)[:160]))

        threads = [threading.Thread(target=search, args=(k,)) for k in KINDS for _ in range(PER_KIND)]
        for th in threads:
            th.start()
        t0 = time.time()
        gate.set()
        for th in threads:
            th.join()
        took = time.time() - t0
        log = server_log(t.db.container, t0)
        sharing = [line for line in log.splitlines() if SHARING in line]
        addon_errors = log.count(ADDON_ERROR)
        failed = [(k, st, body) for k, st, body in results if st != 200]
        t.log(f"'{term}': {len(results)} requests in {took:.1f}s, {dict(Counter((k, st) for k, st, _ in results))}, "
              f"addon errors {addon_errors}, sharing violations {len(sharing)}")
        for k, st, body in failed:
            t.log(f"    {k} HTTP {st}: {body}")
        for line in sharing[:3]:
            t.log("    " + line[:200])
        t.equal(len(sharing), 0, f"'{term}': no sharing violation on a Gelato file")
        t.check(not failed or addon_errors, f"'{term}': every search answered 200, or the addon failed ({len(failed)} failed)")
        t.check(any(st == 200 and n for _, st, n in results), f"'{term}': the searches found something")
