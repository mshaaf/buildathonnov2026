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

    def code(self, f, key):
        return self.lines.get(f, {}).get(key, {})

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

class SharedSetup(unittest.TestCase):
    calls: list = []

    @classmethod
    def setUpClass(cls):
        cls.calls.append("setup")

    @classmethod
    def tearDownClass(cls):
        cls.calls.append("teardown")

    def probe(self):
        pass

    def probe2(self):
        pass


class Hangs(unittest.TestCase):  # probe_* methods: not collected by unittest.main()
    def probe_pass(self):
        pass

    def probe_hang(self):
        while True:
            pass

    def probe_pass2(self):
        pass

    def probe_fail(self):
        self.fail("x")


def _hang_engine():
    """A real Engine (no warm-up) whose tests are the Hangs methods, run in forked sessions."""
    engine = Engine.__new__(Engine)
    engine.root = "/nonexistent-rwx-root"
    engine.tests = {i: {"id": f"{__name__}.py::Hangs::{m}"}
                    for i, m in enumerate(("probe_pass", "probe_hang", "probe_pass2", "probe_fail"))}
    engine.executor_profile = {}
    engine.executor_test = None
    engine.presetup_done = set()
    engine.warm_keys = set()
    engine.funcs = {}
    return engine


class WatchdogTests(unittest.TestCase):
    def _run(self, stop_first):
        import engine as engine_module

        seen = []

        def fake_policy(C, dur, data_only, focus=None, known=None):
            res = yield [0, 1, 2, 3], stop_first
            seen.append(dict(res))
            return {n for n, st in res.items() if st == "F"} | (set(C) - set(res))

        nums = [0, 1, 2, 3]
        with patch.object(engine_module, "WATCH_MIN_S", 0.5), patch.object(selector, "policy", fake_policy), \
                patch.multiple(selector, DUR={n: 1 for n in nums}, CPU_EST={n: 1 for n in nums},
                               CLS_OF={n: "Hangs" for n in nums}, CLS_SETUP={}):
            sched = selector.Scheduler(_hang_engine(), lanes=1, deadline=selector.time.monotonic() + 60)
            out = list(sched.run([("c", nums, {}, {}, False, {}, set(), None)]))
        return seen, out

    def test_hung_test_fails_alone_and_the_batch_continues(self):
        seen, out = self._run(stop_first=False)
        self.assertEqual(seen, [{0: "P", 1: "F", 2: "P", 3: "F"}])
        self.assertEqual(out[0][1], {1, 3})

    def test_hung_test_ends_a_stop_first_batch(self):
        seen, out = self._run(stop_first=True)
        self.assertEqual(seen, [{0: "P", 1: "F"}])
        self.assertEqual(out[0][1], {1, 2, 3})


class ImportHangTests(unittest.TestCase):
    def test_import_time_hang_is_an_import_failure(self):
        import tempfile
        import time
        import engine as engine_module

        class Importers:
            def file_mask(self, rel):
                return 0b110 if rel == "tests/zz_hang.py" else 0

        with tempfile.TemporaryDirectory() as root:
            engine = _hang_engine()
            engine.root = root
            engine.plan = lambda changes: ("reimport", "test")
            py = {"tests/__init__.py": ("", ""), "tests/zz_hang.py": ("", "while True:\n    pass\n")}

            def fake_policy(C, dur, data_only, focus=None, known=None):
                res = yield [0], True
                return set(C) - {n for n, st in res.items() if st == "P"}

            sched = selector.Scheduler(engine, lanes=1, deadline=time.monotonic() + 60)
            with patch.object(engine_module, "WATCH_MIN_S", 0.3), \
                    patch.dict(engine_module.STARTUP_EST_S, {"reimport": 0.1}), \
                    patch.object(selector, "policy", fake_policy), \
                    patch.multiple(selector, DUR={0: 1}, CPU_EST={0: 1}, CLS_OF={0: "Hangs"}, CLS_SETUP={},
                                   CMAP=[Importers()], IMPORT_OK={}, DEFERRED={}):
                start = time.monotonic()
                out = list(sched.run([("c", [0], py, {}, False, {}, set(), None)]))
                self.assertLess(time.monotonic() - start, 5)
                self.assertIs(selector.IMPORT_OK["c"], False)
                self.assertEqual(selector.DEFERRED["c"], 0b110)
        self.assertEqual(out[0][1], {0})


