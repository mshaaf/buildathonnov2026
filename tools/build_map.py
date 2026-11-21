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


def build_refs(src_root):
    """Static index of where names and Class.attr pairs are referenced, per code object."""
    import ast

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rwx"))
    from patchlib import _code_key

    name_refs: dict = {}
    attr_refs: dict = {}
    for d, _, fs in os.walk(src_root):
        rel_d = os.path.relpath(d, src_root)
        if not (rel_d == "sqlglot" or rel_d.startswith("sqlglot/") or rel_d == "tests" or rel_d.startswith("tests/")):
            continue
        for f in fs:
            if not f.endswith(".py"):
                continue
            rel = os.path.normpath(os.path.join(rel_d, f))
            try:
                tree = ast.parse(open(os.path.join(d, f)).read())
            except SyntaxError:
                continue

            def visit(node, scope_key, scope_kind):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.GeneratorExp)):
                        # decorators/defaults/bases belong to the enclosing scope
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            for sub in child.decorator_list:
                                visit_expr(sub, scope_key, scope_kind)
                        if isinstance(child, ast.ClassDef):
                            for sub in child.bases + [k.value for k in child.keywords]:
                                visit_expr(sub, scope_key, scope_kind)
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                            for sub in child.args.defaults + [x for x in child.args.kw_defaults if x]:
                                visit_expr(sub, scope_key, scope_kind)
                        kind = "class" if isinstance(child, ast.ClassDef) else "func"
                        visit(child, _code_key(child), kind)
                    else:
                        record(child, scope_key, scope_kind)
                        visit(child, scope_key, scope_kind)

            def visit_expr(e, scope_key, scope_kind):
                record(e, scope_key, scope_kind)
                visit(e, scope_key, scope_kind)

            def record(n, scope_key, scope_kind):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    name_refs.setdefault(n.id, set()).add((rel, scope_key, scope_kind))
                elif isinstance(n, ast.Attribute):
                    v = n.value
                    recv = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
                    if recv:
                        attr_refs.setdefault((recv, n.attr), set()).add((rel, scope_key, scope_kind))
                    if isinstance(n.ctx, ast.Load):
                        # module.NAME style access counts as a reference to NAME too
                        name_refs.setdefault(n.attr, set()).add((rel, scope_key, scope_kind))
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier() and len(n.value) > 2:
                    # getattr(obj, "NAME") / hasattr / setattr style access
                    name_refs.setdefault(n.value, set()).add((rel, scope_key, scope_kind))

            visit(tree, "1:<module>", "module")
    return {k: sorted(v) for k, v in name_refs.items()}, {k: sorted(v) for k, v in attr_refs.items()}


def main():
    cov_dir, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
    cls_dir = sys.argv[4] if len(sys.argv) > 4 else None
    src_root = sys.argv[5] if len(sys.argv) > 5 else None
    tests = json.load(open(tests_json))["tests"]
    acc_lines: dict = {}
    acc_opens: dict = {}
    focus: dict = {}  # file -> {test: lines executed in that file}
    total_lines: dict = {}
    nonpass = {}
    seen = set()
    for path in glob.glob(os.path.join(cov_dir, "*.json")):
        d = json.load(open(path))
        n = d["number"]
        seen.add(n)
        bit = 1 << n
        if d["status"] != "ok":
            nonpass[n] = d["status"]
        for f, keys in d["run"].items():
            c = sum(len(v) for v in keys.values())
            focus.setdefault(f[len(ROOT):], {})[n] = c
            total_lines[n] = total_lines.get(n, 0) + c
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
    payload = {"masks": masks, "lines": lines, "opens": opens, "nonpass": nonpass,
               "focus": focus, "total_lines": total_lines}
    if cls_dir:
        users: dict = {}
        for path in glob.glob(os.path.join(cls_dir, "[0-9]*.json")):
            d = json.load(open(path))
            bit = 1 << d["number"]
            for k in d["classes"]:
                users[k] = users.get(k, 0) | bit
        hier = json.load(open(os.path.join(cls_dir, "hier.json")))
        # descendants: class key -> keys of every class having it in its MRO
        desc: dict = {}
        for k, mro in hier.items():
            for a in mro:
                desc.setdefault(a, []).append(k)
        payload["class_users"] = {k: mid(m) for k, m in users.items()}
        payload["class_desc"] = desc
        print(f"class users {len(users)}, hierarchy {len(hier)}", file=sys.stderr)
    calib = sorted(glob.glob(os.path.join(os.path.dirname(cov_dir.rstrip("/")), "calib2.*.json")))
    if calib:
        body, setup = {}, {}
        for f in calib:
            d = json.load(open(f))
            body.update({int(k): int(v * 1000) + 1 for k, v in d["body"].items()})
            setup.update({k: int(v * 1000) for k, v in d["setup"].items()})
        payload["cpu_ms"] = body
        payload["cls_setup_ms"] = setup
        print(f"cpu calibration: {len(body)} tests, {len(setup)} classes; biggest setups "
              f"{sorted(((v, k) for k, v in setup.items()), reverse=True)[:3]}", file=sys.stderr)
    if src_root:
        payload["name_refs"], payload["attr_refs"] = build_refs(src_root)
        print(f"name refs {len(payload['name_refs'])}, attr refs {len(payload['attr_refs'])}", file=sys.stderr)
    blob = zlib.compress(pickle.dumps(payload, protocol=5), 9)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(blob)
    nl = sum(len(lk) for fk in lines.values() for lk in fk.values())
    print(f"files {len(lines)}, line entries {nl}, distinct masks {len(masks)}, opens {len(opens)}, bytes {len(blob)}", file=sys.stderr)


if __name__ == "__main__":
    main()
