# Research notes: where our time goes (Nov 22)

Everything here was measured on the real image (`tools/lab/build_local.sh`, scorer
flags) or with `tools/lab/simulate.py`, which replays the selector's real policy. The
samples come from the real generator; the lab sets (`mut1`, `mut2`) are my own,
harsher generator. See HANDOFF.md for the branches and how to run things.

## The score today

Practice (40 commits): **141.0** = 125.762 s simulated + about 15.2 s CPU, 0 misses
(`2ac5328`). The leader is at 103.

Practice decomposition (diagnostic branches): 16 live commits (something fails and the
engine ran them) cost about 66 s now; 13 fixture-only commits cost 57.5 s; the 11 others
cost about 2.4 s.

## Findings

### 1. The packing is already optimal
`pack()` is greedy longest-first over 1 to 8 workers. Against a lower bound
(`max(longest test, total / k) + 2 s * k`) on 280 real output sets, the total gap is
**0.02 s**. Nothing to win in packing.

### 2. On the samples we are at the floor; only CPU is left there
Samples: 94.581 s simulated against an oracle (exact failing sets, packed) of 94.4 s.
The rest of the sample score is CPU.

### 3. Where the CPU goes (samples, about 15 s)
| Part | CPU |
|---|---|
| Parent: test-module imports | 0.56 s |
| Parent: TestExecutor setUpClass (TPC-DS tables 0.59 s of it) | 0.74 s |
| Parent: map load, analysis, gc | about 0.3 s |
| Sessions: `2haz` (`is` -> `==` on an enum, breaks nothing; verifying 64 tests) | 6.0 s |
| Sessions: next six commits | about 1 s each |

- **Done:** the setUpClass state is now pickled at build time (`rwx/presetup_cache.py`).
  It loads in about 0.14 s and is byte-identical to a live setUpClass. Alternating
  old/new images on the samples: 16.2 / 16.7 / 15.9 s against 14.9 / 15.5 / 13.9 s CPU.
- **No effect:** gc tuning in the sessions (off, or high thresholds): within noise.
- The `2haz` 6 s pays for the bet's finishing step. That step is what keeps a clean bet
  from missing (Astra's set has a commit where it matters). Lowering it brings that miss
  back.

### 4. Lab waste, by kind (571 lab commits)
Simulated 2216.8 s against an oracle of 1825.1 s: 391.7 s of waste, almost all of it on
commits where something fails.

| Kind of test printed without failing | Commits | Waste |
|---|---|---|
| runnable tests left unrun (too many to afford) | 115 | 209.3 s |
| heavy tests (CPU > 2.5 s, never run) | 20 | 124.9 s |
| `test_lazy_load` (could not be run) | 29 + 6 clean | 49.0 s |
| readers of an edited data file | 9 | 18.4 s |

- **Runnable tests left unrun:** on every one of the 115 commits, running them would cost
  more CPU than it saves (2610 s of CPU to save 209 s). The current "verify when
  affordable" rule is already right; tests cost about as much CPU as recorded time.
- **Failures do not cluster by file.** On live lab commits with more than 50 candidates,
  only 26% fail in a single test file, so "verify the failing file, bet on the rest" would
  miss too often.
- **Heavy tests:** the biggest are `test_max_nodes` (28.8 s recorded, 34 s CPU),
  `test_execute_tpcds` (21.5 s / 36 s) and `test_generator_memory` (6.7 s / 3.0 s). On the
  samples, `test_max_nodes` is never a candidate. Whether they cost us on practice is
  what the `diag-heavy` branch measures.
- **Done:** `test_lazy_load` now runs (`rwx/srchook.py`). Engine children rewrite
  `python -c CODE` to load an import hook that serves the patched sources. Checked against
  pytest on patched copies of the tree: 7 of 7 agree (`tools/lab/lazy_check.py`). On the
  TPC-DS lab set it drops the test on 7 commits where it passes (-16.6 s), with no other
  change.

### 5. Fixture-only commits
The only fixtures whose readers cost much are `tpc-h.sql` and `tpc-ds.sql`
(`test_execute_tpcds` is 21.5 s). `RWX_FIXTURE_RUN=1` runs TestExecutor's per-query tests
on just the edited queries: it is exact on both TPC lab sets. Practice did not change
(125.762 s before and after), so its 13 fixture commits are already at the floor. Most
likely one of them is a `tpc-ds.sql` query edit that really breaks `test_execute_tpcds`
(25.45 s; 9 of 10 such lab edits do).

## What the practice gap must be
If the leader's 103 includes even 1 s of CPU, the practice floor is at most about 102 s
simulated, so we print at least about 24 s of tests that do not fail. The samples show
none of that waste, so it sits in commit types the samples do not have. Candidates, in
order of likelihood:
1. a live commit that keeps a giant (`test_max_nodes`, `test_execute_tpcds`) because
   another test in its file failed;
2. a live commit with hundreds of candidates and a few failures (we print them all);
3. `test_lazy_load` on module-level edits (fixed in `fast-v1`).

`diag-heavy` (`9fac365`) prints the 141.0 output minus every unrun heavy test on engine
commits. From its result:
- `misses` = commits where a heavy test really fails;
- `141.0 - (score - 300 * misses)` = what heavy tests cost us on practice.

A large drop with few misses means stand-ins for the heavy tests are the next win.

**Result:** 397.708 s with 1 miss, so 97.7 s simulated without the penalty, against
125.762 s: unrun heavy tests cost 28 s on practice. One commit really needs one of them,
so the true waste is between about 3 s and 26 s. `fast-v2` adds the `test_max_nodes`
stand-in (`rwx/proxies.py`, checked on 66 lab commits) and a larger budget for the
TPC-DS affected-query check. The three narrower diagnostics split the 28 s by test.

## Ideas not yet built
- A cheap stand-in for `test_max_nodes`: the same parse checks on 100 columns instead of
  10,000 (about 0.3 s). Only worth building if `diag-heavy` shows heavy tests cost us.
- Per-query subsets for the optimizer's `test_tpcds` (8 s CPU), the way Astra's TPC-DS
  executor check works.
- Running `test_generator_memory` (3 s CPU for 6.7 s recorded) on live commits only.
