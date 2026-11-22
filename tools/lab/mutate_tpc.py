"""Lab: sample-style edits to the TPC-H / TPC-DS optimizer fixtures (query text and
expected outputs). Usage: python3 tools/lab/mutate_tpc.py SQLGLOT_ROOT OUT_DIR PER_FILE SEED"""
import json, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from mutate import make_diff

root, out, per, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(f"{out}/commits", exist_ok=True)
meta = {}
for rel in ("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql", "tests/fixtures/optimizer/tpc-h/tpc-h.sql"):
    src = open(os.path.join(root, rel)).read()
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("--") and not l.startswith("#")]
    for k in range(per):
        i = rng.choice(idx)
        parts = lines[i].split(" ")
        words = [j for j, w in enumerate(parts) if w]
        j = rng.choice(words)
        r = rng.random()
        if any(c.isdigit() for c in parts[j]) and r < 0.4:
            parts[j] = "".join(str((int(c) + 1) % 10) if c.isdigit() else c for c in parts[j])
        elif r < 0.7:
            parts.insert(j, "x")
        else:
            parts[j] = parts[j] + "1"
        new = lines[:]
        new[i] = " ".join(parts)
        cid = f"t{seed:02d}{len(meta):03d}"
        open(f"{out}/commits/{cid}.patch", "w").write(make_diff(rel, src, "\n".join(new)))
        meta[cid] = {"file": rel, "line": i + 1}
json.dump(meta, open(f"{out}/meta.json", "w"))
print(len(meta), "patches")
