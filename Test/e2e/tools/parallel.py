"""Runs the suite as shards, one per instance, at the same time.

    python tools/parallel.py --adminuser nistei --destructive jf-prodrun:8097 jf-prodrun2:8098 jf-prodrun3:8099

Each argument is `container:port` of an instance built from the same dump. Shard I of N goes to the
Ith instance (`run.py --shard I/N`, split by jfapi/weights.json), the output of each into
`.cache/shard-<I>.txt`. Extra arguments after `--` go to every run.py. Exit code 1 if any shard failed.
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("instances", nargs="+", help="container:port")
    p.add_argument("--adminuser", default="admin")
    p.add_argument("--adminpassword", default="")
    p.add_argument("--destructive", action="store_true")
    p.add_argument("--seed", type=int)
    p.add_argument("extra", nargs="*")
    args = p.parse_args()
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
    return rc


if __name__ == "__main__":
    sys.exit(main())
