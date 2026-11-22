"""Merge and/or value profiles (tools/lab/boolrun.py) into the coverage map (lab only).

usage: merge_bool.py MAP_IN BOOL_DIR tests.json MAP_OUT

Adds, keeping every existing key:
    bool_sites: {file: {(line, col): (op, mask_id)}}  every instrumented site; mask = tests
                that saw an evaluation where flipping and/or could change the result
    bool_unprofiled: mask_id   tests without a trusted profile (never narrowed away)
"""

import glob
import json
import os
import pickle
import sys
import zlib

SUBPROCESS_TESTS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)


def main():
    map_in, bool_dir, tests_json, map_out = sys.argv[1:5]
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
    sites = json.load(open(os.path.join(bool_dir, "sites.json")))
    acc = [0] * len(sites)
    profiled, statuses = 0, {}
    for path in glob.glob(os.path.join(bool_dir, "[0-9]*.json")):
        r = json.load(open(path))
        statuses[r["number"]] = r["status"]
        if r["status"] != "ok":
            continue
        bit = 1 << r["number"]
        profiled |= bit
        for sid in r["differs"]:
            acc[sid] |= bit
    allm = 0
    for t in tests:
        allm |= 1 << t["number"]
        if t["id"] in SUBPROCESS_TESTS:
            profiled &= ~(1 << t["number"])
    out: dict = {}
    for sid, (rel, line, col, op, _ctx) in enumerate(sites):
        out.setdefault(rel, {})[(line, col)] = (op, mid(acc[sid]))
    d["bool_sites"] = out
    d["bool_unprofiled"] = mid(allm & ~profiled)
    d["masks"] = masks
    blob = zlib.compress(pickle.dumps(d, protocol=5), 9)
    open(map_out, "wb").write(blob)
    bad = {n: s for n, s in statuses.items() if s != "ok"}
    print(f"profiles {len(statuses)}/{len(tests)}, not ok {len(bad)} {sorted(bad.items())[:10]}, "
          f"unprofiled {(allm & ~profiled).bit_count()}, sites {len(sites)}, with differing tests "
          f"{sum(1 for m in acc if m)}, bytes {len(blob)}", file=sys.stderr)


if __name__ == "__main__":
    main()
