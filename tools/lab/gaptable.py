"""Lab: gap-to-oracle table from simeval DUMP files.

    python tools/lab/gaptable.py DUMP [TOP]
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "rwx"))
import selector as sel  # noqa: E402

LAB = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(os.path.join(LAB, "..", "..", "RWXblast-radius-sample", "sample", "tests.json")))["tests"]
name = {t["number"]: t["id"].rsplit("::", 1)[-1] for t in tests}
sel.DUR.update({t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests})
rows = [json.loads(line) for line in open(sys.argv[1])]
top = int(sys.argv[2]) if len(sys.argv) > 2 else 20


def bucket(r):
    n = r["nC"]
    size = "0" if n == 0 else "1-50" if n <= 50 else "51-500" if n <= 500 else "501+"
    return f"{size} {'fail' if r['nF'] else 'clean'}{' static' if r['cls'] == 'static' else ''}"


print(f"{'class':20} {'n':>4} {'sim':>7} {'oracle':>7} {'gap':>7} {'cpu':>6}")
for c in sorted({bucket(r) for r in rows}) + ["ALL"]:
    rs = [r for r in rows if c in ("ALL", bucket(r))]
    s, o, u = (sum(r[k] for r in rs) / 1000 for k in ("sim", "oracle", "cpu"))
    print(f"{c:20} {len(rs):4} {s:7.1f} {o:7.1f} {s - o:7.1f} {u:6.1f}")
slow, gaps = {}, []
for r in rows:
    out, F = set(r["outs"]), set(r["fails"])
    passing = [n for n in out if n not in F]
    base = sel._pack_cost(out)
    big = [n for n in passing if sel.DUR[n] >= 2000]
    for n in big:
        d = slow.setdefault(name[n], [0, 0])
        d[0] += 1
        d[1] += base - sel._pack_cost(out - {n})
    gaps.append((r["sim"] - r["oracle"], r, sorted(big, key=lambda n: -sel.DUR[n])))
gaps.sort(key=lambda g: -g[0])
print(f"\ntop {top} commits by gap (printed passing tests >= 2 s recorded):")
for g, r, big in gaps[:top]:
    print(f"{g / 1000:6.1f} {r['cid']:20} {bucket(r):14} C={r['nC']:4} F={r['nF']:4} out={r['out']:4} "
          f"cpu={r['cpu'] / 1000:4.1f} | " + ", ".join(f"{name[n]} {sel.DUR[n] / 1000:.1f}" for n in big))
total_gap = sum(g for g, *_ in gaps)
print(f"\nslow tests (>= 2 s) printed while passing: times, summed marginal worker cost (total gap {total_gap / 1000:.1f} s)")
for k, (n, m) in sorted(slow.items(), key=lambda kv: -kv[1][1]):
    print(f"  {k:40} {n:4} {m / 1000:7.1f}")
