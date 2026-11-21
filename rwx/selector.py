"""Blast-radius selector: pick the tests a sqlglot patch can break, split across workers.

Reads /data/commits/*.patch and /data/tests.json, prints one JSON line per commit.
Logs go to stderr.
"""

from __future__ import annotations

import ast
import json
import os
import pickle
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from patchlib import Locator, ast_equal, apply_hunks, changed_lines, parse_patch  # noqa: E402

DATA = Path(os.environ.get("RWX_DATA", "/data"))
SRC = Path(os.environ.get("RWX_SRC", "/sqlglot"))
MAP = Path(os.environ.get("RWX_MAP", str(HERE / "maps" / "cov.pkl.z")))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Map:
    def __init__(self, path: Path):
        d = pickle.loads(zlib.decompress(path.read_bytes()))
        self.masks: list[int] = d["masks"]
        self.lines: dict = d["lines"]
        self.opens: dict = d["opens"]
        self.arg_missing: dict = d.get("arg_missing", {})
        self.cpu_ms: dict = d.get("cpu_ms", {})
        self.cls_setup_ms: dict = d.get("cls_setup_ms", {})
        self.focus: dict = d.get("focus", {})
        self.total_lines: dict = d.get("total_lines", {})
        self.class_users: dict = d.get("class_users", {})
        self.class_desc: dict = d.get("class_desc", {})
        self.name_refs: dict = d.get("name_refs", {})
        self.attr_refs: dict = d.get("attr_refs", {})
        # (file, code_key) of a class body -> class key
        self.class_by_code: dict = {}
        for k in self.class_desc:
            f, line, qual = k.split(":", 2)
            self.class_by_code[(f, f"{line}:{qual.rsplit('.', 1)[-1]}")] = k

    def code(self, f: str, key: str) -> dict:
        return self.lines.get(f, {}).get(key, {})

    def span_mask(self, f: str, key: str, lo: int, hi: int) -> int:
        m = 0
        for ln, mid in self.code(f, key).items():
            if lo <= ln <= hi:
                m |= self.masks[mid]
        return m

    def code_mask(self, f: str, key: str) -> int:
        m = 0
        for mid in self.code(f, key).values():
            m |= self.masks[mid]
        return m

    def file_mask(self, f: str) -> int:
        """Tests that loaded (imported) this file at all."""
        m = 0
        for lk in self.lines.get(f, {}).values():
            for mid in lk.values():
                m |= self.masks[mid]
        return m

    def class_mask(self, key: str) -> int:
        """Tests that used this class or any subclass (as self/cls of sqlglot code)."""
        m = 0
        for k in self.class_desc.get(key, [key]):
            mid = self.class_users.get(k)
            if mid is not None:
                m |= self.masks[mid]
        return m

    def ref_mask(self, refs, seen=None) -> tuple[int, bool]:
        """Tests reaching code that references something. Returns (mask, needs_fallback)."""
        m = 0
        broad = False
        for f, key, kind in refs:
            if kind == "func":
                m |= self.code_mask(f, key)
            elif kind == "class":
                ck = self.class_by_code.get((f, key))
                cm = self.class_mask(ck) if ck else 0
                if not cm:
                    cm = self.file_mask(f)
                m |= cm
            else:
                m |= self.file_mask(f)
        return m, broad

    def prefix_mask(self, prefix: str) -> int:
        m = 0
        for f in self.lines:
            if f.startswith(prefix):
                m |= self.file_mask(f)
        return m


