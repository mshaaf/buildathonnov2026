"""Cheap equivalence check for test_execute_tpcds (RWX_TPCDS_STAGES=1).

Each TPC-DS query in the test goes: parse_one(sql) and a pickle round trip (in the
test), find_tables + execute() (in the pool worker), and transpile(sql, write="duckdb")
for the expected side. execute() is ensure_tables / ensure_schema, then optimize(),
then Plan + PythonExecutor on the optimized expression.

At build time this records, for every query: the tables the worker selects, the
duckdb SQL, and a fingerprint of the expression optimize() hands to the planner
(class, args, type, meta and comments of every node). It also records every code
object that runs inside execute() outside optimize(), for all queries: the
"post" code.

A patch whose changed functions are none of the post code can only change a query's
result through those three recorded values. In the session the check recomputes them
for the affected queries with the patched code; if they are identical the planner and
executor run unchanged code on identical input, so the query's result and the expected
frame are the pinned ones, and the test passes as it does at the pinned commit. Any
difference, or any error, fails the check and the test stays printed.

    python /app/rwx/tpcds_stages.py /sqlglot      (Dockerfile)
"""

import hashlib
import json
import pickle
import sys
import time
from pathlib import Path

PROFILE = ".rwx-tpcds-stages.json"


class _Tag(str):
    """A marker on the fingerprint stack (never a value of the tree)."""


def fingerprint(node) -> str:
    """Digest of everything a planner can read from an expression tree."""
    out = []
    stack = [node]
    while stack:
        v = stack.pop()
        if type(v) is _Tag:
            out.append(str(v))
            continue
        args = getattr(v, "args", None)
        if isinstance(args, dict) and type(v).__module__.startswith("sqlglot"):
            out.append("(" + type(v).__module__ + "." + type(v).__qualname__)
            items = [(_Tag(k + "="), x) for k, x in args.items()]
            for attr in ("_type", "_meta", "comments"):
                x = getattr(v, attr, None)
                if x is not None:
                    items.append((_Tag("@" + attr + "="), x))
            stack.append(_Tag(")"))
            for tag, x in reversed(items):
                stack.append(x)
                stack.append(tag)
        elif isinstance(v, (list, tuple)):
            stack.append(_Tag("]"))
            for x in reversed(v):
                stack.append(x)
                stack.append(_Tag(","))
            out.append("[" + type(v).__name__)
        elif isinstance(v, dict):
            stack.append(_Tag("}"))
            for k, x in reversed(list(v.items())):
                stack.append(x)
                stack.append(_Tag(repr(k) + ":"))
            out.append("{")
        elif isinstance(v, (set, frozenset)):
            out.append("set" + repr(sorted(map(repr, v))))
        else:
            out.append(type(v).__name__ + ":" + repr(v))
    return hashlib.sha1("\x01".join(out).encode("utf-8", "surrogatepass")).hexdigest()


def stage_values(tests, sql, args):
    """What the test's worker and expected side derive from one query before planning.
    Returns None for a query the test does not execute (after parsing it as the test does)."""
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as executor
    from sqlglot.schema import ensure_schema

    expression = parse_one(sql)
    expression, meta = pickle.loads(pickle.dumps((expression, args)))  # the pool's round trip
    if not meta.get("execute"):
        return None
    tables = sorted({t.name for t in find_tables(expression)})
    schema = ensure_schema(pickle.loads(pickle.dumps(tests.TPCDS_SCHEMA)), dialect=None)
    optimized = executor.optimize(expression, schema, leave_tables_isolated=True, dialect=None)
    return {"tables": tables, "fp": fingerprint(optimized), "duck": transpile(sql, write="duckdb")[0]}


