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
        return 0

    def span_mask(self, f, key, lo, hi):
        return 0

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

    def test_settle_runs_unproven_tests_worth_more_on_workers_than_in_cpu(self):
        durations = {0: 1, 1: 1, 2: 3000, 3: 9000}
        cpu = {0: 1, 1: 1, 2: 100, 3: 9000}
        for response, expected in (({1: "P", 2: "P"}, {3}), ({1: "P", 2: "F"}, {2, 3})):
            with self.subTest(response=response), patch.multiple(
                    selector, DUR=durations, CPU_EST=cpu, CLS_SETUP={}, CLS_OF={}, FILE_OF={}, SETTLE_RATIO=0.5):
                gen = selector.policy([0, 1, 2, 3], durations, False)
                self.assertEqual(next(gen), ([0, 1, 2], True))
                # 1 and 2 never reported: settling runs them, most valuable first, without stopping
                self.assertEqual(gen.send({0: "P"}), ([2, 1], False))
                with self.assertRaises(StopIteration) as done:
                    gen.send(response)
                self.assertEqual(done.exception.value, expected)

    def test_settle_is_off_after_a_failure_by_default(self):
        durations = {0: 1, 1: 3000, 2: 9000}
        cpu = {0: 1, 1: 100, 2: 9000}
        with patch.multiple(selector, DUR=durations, CPU_EST=cpu, CLS_SETUP={}, CLS_OF={}, FILE_OF={}, SETTLE_RATIO=0.5):
            gen = selector.policy([0, 1, 2], durations, False)
            self.assertEqual(next(gen), ([0, 1], True))
            with self.assertRaises(StopIteration) as done:
                gen.send({0: "F"})
            self.assertEqual(done.exception.value, {0, 1, 2})

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


if __name__ == "__main__":
    unittest.main()
