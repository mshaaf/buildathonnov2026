"""Per-test coverage collector for sqlglot at the pinned commit (lab only).

Each test runs in its own forked child of a parent that has imported only the
stdlib and third-party packages, never sqlglot, so every test sees a cold
sqlglot: lazy caches cannot hide lines from later tests.

Usage (inside the lab image, cwd=/sqlglot):
    python /tools/covrun.py tests.json OUTDIR [--jobs 4] [--only 1,2,3]
Writes OUTDIR/<number>.json with lines hit during import of the test module
("imp") and while running the test ("run"), files opened, and the outcome.
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import importlib
import json
import sys
import time
import traceback
import unittest

# Heavy third-party imports done once in the parent.
for _mod in ("duckdb", "pandas", "numpy", "doctest", "decimal", "datetime", "typing", "re"):
    try:
        importlib.import_module(_mod)
    except Exception:
        pass

ROOT = "/sqlglot/"


class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = []

    def addError(self, test, err):
        super().addError(test, err)
        self.bad.append(("error", str(test), self._exc_info_to_string(err, test)[-2000:]))

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.bad.append(("failure", str(test), self._exc_info_to_string(err, test)[-2000:]))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.bad.append(("subfail", str(subtest), self._exc_info_to_string(err, test)[-2000:]))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.bad.append(("unexpected_success", str(test), ""))


class InlinePool:
    """Stand-in for ProcessPoolExecutor: runs work in-process so coverage sees it,
    but round-trips function, arguments and results through pickle like a real pool."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        import pickle

        self._pickle = pickle
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        import concurrent.futures as cf

        fut = cf.Future()
        p = self._pickle
        try:
            fn2, a2, k2 = p.loads(p.dumps((fn, args, kwargs)))
            fut.set_result(p.loads(p.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, **kw):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()
        return False


def child(test, outdir):
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    hits = {}
    phase = {"cur": {}}
    opens = set()

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(ROOT):
            # key: file -> "firstlineno:name" of the code object -> lines
            f = phase["cur"].get(fn)
            if f is None:
                f = phase["cur"][fn] = {}
            k = f"{code.co_firstlineno}:{code.co_name}"
            s = f.get(k)
            if s is None:
                s = f[k] = set()
            s.add(line)
        return mon.DISABLE

    def audit(event, args):
        if event == "open" and args and isinstance(args[0], str) and args[0].startswith(ROOT):
            opens.add(args[0])

    sys.addaudithook(audit)
    mon.use_tool_id(tool, "rwxcov")
    mon.register_callback(tool, mon.events.LINE, on_line)

    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlinePool
    concurrent.futures.process.ProcessPoolExecutor = InlinePool

    imp = phase["cur"] = {}
    mon.set_events(tool, mon.events.LINE)
    module_name, cls_name, meth = test["mod"], test["cls"], test["meth"]
    t0 = time.time()
    status, bad = "ok", []
    try:
        mod = importlib.import_module(module_name)
        cls = getattr(mod, cls_name)
        run = phase["cur"] = {}
        mon.restart_events()
        suite = unittest.TestSuite([cls(meth)])
        result = Result()
        suite.run(result)
        bad = result.bad
        if bad:
            status = "fail"
        elif result.skipped:
            status = "skipped"
    except Exception:
        run = phase["cur"]
        status, bad = "crash", [("crash", "", traceback.format_exc()[-3000:])]
    mon.set_events(tool, 0)
    dt = time.time() - t0
    out = {
        "number": test["number"],
        "id": test["id"],
        "status": status,
        "bad": bad,
        "secs": dt,
        "imp": {f: {k: sorted(l) for k, l in v.items()} for f, v in imp.items()},
        "run": {f: {k: sorted(l) for k, l in v.items()} for f, v in run.items()} if run is not imp else {},
        "opens": sorted(opens),
    }
    with open(os.path.join(outdir, f"{test['number']}.json"), "w") as fh:
        json.dump(out, fh)


def parse_id(tid):
    path, cls, meth = tid.split("::")
    mod = path[:-3].replace("/", ".")
    return mod, cls, meth


def main():
    tests_json, outdir = sys.argv[1], sys.argv[2]
    jobs = 4
    only = None
    args = sys.argv[3:]
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
        mod, cls, meth = parse_id(t["id"])
        todo.append(dict(t, mod=mod, cls=cls, meth=meth))
    # Longest first so the slow ones don't straggle at the end.
    todo.sort(key=lambda t: -t["seconds"])
    running = {}
    started = time.time()
    done = 0
    while todo or running:
        while todo and len(running) < jobs:
            t = todo.pop(0)
            pid = os.fork()
            if pid == 0:
                try:
                    child(t, outdir)
                finally:
                    os._exit(0)
            running[pid] = t
        pid, _ = os.wait()
        running.pop(pid, None)
        done += 1
        if done % 100 == 0:
            print(f"{done} done, {time.time() - started:.0f}s", file=sys.stderr, flush=True)
    print(f"all done in {time.time() - started:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
