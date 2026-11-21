"""Lab: how much focused probing does it take to catch a live mutation?"""
import json, glob, sys, os, collections
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, 'rwx'); sys.argv = sys.argv[:1]
import simulate as S
sel = S.sel; M = S._M
rows = []
for L in ['RWXblast-radius-sample/sample', '/home/user/lab/mut1', '/home/user/lab/mut2']:
    tests = json.load(open(f'{L}/tests.json'))['tests']; dur = {t['number']: int(t['seconds']*1000+.5) for t in tests}
    sel.DUR.update(dur); sel.FILE_OF.update({t['number']: t['file'] for t in tests}); sel.CLS_OF.update({t['number']: t['id'].rsplit('::', 1)[0] for t in tests})
    sel.NO_RUN.update(t['number'] for t in tests if t['id'] in sel.NO_RUN_IDS)
    sel.CPU_EST.update({n: S.CALIB.get(n, dur[n]) for n in dur})
    an = sel.Analyzer(M, sum(1 << n for n in dur))
    truth = {c: set(v['failing']) for c, v in json.load(open(f'{L}/answers.json'))['commits'].items()} if os.path.exists(f'{L}/answers.json') else {json.loads(l)['commit']: set(json.loads(l)['fail']) for f in glob.glob(f'{L}/truth.*.jsonl') for l in open(f)}
    for cid, F in truth.items():
        mask, _ = an.analyze(open(f'{L}/commits/{cid}.patch').read()); C = [n for n in dur if mask >> n & 1]
        if not C or (an.data_changes and not an.py_changes): continue
        heavy = {n for n in C if n in sel.NO_RUN or sel.CPU_EST[n] + sel.CLS_SETUP.get(sel.CLS_OF[n], 0) > sel.RUN_MAX_MS}
        rest = [n for n in C if n not in heavy]
        if not rest or sel.batch_cost(rest) <= sel.RUN_LIMIT_MS: continue
        focus = sel.commit_focus(M, C, an.py_changes)
        tot = {n: M.total_lines.get(n, 1) for n in rest}
        # rarity of each line in the changed code objects: tests running rare lines nearby
        rar = collections.Counter()
        keys = set()
        from patchlib import Locator, changed_lines
        import ast as _ast
        for rel, (old, new) in an.py_changes.items():
            loc = Locator(_ast.parse(old))
            fp_lines = set()
            # recover changed old lines by diffing old/new line lists
            ol, nl = old.split("\n"), new.split("\n")
            import difflib
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ol, nl, autojunk=False).get_opcodes():
                if tag != "equal":
                    fp_lines.update(range(i1 + 1, max(i2, i1 + 1) + 1))
            for ln in fp_lines:
                st = loc.locate(min(ln, len(ol)))
                if st is not None:
                    keys.add((rel, st.code_key))
        restset = set(rest)
        for rel, key in keys:
            for ln, mid in M.code(rel, key).items():
                m = M.masks[mid]
                cnt = bin(m).count("1")
                w = 1.0 / cnt
                for n in restset:
                    if m >> n & 1:
                        rar[n] += w
        orders = {
            'rare/cost': sorted(rest, key=lambda n: (-rar.get(n, 0) / (sel.batch_cost([n]) + 20), n)),
            'rare': sorted(rest, key=lambda n: (-rar.get(n, 0), sel.batch_cost([n]), n)),
            'focus': sorted(rest, key=sel._probe_order(focus)),
            'cheap': sorted(rest, key=lambda n: (sel.batch_cost([n]), n)),
            'focus/cost': sorted(rest, key=lambda n: (-focus.get(n, 0) / (sel.batch_cost([n]) + 20), n)),
            'small-cover': sorted(rest, key=lambda n: (tot[n] * (sel.batch_cost([n]) + 20), n)),
        }
        first = {}
        for k, o in orders.items():
            c, seen = 0, []
            first[k] = None
            for n in o:
                seen.append(n)
                c = sel.batch_cost(seen)
                if n in F:
                    first[k] = c; break
        rows.append((len(C), bool(F & set(rest)), first, sel.batch_cost(rest)))
live = [r for r in rows if r[1]]
print(f"probe-path commits {len(rows)}, live among runnable {len(live)}")
for k in ['focus/cost', 'rare', 'rare/cost']:
    out = []
    for B in (400, 1000, 2000, 4000, 8000, 16000):
        caught = sum(1 for r in live if r[2][k] is not None and r[2][k] <= B)
        out.append(f"{B/1000:g}s:{100*caught/len(live):.0f}%")
    print(f"{k:12} " + "  ".join(out))
