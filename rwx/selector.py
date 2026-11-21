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

    def prefix_mask(self, prefix: str) -> int:
        m = 0
        for f in self.lines:
            if f.startswith(prefix):
                m |= self.file_mask(f)
        return m


class Analyzer:
    def __init__(self, cmap: Map, all_mask: int):
        self.map = cmap
        self.all = all_mask
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
            mask = m.span_mask(rel, site.code_key, *site.lines)
            if not mask:
                mask = m.file_mask(rel)
            why.append(f"import {site.code_key} {site.names[:3]} -> {bin(mask).count('1')}")
            return mask
        if site.kind == "header":
            stmt = site.stmt
            own = f"{(stmt.decorator_list[0].lineno if stmt.decorator_list else stmt.lineno)}:{stmt.name}"
            mask = m.code_mask(rel, own) | m.span_mask(rel, site.code_key, *site.lines)
            if isinstance(stmt, ast.ClassDef):
                mask |= m.file_mask(rel)
            why.append(f"header {own} -> {bin(mask).count('1')}")
            return mask
        # blank/unknown position: whole enclosing code object
        mask = m.code_mask(rel, site.code_key) or m.file_mask(rel)
        why.append(f"blank {site.code_key} -> {bin(mask).count('1')}")
        return mask

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
        for fp in parse_patch(text):
            rel = fp.path
            if fp.binary:
                why.append(f"binary {rel} -> ALL")
                mask |= self.all
                continue
            if rel.endswith(".py"):
                mask |= self.python_file(fp, rel, why)
            elif rel.startswith("tests/"):
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


def main() -> None:
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    valid = 0
    for n in dur:
        valid |= 1 << n
    cmap = Map(MAP)
    an = Analyzer(cmap, valid)
    total_cost = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            mask, why = an.analyze(patch.read_text(errors="replace"))
        except Exception as e:  # never crash: run everything for this commit
            mask, why = valid, [f"analyzer error {e!r} -> ALL"]
        mask &= valid
        nums = [n for n in dur if mask >> n & 1]
        workers = pack(nums, dur)
        cost = (max(sum(dur[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total_cost += cost
        print(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        log(f"{cid}: {len(nums)} tests, {len(workers)} workers, {cost/1000:.2f}s | " + "; ".join(why)[:300])
    log(f"estimated simulated {total_cost/1000:.1f}s (without misses), cpu {time.process_time()-t0:.2f}s")


if __name__ == "__main__":
    main()
