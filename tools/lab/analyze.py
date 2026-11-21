import json, glob, sys, collections
L = sys.argv[1]
tests = json.load(open(f"{L}/tests.json"))["tests"]; dur = {t["number"]: t["seconds"] for t in tests}
meta = json.load(open(f"{L}/meta.json"))
recs = [json.loads(l) for f in glob.glob(f"{L}/truth.*.jsonl") for l in open(f)]
print("records", len(recs))
EXP = 1.0
kinds = collections.Counter(); live = collections.Counter()
stats = collections.Counter()
cpu_ratio = []
for r in recs:
    k = meta[r["commit"]]["kind"]; kinds[k] += 1
    C = r["cand"]; F = set(r["fail"])
    if F: live[k] += 1
    for n, c in r["cpu"].items():
        n = int(n)
        if n not in F and dur[n] > 0.05: cpu_ratio.append((c / dur[n], n))
    if k not in ("code", "multi") or not C: continue
    cheap = sorted([n for n in C if dur[n] < EXP], key=lambda n: dur[n])
    exp = [n for n in C if dur[n] >= EXP]
    Fc = F & set(cheap); Fe = F & set(exp)
    stats["code_with_cand"] += 1
    if F: stats["live"] += 1
    if Fe and not Fc: stats["exp_only_fail"] += 1; print("  EXP-ONLY", r["commit"], [tests[n]["id"].split("::")[-1] for n in Fe], "cheap cand", len(cheap))
    if Fe and Fc: stats["exp_and_cheap"] += 1
    for K in (1, 3, 5, 10):
        if F and Fc and not (Fc & set(cheap[:K])): stats[f"probe{K}_misses_live"] += 1
    if not cheap and exp: stats["only_exp_cands"] += 1
print(kinds, "live:", live)
print(dict(stats))
cpu_ratio.sort()
import statistics
print("cpu/dur ratio median", round(statistics.median(r for r, _ in cpu_ratio), 2), "p90", round(cpu_ratio[int(len(cpu_ratio)*.9)][0], 2), "max", [(round(r,1), tests[n]["id"].split("::")[-1]) for r, n in cpu_ratio[-5:]])
