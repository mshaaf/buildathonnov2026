# Handoff: RWX Blast Radius entry

Everything a new engineer or agent needs to pick this up: the problem, what is
built, what the numbers say, what failed, and what to try next.

**Deadline:** Sunday Nov 22, 11:00 EST (16:00 UTC). Only the **final** run counts
(about 60 hidden commits, 4.5 min limit). The practice board is signal only.
**Branch:** `rwx` of `github.com/mshaaf/buildathonnov2026` (public).
**Submit:** the repo URL plus a commit hash on the track page (once an hour).

## Local candidate, November 21

The candidate defers the executor fixture and compares parsed syntax directly.
After two clean probes clear an expensive test, it verifies the remaining
runnable candidates if their estimated CPU fits within the possible worker
savings. Class fixture failures retain the affected
tests. Changes to nested functions reload the patched source, and file creation,
deletion, renames and binary patches fall back to selecting the full suite.

| Batch | v3 simulated | Candidate simulated | v3 local total | Candidate local total | Candidate misses |
|---|---:|---:|---:|---:|---:|
| 30 official samples | 112.254 s | 103.655 s | 121.01 s | 111.45 s | 0 |
| 60 generated patches, seed 1729 | 198.253 s | 198.253 s | 208.72 s | 206.69 s | 0 |
| 5 targeted budget cases, seed 1730 | 53.564 s | 45.212 s | 60.37 s | 52.87 s | 0 |

Totals are the median of three interleaved runs on macOS arm64 with Python
3.13 and the locked dependencies. Each total includes CPU for the selector and
its children. The sample improvement in simulated time came from one patch.
The 60-patch batch saved CPU time. For that batch, running the broad reference
set with forced re-imports reproduced the original reference labels. The
5 targeted cases were the patches in a pool of 320 that switched initial
batches when the full-verification budget was raised. Raising that budget
alone increased their simulated time to 101.816 s, so the candidate retains
the original probe order. A separate seed (1731) produced two eligible
cases among 240 mutations. Both passed the cold-reference check with zero
misses. The historical 601-patch lab corpus is
outside this repository. The Linux/amd64 practice score for this candidate is
still unmeasured. Section 2 records the previously submitted versions.

Run `python3 tools/lab/test_verification.py` for the focused regressions and
`make sample` for the sample under the container limits.

---

See also **RESEARCH.md** for the measurements behind the current plan.

## 1. The problem in one screen

