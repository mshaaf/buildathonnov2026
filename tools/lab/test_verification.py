"""Focused checks for the verifier, runnable without sqlglot or Docker."""

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rwx"))
from engine import Engine
from executor_cases import affected_cases
from executor_fixture import defer_setup
from patchlib import ast_equal
import selector


class BrokenSetup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise ValueError("setup")

    def probe(self):
        pass


class BrokenTeardown(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        raise ValueError("teardown")

    def probe(self):
        pass


class FakeMap:
    """Just enough of selector.Map for analyzer tests: file -> importers mask."""

    def __init__(self, importers=None, opens=None):
        self.importers = importers or {}
        self.masks = []
        self.opens = {}
        for f, m in (opens or {}).items():
            self.opens[f] = len(self.masks)
            self.masks.append(m)
        self.class_desc, self.class_users, self.name_refs, self.attr_refs, self.lines = {}, {}, {}, {}, {}
        self.arg_missing, self.class_by_code = {}, {}

    def file_mask(self, f):
        return self.importers.get(f, 0)

    def code_mask(self, f, key):
        m = 0
        for v in self.lines.get(f, {}).get(key, {}).values():
            m |= v
        return m

    def span_mask(self, f, key, lo, hi):
        m = 0
        for ln, v in self.lines.get(f, {}).get(key, {}).items():
            if lo <= ln <= hi:
                m |= v
        return m

    def ref_mask(self, refs, seen=None):
        return 0, False

    def class_mask(self, key):
        return 0


class VerificationTests(unittest.TestCase):
    def test_unused_and_repeated_fixtures(self):
        calls = []

        class Fixture:
            @classmethod
            def setUpClass(cls):
                calls.append("setup")
                cls.rows = []
                cls.count = len(cls.rows)

            @classmethod
            def tearDownClass(cls):
                calls.append("teardown")

        defer_setup(Fixture, ["rows", "count"])
        Fixture.setUpClass()
        Fixture.tearDownClass()
        self.assertEqual(calls, [])
        for _ in range(2):
            Fixture.setUpClass()
            self.assertEqual(Fixture().count, 0)
            self.assertEqual(Fixture.rows, [])
            Fixture().rows.append(1)
            Fixture.tearDownClass()
        self.assertEqual(calls, ["setup", "teardown"] * 2)

    def test_class_errors_override_passes_and_cover_missing_results(self):
        engine = Engine.__new__(Engine)
        for cls in (BrokenSetup, BrokenTeardown):
            with self.subTest(cls=cls):
                engine.tests = {0: {"id": f"{__name__}.py::{cls.__name__}::probe"}}
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd) as reader, os.fdopen(write_fd, "w") as writer:
                    engine._run_tests([0], writer.fileno(), stop_first=True)
                    writer.close()
                    self.assertEqual(reader.read().splitlines()[-1], "0 F")

    def test_nested_function_patch_reaches_new_instances(self):
        engine = Engine.__new__(Engine)
        engine.root = "/base"
        engine.warm_keys = set()
        old = "def make():\n    def inner(): return 1\n    return inner"
        key = ("/base/example.py", 2, "make.<locals>.inner")
        engine.funcs = {key: []}
        with patch.dict(sys.modules, example=object()):
            self.assertEqual(engine.plan({"example.py": (old, old.replace("1", "2"))})[0], "reimport")

    def test_partial_clean_batch_keeps_unreported_and_heavy_tests(self):
        with patch.dict(selector.DUR, {0: 1, 1: 1, 2: 9000}, clear=True):
            gen = selector.policy([0, 1, 2], selector.DUR, False)
            self.assertEqual(next(gen), ([0, 1], True))
            with self.assertRaises(StopIteration) as result:
                gen.send({0: "P"})
            self.assertEqual(result.exception.value, {1, 2})

    def test_final_batch_uses_the_giants_own_probe_evidence(self):
        # v4 rule: a giant is cleared only by ALL runnable candidates from its own test
        # file (here the executor tests 1-4 left after probe 1), then the final batch
        # checks the rest.
        durations = {n: 1 for n in range(7)} | {7: 9000}
        files = {n: "executor" if n <= 4 or n == 7 else "parser" for n in durations}
        cases = [
            ({5: "P", 6: "P"}, set()),
            ({5: "P", 6: "F"}, {6}),
            ({5: "F"}, {5, 6}),
            ({}, {5, 6, 7}),
        ]
        for response, expected in cases:
            with self.subTest(response=response), patch.multiple(selector, DUR=durations, FILE_OF=files):
                with patch.object(selector, "_spread", side_effect=[[0], []]):
                    gen = selector._probe_then_decide(list(durations), list(range(7)), [7], durations, {})
                    self.assertEqual(next(gen), ([0], True))
                    self.assertEqual(gen.send({0: "P"}), ([1, 2, 3, 4], True))
                    self.assertEqual(gen.send({1: "P", 2: "P", 3: "P", 4: "P"}), ([5, 6], True))
                    with self.assertRaises(StopIteration) as done:
                        gen.send(response)
                    self.assertEqual(done.exception.value, expected)

    def test_unmodeled_changes_cannot_be_verified_on_the_base_tree(self):
        analyzer = selector.Analyzer(FakeMap({"sqlglot/x.py": 2}), 7)
        mask, _ = analyzer.analyze("diff --git a/sqlglot/x.py b/sqlglot/x.py\ndeleted file mode 100644\n--- a/sqlglot/x.py\n+++ /dev/null\n")
        self.assertEqual(mask, 2)  # importers of the removed module
        self.assertFalse(analyzer.py_changes)
        mask, _ = analyzer.analyze("diff --git a/sqlglot/y.py b/sqlglot/y.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/y.py\n@@ -0,0 +1 @@\n+X = 1\n")
        self.assertEqual(mask, 7)  # a new module can be imported by a computed name
        self.assertFalse(analyzer.py_changes)

    def test_tokenizer_readers_only_narrow_an_isolated_literal_flip(self):
        analyzer = selector.Analyzer(FakeMap(), 15, lazy_mask=8)
        analyzer.tokenizer = {"classes": ["sqlglot/demo.py:1:Tokenizer"], "flags": {"FLAG": "0x2"}}
        base = "class Tokenizer:\n    FLAG = False\n"
        diff = "diff --git a/sqlglot/demo.py b/sqlglot/demo.py\n@@ -2 +2 @@\n-    FLAG = False\n+    FLAG = "
        with patch.object(analyzer, "old_source", return_value=base), patch.object(analyzer, "site_mask", return_value=15):
            self.assertEqual(analyzer.analyze(diff + "True\n")[0], 10)
            self.assertEqual(analyzer.analyze(diff + "bool(1)\n")[0], 15)
            self.assertEqual(analyzer.analyze(diff + "True\ndiff --git a/README b/README\n")[0], 15)

    def test_executor_coverage_falls_back_if_shared_code_changed(self):
        source = "sqlglot/parser.py"
        changed = [(("/base/" + source, 10, "Parser.parse"), None)]
        profile = {"coverage": {source + ":10:Parser.parse": "0x6"},
                   "lines": {source + ":10:Parser.parse": {"20": "0x4"}}, "ms": [20, 30]}
        self.assertEqual(affected_cases(profile, "/base", changed), [0, 1])
        self.assertEqual(affected_cases(profile, "/base", changed, (source, "10:parse", (20, 20))), [1])
        profile["coverage"][source + ":10:Parser.parse"] = "0x7"
        self.assertIsNone(affected_cases(profile, "/base", changed, (source, "10:parse", (20, 20))))

    def test_ast_equality(self):
        self.assertTrue(ast_equal(ast.parse("x = 1"), "# comment\nx=(1)"))
        self.assertFalse(ast_equal("x = 1", "x = True"))
        self.assertFalse(ast_equal("x = '''hello'''", "x = '''# hello'''"))
        self.assertIsNone(ast_equal("x = 1", "x = "))


