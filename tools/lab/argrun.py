"""Lab (argrun): per-test (class, arg) pairs missing at validation time; derived from classrun:
per-test record of which sqlglot classes were used (as self/cls of a running
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
    """Record (class key, arg) pairs for every required-arg check that saw the arg missing."""
    import importlib.machinery

    missing = set()

    def make(orig):
        def error_messages(self, *a, **k):
            try:
                args = self.args
                for key in self.arg_types:
                    v = args.get(key)
                    if v is None or (isinstance(v, list) and not v):
                        missing.add((type(self), key))
            except Exception:
                pass
            return orig(self, *a, **k)

        return error_messages

    def patch_module(mod):
        for obj in list(vars(mod).values()):
            if isinstance(obj, type) and "error_messages" in vars(obj) and obj.__module__ == mod.__name__:
                setattr(obj, "error_messages", make(vars(obj)["error_messages"]))

    class Hook:
        """Wrap error_messages the moment sqlglot.expressions.core finishes executing,
        so validations during the rest of the sqlglot import are recorded too."""

        def find_spec(self, name, path=None, target=None):
            if name != "sqlglot.expressions.core":
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None:
                return None
            orig_exec = spec.loader.exec_module

            def exec_module(module):
                orig_exec(module)
                patch_module(module)

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, Hook())
    install_inline_pool()
    status = "ok"
    try:
        mod = importlib.import_module(test["mod"])
        cls = getattr(mod, test["cls"])
        suite = unittest.TestSuite([cls(test["meth"])])
        r = unittest.TestResult()
        suite.run(r)
        if r.errors or r.failures:
            status = "fail"
    except Exception as e:  # noqa: BLE001
        status = f"crash {e!r}"
    pairs = sorted({f"{class_key(t)}|{k}" for t, k in missing if class_key(t)})
    with open(os.path.join(outdir, f"{test['number']}.json"), "w") as fh:
        json.dump({"number": test["number"], "status": status, "missing": pairs}, fh)


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
