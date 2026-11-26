#!/usr/bin/env bash
# Full test suite: security, limits and agent-budget tests against a small fixture database (no downloads).
# Runs in the `test` image target with networking off, so nothing here can reach the internet or the host.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -q --target test -t wnyc/test . >/dev/null
exec docker run --rm --network none --read-only --tmpfs /tmp:size=256m --cap-drop ALL \
  --security-opt no-new-privileges:true wnyc/test .venv/bin/python -m pytest -q -p no:cacheprovider tests "$@"
