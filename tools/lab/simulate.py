"""Lab: replay the selector's real policy against ground truth.

    python3 tools/lab/simulate.py LAB_DIR [LAB_DIR...]   (env RWX_* knobs apply)

Candidates are recomputed with the current analyzer; each batch the policy asks for is
answered from the truth records (stop-at-first-failure in engine order) and charged at
the calibrated per-test CPU. A failing test outside the recomputed candidates is a miss.
Sample dirs (with answers.json) are replayed from the answers.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "rwx"))
os.environ.setdefault("RWX_SRC", "/home/user/lab/sqlglot")
import selector as sel  # noqa: E402

_M = sel.Map(sel.MAP)
CALIB = {int(k): v for k, v in _M.cpu_ms.items()}
sel.CLS_SETUP.update(_M.cls_setup_ms)
WARM_MS = 2800
SESSION_MS = 40  # fork + apply


def engine_order(batch, tests):
    groups = {}
    for n in batch:
        path, cls, _ = tests[n]["id"].split("::")
        groups.setdefault((path, cls), []).append(n)
    return [n for g in groups.values() for n in g]


def replay(C, F, data_only, tests, est, focus=None, known=None):
    gen = sel.policy(C, sel.DUR, data_only, focus, known)
    cpu = 0
    sess = False
    try:
        batch, stop_first = next(gen)
        while True:
            if not sess:
                cpu += SESSION_MS
                sess = True
            res = {}
            seen_cls = set()
            for n in engine_order(batch, tests):
                res[n] = "F" if n in F else "P"
                cpu += est(n)
                c = sel.CLS_OF.get(n)
                if c not in seen_cls:
                    seen_cls.add(c)
                    cpu += sel.CLS_SETUP.get(c, 0)
                if stop_first and n in F:
                    break
            batch, stop_first = gen.send(res)
    except StopIteration as e:
        return e.value, cpu


def main():
    tot = {"sim": 0, "cpu": 0, "miss": 0, "oracle": 0, "n": 0, "static": 0}
    rows = []
    for L in sys.argv[1:]:
        tests = json.load(open(f"{L}/tests.json"))["tests"]
        dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
        sel.DUR.clear(); sel.DUR.update(dur)
        sel.FILE_OF.update({t["number"]: t["file"] for t in tests})
        sel.CLS_OF.update({t["number"]: t["id"].rsplit("::", 1)[0] for t in tests})
        sel.NO_RUN.update(t["number"] for t in tests if t["id"] in sel.NO_RUN_IDS)
        est = lambda n: CALIB.get(n, dur[n])  # noqa: E731
        sel.CPU_EST.clear(); sel.CPU_EST.update({n: est(n) for n in dur})
        valid = 0
        for n in dur:
            valid |= 1 << n
        lazy = 0
        for t in tests:
            if t["id"] in sel.NO_RUN_IDS:
                lazy |= 1 << t["number"]
        an = sel.Analyzer(sel.Map(sel.MAP), valid, lazy)
        if os.path.exists(f"{L}/answers.json"):
            truth = {c: (set(v["failing"]), None) for c, v in json.load(open(f"{L}/answers.json"))["commits"].items()}
        else:
            truth = {}
            for f in glob.glob(f"{L}/truth.*.jsonl"):
                for l in open(f):
                    r = json.loads(l)
                    truth[r["commit"]] = (set(r["fail"]), set(r["cand"]))
        any_engine = False
        for cid, (F, oldC) in sorted(truth.items()):
            text = open(f"{L}/commits/{cid}.patch").read()
            mask, why = an.analyze(text)
            C = [n for n in dur if mask >> n & 1]
            data_only = bool(an.data_changes) and not an.py_changes
            known = set()
            for rel in an.data_changes:
                mid = _M.opens.get(rel)
                if mid is not None:
                    known |= {n for n in C if _M.masks[mid] >> n & 1}
            out, cpu = replay(C, F, data_only, tests, est, sel.commit_focus(_M, C, an.py_changes), known)
            if cpu:
                any_engine = True
            miss = bool(F - out)
            w = sel.pack(sorted(out), dur)
            cost = (max(sum(dur[n] for n in x) for x in w) + 2000 * len(w)) if w else 0
            wo = sel.pack(sorted(F), dur)
            ocost = (max(sum(dur[n] for n in x) for x in wo) + 2000 * len(wo)) if wo else 0
            ws = sel.pack(sorted(C), dur)
            scost = (max(sum(dur[n] for n in x) for x in ws) + 2000 * len(ws)) if ws else 0
            tot["sim"] += cost; tot["cpu"] += cpu; tot["miss"] += miss; tot["oracle"] += ocost; tot["n"] += 1; tot["static"] += scost
            rows.append((cost + cpu + 300000 * miss - ocost, cid, len(C), len(F), len(out), cost, cpu, miss, "; ".join(why)[:90]))
        if any_engine:
            tot["cpu"] += WARM_MS
    rows.sort(reverse=True)
    for r in rows[: int(os.environ.get("TOP", "15"))]:
        print(f"{r[0]/1000:7.1f} {r[1]} C={r[2]:4} F={r[3]:4} out={r[4]:4} sim={r[5]/1000:5.1f} cpu={r[6]/1000:5.1f} miss={r[7]} | {r[8]}")
    n = tot["n"]
    print(f"n={n} sim={tot['sim']/1000:.1f} cpu={tot['cpu']/1000:.1f} misses={tot['miss']} total={(tot['sim']+tot['cpu']+300000*tot['miss'])/1000:.1f} "
          f"static={tot['static']/1000:.1f} oracle={tot['oracle']/1000:.1f}  per-commit total={(tot['sim']+tot['cpu']+300000*tot['miss'])/1000/max(n,1):.2f}")


if __name__ == "__main__":
    main()
