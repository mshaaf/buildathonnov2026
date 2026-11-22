"""Unified-diff parsing, patch application and AST change localization.

Stdlib only: this runs inside the scored container.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # with leading ' ', '-', '+'
    old_seen: int = 0
    new_seen: int = 0

    def add(self, line: str) -> None:
        self.lines.append(line)
        tag = line[:1]
        if tag != "+":
            self.old_seen += 1
        if tag != "-":
            self.new_seen += 1


@dataclass
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    created: bool = False
    deleted: bool = False
    copy: bool = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""


def _strip_prefix(p: str) -> str | None:
    p = p.rstrip("\r\n")
    if "\t" in p:
        p = p.split("\t", 1)[0]
    p = p.strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def _git_paths(line: str) -> tuple[str | None, str | None]:
    """Paths of a 'diff --git' header, with or without a/ b/ prefixes (best effort)."""
    rest = line[len("diff --git ") :].rstrip("\r")
    m = re.match(r"a/(.*) b/(.*)$", rest)
    if m:
        return m.group(1), m.group(2)
    parts = rest.split(" ")
    if len(parts) % 2 == 0:
        half = len(parts) // 2
        a, b = " ".join(parts[:half]), " ".join(parts[half:])
        if a == b:
            return a, b
    return None, None


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch(*_git_paths(line))
            files.append(cur)
            hunk = None
        elif line.startswith("--- ") and (hunk is None or _hunk_done(hunk)):
            if cur is None or cur.hunks:
                cur = FilePatch(None, None)
                files.append(cur)
            cur.old_path = _strip_prefix(line[4:])
            hunk = None
        elif line.startswith("+++ ") and (hunk is None or _hunk_done(hunk)):
            if cur is not None:
                cur.new_path = _strip_prefix(line[4:])
            hunk = None
        elif (line.startswith("Binary files") or line.startswith("GIT binary patch")) and (hunk is None or _hunk_done(hunk)):
            if cur is None:
                cur = FilePatch(None, None)
                files.append(cur)
            cur.binary = True
            hunk = None
        elif line.startswith("new file mode") and cur is not None and hunk is None:
            cur.old_path = None
            cur.created = True
        elif line.startswith("deleted file mode") and cur is not None and hunk is None:
            cur.new_path = None
            cur.deleted = True
        elif line.startswith(("rename from ", "copy from ")) and cur is not None and hunk is None:
            cur.old_path = _strip_prefix(line.split(" from ", 1)[1])
            if line.startswith("copy"):
                cur.copy = True
        elif line.startswith(("rename to ", "copy to ")) and cur is not None and hunk is None:
            cur.new_path = _strip_prefix(line.split(" to ", 1)[1])
        elif line.startswith("@@") and cur is not None:
            m = HUNK_RE.match(line)
            if m:
                hunk = Hunk(
                    int(m.group(1)),
                    int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)),
                    int(m.group(4)) if m.group(4) is not None else 1,
                )
                cur.hunks.append(hunk)
        elif hunk is not None and not _hunk_done(hunk):
            if line.startswith("\\"):
                pass  # "\ No newline at end of file"
            elif line[:1] in (" ", "-", "+") or line == "":
                hunk.add(line if line else " ")
        i += 1
    return files


def _hunk_done(h: Hunk) -> bool:
    return h.old_seen >= h.old_len and h.new_seen >= h.new_len


class PatchMismatch(ValueError):
    """A hunk's old lines are not in the base file (the patch is not against the pinned tree)."""


def _old_block(h: Hunk) -> list[str]:
    return [l[1:] for l in h.lines if l[:1] in (" ", "-")]


def _matches(old_lines: list[str], start: int, block: list[str]) -> bool:
    if start < 0 or start + len(block) > len(old_lines):
        return False
    for i, b in enumerate(block):
        if old_lines[start + i].rstrip("\r") != b.rstrip("\r"):
            return False
    return True


