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


@dataclass
class FilePatch:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""


def _strip_prefix(p: str) -> str | None:
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FilePatch(m.group(1) if m else None, m.group(2) if m else None)
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
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if cur is not None:
                cur.binary = True
        elif line.startswith("new file mode") and cur is not None:
            cur.old_path = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new_path = None
        elif line.startswith("rename from ") and cur is not None:
            cur.old_path = line[len("rename from ") :]
        elif line.startswith("rename to ") and cur is not None:
            cur.new_path = line[len("rename to ") :]
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
                hunk.lines.append(line if line else " ")
        i += 1
    return files


def _hunk_done(h: Hunk) -> bool:
    old = sum(1 for l in h.lines if l[:1] in (" ", "-"))
    new = sum(1 for l in h.lines if l[:1] in (" ", "+"))
    return old >= h.old_len and new >= h.new_len


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

    def locate(self, line: int) -> Site | None:
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
        for sub in ast.walk(stmt_found):
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


def ast_equal(a: str, b: str) -> bool | None:
    """True if two sources have the same AST (comments/whitespace only). None if b does not parse."""
    try:
        tb = ast.parse(b)
    except SyntaxError:
        return None
    ta = ast.parse(a)
    return ast.dump(ta) == ast.dump(tb)
