#!/usr/bin/env bash
# Start Wheelchair NYC on http://127.0.0.1:8765
# First run downloads the public datasets (~0.7 GB, mostly one month of TLC trips) and builds the data layer.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -c "import duckdb, fastapi, anthropic, matplotlib, certifi" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt
[ -f data/fhvhv_2026-09.parquet ] || bash scripts/fetch.sh            # core datasets
[ -d data/extra ] || bash scripts/fetch_extra.sh                      # enrichment + subway timetable + live elevator status
[ -f data/basemap/style.json ] || .venv/bin/python scripts/fetch_basemap.py || true   # street map for offline use
[ -f data/prepared.duckdb ] || .venv/bin/python app/build_data.py     # ~20 s (first run ~3 min: elevation samples)
exec .venv/bin/python app/server.py