def build(root: Path):
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sqlglot.executor as executor
    import tests.test_executor as tests

    klass = tests.TestExecutor
    klass.setUpClass()
    sqls = klass.tpcds_sqls
    prefix = str(root) + "/"
    post = set()
    mon = sys.monitoring
    tool = mon.PROFILER_ID

    def on_start(code, offset):
        if code.co_filename.startswith(prefix):
            post.add(f"{code.co_filename[len(prefix):]}:{code.co_firstlineno}:{code.co_qualname}")
        return mon.DISABLE

    seen = {}
    real_optimize = executor.optimize
    real_execute = tests.execute

    def optimize(*a, **k):
        mon.set_events(tool, 0)
        try:
            result = real_optimize(*a, **k)
            seen["fp"] = fingerprint(result)
            return result
        finally:
            mon.set_events(tool, mon.events.PY_START)

    def execute(*a, **k):
        mon.set_events(tool, mon.events.PY_START)
        try:
            result = real_execute(*a, **k)
            return pickle.loads(pickle.dumps(result))  # the pool's round trip of the result
        finally:
            mon.set_events(tool, 0)

    # first pass: what the check recomputes, and its CPU
    queries = []
    for args, sql, _ in sqls:
        start = time.process_time()
        values = stage_values(tests, sql, args)
        ms = max(1, round((time.process_time() - start) * 1000))
        queries.append({"execute": False, "ms": ms} if values is None else {"execute": True, "ms": ms, **values})

    # second pass: the test's own execution, recording the code execute() runs outside optimize()
    tests.initializer(*pickle.loads(pickle.dumps((tests.TPCDS_SCHEMA, klass.tpcds_tables))))
    mon.use_tool_id(tool, "rwx-tpcds-stages")
    mon.register_callback(tool, mon.events.PY_START, on_start)
    executor.optimize = optimize
    tests.execute = execute
    try:
        for (args, sql, _), q in zip(sqls, queries):
            if not q["execute"]:
                continue
            seen.clear()
            expression, meta = pickle.loads(pickle.dumps((tests.parse_one(sql), args)))
            tests.mp_execute(expression, meta)
            q["usable"] = seen.get("fp") == q["fp"]  # the check sees what execute() planned
    finally:
        executor.optimize = real_optimize
        tests.execute = real_execute
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.PY_START, None)
        mon.free_tool_id(tool)
    return {"count": len(sqls), "queries": queries, "post": sorted(post)}


def applicable(profile, root, swaps, indices):
    """Estimated CPU (ms) of the check for these queries, or None when it cannot decide."""
    if not profile or not indices:
        return None
    post = profile.get("_post_set")
    if post is None:
        post = profile["_post_set"] = set(profile["post"])
    for (filename, line, name), _ in swaps:
        rel = filename.removeprefix(root + "/")
        if f"{rel}:{line}:{name}" in post:
            return None
    queries = profile["queries"]
    ms = 0
    for i in indices:
        if i >= len(queries) or (queries[i]["execute"] and not queries[i].get("usable")):
            return None
        ms += queries[i]["ms"]
    return ms


def install(indices, profile):
    """Replace test_execute_tpcds with the check on these queries (in the session child)."""
    import tests.test_executor as tests

    queries = profile["queries"]

    def staged(self):
        sqls = self.tpcds_sqls
        if len(sqls) != profile["count"]:
            raise AssertionError("TPC-DS fixture changed")
        for i in indices:
            args, sql, _ = sqls[i]
            values = stage_values(tests, sql, args)
            want = queries[i]
            if (values is None) != (not want["execute"]):
                raise AssertionError(f"query {i + 1}: execute flag differs")
            if values is not None and (values["tables"] != want["tables"] or values["fp"] != want["fp"]
                                       or values["duck"] != want["duck"]):
                raise AssertionError(f"query {i + 1}: planner input differs from the pinned commit")

    tests.TestExecutor.test_execute_tpcds = staged


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    t0 = time.process_time()
    data = build(root)
    (root / PROFILE).write_text(json.dumps(data, sort_keys=True))
    bad = sum(1 for q in data["queries"] if q["execute"] and not q["usable"])
    print(f"tpcds stages: {len(data['queries'])} queries ({bad} unusable), {len(data['post'])} post code objects,"
          f" {sum(q['ms'] for q in data['queries'])} ms, built in {time.process_time() - t0:.1f}s", file=sys.stderr)