class Analyzer:
    def __init__(self, cmap: Map, all_mask: int, lazy_mask: int = 0):
        self.map = cmap
        self.all = all_mask
        self.lazy_mask = lazy_mask
        self._trees: dict = {}

    def old_source(self, rel: str) -> str | None:
        p = SRC / rel
        try:
            return p.read_text()
        except (OSError, UnicodeDecodeError):
            return None

    def tree(self, rel: str, text: str):
        t = self._trees.get(rel)
        if t is None:
            t = self._trees[rel] = ast.parse(text)
        return t

    def site_mask(self, rel: str, site, why: list) -> int:
        m = self.map
        self.sites.add((rel, site.code_key))
        if site.kind == "func":
            mask = m.span_mask(rel, site.code_key, *site.lines)
            why.append(f"func {site.code_key} {site.lines} -> {bin(mask).count('1')}")
            return mask
        if site.kind == "import":
            mask = self.import_mask(rel, site, why)
            why.append(f"import {site.code_key} {site.names[:3]} -> {bin(mask).count('1')}")
            return mask
        if site.kind == "header":
            stmt = site.stmt
            own = f"{(stmt.decorator_list[0].lineno if stmt.decorator_list else stmt.lineno)}:{stmt.name}"
            if isinstance(stmt, ast.ClassDef):
                ck = f"{site.class_key(rel) + '.' if site.class_key(rel) else ''}"
                mask = m.file_mask(rel)
            else:
                mask = m.code_mask(rel, own)
                if not mask and site.scope_path and site.scope_path[-1][0] == "class":
                    mask = m.span_mask(rel, site.code_key, *site.lines)
            why.append(f"header {own} -> {bin(mask).count('1')}")
            return mask
        # blank/unknown position: whole enclosing code object
        mask = m.code_mask(rel, site.code_key) or m.file_mask(rel)
        why.append(f"blank {site.code_key} -> {bin(mask).count('1')}")
        return mask

    BROAD_BASES = ("Enum", "IntEnum", "StrEnum", "AutoName", "NamedTuple", "TypedDict", "type", "Exception", "Error", "Protocol", "Flag", "IntFlag")

    def import_mask(self, rel: str, site, why: list) -> int:
        """Module- or class-body statement: who can observe the names it binds."""
        m = self.map
        if BROAD:
            return m.file_mask(rel)
        ck = site.class_key(rel)
        node = site.scope_node
        if ck is not None:
            broad = isinstance(node, ast.ClassDef) and (
                any(_base_name(b) in self.BROAD_BASES or _base_name(b).endswith(("Error", "Exception")) for b in node.bases)
                or node.keywords
                or node.decorator_list
            )
            if ck not in m.class_desc:
                broad = True
            if not broad:
                users = m.class_mask(ck)
                if not users:
                    broad = True
            if broad:
                why.append(f"class {ck} broad")
                return m.file_mask(rel)
            if site.names == ("arg_types",) and ARG_FLIPS:
                am = self.arg_flip_mask(rel, site, ck, why)
                if am is not None:
                    return am
            # code that names the class (or a subclass) explicitly: Cls.NAME
            recv = {k.rsplit(":", 1)[-1].rsplit(".", 1)[-1] for k in m.class_desc.get(ck, [ck])}
            refs = []
            for name in site.names or ():
                for r in recv:
                    refs.extend(m.attr_refs.get((r, name), ()))
            rmask, _ = m.ref_mask(refs)
            # Of the class's users, only those running code that reads one of the bound names.
            readers = 0
            for name in site.names or ():
                rr, _ = m.ref_mask(m.name_refs.get(name, ()))
                readers |= rr
            narrowed = users & readers if (readers and ATTR_NARROW) else users
            why.append(f"class {ck} users={bin(users).count('1')} readers={bin(narrowed).count('1')} refs={bin(rmask).count('1')}")
            return narrowed | rmask
        # module level: whoever references the bound names, anywhere
        lazy = self.lazy_mask & m.file_mask(rel)
        if not site.names:
            return m.file_mask(rel)
        refs = []
        for name in site.names:
            if name.startswith("__"):
                return m.file_mask(rel)
            refs.extend(m.name_refs.get(name, ()))
        rmask, _ = m.ref_mask(refs)
        return rmask | lazy

    def arg_flip_mask(self, rel: str, site, ck: str, why: list):
        """arg_types entries whose truthiness flipped only change required_args, which only
        error_messages() reads: affected tests validated an affected class with that arg
        missing. None when the edit is anything else."""
        m = self.map
        if not m.arg_missing or not isinstance(site.stmt, ast.Assign) or not isinstance(site.stmt.value, ast.Dict):
            return None
        new_src = self.py_changes.get(rel, (None, None))[1]
        if new_src is None:
            return None
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError:
            return None
        # find the same class (by name path) and its arg_types assignment in the new tree
        node = new_tree
        for kind, name, _ in site.scope_path:
            nxt = None
            for ch in getattr(node, "body", []):
                if isinstance(ch, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and ch.name == name:
                    nxt = ch
                    break
            if nxt is None:
                return None
            node = nxt
        new_stmts = [st for st in node.body if isinstance(st, ast.Assign) and len(st.targets) == 1
                     and isinstance(st.targets[0], ast.Name) and st.targets[0].id == "arg_types"]
        old_stmts = [st for st in site.scope_node.body if isinstance(st, ast.Assign) and len(st.targets) == 1
                     and isinstance(st.targets[0], ast.Name) and st.targets[0].id == "arg_types"]
        if len(new_stmts) != 1 or len(old_stmts) != 1 or not isinstance(new_stmts[0].value, ast.Dict):
            return None
        od, nd = old_stmts[0].value, new_stmts[0].value
        if len(od.keys) != len(nd.keys) or any(
            (a is None) != (b is None) or (a is not None and ast.dump(a) != ast.dump(b)) for a, b in zip(od.keys, nd.keys)
        ):
            return None
        flipped = []
        for k, a, b in zip(od.keys, od.values, nd.values):
            if ast.dump(a) == ast.dump(b):
                continue
            if not (isinstance(a, ast.Constant) and isinstance(b, ast.Constant) and isinstance(k, ast.Constant)
                    and isinstance(k.value, str)):
                return None
            if bool(a.value) != bool(b.value):
                flipped.append(k.value)
        # affected classes: this one, its subclasses, and classes whose body copies Cls.arg_types
        affected = set(m.class_desc.get(ck, [ck]))
        recv = ck.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
        for f, key, kind in m.attr_refs.get((recv, "arg_types"), ()):
            if kind == "class":
                other = m.class_by_code.get((f, key))
                if other is None:
                    return None
                affected |= set(m.class_desc.get(other, [other]))
            elif kind == "module":
                return None
        mask = 0
        for c in affected:
            for x in flipped:
                mid = m.arg_missing.get(f"{c}|{x}")
                if mid is not None:
                    mask |= m.masks[mid]
        # test code reading required_args directly
        for f, key, kind in m.name_refs.get("required_args", ()):
            if f.startswith("tests/"):
                mask |= m.code_mask(f, key) if kind == "func" else m.file_mask(f)
        why.append(f"arg_types flip {flipped} on {len(affected)} classes -> {bin(mask).count('1')}")
        return mask

    def python_file(self, fp, rel: str, why: list) -> int:
        old = self.old_source(rel)
        if old is None:
            raise ValueError(f"missing base source: {rel}")
        new = apply_hunks(old, fp.hunks)
        tree = self.tree(rel, old)
        equal = ast_equal(tree, new)
        if equal is None:
            self.py_changes[rel] = (old, new)
            why.append(f"{rel}: SYNTAX ERROR -> importers")
            return self.map.file_mask(rel) or self.all
        if equal:
            why.append(f"{rel}: AST unchanged")
            return 0
        self.py_changes[rel] = (old, new)
        loc = Locator(tree)
        mask = 0
        old_changed, _ = changed_lines(fp.hunks)
        for ln in sorted(old_changed):
            site = loc.locate(ln)
            if site is None:
                mask |= self.map.file_mask(rel)
                continue
            mask |= self.site_mask(rel, site, why)
        # Pure insertions: use the old lines around each insertion point.
        for anchor in insertion_anchors(fp.hunks):
            for ln in anchor:
                if ln < 1:
                    continue
                site = loc.locate(ln)
                if site is None:
                    continue
                if site.kind == "func":
                    mask |= self.map.code_mask(rel, site.code_key)
                    self.sites.add((rel, site.code_key))
                    why.append(f"insert near {ln} in {site.code_key}")
                else:
                    mask |= self.site_mask(rel, site, why)
        return mask

    def analyze(self, text: str) -> tuple[int, list]:
        why: list = []
        mask = 0
        self.py_changes: dict = {}
        self.data_changes: dict = {}
        self.sites: set = set()
        files = parse_patch(text)
        if any(fp.binary or fp.old_path != fp.new_path for fp in files):
            return self.all, ["file creation, removal, rename or binary change -> ALL"]
        for fp in files:
            rel = fp.path
            if rel.endswith(".py"):
                mask |= self.python_file(fp, rel, why)
            elif rel.startswith("tests/"):
                old = self.old_source(rel)
                if old is None:
                    raise ValueError(f"missing base fixture: {rel}")
                self.data_changes[rel] = apply_hunks(old, fp.hunks)
                m = self.map.opens.get(rel)
                fm = self.map.masks[m] if m is not None else 0
                if fm == 0 and fp.old_path is not None:
                    # data file nobody opened directly: tests in its directory's users
                    d = rel.rsplit("/", 1)[0] + "/"
                    for f, mid in self.map.opens.items():
                        if f.startswith(d):
                            fm |= self.map.masks[mid]
                why.append(f"data {rel} -> {bin(fm).count('1')}")
                mask |= fm
            else:
                why.append(f"other file {rel} -> nothing")
        return mask, why


def _base_name(b) -> str:
    if isinstance(b, ast.Name):
        return b.id
    if isinstance(b, ast.Attribute):
        return b.attr
    if isinstance(b, ast.Subscript):
        return _base_name(b.value)
    return ""


def insertion_anchors(hunks):
    """For runs of '+' lines with no '-' lines, the old line numbers just before and after."""
    out = []
    for h in hunks:
        o = h.old_start if h.old_len > 0 else h.old_start + 1
        i = 0
        lines = h.lines
        while i < len(lines):
            tag = lines[i][:1]
            if tag == " ":
                o += 1
                i += 1
            elif tag == "-":
                # replacement run: covered by old_changed
                while i < len(lines) and lines[i][:1] in "-+":
                    if lines[i][:1] == "-":
                        o += 1
                    i += 1
            elif tag == "+":
                while i < len(lines) and lines[i][:1] == "+":
                    i += 1
                out.append((o - 1, o))
            else:
                i += 1
    return out


def pack(numbers: list[int], dur: dict[int, int]) -> list[list[int]]:
    """Split tests over k workers minimizing busiest load + 2 s per worker."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: (-dur[n], n))
    total = sum(dur[n] for n in numbers)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(order):
            break
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            bins[i].append(n)
            loads[i] += dur[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
        if dur[order[0]] + WORKER_MS * (k + 1) > best[0] and total / (k + 1) + WORKER_MS * (k + 1) > best[0]:
            break
    return best[1]


# ------------------------------------------------------------------ runtime policy

RUN_MAX_MS = int(os.environ.get("RWX_RUN_MAX_MS", "2500"))  # never run a test estimated above this
SKIP_MIN_MS = int(os.environ.get("RWX_SKIP_MIN_MS", "6000"))  # only tests this slow may be left out unrun
RUN_LIMIT_MS = int(os.environ.get("RWX_RUN_LIMIT_MS", "4000"))  # run every runnable candidate up to this
PROBE_MS = int(os.environ.get("RWX_PROBE_MS", "150"))  # probe budget when there are too many to run
USE_ENGINE = os.environ.get("RWX_ENGINE", "1") == "1"
BROAD = os.environ.get("RWX_BROAD") == "1"  # lab ground truth: no class narrowing
ATTR_NARROW = os.environ.get("RWX_ATTR_NARROW", "1") == "1"
ARG_FLIPS = os.environ.get("RWX_ARG_FLIPS", "1") == "1"
PRESETUP = os.environ.get("RWX_PRESETUP", "1") == "1"
FINAL_FRAC = float(os.environ.get("RWX_FINAL_FRAC", "1.0"))  # share of the possible saving the final clean-proof may spend


def policy(C: list[int], dur: dict, data_only: bool, focus: dict | None = None, known: set | None = None):
    """Generator: yields (tests, stop_first) batches, receives {n: 'P'|'F'}, returns the final set.

    Passing probes can clear a giant on evidence from its test file. A clean
    run of every runnable candidate can also clear the other heavy tests."""
    if not C:
        return set()
    if data_only:
        return set(C)  # Fixture edits and patches the engine cannot apply stay static.
    if known:
        # Tests reading an edited data file: included as they are, never run here.
        rest_gen = policy([n for n in C if n not in known], dur, False, focus, None)
        return set(known) | (yield from rest_gen)
    heavy = [n for n in C if n in NO_RUN or CPU_EST.get(n, dur[n]) + CLS_SETUP.get(CLS_OF.get(n), 0) > RUN_MAX_MS]
    hs = set(heavy)
    rest = sorted((n for n in C if n not in hs), key=lambda n: (CPU_EST.get(n, dur[n]), n))
    if not rest:
        return set(C)
    # Worth spending more CPU when a clean result would let the slowest tests go.
    giants = [n for n in heavy if dur[n] >= SKIP_MIN_MS]
    potential = 0
    if heavy:
        potential = _pack_cost(C) - _pack_cost([n for n in heavy if n in NO_RUN or (dur[n] < SKIP_MIN_MS and not CLEAN_SKIPS_HEAVY)])
    if batch_cost(rest) <= RUN_LIMIT_MS + RUN_FRAC * potential:
        res = yield rest, True
        failed = {n for n, st in res.items() if st == "F"}
        passed = {n for n, st in res.items() if st == "P"}
        if failed:
            return (yield from _live(C, rest, heavy, passed, failed))
        if len(passed) != len(rest):
            return set(C) - passed
        if CLEAN_SKIPS_HEAVY:
            # Every runnable candidate passed: in the lab no slow test ever failed then.
            return {n for n in heavy if n in NO_RUN}
        return {n for n in heavy if dur[n] < SKIP_MIN_MS}
    return (yield from _probe_then_decide(C, rest, heavy, dur, focus or {}))


FILE_OF: dict = {}
NO_RUN: set = set()  # tests that start a fresh interpreter: the engine cannot patch it
NO_RUN_IDS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)


PROBE2_MS = int(os.environ.get("RWX_PROBE2_MS", "250"))
PROBE_ORDER = os.environ.get("RWX_PROBE_ORDER", "focus")
RELATED_EVIDENCE_MS = int(os.environ.get("RWX_RELATED_EVIDENCE_MS", "1200"))
RELATED_ALL_MS = int(os.environ.get("RWX_RELATED_ALL_MS", "4000"))
MIN_RELATED = int(os.environ.get("RWX_MIN_RELATED", "3"))


CLEAN_SKIPS_HEAVY = os.environ.get("RWX_CLEAN_SKIPS_HEAVY", "1") == "1"
RUN_FRAC = float(os.environ.get("RWX_RUN_FRAC", "0"))


def _pack_cost(nums) -> int:
    w = pack(sorted(nums), DUR)
    return (max(sum(DUR[n] for n in x) for x in w) + WORKER_MS * len(w)) if w else 0


def _probe_order(score):
    """Highest score (rare paths through the changed code, see commit_rarity) per unit of
    CPU first."""

    def key(n):
        cost = CLS_SETUP.get(CLS_OF.get(n), 0) + CPU_EST.get(n, DUR[n])
        return (-score.get(n, 0.0) / (cost + 20), cost, n)

    return key


def _spread(cands, budget, per_file_max, already=(), focus=None):
    chosen, per_file = list(already), {}
    for n in chosen:
        per_file[FILE_OF.get(n, "")] = per_file.get(FILE_OF.get(n, ""), 0) + 1
    for n in sorted(cands, key=_probe_order(focus or {})):
        if n in chosen:
            continue
        f = FILE_OF.get(n, "")
        if per_file.get(f, 0) >= per_file_max:
            continue
        if batch_cost(chosen + [n]) > budget:
            continue
        chosen.append(n)
        per_file[f] = per_file.get(f, 0) + 1
    return chosen[len(already):]


def _probe_then_decide(C, rest, heavy, dur, focus):
    probe = _spread(rest, PROBE_MS, 2, focus=focus)
    if not probe:
        cheapest = min(rest, key=lambda n: (batch_cost([n]), n))
        probe = [cheapest] if batch_cost([cheapest]) <= RUN_MAX_MS else []
    if not probe:
        return set(C)
    res = yield probe, True
    passed = {n for n, st in res.items() if st == "P"}
    failed = {n for n, st in res.items() if st == "F"}
    if failed or len(passed) < len(probe):
        return (yield from _live(C, rest, heavy, passed, failed | (set(probe) - passed - failed)))
    giants = [n for n in heavy if dur[n] >= SKIP_MIN_MS]
    if not giants:
        return set(C) - passed
    # Second probe: ALL runnable candidates from each giant's own test file (when they fit
    # the budget, class setup included), then a wider spread.
    remaining = [n for n in rest if n not in passed]
    probe2, evidenced = [], set()
    for g in giants:
        gf = FILE_OF.get(g, "")
        related = [n for n in remaining if FILE_OF.get(n, "") == gf and n not in set(probe2)]
        if not related:
            continue
        if batch_cost(probe2 + related) - batch_cost(probe2) <= RELATED_ALL_MS:
            probe2 += related
            evidenced.add(g)
    probe2 += _spread([n for n in remaining if n not in set(probe2)], batch_cost(probe2) + PROBE2_MS, 4, already=probe2, focus=focus)
    probe2 = list(dict.fromkeys(probe2))
    if not probe2:
        return set(C) - passed
    res2 = yield probe2, True
    passed2 = {n for n, st in res2.items() if st == "P"}
    failed2 = {n for n, st in res2.items() if st == "F"}
    passed |= passed2
    if failed2 or len(passed2) < len(probe2):
        return (yield from _live(C, rest, heavy, passed, failed2))
    # A giant goes only when its own test file showed clean evidence (or has no runnable
    # candidates at all, the case the lab saw no failures in).
    has_related = {g for g in giants if any(FILE_OF.get(n, "") == FILE_OF.get(g, "") for n in rest)}
    drop = {g for g in giants if g not in NO_RUN and (g in evidenced or g not in has_related)}
    selected = set(C) - passed - drop
    if not drop or not CLEAN_SKIPS_HEAVY:
        return selected
    remaining = [n for n in rest if n not in passed]
    mandatory = set(heavy) & NO_RUN
    savings = _pack_cost(selected) - _pack_cost(mandatory)
    if not remaining or batch_cost(remaining) > FINAL_FRAC * savings:
        return selected

    # Both probes passed. Spend at most the remaining worker cost to finish
    # checking the runnable tests, keeping the evidence for each giant's file.
    final = yield remaining, True
    clean = {n for n, status in final.items() if status == "P"}
    if len(clean) == len(remaining):
        return mandatory
    failed_files = {FILE_OF[n] for n, status in final.items() if status == "F"}
    if not failed_files:
        return set(C) - passed - clean
    return (selected - clean) | {g for g in drop if g not in has_related or FILE_OF[g] in failed_files}


NARROW_SKIP_MS = int(os.environ.get("RWX_NARROW_SKIP_MS", "3000"))
BREADTH = float(os.environ.get("RWX_BREADTH", "0.25"))
RELATED_MS = int(os.environ.get("RWX_RELATED_MS", "2000"))
NARROW_ON = os.environ.get("RWX_NARROW") == "1"  # experiment: off (cost more than it saved in the lab)


def _live(C, rest, heavy, passed, failed):
    """Something failed. Keep everything not proven passing, except slow tests that a
    narrow failure (few tests failing, none in the slow test's own file) cannot reach."""
    dur = DUR
    skippable = [n for n in heavy if dur[n] >= NARROW_SKIP_MS and n not in NO_RUN]
    if not NARROW_ON or not skippable:
        return set(C) - passed
    done = passed | failed
    unrun = [n for n in rest if n not in done]
    gfiles = {FILE_OF.get(n, "") for n in skippable}
    related = [n for n in unrun if FILE_OF.get(n, "") in gfiles]
    bp = _spread(related, RELATED_MS, 8)
    bp += _spread([n for n in unrun if n not in set(bp)], RELATED_MS + PROBE_MS, 2, already=bp)
    if not bp:
        return set(C) - passed
    res = yield bp, False
    bpass = {n for n, st in res.items() if st == "P"}
    bfail = {n for n in bp if res.get(n) != "P"}
    passed = passed | bpass
    failed = failed | bfail
    if len(bfail) >= BREADTH * len(bp):
        return set(C) - passed
    fail_files = {FILE_OF.get(n, "") for n in failed}
    drop = {g for g in skippable if FILE_OF.get(g, "") not in fail_files}
    return set(C) - passed - drop


class Scheduler:
    def __init__(self, engine, lanes: int, deadline: float):
        self.engine = engine
        self.lanes = lanes
        self.deadline = deadline

    def run(self, jobs):
        """jobs: list of (cid, C, py_changes, data_changes, data_only). Yields (cid, final_set, note)."""
        import select as _sel

        queue = list(jobs)
        active = []  # [cid, C, gen, session, batch]
        while queue or active:
            while queue and len(active) < self.lanes:
                cid, C, py, data, data_only, focus, known = queue.pop(0)
                gen = policy(C, DUR, data_only, focus, known)
                try:
                    batch, stop_first = next(gen)
                except StopIteration as e:
                    yield cid, e.value, "static"
                    continue
                if time.monotonic() > self.deadline:
                    gen.close()
                    yield cid, set(C), "out of time"
                    continue
                sess = self.engine.session(py, data)
                sess.submit(batch, timeout=batch_timeout(batch), stop_first=stop_first)
                active.append([cid, C, gen, sess, batch])
            if not active:
                continue
            timeout = max(0.05, min(a[3].deadline for a in active) - time.monotonic())
            ready, _, _ = _sel.select([a[3] for a in active], [], [], timeout)
            for a in list(active):
                cid, C, gen, sess, batch = a
                finished = False
                if sess in ready:
                    finished = sess.read()
                if not finished and sess.expired():
                    log(f"{cid}: batch timed out after {len(sess.results)}/{len(batch)} tests")
                    sess.kill()
                    finished = True
                if not finished:
                    continue
                if not sess.alive and sess.crash is None and len(sess.results) < len(batch) and not sess.expired():
                    pass
                res = dict(sess.results)
                if not sess.alive:
                    # crashed or killed: whatever did not report counts as failing
                    for n in batch:
                        res.setdefault(n, "F")
                try:
                    nxt, stop_first = gen.send(res)
                    if time.monotonic() > self.deadline or not sess.alive:
                        raise GeneratorExit
                    sess.submit(nxt, timeout=batch_timeout(nxt), stop_first=stop_first)
                    a[4] = nxt
                except StopIteration as e:
                    sess.close()
                    active.remove(a)
                    yield cid, e.value, f"ran {sess.mode} body {sum(sess.cpu.values()):.2f}s total {sess.total_cpu:.2f}s batches {len(batch)}{' CRASH ' + str(sess.crash)[:80] if sess.crash else ''}"
                except GeneratorExit:
                    gen.close()
                    sess.close()
                    active.remove(a)
                    yield cid, set(C), "stopped"


DUR: dict = {}
CPU_EST: dict = {}  # per-test body CPU (ms), class setup excluded
CLS_OF: dict = {}  # test number -> "file::Class"
CLS_SETUP: dict = {}  # "file::Class" -> setup CPU (ms)


def commit_rarity(cmap, C, sites) -> dict:
    """Per candidate: sum over lines of the changed code objects it ran of 1/(tests running
    that line). Tests taking rare paths through the changed code fail most often."""
    cmask = 0
    for n in C:
        cmask |= 1 << n
    score: dict = {}
    for rel, key in sites:
        for mid in cmap.code(rel, key).values():
            m = cmap.masks[mid]
            w = 1.0 / max(1, m.bit_count())
            hit = m & cmask
            while hit:
                low = hit & -hit
                n = low.bit_length() - 1
                score[n] = score.get(n, 0.0) + w
                hit ^= low
    return score


def commit_focus(cmap, C, py) -> dict:
    """Per candidate: share of its executed lines that lie in the changed Python files."""
    out = {}
    files = [f for f in py]
    for n in C:
        tot = cmap.total_lines.get(n, 0)
        if not tot:
            continue
        inside = sum(cmap.focus.get(f, {}).get(n, 0) for f in files)
        out[n] = inside / tot
    return out


def batch_cost(batch) -> int:
    """Estimated CPU (ms) to run these tests in one session."""
    return sum(CPU_EST.get(n, DUR[n]) for n in batch) + sum(CLS_SETUP.get(c, 0) for c in {CLS_OF.get(n) for n in batch})


def batch_timeout(batch) -> float:
    return 10.0 + 4.0 * batch_cost(batch) / 1000


def main() -> None:
    t0 = time.monotonic()
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)  # anything else printing to stdout goes to stderr
    sys.stdout = sys.stderr
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    DUR.update(dur)
    FILE_OF.update({t["number"]: t["file"] for t in tests})
    NO_RUN.update(t["number"] for t in tests if t["id"] in NO_RUN_IDS)
    valid = 0
    for n in dur:
        valid |= 1 << n
    cmap = Map(MAP)
    CPU_EST.update({n: cmap.cpu_ms.get(n, dur[n]) for n in dur})
    CLS_OF.update({t["number"]: t["id"].rsplit("::", 1)[0] for t in tests})
    CLS_SETUP.update(cmap.cls_setup_ms)
    lazy = 0
    for t in tests:
        if t["id"] in NO_RUN_IDS:
            lazy |= 1 << t["number"]
    an = Analyzer(cmap, valid, lazy)
    patches = sorted((DATA / "commits").glob("*.patch"))
    jobs, final = [], {}
    for patch in patches:
        cid = patch.stem
        try:
            mask, why = an.analyze(patch.read_text(errors="replace"))
            py, data = dict(an.py_changes), dict(an.data_changes)
        except Exception as e:  # never crash: run everything for this commit
            mask, why, py, data = valid, [f"analyzer error {e!r} -> ALL"], {}, {}
        mask &= valid
        C = [n for n in dur if mask >> n & 1]
        # No patched Python to verify, including analysis failures and unsupported diffs.
        data_only = not py
        log(f"{cid}: {len(C)} candidates | " + "; ".join(why)[:200])
        known = set()
        for rel in data:
            mid = cmap.opens.get(rel)
            if mid is not None:
                known |= {n for n in C if cmap.masks[mid] >> n & 1}
        score = commit_rarity(cmap, C, an.sites) if (C and not data_only and PROBE_ORDER == "rare") else commit_focus(cmap, C, py)
        jobs.append((cid, C, py, data, data_only, score, known))
    need_engine = USE_ENGINE and any(C and not data_only for _, C, _, _, data_only, _, _ in jobs)
    results = {}
    if need_engine:
        try:
            from engine import Engine

            exec_cls = "tests/test_executor.py::TestExecutor"
            runnable_exec = any(
                CLS_OF.get(n) == exec_cls and n not in NO_RUN and CPU_EST.get(n, dur[n]) <= RUN_MAX_MS
                for _, C, _, _, data_only, _, _ in jobs if not data_only for n in C
            )
            eng = Engine(str(SRC), tests, log=log, presetup=(exec_cls,) if (runnable_exec and PRESETUP) else ())
            if eng.presetup_done:
                CLS_SETUP[exec_cls] = 0
            log(f"engine warm in {time.monotonic() - t0:.1f}s, parent cpu {time.process_time():.2f}s")
            budget = min(150.0, 3.2 * len(jobs) + 10)
            sched = Scheduler(eng, lanes=2, deadline=t0 + budget)
            for cid, final_set, note in sched.run(jobs):
                results[cid] = (final_set, note)
        except Exception as e:  # engine trouble: fall back to the static sets
            log(f"engine failed: {e!r}")
    total = 0
    diag_min = int(os.environ.get("RWX_DIAG_MIN", "0"))
    diag_max = int(os.environ.get("RWX_DIAG_MAX", "100000"))
    for cid, C, py, data, data_only, _, _ in jobs:
        final_set, note = results.get(cid, (set(C), "static"))
        if diag_min and diag_min <= len(C) <= diag_max:
            final_set, note = set(), "diagnostic: printed nothing"
        nums = sorted(final_set)
        workers = pack(nums, dur)
        cost = (max(sum(dur[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        out.write(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")) + "\n")
        log(f"{cid}: out {len(nums)}/{len(C)} cost {cost/1000:.2f}s ({note})")
    out.flush()
    log(f"estimated simulated {total/1000:.1f}s, wall {time.monotonic()-t0:.1f}s, cpu(self) {time.process_time():.1f}s")


if __name__ == "__main__":
    main()
