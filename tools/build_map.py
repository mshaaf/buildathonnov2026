"""Build the committed coverage map from per-test coverage JSON (lab tool).

    python3 tools/build_map.py OUT_COV_DIR tests.json rwx/maps/cov.pkl.z

Map layout (pickled, zlib-compressed):
    masks: list[int]                      interned test bitmasks (bit n = test number n)
    lines: {file: {code_key: {line: mask_id}}}
    opens: {file: mask_id}                non-.py files under the repo each test opened
    nonpass: {number: status}             tests that did not pass on the pinned commit
"""

import glob
import json
import os
import pickle
import sys
import zlib

ROOT = "/sqlglot/"


def main():
    cov_dir, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
    tests = json.load(open(tests_json))["tests"]
    acc_lines: dict = {}
    acc_opens: dict = {}
    nonpass = {}
    seen = set()
    for path in glob.glob(os.path.join(cov_dir, "*.json")):
        d = json.load(open(path))
        n = d["number"]
        seen.add(n)
        bit = 1 << n
        if d["status"] != "ok":
            nonpass[n] = d["status"]
        for phase in ("imp", "run"):
            for f, keys in d[phase].items():
                rel = f[len(ROOT):]
                fk = acc_lines.setdefault(rel, {})
                for k, lines in keys.items():
                    lk = fk.setdefault(k, {})
                    for ln in lines:
                        lk[ln] = lk.get(ln, 0) | bit
        for f in d["opens"]:
            rel = f[len(ROOT):]
            if rel.endswith((".py", ".pyc")) or "__pycache__" in rel:
                continue
            acc_opens[rel] = acc_opens.get(rel, 0) | bit
    missing = [t["number"] for t in tests if t["number"] not in seen]
    print(f"tests with coverage: {len(seen)}, missing: {missing[:20]}", file=sys.stderr)
    print(f"non-passing on base: {nonpass}", file=sys.stderr)

    intern: dict = {}
    masks: list = []

    def mid(m):
        i = intern.get(m)
        if i is None:
            i = intern[m] = len(masks)
            masks.append(m)
        return i

    lines = {f: {k: {ln: mid(m) for ln, m in lk.items()} for k, lk in fk.items()} for f, fk in acc_lines.items()}
    opens = {f: mid(m) for f, m in acc_opens.items()}
    blob = zlib.compress(pickle.dumps({"masks": masks, "lines": lines, "opens": opens, "nonpass": nonpass}, protocol=5), 9)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(blob)
    nl = sum(len(lk) for fk in lines.values() for lk in fk.values())
    print(f"files {len(lines)}, line entries {nl}, distinct masks {len(masks)}, opens {len(opens)}, bytes {len(blob)}", file=sys.stderr)


if __name__ == "__main__":
    main()
