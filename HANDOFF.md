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

## 13. Merged line `opt-merge` (Nov 22, ~06:00 EST): team rwx + the `opt` work

`opt-merge` = `rwx` (through af47052) merged with the `opt` branch. Submission builds are
`opt-merge` plus one `ENV` line in the Dockerfile, like the `rwx` variants.

**Board (practice, 40 commits), all 0 misses:**

| Commit (branch) | Simulated | CPU | Score | What |
|---|---|---|---|---|
| `d17c048` (opt-merge-bet, older) | 123.394 | 7.3 | **130.7** | team switches (BET_CLEAN, NARROW 1000, FIXTURE_RUN, PRESETUP_CACHE, LAZY_RUN) on the merged code |
| `9168129` (exp-settle-deep) | 121.024 | 11.4 | 132.5 | same + deep settling: not worth it |
| `e715cb2` (opt-merge-narrow, older) | 123.394 | 32.8 | 156.2 | no BET_CLEAN: clean commits verified instead of bet |
| `132624f` (diag-blind-merge) | 2490.2 (8 misses) | 7.2 | diagnostic | observed failures only: 90.2 s + 8 of 16 live commits need more |

Scored since this section was written: `4acf4a6`, `fb9f612`, `e715cb2` (see section 15).
Still not scored: `ca468a7` (opt-merge-bet-probe = bet2 + PROBE_MS 1000 / PROBE2_MS 2000),
`8a119cc` (opt-merge-narrow = no BET_CLEAN).

**What `opt` added to the team line**
- Soundness: one-line lambdas/defs had no coverage (LINE events never fire on a code
  object's first line): the map now has code-object entries (`tools/lab/startrun.py`,
  `merge_starts.py`); unknown code objects fall back to the creating statement or importers.
  Import-time statements that may raise are checked by importing the patched module in the
  session (failure selects every importer). File-level and malformed patches no longer
  crash or select blindly. Hung tests: progress watchdog. Hard wall-clock guard.
- Precision: operand-relation profiles for comparison sites (`cmp_sites` in the map, fresh
  and shared-process runs unioned; `tools/lab/cmprun.py`, `merge_cmp.py`), also inside
  multi-change patches. `RWX_PROFILE_SENTINEL` (on): when a profile narrows to a non-empty
  set, dropped candidates from the same classes/files run after the infected tests in the
  same session; a failure restores the dropped set (cross-test pollution, found on an
  and/or flip). Flags, off by default: `RWX_REF_LINES`, `RWX_META_STATIC` (class attributes),
  `RWX_TPCDS_VERIFY_MS`/`RWX_TPCDS_SETTLE`. and/or profiles exist in the lab but are OFF
  (pollution violation on `boolx/b009`).
- Settle step (`RWX_SETTLE_RATIO`, default 0.5): runs printed-unverified tests when cheaper.
- **Rebuilding the map:** `tools/build_map.py` alone drops the supplements. Re-run
  `merge_starts.py` (data: lab `scratch-robust/starts`) and `merge_cmp.py`
  (`scratch-robust/cmp,scratch-robust/cmp_shared`) on top.

**Measured risk of the bets** (lab corpus: samples + 375 synthetic commits with engine truth,
8 of 8 full-suite pytest spot checks agree): `RWX_BET_CLEAN` misses 20 of 405 commits (when
it bets after running under 20% of the candidates it is wrong 20 times out of 25);
`RWX_NARROW` (any threshold) 0 misses. Practice and samples: 0 misses in 70 commits. So the
synthetic mutants differ from the organizers' (theirs break broadly or not at all), and the
final adds kinds not in practice. Larger probes (1000/2000 ms) halve the lab misses for
about +0.25 s est CPU per commit.

## 14. `opt-merge-fast` (Nov 22, ~12:00 UTC): CPU, and what is left

