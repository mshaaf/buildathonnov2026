"""Build the "odd" robustness corpus: unusual patches against the pinned tree (lab only).

usage: make_odd.py BASE_GIT_TREE OUT_DIR

BASE_GIT_TREE is a scratch copy of the pinned sqlglot tree with one git commit (never the
shared tree). Each spec edits the tree, the diff is taken with git (or reformatted to an
edge-case format), and the tree is reset. Writes OUT_DIR/commits/<id>.patch, OUT_DIR/kinds.json.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()


def git(*a, binary=False) -> bytes:
    return subprocess.run(["git", *a], cwd=BASE, check=True, capture_output=True).stdout


def rd(p):
    return (BASE / p).read_text()


def R(p, old, new, count=1):
    s = rd(p)
    n = s.count(old)
    assert n >= 1, f"{p}: {old!r} not found"
    assert count == 0 or n == count, f"{p}: {old!r} found {n} times"
    (BASE / p).write_text(s.replace(old, new, 1))


def W(p, text):
    (BASE / p).parent.mkdir(parents=True, exist_ok=True)
    (BASE / p).write_text(text)


def WB(p, data: bytes):
    (BASE / p).write_bytes(data)


def D(p):
    (BASE / p).unlink()


def MV(a, b):
    git("mv", a, b)


def reset():
    git("reset", "-q", "--hard")
    git("clean", "-qfd")


def diff(*extra) -> bytes:
    git("add", "-A")
    return git("diff", "--cached", "-M", "--binary", *extra)


SPECS = []


def spec(cid, kind):
    def deco(f):
        SPECS.append((cid, kind, f))
        return f

    return deco


# ------------------------------------------------------------------ test files
@spec("ta01", "test assertion in one test method")
def _():
    R("tests/test_transpile.py", 'self.validate("+-1", "-1")', 'self.validate("+-1", "1")')


@spec("th01", "shared helper tests/helpers.py")
def _():
    R("tests/helpers.py", 'meta[key.lstrip("#").strip()] = val.strip()', 'meta[key.lstrip("#").strip()] = val')


@spec("ts01", "setUp edit")
def _():
    R("tests/test_sqlglotc.py", "        self.source.mkdir()\n", "")


@spec("ts02", "setUp edit, entry points")
def _():
    R("tests/test_dialect_entry_points.py", "    def setUp(self):\n        Dialect._classes.clear()", "    def setUp(self):\n        pass")


@spec("tc01", "setUpClass edit")
def _():
    R("tests/test_sqlglotc.py", '{"SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0"}', '{"SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.1"}')


@spec("tm01", "module-level code in a test file (breaking)")
def _():
    R("tests/test_executor.py", 'DIR_TPCH = FIXTURES_DIR + "/optimizer/tpc-h/"', 'DIR_TPCH = FIXTURES_DIR + "/optimizer/tpch/"')


@spec("tm02", "module-level code in a test file (harmless)")
def _():
    R("tests/test_time.py", "KNOWN_CANONICAL_ATOM = ", "UNUSED_ODD = 1\nKNOWN_CANONICAL_ATOM = ")


@spec("td01", "deleted test method")
def _():
    s = rd("tests/test_time.py")
    a = s.index("    def test_subsecond_precision(self):")
    b = s.index("    @unittest.skipUnless(")
    W("tests/test_time.py", s[:a] + s[b:])


@spec("tn01", "added test method (failing)")
def _():
    R("tests/test_time.py", "class TestTime(unittest.TestCase):\n", "class TestTime(unittest.TestCase):\n    def test_odd_new(self):\n        self.assertTrue(False)\n\n")


@spec("tr01", "renamed test method")
def _():
    R("tests/test_time.py", "def test_format_time(self):", "def test_format_time_renamed(self):")


# ------------------------------------------------------------------ syntax errors
@spec("sx01", "syntax error in a sqlglot module")
def _():
    R("sqlglot/dialects/tableau.py", "class Tableau(", "def broken(:\n    pass\n\n\nclass Tableau(")


@spec("sx02", "syntax error in a test file")
def _():
    R("tests/test_time.py", "class TestTime(unittest.TestCase):\n", "class TestTime(unittest.TestCase)\n")


@spec("sx03", "syntax error in tests/helpers.py")
def _():
    R("tests/helpers.py", "def is_compiled(module):", "def is_compiled(module)")


# ------------------------------------------------------------------ file-level
@spec("nf01", "new unimported module under sqlglot/")
def _():
    W("sqlglot/odd_new.py", "X = 1\n")


@spec("nf02", "new test file")
def _():
    W("tests/test_odd_new.py", "import unittest\n\n\nclass TestOdd(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(False)\n")


@spec("nf03", "new harmless conftest.py")
def _():
    W("tests/conftest.py", "# odd: empty conftest\n")


@spec("dl01", "deleted sqlglot module")
def _():
    D("sqlglot/dialects/tableau.py")


@spec("dl02", "deleted fixture")
def _():
    D("tests/fixtures/optimizer/quote_identifiers.sql")


@spec("rn01", "pure rename of a module")
def _():
    MV("sqlglot/dialects/druid.py", "sqlglot/dialects/druid2.py")


@spec("rn02", "rename + edit of a module")
def _():
    MV("sqlglot/dialects/solr.py", "sqlglot/dialects/solr_dialect.py")
    s = rd("sqlglot/dialects/solr_dialect.py")
    W("sqlglot/dialects/solr_dialect.py", s + "\nODD = 1\n")


@spec("rn03", "pure rename of a test file")
def _():
    MV("tests/test_time.py", "tests/test_time_renamed.py")


@spec("md01", "mode-only change")
def _():
    os.chmod(BASE / "sqlglot/time.py", 0o755)


@spec("bn01", "git binary patch to a .csv.gz fixture")
def _():
    p = BASE / "tests/fixtures/optimizer/tpc-h/region.csv.gz"
    rows = gzip.decompress(p.read_bytes()).decode()
    WB("tests/fixtures/optimizer/tpc-h/region.csv.gz", gzip.compress(rows.replace("AFRICA", "AFRIKA").encode(), mtime=0))


@spec("em01", "empty patch file")
def _():
    return b""


@spec("nh01", "patch with no hunks")
def _():
    return b"diff --git a/sqlglot/time.py b/sqlglot/time.py\nindex 1111111..2222222 100644\n"


# ------------------------------------------------------------------ non-Python files
@spec("np01", "pyproject.toml edit")
def _():
    R("pyproject.toml", "line-length = 100", "line-length = 120")


@spec("np02", "setup.cfg edit")
def _():
    R("setup.cfg", "disallow_untyped_calls = False", "disallow_untyped_calls = True")


@spec("np03", "setup.py edit")
def _():
    s = rd("setup.py")
    W("setup.py", s + "\n# odd\nODD = 1\n")


@spec("np04", "README.md edit")
def _():
    s = rd("README.md")
    W("README.md", s.replace("\n", "\nOdd line.\n", 1))


@spec("np05", "Makefile edit")
def _():
    R("Makefile", "SO_BACKUP := /tmp/sqlglot_so_backup", "SO_BACKUP := /tmp/sqlglot_so_backup2")


@spec("np06", "sqlglotc non-Python source")
def _():
    s = rd("sqlglotc/pyproject.toml")
    W("sqlglotc/pyproject.toml", s + "\n# odd\n")


@spec("np07", ".sql fixture outside optimizer (identity.sql)")
def _():
    s = rd("tests/fixtures/identity.sql")
    lines = s.split("\n")
    lines[10] = lines[10] + " + 1 +"
    W("tests/fixtures/identity.sql", "\n".join(lines))


@spec("np08", "pretty.sql fixture")
def _():
    s = rd("tests/fixtures/pretty.sql")
    W("tests/fixtures/pretty.sql", s.replace("SELECT", "SELECT  ", 1))


@spec("np09", "jsonpath cts.json fixture")
def _():
    s = rd("tests/fixtures/jsonpath/cts.json")
    W("tests/fixtures/jsonpath/cts.json", s.replace('"name"', '"nam"', 1))


@spec("np10", "sqlglot/py.typed")
def _():
    W("sqlglot/py.typed", "partial\n")


@spec("np11", "pyproject.toml syntax error")
def _():
    R("pyproject.toml", "[tool.ruff]\n", "[tool.ruff\n")


@spec("np12", "setup.cfg syntax error")
def _():
    R("setup.cfg", "[mypy]\n", "mypy]\n")


# ------------------------------------------------------------------ Python edits
@spec("py01", "decorator change")
def _():
    R("sqlglot/schema.py", "    @classmethod\n    def from_mapping_schema", "    @staticmethod\n    def from_mapping_schema")


@spec("py02", "default-argument change")
def _():
    R("sqlglot/helper.py", 'def csv(*args: str, sep: str = ", ") -> str:', 'def csv(*args: str, sep: str = ",") -> str:')


@spec("py03", "class bases change")
def _():
    R("sqlglot/dialects/redshift.py", "class Redshift(Postgres):", "class Redshift(Postgres.__base__):")


@spec("py04", "module-level constant")
def _():
    R("sqlglot/helper.py", 'CAMEL_CASE_PATTERN = re.compile("(?<!^)(?=[A-Z])")', 'CAMEL_CASE_PATTERN = re.compile("(?=[A-Z])")')


@spec("py05", "__init__.py edit")
def _():
    R("sqlglot/__init__.py", "pretty = False\n", "pretty = True\n")


@spec("py06", "import statement edit (harmless alias)")
def _():
    R("sqlglot/helper.py", "from difflib import get_close_matches\n", "from difflib import get_close_matches as get_close_matches\n")


@spec("py07", "type-annotation-only change")
def _():
    R("sqlglot/helper.py", "def seq_get(seq: Sequence[T], index: int) -> T | None:", "def seq_get(seq: Sequence[T], index: int) -> T:")


@spec("py08", "docstring-only change")
def _():
    R("sqlglot/helper.py", '"""Returns the value in `seq` at position `index`, or `None` if `index` is out of bounds."""', '"""Returns the value at `index`, or `None`."""')


@spec("py09", "error-message string change")
def _():
    R("sqlglot/helper.py", "raise ValueError(f\"Unknown {kind} '{word}'.{similar}\")", "raise ValueError(f\"Unknown {kind}: '{word}'.{similar}\")")


@spec("py10", "deleted statement in a try block")
def _():
    R("sqlglot/schema.py", "                expression.transform(dialect.normalize_identifier, copy=False)\n", "")


@spec("py11", "inserted return")
def _():
    R("sqlglot/helper.py", '    """Returns a name generator given a prefix (e.g. a0, a1, a2, ... if the prefix is "a")."""\n',
      '    """Returns a name generator given a prefix (e.g. a0, a1, a2, ... if the prefix is "a")."""\n    return lambda: prefix\n')


@spec("py12", "inserted raise in a rare branch")
def _():
    R("sqlglot/schema.py", "            except AttributeError:\n", "            except AttributeError:\n                raise ValueError('odd')\n")


@spec("py13", "inserted assignment")
def _():
    R("sqlglot/helper.py", "    if value is None:\n        return []\n    if isinstance(value, list):",
      "    if value is None:\n        return []\n    value = value\n    if isinstance(value, list):")


@spec("py14", "re-indent a line out of an if")
def _():
    R("sqlglot/helper.py", '    similar = seq_get(close_matches, 0) or ""\n    if similar:\n        similar = f" Did you mean {similar}?"\n',
      '    similar = seq_get(close_matches, 0) or ""\n    if similar:\n        pass\n    similar = f" Did you mean {similar}?"\n')


@spec("py15", "edit in nested function + generator expression")
def _():
    R("sqlglot/dialects/dialect.py", "                    if t == token_type\n", "                    if t != token_type\n")


@spec("py16", "edit in a lambda")
def _():
    R("sqlglot/helper.py", 'return lambda: f"{prefix}{next(sequence)}"', 'return lambda: f"{prefix}_{next(sequence)}"')


@spec("py17", "edit in a multi-line generator expression")
def _():
    R("sqlglot/diff.py", "        if not isinstance(arg, IGNORED_LEAF_EXPRESSION_TYPES)\n", "        if isinstance(arg, IGNORED_LEAF_EXPRESSION_TYPES)\n")


@spec("py18", "try/except edit")
def _():
    R("sqlglot/helper.py", "        return seq[index]\n    except IndexError:\n", "        return seq[index]\n    except KeyError:\n")


@spec("py19", "change inside if TYPE_CHECKING")
def _():
    R("sqlglot/helper.py", "if t.TYPE_CHECKING:\n    from sqlglot.expressions import Expr\n", "if t.TYPE_CHECKING:\n    from sqlglot.expressions import Expr, Nonexistent\n")


@spec("py20", "global set literal entry")
def _():
    R("sqlglot/helper.py", 'DATE_UNITS = {"day", "week", "month", "quarter", "year", "year_month"}', 'DATE_UNITS = {"day", "week", "month", "quarter", "year"}')


@spec("py21", "sqlglotc/setup.py edit")
def _():
    R("sqlglotc/setup.py", '        "anonymize.py",\n', "")


@spec("py22", "comment-only: coding cookie that breaks import")
def _():
    s = rd("sqlglot/parsers/tsql.py")
    W("sqlglot/parsers/tsql.py", "# -*- coding: ascii -*-\n" + s)


@spec("py23", "multi-hunk multi-file mix")
def _():
    R("sqlglot/helper.py", "raise ValueError(f\"Unknown {kind} '{word}'.{similar}\")", "raise ValueError(f\"Unknown {kind}: '{word}'.{similar}\")")
    R("sqlglot/helper.py", 'DATE_UNITS = {"day", "week", "month", "quarter", "year", "year_month"}', 'DATE_UNITS = {"day", "week", "month", "quarter", "year", "year_month", "odd"}')
    R("tests/test_transpile.py", 'self.validate("+-1", "-1")', 'self.validate("+-1", "-1")  # odd')
    R("pyproject.toml", "line-length = 100", "line-length = 101")
    s = rd("tests/fixtures/optimizer/qualify_tables.sql")
    W("tests/fixtures/optimizer/qualify_tables.sql", "-- odd comment\n" + s)


@spec("py24", "import statement edit (breaking)")
def _():
    R("sqlglot/helper.py", "from itertools import count\n", "from itertools import count as _count\n")


# ------------------------------------------------------------------ formats
def _crlf_edit():
    R("sqlglot/helper.py", "def csv(*args: str, sep: str = \", \") -> str:\n", "def csv(*args: str, sep: str = \", \") -> str:\r\n")


@spec("fm01", "CRLF line in a .py file")
def _():
    _crlf_edit()


@spec("fm02", r"'\ No newline at end of file'")
def _():
    s = rd("sqlglot/helper.py")
    assert s.endswith("\n")
    W("sqlglot/helper.py", s.rstrip("\n") + "  # odd")


@spec("fm03", "diff -u without the diff --git header")
def _():
    R("sqlglot/helper.py", 'def csv(*args: str, sep: str = ", ") -> str:', 'def csv(*args: str, sep: str = ",") -> str:')
    raw = diff().decode()
    out = []
    for line in raw.split("\n"):
        if line.startswith(("diff --git", "index ")):
            continue
        if line.startswith("--- "):
            line += "\t2026-11-21 10:00:00.000000000 +0000"
        if line.startswith("+++ "):
            line += "\t2026-11-21 10:01:00.000000000 +0000"
        out.append(line)
    return "\n".join(out).encode()


@spec("fm04", "paths without a/ b/ prefixes")
def _():
    R("sqlglot/helper.py", 'def csv(*args: str, sep: str = ", ") -> str:', 'def csv(*args: str, sep: str = ",") -> str:')
    return diff("--no-prefix")


@spec("fm05", "same file appearing twice")
def _():
    R("sqlglot/helper.py", "if similar:\n        similar = f\" Did you mean {similar}?\"", "if similar:\n        similar = f\" Did you mean {similar}?\"\n        similar = similar")
    first = diff()
    git("-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "tmp")
    R("sqlglot/helper.py", 'def csv(*args: str, sep: str = ", ") -> str:', 'def csv(*args: str, sep: str = ",") -> str:')
    second = diff()
    git("reset", "-q", "--hard", "HEAD~1")
    return first + second


@spec("fm06", "non-UTF-8 byte in a .py comment")
def _():
    s = (BASE / "sqlglot/helper.py").read_bytes()
    WB("sqlglot/helper.py", s.replace(b"# Interval units", b"# Interval units \xe9t\xe9", 1))


@spec("fm07", "non-UTF-8 byte in a .sql fixture")
def _():
    s = (BASE / "tests/fixtures/optimizer/qualify_tables.sql").read_bytes()
    WB("tests/fixtures/optimizer/qualify_tables.sql", b"-- caf\xe9\n" + s)


@spec("fm08", "very long line")
def _():
    R("sqlglot/helper.py", 'DATE_UNITS = {"day", "week", "month", "quarter", "year", "year_month"}',
      'DATE_UNITS = {"day", "week", "month", "quarter", "year", "year_month"}  # ' + "x" * 20000)


@spec("fm09", "zero-context diff (-U0)")
def _():
    R("sqlglot/helper.py", "raise ValueError(f\"Unknown {kind} '{word}'.{similar}\")", "raise ValueError(f\"Unknown {kind}: '{word}'.{similar}\")")
    R("sqlglot/helper.py", "if t.TYPE_CHECKING:\n", "if t.TYPE_CHECKING:\n    pass\n")
    return diff("-U0")


@spec("fm10", "whole patch with CRLF line endings on added lines of a fixture")
def _():
    s = rd("tests/fixtures/optimizer/qualify_tables.sql")
    lines = s.split("\n")
    lines[3] = lines[3] + "\r"
    W("tests/fixtures/optimizer/qualify_tables.sql", "\n".join(lines))


def main():
    (OUT / "commits").mkdir(parents=True, exist_ok=True)
    kinds = {}
    for cid, kind, f in SPECS:
        reset()
        try:
            res = f()
            data = res if isinstance(res, (bytes, bytearray)) else diff()
        finally:
            reset()
        if res is None:
            assert data, f"{cid}: empty diff"
        (OUT / "commits" / f"{cid}.patch").write_bytes(data)
        kinds[cid] = kind
        print(cid, len(data), kind)
    (OUT / "kinds.json").write_text(json.dumps(kinds, indent=1))


if __name__ == "__main__":
    main()
