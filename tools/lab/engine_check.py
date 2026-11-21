"""Lab: run each sample commit's full candidate set through the engine and compare
with the labeled answers. Run inside the lab image with the repo at /app."""
import json, sys, time, resource, os
sys.path.insert(0, "/app/rwx")
os.environ.setdefault("RWX_SRC", "/sqlglot")
import selector as sel
from engine import Engine

def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF); c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime + c.ru_utime + c.ru_stime

data = sys.argv[1]; mode_force = sys.argv[2] if len(sys.argv) > 2 else None
tests = json.load(open(f"{data}/tests.json"))["tests"]
ans = json.load(open(f"{data}/answers.json"))["commits"] if os.path.exists(f"{data}/answers.json") else {}
dur = {t["number"]: t["seconds"] for t in tests}
valid = 0
for n in dur: valid |= 1 << n
t0 = cpu()
eng = Engine("/sqlglot", tests, log=lambda *a: print(*a, file=sys.stderr))
print(f"warm cpu {cpu()-t0:.2f}s, warm codes {len(eng.warm_keys)}", flush=True)
an = sel.Analyzer(sel.Map(sel.MAP), valid)
import glob
bad = 0
for p in sorted(glob.glob(f"{data}/commits/*.patch")):
    cid = os.path.basename(p)[:-6]
    mask, why = an.analyze(open(p).read())
    nums = [n for n in dur if mask >> n & 1]
    if mode_force == "reimport":
        eng.plan = (lambda orig: (lambda ch: ("reimport", "forced")))(eng.plan)
    c0 = cpu(); w0 = time.time()
    res, info = eng.run(an.py_changes, an.data_changes, nums, timeout=600)
    failed = {n for n, s in res.items() if s == "F"}
    truth = set(ans.get(cid, {}).get("failing", []))
    ok = failed == (truth & set(nums)) and truth <= set(nums)
    if not ok: bad += 1
    print(f"{cid} {'OK ' if ok else 'BAD'} n={len(nums)} fail={len(failed)} truth={len(truth)} extra={sorted(failed-truth)[:5]} missing={sorted(truth-failed)[:5]} mode={info['mode']} {str(info['why'])[:60]} cpu={cpu()-c0:.2f} wall={time.time()-w0:.1f} expected_secs={sum(dur[n] for n in nums):.1f}", flush=True)
print("BAD", bad)