- `opt-merge-fast` = `opt-merge-proxy` + two output-neutral CPU cuts:
  - the stdlib is precompiled in the image (the slim base ships 163 of about 640 `.pyc`,
    so every run recompiled 67 modules, about 0.2 s, and so did every `python -c` that
    test_lazy_load starts);
  - `_undecodable` uses a regex instead of a Python loop over every character (0.13 s on
    the samples).
  Parent CPU on the samples 1.75 -> 1.57 s. Output byte-identical to `opt-merge-proxy` on
  the samples (3 runs), tpcfix2, astra6 and tpcdsset.
- Board: `fast-v2` (`cdbef83`, the rwx line + test_max_nodes stand-in + live slow-test runs +
  TPC-DS check budget) scored 142.9 = 123.394 simulated (same as `d17c048`) + 19.5 CPU.
  The rwx line spends about twice the merged line's CPU (samples, same machine: 18.4 vs
  8.5 s): the merged line's comparison profiles leave `2haz`, `66ks` and `2qn5` with no
  candidates.
- The rwx line and `d17c048` reach the same 123.394, with and without the stand-in.
  `test_lazy_load` is the common part (both have `RWX_LAZY_RUN`), so the stand-in and the
  TPC-DS budget saved nothing on practice. `diag-heavy` (28 s with 1 miss) is then mostly
  one commit that really needs its slow tests plus test_lazy_load. On the samples every
  live commit with TPC-DS candidates really fails both TPC-DS tests. (Superseded by
  section 15: one slow test, `test_execute_tpcds`, is about 21.5 s of the remaining
  practice cost; `test_max_nodes` is not.)
- Kernel time is 8% of CPU (copy-on-write after fork); gc in sessions makes no
  difference. The remaining cost is runnable tests printed unverified on live commits;
  running them costs more CPU than it saves (RESEARCH.md on the rwx line). Precision (the
  profiles) is the lever.

## 15. Overnight board results (Nov 22) and the final-entry warning

Practice board, 40 commits, 3 identical runs each. Format: simulated s / CPU s / misses.

**WARNING: do not let `3b7f152` be the final entry.** It scored 109.6, so the site marks it
"Entered in the final (your best score)". It is a diagnostic: it leaves `test_execute_tpcds`
out whenever that test was not observed failing. On the 30 official samples it misses 2
commits (`2jrq`, `3utz`, where the test really fails): 661.1 s with 2 misses. Before the
cutoff, either a safe build must beat 130.5 on the board, or a safe entry must be picked by
hand (`00762c6` at 130.5 or `4acf4a6` at 131.1). The site takes the best practice score
unless another is chosen. Diagnostics are never final entries.

### Safe candidates

| Commit (branch) | Simulated | CPU | Misses | Score | Notes |
|---|---|---|---|---|---|
| `00762c6` (opt-merge-fast) | 123.394 | 7.1 | 0 | **130.5** | has `RWX_MAXNODES_PROXY=1`; best safe |
| `d17c048` (opt-merge-bet, old) | 123.394 | 7.3 | 0 | 130.7 | merged line + BET_CLEAN, NARROW 1000, CHUNKS 0, FIXTURE_RUN, PRESETUP_CACHE, LAZY_RUN |
| `e222881` (opt-merge-tv40) | 123.394 | 7.4 | 0 | 130.8 | `RWX_TPCDS_VERIFY_MS=40000 RWX_TPCDS_SETTLE=1`: no change on practice |
| `4acf4a6` (opt-merge-bet) | 123.394 | 7.7 | 0 | 131.1 | same switches on newer opt-merge (pollution sentinels, multi-change comparison narrowing, fresh+shared profiles) |
| `9168129` (exp-settle-deep) | 121.024 | 11.4 | 0 | 132.5 | `RWX_SETTLE_RATIO=0.1`, MAX_MS 9000: not worth it |
| `fb9f612` (opt-merge-bet2) | 123.394 | 11.6 | 0 | 135.0 | + `RWX_REF_LINES`, `RWX_META_STATIC`: about 4 s CPU, saves nothing on practice; leave off |
| `cdbef83` (fast-v2, rwx line) | 123.394 | 19.5 | 0 | 142.9 | stand-in changes nothing on practice |
| `e715cb2` (no BET_CLEAN, older) | 123.394 | 32.8 | 0 | 156.2 | verifying instead of betting costs about 25 s CPU for the same simulated time |

