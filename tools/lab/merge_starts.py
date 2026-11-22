"""Merge code-object entries (tools/lab/startrun.py) into the coverage map (lab only).

usage: merge_starts.py MAP_IN STARTS_DIR MAP_OUT [--check-only]

For every code object some test entered, the map gets the ordinary entry
lines[file]["<first>:<name>"][<first>] = mask of the tests that entered it (OR-ed into an
existing entry for that line). A code object's first line never produces a LINE event,
so this is exactly the line the map was missing; no analyzer special case is needed.

Validation printed first: for code objects the map already has, the set of tests that
entered them must equal the union of their recorded lines (a body line runs iff the
code object was entered, for any code object with a line after its first).
"""

import glob
import json
import os
import pickle
import sys
import zlib


def main():
    map_in, starts_dir, map_out = sys.argv[1], sys.argv[2], sys.argv[3]
    check_only = "--check-only" in sys.argv
    d = pickle.loads(zlib.decompress(open(map_in, "rb").read()))
    masks, lines = d["masks"], d["lines"]
    entered: dict = {}  # (file, key) -> mask
    n_tests = 0
    collected = 0
    for path in glob.glob(os.path.join(starts_dir, "[0-9]*.json")):
        r = json.load(open(path))
        n_tests += 1
        bit = 1 << r["number"]
        collected |= bit
        for f, keys in r["start"].items():
            for k in keys:
                entered[(f, k)] = entered.get((f, k), 0) | bit
    print(f"tests {n_tests}, entered code objects {len(entered)}", file=sys.stderr)

    # validation on code objects the map already records
    same = diff = 0
    only_start = only_lines = 0
    examples = []
    for (f, k), s in entered.items():
        lk = lines.get(f, {}).get(k)
        if not lk or k.endswith(":<module>"):
            continue
        u = 0
        for mid in lk.values():
            u |= masks[mid]
        u &= collected
        if u == s:
            same += 1
        else:
            diff += 1
            only_start += (s & ~u).bit_count()
            only_lines += (u & ~s).bit_count()
            if len(examples) < 10:
                examples.append((f, k, (s & ~u).bit_count(), (u & ~s).bit_count()))
    print(f"validation: {same} code objects agree, {diff} differ "
          f"(tests only in entries {only_start}, only in lines {only_lines})", file=sys.stderr)
    for e in examples:
        print("  differ:", e, file=sys.stderr)
    missed = 0
    for f, fk in lines.items():
        for k, lk in fk.items():
            if k.endswith(":<module>") or (f, k) in entered:
                continue
            u = 0
            for mid in lk.values():
                u |= masks[mid]
            if u & collected:
                missed += 1
    print(f"map code objects with recorded runs but no entry: {missed}", file=sys.stderr)
    absent = [(f, k) for (f, k) in entered if k not in lines.get(f, {})]
    print(f"code objects new to the map: {len(absent)} "
          f"(lambdas {sum(1 for _, k in absent if k.endswith('<lambda>'))})", file=sys.stderr)
    if check_only:
        return

    intern = {m: i for i, m in enumerate(masks)}

    def mid(m):
        i = intern.get(m)
        if i is None:
            i = intern[m] = len(masks)
            masks.append(m)
        return i

    added = 0
    for (f, k), s in entered.items():
        first = int(k.split(":", 1)[0])
        lk = lines.setdefault(f, {}).setdefault(k, {})
        old = masks[lk[first]] if first in lk else 0
        if old | s != old:
            lk[first] = mid(old | s)
            added += 1
    d["masks"], d["lines"] = masks, lines
    blob = zlib.compress(pickle.dumps(d, protocol=5), 9)
    with open(map_out, "wb") as fh:
        fh.write(blob)
    print(f"first-line entries added/extended {added}, masks {len(masks)}, bytes {len(blob)}", file=sys.stderr)


if __name__ == "__main__":
    main()
