"""Check comparison-profile narrowing against truth (lab only).

usage: RWX_SRC=... cmpcheck.py DATA_DIR [DATA_DIR...]

For every commit whose whole patch flips one comparison operator, prints candidates
without and with the value-profile narrowing, the truth failing set size, and whether
truth-failing tests are a subset of the narrowed candidates. Truth: answers.json
("failing") or truth*.jsonl ("fail"; records whose candidates do not cover the current
analyzer's un-narrowed set are reported as such).
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "rwx"))
import selector as sel  # noqa: E402


def truth_of(d):
    out = {}
    if os.path.exists(os.path.join(d, "answers.json")):
        for c, v in json.load(open(os.path.join(d, "answers.json")))["commits"].items():
            out[c] = (set(v["failing"]), None)
    for p in sorted(glob.glob(os.path.join(d, "truth*.jsonl"))):
        for line in open(p):
            r = json.loads(line)
            out[r["commit"]] = (set(r.get("fail", [])), set(r.get("cand", [])))
    return out


def main():
    cmap = sel.Map(sel.MAP)
    bad = 0
    for d in sys.argv[1:]:
        tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
        valid = 0
        for t in tests:
            valid |= 1 << t["number"]
        an = sel.Analyzer(cmap, valid)
        truth = truth_of(d)
        for p in sorted(glob.glob(os.path.join(d, "commits", "*.patch"))):
            cid = os.path.basename(p)[:-6]
            text = open(p, encoding="utf-8", errors="surrogateescape").read()
            sel.CMP_PROFILE = False
            try:
                full, _ = an.analyze(text)
            except Exception:  # noqa: BLE001
                continue
            if not an.cmp_flips:
                continue
            nflips, nfiles = len(an.cmp_flips), len({f[0] for f in an.cmp_flips})
            sel.CMP_PROFILE = True
            narrow, why = an.analyze(text)
            full &= valid
            narrow &= valid
            fail, cand = truth.get(cid, (None, None))
            covers = None if cand is None else all(full >> n & 1 == 0 or n in cand for n in range(len(tests)))
            ok = None if fail is None else all(narrow >> n & 1 for n in fail)
            if ok is False:
                bad += 1
            kind = "single" if text.count("diff --git") == 1 and nflips == 1 else f"multi({nflips} flips)"
            print(f"{os.path.basename(d.rstrip('/'))} {cid} {kind} {an.cmp_flip[3]}->{an.cmp_flip[4]} "
                  f"cands {full.bit_count()} -> {narrow.bit_count()} fail {('?' if fail is None else len(fail))} "
                  f"{'OK' if ok else 'VIOLATION' if ok is False else 'no-truth'}"
                  f"{'' if covers in (None, True) else ' (truth cand set smaller than analyzer full)'}")
    print(f"violations: {bad}")


if __name__ == "__main__":
    main()
