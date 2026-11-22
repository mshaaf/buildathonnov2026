"""Coverage of individual TPC-DS queries in the pinned TestExecutor test."""

import json
import sys
import time
import unittest
from functools import wraps
from pathlib import Path

PROFILE = ".rwx-executor-cases.json"
TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def build(root):
    from engine import install_inline_pool

    install_inline_pool()
    import tests.test_executor as tests

    records = {}
    lines = {}
    total = []
    durations = []
    current = None
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    prefix = str(root) + "/"
    parse = tests.parse_one
    check = tests.TestExecutor.subtestHelper
    run = tests.TestExecutor._mp_execute

    def enter(code, line):
        if code.co_filename.startswith(prefix):
            rel = code.co_filename[len(prefix) :]
            key = f"{rel}:{code.co_firstlineno}:{code.co_qualname}"
            bit = 1 << (current + 1) if current is not None else 1
            records[key] = records.get(key, 0) | bit
            hits = lines.setdefault(key, {})
            hits[line] = hits.get(line, 0) | bit
        return mon.DISABLE

    def start(code, offset):
        # Code objects whose body sits on their first line (one-line lambdas/defs) never
        # raise a LINE event: record every start too, so a missing key means "never ran".
        if code.co_filename.startswith(prefix):
            rel = code.co_filename[len(prefix) :]
            key = f"{rel}:{code.co_firstlineno}:{code.co_qualname}"
            records[key] = records.get(key, 0) | (1 << (current + 1) if current is not None else 1)
        return mon.DISABLE

    def enter_case(index):
        nonlocal current
        current = index
        mon.restart_events()

    def parse_case(*args, **kwargs):
        index = len(durations)
        start = time.process_time()
        enter_case(index)
        try:
            return parse(*args, **kwargs)
        finally:
            # mp_execute runs immediately in InlinePool, so keep recording in this case.
            durations.append(start)
            total.append(0)

    execute = tests.mp_execute

    @wraps(execute)
    def execute_case(*args, **kwargs):
        try:
            return execute(*args, **kwargs)
        finally:
            total[current] = time.process_time() - durations[current]
            enter_case(None)

    def check_case(self, index, *args, **kwargs):
        start = time.process_time()
        enter_case(index)
        try:
            return check(self, index, *args, **kwargs)
        finally:
            total[index] += time.process_time() - start
            enter_case(None)

    def run_cases(self, schema, tables, sqls, tpch):
        tests.parse_one = parse_case
        try:
            run(self, schema, tables, sqls, tpch)
        finally:
            tests.parse_one = parse

    tests.TestExecutor._mp_execute = run_cases
    tests.TestExecutor.subtestHelper = check_case
    tests.mp_execute = execute_case
    mon.use_tool_id(tool, "executor-query-coverage")
    events = mon.events.LINE | mon.events.PY_START
    mon.register_callback(tool, mon.events.LINE, enter)
    mon.register_callback(tool, mon.events.PY_START, start)
    mon.set_events(tool, events)
    try:
        result = unittest.TestSuite([tests.TestExecutor("test_execute_tpcds")]).run(unittest.TestResult())
    finally:
        mon.set_events(tool, 0)
    count = len(getattr(tests.TestExecutor, "tpcds_sqls", ()))
    if count and count == len(total) and result.wasSuccessful() and not result.skipped:
        return {
            "coverage": {key: hex(mask) for key, mask in records.items()},
            "lines": {key: {line: hex(mask) for line, mask in hits.items()} for key, hits in lines.items()},
            "ms": [max(1, round(t * 1000)) for t in total],
        }
    return {}


def affected_cases(profile, root, swaps, site=None, sites=None):
    """Indices of the TPC-DS queries that ran a swapped code object, or None when unknown.

    sites: {(rel, "firstlineno:name"): [(lo, hi), ...]} changed statement spans (every change
    of that code object), narrowing it to the queries that ran a line of those statements.
    A key missing from the profile is unknown, not "never ran": line events never fire on a
    code object's first line, so one-line lambdas/defs leave no trace (None). A span that
    covers the code object's first line is decided at function level for the same reason."""
    coverage = profile["coverage"]
    spans = dict(sites or {})
    if site and not spans:
        spans[(site[0], site[1])] = [tuple(site[2])]
    indices = 0
    for (filename, line, name), _ in swaps:
        rel = filename.removeprefix(root + "/")
        key = f"{rel}:{line}:{name}"
        if key not in coverage:
            return None
        mask = int(coverage[key], 16)
        if mask & 1:
            return None
        sp = spans.get((rel, f"{line}:{name.rsplit('.', 1)[-1]}"))
        if sp and "lines" in profile and all(lo > line for lo, _ in sp):
            hits = profile["lines"].get(key, {})
            for lineno, h in hits.items():
                if any(lo <= int(lineno) <= hi for lo, hi in sp):
                    indices |= int(h, 16)
        else:
            indices |= mask
    return [i for i in range(len(profile["ms"])) if (indices >> (i + 1)) & 1]


def install(indices):
    from tests.test_executor import TestExecutor

    original = TestExecutor.test_execute_tpcds

    def selected(self):
        self.tpcds_sqls = [self.tpcds_sqls[i] for i in indices]
        try:
            return original(self)
        finally:
            del self.tpcds_sqls

    TestExecutor.test_execute_tpcds = selected


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    (root / PROFILE).write_text(json.dumps(build(root), sort_keys=True))