def locate_hunks(old_text: str, hunks: list[Hunk]) -> list[Hunk]:
    """Check each hunk against the base text; a hunk found at another position (like
    `git apply`/`patch` would accept) is moved there. Raises PatchMismatch otherwise."""
    old_lines = old_text.split("\n")
    out = []
    shift = 0
    floor = 0
    for h in hunks:
        block = _old_block(h)
        start = (h.old_start - 1 if h.old_len > 0 else h.old_start) + shift
        if not block:
            out.append(h if not shift else Hunk(h.old_start + shift, h.old_len, h.new_start, h.new_len, h.lines))
            continue
        if not _matches(old_lines, start, block):
            found = [i for i in range(floor, len(old_lines) - len(block) + 1) if _matches(old_lines, i, block)]
            if not found:
                raise PatchMismatch(f"hunk at {h.old_start} does not match the base file")
            best = min(found, key=lambda i: (abs(i - start), i))
            shift += best - start
            start = best
        floor = start + len(block)
        if start + 1 != h.old_start:
            h = Hunk(start + 1, h.old_len, h.new_start + (start + 1 - h.old_start), h.new_len, h.lines)
        out.append(h)
    return out


def apply_hunks(old_text: str, hunks: list[Hunk]) -> str:
    """Apply hunks at their stated positions (patches are against the pinned tree)."""
    old_lines = old_text.split("\n")
    out: list[str] = []
    pos = 0  # index into old_lines (0-based)
    for h in hunks:
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        out.extend(old_lines[pos:start])
        pos = start
        for l in h.lines:
            tag, body = l[:1], l[1:]
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else body)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(body)
    out.extend(old_lines[pos:])
    return "\n".join(out)


def diff_hunks(old_text: str, new_text: str) -> list[Hunk]:
    """Zero-context hunks turning old_text into new_text (used to merge repeated file entries)."""
    import difflib

    a, b = old_text.split("\n"), new_text.split("\n")
    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        h = Hunk(i1 + 1 if i2 > i1 else i1, i2 - i1, j1 + 1 if j2 > j1 else j1, j2 - j1)
        for x in a[i1:i2]:
            h.add("-" + x)
        for x in b[j1:j2]:
            h.add("+" + x)
        hunks.append(h)
    return hunks


def changed_lines(hunks: list[Hunk]) -> tuple[set[int], set[int]]:
    """Old line numbers removed/replaced, and new line numbers added."""
    old_changed: set[int] = set()
    new_added: set[int] = set()
    for h in hunks:
        o, n = h.old_start, h.new_start
        for l in h.lines:
            tag = l[:1]
            if tag == " ":
                o += 1
                n += 1
            elif tag == "-":
                old_changed.add(o)
                o += 1
            elif tag == "+":
                new_added.add(n)
                n += 1
    return old_changed, new_added


# ---------------------------------------------------------------- AST locating

SCOPE_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)


@dataclass
class Site:
    """Where a change lands: the code object that runs it and the lines that matter."""

    kind: str  # 'func' (runs when called) or 'import' (module/class body) or 'header'
    scope_path: tuple  # e.g. (('class', 'Foo', 12), ('def', 'bar', 30))
    code_key: str  # "firstlineno:name" of the code object, as recorded by coverage
    lines: tuple[int, int]  # inclusive line span in the *old* file to look up
    class_line: int | None = None  # enclosing ClassDef line when kind == 'import' in a class body
    names: tuple = ()  # names assigned by an import-time statement
    stmt: ast.AST | None = None
    scope_node: ast.AST | None = None

    def class_key(self, rel: str) -> str | None:
        """'relpath:firstlineno:qualname' of the innermost enclosing class, as recorded at runtime."""
        if not self.scope_path or self.scope_path[-1][0] != "class":
            return None
        parts = []
        for kind, name, _ in self.scope_path:
            parts.append(name)
            if kind == "def":
                parts.append("<locals>")
        return f"{rel}:{self.scope_path[-1][2]}:{'.'.join(parts)}"


