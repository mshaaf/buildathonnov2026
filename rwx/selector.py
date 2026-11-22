"""Blast-radius selector: pick the tests a sqlglot patch can break, split across workers.

Reads /data/commits/*.patch and /data/tests.json, prints one JSON line per commit.
Logs go to stderr.
"""

from __future__ import annotations

import ast
import gc
import json
import os
import pickle
import re
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from patchlib import (  # noqa: E402
    Locator,
    PatchMismatch,
    _same_syntax as same_syntax,
    apply_hunks,
    bound_names,
    change_blocks,
    changed_lines,
    diff_hunks,
    has_future_annotations,
    known_lookups,
    locate_hunks,
    parse_patch,
    stmt_inert,
    stmt_safe,
)

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
    def __init__(self, cmap: Map, all_mask: int, lazy_mask: int = 0, file_tests: dict | None = None):
        self.map = cmap
        self.all = all_mask
        self.lazy_mask = lazy_mask
        self.file_tests = file_tests or {}  # test file -> mask of its tests
        self._trees: dict = {}
        profile = SRC / ".rwx-tokenizer.json"
        self.tokenizer = json.loads(profile.read_text()) if ATTR_NARROW and not BROAD and profile.is_file() else {}

    def old_source(self, rel: str) -> str | None:
        p = SRC / rel
        try:
            return p.read_bytes().decode("utf-8", "surrogateescape")
        except (OSError, ValueError):
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
                if stmt.body and stmt.body[0].lineno <= stmt.lineno:
                    # Body on the def line: coverage never records a code object's first
                    # line, so callers are invisible. Anyone reaching the binding may call it.
                    if isinstance(site.scope_node, (ast.Module, ast.ClassDef)):
                        mask |= self.import_mask(rel, site, why)
                    else:
                        mask |= m.span_mask(rel, site.code_key, *site.lines)
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

    def importers(self, rel: str) -> int:
        """Every test that imported the file (all of them if the map never saw it load)."""
        return self.map.file_mask(rel) or self.all

    def python_file(self, fp, rel: str, why: list, single_file=False) -> int:
        old = self.old_source(rel)
        if old is None:
            raise ValueError(f"missing base source: {rel}")
        hunks = locate_hunks(old, fp.hunks)
        if not hunks:
            why.append(f"{rel}: no hunks")
            return 0
        new = apply_hunks(old, hunks)
        if _encoding_marks(old) != _encoding_marks(new) or _undecodable(new):
            # A coding cookie or bytes that are not UTF-8 decide whether the file imports at
            # all. The engine compiles text, so it cannot check this: static importers.
            self.unverifiable = True
            why.append(f"{rel}: encoding change -> importers")
            return self.importers(rel)
        tree = self.tree(rel, old)
        try:
            new_tree = ast.parse(new)
        except Exception:  # SyntaxError, or ValueError for null bytes / surrogates
            new_tree = None
        if new_tree is None:
            self.py_changes[rel] = (old, new)
            why.append(f"{rel}: SYNTAX ERROR -> importers")
            return self.importers(rel)
        if same_syntax(tree, new_tree):
            why.append(f"{rel}: AST unchanged")
            return 0
        self.py_changes[rel] = (old, new)
        loc = Locator(tree)
        mask = 0
        old_changed, added = changed_lines(hunks)
        for ln in sorted(old_changed):
            site = loc.locate(ln)
            if site is None:
                mask |= self.map.file_mask(rel)
                continue
            candidates = self.site_mask(rel, site, why) | self.lambda_outer(rel, loc, site, ln, why)
            if single_file and old_changed == added == {ln} and site.kind == "func":
                self.case_site = rel, site.code_key, site.lines
            if (single_file and self.tokenizer and old_changed == added == {ln}
                    and site.kind == "import" and site.class_key(rel) in self.tokenizer["classes"]):
                name = flipped_bool(site.stmt, old.splitlines()[ln - 1], new.splitlines()[ln - 1])
                readers = self.tokenizer["flags"].get(name)
                if readers is not None:
                    candidates = (candidates & int(readers, 16)) | self.lazy_mask
                    why.append(f"{name} consumers -> {candidates.bit_count()}")
            mask |= candidates
        # Pure insertions: use the old lines around each insertion point.
        for anchor in insertion_anchors(hunks):
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
                mask |= self.lambda_outer(rel, loc, site, ln, why, inserted=True)
        if sum(h.old_len + h.new_len for h in hunks) > 3000:
            why.append(f"{rel}: very large change -> importers")
            return mask | self.importers(rel)
        mask |= self.import_blocks(rel, tree, new_tree, hunks, loc, why)
        return mask

    def lambda_outer(self, rel, loc, site, ln, why, inserted=False) -> int:
        """Coverage never records a code object's first line, so runs of a lambda that only
        execute its first line (all runs, for a one-line lambda) are invisible. A lambda only
        runs after the statement creating it did: charge such a change to that statement."""
        if not site.scope_path or site.scope_path[-1][0] != "lambda":
            return 0
        if not inserted and ln != site.scope_path[-1][2]:
            return 0  # a later line of the lambda: its own coverage is complete
        outer = loc.locate(ln, lambdas=False)
        if outer is None:
            return self.map.file_mask(rel)
        mask = self.site_mask(rel, outer, why)
        why.append(f"lambda via {outer.kind} {outer.code_key} -> {mask.bit_count()}")
        return mask

    def import_blocks(self, rel, tree, new_tree, hunks, loc, why) -> int:
        """Changes to statements that run at import time (module/class bodies, def and class
        headers). Two things the per-line sites miss: a statement that may now raise breaks
        every importer, and names bound only by the new (or only by the old) statements
        change who sees what."""
        if BROAD:
            return 0
        new_loc = Locator(new_tree)
        fut = has_future_annotations(tree)
        mask = 0
        for o_lo, o_hi, n_lo, n_hi in change_blocks(hunks):
            old_sites = [s for s in (loc.locate(ln, lambdas=False) for ln in range(o_lo, o_hi + 1)) if _import_time(s)]
            new_sites = [s for s in (new_loc.locate(ln, lambdas=False) for ln in range(n_lo, n_hi + 1)) if _import_time(s)]
            old_stmts = list({id(s.stmt): s.stmt for s in old_sites}.values())
            new_stmts = list({id(s.stmt): s.stmt for s in new_sites}.values())
            if not old_stmts and not new_stmts:
                continue
            paths = {tuple(name for _, name, _ in x.scope_path) for x in old_sites + new_sites}
            if len(paths) != 1:
                why.append(f"import-time {o_lo}-{o_hi}: several scopes -> importers")
                mask |= self.importers(rel)
                continue
            site = self._scope_site(tree, next(iter(paths)))
            if site is None:
                why.append(f"import-time {o_lo}-{o_hi}: scope not found -> importers")
                mask |= self.importers(rel)
                continue
            known = known_lookups(tree, o_lo, _scope_nodes(tree, site))
            rem_new = [b for b in new_stmts if not any(same_syntax(a, b) for a in old_stmts)]
            rem_old = [a for a in old_stmts if not any(same_syntax(a, b) for b in new_stmts)]
            if len(rem_old) == len(rem_new):
                safe = all(stmt_safe(a, b, fut, known) for a, b in zip(rem_old, rem_new))
            else:
                safe = all(stmt_inert(b, fut, known) for b in rem_new)
            if not safe:
                why.append(f"import-time {o_lo}-{o_hi} may raise -> importers")
                mask |= self.importers(rel)
                continue
            old_names = {n for x in rem_old for n in bound_names(x)}
            new_names = {n for x in rem_new for n in bound_names(x)}
            # Names bound on both sides are the per-line sites' business (with their narrowing).
            names = old_names ^ new_names
            if not names:
                continue
            site.names = tuple(sorted(names))
            extra = self.import_mask(rel, site, why)
            why.append(f"bindings {list(site.names)[:3]} -> {extra.bit_count()}")
            mask |= extra
        return mask

    def _scope_site(self, tree, path):
        """An old-file import-time Site for the class (or module) named by `path`."""
        from patchlib import Site, _code_key

        node, scope_path = tree, []
        for name in path:
            nxt = None
            for ch in node.body:
                if isinstance(ch, ast.ClassDef) and ch.name == name:
                    nxt = ch
            if nxt is None:
                return None
            first = nxt.decorator_list[0].lineno if nxt.decorator_list else nxt.lineno
            scope_path.append(("class", name, first))
            node = nxt
        return Site("import", tuple(scope_path), _code_key(node), (0, 0),
                    class_line=node.lineno if isinstance(node, ast.ClassDef) else None,
                    names=(), stmt=None, scope_node=node)

    # ------------------------------------------------------------ whole-file changes
    def file_level(self, fp, why: list) -> int:
        """Creation, deletion, rename, copy or binary change of a file."""
        removed = None if (fp.created or fp.copy) else fp.old_path
        added = None if fp.deleted else fp.new_path
        if fp.old_path == fp.new_path and not fp.created and not fp.deleted:
            removed = added = None  # in place (binary) change
        mask = 0
        if fp.binary and fp.old_path == fp.new_path and fp.path:
            mask |= self.data_mask(fp.path, why, binary=True)
        if removed:
            mask |= self.removed_file(removed, why)
        if added:
            content = None
            if not fp.binary:
                base = self.old_source(removed) if (removed and not fp.copy) else ("" if fp.created else self.old_source(fp.old_path))
                try:
                    content = apply_hunks(base, locate_hunks(base, fp.hunks)) if base is not None else None
                except PatchMismatch:
                    content = None
            if (removed and not fp.copy and content is not None and content == self.old_source(removed)
                    and added.startswith("tests/") and removed.startswith("tests/")):
                # A test module moved unchanged: its import already ran in every session.
                why.append(f"moved {removed} -> {added} unchanged")
            else:
                mask |= self.added_file(added, content, fp.binary, why)
        return mask

    def removed_file(self, rel: str, why: list) -> int:
        if rel.endswith(".py"):
            mask = self.map.file_mask(rel) | self.file_tests.get(rel, 0)
            if rel.startswith("sqlglot/"):
                mask |= self.file_tests.get("tests/test_sqlglotc.py", 0)  # lists the package's files
            why.append(f"removed {rel}: importers and its own tests -> {mask.bit_count()}")
            return mask
        if rel.startswith("tests/"):
            mid = self.map.opens.get(rel)
            mask = self.map.masks[mid] if mid is not None else 0
            why.append(f"removed data {rel}: readers -> {mask.bit_count()}")
            return mask
        if "/" not in rel and rel in CONFIG_FILES:
            why.append(f"removed config {rel} -> ALL")
            return self.all
        why.append(f"removed {rel}: nothing reads it")
        return 0

    def added_file(self, rel: str, content, binary: bool, why: list) -> int:
        base = rel.rsplit("/", 1)[-1]
        if rel.endswith(".py"):
            if base == "conftest.py" or "/" not in rel or rel.startswith("sqlglot/"):
                # conftest: pytest loads it; root level: shadows imports; sqlglot/: dialects and
                # optimizer passes are imported by computed names.
                why.append(f"new {rel} -> ALL")
                return self.all
            if _is_test_file(base):
                if content is not None and _inert_module(content):
                    why.append(f"new test file {rel}: inert module -> nothing")
                    return 0
                why.append(f"new test file {rel} runs code at import -> ALL")
                return self.all
            why.append(f"new {rel}: not imported by anything -> nothing")
            return 0
        if "/" not in rel and rel in CONFIG_FILES:
            why.append(f"new config {rel} -> ALL")
            return self.all
        if binary and rel.startswith("sqlglot/"):
            why.append(f"new binary {rel} -> ALL")
            return self.all
        why.append(f"new {rel}: nothing opens it")
        return 0

    def data_mask(self, rel: str, why: list, binary=False) -> int:
        m = self.map.opens.get(rel)
        fm = self.map.masks[m] if m is not None else 0
        if fm == 0:
            # data file nobody opened directly: tests in its directory's users
            d = rel.rsplit("/", 1)[0] + "/"
            for f, mid in self.map.opens.items():
                if f.startswith(d):
                    fm |= self.map.masks[mid]
        if binary and not rel.startswith("tests/"):
            fm = self.all if rel.startswith("sqlglot/") else 0
        why.append(f"{'binary ' if binary else ''}data {rel} -> {fm.bit_count()}")
        return fm

    def other_file(self, fp, rel: str, why: list) -> int:
        if "/" not in rel and rel in CONFIG_FILES:
            old = self.old_source(rel)
            if old is None:
                raise ValueError(f"missing base config: {rel}")
            new = apply_hunks(old, locate_hunks(old, fp.hunks))
            if _config_same_for_pytest(rel, old, new):
                why.append(f"config {rel}: parses, pytest settings unchanged -> nothing")
                return 0
            why.append(f"config {rel}: pytest may read it differently -> ALL")
            return self.all
        why.append(f"other file {rel} -> nothing")
        return 0

    def analyze(self, text: str) -> tuple[int, list]:
        why: list = []
        mask = 0
        self.py_changes: dict = {}
        self.data_changes: dict = {}
        self.sites: set = set()
        self.case_site = None
        self.unverifiable = False
        files = self.merge_repeats(parse_patch(text), why)
        for fp in files:
            if not fp.path and (fp.hunks or fp.binary):
                return self.all, ["patch entry without a path -> ALL"]
        for fp in files:
            if fp.binary or fp.created or fp.deleted or fp.copy or fp.old_path != fp.new_path:
                mask |= self.file_level(fp, why)
                continue
            rel = fp.path
            if not rel:
                continue
            if rel.endswith(".py"):
                mask |= self.python_file(fp, rel, why, single_file=len(files) == 1)
            elif rel.startswith("tests/"):
                old = self.old_source(rel)
                if old is None:
                    raise ValueError(f"missing base fixture: {rel}")
                self.data_changes[rel] = apply_hunks(old, locate_hunks(old, fp.hunks))
                mask |= self.data_mask(rel, why)
            else:
                mask |= self.other_file(fp, rel, why)
        if self.unverifiable:
            self.py_changes = {}  # nothing the engine runs could stand in for the real import
        return mask, why

    def merge_repeats(self, files, why):
        """The same file patched by several entries: apply them in order, re-diff once."""
        seen: dict = {}
        for fp in files:
            if fp.path and not (fp.binary or fp.created or fp.deleted or fp.copy or fp.old_path != fp.new_path):
                seen.setdefault(fp.path, []).append(fp)
        out = []
        done = set()
        for fp in files:
            group = seen.get(fp.path) if fp.path else None
            if not group or len(group) == 1:
                out.append(fp)
                continue
            if fp.path in done:
                continue
            done.add(fp.path)
            old = self.old_source(fp.path)
            if old is None:
                raise ValueError(f"missing base source: {fp.path}")
            text = old
            for g in group:
                text = apply_hunks(text, locate_hunks(text, g.hunks))
            merged = type(fp)(fp.path, fp.path, diff_hunks(old, text))
            why.append(f"{fp.path}: {len(group)} entries merged")
            out.append(merged)
        return out


