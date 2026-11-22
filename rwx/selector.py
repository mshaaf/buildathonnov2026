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
    same_outside_lambdas,
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
        self.fixture_attrs: dict = d.get("fixture_attrs", {})
        self.attr_reads: dict = d.get("attr_reads", {})
        self.line_loaded = d.get("line_loaded")  # None: unknown, title rule off
        self.expected_codes: dict = d.get("expected_codes", {})
        self.cpu_ms: dict = d.get("cpu_ms", {})
        self.cls_setup_ms: dict = d.get("cls_setup_ms", {})
        self.focus: dict = d.get("focus", {})
        self.total_lines: dict = d.get("total_lines", {})
        self.class_users: dict = d.get("class_users", {})
        self.class_desc: dict = d.get("class_desc", {})
        self.name_refs: dict = d.get("name_refs", {})
        self.attr_refs: dict = d.get("attr_refs", {})
        self.cmp_sites: dict = d.get("cmp_sites", {})  # file -> {(line, col): (op, {vector: mask_id})}
        self.cmp_unprofiled: int = d["masks"][d["cmp_unprofiled"]] if "cmp_unprofiled" in d else -1
        self.bool_sites: dict = d.get("bool_sites", {})  # file -> {(line, col): (op, mask_id)}
        self.bool_unprofiled: int = d["masks"][d["bool_unprofiled"]] if "bool_unprofiled" in d else -1
        if not self.bool_sites and BOOL_PROFILE:
            self._load_bool_side(path.with_name("bool.pkl.z"))
        # (file, code_key) of a class body -> class key
        self.class_by_code: dict = {}
        for k in self.class_desc:
            f, line, qual = k.split(":", 2)
            self.class_by_code[(f, f"{line}:{qual.rsplit('.', 1)[-1]}")] = k

    def _load_bool_side(self, side: Path) -> None:
        """and/or profiles from the side file (tools/lab/bool_side.py). Any problem: no
        profiles, so and/or flips keep their full candidate sets."""
        try:
            s = pickle.loads(zlib.decompress(side.read_bytes()))
            sites = {}
            for f, v in s["bool_sites"].items():
                sites[f] = {}
                for k, (op, mask) in v.items():
                    self.masks.append(mask)
                    sites[f][k] = (op, len(self.masks) - 1)
            unprof = s["bool_unprofiled"]
        except Exception:
            return
        self.bool_sites, self.bool_unprofiled = sites, unprof

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
        self._code_nodes: dict = {}
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
                const_defaults = DEFAULT_CONST and self.only_constant_defaults(rel, site)
                if const_defaults:
                    why.append("only constant defaults changed: callers only")
                if not mask and site.scope_path and site.scope_path[-1][0] == "class" and not const_defaults:
                    mask = m.span_mask(rel, site.code_key, *site.lines)
                first = stmt.decorator_list[0].lineno if stmt.decorator_list else stmt.lineno
                if not isinstance(site.scope_node, (ast.Module, ast.ClassDef)):
                    # nested def: decorators/defaults run in the enclosing function
                    mask |= m.span_mask(rel, site.code_key, *site.lines)
                elif stmt.body and stmt.body[0].lineno <= stmt.lineno and first not in m.code(rel, own):
                    # Body on the def line and no entry for it: coverage never records a code
                    # object's first line, so callers are unknown. Anyone reaching the binding
                    # may call it; nobody known -> importers.
                    mask |= self.import_mask(rel, site, why) or self.importers(rel)
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
                or (node.keywords and not (META_STATIC and self.static_metaclass(rel, node, site.names)))
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
            if REF_LINES and readers:
                # same references, but only tests that executed a line where the name appears
                rmask = self.ref_line_mask(refs, site.names)
                line_readers = 0
                for name in site.names or ():
                    line_readers |= self.ref_line_mask(m.name_refs.get(name, ()), (name,))
                readers = line_readers
                narrowed = users & readers if ATTR_NARROW else users
            else:
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

    def static_metaclass(self, rel, node, names) -> bool:
        """`class X(metaclass=M)` with M defined in the same module, where M's code reads class
        attributes only by literal names, none of them the edited ones: then M cannot observe
        the edit, and the class is no broader than a plain one. Dynamic access (getattr/setattr/
        hasattr with a computed name, vars(), dir(), iterating a __dict__) disqualifies M, except
        getattr/dir on a module object just returned by importlib.import_module."""
        if len(node.keywords) != 1 or node.keywords[0].arg != "metaclass" or not isinstance(node.keywords[0].value, ast.Name):
            return False
        text = self.old_source(rel)
        if text is None:
            return False
        tree = self.tree(rel, text)
        metas = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == node.keywords[0].value.id]
        if len(metas) != 1:
            return False
        meta = metas[0]
        modules = {t.id for n in ast.walk(meta) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "import_module"
                   for t in n.targets if isinstance(t, ast.Name)}
        wanted = set(names or ())
        for n in ast.walk(meta):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                f = n.func.id
                if f in ("vars", "dir") and not (n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in modules):
                    return False
                if f in ("getattr", "setattr", "hasattr", "delattr") and len(n.args) >= 2 and not isinstance(n.args[1], ast.Constant):
                    if not (isinstance(n.args[0], ast.Name) and n.args[0].id in modules):
                        return False
            if isinstance(n, ast.Attribute) and n.attr == "__dict__":
                parent_ok = False
                for p in ast.walk(meta):
                    if (isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute) and p.func.value is n
                            and p.func.attr == "get" and p.args and isinstance(p.args[0], ast.Constant)):
                        parent_ok = True
                        break
                if not parent_ok:
                    return False
            if isinstance(n, ast.Name) and n.id in wanted:
                return False
            if isinstance(n, ast.Attribute) and n.attr in wanted:
                return False
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in wanted:
                return False
        return True

    def only_constant_defaults(self, rel, site) -> bool:
        """The def statement differs only in default values, each a constant before and after.
        Evaluating a constant default cannot fail or touch anything, and no code in sqlglot or
        its tests reads __defaults__, so only calls (which enter the function) observe it."""
        old_stmt = site.stmt
        new_src = self.py_changes.get(rel, (None, None))[1]
        if new_src is None or not isinstance(old_stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError:
            return False
        twins = [n for n in ast.walk(new_tree) if isinstance(n, type(old_stmt)) and n.name == old_stmt.name
                 and n.lineno == old_stmt.lineno]
        if len(twins) != 1:
            return False
        new_stmt = twins[0]
        od = old_stmt.args.defaults + [d for d in old_stmt.args.kw_defaults if d is not None]
        nd = new_stmt.args.defaults + [d for d in new_stmt.args.kw_defaults if d is not None]
        if len(od) != len(nd) or [d is None for d in old_stmt.args.kw_defaults] != [d is None for d in new_stmt.args.kw_defaults]:
            return False
        changed = [(a, b) for a, b in zip(od, nd) if ast.dump(a) != ast.dump(b)]
        if not changed or not all(isinstance(a, ast.Constant) and isinstance(b, ast.Constant) for a, b in changed):
            return False

        def strip(fn):
            clone = ast.parse(ast.unparse(fn)).body[0]
            clone.args.defaults = []
            clone.args.kw_defaults = [None] * len(clone.args.kw_defaults)
            return ast.dump(clone)

        return strip(old_stmt) == strip(new_stmt)

    def ref_line_mask(self, refs, names) -> int:
        """Like Map.ref_mask, but for references inside functions only the tests that executed
        a line on which one of the names appears (Name, .attr or "string"). Import-time
        references (class/module bodies) and anything not located keep the code-object mask."""
        m = self.map
        out = 0
        for f, key, kind in refs:
            lines = self._ref_lines(f, key, names) if kind == "func" else None
            if lines is None:
                out |= m.ref_mask([(f, key, kind)])[0]
                continue
            for ln, mid in m.code(f, key).items():
                if ln in lines:
                    out |= m.masks[mid]
        return out

    def _ref_lines(self, f, key, names):
        from patchlib import _code_key

        idx = self._code_nodes.get(f)
        if idx is None:
            text = self.old_source(f)
            idx = {}
            if text is not None:
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    tree = None
                for node in ast.walk(tree) if tree is not None else ():
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        k = _code_key(node)
                        idx[k] = None if k in idx else node  # duplicate keys: cannot tell them apart
            self._code_nodes[f] = idx
        node = idx.get(key)
        if node is None:
            return None
        wanted = set(names)
        lines: set = set()

        def hit(n):
            return ((isinstance(n, ast.Name) and n.id in wanted) or (isinstance(n, ast.Attribute) and n.attr in wanted)
                    or (isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in wanted))

        def visit(n):
            for ch in ast.iter_child_nodes(n):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    # decorators, defaults and bases run in this scope; the body is another code object
                    extra = list(getattr(ch, "decorator_list", []))
                    if isinstance(ch, ast.ClassDef):
                        extra += ch.bases + [k.value for k in ch.keywords]
                    else:
                        extra += ch.args.defaults + [d for d in ch.args.kw_defaults if d]
                    for e in extra:
                        if hit(e):
                            lines.update(range(e.lineno, e.end_lineno + 1))
                        visit(e)
                    continue
                if isinstance(ch, ast.GeneratorExp):
                    return_none[0] = True  # generator bodies are separate code objects: stay coarse
                if hit(ch):
                    lines.update(range(ch.lineno, ch.end_lineno + 1))
                visit(ch)

        return_none = [False]
        body = node.body if isinstance(node.body, list) else [node.body]
        for st in body:
            if hit(st):
                lines.update(range(st.lineno, st.end_lineno + 1))
            visit(st)
        if return_none[0] or not lines:
            return None
        return lines

    def refine_fixture(self, rel, old, new, fm, why):
        """A fixture a test class loads in setUpClass (into cls.ATTR) only reaches the
        tests that read ATTR; tests that never unpack the expected part of the pairs
        cannot see an edit confined to expected outputs. Anything unusual keeps every
        opener (including an edit that would make the shared setup itself raise)."""
        fa = self.map.fixture_attrs.get(rel)
        cm = getattr(self, "class_masks", None)
        if not fa or not cm or not FIXTURE_REFINE:
            return fm
        old_pairs, new_pairs = _load_pairs(old), _load_pairs(new)
        if old_pairs is None or new_pairs is None:
            why.append(f"{rel}: fixture would not load -> every opener")
            return fm
        if len(old_pairs) == len(new_pairs):
            sql_changed = any(o[:2] != n[:2] for o, n in zip(old_pairs, new_pairs))
            exp_changed = any(o[2] != n[2] for o, n in zip(old_pairs, new_pairs))
            self.fixture_runs[rel] = {
                "attrs": [(class_id, attr) for class_id, attr in fa],
                "edited": [i for i, (o, n) in enumerate(zip(old_pairs, new_pairs)) if o != n],
                "count": len(new_pairs),
            }
        else:
            sql_changed = exp_changed = True
        for class_id, attr in fa:
            in_class = cm.get(class_id, 0)
            file = class_id.split("::")[0]
            if attr in self.map.attr_reads:
                # recorded: the tests that actually read cls.ATTR (tools/lab/attrrun.py)
                readers = self.map.masks[self.map.attr_reads[attr]]
            else:
                readers = 0
                for f, key, kind in self.map.name_refs.get(attr, ()):
                    if f == file and kind == "func":
                        readers |= self.map.code_mask(f, key)
            exp_users = 0
            for key in self.map.expected_codes.get(file, ()):
                exp_users |= self.map.code_mask(file, key)
            keep = (readers if sql_changed else 0) | ((readers & exp_users) if exp_changed else 0)
            before = bin(fm & in_class).count("1")
            fm = (fm & ~in_class) | (fm & in_class & keep)
            why.append(f"{rel}: {class_id} readers of {attr} {before} -> {bin(fm & in_class).count('1')}"
                       f" (sql {'changed' if sql_changed else 'same'}, expected {'changed' if exp_changed else 'same'})")
        return fm

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
        flips = compare_flips(tree, new_tree)
        if flips:
            self.cmp_flips += [(rel,) + f for f in flips]
            if self.cmp_flip is None:
                self.cmp_flip = (rel,) + flips[0]
        self.py_changes[rel] = (old, new)
        loc = Locator(tree)
        mask = 0
        old_changed, added = changed_lines(hunks)
        if len(old_changed) + len(added) > 3000:
            why.append(f"{rel}: very large change -> importers")
            return self.importers(rel)
        # Statement spans narrow the TPC-DS per-query check only when the edit replaces lines
        # one for one and leaves every statement where it was (same kind, lines, indent):
        # then only expressions changed, and a query that ran none of a changed statement's
        # lines runs the same code as before.
        spans_ok = bool(TPCDS_WIDE and rel.startswith("sqlglot/") and old_changed and old_changed == added
                        and all(h.old_len == h.new_len for h in hunks) and _stmt_skeleton(tree) == _stmt_skeleton(new_tree))
        for ln in sorted(old_changed):
            site = loc.locate(ln)
            if site is None:
                mask |= self.map.file_mask(rel)
                continue
            candidates = self.site_mask(rel, site, why) | self.lambda_outer(rel, loc, site, ln, why)
            if single_file and old_changed == added == {ln} and site.kind == "func":
                self.case_site = rel, site.code_key, site.lines
            if site.kind == "func" and spans_ok:
                self.case_sites.setdefault((rel, site.code_key), []).append(tuple(site.lines))
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
        if flips and (CMP_PROFILE or BOOL_PROFILE) and not BROAD:
            risky = [(ln, c) for ln, c, op, _ in flips if op in ("and", "or") and _bool_rhs_may_raise(tree, ln, c)]
            if risky and BOOL_RHS_GUARD:
                # the pinned run evaluating a raising right operand left no profile record,
                # and the flipped operator would skip it: no narrowing
                why.append(f"and/or flip at {risky[0][0]}:{risky[0][1]}: right operand may raise, no narrowing")
            else:
                mask = self.compare_narrow(rel, flips, mask, why)
        return mask

    def lambda_outer(self, rel, loc, site, ln, why, inserted=False) -> int:
        """Coverage never records a code object's first line, so runs of a lambda that only
        execute its first line (all runs, for a one-line lambda) are invisible. A lambda only
        runs after the statement creating it did: charge such a change to that statement."""
        if not site.scope_path or site.scope_path[-1][0] != "lambda":
            return 0
        first = site.scope_path[-1][2]
        if not inserted and ln != first:
            return 0  # a later line of the lambda: its own coverage is complete
        if not inserted and first in self.map.code(rel, site.code_key):
            return 0  # entries (PY_START supplement) record who ran the first line
        outer = loc.locate(ln, lambdas=False)
        mask = self.site_mask(rel, outer, why) if outer is not None else 0
        if not mask:
            mask = self.importers(rel)  # unknown, never "unreached"
        why.append(f"lambda via {outer.kind if outer else '?'} -> {mask.bit_count()}")
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
            changed = set(range(o_lo, o_hi + 1))
            if len(rem_old) == len(rem_new):
                safe = all(same_outside_lambdas(a, b, changed) or stmt_safe(a, b, fut, known)
                           for a, b in zip(rem_old, rem_new))
            else:
                safe = all(stmt_inert(b, fut, known) for b in rem_new)
            if not safe:
                if self.runtime_checkable(rel, rem_old + rem_new):
                    # The engine imports the patched module once: if that works, nothing raised.
                    why.append(f"import-time {o_lo}-{o_hi} may raise -> importers unless it imports")
                    self.deferred |= self.importers(rel)
                else:
                    why.append(f"import-time {o_lo}-{o_hi} may raise -> importers")
                    mask |= self.importers(rel)
                continue
            # A value whose type changed can make other import-time code that reads it raise
            # (`B = A + 1` in a class body): those readers' modules break for every importer.
            retyped = set()
            for a, b in zip(rem_old, rem_new):
                if isinstance(a, (ast.Assign, ast.AnnAssign)) and _value_type(a.value) != _value_type(b.value):
                    retyped.update(bound_names(a))
            for name in sorted(retyped):
                for f, _key, kind in self.map.name_refs.get(name, ()):
                    if kind in ("class", "module"):
                        mask |= self.map.file_mask(f)
            if retyped:
                why.append(f"retyped {sorted(retyped)[:3]}: import-time readers' importers")
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

    @staticmethod
    def runtime_checkable(rel: str, stmts) -> bool:
        """Can the session's import of the patched module decide "may raise"? Not for edits
        of import statements (their outcome depends on import order), nor for files the
        engine does not import by name."""
        if not rel.startswith(("sqlglot/", "tests/")) or rel.rsplit("/", 1)[-1] == "conftest.py":
            return False
        for st in stmts:
            for sub in ast.walk(st):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    return False
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "__import__":
                    return False
        return True

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
                    and added.startswith("tests/") and removed.startswith("tests/") and _in_package(added)):
                # A test module moved unchanged: its import already ran in every session.
                why.append(f"moved {removed} -> {added} unchanged")
            else:
                mask |= self.added_file(added, content, fp.binary, why)
        return mask

    def removed_file(self, rel: str, why: list) -> int:
        if _is_config(rel):
            why.append(f"removed config {rel} -> ALL")
            return self.all
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
        if _is_config(rel):
            why.append(f"new config {rel} -> ALL")
            return self.all
        if rel.endswith(".py"):
            if base == "conftest.py" or "/" not in rel:
                # conftest: pytest loads it; root level: may shadow any import
                why.append(f"new {rel} -> ALL")
                return self.all
            if rel.startswith("sqlglot/"):
                return self.new_module(rel, content, why)
            if not _in_package(rel):
                # pytest puts a non-package test directory on sys.path (shadowing imports)
                why.append(f"new {rel} outside a package -> ALL")
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
        if rel.startswith("sqlglot/") and (binary or not rel.endswith(TEXT_SUFFIXES)):
            # an extension module (.so/.pyd) or a .pyc would shadow a module of the package
            why.append(f"new {rel} inside the package -> ALL")
            return self.all
        why.append(f"new {rel}: nothing opens it")
        return 0

    def new_module(self, rel: str, content, why: list) -> int:
        """A new module under sqlglot/. Dialects and optimizer passes are imported by computed
        names (`sqlglot.dialects.<key>`), so its name could be reached from any string.
        Only when that name occurs nowhere in sqlglot/ or tests/ (not as a word, not in a
        string) can nothing import it; then the observers are the directory listers:
        sqlglotc/setup.py (test_sqlglotc) and, for sqlglot/typing, test_typing."""
        parts = rel[: -len(".py")].split("/")
        names = {parts[-1]} if parts[-1] != "__init__" else set()
        names.add(parts[-2] if parts[-1] == "__init__" else parts[-1])
        if _is_test_file(rel.rsplit("/", 1)[-1]) or any(n.startswith("_") and n.endswith("_") for n in names):
            why.append(f"new {rel} -> ALL")
            return self.all
        words = source_words()
        if words is None or any(n in words for n in names) or (SRC / rel[: -len(".py")]).exists():
            why.append(f"new {rel}: its name is referenced (or shadows a package) -> ALL")
            return self.all
        mask = self.file_tests.get("tests/test_sqlglotc.py", 0)
        if rel.startswith("sqlglot/typing/"):
            mask |= self.file_tests.get("tests/test_typing.py", 0)
        why.append(f"new {rel}: name unreferenced -> directory listers {mask.bit_count()}")
        return mask

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
        if _is_config(rel) and not ("/" not in rel and rel in CONFIG_FILES):
            why.append(f"config {rel} -> ALL")
            return self.all
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
        self.case_sites: dict = {}  # (rel, code key) -> changed statement spans (TPC-DS per-query check)
        self.unverifiable = False
        self.deferred = 0  # importers needed unless the engine proves the patched modules import
        self.cmp_flip = None  # (rel, line, col, old op, new op): first comparison flip seen
        self.cmp_flips: list = []  # every flip of files whose only change is operator flips
        self.profile_dropped = 0  # candidates a value profile removed (checked by sentinels at run time)
        files = self.merge_repeats(parse_patch(text), why)
        self.data_masks: dict = {}
        self.fixture_runs: dict = {}  # setup-loaded fixture rel -> [class attr] when pairs line up
        for fp in files:
            if not fp.path and (fp.hunks or fp.binary):
                return self.all, ["patch entry without a path -> ALL"]
        blind = 0  # tests reached through changes no session applies (file-level, config)
        for fp in files:
            if fp.binary or fp.created or fp.deleted or fp.copy or fp.old_path != fp.new_path:
                fm = self.file_level(fp, why)
                blind |= fm
                mask |= fm
                continue
            rel = fp.path
            if not rel:
                continue
            if rel.endswith(".py"):
                mask |= self.python_file(fp, rel, why, single_file=len(files) == 1)
            elif _is_config(rel):
                fm = self.other_file(fp, rel, why)  # e.g. tests/pytest.ini: config, not a fixture
                blind |= fm
                mask |= fm
            elif rel.startswith("tests/"):
                old = self.old_source(rel)
                if old is None:
                    raise ValueError(f"missing base fixture: {rel}")
                new_text = apply_hunks(old, locate_hunks(old, fp.hunks))
                try:
                    new_text.encode("utf-8")
                except UnicodeEncodeError:
                    # Bytes that are not UTF-8 (kept as surrogates): tests/helpers opens
                    # fixtures with encoding="utf-8" and would raise, even on a comment line,
                    # while a session's patched open() would hand the text over. Every reader
                    # statically, no shortcut and no session.
                    m = self.map.opens.get(rel)
                    fm = self.map.masks[m] if m is not None else 0
                    if fm == 0:
                        fm = self.data_mask(rel, why)
                    why.append(f"{rel}: not UTF-8 -> readers {fm.bit_count()}")
                    blind |= fm
                    mask |= fm
                    continue
                if (FIXTURE_COMMENTS and rel.startswith("tests/fixtures/") and rel.endswith(".sql")
                        and _filter_comments(old) == _filter_comments(new_text)):
                    # .sql fixtures are only read through tests/helpers, which drop empty
                    # lines and "--" comments first: nothing a test sees has changed.
                    why.append(f"{rel}: only comments/blank lines changed -> nothing")
                    continue
                if (FIXTURE_COMMENTS and rel.startswith("tests/fixtures/") and rel.endswith(".sql")
                        and self.map.line_loaded is not None and rel not in self.map.line_loaded):
                    # Pair-loaded fixtures: '# title:' only names a subtest.
                    po, pn = _load_pairs(old), _load_pairs(new_text)
                    if po is not None and pn is not None and _strip_titles(po) == _strip_titles(pn):
                        why.append(f"{rel}: only fixture titles changed -> nothing")
                        continue
                self.data_changes[rel] = new_text
                m = self.map.opens.get(rel)
                fm = self.map.masks[m] if m is not None else 0
                fm = self.refine_fixture(rel, old, new_text, fm, why)
                self.data_masks[rel] = fm
                if fm == 0:
                    # nobody (left) reads it directly: every opener, else its directory's users
                    fm = self.data_mask(rel, why)
                else:
                    why.append(f"data {rel} -> {fm.bit_count()}")
                mask |= fm
            else:
                fm = self.other_file(fp, rel, why)
                blind |= fm
                mask |= fm
        if blind or self.unverifiable:
            # A session applies only the in-place .py and fixture edits: the rest of the tree
            # would be in its old state there, so tests reached through these changes could
            # pass and be dropped. Such commits are not narrowed at all: static set.
            if self.py_changes or self.data_changes:
                why.append("engine cannot apply every change -> static")
            mask |= self.deferred | self.profile_dropped
            self.py_changes, self.data_changes, self.fixture_runs = {}, {}, {}
            self.deferred = self.profile_dropped = 0
            self.case_site, self.case_sites = None, {}
        if self.unverifiable:
            self.py_changes = {}  # nothing the engine runs could stand in for the real import
        if not self.py_changes:
            mask |= self.deferred  # no session will import anything
            self.deferred = 0
        return mask, why

    def compare_narrow(self, rel: str, flips: list, mask: int, why: list) -> int:
        """This file's only change is operator flips of single-operator comparisons. Every
        changed file contributes its own set and the commit takes the union, so this is
        sound in multi-file patches too: consider a test in none of the other files'
        sets that saw no distinguishing evaluation at any flip here. Up to its first
        divergent evaluation, its run is the pinned run; that evaluation must be at a
        changed site; it is not in another file (that test would be in that file's set),
        and at a flip the operands are the pinned ones, on which old and new operators
        agree. So it never diverges. Keep this file's candidates whose pinned value
        profile shows a distinguishing evaluation at some flip (plus unprofiled tests)."""
        keep = self.map.cmp_unprofiled
        for line, col, old_op, new_op in flips:
            k = self.flip_keep(rel, line, col, old_op, new_op)
            if k is None:
                why.append(f"compare {old_op} -> {new_op} at {line}:{col}: no profile")
                return mask
            keep |= k
        narrowed = mask & keep
        self.profile_dropped |= mask & ~narrowed
        why.append(f"compare flips x{len(flips)} in {rel}: {mask.bit_count()} -> {narrowed.bit_count()}")
        return narrowed

    def flip_keep(self, rel, line, col, old_op, new_op):
        """Tests that may behave differently under this one operator flip, from the
        per-site narrower registered for its kind (SITE_NARROWERS); None = cannot narrow."""
        for kind_ops, narrower in SITE_NARROWERS:
            if old_op in kind_ops:
                return narrower(self.map, rel, line, col, old_op, new_op)
        return None

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
# pytest 9 also reads pytest.toml / .pytest.toml, and starts its config search in the test
# directory: these names at the root or anywhere under tests/ can change how tests run.
PYTEST_CONFIGS = CONFIG_FILES + ("pytest.toml", ".pytest.toml")