def _code_key(node) -> str:
    if isinstance(node, ast.Module):
        return "1:<module>"  # module code objects start at line 1 (may be 0 for empty)
    if isinstance(node, ast.ClassDef):
        first = node.decorator_list[0].lineno if node.decorator_list else node.lineno
        return f"{first}:{node.name}"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        first = node.decorator_list[0].lineno if node.decorator_list else node.lineno
        return f"{first}:{node.name}"
    if isinstance(node, ast.Lambda):
        return f"{node.lineno}:<lambda>"
    if isinstance(node, ast.GeneratorExp):
        return f"{node.lineno}:<genexpr>"
    return "?"


def _span(node) -> tuple[int, int]:
    return node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno


def _header_end(stmt) -> int:
    """Last line of a compound statement's header (before its body)."""
    body = getattr(stmt, "body", None)
    if isinstance(body, list) and body:
        return max(stmt.lineno, body[0].lineno - 1)
    return _span(stmt)[1]


class Locator:
    """Finds, for a line in a source file, the innermost scope and statement."""

    def __init__(self, tree: ast.Module):
        self.tree = tree

    def locate(self, line: int, lambdas: bool = True) -> Site | None:
        scope_path: list = []
        scope = self.tree
        node = self.tree
        stmt_found = None
        # Walk down through statements containing the line.
        while True:
            body_lists = []
            for fname in ("body", "orelse", "finalbody", "handlers", "cases"):
                v = getattr(node, fname, None)
                if isinstance(v, list):
                    body_lists.append(v)
            nxt = None
            for lst in body_lists:
                for child in lst:
                    if not hasattr(child, "lineno"):
                        continue
                    s, e = _span(child)
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.decorator_list:
                        s = child.decorator_list[0].lineno
                    if s <= line <= e:
                        nxt = child
                        break
                if nxt:
                    break
            if nxt is None:
                break
            if isinstance(nxt, (ast.ExceptHandler, ast.match_case)):
                node = nxt
                continue
            stmt_found = nxt
            if isinstance(nxt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                hdr_end = _header_end(nxt)
                first = nxt.decorator_list[0].lineno if nxt.decorator_list else nxt.lineno
                if first <= line <= hdr_end:
                    # decorators/signature/bases: evaluated in the enclosing scope
                    break
                scope_path.append(("class" if isinstance(nxt, ast.ClassDef) else "def", nxt.name, first))
                scope = nxt
                node = nxt
                stmt_found = None
                continue
            # compound statement: descend if the line is in its body
            if isinstance(nxt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)):
                hdr_end = _header_end(nxt)
                if line > hdr_end:
                    node = nxt
                    continue
            break

        if stmt_found is None:
            # A line between statements (blank/comment/else:) in this scope.
            return Site("blank", tuple(scope_path), _code_key(scope), (line, line), stmt=None)

        # Lambdas / generator expressions inside the statement run as their own code objects.
        inner = None
        for sub in ast.walk(stmt_found) if lambdas else ():
            if isinstance(sub, (ast.Lambda, ast.GeneratorExp)) and sub is not stmt_found:
                s, e = _span(sub)
                if isinstance(sub, ast.Lambda):
                    bs, be = _span(sub.body)
                    if bs <= line <= be:
                        if inner is None or (s >= inner[1][0] and e <= inner[1][1]):
                            inner = (sub, (bs, be))
        if inner is not None:
            lam, (bs, be) = inner
            return Site("func", tuple(scope_path) + (("lambda", "<lambda>", lam.lineno),), _code_key(lam), (bs, be), stmt=lam.body)

        span = _span(stmt_found)
        if isinstance(stmt_found, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Match)):
            span = (stmt_found.lineno, _header_end(stmt_found))
        is_import_time = isinstance(scope, (ast.Module, ast.ClassDef))
        kind = "import" if is_import_time else "func"
        if isinstance(stmt_found, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "header"  # decorator/signature/bases of a def or class
        names = []
        if is_import_time:
            for sub in ast.walk(stmt_found) if not isinstance(stmt_found, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [stmt_found]:
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.append(sub.id)
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(sub.name)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.append((a.asname or a.name).split(".")[0])
        return Site(
            kind,
            tuple(scope_path),
            _code_key(scope),
            span,
            class_line=scope.lineno if isinstance(scope, ast.ClassDef) else None,
            names=tuple(names),
            stmt=stmt_found,
            scope_node=scope,
        )


def _same_syntax(a, b) -> bool:
    """Compare parsed syntax without source positions or whole-tree string dumps."""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for name in a._fields:
            if not _same_syntax(getattr(a, name), getattr(b, name)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_syntax(x, y) for x, y in zip(a, b))
    return a == b


def ast_equal(a: str | ast.Module, b: str) -> bool | None:
    """Compare source syntax, optionally reusing the parsed base. None means invalid new syntax."""
    try:
        patched = ast.parse(b)
    except SyntaxError:
        return None
    return _same_syntax(ast.parse(a) if isinstance(a, str) else a, patched)


# ---------------------------------------------------------------- import-time safety
#
# A statement that runs at import time (module or class body, the decorators, defaults
# and evaluated annotations of a def, the bases of a class) breaks every importer of the
# module when it starts raising. These helpers decide, conservatively, that an edit cannot
# introduce such an exception; anything they cannot prove counts as "may raise".


def _is_container(n) -> bool:
    return isinstance(n, (ast.Dict, ast.List, ast.Tuple, ast.Set))


def expr_safe(e, known=frozenset()) -> bool:
    """Evaluating e cannot raise: literals, lambdas with safe defaults, containers of those,
    and lookups whose exact text already evaluated at import time (`known`)."""
    if e is None or isinstance(e, ast.Constant):
        return True
    if isinstance(e, ast.UnaryOp) and isinstance(e.operand, ast.Constant):
        v = e.operand.value
        if isinstance(e.op, ast.Not):
            return True
        if isinstance(e.op, (ast.USub, ast.UAdd)):
            return isinstance(v, (int, float, complex))
        return isinstance(e.op, ast.Invert) and isinstance(v, int)
    if isinstance(e, ast.Lambda):
        return all(expr_safe(d, known) for d in e.args.defaults + [d for d in e.args.kw_defaults if d is not None])
    if isinstance(e, ast.Dict):
        return all(k is not None and _hashable(k) and expr_safe(k, known) and expr_safe(v, known)
                   for k, v in zip(e.keys, e.values))
    if isinstance(e, ast.Set):
        return all(_hashable(x) and expr_safe(x, known) for x in e.elts)
    if isinstance(e, (ast.List, ast.Tuple)):
        return all(expr_safe(x, known) and not isinstance(x, ast.Starred) for x in e.elts)
    if isinstance(e, (ast.Name, ast.Attribute)) and isinstance(e.ctx, ast.Load):
        return dotted(e) in known
    return False


def _hashable(k) -> bool:
    if isinstance(k, ast.Constant):
        return True
    if isinstance(k, ast.Tuple):
        return all(_hashable(x) for x in k.elts)
    return isinstance(k, (ast.Name, ast.Attribute, ast.Lambda))


def dotted(e) -> str | None:
    parts = []
    while isinstance(e, ast.Attribute):
        parts.append(e.attr)
        e = e.value
    if not isinstance(e, ast.Name):
        return None
    parts.append(e.id)
    return ".".join(reversed(parts))


def value_safe(a, b, known=frozenset()) -> bool:
    """Replacing expression a by b cannot introduce an exception at evaluation time."""
    if _same_syntax(a, b):
        return True
    if expr_safe(b, known):
        return True
    if isinstance(a, ast.Lambda) and isinstance(b, ast.Lambda):
        # the body only runs when called; creating the lambda evaluates its defaults
        return _same_syntax(a.args, b.args) or all(
            expr_safe(d, known) for d in b.args.defaults + [d for d in b.args.kw_defaults if d is not None]
        )
    if type(a) is type(b) and _is_container(a):
        if isinstance(a, ast.Dict):
            old = list(zip(a.keys, a.values))
            for k, v in zip(b.keys, b.values):
                if any(_same_syntax(k, ok) and _same_syntax(v, ov) for ok, ov in old):
                    continue
                twin = [ov for ok, ov in old if ok is not None and k is not None and _same_syntax(k, ok)]
                if k is None:
                    return False
                if not (_hashable(k) and (twin or expr_safe(k, known))):
                    return False
                if not (expr_safe(v, known) or any(value_safe(ov, v, known) for ov in twin)):
                    return False
            return True
        olds = a.elts
        for i, x in enumerate(b.elts):
            if isinstance(x, ast.Starred):
                return False
            if any(_same_syntax(x, o) for o in olds):
                continue
            if expr_safe(x, known) and (not isinstance(a, ast.Set) or _hashable(x)):
                continue
            if i < len(olds) and value_safe(olds[i], x, known) and not isinstance(a, ast.Set):
                continue
            return False
        return True
    return False


def def_time_parts(node, future_annotations: bool) -> list:
    """Expressions a def/class statement evaluates when it runs (not the body of a def)."""
    parts = list(node.decorator_list)
    if isinstance(node, ast.ClassDef):
        return parts + list(node.bases) + [k.value for k in node.keywords]
    a = node.args
    parts += list(a.defaults) + list(a.kw_defaults)
    if not future_annotations:
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            parts.append(arg.annotation if arg is not None else None)
        parts.append(node.returns)
    return parts


def stmt_safe(a, b, future_annotations: bool, known=frozenset()) -> bool:
    """Running statement b at import time cannot raise where a did not (same position)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Assign):
        return (len(a.targets) == len(b.targets)
                and all(_same_syntax(x, y) or (isinstance(x, ast.Name) and isinstance(y, ast.Name))
                        for x, y in zip(a.targets, b.targets))
                and value_safe(a.value, b.value, known))
    if isinstance(a, ast.AnnAssign):
        ann_ok = future_annotations or _same_syntax(a.annotation, b.annotation) or expr_safe(b.annotation, known)
        tgt_ok = _same_syntax(a.target, b.target) or (isinstance(a.target, ast.Name) and isinstance(b.target, ast.Name))
        if b.value is None:
            return ann_ok and tgt_ok
        return ann_ok and tgt_ok and a.value is not None and value_safe(a.value, b.value, known)
    if isinstance(a, ast.Expr):
        return value_safe(a.value, b.value, known)
    if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        pa, pb = def_time_parts(a, future_annotations), def_time_parts(b, future_annotations)
        if len(pa) != len(pb) or len(a.decorator_list) != len(b.decorator_list):
            return False
        if not all(_same_syntax(x, y) for x, y in zip(a.decorator_list, b.decorator_list)):
            return False  # a decorator can do anything with the object, including register it
        if isinstance(a, ast.ClassDef):
            return all(_same_syntax(x, y) for x, y in zip(pa, pb)) and _same_syntax(a.keywords, b.keywords)
        for x, y in zip(pa, pb):
            if y is None:
                continue
            if not (expr_safe(y, known) if x is None else value_safe(x, y, known)):
                return False
        return True
    if isinstance(a, ast.Import):
        # same modules (aliases may differ): the import already succeeded
        return {x.name for x in b.names} <= {x.name for x in a.names}
    if isinstance(a, ast.ImportFrom):
        return (a.module == b.module and a.level == b.level and "*" not in {x.name for x in b.names}
                and {x.name for x in b.names} <= {x.name for x in a.names})
    if isinstance(a, ast.Pass):
        return True
    return False


def stmt_inert(b, future_annotations: bool, known=frozenset()) -> bool:
    """A newly inserted import-time statement cannot raise."""
    if isinstance(b, ast.Pass):
        return True
    if isinstance(b, ast.Assign):
        return all(isinstance(t, ast.Name) for t in b.targets) and expr_safe(b.value, known)
    if isinstance(b, ast.AnnAssign):
        return (isinstance(b.target, ast.Name) and (future_annotations or expr_safe(b.annotation, known))
                and (b.value is None or expr_safe(b.value, known)))
    if isinstance(b, ast.Expr):
        return isinstance(b.value, ast.Constant)
    if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return not b.decorator_list and all(p is None or expr_safe(p, known) for p in def_time_parts(b, future_annotations))
    return False


def bound_names(stmt) -> tuple:
    """Names an import-time statement binds in its scope."""
    names = []
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (stmt.name,)
    for sub in ast.walk(stmt):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            names.append(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                names.append((a.asname or a.name).split(".")[0])
    return tuple(dict.fromkeys(names))


def has_future_annotations(tree) -> bool:
    for st in tree.body:
        if isinstance(st, ast.ImportFrom) and st.module == "__future__" and any(a.name == "annotations" for a in st.names):
            return True
    return False


def old_to_new_line(hunks: list[Hunk], ln: int) -> int:
    """Map an old line number to the new file (a changed line maps to the start of its replacement)."""
    delta = 0
    for h in hunks:
        o = h.old_start if h.old_len > 0 else h.old_start + 1
        n = h.new_start if h.new_len > 0 else h.new_start + 1
        if ln < o:
            return ln + delta
        for l in h.lines:
            tag = l[:1]
            if tag == "-":
                if o == ln:
                    return n
                o += 1
            elif tag == "+":
                n += 1
            else:
                if o == ln:
                    return n
                o += 1
                n += 1
        delta = n - o
    return ln + delta


def change_blocks(hunks: list[Hunk]):
    """Runs of changed lines: (old_lo, old_hi, new_lo, new_hi), inclusive, empty side as hi < lo."""
    out = []
    for h in hunks:
        o = h.old_start if h.old_len > 0 else h.old_start + 1
        n = h.new_start if h.new_len > 0 else h.new_start + 1
        i, lines = 0, h.lines
        while i < len(lines):
            if lines[i][:1] not in ("-", "+"):
                o += 1
                n += 1
                i += 1
                continue
            o0, n0 = o, n
            while i < len(lines) and lines[i][:1] in ("-", "+"):
                if lines[i][:1] == "-":
                    o += 1
                else:
                    n += 1
                i += 1
            out.append((o0, o - 1, n0, n - 1))
    return out


def known_lookups(tree, line: int, scope_chain) -> frozenset:
    """Dotted names that already evaluated, at import time, before `line`: straight-line
    statements of the module and of the enclosing class bodies (no if/try/loop bodies),
    plus builtins. Their prefixes count too."""
    import builtins

    known = set(dir(builtins))

    def add(text):
        if text:
            parts = text.split(".")
            for i in range(1, len(parts) + 1):
                known.add(".".join(parts[:i]))

    for body in [tree.body] + [c.body for c in scope_chain if isinstance(c, ast.ClassDef)]:
        for st in body:
            end = getattr(st, "end_lineno", st.lineno) or st.lineno
            if end >= line:
                break
            if isinstance(st, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith, ast.Match)):
                continue
            for name in bound_names(st):
                known.add(name)
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nodes = def_time_parts(st, True)
            else:
                nodes = [st]
            stack = [n for n in nodes if n is not None]
            while stack:
                sub = stack.pop()
                if isinstance(sub, ast.Lambda):
                    continue  # its body runs only when called
                if isinstance(sub, (ast.Name, ast.Attribute)) and isinstance(sub.ctx, ast.Load):
                    add(dotted(sub))
                stack.extend(ast.iter_child_nodes(sub))
    return frozenset(known)


def same_outside_lambdas(a, b, changed: set) -> bool:
    """a and b differ only inside bodies of lambdas whose first line is not in `changed`.
    Such a body runs only when the lambda is called, and every run of a changed line that
    is not a code object's first line is recorded by coverage (even calls made at import)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Lambda):
        return _same_syntax(a.args, b.args) and (a.lineno not in changed or _same_syntax(a.body, b.body))
    if isinstance(a, ast.AST):
        return all(same_outside_lambdas(getattr(a, f), getattr(b, f), changed) for f in a._fields)
    if isinstance(a, list):
        return len(a) == len(b) and all(same_outside_lambdas(x, y, changed) for x, y in zip(a, b))
    return a == b
