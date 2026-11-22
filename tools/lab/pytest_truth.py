"""Ground truth by real pytest, the organizers' arbiter (lab only, never shipped).

usage: pytest_truth.py DATA_DIR [--src PINNED_TREE] [--out answers.json] [--work DIR] [-n 4] [ids...]

For each DATA_DIR/commits/<id>.patch: copy the pinned tree, apply the patch, run the whole
suite (`pytest -p no:cacheprovider -n N -rA`, under `taskpolicy -b` on macOS) and map the
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


def run_suite(tree: Path, workers: int, timeout: int) -> tuple[str, int]:
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(tree), SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
        PYTHONDONTWRITEBYTECODE="1", PATH=f"{LAB / 'venv' / 'bin'}:{env.get('PATH', '')}",
    )
    env.pop("RWX_SRC", None)
    cmd = [str(LAB / "venv" / "bin" / "python"), "-m", "pytest", "-p", "no:cacheprovider", "-n", str(workers),
           "-rA", "-q", "--color=no", f"--timeout={timeout}", "tests"]
    if sys.platform == "darwin":
        cmd = ["taskpolicy", "-b"] + cmd
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
            output, rc = run_suite(tree, a.n, a.timeout)
            if a.keep_log:
                (Path(a.work) / f"{cid}.log").write_text(output)
        failing, missing, extra = classify(output, tests)
        tail = output.strip().splitlines()[-1:] if output.strip() else [""]
        answers["commits"][cid] = {"failing": failing, "kind": "odd", "missing": missing,
                                   "note": f"{how}; rc={rc}; {tail[0][:160]}", **extra}
        print(f"{cid}: failing {len(failing)} missing {len(missing)} rc={rc} | {tail[0][:120]}", flush=True)
        out.write_text(json.dumps(answers, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
