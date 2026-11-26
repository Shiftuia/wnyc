#!/usr/bin/env bash
# Integrity check of the data volume against the manifest written by scripts/bootstrap.sh.
set -euo pipefail
cd "$(dirname "$0")/../data"
[ -f MANIFEST.sha256 ] || { echo "verify: data/MANIFEST.sha256 missing: run the bootstrap" >&2; exit 1; }
sha256sum --quiet -c MANIFEST.sha256 && echo "verify: OK ($(wc -l < MANIFEST.sha256) files)"
