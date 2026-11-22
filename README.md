# Blast Radius: predicting which tests a code change breaks

Our entry for the **RWX "Blast Radius" track** at Buildathon (November 2026, 438 teams), built by
team **Fenwick Fencers**.

Continuous integration usually runs every test on every change. This project predicts which
tests a change will actually break, and runs only those, spread across parallel workers.
It's like a doctor who reads the symptoms and orders the two tests that matter instead of
every test in the hospital.

## The challenge

The code under test is [sqlglot](https://github.com/tobymao/sqlglot), an open-source SQL
library with about 1,620 tests, pinned at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.
For each hidden code change (a "commit"), our program must print which tests to run and how to
split them across up to 8 workers.

Every commit is scored in simulated CI seconds:

- the time of the **busiest worker** (the run finishes when the slowest worker does),
- plus **2 seconds per worker** used (spinning up a machine isn't free),
- plus a **300-second penalty** if any test the change breaks is left out,
- plus the **CPU time our own program spends** deciding.

So it's a three-way trade-off. Print too many tests and CI is slow. Print too few and a
single missed failure costs 300 s. Think too hard and our own CPU time counts against us.
Everything runs in a locked-down container: 2 CPU cores, 4 GB of memory, no network, and a few
minutes for the whole batch of commits.

## How it works

The selector works in three stages, from cheapest to most expensive.

### 1. A map of the codebase, built ahead of time

When the Docker image is built, we run every sqlglot test once against the pinned code and
record what it touches: which lines of which functions it executes, which fixture files it
reads, and how much CPU it costs. We also record finer detail where it pays off, for example
which values each test actually feeds into comparison operators (`<`, `==`, ...), and which of
the 99 TPC-DS benchmark queries reach which functions. All of this ships inside the image as a
compressed map.

### 2. Reading the change

For each commit we apply the patch in memory and compare the code's syntax tree before and
after. Edits that change nothing a test can observe (comments, whitespace, formatting) select
no tests at all. Real edits are traced to the functions, class attributes or fixture files
they touch, and the map gives the tests that reach them. When an edit flips a comparison (say
`<` to `<=`), the recorded values let us keep only the tests that ever hit the case where the
two operators disagree.

### 3. Actually running the cheap tests, very fast

Predictions are good, but proof is better. A background process starts once with sqlglot and
all test modules already loaded. For each commit it creates a copy of itself (a Unix `fork`,
which takes milliseconds) and swaps the changed functions into that copy. The copy then runs
the cheap candidate tests directly. Tests that pass are dropped; tests that fail are printed.
Expensive tests are handled by a cost model that weighs CPU spent checking against CI time
saved, then either checks them, prints them, or uses a cheaper stand-in check.

Finally, the printed tests are packed onto workers. Our packing is within 0.02 s of the
mathematical optimum.

## Engineering highlights

- **Fork-based test isolation with hot-swapped code.** Each commit gets a fresh process
  without paying Python and test start-up costs again; patched functions are replaced at the
  bytecode level, or the module is re-imported through a custom import hook when the change
  runs at import time.
- **Build-time caching.** Expensive test-class setup (loading the TPC-H and TPC-DS benchmark
  tables) is captured once as a snapshot, and database connections open only when a test
  actually uses them.
- **Cheap checks for the slowest tests.** The suite has a 21-second TPC-DS test and a
  29-second parser stress test. We built per-query checks, a sampled "evidence gate" for
  TPC-DS, cached database answers for it, and a scaled-down stand-in for the parser test.
- **Tests that launch new Python processes.** An import hook is injected into those child
  processes so they see the patched code too.
- **A testing lab.** A mutation generator produced hundreds of synthetic commits with known
  answers, a simulator replayed our policy offline, and small "diagnostic" submissions
  measured exactly where the remaining cost was on the hidden practice set.

## Results (practice board)

On the 40-commit hidden practice set we went from **285 s on our first submission to
111.2 s**, with no missed commits. On the 30 official sample commits the selector printed
essentially the ideal set: 94.58 s simulated against a theoretical best of 94.4 s.

## What we learned

The last gains on the practice board came from calculated bets, such as "if a quick sample of
tests passes, treat the change as safe". They held on all 70 practice and sample commits we
could see, but our own lab showed they miss a few percent of harder cases, and the hidden
final set included kinds of changes practice didn't have. The takeaway: optimize for the
distribution you'll be judged on, not the scoreboard you can see, and put an explicit price
on rare but expensive failures.

## Where the code is

- `rwx/`: the selector that ships in the image (`selector.py` decides, `engine.py` runs
  tests in forked processes, `patchlib.py` reads patches, plus build-time helpers).
- `tools/`: building the coverage map; `tools/lab/`: the validation lab (mutation
  generator, ground-truth runner, simulator). None of the lab output ships.
- `HANDOFF.md` holds our working notes and measurements from the event (the `opt-*`
  branches have the latest version).
- This branch holds an earlier version of the code. Our final entry is branch
  [`opt-c14`](https://github.com/mshaaf/buildathonnov2026/tree/opt-c14) (commit `b52a7d5`);
  the best practice builds are on the `opt-*` branches.

## Open-source code used

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT) at the pinned commit: the system
  under test, downloaded in the Dockerfile.
- The Python 3.13 standard library.
- The packages in `rwx/requirements.lock` (pytest, DuckDB, pandas, NumPy and their
  dependencies), installed so sqlglot's own tests can run inside the image.

Everything else here was written during the event.

## License

MIT, see [LICENSE](LICENSE).