Every safe variant sits at 123.394 simulated except the deep-settle one. Differences
between them are CPU only.

### Diagnostics (never final)

| Commit | Variant | Simulated | CPU | Misses | Result |
|---|---|---|---|---|---|
| `132624f` | blind: only observed failures printed on engine-run commits | 2490.231 | 7.2 | 8 | 90.2 s + 8 misses |
| `8c2bacc` | heavy: unrun slow tests left out on engine-run commits | 397.708 | 10 | 1 | 97.7 s + 1 miss |
| `3b7f152` (diag-heavy-merge) | `RWX_DIAG_MODE=heavy RWX_DIAG_HEAVY_IDS=test_execute_tpcds`: only unrun `test_execute_tpcds` left out | 101.94 | 7.7 | 0 | 109.6 (but 2 misses on samples, see warning) |

### What the board says

Exactly one kind of waste is left on practice, about 21.5 s (123.394 vs 101.94):
`test_execute_tpcds` is printed unrun on some engine-run commit(s) where it passes, and the
per-query check (`RWX_FIXTURE_RUN`, `RWX_TPCDS_VERIFY_MS`) does not apply there or does not
clear it. Uncapped per-query verification (`e222881`) changed nothing, so the check is
"not applicable" on those commits, not "too small a budget". The `test_max_nodes` stand-in
(`00762c6`, `cdbef83`) also changes nothing on practice.

### In progress (lead's session, not merged)

An evidence rule for the "per-query check not applicable" case: run a cheap subset of
TPC-DS queries in the session as a gate before leaving `test_execute_tpcds` out. It must
keep TPC-DS on `2jrq` and `3utz` (samples) and be validated on the lab corpus before any
submission.

### What the `opt` work added (short; details in section 13)

Coverage for one-line lambdas/defs (code-object map entries), runtime import check, robust
patch parsing and file-level rules, comparison-site operand profiles (fresh + shared-process
union, also multi-change patches), pollution sentinels (`RWX_PROFILE_SENTINEL`), settle
step, progress watchdog, hard deadline. and/or profiles exist in the lab but are OFF:
cross-test pollution on lab commit `boolx/b009` (a test that is not itself affected fails
after an affected test ran in the same process).

### Lab findings

- `RWX_BET_CLEAN` misses 20 of 405 lab commits (synthetic mutants); 0 misses on the 70 real
  practice + sample commits. `RWX_NARROW` misses 0.
- The engine's truth runs wrongly mark `test_max_nodes` as failing in re-import sessions
  (the child dies on the 1.5 GB test). Plain pytest passes it on 5 of 5 such commits. With
  those corrected, the stand-in agrees with real outcomes on 84 lab commits.

### Operations

- Submissions: 4 per hour, shared by the whole team. Always paste the full commit hash.
- Ali's session submits through a Codex agent driving the browser; it only works while the
  Mac screen is unlocked.
- Cutoff: Sunday 11:00 AM EST.

## 16. Sunday morning (Nov 22, from 08:15 EST): the TPC-DS gate, live status

Kept current by Ali's session through the morning. Newest entries at the bottom of each list.

**FINAL ENTRY, still open at 09:10 EST:** the page still marks `3b7f152` (109.6, the blind
diagnostic, 2 misses on the 30 samples) as "Entered in the final". Someone has to click
"Pick as our final entry" on a safe build. Best safe candidate right now: **BB-A0EE91C1,
`343cf5a1c8044a1d6c6ff463822e1a4815cfe200`, branch `opt-gate-ng`, 113.3**.

### What the gate is

