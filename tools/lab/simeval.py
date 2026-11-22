"""Lab: replay the real policy against ground truth, with the runtime details simulate.py
leaves out, and break the result down by commit class.

    RWX_SRC=$LAB/sqlglot python tools/lab/simeval.py DIR [DIR...]

Compared with simulate.py this also models
  - the shared TestExecutor setup (class setup charged once to the warm-up, as at run time),
  - the per-query TPC-DS check the scheduler runs after the policy (needs the engine:
    SIM_EXEC=0 turns it off and the warm-up with it),
  - re-import session cost (REIMPORT_MS) for commits the engine cannot patch by swapping code.

Env: RATIO=0.5,1.0   CPU weights to report (scorer CPU / calibrated estimate)
     CPU_SRC=est|truth  charge calibrated estimates (default) or the CPU the truth run measured
     TOP=n           worst commits to list     DUMP=path  per-commit JSON lines for A/B diffs
"""
import glob
import json
import os
import sys

for k, v in (("SKIP_INTEGRATION", "0"), ("OPENBLAS_NUM_THREADS", "1"), ("OMP_NUM_THREADS", "1"),
             ("PYTHONDONTWRITEBYTECODE", "1"), ("RWX_SRC", "/sqlglot")):
    os.environ.setdefault(k, v)
os.environ["RWX_SRC"] = os.path.abspath(os.environ["RWX_SRC"])  # the engine matches code by absolute path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "rwx"))
if os.environ["RWX_SRC"] not in sys.path:
    sys.path.insert(0, os.environ["RWX_SRC"])
import selector as sel  # noqa: E402

M = sel.Map(sel.MAP)
SIM_EXEC = os.environ.get("SIM_EXEC", "1") == "1"
CPU_SRC = os.environ.get("CPU_SRC", "est")
RATIOS = [float(x) for x in os.environ.get("RATIO", "0.5,1.0").split(",")]
WARM_MS = 2800
PRESETUP_MS = 1600
SESSION_MS = 40
REIMPORT_MS = int(os.environ.get("REIMPORT_MS", "400"))
EXEC_CLS = "tests/test_executor.py::TestExecutor"
SLOW = 3000  # recorded ms: tests whose drops are tracked


def bucket(nC, F, data_only):
    if nC == 0:
        return "empty"
    if data_only:
        return "static"
    size = "1-50" if nC <= 50 else "51-500" if nC <= 500 else "500+"
    return f"{size} {'fail' if F else 'clean'}"


def setup_tests(tests):
    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    sel.DUR.clear(); sel.DUR.update(dur)
    sel.FILE_OF.clear(); sel.FILE_OF.update({t["number"]: t["file"] for t in tests})
    sel.CLS_OF.clear(); sel.CLS_OF.update({t["number"]: t["id"].rsplit("::", 1)[0] for t in tests})
    sel.NO_RUN.clear(); sel.NO_RUN.update(t["number"] for t in tests if t["id"] in sel.NO_RUN_IDS)
    sel.CPU_EST.clear(); sel.CPU_EST.update({n: M.cpu_ms.get(n, dur[n]) for n in dur})
    sel.CLS_SETUP.clear(); sel.CLS_SETUP.update(M.cls_setup_ms)
    if SIM_EXEC:
        sel.CLS_SETUP[EXEC_CLS] = 0  # presetup in the warm parent
    return dur


_ENGINE = None


def engine(tests):
    global _ENGINE
    if _ENGINE is None:
        from engine import Engine
        _ENGINE = Engine(os.environ["RWX_SRC"], tests, presetup=(EXEC_CLS,))
    return _ENGINE


def exec_info(eng, py, data, case_site):
    """(mode, executor_ms, partial) as engine.Session computes them."""
    mode, swaps = eng.plan(py)
    if not (eng.executor_profile and not data and len(py) == 1 and mode == "swap" and swaps
            and next(iter(py)).startswith("sqlglot/")):
        return mode, None, None
    from executor_cases import affected_cases
    idx = affected_cases(eng.executor_profile, eng.root, swaps, case_site)
    if idx is None:
        return mode, None, None
    ms = eng.executor_profile["ms"]
    case_ms = sum(ms[i] for i in idx)
    if case_ms <= 5000:
        return mode, case_ms, False
    return mode, None, None  # RWX_TPCDS_SAMPLE_MS=0: no partial gate


