"""Runs the suite on several instances at the same time, each taking its next test from one shared queue.

    python tools/parallel.py --adminuser nistei --destructive jf-playback-1:8112 jf-playback-2:8113
    python tools/parallel.py --adminuser nistei --destructive jf-playback-*      # every running jf-playback-N

Each argument is `container:port` of an instance built from the same dump, or a container alone (its published
port is looked up), or `name-*` for every running container `name-<number>`. Each instance runs its own
run.py (`--queue`), which asks this script for one test after the other (see Queue); the output of the Ith
instance goes into `.cache/shard-<I>.txt`. Extra arguments after `--` go to every run.py. Exit code 1 if any
shard failed or a test was not run.

Every test's verdict is printed as it comes in, so a failure shows minutes before the run ends. The
summary repeats the notes of each test that failed or was flaky (without its passed checks) and ends
with the totals over all shards: the combined output is enough to judge a run.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expand(instances):
    """container:port for each argument: a port looked up when missing, `name-*` for the running name-N in order."""
    sys.path.insert(0, HERE)
    from run import container_url
    out = []
    for inst in instances:
        if inst.endswith("-*"):
            names = subprocess.run(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{inst[:-1]}[0-9]+$"],
                                   capture_output=True, text=True).stdout.split()
            out += sorted(names, key=lambda n: int(n.rsplit("-", 1)[1]))
        else:
            out.append(inst)
    return [i if ":" in i else f"{i}:{container_url(i).rsplit(':', 1)[1]}" for i in out]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("instances", nargs="+", help="container:port, container, or name-* (see above)")
    p.add_argument("--adminuser", default="admin")
    p.add_argument("--adminpassword", default="")
    p.add_argument("--destructive", action="store_true")
    p.add_argument("--seed", type=int)
    # Split by hand: argparse gives everything after "--" to the instances, the extra list stayed empty.
    argv = sys.argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)
    args = p.parse_args(argv[:cut])
    args.extra = argv[cut + 1:]
    args.instances = expand(args.instances)
    print("instances: " + " ".join(args.instances))
    os.makedirs(os.path.join(HERE, ".cache"), exist_ok=True)
    queue = Queue([inst.rsplit(":", 1)[0] for inst in args.instances])
    t0, procs = time.time(), []
    for i, inst in enumerate(args.instances, 1):
        container, port = inst.rsplit(":", 1)
        cmd = [sys.executable, os.path.join(HERE, "run.py"), "--container", container, "--url", f"http://localhost:{port}",
               "--adminuser", args.adminuser, "--adminpassword", args.adminpassword, "-v", "--queue", queue.url]
        if args.destructive:
            cmd.append("--destructive")
        if args.seed:
            cmd += ["--seed", str(args.seed)]
        out = open(os.path.join(HERE, ".cache", f"shard-{i}.txt"), "w", encoding="utf-8")
        procs.append((i, subprocess.Popen(cmd + args.extra, stdout=out, stderr=subprocess.STDOUT, cwd=HERE), out))
    tails = [Tail(i, out.name) for i, _, out in procs]
    while any(proc.poll() is None for _, proc, _ in procs):
        time.sleep(0.5)
        for tail in tails:
            tail.follow()
        for (i, proc, _), inst in zip(procs, args.instances):
            if proc.poll() is not None:
                queue.gone(inst.rsplit(":", 1)[0])  # the others plan the end without it
    rc, totals = 0, {}
    for (i, proc, out), tail in zip(procs, tails):
        out.close()
        tail.follow()
        text = open(out.name, encoding="utf-8", errors="replace").read()
        summary = re.findall(r"^\d+ passed.*$", text, re.M)
        print(f"shard {i}: exit {proc.returncode}: {summary[-1] if summary else 'no summary'}")
        for line in re.findall(r"^  (?:FAIL|ERROR|flaky): .*$", text, re.M):
            print("  " + line.strip())
        for n, what in re.findall(r"(\d+) (passed|failed|errored|flaky|known|skipped)", summary[-1] if summary else ""):
            totals[what] = totals.get(what, 0) + int(n)
        rc |= 1 if proc.returncode else 0
    for tail in tails:
        for name in tail.failed:
            print(f"\n== shard {tail.shard}: {name}")
            print("\n".join(tail.blocks[name]))
    # A test nobody was handed, or one without a verdict (its instance's run died under it), must not
    # go missing quietly: the totals only count what the shards reported.
    judged = {name for tail in tails for name in tail.judged}
    lost = [name for name, _ in queue.tests or [] if name not in judged]
    if lost:
        print(f"\nNOT RUN ({len(lost)}): " + " ".join(lost))
        rc = 1
    print("\ntotal: " + ", ".join(f"{totals.get(w, 0)} {w}" for w in ("passed", "failed", "errored", "flaky", "known", "skipped"))
          + f", wall clock {time.time() - t0:.0f}s")
    update_weights([out.name for _, _, out in procs])
    return rc


END_GAME = 3  # the longest test first once this many tests per instance are left


class Queue:
    """Hands the selected tests out to the instances, one at a time as each asks for its next.

    A split made before the run is only as good as its guesses: one failure with its run alone, or a
    test that has no measured time yet, and a shard ran minutes after the others had finished (281 s
    against 533 s in one run of four). From one list no instance is idle while there is work.

    The tests go out in run order, until three per instance are left: from there the longest goes
    first, or whoever drew a long one at the very end kept the run waiting for it (simulated on the
    suite's times, four instances ended 6 % over the even share in run order and 1 % like this).
    A test with `LAST = True` ends its instance's run, so it is given to the instance that asks once
    the others need about as long for the rest as that test takes."""

    def __init__(self, workers):
        sys.path.insert(0, HERE)
        from run import load_weights
        self.weights = load_weights()
        self.default = sum(self.weights.values()) / max(len(self.weights), 1)
        self.lock = threading.Lock()
        self.tests = None       # [(name, last)] in run order, from the first instance that asks
        self.given = []         # names handed out
        self.busy = {}          # worker -> (name, since)
        self.active = set(workers)
        queue = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with queue.lock:
                    if self.path == "/tests":
                        queue.tests = queue.tests or [tuple(t) for t in body["tests"]]
                        answer = {}
                    else:
                        answer = queue.next(body["worker"])
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def weight(self, name):
        return self.weights.get(name, self.default)

    def gone(self, worker):
        with self.lock:
            self.active.discard(worker)
            self.busy.pop(worker, None)

    def next(self, worker):
        """{"test", "label"} for the worker's next test, {} when there is none left for it."""
        self.busy.pop(worker, None)
        if worker not in self.active:
            return {}
        open_ = [(n, last) for n, last in self.tests if n not in self.given]
        ordinary = [n for n, last in open_ if not last]
        final = [n for n, last in open_ if last]
        others = self.active - {worker}
        name = ordinary[0] if ordinary else None
        if ordinary and len(ordinary) <= END_GAME * len(self.active):
            name = max(ordinary, key=self.weight)
        if final:
            now = time.time()
            rest = sum(self.weight(n) for n in ordinary) + sum(
                max(0.0, self.weight(n) - (now - since)) for w, (n, since) in self.busy.items() if w in others)
            if not ordinary or (others and rest / len(others) <= self.weight(final[0])):
                name = final[0]
                self.active.discard(worker)
        if name is None:
            self.active.discard(worker)
            return {}
        self.given.append(name)
        self.busy[worker] = (name, time.time())
        return {"test": name, "label": f"[{len(self.given):2}/{len(self.tests)}]"}


class Tail:
    """Follows one shard's output: prints each verdict as it is written, and keeps the notes of every
    test that failed (in the run and alone) for the summary."""

    def __init__(self, shard, path):
        self.shard, self.path, self.pos, self.rest = shard, path, 0, ""
        self.label = self.name = None
        self.block, self.blocks, self.failed, self.judged = [], {}, [], set()

    def follow(self):
        with open(self.path, "rb") as h:
            h.seek(self.pos)
            data = h.read()
            self.pos += len(data)
        *lines, self.rest = (self.rest + data.decode("utf-8", errors="replace")).split("\n")
        for line in lines:
            line = line.rstrip("\r")
            m = re.match(r"^(\[\s*\d+/\d+\]|\[alone\]) (\S+)", line)
            if m:
                self.label, self.name, self.block = m.group(1), m.group(2), []
            if self.name is None:
                continue
            if not line.startswith("      ok  "):
                self.block.append(line)
            m = re.match(r"^\s+-> (\S+) \((.*)\)", line)
            if m:
                checks, seconds = m.group(2).rsplit(", ", 1)
                failed = m.group(1) in ("FAIL", "ERROR")
                self.judged.add(self.name)
                print(f"shard {self.shard} {self.label} {self.name:14} {m.group(1):5} {seconds}" + (f"   ({checks})" if failed else ""), flush=True)
                if failed or self.name in self.blocks:  # its run alone belongs to the same story
                    if self.name not in self.blocks:
                        self.failed.append(self.name)
                    self.blocks.setdefault(self.name, []).extend(self.block)
                self.name = None


def update_weights(outputs):
    """Folds the measured seconds of the tests that passed (or ended KNOWN) into .cache/weights.json, half the
    old value and half the new: the next run's queue then plans its end by what the tests take now. A failed or skipped
    test says nothing about its usual length (a dead link waits 100 s, a skip ends early)."""
    sys.path.insert(0, HERE)
    from run import load_weights
    weights, measured = load_weights(), {}
    for path in outputs:
        name = None
        for line in open(path, encoding="utf-8", errors="replace"):
            m = re.match(r"^\[\s*(\d+/\d+|alone)\] (\S+)", line)
            if m:
                name = None if m.group(1) == "alone" else m.group(2)
            m = re.match(r"^\s+-> (ok|KNOWN) \(.*, (\d+)s\)", line)
            if m and name:
                measured[name] = int(m.group(2))
    if not measured:
        return
    path = os.path.join(HERE, ".cache", "weights.json")
    try:
        with open(path, encoding="utf-8") as h:
            local = json.load(h)
    except (OSError, ValueError):
        local = {}
    for name, sec in measured.items():
        old = weights.get(name)
        local[name] = max(1, round((old + sec) / 2) if old is not None else sec)
    with open(path, "w", encoding="utf-8") as h:
        json.dump(dict(sorted(local.items())), h, indent=1)
    print(f"weights of {len(measured)} test(s) updated in .cache/weights.json")


if __name__ == "__main__":
    sys.exit(main())
