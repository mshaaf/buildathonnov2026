"""Build-time snapshot of TestExecutor.setUpClass at the pinned commit.

setUpClass parses the TPC-H / TPC-DS CSV files into executor tables (0.6-0.7 s of CPU
in the parent on every run). The image is built from the pinned tree, so the result is
fixed: it is pickled here once and loaded in about 0.14 s at run time.

Alongside the state the snapshot keeps the code keys setUpClass runs. The engine treats
them as warm-up code exactly as when it runs setUpClass itself: a patch touching any of
them makes the child re-import and set the class up afresh.

    python /app/rwx/presetup_cache.py /sqlglot      (Dockerfile)
"""

import pickle
import sys
from pathlib import Path

CACHE = ".rwx-presetup.pkl"
REF = "tests/test_executor.py::TestExecutor"


def build(root: Path):
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engine import _LazyConn, _LazyDuckModule, install_inline_pool

    install_inline_pool()
    import tests.test_executor as mod

    klass = mod.TestExecutor
    prefix = str(root) + "/"
    keys = set()
    mon = sys.monitoring
    tool = mon.PROFILER_ID

    def on_start(code, offset):
        if code.co_filename.startswith(prefix):
            keys.add((code.co_filename, code.co_firstlineno, code.co_qualname))
        return mon.DISABLE

    before = set(vars(klass))
    real = mod.duckdb
    mod.duckdb = _LazyDuckModule(real)
    mon.use_tool_id(tool, "rwxpresetup")
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.PY_START)
    try:
        klass.setUpClass()
    finally:
        mon.set_events(tool, 0)
        mon.register_callback(tool, mon.events.PY_START, None)
        mon.free_tool_id(tool)
        mod.duckdb = real
    fields = {}
    for name in sorted(set(vars(klass)) - before):
        value = vars(klass)[name]
        if isinstance(value, _LazyConn):
            if value._conn is not None:
                raise RuntimeError(f"{name}: duckdb connection opened during setUpClass")
            fields[name] = ("conn", value._a, value._k, list(value._log))
        else:
            fields[name] = ("value", value)
    snapshot = {"ref": REF, "fields": fields, "keys": sorted(keys)}
    (root / CACHE).write_bytes(pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL))
    print(f"presetup snapshot: {len(fields)} fields, {len(keys)} code keys", file=sys.stderr)


def load(root: str, klass, duckdb_module):
    """Install the snapshot on klass. Returns the code keys setUpClass runs, or None if
    there is no usable snapshot (the caller then runs setUpClass itself)."""
    from engine import _LazyConn

    path = Path(root) / CACHE
    if not path.is_file():
        return None
    snapshot = pickle.loads(path.read_bytes())
    if snapshot.get("ref") != REF:
        return None
    for name, entry in snapshot["fields"].items():
        if entry[0] == "conn":
            _, args, kwargs, log = entry
            conn = _LazyConn(duckdb_module, args, kwargs)
            conn._log = list(log)
            setattr(klass, name, conn)
        else:
            setattr(klass, name, entry[1])
    return {tuple(k) for k in snapshot["keys"]}


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve())
