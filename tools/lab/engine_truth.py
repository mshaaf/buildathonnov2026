"""Native engine truth for a data dir (lab only; tools/lab/truth.py is the container version).

usage: RWX_SRC=<pinned tree> engine_truth.py DATA_DIR OUT_JSONL

Runs each commit's whole un-narrowed candidate set (RWX_CMP_PROFILE off) through the
engine in a forked session and records the failing tests, in truth.py's format.
"""

import glob
import json
import os
import sys

os.environ["RWX_CMP_PROFILE"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "rwx"))
import selector as sel  # noqa: E402
from engine import Engine  # noqa: E402


def main():
    data, out = sys.argv[1], sys.argv[2]
    tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
    valid = 0
    for t in tests:
        valid |= 1 << t["number"]
    done = set()
    if os.path.exists(out):
        done = {json.loads(line)["commit"] for line in open(out)}
    an = sel.Analyzer(sel.Map(sel.MAP), valid)
    eng = Engine(str(sel.SRC), tests)
    no_run = {t["number"] for t in tests if t["id"] in sel.NO_RUN_IDS}
    with open(out, "a") as fh:
        for p in sorted(glob.glob(os.path.join(data, "commits", "*.patch"))):
            cid = os.path.basename(p)[:-6]
            if cid in done:
                continue
            mask, why = an.analyze(open(p).read())
            cand = [t["number"] for t in tests if mask >> t["number"] & 1]
            run = [n for n in cand if n not in no_run]
            res, info = eng.run(an.py_changes, an.data_changes, run, timeout=600 + 3 * len(run)) if run else ({}, {})
            fail = sorted(n for n in run if res.get(n) != "P") + sorted(n for n in cand if n in no_run)
            fh.write(json.dumps({"commit": cid, "cand": cand, "fail": fail, "info": {k: str(v)[:200] for k, v in info.items()}}) + "\n")
            fh.flush()
            print(cid, len(cand), len(fail), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
