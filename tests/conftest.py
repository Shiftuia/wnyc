"""Test setup: a small fixture DuckDB in a temp dir (no downloads, no real data), caches in a temp dir,
no API credential. Runs inside the `test` image target, which has the spatial extension baked in."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import duckdb
import pytest

TMP = Path(tempfile.mkdtemp(prefix="wnyc-test-"))
os.environ["WNYC_CACHE_DIR"] = str(TMP / "cache")
os.environ["WNYC_TRUST_CLIENT_HEADER"] = "1"
os.environ["WNYC_BASEMAP_UPSTREAM"] = "0"
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

DB = TMP / "prepared.duckdb"
c = duckdb.connect(str(DB))
c.execute("INSTALL spatial; LOAD spatial;")
c.execute("""CREATE TABLE zones AS SELECT * FROM (VALUES
    (1, 'Alphabet City', 'Manhattan', 'Yellow Zone', ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
    (2, 'Astoria', 'Queens', 'Boro Zone', ST_GeomFromText('POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))')))
    t(zone_id, zone_name, borough, service_zone, geom_ft)""")
c.execute("""CREATE TABLE zone_features AS SELECT * FROM (VALUES
    (1, 'Alphabet City', 'Manhattan', 3400.0, 12000, 0.97),
    (2, 'Astoria', 'Queens', 2900.0, 30000, 0.91)) t(zone_id, zone_name, borough, rent_zori, mf_units, ada_elevator_uptime)""")
c.execute("CREATE TABLE stations AS SELECT range AS station_id, 'Stop ' || range AS stop_name, 1 AS ada FROM range(200)")
c.close()

import engine  # noqa: E402

engine.DB_PATH = DB
engine._con = None
engine.allowed_tables.cache_clear()


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    import agent
    monkeypatch.setattr(agent, "CACHE_DIR", tmp_path)
    return tmp_path