def replay(C, F, data_only, focus, known, cost, info, exec_n):
    gen = sel.policy(C, sel.DUR, data_only, focus, known)
    cpu, ran, batches = 0, set(), 0
    mode, exec_ms, partial = info
    try:
        batch, stop_first = next(gen)
        cpu += SESSION_MS + (REIMPORT_MS if mode == "reimport" else 0)
        while True:
            batches += 1
            res, seen_cls = {}, set()
            groups = {}
            for n in batch:
                groups.setdefault(sel.CLS_OF[n], []).append(n)
            for n in (n for g in groups.values() for n in g):
                res[n] = "F" if n in F else "P"
                ran.add(n)
                cpu += cost(n)
                c = sel.CLS_OF.get(n)
                if c not in seen_cls:
                    seen_cls.add(c)
                    cpu += sel.CLS_SETUP.get(c, 0)
                if stop_first and n in F:
                    break
            batch, stop_first = gen.send(res)
    except StopIteration as e:
        out = set(e.value)
    note = ""
    if batches and exec_n is not None and exec_ms is not None and exec_n not in ran:
        ecpu = exec_ms + sel.CLS_SETUP.get(EXEC_CLS, 0)
        if exec_n in out and not partial and sel._pack_cost(out) - sel._pack_cost(out - {exec_n}) >= 3 * ecpu:
            cpu += ecpu
            note = "verify"
            if exec_n not in F:
                out.discard(exec_n)
        elif exec_n in C and exec_n not in out:
            cpu += ecpu
            note = "gate"
            if exec_n in F:
                out.add(exec_n)
    return out, cpu, ran, note


def load_truth(L):
    if os.path.exists(f"{L}/answers.json"):
        return {c: (set(v["failing"]), {}, False) for c, v in json.load(open(f"{L}/answers.json"))["commits"].items()}
    last = {}
    for f in sorted(glob.glob(f"{L}/truth.*.jsonl")):
        for line in open(f):
            r = json.loads(line)
            last[r["commit"]] = r  # a re-run ("redo") is appended later: the last record wins
    truth = {}
    for cid, r in last.items():
        bad = bool(r.get("timeout") or r.get("crash"))
        if bad and os.environ.get("KEEP_UNRELIABLE") != "1":
            continue  # unreported tests were marked failing: not ground truth
        truth[cid] = (set(r["fail"]), {int(k): int(v * 1000) for k, v in r.get("cpu", {}).items()}, bad)
    if os.path.exists(f"{L}/pytest.jsonl"):  # real-pytest failing sets override the engine's
        for line in open(f"{L}/pytest.jsonl"):
            r = json.loads(line)
            if "fail" in r and r["commit"] in truth:
                truth[r["commit"]] = (set(r["fail"]),) + truth[r["commit"]][1:]
    return truth


