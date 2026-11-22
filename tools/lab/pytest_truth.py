"""Ground truth by real pytest, the organizers' arbiter (lab only, never shipped).

usage: pytest_truth.py DATA_DIR [--src PINNED_TREE] [--out answers.json] [--work DIR] [-n 4] [ids...]

For each DATA_DIR/commits/<id>.patch: copy the pinned tree, apply the patch, run the whole
suite (`pytest -p no:cacheprovider -n N -rA`, under `taskpolicy -c utility` on macOS) and map the
result to test numbers of DATA_DIR/tests.json. A test fails when pytest reports a failure,
an error (setup/teardown/collection of its file) or a timeout for it, or one of its
subtests fails. A test of tests.json that pytest never reports (deleted, renamed, crashed
worker) counts as failing and is flagged in "missing". Results are merged into --out in
the sample answers format (extra keys "missing" and "note" per commit), so score.py works.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LAB = Path("/Users/alibaduqail/Documents/Projects/blast-lab")
STATUS_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|SUBFAILED|SUBPASSED|SUBSKIPPED)(\[[^\]]*\])?\s+(\S+)")
IGNORE = shutil.ignore_patterns(".rwx-*", "__pycache__", "*.pyc", ".git")


def apply_patch(tree: Path, patch: Path) -> str:
    """Try the usual appliers; returns the one that worked or raises."""
    if patch.stat().st_size == 0:
        return "empty"
    tries = [
        ["git", "apply", "--unsafe-paths", "--binary", "-p1", str(patch)],
        ["git", "apply", "--unsafe-paths", "--binary", "-p0", str(patch)],
        ["patch", "-p1", "-s", "-f", "-i", str(patch)],
        ["patch", "-p0", "-s", "-f", "-i", str(patch)],
    ]
    errs = []
    for cmd in tries:
        r = subprocess.run(cmd, cwd=tree, capture_output=True, text=True, errors="replace")
        if r.returncode == 0:
            return " ".join(cmd[:2] + cmd[-2:-1])
        errs.append(f"{cmd[:3]}: {r.stderr.strip()[:200]}")
        # undo partial application by `patch`
    raise RuntimeError("could not apply patch: " + " | ".join(errs))


def auto_files(patch: Path, tests: list[dict], map_path: Path) -> list[str] | None:
    """Test files that can observe the patch, from the import map (independent of the
    selector's narrowing): files of tests that imported a changed .py file or opened a
    changed data file, plus changed test files. None (= whole suite) for anything else."""
    import pickle
    import zlib

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rwx"))
    from patchlib import parse_patch

    d = pickle.loads(zlib.decompress(map_path.read_bytes()))
    file_of = {t["number"]: t["file"] for t in tests}
    test_files = set(file_of.values())
    text = patch.read_bytes().decode("utf-8", "surrogateescape")
    files: set = set()
    for fp in parse_patch(text):
        if fp.binary or fp.created or fp.deleted or fp.copy or fp.old_path != fp.new_path or not fp.path:
            return None
        rel = fp.path
        if any("\udc80" <= ch <= "\udcff" for h in fp.hunks for ch in "".join(h.lines)):
            return None
        mask = 0
        if rel.endswith(".py"):
            if rel in test_files:
                files.add(rel)
            for lk in d["lines"].get(rel, {}).values():
                for mid in lk.values():
                    mask |= d["masks"][mid]
            if not mask and rel not in test_files:
                if rel.startswith(("sqlglot/", "tests/")) or "/" not in rel:
                    return None
        elif rel.startswith("tests/"):
            mid = d["opens"].get(rel)
            if mid is None:
                return None
            mask = d["masks"][mid]
        else:
            return None  # config and other files: run everything
        n = 0
        while mask:
            if mask & 1:
                files.add(file_of[n])
            mask >>= 1
            n += 1
    return sorted(files)


def run_suite(tree: Path, workers: int, timeout: int, files=None, keep_going=True) -> tuple[str, int]:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(tree), SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
        PYTHONDONTWRITEBYTECODE="1", PATH=f"{LAB / 'venv' / 'bin'}:{env.get('PATH', '')}",
    )
    env.pop("RWX_SRC", None)
    cmd = [str(LAB / "venv" / "bin" / "python"), "-m", "pytest", "-p", "no:cacheprovider", "-n", str(workers),
           "-rA", "-q", "--color=no", f"--timeout={timeout}"] + (["--continue-on-collection-errors"] if keep_going else []) + (files or ["tests"])
    if sys.platform == "darwin":
        # "-b" (background) starves completely when the machine is loaded; utility still yields
        cmd = ["taskpolicy", "-c", os.environ.get("PT_QOS", "utility")] + cmd
    r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, errors="replace")
    return r.stdout + "\n" + r.stderr, r.returncode


def classify(output: str, tests: list[dict]) -> tuple[list[int], list[int], dict]:
    by_id = {t["id"]: t["number"] for t in tests}
    by_file: dict = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    good, bad = set(), set()
    file_errors = set()
    for line in output.splitlines():
        m = STATUS_RE.match(line)
        if not m:
            continue
        status, node = m.group(1), m.group(3)
        node = re.sub(r"\[.*$", "", node)  # parametrized/subtest suffix
        if "::" not in node:
            if status == "ERROR":
                file_errors.add(node)  # collection error: the file cannot be imported
            continue
        if node not in by_id:
            continue
        if status in ("PASSED", "SKIPPED", "XFAIL", "SUBPASSED", "SUBSKIPPED"):
            good.add(by_id[node])
        else:
            bad.add(by_id[node])
    for f in file_errors:
        bad.update(by_file.get(f, ()))
    seen = good | bad
    missing = sorted(n for n in by_id.values() if n not in seen)
    failing = sorted(bad | set(missing))
    return failing, missing, {"file_errors": sorted(file_errors)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--src", default=str(LAB / "sqlglot"))
    ap.add_argument("--out")
    ap.add_argument("--work", default=str(LAB / "scratch-robust" / "truth"))
    ap.add_argument("-n", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--keep-log", action="store_true")
    ap.add_argument("--auto-files", action="store_true", help="only test files the import map links to the patch")
    ap.add_argument("--files", help="comma-separated test files to run (targeted truth, recorded as such)")
    ap.add_argument("--no-continue", action="store_true", help="pytest default: stop on collection errors")
    ap.add_argument("--map", default=str(Path(__file__).resolve().parents[2] / "rwx" / "maps" / "cov.pkl.z"))
    a = ap.parse_args()
    data = Path(a.data)
    tests = json.loads((data / "tests.json").read_text())["tests"]
    out = Path(a.out) if a.out else data / "answers.json"
    answers = json.loads(out.read_text()) if out.exists() else {"commits": {}}
    ids = a.ids or sorted(p.stem for p in (data / "commits").glob("*.patch"))
    Path(a.work).mkdir(parents=True, exist_ok=True)
    for cid in ids:
        patch = data / "commits" / f"{cid}.patch"
        with tempfile.TemporaryDirectory(dir=a.work) as tmp:
            tree = Path(tmp) / "sqlglot"
            shutil.copytree(a.src, tree, ignore=IGNORE, symlinks=True)
            try:
                how = apply_patch(tree, patch.resolve())
            except Exception as e:
                answers["commits"][cid] = {"failing": [t["number"] for t in tests], "kind": "unappliable",
                                           "missing": [], "note": str(e)[:300]}
                print(f"{cid}: UNAPPLIABLE {e}", flush=True)
                out.write_text(json.dumps(answers, indent=1, sort_keys=True))
                continue
            files = auto_files(patch, tests, Path(a.map)) if a.auto_files else None
            if a.files:
                files = a.files.split(",")
            if files is not None and not files:
                answers["commits"][cid] = {"failing": [], "kind": "odd", "missing": [], "note": "no test file linked"}
                print(f"{cid}: no test file linked", flush=True)
                out.write_text(json.dumps(answers, indent=1, sort_keys=True))
                continue
            output, rc = run_suite(tree, a.n, a.timeout, files, not a.no_continue)
            if a.keep_log:
                (Path(a.work) / f"{cid}.log").write_text(output)
        scope = None if files is None else set(files)
        failing, missing, extra = classify(output, [t for t in tests if scope is None or t["file"] in scope])
        tail = output.strip().splitlines()[-1:] if output.strip() else [""]
        answers["commits"][cid] = {"failing": failing, "kind": "odd", "missing": missing,
                                   "files": "all" if files is None else len(files),
                                   "note": f"{how}; rc={rc}; {tail[0][:160]}", **extra}
        print(f"{cid}: failing {len(failing)} missing {len(missing)} rc={rc} | {tail[0][:120]}", flush=True)
        out.write_text(json.dumps(answers, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
