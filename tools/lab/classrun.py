"""Lab: per-test record of which sqlglot classes were used (as self/cls of a running
sqlglot function), plus the class hierarchy. Same fork-per-test scheme as covrun.

    python /app/tools/lab/classrun.py tests.json OUTDIR [--jobs 4]
Writes OUTDIR/<number>.json {"number", "classes": [key...]} and OUTDIR/hier.json.
A class key is "relpath:firstlineno:qualname".
"""

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import importlib
import json
import sys
import time
import types
import unittest

for _m in ("duckdb", "pandas", "numpy"):
    try:
        importlib.import_module(_m)
    except Exception:
        pass

sys.path.insert(0, "/app/rwx")
from engine import InlinePool, install_inline_pool  # noqa: E402

ROOT = "/sqlglot/"


def class_key(c):
    mod = sys.modules.get(c.__module__)
    f = getattr(mod, "__file__", None) or ""
    if not f.startswith(ROOT):
        return None
    return f"{f[len(ROOT):]}:{getattr(c, '__firstlineno__', 0)}:{c.__qualname__}"


def child(test, outdir):
    mon = sys.monitoring
    tool = mon.PROFILER_ID
    seen_types = set()
    keyed = {}
    phase = {"imp": True}

    def on_start(code, offset):
        if not code.co_filename.startswith(ROOT) or code.co_argcount == 0:
            return mon.DISABLE
        name = code.co_varnames[0]
        if name not in ("self", "cls", "klass", "mcs", "metacls"):
            return mon.DISABLE
        f = sys._getframe(1)
        try:
            obj = f.f_locals[name]
        except Exception:
            return None
        if isinstance(obj, type):
            if phase["imp"]:
                return None  # class-creation hooks while modules load: not a use
            t = obj
        else:
            t = type(obj)
        seen_types.add(t)
        return None

    mon.use_tool_id(tool, "rwxcls")
    mon.register_callback(tool, mon.events.PY_START, on_start)
    install_inline_pool()
    status = "ok"
    mon.set_events(tool, mon.events.PY_START)
    try:
        mod = importlib.import_module(test["mod"])
        cls = getattr(mod, test["cls"])
        phase["imp"] = False
        suite = unittest.TestSuite([cls(test["meth"])])
        r = unittest.TestResult()
        suite.run(r)
        if r.errors or r.failures:
            status = "fail"
    except Exception as e:  # noqa: BLE001
        status = f"crash {e!r}"
    mon.set_events(tool, 0)
    for t in seen_types:
        for c in t.__mro__:
            k = keyed.get(c)
            if k is None:
                k = keyed[c] = class_key(c)
    keys = sorted({class_key(t) for t in seen_types} - {None})
    with open(os.path.join(outdir, f"{test['number']}.json"), "w") as fh:
        json.dump({"number": test["number"], "status": status, "classes": keys}, fh)


def hierarchy(outdir):
    import pkgutil

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException:
            pass
    seen = {}
    todo = [object]
    while todo:
        c = todo.pop()
        try:
            subs = type.__subclasses__(c)
        except TypeError:
            subs = []
        for s in subs:
            if s not in seen:
                seen[s] = 1
                todo.append(s)
    out = {}
    for c in seen:
        k = class_key(c)
        if k is None:
            continue
        out[k] = [x for x in (class_key(b) for b in c.__mro__) if x]
    json.dump(out, open(os.path.join(outdir, "hier.json"), "w"))


def main():
    tests_json, outdir = sys.argv[1], sys.argv[2]
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else 4
    os.makedirs(outdir, exist_ok=True)
    pid = os.fork()
    if pid == 0:
        hierarchy(outdir)
        os._exit(0)
    os.waitpid(pid, 0)
    tests = json.load(open(tests_json))["tests"]
    todo = []
    for t in tests:
        if os.path.exists(os.path.join(outdir, f"{t['number']}.json")):
            continue
        path, cls, meth = t["id"].split("::")
        todo.append(dict(t, mod=path[:-3].replace("/", "."), cls=cls, meth=meth))
    todo.sort(key=lambda t: -t["seconds"])
    running = {}
    t0 = time.time()
    while todo or running:
        while todo and len(running) < jobs:
            t = todo.pop(0)
            p = os.fork()
            if p == 0:
                try:
                    child(t, outdir)
                finally:
                    os._exit(0)
            running[p] = t
        p, _ = os.wait()
        running.pop(p, None)
    print(f"done in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
