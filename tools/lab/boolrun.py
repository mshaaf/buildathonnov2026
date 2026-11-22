"""Per-test value profiles of `and`/`or` sites (lab only), same harness as cmprun.py.

For every two-operand BoolOp `A and B` / `A or B` in sqlglot/ whose right operand is
syntactically pure (names, attributes, constants, ==/!=/is/is not between those, `not`)
and that is not lexically inside a class body, records per test whether flipping the
operator could change the result at some evaluation:

  pinned evaluates B (and: A truthy; or: A falsy): pinned result B, flipped result A;
  otherwise: pinned result A, flipped would evaluate B (speculatively, guarded) and
  return it. In a test context (if/while/assert/ternary test/comprehension if/not) the
  results differ iff their truthiness differs; elsewhere iff they are not the same
  object. A speculative evaluation that raises counts as "differs".

The original evaluation order and result are kept: A is evaluated once, bool(A) is
called once, B is evaluated for real exactly when the pinned code would (via a lambda
thunk, which is why class bodies are skipped: a lambda there cannot see class names).

usage: boolrun.py SQLGLOT_ROOT tests.json OUTDIR [--jobs 2] [--only ...] [--timeout 1200]
Writes OUTDIR/sites.json (id -> [file, line, col, op, ctx, speculate]) and OUTDIR/<n>.json with
"differs": [site ids].
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cmprun  # noqa: E402  (harness, loader; third-party imports in the parent)

import ast  # noqa: E402
import builtins  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402

SITES: list = []


def pure(e) -> bool:
    if isinstance(e, ast.Constant):
        return True
    if isinstance(e, ast.Name):
        return isinstance(e.ctx, ast.Load)
    if isinstance(e, ast.Attribute):
        return pure(e.value)
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
        return pure(e.operand)
    if isinstance(e, ast.Compare):
        return all(isinstance(o, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)) for o in e.ops) and pure(e.left) and all(
            pure(c) for c in e.comparators)
    return False


class Instrument(ast.NodeTransformer):
    def __init__(self, rel, test_ids):
        self.rel = rel
        self.test_ids = test_ids
        self.class_depth = 0  # > 0: lexically in a class body (not inside a def)

    def visit_ClassDef(self, node):
        self.class_depth += 1
        self.generic_visit(node)
        self.class_depth -= 1
        return node

    def visit_FunctionDef(self, node):
        saved, self.class_depth = self.class_depth, 0
        node.decorator_list = [self.visit(d) for d in node.decorator_list] if saved == 0 else node.decorator_list
        if saved == 0:
            node.args.defaults = [self.visit(d) for d in node.args.defaults]
        node.body = [self.visit(b) for b in node.body]
        self.class_depth = saved
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        return node

    def visit_AnnAssign(self, node):
        if node.value is not None:
            node.value = self.visit(node.value)
        return node

    def visit_BoolOp(self, node):
        test_ctx = id(node) in self.test_ids
        self.generic_visit(node)
        if self.class_depth or len(node.values) != 2 or not pure(node.values[1]):
            return node
        sid = len(SITES)
        op = "and" if isinstance(node.op, ast.And) else "or"
        # Speculation (evaluating B where the pinned code would not) only without attribute
        # access: an attribute can run a property or a lazy module __getattr__ (imports).
        spec = not any(isinstance(n, ast.Attribute) for n in ast.walk(node.values[1]))
        SITES.append((self.rel, node.lineno, node.col_offset, op, "test" if test_ctx else "value", spec))
        thunk = ast.Lambda(ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), node.values[1])
        call = ast.Call(ast.Name("_rwx_bool_", ast.Load()), [ast.Constant(sid), node.values[0], thunk], [])
        return ast.copy_location(call, node)


def test_contexts(tree) -> set:
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            ids.add(id(n.test))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            ids.add(id(n.operand))
        elif isinstance(n, ast.comprehension):
            ids.update(id(c) for c in n.ifs)
    return ids


def precompile(root):
    for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if not f.endswith(".py"):
                continue
            path = os.path.join(d, f)
            rel = os.path.relpath(path, root)
            tree = ast.parse(open(path, "rb").read(), path)
            tree = Instrument(rel, test_contexts(tree)).visit(tree)
            ast.fix_missing_locations(tree)
            cmprun.CODES[path] = compile(tree, path, "exec", dont_inherit=True)


def child(test, outdir):
    differs = set()
    sites = SITES
    probing = [False]

    def rec(sid, a, thunk):
        if probing[0]:
            return (a and thunk()) if sites[sid][3] == "and" else (a or thunk())
        op, ctx = sites[sid][3], sites[sid][4]
        ta = bool(a)  # the pinned code calls bool(A) exactly once
        if (op == "and") == ta:
            b = thunk()  # pinned evaluates B: for real, exceptions propagate
            if sid not in differs:
                probing[0] = True
                try:
                    same = (bool(b) == ta) if ctx == "test" else (a is b)
                except BaseException:  # noqa: BLE001
                    same = False
                finally:
                    probing[0] = False
                if not same:
                    differs.add(sid)
            return b
        if sid not in differs:
            if not sites[sid][5]:
                differs.add(sid)  # cannot speculate safely: the flip may differ here
                return a
            probing[0] = True
            try:
                b = thunk()  # speculative: what the flipped operator would evaluate
                same = (bool(b) == ta) if ctx == "test" else (a is b)
            except BaseException:  # noqa: BLE001
                same = False
            finally:
                probing[0] = False
            if not same:
                differs.add(sid)
        return a

    builtins._rwx_bool_ = rec
    sys.meta_path.insert(0, cmprun.Finder())

    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = cmprun.covrun.InlinePool
    concurrent.futures.process.ProcessPoolExecutor = cmprun.covrun.InlinePool
    status = "ok"
    try:
        mod = importlib.import_module(test["mod"])
        cls = getattr(mod, test["cls"])
        result = cmprun.covrun.Result()
        unittest.TestSuite([cls(test["meth"])]).run(result)
        if result.bad:
            status = "fail"
    except Exception:  # noqa: BLE001
        status = "crash"
    out = {"number": test["number"], "status": status, "differs": sorted(differs)}
    tmp = os.path.join(outdir, f".{test['number']}.json")
    with open(tmp, "w") as fh:
        json.dump(out, fh)
    os.replace(tmp, os.path.join(outdir, f"{test['number']}.json"))


def main():
    root, tests_json, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    jobs, only, timeout = 2, None, 1200.0
    args = sys.argv[4:]
    while args:
        a = args.pop(0)
        if a == "--jobs":
            jobs = int(args.pop(0))
        elif a == "--only":
            only = {int(x) for x in args.pop(0).split(",")}
        elif a == "--timeout":
            timeout = float(args.pop(0))
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    precompile(os.path.abspath(root))
    json.dump(SITES, open(os.path.join(outdir, "sites.json"), "w"))
    print(f"instrumented {len(SITES)} and/or sites, {time.time() - t0:.1f}s", file=sys.stderr, flush=True)
    tests = json.load(open(tests_json))["tests"]
    todo = []
    for t in tests:
        if only is not None and t["number"] not in only:
            continue
        if os.path.exists(os.path.join(outdir, f"{t['number']}.json")):
            continue
        mod, cls, meth = cmprun.covrun.parse_id(t["id"])
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
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid:
                running.pop(pid, None)
                done += 1
                if done % 100 == 0:
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