`RWX_TPCDS_BLIND_MS` (branches `opt-gate`, `opt-gate-ng`, both on top of `00762c6`): when the
policy would print `test_execute_tpcds` unrun and the per-query check does not apply, the
session runs the cheapest TPC-DS queries up to that budget (ms, estimated) through the real
test. A pass leaves the test out; a failure stops at the first failing query and keeps it.
It also covers code + non-TPC-fixture commits and several changed files. When the affected
queries are known but too many to verify, the cheapest affected ones are used instead. It no
longer re-checks commits where the policy had already left TPC-DS out (that cost 3 to 5 s).
Of the 99 queries only 57 are executed by the test (`execute: true`); the other 42 are only
parsed and cost 3 to 15 ms each, so a 500 ms budget is 42 parsed + 1 executed query, and
1500 ms is 42 parsed + about 5 executed.

### Board this morning (practice, 3 runs each, all 0 misses)

| Commit (branch) | Simulated | CPU | Score | What |
|---|---|---|---|---|
| `c356ee4` | 101.94 | 10.3 to 12.1 | 114.0 | gate, 500 ms, no guard, with the re-check on already-dropped commits |
| `2a8de13` | 123.394 | 7.2 to 8.0 | 131.0 | gate kept TPC-DS when any other executor test failed: blocks the practice commit |
| `851cb8d` (opt-gate) | 123.394 | 7.1 to 7.2 | 130.6 | guard at 4 or more other executor failures: still blocks it |
| **`343cf5a` (opt-gate-ng)** | **101.94** | 11.2 to 11.5 | **113.3** | gate, 1500 ms, no guard, no re-check |

What this says about the one practice commit that costs 21.454 s: it prints TPC-DS plus small
tests summing to at most 2 s on one worker; at least 4 other tests of `tests/test_executor.py`
really fail there while TPC-DS passes; the per-query check does not apply to it. The
`diag-tpcds-*` property diagnostics are not needed any more for `execfail` (answer: yes).
One gate pass costs about 4 s of board CPU at 1500 ms (about 2.5x the native Mac figure).
Expected at 500 ms: about 111.5; parse-only (280 ms): about 110.

### Lab evidence for the gate

- Samples: output byte-identical to `00762c6` (94.581 s, 0 misses) for every variant; `2jrq`
  and `3utz` (TPC-DS really fails) keep TPC-DS.
- Real selector end to end over s1, s2, s3, multi, multix, cmpx, boolx (483 commits with
  truth; `scratch-lead/e2e/e2e.sh`, `score.py`): the gate adds no miss, with or without a
  guard, at 500 and 1500 ms. Misses stay at the 28 that `00762c6`'s settings already have
  there (the known `RWX_BET_CLEAN` misses on synthetic commits). No miss involves TPC-DS.
- Lab truth, commits where another executor test fails and TPC-DS is a candidate: TPC-DS
  fails on 27 of 41. Run alone in a fresh session the gate passes on two of those
  (`s1/m41010113`, `s2/m41020010`); in the real session, after the other candidates ran, it
  fails or times out on both and TPC-DS stays. So the gate is evidence, not proof.
- `executor_cases.affected_cases` returns no queries for a code object that is missing from
  the profile (one-line lambdas have no LINE coverage), so the "verify" passes with zero
  queries. `s3/l41030000` (`"GTE"` lambda in `executor/env.py`, TPC-DS really fails) is only
  saved by the import-time rule that prints all importers. Being fixed on `opt-dig2`.

### In progress (not pushed unless listed above)

- `opt-dig2` (agent, local): sound per-query check for one-line code and for several files /
  non-TPC fixture edits (`RWX_TPCDS_WIDE`), to turn the gate's bet into a proof where possible.
- `opt-sim` (agent, local): simulated-time experiments behind switches, all off.
- `opt-final` (local): `opt-gate` + `RWX_FAST_CMP` (faster patch analysis, output identical on
  every corpus, about 0.1 to 0.2 s on the board).
