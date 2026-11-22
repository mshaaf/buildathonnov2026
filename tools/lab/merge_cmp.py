"""Merge comparison-site value profiles (tools/lab/cmprun.py) into the coverage map (lab only).

usage: merge_cmp.py MAP_IN CMP_DIR tests.json MAP_OUT

Adds two keys and keeps every existing key:
    cmp_sites: {file: {(line, col): (op, {vector: mask_id})}}   single-operator comparisons
    cmp_unprofiled: mask_id    tests without a trustworthy profile (timeout, crash, not
                               passing under instrumentation, missing, or running sqlglot in
                               a subprocess); never narrowed away
"""

import glob
import json
import os
import pickle
import sys
import zlib

SUBPROCESS_TESTS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)


def main():
    map_in, cmp_dir, tests_json, map_out = sys.argv[1:5]
    d = pickle.loads(zlib.decompress(open(map_in, "rb").read()))
    masks = d["masks"]
    intern = {m: i for i, m in enumerate(masks)}

    def mid(m):
        i = intern.get(m)
        if i is None:
            i = intern[m] = len(masks)
            masks.append(m)
        return i

    tests = json.load(open(tests_json))["tests"]
    sites = json.load(open(os.path.join(cmp_dir, "sites.json")))
    acc: dict = {}  # sid -> {vec: mask}
    profiled = 0
    statuses: dict = {}
    for path in glob.glob(os.path.join(cmp_dir, "[0-9]*.json")):
        r = json.load(open(path))
        statuses[r["number"]] = r["status"]
        if r["status"] != "ok":
            continue
        bit = 1 << r["number"]
        profiled |= bit
        for sid, vec in r["seen"]:
            v = acc.setdefault(sid, {})
            v[vec] = v.get(vec, 0) | bit
    for t in tests:
        if t["id"] in SUBPROCESS_TESTS:
            profiled &= ~(1 << t["number"])
    allm = 0
    for t in tests:
        allm |= 1 << t["number"]
    unprofiled = allm & ~profiled
    out: dict = {}
    for sid, vecs in acc.items():
        rel, line, col, op = sites[sid]
        out.setdefault(rel, {})[(line, col)] = (op, {v: mid(m) for v, m in vecs.items()})
    d["cmp_sites"] = out
    d["cmp_unprofiled"] = mid(unprofiled)
    d["masks"] = masks
    blob = zlib.compress(pickle.dumps(d, protocol=5), 9)
    open(map_out, "wb").write(blob)
    bad = {n: s for n, s in statuses.items() if s != "ok"}
    print(f"profiles {len(statuses)}/{len(tests)}, not ok {len(bad)} {sorted(bad.items())[:10]}, "
          f"unprofiled {unprofiled.bit_count()}, sites evaluated {len(acc)}/{len(sites)}, bytes {len(blob)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
