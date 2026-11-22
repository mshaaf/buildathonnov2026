"""Lab: measured miss rate of evidence rules for leaving a slow test out unrun.

    RWX_SRC=$LAB/sqlglot python tools/lab/droprules.py DIR [DIR...]

For every commit with Python changes and every slow candidate g (recorded >= 3 s,
not runnable under the policy's RUN_MAX_MS), using the analyzer's candidates C and the
truth failures F:
  clean      no runnable candidate fails                    (the CLEAN_SKIPS_HEAVY rule)
  file       some runnable candidate fails, but every runnable candidate in g's own test
             file exists, was affordable (RELATED_ALL_MS) and passes
  file_cls   as file, restricted to g's own test class
  narrow     file, and at most 3% of runnable candidates fail
  norel      some runnable candidate fails and g's file has no runnable candidate
Prints, per slow test and rule: times the rule fires / of which g fails (= a miss).
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simeval as E  # noqa: E402

sel = E.sel


def main():
    stats = {}
    examples = []
    for L in sys.argv[1:]:
        tests = json.load(open(f"{L}/tests.json"))["tests"]
        names = {t["number"]: t["id"].rsplit("::", 1)[-1] for t in tests}
        dur = E.setup_tests(tests)
        an = sel.Analyzer(E.M, sum(1 << n for n in dur), sum(1 << n for n in sel.NO_RUN))
        for cid, (F, _, unreliable, tcand, skipped) in sorted(E.load_truth(L).items()):
            mask, _ = an.analyze(open(f"{L}/commits/{cid}.patch").read())
            if not an.py_changes:
                continue
            C = [n for n in dur if mask >> n & 1]
            if not E.covered(C, tcand):
                continue
            heavy = {n for n in C if sel.is_heavy(n)}
            rest = [n for n in C if n not in heavy]
            rf = [n for n in rest if n in F]
            for g in heavy:
                if dur[g] < E.SLOW or g in sel.NO_RUN or g in skipped:  # skipped: outcome unknown
                    continue
                gf, gc = sel.FILE_OF[g], sel.CLS_OF[g]
                rel = [n for n in rest if sel.FILE_OF[n] == gf]
                relc = [n for n in rel if sel.CLS_OF[n] == gc]
                afford = rel and sel.batch_cost(rel) <= sel.RELATED_ALL_MS
                fired = {}
                fired["clean"] = not rf and bool(rest)
                fired["file"] = bool(rf) and bool(afford) and not any(n in F for n in rel)
                fired["file_cls"] = bool(rf) and bool(relc) and sel.batch_cost(relc) <= sel.RELATED_ALL_MS \
                    and not any(n in F for n in relc)
                fired["narrow"] = fired["file"] and len(rf) <= 0.03 * len(rest)
                fired["norel"] = bool(rf) and not rel
                cheap = sorted(rel, key=lambda n: (sel.batch_cost([n]), n))
                for k in (10, 5, 3):
                    fired[f"file_{k}"] = bool(rf) and len(rel) >= k and not any(n in F for n in cheap[:k])
                for rule, on in fired.items():
                    if on:
                        s = stats.setdefault((names[g], rule), [0, 0, 0])
                        s[0] += 1
                        s[1] += g in F
                        s[2] += unreliable
                        if g in F:
                            examples.append((names[g], rule, f"{os.path.basename(L)}/{cid}", len(C), len(rf), len(rel)))
    print(f"{'slow test':28} {'rule':9} {'fires':>5} {'fails':>5} {'unrel':>5}")
    for (name, rule), (n, f, u) in sorted(stats.items()):
        print(f"{name:28} {rule:9} {n:5} {f:5} {u:5}")
    for e in examples:
        print("miss example:", e)


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
