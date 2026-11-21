"""Lab: ground truth for synthetic patches. Runs each commit's whole candidate set
through the engine and records every test's outcome and CPU time.

    python /app/tools/lab/truth.py DATA_DIR OUT_JSONL [SHARD NSHARDS]
"""
import glob
import json
import os
import resource
import sys
import time

sys.path.insert(0, "/app/rwx")
os.environ.setdefault("RWX_SRC", "/sqlglot")
os.environ.setdefault("RWX_BROAD", "1")
import selector as sel  # noqa: E402
from engine import Engine  # noqa: E402

data, out = sys.argv[1], sys.argv[2]
shard, nshards = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (0, 1)
tests = json.load(open(f"{data}/tests.json"))["tests"]
dur = {t["number"]: t["seconds"] for t in tests}
valid = 0
for n in dur:
    valid |= 1 << n
eng = Engine("/sqlglot", tests)
an = sel.Analyzer(sel.Map(sel.MAP), valid)
done = set()
if os.path.exists(out):
    done = {json.loads(l)["commit"] for l in open(out)}
patches = sorted(glob.glob(f"{data}/commits/*.patch"))
with open(out, "a") as fh:
    for i, p in enumerate(patches):
        if i % nshards != shard:
            continue
        cid = os.path.basename(p)[:-6]
        if cid in done:
            continue
        mask, why = an.analyze(open(p).read())
        nums = [n for n in dur if mask >> n & 1]
        w0 = time.time()
        res, info = eng.run(an.py_changes, an.data_changes, nums, timeout=400)
        rec = {
            "commit": cid,
            "cand": nums,
            "fail": sorted(n for n, s in res.items() if s == "F"),
            "cpu": {str(n): c for n, c in eng.last_cpu.items()},
            "mode": info["mode"],
            "timeout": info["timeout"],
            "crash": info["crash"],
            "why": why,
            "wall": time.time() - w0,
        }
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
