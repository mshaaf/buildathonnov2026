"""Lab: generate sample-style synthetic patches against the pinned sqlglot tree.

    python3 tools/lab/mutate.py SQLGLOT_ROOT OUT_DIR COUNT SEED

Writes OUT_DIR/commits/<id>.patch and OUT_DIR/meta.json (kind, file, line per id).
Used only to validate and tune the selector; nothing here ships.
"""

import ast
import difflib
import io
import json
import os
import random
import sys
import tokenize

SWAP_OPS = {"==": "!=", "!=": "==", "<": "<=", "<=": "<", ">": ">=", ">=": ">", "+": "-", "-": "+"}
SWAP_NAMES = {"and": "or", "or": "and", "True": "False", "False": "True"}
COMMENTS = ["# Cheap check first", "# Keep in sync with the generator", "# Kept for backwards compatibility",
            "# NOTE: order matters here", "# TODO: revisit", "# See the dialect docs"]


def py_files(root):
    out = []
    for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def code_mutation(src, rng):
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    cands = []
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in SWAP_OPS:
            cands.append((i, "op"))
        elif t.type == tokenize.NAME and t.string in SWAP_NAMES:
            cands.append((i, "name"))
        elif t.type == tokenize.NAME and t.string == "is":
            cands.append((i, "is"))
        elif t.type == tokenize.NAME and t.string == "not":
            cands.append((i, "not"))
        elif t.type == tokenize.NUMBER and t.string.isdigit():
            cands.append((i, "num"))
    if not cands:
        return None
    lines = src.split("\n")
    for _ in range(30):
        i, kind = rng.choice(cands)
        t = toks[i]
        (r, c0), (_, c1) = t.start, t.end
        line = lines[r - 1]
        if kind == "op":
            new = SWAP_OPS[t.string]
        elif kind == "name":
            new = SWAP_NAMES[t.string]
        elif kind == "is":
            nxt = toks[i + 1].string if i + 1 < len(toks) else ""
            if nxt == "not":
                c1 = toks[i + 1].end[1]
                new = "!="
            else:
                new = "=="
        elif kind == "not":
            if i > 0 and toks[i - 1].string == "is":
                continue
            nxt = toks[i + 1].string if i + 1 < len(toks) else ""
            if nxt == "in":
                c1 = toks[i + 1].end[1]
                new = "in"
            else:
                c1 = c1 + 1 if line[c1:c1 + 1] == " " else c1
                new = ""
        else:
            v = int(t.string)
            new = str(v + 1 if rng.random() < 0.7 or v == 0 else v - 1)
        nl = line[:c0] + new + line[c1:]
        if nl == line:
            continue
        lines2 = lines[:]
        lines2[r - 1] = nl
        out = "\n".join(lines2)
        try:
            if ast.dump(ast.parse(out)) == ast.dump(ast.parse(src)):
                continue
        except SyntaxError:
            continue
        return out, r
    return None


def comment_mutation(src, rng):
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"', "'", ")", "]", "}", "@"))
           and l.rstrip().endswith((":", ")")) is False]
    for _ in range(30):
        i = rng.choice(idx)
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        lines2 = lines[:i] + [ind + rng.choice(COMMENTS)] + lines[i:]
        out = "\n".join(lines2)
        try:
            ast.parse(out)
        except SyntaxError:
            continue
        return out, i + 1
    return None


def fixture_mutation(root, rng):
    d = os.path.join(root, "tests/fixtures/optimizer")
    files = sorted(f for f in os.listdir(d) if f.endswith(".sql"))
    for _ in range(30):
        f = rng.choice(files)
        rel = f"tests/fixtures/optimizer/{f}"
        src = open(os.path.join(root, rel)).read()
        lines = src.split("\n")
        idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and not l.startswith("--")]
        if not idx:
            continue
        i = rng.choice(idx)
        l = lines[i]
        words = [(k, w) for k, w in enumerate(l.split(" ")) if w]
        k, w = rng.choice(words)
        parts = l.split(" ")
        choice = rng.random()
        if any(ch.isdigit() for ch in w) and choice < 0.5:
            parts[k] = "".join(str((int(ch) + 1) % 10) if ch.isdigit() else ch for ch in w)
        elif choice < 0.75:
            parts.insert(k, "x")
        else:
            parts[k] = w + "1"
        lines[i] = " ".join(parts)
        return rel, src, "\n".join(lines), i + 1
    return None


def make_diff(rel, old, new):
    d = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    body = "\n".join(d)
    return f"diff --git a/{rel} b/{rel}\n{body}\n"


def main():
    root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    rng = random.Random(seed)
    files = py_files(root)
    weights = [sum(1 for l in open(os.path.join(root, f)) if l.strip()) for f in files]
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    meta = {}

    def one(kind):
        if kind == "fixture":
            r = fixture_mutation(root, rng)
            if r is None:
                return None
            rel, old, new, ln = r
            return [(rel, old, new, ln)]
        f = rng.choices(files, weights)[0]
        old = open(os.path.join(root, f)).read()
        r = code_mutation(old, rng) if kind == "code" else comment_mutation(old, rng)
        if r is None:
            return None
        return [(f, old, r[0], r[1])]

    n = 0
    while n < count:
        x = rng.random()
        kind = "code" if x < 0.55 else "fixture" if x < 0.8 else "comment" if x < 0.9 else "multi"
        if kind == "multi":
            parts = []
            for k in rng.sample(["code", "comment", "fixture", "code"], rng.choice([2, 3])):
                p = one(k)
                if p and all(p[0][0] != q[0] for q in parts):
                    parts += p
        else:
            parts = one(kind)
        if not parts:
            continue
        cid = f"m{seed:02d}{n:04d}"
        with open(os.path.join(out, "commits", cid + ".patch"), "w") as fh:
            for rel, old, new, ln in sorted(parts):
                fh.write(make_diff(rel, old, new))
        meta[cid] = {"kind": kind, "parts": [(rel, ln) for rel, _, _, ln in parts]}
        n += 1
    json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=0)


if __name__ == "__main__":
    main()