CONFIG_FILES = ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", ".pytest.ini")
CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _encoding_marks(text: str) -> tuple:
    """BOM and coding cookies (first two lines): they decide how Python decodes the file."""
    head = text.split("\n", 2)[:2]
    return (text.startswith("\ufeff"),) + tuple(l for l in head if CODING_RE.match(l))


def _undecodable(text: str) -> bool:
    """Bytes that were not UTF-8 (decoded with surrogateescape)."""
    return any("\udc80" <= ch <= "\udcff" for ch in text)


def _import_time(site) -> bool:
    return (site is not None and site.stmt is not None and site.kind in ("import", "header")
            and isinstance(site.scope_node, (ast.Module, ast.ClassDef)))


def _scope_nodes(tree, site) -> list:
    """The class nodes enclosing a site, outermost first (module scope: empty)."""
    out, node = [], tree
    for kind, name, _ in site.scope_path:
        nxt = None
        for ch in getattr(node, "body", []):
            if isinstance(ch, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and ch.name == name:
                nxt = ch
        if nxt is None or kind != "class":
            break
        out.append(nxt)
        node = nxt
    return out


def _is_test_file(base: str) -> bool:
    return (base.startswith("test") or base.endswith("_test.py")) and base.endswith(".py")


def _inert_module(src: str) -> bool:
    """A new test module whose import cannot touch other tests: imports, defs, classes made
    of defs and literal attributes, no decorators, no other top-level code."""
    try:
        tree = ast.parse(src)
    except Exception:
        return True  # cannot be imported: only its own (unnumbered) tests fail
    for st in tree.body:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" for a in st.names):
                return False
            continue
        if isinstance(st, ast.ClassDef):
            if st.decorator_list or st.keywords or not all(isinstance(b, (ast.Name, ast.Attribute)) for b in st.bases):
                return False
            for sub in st.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.decorator_list and all(
                        isinstance(d, ast.Constant) for d in sub.args.defaults + [d for d in sub.args.kw_defaults if d]):
                    continue
                if isinstance(sub, ast.Pass) or (isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Constant)):
                    continue
                if isinstance(sub, ast.Assign) and all(isinstance(t, ast.Name) for t in sub.targets) and isinstance(sub.value, ast.Constant):
                    continue
                return False
            continue
        if stmt_inert(st, True):
            continue
        if (isinstance(st, ast.If) and isinstance(st.test, ast.Compare) and isinstance(st.test.left, ast.Name)
                and st.test.left.id == "__name__"):
            continue
        return False
    return True