def _is_config(rel: str) -> bool:
    return rel.rsplit("/", 1)[-1] in PYTEST_CONFIGS and ("/" not in rel or rel.startswith("tests/"))
CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


TPCDS_WIDE = os.environ.get("RWX_TPCDS_WIDE", "0") == "1"


def _stmt_skeleton(tree) -> list:
    return [(type(n).__name__, n.lineno, n.end_lineno, n.col_offset) for n in ast.walk(tree) if isinstance(n, ast.stmt)]


def _encoding_marks(text: str) -> tuple:
    """BOM and coding cookies (first two lines): they decide how Python decodes the file."""
    head = text.split("\n", 2)[:2]
    return (text.startswith("\ufeff"),) + tuple(l for l in head if CODING_RE.match(l))


_SURROGATE_ESCAPES = re.compile("[\udc80-\udcff]")


def _undecodable(text: str) -> bool:
    """Bytes that were not UTF-8 (decoded with surrogateescape)."""
    return _SURROGATE_ESCAPES.search(text) is not None


def _import_time(site) -> bool:
    return (site is not None and site.stmt is not None and site.kind in ("import", "header")
            and isinstance(site.scope_node, (ast.Module, ast.ClassDef)))


CMP_PROFILE = os.environ.get("RWX_CMP_PROFILE", "1") == "1"
BOOL_PROFILE = os.environ.get("RWX_BOOL_PROFILE", "0") == "1"  # and/or flips (needs bool_sites)
BOOL_RHS_GUARD = os.environ.get("RWX_BOOL_RHS_GUARD", "0") == "1"  # and/or: no narrowing when the right operand may raise (off: see commit message)
CMP_OPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
           ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}
