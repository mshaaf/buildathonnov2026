# Blast-radius selector. Built on the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS sqlglotsrc
# sqlglot at the pinned commit, from sqlglot's own repository.
RUN python - <<'PY'
import io, tarfile, urllib.request, shutil, os
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
for attempt in range(5):
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
        break
    except Exception as e:
        print("download failed", attempt, e)
        if attempt == 4:
            raise
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    tf.extractall("/tmp/src", filter="data")
shutil.move(f"/tmp/src/sqlglot-{sha}", "/sqlglot")
assert os.path.exists("/sqlglot/sqlglot/parser.py")
PY

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The packages the commits were labeled with: the runtime engine imports sqlglot's tests.
COPY rwx/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# The slim image ships most of the standard library without bytecode: without this every
# run (and every fresh interpreter test_lazy_load starts) recompiles it, about 0.2 s each.
RUN python -m compileall -q -j 0 /usr/local/lib/python3.13 > /dev/null 2>&1 || true
COPY --from=sqlglotsrc /sqlglot /sqlglot
RUN python -m compileall -q /sqlglot/sqlglot /sqlglot/tests > /dev/null || true
COPY rwx /app/rwx
RUN python -m compileall -q /app/rwx > /dev/null && chmod -R a+rX /app /sqlglot
ENV RWX_TPCDS_EXPECTED=1 RWX_TPCDS_BLIND_GUARD=0 RWX_TPCDS_BLIND_MS=1500 RWX_MAXNODES_PROXY=1 RWX_BET_CLEAN=1 RWX_NARROW=1 RWX_NARROW_SKIP_MS=1000 RWX_NARROW_CHUNKS=0 RWX_FIXTURE_RUN=1 RWX_PRESETUP_CACHE=1 RWX_LAZY_RUN=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/sqlglot SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN python /app/rwx/executor_fixture.py /sqlglot && chmod a+r /sqlglot/.rwx-executor.json
RUN python /app/rwx/tokenizer_coverage.py /sqlglot && chmod a+r /sqlglot/.rwx-tokenizer.json
RUN python /app/rwx/executor_cases.py /sqlglot && chmod a+r /sqlglot/.rwx-executor-cases.json
RUN python /app/rwx/tpcds_expected.py /sqlglot > /dev/null 2>&1; chmod a+r /sqlglot/.rwx-tpcds-expected.pkl 2>/dev/null; true
# optional: without the snapshot the engine runs setUpClass itself
RUN python /app/rwx/presetup_cache.py /sqlglot > /dev/null 2>&1; chmod a+r /sqlglot/.rwx-presetup.pkl 2>/dev/null; true
CMD ["python", "/app/rwx/selector.py"]
