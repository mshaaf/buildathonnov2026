"""Per-test code-object ENTRY collector (lab only): supplements the line coverage map.

sys.monitoring LINE events never fire for instructions on a code object's first line, so
one-line lambdas/defs (and runs that only execute a first line) are missing from the
line map. PY_START fires whenever a code object starts executing, so it records them.

Same harness as covrun.py: each test runs in its own forked child of a parent that has
imported only stdlib/third-party packages (never sqlglot), so every test sees a cold
sqlglot; process pools run inline.

usage: startrun.py SQLGLOT_ROOT tests.json OUTDIR [--jobs 2] [--only 1,2,3]
Writes OUTDIR/<number>.json: {"number", "status", "start": {relfile: ["first:name", ...]}}
(both the import of the test module and the test itself). Run with cwd=SQLGLOT_ROOT and
PYTHONPATH=SQLGLOT_ROOT SKIP_INTEGRATION=0.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covrun  # noqa: E402  (heavy third-party imports happen here, in the parent)

import importlib  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402

ROOT = ""


def child(test, outdir):
    mon = sys.monitoring
    tool = mon.PROFILER_ID
    seen = {}

    def on_start(code, offset):
        fn = code.co_filename
        if fn.startswith(ROOT):
            seen.setdefault(fn[len(ROOT):], set()).add(f"{code.co_firstlineno}:{code.co_name}")
        return mon.DISABLE

    mon.use_tool_id(tool, "rwxstart")
    mon.register_callback(tool, mon.events.PY_START, on_start)

    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = covrun.InlinePool
    concurrent.futures.process.ProcessPoolExecutor = covrun.InlinePool

    mon.set_events(tool, mon.events.PY_START)
    status = "ok"
    try:
        mod = importlib.import_module(test["mod"])
        cls = getattr(mod, test["cls"])
        result = covrun.Result()
        unittest.TestSuite([cls(test["meth"])]).run(result)
        if result.bad:
            status = "fail"
    except Exception:  # noqa: BLE001
        status = "crash"
    mon.set_events(tool, 0)
    out = {"number": test["number"], "status": status, "start": {f: sorted(v) for f, v in seen.items()}}
    tmp = os.path.join(outdir, f".{test['number']}.json")
    with open(tmp, "w") as fh:
        json.dump(out, fh)
    os.replace(tmp, os.path.join(outdir, f"{test['number']}.json"))


def main():
    global ROOT
    root, tests_json, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    ROOT = os.path.abspath(root).rstrip("/") + "/"
    jobs, only = 2, None
    args = sys.argv[4:]
    while args:
        a = args.pop(0)
        if a == "--jobs":
            jobs = int(args.pop(0))
        elif a == "--only":
            only = {int(x) for x in args.pop(0).split(",")}
    os.makedirs(outdir, exist_ok=True)
    tests = json.load(open(tests_json))["tests"]
    todo = []
    for t in tests:
        if only is not None and t["number"] not in only:
            continue
        if os.path.exists(os.path.join(outdir, f"{t['number']}.json")):
            continue
        mod, cls, meth = covrun.parse_id(t["id"])
        todo.append(dict(t, mod=mod, cls=cls, meth=meth))
    todo.sort(key=lambda t: -t["seconds"])
    running, started, done = {}, time.time(), 0
    while todo or running:
        while todo and len(running) < jobs:
            t = todo.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    child(t, outdir)
                finally:
                    os._exit(0)
            running[pid] = (t, time.time())
        pid, _ = os.wait()
        running.pop(pid, None)
        done += 1
        if done % 50 == 0:
            print(f"{done} done, {time.time() - started:.0f}s", file=sys.stderr, flush=True)
    print(f"all done in {time.time() - started:.0f}s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
