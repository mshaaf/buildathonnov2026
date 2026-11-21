"""Focused checks for the verifier, runnable without sqlglot or Docker."""

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rwx"))
from engine import Engine
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
        durations = {n: 1 for n in range(7)} | {7: 9000}
        files = {n: "executor" if n <= 4 or n == 7 else "parser" for n in durations}
        cases = [
            ({4: "P", 5: "P", 6: "P"}, set()),
            ({4: "P", 5: "F"}, {5, 6}),
            ({4: "F"}, {4, 5, 6, 7}),
            ({}, {4, 5, 6, 7}),
        ]
        for response, expected in cases:
            with self.subTest(response=response), patch.multiple(selector, DUR=durations, FILE_OF=files):
                with patch.object(selector, "_spread", side_effect=[[0], [1, 2, 3], []]):
                    gen = selector._probe_then_decide(list(durations), list(range(7)), [7], durations, {})
                    self.assertEqual(next(gen), ([0], True))
                    self.assertEqual(gen.send({0: "P"}), ([1, 2, 3], True))
                    self.assertEqual(gen.send({1: "P", 2: "P", 3: "P"}), ([4, 5, 6], True))
                    with self.assertRaises(StopIteration) as done:
                        gen.send(response)
                    self.assertEqual(done.exception.value, expected)

    def test_unmodeled_changes_cannot_be_verified_on_the_base_tree(self):
        analyzer = selector.Analyzer(None, 7)
        mask, _ = analyzer.analyze("diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n")
        self.assertEqual(mask, 7)
        self.assertFalse(analyzer.py_changes)

    def test_ast_equality(self):
        self.assertTrue(ast_equal(ast.parse("x = 1"), "# comment\nx=(1)"))
        self.assertFalse(ast_equal("x = 1", "x = True"))
        self.assertFalse(ast_equal("x = '''hello'''", "x = '''# hello'''"))
        self.assertIsNone(ast_equal("x = 1", "x = "))


if __name__ == "__main__":
    unittest.main()
