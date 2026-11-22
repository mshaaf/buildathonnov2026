"""Lab: test_lazy_load through the engine (RWX_LAZY_RUN=1) against the real thing on a
patched copy of the tree.  python /app/tools/lab/lazy_check.py DATA_DIR"""
import glob, json, os, shutil, subprocess, sys
sys.path.insert(0, "/app/rwx")
os.environ["RWX_LAZY_RUN"] = "1"
import selector as sel  # noqa: E402
from engine import Engine  # noqa: E402
from patchlib import apply_hunks, parse_patch  # noqa: E402

data = sys.argv[1]
tests = json.load(open(f"{data}/tests.json"))["tests"]
lazy = next(t["number"] for t in tests if t["id"] in sel.NO_RUN_IDS)
eng = Engine("/sqlglot", tests)
valid = 0
for t in tests:
    valid |= 1 << t["number"]
an = sel.Analyzer(sel.Map(sel.MAP), valid)
bad = 0
for p in sorted(glob.glob(f"{data}/commits/*.patch")):
    cid = os.path.basename(p)[:-6]
    text = open(p).read()
    an.analyze(text)
    res, info = eng.run(an.py_changes, an.data_changes, [lazy], timeout=60)
    tree = "/tmp/tree"
    shutil.rmtree(tree, ignore_errors=True)
    shutil.copytree("/sqlglot", tree, ignore=shutil.ignore_patterns("*.csv.gz", "__pycache__"))
    for fp in parse_patch(text):
        old = open(f"/sqlglot/{fp.path}").read()
        open(f"{tree}/{fp.path}", "w").write(apply_hunks(old, fp.hunks))
    r = subprocess.run(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"],
                       capture_output=True, text=True, cwd=tree,
                       env={**os.environ, "PYTHONPATH": tree, "PYTHONDONTWRITEBYTECODE": "1"})
    real = "P" if r.returncode == 0 else "F"
    got = res.get(lazy, "?")
    bad += got != real
    print(cid, "engine", got, "real", real, "mode", info["mode"], "OK" if got == real else "MISMATCH", flush=True)
print("mismatches", bad, flush=True)
os._exit(0)
