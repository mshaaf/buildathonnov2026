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
        self.cpu_ms: dict = d.get("cpu_ms", {})
        self.cls_setup_ms: dict = d.get("cls_setup_ms", {})
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
            # code that names the class (or a subclass) explicitly: Cls.NAME
            recv = {k.rsplit(":", 1)[-1].rsplit(".", 1)[-1] for k in m.class_desc.get(ck, [ck])}
            refs = []
            for name in site.names or ():
                for r in recv:
                    refs.extend(m.attr_refs.get((r, name), ()))
            rmask, _ = m.ref_mask(refs)
            why.append(f"class {ck} users={bin(users).count('1')} refs={bin(rmask).count('1')}")
            return users | rmask
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

    def python_file(self, fp, rel: str, why: list) -> int:
        old = self.old_source(rel)
        if old is None:
            # New module: tests that walk/import its package could see it.
            pkg = rel.rsplit("/", 1)[0] + "/"
            why.append(f"new file {rel}")
            return self.map.prefix_mask(pkg + "__init__.py")
        if fp.new_path is None:
            why.append(f"deleted {rel}")
            return self.map.file_mask(rel) or self.all
        new = apply_hunks(old, fp.hunks)
        eq = ast_equal(old, new)
        if eq is not True:
            self.py_changes[rel] = (old, new)
        if eq is True:
            why.append(f"{rel}: AST unchanged")
            return 0
        if eq is None:
            why.append(f"{rel}: SYNTAX ERROR -> importers")
            return self.map.file_mask(rel) or self.all
        tree = self.tree(rel, old)
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
                    why.append(f"insert near {ln} in {site.code_key}")
                else:
                    mask |= self.site_mask(rel, site, why)
        return mask

    def analyze(self, text: str) -> tuple[int, list]:
        why: list = []
        mask = 0
        self.py_changes: dict = {}
        self.data_changes: dict = {}
        for fp in parse_patch(text):
            rel = fp.path
            if fp.binary:
                why.append(f"binary {rel} -> ALL")
                mask |= self.all
                continue
            if rel.endswith(".py"):
                mask |= self.python_file(fp, rel, why)
            elif rel.startswith("tests/"):
                old = self.old_source(rel)
                if old is not None and fp.new_path is not None:
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
PROBE_MS = int(os.environ.get("RWX_PROBE_MS", "600"))  # probe budget when there are too many to run
USE_ENGINE = os.environ.get("RWX_ENGINE", "1") == "1"


def policy(C: list[int], dur: dict, data_only: bool):
    """Generator: yields (tests, stop_first) batches, receives {n: 'P'|'F'}, returns the final set.

    Sound for every test it runs or includes. The one gamble: when every runnable
    candidate ran and passed, the very slow unrunnable ones (>= SKIP_MIN_MS recorded)
    are left out as well."""
    if not C:
        return set()
    if data_only:
        return set(C)  # an edited fixture line almost always breaks the tests reading it
    heavy = [n for n in C if n in NO_RUN or CPU_EST.get(n, dur[n]) + CLS_SETUP.get(CLS_OF.get(n), 0) > RUN_MAX_MS]
    hs = set(heavy)
    rest = sorted((n for n in C if n not in hs), key=lambda n: (CPU_EST.get(n, dur[n]), n))
    if not rest:
        return set(C)
    if batch_cost(rest) <= RUN_LIMIT_MS:
        res = yield rest, True
        failed = {n for n, st in res.items() if st == "F"}
        unrun = {n for n in rest if n not in res}
        if failed:
            return failed | unrun | hs
        return {n for n in heavy if dur[n] < SKIP_MIN_MS}
    return (yield from _probe_then_decide(C, rest, heavy, dur))


FILE_OF: dict = {}
NO_RUN: set = set()  # tests that start a fresh interpreter: the engine cannot patch it
NO_RUN_IDS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)


PROBE2_MS = int(os.environ.get("RWX_PROBE2_MS", "2500"))


def _spread(cands, budget, per_file_max, already=()):
    chosen, per_file = list(already), {}
    for n in chosen:
        per_file[FILE_OF.get(n, "")] = per_file.get(FILE_OF.get(n, ""), 0) + 1
    for n in sorted(cands, key=lambda n: (CLS_SETUP.get(CLS_OF.get(n), 0) + CPU_EST.get(n, DUR[n]), n)):
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


def _probe_then_decide(C, rest, heavy, dur):
    probe = _spread(rest, PROBE_MS, 2)
    if not probe:
        return set(C)
    res = yield probe, True
    passed = {n for n, st in res.items() if st == "P"}
    if len(passed) < len(probe):
        return set(C) - passed  # live: keep everything not proven passing
    giants = [n for n in heavy if dur[n] >= SKIP_MIN_MS]
    if not giants:
        return set(C) - passed
    # Second probe: runnable candidates from the giants' own test files, then a wider spread.
    gfiles = {FILE_OF.get(n, "") for n in giants}
    remaining = [n for n in rest if n not in passed]
    related = [n for n in remaining if FILE_OF.get(n, "") in gfiles]
    probe2 = _spread(related, PROBE2_MS * 0.6, 6)
    probe2 += _spread([n for n in remaining if n not in set(probe2)], PROBE2_MS, 4, already=probe2)
    if not probe2:
        return set(C) - passed
    res2 = yield probe2, True
    passed2 = {n for n, st in res2.items() if st == "P"}
    passed |= passed2
    if len(passed2) < len(probe2):
        return set(C) - passed
    return set(C) - passed - set(giants)


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
                cid, C, py, data, data_only = queue.pop(0)
                gen = policy(C, DUR, data_only)
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
                    yield cid, e.value, f"ran {sess.mode} cpu {sum(sess.cpu.values()):.2f}s{' CRASH ' + str(sess.crash)[:80] if sess.crash else ''}"
                except GeneratorExit:
                    gen.close()
                    sess.close()
                    active.remove(a)
                    yield cid, set(C), "stopped"


DUR: dict = {}
CPU_EST: dict = {}  # per-test body CPU (ms), class setup excluded
CLS_OF: dict = {}  # test number -> "file::Class"
CLS_SETUP: dict = {}  # "file::Class" -> setup CPU (ms)


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
        data_only = bool(data) and not py
        log(f"{cid}: {len(C)} candidates | " + "; ".join(why)[:200])
        jobs.append((cid, C, py, data, data_only))
    need_engine = USE_ENGINE and any(C and not data_only for _, C, _, _, data_only in jobs)
    results = {}
    if need_engine:
        try:
            from engine import Engine

            eng = Engine(str(SRC), tests, log=log)
            log(f"engine warm in {time.monotonic() - t0:.1f}s")
            budget = min(150.0, 3.2 * len(jobs) + 10)
            sched = Scheduler(eng, lanes=2, deadline=t0 + budget)
            for cid, final_set, note in sched.run(jobs):
                results[cid] = (final_set, note)
        except Exception as e:  # engine trouble: fall back to the static sets
            log(f"engine failed: {e!r}")
    total = 0
    for cid, C, py, data, data_only in jobs:
        final_set, note = results.get(cid, (set(C), "static"))
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
