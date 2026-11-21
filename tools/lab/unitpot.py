"""Lab: potential of call-level selection.

For lab commits whose change is inside functions, run every runnable candidate on
the ORIGINAL code, timing each independent unit (validate_* call, pool submission)
and noting whether it reached a changed code object. Prints, per commit, the CPU of
running all candidates vs. only the units that reach the change (plus non-unit code).

    python /app/tools/lab/unitpot.py /data OUT.jsonl SHARD NSHARDS MAXCOMMITS
"""
import ast
import glob
import json
import os
import sys
import time
import types

sys.path.insert(0, "/app/rwx")
os.environ.setdefault("RWX_SRC", "/sqlglot")
import selector as sel  # noqa: E402
from engine import Engine  # noqa: E402
from patchlib import Locator, changed_lines, parse_patch  # noqa: E402

data, out, shard, nsh, maxc = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
tests = json.load(open(f"{data}/tests.json"))["tests"]
dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
M = sel.Map(sel.MAP)
sel.DUR.update(dur); sel.CPU_EST.update({n: M.cpu_ms.get(n, dur[n]) for n in dur}); sel.CLS_SETUP.update(M.cls_setup_ms)
sel.CLS_OF.update({t["number"]: t["id"].rsplit("::", 1)[0] for t in tests})
sel.NO_RUN.update(t["number"] for t in tests if t["id"] in sel.NO_RUN_IDS)
an = sel.Analyzer(M, sum(1 << n for n in dur))
eng = Engine("/sqlglot", tests)

picked = []
for p in sorted(glob.glob(f"{data}/commits/*.patch")):
    text = open(p).read()
    mask, why = an.analyze(text)
    C = [n for n in dur if mask >> n & 1]
    if not C or an.data_changes or not an.py_changes:
        continue
    targets = set()
    ok = True
    for fp in parse_patch(text):
        rel = fp.path
        if rel not in an.py_changes:
            continue
        loc = Locator(ast.parse(an.py_changes[rel][0]))
        oc, _ = changed_lines(fp.hunks)
        for ln in oc:
            st = loc.locate(ln)
            if st is None or st.kind != "func":
                ok = False
                break
            targets.add((f"/sqlglot/{rel}", st.code_key))
    if not ok or not targets:
        continue
    heavy = {n for n in C if n in sel.NO_RUN or sel.CPU_EST[n] + sel.CLS_SETUP.get(sel.CLS_OF[n], 0) > sel.RUN_MAX_MS}
    rest = [n for n in C if n not in heavy]
    if not rest or sel.batch_cost(rest) <= sel.RUN_LIMIT_MS:
        continue
    picked.append((os.path.basename(p)[:-6], rest, targets))
picked = picked[:maxc]


def child(cid, rest, targets, wfd):
    import unittest

    mon = sys.monitoring
    tool = mon.DEBUGGER_ID
    state = {"hit": False}

    def on_start(code, off):
        state["hit"] = True
        return None

    mon.use_tool_id(tool, "unitpot")
    mon.register_callback(tool, mon.events.PY_START, on_start)
    # enable PY_START only on the changed code objects (and nested copies)
    seen = set()

    def walk(code):
        if id(code) in seen:
            return
        seen.add(id(code))
        if (code.co_filename, f"{code.co_firstlineno}:{code.co_name}") in targets:
            mon.set_local_events(tool, code, mon.events.PY_START)
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                walk(c)

    import gc

    for o in gc.get_objects():
        if type(o) is types.FunctionType and o.__code__.co_filename.startswith("/sqlglot/"):
            walk(o.__code__)
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None) or ""
        if f.startswith("/sqlglot/"):
            for v in vars(m).values():
                if isinstance(v, type):
                    for a in vars(v).values():
                        fn = getattr(a, "__func__", a)
                        if isinstance(fn, types.FunctionType):
                            walk(fn.__code__)

    units = {"cpu": 0.0, "reach": 0.0, "n": 0, "nr": 0}
    depth = {"d": 0}

    def wrap(fn):
        def w(*a, **k):
            if depth["d"]:
                return fn(*a, **k)
            depth["d"] += 1
            state["hit"] = False
            t0 = time.process_time()
            try:
                return fn(*a, **k)
            finally:
                dt = time.process_time() - t0
                depth["d"] -= 1
                units["cpu"] += dt
                units["n"] += 1
                if state["hit"]:
                    units["reach"] += dt
                    units["nr"] += 1

        return w

    from tests.dialects.test_dialect import Validator
    import engine as E

    Validator.validate_identity = wrap(Validator.validate_identity)
    Validator.validate_all = wrap(Validator.validate_all)
    E.InlinePool.submit = wrap(E.InlinePool.submit)
    try:
        import tests.test_transpile as tt

        tt.TestTranspile.validate = wrap(tt.TestTranspile.validate)
    except Exception:
        pass
    t0 = time.process_time()
    eng._run_tests(rest, os.open(os.devnull, os.O_WRONLY))
    total = time.process_time() - t0
    os.write(wfd, (json.dumps({"commit": cid, "n": len(rest), "total": total, **units}) + "\n").encode())


with open(out, "a") as fh:
    for i, (cid, rest, targets) in enumerate(picked):
        if i % nsh != shard:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                child(cid, rest, targets, w)
            finally:
                os._exit(0)
        os.close(w)
        buf = b""
        while True:
            c = os.read(r, 65536)
            if not c:
                break
            buf += c
        os.waitpid(pid, 0)
        if buf:
            fh.write(buf.decode())
            fh.flush()
