"""Lab: gap to oracle by edit kind, from a simeval DUMP.

    python tools/lab/gapkind.py DUMP DIR [DIR...]      (DIRs: the corpora the DUMP was made from)
"""
import io
import json
import os
import sys
import tokenize

BOUNDARY = {"<", "<=", ">", ">="}
IS = {"is", "is not", "==", "!="}


def toks(line):
    out = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline):
            if t.type in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT,
                          tokenize.COMMENT):
                continue
            out.append((t.type, t.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    # merge "is not" / "not in"
    merged = []
    for t in out:
        if merged and ((merged[-1][1], t[1]) in (("is", "not"), ("not", "in"))):
            merged[-1] = (tokenize.NAME, merged[-1][1] + " " + t[1])
        else:
            merged.append(t)
    return merged


def token_kind(old, new):
    a, b = toks(old), toks(new)
    if a is None or b is None:
        return "other"
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    da, db = a[i:len(a) - j], b[i:len(b) - j]
    sa, sb = [t[1] for t in da], [t[1] for t in db]
    if (sa, sb) in ((["and"], ["or"]), (["or"], ["and"])):
        return "and/or"
    if sorted([sa, sb], key=len) in ([[], ["not"]],) or (sa, sb) in ((["not in"], ["in"]), (["in"], ["not in"])):
        return "in/not in" if "in" in (sa + sb)[0] else "not"
    if len(sa) == 1 and len(sb) == 1:
        x, y = sa[0], sb[0]
        if x in BOUNDARY and y in BOUNDARY:
            return "cmp boundary"
        if {x, y} <= IS and ({x, y} & {"is", "is not"}):
            return "cmp is/=="
        if {x, y} == {"==", "!="}:
            return "cmp ==/!="
        if {x, y} <= {"True", "False", "None"}:
            return "True/False"
        if da[0][0] == tokenize.NUMBER and db[0][0] == tokenize.NUMBER:
            return "number"
        if {x, y} <= {"+", "-", "*", "/", "//", "%"}:
            return "+/-"
        if da[0][0] == tokenize.STRING or db[0][0] == tokenize.STRING:
            return "string"
    if any(t[0] == tokenize.NUMBER for t in da + db):
        return "number"
    return "other"


def classify(patch, why, meta_kind):
    if meta_kind in ("fixture", "comment"):
        return meta_kind
    files = [l for l in patch.splitlines() if l.startswith("+++ ")]
    if len(files) > 1 or meta_kind == "multi":
        return "multi"
    if not files or not files[0].endswith(".py"):
        return "fixture"
    rem = [l[1:] for l in patch.splitlines() if l.startswith("-") and not l.startswith("---")]
    add = [l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
    if "AST unchanged" in why and "func" not in why and "class" not in why:
        return "comment"
    if len(rem) != 1 or len(add) != 1:
        return "multi-line"
    old, new = rem[0], add[0]
    if "lambda" in old:
        return "lambda body"
    s = old.strip()
    if s.startswith(("def ", "async def ")) or ("=" in s and s.endswith(("):", ",", ")")) and "def " in s):
        return "default arg"
    if why.startswith(("class ", "import ")) or " import " in why[:40]:
        return "class/module attr"
    return token_kind(old, new)


def main():
    rows = [json.loads(l) for l in open(sys.argv[1])]
    dirs = {os.path.basename(d.rstrip("/")): d for d in sys.argv[2:]}
    metas = {k: (json.load(open(f"{d}/meta.json")) if os.path.exists(f"{d}/meta.json") else {}) for k, d in dirs.items()}
    agg = {}
    for r in rows:
        dname, cid = r["cid"].split("/", 1)
        d = dirs[dname]
        patch = open(f"{d}/commits/{cid}.patch").read()
        k = classify(patch, r["why"], metas[dname].get(cid, {}).get("kind"))
        a = agg.setdefault(k, [0, 0, 0, 0])
        a[0] += 1
        a[1] += r["sim"]
        a[2] += r["oracle"]
        a[3] += r["cpu"]
    print(f"{'kind':18} {'n':>4} {'sim':>7} {'oracle':>7} {'gap':>7} {'cpu':>6}")
    tot = [0, 0, 0, 0]
    for k, (n, s, o, c) in sorted(agg.items(), key=lambda kv: -(kv[1][1] - kv[1][2])):
        print(f"{k:18} {n:4} {s / 1000:7.1f} {o / 1000:7.1f} {(s - o) / 1000:7.1f} {c / 1000:6.1f}")
        tot = [x + y for x, y in zip(tot, (n, s, o, c))]
    print(f"{'ALL':18} {tot[0]:4} {tot[1] / 1000:7.1f} {tot[2] / 1000:7.1f} {(tot[1] - tot[2]) / 1000:7.1f} {tot[3] / 1000:6.1f}")


if __name__ == "__main__":
    main()
