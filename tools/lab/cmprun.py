"""Per-test value profiles of comparison sites (lab only).

For every single-operator comparison `L op R` in sqlglot/, records per test which
outcome vectors of the operator family were observed on the actual operands:

    ord: (<, <=, >, >=, ==, !=)      for an original <, <=, >, >=
    ide: (is, is not, ==, !=)        for an original is, is not, ==, !=
    mem: (in, not in)                for an original in, not in

Each entry is T/F, X (non-bool result) or E (the comparison raised). A test can only
behave differently under an operator flip X->Y at a site if it evaluated the site with
operands on which X and Y disagree (or one is X/E). This is a coverage map of the pinned
commit (which tests observe which operand relations), not a table from edits to tests.

Instrumentation: sqlglot sources are parsed once in the parent, each single comparison is
rewritten to `_rwx_cmp_(site, L, R)` (operands evaluated once, in order; the original
operator applied first through the `operator` module and its result returned unchanged;
exceptions propagate), compiled once, and served to the children by an import hook.
Chained comparisons and annotations are left alone (no profile -> no narrowing).
Harness as covrun.py/startrun.py: one forked child per test from a parent that never
imported sqlglot, process pools inline. A test that times out or crashes is recorded as
unprofiled (never narrowed away).

usage: cmprun.py SQLGLOT_ROOT tests.json OUTDIR [--jobs 2] [--only 1,2] [--timeout 900]
Writes OUTDIR/sites.json (site id -> [file, line, col, op]) and OUTDIR/<n>.json.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covrun  # noqa: E402  (third-party imports in the parent)

import ast  # noqa: E402
import builtins  # noqa: E402
import importlib  # noqa: E402
import importlib.abc  # noqa: E402
import importlib.machinery  # noqa: E402
import json  # noqa: E402
import operator as O  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402

OPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
       ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}
FUNCS = {"<": O.lt, "<=": O.le, ">": O.gt, ">=": O.ge, "==": O.eq, "!=": O.ne, "is": O.is_,
         "is not": O.is_not, "in": lambda a, b: O.contains(b, a), "not in": lambda a, b: not O.contains(b, a)}
FAMILY = {"<": "ord", "<=": "ord", ">": "ord", ">=": "ord", "==": "ide", "!=": "ide",
          "is": "ide", "is not": "ide", "in": "mem", "not in": "mem"}
FAM_OPS = {"ord": ("<", "<=", ">", ">=", "==", "!="), "ide": ("is", "is not", "==", "!="), "mem": ("in", "not in")}

SITES: list = []  # id -> (rel, line, col, op)
CODES: dict = {}  # abs path -> code object
ROOT = ""


class Instrument(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        sid = len(SITES)
        SITES.append((self.rel, node.lineno, node.col_offset, OPS[type(node.ops[0])]))
        call = ast.Call(ast.Name("_rwx_cmp_", ast.Load()), [ast.Constant(sid), node.left, node.comparators[0]], [])
        return ast.copy_location(call, node)

    # annotations are not evaluated like expressions (future import): leave them alone
    def visit_arg(self, node):
        return node

    def visit_AnnAssign(self, node):
        if node.value is not None:
            node.value = self.visit(node.value)
        node.target = self.visit(node.target)
        return node

    def visit_FunctionDef(self, node):
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.args.defaults = [self.visit(d) for d in node.args.defaults]
        node.args.kw_defaults = [self.visit(d) if d is not None else None for d in node.args.kw_defaults]
        node.body = [self.visit(b) for b in node.body]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def precompile(root):
    for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if not f.endswith(".py"):
                continue
            path = os.path.join(d, f)
            rel = os.path.relpath(path, root)
            src = open(path, "rb").read()
            tree = ast.parse(src, path)
            tree = Instrument(rel).visit(tree)
            ast.fix_missing_locations(tree)
            CODES[path] = compile(tree, path, "exec", dont_inherit=True)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        code = CODES.get(self.path)
        return code if code is not None else super().get_code(fullname)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin not in CODES:
            return spec
        spec.loader = Loader(fullname, spec.origin)
        return spec


def child(test, outdir):
    child_many([test], outdir)


def child_many(tests, outdir):
    """Run tests one after another in this process (shared mode: caches and registries
    left by earlier tests stay, as in a pytest worker), one profile file per test."""
    seen = set()
    sites = SITES

    def code(v):
        return "T" if v is True else "F" if v is False else "X"

    probing = [False]

    def rec(sid, a, b):
        op = sites[sid][3]
        if probing[0]:
            # evaluated inside our own extra comparison (e.g. an __eq__ that compares):
            # not a program evaluation; apply the operator only
            return FUNCS[op](a, b)
        r = FUNCS[op](a, b)  # the original operator, first; exceptions propagate
        if FAMILY[op] == "mem":
            # never evaluate `in` twice (b may be an iterator); `not in` is its negation
            seen.add((sid, "TF" if (r if op == "in" else not r) else "FT"))
            return r
        if r is not True and r is not False:
            # an overloaded operator (sqlglot builds expression nodes): never call the
            # alternatives, they could mutate the operands; every flip counts as differing
            seen.add((sid, "X"))
            return r
        vec = []
        probing[0] = True
        try:
            for alt in FAM_OPS[FAMILY[op]]:
                if alt == op:
                    vec.append(code(r))
                    continue
                try:
                    vec.append(code(FUNCS[alt](a, b)))
                except BaseException:  # noqa: BLE001
                    vec.append("E")
        finally:
            probing[0] = False
        seen.add((sid, "".join(vec)))
        return r

    builtins._rwx_cmp_ = rec
    sys.meta_path.insert(0, Finder())

    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = covrun.InlinePool
    concurrent.futures.process.ProcessPoolExecutor = covrun.InlinePool
    for test in tests:
        seen.clear()
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
        out = {"number": test["number"], "status": status, "seen": sorted([s, v] for s, v in seen)}
        tmp = os.path.join(outdir, f".{test['number']}.json")
        with open(tmp, "w") as fh:
            json.dump(out, fh)
        os.replace(tmp, os.path.join(outdir, f"{test['number']}.json"))
    builtins._rwx_cmp_ = lambda sid, a, b: FUNCS[sites[sid][3]](a, b)


def main():
    global ROOT
    root, tests_json, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    ROOT = os.path.abspath(root)
    jobs, only, timeout, shared = 2, None, 900.0, 0
    args = sys.argv[4:]
    while args:
        a = args.pop(0)
        if a == "--jobs":
            jobs = int(args.pop(0))
        elif a == "--only":
            only = {int(x) for x in args.pop(0).split(",")}
        elif a == "--timeout":
            timeout = float(args.pop(0))
        elif a == "--shared":
            shared = int(args.pop(0))
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    precompile(ROOT)
    json.dump(SITES, open(os.path.join(outdir, "sites.json"), "w"))
    print(f"instrumented {len(SITES)} sites in {len(CODES)} files, {time.time() - t0:.1f}s", file=sys.stderr, flush=True)
    tests = json.load(open(tests_json))["tests"]
    todo = []
    for t in tests:
        if only is not None and t["number"] not in only:
            continue
        if os.path.exists(os.path.join(outdir, f"{t['number']}.json")):
            continue
        mod, cls, meth = covrun.parse_id(t["id"])
        todo.append(dict(t, mod=mod, cls=cls, meth=meth))
    if shared:
        # tests.json order (pytest's), split into contiguous chunks, one process each
        chunks = [todo[i * len(todo) // shared:(i + 1) * len(todo) // shared] for i in range(shared)]
        pids = []
        for chunk in chunks:
            pid = os.fork()
            if pid == 0:
                try:
                    child_many(chunk, outdir)
                finally:
                    os._exit(0)
            pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        print(f"shared mode done in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
        return
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
        # reap, killing children over the time limit (they stay unprofiled)
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid:
                running.pop(pid, None)
                done += 1
                if done % 50 == 0:
                    print(f"{done} done, {time.time() - started:.0f}s", file=sys.stderr, flush=True)
                break
            now = time.time()
            for p, (t, st) in list(running.items()):
                if now - st > timeout:
                    print(f"timeout: {t['id']}", file=sys.stderr, flush=True)
                    os.kill(p, signal.SIGKILL)
            time.sleep(0.05)
    print(f"all done in {time.time() - started:.0f}s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
