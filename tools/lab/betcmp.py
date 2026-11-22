"""Lab: compare a bet's simeval DUMP with the baseline DUMP.

    python tools/lab/betcmp.py BASE.dump BET.dump [RATIO,...]
(i) commits whose printed set changed, (ii) simulated / est CPU saved, (iii) misses added,
(iv) break-even miss probability = saving / 300 s (overall and per firing), worst commit.
"""
import json
import sys

base = {r["cid"]: r for r in map(json.loads, open(sys.argv[1]))}
bet = {r["cid"]: r for r in map(json.loads, open(sys.argv[2]))}
ratios = [float(x) for x in (sys.argv[3] if len(sys.argv) > 3 else "0.45,0.7").split(",")]
fired = [c for c in bet if c in base and (bet[c]["outs"] != base[c]["outs"] or bet[c]["cpu"] != base[c]["cpu"])]
dsim = sum(base[c]["sim"] - bet[c]["sim"] for c in fired) / 1000
dcpu = sum(base[c]["cpu"] - bet[c]["cpu"] for c in fired) / 1000
miss = sum(bet[c]["miss"] and not base[c]["miss"] for c in fired)
changed = [c for c in fired if bet[c]["outs"] != base[c]["outs"]]
print(f"printed set changed on {len(changed)} commits ({len(fired)} incl. CPU-only changes); simulated saved {dsim:.1f} s, est CPU saved {dcpu:.1f} s, misses added {miss}")
for r in ratios:
    save = dsim + r * dcpu
    per = save / len(changed) if changed else 0
    print(f"  ratio {r}: saving {save:.1f} s before misses -> break-even {save / 300:.2f} misses overall, "
          f"{per / 300 * 100:.1f}% miss chance per commit where the printed set changed")
worst = sorted(fired, key=lambda c: (bet[c]["sim"] + 300000 * bet[c]["miss"]) - base[c]["sim"], reverse=True)
for c in worst[:3]:
    print(f"  worst: {c} sim {base[c]['sim'] / 1000:.1f} -> {bet[c]['sim'] / 1000:.1f}{' MISS' if bet[c]['miss'] else ''}")
