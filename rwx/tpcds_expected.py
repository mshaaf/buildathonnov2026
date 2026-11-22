"""Build-time duckdb results for the TPC-DS queries (RWX_TPCDS_EXPECTED=1).

test_execute_tpcds compares each executed query with duckdb's answer to
transpile(sql, write="duckdb"), run on views of the pinned TPC-DS CSV files
(TestExecutor.cached_execute). duckdb, the data and the pinned SQL are fixed in the
image, so the answer to a given duckdb SQL string is fixed too. This records it per
query. In a session the test still transpiles with the patched code; when that gives
the recorded string the recorded frame is used, otherwise duckdb runs as before. The
frames are what the pinned run computes, so every comparison comes out the same, while
the duckdb side (about half of a small TPC-DS check's CPU) is skipped.

    python /app/rwx/tpcds_expected.py /sqlglot      (Dockerfile)
"""

import pickle
import sys
from pathlib import Path

CACHE = ".rwx-tpcds-expected.pkl"


def build(root: Path):
    sys.path.insert(0, str(root))
    import tests.test_executor as tests

    klass = tests.TestExecutor
    klass.setUpClass()
    frames = {}
    for args, sql, _ in klass.tpcds_sqls:
        if not args.get("execute") or sql in frames:
            continue
        duck = tests.transpile(sql, write="duckdb")[0]
        frames[sql] = (duck, klass.tpcds_conn.execute(duck).fetchdf())
    return frames


_FRAMES = None


def install(root: str):
    """Serve recorded duckdb frames to TestExecutor.cached_execute for TPC-DS queries."""
    global _FRAMES
    import tests.test_executor as tests

    if _FRAMES is None:
        path = Path(root) / CACHE
        try:
            _FRAMES = pickle.loads(path.read_bytes()) if path.is_file() else {}
        except Exception:  # noqa: BLE001 - an unusable cache only loses the speed-up
            _FRAMES = {}
    if not _FRAMES:
        return
    klass = tests.TestExecutor
    real = klass.cached_execute
    if getattr(real, "_rwx_expected", False):
        return

    def cached_execute(self, sql, tpch=True):
        if not tpch and sql not in self.cache and sql in _FRAMES:
            duck = tests.transpile(sql, write="duckdb")[0]  # the test's own transpile (patched code)
            want, frame = _FRAMES[sql]
            self.cache[sql] = frame.copy(deep=True) if duck == want else self.tpcds_conn.execute(duck).fetchdf()
            return self.cache[sql]
        return real(self, sql, tpch)

    cached_execute._rwx_expected = True
    klass.cached_execute = cached_execute


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    data = build(root)
    (root / CACHE).write_bytes(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
    print(f"tpcds expected: {len(data)} queries", file=sys.stderr)
