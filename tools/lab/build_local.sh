#!/bin/sh
# Local build of ./Dockerfile behind this sandbox's TLS-intercepting proxy:
# injects the proxy CA for pip and swaps the sqlglot download for a local checkout.
set -e
cd "$(dirname "$0")/../.."
cp /root/.ccr/ca-bundle.crt /tmp/claude-0/ca.crt
sed 's#^RUN pip install --no-cache-dir -r /app/requirements.lock#COPY tools/lab/ca.crt /tmp/ca.crt\nRUN PIP_CERT=/tmp/ca.crt pip install --no-cache-dir -r /app/requirements.lock#' Dockerfile > /tmp/claude-0/Dockerfile.local
cp /tmp/claude-0/ca.crt tools/lab/ca.crt
docker build --network host --build-context sqlglotsrc=/home/user/lab/srcctx -f /tmp/claude-0/Dockerfile.local -t blast-radius-entry . "$@"
rm -f tools/lab/ca.crt
