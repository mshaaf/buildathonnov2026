"""Runtime test engine: run chosen sqlglot tests against a patched tree, cheaply.

A warm parent imports sqlglot and the test modules once. For each commit it forks
a child that applies the patch, runs the requested tests with unittest and streams
one result line per test back through a pipe.

Applying the patch in the child:
  * swap: when every changed code object is a function (not a module or class body)
    that never ran while the parent warmed up, the child replaces __code__ on the
    live function objects. Nothing computed at import time can depend on them.
  * reimport: otherwise the child drops every sqlglot/tests module and imports
    them again, serving the patched sources through an import hook.
Fixture and other data files are served patched through open().
"""

from __future__ import annotations

import builtins
import gc
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import select as _select
import signal
import sys
import time
import types
import unittest
from pathlib import Path

PKG_PREFIXES = ("sqlglot", "tests")
TPCDS_SAMPLE_MS = int(os.environ.get("RWX_TPCDS_SAMPLE_MS", "0"))
WATCH_MIN_S = 4.0  # progress watchdog: shortest wait for the next result line
WATCH_STARTUP_S = 10.0  # ... and for the child's first line (patching, re-import, import check)


class InlinePool:
    """ProcessPoolExecutor stand-in: runs work in-process, pickling it like a real pool."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        import pickle

        self._p = pickle
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        import concurrent.futures as cf

        fut = cf.Future()
        p = self._p
        try:
            fn2, a2, k2 = p.loads(p.dumps((fn, args, kwargs)))
            fut.set_result(p.loads(p.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, **kw):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_inline_pool():
    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlinePool
    concurrent.futures.process.ProcessPoolExecutor = InlinePool


# ------------------------------------------------------------------ code pairing


def _walk_codes(code, out, prefix=()):
    counts: dict = {}
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            k = c.co_qualname
            i = counts.get(k, 0)
            counts[k] = i + 1
            key = prefix + ((k, i),)
            out[key] = c
            _walk_codes(c, out, key)
    return out


def _sig(code):
    body = "__firstlineno__" in code.co_names
    consts = tuple(
        ("<code>", c.co_qualname)
        if isinstance(c, types.CodeType)
        else ("<firstlineno>",)
        if body and type(c) is int and c == code.co_firstlineno
        # A frozenset's repr follows its hash/insertion order, which differs between a
        # .pyc-loaded and a freshly compiled copy of the same source: compare contents.
        else ("frozenset", tuple(sorted(map(repr, c))))
        if type(c) is frozenset
        else (type(c).__name__, repr(c))
        for c in code.co_consts
    )
    return (
        code.co_code,
        consts,
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_flags,
        code.co_exceptiontable,
    )


_OLD_CODES: dict = {}  # path -> (source, code) of pinned sources compiled once
NEW_CODES: dict = {}  # path -> (source, code) of the patched sources the last plan compiled


def _pinned_code(path: str, src: str):
    """Code object of a pinned source: from its build-time .pyc when that matches the
    file and the file matches src (same code compile() makes), else compiled. Cached."""
    hit = _OLD_CODES.get(path)
    if hit is not None and hit[0] == src:
        return hit[1]
    code = None
    try:
        loader = importlib.machinery.SourceFileLoader("_rwx_pinned", path)
        if importlib.util.decode_source(loader.get_data(path)) == src:
            code = loader.get_code("_rwx_pinned")
    except Exception:  # noqa: BLE001
        code = None
    if code is None:
        code = compile(src, path, "exec", dont_inherit=True)
    _OLD_CODES[path] = (src, code)
    return code


def diff_codes(path: str, old_src: str, new_src: str):
    """Returns (changed list of (old_code, new_code), body_changed: bool) or None if unpairable."""
    try:
        old_root = _pinned_code(path, old_src)
        new_root = compile(new_src, path, "exec", dont_inherit=True)
    except SyntaxError:
        return None
    NEW_CODES[path] = (new_src, new_root)
    body_changed = _sig(old_root) != _sig(new_root)
    old = _walk_codes(old_root, {})
    new = _walk_codes(new_root, {})
    if old.keys() != new.keys():
        return None
    changed = []
    for k, oc in old.items():
        nc = new[k]
        if _sig(oc) != _sig(nc):
            changed.append((oc, nc))
    return changed, body_changed


def is_body_code(code) -> bool:
    """Class bodies store __qualname__/__module__; functions never do that first."""
    return "__qualname__" in code.co_names and "__module__" in code.co_names and code.co_argcount == 0


# ------------------------------------------------------------------ import hook


class PatchedFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sources: dict[str, str], root: str):
        self.by_path = sources  # absolute path -> source
        self.root = root

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(PKG_PREFIXES):
            return None
        rel = fullname.replace(".", "/")
        for cand, is_pkg in ((f"{self.root}/{rel}.py", False), (f"{self.root}/{rel}/__init__.py", True)):
            if cand in self.by_path:
                spec = importlib.util.spec_from_file_location(
                    fullname, cand, loader=self, submodule_search_locations=[cand.rsplit("/", 1)[0]] if is_pkg else None
                )
                return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = module.__spec__.origin
        src = self.by_path[path]
        hit = NEW_CODES.get(path)  # compiled by the parent's plan() before the fork
        code = hit[1] if hit is not None and hit[0] is src else compile(src, path, "exec", dont_inherit=True)
        module.__file__ = path
        exec(code, module.__dict__)


# ------------------------------------------------------------------ test running


# Class fixtures kept alive for the whole session once set up: later batches of the
# same session reuse them instead of paying setUpClass again (about 1.5 s for
# TestExecutor, which a re-import session would otherwise repeat per batch). This is
# what the parent's shared setup already does for swap sessions, and what pytest does
# within one run: one setUpClass for all of a class's tests.
SESSION_FIXTURES = ("tests/test_executor.py::TestExecutor",)


def _class_ref(cls) -> str:
    return f"{cls.__module__.replace('.', '/')}.py::{cls.__qualname__}"


class _SessionSuite(unittest.TestSuite):
    ready: set = set()  # classes whose setUpClass succeeded in this process

    def _handleClassSetUp(self, test, result):
        cls = test.__class__
        if cls in self.ready:
            if getattr(result, "_previousTestClass", None) is not cls:
                try:
                    cls._classSetupFailed = False
                except TypeError:
                    pass
            return
        super()._handleClassSetUp(test, result)
        if (_class_ref(cls) in SESSION_FIXTURES and not getattr(cls, "_classSetupFailed", False)
                and not getattr(result, "_moduleSetUpFailed", False)
                and not getattr(cls, "__unittest_skip__", False)):
            self.ready.add(cls)

    def _tearDownPreviousClass(self, test, result):
        if getattr(result, "_previousTestClass", None) in self.ready:
            return  # keep it set up for the session's later batches
        super()._tearDownPreviousClass(test, result)


class _StopTest(Exception):
    """Raised from addSubTest to end a test at its first failing subtest."""


class StreamResult(unittest.TestResult):
    def __init__(self, fd, ids):
        super().__init__()
        self.fd = fd
        self.ids = ids  # str(test.id()) -> number
        self.cur_bad = False
        self.fixture_error = False
        self.any_bad = False
        self.stop_first = False

    def _num(self, test):
        return self.ids.get(test.id())

    def _exc_info_to_string(self, err, test):
        # A failure only needs to be known, not explained: unittest would format the
        # full traceback (source lines, chained exceptions) for every failing test.
        return ""

    def startTest(self, test):
        super().startTest(test)
        self.cur_bad = False
        self.t0 = time.process_time()

    def addError(self, test, err):
        super().addError(test, err)
        self.cur_bad = True
        if self._num(test) is None:
            self.fixture_error = True

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cur_bad = True

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.cur_bad = True
            raise _StopTest()

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.cur_bad = True

    def stopTest(self, test):
        super().stopTest(test)
        n = self._num(test)
        if n is not None:
            os.write(self.fd, f"{n} {'F' if self.cur_bad else 'P'} {time.process_time() - self.t0:.4f}\n".encode())
        if self.cur_bad:
            self.any_bad = True
            if self.stop_first:
                self.stop()


class Engine:
    def __init__(self, src_root: str, tests: list[dict], log=None, presetup=(), needed=None):
        self.root = src_root.rstrip("/")
        self.log = log or (lambda *a: None)
        self.tests = {t["number"]: t for t in tests}
        from executor_cases import PROFILE, TEST_ID

        self.executor_test = next((n for n, test in self.tests.items() if test["id"] == TEST_ID), None)
        profile = Path(self.root) / PROFILE
        try:
            self.executor_profile = json.loads(profile.read_text()) if self.executor_test is not None and profile.is_file() else {}
        except (OSError, ValueError) as e:  # a bad manifest only loses this optimization
            self.log(f"executor profile unusable: {e!r}")
            self.executor_profile = {}
        self.warm_codes: set = set()
        self.presetup_done: set = set()
        install_inline_pool()
        if self.root not in sys.path:
            sys.path.insert(0, self.root)
        mods = sorted({t["id"].split("::")[0][:-3].replace("/", ".") for t in tests
                       if needed is None or t["number"] in needed})
        self._warm(mods, presetup)
        self._index_functions()
        _LazyDuckModule.recording = False
        # Keep forked children from touching (and so copying) every inherited object:
        # the collector ignores frozen objects.
        gc.collect()
        gc.freeze()
        gc.enable()  # main() runs start-up with the collector off; children get it on

    def _warm(self, mods, presetup=()):
        mon = sys.monitoring
        tool = mon.PROFILER_ID
        root = self.root + "/"
        seen = self.warm_codes

        def on_start(code, offset):
            if code.co_filename.startswith(root):
                seen.add(code)
            return mon.DISABLE

        mon.use_tool_id(tool, "rwxwarm")
        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.PY_START)
        try:
            import sqlglot  # noqa: F401

            for m in mods:
                try:
                    importlib.import_module(m)
                except Exception as e:  # noqa: BLE001
                    self.log(f"warm import failed {m}: {e!r}")
            for ref in presetup:
                try:
                    self._presetup(ref)
                except Exception as e:  # noqa: BLE001
                    self.log(f"presetup failed {ref}: {e!r}")
        finally:
            mon.set_events(tool, 0)
            mon.register_callback(tool, mon.events.PY_START, None)
            mon.free_tool_id(tool)
        self.warm_keys = {(c.co_filename, c.co_firstlineno, c.co_qualname) for c in seen}

    def _presetup(self, ref: str):
        """Run a test class's setUpClass once here, so forked children share its data.
        Code it runs is recorded as warm-up code: a patch touching it makes the child
        re-import (and so set up afresh). duckdb connections are made lazy: they record
        statements here and open for real, replaying them, on first use in a child."""
        path, cls_name = ref.split("::")
        mod = importlib.import_module(path[:-3].replace("/", "."))
        klass = getattr(mod, cls_name)
        if getattr(klass, "__unittest_skip__", False):
            return
        real = getattr(mod, "duckdb", None)
        if real is not None:
            mod.duckdb = _LazyDuckModule(real)
        try:
            klass.setUpClass()
        finally:
            if real is not None:
                mod.duckdb = real
        klass.setUpClass = classmethod(lambda cls: None)
        klass.tearDownClass = classmethod(lambda cls: None)
        self.presetup_done.add(ref)
        self.log(f"presetup done: {ref}")

    def _index_functions(self):
        root = self.root + "/"
        idx: dict = {}
        for o in gc.get_objects():
            if type(o) is types.FunctionType:
                c = o.__code__
                if c.co_filename.startswith(root):
                    idx.setdefault((c.co_filename, c.co_firstlineno, c.co_qualname), []).append(o)
        self.funcs = idx

    # -------------------------------------------------------------- planning

    def plan(self, changes: dict[str, tuple[str, str]]):
        """changes: rel path -> (old_text, new_text) for .py files. Returns ('swap', swaps) or ('reimport', None)."""
        swaps = []
        for rel, (old, new) in changes.items():
            path = f"{self.root}/{rel}"
            modname = rel[:-3].replace("/", ".")
            if modname.endswith(".__init__"):
                modname = modname[: -len(".__init__")]
            loaded = modname in sys.modules
            d = diff_codes(path, old, new)
            if d is None:
                return "reimport", f"unpairable {rel}"
            changed, body_changed = d
            if body_changed:
                return "reimport", f"module body {rel}"
            for oc, nc in changed:
                if is_body_code(oc):
                    return "reimport", f"class body {oc.co_qualname}"
                key = (path, oc.co_firstlineno, oc.co_qualname)
                if key in self.warm_keys:
                    return "reimport", f"ran at import {oc.co_qualname}"
                if oc.co_freevars != nc.co_freevars:
                    return "reimport", f"freevars {oc.co_qualname}"
                if not loaded:
                    continue  # module not imported yet: the hook serves the patched source
                if "<locals>" in oc.co_qualname.split(".") or key not in self.funcs:
                    # A factory could create a fresh function from its old co_consts.
                    return "reimport", f"cannot replace every instance of {oc.co_qualname}"
                swaps.append((key, nc))
        return "swap", swaps

    # -------------------------------------------------------------- running

    def run(self, py_changes: dict, data_changes: dict, numbers: list[int], timeout: float):
        """Fork a child, apply changes, run tests. Returns ({number: 'P'|'F'}, info)."""
        mode, swaps = self.plan(py_changes)
        self.last_cpu = {}
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            code = 0
            try:
                self._child(mode, swaps, py_changes, data_changes, numbers, wfd)
            except BaseException as e:  # noqa: BLE001
                try:
                    os.write(wfd, f"X {e!r}\n".encode())
                except Exception:  # noqa: BLE001
                    pass
                code = 1
            finally:
                os._exit(code)
        os.close(wfd)
        results: dict = {}
        buf = b""
        deadline = time.monotonic() + timeout
        timed_out = False
        crash = None
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            r, _, _ = _select.select([rfd], [], [], left)
            if not r:
                continue
            chunk = os.read(rfd, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                parts = line.decode().split(" ", 1)
                if parts[0] == "X":
                    crash = parts[1] if len(parts) > 1 else "?"
                else:
                    st = parts[1].split(" ")
                    results[int(parts[0])] = st[0]
                    if len(st) > 1:
                        self.last_cpu[int(parts[0])] = float(st[1])
        os.close(rfd)
        if timed_out:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.waitpid(pid, 0)
        # anything that did not report ran into trouble: count it as failing
        for n in numbers:
            results.setdefault(n, "F")
        return results, {"mode": mode, "why": swaps if mode == "reimport" else len(swaps), "timeout": timed_out, "crash": crash}

    def session(self, py_changes: dict, data_changes: dict, case_site=None):
        return Session(self, py_changes, data_changes, case_site)

    def _child_loop(self, mode, swaps, py_changes, data_changes, cmd_fd, wfd, executor_indices):
        self._apply(mode, swaps, py_changes, data_changes)
        self._check_imports(mode, py_changes, wfd)
        rbuf = b""
        while True:
            while b"\n" not in rbuf:
                chunk = os.read(cmd_fd, 65536)
                if not chunk:
                    return
                rbuf += chunk
            line, rbuf = rbuf.split(b"\n", 1)
            parts = line.decode().split()
            if not parts or parts[0] == "Q":
                return
            if parts[0] == "E":
                from executor_cases import install

                install(executor_indices)
            stop_first = parts[0] == "R1"
            self._run_tests([int(x) for x in parts[1:]], wfd, stop_first)
            os.write(wfd, b"D\n")

    def _check_imports(self, mode, py_changes, wfd):
        """Import every changed module once, right after patching, and report on the result
        pipe: "M ok" (all imported from the patched source), "M fail <rel>..." or "M skip"
        (some module was not re-imported here, e.g. swap mode). Import-time statements run
        exactly once, at import: "M ok" proves none of them raises."""
        bad, skipped = [], False
        for rel in py_changes:
            if not rel.endswith(".py") or not rel.startswith(("sqlglot/", "tests/")):
                skipped = True
                continue
            name = rel[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            if mode != "reimport" and name in sys.modules:
                skipped = True  # not re-executed in this session
                continue
            try:
                importlib.import_module(name)
            except BaseException:  # noqa: BLE001
                bad.append(rel)
        msg = f"M fail {' '.join(bad)}" if bad else "M skip" if skipped else "M ok"
        os.write(wfd, (msg + "\n").encode())

    def _child(self, mode, swaps, py_changes, data_changes, numbers, wfd):
        self._apply(mode, swaps, py_changes, data_changes)
        self._run_tests(numbers, wfd)

    def _apply(self, mode, swaps, py_changes, data_changes):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        sys.stdout = sys.stderr = open(os.devnull, "w")
        sources = {f"{self.root}/{rel}": new for rel, (old, new) in py_changes.items()}
        if mode == "reimport":
            for name in list(sys.modules):
                if name.split(".")[0] in PKG_PREFIXES:
                    del sys.modules[name]
            sys.meta_path.insert(0, PatchedFinder(sources, self.root))
            importlib.invalidate_caches()
        else:
            if sources:
                sys.meta_path.insert(0, PatchedFinder(sources, self.root))
            for key, nc in swaps:
                for f in self.funcs.get(key, ()):
                    f.__code__ = nc
        import difflib

        # Failure messages are never read: skip building (possibly huge) diffs.
        difflib.ndiff = lambda *a, **k: iter(())
        difflib.unified_diff = lambda *a, **k: iter(())
        difflib.context_diff = lambda *a, **k: iter(())
        # ... nor the reprs/pprints unittest's assert methods put into messages before
        # raising (only unittest.case's own names: sqlglot's messages are untouched).
        import unittest.case as _case

        _case.safe_repr = lambda obj, short=False: "?"
        _case._common_shorten_repr = lambda *args: ("?",) * len(args)
        _case.pprint = types.SimpleNamespace(pformat=lambda *a, **k: "")
        if data_changes:
            self._patch_open({f"{self.root}/{rel}": new for rel, new in data_changes.items()})
        elif (
            py_changes
            and all(rel.startswith("sqlglot/") for rel in py_changes)
            and (mode == "reimport" or "tests/test_executor.py::TestExecutor" not in self.presetup_done)
        ):
            # Deferred fixture only when the parent's shared setup is not in use (a
            # re-import builds a fresh class): wrapping a shared class would replace
            # its data with lazy fields. install() checks that no source the setup
            # runs is patched, so the setup cannot fail where the pinned one passed.
            from executor_fixture import install

            install(self.root, py_changes)

    def _patch_open(self, files: dict):
        real_open = builtins.open

        def patched_open(file, mode="r", *a, **kw):
            try:
                p = os.path.abspath(os.fspath(file))
            except TypeError:
                p = None
            if p in files and ("r" in mode or mode == "") and "+" not in mode and "w" not in mode:
                data = files[p]
                if "b" in mode:
                    return io.BytesIO(data.encode("utf-8"))
                return io.StringIO(data)
            return real_open(file, mode, *a, **kw)

        builtins.open = patched_open
        io.open = patched_open

    def run_order(self, numbers) -> list:
        """The order in which _run_tests runs (and reports) these tests: grouped by class,
        classes in order of first appearance."""
        by_class: dict = {}
        for n in numbers:
            path, cls, _ = self.tests[n]["id"].split("::")
            by_class.setdefault((path, cls), []).append(n)
        return [n for group in by_class.values() for n in group]

    def _run_tests(self, numbers, wfd, stop_first=False):
        by_class: dict = {}
        ids = {}
        for n in numbers:
            tid = self.tests[n]["id"]
            path, cls, meth = tid.split("::")
            mod = path[:-3].replace("/", ".")
            by_class.setdefault((mod, cls), []).append(meth)
            ids[f"{mod}.{cls}.{meth}"] = n
        for (mod, cls), meths in by_class.items():
            try:
                m = importlib.import_module(mod)
                klass = getattr(m, cls)
            except BaseException:  # noqa: BLE001
                for meth in meths:
                    os.write(wfd, f"{ids[f'{mod}.{cls}.{meth}']} F\n".encode())
                continue
            suite = _SessionSuite([klass(meth) for meth in meths])
            result = StreamResult(wfd, ids)
            result.stop_first = stop_first
            _SessionSuite([suite]).run(result)
            if result.fixture_error:
                # unittest reports setup/teardown errors against a synthetic
                # holder. A teardown error can arrive after a passing result.
                for meth in meths:
                    os.write(wfd, f"{ids[f'{mod}.{cls}.{meth}']} F\n".encode())
            if stop_first and (result.any_bad or result.fixture_error):
                return


class _LazyConn:
    """duckdb connection stand-in: records statements until first real use, then opens
    a real connection (in whichever process uses it) and replays them."""

    def __init__(self, real_module, args, kwargs):
        self._m, self._a, self._k = real_module, args, kwargs
        self._log = []
        self._conn = None

    def _open(self):
        if self._conn is None:
            self._conn = self._m.connect(*self._a, **self._k)
            for q, a, k in self._log:
                self._conn.execute(q, *a, **k)
        return self._conn

    def execute(self, q, *a, **k):
        if self._conn is None and _LazyDuckModule.recording:
            self._log.append((q, a, k))
            return self
        return self._open().execute(q, *a, **k)

    def __getattr__(self, name):
        return getattr(self._open(), name)


class _LazyDuckModule:
    recording = True

    def __init__(self, real):
        self._real = real

    def connect(self, *a, **k):
        return _LazyConn(self._real, a, k)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Session:
    """One forked child holding one commit's patched tree; runs test batches on request."""

    def __init__(self, engine: Engine, py_changes: dict, data_changes: dict, case_site=None):
        self.mode, swaps = engine.plan(py_changes)
        self.info = swaps if self.mode == "reimport" else len(swaps)
        executor_indices = None
        self.executor_ms = None
        self.executor_partial = None
        if (engine.executor_profile and not data_changes and len(py_changes) == 1
                and self.mode == "swap" and swaps
                and next(iter(py_changes)).startswith("sqlglot/")):
            from executor_cases import affected_cases

            case_indices = affected_cases(engine.executor_profile, engine.root, swaps, case_site)
            if case_indices is not None:
                ms = engine.executor_profile["ms"]
                case_ms = sum(ms[i] for i in case_indices)
                if case_ms <= 5000:
                    executor_indices = case_indices
                    self.executor_ms = case_ms
                    self.executor_partial = False
                elif case_indices:
                    # Too many to verify: keep the cheapest affected queries as a gate
                    # for the case where the policy would leave TPC-DS out unrun.
                    picked, used = [], 0
                    for i in sorted(case_indices, key=lambda i: (ms[i], i)):
                        if used + ms[i] > TPCDS_SAMPLE_MS:
                            break
                        picked.append(i)
                        used += ms[i]
                    if picked:
                        executor_indices = sorted(picked)
                        self.executor_ms = used
                        self.executor_partial = True
        cmd_r, cmd_w = os.pipe()
        res_r, res_w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(cmd_w)
            os.close(res_r)
            code = 0
            try:
                engine._child_loop(self.mode, swaps, py_changes, data_changes, cmd_r, res_w, executor_indices)
            except BaseException as e:  # noqa: BLE001
                try:
                    os.write(res_w, f"X {e!r}\n".encode())
                except Exception:  # noqa: BLE001
                    pass
                code = 1
            finally:
                os._exit(code)
        os.close(cmd_r)
        os.close(res_w)
        self.engine = engine
        self.order: list = []  # current batch in engine order (progress watchdog only)
        self.windows: dict | None = None  # test -> seconds it may run without a result line
        self.mark = 0.0  # last progress: batch submitted or a line arrived
        self.progressed = False  # any line ever arrived from this child (the first is "M")
        self.pid = pid
        self.cmd = cmd_w
        self.res = res_r
        self.buf = b""
        self.alive = True
        self.pending: list = []
        self.results: dict = {}
        self.cpu: dict = {}
        self._deadline = None
        self.crash = None
        self.import_ok = None  # set by the child's "M" line: changed modules imported cleanly
        self.total_cpu = 0.0
        self.maxrss_mb = 0.0

    def fileno(self):
        return self.res

    def submit(self, numbers, timeout: float, stop_first: bool = False, executor_only: bool = False,
               windows: dict | None = None):
        """windows: per-test seconds without a result line before the batch counts as hung
        (progress watchdog, replaces `timeout`); results arrive in run_order()."""
        self.pending = list(numbers)
        self.results = {}
        self.mark = time.monotonic()
        self._deadline = self.mark + timeout
        self.windows = windows
        self.order = self.engine.run_order(numbers) if windows is not None else []
        if not self.alive:
            return
        try:
            command = "E" if executor_only else "R1" if stop_first else "R"
            os.write(self.cmd, (command + " " + " ".join(map(str, numbers)) + "\n").encode())
        except OSError:
            self._dead()

    def read(self) -> bool:
        """Consume output; True once the current batch is finished."""
        try:
            chunk = os.read(self.res, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._dead()
            return True
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        if lines:
            self.mark = time.monotonic()
            self.progressed = True
        done = False
        for line in lines:
            parts = line.decode(errors="replace").split(" ")
            if parts[0] == "D":
                done = True
            elif parts[0] == "X":
                self.crash = " ".join(parts[1:])
            elif parts[0] == "M":
                self.import_ok = parts[1] == "ok"
            else:
                n = int(parts[0])
                self.results[n] = parts[1]
                if len(parts) > 2:
                    self.cpu[n] = float(parts[2])
        return done

    @property
    def deadline(self):
        if self.windows is None or self._deadline is None:
            return self._deadline
        if not self.progressed:
            return self.mark + WATCH_STARTUP_S
        nxt = self.next_unreported()
        return self.mark + (self.windows.get(nxt, WATCH_MIN_S) if nxt is not None else WATCH_MIN_S)

    def next_unreported(self):
        """The test the child is running now (first unreported in run order), or None."""
        for n in self.order:
            if n not in self.results:
                return n
        return None

    def expired(self) -> bool:
        d = self.deadline
        return d is not None and time.monotonic() > d

    def kill(self):
        if self.alive:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._dead()

    def _dead(self):
        if self.alive:
            self.alive = False
            try:
                _, _, ru = os.wait4(self.pid, 0)
                self.total_cpu = ru.ru_utime + ru.ru_stime
                self.maxrss_mb = ru.ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)
            except ChildProcessError:
                pass

    def close(self):
        if self.alive:
            try:
                os.write(self.cmd, b"Q\n")
            except OSError:
                pass
            self._dead()
        for fd in (self.cmd, self.res):
            try:
                os.close(fd)
            except OSError:
                pass
