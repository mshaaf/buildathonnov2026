"""Lab: stand-ins (rwx/proxies.py) against ground truth from truth.py.
    python /app/tools/lab/standin_check.py TEST_ID LAB_DIR [LAB_DIR...] SHARD NSHARDS"""
import glob, json, os, sys
sys.path.insert(0, "/app/rwx")
os.environ["RWX_STANDINS"] = "1"
import selector as sel  # noqa: E402
from engine import Engine  # noqa: E402

tid, dirs, shard, nshards = sys.argv[1], sys.argv[2:-2], int(sys.argv[-2]), int(sys.argv[-1])
tests = json.load(open(f"{dirs[0]}/tests.json"))["tests"]
num = next(t["number"] for t in tests if t["id"] == tid)
eng = Engine("/sqlglot", tests)
valid = 0
for t in tests:
    valid |= 1 << t["number"]
an = sel.Analyzer(sel.Map(sel.MAP), valid)
an.class_masks = sel.class_masks(tests)
k = 0
for d in dirs:
    for f in sorted(glob.glob(f"{d}/truth.*.jsonl")):
        for line in open(f):
            r = json.loads(line)
            if num not in r["cand"] or r["timeout"] or r["crash"]:
                continue
            k += 1
            if k % nshards != shard:
                continue
            an.analyze(open(f"{d}/commits/{r['commit']}.patch").read())
            res, info = eng.run(an.py_changes, an.data_changes, [num], timeout=60)
            real = "F" if num in r["fail"] else "P"
            got = res.get(num, "?")
            print(json.dumps({"commit": r["commit"], "got": got, "real": real, "nfail": len(r["fail"]),
                              "mode": info["mode"], "cpu": eng.last_cpu.get(num)}), flush=True)
os._exit(0)
