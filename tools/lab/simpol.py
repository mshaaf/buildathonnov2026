import json, glob, sys, importlib.util
sys.path.insert(0, "rwx")
spec = importlib.util.spec_from_file_location("sel", "rwx/selector.py"); sel = importlib.util.module_from_spec(spec); spec.loader.exec_module(sel)
L = sys.argv[1]
tests = json.load(open(f"{L}/tests.json"))["tests"]; d = {t["number"]: int(t["seconds"] * 1000 + .5) for t in tests}
meta = json.load(open(f"{L}/meta.json")) if glob.glob(f"{L}/meta.json") else {}
recs = [json.loads(l) for f in glob.glob(f"{L}/truth.*.jsonl") for l in open(f)]
def pack(nums):
    w = sel.pack(sorted(nums), d); return (max(sum(d[n] for n in x) for x in w) + 2000 * len(w)) if w else 0
def run_policy(r, limit, exp_ms, gamble=True):
    C = r["cand"]; F = set(r["fail"]); cpu = {int(k): int(v * 1000) for k, v in r["cpu"].items()}
    datafix = all(not p.endswith(".py") for p, _ in meta.get(r["commit"], {}).get("parts", [])) if meta else False
    if not C: return 0, 0, 0
    if datafix: return pack(C), 0, 0
    cheap = sorted([n for n in C if d[n] < exp_ms], key=lambda n: d[n]); exp = [n for n in C if d[n] >= exp_ms]
    if sum(d[n] for n in cheap) > limit: return pack(C), 0, 0
    used = 0; failed = []; unrun = []
    for i, n in enumerate(cheap):
        used += cpu.get(n, d[n]) + 5
        if n in F:
            failed.append(n); unrun = cheap[i + 1:]; break
    if failed:
        out = set(failed) | set(unrun) | set(exp)
        miss = 0
    else:
        out = set() if gamble else set(exp)
        miss = 1 if (F & set(exp)) and gamble else 0
    return pack(out) + used + 300000 * miss, used, miss
oracle = sum(pack(r["fail"]) for r in recs); static = sum(pack(r["cand"]) for r in recs)
print(f"n={len(recs)} oracle={oracle/1000:.1f} static={static/1000:.1f}")
for exp_ms in (1000, 3000):
    for limit in (1000, 3000, 10000, 30000):
        tot = cpu = miss = 0
        for r in recs:
            c, u, m = run_policy(r, limit, exp_ms); tot += c; cpu += u; miss += m
        print(f"exp>={exp_ms/1000}s limit={limit/1000}s total={tot/1000:.1f} (cpu {cpu/1000:.1f}, misses {miss})")