- Being measured: larger probes before the clean bet (`RWX_PROBE_MS=1000 RWX_PROBE2_MS=2000`)
  against the 28 lab misses.
- CPU: an agent measured the samples natively (parent about 0.9 s, sessions about 2.5 s) and
  found nothing output-neutral beyond `RWX_FAST_CMP`.

### Operations

- Submissions are a rolling 4 per hour. Ali's session used 4 between 8:24 and 8:44; slots
  free up at 9:24, 9:31, 9:37 and 9:44. Say so in the team chat before using one.
- Ali's lab is on his Mac (`blast-lab/`); the previous lead session was lost in a reboot at
  about 08:08 EST and this section restarts from its files.

### Update 09:10 EST

- Board: `851cb8d` (guard at 4+) scored 130.6 = 123.394 simulated, so at least 4 other executor
  tests fail on the practice commit while TPC-DS passes; no guard of that kind keeps the gain.
  `343cf5a` (no guard, 1500 ms) scored **113.3** = 101.94 + 11.2 to 11.5 CPU, 0 misses.
- A failing gate costs as much as a passing one: the test executes every selected query before
  it compares any result. Native Mac, one gate run: 280 ms budget (42 parsed, none executed)
  0.26 s; 500 ms 0.75 to 0.93 s; 1500 ms 1.2 to 1.5 s. Board CPU is about 2.7x that.
- Larger probes before the clean bet, real selector over the 483 lab commits (misses / native
  session CPU): 150/250 ms 28 / 231 s; 400/800 21 / 234 s; **1000/2000 18 / 251 s**;
  2000/4000 16 / 254 s but one new miss (`boolx/b005`: with the bigger first probe failing,
  the narrow rule's partial path drops TPC-DS, which really fails). Every commit that
  1000/2000 fixes is one where the larger probe saw a failing test; no new misses. On the 30
  samples all sizes give 94.581 s and 0 misses, and cost about 0.3 s of native CPU.
- and/or value profiles (`RWX_BOOL_PROFILE=1`, side file `rwx/maps/bool.pkl.z`, agent branch
  `opt-sim` 03892b2): real selector, misses 28 -> 24, no new ones (boolx b023, b024, b025,
  b035: narrowed sets are verified in full instead of bet). Narrows 33 of 513 commits with
  truth, drops a truly failing test on none. Samples byte-identical.
- Simulated time outside TPC-DS is at the floor on sample-like commits (agent report): packing
  is within 0.07 s of optimal over 156 distinct sets; verifying more on live commits costs
  more CPU than it saves; mid-weight optimizer tests printed unrun fail more often than they
  pass. CPU: nothing output-neutral beyond `RWX_FAST_CMP` (0.1 to 0.2 s), which is NOT in the
  candidates below (new code, tiny gain).
- **Candidates for the 9:24 and 9:31 slots** (local until submitted):
  - C1 `d9cad54` (branch `opt-c1`): `343cf5a` + settings only: `RWX_PROBE_MS=1000
    RWX_PROBE2_MS=2000 RWX_TPCDS_BLIND_MS=500`. Samples 94.581 s, 0 misses.
  - C2 `5a4ad21` (branch `opt-c2`): C1 + `RWX_BOOL_PROFILE=1` (45 lines of loader + 24 KB
    data). Samples identical to C1.
  Expected on practice: 101.94 simulated, CPU about 10 to 11, so about 112.
- Still running: `opt-dig2` (sound per-query TPC-DS check for one-line code, several files,
  non-TPC fixture edits); an agent classifying the remaining lab misses; a cloud code review
  of `343cf5a` against `main`.
- Note for local runs on zsh: `env $FLAGS cmd` does not split `$FLAGS`; use bash
  (`blast-lab/scratch-lead/srun.sh`, `e2e/e2e.sh`). Ali's session's first sample checks this
  morning ran with default flags because of this and were redone.