def _diff(path, old, new):
    import difflib

    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + path, "b/" + path, n=1))
    return f"diff --git a/{path} b/{path}\n" + body


class RobustnessTests(unittest.TestCase):
    """Unusual patches: the analyzer must select a superset, main() must stay valid."""

    def analyze(self, fmap, path, old, new, all_mask=0xFF):
        analyzer = selector.Analyzer(fmap, all_mask)
        with patch.object(analyzer, "old_source", return_value=old):
            return analyzer.analyze(_diff(path, old, new)), analyzer

    def test_one_line_lambda_is_charged_to_its_creating_statement(self):
        # coverage never records a code object's first line: the lambda itself shows no runs
        old = "def make():\n    x = 1\n    return lambda: x + 1\n"
        fmap = FakeMap({"sqlglot/m.py": 0xF0})
        fmap.lines = {"sqlglot/m.py": {"1:make": {2: 0b100, 3: 0b100}}}
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old.replace("x + 1", "x + 2"))
        self.assertEqual(mask, 0b100, why)

    def test_one_line_def_is_charged_to_readers_of_its_name(self):
        old = "import re\n\n\ndef f(): return 1\n\n\nY = 2\n"
        fmap = FakeMap({"sqlglot/m.py": 0xF0})
        fmap.name_refs = {"f": [("sqlglot/x.py", "5:g", "func")]}
        fmap.ref_mask = lambda refs, seen=None: (0b10 if refs else 0, False)
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old.replace("return 1", "return 2"))
        self.assertEqual(mask, 0b10, why)

    def test_import_time_statement_that_may_raise_selects_importers(self):
        old = 'import re\nP = re.compile("a")\nQ = 1\n'
        fmap = FakeMap({"sqlglot/m.py": 0xF0})
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old.replace('"a"', '"("'))
        self.assertEqual(mask, 0xF0, why)
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old.replace("Q = 1", "Q = -2"))
        self.assertEqual(mask, 0, why)  # a literal cannot raise; nobody reads Q
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old + "raise ValueError\n")
        self.assertEqual(mask, 0xF0, why)

    def test_encoding_changes_select_importers_statically(self):
        old = "# caf\u00e9\nX = 1\n"
        fmap = FakeMap({"sqlglot/m.py": 0xF0})
        (mask, why), an = self.analyze(fmap, "sqlglot/m.py", old, "# -*- coding: ascii -*-\n" + old)
        self.assertEqual(mask, 0xF0, why)
        self.assertFalse(an.py_changes)  # the engine cannot model decoding
        (mask, why), an = self.analyze(fmap, "sqlglot/m.py", old, old.replace("caf\u00e9", "caf\udce9"))
        self.assertEqual(mask, 0xF0, why)

    def test_repeated_entries_and_moved_hunks(self):
        from patchlib import PatchMismatch, apply_hunks, locate_hunks, parse_patch

        old = "".join(f"x{i} = {i}\n" for i in range(20))
        mid = old.replace("x3 = 3", "x3 = 30")
        new = mid.replace("x15 = 15", "x15 = 150")
        text = _diff("t.py", old, mid) + _diff("t.py", mid, new)
        fmap = FakeMap({"t.py": 1})
        analyzer = selector.Analyzer(fmap, 0xFF)
        with patch.object(analyzer, "old_source", return_value=old):
            analyzer.analyze(text)
        self.assertEqual(analyzer.py_changes["t.py"][1], new)
        fp = parse_patch(_diff("t.py", old, mid))[0]
        shifted = "a = 0\n" + old
        self.assertEqual(apply_hunks(shifted, locate_hunks(shifted, fp.hunks)), "a = 0\n" + mid)
        with self.assertRaises(PatchMismatch):
            locate_hunks(old.replace("x3 = 3", "zz"), fp.hunks)

    def test_headers_without_prefixes(self):
        from patchlib import parse_patch

        fp = parse_patch("diff --git sqlglot/a.py sqlglot/a.py\n--- sqlglot/a.py\n+++ sqlglot/a.py\n@@ -1 +1 @@\n-x\n+y\n")[0]
        self.assertEqual((fp.old_path, fp.new_path, len(fp.hunks)), ("sqlglot/a.py", "sqlglot/a.py", 1))
        fp = parse_patch("--- a/s.py\t2026-01-01\r\n+++ b/s.py\t2026-01-01\r\n@@ -1 +1 @@\r\n-x\r\n+y\r\n")[0]
        self.assertEqual((fp.old_path, fp.new_path), ("s.py", "s.py"))

    def test_main_survives_odd_inputs_and_stays_small(self):
        import json
        import subprocess
        import tempfile

        root = Path(__file__).resolve().parents[2]
        tests = [{"number": i, "id": f"tests/test_x.py::T::t{i}", "file": "tests/test_x.py", "seconds": 0.001} for i in range(1620)]
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "commits").mkdir()
            (data / "tests.json").write_text(json.dumps({"tests": tests, "skipped": []}))
            odd = {"empty": b"", "garbage": b"\x00\xff\xfe not a patch", "nohunk": b"diff --git a/x b/x\n",
                   "bad": b"diff --git a/sqlglot/x.py b/sqlglot/x.py\n@@ -1 +1 @@\n-\xff\n+\xfe\n"}
            for i in range(100):
                name = list(odd)[i % len(odd)]
                (data / "commits" / f"c{i:03d}.patch").write_bytes(odd[name])
            (data / "commits" / "dir.patch").mkdir()
            (data / "commits" / "notes.txt").write_text("x")
            (data / "commits" / ".patch").write_text("")
            env = dict(os.environ, RWX_DATA=str(data), RWX_SRC=str(data / "nosrc"), RWX_ENGINE="0")
            r = subprocess.run([sys.executable, str(root / "rwx" / "selector.py")], env=env, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        lines = r.stdout.decode("utf-8").splitlines()
        self.assertEqual(sorted(json.loads(l)["commit"] for l in lines), [f"c{i:03d}" for i in range(100)])
        self.assertLess(len(r.stdout), 1_000_000)  # 2 MB limit, worst case: everything for 100 commits
        widest = 0
        for l in lines:
            w = json.loads(l)["workers"]
            self.assertLessEqual(len(w), 8)
            self.assertEqual(sum(map(len, w)), len({n for x in w for n in x}))
            if sum(map(len, w)) == 1620:
                widest = max(widest, len(l) + 1)
        self.assertGreater(widest, 0)  # unanalyzable patches select everything
        self.assertLess(widest * 150, 2_000_000)  # even 150 commits selecting everything fit


if __name__ == "__main__":
    unittest.main()