class VerificationTests(unittest.TestCase):
    def test_session_fixture_is_set_up_once_per_session(self):
        import engine as engine_module

        engine = Engine.__new__(Engine)
        engine.tests = {i: {"id": f"{__name__}.py::SharedSetup::{m}"} for i, m in enumerate(("probe", "probe2"))}
        for shared, expected in ((False, ["setup", "teardown"] * 2), (True, ["setup"])):
            with self.subTest(shared=shared):
                SharedSetup.calls = []
                ref = f"{__name__.replace('.', '/')}.py::SharedSetup"
                with patch.object(engine_module, "SESSION_FIXTURES", (ref,) if shared else ()), \
                        patch.object(engine_module._SessionSuite, "ready", set()):
                    fd = os.open(os.devnull, os.O_WRONLY)
                    try:
                        engine._run_tests([0], fd)
                        engine._run_tests([1], fd)
                    finally:
                        os.close(fd)
                self.assertEqual(SharedSetup.calls, expected)

    def test_pinned_code_matches_compile_and_frozensets_compare_by_content(self):
        import tempfile
        import engine as engine_module

        src = "def f(x):\n    return x in {'a', 'b', 'c'}\n"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.py")
            with open(path, "w") as f:
                f.write(src)
            code = engine_module._pinned_code(path, src)
            self.assertIs(engine_module._pinned_code(path, src), code)
        fresh = compile(src, path, "exec", dont_inherit=True)
        self.assertEqual(engine_module._sig(code.co_consts[0]), engine_module._sig(fresh.co_consts[0]))
        reordered = compile(src.replace("'a', 'b', 'c'", "'c', 'a', 'b'"), path, "exec", dont_inherit=True)
        self.assertEqual(engine_module._sig(reordered.co_consts[0]), engine_module._sig(fresh.co_consts[0]))
        other = compile(src.replace("'c'", "'d'"), path, "exec", dont_inherit=True)
        self.assertNotEqual(engine_module._sig(other.co_consts[0]), engine_module._sig(fresh.co_consts[0]))

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
        with patch.dict(selector.DUR, {0: 1, 1: 1, 2: 9000}, clear=True), patch.object(selector, "SETTLE_RATIO", 0):
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

    def test_settle_grows_in_chunks_and_rechecks_between_them(self):
        durations = {0: 1, 1: 1, 2: 3000, 3: 9000}
        cpu = {0: 1, 1: 1, 2: 100, 3: 9000}
        with patch.multiple(selector, DUR=durations, CPU_EST=cpu, CLS_SETUP={}, CLS_OF={}, FILE_OF={}, SETTLE_RATIO=0.5,
                            SETTLE_CHUNK_MS=40):
            gen = selector.policy([0, 1, 2, 3], durations, False)
            next(gen)
            self.assertEqual(gen.send({0: "P"}), ([2], False))  # [2, 1] costs 101 > 2 x 40: first chunk only
            self.assertEqual(gen.send({2: "P"}), ([1], False))
            with self.assertRaises(StopIteration) as done:
                gen.send({1: "P"})
            self.assertEqual(done.exception.value, {3})

    def test_settle_stops_when_the_batch_budget_is_spent(self):
        durations = {0: 1, 1: 1, 2: 3000, 3: 9000}
        cpu = {0: 1, 1: 1, 2: 100, 3: 9000}
        with patch.multiple(selector, DUR=durations, CPU_EST=cpu, CLS_SETUP={}, CLS_OF={}, FILE_OF={}, SETTLE_RATIO=0.5,
                            SETTLE_SCALE=[50 / 13000]):  # allowance 50 ms for this worker cost (13 s)
            gen = selector.policy([0, 1, 2, 3], durations, False)
            next(gen)
            with self.assertRaises(StopIteration) as done:
                gen.send({0: "P"})  # settling [2, 1] would cost 101 ms
            self.assertEqual(done.exception.value, {1, 2, 3})

    def test_bet_file_rule_drops_a_giant_only_on_own_file_evidence(self):
        durations = {0: 1, 1: 1, 2: 1, 3: 1, 4: 9000}
        files = {0: "a", 1: "a", 2: "g", 3: "g", 4: "g"}
        for answer, expected in (({2: "P", 3: "P"}, {0, 1}), ({2: "P", 3: "F"}, {0, 1, 3, 4})):
            with self.subTest(answer=answer), patch.multiple(
                    selector, DUR=durations, CPU_EST=durations, CLS_SETUP={}, CLS_OF=files, FILE_OF=files,
                    BET_FILE_N="all", BET_BREADTH=1.0):
                gen = selector._live([0, 1, 2, 3, 4], [0, 1, 2, 3], [4], {9}, {0})
                self.assertEqual(next(gen), ([2, 3], False))
                with self.assertRaises(StopIteration) as done:
                    gen.send(answer)
                self.assertEqual(done.exception.value, expected)

    def test_settle_after_a_failure(self):
        durations = {0: 1, 1: 1, 2: 3000, 3: 9000}
        cpu = {0: 1, 1: 1, 2: 1500, 3: 9000}
        classes = {0: "a", 1: "b", 2: "c", 3: "d"}
        cases = [
            # 2 runs, 1 failure: pass chance 1/2 x 2 s saved beats 0.5 x 1.5 s CPU (test 0's class is skipped)
            (0, {1: "P", 0: "F"}, ([2], False), {2: "P"}, {0, 3}),
            (0, {1: "F"}, None, None, {0, 1, 2, 3}),  # all runs failed: pass chance 1/3 does not pay
        ]
        for mult, first, ask, answer, expected in [(1.0,) + c[1:] for c in cases] + [(0, {1: "P", 0: "F"}, None, None, {0, 2, 3})]:
            with self.subTest(mult=mult, first=first), patch.multiple(
                    selector, DUR=durations, CPU_EST=cpu, CLS_SETUP={}, CLS_OF=classes, FILE_OF=classes,
                    SETTLE_RATIO=0.5, SETTLE_FAIL_MULT=mult):
                gen = selector.policy([0, 1, 2, 3], durations, False)
                self.assertEqual(next(gen), ([0, 1, 2], True))
                with self.assertRaises(StopIteration) as done:
                    got = gen.send(first)
                    self.assertEqual(got, ask)
                    gen.send(answer)
                self.assertEqual(done.exception.value, expected)

    def test_a_batch_cut_for_time_prints_everything_unproven(self):
        durations = {0: 1, 1: 3000, 2: 9000}
        cpu = {0: 1, 1: 100, 2: 9000}
        with patch.multiple(selector, DUR=durations, CPU_EST=cpu, CLS_SETUP={}, CLS_OF={}, FILE_OF={}, SETTLE_RATIO=0.5):
            gen = selector.policy([0, 1, 2], durations, False)
            next(gen)
            self.assertEqual(selector._drain(gen), {0, 1, 2})
            gen = selector.policy([0, 1, 2], durations, False)
            next(gen)
            gen.send({0: "P"})  # settle asks for 1; the cut leaves it (and the heavy test) printed
            self.assertEqual(selector._drain(gen), {1, 2})

    def test_hard_deadline_kills_a_batch_that_never_finishes(self):
        import time

        class StuckSession:
            def __init__(self):
                self.r, self.w = os.pipe()
                self.deadline = time.monotonic() + 3600
                self.results = {0: "P"}
                self.killed = False

            def fileno(self):
                return self.r

            def submit(self, numbers, timeout, stop_first=False, executor_only=False):
                pass

            def read(self):
                return False

            def expired(self):
                return False

            def kill(self):
                self.killed = True

            def close(self):
                os.close(self.r)
                os.close(self.w)

        class StuckEngine:
            executor_test = None

            def __init__(self):
                self.sessions = []

            def session(self, py, data, case_site):
                self.sessions.append(StuckSession())
                return self.sessions[-1]

        durations = {0: 1, 1: 1, 2: 9000}
        jobs = [(c, [0, 1, 2], {"x.py": ("a", "b")}, {}, False, {}, set(), None) for c in ("a", "b", "c")]
        with patch.multiple(selector, DUR=durations, CPU_EST=durations, CLS_SETUP={}, CLS_OF={}, FILE_OF={},
                            HARD_GRACE_S=0.2):
            engine = StuckEngine()
            start = time.monotonic()
            sched = selector.Scheduler(engine, lanes=2, deadline=start + 0.3)
            out = {cid: (final, note) for cid, final, note in sched.run(jobs)}
            self.assertLess(time.monotonic() - start, 2.0)
        self.assertTrue(all(sess.killed for sess in engine.sessions))
        # in-flight: the reported pass counts, the rest is printed; queued: static set
        self.assertEqual(out["a"], ({1, 2}, "killed at the hard deadline"))
        self.assertEqual(out["b"], ({1, 2}, "killed at the hard deadline"))
        self.assertEqual(out["c"], ({0, 1, 2}, "out of time"))

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
        fmap.lines["sqlglot/m.py"]["3:<lambda>"] = {3: 0b1000}  # entry from the PY_START supplement
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old.replace("x + 1", "x + 2"))
        self.assertEqual(mask, 0b1000, why)
        fmap.lines = {}  # nothing known at all: importers, never "unreached"
        (mask, why), _ = self.analyze(fmap, "sqlglot/m.py", old, old.replace("x + 1", "x + 2"))
        self.assertEqual(mask, 0xF0, why)

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
        # importers are deferred: needed unless the engine's session imports the patch cleanly
        (mask, why), an = self.analyze(fmap, "sqlglot/m.py", old, old.replace('"a"', '"("'))
        self.assertEqual((mask, an.deferred), (0, 0xF0), why)
        (mask, why), an = self.analyze(fmap, "sqlglot/m.py", old, old.replace("Q = 1", "Q = -2"))
        self.assertEqual((mask, an.deferred), (0, 0), why)  # a literal cannot raise; nobody reads Q
        (mask, why), an = self.analyze(fmap, "sqlglot/m.py", old, old.replace("Q = 1", "raise ValueError\nQ = 1"))
        self.assertEqual(an.deferred, 0xF0, why)
        # an edited import statement cannot be decided by importing once: static importers
        (mask, why), an = self.analyze(fmap, "sqlglot/m.py", old, old.replace("import re", "import re2"))
        self.assertEqual((mask & 0xF0, an.deferred), (0xF0, 0), why)
        # outside sqlglot/ and tests/ nothing imports it by name: static importers
        fmap.importers["setup.py"] = 0xF00
        (mask, why), an = self.analyze(fmap, "setup.py", old, old.replace('"a"', '"("'), all_mask=0xFFF)
        self.assertEqual((mask, an.deferred), (0xF00, 0), why)

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

    def test_new_module_only_reachable_by_its_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "sqlglot" / "dialects").mkdir(parents=True)
            (src / "sqlglot" / "__init__.py").write_text("")
            (src / "tests").mkdir()
            (src / "tests" / "test_d.py").write_text('Dialect.get_or_raise("duck")\n')
            fmap = FakeMap()
            files = {"tests/test_sqlglotc.py": 0b1, "tests/test_typing.py": 0b10}
            new = "diff --git a/sqlglot/dialects/{0}.py b/sqlglot/dialects/{0}.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/dialects/{0}.py\n@@ -0,0 +1 @@\n+X = 1\n"
            with patch.object(selector, "SRC", src):
                selector._WORDS.clear()
                analyzer = selector.Analyzer(fmap, 0xFF, file_tests=files)
                self.assertEqual(analyzer.analyze(new.format("fresh"))[0], 0b1)
                self.assertEqual(analyzer.analyze(new.format("duck"))[0], 0xFF)  # named in a test string
            selector._WORDS.clear()

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