_ORD, _IDE, _MEM = ("<", "<=", ">", ">=", "==", "!="), ("is", "is not", "==", "!="), ("in", "not in")
# family recorded for each original operator (tools/lab/cmprun.py), in vector order
_ORD_RAISING = ("<", "<=", ">", ">=")  # raise together on unorderable operands
CMP_FAMILIES = {"<": _ORD, "<=": _ORD, ">": _ORD, ">=": _ORD, "==": _IDE, "!=": _IDE,
                "is": _IDE, "is not": _IDE, "in": _MEM, "not in": _MEM}


def _cmp_keep(cmap, rel, line, col, old_op, new_op):
    """Comparison sites (tools/lab/cmprun.py profiles): tests that evaluated the site on
    operands where old_op and new_op disagree (or raise / return non-bools), plus unprofiled."""
    if not CMP_PROFILE:
        return None
    entry = cmap.cmp_sites.get(rel, {}).get((line, col))
    if entry is None or entry[0] != old_op or cmap.cmp_unprofiled < 0:
        return None
    fam = CMP_FAMILIES.get(old_op)
    if fam is None or new_op not in fam:
        return None
    if old_op in _ORD_RAISING and new_op not in _ORD_RAISING:
        # An evaluation where old_op raised (None < 1, caught by sqlglot or the test) left
        # no record, but == / != would not raise there: the profile cannot see that test.
        return None
    i, j = fam.index(old_op), fam.index(new_op)
    keep = cmap.cmp_unprofiled
    for vec, mid in entry[1].items():
        if vec == "X" or len(vec) != len(fam) or vec[i] != vec[j] or "E" in (vec[i], vec[j]) or "X" in (vec[i], vec[j]):
            keep |= cmap.masks[mid]
    return keep


def _bool_keep(cmap, rel, line, col, old_op, new_op):
    """and/or sites (tools/lab/boolrun.py profiles): tests that saw an evaluation where the
    flip could change the result, plus unprofiled."""
    if not BOOL_PROFILE:
        return None
    entry = cmap.bool_sites.get(rel, {}).get((line, col))
    if entry is None or entry[0] != old_op or cmap.bool_unprofiled < 0:
        return None
    return cmap.masks[entry[1]] | cmap.bool_unprofiled


# Per-site narrowers for operator flips found by compare_flips(): (old operators handled,
# fn(map, rel, line, col, old_op, new_op) -> mask of tests to keep, or None = cannot narrow).
# Analyzer.compare_narrow() takes the union over a file's flips (any None: no narrowing).
SITE_NARROWERS = [
    (set(CMP_FAMILIES), _cmp_keep),
    ({"and", "or"}, _bool_keep),
]


def compare_flip(old_tree, new_tree):
    """(line, col, old op, new op) when the trees differ in exactly one operator of a
    single-operator comparison and nowhere else; None otherwise."""
    flips = compare_flips(old_tree, new_tree)
    return flips[0] if flips and len(flips) == 1 else None


def _bool_rhs_may_raise(tree, line, col) -> bool:
    """The right operand of the two-operand and/or at (line, col) could raise when evaluated
    (attribute lookups, ==/!= with a custom __eq__, ...). Only names, constants, `not` and
    is / is not between those count as unable to raise. Not found: may raise."""
    found = False
    for node in ast.walk(tree):
        # nested BoolOps can share a position: every one there must be safe
        if isinstance(node, ast.BoolOp) and node.lineno == line and node.col_offset == col:
            found = True
            if len(node.values) != 2:
                return True
            for sub in ast.walk(node.values[1]):
                if isinstance(sub, (ast.Name, ast.Constant, ast.Load, ast.Not, ast.Is, ast.IsNot)):
                    continue
                if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Not):
                    continue
                if isinstance(sub, ast.Compare) and all(isinstance(o, (ast.Is, ast.IsNot)) for o in sub.ops):
                    continue
                return True
    return not found


def compare_flips(old_tree, new_tree):
    """[(line, col, old op, new op), ...] when the trees differ only in operators of
    single-operator comparisons (one or more); None when anything else differs."""
    found = []

    def walk(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and len(b.ops) == 1 and type(a.ops[0]) is not type(b.ops[0]):
            if not (walk(a.left, b.left) and walk(a.comparators, b.comparators)):
                return False
            if type(a.ops[0]) not in CMP_OPS or type(b.ops[0]) not in CMP_OPS:
                return False
            found.append((a.lineno, a.col_offset, CMP_OPS[type(a.ops[0])], CMP_OPS[type(b.ops[0])]))
            return True
        if (isinstance(a, ast.BoolOp) and len(a.values) == 2 and len(b.values) == 2
                and type(a.op) is not type(b.op)):
            if not walk(a.values, b.values):
                return False
            names = {ast.And: "and", ast.Or: "or"}
            found.append((a.lineno, a.col_offset, names[type(a.op)], names[type(b.op)]))
            return True
        if isinstance(a, ast.AST):
            return all(walk(getattr(a, f), getattr(b, f)) for f in a._fields)
        if isinstance(a, list):
            return len(a) == len(b) and all(walk(x, y) for x, y in zip(a, b))
        return a == b

    return found if walk(old_tree, new_tree) and found else None


def _value_type(v):
    """Coarse runtime type of an assigned expression: literal type, or the node type."""
    if v is None:
        return None
    if isinstance(v, ast.Constant):
        return type(v.value)
    if isinstance(v, ast.UnaryOp) and isinstance(v.operand, ast.Constant):
        return type(v.operand.value)
    if isinstance(v, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Lambda)):
        return type(v)
    return ("expr", ast.dump(v))  # a lookup or computation: unknown, equal only if identical


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


_WORDS: list = []


def source_words():
    """Every identifier-like word in the pinned sqlglot/ and tests/ sources (lazy, once)."""
    if not _WORDS:
        words: set = set()
        try:
            if not (SRC / "sqlglot" / "__init__.py").is_file():
                raise FileNotFoundError(SRC / "sqlglot")
            for top in ("sqlglot", "tests"):
                for p in (SRC / top).rglob("*"):
                    if p.suffix in (".py", ".sql", ".json", ".txt", ".toml", ".cfg") and p.is_file():
                        words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", p.read_bytes().decode("utf-8", "replace")))
            _WORDS.append(words)
        except Exception as e:  # noqa: BLE001
            log(f"source scan failed: {e!r}")
            _WORDS.append(None)
    return _WORDS[0]