def _config_same_for_pytest(rel: str, old: str, new: str) -> bool:
    """The config still parses and its pytest settings did not change."""
    if _undecodable(new):
        return False
    if rel.endswith(".toml"):
        try:
            import tomllib
        except ImportError:
            return False
        try:
            a, b = tomllib.loads(old), tomllib.loads(new)
        except Exception:
            return False
        ta, tb = a.get("tool", {}), b.get("tool", {})
        return ta.get("pytest") == tb.get("pytest") and "pytest" not in tb
    import configparser

    def load(text):
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read_string(text)
        return {k: dict(cp[k]) for k in cp.sections() if k in ("pytest", "tool:pytest")}

    try:
        return load(old) == load(new) and not load(new)
    except Exception:
        return False


def flipped_bool(stmt, before, after):
    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.lineno != stmt.end_lineno:
        return None
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    value = stmt.value
    if (len(targets) != 1 or not isinstance(targets[0], ast.Name)
            or not isinstance(value, ast.Constant) or type(value.value) is not bool):
        return None
    # AST columns are UTF-8 byte offsets. Match the entire replacement line.
    line = before.encode()
    flipped = b"False" if value.value else b"True"
    if line[:value.col_offset] + flipped + line[value.end_col_offset:] == after.encode():
        return targets[0].id
    return None


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
WARM_NEEDED_ONLY = os.environ.get("RWX_WARM_NEEDED_ONLY", "0") == "1"
BET_CLEAN = os.environ.get("RWX_BET_CLEAN") == "1"  # experiment: clean after two passing probes
FINAL_FRAC = float(os.environ.get("RWX_FINAL_FRAC", "1.0"))  # share of the possible saving the final clean-proof may spend