For each patch to sqlglot (pinned at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`),
print one JSON line choosing which of its 1,620 tests to run, split over at most
8 workers. Full rules: `RWXblast-radius-sample/SCORING.md` and `.../README.md`.

```
commit cost = busiest worker's recorded test seconds + 2 s per worker   (0 if no workers)
miss        = any test that really fails on the commit is not selected   -> +300 s
total       = sum of commit costs + 300 s x misses + CPU seconds of the whole container
score       = middle of 3 runs
```

- 1,620 tests, 103 s recorded in total, but very skewed: `test_max_nodes` 28.8 s,
  `test_execute_tpcds` 21.5 s, `test_generator_memory` 6.7 s, two TPC-H tests about 3.5 s,
  then a few 1–2.5 s optimizer tests; everything else is milliseconds.
- Patches are **synthetic mutations**: one-line operator flips (`==`/`!=`, `<`/`<=`,
  `and`/`or`, `is`/`==`), constant tweaks, inserted comments, edited optimizer fixture
  lines, and "multi" combos. About 40% of sample commits break nothing.
- Runtime limits: 2 cores, 4 GB, no network, read-only disk, user `nobody`, 64 MB `/tmp`,
  256 processes, 3 min (practice) / 4.5 min (final). Build: 4 cores, 10 min, network.
- **Rules that bite:** no table from a patch or a kind of edit to the tests it breaks.
  Maps built from the pinned commit (coverage, imports, fixtures) are allowed. Top
  entries are read. MIT license; the README must name open-source code used.

## 2. Score history

| Version | Commit | Simulated | CPU | Board | Notes |
|---|---|---|---|---|---|
| v1 | `e4989ca` | 284.6 | 1.0 | 285.6 | static coverage lookup only, never runs tests |
| v2 | `fa10564` | 179.3 | 13.0 | 192.3 | runtime engine verifies cheap candidates |
| v2.2 | `21cf8f8` | 172.5 | 16.0 | 188.5 | class-attribute reader narrowing |
| v3 | `760cde2` | 172.6 | 10.2 | 182.8 | focus-ranked probes, gc.freeze, evidence-gated giants |
| v3 (resubmitted) | `760cde2` | 172.6 | 8.4 | 181.0 | same code, their CPU varies |
| **v4** | `7d6929b` | not yet scored | | | exact `arg_types` flips, shared executor setup; samples 112.2 + 8.0 CPU (v3: 112.3 + 14.0) |

Zero misses on every board run. Leader at the time of writing: **126.1**. The team goal
is roughly 20 s better than 182.8.

**Why v1 had 1 s of CPU:** it never ran a test. It only looked up a precomputed map.
v2+ runs sqlglot's own tests inside the container to prove which candidates pass,
which costs CPU but removed about 110 s of simulated time. The CPU is the price of knowing.

**Language does not matter much:** our own code (imports, map load, patch analysis)
is about 1.5 s. The rest of the CPU is sqlglot's Python tests, which must run as Python,
and 172 of 182.8 s is *which tests we print*. Rewriting the selector in C/C++/Rust
saves under 1 s.

## 3. Layout

```
Dockerfile                 scorer build: downloads sqlglot at the pin (stage "sqlglotsrc"),
                           installs rwx/requirements.lock, precompiles, CMD selector.py
rwx/selector.py            entry point: analysis, policy, scheduler, packing, output
rwx/engine.py              warm parent + forked per-commit sessions that run tests
rwx/patchlib.py            unified diff parsing/applying, AST change locator
rwx/maps/cov.pkl.z         the map (about 1.2 MB, zlib+pickle), built by tools/build_map.py
rwx/requirements.lock      exact packages the commits were labeled with
tools/build_map.py         builds the map from collected data (see section 6)
tools/lab/                 validation lab, never shipped (see section 7)
RWXblast-radius-sample/    the starter kit: 30 sample commits + answers, score.py
```

## 4. How the selector works

1. **Static candidates per commit** (`Analyzer` in `selector.py`)
   - Patch applied in memory. AST identical (comment or whitespace only) -> nothing.
     Syntax error -> every test that loaded the module.
   - Each changed line is located (`patchlib.Locator`) in its innermost code object
     (function, lambda, class body, module body), as recorded by coverage
     (`"firstlineno:name"` keys).
   - **Function body:** tests that executed that statement's lines in that code object.
   - **Class body** (e.g. `WEEK_OFFSET = -1`, `arg_types`, `FUNCTIONS` entries): tests that
     *used* the class or a subclass (as `self`/`cls`, including during test-module import)
     **and** ran code that reads one of the changed names (`x.NAME`, `NAME`, or the string
     `"NAME"`), plus code naming `Cls.NAME` explicitly. Enums, dataclasses, metaclasses,
     exceptions and classes with no recorded users fall back to "every test loading the module".
   - **Module body:** tests running code that references the bound names.
   - **Fixture files:** tests that opened the file.
2. **Runtime verification** (`engine.py`)
   - The parent imports sqlglot and all 58 test modules once (about 1.4 s), records every code
     object that ran during warm-up, indexes live function objects, then `gc.freeze()`.
   - Per commit it forks a **session**. If every changed code object is a function that
     never ran at warm-up, it swaps `__code__` on the live functions (cheap). Otherwise
     it purges sqlglot/tests modules and re-imports them from patched sources via an
     import hook (about 0.3–1 s). Data files are served patched through `open()`.
   - Tests run with `unittest` (all tests are `unittest.TestCase` methods).
     `ProcessPoolExecutor` is replaced by an in-process pool that pickles like a real one,
     because the optimizer and executor tests use process pools and pickling runs `serde`.
   - Results stream back per test, with a stop-at-first-failure option.
     A child that times out or crashes makes every unreported test count as failing (safe).
   - **Verified:** the engine reproduces all 30 sample answers exactly
     (`tools/lab/engine_check.py`).
3. **Policy** (`policy()` in `selector.py`), a generator per commit asking for test batches:
   - "Heavy" = estimated CPU (body + class setup) above 2.5 s, or never runnable here
     (`test_lazy_load` spawns a fresh interpreter that would see the unpatched tree).
   - If all runnable candidates cost at most 4 s: run them all (stop at first failure).
     Clean -> print nothing except never-runnable tests. Failure -> print failures + unrun + heavy.
   - Otherwise **probe 1** (0.15 s) and **probe 2** (0.25 s): candidates most *focused* on
     the changed files (share of each test's executed lines inside them), cheapest first.
     Any failure -> print everything not proven passing. Both clean -> also drop the
     "giants" (recorded >= 6 s), but only those whose own test file had runnable tests run
     clean (class setup budgeted in), or that have no runnable relatives at all.
   - Tests reading an edited fixture are printed without running (they always fail).
   - A giant is dropped only when *all* runnable candidates from its own test file ran
     clean (budget `RWX_RELATED_ALL_MS`, 4 s). A 3-test sample was not enough: it only
     avoided a lab miss by luck of ordering.
   - **`arg_types` flips (v4):** a class-body `arg_types` edit that only flips the
     truthiness of entries changes nothing but `required_args`, read only by
     `error_messages()`. Candidates = tests that validated an affected class (the class,
     its subclasses, classes copying its `arg_types`) with that argument missing. This is
     a per-test profile recorded by `tools/lab/argrun.py` (`arg_missing` in the map).
     In the lab it turns 1,617 candidates into exactly the failing set.
   - **Shared executor setup (v4):** `TestExecutor.setUpClass` runs once in the warm
     parent (under the warm-up monitor, so patches touching code it ran force a
     re-import). duckdb connections are lazy proxies that replay their statements in the
     child on first use. This saves about 1.5 s per session that runs executor tests.
4. **Scheduler:** two sessions at a time (2 cores), global wall-clock guard falls back
   to the static set if time runs short.
5. **Packing:** LPT over k = 1..8, keeps the k with the smallest busiest-load + 2k.

## 5. Where the remaining cost is

**Samples (30 commits): 112.3 s simulated + about 14 s CPU, floor (exact failing sets) 94.4 s.**
The simulated gap is almost entirely two commits:

- `2haz` (`is` -> `==` on an enum, equivalent mutant, 86 candidates incl. TPC-DS): 8.6 s, ideal 0
- `4d63` (Snowflake tokenizer `NUMERIC_ESCAPES`, 278 candidates): 8.9 s, ideal 0

**Lab (601 synthetic commits), gap to the floor by type** (per commit, current policy):

| Type | Share | Gap each | Why |
|---|---|---|---|
| 500+ candidates, something fails | about 7% | about 17 s | hot parser paths: about 1,400 candidates, 1–12 fail. Must print all |
| 51–500 candidates, something fails | about 7% | about 8 s | volume of unrun candidates |
| 51–500 candidates, nothing fails | about 4% | about 11 s | cannot afford to prove clean |
| up to 50 candidates | about 40% | under 2.5 s | mostly CPU of the runs |
| fixture edits, empty, comment-only | about 40% | about 0 | already at the floor |

**Measured facts (do not re-derive):**
- Probe detection curve (lab, commits too big to run fully, live among runnable):
  ordered by rarity per CPU, 1 s of probing catches 81%, 4 s 93%, 8 s 96%, 16 s 98%.
  After a clean 4 s probe about 20% are still live, so "clean after probe" bets lose
  about 60 s each in expectation.
- Call-level selection (only the `validate_*` calls / pool submissions that reach the
  changed code): those units are only 20–60% of test CPU, total saving about 15%
  (`tools/lab/unitpot.py`). Not worth its complexity.
- Gap by edit kind (lab): function-body edits 68% (`and`/`or` 24%, comparisons 22%,
  numbers 8%, `is`->`==` 8%, booleans 6%), class attributes 17% (mostly `arg_types`, now
  handled), default-argument edits 11%.
- Running *every* non-heavy test costs about 42 s of CPU, so full runs never pay for big sets.
- Including tests worth S seconds costs about 2·sqrt(2S); running them costs about S of CPU.
  Running wins only below roughly 8 s.
- **After both focused probes pass, 50–72% of commits still have a failing test among
  the unrun candidates.** Betting ("print nothing after clean probes") would be a disaster:
  one miss = 300 s, and the final has about 60 commits.
- When *all* runnable candidates ran clean, no heavy test ever failed (0 of 94 instances).
- Giants fail essentially only on broad mutations (at least 38% of probes failing), with one
  exception: TPC-DS failed on a narrow tokenizer mutation (3% failing), always together with
  other `test_executor.py` tests.
- Perfect cheap stand-ins for the giants would save only about 0.1–0.4 s per commit.
- Concrete hot-path examples (all valid lab ground truth):
  - `parser.py` `next_tt != MATCH_CONDITION` -> `==`: 1,372 candidates, only `TestSnowflake::test_snowflake` fails
  - `_parse_select(consume_pipe=True -> False)`: 1,617 candidates, only the 12 `TestPipeSyntax` tests fail
  - `errors.highlight_sql` `end + 1 -> end + 0`: 1,179 candidates, only 16 `TestErrors` tests fail
  - `_parse_factor` IntDiv condition `and -> or`: 1,139 candidates, 6 fail (DIV operator tests)

  The failures come from runtime behavior (fast path vs. slow path giving the same
  result except for one feature), so static analysis cannot see them; only running does.

## 6. Rebuilding the map (only if coverage logic changes)

Everything runs in a lab image matching the scorer (python:3.13-slim at the same digest,
`requirements.lock`, plus `coverage`), with sqlglot at `/sqlglot`.
See `tools/lab/Dockerfile.lab`. Lab data lives in `/home/user/lab` in this container
and is **not** in git. It is regenerated by:

```bash
# in the lab image, cwd /sqlglot, repo mounted at /app
python /app/tools/lab/covrun.py   tests.json OUT/cov  --jobs 4   # per-test line coverage by code object, about 3 min
python /app/tools/lab/classrun.py tests.json OUT/cls2 --jobs 4   # classes used per test + hierarchy, about 10 min
python /app/tools/lab/calib2.py   tests.json OUT/calib2.N.json N 2  # per-test / per-class-setup CPU
python3 tools/build_map.py OUT/cov tests.json rwx/maps/cov.pkl.z OUT/cls2 /path/to/sqlglot
```

`covrun.py` lives at `tools/lab/covrun.py`. Every collector forks one child per test
from a parent that has not imported sqlglot, so lazy caches cannot hide lines.

## 7. Validation workflow (never ship without it)

```bash
python3 tools/lab/mutate.py SQLGLOT_ROOT LABDIR 400 SEED      # synthetic sample-style patches
# ground truth: runs the BROAD (v1-style) candidate set of each patch through the engine
docker run --rm -v $PWD:/app -v LABDIR:/data rwx-lab python /app/tools/lab/truth.py /data /data/truth.0.jsonl 0 1
TOP=10 python3 tools/lab/simulate.py RWXblast-radius-sample/sample LABDIR   # replays the real policy
```

- `simulate.py` drives the real `policy()` against ground truth with calibrated CPU, and
  reports misses. **Ship only with 0 misses on samples + all lab sets.**
- The **samples** predict board movement much better than the lab (the hidden set looks
  like the samples): the samples predicted -4% for v2.2 and the board gave -3.8%. The lab
  is for catching misses and testing ideas on many patterns.
- Real numbers: `tools/lab/build_local.sh` builds the actual image, then run it with the
  scorer's flags (see `Makefile`) wrapped to print total CPU, and score with `score.py`.
  `tools/lab/engine_check.py` re-verifies that the engine reproduces all sample answers.

## 8. Ideas, ranked by expected value

Done in v4: exact `arg_types` flips, shared executor setup. Measured and dropped:
call-level selection (about 15% CPU, see section 5), rarity probe order (raw detection is
better but the full simulation is slightly worse than focus order).

0. **Value profiles for comparison sites** (`<` vs `<=`, `is` vs `==`): record per test
   whether operands were ever equal/identical-but-not-equal at each site, so boundary
   flips select only tests that hit the boundary. Covers part of the 22% comparison
   slice. `and`/`or` flips cannot use this (short-circuiting hides the other operand).
1. *(measured, low value)* **Sub-test (call-level) selection.** Dialect tests are long lists of
   `validate_identity`/`validate_all` calls; optimizer tests loop over fixture entries
   through the pool. Record coverage per call/per pool submission, then at runtime run
   only the calls that executed the changed lines and stub the rest. Skipping a call that
   never reaches the changed code cannot hide a failure (it behaves exactly as on the
   pinned commit), so this stays sound. It would make "run every candidate" cheap for
   mid-size sets (51–500), which carry about 40% of the remaining gap. Watch out for
   chained use of return values (`validate_identity(...).assert_is(...)`) and shared
   state between calls.
2. *(done in v4)* **Share expensive class setup across sessions.** `TestExecutor.setUpClass` costs about
   1.5 s per session (0.8 s CSV parsing, 0.7 s duckdb). Precompute in the warm parent
   under the warm-up monitor, so changed code that ran during setup still forces a
   re-import. duckdb connections cannot cross `fork`, so make them lazy in the child.
   Expected -2 to -4 s.
3. **Cheaper re-import sessions** (class/module-body changes): re-import only the
   modules that transitively import the changed one, instead of all of sqlglot + tests.
4. **Compiled sqlglot (mypyc)** to run tests 2–4x faster. High risk: the mutated module
   must stay interpreted, and mypyc semantics can differ from CPython, so outcomes
   could diverge from the labeling environment.
5. Smaller wins: tune `RUN_LIMIT_MS`/probe budgets against the samples (not only the lab);
   trim warm-up (pandas/duckdb imports are only needed by two test modules).

**Ruled out by measurement:** betting after clean probes; skipping giants on "narrow"
failures (caused a miss); spending more CPU to prove mid-size sets clean (mid-weight
tests still have to be printed); stand-ins for the giants (tiny gain).

## 9. Gotchas

- The Docker daemon here dies with its shell or after the 2 h background limit:
  `dockerd > /tmp/claude-0/dockerd.log 2>&1` as a background task.
- This sandbox intercepts TLS: local builds need `tools/lab/build_local.sh` (injects the
  proxy CA for pip and swaps the GitHub download for a local checkout). The committed
  `Dockerfile` must stay clean for the scorer.
- Never name a module after a stdlib module (`select.py` broke `select` once).
- Nothing but answer lines may reach stdout: `main()` dups the real stdout and points fd 1
  at stderr; children write to `/dev/null`.
- Every file must be readable by `nobody` (`chmod -R a+rX` in the Dockerfile).
- Output must be deterministic; it has been identical across all runs so far.
- `nanobind: leaked ...` lines on stderr at exit come from duckdb objects kept alive by
  `gc.freeze()`. They are harmless.

## 10. Update: merged branches, the TPC-DS drop risk, GitHub housekeeping

- `rwx` now contains everything: v4 + Astra's `main` work (PR #2) + Astra's
  `optimize-executor-setup` (per-query TPC-DS verification, tokenizer flag coverage).
  `main` and `rwx` choose identically on the samples (103.655 s); practice has been at
  172.56 s simulated for every version since v2.2, so the practice set barely contains
  the commit types any of these changes target. Always paste the full commit hash on
  the submit form: an empty box scores the newest commit on `main`.
- **Known risk (since v3):** when every runnable candidate passes, the policy leaves the
  21 s `test_execute_tpcds` out unrun. In the lab that happened on 54 commits and was
  wrong once (`m020156`, a predicate-pushdown change that only breaks complex TPC-DS
  queries), about 2% per drop. Expected value still favors dropping (about 21 s saved vs
  about 6 s expected penalty), but expect roughly a 1-in-10 chance of one miss across the
  final. A gate that runs the cheapest 3 s of affected TPC-DS queries first did not catch
  it (the failing queries are the expensive ones) and added about 5 s CPU, so it is off
  (`RWX_TPCDS_SAMPLE_MS=0`).
- Three lab ground-truth records (`m020156`, `m020229`, `m020302`) were timeouts: tests that
  did not report were recorded as failing. Treat lab records with `timeout`/`crash` set as
  unreliable; real pytest on the patched tree is the arbiter.
- `.github/validation` on branch `validate-tpcds-758caa9` (Astra's amd64 comparison
  workflow) now measures CPU with `getrusage` when cgroup `cpu.stat` is absent, and finds
  its marker even when duckdb's leak report (no trailing newline) precedes it. The
  selector now exits with `os._exit`, so that leak report no longer appears.

## 11. Practice results with the real generator (Nov 22)

| Commit (branch) | Simulated | CPU | Board | What it adds |
|---|---|---|---|---|
| `53171bc` (bet-clean) | 151.3 | 8.4 | 159.8 | treat a commit as clean after both probes pass |
| `e539940` (bet) | 151.3 | 9.4 | 160.7 | + TPC fixture refinement (practice has none), finish affordable verification first |
| **`d44ba30` (bet-narrow)** | **129.7** | **14.6** | **144.8** | + on live commits, finish verifying and leave out giants when failures are narrow and none in the giant's file |

0 misses on every run. **Final pick: `d44ba302dc5cbd9bdea4a5708aeb62bec802ad7c`.**

- Practice decomposition (diagnostic branches, bet version): live commits 16 / 91.4 s,
  static commits 13 / 57.5 s, everything else 11 / 2.4 s. The narrow rule then cut the live
  part by 21.6 s.
- The bets hold on the real generator (samples + practice) but not on my synthetic lab or
  Astra's: real mutations tend to break broadly or not at all. Samples are the arbiter.
- Rejected on evidence: live bet (5 sample misses), skipping mid-weight tests under the
  narrow rule (6 new lab misses for 23 s), lowering the bet's finishing threshold (brings
  back Astra's miss).

## 12. Where things stand (Nov 22, ~03:45 UTC) and how to keep going

**Board:** our best is **141.0** (`2ac5328`, branch `bet-narrow-mid-fx`). The leader
is at 103. The board keeps the best submission automatically.

**Branches that matter** (always paste the full hash on the submit form; an empty box
scores the newest commit on `main`, which is old):

| Branch | Commit | What it is |
|---|---|---|
| `rwx` | latest | development line, all flags off by default |
| `bet-narrow` | `d44ba30` | **current best, 144.8** (bet + narrow-live giants) |
| `bet-narrow-mid` | `bec9335` | **141.8**: narrow rule also skips mid-weight tests (>= 1 s) + fixture comment/title rules |
| `fast-v1` | `8849616` | **candidate**: 141.0 settings + `RWX_PRESETUP_CACHE=1` (build-time setUpClass snapshot, ~0.5 s less parent CPU; -1.3 s CPU on samples in paired runs) + `RWX_LAZY_RUN=1` (test_lazy_load runs on the patched tree). Sample output identical |
| `diag-heavy` | `9fac365` | DIAGNOSTIC ONLY: 141.0 output minus unrun heavy tests on engine commits (see RESEARCH.md) |
| `bet-narrow-mid-fx` | `2ac5328` | **141.0 (current best)**: bet-narrow-mid + `RWX_FIXTURE_RUN=1` (TPC fixture edits run on the edited queries), `RWX_NARROW_CHUNKS=0`. Practice: 125.762 s simulated (unchanged: no practice fixture commit benefits), CPU 15.09 / 15.19 / 15.42 |
| `bet`, `bet-clean` | `e539940`, `53171bc` | earlier bets (160.7, 159.8) |
| `diag-*` | | diagnostics only, never pick these |

Submission variants are just `rwx` plus an `ENV` line in the Dockerfile. The switches
(all in `rwx/selector.py`, read from the environment):

| Variable | Effect |
|---|---|
| `RWX_BET_CLEAN=1` | after both probes pass, finish verifying if cheap, else treat the commit as clean |
| `RWX_NARROW=1` | live commits: verify the runnable candidates fully, then drop slow tests when failures are narrow (< 25%) and none is in that test's file |
| `RWX_NARROW_SKIP_MS` | which slow tests the narrow rule may drop (3000 = giants; 1000 = mid-weight too) |
| `RWX_DIAG_MODE=live/static`, `RWX_DIAG_MIN/MAX` | diagnostics: print nothing for a category; misses = how many in it break |
| `RWX_PROBE_ORDER=rare` | rarity-ranked probes (tested, not better) |
| `RWX_FIXTURE_RUN=1` | fixture-only TPC edits: run TestExecutor's per-query tests on the edited queries |
| `RWX_PRESETUP_CACHE=1` | load the build-time snapshot of TestExecutor.setUpClass (`rwx/presetup_cache.py`) |
| `RWX_LAZY_RUN=1` | run test_lazy_load: `python -c` children get an import hook with the patched sources (`rwx/srchook.py`) |
| `RWX_DIAG_MODE=heavy/blind` | diagnostics: drop unrun heavy tests / print only observed failures on engine commits |

**What practice looks like** (from the diagnostics on the bet version, 40 commits):
16 live commits, 13 static commits (all of them break something: fixture edits), 11 that
break nothing. Since the leader is at 103, roughly 25-30 s of our 129.7 s simulated must be
tests we print on breaking commits that do not actually fail (most likely mid-weight tests
included blind), plus about 15 s CPU.

**How to try an idea safely:**
1. Implement it behind an environment switch in `rwx/selector.py`.
2. `python3 tools/lab/simulate.py RWXblast-radius-sample/sample` must show 0 misses (the
   samples are the real generator; the lab sets are harsher than reality).
3. Build the real image (`tools/lab/build_local.sh` here, or `docker build .`) and run it
   with the scorer flags from the `Makefile` on the samples twice: same output, 0 misses.
4. Branch from `rwx`, add the `ENV` line, push, submit the full hash.

**Ideas not yet tried:** making the slow optimizer tests cheaper to verify (run only the
fixture entries that reach the changed code, like Astra's per-query TPC-DS check);
cutting CPU in the bet's finishing step for large clean commits.

### Nov 22, ~05:30 UTC: TPC fixture runs (`RWX_FIXTURE_RUN=1`)

Practice has 13 fixture-only commits that cost 57.5 s (4.4 s each), against 2.2 s each
for the samples' fixture commits. The only fixtures that can cost much more are
`tpc-h.sql` and `tpc-ds.sql`: their readers include `test_execute_tpch` (3.9 s),
`test_optimized_tpch` (3.5 s) and `test_execute_tpcds` (21.5 s), and fixture-only
commits were never run, so they were printed blind.

Now the session swaps in the edited file's query pairs (`rwx/fixture_run.py`) and runs
TestExecutor's per-query tests on the edited queries only. The other queries are
byte-identical with no code changed, so they pass as at the pinned commit. A pass drops
the test. TestOptimizer readers (`test_tpch`, `test_tpcds`) stay printed. Lab:
`tpcfix` 219.4 -> 196.1 s, `tpcfix2` (sample-style edits) 196.2 -> 190.2 s. Both are
exact (equal to the oracle) with 0 misses. Sample output is byte-identical.

Other things measured this round:
- gc tuning in sessions (off, high thresholds): no change, within noise.
- Parent CPU is ~1.8 s: test-module imports 0.56 s, the TestExecutor presetup 0.74 s
  (TPC-DS tables 0.59 s of it), map + analysis ~0.2 s. A build-time pickle of the
  presetup state would load in 0.14 s (saves ~0.6 s), but it needs a manifest of the
  code setUpClass runs (as warm keys). Not built yet.
- The largest sample session is 2haz (6 s): `is` -> `==` on an enum, which breaks
  nothing. The bet's finishing step verifies its 64 runnable candidates.