TEXT_SUFFIXES = (".md", ".txt", ".rst", ".typed", ".json", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".csv", ".sql")


def _in_package(rel: str) -> bool:
    """The file's directory is a package in the pinned tree (pytest imports by package name)."""
    d = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return bool(d) and (SRC / d / "__init__.py").is_file()


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


def _filter_comments(s):
    """tests/helpers._filter_comments, the first step of both fixture loaders."""
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _strip_titles(pairs):
    return [(tuple(kv for kv in meta if kv[0] != "title"), sql, exp) for meta, sql, exp in pairs]


def _load_pairs(text):
    """tests/helpers.load_sql_fixture_pairs on a string: [(meta, sql, expected)], or None
    if the real loader would raise."""
    try:
        lines = [line for line in text.splitlines() if line and not line.startswith("--")]
        statements = "\n".join(lines).split(";")
        out = []
        for i in range(0, len(statements), 2):
            if i + 1 < len(statements):
                sql = statements[i].strip()
                sql_lines = sql.split("\n")
                meta = {}
                j = 0
                while sql_lines[j].startswith("#"):
                    key, val = sql_lines[j].split(":", maxsplit=1)
                    meta[key.lstrip("#").strip()] = val.strip()
                    j += 1
                out.append((tuple(sorted(meta.items())), "\n".join(sql_lines[j:]), statements[i + 1].strip()))
        return out
    except Exception:  # noqa: BLE001
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
REF_LINES = os.environ.get("RWX_REF_LINES", "0") == "1"
META_STATIC = os.environ.get("RWX_META_STATIC", "0") == "1"  # metaclass reading only literal names: not broad
DEFAULT_CONST = os.environ.get("RWX_DEFAULT_CONST", "1") == "1"  # constant default-value edits: callers only  # class-attribute readers by line, not code object
FIXTURE_REFINE = os.environ.get("RWX_FIXTURE_REFINE", "1") == "1"
# Fixture-only commits editing a setup-loaded fixture (tpc-h.sql, tpc-ds.sql): run the
# loading class's tests on the edited queries instead of printing them unrun.
FIXTURE_RUN = os.environ.get("RWX_FIXTURE_RUN", "0") == "1"
FIXTURE_COMMENTS = os.environ.get("RWX_FIXTURE_COMMENTS", "1") == "1"
PRESETUP = os.environ.get("RWX_PRESETUP", "1") == "1"
WARM_NEEDED_ONLY = os.environ.get("RWX_WARM_NEEDED_ONLY", "0") == "1"
BET_CLEAN = os.environ.get("RWX_BET_CLEAN") == "1"
BET_FINISH_FRAC = float(os.environ.get("RWX_BET_FINISH_FRAC", "1.0"))  # bet: finish verifying only when this cheap
# RWX_KIND_PROBES=1: the probe sizes and the finishing share above apply only to commits whose
# Python edits are all one-token mutations (an operator flipped, a `not` dropped or added, a
# literal changed): the only code edits of the 70 practice and sample commits, on which the
# clean bet was never wrong. Every other kind of edit gets the strict values below. The lab's
# mutants show what the bet costs when a kind behaves differently (28 misses in 483 commits
# with the small probes, 14 with 1000/2000 ms, none left from the bet with the share at 2).
KIND_PROBES = os.environ.get("RWX_KIND_PROBES", "0") == "1"
STRICT_PROBE_MS = int(os.environ.get("RWX_STRICT_PROBE_MS", "1000"))
STRICT_PROBE2_MS = int(os.environ.get("RWX_STRICT_PROBE2_MS", "2000"))
STRICT_FINISH_FRAC = float(os.environ.get("RWX_STRICT_FINISH_FRAC", "2.0"))
_FLIP_OPS = frozenset("== != < <= > >= + - * / // % ** & | ^ << >> ~".split())
_FLIP_WORDS = frozenset(("and", "or", "not", "is", "in", "True", "False", "None"))


def _code_tokens(src: str) -> list:
    import io
    import tokenize as tk

    skip = (tk.COMMENT, tk.NL, tk.ENCODING, tk.ENDMARKER)
    out = []
    for t in tk.generate_tokens(io.StringIO(src).readline):
        if t.type in skip:
            continue
        if t.type in (tk.NEWLINE, tk.DEDENT):
            # statement structure counts (never mutable): a moved, added or removed
            # statement is not a one-token edit even when no other token changed
            out.append((t.type, "", False))
            continue
        mutable = (t.type == tk.OP and t.string in _FLIP_OPS) or (t.type == tk.NAME and t.string in _FLIP_WORDS) \
            or t.type in (tk.NUMBER, tk.STRING)
        out.append((t.type, t.string, mutable))
    return out


def one_token_edit(old_src: str, new_src: str, limit: int = 4) -> bool:
    """True when the two sources differ (comments and layout aside) in one short run of
    tokens made only of operators, and/or/not/is/in, True/False/None and literals."""
    try:
        a, b = _code_tokens(old_src), _code_tokens(new_src)
    except Exception:  # noqa: BLE001  (anything the tokenizer rejects is not a familiar edit)
        return False
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    j = 0
    while j < n - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
        j += 1
    am, bm = a[i:len(a) - j], b[i:len(b) - j]
    if not am and not bm:
        return False  # same tokens, yet the analyzer saw a change: not a token edit
    if len(am) > limit or len(bm) > limit:
        return False
    return all(t[2] for t in am) and all(t[2] for t in bm)


def familiar_edit(py_changes) -> bool:
    """Every changed Python file is a one-token mutation of its pinned source.
    py_changes: {rel: (old source, new source)}."""
    try:
        for pair in (py_changes or {}).values():
            old, new = pair
            if not isinstance(old, str) or not isinstance(new, str) or not one_token_edit(old, new):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False
LIVE_FINISH = os.environ.get("RWX_LIVE_FINISH", "1") == "1"  # sound: finish verifying live commits when cheaper
BET_LIVE = os.environ.get("RWX_BET_LIVE") == "1"  # experiment: drop unverified candidates on live commits  # experiment: clean after two passing probes
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
# Batch-wide settle budget, split up front so no commit's decisions depend on which other
# commits finished first: commit share = SETTLE_SCALE x worker cost of its candidate set
# (set by main(): budget per commit x commits / summed worker cost of the verifiable commits).
SETTLE_SCALE = [float("inf")]
SETTLE_MAX_MS = int(os.environ.get("RWX_SETTLE_MAX_MS", "4500"))  # settle may also run tests this costly (e.g. generator_memory)
SETTLE_FAIL_MULT = float(os.environ.get("RWX_SETTLE_FAIL_MULT", "1"))  # ratio multiplier once something failed; 0: no settling then


MAXNODES_PROXY = os.environ.get("RWX_MAXNODES_PROXY", "0") == "1"
PROXY_IDS = {"tests/test_parser.py::TestParser::test_max_nodes": "tests/test_parser.py::TestParser::test_max_nodes__rwx_proxy"}
PROXY_OF: dict = {}  # slow test number -> number of its stand-in (filled by main)


def policy(C: list[int], dur: dict, data_only: bool, focus: dict | None = None, known: set | None = None,
           dropped=None, proxy_ok: bool = True, cid=None, strict: bool = False):
    """The settled policy, then (RWX_MAXNODES_PROXY) a stand-in run for a slow test that is
    still printed unrun: when the scaled-down copy of the test passes on the patched tree,
    the slow test is left out. Evidence, not proof (like the other bets)."""
    final = yield from _policy_settled(C, dur, data_only, focus, known, dropped, cid, strict)
    if not (MAXNODES_PROXY and proxy_ok and PROXY_OF) or data_only:
        return final
    final = set(final)
    todo = [(real, p) for real, p in sorted(PROXY_OF.items()) if real in final and real not in (known or ())]
    if not todo:
        return final
    res = yield [p for _, p in todo], False
    for real, p in todo:
        if res.get(p) == "P":
            final.discard(real)
    return final


def _policy_settled(C: list[int], dur: dict, data_only: bool, focus: dict | None = None, known: set | None = None,
                    dropped=None, cid=None, strict: bool = False):
    """Generator: the batches of _policy, then (RWX_SETTLE_RATIO) a last batch of the printed
    runnable tests whose estimated CPU is worth less than the worker time they add."""
    gen = _policy(C, dur, data_only, focus, known, strict)
    seen: dict = {}
    try:
        req = next(gen)
        while True:
            res = yield req
            seen.update(res)
            req = gen.send(res)
    except StopIteration as e:
        final = set(e.value)
    if dropped and C and not data_only and PROFILE_SENTINEL:
        final = yield from _sentinels(C, final, seen, set(dropped), focus or {}, cid)
    if SETTLE_RATIO <= 0 or data_only or not seen:
        return final
    left_out = {g for g in C if g not in final and g not in seen and is_heavy(g)}
    allowance = SETTLE_SCALE[0] * _pack_lb(C) if SETTLE_SCALE[0] != float("inf") else float("inf")
    spent = 0
    settled: dict = {}
    chunk = SETTLE_CHUNK_MS
    for _ in range(SETTLE_ROUNDS):
        batch = _settle_batch(final, seen | settled, known)
        if not batch:
            break
        if batch_cost(batch) > 2 * chunk:
            # Grow in steps: results so far re-estimate the pass chance before spending more.
            cut = []
            for n in batch:
                if cut and batch_cost(cut + [n]) > chunk:
                    break
                cut.append(n)
            batch = cut
        chunk *= 2
        if spent + batch_cost(batch) > allowance:
            break  # this commit's share of the settle budget is spent: print the unproven tests
        spent += batch_cost(batch)
        res = yield batch, False
        settled.update({n: res.get(n, "F") for n in batch})
        final -= {n for n in batch if res.get(n) == "P"}
    if any(st != "P" for st in settled.values()):
        # A new failure: slow tests left out on clean evidence come back unless their own
        # test file showed all of its runnable candidates passing.
        allpass = {n for n, st in (seen | settled).items() if st == "P"}
        rest = [n for n in C if not is_heavy(n)]
        new_fail_files = {FILE_OF.get(n) for n, st in settled.items() if st != "P"}
        for g in left_out:
            if NARROW_ON or BET_CLEAN:
                # a bet left g out on its own rule (no failure in g's file): only a new
                # failure in g's own file overturns that
                if FILE_OF.get(g) in new_fail_files:
                    final.add(g)
                continue
            rel = [n for n in rest if FILE_OF.get(n) == FILE_OF.get(g)]
            if not rel or not all(n in allpass for n in rel):
                final.add(g)
    if SETTLE_GIANT_MS > 0:
        final = yield from _settle_giants(C, final, seen | settled, known)
    return final


# Giants in settle (RWX_SETTLE_GIANT_MS > 0, default off): run a printed slow test (est CPU
# above SETTLE_MAX_MS, at most SETTLE_GIANT_MS) when ratio x CPU < pass chance x the worker
# time it adds. Never a bet: the test runs. The guards only avoid spending CPU on a giant that
# probably fails: no failure seen in its own test file, at most BET_BREADTH of runs failed.
SETTLE_GIANT_MS = int(os.environ.get("RWX_SETTLE_GIANT_MS", "0"))
SETTLE_GIANT_BUDGET_MS = int(os.environ.get("RWX_SETTLE_GIANT_BUDGET_MS", "110000"))  # per batch (~3 giants)
HEAVY_BATCH: set = set()  # test numbers settle may run as a giant batch (the scheduler may serialize these)
GIANT_GRANT: dict = {}  # commit key -> est ms of giant runs granted (set by main, in job order)
EXEC_VERIFIABLE: set = set()  # commit keys whose TPC-DS the scheduler checks per query (cheaper)
EXECUTOR_TEST = [None]


