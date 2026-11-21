"""Lab: re-run candidates of lab commits that involve executor tests with the shared
executor setup and compare every outcome with recorded ground truth."""
import glob, json, os, sys
sys.path.insert(0, "/app/rwx")
os.environ.setdefault("RWX_SRC", "/sqlglot"); os.environ.setdefault("RWX_BROAD", "1")
import selector as sel
from engine import Engine
data = sys.argv[1]; limit = int(sys.argv[2])
tests = json.load(open(f"{data}/tests.json"))["tests"]
cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
EX = "tests/test_executor.py::TestExecutor"
truth = {}
for f in glob.glob(f"{data}/truth.*.jsonl"):
    for l in open(f):
        r = json.loads(l); truth[r["commit"]] = r
eng = Engine("/sqlglot", tests, presetup=(EX,))
print("presetup", eng.presetup_done, flush=True)
an = sel.Analyzer(sel.Map(sel.MAP), sum(1 << t["number"] for t in tests))
bad = done = 0
for cid, r in sorted(truth.items()):
    run = [n for n in r["cand"] if cls_of[n] == EX and tests[n]["seconds"] < 3]
    if not run:
        continue
    an.analyze(open(f"{data}/commits/{cid}.patch").read())
    res, info = eng.run(an.py_changes, an.data_changes, run, timeout=300)
    got = {n for n, s in res.items() if s == "F"}
    want = set(r["fail"]) & set(run)
    ok = got == want
    bad += not ok; done += 1
    print(cid, "OK " if ok else "BAD", info["mode"], len(run), sorted(got - want)[:4], sorted(want - got)[:4], flush=True)
    if done >= limit:
        break
print("BAD", bad, "of", done)
