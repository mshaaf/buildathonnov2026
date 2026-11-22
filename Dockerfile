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
COPY --from=sqlglotsrc /sqlglot /sqlglot
RUN python -m compileall -q /sqlglot/sqlglot /sqlglot/tests > /dev/null || true
COPY rwx /app/rwx
RUN python -m compileall -q /app/rwx > /dev/null && chmod -R a+rX /app /sqlglot
ENV RWX_SETTLE_RATIO=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/sqlglot SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN python /app/rwx/executor_fixture.py /sqlglot && chmod a+r /sqlglot/.rwx-executor.json
RUN python /app/rwx/tokenizer_coverage.py /sqlglot && chmod a+r /sqlglot/.rwx-tokenizer.json
RUN python /app/rwx/executor_cases.py /sqlglot && chmod a+r /sqlglot/.rwx-executor-cases.json
CMD ["python", "/app/rwx/selector.py"]