def commit_key(C) -> tuple:
    return (len(C), hash(tuple(sorted(C))))


def giant_candidates(C) -> list:
    return [g for g in C if g not in NO_RUN and SETTLE_MAX_MS < batch_cost([g]) <= SETTLE_GIANT_MS]


def grant_giants(job_sets) -> None:
    """Split the giant budget in job order by each commit's potential giant runs, before any
    run, so no commit's decisions depend on scheduling order."""
    left = SETTLE_GIANT_BUDGET_MS
    for C in job_sets:
        need = sum(batch_cost([g]) for g in giant_candidates(C))
        if not need:
            continue
        grant = min(need, left)
        GIANT_GRANT[commit_key(C)] = grant
        left -= grant


def _settle_giants(C, final, seen, known):
    key = commit_key(C)
    allowance = GIANT_GRANT.get(key, float("inf") if not GIANT_GRANT else 0)
    while True:
        nfail = sum(st != "P" for st in seen.values())
        if nfail > BET_BREADTH * max(1, len(seen)):
            return final
        keep = 1 - (nfail + 1) / (len(seen) + 2) if nfail else 1.0
        bad_files = {FILE_OF.get(n) for n, st in seen.items() if st != "P"}
        eligible = []
        for g in giant_candidates(C):
            if g not in final or g in seen or g in (known or ()) or FILE_OF.get(g) in bad_files:
                continue
            if g == EXECUTOR_TEST[0] and key in EXEC_VERIFIABLE:
                continue  # the per-query TPC-DS check after the policy is cheaper
            eligible.append(g)
        # Worker time is not additive (one giant can hide another): value whole subsets.
        best = None
        base = _pack_lb(final)
        for mask in range(1, 1 << len(eligible)):
            group = [g for i, g in enumerate(eligible) if mask >> i & 1]
            cost = sum(batch_cost([g]) for g in group)
            if cost > allowance:
                continue
            value = keep ** len(group) * (base - _pack_lb(final - set(group))) - SETTLE_RATIO * cost
            if value > 0 and (best is None or value > best[0]):
                g = min(group, key=lambda n: (batch_cost([n]), n))
                best = (value, g, batch_cost([g]))
        if best is None:
            return final
        _, g, cost = best
        allowance -= cost
        res = yield [g], False
        seen = seen | {g: res.get(g, "F")}
        if res.get(g) == "P":
            final = final - {g}


SETTLE_CHUNK_MS = int(os.environ.get("RWX_SETTLE_CHUNK_MS", "2000"))  # first settle batch size (est), doubling
SETTLE_ROUNDS = 8


def set_settle_scale(commits: int, verifiable: list) -> None:
    """Split the batch's settle budget over the commits by worker cost, before any runs."""
    weight = sum(_pack_lb(C) for C in verifiable)
    SETTLE_SCALE[0] = SETTLE_BUDGET_PER_COMMIT_MS * commits / weight if weight else float("inf")


# Pollution sentinels (RWX_PROFILE_SENTINEL, default on). A value profile drops candidates R
# that cannot be infected directly, but an infected test can leave shared state behind that
# makes a later test fail in the same process. After the infected tests N have run in this
# session, run some of R (same class first, then same file, then a spread): if one fails, the
# profile is not trusted for this commit and R goes back to the unproven set. main() prints R
# unless this marked the commit cleared, so every fallback path (crash, cut, kill) keeps it.
PROFILE_SENTINEL = os.environ.get("RWX_PROFILE_SENTINEL", "1") == "1"
SENTINEL_INFECTED_MS = int(os.environ.get("RWX_SENTINEL_INFECTED_MS", "3000"))
SENTINEL_CLASS_MS = int(os.environ.get("RWX_SENTINEL_CLASS_MS", "3000"))
SENTINEL_FILE_MS = int(os.environ.get("RWX_SENTINEL_FILE_MS", "1500"))
SENTINEL_SPREAD_MS = int(os.environ.get("RWX_SENTINEL_SPREAD_MS", "500"))
SENTINEL_CLEARED: set = set()  # commit keys whose sentinels all passed
FRESH_PROCESS: set = set()  # tests that run their checks in a fresh interpreter (test_lazy_load)


def _sentinels(C, final, seen, R, focus, cid=None):
    if all(n in NO_RUN or n in FRESH_PROCESS for n in C):
        # the only infected tests start their own interpreter: nothing in this process is touched
        SENTINEL_CLEARED.add(("cid", cid) if cid is not None else commit_key(C))
        return final
    seen = dict(seen)
    infected_unrun = [n for n in C if n not in seen and not is_heavy(n)]
    if infected_unrun and batch_cost(infected_unrun) <= SENTINEL_INFECTED_MS:
        # the potential polluters must have run before the sentinels
        res = yield sorted(infected_unrun, key=lambda n: (batch_cost([n]), n)), False
        seen.update({n: res.get(n, "F") for n in infected_unrun})
        final = final - {n for n in infected_unrun if res.get(n) == "P"}
    if any(n not in seen for n in C if not is_heavy(n)):
        return final | R  # not every runnable infected test ran: R stays unproven
    runnable = sorted((n for n in R if not is_heavy(n)), key=lambda n: (batch_cost([n]), n))
    classes = {CLS_OF.get(n) for n in C}
    files = {FILE_OF.get(n) for n in C}
    picks: list = []

    def take(pool, budget):
        start = batch_cost(picks)
        for n in pool:
            if n not in picks and batch_cost(picks + [n]) - start <= budget:
                picks.append(n)

    take([n for n in runnable if CLS_OF.get(n) in classes], SENTINEL_CLASS_MS)
    take([n for n in runnable if FILE_OF.get(n) in files], SENTINEL_FILE_MS)
    picks += _spread([n for n in runnable if n not in picks], SENTINEL_SPREAD_MS, 2, focus=focus)
    if not picks:
        return final | R
    res = yield picks, True
    if all(res.get(n) == "P" for n in picks):
        SENTINEL_CLEARED.add(("cid", cid) if cid is not None else commit_key(C))
        return final
    return final | (R - {n for n in picks if res.get(n) == "P"})


