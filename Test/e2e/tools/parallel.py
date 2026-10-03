"""Runs the suite as shards, one per instance, at the same time.

    python tools/parallel.py --adminuser nistei --destructive jf-playback-1:8112 jf-playback-2:8113
    python tools/parallel.py --adminuser nistei --destructive jf-playback-*      # every running jf-playback-N

Each argument is `container:port` of an instance built from the same dump, or a container alone (its published
port is looked up), or `name-*` for every running container `name-<number>`. Shard I of N goes to the
Ith instance (`run.py --shard I/N`, split by jfapi/weights.json), the output of each into
`.cache/shard-<I>.txt`. Extra arguments after `--` go to every run.py. Exit code 1 if any shard failed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

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
    p.add_argument("extra", nargs="*")
    args = p.parse_args()
    args.instances = expand(args.instances)
    print("instances: " + " ".join(args.instances))
    os.makedirs(os.path.join(HERE, ".cache"), exist_ok=True)
    t0, procs = time.time(), []
    for i, inst in enumerate(args.instances, 1):
        container, port = inst.rsplit(":", 1)
        cmd = [sys.executable, os.path.join(HERE, "run.py"), "--container", container, "--url", f"http://localhost:{port}",
               "--adminuser", args.adminuser, "--adminpassword", args.adminpassword, "-v",
               "--shard", f"{i}/{len(args.instances)}"]
        if args.destructive:
            cmd.append("--destructive")
        if args.seed:
            cmd += ["--seed", str(args.seed)]
        out = open(os.path.join(HERE, ".cache", f"shard-{i}.txt"), "w", encoding="utf-8")
        procs.append((i, subprocess.Popen(cmd + args.extra, stdout=out, stderr=subprocess.STDOUT, cwd=HERE), out))
    rc = 0
    for i, proc, out in procs:
        proc.wait()
        out.close()
        text = open(out.name, encoding="utf-8", errors="replace").read()
        summary = re.findall(r"^\d+ passed.*$", text, re.M)
        print(f"shard {i}: exit {proc.returncode}: {summary[-1] if summary else 'no summary'}")
        for line in re.findall(r"^  (?:FAIL|ERROR|flaky): .*$", text, re.M):
            print("  " + line.strip())
        rc |= 1 if proc.returncode else 0
    print(f"wall clock {time.time() - t0:.0f}s")
    update_weights([out.name for _, _, out in procs])
    return rc


def update_weights(outputs):
    """Folds the measured seconds of the tests that passed (or ended KNOWN) into .cache/weights.json, half the
    old value and half the new: the next split then balances by what the tests take now. A failed or skipped
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