RUN_GAIN = float(os.environ.get("RWX_RUN_GAIN", "0"))  # also run tests whose CPU is below this share of their recorded time


def is_heavy(n) -> bool:
    """Never run here: starts a fresh interpreter, or estimated CPU too high for what
    printing it costs (a test far slower on the board than in CPU is still worth running)."""
    if n in NO_RUN:
        return True
    cpu = CPU_EST.get(n, DUR[n]) + CLS_SETUP.get(CLS_OF.get(n), 0)
    return cpu > RUN_MAX_MS and cpu > RUN_GAIN * DUR[n]


SETTLE_RATIO = float(os.environ.get("RWX_SETTLE_RATIO", "0.5"))  # assumed scorer CPU per estimated CPU ms; 0: off
SETTLE_PREFIX = 40
SETTLE_BUDGET_PER_COMMIT_MS = int(os.environ.get("RWX_SETTLE_BUDGET_MS", "6000"))  # est CPU settle may spend, per commit in the batch
SETTLE_BUDGET = [float("inf")]  # set by main(): per-commit budget x commits
SETTLE_SPENT = [0]
SETTLE_MAX_MS = int(os.environ.get("RWX_SETTLE_MAX_MS", "4500"))  # settle may also run tests this costly (e.g. generator_memory)
SETTLE_FAIL_MULT = float(os.environ.get("RWX_SETTLE_FAIL_MULT", "1"))  # ratio multiplier once something failed; 0: no settling then