def _settle_batch(final, seen, known):
    """The printed, never-run tests worth running: the prefix (by recorded time per CPU) that
    minimises ratio x CPU + expected worker cost of what stays printed."""
    ratio = SETTLE_RATIO
    nfail = sum(st != "P" for st in seen.values())
    keep = 1.0  # chance an unproven test passes, so that running it removes it
    if nfail:
        if SETTLE_FAIL_MULT <= 0:
            return []
        ratio *= SETTLE_FAIL_MULT
        keep = 1 - (nfail + 1) / (len(seen) + 2)  # Laplace estimate from this commit's runs
    # Tests sharing a class with a seen failure usually fail too: running them buys nothing.
    bad_cls = {CLS_OF.get(n) for n, st in seen.items() if st != "P"}
    unproven = [n for n in final if n not in seen and n not in NO_RUN and n not in (known or ())
                and (not is_heavy(n) or batch_cost([n]) <= SETTLE_MAX_MS) and CLS_OF.get(n) not in bad_cls]
    if not unproven:
        return []
    order = sorted(unproven, key=lambda n: (-DUR[n] / (batch_cost([n]) + 1), n))
    best0 = best = _pack_lb(final)
    best_j = 0
    steps = sorted(set(range(1, min(SETTLE_PREFIX, len(order)) + 1))
                   | {len(order) * i // 10 for i in range(1, 11) if len(order) * i // 10 > SETTLE_PREFIX})
    for j in steps:
        c = ratio * batch_cost(order[:j]) + keep * _pack_lb(final - set(order[:j])) + (1 - keep) * best0
        if c < best:
            best, best_j = c, j
    return order[:best_j]


def _policy(C: list[int], dur: dict, data_only: bool, focus: dict | None = None, known: set | None = None,
            strict: bool = False):
    """Generator: yields (tests, stop_first) batches, receives {n: 'P'|'F'}, returns the final set.

    Passing probes can clear a giant on evidence from its test file. A clean
    run of every runnable candidate can also clear the other heavy tests."""
    if not C:
        return set()
    if data_only:
        return set(C)  # Fixture edits and patches the engine cannot apply stay static.
    if known:
        # Tests reading an edited data file: included as they are, never run here.
        rest_gen = _policy([n for n in C if n not in known], dur, False, focus, None, strict)
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
    return (yield from _probe_then_decide(C, rest, heavy, dur, focus or {}, strict))


def fixture_policy(C: list[int], runnable: list[int]):
    """Fixture-only commit: run the loading class's tests on the edited queries; the
    other readers stay in unrun. A test that does not report a pass stays in."""
    res = yield sorted(runnable), False
    return {n for n in C if n not in runnable or res.get(n) != "P"}


FILE_OF: dict = {}
NO_RUN: set = set()  # tests that start a fresh interpreter: the engine cannot patch it
NO_RUN_IDS = ("tests/dialects/test_dialect.py::TestDialect::test_lazy_load",)
LAZY_RUN = os.environ.get("RWX_LAZY_RUN") == "1"  # run it anyway: the engine hooks `python -c`
LAZY_RUN_MS = 400  # a fresh interpreter importing sqlglot, patched module compiled from source


PROBE2_MS = int(os.environ.get("RWX_PROBE2_MS", "250"))
PROBE_ORDER = os.environ.get("RWX_PROBE_ORDER", "focus")
RELATED_EVIDENCE_MS = int(os.environ.get("RWX_RELATED_EVIDENCE_MS", "1200"))
RELATED_ALL_MS = int(os.environ.get("RWX_RELATED_ALL_MS", "4000"))
# giants whose own test file must run clean before they can be dropped (empty: all giants)
EVIDENCE_FILES = {f for f in os.environ.get("RWX_EVIDENCE_FILES", "").split(",") if f}
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


def _probe_then_decide(C, rest, heavy, dur, focus, strict=False):
    probe_ms, probe2_ms, finish_frac = PROBE_MS, PROBE2_MS, BET_FINISH_FRAC
    if strict:
        probe_ms, probe2_ms = max(probe_ms, STRICT_PROBE_MS), max(probe2_ms, STRICT_PROBE2_MS)
        finish_frac = max(finish_frac, STRICT_FINISH_FRAC)
    probe = _spread(rest, probe_ms, 2, focus=focus)
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
        if EVIDENCE_FILES and gf not in EVIDENCE_FILES:
            # the lab never saw this giant fail once the first probe passed
            evidenced.add(g)
            continue
        related = [n for n in remaining if FILE_OF.get(n, "") == gf and n not in set(probe2)]
        if not related:
            continue
        if batch_cost(probe2 + related) - batch_cost(probe2) <= RELATED_ALL_MS:
            probe2 += related
            evidenced.add(g)
    probe2 += _spread([n for n in remaining if n not in set(probe2)], batch_cost(probe2) + probe2_ms, 4, already=probe2, focus=focus)
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
        # Both probes passed. If finishing the verification is affordable, finish it;
        # only when it is not do we bet that the commit is clean.
        mandatory = set(heavy) & NO_RUN
        remaining = [n for n in rest if n not in passed]
        savings = _pack_cost(set(C) - passed) - _pack_cost(mandatory)
        if remaining and batch_cost(remaining) <= finish_frac * savings:
            final = yield remaining, True
            failed3 = {n for n, st in final.items() if st == "F"}
            clean = {n for n, st in final.items() if st == "P"}
            if failed3 or len(clean) < len(remaining):
                return (yield from _live(C, rest, heavy, passed | clean, failed3))
        return mandatory
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
NARROW_ON = os.environ.get("RWX_NARROW") == "1"
NARROW_FULL_MS = int(os.environ.get("RWX_NARROW_FULL_MS", "4000"))
NARROW_CHUNKS = os.environ.get("RWX_NARROW_CHUNKS", "1") == "1"
NARROW_SAMEFILE = os.environ.get("RWX_NARROW_SAMEFILE") == "1"
NARROW_CHUNK_MS = int(os.environ.get("RWX_NARROW_CHUNK_MS", "600"))  # experiment: off (cost more than it saved in the lab)


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
    if BET_FILE_N or BET_CLASS_N or BET_CONFINED:
        return (yield from _live_bet(C, rest, heavy, passed, failed))
    if LIVE_FILE_FRAC > 0 or LIVE_REST_FRAC > 0:
        return (yield from _live_evidence(C, rest, heavy, passed, failed))
    skippable = [n for n in heavy if dur[n] >= NARROW_SKIP_MS and n not in NO_RUN]
    if not NARROW_ON or not skippable:
        if LIVE_FINISH or BET_LIVE:
            # Something failed. Finish verifying the runnable candidates when that costs
            # less than printing them unverified; only then (or under the live bet)
            # print just what failed plus the tests that cannot be run here.
            remaining = [n for n in rest if n not in passed and n not in failed]
            keep = set(failed) | set(heavy)
            savings = _pack_cost(set(C) - passed) - _pack_cost(keep)
            if remaining and LIVE_FINISH and batch_cost(remaining) <= FINAL_FRAC * savings:
                res = yield remaining, False
                failed = set(failed) | {n for n, st in res.items() if st == "F"}
                unreported = {n for n in remaining if n not in res}
                return set(failed) | set(heavy) | unreported
            if BET_LIVE:
                return set(failed) | set(heavy)
        return set(C) - passed
    done = passed | failed
    unrun = [n for n in rest if n not in done]
    if unrun and batch_cost(unrun) <= NARROW_FULL_MS:
        # Full knowledge of the runnable candidates, then decide the slow tests on it.
        # Run in chunks, the slow tests' own files first, and stop as soon as the
        # answer cannot change: every droppable test already has a failure in its
        # file, or failures are already broad whatever the rest would show.
        sfiles = {FILE_OF.get(g, "") for g in skippable}
        if NARROW_SAMEFILE:
            # only the slow tests' own files decide; the rest is printed unverified
            others = [n for n in unrun if FILE_OF.get(n, "") not in sfiles]
            same = [n for n in unrun if FILE_OF.get(n, "") in sfiles]
            if same:
                res = yield same, False
                passed = passed | {n for n, st in res.items() if st == "P"}
                failed = failed | {n for n in same if res.get(n) != "P"}
            fail_files = {FILE_OF.get(n, "") for n in failed}
            drop = {g for g in skippable if FILE_OF.get(g, "") not in fail_files}
            return set(failed) | set(others) | (set(heavy) - drop)
        order = sorted(unrun, key=lambda n: (FILE_OF.get(n, "") not in sfiles, CPU_EST.get(n, dur[n]), n))
        total = len(passed) + len(failed) + len(order)
        while order:
            if NARROW_CHUNKS:
                chunk, budget = [], max(NARROW_CHUNK_MS, batch_cost(order[:1]))
                while order and batch_cost(chunk + order[:1]) <= budget:
                    chunk.append(order.pop(0))
                # keep a file's tests together with the slow tests' files first
            else:
                chunk, order = order, []
            res = yield chunk, False
            passed = passed | {n for n, st in res.items() if st == "P"}
            failed = failed | {n for n in chunk if res.get(n) != "P"}
            if not order:
                break
            fail_files = {FILE_OF.get(n, "") for n in failed}
            if all(FILE_OF.get(g, "") in fail_files for g in skippable) or len(failed) >= BREADTH * total:
                # nothing can be dropped any more: keep every slow test, unverified rest included
                return set(C) - passed
        breadth = len(failed) / max(1, len(passed) + len(failed))
        fail_files = {FILE_OF.get(n, "") for n in failed}
        drop = set()
        if breadth < BREADTH:
            drop = {g for g in skippable if FILE_OF.get(g, "") not in fail_files}
        return set(failed) | (set(heavy) - drop)
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


# BETS (lab measurement only, all off by default; each can create misses):
#   RWX_BET_FILE_N=all|N    after a failure, drop a slow test (>= SKIP_MIN_MS recorded) when no
#                           runnable candidate of its own test file failed and all / at least N
#                           of them ran and passed (missing ones are run first)
#   RWX_BET_CLASS_N=all|N   the same for mid-weight heavy tests (2 s to SKIP_MIN_MS) by test class
#   RWX_BET_CONFINED=file|class  501+ candidates, failures confined to one file/class: print only
#                           the failures and that file/class (plus slow tests kept by the rules above)
BET_FILE_N = os.environ.get("RWX_BET_FILE_N", "")
BET_CLASS_N = os.environ.get("RWX_BET_CLASS_N", "")
BET_CONFINED = os.environ.get("RWX_BET_CONFINED", "")
BET_MID_MS = 2000
BET_BREADTH = float(os.environ.get("RWX_BET_BREADTH", "0.25"))  # no bet drops when more than this share of runs failed


def _live_bet(C, rest, heavy, passed, failed):
    passed, failed = set(passed), set(failed)
    targets = []
    for g in heavy:
        if g in NO_RUN:
            continue
        if BET_FILE_N and DUR[g] >= SKIP_MIN_MS:
            targets.append((g, FILE_OF, BET_FILE_N))
        elif BET_CLASS_N and BET_MID_MS <= DUR[g] < SKIP_MIN_MS:
            targets.append((g, CLS_OF, BET_CLASS_N))
    plans, batch = {}, []
    for g, group_of, rule in targets:
        rel = [n for n in rest if group_of.get(n) == group_of.get(g)]
        if not rel or any(n in failed for n in rel):
            continue
        need = len(rel) if rule == "all" else int(rule)
        if need > len(rel):
            continue
        have = [n for n in rel if n in passed]
        todo = sorted((n for n in rel if n not in passed and n not in batch), key=lambda n: (batch_cost([n]), n))
        todo = todo[: max(0, need - len(have))] if rule != "all" else todo
        if todo and batch_cost(batch + todo) - batch_cost(batch) > RELATED_ALL_MS:
            continue
        batch += todo
        plans[g] = (rel, need)
    if batch:
        res = yield batch, False
        passed |= {n for n, st in res.items() if st == "P"}
        failed |= {n for n in batch if res.get(n) != "P"}
    drop = {g for g, (rel, need) in plans.items()
            if not any(n in failed for n in rel) and sum(n in passed for n in rel) >= need}
    if len(failed) > BET_BREADTH * max(1, len(passed | failed)):
        drop = set()  # broad breakage: slow tests fail with it (lab: giants fail on broad mutations)
    selected = set(C) - passed - drop
    if BET_CONFINED and len(C) > 500 and failed:
        group_of = CLS_OF if BET_CONFINED == "class" else FILE_OF
        groups = {group_of.get(n) for n in failed}
        if len(groups) == 1:
            # heavy tests outside the group stay unless a rule above dropped them
            selected = {n for n in selected if n in failed or group_of.get(n) in groups or n in heavy}
    return selected


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


# Per-query TPC-DS check after the policy: with RWX_TPCDS_SETTLE the settle rule decides (ratio x
# CPU < worker time saved) instead of "saving >= 3 x CPU"; pair it with RWX_TPCDS_VERIFY_MS (engine)
# to let sessions prepare larger affected-query sets. The gate (TPC-DS already left out) keeps
# its old 5 s bound so a larger verify cap does not add CPU to clean commits.
TPCDS_SETTLE = os.environ.get("RWX_TPCDS_SETTLE", "0") == "1"
TPCDS_BLIND = int(os.environ.get("RWX_TPCDS_BLIND_MS", "0")) > 0  # evidence gate when the affected queries are unknown
# The evidence gate never clears TPC-DS when this many other tests of its file were seen
# failing (0 = no guard). Lab truth, commits where another executor test fails: with 1 to 3
# failing TPC-DS passes on 9 of 12, with 4 or more it fails on 24 of 29; and on
# s1/m41010113 (11 failing) every query passes when the test runs alone.
TPCDS_BLIND_GUARD = int(os.environ.get("RWX_TPCDS_BLIND_GUARD", "4"))
# Commits with an unfamiliar kind of edit (RWX_KIND_PROBES) keep the guard even when the
# build turns it off: the gate has only been seen clearing TPC-DS correctly on one-token
# mutations (practice, samples, lab).
STRICT_BLIND_GUARD = int(os.environ.get("RWX_STRICT_BLIND_GUARD", "4"))
STRICT_CIDS: dict = {}  # cid -> _LazyStrict: whether the commit runs with the strict settings


class _LazyStrict:
    """Truthy when the commit's edit is not familiar. Decided on first use (the probe step
    or the TPC-DS gate), so commits that never get there never tokenize their files
    (sqlglot/parser.py: about 0.1 s for both copies)."""

    def __init__(self, cid, py):
        self.cid, self.py, self.value = cid, py, None

    def __bool__(self):
        if self.value is None:
            self.value = not familiar_edit(self.py)
            if self.value:
                log(f"{self.cid}: unfamiliar kind of edit -> strict probes")
        return self.value


def _blind_guard(cid) -> int:
    if STRICT_CIDS.get(cid) and STRICT_BLIND_GUARD:
        return min(TPCDS_BLIND_GUARD, STRICT_BLIND_GUARD) if TPCDS_BLIND_GUARD else STRICT_BLIND_GUARD
    return TPCDS_BLIND_GUARD


TPCDS_GATE_MS = int(os.environ.get("RWX_TPCDS_GATE_MS", "5000"))
HARD_GRACE_S = 8.0  # after the scheduling deadline, in-flight batches get this long, then die
# Progress watchdog: a batch is hung when no result line arrived for max(WATCH_MIN_S,
# WATCH_FACTOR x (estimate of the test running now + its class setup)); a fresh child
# first gets max(4 s, 8 x estimated start-up) for patching and its import check; silence
# past that in a re-import session is an import-time hang, reported as an import
# failure (every test that loads a changed module fails, as pytest would report the
# importers of a module whose import never returns). Only the running test
# counts as failing; the batch's later tests continue in a fresh session of the same
# commit, at most WATCH_RESTARTS times per commit (never for a child silent from start).
WATCH_FACTOR = 8.0
WATCH_RESTARTS = 2


class Scheduler:
    def __init__(self, engine, lanes: int, deadline: float):
        self.engine = engine
        self.lanes = lanes
        self.deadline = deadline

    def _windows(self, nums, sess=None):
        """Per-test watchdog windows, or None (plain batch timeout) for engines without
        run_order (test doubles)."""
        from engine import WATCH_MIN_S

        run_order = getattr(self.engine, "run_order", None)
        if run_order is None:
            return None
        # Class setup counts for every test of the class: a deferred setup runs inside
        # whichever test first reads its data. A re-import session builds the classes
        # afresh, so it may pay the map's full setup (WATCH_SETUP). A swap session uses
        # the parent's shared setup where there is one (CLS_SETUP 0) but still opens its
        # duckdb connections lazily (about half of that setup): count half of what was shared.
        swap = getattr(sess, "mode", None) == "swap"

        def setup(c):
            full = WATCH_SETUP.get(c, 0)
            return (CLS_SETUP.get(c, 0) + full) / 2 if swap else full

        return {n: max(WATCH_MIN_S, WATCH_FACTOR * (CPU_EST.get(n, DUR.get(n, 0)) + setup(CLS_OF.get(n))) / 1000)
                for n in run_order(nums)}

    def _submit(self, a, nums, stop_first):
        """Send a policy batch to the commit's session under the progress watchdog."""
        a[6]["stop_first"] = stop_first
        a[6]["carry"] = {}
        a[4] = nums
        self._send(a[3], nums, stop_first)

    def _send(self, sess, nums, stop_first):
        windows = self._windows(nums, sess)
        if windows is None:
            sess.submit(nums, timeout=batch_timeout(nums), stop_first=stop_first)
        else:
            sess.submit(nums, timeout=batch_timeout(nums), stop_first=stop_first, windows=windows)

    def _import_hang(self, cid, py):
        """The patched tree never finished importing: every importer of a changed module fails."""
        mask = 0
        try:
            for rel in py:
                mask |= CMAP[0].file_mask(rel) if CMAP else 0
        except Exception as e:  # noqa: BLE001
            log(f"{cid}: importer mask failed {e!r}"[:200])
        IMPORT_OK[cid] = False
        DEFERRED[cid] = DEFERRED.get(cid, 0) | mask
        log(f"{cid}: no import check line: import-time hang, {mask.bit_count()} importers fail")

    def _restart(self, a) -> bool:
        """Replace a hung (killed) session by a fresh one of the same commit, if allowed."""
        ctx = a[6]
        if not ctx.get("hung") or ctx["restarts"] >= WATCH_RESTARTS or time.monotonic() > self.deadline:
            return False
        try:
            sess = self.engine.session(ctx["py"], ctx["data"], ctx["site"], ctx.get("fixture"))
        except Exception as e:  # noqa: BLE001
            log(f"{a[0]}: restart failed {e!r}"[:300])
            return False
        ctx["restarts"] += 1
        ctx["hung"] = False
        a[3] = sess
        return True

    def _abandon(self, a):
        """Kill an active commit's session; its final set treats the in-flight batch as
        unproven (reported passes still count)."""
        cid, C, gen, sess, batch, selected = a[:6]
        carry = a[6].get("carry", {}) if len(a) > 6 else {}
        partial = {n: st for n, st in {**carry, **getattr(sess, "results", {})}.items() if st == "P"}
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
                job = queue.pop(0)
                cid, C, py, data, data_only, focus, known, case_site = job[:8]
                fixture = job[8] if len(job) > 8 else None
                if fixture:
                    gen = fixture_policy(C, fixture[1])
                else:
                    extra = [job[9]] if len(job) > 9 and job[9] else []
                    kw = {}
                    if MAXNODES_PROXY and any(rel.startswith("tests/") for rel in (py or ())):
                        kw["proxy_ok"] = False  # the patch edits test code: the stand-in copies the pinned test
                    if KIND_PROBES and py:
                        kw["strict"] = STRICT_CIDS[cid] = _LazyStrict(cid, py)
                    gen = policy(C, DUR, data_only, focus, known, *extra, cid=cid, **kw)
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
                    fx = fixture[0] if fixture else None
                    sess = self.engine.session(py, data, case_site, fx)
                    if getattr(sess, "executor_ms", None) is not None and getattr(sess, "executor_partial", True) is False:
                        EXEC_VERIFIABLE.add(commit_key(C))
                    a = [cid, C, gen, sess, batch, None,
                         {"py": py, "data": data, "site": case_site, "fixture": fx, "restarts": 0, "hung": False}]
                    if fixture:
                        # fixture runs: the per-query tests get their own time limit
                        a[6]["stop_first"], a[6]["carry"] = stop_first, {}
                        sess.submit(batch, timeout=fixture_timeout(fixture, self.engine), stop_first=stop_first)
                    else:
                        self._submit(a, batch, stop_first)
                except Exception as e:  # one commit's trouble must not stop the others
                    log(f"{cid}: session failed {e!r}"[:300])
                    gen.close()
                    yield cid, set(C), "session failed"
                    continue
                active.append(a)
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
                    cid, C, gen, sess, batch, selected = a[:6]
                    ctx = a[6]
                    finished = False
                    hung_res = None
                    if sess in ready:
                        finished = sess.read()
                    import_ok = getattr(sess, "import_ok", None)
                    if import_ok is not None and cid not in IMPORT_OK:
                        IMPORT_OK[cid] = import_ok
                    if not finished and sess.expired():
                        hung = sess.next_unreported() if getattr(sess, "windows", None) is not None else None
                        if hung is None:
                            log(f"{cid}: batch timed out after {len(sess.results)}/{len(batch)} tests")
                            sess.kill()
                        else:
                            # The test running now is hung: it fails, reported results stand.
                            hung_res = {**ctx["carry"], **sess.results, hung: "F"}
                            progressed = sess.progressed
                            rest = [] if ctx["stop_first"] else [n for n in sess.order if n not in hung_res]
                            sess.kill()
                            sess.close()
                            ctx["hung"] = progressed  # a child silent from the start would hang again
                            if not progressed and getattr(sess, "mode", None) == "reimport" and ctx["py"]:
                                self._import_hang(cid, ctx["py"])
                            log(f"{cid}: test {hung} hung after {len(hung_res) - 1} results, {len(rest)} left in the batch")
                            if rest:
                                if self._restart(a):
                                    self._send(a[3], rest, False)
                                    ctx["carry"] = hung_res
                                    a[4] = rest
                                    continue
                                # no restart left: the rest of the batch stays unproven
                                final = _drain(gen, hung_res)
                                active.remove(a)
                                yield cid, final if final is not None else set(C), "hung: rest of the batch unproven"
                                continue
                        finished = True
                    if not finished:
                        continue
                    if hung_res is not None:
                        res = hung_res
                    else:
                        res = {**ctx["carry"], **sess.results}
                        if not sess.alive:
                            # crashed or killed: whatever did not report counts as failing
                            for n in batch:
                                res.setdefault(n, "F")
                    if any(st == "F" for st in res.values()):
                        LIVE.add(cid)
                    OBSERVED.setdefault(cid, set()).update(n for n, st in res.items() if st == "F")
                    executor = self.engine.executor_test
                    if selected is not None:
                        kind, selected = selected
                        if kind == "verify":
                            # every affected query was run: a pass clears TPC-DS
                            if sess.alive and res.get(executor) == "P":
                                selected = selected - {executor}
                            note = "checked affected TPC-DS queries"
                            note += f" ({'pass' if executor not in selected else 'FAIL'}, {sess.executor_ms} ms{', blind' if getattr(sess, 'executor_blind', False) else ''}{', partial' if sess.executor_partial else ''})"
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
                        if not sess.alive and self._restart(a):
                            sess = a[3]
                        if not sess.alive and hung_res is not None:
                            # hung, no restart left: the requested batch stays unproven
                            final = _drain(gen)
                            active.remove(a)
                            yield cid, final if final is not None else set(C), "hung: no restart left"
                            continue
                        if time.monotonic() > self.deadline or not sess.alive:
                            raise GeneratorExit
                        if time.monotonic() + batch_cost(nxt) / 1000 > self.deadline:
                            # No time for this batch: the policy settles with it unreported.
                            final = _drain(gen)
                            sess.close()
                            active.remove(a)
                            yield cid, final if final is not None else set(C), "cut: no time for the next batch"
                            continue
                        self._submit(a, nxt, stop_first)
                    except StopIteration as e:
                        if (TPCDS_BLIND and executor in e.value and sess.executor_ms is not None
                                and sess.executor_partial
                                and not (_blind_guard(cid) and sum(
                                    FILE_OF.get(n) == FILE_OF.get(executor) for n in OBSERVED.get(cid, ())
                                ) >= _blind_guard(cid))
                                and sess.alive and executor not in res and time.monotonic() < self.deadline):
                            # Bet with evidence: the cheapest TPC-DS queries run on the patched
                            # tree; a pass leaves the full test out.
                            cpu = sess.executor_ms + CLS_SETUP.get(CLS_OF.get(executor), 0)
                            sess.submit([executor], timeout=10.0 + 3.0 * cpu / 1000, executor_only=True)
                            ctx["carry"] = {}
                            a[4], a[5] = [executor], ("verify", e.value)
                            continue
                        if (executor in e.value and sess.executor_ms is not None and not sess.executor_partial
                                and sess.alive and executor not in res and time.monotonic() < self.deadline):
                            cpu = sess.executor_ms + CLS_SETUP.get(CLS_OF.get(executor), 0)
                            saving = _pack_cost(e.value) - _pack_cost(e.value - {executor})
                            if saving >= 3 * cpu or (TPCDS_SETTLE and SETTLE_RATIO > 0 and SETTLE_RATIO * cpu < saving):
                                sess.submit([executor], timeout=2.0 + 2.0 * cpu / 1000, executor_only=True)
                                ctx["carry"] = {}
                                a[4], a[5] = [executor], ("verify", e.value)
                                continue
                        if (executor is not None and executor in C and executor not in e.value
                                and sess.executor_ms is not None and sess.executor_ms <= TPCDS_GATE_MS
                                and not (TPCDS_BLIND and sess.executor_partial)
                                and sess.alive and executor not in res and time.monotonic() < self.deadline):
                            cpu = sess.executor_ms + CLS_SETUP.get(CLS_OF.get(executor), 0)
                            sess.submit([executor], timeout=10.0 + 3.0 * cpu / 1000, executor_only=True)
                            ctx["carry"] = {}
                            a[4], a[5] = [executor], ("gate", e.value)
                            continue
                        sess.close()
                        active.remove(a)
                        _dump_cpu(cid, sess)
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

def _dump_cpu(cid, sess):
    """Lab diagnostics (RWX_CPU_DUMP=file): one JSON line per finished session."""
    from engine import CPU_DUMP

    if not CPU_DUMP:
        return
    try:
        with open(CPU_DUMP, "a") as f:
            f.write(json.dumps({"cid": cid, "mode": sess.mode, "total": sess.total_cpu,
                                "startup": sess.startup_cpu, "tests": sess.cpu}) + "\n")
    except Exception as e:  # noqa: BLE001
        log(f"cpu dump failed {e!r}")


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
DEFERRED: dict = {}  # commit -> importers mask needed unless its session imported the patch cleanly
IMPORT_OK: dict = {}  # commit -> the session's import check result (True/False)
CMAP: list = []  # [the Map], for importer masks found at run time
LIVE: set = set()  # commits where some run reported a failure
OBSERVED: dict = {}  # commit -> tests a run reported failing (engine-run commits only)
CPU_EST: dict = {}  # per-test body CPU (ms), class setup excluded
CLS_OF: dict = {}  # test number -> "file::Class"
CLS_SETUP: dict = {}  # "file::Class" -> setup CPU (ms)
WATCH_SETUP: dict = {}  # the map's class setup CPU, also where shared/deferred setup can land
TEST_ID_OF: dict = {}  # test number -> full test id


def class_masks(tests) -> dict:
    out: dict = {}
    for t in tests:
        k = t["id"].rsplit("::", 1)[0]
        out[k] = out.get(k, 0) | (1 << t["number"])
    return out


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


def safe_pack(nums: list, dur: dict) -> list:
    """pack(), or a plain round-robin split if packing fails (output must always be valid)."""
    try:
        return pack(nums, dur)
    except Exception as e:  # noqa: BLE001
        log(f"pack failed: {e!r}")
        return [w for w in (nums[i::MAX_WORKERS] for i in range(MAX_WORKERS)) if w]


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


EXEC_CLS = "tests/test_executor.py::TestExecutor"
PER_QUERY = {"test_execute_tpch": "tpch_sqls", "test_optimized_tpch": "tpch_sqls", "test_execute_tpcds": "tpcds_sqls"}


def fixture_timeout(fixture, engine) -> float:
    """Expected CPU of the edited queries, with slack. An edit can make a query far slower
    (a dropped join condition); a timeout leaves the unreported tests in."""
    spec, runnable, edits = fixture
    by_attr = {attr: edits[fx] for fx, attr in spec}
    ms = (engine.executor_profile or {}).get("ms") or []
    est = CLS_SETUP.get(EXEC_CLS, 0)
    for n in runnable:
        attr = PER_QUERY.get(TEST_ID_OF.get(n, "").rsplit("::", 1)[-1])
        e = by_attr.get(attr)
        if e is None:
            est += CPU_EST.get(n, DUR.get(n, 0))
        elif attr == "tpcds_sqls" and len(ms) == e["count"]:
            est += sum(ms[i] for i in e["edited"])
        else:
            est += 3 * CPU_EST.get(n, DUR.get(n, 0)) * max(1, len(e["edited"])) / max(1, e["count"])
    return 3.0 + 3.0 * est / 1000


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
    FRESH_PROCESS.update(t["number"] for t in tests if t["id"] in NO_RUN_IDS)
    if not LAZY_RUN:
        NO_RUN.update(t["number"] for t in tests if t["id"] in NO_RUN_IDS)
    valid = 0
    for n in dur:
        valid |= 1 << n
    try:
        cmap = Map(MAP)
        CMAP[:] = [cmap]
    except Exception as e:  # no map: every commit selects every test, statically
        log(f"map failed: {e!r}")
        for patch in list_patches(DATA / "commits"):
            cid = patch.name[: -len(".patch")]
            out.write(json.dumps({"commit": cid, "workers": safe_pack(sorted(dur), dur)}, separators=(",", ":")) + "\n")
        out.flush()
        return
    CPU_EST.update({n: cmap.cpu_ms.get(n, dur[n]) for n in dur})
    if LAZY_RUN:
        # the engine runs test_lazy_load's fresh interpreter on the patched tree
        for t in tests:
            if t["id"] in NO_RUN_IDS:
                CPU_EST[t["number"]] = max(CPU_EST.get(t["number"], 0), LAZY_RUN_MS)
    CLS_OF.update({t["number"]: t["id"].rsplit("::", 1)[0] for t in tests})
    proxy_tests: list = []
    if MAXNODES_PROXY:
        by_id = {t["id"]: t["number"] for t in tests}
        nxt = max(dur) + 1
        for real_id, proxy_id in PROXY_IDS.items():
            real = by_id.get(real_id)
            if real is None:
                continue
            PROXY_OF[real] = nxt
            DUR[nxt], CPU_EST[nxt] = 300, 400
            FILE_OF[nxt], CLS_OF[nxt] = FILE_OF[real], CLS_OF[real]
            proxy_tests.append({"number": nxt, "id": proxy_id, "file": FILE_OF[real], "seconds": 0.3})
            nxt += 1
    TEST_ID_OF.update({t["number"]: t["id"] for t in tests})
    CLS_SETUP.update(cmap.cls_setup_ms)
    WATCH_SETUP.update(cmap.cls_setup_ms)
    lazy = 0
    for t in tests:
        if t["id"] in NO_RUN_IDS:
            lazy |= 1 << t["number"]
    file_tests: dict = {}
    for t in tests:
        file_tests[t["file"]] = file_tests.get(t["file"], 0) | 1 << t["number"]
    try:
        an = Analyzer(cmap, valid, lazy, file_tests)
    except Exception as e:  # e.g. an unreadable manifest: analyze without it
        log(f"analyzer setup failed: {e!r}")
        global ATTR_NARROW
        ATTR_NARROW = False
        an = Analyzer(cmap, valid, lazy, file_tests)
    an.class_masks = class_masks(tests)
    patches = list_patches(DATA / "commits")
    jobs, final = [], {}
    for patch in patches:
        cid = patch.name[: -len(".patch")]
        try:
            mask, why = an.analyze(read_patch(patch))
            py, data, case_site = dict(an.py_changes), dict(an.data_changes), an.case_site
            if TPCDS_WIDE:
                case_site = {k: list(v) for k, v in an.case_sites.items()}
            if an.deferred:
                DEFERRED[cid] = an.deferred & valid
            fruns = dict(an.fixture_runs)
        except Exception as e:  # never crash: run everything for this commit
            mask, why, py, data, case_site = valid, [f"analyzer error {e!r} -> ALL"], {}, {}, None
            DEFERRED.pop(cid, None)
            fruns = {}
        try:
            mask &= valid
            C = [n for n in dur if mask >> n & 1]
            # No patched Python to verify, including analysis failures and unsupported diffs.
            data_only = not py
            log(f"{cid}: {len(C)} candidates | " + "; ".join(why)[:200])
            known = set()
            for rel in data:
                dm = an.data_masks.get(rel, 0)
                known |= {n for n in C if dm >> n & 1}
            score = commit_rarity(cmap, C, an.sites) if (C and not data_only and PROBE_ORDER == "rare") else commit_focus(cmap, C, py)
            fixture = None
            if FIXTURE_RUN and data_only and data and C and set(data) <= set(fruns):
                runnable = [n for n in C if CLS_OF.get(n) == EXEC_CLS and n not in NO_RUN]
                spec = sorted({(rel.removeprefix("tests/fixtures/"), attr)
                               for rel in data for cls_id, attr in fruns[rel]["attrs"] if cls_id == EXEC_CLS})
                if runnable and spec and _pack_cost(C) > _pack_cost([n for n in C if n not in runnable]):
                    fixture = (spec, runnable, {rel.removeprefix("tests/fixtures/"): fruns[rel] for rel in data})
                    log(f"{cid}: fixture run: {len(runnable)} {EXEC_CLS} tests on the edited queries")
            dropped = None
            if PROFILE_SENTINEL and C and not data_only and an.profile_dropped & valid:
                in_c = set(C)
                dropped = sorted(n for n in dur if (an.profile_dropped & valid) >> n & 1 and n not in in_c)
                log(f"{cid}: profile dropped {len(dropped)} candidates: sentinels check them")
            jobs.append((cid, C, py, data, data_only, score, known, case_site, fixture, dropped))
        except Exception as e:  # never crash: run everything for this commit
            log(f"{cid}: job error {e!r} -> ALL")
            jobs.append((cid, sorted(dur), {}, {}, True, {}, set(), None, None, None))
    set_settle_scale(len(jobs), [C for _, C, _, _, data_only, *_ in jobs if C and not data_only])
    if SETTLE_GIANT_MS > 0:
        grant_giants([C for _, C, _, _, data_only, *_ in jobs if C and not data_only])
        HEAVY_BATCH.update(n for n in dur if n not in NO_RUN and SETTLE_MAX_MS < batch_cost([n]) <= SETTLE_GIANT_MS)
    need_engine = USE_ENGINE and any((job[1] and not job[4]) or job[8] for job in jobs)
    results = {}
    if need_engine:
        try:
            from engine import Engine

            exec_cls = EXEC_CLS
            runnable_exec = any(
                CLS_OF.get(n) == exec_cls and n not in NO_RUN and CPU_EST.get(n, dur[n]) <= RUN_MAX_MS
                for _, C, _, _, data_only, *_ in jobs if not data_only for n in C
            ) or any(job[8] for job in jobs)
            # Warm up only the test modules some commit may actually run.
            needed = {n for _, C, _, _, data_only, *_ in jobs if not data_only for n in C}
            needed |= {n for job in jobs if job[8] for n in job[8][1]}
            eng = Engine(str(SRC), tests + proxy_tests, log=log, presetup=(exec_cls,) if (runnable_exec and PRESETUP) else (),
                         needed=needed if WARM_NEEDED_ONLY else None)
            if eng.presetup_done:
                CLS_SETUP[exec_cls] = 0
            EXECUTOR_TEST[0] = eng.executor_test
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
    for job in jobs:
        cid, C, py, data, data_only = job[:5]
        final_set, note = results.get(cid, (set(C), "static"))
        if diag_min and diag_min <= len(C) <= diag_max:
            final_set, note = set(), "diagnostic: printed nothing"
        diag_mode = os.environ.get("RWX_DIAG_MODE", "")
        if (diag_mode == "live" and cid in LIVE) or (diag_mode == "static" and C and note == "static"):
            final_set, note = set(), f"diagnostic ({diag_mode}): printed nothing"
        if diag_mode == "blind" and cid in OBSERVED:
            # diagnostic: on commits the engine ran, print only what a run saw failing
            final_set, note = set(OBSERVED[cid]), "diagnostic (blind): observed failures only"
        if diag_mode == "heavy" and cid in OBSERVED:
            # diagnostic: on commits the engine ran, leave out every unrun slow test
            seen = OBSERVED[cid]
            final_set = {n for n in final_set if n in seen or not (
                n in NO_RUN or CPU_EST.get(n, dur[n]) + CLS_SETUP.get(CLS_OF.get(n), 0) > RUN_MAX_MS)}
            note = "diagnostic (heavy): unrun slow tests left out"
        dropped = job[9] if len(job) > 9 else None
        if dropped and C and ("cid", cid) not in SENTINEL_CLEARED:
            # profile-dropped candidates whose sentinels did not all pass (or never ran)
            final_set = set(final_set) | set(dropped)
            note += f"; +{len(dropped)} profile-dropped (sentinels not passed)"
        if DEFERRED.get(cid) and IMPORT_OK.get(cid) is not True:
            # the patched modules were not shown to import: every importer may fail
            extra = DEFERRED[cid]
            final_set = set(final_set) | {n for n in dur if extra >> n & 1}
            note += f"; +importers ({'import failed' if IMPORT_OK.get(cid) is False else 'import unchecked'})"
        try:
            nums = sorted(n for n in (set(C) if final_set is None else set(final_set)) if n in dur)
        except Exception as e:  # malformed result: everything the analysis named
            log(f"{cid}: bad result {e!r}")
            nums = sorted(n for n in C if n in dur)
        workers = safe_pack(nums, dur)
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
