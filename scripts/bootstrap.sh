#!/usr/bin/env bash
# One-shot data bootstrap for the container stack (compose service `bootstrap`, profile `bootstrap`).
# Downloads the public datasets into the data volume, builds data/prepared.duckdb and writes
# data/MANIFEST.sha256 over every file the server reads. Idempotent: existing downloads are kept.
#   docker compose --profile bootstrap run --rm bootstrap            # first build / resume
#   docker compose --profile bootstrap run --rm bootstrap refresh    # re-fetch the live feeds and rebuild
#   docker compose --profile bootstrap run --rm bootstrap verify     # integrity check only
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

case "${1:-build}" in
  verify) exec bash scripts/verify_data.sh ;;
  refresh) rm -rf data/extra data/prepared.duckdb data/MANIFEST.sha256 ;;
  build) ;;
  *) echo "usage: bootstrap.sh [build|refresh|verify]" >&2; exit 2 ;;
esac

[ -f data/fhvhv_2026-09.parquet ] || bash scripts/fetch.sh
[ -d data/extra ] || bash scripts/fetch_extra.sh
[ -f data/basemap/style.json ] || $PY scripts/fetch_basemap.py || echo "bootstrap: some basemap files failed (tolerated, as in run.sh)"
# Build into a temp file and rename only on success: a failed build must not leave a partial database that the
# next run would take as done.
if [ ! -f data/prepared.duckdb ]; then
  rm -f data/prepared.duckdb.tmp data/prepared.duckdb.tmp.wal
  WNYC_BUILD_DB="$PWD/data/prepared.duckdb.tmp" $PY app/build_data.py
  mv data/prepared.duckdb.tmp data/prepared.duckdb
fi
# the served container never writes here: route/geocode/answer caches live in the separate cache volume
cd data
find prepared.duckdb zones.geojson basemap -type f ! -name '*.part' -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256.tmp
mv MANIFEST.sha256.tmp MANIFEST.sha256
echo "bootstrap: $(wc -l < MANIFEST.sha256) files in MANIFEST.sha256, $(du -sh . | cut -f1) in the data volume"
