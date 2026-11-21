# Blast Radius: test selection for sqlglot patches

Entry for the RWX Blast Radius track. For each patch against sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, the image prints which tests to run
and how to split them over up to 8 workers.

## How it works

1. **Coverage map, built once from the pinned commit** (`rwx/maps/cov.pkl.z`).
   Every test ran alone in a fresh process; we recorded which lines of which code
   objects it executed, which data files it opened, which sqlglot classes it used
   (as `self`/`cls`, including while its test module imported), the class
   hierarchy, a static index of where names and `Class.attr` pairs are referenced,
   and calibrated CPU costs per test and per test-class setup.
2. **Static analysis of each patch** (`rwx/patchlib.py`, `rwx/selector.py`).
   The patch is applied in memory; comment/whitespace-only edits (identical AST)
   select nothing; changed statements are located and mapped to the tests that
   executed them. Class- and module-body edits map to the tests that used the
   class and ran code reading the changed names. Fixture edits map to the tests
   that opened the file.
3. **Runtime verification** (`rwx/engine.py`). A warm process imports sqlglot and
   its tests once; per commit it forks a child that applies the patch (swapping
   function bytecode, or re-importing patched modules when import-time code
   changed) and runs candidate tests with `unittest`. Cheap candidates that pass
   are dropped. Larger batches get two focused probes. If both pass and clear
   an expensive test, the selector checks the remaining runnable tests when
   their estimated CPU fits within the worker time that a clean run could save.
   A subsequent failure restores an omitted giant from the same test file or
   one with no runnable relatives. When a patch can affect a
   nested function factory, the child reloads the patched module tree.
   A setup or teardown error keeps every requested test in the affected class.
   The image build records the source files and class attributes used by the
   pinned executor fixture. For eligible function-body patches, the child
   opens the DuckDB connections and loads the CSVs on the first fixture read.
   Fixture-dependent patches use the original eager setup.
4. **Packing**: tests are split over the number of workers that minimizes the
   busiest worker plus 2 s per worker.

`tools/` holds the build-time tooling (coverage and class collection, map
builder) and `tools/lab/` the validation lab (synthetic patch generator, ground
truth runner, policy simulator). None of the lab output ships.

## Open-source code used

- [sqlglot](https://github.com/tobymao/sqlglot) (MIT) at the pinned commit:
  the system under test, downloaded in the Dockerfile.
- Python 3.13 standard library.
- The packages in `rwx/requirements.lock` (the environment the commits were
  labeled in: pytest, duckdb, pandas, numpy and their dependencies), installed so
  sqlglot's tests can run inside the image.

Everything else here was written during the event.

## License

MIT, see [LICENSE](LICENSE).