def main():
    rows, drops = [], {}
    settle_peak = [0.0]
    any_engine = False
    for L in sys.argv[1:]:
        tests = json.load(open(f"{L}/tests.json"))["tests"]
        names = {t["number"]: t["id"].rsplit("::", 1)[-1] for t in tests}
        dur = setup_tests(tests)
        eng = engine(tests) if SIM_EXEC else None
        exec_n = eng.executor_test if eng else None
        valid = sum(1 << n for n in dur)
        lazy = sum(1 << n for n in sel.NO_RUN)
        an = sel.Analyzer(M, valid, lazy)
        truth = load_truth(L)
        sel.SETTLE_BUDGET[0] = sel.SETTLE_BUDGET_PER_COMMIT_MS * len(truth)
        sel.SETTLE_SPENT[0] = 0
        for cid, (F, tcpu, unreliable) in sorted(truth.items()):
            mask, why = an.analyze(open(f"{L}/commits/{cid}.patch").read())
            C = [n for n in dur if mask >> n & 1]
            py, data = dict(an.py_changes), dict(an.data_changes)
            data_only = not py
            known = set()
            for rel in data:
                mid = M.opens.get(rel)
                if mid is not None:
                    known |= {n for n in C if M.masks[mid] >> n & 1}
            focus = sel.commit_focus(M, C, py)
            info = exec_info(eng, py, data, an.case_site) if (eng and py) else ("swap", None, None)
            cost = (lambda n: tcpu.get(n, sel.CPU_EST[n])) if CPU_SRC == "truth" else (lambda n: sel.CPU_EST[n])
            out, cpu, ran, note = replay(C, F, data_only, focus, known, cost, info, exec_n)
            any_engine |= bool(cpu)
            settle_peak[0] = max(settle_peak[0], sel.SETTLE_SPENT[0] / max(1, len(truth)))
            sim = sel._pack_cost(out)
            orc = sel._pack_cost(F)
            miss = bool(F - out)
            for n in C:
                if dur[n] >= SLOW and n not in out and n not in ran and n not in sel.NO_RUN:
                    d = drops.setdefault(names[n], [0, 0])
                    d[0] += 1
                    d[1] += n in F
            rows.append(dict(cid=f"{os.path.basename(L.rstrip('/'))}/{cid}", nC=len(C), nF=len(F), out=len(out), sim=sim,
                             cpu=cpu, miss=miss, oracle=orc, cls=bucket(len(C), F, data_only), note=note,
                             unreliable=unreliable, why="; ".join(why)[:80]))
    warm = (WARM_MS + (PRESETUP_MS if SIM_EXEC else 0)) if any_engine else 0
    report(rows, drops, warm)
    print(f"settle est CPU per commit (largest dir): {settle_peak[0]:.0f} ms (budget {sel.SETTLE_BUDGET_PER_COMMIT_MS} ms)")


def report(rows, drops, warm):
    r0 = RATIOS[0]
    rows.sort(key=lambda r: -(r["sim"] + r0 * r["cpu"] + 300000 * r["miss"] - r["oracle"]))
    for r in rows[: int(os.environ.get("TOP", "10"))]:
        print(f"{(r['sim'] + r0 * r['cpu'] - r['oracle']) / 1000:6.1f} {r['cid']:22} C={r['nC']:4} F={r['nF']:3} out={r['out']:4} "
              f"sim={r['sim'] / 1000:5.1f} cpu={r['cpu'] / 1000:5.1f}{' MISS' if r['miss'] else ''} {r['note']} | {r['why']}")
    classes = sorted({r["cls"] for r in rows})
    print(f"{'class':14} {'n':>4} {'sim':>7} {'cpu':>7} {'oracle':>7} {'miss':>4}")
    for c in classes + ["ALL"]:
        rs = [r for r in rows if c in ("ALL", r["cls"])]
        print(f"{c:14} {len(rs):4} {sum(r['sim'] for r in rs) / 1000:7.1f} {sum(r['cpu'] for r in rs) / 1000:7.1f} "
              f"{sum(r['oracle'] for r in rs) / 1000:7.1f} {sum(r['miss'] for r in rs):4}")
    sim = sum(r["sim"] for r in rows)
    cpu = sum(r["cpu"] for r in rows) + warm
    miss = sum(r["miss"] for r in rows)
    print("drops (slow tests left out unrun: times, of which failing):",
          ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(drops.items())) or "none")
    print(f"unreliable truth records: {sum(r['unreliable'] for r in rows)}")
    for ratio in RATIOS:
        print(f"n={len(rows)} ratio={ratio} sim={sim / 1000:.1f} cpu={cpu / 1000:.1f} misses={miss} "
              f"total={(sim + ratio * cpu + 300000 * miss) / 1000:.1f}")
    if os.environ.get("DUMP"):
        with open(os.environ["DUMP"], "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)  # skip teardown of the frozen warm-up heap (duckdb leak reports)