def policy(C: list[int], dur: dict, data_only: bool, focus: dict | None = None, known: set | None = None):
    """Generator: the batches of _policy, then (RWX_SETTLE_RATIO) a last batch of the printed
    runnable tests whose estimated CPU is worth less than the worker time they add."""
    gen = _policy(C, dur, data_only, focus, known)
    seen: dict = {}
    try:
        req = next(gen)
        while True:
            res = yield req
            seen.update(res)
            req = gen.send(res)
    except StopIteration as e:
        final = set(e.value)
    if SETTLE_RATIO <= 0 or data_only or not seen:
        return final
    ratio = SETTLE_RATIO
    nfail = sum(st != "P" for st in seen.values())
    keep = 1.0  # chance an unproven test passes, so that running it removes it
    if nfail:
        if SETTLE_FAIL_MULT <= 0:
            return final
        ratio *= SETTLE_FAIL_MULT
        keep = 1 - (nfail + 1) / (len(seen) + 2)  # Laplace estimate from this commit's runs
    # Tests sharing a class with a seen failure usually fail too: running them buys nothing.
    bad_cls = {CLS_OF.get(n) for n, st in seen.items() if st != "P"}
    unproven = [n for n in final if n not in seen and n not in NO_RUN and n not in (known or ())
                and (not is_heavy(n) or batch_cost([n]) <= SETTLE_MAX_MS) and CLS_OF.get(n) not in bad_cls]
    if not unproven:
        return final
    order = sorted(unproven, key=lambda n: (-DUR[n] / (batch_cost([n]) + 1), n))
    best0 = best = _pack_lb(final)
    best_j = 0
    steps = sorted(set(range(1, min(SETTLE_PREFIX, len(order)) + 1))
                   | {len(order) * i // 10 for i in range(1, 11) if len(order) * i // 10 > SETTLE_PREFIX})
    for j in steps:
        c = ratio * batch_cost(order[:j]) + keep * _pack_lb(final - set(order[:j])) + (1 - keep) * best0
        if c < best:
            best, best_j = c, j
    if not best_j:
        return final
    batch = order[:best_j]
    if SETTLE_SPENT[0] + batch_cost(batch) > SETTLE_BUDGET[0]:
        return final  # batch-wide settle budget exhausted: print the unproven tests
    SETTLE_SPENT[0] += batch_cost(batch)
    res = yield batch, False
    ok = {n for n, st in res.items() if st == "P"}
    bad = {n for n in batch if res.get(n) != "P"}
    final -= ok
    if bad:
        # A new failure: slow tests left out on clean evidence come back unless their own
        # test file showed all of its runnable candidates passing.
        allpass = {n for n, st in (seen | res).items() if st == "P"}
        rest = [n for n in C if not is_heavy(n)]
        for g in C:
            if g in final or g in seen or g in res or not is_heavy(g):
                continue
            rel = [n for n in rest if FILE_OF.get(n) == FILE_OF.get(g)]
            if not rel or not all(n in allpass for n in rel):
                final.add(g)
    return final


def _policy(C: list[int], dur: dict, data_only: bool, focus: dict | None = None, known: set | None = None):
    """Generator: yields (tests, stop_first) batches, receives {n: 'P'|'F'}, returns the final set.

    Passing probes can clear a giant on evidence from its test file. A clean
    run of every runnable candidate can also clear the other heavy tests."""
    if not C:
        return set()
    if data_only:
        return set(C)  # Fixture edits and patches the engine cannot apply stay static.
    if known:
        # Tests reading an edited data file: included as they are, never run here.
        rest_gen = _policy([n for n in C if n not in known], dur, False, focus, None)
        return set(known) | (yield from rest_gen)
    heavy = [n for n in C if is_heavy(n)]
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


def _pack_lb(nums) -> int:
    """Lower bound of _pack_cost (longest test vs even split, per worker count). LPT met it
    on every lab, sample and synthetic set checked but 2 of 5,040 (by at most 20 ms)."""
    if not nums:
        return 0
    ds = [DUR[n] for n in nums]
    top, tot = max(ds), sum(ds)
    return min(max(top, -(-tot // k)) + WORKER_MS * k for k in range(1, min(MAX_WORKERS, len(ds)) + 1))


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
    if not giants and not BET_CLEAN:
        return (yield from _finish_clean(C, rest, heavy, passed, NOGIANT_FRAC))
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
    if BET_CLEAN:
        # EXPERIMENT (off by default): both probes passed -> treat the commit as clean.
        return set(heavy) & NO_RUN
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


NOGIANT_FRAC = float(os.environ.get("RWX_NOGIANT_FRAC", "0"))
LIVE_FILE_FRAC = float(os.environ.get("RWX_LIVE_FILE_FRAC", "0"))
LIVE_REST_FRAC = float(os.environ.get("RWX_LIVE_REST_FRAC", "0"))


def _finish_clean(C, rest, heavy, passed, frac):
    """Everything run so far passed. Run the remaining runnable candidates when their CPU
    is below frac x the worker cost a clean result would save."""
    selected = set(C) - passed
    remaining = [n for n in rest if n not in passed]
    mandatory = set(heavy) & NO_RUN if CLEAN_SKIPS_HEAVY else {n for n in heavy if n in NO_RUN or DUR[n] < SKIP_MIN_MS}
    if not remaining or frac <= 0 or batch_cost(remaining) > frac * (_pack_cost(selected) - _pack_cost(mandatory)):
        return selected
    res = yield remaining, True
    clean = {n for n, st in res.items() if st == "P"}
    if len(clean) == len(remaining):
        return mandatory
    return (yield from _live(C, rest, heavy, passed | clean, {n for n, st in res.items() if st == "F"}))


def _live(C, rest, heavy, passed, failed):
    """Something failed. Keep everything not proven passing, except slow tests that a
    narrow failure (few tests failing, none in the slow test's own file) cannot reach."""
    dur = DUR
    if LIVE_FILE_FRAC > 0 or LIVE_REST_FRAC > 0:
        return (yield from _live_evidence(C, rest, heavy, passed, failed))
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


def _live_evidence(C, rest, heavy, passed, failed):
    """Something failed. A giant goes only when every runnable candidate of its own test
    file ran and passed (bought when that costs under LIVE_FILE_FRAC x the worker time
    the giant adds). Then the other unrun candidates are run when their CPU is below
    LIVE_REST_FRAC x the worker time they add."""
    passed, failed = set(passed), set(failed)
    giants = [g for g in heavy if DUR[g] >= SKIP_MIN_MS and g not in NO_RUN]
    drop = set()
    if giants and LIVE_FILE_FRAC > 0:
        batch = []
        for g in giants:
            rel = [n for n in rest if FILE_OF.get(n) == FILE_OF.get(g)]
            if not rel or any(n in failed for n in rel):
                continue
            todo = [n for n in rel if n not in passed and n not in batch]
            sel_now = set(C) - passed
            gain = _pack_cost(sel_now) - _pack_cost(sel_now - {g})
            if batch_cost(batch + todo) - batch_cost(batch) <= min(RELATED_ALL_MS, LIVE_FILE_FRAC * gain):
                batch += todo
                drop.add(g)
        if batch:
            res = yield batch, False
            passed |= {n for n, st in res.items() if st == "P"}
            failed |= {n for n in batch if res.get(n) != "P"}
        drop = {g for g in drop if all(n in passed for n in rest if FILE_OF.get(n) == FILE_OF.get(g))}
    selected = set(C) - passed - drop
    unrun = [n for n in rest if n not in passed and n not in failed]
    if unrun and LIVE_REST_FRAC > 0:
        gain = _pack_cost(selected) - _pack_cost(selected - set(unrun))
        if batch_cost(unrun) <= LIVE_REST_FRAC * gain:
            res = yield unrun, False
            selected -= {n for n, st in res.items() if st == "P"}
    return selected


HARD_GRACE_S = 8.0  # after the scheduling deadline, in-flight batches get this long, then die


class Scheduler:
    def __init__(self, engine, lanes: int, deadline: float):
        self.engine = engine
        self.lanes = lanes
        self.deadline = deadline

    def _abandon(self, a):
        """Kill an active commit's session; its final set treats the in-flight batch as
        unproven (reported passes still count)."""
        cid, C, gen, sess, batch, selected = a
        partial = {n: st for n, st in getattr(sess, "results", {}).items() if st == "P"}
        try:
            sess.kill()
            sess.close()
        except Exception:  # noqa: BLE001
            pass
        log(f"{cid}: killed at the hard deadline after {len(partial)}/{len(batch)} tests")
        if selected is not None:  # TPC-DS check in flight: keep TPC-DS
            final = selected[1] | {self.engine.executor_test}
        else:
            final = _drain(gen, partial)
        return cid, final if final is not None else set(C), "killed at the hard deadline"

    def run(self, jobs):
        """jobs: list of (cid, C, py_changes, data_changes, data_only). Yields (cid, final_set, note)."""
        import select as _sel

        queue = list(jobs)
        active = []  # [cid, C, gen, session, batch, selected before executor verification]
        while queue or active:
            while queue and len(active) < self.lanes:
                cid, C, py, data, data_only, focus, known, case_site = queue.pop(0)
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
                try:
                    sess = self.engine.session(py, data, case_site)
                    sess.submit(batch, timeout=batch_timeout(batch), stop_first=stop_first)
                except Exception as e:  # one commit's trouble must not stop the others
                    log(f"{cid}: session failed {e!r}"[:300])
                    gen.close()
                    yield cid, set(C), "session failed"
                    continue
                active.append([cid, C, gen, sess, batch, None])
            if not active:
                continue
            hard = self.deadline + HARD_GRACE_S
            if time.monotonic() > hard:
                # Past the hard limit: kill what is still running and print what is unproven.
                for a in list(active):
                    yield self._abandon(a)
                    active.remove(a)
                continue
            timeout = max(0.05, min(min(a[3].deadline for a in active), hard) - time.monotonic())
            ready, _, _ = _sel.select([a[3] for a in active], [], [], timeout)
            for a in list(active):
                try:
                    cid, C, gen, sess, batch, selected = a
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
                    if any(st == "F" for st in res.values()):
                        LIVE.add(cid)
                    if not sess.alive:
                        # crashed or killed: whatever did not report counts as failing
                        for n in batch:
                            res.setdefault(n, "F")
                    executor = self.engine.executor_test
                    if selected is not None:
                        kind, selected = selected
                        if kind == "verify":
                            # every affected query was run: a pass clears TPC-DS
                            if sess.alive and res.get(executor) == "P":
                                selected = selected - {executor}
                            note = "checked affected TPC-DS queries"
                        else:
                            # a sample of affected queries gated leaving TPC-DS out
                            if not (sess.alive and res.get(executor) == "P"):
                                selected = selected | {executor}
                            note = f"TPC-DS sample gate {'passed' if executor not in selected else 'FAILED: kept TPC-DS'}"
                        sess.close()
                        active.remove(a)
                        yield cid, selected, note
                        continue
                    try:
                        nxt, stop_first = gen.send(res)
                        if time.monotonic() > self.deadline or not sess.alive:
                            raise GeneratorExit
                        if time.monotonic() + batch_cost(nxt) / 1000 > self.deadline:
                            # No time for this batch: the policy settles with it unreported.
                            final = _drain(gen)
                            sess.close()
                            active.remove(a)
                            yield cid, final if final is not None else set(C), "cut: no time for the next batch"
                            continue
                        sess.submit(nxt, timeout=batch_timeout(nxt), stop_first=stop_first)
                        a[4] = nxt
                    except StopIteration as e:
                        if (executor in e.value and sess.executor_ms is not None and not sess.executor_partial
                                and sess.alive and executor not in res and time.monotonic() < self.deadline):
                            cpu = sess.executor_ms + CLS_SETUP.get(CLS_OF.get(executor), 0)
                            if _pack_cost(e.value) - _pack_cost(e.value - {executor}) >= 3 * cpu:
                                sess.submit([executor], timeout=2.0 + 2.0 * cpu / 1000, executor_only=True)
                                a[4:] = [[executor], ("verify", e.value)]
                                continue
                        if (executor is not None and executor in C and executor not in e.value
                                and sess.executor_ms is not None and sess.alive and executor not in res
                                and time.monotonic() < self.deadline):
                            cpu = sess.executor_ms + CLS_SETUP.get(CLS_OF.get(executor), 0)
                            sess.submit([executor], timeout=10.0 + 3.0 * cpu / 1000, executor_only=True)
                            a[4:] = [[executor], ("gate", e.value)]
                            continue
                        sess.close()
                        active.remove(a)
                        yield cid, e.value, f"ran {sess.mode} body {sum(sess.cpu.values()):.2f}s total {sess.total_cpu:.2f}s rss {sess.maxrss_mb:.0f}MB batches {len(batch)}{' CRASH ' + str(sess.crash)[:80] if sess.crash else ''}"
                    except GeneratorExit:
                        gen.close()
                        sess.close()
                        active.remove(a)
                        yield cid, set(C), "stopped"

                except Exception as e:  # one commit's trouble must not stop the others
                    log(f"{a[0]}: scheduler error {e!r}"[:300])
                    try:
                        a[3].kill()
                        a[3].close()
                    except Exception:  # noqa: BLE001
                        pass
                    if a in active:
                        active.remove(a)
                    yield a[0], set(a[1]), "scheduler error"

def _drain(gen, first=None):
    """Answer the pending batch with `first` (default: nothing reported) and every further
    batch with no results; the policy's final set, or None if it keeps asking."""
    try:
        gen.send(dict(first or {}))
        for _ in range(20):
            gen.send({})
    except StopIteration as e:
        return set(e.value)
    except Exception:  # noqa: BLE001
        return None
    gen.close()
    return None


DUR: dict = {}
LIVE: set = set()  # commits where some run reported a failure
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


def list_patches(d: Path) -> list:
    """Commit patch files: regular files named <id>.patch (anything else is not a commit)."""
    try:
        entries = sorted(d.iterdir(), key=lambda p: p.name)
    except OSError as e:
        log(f"cannot list {d}: {e!r}")
        return []
    out = []
    for p in entries:
        try:
            if p.name.endswith(".patch") and len(p.name) > len(".patch") and p.is_file():
                out.append(p)
        except OSError:
            continue
    return out


def read_patch(p: Path) -> str:
    """Patch text; bytes that are not UTF-8 survive as surrogates so they can be detected."""
    data = p.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", "surrogateescape")


def main() -> None:
    t0 = time.monotonic()
    # Start-up allocates mostly long-lived objects (map, trees, warm-up modules): the
    # collector would only rescan them. Engine re-enables it after gc.freeze().
    gc.disable()
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
    file_tests: dict = {}
    for t in tests:
        file_tests[t["file"]] = file_tests.get(t["file"], 0) | 1 << t["number"]
    an = Analyzer(cmap, valid, lazy, file_tests)
    patches = list_patches(DATA / "commits")
    SETTLE_BUDGET[0] = SETTLE_BUDGET_PER_COMMIT_MS * len(patches)
    jobs, final = [], {}
    for patch in patches:
        cid = patch.name[: -len(".patch")]
        try:
            mask, why = an.analyze(read_patch(patch))
            py, data, case_site = dict(an.py_changes), dict(an.data_changes), an.case_site
        except Exception as e:  # never crash: run everything for this commit
            mask, why, py, data, case_site = valid, [f"analyzer error {e!r} -> ALL"], {}, {}, None
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
        jobs.append((cid, C, py, data, data_only, score, known, case_site))
    need_engine = USE_ENGINE and any(C and not data_only for _, C, _, _, data_only, *_ in jobs)
    results = {}
    if need_engine:
        try:
            from engine import Engine

            exec_cls = "tests/test_executor.py::TestExecutor"
            runnable_exec = any(
                CLS_OF.get(n) == exec_cls and n not in NO_RUN and CPU_EST.get(n, dur[n]) <= RUN_MAX_MS
                for _, C, _, _, data_only, *_ in jobs if not data_only for n in C
            )
            # Warm up only the test modules some commit may actually run.
            needed = {n for _, C, _, _, data_only, *_ in jobs if not data_only for n in C}
            eng = Engine(str(SRC), tests, log=log, presetup=(exec_cls,) if (runnable_exec and PRESETUP) else (),
                         needed=needed if WARM_NEEDED_ONLY else None)
            if eng.presetup_done:
                CLS_SETUP[exec_cls] = 0
            log(f"engine warm in {time.monotonic() - t0:.1f}s, parent cpu {time.process_time():.2f}s")
            budget = min(140.0, 3.2 * len(jobs) + 10)
            sched = Scheduler(eng, lanes=2, deadline=t0 + budget)
            for cid, final_set, note in sched.run(jobs):
                results[cid] = (final_set, note)
        except Exception as e:  # engine trouble: fall back to the static sets
            log(f"engine failed: {e!r}")
    total = 0
    diag_min = int(os.environ.get("RWX_DIAG_MIN", "0"))
    diag_max = int(os.environ.get("RWX_DIAG_MAX", "100000"))
    for cid, C, py, data, data_only, *_ in jobs:
        final_set, note = results.get(cid, (set(C), "static"))
        if diag_min and diag_min <= len(C) <= diag_max:
            final_set, note = set(), "diagnostic: printed nothing"
        diag_mode = os.environ.get("RWX_DIAG_MODE", "")
        if (diag_mode == "live" and cid in LIVE) or (diag_mode == "static" and C and note == "static"):
            final_set, note = set(), f"diagnostic ({diag_mode}): printed nothing"
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
    # Skip interpreter teardown: freeing the frozen warm-up heap costs CPU, and duckdb's
    # nanobind prints leak reports for objects gc.freeze() kept alive.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
