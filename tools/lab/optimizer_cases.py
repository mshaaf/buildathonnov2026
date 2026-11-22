"""Coverage of individual fixture entries in the pinned TestOptimizer tests.

Many TestOptimizer methods loop over the entries of a fixture file (load_sql_fixture_pairs,
directly or through check_file). Build time records, per test and per entry, which lines
of sqlglot and the tests the entry executed, plus whatever the test ran outside any entry
(bit 0). At run time, when every changed code object ran only inside entries, the test
can be run with just the entries that reached the changed code: the others execute none of
the changed code and so behave as on the pinned commit.

    python tools/lab/optimizer_cases.py SQLGLOT_ROOT   (lab only: measured, not shipped)     (writes SQLGLOT_ROOT/.rwx-optimizer-cases.json)
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path

PROFILE = ".rwx-optimizer-cases.json"
CLASS = "tests/test_optimizer.py::TestOptimizer"
MAX_CASE_MS = int(os.environ.get("RWX_OPT_CASE_MS", "60000"))


class _Entries:
    """Wraps load_sql_fixture_pairs (as seen by tests.test_optimizer) and check_file so each
    fixture entry, and check_file's later per-entry assertions, are attributed to it."""

    def __init__(self, module, keep=None):
        self.module = module
        self.keep = keep  # run time: entry indices to run (None: all)
        self.count = 0
        self.current = None
        self.on_switch = None
        self.titles = None  # check_file: subTest title -> entry indices

    def switch(self, index):
        self.current = index
        if self.on_switch:
            self.on_switch(index)

    def install(self):
        mod = self.module
        load, check = mod.load_sql_fixture_pairs, mod.TestOptimizer.check_file
        entries = self

        def pairs(*args, **kwargs):
            pos = 0
            try:
                for meta, sql, expected in load(*args, **kwargs):
                    index = entries.count
                    entries.count += 1
                    pos += 1
                    if entries.titles is not None:
                        title = meta.get("title") or f"{pos}, {sql}"
                        entries.titles.setdefault(title, []).append(index)
                    if entries.keep is not None and index not in entries.keep:
                        continue
                    entries.switch(index)
                    yield meta, sql, expected
            finally:
                entries.switch(None)

        def check_file(self, *args, **kwargs):
            outer = entries.titles
            entries.titles = {}
            try:
                return check(self, *args, **kwargs)
            finally:
                entries.titles = outer

        mod.load_sql_fixture_pairs = pairs
        mod.TestOptimizer.check_file = check_file
        return load, check

    def subtest_hook(self):
        """For check_file's assertion loop: a subTest(title) is that entry's work."""
        import contextlib

        original = unittest.TestCase.subTest
        entries = self

        @contextlib.contextmanager
        def sub_test(case, msg=unittest.case._subtest_msg_sentinel, **params):
            idx = entries.titles.get(msg) if (entries.titles and isinstance(msg, str)) else None
            if idx is None:
                with original(case, msg, **params):
                    yield
                return
            prev = entries.current
            entries.switch(idx if len(idx) > 1 else idx[0])
            try:
                with original(case, msg, **params):
                    yield
            finally:
                entries.switch(prev)

        unittest.TestCase.subTest = sub_test


def _profile_one(root, name):
    """Run one TestOptimizer test with line monitoring; return its entry profile or None."""
    import tests.test_optimizer as mod

    entries = _Entries(mod)
    entries.install()
    entries.subtest_hook()
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    prefix = str(root) + "/"
    lines: dict = {}
    ms: dict = {}
    clock = [time.process_time(), None]

    def bits(index):
        if index is None:
            return 1
        if isinstance(index, list):
            return sum(1 << (i + 1) for i in index)
        return 1 << (index + 1)

    state = {"bits": 1}

    def on_switch(index):
        now = time.process_time()
        key = clock[1]
        ms[key] = ms.get(key, 0.0) + now - clock[0]
        clock[0], clock[1] = now, (index if not isinstance(index, list) else index[0])
        state["bits"] = bits(index)
        mon.restart_events()

    def on_line(code, line):
        if code.co_filename.startswith(prefix):
            key = f"{code.co_filename[len(prefix):]}:{code.co_firstlineno}:{code.co_qualname}"
            hits = lines.setdefault(key, {})
            hits[line] = hits.get(line, 0) | state["bits"]
        return mon.DISABLE

    entries.on_switch = on_switch
    mon.use_tool_id(tool, "optimizer-entry-coverage")
    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)
    try:
        result = unittest.TestSuite([mod.TestOptimizer(name)]).run(unittest.TestResult())
    finally:
        mon.set_events(tool, 0)
        on_switch(None)
    if not entries.count or not result.wasSuccessful() or result.skipped:
        return None
    return {
        "lines": {k: {str(ln): hex(m) for ln, m in v.items()} for k, v in lines.items()},
        "ms": [max(1, round(ms.get(i, 0.0) * 1000)) for i in range(entries.count)],
        "base_ms": max(1, round(ms.get(None, 0.0) * 1000)),
    }


def build(root, jobs=4):
    import tempfile

    from engine import install_inline_pool

    install_inline_pool()
    import tests.test_optimizer as mod

    names = sorted(n for n in dir(mod.TestOptimizer) if n.startswith("test_"))
    out, running = {}, {}
    pending = list(names)
    with tempfile.TemporaryDirectory() as tmp:
        while pending or running:
            while pending and len(running) < jobs:
                name = pending.pop(0)
                path = os.path.join(tmp, name + ".json")
                pid = os.fork()
                if pid == 0:
                    try:
                        with open(path, "w") as fh:
                            json.dump(_profile_one(root, name), fh)
                    finally:
                        os._exit(0)
                running[pid] = (name, path)
            pid, _ = os.wait()
            if pid not in running:
                continue
            name, path = running.pop(pid)
            try:
                with open(path) as fh:
                    prof = json.load(fh)
            except (OSError, ValueError):
                prof = None
            if prof:
                out[f"{CLASS}::{name}"] = prof
    return out


def affected_entries(profile, root, swaps, site=None):
    """Entry indices of one test's profile that reached the swapped code, or None when a
    changed code object ran outside entries (then the whole test must run)."""
    indices = 0
    for (filename, line, name), _ in swaps:
        rel = filename.removeprefix(root + "/")
        hits = profile["lines"].get(f"{rel}:{line}:{name}")
        if not hits:
            continue  # this test never ran the code object
        masks = {int(ln): int(m, 16) for ln, m in hits.items()}
        whole = 0
        for m in masks.values():
            whole |= m
        if whole & 1:
            return None
        if site and site[:2] == (rel, f"{line}:{name.rsplit('.', 1)[-1]}"):
            lo, hi = site[2]
            for ln, m in masks.items():
                if lo <= ln <= hi:
                    indices |= m
        else:
            indices |= whole
    return [i for i in range(len(profile["ms"])) if (indices >> (i + 1)) & 1]


def install(keep):
    """Run time: TestOptimizer fixture loops yield only the entries in `keep`."""
    import tests.test_optimizer as mod

    _Entries(mod, keep=set(keep)).install()


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rwx"))
    t0 = time.time()
    data = build(root)
    (root / PROFILE).write_text(json.dumps(data, sort_keys=True))
    print(f"optimizer cases: {len(data)} tests, {sum(len(p['ms']) for p in data.values())} entries, "
          f"{time.time() - t0:.0f}s", file=sys.stderr)
